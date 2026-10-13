import importlib
import json
from types import SimpleNamespace

import pytest

from frigate_marmot.cli import main
from frigate_marmot.config import ConfigError, write_json
from frigate_marmot.marmot import WnError
from frigate_marmot.runner import clean_staging

provisioning = importlib.import_module("frigate_marmot.configure")
NPUB = "npub1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqzqujme"
BOT, GROUP = "a" * 64, "b" * 64


class FakeSetup:
    def __init__(self, config, account=None, group=None):
        self.account, self.group = account, group

    accounts = []
    created = 0
    groups_created = 0
    failure = None

    def call(self, *args, **kw):
        cls = type(self)
        if args[:2] == ("daemon", "status"):
            return {"running": True}
        if args[:2] == ("accounts", "list"):
            return {"accounts": cls.accounts}
        if args[0] == "create-identity":
            cls.created += 1
            cls.accounts = [{"account_id": BOT, "local_signing": True}]
            if cls.failure == "identity":
                raise WnError("transport", uncertain=True)
            return {"account_id": BOT}
        if args[:2] == ("keys", "fetch"):
            return {"account_id": "0" * 64, "key_package_bytes": 100}
        if args[:2] == ("groups", "create"):
            cls.groups_created += 1
            if cls.failure == "group":
                raise WnError("transport", uncertain=True)
            return {"group_id": GROUP}
        if args[:2] == ("groups", "members"):
            return {"members": [{"member_id": BOT}, {"member_id": "0" * 64}]}
        raise AssertionError(args)

    def preflight(self):
        assert self.account == BOT and self.group == GROUP


@pytest.fixture
def fake_setup(monkeypatch):
    FakeSetup.accounts = []
    FakeSetup.created = FakeSetup.groups_created = 0
    FakeSetup.failure = None
    monkeypatch.setattr(provisioning, "Marmot", FakeSetup)
    return FakeSetup


def test_recipient_setup_idempotent(config, fake_setup):
    provisioning.configure(config, recipient=NPUB)
    assert config.destination()["group"] == GROUP
    provisioning.configure(config, recipient=NPUB)
    assert fake_setup.created == fake_setup.groups_created == 1


def test_lost_identity_ack_reconciles_one_account(config, fake_setup):
    fake_setup.failure = "identity"
    with pytest.raises(WnError):
        provisioning.configure(config, group=GROUP)
    fake_setup.failure = None
    provisioning.configure(config, group=GROUP)
    assert fake_setup.created == 1


def test_lost_group_ack_cannot_create_duplicate(config, fake_setup):
    fake_setup.failure = "group"
    with pytest.raises(WnError):
        provisioning.configure(config, recipient=NPUB)
    fake_setup.failure = None
    with pytest.raises(ConfigError):
        provisioning.configure(config, recipient=NPUB)
    assert fake_setup.groups_created == 1
    provisioning.recover_setup(config, GROUP)
    provisioning.configure(config, recipient=NPUB)
    assert fake_setup.groups_created == 1


def test_does_not_adopt_existing_chat_identity_implicitly(config, fake_setup):
    fake_setup.accounts = [{"account_id": BOT, "local_signing": True}]
    with pytest.raises(ConfigError):
        provisioning.configure(config, group=GROUP)
    provisioning.adopt_bot(config, group=GROUP)
    provisioning.configure(config, group=GROUP)
    assert fake_setup.created == 0


def test_multiple_accounts_never_adopted(config, fake_setup):
    fake_setup.accounts = [{"account_id": BOT, "local_signing": True}] * 2
    with pytest.raises(ConfigError):
        provisioning.adopt_bot(config, group=GROUP)


def test_destination_change_refused(config, fake_setup):
    provisioning.configure(config, group=GROUP)
    with pytest.raises(ConfigError):
        provisioning.configure(config, group="c" * 64)


def test_owner_retry_requires_uncertain_job(config, store):
    identity = "e" * 64
    store.add(identity, "snapshot", 0, {})
    store.update(identity, "enqueued")
    args = ["--config", str(config.path), "retry-uncertain", "--job", identity, "--acknowledge-possible-duplicate"]
    assert main(args) == 1
    store.update(identity, "uncertain", reference=json.dumps({"source_epoch": 1}))
    assert main(args) == 0
    assert store.db.execute("SELECT state FROM jobs WHERE id=?", (identity,)).fetchone()[0] == "uploaded"


def test_startup_deletes_only_owned_abandoned_staging(config):
    stage = config.state / "staging"
    stage.mkdir(mode=0o700)
    attempt = stage / "attempt-synthetic"
    attempt.mkdir(mode=0o700)
    (attempt / "synthetic.jpg").write_bytes(b"FAKE MEDIA")
    clean_staging(config)
    assert not list(stage.iterdir())
    (stage / "unexpected").write_text("preserve")
    with pytest.raises(ConfigError):
        clean_staging(config)
    assert (stage / "unexpected").exists()


def test_snapshot_worker_does_not_reclaim_active_video(store):
    store.add("video", "video", 0, {})
    store.claim(("video",), 0)
    store.claim(("snapshot", "health"), 1000)
    assert store.db.execute("SELECT state FROM jobs WHERE id='video'").fetchone()[0] == "preparing"
