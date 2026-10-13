"""Owner commands. Status never exports message text, credentials or media."""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
from pathlib import Path

from .config import Config, ConfigError, hex_id
from .configure import adopt_bot, configure, recover_setup
from .marmot import WnError
from .store import Store


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/frigate-marmot/config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("check-config")
    sub.add_parser("status")
    sub.add_parser("inspect-jobs")
    recovery = sub.add_parser("recover-setup")
    recovery.add_argument("--group", required=True)
    retry = sub.add_parser("retry-uncertain")
    retry.add_argument("--job", required=True)
    retry.add_argument("--acknowledge-possible-duplicate", action="store_true", required=True)
    adopt = sub.add_parser("adopt-bot")
    selection = adopt.add_mutually_exclusive_group(required=True)
    selection.add_argument("--recipient")
    selection.add_argument("--group")
    setup = sub.add_parser("configure")
    target = setup.add_mutually_exclusive_group(required=True)
    target.add_argument("--recipient")
    target.add_argument("--group")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = Config.load(arguments.config)
        if arguments.command == "check-config":
            config.destination()
            return 0
        if arguments.command in ("status", "inspect-jobs"):
            store = Store(config.state / "queue.sqlite3")
            rows = store.db.execute("SELECT state,count(*) FROM jobs GROUP BY state").fetchall()
            if arguments.command == "status":
                bad = store.db.execute("SELECT value FROM metadata WHERE key='invalid_mqtt_payload'").fetchone()
                print(json.dumps({"jobs": dict(rows), "invalid_mqtt_payloads": int(bad[0]) if bad else 0,
                                  "phone_delivery": "not_confirmed_by_connector"}))
            else:
                print(json.dumps({"uncertain": [dict(r) for r in store.db.execute("SELECT id,kind,error FROM jobs WHERE state='uncertain'")]}))
            store.close()
            return 0
        # Configuration and runtime must not race destination or staging changes.
        with (config.state / "owner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if arguments.command == "configure":
                print(json.dumps(configure(config, arguments.recipient, arguments.group)))
            elif arguments.command == "adopt-bot":
                adopt_bot(config, arguments.recipient, arguments.group)
            elif arguments.command == "recover-setup":
                recover_setup(config, arguments.group)
            elif arguments.command == "retry-uncertain":
                store = Store(config.state / "queue.sqlite3")
                identity = hex_id(arguments.job)
                with store.transaction():
                    row = store.db.execute("SELECT state,reference FROM jobs WHERE id=?", (identity,)).fetchone()
                    if not row or row["state"] != "uncertain":
                        raise ConfigError("only an uncertain job can be retried explicitly")
                    store.update(identity, "uploaded" if row["reference"] else "scheduled", due=0, error="owner_acknowledged_possible_duplicate")
                store.close()
            else:
                from .runner import run
                run(config)
        return 0
    except (ConfigError, WnError, OSError, KeyError, ValueError, RuntimeError):
        logging.error("operation failed; check private configuration, service state and recovery guide")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
