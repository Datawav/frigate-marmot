"""Real supervisor/SQLite/worker threads, with entirely synthetic external systems."""
import hashlib
import signal
import threading
import time
from pathlib import Path

import frigate_marmot.runner as runner
from frigate_marmot.config import write_json
from frigate_marmot.planner import ingest
from frigate_marmot.store import Store
from frigate_marmot.worker import Worker


def test_full_runtime_mocked_startup_duplicate_ingestion_workers_and_shutdown(config, review, event, monkeypatch):
    now = time.time()
    review.update(start_time=now - 1000, end_time=now - 900)
    event.update(start_time=now - 1000, end_time=now - 900)
    write_json(config.destination_path, {"account": "a" * 64, "group": "b" * 64,
                                       "config_sha256": hashlib.sha256(config.path.read_bytes()).hexdigest()})
    profile = Path(config.data["health"]["profile_file"])
    saved_profile = profile.read_bytes()
    handlers, deliveries = {}, []
    ready = threading.Event()
    lock = threading.Lock()
    monkeypatch.setattr(signal, "signal", lambda sig, fn: handlers.update({sig: fn}))

    class Broker:
        connected, available, persistence_failed, subscription_failed = True, "online", False, False
        def __init__(self, cfg):
            pass
        def start(self):
            s = Store(config.state / "queue.sqlite3")
            ingest(s, review)
            s.close()
        def stop(self):
            self.connected = False

    class API:
        max_bytes = 1024 * 1024
        def __init__(self, cfg):
            pass
        def review_window(self, *args):
            ready.set()
            return [review]  # same incident as MQTT: must remain deduplicated
        def events(self, row):
            assert ready.wait(5)
            return [event]
        def get(self, *args, **kw):
            return {"service": {"last_updated": time.time()}, "cameras": {}}
        def snapshot(self, identity, target):
            target.write_bytes(b"SYNTHETIC SNAPSHOT")
        def clip(self, camera, start, end, target):
            target.write_bytes(b"SYNTHETIC VIDEO")
            return 0

    class Bot:
        def __init__(self, *args):
            pass
        def preflight(self):
            pass
        def upload(self, path):
            assert path.read_bytes().startswith(b"SYNTHETIC")
            return {"source_epoch": 1}
        def send(self, caption, reference=None):
            with lock:
                deliveries.append(caption)
                if len(deliveries) == 2:
                    handlers[signal.SIGTERM]()
            return {"message_ids": ["c" * 64], "published": 0}
        def find_send(self, marker):
            return None

    class SyntheticWorker(Worker):
        def __init__(self, *args):
            def trim(source, target, duration, offset):
                target.write_bytes(source.read_bytes())
            super().__init__(*args, trim=trim)

    monkeypatch.setattr(runner, "MQTT", Broker)
    monkeypatch.setattr(runner, "Frigate", API)
    monkeypatch.setattr(runner, "Marmot", Bot)
    monkeypatch.setattr(runner, "Worker", SyntheticWorker)
    deadline = threading.Timer(30, lambda: handlers.get(signal.SIGTERM, lambda: None)())
    deadline.start()
    try:
        runner.run(config)
    finally:
        deadline.cancel()
        deadline.join()
    s = Store(config.state / "queue.sqlite3")
    assert len(s.rows("enqueued")) == 2
    assert not list((config.state / "staging").iterdir())
    assert profile.read_bytes() == saved_profile
    assert len(deliveries) == 2
    s.close()
