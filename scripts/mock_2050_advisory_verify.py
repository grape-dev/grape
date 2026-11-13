"""#2050 交接「场景序推导只告警不派发」改动 —— mock 数据功能验证 + 性能基准。

设计口径（不碰远端、不压生产）：
- 真实函数：patrol.detect_broken_chains / patrol_compensate.compensate_finding /
  pipeline_status.compute_stall_state / patrol.resolve_handoff_target。
- 真实 SQL：sqlite 内存库，建表用 src/api/db_setup.py 的原 DDL（列/索引与线上同构）。
- 只打桩网络：DispatchHandler 换成记录器（任何派发尝试都会被记下来，用于证伪）。

跑法：
    .venv/bin/python scripts/mock_2050_advisory_verify.py            # 功能 + 性能
    .venv/bin/python scripts/mock_2050_advisory_verify.py --perf    # 只跑性能
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCENE = "scene-linear-2050"
NODES = [{"id": "vod", "next": "issue"}, {"id": "issue", "next": "coding"},
         {"id": "coding", "next": "qa"}, {"id": "qa", "next": "sre"},
         {"id": "sre", "next": "done"}]
REPO = "gitcode:mock/owner-repo"


# ====== 1. 内存库：原 DDL + 原索引 ======

def build_db() -> tuple[sqlite3.Connection, threading.RLock]:
    from src.api import db_setup as ds

    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = None
    for ddl in (ds._EVENTS_TABLE_DDL, ds._AGENT_SESSIONS_TABLE_DDL, ds._ISSUE_DDL,
                ds._ISSUE_STATE_DDL, ds._AGENT_TASKS_DDL, ds._PATROL_FINDINGS_DDL,
                ds._META_TABLE_DDL, ds._PIPELINE_SCENE_DDL):
        con.execute(ddl)
    # 与 db_setup 初始化段同款索引（判据查询依赖这些索引）
    for sql in ("CREATE INDEX IF NOT EXISTS idx_events_repo_issue ON events(repo_key, issue_id)",
                "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)",
                "CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status)",
                "CREATE INDEX IF NOT EXISTS idx_agent_tasks_event ON agent_tasks(event_seq)"):
        con.execute(sql)
    con.commit()

    for _m in ("src.user.db", "src.domains.mgmt.user.db"):
        try:
            _mod = __import__(_m, fromlist=["_MANAGED_REPOS_DDL"])
            con.execute(_mod._MANAGED_REPOS_DDL)
            break
        except Exception:
            continue
    # issue 表在现网由迁移补列（stage/scene_id），DDL 常量里还没有——同构补上
    for col in ("stage TEXT DEFAULT ''", "scene_id TEXT DEFAULT ''"):
        try:
            con.execute(f"ALTER TABLE issue ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    con.execute("INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active) "
                "VALUES (?, ?, ?, ?, 1)",
                (SCENE, "linear-2050", "", json.dumps(NODES, ensure_ascii=False)))
    con.commit()
    return con, threading.RLock()


def patch_runtime(con, lock):
    """把 session_manager 的 db/db_lock 换成内存库（先于业务模块导入）。"""
    import src.session_manager as sm
    sm.db = con
    sm.db_lock = lock
    from src.api import pipeline_config
    pipeline_config.invalidate_scene_cache()
    pipeline_config._SCENE_COLUMNS_CACHE = None
    return sm


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S")


# ====== 2. 造数 ======

def seed_issue(con, num: int, *, stage: str = "coding", status: str = "processing",
               text: str = "", minutes_ago: float = 30.0,
               segment_agents=("vod", "issue", "coding"),
               newer_after: str = "", admin_after: bool = False,
               structured_ref: int | None = None, agent_type: str = "coding") -> tuple[str, int]:
    """造一条 Issue + 历史段 + 一条 completed 任务；返回 (task_id, source_seq)。"""
    issue_id = f"#{num}"
    con.execute("INSERT INTO issue (repo_key, issue_number, title, status, stage, scene_id, "
                "user_id, assignee) VALUES (?, ?, ?, ?, ?, ?, 0, 'mock-assignee')",
                (REPO, num, f"mock issue {num}", status, stage, SCENE))
    con.execute("INSERT INTO issue_state (repo_key, issue_id, stage, owner_agent, scene_id) "
                "VALUES (?, ?, ?, ?, ?)", (REPO, issue_id, stage, stage, SCENE))
    seq = 0
    for i, agent in enumerate(segment_agents):
        mins = minutes_ago + (len(segment_agents) - i) * 5
        cur = con.execute(
            "INSERT INTO events (repo_key, event_type, issue_id, agent_type, status, "
            "payload, created_at, completed_at) VALUES (?, 'a2a_event', ?, ?, 'completed', ?, ?, ?)",
            (REPO, issue_id, agent, json.dumps({"source_agent": agent}), _iso(mins), _iso(mins)))
        seq = cur.lastrowid or 0
    if admin_after:
        con.execute("INSERT INTO events (repo_key, event_type, issue_id, status, payload, "
                    "created_at) VALUES (?, 'admin_action', ?, 'done', '{}', ?)",
                    (REPO, issue_id, _iso(minutes_ago - 1)))
    if newer_after:
        con.execute("INSERT INTO events (repo_key, event_type, issue_id, agent_type, status, "
                    "payload, created_at) VALUES (?, 'a2a_event', ?, ?, 'running', '{}', ?)",
                    (REPO, issue_id, newer_after, _iso(minutes_ago - 1)))
    if structured_ref is not None:
        con.execute("INSERT INTO events (repo_key, event_type, issue_id, agent_type, status, "
                    "payload, created_at) VALUES (?, 'a2a_event', ?, 'qa', 'queued', ?, ?)",
                    (REPO, issue_id, json.dumps({"source_seq": structured_ref}), _iso(1)))
    tid = f"task-{num}"
    con.execute(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
        "repo_key, agent_type, prompt, status, completed_at, result_text) "
        "VALUES (?, 'sess-mock', 0, ?, ?, ?, ?, 'mock', 'completed', ?, ?)",
        (tid, seq, issue_id, REPO, agent_type, _iso(minutes_ago), text))
    con.commit()
    return tid, seq


def seed_scale(con, n: int):
    """万人级背景数据：n 条 Issue × 6 段 a2a_event + 1 条 admin_action + completed 任务。"""
    con.execute("DELETE FROM events")
    con.execute("DELETE FROM agent_tasks")
    con.execute("DELETE FROM issue")
    con.execute("DELETE FROM issue_state")
    con.execute("DELETE FROM grape_t_patrol_findings")
    con.commit()
    base = 100000
    agents = ("vod", "issue", "coding", "qa", "sre", "done")
    for i in range(n):
        num = base + i
        issue_id = f"#{num}"
        con.execute("INSERT INTO issue (repo_key, issue_number, title, status, stage, scene_id, "
                    "user_id, assignee) VALUES (?, ?, ?, 'processing', 'coding', ?, 0, 'mock')",
                    (REPO, num, f"i{num}", SCENE))
        last_seq = 0
        rows = []
        for j, agent in enumerate(agents):
            mins = 40 + (len(agents) - j) * 5
            rows.append((REPO, issue_id, agent, json.dumps({"a": agent}), _iso(mins), _iso(mins)))
        con.executemany(
            "INSERT INTO events (repo_key, event_type, issue_id, agent_type, status, payload, "
            "created_at, completed_at) VALUES (?, 'a2a_event', ?, ?, 'completed', ?, ?, ?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])
        con.execute("INSERT INTO events (repo_key, event_type, issue_id, status, payload, created_at) "
                    "VALUES (?, 'admin_action', ?, 'done', '{}', ?)", (REPO, issue_id, _iso(10)))
        last_seq = con.execute("SELECT MAX(seq) FROM events WHERE issue_id=?", (issue_id,)).fetchone()[0]
        con.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, completed_at, result_text) "
            "VALUES (?, 'sess-mock', 0, ?, ?, ?, 'coding', 'mock', 'completed', ?, '')",
            (f"task-{num}", last_seq, issue_id, REPO, _iso(30)))
        if i % 200 == 0:
            con.commit()
    con.commit()


# ====== 3. 功能验证 ======

class DispatchSpy:
    """替换 DispatchHandler：只记录不派发（证伪「有没有真的去派人」）。"""
    calls: list = []

    async def handle(self, ctx):
        DispatchSpy.calls.append({"target": ctx.target_agent, "verdict": ctx.verdict})
        return True


def run_functional(con, lock) -> list[tuple[str, bool, str]]:
    from src.api import patrol as p
    from src.api import patrol_compensate as pc
    from src.api import pipeline_status as ps

    results: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    # --- 场景 1：末段完成 / 空文本 / 场景可推导 → 只告警不派发
    tid, seq = seed_issue(con, 1001, text="", segment_agents=("vod", "issue", "coding"))
    DispatchSpy.calls = []
    findings = p.detect_broken_chains("", known=set())
    hit = [f for f in findings if f["task_id"] == tid]
    check("场景1 空文本+可推导 → 产生断链 finding", bool(hit), f"findings={len(findings)}")
    if hit:
        d = json.loads(hit[0]["detail"])
        check("场景1 finding 标注 target_source=pipeline 且给出建议目标",
              d["target_source"] == "pipeline" and d["target_agent"] == "qa",
              f"{d['target_source']}/{d['target_agent']}")
        check("场景1 建议文案声明「仅告警，不自动派发」",
              "不自动派发" in hit[0]["suggestion"], hit[0]["suggestion"])
    p.insert_findings_batch(hit)
    fid = con.execute("SELECT id FROM grape_t_patrol_findings WHERE task_id=?", (tid,)).fetchone()[0]
    orig = pc.DispatchHandler
    pc.DispatchHandler = DispatchSpy
    try:
        verdict = asyncio.run(pc.compensate_finding(fid))
    finally:
        pc.DispatchHandler = orig
    check("场景1 补偿终检落 pipeline_target_advisory", verdict == "pipeline_target_advisory", verdict)
    row = con.execute("SELECT status, action_meta FROM grape_t_patrol_findings WHERE id=?",
                      (fid,)).fetchone()
    check("场景1 finding 落 needs_human（不进 open 批处理）", row[0] == "needs_human", row[0])
    check("场景1 建议目标 qa 保留在 action_meta 供人工确认", "qa" in (row[1] or ""), row[1] or "")
    check("场景1 派发零次（DispatchHandler 未被调用）", DispatchSpy.calls == [], str(DispatchSpy.calls))

    # --- 场景 2：agent 已声明目标（结构化 next_step）→ 仍可补发
    tid2, _ = seed_issue(con, 1002, text=json.dumps({"next_step": "a2a-qa", "conclusion": "done"}),
                         segment_agents=("vod", "issue", "coding"))
    ctx = pc.CompensateContext(finding_id="f-scene2", task_id=tid2, repo_key=REPO,
                               issue_id="#1002", status="claiming")
    ok = pc.RecheckHandler().handle(ctx)
    check("场景2 文本已声明目标 → 终检放行（照旧可补发）",
          ok is True and ctx.target_agent == "qa", f"ok={ok} target={ctx.target_agent}")

    # --- 场景 3：结构化 handoff 引用源段 seq → 不报（已交棒）
    tid3, seq3 = seed_issue(con, 1003, text="", segment_agents=("vod", "issue", "coding"))
    seed_issue(con, 1004, text="", segment_agents=("vod", "issue", "coding"), newer_after="qa")
    con.execute("INSERT INTO events (repo_key, event_type, issue_id, agent_type, status, payload, "
                "created_at) VALUES (?, 'a2a_event', '#1003', 'qa', 'queued', ?, ?)",
                (REPO, json.dumps({"source_seq": seq3}), _iso(1)))
    con.commit()
    findings = p.detect_broken_chains("", known=set())
    check("场景3 结构化 handoff 命中 → #1003 不报", not [f for f in findings if f["task_id"] == tid3],
          str([f["task_id"] for f in findings]))
    check("场景4 已有新段（#1004 qa running）→ 不报（链在推进）",
          not [f for f in findings if f["task_id"] == "task-1004"],
          str([f["task_id"] for f in findings]))

    # --- 场景 5：审计事件（admin_action）不算新段 → 仍要报
    con.execute("DELETE FROM events WHERE issue_id='#1003' AND agent_type='qa'")
    con.commit()
    tid5, _ = seed_issue(con, 1005, text="", segment_agents=("vod", "issue", "coding"),
                         admin_after=True)
    findings = p.detect_broken_chains("", known=set())
    check("场景5 admin_action 不抑制停滞检测 → #1005 仍报",
          any(f["task_id"] == tid5 for f in findings), str([f["task_id"] for f in findings]))

    # --- 场景 6：awaiting_input 暂停态 → 不报
    tid6, _ = seed_issue(con, 1006, text="", status="awaiting_input",
                         segment_agents=("vod", "issue", "coding"))
    findings = p.detect_broken_chains("", known=set())
    check("场景6 awaiting_input → 不报（等用户，不自动补）",
          not any(f["task_id"] == tid6 for f in findings), str([f["task_id"] for f in findings]))

    # --- 场景 7：场景推导不出来（stage=done）→ 不派发，落 target_unresolvable
    tid7, _ = seed_issue(con, 1007, text="", stage="ghost",
                         segment_agents=("vod", "issue", "ghost"), agent_type="ghost")
    findings = p.detect_broken_chains("", known=set())
    hit7 = [f for f in findings if f["task_id"] == tid7]
    if hit7:
        p.insert_findings_batch(hit7)
        fid7 = con.execute("SELECT id FROM grape_t_patrol_findings WHERE task_id=?",
                           (tid7,)).fetchone()[0]
        DispatchSpy.calls = []
        pc.DispatchHandler = DispatchSpy
        try:
            v7 = asyncio.run(pc.compensate_finding(fid7))
        finally:
            pc.DispatchHandler = orig
        check("场景7 推导不出目标 → needs_human 且不派发",
              v7 in ("target_unresolvable", "false_positive") and DispatchSpy.calls == [],
              f"{v7} calls={DispatchSpy.calls}")
    else:
        check("场景7 推导不出目标 → 亦不进入派发", True, "no finding（终态口径）")

    # --- 场景 8：停滞徽标（看板纯函数）
    ev = [{"seq": 1, "status": "completed", "agent_type": "coding", "event_type": "a2a_event",
           "created_at": _iso(30), "completed_at": _iso(30)}]
    st = ps.compute_stall_state(ev, "coding", "processing", confirm_minutes=10)
    check("场景8 停滞态可计算", st["stalled"] is True and st["stall_state"] == ps.STALL_STATE_STALLED,
          str(st))
    return results


# ====== 4. 性能基准 ======

def _timed(fn, repeat=1):
    t0 = time.perf_counter()
    out = None
    for _ in range(repeat):
        out = fn()
    return (time.perf_counter() - t0) / max(repeat, 1), out


def run_perf(con, lock) -> dict:
    from src.api import patrol as p
    from src.api import patrol_compensate as pc

    sql_count = {"n": 0}
    con.set_trace_callback(lambda s: sql_count.__setitem__("n", sql_count["n"] + 1))

    out = {}

    # --- 单函数成本（1 万 Issue 库上，逐条计时）
    seed_scale(con, 200)
    issue = "#100001"
    task = "task-100001"
    per = {}
    sql_count["n"] = 0
    per["has_newer_issue_event"], _ = _timed(
        lambda: p.has_newer_issue_event(REPO, issue, _iso(35)), repeat=200)
    per["has_newer_issue_event_sql"] = sql_count["n"] / 200
    sql_count["n"] = 0
    per["structured_handoff_exists"], _ = _timed(
        lambda: p.structured_handoff_exists(REPO, issue, task), repeat=200)
    per["structured_handoff_exists_sql"] = sql_count["n"] / 200
    sql_count["n"] = 0
    per["resolve_handoff_target(derive)"], _ = _timed(
        lambda: p.resolve_handoff_target(REPO, issue, "coding", ""), repeat=200)
    per["resolve_handoff_target(derive)_sql"] = sql_count["n"] / 200
    sql_count["n"] = 0
    per["issue_terminal"], _ = _timed(lambda: p.issue_terminal(REPO, issue), repeat=200)
    per["issue_terminal_sql"] = sql_count["n"] / 200
    ctx = pc.CompensateContext(finding_id="x", task_id=task, repo_key=REPO, issue_id=issue,
                               status="claiming")
    sql_count["n"] = 0
    per["recheck(pipeline)"], _ = _timed(lambda: pc.RecheckHandler().handle(ctx), repeat=200)
    per["recheck(pipeline)_sql"] = sql_count["n"] / 200

    ctx2 = pc.CompensateContext(finding_id="x", task_id=task, repo_key=REPO, issue_id=issue,
                                status="claiming")
    con.execute("UPDATE agent_tasks SET result_text=? WHERE id=?",
                (json.dumps({"next_step": "a2a-qa"}), task))
    con.commit()
    sql_count["n"] = 0
    per["recheck(text)"], _ = _timed(lambda: pc.RecheckHandler().handle(ctx2), repeat=200)
    per["recheck(text)_sql"] = sql_count["n"] / 200
    con.execute("UPDATE agent_tasks SET result_text='' WHERE id=?", (task,))
    con.commit()
    out["unit"] = per

    # --- 整轮巡检：规模阶梯
    ladder = []
    for n in (200, 1000, 3000):
        seed_scale(con, n)
        sql_count["n"] = 0
        dt, findings = _timed(lambda: p.detect_broken_chains("", known=set()))
        ladder.append({"issues": n, "seconds": round(dt, 3),
                       "findings": len(findings), "sql_per_cycle": sql_count["n"],
                       "sql_per_candidate": round(sql_count["n"] / n, 2)})
    out["scan_ladder"] = ladder

    # --- A/B：本批 #2050 判据（结构化 handoff + 无新段 + 场景推导）相对旧口径的增量
    seed_scale(con, 3000)
    sql_count["n"] = 0
    dt_new, f_new = _timed(lambda: p.detect_broken_chains("", known=set()))
    sql_new = sql_count["n"]
    from unittest.mock import patch as _patch
    with _patch.object(p, "structured_handoff_exists", lambda *a: False), \
         _patch.object(p, "has_newer_issue_event", lambda *a: False):
        sql_count["n"] = 0
        dt_old, f_old = _timed(lambda: p.detect_broken_chains("", known=set()))
        sql_old = sql_count["n"]
    out["ab"] = {"issues": 3000, "new_seconds": round(dt_new, 3), "new_sql": sql_new,
                 "old_seconds": round(dt_old, 3), "old_sql": sql_old,
                 "delta_sql_per_candidate": round((sql_new - sql_old) / 3000, 2),
                 "delta_pct": round((dt_new - dt_old) / dt_old * 100, 1) if dt_old else 0}

    # --- MySQL 网络口径投影：每 SQL 一次往返（持 db_lock 全程）
    per_cand_sql = sql_new / 3000
    out["mysql_projection"] = {
        "sql_per_candidate": round(per_cand_sql, 2),
        "at_6x10k_issues": {f"{rtt}ms": round(per_cand_sql * 10000 * rtt / 1000, 1)
                            for rtt in (0.3, 0.6, 1.0)},
    }
    con.set_trace_callback(None)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--perf", action="store_true")
    args = ap.parse_args()

    con, lock = build_db()
    patch_runtime(con, lock)

    print("=== 功能验证（mock 数据 → 真实函数 + 真实 SQL）===")
    results = run_functional(con, lock)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"--- {sum(1 for _, ok, _ in results if ok)}/{len(results)} passed")

    if not args.perf:
        print("\n=== 性能基准（万人级阶梯）===")
        perf = run_perf(con, lock)
        print("单函数成本（µs/次，1 万 Issue 库）：")
        for k, v in perf["unit"].items():
            if k.endswith("_sql"):
                continue
            print(f"  {k:34s} {v * 1e6:9.1f} µs   SQL/次={perf['unit'].get(k + '_sql', 0):.2f}")
        print("整轮巡检扫描成本：")
        for row in perf["scan_ladder"]:
            per_issue = row["seconds"] / row["issues"] * 1000
            print(f"  {row['issues']:5d} issues → {row['seconds']}s  "
                  f"({per_issue:.2f} ms/issue) findings={row['findings']} "
                  f"SQL/轮={row['sql_per_cycle']} (每候选 {row['sql_per_candidate']})")
        # 线性外推到 1 万 / 3 万 Issue
        last = perf["scan_ladder"][-1]
        per_issue = last["seconds"] / last["issues"]
        for target in (10000, 30000):
            est = per_issue * target
            sqls = last["sql_per_candidate"] * target
            print(f"  外推 {target} issues ≈ {est:.1f}s/轮, {sqls:.0f} 条 SQL/轮 "
                  f"（峰值 {sqls / est:.0f} SQL/s，均为同步持锁查询）")
        ab = perf["ab"]
        print(f"A/B（{ab['issues']} issues，本批 #2050 判据 vs 旧口径）："
              f"{ab['old_seconds']}s/{ab['old_sql']}SQL → {ab['new_seconds']}s/{ab['new_sql']}SQL"
              f"（{ab['delta_pct']:+.1f}% 耗时，+{ab['delta_sql_per_candidate']} SQL/候选）")
        mp = perf["mysql_projection"]
        print(f"MySQL 网络口径（每条 SQL 一次往返，巡检全程同步持 db_lock）："
              f"{mp['sql_per_candidate']} SQL/候选 → 1 万候选单轮 ≈ "
              + ", ".join(f"{k}: {v}s" for k, v in mp["at_6x10k_issues"].items()))
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())