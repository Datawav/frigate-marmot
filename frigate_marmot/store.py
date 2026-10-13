"""Durable deduplication, job leases and external-effect boundaries."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import private_dir


class Store:
    def __init__(self, path: Path):
        private_dir(path.parent)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS faces(id TEXT PRIMARY KEY, body TEXT NOT NULL, stamp REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, review_id TEXT, due REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'scheduled', payload TEXT NOT NULL,
          reference TEXT, result TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT, lease_until REAL NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS due_jobs ON jobs(kind,state,due);
        CREATE TABLE IF NOT EXISTS health(name TEXT PRIMARY KEY, since REAL,
          generation INTEGER NOT NULL DEFAULT 0, warned INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def add(self, identity: str, kind: str, due: float, payload: dict, review_id=None):
        self.db.execute("INSERT OR IGNORE INTO jobs(id,kind,review_id,due,payload) VALUES(?,?,?,?,?)",
                        (identity, kind, review_id, due, json.dumps(payload)))

    def review(self, identity: str) -> dict:
        row = self.db.execute("SELECT body FROM reviews WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else {}

    def face(self, value: dict, stamp: float):
        if value.get("type") != "face" or not value.get("id"):
            return
        self.db.execute("""INSERT INTO faces VALUES(?,?,?) ON CONFLICT(id) DO UPDATE
          SET body=excluded.body,stamp=excluded.stamp WHERE excluded.stamp>=faces.stamp""",
                        (value["id"], json.dumps(value), stamp))

    def face_for(self, identity):
        row = self.db.execute("SELECT body FROM faces WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def claim(self, kinds: tuple, now: float):
        with self.transaction():
            q = ",".join("?" for _ in kinds)
            # Only local preparation can be repeated after a crash. Effect intents cannot.
            # Recover only this worker's kinds: video preparation may exceed a lease
            # while the independent snapshot worker continues polling.
            self.db.execute(f"UPDATE jobs SET state='scheduled' WHERE kind IN ({q}) AND state='preparing' AND lease_until<?", (*kinds, now))
            self.db.execute("""UPDATE jobs SET state='uncertain',error='interrupted_external_effect'
                             WHERE state IN ('upload_intent','send_intent') AND lease_until<?""", (now,))
            row = self.db.execute(f"SELECT * FROM jobs WHERE kind IN ({q}) AND state IN ('scheduled','uploaded') AND due<=? ORDER BY due,id LIMIT 1", (*kinds, now)).fetchone()
            if row is None:
                return None
            previous = row["state"]
            self.db.execute("UPDATE jobs SET state='preparing',lease_until=?,attempts=attempts+1 WHERE id=?", (now + 600, row["id"]))
            return dict(row) | {"previous_state": previous}

    def update(self, identity, state, **fields):
        allowed = {"due", "reference", "result", "error", "lease_until"}
        if fields.keys() - allowed:
            raise ValueError("invalid job fields")
        values = {"state": state, **fields}
        self.db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), identity))

    def retry(self, job, now, code="preparation_failed"):
        delay = min(3600, 15 * 2 ** min(job["attempts"], 8))
        # Accepted upload metadata survives: repeat only the unsent message.
        self.update(job["id"], "uploaded" if job.get("reference") else "scheduled", due=now + delay, error=code)

    def rows(self, state=None):
        return self.db.execute("SELECT * FROM jobs" + (" WHERE state=?" if state else ""), (state,) if state else ()).fetchall()

    def close(self):
        self.db.close()
