"""Pure alert timing and conservative recognition decisions."""
from __future__ import annotations

import hashlib
import json
import math


def job_key(review, kind, part=0):
    return hashlib.sha256(f"{review}\0{kind}\0{part}".encode()).hexdigest()


def intervals(start, end):
    if not math.isfinite(start) or not math.isfinite(end) or end < start:
        raise ValueError("invalid incident interval")
    cursor, finish = max(0, start - 60), end + 120
    while cursor < finish:
        following = min(finish, cursor + 300)
        yield cursor, following
        cursor = following


def ingest(store, value, snapshot_mode="early"):
    review = value.get("after", value)
    if not isinstance(review, dict) or not review.get("id"):
        raise ValueError("invalid review")
    start = float(review["start_time"])
    end = review.get("end_time")
    if not math.isfinite(start) or (end is not None and (not math.isfinite(float(end)) or float(end) < start)):
        raise ValueError("invalid review time")
    with store.transaction():
        old = store.review(review["id"])
        if old:
            # Stable identity/timing, monotonic completed state and detection membership.
            if old["camera"] != review["camera"] or old["start_time"] != review["start_time"]:
                raise ValueError("review identity changed")
            data = dict(old.get("data", {})) | review.get("data", {})
            for name in ("detections", "objects"):
                data[name] = sorted(set(old.get("data", {}).get(name, [])) | set(review.get("data", {}).get(name, [])))
            review = old | review | {"data": data}
            if old.get("end_time") is not None:
                review["end_time"] = old["end_time"]
            if old.get("severity") == "alert":
                review["severity"] = "alert"
        store.db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?)", (review["id"], json.dumps(review)))
        if review.get("severity") != "alert" or "person" not in review.get("data", {}).get("objects", []):
            return
        end = review.get("end_time")
        if snapshot_mode == "early" or end is not None:
            due = start + 15 if snapshot_mode == "early" else float(end) + 15
            store.add(job_key(review["id"], "snapshot"), "snapshot", due, {}, review["id"])
        if end is not None:
            for part, (a, b) in enumerate(intervals(start, float(end))):
                store.add(job_key(review["id"], "video", part), "video", float(end) + 140,
                          {"start": a, "end": b, "part": part + 1}, review["id"])


def known_only(review: dict, events: list[dict], faces: dict, registered: set[str]) -> bool:
    """Suppress only a fully resolved, completed, nonempty person visit."""
    expected = set(review.get("data", {}).get("detections", []))
    if review.get("end_time") is None or not expected or {e.get("id") for e in events} != expected:
        return False
    persons = [e for e in events if e.get("label") == "person"]
    if not persons or any(not e.get("label") for e in events):
        return False
    for event in persons:
        if event.get("end_time") is None:
            return False
        # Persisted Frigate sub_label is its recognition aggregate, not detector top_score.
        label = event.get("sub_label")
        latest = faces.get(event["id"])
        if isinstance(label, list) and len(label) == 2:
            name, score = label
        elif isinstance(label, str) and "sub_label_score" in event.get("data", {}):
            name, score = label, event["data"]["sub_label_score"]
        else:
            evidence = faces.get(event["id"], {})
            # Late MQTT face updates lacking camera/time are untrustworthy for suppression.
            if evidence.get("camera") != review.get("camera") or not evidence.get("timestamp"):
                return False
            name, score = evidence.get("name"), evidence.get("score")
        if name not in registered or isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score) or score < .95 or score > 1:
            return False
        if latest:
            latest_score = latest.get("score")
            if latest.get("camera") != review.get("camera") or latest.get("name") != name or not latest.get("timestamp"):
                return False
            if isinstance(latest_score, bool) or not isinstance(latest_score, (float, int)) or not math.isfinite(latest_score) or not .95 <= latest_score <= 1:
                return False
    return True
