#!/usr/bin/env python3
"""一次性补全脚本（weekly-05 阶段 B-3，可选）：把存量 event.json 的结构化产出/日志补写进 events 表。

背景：阶段 A 起新事件结构化产出写 events.structured_output、执行日志写 events.agent_logs；
存量历史 event.json 文件（阶段 A 之前）仍只存文件。本脚本遍历 workspace 下存量 event.json，
把 `pipeline[].output` → events.structured_output、`logs` → events.agent_logs 补写，
减少详情页对文件的回退依赖。幂等：仅覆盖 structured_output 为 NULL 的行。

用法（在服务端执行）：
  cd /opt/grape && python3 scripts/backfill_event_archive_to_events.py [--dry-run]

安全：只 UPDATE 目标行；不做删除。异常行跳过并计数。
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import WORKSPACE_DIR


def _iter_event_json_files():
    """遍历 {WORKSPACE_DIR}/**/issues/**/event.json"""
    base = WORKSPACE_DIR
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        if "event.json" in files and "issues" in root:
            yield os.path.join(root, "event.json")


def _locate_seq_repo(path) -> tuple[str | None, list]:
    """从 event.json 文件反推 repo_key（路径含 repo_key/issues/n/）。"""
    try:
        rel = os.path.relpath(path, base := WORKSPACE_DIR)
        # rel 形如：gitcode/org/repo/issues/5/event.json 或 gitcode:org/repo/... 简化处理
        if os.sep not in rel:
            return None, []
        parts = rel.split(os.sep)
        # 格式 {provider}/{owner}/{repo}/issues/{n}/event.json
        if "issues" not in parts:
            return None, []
        idx = parts.index("issues")
        owner = parts[idx - 1] if idx >= 1 else ""
        repo = parts[idx - 2] if idx >= 2 else ""
        prov = parts[idx - 3] if idx >= 3 else "gitcode"
        repo_key = f"{prov}:{owner}/{repo}"
        return repo_key, parts
    except Exception:
        return None, []


def main():
    ap = argparse.ArgumentParser(description="补写存量 event.json → events 表")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    from src.session_manager import db, db_lock
    from src.api.event_archive import _load as _load_arch

    processed = written = skipped = failed = 0
    for path in _iter_event_json_files():
        processed += 1
        try:
            repo_key, parts = _locate_seq_repo(path)
            if not repo_key:
                skipped += 1
                continue
            arch = _load_arch(path)
            if not arch:
                skipped += 1
                continue
            with db_lock:
                c = db.cursor()
                for seg in (arch.get("pipeline") or []):
                    seq = seg.get("seq")
                    if not seq:
                        continue
                    out = seg.get("output") or {}
                    logs = seg.get("logs") or []
                    _has_structured = bool(out.get("actions") or out.get("conclusion")
                                           or out.get("artifacts") or out.get("issue_summary"))
                    if not (_has_structured or logs):
                        continue
                    if args.dry_run:
                        written += 1
                        continue
                    if _has_structured:
                        c.execute(
                            "UPDATE events SET structured_output=? WHERE seq=? AND repo_key=? AND "
                            "(structured_output IS NULL OR structured_output='')",
                            (json.dumps(out, ensure_ascii=False), seq, repo_key))
                        if c.rowcount:
                            written += 1
                    if logs:
                        c.execute(
                            "UPDATE events SET agent_logs=? WHERE seq=? AND repo_key=?",
                            (json.dumps(logs, ensure_ascii=False), seq, repo_key))
                db.commit()
        except Exception as e:
            failed += 1
            print(f"WARN {path}: {e}")

    print(f"done: scanned={processed} written={written} skipped={skipped} failed={failed}"
          + (" (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()