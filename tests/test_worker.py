import json
from pathlib import Path

import pytest

from frigate_marmot.marmot import WnError
from frigate_marmot.planner import ingest
from frigate_marmot.worker import Worker


class FakeFrigate:
    max_bytes = 128 * 1024 * 1024

    def __init__(self, event):
        self.event = event

    def events(self, review):
        return [self.event]

    def snapshot(self, event, target):
        target.write_bytes(b"SYNTHETIC SNAPSHOT")

    def clip(self, camera, start, end, target):
        target.write_bytes(b"SYNTHETIC VIDEO")
        return .2


class FakeMarmot:
    def __init__(self):
        self.uploads = self.sends = 0
        self.failure = None
        self.found = None

    def preflight(self):
        if self.failure == "preflight":
            raise WnError("wn_transport_failure")

    def upload(self, path):
        self.uploads += 1
        if self.failure == "upload":
            raise WnError("wn_transport_failure", uncertain=True)
        return {"plaintext_sha256": "a" * 64, "source_epoch": 1}

    def send(self, caption, reference=None):
        self.sends += 1
        if self.failure == "send":
            raise WnError("wn_transport_failure", uncertain=True)
        if self.failure == "epoch":
            raise WnError("media_reference_stale_epoch")
        return {"message_ids": ["b" * 64], "published": 0}

    def find_send(self, marker):
        return self.found


def trim(source, target, duration, offset):
    assert offset == .2
    target.write_bytes(source.read_bytes())


def make_worker(config, store, review, event, kind="snapshot", clock=lambda: 1300):
    ingest(store, review)
    m = FakeMarmot()
    return Worker(config, store, FakeFrigate(event), m, (kind,), clock, trim), m


def test_enqueued_not_reuploaded_or_claimed_as_phone_delivery(config, store, review, event):
    w, m = make_worker(config, store, review, event)
    assert w.once()
    assert not w.once()
    assert (m.uploads, m.sends) == (1, 1)
    assert store.rows("enqueued")
    assert not list(w.stage.iterdir())


@pytest.mark.parametrize("failure", ["upload", "send"])
def test_lost_response_holds_unknown_effect_and_cleans_staging(config, store, review, event, failure):
    w, m = make_worker(config, store, review, event)
    m.failure = failure
    assert w.once()
    assert store.rows("uncertain")
    assert not w.once()
    assert not list(w.stage.iterdir())
    m.found = {"message_ids": ["c" * 64], "published": None}
    w.reconcile_uncertain()
    assert bool(store.rows("enqueued")) == (failure == "send")


def test_preflight_network_failure_retries_safely(config, store, review, event):
    w, m = make_worker(config, store, review, event)
    m.failure = "preflight"
    w.once()
    assert (m.uploads, m.sends) == (0, 0)
    assert store.rows("scheduled")
    m.failure = None
    w.clock = lambda: 1400
    w.once()
    assert store.rows("enqueued")


def test_stale_epoch_reencrypts_only_unsent_media(config, store, review, event):
    w, m = make_worker(config, store, review, event)
    m.failure = "epoch"
    w.once()
    assert store.rows("scheduled")
    m.failure = None
    w.clock = lambda: 1400
    w.once()
    assert (m.uploads, m.sends) == (2, 2)
    assert store.rows("enqueued")


def test_known_only_video_suppressed_early_snapshot_kept(config, store, review, event):
    config.data["alerts"]["suppress_known"] = True
    w, m = make_worker(config, store, review, event)
    w.once()
    w.kinds = ("video",)
    w.once()
    assert len(store.rows("suppressed")) == 1
    assert len(store.rows("enqueued")) == 1
    assert (m.uploads, m.sends) == (1, 1)


def test_after_visit_known_only_snapshot_suppressed(config, store, review, event):
    config.data["alerts"].update(suppress_known=True, snapshot_mode="after_visit")
    w, m = make_worker(config, store, review, event)
    w.once()
    assert store.rows("suppressed")
    assert not m.uploads


def test_video_waits_until_post_buffer_settled(config, store, review, event):
    w, m = make_worker(config, store, review, event, "video", lambda: 1239)
    assert not w.once()
    w.clock = lambda: 1240
    assert w.once()
    assert m.uploads == 1


def test_saved_upload_is_reused_after_restart(config, store, review, event):
    w, m = make_worker(config, store, review, event)
    row = next(r for r in store.rows() if r["kind"] == "snapshot")
    store.update(row["id"], "uploaded", reference=json.dumps({"source_epoch": 1}))
    w.once()
    assert m.uploads == 0 and m.sends == 1
