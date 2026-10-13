"""Read-only Frigate HTTP API, bounded downloads and coverage checks."""
from __future__ import annotations

import math
import time
from pathlib import Path
from urllib.parse import quote

import requests


class FrigateError(RuntimeError):
    pass


def component(value):
    return quote(str(value), safe="")


def coverage(records, start, end):
    cursor = start
    for r in sorted(records, key=lambda r: r["start_time"]):
        a, b = float(r["start_time"]), float(r["end_time"])
        if not math.isfinite(a) or not math.isfinite(b) or b < a:
            return False
        if b <= cursor:
            continue
        if a > cursor + .25:
            return False
        cursor = max(cursor, b)
        if cursor >= end - .25:
            return True
    return False


class Frigate:
    def __init__(self, config, session=None):
        self.base = config["url"].rstrip("/") + "/api"
        self.session = session or requests.Session()
        self.session.trust_env = False  # credentials/media must not follow ambient proxies
        self.session.verify = config.get("ca_file", True)
        if self.session.verify is False:
            raise FrigateError("TLS verification cannot be disabled")
        self.max_bytes = int(config.get("max_attachment_mb", 128)) * 1024 * 1024
        if config.get("token_file"):
            token = Path(config["token_file"]).expanduser().read_text().strip()
            self.session.headers["Authorization"] = "Bearer " + token

    def get(self, path, timeout=(5, 30), **params):
        response = self.session.get(self.base + path, params=params, timeout=timeout, allow_redirects=False)
        try:
            if response.status_code != 200:
                raise FrigateError("Frigate API request failed")
            return response.json()
        finally:
            response.close()

    def review(self, identity):
        return self.get("/review/" + component(identity))

    def events(self, review):
        return [self.get("/events/" + component(i)) for i in review.get("data", {}).get("detections", [])]

    def review_window(self, after, before):
        # Frigate review API is ordered newest-first and uses strict before/after.
        # A recursive time split avoids skipping equal-timestamp rows at page boundaries.
        rows = self.get("/review", after=after, before=before, limit=1000, severity="alert")
        if not isinstance(rows, list):
            raise FrigateError("unexpected review API response")
        if len(rows) < 1000:
            return rows
        if before - after <= .001:
            raise FrigateError("review pagination overflow; reconciliation incomplete")
        middle = (after + before) / 2
        lower = self.review_window(after, middle + .000001)
        upper = self.review_window(middle - .000001, before)
        return list({r["id"]: r for r in lower + upper}.values())

    def download(self, path, target):
        deadline = time.monotonic() + 180
        with self.session.get(self.base + path, stream=True, timeout=(5, 90), allow_redirects=False) as response:
            if response.status_code != 200:
                raise FrigateError("Frigate media unavailable")
            count = 0
            with target.open("xb") as stream:
                for chunk in response.iter_content(65536):
                    if time.monotonic() > deadline:
                        raise FrigateError("media download deadline exceeded")
                    count += len(chunk)
                    if count > self.max_bytes:
                        raise FrigateError("attachment exceeds configured size bound")
                    stream.write(chunk)
            if count == 0:
                raise FrigateError("empty attachment")

    def snapshot(self, event_id, target):
        self.download("/events/" + component(event_id) + "/snapshot.jpg", target)

    def clip(self, camera, start, end, target):
        records = self.get("/" + component(camera) + "/recordings", after=start, before=end)
        if not coverage(records, start, end):
            raise FrigateError("recording coverage incomplete; check retention and recording mode")
        overlap = [r for r in records if r["end_time"] > start and r["start_time"] < end]
        # Fetch complete boundary segments, bypassing Frigate's integer inpoint
        # truncation and stream-copy keyframe preroll. Local decoding trims exactly.
        first = min(r["start_time"] for r in overlap)
        last = max(r["end_time"] for r in overlap)
        self.download(f"/{component(camera)}/start/{first}/end/{last}/clip.mp4", target)
        return start - first
