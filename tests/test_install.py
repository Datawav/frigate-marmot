import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from frigate_marmot.config import ConfigError
from frigate_marmot.worker import trim_video

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_install_preserves_config_profile_and_never_starts_services(tmp_path, config, monkeypatch):
    home, prefix = tmp_path / "owner", tmp_path / "prefix"
    home.mkdir(mode=0o700)
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda args, **kw: calls.append(args))
    bins = Path(config.data["marmot"]["wn"]).parent
    path = installer.install(home, prefix, bins, install_deps=False)
    path.write_text("# owner settings preserved\n")
    profile = home / ".config/frigate-marmot/profile.json"
    profile.write_text('{"profile":"Disarmed"}')
    installer.install(home, prefix, bins, install_deps=False)
    assert path.read_text() == "# owner settings preserved\n"
    assert profile.read_text() == '{"profile":"Disarmed"}'
    assert all(args[0] == "systemd-analyze" for args in calls)
    assert (home / ".config/systemd/user/frigate-marmot-notifier.service").stat().st_mode & 0o777 == 0o600


def test_installer_rejects_unpinned_binary(tmp_path, config):
    bins = Path(config.data["marmot"]["wn"]).parent
    (bins / "wn").write_text("wrong binary")
    with pytest.raises(ConfigError):
        installer.install(tmp_path / "home", tmp_path / "prefix", bins, install_deps=False)


def test_user_units_include_restart_umask_config_gate_and_fixed_relays(tmp_path):
    units = installer.unit_files(tmp_path / "owner", tmp_path / "prefix")
    for unit in units.values():
        assert "Restart=on-failure" in unit
        assert "UMask=0077" in unit
        assert "reboot" not in unit
    assert "ExecCondition=" in units["frigate-marmot-notifier.service"]
    assert "--default-account-relays wss://relay.eu.whitenoise.chat,wss://relay.us.whitenoise.chat" in units["frigate-marmot-daemon.service"]


def test_unit_path_injection_rejected(tmp_path):
    with pytest.raises(ConfigError):
        installer.unit_files(tmp_path / "bad%name", tmp_path / "prefix")


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze required")
def test_real_systemd_user_unit_syntax_offline(tmp_path):
    home, prefix = tmp_path / "owner", tmp_path / "prefix"
    for folder in (prefix / "bin", prefix / "venv/bin", tmp_path / "runtime"):
        folder.mkdir(parents=True, mode=0o700)
    for binary in (prefix / "bin/wnd", prefix / "venv/bin/frigate-marmot"):
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
    paths = []
    for name, unit in installer.unit_files(home, prefix).items():
        path = tmp_path / name
        path.write_text(unit)
        paths.append(str(path))
    env = dict(os.environ, XDG_RUNTIME_DIR=str(tmp_path / "runtime"))
    subprocess.run(["systemd-analyze", "--user", "verify", "--man=no", *paths],
                   env=env, capture_output=True, check=True, timeout=30)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_real_synthetic_video_trim(tmp_path):
    source, target = tmp_path / "synthetic.mp4", tmp_path / "trimmed.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10",
                    "-t", "3", "-c:v", "libx264", str(source)], check=True, timeout=30)
    trim_video(source, target, 1.5, .5)
    assert target.stat().st_size > 0
