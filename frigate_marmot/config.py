"""Private, explicit configuration. No secret values in exceptions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import MDK_COMMIT


class ConfigError(ValueError):
    pass


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ConfigError("directory must be owned by the current user, not a symlink")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ConfigError("directory must have mode 0700")


def private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ConfigError("private file is missing or is a symlink")
    s = path.stat()
    if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
        raise ConfigError("private file must be owner-only")


def write_json(path: Path, value: dict) -> None:
    private_dir(path.parent)
    if path.is_symlink():
        raise ConfigError("refusing symlink destination")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        d = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        Path(tmp).unlink(missing_ok=True)


def hex_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ConfigError("expected a 32-byte hexadecimal identifier")
    return value.lower()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def npub_hex(value: str) -> str:
    # NIP-19 Bech32, with full checksum and 32-byte payload validation.
    if not value.startswith("npub1") or value.lower() != value:
        raise ConfigError("recipient must be a lowercase npub")
    alphabet = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    try:
        digits = [alphabet.index(c) for c in value[5:]]
    except ValueError:
        raise ConfigError("invalid npub") from None
    chk = 1
    expanded = [ord(c) >> 5 for c in "npub"] + [0] + [ord(c) & 31 for c in "npub"]
    for n in expanded + digits:
        top = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ n
        for i, g in enumerate((0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)):
            if (top >> i) & 1:
                chk ^= g
    if chk != 1 or len(digits) != 58:
        raise ConfigError("invalid npub checksum or length")
    acc = bits = 0
    data = bytearray()
    for n in digits[:-6]:
        acc = (acc << 5) | n
        bits += 5
        while bits >= 8:
            bits -= 8
            data.append((acc >> bits) & 255)
    if len(data) != 32 or bits >= 5 or ((acc << (8 - bits)) & 255):
        raise ConfigError("invalid npub payload")
    return data.hex()


@dataclass(frozen=True)
class Config:
    path: Path
    state: Path
    data: dict

    @classmethod
    def load(cls, path: Path) -> "Config":
        private_file(path)
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        state = Path(data["state_dir"]).expanduser().absolute()
        private_dir(state)
        for name in ("mqtt", "frigate", "marmot"):
            if not isinstance(data.get(name), dict):
                raise ConfigError("missing configuration section")
        mqtt = data["mqtt"]
        if not mqtt.get("username") or not mqtt.get("password_file"):
            raise ConfigError("authenticated MQTT is required")
        private_file(Path(mqtt["password_file"]).expanduser())
        u = urlsplit(data["frigate"]["url"])
        if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.query:
            raise ConfigError("invalid Frigate URL")
        if u.scheme == "http" and not data["frigate"].get("allow_http", False):
            raise ConfigError("HTTP requires explicit allow_http on a trusted local network")
        if data["frigate"].get("token_file"):
            private_file(Path(data["frigate"]["token_file"]).expanduser())
        mode = data.get("alerts", {}).get("snapshot_mode", "early")
        if mode not in ("early", "after_visit"):
            raise ConfigError("snapshot_mode must be early or after_visit")
        server = data["marmot"].get("media_server")
        if server:
            parsed = urlsplit(server)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
                raise ConfigError("media server must be HTTPS without embedded credentials")
        private_file(Path(data["marmot"]["manifest"]))
        manifest = json.loads(Path(data["marmot"]["manifest"]).read_text())
        if manifest.get("commit") != MDK_COMMIT:
            raise ConfigError("unsupported MDK source commit")
        for name in ("wn", "wnd"):
            binary = Path(data["marmot"]["wn"]).parent / name
            private_file(binary)
            if digest(binary) != manifest.get("sha256", {}).get(name):
                raise ConfigError("MDK binary does not match source provenance")
        for section, key, low, high in (("health", "min_free_mb", 64, 1000000), ("frigate", "max_attachment_mb", 1, 1024)):
            value = data.get(section, {}).get(key, 1024 if key == "min_free_mb" else 128)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ConfigError("invalid resource bound")
        if data["mqtt"].get("tls", True) is False and not data["mqtt"].get("allow_plaintext", False):
            raise ConfigError("plaintext MQTT requires explicit allow_plaintext")
        return cls(path.absolute(), state, data)

    @property
    def bot_home(self) -> Path:
        return self.state / "bot"

    @property
    def destination_path(self) -> Path:
        return self.state / "destination.json"

    def destination(self) -> dict:
        private_file(self.destination_path)
        value = json.loads(self.destination_path.read_text())
        if value.get("config_sha256") != hashlib.sha256(self.path.read_bytes()).hexdigest():
            raise ConfigError("configuration changed; run configure.py again")
        hex_id(value["account"])
        hex_id(value["group"])
        return value

    def profile(self) -> str:
        # Owner maintains this file; notifier never creates or changes it.
        path = Path(self.data["health"]["profile_file"]).expanduser()
        private_file(path)
        profile = json.loads(path.read_text()).get("profile")
        if profile not in ("Armed", "Disarmed"):
            raise ConfigError("profile must be Armed or Disarmed")
        return profile
