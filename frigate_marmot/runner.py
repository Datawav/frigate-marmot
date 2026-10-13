"""Read-only ingestion and supervision. No surveillance controls are published."""
from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt

from .config import ConfigError
from .frigate import Frigate
from .health import camera_healthy, health_sample
from .marmot import Marmot
from .planner import ingest
from .store import Store
from .worker import Worker

LOG = logging.getLogger("frigate-marmot")


def clean_staging(config):
    stage = config.state / "staging"
    if not stage.exists():
        return
    from .config import private_dir
    private_dir(stage)
    for path in stage.iterdir():
        if path.name.startswith("attempt-") and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            raise ConfigError("unexpected staging entry; inspect privately before recovery")


class MQTT:
    def __init__(self, config):
        self.config, self.connected, self.available = config, False, None
        self.persistence_failed = False
        self.subscription_failed = False
        self.subscription_mid = None
        settings = config.data["mqtt"]
        store = Store(config.state / "queue.sqlite3")
        row = store.db.execute("SELECT value FROM metadata WHERE key='mqtt_client_id'").fetchone()
        self.client_id = row[0] if row else "frigate-marmot-" + uuid.uuid4().hex
        store.db.execute("INSERT OR IGNORE INTO metadata VALUES('mqtt_client_id',?)", (self.client_id,))
        store.close()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id,
                                  clean_session=False, manual_ack=True)
        self.client.username_pw_set(settings["username"], Path(settings["password_file"]).expanduser().read_text().strip())
        if settings.get("tls", True):
            self.client.tls_set(ca_certs=settings.get("ca_file"))
        elif not settings.get("allow_plaintext", False):
            raise ConfigError("plaintext MQTT requires explicit allow_plaintext")
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.client.on_subscribe = self.on_subscribe
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        if reason_code == 0:
            base = self.config.data["mqtt"].get("topic_prefix", "frigate")
            result, self.subscription_mid = client.subscribe([(base + "/reviews", 1), (base + "/tracked_object_update", 1), (base + "/available", 1)])
            if result != mqtt.MQTT_ERR_SUCCESS:
                self.subscription_failed = True

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if mid != self.subscription_mid:
            return
        self.connected = len(reason_codes) == 3 and all(getattr(c, "value", c) in (0, 1) for c in reason_codes)
        if not self.connected:
            self.subscription_failed = True
            LOG.warning("MQTT subscription rejected; verify read-only topic ACLs")

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected = False

    def on_message(self, client, userdata, message):
        store = None
        try:
            store = Store(self.config.state / "queue.sqlite3")
            if len(message.payload) > 1024 * 1024:
                raise ValueError("oversized MQTT payload")
            topic = message.topic.rsplit("/", 1)[-1]
            if topic == "available":
                self.available = message.payload.decode()
            else:
                value = json.loads(message.payload)
                if not isinstance(value, dict):
                    raise ValueError("MQTT event must be an object")
                if topic == "reviews":
                    ingest(store, value, self.config.data.get("alerts", {}).get("snapshot_mode", "early"))
                elif topic == "tracked_object_update":
                    stamp = value.get("timestamp", time.time())
                    if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                        raise ValueError("invalid face timestamp")
                    store.face(value, stamp)
            # QoS1 ACK only after committed ingestion. Duplicate delivery is harmless.
            client.ack(message.mid, message.qos)
        except (ValueError, KeyError, TypeError):
            try:
                with store.transaction():
                    row = store.db.execute("SELECT value FROM metadata WHERE key='invalid_mqtt_payload'").fetchone()
                    store.db.execute("INSERT OR REPLACE INTO metadata VALUES('invalid_mqtt_payload',?)", (str(int(row[0]) + 1 if row else 1),))
                    store.db.execute("INSERT OR REPLACE INTO metadata VALUES('invalid_mqtt_last_at',?)", (str(time.time()),))
            except Exception:
                self.persistence_failed = True
                self.connected = False
                client.disconnect()
                return
            client.ack(message.mid, message.qos)  # persist poison-message diagnostic, then drain it
            LOG.warning("invalid MQTT event; payload omitted")
        except Exception:
            self.persistence_failed = True
            self.connected = False
            LOG.warning("MQTT persistence failed; reconnecting without acknowledgement")
            client.disconnect()
        finally:
            if store is not None:
                store.close()

    def start(self):
        settings = self.config.data["mqtt"]
        self.client.connect_async(settings["host"], int(settings.get("port", 8883)), 30)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()


def reconcile(store, frigate, now, snapshot_mode):
    # Full bounded window, plus older unfinished incidents retained locally.
    rows = frigate.review_window(now - 86400 - .001, now + .001)
    pending = [json.loads(r[0]) for r in store.db.execute("SELECT body FROM reviews")]
    for row in pending:
        if row.get("end_time") is None:
            rows.append(frigate.review(row["id"]))
    for review in rows:
        ingest(store, review, snapshot_mode)
    store.db.execute("INSERT OR REPLACE INTO metadata VALUES('last_reconciled',?)", (str(now),))


def run(config):
    destination = config.destination()
    clean_staging(config)
    stop = threading.Event()
    health_failed = threading.Event()
    import signal
    signal.signal(signal.SIGTERM, lambda *args: stop.set())
    signal.signal(signal.SIGINT, lambda *args: stop.set())
    broker = MQTT(config)
    broker.start()  # subscribe before reconciliation; periodic scans cover subscription races
    threads = []

    def worker_thread(kinds):
        store = Store(config.state / "queue.sqlite3")
        worker = Worker(config, store, Frigate(config.data["frigate"]),
                        Marmot(config, destination["account"], destination["group"]), kinds)
        try:
            while not stop.is_set():
                try:
                    if not worker.once():
                        stop.wait(1)
                except Exception:
                    LOG.warning("worker check failed; retrying")
                    stop.wait(5)
        finally:
            store.close()

    for kinds in (("snapshot", "health"), ("video",)):
        t = threading.Thread(target=worker_thread, args=(kinds,), daemon=False)
        t.start()
        threads.append(t)

    def health_thread():
        health_store = Store(config.state / "queue.sqlite3")
        api = Frigate(config.data["frigate"])
        bot = Marmot(config, destination["account"], destination["group"])
        try:
            while not stop.is_set():
                started, now = time.monotonic(), time.time()
                stats = {}
                try:
                    stats = api.get("/stats", timeout=(3, 5))
                    stamp = stats.get("service", {}).get("last_updated", 0)
                    fresh = isinstance(stamp, (float, int)) and 0 <= now - stamp < 90
                    health_sample(health_store, "Frigate", fresh and broker.available not in ("offline", "stopped"), now)
                except Exception:
                    health_sample(health_store, "Frigate", False, now)
                try:
                    profile = config.profile()
                    healthy = camera_healthy(stats, config.data["health"].get("cameras", []), profile)
                    health_sample(health_store, "camera processing", healthy, now)
                    health_sample(health_store, "saved profile", True, now)
                except Exception:
                    health_sample(health_store, "saved profile", False, now)
                health_sample(health_store, "MQTT", broker.connected, now)
                bad_payload = health_store.db.execute("SELECT value FROM metadata WHERE key='invalid_mqtt_last_at'").fetchone()
                health_sample(health_store, "MQTT event validation", not bad_payload or now - float(bad_payload[0]) >= 300, now)
                try:
                    disks = [config.state] + [Path(p).expanduser() for p in config.data["health"].get("disk_paths", [])]
                    enough = all(shutil.disk_usage(p).free >= int(config.data["health"].get("min_free_mb", 1024)) * 1024 * 1024 for p in disks)
                except OSError:
                    enough = False
                health_sample(health_store, "disk space", enough, now)
                try:
                    bot.preflight()
                    health_sample(health_store, "Marmot daemon", True, now)
                except Exception:
                    health_sample(health_store, "Marmot daemon", False, now)
                stop.wait(max(0, 30 - (time.monotonic() - started)))
        except Exception:
            LOG.error("health worker failed; restarting notifier")
            health_failed.set()
            stop.set()
        finally:
            health_store.close()

    monitor = threading.Thread(target=health_thread, daemon=False)
    monitor.start()
    threads.append(monitor)
    store, frigate = Store(config.state / "queue.sqlite3"), Frigate(config.data["frigate"])
    marmot = Marmot(config, destination["account"], destination["group"])
    last_scan = 0
    try:
        while not stop.is_set():
            if broker.persistence_failed:
                raise RuntimeError("MQTT persistence failed; restart to recover unacknowledged messages")
            if broker.subscription_failed:
                raise RuntimeError("MQTT subscription rejected; retry through service restart")
            if not all(t.is_alive() for t in threads):
                raise RuntimeError("notifier worker exited unexpectedly")
            now = time.time()
            if now - last_scan >= 300:
                try:
                    reconcile(store, frigate, now, config.data.get("alerts", {}).get("snapshot_mode", "early"))
                    last_scan = now
                    health_sample(store, "reconciliation", True, now)
                except Exception:
                    health_sample(store, "reconciliation", False, now)
            try:
                Worker(config, store, frigate, marmot, ()).reconcile_uncertain()
            except Exception:
                LOG.warning("send reconciliation unavailable; preserving unknown effects")
            stop.wait(30)
    finally:
        stop.set()
        broker.stop()
        for t in threads:
            t.join()
        store.close()
    if health_failed.is_set():
        raise RuntimeError("health worker failed")
