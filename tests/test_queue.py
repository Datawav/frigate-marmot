import copy
import json

import pytest

from frigate_marmot.health import camera_healthy, health_sample
from frigate_marmot.planner import ingest, intervals, known_only
from frigate_marmot.store import Store


def test_duplicate_and_out_of_order_reviews(store, review):
    live = copy.deepcopy(review)
    live["end_time"] = None
    ingest(store, live)
    ingest(store, review)
    ingest(store, live)
    ingest(store, review)
    assert store.review(review["id"])["end_time"] == 1100
    rows = store.rows()
    assert len(rows) == 2
    assert {r["kind"]: r["due"] for r in rows} == {"snapshot": 1015, "video": 1240}


def test_late_person_promotes_detection_without_duplicate(store, review):
    detection = copy.deepcopy(review)
    detection["severity"] = "detection"
    ingest(store, detection)
    assert not store.rows()
    ingest(store, review)
    ingest(store, detection)
    assert len(store.rows()) == 2


def test_after_visit_does_not_queue_early_snapshot(store, review):
    live = review | {"end_time": None}
    ingest(store, live, "after_visit")
    assert not store.rows()
    ingest(store, review, "after_visit")
    assert next(r for r in store.rows() if r["kind"] == "snapshot")["due"] == 1115


def test_long_incident_parts_are_contiguous():
    parts = list(intervals(1000.2, 1940.6))
    assert parts[0][0] == 940.2
    assert parts[-1][1] == 2060.6
    assert all(b - a <= 300 for a, b in parts)
    assert all(parts[i][1] == parts[i + 1][0] for i in range(len(parts) - 1))


@pytest.mark.parametrize("start,end", [(2, 1), (float("nan"), 2), (1, float("inf"))])
def test_bad_intervals_rejected(start, end):
    with pytest.raises(ValueError):
        list(intervals(start, end))


def test_restart_preserves_jobs_and_health(config, review):
    s = Store(config.state / "queue.sqlite3")
    ingest(s, review)
    health_sample(s, "MQTT", False, 100)
    s.close()
    s = Store(config.state / "queue.sqlite3")
    health_sample(s, "MQTT", False, 279)
    assert len(s.rows()) == 2
    health_sample(s, "MQTT", False, 280)
    health_sample(s, "MQTT", False, 500)
    assert len(s.rows()) == 3
    s.close()
    s = Store(config.state / "queue.sqlite3")
    health_sample(s, "MQTT", True, 510)
    health_sample(s, "MQTT", True, 520)
    assert len(s.rows()) == 4
    s.close()


def test_preparation_recovered_but_effect_never_replayed(store):
    store.add("a", "snapshot", 0, {})
    store.add("b", "video", 0, {})
    a = store.claim(("snapshot",), 10)
    store.update("b", "send_intent", lease_until=100)
    assert a["id"] == "a"
    assert store.claim(("snapshot",), 620)["id"] == "a"
    assert store.db.execute("SELECT state FROM jobs WHERE id='b'").fetchone()[0] == "uncertain"


def test_registered_only_requires_complete_final_evidence(review, event):
    assert known_only(review, [event], {}, {"Alice"})
    assert not known_only(review, [], {}, {"Alice"})
    assert not known_only(review | {"end_time": None}, [event], {}, {"Alice"})
    assert not known_only(review, [event | {"end_time": None}], {}, {"Alice"})
    assert not known_only(review, [event], {}, {"Bob"})


@pytest.mark.parametrize("score", [.949, None, "0.99", True, float("nan"), float("inf"), 1.1])
def test_uncertain_confidence_never_suppressed(review, event, score):
    event["data"]["sub_label_score"] = score
    assert not known_only(review, [event], {}, {"Alice"})


def test_detector_score_not_face_recognition(review, event):
    event.pop("sub_label")
    event.pop("data")
    event["top_score"] = .999
    event["attributes"] = {"face": .999}
    assert not known_only(review, [event], {}, {"Alice"})


def test_latest_uncertain_face_cannot_be_overridden_by_stored_high_score(review, event):
    faces = {event["id"]: {"name": "Unknown", "score": .3, "camera": "indoor", "timestamp": 1099}}
    assert not known_only(review, [event], faces, {"Alice"})


def test_unknown_person_in_mixed_visit_preserved(review, event):
    review["data"]["detections"].append("unknown-person")
    unknown = event | {"id": "unknown-person", "sub_label": None, "data": {}}
    assert not known_only(review, [event, unknown], {}, {"Alice"})


def test_latest_face_evidence_replaces_high_score(store, review, event):
    store.face({"type": "face", "id": event["id"], "name": "Alice", "score": .99, "camera": "indoor", "timestamp": 1005}, 1005)
    store.face({"type": "face", "id": event["id"], "name": "Unknown", "score": .4, "camera": "indoor", "timestamp": 1010}, 1010)
    store.face({"type": "face", "id": event["id"], "name": "Alice", "score": .99, "camera": "indoor", "timestamp": 1006}, 1006)
    event.pop("sub_label")
    event.pop("data")
    assert not known_only(review, [event], {event["id"]: store.face_for(event["id"])}, {"Alice"})


def test_disarmed_quiet_is_expected():
    assert camera_healthy({}, ["indoor"], "Disarmed")
    assert not camera_healthy({}, ["indoor"], "Armed")
    assert camera_healthy({"cameras": {"indoor": {"camera_fps": 5, "process_fps": 5, "detection_fps": 0}}}, ["indoor"], "Armed")


def test_concurrent_connections_cannot_claim_same_job(config):
    a = Store(config.state / "queue.sqlite3")
    b = Store(config.state / "queue.sqlite3")
    a.add("one", "snapshot", 0, {})
    assert a.claim(("snapshot",), 1)
    assert b.claim(("snapshot",), 1) is None
    a.close()
    b.close()
