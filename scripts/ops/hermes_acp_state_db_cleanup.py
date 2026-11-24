#!/usr/bin/env python3
"""hermes ACP state.db 一次性清理脚本（Issue #3773）。

流程：integrity_check → 备份 → 按保留窗口裁剪旧 session/message → VACUUM →
再次 integrity_check。清理后仍超阈值时归档重建（默认开启），失败自动回滚备份。
支持 dry-run 与重复执行；结果以 JSON 行输出，退出码 0=全部成功/跳过，1=存在失败。

示例：
    python3 scripts/ops/hermes_acp_state_db_cleanup.py --dry-run
    python3 scripts/ops/hermes_acp_state_db_cleanup.py --db-path ~/.hermes/state.db
    python3 scripts/ops/hermes_acp_state_db_cleanup.py --all --retention-days 7
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.domains.runtime.engine import state_db_maintenance as m  # noqa: E402


def _default_roots(args) -> list[str]:
    roots = []
    hermes_home = args.hermes_home or os.environ.get("HERMES_HOME") or "~/.hermes"
    roots.append(os.path.expanduser(hermes_home))
    iso_root = args.iso_homes_root
    if iso_root is not None:
        if iso_root:
            roots.append(os.path.expanduser(iso_root))
    else:
        roots.append(os.path.join(os.path.expanduser("~"), ".cache", "hermes-acp-homes"))
    return roots


def _targets(args) -> list[str]:
    explicit = [os.path.expanduser(p) for p in (args.db_path or []) if p]
    if explicit:
        return m._dedup(explicit)
    paths: list[str] = []
    for root in _default_roots(args):
        paths.extend(m.discover_state_dbs(root, include_children=True))
    return m._dedup(paths)


def _process_one(path: str, args) -> dict:
    max_bytes = int(float(args.max_mb) * 1024 * 1024)
    low_bytes = int(float(args.low_water_mb) * 1024 * 1024)
    before = m.db_size_bytes(path)
    if not os.path.isfile(path):
        return {"db_path": path, "result": "skipped", "reason": "not_found", "before_bytes": 0}
    if not args.all and before <= max_bytes:
        return {"db_path": path, "result": "skipped", "reason": "below_threshold",
                "before_bytes": before, "after_bytes": before}
    cleanup = m.prune_and_vacuum(
        path,
        retention_days=args.retention_days,
        min_sessions=args.min_sessions,
        backup_dir=args.backup_dir,
        target_bytes=low_bytes,
        dry_run=args.dry_run,
        lock_timeout=args.lock_timeout,
    )
    payload = cleanup.to_dict()
    if args.dry_run or cleanup.result != "ok":
        return payload
    if m.db_size_bytes(path) > max_bytes and args.archive:
        archive = m.archive_and_recreate(
            path, backup_dir=args.backup_dir, lock_timeout=args.lock_timeout)
        payload["archive"] = archive.to_dict()
        payload["after_bytes"] = archive.after_bytes
    payload["after_bytes"] = m.db_size_bytes(path)
    return payload


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", action="append", default=[],
                   help="显式指定 state.db（可重复）；缺省自动扫描 --hermes-home / --iso-homes-root")
    p.add_argument("--hermes-home", default="", help="共享 HERMES_HOME（默认 $HERMES_HOME 或 ~/.hermes）")
    p.add_argument("--iso-homes-root", default=None,
                   help="per-issue 隔离 home 根目录（默认 ~/.cache/hermes-acp-homes；传空串禁用）")
    p.add_argument("--backup-dir", default="", help="备份目录（默认 $ACP_STATE_DB_BACKUP_DIR 或 ~/.cache/hermes-acp-backups）")
    p.add_argument("--retention-days", type=float, default=m.retention_days_default())
    p.add_argument("--min-sessions", type=int, default=m.min_sessions_default())
    p.add_argument("--max-mb", type=float, default=m._env_float(m.MAX_MB_ENV, m.DEFAULT_MAX_MB))
    p.add_argument("--low-water-mb", type=float,
                   default=m._env_float(m.LOW_WATER_MB_ENV, m.DEFAULT_LOW_WATER_MB))
    p.add_argument("--dry-run", action="store_true", help="只统计候选、不删除、不备份")
    p.add_argument("--all", action="store_true", help="即使未超阈值也执行清理")
    p.add_argument("--no-archive", dest="archive", action="store_false", default=True,
                   help="清理后仍超阈值时不做归档重建")
    p.add_argument("--lock-timeout", type=float, default=0.0)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    targets = _targets(args)
    if not targets:
        print(json.dumps({"result": "skipped", "reason": "no_state_db_found",
                          "roots": _default_roots(args)}, ensure_ascii=False))
        return 0
    failed = 0
    for path in targets:
        try:
            payload = _process_one(path, args)
        except Exception as exc:  # noqa: BLE001 - 单库失败不阻断其余库
            payload = {"db_path": path, "result": "failed", "error": str(exc)[:300]}
        if payload.get("result") == "failed":
            failed += 1
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    print(json.dumps({"result": "failed" if failed else "ok",
                      "targets": len(targets), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())