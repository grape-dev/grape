"""gitsync command-line entry point."""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import exitcodes
from .config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitsync", description="GitCode sync toolchain")
    parser.add_argument("--config", help="path to config YAML (or $GITSYNC_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_)

    p = add("run", "one-shot: sync + publish + db-update")
    p.add_argument("--multica-issue", required=True, help="multica issue id for DB write-back")
    p.add_argument("--force", action="store_true", help="allow non-fast-forward/overwrite")
    p.add_argument("--verbose", action="store_true", help="print detailed logs")

    p = add("sync", "incremental multi-target git sync")
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true")

    p = add("check", "read-only idempotency/guard check")
    p.add_argument("--json", action="store_true")

    p = add("lookup", "bidirectional commit traceability")
    p.add_argument("sha", help="40-hex commit sha")
    p.add_argument("--json", action="store_true")

    p = add("bootstrap", "first-run init with probe-first")
    p.add_argument("--dry-run", action="store_true", help="probe only, write nothing")
    p.add_argument("--force", action="store_true",
                   help="allow full rewrite for non-fast-forward targets")
    p.add_argument("--verbose", action="store_true")

    p = add("map", "map file maintenance")
    map_sub = p.add_subparsers(dest="map_action", required=True)
    map_sub.add_parser("rebuild", help="rebuild map from scratch")

    p = add("publish", "ClawHub skill publish")
    p.add_argument("--force", action="store_true")

    p = add("db-update", "write-back platform status by multica issue id")
    p.add_argument("--multica-issue", required=True, help="multica issue id")
    p.add_argument("--target", default=None, help="github|gitcode|clawhub (default: all)")

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"gitsync: error: {e}", file=sys.stderr)
        return exitcodes.EXIT_GUARD

    from . import commands

    handler_name = {
        "db-update": "cmd_db_update",
    }.get(args.command, f"cmd_{args.command}")
    handler = getattr(commands, handler_name, None)
    if handler is None:
        print(f"gitsync: error: unknown command: {args.command}", file=sys.stderr)
        return exitcodes.EXIT_GUARD

    # ---- single-instance lock for `run` (queue-and-wait, not fail-fast) ----
    # Serializes concurrent distribution runs (shared map/manifest/ClawHub
    # version state). Lives here — not just in agent-run.sh — so runs spawned
    # directly via `gitsync run` are protected too. Timeout via
    # GITSYNC_LOCK_WAIT_SEC (agent-run.sh forwards its --timeout).
    run_lock = None
    if args.command == "run":
        from .runlock import RunLock, lock_path_for_config, wait_sec_from_env
        run_lock = RunLock(
            lock_path_for_config(config),
            wait_sec=wait_sec_from_env(),
        )
        try:
            run_lock.acquire()
        except Exception as e:  # noqa: BLE001 - lock timeout etc.
            print(f"gitsync: error: {e}", file=sys.stderr)
            return exitcodes.EXIT_GUARD
        print(f"gitsync: acquired run lock ({run_lock.lock_path})")
    try:
        return handler(config, args)
    except Exception as e:  # noqa: BLE001 - top-level catch
        print(f"gitsync: error: {e}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return exitcodes.EXIT_GUARD
    finally:
        if run_lock is not None:
            run_lock.release()


if __name__ == "__main__":
    sys.exit(main())
