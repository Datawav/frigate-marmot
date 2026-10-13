#!/usr/bin/env python3
"""Convenience wrapper: configure.py --recipient NPUB or --group HEX_GROUP_ID."""
import sys
from frigate_marmot.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    global_args = []
    if "--config" in args:
        i = args.index("--config")
        global_args = args[i:i + 2]
        del args[i:i + 2]
    raise SystemExit(main(global_args + ["configure"] + args))
