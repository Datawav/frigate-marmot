import hashlib
import json
import os
from pathlib import Path

import pytest

from frigate_marmot import MDK_COMMIT
from frigate_marmot.config import Config, write_json
from frigate_marmot.store import Store


@pytest.fixture(autouse=True)
def private_umask():
    previous = os.umask(0o077)
    yield
    os.umask(previous)


@pytest.fixture
def config(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    bins = tmp_path / "bin"
    bins.mkdir(mode=0o700)
    hashes = {}
    for n in ("wn", "wnd"):
        (bins / n).write_text("#!/bin/sh\nexit 0\n")
        (bins / n).chmod(0o700)
        hashes[n] = hashlib.sha256((bins / n).read_bytes()).hexdigest()
    write_json(bins / "mdk-provenance.json", {"commit": MDK_COMMIT, "sha256": hashes})
    password = tmp_path / "mqtt-password"
    password.write_text("SYNTHETIC_TEST_PASSWORD")
    password.chmod(0o600)
    profile = tmp_path / "profile.json"
    write_json(profile, {"profile": "Disarmed"})
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'''state_dir = "{state}"
[mqtt]
host="localhost"
username="test"
password_file="{password}"
tls=true
[frigate]
url="http://127.0.0.1:5000"
allow_http=true
max_attachment_mb=128
[marmot]
wn="{bins / 'wn'}"
manifest="{bins / 'mdk-provenance.json'}"
[alerts]
snapshot_mode="early"
suppress_known=false
registered_people=["Alice"]
[health]
profile_file="{profile}"
cameras=["indoor"]
min_free_mb=64
''')
    cfg.chmod(0o600)
    return Config.load(cfg)


@pytest.fixture
def store(config):
    result = Store(config.state / "queue.sqlite3")
    yield result
    result.close()


@pytest.fixture
def review():
    return {"id": "synthetic-review", "camera": "indoor", "start_time": 1000,
            "end_time": 1100, "severity": "alert",
            "data": {"objects": ["person"], "detections": ["synthetic-person"]}}


@pytest.fixture
def event():
    return {"id": "synthetic-person", "camera": "indoor", "label": "person",
            "start_time": 1000, "end_time": 1100, "sub_label": "Alice",
            "data": {"sub_label_score": .99}}
