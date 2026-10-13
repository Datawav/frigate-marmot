import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from frigate_marmot.config import Config, ConfigError, npub_hex, write_json
from frigate_marmot.frigate import Frigate, FrigateError, coverage
from frigate_marmot.marmot import Marmot, WnError
from frigate_marmot.planner import ingest
from frigate_marmot.runner import MQTT, reconcile


NPUB = "npub1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqzqujme" # synthetic all-zero public bytes


def test_npub_checksum():
    assert len(npub_hex(NPUB)) == 64
    with pytest.raises(ConfigError):
        npub_hex(NPUB[:-1] + "q")
    with pytest.raises(ConfigError):
        npub_hex("nsec" + NPUB[4:])


def test_config_permissions_and_binary_provenance(config):
    config.path.chmod(0o644)
    with pytest.raises(ConfigError):
        Config.load(config.path)
    config.path.chmod(0o600)
    Path(config.data["marmot"]["wn"]).write_text("tampered")
    with pytest.raises(ConfigError):
        Config.load(config.path)


def test_destination_gates_changed_config(config):
    write_json(config.destination_path, {"account": "a" * 64, "group": "b" * 64,
                                       "config_sha256": hashlib.sha256(config.path.read_bytes()).hexdigest()})
    assert config.destination()["group"] == "b" * 64
    with config.path.open("a") as f:
        f.write("\n# owner changed configuration\n")
    with pytest.raises(ConfigError):
        config.destination()


def test_profile_read_only(config):
    path = Path(config.data["health"]["profile_file"])
    original = path.read_bytes()
    assert config.profile() == "Disarmed"
    assert path.read_bytes() == original


def test_wn_exact_command_and_upload_receipt(config):
    calls = []
    ref = {"plaintext_sha256": "a" * 64, "ciphertext_sha256": "b" * 64,
           "locators": [{"kind": "url", "value": "https://synthetic.invalid/blob"}], "source_epoch": 2}

    def fake(args, **kw):
        calls.append(args)
        assert "WN_HOME" not in kw["env"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"attachments": [{"media": ref}]}}))

    m = Marmot(config, "c" * 64, "d" * 64, run=fake)
    assert m.upload(Path("synthetic.jpg")) == ref
    assert calls[0][-4:] == ["media", "upload", "d" * 64, "synthetic.jpg"]
    assert calls[0].index("--account") < calls[0].index("media")


def test_wn_send_marker_readback_requires_own_group_and_sender(config):
    def fake(args, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"messages": [
            {"from": "a" * 64, "group_id": "b" * 64, "plaintext": "[fm:test]", "message_id": "c" * 64}]}}))
    m = Marmot(config, "a" * 64, "b" * 64, run=fake)
    assert m.find_send("[fm:test]")["published"] is None
    m.account = "d" * 64
    assert m.find_send("[fm:test]") is None


@pytest.mark.parametrize("effect", [False, True])
def test_cli_timeout_classifies_uncertainty(config, effect):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic", 1)
    with pytest.raises(WnError) as e:
        Marmot(config, run=timeout).call("synthetic", effect=effect)
    assert e.value.uncertain is effect


def test_enqueue_receipt_without_publish_is_accepted(config):
    def fake(*args, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"message_ids": ["a" * 64], "published": 0}}))
    assert Marmot(config, "b" * 64, "c" * 64, run=fake).send("synthetic")["published"] == 0


def test_incomplete_recordings_not_exported():
    assert coverage([{"start_time": 90, "end_time": 200}], 100, 180)
    assert not coverage([{"start_time": 90, "end_time": 120}, {"start_time": 125, "end_time": 200}], 100, 180)
    assert not coverage([], 100, 180)


def test_clip_requests_complete_segments_for_precise_local_trim(tmp_path):
    f = Frigate({"url": "https://synthetic.invalid"})
    requests = []
    f.get = lambda *a, **k: [{"start_time": 90.2, "end_time": 110.2}, {"start_time": 110.2, "end_time": 130.2}]
    f.download = lambda path, target: requests.append(path)
    assert f.clip("indoor", 100.7, 120.1, tmp_path / "synthetic.mp4") == pytest.approx(10.5)
    assert requests == ["/indoor/start/90.2/end/130.2/clip.mp4"]


def test_read_only_frigate_never_follows_redirect_or_proxy():
    f = Frigate({"url": "https://synthetic.invalid"})
    assert not f.session.trust_env
    calls = []

    class Response:
        status_code = 302
        def close(self):
            pass
    def get(*a, **kw):
        calls.append(kw)
        return Response()
    f.session.get = get
    with pytest.raises(FrigateError):
        f.get("/stats")
    assert calls[0]["allow_redirects"] is False


def test_mqtt_commits_before_ack_and_deduplicates(config, store, review):
    broker = MQTT(config)
    acknowledgements = []

    def ack(mid, qos):
        assert store.review(review["id"])
        assert len(store.rows()) == 2
        acknowledgements.append(mid)
    client = SimpleNamespace(ack=ack)
    message = SimpleNamespace(topic="frigate/reviews", payload=json.dumps({"after": review}).encode(), mid=1, qos=1)
    broker.on_message(client, None, message)
    broker.on_message(client, None, message)
    assert acknowledgements == [1, 1]


def test_mqtt_uses_authenticated_persistent_qos1_subscription(config):
    broker = MQTT(config)
    values = []
    def subscribe(x):
        values.extend(x)
        return 0, 7
    client = SimpleNamespace(subscribe=subscribe)
    broker.on_connect(client, None, None, 0, None)
    assert values == [("frigate/reviews", 1), ("frigate/tracked_object_update", 1), ("frigate/available", 1)]
    assert not broker.connected
    broker.on_subscribe(client, None, 7, [1, 1, 1], None)
    assert broker.connected


def test_subscription_denial_is_not_healthy(config):
    broker = MQTT(config)
    broker.subscription_mid = 7
    broker.on_subscribe(None, None, 7, [1, 128, 1], None)
    assert not broker.connected and broker.subscription_failed


def test_database_open_failure_does_not_ack_and_is_detected(config, monkeypatch):
    import frigate_marmot.runner as module
    broker = MQTT(config)
    def fail(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(module, "Store", fail)
    outcomes = []
    client = SimpleNamespace(ack=lambda *a: outcomes.append("ack"), disconnect=lambda: outcomes.append("disconnect"))
    broker.on_message(client, None, SimpleNamespace(topic="frigate/reviews", payload=b"{}", mid=1, qos=1))
    assert outcomes == ["disconnect"]
    assert broker.persistence_failed


def test_invalid_payload_is_persisted_before_ack(config, store):
    broker = MQTT(config)
    def ack(*args):
        assert store.db.execute("SELECT value FROM metadata WHERE key='invalid_mqtt_payload'").fetchone()[0] == "1"
    broker.on_message(SimpleNamespace(ack=ack), None, SimpleNamespace(topic="frigate/reviews", payload=b"[]", mid=1, qos=1))
    assert store.db.execute("SELECT value FROM metadata WHERE key='invalid_mqtt_last_at'").fetchone()


def test_reconciliation_restart_and_failed_scan_watermark(store, review):
    class API:
        def review_window(self, after, before):
            assert before - after > 86400
            return [review]
    reconcile(store, API(), 90000, "early")
    reconcile(store, API(), 90000, "early")
    assert len(store.rows()) == 2
    assert store.db.execute("SELECT value FROM metadata WHERE key='last_reconciled'").fetchone()[0] == "90000"


def test_equal_timestamp_api_pages_fail_visible_instead_of_skip():
    f = Frigate({"url": "https://synthetic.invalid"})
    f.get = lambda *a, **kw: [{"id": str(i), "start_time": 1} for i in range(1000)]
    with pytest.raises(FrigateError):
        f.review_window(0, .0001)
