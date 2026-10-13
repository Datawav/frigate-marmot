"""Official wn/wnd JSON adapter. Never import an owner's private key."""
from __future__ import annotations

import json
import os
import re
import subprocess

from .config import ConfigError, hex_id, private_dir


class WnError(RuntimeError):
    def __init__(self, code="wn_failed", uncertain=False):
        self.code = code if re.fullmatch(r"[a-z0-9_]{1,80}", code) else "wn_failed"
        self.uncertain = uncertain
        super().__init__(self.code)


class Marmot:
    def __init__(self, config, account=None, group=None, run=subprocess.run):
        self.config, self.account, self.group, self.run = config, account, group, run
        private_dir(config.bot_home)
        self.binary = config.data["marmot"]["wn"]

    def call(self, *args, effect=False, timeout=120):
        argv = [self.binary, "--home", str(self.config.bot_home), "--socket", str(self.config.bot_home / "wnd.sock"),
                "--secret-store", "file", "--json"]
        if self.account:
            argv += ["--account", self.account]
        argv += list(args)
        allowed_env = {"HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
                       "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
        env = {k: v for k, v in os.environ.items() if k in allowed_env}
        try:
            r = self.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        except (subprocess.TimeoutExpired, OSError):
            raise WnError("wn_transport_failure", uncertain=effect) from None
        try:
            envelope = json.loads(r.stdout)
        except (ValueError, TypeError):
            raise WnError("wn_invalid_response", uncertain=effect) from None
        if r.returncode != 0 or "error" in envelope:
            code = envelope.get("error", {}).get("code", "wn_failed")
            # Known validation failures have no send/upload effect. Everything else
            # at a mutation boundary is uncertain, including a lost daemon response.
            safe = code in {"media_reference_stale_epoch", "media_epoch_unsettled", "unknown_group", "not_group_member", "invalid_media_attachment"}
            raise WnError(code, uncertain=effect and not safe)
        if not isinstance(envelope.get("result"), dict):
            raise WnError("wn_invalid_response", uncertain=effect)
        return envelope["result"]

    def preflight(self):
        if self.call("daemon", "status", timeout=5).get("running") is not True:
            raise WnError("daemon_not_running")
        status = self.call("accounts", "status", timeout=5)
        if status.get("account_id") != self.account or status.get("local_signing") is not True:
            raise ConfigError("bot account is not a local signing identity")
        group = self.call("groups", "show", self.group, timeout=5).get("group", {})
        if group.get("group_id") != self.group or group.get("self_membership") != "member" or group.get("pending_confirmation") or group.get("disbanded") or group.get("unrecoverable"):
            raise ConfigError("bot group is not ready")

    def upload(self, path):
        args = ["media", "upload", self.group, str(path)]
        if self.config.data["marmot"].get("media_server"):
            args += ["--server", self.config.data["marmot"]["media_server"]]
        value = self.call(*args, effect=True)
        try:
            reference = value["attachments"][0]["media"]
            hex_id(reference["plaintext_sha256"])
            hex_id(reference["ciphertext_sha256"])
            if not reference["locators"]:
                raise ValueError()
            return reference
        except (KeyError, IndexError, ValueError, TypeError):
            raise WnError("wn_invalid_upload_receipt", uncertain=True) from None

    def send(self, caption, reference=None):
        if reference:
            value = self.call("media", "send", self.group, json.dumps(reference), "--message", caption, effect=True)
        else:
            value = self.call("messages", "send", self.group, caption, effect=True)
        ids = value.get("message_ids")
        if not isinstance(ids, list) or not ids:
            raise WnError("wn_missing_enqueue_receipt", uncertain=True)
        for i in ids:
            hex_id(i)
        return {"message_ids": ids, "published": value.get("published", 0)}

    def find_send(self, marker):
        value = self.call("messages", "search", self.group, marker)
        matches = [m for m in value.get("messages", []) if m.get("from") == self.account and
                   m.get("group_id") == self.group and marker in m.get("plaintext", "")]
        if len(matches) == 1:
            return {"message_ids": [hex_id(matches[0]["message_id"])], "published": None}
        return None
