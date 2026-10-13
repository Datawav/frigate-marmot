#!/usr/bin/env python3
"""Install user-scoped files. No surveillance changes, sudo, reboot or service start."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path

from frigate_marmot import MDK_COMMIT
from frigate_marmot.config import ConfigError, private_dir, private_file


def unit_files(home, prefix):
    # Deliberately reject paths requiring systemd specifier/shell interpretation.
    for value in (str(home), str(prefix)):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
            raise ConfigError("installation paths must be absolute simple paths")
    config = home / ".config/frigate-marmot/config.toml"
    state = home / ".local/state/frigate-marmot/bot"
    command = f"{prefix}/venv/bin/frigate-marmot --config {config}"
    common = "\n[Install]\nWantedBy=default.target\n"
    daemon = f"""[Unit]
Description=Dedicated Frigate Marmot bot daemon
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
ExecStart={prefix}/bin/wnd --home {state} --socket {state}/wnd.sock --secret-store file --discovery-relays wss://relay.eu.whitenoise.chat,wss://relay.us.whitenoise.chat --default-account-relays wss://relay.eu.whitenoise.chat,wss://relay.us.whitenoise.chat
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
""" + common
    notifier = f"""[Unit]
Description=Private Frigate person alerts through Marmot
Wants=frigate-marmot-daemon.service
After=frigate-marmot-daemon.service
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
ExecCondition={command} check-config
ExecStart={command} run
Restart=on-failure
RestartSec=10
TimeoutStopSec=420
UMask=0077
NoNewPrivileges=true
""" + common
    return {"frigate-marmot-daemon.service": daemon, "frigate-marmot-notifier.service": notifier}


def install(home, prefix, mdk_dir, install_deps=True, verify_units=True):
    os.umask(0o077)
    unit_files(home, prefix)
    private_dir(prefix)
    manifest_path = mdk_dir / "mdk-provenance.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("commit") != MDK_COMMIT:
        raise ConfigError("MDK build must match the supported source commit")
    for name in ("wn", "wnd"):
        source = mdk_dir / name
        if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != manifest.get("sha256", {}).get(name):
            raise ConfigError("MDK binary does not match provenance manifest")
    source_root = Path(__file__).resolve().parent
    config_dir = home / ".config/frigate-marmot"
    state = home / ".local/state/frigate-marmot"
    for path in (prefix / "bin", config_dir, state, state / "bot"):
        private_dir(path)
    for name in ("wn", "wnd", "mdk-provenance.json"):
        target = prefix / "bin" / name
        if target.is_symlink():
            raise ConfigError("refusing symlink binary target")
        shutil.copyfile(mdk_dir / name, target)
        target.chmod(0o600 if name.endswith("json") else 0o700)
    if install_deps:
        venv.create(prefix / "venv", with_pip=True)
        subprocess.run([str(prefix / "venv/bin/python"), "-m", "pip", "install", str(source_root)], check=True)
    config = config_dir / "config.toml"
    if not config.exists():
        text = (source_root / "config.example.toml").read_text().replace("@HOME@", str(home)).replace("@PREFIX@", str(prefix))
        config.write_text(text)
        config.chmod(0o600)
    else:
        private_file(config)  # Never overwrite saved settings, profile or credentials.
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, text in unit_files(home, prefix).items():
        path = unit_dir / name
        if path.is_symlink():
            raise ConfigError("refusing symlink unit target")
        path.write_text(text)
        path.chmod(0o600)
        if verify_units:
            # --man=no keeps verification offline. Does not start or enable anything.
            subprocess.run(["systemd-analyze", "--user", "verify", "--man=no", str(path)], check=True)
    return config


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mdk-bin-dir", type=Path, required=True)
    args = p.parse_args()
    try:
        home = Path.home()
        config = install(home, home / ".local/share/frigate-marmot", args.mdk_bin_dir)
        print(f"Installed. Edit {config}; follow README setup before enabling user services.")
        return 0
    except (ConfigError, OSError, KeyError, ValueError, subprocess.CalledProcessError):
        print("Installation failed; check prerequisites, file permissions and the MDK manifest.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
