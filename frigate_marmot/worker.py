"""Independent snapshot/video workers with persisted external-effect intents."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import private_dir
from .marmot import WnError
from .planner import known_only


def trim_video(source, target, duration, offset=0, run=subprocess.run):
    # Encode to make the five-minute boundary independent of input keyframes.
    r = run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-ss", str(offset), "-t", str(duration),
             "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "25", "-c:a", "aac", "-movflags", "+faststart", str(target)],
            capture_output=True, timeout=300)
    if r.returncode:
        raise RuntimeError("video preparation failed")
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(target)],
                 capture_output=True, text=True, timeout=30)
    actual = float(json.loads(result.stdout)["format"]["duration"])
    if result.returncode or not math.isfinite(actual) or actual < duration - .5 or actual > min(300, duration) + .1:
        raise RuntimeError("video duration does not match requested interval")


class Worker:
    def __init__(self, config, store, frigate, marmot, kinds, clock=time.time, trim=trim_video):
        self.config, self.store, self.frigate, self.marmot = config, store, frigate, marmot
        self.kinds, self.clock, self.trim = kinds, clock, trim
        self.stage = config.state / "staging"
        private_dir(self.stage)

    def once(self):
        now = self.clock()
        job = self.store.claim(self.kinds, now)
        if job is None:
            return False
        identity = job["id"]
        marker = f"[fm:{identity}]"
        stage = Path(tempfile.mkdtemp(prefix="attempt-", dir=self.stage))
        effect = False
        try:
            self.marmot.preflight()  # A failed network preflight is safely retryable.
            payload = json.loads(job["payload"])
            reference = json.loads(job["reference"]) if job["reference"] else None
            if job["kind"] == "health":
                caption = payload["text"] + "\n" + marker
            else:
                review = self.store.review(job["review_id"])
                events = self.frigate.events(review)
                alerts = self.config.data.get("alerts", {})
                suppress = alerts.get("suppress_known", False) and (job["kind"] == "video" or alerts.get("snapshot_mode") == "after_visit")
                faces = {e["id"]: self.store.face_for(e["id"]) or {} for e in events}
                if suppress and known_only(review, events, faces, set(alerts.get("registered_people", []))):
                    self.store.update(identity, "suppressed")
                    return True
                caption = f"Person alert · {review['camera']} · {job['kind']}"
                if job["kind"] == "video":
                    caption += f" part {payload['part']} · {payload['start']:.0f}–{payload['end']:.0f} UTC Unix time"
                caption += "\n" + marker
                if not reference:
                    if shutil.disk_usage(self.stage).free < int(self.config.data["health"].get("min_free_mb", 1024)) * 1024 * 1024:
                        raise RuntimeError("insufficient staging space")
                    if job["kind"] == "snapshot":
                        persons = [e for e in events if e.get("label") == "person"]
                        if not persons:
                            raise RuntimeError("person event not available")
                        target = stage / "snapshot.jpg"
                        self.frigate.snapshot(persons[0]["id"], target)
                    else:
                        raw, target = stage / "source.mp4", stage / "video.mp4"
                        offset = self.frigate.clip(review["camera"], payload["start"], payload["end"], raw)
                        self.trim(raw, target, payload["end"] - payload["start"], offset)
                    if target.stat().st_size > self.frigate.max_bytes:
                        raise RuntimeError("prepared attachment too large")
                    self.store.update(identity, "upload_intent", lease_until=self.clock() + 600)
                    effect = True
                    reference = self.marmot.upload(target)
                    self.store.update(identity, "uploaded", reference=json.dumps(reference))
                    job["reference"] = json.dumps(reference)
                    effect = False
            self.store.update(identity, "send_intent", lease_until=self.clock() + 600)
            effect = True
            result = self.marmot.send(caption, reference)
            state = "published" if result.get("published", 0) else "enqueued"
            self.store.update(identity, state, result=json.dumps(result), error=None)
        except WnError as error:
            if effect and error.uncertain:
                self.store.update(identity, "uncertain", error=error.code)
            elif error.code == "media_reference_stale_epoch":
                # This rejection occurs before sending. Re-encrypt only unsent media.
                job["reference"] = None
                self.store.update(identity, "scheduled", reference=None, due=now + 30, error=error.code)
            else:
                self.store.retry(job, now, error.code)
        except Exception:
            # Error text can include API credentials, URLs or private media names.
            if effect:
                self.store.update(identity, "uncertain", error="external_effect_unknown")
            else:
                self.store.retry(job, now)
        finally:
            shutil.rmtree(stage)
        return True

    def reconcile_uncertain(self):
        self.marmot.preflight()
        for job in self.store.rows("uncertain"):
            # Only sends are searchable. Uploads lack an upstream idempotency key.
            if job["reference"] or job["kind"] == "health":
                result = self.marmot.find_send(f"[fm:{job['id']}]")
                if result:
                    self.store.update(job["id"], "enqueued", result=json.dumps(result), error=None)
