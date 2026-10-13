"""Persistent sustained-fault warnings and exactly one recovery job."""
from __future__ import annotations

import hashlib


def health_sample(store, name, healthy, now):
    with store.transaction():
        row = store.db.execute("SELECT * FROM health WHERE name=?", (name,)).fetchone()
        since = row["since"] if row else None
        generation = row["generation"] if row else 0
        warned = row["warned"] if row else 0
        if healthy:
            if warned:
                key = hashlib.sha256(f"health:{name}:{generation}:recovery".encode()).hexdigest()
                store.add(key, "health", now, {"text": f"Camera notifier recovered: {name}."})
            since, warned = None, 0
        else:
            if since is None:
                since, generation = now, generation + 1
            if not warned and now - since >= 180:
                key = hashlib.sha256(f"health:{name}:{generation}:warning".encode()).hexdigest()
                store.add(key, "health", now, {"text": f"Camera notifier fault for at least three minutes: {name}."})
                warned = 1
        store.db.execute("INSERT OR REPLACE INTO health VALUES(?,?,?,?)", (name, since, generation, warned))


def camera_healthy(stats, cameras, profile):
    if profile == "Disarmed":
        return True
    if profile != "Armed" or not cameras:
        return False
    # Quiet scenes have no detections; processing must still be running.
    rows = stats.get("cameras", stats)
    return all(isinstance(rows.get(c), dict) and rows[c].get("camera_fps", 0) > 0
               and rows[c].get("process_fps", 0) > 0 for c in cameras)
