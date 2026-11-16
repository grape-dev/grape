#!/usr/bin/env python3
"""Issue #1011 In Scope 2：autopilot_tasks 存量引擎引用升格脚本（id 模式 → uid 模式）。

契约（tests/atomic/test_issue_1011_autopilot_uid.py::TestUpgradeScript / TestUpgradeScriptRecover）：
- scan()：遍历 autopilot_tasks，返回 {"upgradable": [...], "dangling": [...], "already_uid": N}
  - 已带 engine_runtime_uids（非空）→ already_uid 计数（幂等跳过，uid 稳定不重派）；
  - 仅带 engine_runtime_ids 且全部 id 可解析（agent_runtimes.engine_uid 命中）→ upgradable；
  - ids 存在悬空 id（引擎已删除等）→ 先走 v2 历史反查（TestUpgradeScriptRecover 契约）：
    agent_tasks 历史行 pool_engine_ids ↔ pool_engine_uids 按 index 对齐（uid 终身不变，
    历史值可信），悬空 id 反查到唯一 uid → 并入 upgradable（source=history 标记）；
    反查失败（无历史/对齐校验失败/多 uid 冲突）→ dangling 清单（不静默：跳过升格 + 报告落盘，
    携带 recover 状态字段），整任务保持 ids 原样（不做部分升格——清 ids 会丢悬空 id）；
  - 纯配置任务（uids/ids 均空）→ 不进任何清单。
- apply(upgradable)：逐任务写 engine_runtime_uids（升格）并清空 engine_runtime_ids，返回升格任务数。

幂等：两跑 uid 稳定（uid 直接取自 agent_runtimes.engine_uid 现值，不重派生）；
悬空任务保持 ids 原样并每次报告，待引擎重建/重注册后再升格。

用法：
  .venv/bin/python scripts/upgrade_autopilot_engine_uids.py           # 扫描 + 升格
  .venv/bin/python scripts/upgrade_autopilot_engine_uids.py --dry-run # 仅扫描报告
"""
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.session_manager import db, db_lock  # noqa: E402

REPORT_TAG = "1011-autopilot-upgrade"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ids(raw) -> list:
    try:
        v = json.loads(raw) if raw else []
        return [int(x) for x in v if str(x).strip().lstrip("-").isdigit()]
    except Exception:
        return []


def _parse_uids(raw) -> list:
    try:
        v = json.loads(raw) if raw else []
        return [str(x) for x in v if str(x).strip()]
    except Exception:
        return []


def _history_uid_map(c, dangling_ids: set) -> "dict | None":
    """v2 历史反查：agent_tasks 历史行 pool_engine_ids ↔ pool_engine_uids 按 index
    对齐，构建 悬空id → {uid: 出现次数} 映射。uid 终身不变，历史值可信。

    防线：
    - 任一行 ids/uids 长度不匹配（双写早期空窗/坏行）→ 该行整体跳过（对齐校验失败）；
    - uid 值格式粗校验（含 '-' 且长度 ≥ 32）不通过 → 忽略该分量；
    - 返回 None 表示 agent_tasks 表缺失/查询失败（未迁移旧库容错降级）。
    """
    try:
        rows = c.execute("SELECT pool_engine_ids, pool_engine_uids FROM agent_tasks "
                         "WHERE pool_engine_ids != '' AND pool_engine_uids != ''").fetchall()
    except Exception:
        return None
    hist = {}
    for ids_raw, uids_raw in rows:
        ids, uids = _parse_ids(ids_raw), _parse_uids(uids_raw)
        if len(ids) != len(uids):
            continue  # 对齐校验失败：该行不可信，整体跳过
        for rid, uid in zip(ids, uids):
            if rid not in dangling_ids or not uid or len(uid) < 32 or "-" not in uid:
                continue
            hist.setdefault(rid, {})
            hist[rid][uid] = hist[rid].get(uid, 0) + 1
    return hist


def scan() -> dict:
    """扫描存量任务：分类 upgradable / dangling / already_uid（只读，不改行）。"""
    result = {"scanned_at": _now_iso(), "upgradable": [], "dangling": [],
              "already_uid": 0, "pure_config": 0}
    with db_lock:
        c = db.cursor()
        rows = c.execute("SELECT id, engine_runtime_ids, engine_runtime_uids "
                         "FROM autopilot_tasks").fetchall()
        uid_map = {r[0]: r[1] for r in
                   c.execute("SELECT id, engine_uid FROM agent_runtimes").fetchall()
                   if r[1]}
    for tid, ids_raw, uids_raw in rows:
        uids = _parse_uids(uids_raw)
        if uids:
            result["already_uid"] += 1
            continue
        ids = _parse_ids(ids_raw)
        if not ids:
            result["pure_config"] += 1
            continue
        resolved, dangling = [], []
        for rid in ids:
            uid = uid_map.get(rid)
            (resolved.append(uid) if uid else dangling.append(rid))
        if not dangling:
            result["upgradable"].append({"task_id": tid, "ids": ids, "uids": resolved})
            continue
        # v2 历史反查：悬空 id 尝试从 agent_tasks 历史行恢复 uid（终身不变，可信）
        with db_lock:
            c = db.cursor()
            hist = _history_uid_map(c, set(dangling))
        recovered, unrecovered = {}, []
        for rid in dangling:
            cand = (hist or {}).get(rid) or {}
            if len(cand) == 1:
                recovered[rid] = next(iter(cand))
            else:
                unrecovered.append({
                    "id": rid,
                    "reason": "conflict" if len(cand) > 1 else
                              ("history_missing" if hist is None or not cand else "none"),
                    "candidates": cand,
                })
        if unrecovered:
            # 有任一 id 无法恢复 → 整任务保持 ids 原样（不做部分升格：清 ids 会丢悬空 id）
            result["dangling"].append({
                "task_id": tid, "ids": ids, "dangling_ids": dangling,
                "resolved_uids": resolved, "recovered": recovered,
                "unrecovered": unrecovered,
                "recover": "partial" if recovered else
                           ("no_history" if hist is None or not hist else "failed"),
            })
        else:
            # 全部悬空 id 均恢复到唯一 uid → 并入 upgradable（source=history 标记）
            all_uids = resolved + [recovered[rid] for rid in dangling]
            result["upgradable"].append({"task_id": tid, "ids": ids, "uids": all_uids,
                                         "source": "history",
                                         "recovered_ids": dangling})
    _write_report(result)
    if result["dangling"]:
        # 悬空 id 不静默：显式日志取证（升级仍跳过这些任务）
        print(f"[Issue #1011] dangling engine ids not upgraded: "
              f"{json.dumps(result['dangling'], ensure_ascii=False)}", file=sys.stderr)
    return result


def apply(upgradable: list) -> int:
    """升格：写 engine_runtime_uids + 清空 engine_runtime_ids（原子，幂等）。"""
    n = 0
    with db_lock:
        c = db.cursor()
        for item in upgradable or []:
            uids = json.dumps(item.get("uids") or [])
            c.execute("UPDATE autopilot_tasks SET engine_runtime_uids=?, "
                      "engine_runtime_ids='' WHERE id=?", (uids, item["task_id"]))
            n += c.rowcount
        db.commit()
    return n


def _report_path() -> str:
    """报告路径：库文件旁 <db>.issue-1011-autopilot-upgrade-report.json（对齐迁移报告）。"""
    try:
        main = [r for r in db.execute("PRAGMA database_list").fetchall() if r[1] == "main"]
        db_file = (main[0][2] or "") if main else ""
    except Exception:
        db_file = ""
    if not db_file or db_file == ":memory:":
        return ""
    return f"{db_file}.{REPORT_TAG}-report.json"


def _write_report(result: dict) -> str:
    path = _report_path()
    if not path:
        return ""
    try:
        payload = {"generated_at": _now_iso(), "issue": REPORT_TAG, "report": result}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except Exception as e:
        print(f"[Issue #1011] 升格报告落盘失败（不阻断）: {e}", file=sys.stderr)
        return ""


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    result = scan()
    report_file = _report_path()
    print(f"[Issue #1011] scan: already_uid={result['already_uid']} "
          f"upgradable={len(result['upgradable'])} dangling={len(result['dangling'])} "
          f"pure_config={result['pure_config']} report={report_file or '(skipped)'}")
    if dry_run:
        return 0
    n = apply(result["upgradable"])
    print(f"[Issue #1011] upgraded tasks: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
