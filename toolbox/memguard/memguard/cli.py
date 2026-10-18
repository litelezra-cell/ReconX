"""memguard.cli: two command-line tools.

  `memguard-preflight <bytes> [--ask-release] [--project NAME]`
      Queries every registered daemon, `ollama ps`, and the whole-machine gate. Exits 0 if
      the load may proceed, 1 if it was refused (full refusal message on stderr).

  `memguard-demo --project NAME --gb N [--hold-seconds S]`
      **Live demonstration without models** (see protocol.md): leases N **simulated**
      gigabytes (without touching real memory) in the global ledger, holds them for
      `hold-seconds` seconds (default 20), then releases. A second demo process with a
      different `--gb` is refused if the two together exceed the machine, and the message
      names the project and owner of the first holder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from memguard import preflight as preflight_module
from memguard.broker import MemoryBroker, MemoryReserveError

_BYTES_PER_GB = 1024**3


def main_preflight(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="memguard-preflight", description=__doc__)
    parser.add_argument("bytes", type=int, help="requested number of bytes (for example 75000000000)")
    parser.add_argument("--gb", type=float, default=None, help="convenient alternative to bytes: amount in GB")
    parser.add_argument("--ask-release", action="store_true", help="ask the first daemon found holding models to release them")
    parser.add_argument("--context", default="memguard-preflight (CLI)")
    parser.add_argument("--json", action="store_true", help="raw JSON output instead of human-readable text")
    args = parser.parse_args(argv)

    estimated_bytes = int(args.gb * _BYTES_PER_GB) if args.gb is not None else int(args.bytes)

    try:
        report = preflight_module.preflight_standalone_load(
            estimated_bytes, context=args.context, ask_daemon_release=args.ask_release
        )
    except (preflight_module.PreflightRefusedError, MemoryReserveError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc), "code": getattr(exc, "code", None)}, ensure_ascii=False))
        else:
            print(f"Refused: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, "report": report}, ensure_ascii=False, default=str))
    else:
        print(f"Passed. Checked: {[c['name'] for c in report['checks']]}")
    return 0


def main_demo(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="memguard-demo", description=__doc__)
    parser.add_argument("--project", required=True, help="project name (written to the ledger and shown in refusal messages)")
    parser.add_argument("--gb", type=float, required=True, help="simulated lease size, in GB")
    parser.add_argument("--purpose", default="memguard live demo")
    parser.add_argument("--hold-seconds", type=float, default=20.0, help="how long to hold the lease before releasing it")
    args = parser.parse_args(argv)

    broker = MemoryBroker(project=args.project, require_kv_ceiling=False, sweep_interval_s=0)
    estimated_bytes = int(args.gb * _BYTES_PER_GB)
    owner = f"demo:{args.project}"
    print(f"[{args.project}] pid={os.getpid()} requesting {args.gb:.1f}GB ...", flush=True)
    try:
        lease = broker.reserve(owner, estimated_bytes, args.purpose)
    except MemoryReserveError as exc:
        print(f"[{args.project}] refused: {exc}", flush=True)
        return 1

    print(f"[{args.project}] granted lease_id={lease.lease_id}, holding for {args.hold_seconds:.0f}s ...", flush=True)
    try:
        time.sleep(args.hold_seconds)
    finally:
        lease.release()
        print(f"[{args.project}] released.", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_preflight())
