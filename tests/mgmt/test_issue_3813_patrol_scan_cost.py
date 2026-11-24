"""Issue #3813：巡检扫描固定成本消除——候选短路 / 键集分批让出 / 脱敏采样 / 索引迁移。

分层（与 test_patrol_unit.py 同款原则）：
- 短路 / 分批 / 脱敏 / 限频 / 让步配置 —— mock 数据访问层。seam = session_manager
  进程库句柄：owner 域 `*_any` 原语的回落路径在函数内 import session_manager，
  patch 模块级别名（如 patrol.db）会打空（见 test_patrol_unit.py 头注释的 seam 声明）。
- 迁移幂等 / 索引列序 / EXPLAIN 命中 —— 真实 SQLite 测试库（conftest init_database
  已建 events / agent_tasks / grape_t_patrol_findings；迁移在启动期已由 conftest
  init_database 执行过一轮，测试先 DROP 再重建证明 fn 真在建，防「已存在假绿」）。

验收对齐（Issue #3813 Spec In Scope 四项 / AC③④⑤⑥⑦）：
- ③ 零候选轮次查询数=探针 5 条常量级（全部 LIMIT 1，无 known-set/逐行/清理）；
- ④ EXPLAIN 命中新索引 + 迁移标注列序依据（PRAGMA index_info 列序断言）；
- ⑤ 采样行脱敏（无字面量）+ 限频（窗口内不重采）；
- ⑥ 键集分批不漏不重 + 批间让出钩子逐批触发。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

import src.kernel.session_manager as _sm
from src.api import patrol as patrol_mod
import patrol_fakes
from patrol_fakes import FakeDb


def _old(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


_RUNTIME_CFG = {"lookback_mode": "runtime", "confirm_minutes": 10,
                "interval_seconds": 300, "compensate_enabled": False}


def _patrol_patches(fake):
    """run_patrol_once 的统一 mock 环境（seam=session_manager；配置面 patch 免 meta 查询）。"""
    return [
        patch.object(_sm, "db", fake),
        patch.object(_sm, "db_lock", threading.Lock()),
        patch.object(patrol_mod, "patrol_runtime_config", lambda: dict(_RUNTIME_CFG)),
    ]


# ====== S53 候选短路（AC③：零候选轮次查询数为常量级探针） ======


def test_short_circuit_zero_candidates_zero_row_queries():
    # 探针全空 → 短路：known-set 全表拉取 / TTL 清理 / 逐行判定 / 落库全部不执行；
    # 轮内全部 SELECT 均为 LIMIT 1 探针（AC③「查询数 0 或常量级」的判据）。
    fake = FakeDb()
    with _patrol_patches(fake)[0], _patrol_patches(fake)[1], _patrol_patches(fake)[2]:
        stats = patrol_mod.run_patrol_once("runtime")
    selects = [sql for sql, _ in fake.executed
               if sql.lstrip().upper().startswith("SELECT")]
    assert selects, "探针必须发出（短路≠零探针，否则无从判定候选为空）"
    assert all("LIMIT 1" in sql for sql in selects), \
        f"短路轮只允许 LIMIT 1 探针查询，实际: {selects}"
    assert not any("grape_t_patrol_findings" in sql for sql, _ in fake.executed), \
        "短路轮不得触碰 findings 表（known-set 拉取与清理都是查询路径）"
    assert not any(sql.lstrip().upper().startswith(("INSERT", "DELETE"))
                   for sql, _ in fake.executed), "短路轮零写入零清理"
    assert stats == {"broken": 0, "zombie": 0, "stalled": 0, "inserted": 0,
                     "purged": 0, "duration_ms": stats["duration_ms"]}, \
        "回执契约字段钉死（S34-4），短路轮全零"
    assert stats["duration_ms"] >= 0


def test_short_circuit_audit_mode_probes_without_window():
    # audit 档（全历史）探针不得下推 completed_at 时间窗（下推=漏候选=静默漏扫）。
    fake = FakeDb()
    with patch.object(_sm, "db", fake), \
         patch.object(_sm, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: dict(_RUNTIME_CFG, lookback_mode="audit")):
        patrol_mod.run_patrol_once("audit")
    assert not any("grape_t_patrol_findings" in sql for sql, _ in fake.executed)
    comp = [sql for sql, _ in fake.executed
            if "status='completed'" in sql and "LIMIT 1" in sql]
    assert comp, "audit 档也必须探 completed 候选"
    assert "completed_at" not in comp[0], "audit 探针不得带时间窗（全历史口径）"


def test_probe_fail_open_runs_full_scan():
    # fail-open 铁律：探针异常=判定失败=按有候选处理（静默短路=静默漏扫，禁止）。
    class _ProbeBoomDb(FakeDb):
        def _take(self, sql, params=()):
            if "LIMIT 1" in sql:
                raise RuntimeError("probe exploded")
            return super()._take(sql, params)

    fake = _ProbeBoomDb()
    with patch.object(_sm, "db", fake), \
         patch.object(_sm, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "patrol_runtime_config", lambda: dict(_RUNTIME_CFG)):
        patrol_mod.run_patrol_once("runtime")
    assert any("SELECT kind, task_id FROM grape_t_patrol_findings" in sql
               for sql, _ in fake.executed), "探针失败必须回落全量路径（拉 known-set）"


def test_probe_hit_runs_full_scan_and_detects_zombie():
    # 探针命中（running 非空）→ 正常路径：known-set 拉取 + 僵尸判定照常产出。
    run_row = ("u-r1", "gitcode:x/r", "#9", "coding", 1, _old(10), _old(70), 1)
    fake = FakeDb([
        ("FROM agent_tasks WHERE status='running'", [run_row]),
        ("FROM agent_tasks WHERE status='queued'", []),
        ("SELECT kind, task_id FROM grape_t_patrol_findings", []),
        ("ORDER BY id", []),     # completed 分页（探针页 + 候选页共用脚本行）
        ("FROM events", []),     # dispatch 两形态（探针 + 候选共用脚本行）
    ])
    with patch.object(_sm, "db", fake), \
         patch.object(_sm, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "patrol_runtime_config", lambda: dict(_RUNTIME_CFG)):
        stats = patrol_mod.run_patrol_once("runtime")
    assert any("SELECT kind, task_id FROM grape_t_patrol_findings" in sql
               for sql, _ in fake.executed), "有候选轮照常拉取 known-set"
    assert stats["zombie"] >= 1, "stream_ended=1 的 running 行必须判出僵尸档"
    assert stats["inserted"] >= 1


# ====== S55 脱敏采样 + 限频（AC⑤） ======


def test_sanitize_sql_literals_strips_values_keeps_shape():
    s = patrol_mod._sanitize_sql_literals(
        "SELECT id FROM events WHERE repo_key='gitcode:a|b' AND seq>12345 "
        "AND status IN ('failed','running') LIMIT ?")
    for secret in ("gitcode:a|b", "12345", "'failed'"):
        assert secret not in s, f"字面量泄漏: {secret}"
    assert "repo_key=?" in s and "seq>?" in s and "LIMIT ?" in s and "IN" in s, \
        "SQL 形状必须保留（只脱字面量，不改结构）"


class _ExplainFakeDb(FakeDb):
    """EXPLAIN 前缀语句按脚本片段返回行（共享 FakeDb 把非 SELECT 当写语句吞掉）。"""

    class _Cursor(patrol_fakes._FakeCursor):
        def execute(self, sql, params=()):
            if sql.upper().startswith("EXPLAIN"):
                self._db.executed.append((sql, params))
                self._rows = self._db._take(
                    sql.replace("EXPLAIN QUERY PLAN ", "", 1), params)
                return self
            return super().execute(sql, params)

    def cursor(self):
        c = self._Cursor(self)
        self.cursors.append(c)
        return c


def _reset_sample_window():
    patrol_mod._EXPLAIN_SAMPLE_STATE["ts"] = 0.0


def _sample_env(fake):
    return [patch.object(_sm, "db", fake),
            patch.object(_sm, "db_lock", threading.Lock())]


def test_explain_samples_rate_limited():
    # 限频：窗口内第二次采样直接返回空（零 EXPLAIN 发出）；窗口重置后恢复。
    _reset_sample_window()
    fake = _ExplainFakeDb([
        ("FROM agent_tasks WHERE status='completed'", []),
        ("FROM agent_tasks WHERE status='running'", []),
        ("FROM agent_tasks WHERE status='queued'", []),
        ("FROM events", []),
    ])
    with _sample_env(fake)[0], _sample_env(fake)[1]:
        first = patrol_mod._slow_scan_explain_samples()
    assert first, "首次 slow scan 采样必须产出"
    explains = lambda: [1 for sql, _ in fake.executed if sql.upper().startswith("EXPLAIN")]
    n1 = explains()
    with _sample_env(fake)[0], _sample_env(fake)[1]:
        assert patrol_mod._slow_scan_explain_samples() == [], "窗口内限频：不再采样"
    assert explains() == n1, "限频轮不得新增 EXPLAIN 查询"
    _reset_sample_window()
    with _sample_env(fake)[0], _sample_env(fake)[1]:
        assert patrol_mod._slow_scan_explain_samples(), "窗口过期后恢复采样"
    assert explains() > n1


def test_explain_sample_lines_sanitized_and_bounded():
    # AC⑤：采样行含查询名 + 脱敏形状/计划、限长；EXPLAIN 计划内嵌常量同样脱敏。
    _reset_sample_window()
    fake = _ExplainFakeDb([
        ("FROM agent_tasks WHERE status='completed'", []),
        ("FROM agent_tasks WHERE status='running'", []),
        ("FROM agent_tasks WHERE status='queued'", []),
        ("FROM events", []),
    ])
    with _sample_env(fake)[0], _sample_env(fake)[1]:
        lines = patrol_mod._slow_scan_explain_samples()
    assert len(lines) == len(patrol_mod._PROBE_SAMPLE_CALLS), "五路候选查询逐一采样"
    for ln in lines:
        assert ln.startswith("query=") and "shape=" in ln and "plan=" in ln
        assert len(ln) <= 120 + patrol_mod._EXPLAIN_SAMPLE_LINE_MAX, \
            f"采样行超限（日志放大防线）: {len(ln)}"
        assert "gitcode" not in ln and "'failed'" not in ln, "采样行不得携带数据字面量"
    assert any("patrol_a2a_failed_dispatch_rows" in ln for ln in lines), \
        "固定成本主源（failed LIKE 大扫描）必须在采样清单内"


# ====== S54 键集分批：不漏不重 + 批间让出（AC⑥） ======


def test_keyset_batches_cover_all_rows_without_dup():
    # 5 行 chunk=2 → 3 页全覆盖无重复；游标=上一页尾 id（单调，不重复扫）。
    rows = [(f"t-{i}", "gitcode:x/r", f"#{100+i}", "coding", _old(60))
            for i in range(5)]
    fake = FakeDb([("FROM agent_tasks WHERE status='completed'", rows)])
    collected = []
    with patch.object(_sm, "db", fake), \
         patch.object(_sm, "db_lock", threading.Lock()):
        for row in patrol_mod._iter_completed_tasks("", chunk=2):
            collected.append(row)
    assert [r[0] for r in collected] == [f"t-{i}" for i in range(5)], "不漏不重"
    pages = [(sql, params) for sql, params in fake.executed
             if "id > ?" in sql and "ORDER BY id" in sql and "LIMIT 1" not in sql]
    assert len(pages) >= 3, "3 个数据页 + 1 次空页终止"
    assert pages[1][1][-2] == "t-1" and pages[2][1][-2] == "t-3", \
        "游标必须等于上一页尾 id（键集单调推进）"


def test_batch_yield_called_once_per_page():
    # 批间让步钩子：每页拉取后恰好调用一次（3 页 3 次），可 patch 可计数（AC⑥）。
    rows = [(f"t-{i}", "gitcode:x/r", f"#{100+i}", "coding", _old(60))
            for i in range(5)]
    fake = FakeDb([("FROM agent_tasks WHERE status='completed'", rows)])
    calls = []
    with patch.object(_sm, "db", fake), \
         patch.object(_sm, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_scan_batch_yield", lambda: calls.append(1)):
        list(patrol_mod._iter_completed_tasks("", chunk=2))
    assert len(calls) == 3, f"3 个非空页必须逐批让出（实际 {len(calls)}）"


def test_scan_yield_seconds_meta_default_and_bad_value():
    # 让步配置：默认 0（行为不变）；meta 可调（ms→s）；坏值回落 0（fail-fast 告警）。
    with patch.object(patrol_mod, "_meta_value", lambda k: ""):
        assert patrol_mod.scan_yield_seconds() == 0.0
    with patch.object(patrol_mod, "_meta_value", lambda k: "250"):
        assert patrol_mod.scan_yield_seconds() == 0.25
    with patch.object(patrol_mod, "_meta_value", lambda k: "abc"):
        assert patrol_mod.scan_yield_seconds() == 0.0


# ====== S52 索引迁移：幂等 + 列序依据 + EXPLAIN 命中（AC④，真实 SQLite 测试库） ======

_IDX_SPEC = (
    ("events", "idx_events_type_status_seq",
     ["event_type", "status", "seq"]),
    ("agent_tasks", "idx_tasks_status_completed_id",
     ["status", "completed_at", "id"]),
    ("grape_t_patrol_findings", "idx_patrol_findings_task_kind",
     ["task_id", "kind"]),
    ("grape_t_patrol_findings", "idx_patrol_findings_status_updated",
     ["status", "updated_at"]),
)


def _drop_patrol_indexes_first():
    """先摘索引再建：证明迁移 fn 真的在建（防「启动期已建」的假绿）。"""
    con = sqlite3.connect(os.environ["DEVOPS_DB_PATH"])
    try:
        for _, idx, _cols in _IDX_SPEC:
            con.execute(f"DROP INDEX IF EXISTS {idx}")
        con.commit()
    finally:
        con.close()


def _index_columns(tbl: str, idx: str) -> list:
    con = sqlite3.connect(os.environ["DEVOPS_DB_PATH"])
    try:
        return [r[2] for r in con.execute(f"PRAGMA index_info({idx})").fetchall()]
    finally:
        con.close()


def test_patrol_indexes_migration_idempotent_ordered_and_documented():
    # 幂等（fn 二跑零异常）+ post_check 通过 + 列序与依据标注一致
    # （最左前缀：等值列在前、范围/排序列在后）+ 迁移源码标注列序依据（AC④）。
    _drop_patrol_indexes_first()
    from src.domains.runtime.migrations import patrol_scan_indexes as rt_mig
    from src.domains.mgmt.migrations import patrol_findings_indexes as mg_mig

    rt_mig.ensure_patrol_scan_indexes()
    rt_mig.ensure_patrol_scan_indexes()      # 幂等：二次执行零异常
    mg_mig.ensure_patrol_findings_indexes()
    mg_mig.ensure_patrol_findings_indexes()
    rt_mig.verify_patrol_scan_indexes()      # post_check 语义（缺索引=raise）
    mg_mig.verify_patrol_findings_indexes()

    for tbl, idx, cols in _IDX_SPEC:
        assert _index_columns(tbl, idx) == cols, f"{idx} 列序漂移（依据见迁移 docstring）"

    for mod in (rt_mig, mg_mig):
        with open(mod.__file__, encoding="utf-8") as f:
            assert "列序依据" in f.read(), \
                f"{mod.__name__} 迁移必须标注列序依据（AC④ 可读验收证据）"


def test_patrol_candidate_queries_hit_new_indexes():
    # EXPLAIN 可证（AC④）：候选查询执行计划命中新复合索引（type != ALL）。
    from src.domains.runtime import public as rt_pub
    cutoff = _old(7 * 1440)
    plan = " ".join(str(col) for row in
                    rt_pub.explain_named_query("tasks_completed_page_cutoff",
                                               (cutoff, "", 500))
                    for col in row)
    assert "idx_tasks_status_completed_id" in plan, \
        f"断链候选键集分页必须走新复合索引，实际计划: {plan}"
    probe_plan = " ".join(str(col) for row in
                          rt_pub.explain_named_query("events_probe_failed_dispatch", ())
                          for col in row)
    assert "idx_events_type_status_seq" in probe_plan, \
        f"failed-dispatch 探针必须走新复合索引（等值前缀收窄），实际计划: {probe_plan}"


# ====== slow scan 告警契约保持（修复后正常轮不应触发；触发时采样行随告警输出） ======


def test_slow_scan_warning_contract_kept_and_sample_appended():
    # 短路与采样不改变 S40 告警契约：超阈值仍输出含 "slow scan" 的告警行
    # （Loki 口径不变），且采样行随后输出（_slow_scan_explain_samples 已 patch
    # 限频窗口重置与返回内容——本用例只锁契约，采样细节由上方专项用例覆盖）。
    class _LogRec:
        def __init__(self):
            self.warnings = []

        def warning(self, msg):
            self.warnings.append(msg)

        def info(self, msg):
            pass

    _reset_sample_window()
    rec = _LogRec()
    fake = FakeDb([("FROM agent_tasks WHERE status='running'",
                    [("u-x", "gitcode:x/r", "#9", "coding", 1, _old(10), _old(70), 0)]),
                   ("FROM agent_tasks WHERE status='queued'", []),
                   ("SELECT kind, task_id FROM grape_t_patrol_findings", []),
                   ("ORDER BY id", []),
                   ("FROM events", [])])
    samples = ["query=demo shape=SELECT ... plan=SEARCH ..."]
    with patch.object(patrol_mod, "log", rec), \
         patch.object(_sm, "db", fake), \
         patch.object(_sm, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "patrol_runtime_config", lambda: dict(_RUNTIME_CFG)), \
         patch.object(patrol_mod, "_SLOW_SCAN_WARN_MS", -1), \
         patch.object(patrol_mod, "_slow_scan_explain_samples", lambda: samples):
        patrol_mod.run_patrol_once("runtime")
    assert any("slow scan" in w for w in rec.warnings), "超阈值必须保留 slow scan 告警"
    assert any("slow scan sample" in w for w in rec.warnings), "采样行随告警输出"
