#!/usr/bin/env python3
"""Issue #3914 E：场景节点「存名必存 id」存量回填（灰度：默认只出对照清单）。

背景：`devops_pipeline_scene` / `scene_market` 的 nodes_json 存在只落 `skill` 名、
`skill_id=0` 的节点（新建/存量场景都会产出），派发侧只能按名解析——三档同名时无法
锁定档位，direct 链路更容易丢绑定（#617 官方检查器兜底 → #887 门禁误拒）。

本脚本按与派发侧相同的解析口径（user→repo→official，`resolve_skill_id`）补 `skill_id`：
- 默认 dry-run：逐节点打印对照清单（表 / 行 / scene / tag / skill / 解析 id / 归属档），不写库；
- `--apply` 才写库，幂等（已有 skill_id 跳过；解析不到保持原样）；
- `--json` 输出结构化清单，便于人工确认后批量执行。

用法：
    python scripts/backfill_scene_nodes_skill_id.py            # dry-run 对照清单
    python scripts/backfill_scene_nodes_skill_id.py --json     # 结构化对照清单
    python scripts/backfill_scene_nodes_skill_id.py --apply    # 灰度确认后执行回填

环境：DEVOPS_DB_PATH 指向待处理库（默认 data/sessions.db）。
验证：apply 后 `--json` 的 would_update 应为 0（除解析不到的节点）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.kernel.session_manager import db, db_lock  # noqa: E402

_SCENE_SQL = ("SELECT id, scene_id, repo_key, nodes_json, COALESCE(created_by, '') "
              "FROM devops_pipeline_scene")
_MARKET_SQL = ("SELECT id, name, COALESCE(repo_key, ''), nodes_json, "
               "COALESCE(source_user_id, 0) FROM scene_market")


def _uid_of_username(username: str) -> int:
    if not username:
        return 0
    try:
        from src.domains.mgmt.public import account_id_by_username_ci_any
        return int(account_id_by_username_ci_any(username) or 0)
    except Exception:
        return 0


def _skill_scope(skill_id: int) -> str:
    try:
        from src.domains.mgmt.public import get_skill_by_id
        _sk = get_skill_by_id(skill_id) or {}
        return str(_sk.get("scope") or "unknown")
    except Exception:
        return "unknown"


def _scan_row(table: str, row_id: int, ident: str, repo_key: str, uid: int,
              nodes_json: str) -> tuple:
    """返回 (changed, nodes, candidates)。candidates 为对照清单条目。"""
    from src.domains.mgmt.user.db import resolve_skill_id
    try:
        nodes = json.loads(nodes_json or "[]")
    except Exception as e:
        print(f"[skip] {table} id={row_id} bad nodes_json: {e}", file=sys.stderr)
        return False, None, []
    if not isinstance(nodes, list):
        return False, None, []
    changed = False
    candidates = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        name = str(n.get("skill") or "").strip()
        if not name or n.get("skill_id"):
            continue
        sid = int(resolve_skill_id(name, repo_key=repo_key or "", user_id=uid or 0) or 0)
        candidates.append({"table": table, "row_id": row_id, "scene": ident,
                           "repo_key": repo_key, "tag": n.get("tag") or "",
                           "skill": name, "resolved_skill_id": sid,
                           "scope": _skill_scope(sid) if sid else ""})
        if sid:
            n["skill_id"] = sid
            changed = True
    return changed, nodes, candidates


def collect() -> tuple:
    """遍历两张场景表，返回 (清单, 待写行)。待写行 = [(table, row_id, nodes_json)]。"""
    candidates = []
    pending = []
    with db_lock:
        c = db.cursor()
        rows = []
        for table, sql in (("devops_pipeline_scene", _SCENE_SQL),
                           ("scene_market", _MARKET_SQL)):
            try:
                c.execute(sql)
                for r in c.fetchall() or []:
                    rows.append((table, r))
            except Exception as e:  # 旧库缺表/缺列：跳过该表（不阻断另一张）
                print(f"[skip] {table} read failed: {e}", file=sys.stderr)
    for table, r in rows:
        if table == "devops_pipeline_scene":
            row_id, ident, repo_key, nodes_json, created_by = r
            uid = _uid_of_username(created_by)
        else:
            row_id, ident, repo_key, nodes_json, source_user_id = r
            uid = int(source_user_id or 0)
        changed, nodes, cand = _scan_row(table, row_id, ident, repo_key or "", uid,
                                         nodes_json)
        candidates.extend(cand)
        if changed and nodes is not None:
            pending.append((table, row_id,
                            json.dumps(nodes, ensure_ascii=False)))
    return candidates, pending


def apply(pending: list) -> int:
    sql = {"devops_pipeline_scene": "UPDATE devops_pipeline_scene SET nodes_json=? WHERE id=?",
           "scene_market": "UPDATE scene_market SET nodes_json=? WHERE id=?"}
    affected = 0
    with db_lock:
        c = db.cursor()
        for table, row_id, nodes_json in pending:
            try:
                c.execute(sql[table], (nodes_json, row_id))
                affected += 1
            except Exception as e:
                print(f"[fail] {table} id={row_id}: {e}", file=sys.stderr)
        db.commit()
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Issue #3914 backfill scene node skill_id (default dry-run)")
    parser.add_argument("--apply", action="store_true", help="write back; default dry-run")
    parser.add_argument("--json", action="store_true", help="output candidate list as JSON")
    args = parser.parse_args()

    candidates, pending = collect()
    unresolved = [c for c in candidates if not c["resolved_skill_id"]]
    if args.json:
        print(json.dumps({"candidates": candidates,
                          "rows_to_update": len(pending),
                          "would_update": sum(1 for c in candidates
                                              if c["resolved_skill_id"]),
                          "unresolved": len(unresolved)},
                         ensure_ascii=False, indent=2))
    else:
        print(f"{'table':<24} {'row':>6}  {'scene':<28} {'tag':<8} {'skill':<28} "
              f"{'skill_id':>8}  scope")
        for c in candidates:
            print(f"{c['table']:<24} {c['row_id']:>6}  {str(c['scene'])[:28]:<28} "
                  f"{str(c['tag'])[:8]:<8} {str(c['skill'])[:28]:<28} "
                  f"{c['resolved_skill_id']:>8}  {c['scope']}")
        print(f"\n合计: 候选 {len(candidates)} 节点 / 可回填 "
              f"{sum(1 for c in candidates if c['resolved_skill_id'])} / "
              f"解析不到 {len(unresolved)} / 待写行 {len(pending)}")
    if not args.apply:
        print("dry-run：以上仅为对照清单，确认后加 --apply 执行回填。")
        return 0
    affected = apply(pending)
    print(f"已回填 {affected} 行（幂等：可重复执行）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())