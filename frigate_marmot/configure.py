"""Dedicated identity bootstrap and destination setup with persisted intents."""
from __future__ import annotations

import hashlib
import json

from . import RELAYS
from .config import ConfigError, hex_id, npub_hex, private_file, write_json
from .marmot import Marmot


def adopt_bot(config, recipient=None, group=None):
    if config.destination_path.exists() or (config.state / "setup.json").exists():
        raise ConfigError("bot setup already exists; use its recorded recovery path")
    if bool(recipient) == bool(group):
        raise ConfigError("choose exactly one destination")
    target = npub_hex(recipient) if recipient else hex_id(group)
    wn = Marmot(config)
    accounts = wn.call("accounts", "list").get("accounts", [])
    if len(accounts) != 1 or accounts[0].get("local_signing") is not True:
        raise ConfigError("adoption requires exactly one explicitly created bot")
    write_json(config.state / "setup.json", {"account": hex_id(accounts[0]["account_id"]),
               "target": target, "mode": "recipient" if recipient else "group", "phase": "identity_ready"})


def recover_setup(config, group):
    path = config.state / "setup.json"
    private_file(path)
    value = json.loads(path.read_text())
    group = hex_id(group)
    if value.get("mode") != "recipient" or value.get("phase") not in ("group_intent", "group_ready"):
        raise ConfigError("no interrupted recipient-group intent to reconcile")
    if value.get("group") and value["group"] != group:
        raise ConfigError("cannot replace a saved group")
    wn = Marmot(config, hex_id(value["account"]), group)
    wn.preflight()
    members = wn.call("groups", "members", group).get("members", [])
    if not {value["account"], value["target"]} <= {m.get("member_id") for m in members}:
        raise ConfigError("group does not contain the intended bot and recipient")
    value.update(group=group, phase="group_ready")
    write_json(path, value)


def configure(config, recipient=None, group=None):
    if bool(recipient) == bool(group):
        raise ConfigError("choose exactly one of --recipient or --group")
    target = npub_hex(recipient) if recipient else hex_id(group)
    setup_path = config.state / "setup.json"
    setup = {}
    if setup_path.exists():
        private_file(setup_path)
        setup = json.loads(setup_path.read_text())
    if setup.get("target") and (setup["target"] != target or setup.get("mode") != ("recipient" if recipient else "group")):
        raise ConfigError("destination changes require a separate state directory; pending jobs keep their destination")
    wn = Marmot(config)
    if wn.call("daemon", "status").get("running") is not True:
        raise ConfigError("start the dedicated daemon service before configuring")
    accounts = wn.call("accounts", "list").get("accounts", [])
    if setup.get("account"):
        account = setup["account"]
        if len(accounts) != 1 or accounts[0].get("account_id") != account:
            raise ConfigError("dedicated bot home has unexpected accounts")
    elif accounts:
        if len(accounts) != 1 or setup.get("phase") != "identity_intent" or not accounts[0].get("local_signing"):
            raise ConfigError("use an empty dedicated bot home; never reuse a chat account")
        account = hex_id(accounts[0]["account_id"])
    else:
        if setup.get("phase") == "identity_intent":
            raise ConfigError("identity creation outcome unknown; inspect dedicated bot home before recovery")
        setup = {"target": target, "mode": "recipient" if recipient else "group", "phase": "identity_intent"}
        write_json(setup_path, setup)
        result = wn.call("create-identity", effect=True)
        account = hex_id(result["account_id"])
    setup.update(account=account, target=target, mode="recipient" if recipient else "group")
    # Preserve group_intent across recovery, never silently replay group creation.
    if setup.get("phase") != "group_intent":
        setup["phase"] = "identity_ready"
    write_json(setup_path, setup)
    wn.account = account
    if recipient:
        if setup.get("group"):
            group = setup["group"]
        elif setup.get("phase") == "group_intent":
            raise ConfigError("group creation outcome unknown; inspect groups list and recover explicitly")
        else:
            package = wn.call("keys", "fetch", recipient, "--bootstrap-relays", ",".join(RELAYS))
            if package.get("account_id") != target or package.get("key_package_bytes", 0) <= 0:
                raise ConfigError("recipient has no usable published Marmot KeyPackage")
            setup["phase"] = "group_intent"
            write_json(setup_path, setup)
            created = wn.call("groups", "create", "Camera alerts", recipient, effect=True)
            group = hex_id(created["group_id"])
            setup["group"] = group
            setup["phase"] = "group_ready"
            write_json(setup_path, setup)
    wn.group = hex_id(group)
    wn.preflight()
    members = wn.call("groups", "members", wn.group).get("members", [])
    ids = {m.get("member_id") for m in members}
    if account not in ids or (recipient and target not in ids):
        raise ConfigError("destination membership could not be verified")
    write_json(config.destination_path, {"account": account, "group": wn.group,
               "recipient": recipient, "config_sha256": hashlib.sha256(config.path.read_bytes()).hexdigest()})
    return {"bot_account": account, "group": wn.group, "invitation_acceptance_required": bool(recipient)}
