"""A2A 链路巡检器单元测试（mock 数据访问层）。

分层原则（与 tests/test_patrol.py 集成层互补）：
- 本文件：判定/编排/配置/策略/纯函数/结构断言 —— **mock 数据访问接口**。
  mock 的 seam 是 patrol 模块的事实 Repository 函数（l1_event_exists /
  l2_downstream_exists / issue_terminal / _subtask_parent / _meta_value /
  _engine_unreachable / _scene_missing）与 db.cursor() 的脚本化替身
  （FakeDb：按 SQL 片段返回脚本行，UPDATE/INSERT 记 rowcount），不 mock SQL 细节、
  不碰真实数据库——因此对全树收集下的 env/db 单例 rebind 免疫（隔离缺陷根治层）。
- 集成层（test_patrol.py）：唯一约束/迁移/谓词 UPDATE 原子性/全链路/HTTP API
  ——真实 SQLite。

与需求文档 §七 UC 映射一致，用例名与集成层历史名对齐（同断言，数据源换 mock）。
"""
from __future__ import annotations

import os
import re
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")

from src.api import patrol as patrol_mod
from src.api import patrol_compensate as pc

SENTINEL_REPO = "gitcode:patrol-test/r1"


def _iso(dt=None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _old(minutes: int) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


# ====== FakeDb：数据访问 mock（脚本化 cursor，按 SQL 片段匹配） ======


class _FakeCursor:
    def __init__(self, fake_db):
        self._db = fake_db
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self._db.executed.append((sql, tuple(params) if not isinstance(params, tuple) else params))
        if sql.lstrip().upper().startswith("SELECT"):
            self._rows = self._db._take(sql, params)
        else:                       # INSERT/UPDATE/DELETE：只记 rowcount
            rc = self._db._rowcounts
            self.rowcount = rc.pop(0) if rc else 1
        return self                 # 支持链式 execute().fetchall()

    def executemany(self, sql, seq_of_params=()):
        params = tuple(seq_of_params)
        self._db.executed.append((sql, params))
        rc = self._db._rowcounts
        self.rowcount = rc.pop(0) if rc else len(params)
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDb:
    """脚本化 db 替身：[(sql 片段, rows), ...] 按序消费；未命中 SELECT 返回空。"""

    def __init__(self, results=(), rowcounts=()):
        self._results = [(frag, rows) for frag, rows in results]
        self._rowcounts = list(rowcounts)   # 非 SELECT 语句逐次取值（耗尽回退 1）
        self.cursors = []
        self.executed = []                   # [(sql, params)] 审计轨迹（S34 断言用）
        self.commits = 0                     # S48：commit 计数（批量写对账）

    def cursor(self):
        c = _FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1

    def _take(self, sql, params=()):
        # 首个未消费过标记的匹配片段（同片段可重复命中——同一脚本行服务多轮调用，
        # 如 confirm 窗收紧前后的两轮 detect）
        for i, (frag, rows) in enumerate(self._results):
            if frag in sql:
                # S49 keyset 分页感知：候选分页查询（id > ? ORDER BY id LIMIT ?）
                # 按游标过滤脚本行——否则同一脚本行被每页重放，分页循环不终止
                if "ORDER BY id" in sql and "id > ?" in sql and len(params) >= 2:
                    cursor = params[-2]
                    page = [r for r in rows if str(r[0]) > str(cursor)]
                    return page[:params[-1]]      # LIMIT 同步感知（真库语义）
                return rows
        return []


# ====== 候选行构造（agent_tasks 列序对齐 detect_broken_chains / detect_zombies） ======


def _cand_row(tid, issue="#101", repo=SENTINEL_REPO, agent="coding",
              result_text="", completed_at=None):
    return (tid, repo, issue, agent, result_text, completed_at or _old(60))


def _run_row(tid, started_at=None, created_at=None, stream_ended=0):
    return (tid, SENTINEL_REPO, "#101", "coding", 1,
            started_at or _old(10), created_at or _old(70), stream_ended)


def _queued_row(tid, created_at=None):
    return (tid, SENTINEL_REPO, "#101", "coding", 1, created_at or _old(10))


def _scan_db(candidates):
    """两段式扫描的 FakeDb 脚本：阶段 1 轻量 5 列 + 阶段 2 id IN 回捞
    result_text（_cand_row 仍造 6 元组 [id,repo,issue,agent,result_text,
    completed_at]，阶段 1 取 [0,1,2,3,5] 丢大字段，阶段 2 只为有文本的
    候选编 (id, result_text) 行）。"""
    frag5 = [(r[0], r[1], r[2], r[3], r[5]) for r in candidates]
    texts = [(r[0], r[4]) for r in candidates if r[4]]
    scripts = [("FROM agent_tasks WHERE status='completed'", frag5)]
    if texts:
        scripts.append(("id IN", texts))
    return FakeDb(scripts)


def _detect(candidates, l1=False, l2=False, terminal="", subtask_parent="",
            confirm_minutes=10):
    """组装 detect_broken_chains 的 mock 环境，返回 findings。"""
    fake = _scan_db(candidates)
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", __import__("threading").Lock()), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": confirm_minutes}), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: terminal), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: l1), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: l2), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: subtask_parent):
        return patrol_mod.detect_broken_chains("")


# ====== T7：纯函数 ======


def test_t7_extract_next_step_and_target():
    rt = json.dumps({"next_step": "a2a-qa", "conclusion": "done"})
    assert patrol_mod.extract_next_step(rt) == "a2a-qa"
    assert patrol_mod.parse_target_agent("a2a-qa") == "qa"
    assert patrol_mod.parse_target_agent("a2a:sre") == "sre"
    assert patrol_mod.extract_next_step("not json") == ""      # 解析失败不炸
    assert patrol_mod.parse_target_agent("a2a-") == ""


def test_t7_pr_subscription_chain_excluded():
    assert patrol_mod._is_pr_subscription_chain("#pr_123") is True
    assert patrol_mod._is_pr_subscription_chain("#101") is False


# ====== T1/T2/T3/T11/T1b/T1f/T14：断链三层判定（L1/L2 mock） ======


def test_t1_broken_chain_detected():
    tid = "u-1"
    findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-qa"}))])
    hit = [f for f in findings if f["task_id"] == tid]
    assert hit, "L1 空 + L2 空 + 超窗 → 应命中断链"
    assert hit[0]["suggestion"].startswith("补发交棒")


def test_t2_within_window_silent():
    tid = "u-2"
    findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-qa"}),
                                  completed_at=_old(5))])
    assert not [f for f in findings if f["task_id"] == tid], "未超窗应静默"


def test_t3_non_a2a_prefix_not_reported():
    tid = "u-3"
    findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "review"}))])
    assert not [f for f in findings if f["task_id"] == tid]


def test_t11_normal_chain_no_finding():
    # L1 命中（交棒已发生，全状态）→ 不报
    tid = "u-4"
    findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-qa"}))],
                       l1=True)
    assert not [f for f in findings if f["task_id"] == tid]


def test_t1f_l2_completed_downstream_counts():
    # L2 全状态：下游已完结任务也算交棒证据
    tid = "u-5"
    findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-qa"}))],
                       l2=True)
    assert not [f for f in findings if f["task_id"] == tid]


def test_t1b_l2_stale_correlation_not_evidence():
    # L2 关联性约束由 SQL 判定（集成层覆盖真实时间比较）；单元层钉死
    # "L2 返回 False（无关联证据）→ 断链照报"的编排分支
    tid = "u-6"
    findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-qa"}))],
                       l2=False)
    assert [f for f in findings if f["task_id"] == tid], "无关联下游 → 断链照报"


def test_t14_pr_subscription_excluded_from_candidates():
    tid = "u-7"
    findings = _detect([_cand_row(tid, issue="#pr_77",
                                  result_text=json.dumps({"next_step": "a2a-qa"}))])
    assert not [f for f in findings if f["task_id"] == tid], "PR 订阅链零 finding（D17）"


def test_t1_unparseable_target_suggestion_manual():
    # target 解析不出（a2a- 无 agent 名）→ 建议走人工核对分支
    tid = "u-8"
    findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-"}))])
    hit = [f for f in findings if f["task_id"] == tid]
    assert hit and "人工" in hit[0]["suggestion"]


# ====== T6：停摆链 = 断链的 issue 视图（纯聚合，输入 mock 断链结果） ======


def test_t6_stalled_issue_is_aggregate_view():
    broken = [{"kind": "broken_chain", "task_id": "u-a", "event_seq": 0,
               "repo_key": SENTINEL_REPO, "issue_id": "#104",
               "detail": "{}", "suggestion": "s"}]
    stalled = patrol_mod.derive_stalled_issues(broken)
    hit = [s for s in stalled if s["issue_id"] == "#104" and s["repo_key"] == SENTINEL_REPO]
    assert hit, "断链应聚合出 stalled_issue"
    assert "u-a" in json.loads(hit[0]["detail"])["broken_task_ids"]
    # 同 issue 多断链聚一行；不同 issue 各一行
    broken.append({"kind": "broken_chain", "task_id": "u-b", "event_seq": 0,
                   "repo_key": SENTINEL_REPO, "issue_id": "#104",
                   "detail": "{}", "suggestion": "s"})
    broken.append({"kind": "broken_chain", "task_id": "u-c", "event_seq": 0,
                   "repo_key": "gitcode:patrol-test/r2", "issue_id": "#105",
                   "detail": "{}", "suggestion": "s"})
    stalled = patrol_mod.derive_stalled_issues(broken)
    assert len([s for s in stalled if s["issue_id"] == "#104"]) == 1
    assert len(stalled) == 2


# ====== T5：僵尸两档 + queued 确定性归类（引擎/场景判定 mock） ======


def _detect_zombies(running=(), queued=(), engine_unreachable=False, scene_missing=False):
    fake = FakeDb([
        ("FROM agent_tasks WHERE status='running'", list(running)),
        ("FROM agent_tasks WHERE status='queued'", list(queued)),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", __import__("threading").Lock()), \
         patch.object(patrol_mod, "_engine_unreachable", lambda rid: engine_unreachable), \
         patch.object(patrol_mod, "_scene_missing", lambda rk, iid: scene_missing):
        return patrol_mod.detect_zombies()


def test_t5_zombie_running_signal_tier():
    findings = _detect_zombies(running=[_run_row("u-z1", stream_ended=1)])
    hit = [f for f in findings if f["task_id"] == "u-z1"]
    assert hit and hit[0]["kind"] == "zombie_running_signal", "stream_ended=1 确定性高置信档"


def test_t5_zombie_running_window_tier():
    findings = _detect_zombies(running=[_run_row("u-z2", started_at=_old(130), stream_ended=0)])
    hit = [f for f in findings if f["task_id"] == "u-z2"]
    assert hit and hit[0]["kind"] == "zombie_running_window", "2h 无信号低置信档"


def test_t5_zombie_running_fresh_not_reported():
    findings = _detect_zombies(running=[_run_row("u-z3", started_at=_old(10), stream_ended=0)])
    assert not [f for f in findings if f["task_id"] == "u-z3"], "阈值内不报"


def test_t5_zombie_queued_dead_engine():
    # 引擎不可达（_engine_unreachable 谓词 mock 为 True，分形态判定归集成/专项单测）
    findings = _detect_zombies(queued=[_queued_row("u-q1")], engine_unreachable=True)
    hit = [f for f in findings if f["task_id"] == "u-q1"]
    assert hit and hit[0]["kind"] == "zombie_queued_dead"
    assert json.loads(hit[0]["detail"])["cause"] == "engine_unreachable"


def test_t5_zombie_queued_local_healthy_not_dead():
    # 引擎健康（不可达=False）且未超窗 → 不判死（远端口径误用防回归的编排分支）
    findings = _detect_zombies(queued=[_queued_row("u-q2", created_at=_old(10))],
                               engine_unreachable=False, scene_missing=False)
    dead = [f for f in findings if f["task_id"] == "u-q2" and f["kind"] == "zombie_queued_dead"]
    assert not dead, "引擎健康 → 不归永不调度"


def test_t5_zombie_queued_window_fallback():
    # 引擎在线但超 24h → 窗口档观察
    findings = _detect_zombies(queued=[_queued_row("u-q3", created_at=_old(60 * 25))])
    hit = [f for f in findings if f["task_id"] == "u-q3"]
    assert hit and hit[0]["kind"] == "zombie_queued_window", "引擎在线超窗 → 观察档"


def test_t5_zombie_queued_scene_missing():
    # 场景缺失（引擎在线也判死）→ cause=scene_missing
    findings = _detect_zombies(queued=[_queued_row("u-q4")], scene_missing=True)
    hit = [f for f in findings if f["task_id"] == "u-q4"]
    assert hit and hit[0]["kind"] == "zombie_queued_dead"
    assert json.loads(hit[0]["detail"])["cause"] == "scene_missing"


def test_t5_engine_unreachable_predicate_forms():
    # _engine_unreachable 分形态谓词（db 行 mock）：远端 offline / 本机健康 / 行不存在
    def _engine_row(cred_id, status="online", last_seen=None, installed=1, healthy=1):
        return (cred_id, status, last_seen, installed, healthy)

    def _probe(row):
        fake = FakeDb([("FROM agent_runtimes WHERE id=?", [row] if row else [])])
        with patch.object(patrol_mod, "db", fake), \
             patch.object(patrol_mod, "db_lock", __import__("threading").Lock()):
            return patrol_mod._engine_unreachable(123)

    assert _probe(_engine_row(5, status="offline")) is True        # 远端 offline
    assert _probe(_engine_row(5, status="online", last_seen=_iso())) is False  # 心跳新鲜
    assert _probe(_engine_row(5, status="online", last_seen=_old(60))) is True  # 心跳过期
    assert _probe(_engine_row(0, installed=1, healthy=1)) is False  # 本机健康
    assert _probe(_engine_row(0, installed=0, healthy=1)) is True   # 本机未安装
    assert _probe(None) is True                                    # 引擎行不存在
    assert patrol_mod._engine_unreachable(0) is False               # 未绑定 → 交给时间窗


def test_t5_scene_missing_predicate():
    # _scene_missing 谓词：scene_id 空 → 不判死；fallback=True → 判死
    calls = {}

    def _fake_stage(repo, issue):
        calls["stage"] = (repo, issue)
        return {"scene_id": _scene_id_holder[0]}

    def _fake_cfg(scene_id, repo_key=None):
        calls["cfg"] = scene_id
        return _cfg_holder[0]

    import src.api.session_store as _ss
    import src.api.pipeline_config as _pconf
    _scene_id_holder = [""]
    _cfg_holder = [None]
    with patch.object(_ss, "get_issue_stage", _fake_stage), \
         patch.object(_pconf, "get_pipeline_config", _fake_cfg):
        _scene_id_holder[0] = ""
        assert patrol_mod._scene_missing(SENTINEL_REPO, "#401") is False, "无场景绑定不判死"
        _scene_id_holder[0] = "gone-scene"
        _cfg_holder[0] = None
        assert patrol_mod._scene_missing(SENTINEL_REPO, "#401") is True, "解析不到真实场景"
        _cfg_holder[0] = {"fallback": True}
        assert patrol_mod._scene_missing(SENTINEL_REPO, "#401") is True, "回退 default 即场景不存在"
        _cfg_holder[0] = {"fallback": False, "nodes": ["issue", "qa"]}
        assert patrol_mod._scene_missing(SENTINEL_REPO, "#401") is False
    assert calls["cfg"] == "gone-scene"


# ====== T9/T10/T19：配置面（文件 + meta 数据访问 mock） ======


def test_t9_config_file_bad_values_fall_back():
    import tempfile as _tf
    bad = {"interval_seconds": "banana", "confirm_minutes": -5,
           "lookback_mode": "banana", "compensate_enabled": "yes", "未知键": 1}
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(bad, f)
        p = f.name
    try:
        cfg = patrol_mod._load_patrol_config(p)
        assert cfg == patrol_mod._DEFAULTS, f"坏值应逐键回落默认: {cfg}"
        with open(p, "w") as f:
            json.dump({"interval_seconds": 120, "compensate_enabled": False}, f)
        cfg2 = patrol_mod._load_patrol_config(p)
        assert cfg2["interval_seconds"] == 120
        assert cfg2["compensate_enabled"] is False
        assert cfg2["confirm_minutes"] == patrol_mod._DEFAULTS["confirm_minutes"]
        with open(p, "w") as f:
            f.write("{not-json")
        assert patrol_mod._load_patrol_config(p) == patrol_mod._DEFAULTS
        with open(p, "w") as f:
            f.write("[1,2]")
        assert patrol_mod._load_patrol_config(p) == patrol_mod._DEFAULTS
    finally:
        os.remove(p)


def test_t9b_no_env_reads_config_file_only():
    src = open(os.path.join(ROOT, "src", "api", "patrol.py"), encoding="utf-8").read()
    assert not re.findall(r"os\.environ", src), "patrol 不应读环境变量"
    assert patrol_mod._DEFAULTS["compensate_enabled"] is True


def test_t10_runtime_config_meta_is_source_of_truth():
    # meta 数据访问 mock：缺省 → 代码默认；有效值 → 即生效；坏值 → 回落默认
    _meta = {}

    def _fake_meta(key):
        return _meta.get(key, "")

    with patch.object(patrol_mod, "_meta_value", _fake_meta), \
         patch.object(patrol_mod, "_cfg", dict(patrol_mod._DEFAULTS)):
        rc = patrol_mod.patrol_runtime_config()
        assert rc["interval_seconds"] == 300 and rc["confirm_minutes"] == 10
        assert rc["lookback_mode"] == "runtime" and rc["compensate_enabled"] is True
        _meta.update({"patrol:interval_seconds": "600",
                      "patrol:confirm_minutes": "7",
                      "patrol:lookback_mode": "audit"})
        rc2 = patrol_mod.patrol_runtime_config()
        assert rc2["interval_seconds"] == 600 and rc2["confirm_minutes"] == 7
        assert rc2["lookback_mode"] == "audit"
        _meta.update({"patrol:interval_seconds": "banana",
                      "patrol:lookback_mode": "banana"})
        rc3 = patrol_mod.patrol_runtime_config()
        assert rc3["interval_seconds"] == 300 and rc3["lookback_mode"] == "runtime"


def test_t19_dynamic_switch_and_blocklist():
    _meta = {}

    def _fake_meta(key):
        return _meta.get(key, "")

    with patch.object(patrol_mod, "_meta_value", _fake_meta):
        assert patrol_mod.compensate_enabled() is True
        assert patrol_mod.compensate_repo_blocked(SENTINEL_REPO) is False
        _meta["patrol:compensate_enabled"] = "false"
        assert patrol_mod.compensate_enabled() is False
        _meta["patrol:compensate_enabled"] = "banana"
        assert patrol_mod.compensate_enabled() is False     # 非法值按关
        _meta["patrol:compensate_enabled"] = "true"
        _meta["patrol:compensate_blocklist"] = SENTINEL_REPO
        assert patrol_mod.compensate_repo_blocked(SENTINEL_REPO) is True
        assert patrol_mod.compensate_repo_blocked("gitcode:other/r") is False
        _meta["patrol:compensate_blocklist"] = "*"
        assert patrol_mod.compensate_repo_blocked("gitcode:any/r") is True
        # 逗号分隔多仓
        _meta["patrol:compensate_blocklist"] = "gitcode:a/r1, gitcode:b/r2"
        assert patrol_mod.compensate_repo_blocked("gitcode:b/r2") is True
        assert patrol_mod.compensate_repo_blocked("gitcode:c/r3") is False


def test_s14_3_confirm_window_runtime_change():
    # meta 收紧 confirm 窗 → detect 即时生效（配置数据访问 mock）
    tid = "u-cw"
    with patch.object(patrol_mod, "db",
                      _scan_db([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-qa"}),
                                          completed_at=_old(5))])), \
         patch.object(patrol_mod, "db_lock", __import__("threading").Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""):
        with patch.object(patrol_mod, "patrol_runtime_config",
                          lambda: {"confirm_minutes": 10}):
            assert not [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]
        with patch.object(patrol_mod, "patrol_runtime_config",
                          lambda: {"confirm_minutes": 3}):
            hits = [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]
            assert hits, "confirm=3 应即时生效（5 分钟前完成已超 3 分钟窗）"


# ====== S15_1 / S16_1 / S17_1 / S20_1：判定层守卫（repo seam mock） ======


def test_s15_1_issue_terminal_excluded_from_detection():
    tid = "u-it"
    for terminal in ("completed", "pending_review"):
        findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-qa"}))],
                           terminal=terminal)
        assert not [f for f in findings if f["task_id"] == tid], f"issue 终态 {terminal} 不报"
    findings = _detect([_cand_row(tid, result_text=json.dumps({"next_step": "a2a-qa"}))],
                       terminal="")
    assert [f for f in findings if f["task_id"] == tid], "active 不拦"


def test_s16_1_subtask_l2_key_wired():
    # 子任务链 L2 关联键 = parent_issue+agent_type：编排层断言 detect 把
    # _subtask_parent 的返回值透传给 l2_downstream_exists（防接线丢失→误报/漏报）
    seen = {}

    def _fake_l2(repo_key, issue_id, target_agent, completed_at, parent_issue=""):
        seen["parent"] = parent_issue
        return parent_issue == "#P1"      # 只有 parent_issue 正确传入才构成证据

    with patch.object(patrol_mod, "db",
                      _scan_db([_cand_row("u-st", issue="#S1",
                                          result_text=json.dumps({"next_step": "a2a-qa"}))])), \
         patch.object(patrol_mod, "db_lock", __import__("threading").Lock()), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", _fake_l2), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: "#P1"):
        hits = [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == "u-st"]
        assert not hits, "parent_issue 正确传入且命中 → 交棒证据成立，不报"
    assert seen["parent"] == "#P1", "_subtask_parent 返回值必须透传到 L2（UC1-12 接线）"


def test_s17_1_subtask_parent_reads_stage():
    # _subtask_parent 双源（get_issue_stage）：subtask=1+parent → 返回 parent，否则 ''
    import src.api.session_store as _ss

    def _stage(repo, issue):
        return _stage_holder[0]

    _stage_holder = [{}]
    with patch.object(_ss, "get_issue_stage", _stage):
        _stage_holder[0] = {"is_subtask_mode": 1, "parent_issue_id": "#P707"}
        assert patrol_mod._subtask_parent(SENTINEL_REPO, "#707") == "#P707"
        _stage_holder[0] = {"is_subtask_mode": 1, "parent_issue_id": ""}
        assert patrol_mod._subtask_parent(SENTINEL_REPO, "#707") == ""   # parent 缺失
        _stage_holder[0] = {"is_subtask_mode": 0, "parent_issue_id": "#P707"}
        assert patrol_mod._subtask_parent(SENTINEL_REPO, "#707") == ""   # 非子任务
        _stage_holder[0] = {"parent_issue_id": "#P707"}                  # 缺 is_subtask_mode
        assert patrol_mod._subtask_parent(SENTINEL_REPO, "#707") == ""
    assert patrol_mod._subtask_parent("", "#707") == ""


# ====== Phase 2 处理器（数据访问 mock；a2a_endpoint 打桩与集成层同款） ======


def test_gate_handler_switch_and_blocklist():
    ctx = pc.CompensateContext(finding_id="x", repo_key=SENTINEL_REPO)
    with patch.object(patrol_mod, "compensate_enabled", lambda: False):
        assert pc.GateHandler().handle(ctx) is False and ctx.verdict == "disabled"
    with patch.object(patrol_mod, "compensate_enabled", lambda: True), \
         patch.object(patrol_mod, "compensate_repo_blocked", lambda rk: False):
        assert pc.GateHandler().handle(ctx) is True
    with patch.object(patrol_mod, "compensate_enabled", lambda: True), \
         patch.object(patrol_mod, "compensate_repo_blocked", lambda rk: rk == SENTINEL_REPO):
        assert pc.GateHandler().handle(ctx) is False and ctx.verdict == "repo_blocklisted"
        ctx2 = pc.CompensateContext(finding_id="y", repo_key="gitcode:other/r")
        assert pc.GateHandler().handle(ctx2) is True


def test_recheck_handler_evidence_and_guards():
    # 终检编排：issue 终态 / L1 / L2 命中 → false_positive；target 不可解析 → 回挂 open
    task_row = (_old(60), "coding", json.dumps({"next_step": "a2a-qa"}))

    def _mk(evidence_l1=False, evidence_l2=False, terminal=""):
        ctx = pc.CompensateContext(finding_id="f1", task_id="t1",
                                   repo_key=SENTINEL_REPO, issue_id="#202", status="claiming")
        recorder = []

        def _rec(fid, status, meta):
            recorder.append((status, meta))

        fake = FakeDb([("FROM agent_tasks WHERE id=?", [task_row])])
        with patch.object(pc, "db", fake), \
             patch.object(pc, "db_lock", __import__("threading").Lock()), \
             patch.object(pc, "_set_finding", _rec), \
             patch.object(patrol_mod, "issue_terminal", lambda *a: terminal), \
             patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: evidence_l1), \
             patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: evidence_l2), \
             patch.object(patrol_mod, "_subtask_parent", lambda *a: ""):
            ok = pc.RecheckHandler().handle(ctx)
        return ok, ctx, recorder

    ok, ctx, rec = _mk(terminal="completed")
    assert ok is False and ctx.verdict == "false_positive" and rec[0][0] == "false_positive"
    ok, ctx, rec = _mk(evidence_l1=True)
    assert ok is False and ctx.verdict == "false_positive" and "L1" in rec[0][1]
    ok, ctx, rec = _mk(evidence_l2=True)
    assert ok is False and ctx.verdict == "false_positive" and "L2" in rec[0][1]
    ok, ctx, rec = _mk()
    assert ok is True and ctx.target_agent == "qa" and ctx.result_text  # 放行并解析
    # 原任务行不存在 → 回挂
    ctx = pc.CompensateContext(finding_id="f2", task_id="t2", status="claiming")
    rec = []
    with patch.object(pc, "db", FakeDb([("FROM agent_tasks WHERE id=?", [])])), \
         patch.object(pc, "db_lock", __import__("threading").Lock()), \
         patch.object(pc, "_set_finding",
                      lambda fid, status, meta: rec.append(status)):
        assert pc.RecheckHandler().handle(ctx) is False
    assert ctx.verdict == "task_missing" and rec[0] == "needs_human"  # S29：不自愈类终态
    # target 不可解析 → 回挂 open，人工核对
    ctx = pc.CompensateContext(finding_id="f3", task_id="t3", status="claiming")
    rec = []
    row_bad = (_old(60), "coding", json.dumps({"next_step": "a2a-"}))
    with patch.object(pc, "db", FakeDb([("FROM agent_tasks WHERE id=?", [row_bad])])), \
         patch.object(pc, "db_lock", __import__("threading").Lock()), \
         patch.object(pc, "_set_finding",
                      lambda fid, status, meta: rec.append((status, meta))), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""):
        assert pc.RecheckHandler().handle(ctx) is False
    assert ctx.verdict == "target_unresolvable" and rec[0][0] == "needs_human"  # S29


def test_identity_handler_strategy_chain():
    # 策略族编排（策略本身的数据访问用注入替身——代码原生支持 strategies 注入）
    class _S:
        def __init__(self, uid=None):
            self.uid = uid

        def resolve(self, repo_key):
            return ({"id": self.uid, "auth_type": "jwt", "username": f"u{self.uid}"}
                    if self.uid else None)

    ctx = pc.CompensateContext(finding_id="x", repo_key="r")
    h = pc.IdentityHandler()
    h.strategies = [_S(777), _S(888)]
    assert h.handle(ctx) is True and ctx.caller_user["id"] == 777     # 路径 B 优先
    h.strategies = [_S(None), _S(888)]
    ctx2 = pc.CompensateContext(finding_id="y", repo_key="r")
    assert h.handle(ctx2) is True and ctx2.caller_user["id"] == 888   # 路径 A 回落
    h.strategies = [_S(None), _S(None)]
    ctx3 = pc.CompensateContext(finding_id="z", repo_key="r")
    rec = []
    with patch.object(pc, "_set_finding", lambda fid, status, meta: rec.append(status)):
        assert h.handle(ctx3) is False
    assert ctx3.verdict == "auth_unavailable" and rec[0] == "open"    # 无主体降级挂起


def test_identity_strategy_sql_seam():
    # HostUserStrategy / ManagedUserFallback 数据访问（FakeDb 行 mock）
    fake = FakeDb([("FROM managed_repos WHERE repo_key=?", [(777,)])])
    with patch.object(pc, "db", fake), \
         patch.object(pc, "db_lock", __import__("threading").Lock()):
        user = pc.HostUserStrategy().resolve("gitcode:patrol-test/b")
        assert user and user["id"] == 777 and user["auth_type"] == "jwt"
    fake = FakeDb([("FROM managed_repos WHERE repo_key=?", [(0,)])])
    with patch.object(pc, "db", fake), \
         patch.object(pc, "db_lock", __import__("threading").Lock()):
        assert pc.HostUserStrategy().resolve("gitcode:patrol-test/a") is None
    fake = FakeDb([("FROM user_repos WHERE repo_key=?", [(888,)])])
    with patch.object(pc, "db", fake), \
         patch.object(pc, "db_lock", __import__("threading").Lock()):
        fb = pc.ManagedUserFallbackStrategy().resolve("gitcode:patrol-test/a")
        assert fb and fb["id"] == 888
    fake = FakeDb([("FROM user_repos WHERE repo_key=?", [])])
    with patch.object(pc, "db", fake), \
         patch.object(pc, "db_lock", __import__("threading").Lock()):
        assert pc.ManagedUserFallbackStrategy().resolve("gitcode:patrol-test/a") is None


def test_mode_handler_subtask_recovery():
    import src.api.session_store as _ss
    _stage_holder = [{}]
    _carried = {}

    with patch.object(_ss, "get_issue_stage", lambda rk, iid: _stage_holder[0]), \
         patch.object(_ss, "get_carried_context", lambda rk, num: _carried):
        _stage_holder[0] = {"is_subtask_mode": 1, "parent_issue_id": "#300"}
        ctx = pc.CompensateContext(finding_id="x", repo_key="r", issue_id="#301")
        assert pc.ModeHandler().handle(ctx) is True
        assert ctx.mode_payload["is_subtask_mode"] is True
        assert ctx.mode_payload["parent_issue"] == "#300"
        assert ctx.mode_payload["carried_context"] == _carried
        _stage_holder[0] = {"is_subtask_mode": 1, "parent_issue_id": ""}
        ctx2 = pc.CompensateContext(finding_id="y", repo_key="r", issue_id="#302")
        rec = []
        with patch.object(pc, "_set_finding", lambda fid, status, meta: rec.append(status)):
            assert pc.ModeHandler().handle(ctx2) is False
        assert ctx2.verdict == "mode_unrecoverable" and rec[0] == "needs_human"  # S29
        _stage_holder[0] = {"is_subtask_mode": 0, "parent_issue_id": ""}
        ctx3 = pc.CompensateContext(finding_id="z", repo_key="r", issue_id="#303")
        assert pc.ModeHandler().handle(ctx3) is True
        assert ctx3.mode_payload["is_subtask_mode"] is False
        # issue 号不可解析 → carried_context 空 dict（不炸）
        ctx4 = pc.CompensateContext(finding_id="w", repo_key="r", issue_id="#pr_x")
        assert pc.ModeHandler().handle(ctx4) is True
        assert ctx4.mode_payload["carried_context"] == {}


def test_dispatch_conclusion_and_key_and_marker():
    # 派发适配器：幂等键/context 标记/结论提取/前缀剥离——a2a_endpoint 打桩
    calls = {}

    async def fake_endpoint(request, body, user):
        calls["body"] = body
        calls["user"] = user
        return {"status": "ok", "seq": 9001}

    ctx = pc.CompensateContext(
        finding_id="f", task_id="t-abc", repo_key="github:owner/rep",
        issue_id="#701", agent_type="coding", target_agent="qa",
        result_text=json.dumps({"next_step": "a2a-qa", "conclusion": "修复完成"}),
        caller_user={"id": 9, "auth_type": "jwt", "username": "u9"},
        mode_payload={"is_subtask_mode": True, "parent_issue": "#P", "carried_context": {"k": 1}})
    rec = []
    with patch("src.api.routes.webhook.a2a_endpoint", fake_endpoint), \
         patch.object(pc, "_set_finding", lambda fid, status, meta: rec.append((status, meta))):
        import asyncio
        ok = asyncio.run(pc.DispatchHandler().handle(ctx))
    assert ok is True and ctx.verdict == "compensated"
    body = calls["body"]
    assert body.agent == "qa"
    p = body.payload
    assert p["repository"] == "owner/rep" and p["account"] == "owner"   # 前缀剥离
    assert p["issue_number"] == "701"
    assert "compensated_by=patrol:" in p["context"]
    assert "修复完成" in p["context"] and '"next_step"' not in p["context"]  # 结构化结论
    assert p["idempotency_key"] == "handoff:t-abc:qa"                   # 决策 A 规范键
    assert p["is_subtask_mode"] is True and p["parent_issue"] == "#P"   # 模式字段随载荷
    assert calls["user"]["id"] == 9
    assert rec[0][0] == "compensated"


def test_dispatch_verdict_buckets():
    # 4xx → permanent_failure 终态；5xx → 回挂 open；duplicate_handoff → false_positive；
    # 异常 → dispatch_error 回挂。a2a_endpoint 打桩 + _set_finding 记录器。
    import asyncio

    def _ctx():
        return pc.CompensateContext(
            finding_id="f", task_id="t", repo_key="gitcode:o/r", issue_id="#1",
            target_agent="qa", result_text="{}", caller_user={"id": 1})

    async def _r403(*a, **k):
        class _R:
            status_code = 403
            body = b'{"code":403}'
        return _R()

    async def _r500(*a, **k):
        class _R:
            status_code = 500
            body = b'{"error":"boom"}'
        return _R()

    async def _dup(*a, **k):
        return {"ok": False, "error": "duplicate_handoff"}

    async def _boom(*a, **k):
        raise RuntimeError("net down")

    for fake, want_verdict, want_status in (
            (_r403, "dispatch_permanent_failure", "permanent_failure"),
            (_r500, "dispatch_failed", "open"),
            (_dup, "false_positive", "false_positive"),
            (_boom, "dispatch_error", "open")):
        rec = []
        ctx = _ctx()
        with patch("src.api.routes.webhook.a2a_endpoint", fake), \
             patch.object(pc, "_set_finding", lambda fid, status, meta: rec.append(status)):
            asyncio.run(pc.DispatchHandler().handle(ctx))
        assert ctx.verdict == want_verdict, f"{fake} → {ctx.verdict}"
        assert rec[0] == want_status, f"{fake} 落库 {rec[0]}"


def test_compensate_open_findings_order_and_batch():
    # 批处理编排：只捞 broken_chain open、FIFO（created_at 排序）、限量；
    # 补偿执行 mock 为 verdict 计数（compensate_finding 打桩）。
    import asyncio
    fake = FakeDb([
        ("FROM grape_t_patrol_findings WHERE kind='broken_chain' AND status='open'",
         [("f-new",), ("f-old",), ("f-mid",)]),
    ])

    async def _fake_cf(fid):
        return "compensated"

    with patch.object(pc, "db", fake), \
         patch.object(pc, "db_lock", __import__("threading").Lock()), \
         patch.object(pc, "compensate_finding", _fake_cf):
        counts = asyncio.run(pc.compensate_open_findings(limit=3))
    assert counts == {"compensated": 3}


def test_compensate_batch_order_sql_pinned():
    # SQL 文本守卫：批处理 SELECT 必须含 ORDER BY created_at（UUID id 排序=随机挑）
    import inspect
    src = inspect.getsource(pc.compensate_open_findings)
    assert "ORDER BY created_at" in src


def test_reclaim_stale_claimings_lease_logic():
    # 租约回收编排：超窗 claiming → 回收 open（读-改-写两段 UPDATE）；新鲜不动。
    fake = FakeDb([
        ("status='claiming'", [("f1", "旧回执")]),
    ])
    fake2 = FakeDb([
        ("status='claiming'", []),
    ])
    import threading
    with patch.object(pc, "db", fake), \
         patch.object(pc, "db_lock", threading.Lock()):
        assert pc.reclaim_stale_claimings() == 1
    with patch.object(pc, "db", fake2), \
         patch.object(pc, "db_lock", threading.Lock()):
        assert pc.reclaim_stale_claimings() == 0


# ====== 纯函数/契约/结构断言 ======


def test_s19_3_summarize_jsonresponse_contract():
    from fastapi.responses import JSONResponse
    r409 = JSONResponse(status_code=409, content={"status": "skipped", "skipped": "not_in_scene"})
    ok, summary = pc.DispatchHandler._summarize(r409)
    assert ok is False and "409" in summary and "not_in_scene" in summary
    r200 = JSONResponse(status_code=200, content={"status": "ok", "seq": 5})
    ok2, summary2 = pc.DispatchHandler._summarize(r200)
    assert ok2 is True and "seq" in summary2
    okd, summaryd = pc.DispatchHandler._summarize({"ok": False, "error": "duplicate_handoff"})
    assert okd is False and "duplicate_handoff" in summaryd


def test_s21_1_handoff_key_single_source_and_nonempty_guard():
    assert patrol_mod.handoff_idempotency_key("t-abc", "qa") == "handoff:t-abc:qa"
    assert patrol_mod.HANDOFF_KEY_TEMPLATE == "handoff:{task_id}:{target_agent}"
    for bad in (("", "qa"), ("t-abc", ""), (" ", "qa"), ("t-abc", "  ")):
        with pytest.raises(ValueError):
            patrol_mod.handoff_idempotency_key(*bad)


def test_s22_1_mysql_dialect_compliance():
    from src.mysql_compat import _CONCAT_RE, _SQLITE_MASTER_SQL_RE
    src_comp = open(os.path.join(ROOT, "src", "api", "patrol_compensate.py"),
                    encoding="utf-8").read()
    assert "action_meta=action_meta||" not in src_comp
    assert "||" not in [l for l in src_comp.splitlines()
                        if "UPDATE grape_t_patrol_findings" in l][0]
    assert "SET status='open', action_meta=? WHERE id=?" in src_comp
    mig_sql = "SELECT sql FROM sqlite_master WHERE type='table' AND name='grape_t_patrol_findings'"
    assert _SQLITE_MASTER_SQL_RE.match(mig_sql)
    assert not _CONCAT_RE.search("UPDATE grape_t_patrol_findings SET action_meta=action_meta||' | x'")


def test_t15_patrol_module_has_no_terminal_write():
    src = open(os.path.join(ROOT, "src/api/patrol.py"), encoding="utf-8").read()
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = "\n".join(l.split("#")[0] for l in src.splitlines())
    assert "UPDATE agent_tasks SET status" not in src, "patrol 禁止终态化写（D18）"
    src2 = open(os.path.join(ROOT, "src/api/patrol_compensate.py"), encoding="utf-8").read()
    src2 = re.sub(r'"""[\s\S]*?"""', '""', src2)
    assert "UPDATE agent_tasks SET status" not in src2, "补偿模块同样禁止终态化写（D18）"


def test_s23_1_a6_table_name_and_unconditional_reclaim():
    # A6：主控新增表必须 grape_t_ 前缀（源码零裸名引用）
    for f in ("src/api/patrol.py", "src/api/patrol_compensate.py",
              "src/api/routes/patrol.py", "src/api/db_setup.py"):
        src = open(os.path.join(ROOT, f), encoding="utf-8").read()
        bad = re.findall(r"(FROM|INTO|UPDATE|TABLE)\s+(?:IF NOT EXISTS\s+)?patrol_findings\b", src)
        assert not bad, f"{f} 存在裸名表引用: {bad[:3]}"
    loop_src = open(os.path.join(ROOT, "src/api/patrol.py"), encoding="utf-8").read()
    i = loop_src.find("reclaim_stale_claimings")   # S42 后为 to_thread(reclaim_stale_claimings)
    j = loop_src.find('if rc["compensate_enabled"]')
    assert 0 < i < j, "reclaim 必须在补偿开关判定之前无条件执行"


def test_s20_1_github_key_forms():
    # github 键形态：黑名单精确匹配不误伤 + 派发前缀剥离（数据访问 mock）
    GH = "github:patrol-test/gh-form"
    _meta = {"patrol:compensate_blocklist": GH}

    with patch.object(patrol_mod, "_meta_value", lambda k: _meta.get(k, "")):
        assert patrol_mod.compensate_repo_blocked(GH) is True
        assert patrol_mod.compensate_repo_blocked(SENTINEL_REPO) is False
    assert GH.split(":", 1)[1] == "patrol-test/gh-form"
    # 派发 repository/account 派生（DispatchHandler 同款切分）
    repository = GH.split(":", 1)[1] if ":" in GH else GH
    assert repository == "patrol-test/gh-form"
    assert repository.split("/", 1)[0] == "patrol-test"


# ====== S24【评审十三轮修正】首轮即时巡检 + 清单排序稳定 + 队列僵尸单次探活 ======


def test_s24_1_first_cycle_immediate():
    # UC24-1：主循环先干活后睡——run_patrol_once() 必须出现在周期 sleep 之前，
    # 启动即出第一份清单（等待从轮首移到轮末，CancelledError 出口不变）。
    src = open(os.path.join(ROOT, "src/api/patrol.py"), encoding="utf-8").read()
    body = src[src.find("async def patrol_loop"):src.find("def _hostname")]
    i_scan = body.find("run_patrol_once")   # D23 后为 to_thread(run_patrol_once) 卸载形态
    i_sleep = body.find('await asyncio.sleep(rc["interval_seconds"])')
    assert 0 < i_scan < i_sleep, "周期 sleep 必须在扫描之后（首轮立即执行）"


def test_s24_2_findings_list_stable_order():
    # UC24-2：清单排序钉死——unowned 置顶之外必须按 created_at 倒序；
    # id 是 UUID，按 id 排序=每次刷新随机序（SQL 文本守卫，先例：批处理 FIFO 钉法）。
    src = open(os.path.join(ROOT, "src/api/routes/patrol.py"), encoding="utf-8").read()
    assert "ORDER BY (owner=0) DESC, created_at DESC" in src, \
        "findings 清单必须按 created_at 倒序（id 仅兜底）"


def test_s24_3_queued_zombie_single_engine_probe():
    # UC24-3：队列僵尸判定对同一 runtime 只探活一次（判定与 cause 拼接复用同一次
    # 调用——原实现对同一条件查两次库）。用调用计数 mock 断言恰好一次。
    calls = []

    def _counting_probe(rid):
        calls.append(rid)
        return True

    fake = FakeDb([
        ("FROM agent_tasks WHERE status='running'", []),
        ("FROM agent_tasks WHERE status='queued'", [_queued_row("q-1")]),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", __import__("threading").Lock()), \
         patch.object(patrol_mod, "_engine_unreachable", _counting_probe), \
         patch.object(patrol_mod, "_scene_missing", lambda rk, iid: False):
        findings = patrol_mod.detect_zombies()
    assert calls == [1], f"同一 runtime 只允许一次探活，实际调用 {calls}"
    assert findings and findings[0]["kind"] == "zombie_queued_dead"
    assert json.loads(findings[0]["detail"])["cause"] == "engine_unreachable"


# ====== S25【PR #802 评审缺口】巡检路由断言覆盖（mock 数据访问，闭 N2 BLOCK）======


class _RouteCursor:
    """路由层 db 替身：SELECT 走脚本行；UPDATE 的 rowcount 从队列取（404 判定依据）。"""

    def __init__(self, owner):
        self._owner = owner
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            self._rows = self._owner._take(sql)
        else:
            self.rowcount = self._owner._updates.pop(0) if self._owner._updates else 1
        return self

    def executemany(self, sql, seq_of_params=()):
        params = tuple(seq_of_params)
        self._db.executed.append((sql, params))
        rc = self._db._rowcounts
        self.rowcount = rc.pop(0) if rc else len(params)
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RouteDb:
    def __init__(self, results=(), updates=()):
        self._results = list(results)
        self._updates = list(updates)

    def cursor(self):
        return _RouteCursor(self)

    def commit(self):
        pass

    def _take(self, sql):
        for frag, rows in self._results:
            if frag in sql:
                return rows
        return []


def _finding_row(fid, kind="broken_chain", owner=0, status="open", created="2026-11-02T00:00:00+00:00"):
    # 列序对齐 routes/patrol.py list_findings SELECT
    return (fid, kind, "task-" + fid, 0, SENTINEL_REPO, "#1", owner,
            "{}", "suggest", status, "", created, created)


def test_s25_1_list_findings_contract_and_unowned_pin():
    # UC25-1（S38 后为 admin 视角）：成功面字段契约 + unowned 置顶 + owner 归一
    # （非 int → 0）。admin 全量可见（管理页口径），非 admin 过滤见 test_s38_1。
    from src.api.routes import patrol as routes_mod
    # 行序模拟 ORDER BY (owner=0) DESC 的返回（unowned 在前；SQL 钉死见 UC24-2）
    rows = [_finding_row("f-unowned", owner=0),
            _finding_row("f-str", owner="9"),       # MySQL 兼容层可能返回 str
            _finding_row("f-owned", owner=7)]
    fake = _RouteDb([("FROM admin_accounts", [(7,)]),
                     ("FROM grape_t_patrol_findings", rows)])
    with patch.object(routes_mod, "db", fake), \
         patch.object(routes_mod, "db_lock", __import__("threading").Lock()):
        res = routes_mod.list_findings(status="", kind="", limit=100,
                                       user={"id": 7, "auth_type": "jwt"})
    assert res["status"] == "ok" and res["total"] == 3
    owners = [f["owner"] for f in res["findings"]]
    assert owners[0] == 0, "unowned 必须置顶（ORDER BY (owner=0) DESC 语义）"
    assert owners == [0, 0, 7] and isinstance(res["findings"][1]["owner"], int)
    keys = {"id", "kind", "task_id", "event_seq", "repo_key", "issue_id", "owner",
            "detail", "suggestion", "status", "action_meta", "created_at", "updated_at"}
    assert keys <= set(res["findings"][0])


def test_s25_2_list_findings_filters():
    # UC25-2：status/kind 过滤参数进入 WHERE（脚本行按片段命中）
    from src.api.routes import patrol as routes_mod
    rows = [_finding_row("f2", kind="zombie_queued_dead", status="open")]
    fake = _RouteDb([("status=? AND kind=? AND owner=?", rows)])  # S38：非 admin 过滤进 WHERE
    with patch.object(routes_mod, "db", fake), \
         patch.object(routes_mod, "db_lock", __import__("threading").Lock()):
        res = routes_mod.list_findings(status="open", kind="zombie_queued_dead", limit=100,
                                       user={"id": 1, "auth_type": "jwt"})
    assert res["total"] == 1 and res["findings"][0]["kind"] == "zombie_queued_dead"


def test_s25_3_dismiss_ok_then_404():
    # UC25-3：open → 200 dismissed；已收敛（rowcount=0）→ 404（PR #802 N2 BLOCK 闭口）
    # S30：归属校验进链——SELECT owner → 归属人/ admin 放行，非归属非 admin → 403
    from src.api.routes import patrol as routes_mod
    import pytest
    # 归属人本人（owner=1，user id=1）dismiss → 200；再 dismiss（rowcount=0）→ 404
    fake = _RouteDb([("SELECT owner FROM grape_t_patrol_findings", [(1,)])], updates=[1, 0])
    with patch.object(routes_mod, "db", fake), \
         patch.object(routes_mod, "db_lock", __import__("threading").Lock()):
        res = routes_mod.dismiss_finding("fid-1", None, {"id": 1, "auth_type": "jwt"})
        assert res == {"status": "ok", "id": "fid-1", "finding_status": "dismissed"}
        with pytest.raises(Exception) as ei:
            routes_mod.dismiss_finding("fid-1", None, {"id": 1, "auth_type": "jwt"})
        assert getattr(ei.value, "status_code", None) == 404
    # 非归属人（owner=7）且非 admin（admin_accounts 空）→ 403 零写入
    fake2 = _RouteDb([("SELECT owner FROM grape_t_patrol_findings", [(7,)])], updates=[1])
    with patch.object(routes_mod, "db", fake2), \
         patch.object(routes_mod, "db_lock", __import__("threading").Lock()):
        with pytest.raises(Exception) as ei2:
            routes_mod.dismiss_finding("fid-2", None, {"id": 1, "auth_type": "jwt"})
        assert getattr(ei2.value, "status_code", None) == 403


# ====== S26【评审十四轮修正①】findings 归属路径 A 接线（D16/UC8-2 补口） ======


def test_s26_1_resolve_owner_path_a_fallback():
    # UC8-2：bot token 托管仓（host_user_id 无值）→ 归属=托管用户映射，不再误标 unowned
    import threading
    fake = FakeDb([
        ("FROM managed_repos WHERE repo_key=?", [(0,)]),          # 路径 B 无值
        ("FROM user_repos WHERE repo_key=? AND is_admin=1", [(888,)]),  # 路径 A 映射
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod._resolve_owner(SENTINEL_REPO) == 888


def test_s26_2_resolve_owner_host_user_wins_and_unowned():
    # UC8-1 优先级：路径 B 命中即返回（不等路径 A）；双源皆空 → 0=unowned（UC8-3）
    import threading
    fake = FakeDb([("FROM managed_repos WHERE repo_key=?", [(77,)]),
                   ("FROM user_repos WHERE repo_key=? AND is_admin=1", [(999,)])])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod._resolve_owner(SENTINEL_REPO) == 77
    fake2 = FakeDb([("FROM managed_repos WHERE repo_key=?", [(0,)]),
                    ("FROM user_repos WHERE repo_key=? AND is_admin=1", [])])
    with patch.object(patrol_mod, "db", fake2), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod._resolve_owner(SENTINEL_REPO) == 0


# ====== S27【评审十四轮修正②】选主总闸：非主实例 skip 整轮动作 ======


def test_s27_1_leader_gate_before_reclaim_and_compensate():
    # 结构断言：claim_patrol_leader 判定必须先于 reclaim/compensate 执行——
    # 多机部署下非主实例直接 skip 整轮（设计 §二"谓词选主 → 非主 skip"），
    # 原实现两动作在选主前跑，每台都执行补偿批处理（重复扫描/日志/DB 压力）。
    src = open(os.path.join(ROOT, "src/api/patrol.py"), encoding="utf-8").read()
    body = src[src.find("async def patrol_loop"):src.find("def _hostname")]
    i_claim = body.find("claim_patrol_leader(")
    i_reclaim = body.find("reclaim_stale_claimings")   # S42 后为 to_thread 形态
    i_comp = body.find("compensate_open_findings()")
    i_scan = body.find("run_patrol_once")
    assert 0 < i_claim < i_reclaim, "选主必须先于孤儿回收（非主 skip 整轮）"
    assert 0 < i_claim < i_comp, "选主必须先于补偿批处理（非主 skip 整轮）"
    assert 0 < i_claim < i_scan, "选主必须先于扫描（既有语义钉死）"
    # S23 语义保持：回收仍在补偿开关判定之前（开关关闭也回收，仅被选主门控）
    j_switch = body.find('if rc["compensate_enabled"]')
    assert 0 < i_reclaim < j_switch, "reclaim 必须在补偿开关判定之前无条件执行"


# ====== S28【评审十五轮修正③】持有者标识收紧 hostname#pid（S12 同族拆除） ======


class _LockDb:
    """选主锁专用 db 替身：脚本化 SELECT 行 + rowcount 队列（UPDATE 逐次取值）。"""

    def __init__(self, lock_rows, updates=()):
        self._rows = lock_rows          # [("value=?", [row])] 形态转交 FakeCursor
        self._updates = list(updates)   # 每次 UPDATE 的 rowcount
        self.executed = []

    def cursor(self):
        return _LockCursor(self)

    def commit(self):
        pass


class _LockCursor:
    def __init__(self, owner):
        self._o = owner
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=()):
        self._o.executed.append((sql, params))
        if sql.lstrip().upper().startswith("SELECT"):
            for frag, rows in self._o._rows:
                if frag in sql:
                    self._rows = rows
                    break
            else:
                self._rows = []
        else:
            self.rowcount = self._o._updates.pop(0) if self._o._updates else 0
        return self

    def executemany(self, sql, seq_of_params=()):
        params = tuple(seq_of_params)
        self._db.executed.append((sql, params))
        rc = self._db._rowcounts
        self.rowcount = rc.pop(0) if rc else len(params)
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_s28_1_holder_identity_includes_pid():
    # UC28-1：_hostname() 必须是 hostname#pid 形态——同机多 worker 每进程唯一，
    # 不再共享 hostname 互开自续约（S12 同族定时炸弹拆除）
    assert re.match(r"^.+#\d+$", patrol_mod._hostname()), \
        "持有者标识必须是 hostname#pid"
    assert "#" in patrol_mod._hostname() and "|" not in patrol_mod._hostname()


def test_s28_2_same_host_diff_pid_cannot_renew():
    # UC28-2：同主机不同 pid 的持有者（h#111 持锁新鲜）不能续 h#222 的锁——
    # 自续约 substr 精确前缀不命中（rowcount=0），读到新鲜锁 → False，锁原值不动
    import threading
    fresh = _iso()  # 新鲜锁：未过期
    fake = _LockDb([("SELECT value FROM meta", [("h#111|" + fresh,)])], updates=[0])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod.claim_patrol_leader("h#222", 300) is False
    # 自续约 UPDATE 的 substr 前缀参数必须是 "h#222|"（精确前缀，非 LIKE）
    upd = [(s, p) for (s, p) in fake.executed if s.lstrip().upper().startswith("UPDATE")][0]
    assert upd[1][3] == "h#222|", f"自续约前缀参数应含 pid: {upd[1]}"


def test_s28_3_same_holder_self_renews():
    # UC28-3：本持有者（h#111）自续约成功（substr 前缀命中，rowcount=1）
    import threading
    fake = _LockDb([("SELECT value FROM meta", [("h#111|" + _old(1),)])], updates=[1])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod.claim_patrol_leader("h#111", 300) is True


# ====== S29-S32【评审十六轮修正】批处理饥饿治理 / dismiss 归属 / audit 短路 / L1 棒次 ======


def test_s29_1_not_selfhealing_verdicts_are_terminal():
    # UC29-1：不自愈三类（task_missing/target_unresolvable/mode_unrecoverable）
    # → needs_human 终态，不回挂 open（不再每 5 分钟空转重试 + 饿死队头）
    import threading
    # target 不可解析（result_text 无 agent 名）
    ctx = pc.CompensateContext(finding_id="f3", task_id="t3",
                               repo_key=SENTINEL_REPO, issue_id="#202", status="claiming")
    row_bad = (_old(60), "coding", json.dumps({"next_step": "a2a"}))
    rec = []
    with patch.object(pc, "db", FakeDb([("FROM agent_tasks WHERE id=?", [row_bad])])), \
         patch.object(pc, "db_lock", threading.Lock()), \
         patch.object(pc, "_set_finding", lambda fid, status, meta: rec.append((status, meta))), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""):
        assert pc.RecheckHandler().handle(ctx) is False
    assert ctx.verdict == "target_unresolvable" and rec[0][0] == "needs_human"
    # 批处理捞单谓词钉死：needs_human 不在捞单 SQL 的 status 里（SQL 文本守卫）
    src = open(os.path.join(ROOT, "src/api/patrol_compensate.py"), encoding="utf-8").read()
    sel = src[src.find('SELECT id FROM grape_t_patrol_findings WHERE kind'):]
    sel = sel[:sel.find("ORDER BY")]
    assert "status='open'" in sel and "needs_human" not in sel


def test_s29_2_batch_cooldown_excludes_recent_failures():
    # UC29-2：冷却谓词——action_meta=''（从未尝试）照常捞取；失败回挂
    # （meta 非空 + updated_at 新鲜）冷却窗内不捞，卡死 finding 不再占据队头
    src = open(os.path.join(ROOT, "src/api/patrol_compensate.py"), encoding="utf-8").read()
    assert "AND (action_meta='' OR updated_at < ?)" in src, "冷却谓词必须在捞单 SQL 里"
    assert "_RETRY_COOLDOWN_MINUTES = 30" in src
    # 行为面：FIFO 捞取按谓词过滤（脚本行=冷却窗外的老失败 + 窗内新失败 + 新 finding）
    import threading
    now = datetime.now(timezone.utc)
    rows = [("f-old-failed",), ("f-fresh",)]
    fake = FakeDb([("FROM grape_t_patrol_findings WHERE kind='broken_chain'", rows)])
    got = []
    with patch.object(pc, "db", fake), \
         patch.object(pc, "db_lock", threading.Lock()), \
         patch.object(pc, "_load_finding", lambda fid: pc.CompensateContext(
             finding_id=fid, kind="broken_chain", status="open")):
        async def _fake_comp(fid):
            got.append(fid)
            return "compensated"
        with patch.object(pc, "compensate_finding", _fake_comp):
            import asyncio
            counts = asyncio.run(pc.compensate_open_findings(limit=10))
    assert got == ["f-old-failed", "f-fresh"] and counts == {"compensated": 2}


def test_s30_1_dismiss_owner_or_admin_only():
    # UC30-1：非归属人非 admin → 403（unowned 高危项不能被任意登录用户消音）
    # 见 test_s25_3 第二段（403 路径）；此处补 admin 放行分支
    from src.api.routes import patrol as routes_mod
    import threading
    fake = _RouteDb([("SELECT owner FROM grape_t_patrol_findings", [(7,)]),
                     ("FROM admin_accounts WHERE id=?", [(1,)])], updates=[1])
    with patch.object(routes_mod, "db", fake), \
         patch.object(routes_mod, "db_lock", threading.Lock()):
        res = routes_mod.dismiss_finding("fid-3", None, {"id": 1, "auth_type": "jwt"})
    assert res["status"] == "ok" and res["finding_status"] == "dismissed"  # admin 兜底放行


def test_s31_1_known_findings_short_circuit_rescan():
    # UC31-1：已落库 finding（含 dismissed/false_positive 终态）短路逐任务反查——
    # audit 重扫只做集合查询，不再对已知名跑 L1/L2/引擎探活
    import threading
    known_rows = [("broken_chain", "t-known"), ("zombie_queued_dead", "z-known")]
    fake = FakeDb([
        ("SELECT kind, task_id FROM grape_t_patrol_findings", known_rows),
        ("FROM agent_tasks WHERE status='completed'",
         [("t-known", SENTINEL_REPO, "#101", "coding", _old(60)),
          ("t-new", SENTINEL_REPO, "#102", "coding", _old(60))]),
        ("id IN", [("t-new", json.dumps({"next_step": "a2a-qa"}))]),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}):
        out = patrol_mod.detect_broken_chains("")
    tids = [f["task_id"] for f in out]
    assert "t-new" in tids and "t-known" not in tids, \
        "已落库任务必须被短路（不重查不重产出）"
    # 僵尸侧同款：queued 已知 dead 短路引擎探活
    fake2 = FakeDb([
        ("SELECT kind, task_id FROM grape_t_patrol_findings", known_rows),
        ("FROM agent_tasks WHERE status='running'", []),
        ("FROM agent_tasks WHERE status='queued'",
         [("z-known", SENTINEL_REPO, "#101", "qa", 999, _old(90))]),
    ])
    with patch.object(patrol_mod, "db", fake2), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        zout = patrol_mod.detect_zombies()
    assert all(z["task_id"] != "z-known" for z in zout), "已知僵尸不重复登记"


def test_s32_1_l1_agent_filter():
    # UC32-1：棒次过滤——completed 后其他棒次的 a2a_event（人工补早前链）不构成本棒
    # 证据（产出 finding）；本棒匹配（大小写不敏感）→ 交棒已发生不出 finding
    import threading
    ev_rows = [(_old(59), "reviewer")]   # 完成后 1 分钟、agent=reviewer（≠qa）
    fake = FakeDb([("FROM events WHERE repo_key=? AND issue_id=? AND event_type='a2a_event'", ev_rows)])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod.l1_event_exists(SENTINEL_REPO, "#101", _old(60), "qa") is False
        assert patrol_mod.l1_event_exists(SENTINEL_REPO, "#101", _old(60), "reviewer") is True
    # target 不可解析 → 宽口径（任意 a2a_event 即证据，误报=0 优先）
    with patch.object(patrol_mod, "db", FakeDb(
            [("FROM events WHERE repo_key=? AND issue_id=? AND event_type='a2a_event'", ev_rows)])), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod.l1_event_exists(SENTINEL_REPO, "#101", _old(60), "") is True


# ====== S33【评审十七轮修正】派发后终态回查 + 对齐收敛（复用 _sync 通道） ======


def test_s33_1_race_detected_aligns_and_records():
    # UC33-1：派发成功后 issue 已终态（completed）→ race_detected 回执 +
    # 对齐收敛调用 _sync_issue_pipeline_terminal（打点验证）+ 延迟二次扫排程
    import asyncio
    import src.api.routes.issues as issues_mod

    async def _ok_endpoint(*a, **k):
        return {"status": "ok"}

    ctx = pc.CompensateContext(finding_id="f1", task_id="t1",
                               repo_key=SENTINEL_REPO, issue_id="#202",
                               target_agent="qa", result_text="{}",
                               caller_user={"id": 7})
    calls, rec, sched = [], [], []

    def _fake_delayed(rk, iid):
        sched.append((rk, iid))
        async def _noop():
            return 0
        return _noop()

    with patch("src.api.routes.webhook.a2a_endpoint", _ok_endpoint), \
         patch.object(issues_mod, "_sync_issue_pipeline_terminal",
                      lambda rk, num: calls.append((rk, num)) or 2), \
         patch.object(pc, "_align_convergence_delayed", _fake_delayed), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: "completed"), \
         patch.object(pc, "_set_finding",
                      lambda fid, status, meta: rec.append((status, meta))):
        ok = asyncio.run(pc.DispatchHandler().handle(ctx))
    assert ok is True and ctx.verdict == "compensated"
    # 对齐收敛被调（repo_key + 数字 issue_number，与 admin-confirm 同款入参形态）
    assert calls == [(SENTINEL_REPO, 202)]
    # 回执含 race_detected + aligned 计数（观测数据源）
    assert rec and rec[0][0] == "compensated" and \
        "race_detected:completed→aligned:2" in rec[0][1]
    # 延迟二次扫已排程（打点）
    assert sched == [(SENTINEL_REPO, "#202")]


def test_s33_2_non_terminal_zero_action():
    # UC33-2：派发成功且 issue 非终态 → 无 race 回执、对齐收敛零调用
    import asyncio
    import src.api.routes.issues as issues_mod

    async def _ok_endpoint(*a, **k):
        return {"status": "ok"}

    ctx = pc.CompensateContext(finding_id="f2", task_id="t2",
                               repo_key=SENTINEL_REPO, issue_id="#203",
                               target_agent="qa", result_text="{}",
                               caller_user={"id": 7})
    calls, rec = [], []

    def _fake_delayed(rk, iid):
        calls.append(("delayed", rk, iid))
        async def _noop():
            return 0
        return _noop()

    with patch("src.api.routes.webhook.a2a_endpoint", _ok_endpoint), \
         patch.object(issues_mod, "_sync_issue_pipeline_terminal",
                      lambda rk, num: calls.append((rk, num)) or 0), \
         patch.object(pc, "_align_convergence_delayed", _fake_delayed), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(pc, "_set_finding",
                      lambda fid, status, meta: rec.append((status, meta))):
        ok = asyncio.run(pc.DispatchHandler().handle(ctx))
    assert ok is True
    assert calls == [], "非终态不得触发对齐收敛/延迟扫"
    assert rec and "race_detected" not in rec[0][1]


def test_s33_3_align_delegates_to_sync_and_guards():
    # UC33-3：_align_convergence_once 委托 _sync（语义单 owner，patrol 零
    # agent_tasks SQL——D18 结构断言保住）；issue_id 非数字（如 #pr_123）→ 零调用
    import src.api.routes.issues as issues_mod
    calls = []
    with patch.object(issues_mod, "_sync_issue_pipeline_terminal",
                      lambda rk, num: calls.append((rk, num)) or 1):
        assert pc._align_convergence_once(SENTINEL_REPO, "#202") == 1
        assert pc._align_convergence_once(SENTINEL_REPO, "#pr_123") == 0
        assert pc._align_convergence_once("", "#202") == 0
    assert calls == [(SENTINEL_REPO, 202)]
    # D18 红线：patrol/patrol_compensate 源码零 agent_tasks 终态化 SQL
    # （与 test_t15 同款剥法：去 docstring 与行内注释后再断言——模块头注释里
    # 本就写着这句禁令原文，裸 grep 会自噬）
    for f in ("src/api/patrol.py", "src/api/patrol_compensate.py"):
        src = open(os.path.join(ROOT, f), encoding="utf-8").read()
        src = re.sub(r'"""[\s\S]*?"""', '""', src)
        src = "\n".join(l.split("#")[0] for l in src.splitlines())
        assert "UPDATE agent_tasks SET status" not in src, \
            f"{f} 不得写 agent_tasks（D18/UC7-5）"


def test_s33_4_delayed_resweep_runs_and_idempotent():
    # UC33-4：延迟二次扫（delay=0 直跑）委托同一 _sync 通道——异步晚生任务
    # 的兜底收回；_sync 幂等（谓词只动 queued/dispatched/running），重复扫零副作用
    import asyncio
    import src.api.routes.issues as issues_mod
    calls = []
    with patch.object(issues_mod, "_sync_issue_pipeline_terminal",
                      lambda rk, num: calls.append((rk, num)) or 1):
        n = asyncio.run(pc._align_convergence_delayed(SENTINEL_REPO, "#204", delay=0))
        n2 = asyncio.run(pc._align_convergence_delayed(SENTINEL_REPO, "#204", delay=0))
    assert n == 1 and n2 == 1 and calls == [(SENTINEL_REPO, 204), (SENTINEL_REPO, 204)]
    # 延迟常量存在且为正（防回退到 0/负值=失去晚生兜底）
    assert pc._RACE_ALIGN_DELAY_SECONDS > 0


# ====== S34【实现后审计】findings 终态行保留策略（R11/D21） ======


def test_s34_1_retention_deletes_only_terminal_old():
    # UC34-1：终态（dismissed/false_positive/compensated/permanent_failure）且
    # updated_at 早于 cutoff 的行被删；SQL 参数钉死状态集合——
    # open/claiming/needs_human 永不进删除名单（活性队列与未决人工信号不清理）。
    import threading
    fake = FakeDb([], rowcounts=[3])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_meta_value", lambda k: "90"):
        n = patrol_mod.cleanup_terminal_findings()
    assert n == 3
    assert len(fake.executed) == 1, "清理必须恰好一条 DELETE（随轮一次）"
    sql, params = fake.executed[0]
    assert sql.startswith("DELETE FROM grape_t_patrol_findings")
    for st in ("dismissed", "false_positive", "compensated", "permanent_failure"):
        assert st in params, f"终态 {st} 必须在删除名单"
    for st in ("open", "claiming", "needs_human"):
        assert st not in params, f"非终态 {st} 不得进删除名单（零危害口径）"
    cutoff = datetime.fromisoformat(params[-1])
    age_days = (datetime.now(timezone.utc) - cutoff).days
    assert 89 <= age_days <= 90, "cutoff 必须按 retention_days=90 回溯"


def test_s34_2_retention_zero_disables():
    # UC34-2：retention=0（显式永不清理）→ 零 DELETE（紧急保留审计轨迹的开关语义）
    import threading
    fake = FakeDb([], rowcounts=[0])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_meta_value", lambda k: "0"):
        assert patrol_mod.cleanup_terminal_findings() == 0
    assert fake.executed == [], "retention=0 不得发出任何 DELETE"


def test_s34_3_retention_bad_value_falls_back():
    # UC34-3：坏值（"abc"）回落默认 90 不静默——清理仍执行、cutoff 仍 90 天口径
    import threading
    fake = FakeDb([], rowcounts=[1])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_meta_value", lambda k: "abc"):
        assert patrol_mod.cleanup_terminal_findings() == 1
    assert len(fake.executed) == 1
    cutoff = datetime.fromisoformat(fake.executed[0][1][-1])
    age_days = (datetime.now(timezone.utc) - cutoff).days
    assert 89 <= age_days <= 90, "坏值必须回落默认 90，不得静默跳过清理"


def test_s34_4_run_once_reports_purged():
    # UC34-4：run_patrol_once 回执带 purged 字段且清理随轮执行（审计可对账）
    with patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"lookback_mode": "runtime"}), \
         patch.object(patrol_mod, "detect_broken_chains", lambda c, known=None: []), \
         patch.object(patrol_mod, "detect_zombies", lambda known=None: []), \
         patch.object(patrol_mod, "derive_stalled_issues", lambda b: []), \
         patch.object(patrol_mod, "cleanup_terminal_findings", lambda d=-1: 7):
        stats = patrol_mod.run_patrol_once()
    assert stats["purged"] == 7
    assert set(stats) == {"broken", "zombie", "stalled", "inserted",
                          "purged", "duration_ms"}, "回执契约字段钉死（S34-4）"


# ====== S35【实现后审计】手动触发巡检（R12/D22 运营介入面） ======


def test_s35_1_scan_endpoint_contract_and_offload():
    # UC35-1：POST /api/patrol/scan 触发一轮（to_thread 卸载），回显 scan 统计
    # 契约；手动轮刻意只做检测面（不补偿不抢主——补偿仍走选主门控自动循环）。
    import asyncio
    from src.api.routes import patrol as routes_mod
    called = []

    def _fake_scan(mode=""):
        called.append(mode)
        return {"broken": 1, "zombie": 0, "stalled": 0, "inserted": 1,
                "purged": 2, "duration_ms": 5}

    with patch.object(routes_mod, "run_patrol_once", _fake_scan):
        res = asyncio.run(routes_mod.scan_run(user={"id": 7, "auth_type": "jwt"}))
    assert res["status"] == "ok"
    assert res["scan"]["broken"] == 1 and res["scan"]["purged"] == 2
    assert called == [""], "手动轮必须以空 mode（运行真源档）触发一轮扫描"
    # D23 结构断言：路由内扫描经 to_thread 卸载（同步全表扫描不冻事件循环）
    src = open(os.path.join(ROOT, "src/api/routes/patrol.py"), encoding="utf-8").read()
    assert "asyncio.to_thread(run_patrol_once" in src


def test_s35_2_scan_endpoint_failure_isolated():
    # UC35-2：扫描异常 → 500 收口，绝不外溢进事件循环（S5 巡检自身故障不伤主链同款）
    import asyncio
    from src.api.routes import patrol as routes_mod
    from fastapi import HTTPException

    def _boom(mode=""):
        raise RuntimeError("db exploded")

    with patch.object(routes_mod, "run_patrol_once", _boom):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(routes_mod.scan_run(user={"id": 7, "auth_type": "jwt"}))
    assert ei.value.status_code == 500


# ====== S36【实现后审计】事件循环卸载（R12/D23） ======


def test_s36_1_loop_offloads_scan_to_thread():
    # UC36-1：主循环扫描经 asyncio.to_thread 卸载（run_patrol_once 持 db_lock
    # 同步全表扫描，裸调会卡住整个 FastAPI 事件循环——DB 车道竞争仍在，
    # API 事件循环不再被整轮冻结）。SQL 文本守卫，先例：test_s24_1 钉法。
    src = open(os.path.join(ROOT, "src/api/patrol.py"), encoding="utf-8").read()
    body = src[src.find("async def patrol_loop"):src.find("def _hostname")]
    assert "await asyncio.to_thread(run_patrol_once)" in body, \
        "扫描必须经 to_thread 卸载出事件循环（D23）"
    assert "run_patrol_once()" not in body, "不得保留裸调形态（D23 防回退）"


# ====== S37【实现后审计收尾】规范交棒键契约冻结（R13/D5 决策 A 的跨仓对齐靶面） ======


def test_s37_1_handoff_key_template_frozen():
    # UC37-1：键模板字面量冻结——skill/worker 侧守门件按同式派生的对齐靶子，
    # 平台侧任何静默改形（加前缀/改分隔符/换字段序）都必须在此测试爆红，
    # 而不是等 skill 侧对齐后双发才被发现。
    assert patrol_mod.HANDOFF_KEY_TEMPLATE == "handoff:{task_id}:{target_agent}"
    # 派生函数与模板严格同构（单源派生护栏①——函数即规范，不许各拼一遍）
    assert patrol_mod.handoff_idempotency_key("t-abc", "qa") == \
        patrol_mod.HANDOFF_KEY_TEMPLATE.format(task_id="t-abc", target_agent="qa")
    # 确定性：同输入恒同键（唯一索引幂等语义的前提）
    assert patrol_mod.handoff_idempotency_key("t-abc", "qa") == \
        patrol_mod.handoff_idempotency_key("t-abc", "qa")


def test_s37_2_handoff_key_whitespace_normalized():
    # UC37-2：空白规整——前后空白不产生新键（" t-abc " 与 "t-abc" 同键，
    # 否则迟到原生交棒仅因空白差异撞不上同键，幂等窗口静默失效）
    assert patrol_mod.handoff_idempotency_key(" t-abc ", " qa ") == "handoff:t-abc:qa"
    assert patrol_mod.handoff_idempotency_key(" t-abc ", "qa ") == \
        patrol_mod.handoff_idempotency_key("t-abc", "qa")


def test_s37_3_handoff_key_none_rejected():
    # UC37-3：护栏②拒绝语义——None/空/纯空白任一即拒（空键=唯一索引对 NULL
    # 互不相等=永不幂等，缺参即抛 ValueError 不静默拼键）
    for bad in ((None, "qa"), ("t-abc", None), (None, None)):
        with pytest.raises(ValueError):
            patrol_mod.handoff_idempotency_key(*bad)


# ====== S38【全链路审计】findings 清单归属过滤（R14/D24，与 S30 对称的读口子锁） ======


def test_s38_1_non_admin_sees_own_rows_only():
    # UC38-1：非 admin → WHERE owner=? 进 SQL，只见自己的行；
    # unowned（owner=0，"无人知晓"高危项）对非 admin 不可见（仅 admin 收敛口径同 S30）
    from src.api.routes import patrol as routes_mod
    rows = [_finding_row("f-mine", owner=7)]
    fake = _RouteDb([("owner=?", rows)])   # 仅在 owner 过滤拼进 WHERE 时命中
    with patch.object(routes_mod, "db", fake), \
         patch.object(routes_mod, "db_lock", __import__("threading").Lock()):
        res = routes_mod.list_findings(status="", kind="", limit=100,
                                       user={"id": 7, "auth_type": "jwt"})
    assert res["total"] == 1 and res["findings"][0]["owner"] == 7
    # admin_accounts 查空（无脚本行）→ 非 admin 判定成立
    # owner=0 行不在脚本返回中=非 admin 不可见（FakeDb 片段即语义）


def test_s38_2_admin_gate_query_failure_degrades_to_owner_filter():
    # UC38-2：admin 台账查询异常 → fail-safe 收敛为归属过滤（宁可少看不可多看）
    from src.api.routes import patrol as routes_mod

    class _BoomDb:
        def cursor(self):
            raise RuntimeError("db gone")

        def commit(self):
            pass

    rows = [_finding_row("f-mine", owner=7)]
    fake = _RouteDb([("owner=?", rows)])
    # 首次 cursor() 抛错（admin 查询），后续 _RouteDb 正常——用 side 队列模拟
    class _SwitchDb:
        def __init__(self, first, second):
            self._first, self._second = first, second
            self._n = 0

        def cursor(self):
            self._n += 1
            return self._first.cursor() if self._n == 1 else self._second.cursor()

        def commit(self):
            pass

    switch = _SwitchDb(_BoomDb(), fake)
    with patch.object(routes_mod, "db", switch), \
         patch.object(routes_mod, "db_lock", __import__("threading").Lock()):
        res = routes_mod.list_findings(status="", kind="", limit=100,
                                       user={"id": 7, "auth_type": "jwt"})
    assert res["total"] == 1 and res["findings"][0]["owner"] == 7, \
        "admin 查询失败必须降级为 owner 过滤（fail-safe）"


# ====== S39【全链路审计】手动轮 single-flight 闸（R15/D25，与选主总闸对称） ======


def test_s39_1_concurrent_scan_second_gets_409():
    # UC39-1：在跑期间第二个手动轮 → 409（N 并发=N 全表扫描互踩 db_lock 的封口）；
    # 首轮完成后闸释放（全局标志回 False）
    import asyncio
    import time as _time
    from fastapi import HTTPException
    from src.api.routes import patrol as routes_mod

    def _slow_scan(mode=""):
        _time.sleep(0.25)
        return {"broken": 0, "zombie": 0, "stalled": 0, "inserted": 0,
                "purged": 0, "duration_ms": 250}

    async def _run():
        first = asyncio.create_task(routes_mod.scan_run(user={"id": 7, "auth_type": "jwt"}))
        await asyncio.sleep(0.08)          # 首轮已置位在跑标志
        with pytest.raises(HTTPException) as ei:
            await routes_mod.scan_run(user={"id": 7, "auth_type": "jwt"})
        res = await first
        return res, ei.value

    with patch.object(routes_mod, "run_patrol_once", _slow_scan):
        res, err = asyncio.run(_run())
    assert res["status"] == "ok" and res["scan"]["duration_ms"] == 250
    assert err.status_code == 409
    assert routes_mod._scan_in_flight is False, "闸必须在轮完成后释放"


def test_s39_2_gate_released_on_error():
    # UC39-2：扫描异常 → 500 收口且 finally 释放闸（闸不死锁）→ 下一轮可正常触发
    import asyncio
    from fastapi import HTTPException
    from src.api.routes import patrol as routes_mod

    calls = []

    def _boom(mode=""):
        calls.append(mode)
        raise RuntimeError("scan exploded")

    with patch.object(routes_mod, "run_patrol_once", _boom):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(routes_mod.scan_run(user={"id": 7, "auth_type": "jwt"}))
        assert ei.value.status_code == 500
        assert routes_mod._scan_in_flight is False, "异常路径必须释放闸（finally）"
        # 释放后可再次触发（不再 409，继续 500=异常来自扫描本身而非闸）
        with pytest.raises(HTTPException) as ei2:
            asyncio.run(routes_mod.scan_run(user={"id": 7, "auth_type": "jwt"}))
        assert ei2.value.status_code == 500
    assert calls == ["", ""]


# ====== S40【全链路审计】慢扫描告警（R15/F 项，检测+报告零危害） ======


def test_s40_1_slow_scan_warns_and_does_not_block():
    # UC40-1：duration 超阈值 → warning 告警（不熔断不阻断返回）；阈值命中条件
    # 语义钉死（> 严格比较，恰等于阈值不告警）
    class _LogRec:
        def __init__(self):
            self.warnings, self.infos = [], []

        def warning(self, msg):
            self.warnings.append(msg)

        def info(self, msg):
            self.infos.append(msg)

    rec = _LogRec()
    with patch.object(patrol_mod, "log", rec), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"lookback_mode": "runtime"}), \
         patch.object(patrol_mod, "detect_broken_chains", lambda c, known=None: []), \
         patch.object(patrol_mod, "detect_zombies", lambda known=None: []), \
         patch.object(patrol_mod, "derive_stalled_issues", lambda b: []), \
         patch.object(patrol_mod, "cleanup_terminal_findings", lambda d=-1: 0), \
         patch.object(patrol_mod, "_SLOW_SCAN_WARN_MS", -1):   # 阈值压到必触发
        stats = patrol_mod.run_patrol_once()
    assert any("slow scan" in w for w in rec.warnings), "超阈值必须 warning 告警"
    assert stats["broken"] == 0 and stats["duration_ms"] >= 0, "告警不熔断（回执照常返回）"
    assert patrol_mod._SLOW_SCAN_WARN_MS == 30000, "默认阈值钉死（无调参依据前不漂移）"


def test_s40_2_threshold_boundary_strict():
    # UC40-2：恰等于阈值不告警（> 严格比较）——阈值语义防回退成 >=
    class _LogRec:
        def __init__(self):
            self.warnings = []

        def warning(self, msg):
            self.warnings.append(msg)

        def info(self, msg):
            pass

    rec = _LogRec()
    with patch.object(patrol_mod, "log", rec), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"lookback_mode": "runtime"}), \
         patch.object(patrol_mod, "detect_broken_chains", lambda c, known=None: []), \
         patch.object(patrol_mod, "detect_zombies", lambda known=None: []), \
         patch.object(patrol_mod, "derive_stalled_issues", lambda b: []), \
         patch.object(patrol_mod, "cleanup_terminal_findings", lambda d=-1: 0), \
         patch.object(patrol_mod, "_SLOW_SCAN_WARN_MS", 10 ** 9):  # 阈值抬到必不触发
        patrol_mod.run_patrol_once()
    assert rec.warnings == [], "未超阈值不得告警（噪音防线）"


# ====== S41【全链路审计·闸收口】全入口 single-flight（R16，自动轮×手动轮并发面） ======


def test_s41_1_function_level_gate_blocks_second_entry():
    # UC41-1：闸下沉到 run_patrol_once（线程安全）——一轮在跑时任何入口的第二发
    # 抛 PatrolScanInProgress；轮完成（含异常）后闸释放。覆盖路由层快检
    # 未命中的窗口：自动轮在扫描、手动轮请求进来。
    import threading
    import time as _time
    entered = []

    def _slow_inner(mode):
        entered.append(mode)
        _time.sleep(0.2)
        return {"broken": 0, "zombie": 0, "stalled": 0, "inserted": 0,
                "purged": 0, "duration_ms": 200}

    with patch.object(patrol_mod, "_run_patrol_once_inner", _slow_inner):
        t = threading.Thread(target=patrol_mod.run_patrol_once, args=("",))
        t.start()
        _time.sleep(0.05)             # 首轮已持闸
        with pytest.raises(patrol_mod.PatrolScanInProgress):
            patrol_mod.run_patrol_once()
        t.join()
    assert entered == [""], "第二发必须被闸挡住（不进 inner）"
    assert patrol_mod._scan_gate_busy is False, "轮完成后闸必须释放"


def test_s41_2_gate_released_on_inner_error():
    # UC41-2：inner 抛异常 → finally 释放闸（闸不死锁）→ 下一轮可正常进入
    calls = []

    def _boom(mode):
        calls.append(mode)
        raise RuntimeError("inner exploded")

    with patch.object(patrol_mod, "_run_patrol_once_inner", _boom):
        with pytest.raises(RuntimeError):
            patrol_mod.run_patrol_once()
        assert patrol_mod._scan_gate_busy is False
        # 释放后下一轮正常进入（异常来自 inner 而非闸）
        with patch.object(patrol_mod, "_run_patrol_once_inner",
                          lambda m: {"broken": 0, "zombie": 0, "stalled": 0,
                                     "inserted": 0, "purged": 0, "duration_ms": 1}):
            stats = patrol_mod.run_patrol_once()
    assert calls == [""] and stats["broken"] == 0


def test_s41_3_loop_skip_is_benign_not_backoff():
    # UC41-3：自动轮遇 PatrolScanInProgress 记 skip 日志不进退避（退避会把
    # 5 分钟周期人为拉长）——结构断言：except 分支先于通用 Exception 处理
    src = open(os.path.join(ROOT, "src/api/patrol.py"), encoding="utf-8").read()
    body = src[src.find("async def patrol_loop"):src.find("def _hostname")]
    i_gate = body.find("except PatrolScanInProgress")
    i_skip = body.find("auto scan skipped: manual scan in flight")
    assert 0 < i_gate and 0 < i_skip < i_gate + 200, \
        "闸命中分支必须存在且紧邻 skip 日志（良性跳过语义）"


# ====== S42【全链路审计·D23 收尾】reclaim 卸载出事件循环（R16） ======


def test_s42_1_reclaim_offloaded_to_thread():
    # UC42-1：reclaim_stale_claimings 也是持 db_lock 的表扫描同步段，
    # 主循环内必须与 run_patrol_once 同款经 to_thread 卸载（D23 此前只覆盖
    # 扫描、漏了回收段）；顺序约束（选主→回收→开关）不因卸载改变。
    src = open(os.path.join(ROOT, "src/api/patrol.py"), encoding="utf-8").read()
    body = src[src.find("async def patrol_loop"):src.find("def _hostname")]
    assert "await asyncio.to_thread(reclaim_stale_claimings)" in body, \
        "reclaim 必须经 to_thread 卸载（S42）"
    assert "reclaim_stale_claimings()" not in body, "不得保留裸调形态（防回退）"
    i_claim = body.find("claim_patrol_leader(")
    i_reclaim = body.find("reclaim_stale_claimings")
    j_switch = body.find('if rc["compensate_enabled"]')
    assert 0 < i_claim < i_reclaim < j_switch, \
        "顺序约束不变：选主 → 回收 → 补偿开关（S23/S27 语义保持）"


# ====== S43【覆盖率对账】insert_finding 落库契约（R6/R8 硬性要求的行为断言） ======

_UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_s43_1_insert_finding_uuid_owner_and_defaults():
    # UC43-1：R6 硬性要求——id 一律 UUID（Python 侧生成，禁自增），task_id 为
    # agent_tasks UUID 或空串；R8——owner 归属随行落库；默认 status=open、
    # created/updated 同刻；rowcount=1 → True（新插入）
    import threading
    fake = FakeDb([], rowcounts=[1])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_resolve_owner", lambda rk: 7):
        ok = patrol_mod.insert_finding(kind="broken_chain", task_id="t-uuid-1",
                                       repo_key=SENTINEL_REPO, issue_id="#101",
                                       detail="d", suggestion="s")
    assert ok is True
    sql, params = fake.executed[0]
    assert sql.startswith("INSERT OR IGNORE INTO grape_t_patrol_findings")
    assert _UUID_RE.match(str(params[0])), "主键必须为 UUID 形态（R6 禁自增）"
    assert params[1] == "broken_chain" and params[2] == "t-uuid-1"
    assert params[6] == 7, "owner 归属必须随行落库（R8/D16）"
    assert params[9] == "open", "新 finding 默认 open"
    assert params[11] and params[12] and params[11] == params[12], \
        "created_at/updated_at 同刻落库"


def test_s43_2_insert_duplicate_rowcount_zero():
    # UC43-2：唯一约束去重命中（INSERT OR IGNORE rowcount=0）→ False
    # （双主/手动轮×自动轮幂等汇合的行为面——调用方按 False 不计数）
    import threading
    fake = FakeDb([], rowcounts=[0])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_resolve_owner", lambda rk: 0):
        assert patrol_mod.insert_finding(kind="stalled_issue", task_id="",
                                         issue_id="#202") is False


def test_s43_3_insert_error_swallowed_not_raising():
    # UC43-3：落库异常吞掉返回 False（S5 巡检自身故障不伤主链——单条失败
    # 不炸整轮循环）
    import threading

    class _BoomDb:
        def cursor(self):
            raise RuntimeError("db gone")

        def commit(self):
            pass

    with patch.object(patrol_mod, "db", _BoomDb()), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        assert patrol_mod.insert_finding(kind="broken_chain") is False


# ====== S44【覆盖率对账】补偿责任链顺序冻结（D20 编排红线） ======


def test_s44_1_chain_order_frozen():
    # UC44-1：同步责任链五处理器顺序钉死——门卫→抢占→终检→身份→模式。
    # 重排即破坏设计红线（例：终检挪到抢占前=先查后抢=竞态窗口回潮；
    # 门卫不在首位=开关/黑名单失效仍耗 DB）。
    chain = pc.build_chain()
    order = [type(h).__name__ for h in chain]
    assert order == ["GateHandler", "ClaimHandler", "RecheckHandler",
                     "IdentityHandler", "ModeHandler"], \
        f"责任链顺序被改动: {order}"


def test_s44_2_dispatch_handler_not_in_sync_chain():
    # UC44-2：DispatchHandler 是 async 接口（直调 a2a_endpoint），不得混入
    # 同步 handle(ctx) 链——同步链里调 async handle 只拿到协程对象不执行，
    # 派发静默失效（build_chain 必须只含五个同步处理器）。
    chain = pc.build_chain()
    assert all(type(h).__name__ != "DispatchHandler" for h in chain), \
        "DispatchHandler 不得进同步责任链（async 签名不兼容）"
    # 六段完整编排（含派发）在 compensate_finding 内：链跑完后单独 await
    src = open(os.path.join(ROOT, "src/api/patrol_compensate.py"),
               encoding="utf-8").read()
    body = src[src.find("async def compensate_finding"):]  # build_chain 定义在其前，取函数尾
    assert "build_chain()" in body and "await" in body and "DispatchHandler" in src, \
        "派发段必须在同步链之后单独 await（六段编排不变）"


# ====== S45【性能·P1】两段式拉取：result_text 只为幸存候选回捞 ======


def test_s45_1_result_text_fetched_only_for_survivors():
    # UC45-1：阶段 1 只拉轻量列；result_text 仅对穿过已知/PR/终态/超窗短路后
    # 的幸存候选按 id IN 回捞——已知任务的大字段不再拉取（20k×2KB=40MB/轮
    # 的 MySQL 网络读降 ~97%）
    import threading
    known_rows = [("broken_chain", "t-known")]
    fake = FakeDb([
        ("SELECT kind, task_id FROM grape_t_patrol_findings", known_rows),
        ("FROM agent_tasks WHERE status='completed'",
         [("t-known", SENTINEL_REPO, "#101", "coding", _old(60)),
          ("t-new", SENTINEL_REPO, "#102", "coding", _old(60))]),
        ("id IN", [("t-new", json.dumps({"next_step": "a2a-qa"}))]),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}):
        out = patrol_mod.detect_broken_chains("")
    # IN 查询参数只含幸存者（已知任务 t-known 不进回捞）
    in_queries = [p for (sql, p) in fake.executed
                  if "id IN" in sql and "result_text" in sql]
    assert in_queries == [("t-new",)], "回捞必须只含幸存候选 id"
    # 阶段 1 拉取不含 result_text 列（大字段不随候选拉）
    phase1 = [sql for (sql, _) in fake.executed
              if "status='completed'" in sql]
    assert phase1 and all("result_text" not in sql for sql in phase1)
    assert [f["task_id"] for f in out] == ["t-new"]


def test_s45_2_batch_chunking_500():
    # UC45-2：幸存候选超 500 分块回捞（1001 → 3 条 IN 查询：500/500/1），
    # 不生成超宽 IN 列表
    import threading
    n = 1001
    rows = [(f"t-{i:05d}", SENTINEL_REPO, "#102", "coding", _old(60))
            for i in range(n)]
    texts = [(f"t-{i:05d}", json.dumps({"next_step": "a2a-qa"}))
             for i in range(n)]
    fake = FakeDb([
        ("SELECT kind, task_id FROM grape_t_patrol_findings", []),
        ("FROM agent_tasks WHERE status='completed'", rows),
        ("id IN", texts),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: True), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}):
        patrol_mod.detect_broken_chains("")
    in_queries = [p for (sql, p) in fake.executed
                  if "id IN" in sql and "result_text" in sql]
    sizes = [len(p) for p in in_queries]
    assert sizes == [500, 500, 1], f"分块必须 500/批，实际 {sizes}"


def test_s45_3_fetch_error_swallowed():
    # UC45-3：回捞单块异常吞掉（缺文本候选判 "" 走不报口径，不炸整轮——S5）
    import threading

    class _BoomOnIN(FakeDb):
        def __init__(self):
            super().__init__()
            self._in_seen = False

        def cursor(self):
            c = super().cursor()
            return c

    fake = _BoomOnIN()
    rows = [("t-new", SENTINEL_REPO, "#102", "coding", _old(60))]
    fake._results = [
        ("SELECT kind, task_id FROM grape_t_patrol_findings", []),
        ("FROM agent_tasks WHERE status='completed'", rows),
    ]

    class _Cursor(_FakeCursor):
        def execute(self, sql, params=()):
            if "id IN" in sql and "result_text" in sql:
                raise RuntimeError("fetch exploded")
            return super().execute(sql, params)

    orig_cursor = fake.cursor

    def cursor():
        c = _Cursor(fake)
        fake.cursors.append(c)
        return c

    fake.cursor = cursor
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}):
        out = patrol_mod.detect_broken_chains("")   # 不得抛出
    assert out == [], "缺文本=next_step 空=不报（不猜）"


# ====== S46【覆盖对账·P1 补口】runtime 窗口分支的两段式行为 ======


def test_s46_1_runtime_window_pushed_into_phase1_sql():
    # UC46-1：runtime 常态分支（cutoff 非空）——窗口谓词必须下推进阶段 1 SQL
    # 并以参数承载（此前全部用例走 audit 空分支，生产默认路径零覆盖）；
    # 阶段 1 仍不含 result_text 大字段（P1 语义在窗口分支同样成立）
    import threading
    fake = _scan_db([_cand_row("t-rt", issue="#102",
                               result_text=json.dumps({"next_step": "a2a-qa"}))])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}):
        out = patrol_mod.detect_broken_chains("2026-10-26T00:00:00+00:00")
    phase1 = [(sql, p) for (sql, p) in fake.executed
              if "status='completed'" in sql]
    assert 1 <= len(phase1) <= 2, "数据页 + 至多一条取尽探针页"
    sql, params = phase1[0]
    assert "completed_at > ?" in sql, "runtime 分支必须带窗口谓词（防谓词丢失=全历史扫描）"
    assert params[0] == "2026-10-26T00:00:00+00:00", "窗口参数必须以绑定参数承载"
    assert params[-1] == 500 and "ORDER BY id" in sql, "S49 keyset 分页形态"
    assert "result_text" not in sql, "阶段 1 不得拉大字段（P1 两分支同语义）"
    if len(phase1) == 2:
        assert phase1[1][1][-2] == "t-rt", "第二页游标=首页末行 id（keyset 语义）"
    in_q = [p for (sql, p) in fake.executed if "id IN" in sql and "result_text" in sql]
    assert in_q == [("t-rt",)], "幸存者仍走 IN 回捞"
    assert [f["task_id"] for f in out] == ["t-rt"]


def test_s46_2_audit_branch_has_no_window_filter():
    # UC46-2：audit 分支（cutoff 空）——阶段 1 无窗口谓词、无参数（全历史口径），
    # 与 runtime 分支形成文本守卫对（两分支形态互斥钉死，防互相漂移）
    import threading
    fake = _scan_db([_cand_row("t-au", issue="#103",
                               result_text=json.dumps({"next_step": "a2a-qa"}))])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}):
        patrol_mod.detect_broken_chains("")
    phase1 = [(sql, p) for (sql, p) in fake.executed
              if "status='completed'" in sql]
    sql, params = phase1[0]
    assert "completed_at >" not in sql, "audit 分支不得带窗口谓词（全历史口径）"
    assert len(params) == 2 and params[0] == "" and params[1] == 500, \
        "audit 分支仅分页游标+页大小参数"
    assert "result_text" not in sql


# ====== S47【性能·分析人清单】探活去重缓存 + 谓词顺序修正（R19） ======


def test_s47_1_queued_probe_dedup_by_runtime_and_issue():
    # UC47-1：轮内探活去重——同 runtime 多任务共享一次引擎探活、
    # 同 (repo,issue) 多任务共享一次场景解析（跨任务 N+1 → O(去重键数)）；
    # 缓存生命周期=单轮（两次调用各自重探，心跳/场景变更下轮自然重判）
    import threading
    rt_calls, scene_calls = [], []

    def _rt_probe(rid):
        rt_calls.append(rid)
        return False

    def _scene_probe(rk, iid):
        scene_calls.append((rk, iid))
        return False

    queued = [_queued_row("q-a", created_at=_old(60)),
              _queued_row("q-b", created_at=_old(60)),
              _queued_row("q-c", created_at=_old(60))]   # 同 runtime=1、同 issue
    fake = FakeDb([
        ("FROM agent_tasks WHERE status='running'", []),
        ("FROM agent_tasks WHERE status='queued'", queued),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_engine_unreachable", _rt_probe), \
         patch.object(patrol_mod, "_scene_missing", _scene_probe):
        patrol_mod.detect_zombies()
    assert rt_calls == [1], f"同 runtime 三任务只允许一次探活，实际 {rt_calls}"
    assert scene_calls == [(SENTINEL_REPO, "#101")], \
        f"同 (repo,issue) 只允许一次场景解析，实际 {scene_calls}"
    # 缓存不跨轮：第二轮重新探活（每轮各 1 次）
    with patch.object(patrol_mod, "db", FakeDb([
            ("FROM agent_tasks WHERE status='running'", []),
            ("FROM agent_tasks WHERE status='queued'", [_queued_row("q-d")])])), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_engine_unreachable", _rt_probe), \
         patch.object(patrol_mod, "_scene_missing", _scene_probe):
        patrol_mod.detect_zombies()
    assert len(rt_calls) == 2, "缓存生命周期=单轮（跨轮重探）"


def test_s47_2_terminal_query_skipped_for_within_window():
    # UC47-2：谓词顺序修正——未超窗候选（confirm 窗内）不再先做 issue_terminal
    # 的 issue 表查询（零成本纯谓词前置；纯谓词交换不变性）
    term_calls = []

    def _term(rk, iid):
        term_calls.append((rk, iid))
        return ""
    # 候选1：5 分钟前完成（窗内）→ 不应触发 issue_terminal
    # 候选2：60 分钟前完成（超窗）→ 正常触发
    import threading
    fake = _scan_db([
        _cand_row("u-in-window", issue="#201", completed_at=_old(5),
                  result_text=json.dumps({"next_step": "a2a-qa"})),
        _cand_row("u-beyond", issue="#202", completed_at=_old(60),
                  result_text=json.dumps({"next_step": "a2a-qa"})),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", _term), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}):
        out = patrol_mod.detect_broken_chains("")
    assert ("gitcode:patrol-test/r1", "#201") not in term_calls, \
        "未超窗候选不得触发 issue 表查询（查询白做）"
    assert ("gitcode:patrol-test/r1", "#202") in term_calls, \
        "超窗候选照常做 issue 终态判定"
    assert [f["task_id"] for f in out] == ["u-beyond"], \
        "结果集与产出顺序不变（纯谓词重排语义等价）"


# ====== S48【性能·分析人清单 A】批量落库：攒批 executemany + 单块单 commit ======


def _finding_desc(tid, repo=SENTINEL_REPO, issue="#101"):
    return {"kind": "broken_chain", "task_id": tid, "event_seq": 0,
            "repo_key": repo, "issue_id": issue, "detail": "{}",
            "suggestion": ""}


def test_s48_1_batch_insert_single_commit_per_chunk():
    # UC48-1：N 条 finding（≤500）只产生 1 次 commit + 1 次 executemany——
    # 逐条 INSERT+commit（SQLite=逐条 fsync、MySQL=逐条事务往返）收敛为块事务
    import threading
    fake = FakeDb([])
    descs = [_finding_desc(f"t-{i:03d}") for i in range(100)]
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_resolve_owner", lambda rk: 7):
        n = patrol_mod.insert_findings_batch(descs)
    assert n == 100
    assert fake.commits == 1, f"100 条 finding 必须恰好 1 次 commit，实际 {fake.commits}"
    many = [p for (sql, p) in fake.executed
            if sql.startswith("INSERT OR IGNORE") and isinstance(p, tuple)
            and len(p) > 13]
    assert len(many) == 1 and len(many[0]) == 100, "必须 executemany 一批写入"
    # 主键全 UUID、owner 随行、默认 open（R6/R8 契约在批量路径同样成立）
    assert all(_UUID_RE.match(str(r[0])) for r in many[0])
    assert all(r[6] == 7 and r[9] == "open" for r in many[0])


def test_s48_2_owner_resolved_once_per_distinct_repo():
    # UC48-2：owner 归属按 repo_key 去重解析（原逐条 _resolve_owner 的 N+1）——
    # 锁外解析（其内部持锁，批量锁内调用会死锁）
    import threading
    calls = []
    fake = FakeDb([])
    descs = [_finding_desc(f"t-{i:03d}", repo=SENTINEL_REPO if i % 2 else
                           "gitcode:patrol-test/r2") for i in range(10)]
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_resolve_owner",
                      lambda rk: calls.append(rk) or 7):
        patrol_mod.insert_findings_batch(descs)
    assert sorted(calls) == [SENTINEL_REPO, "gitcode:patrol-test/r2"], \
        f"两个仓 10 条 finding 只允许 2 次 owner 解析，实际 {calls}"


def test_s48_3_chunk_failure_isolated():
    # UC48-3：单块失败吞掉（该块丢失、其余块照常——S5 口径，
    # 同 _fetch_result_texts 单块容错模式）
    import threading

    fake = FakeDb()   # executemany rowcount=len(params)（第二批 10）
    descs = ([_finding_desc(f"t-a{i:03d}") for i in range(500)] +
             [_finding_desc(f"t-b{i:03d}") for i in range(10)])
    orig_executemany = _FakeCursor.executemany
    state = {"n": 0}

    def _exec(self, sql, seq=()):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("chunk 0 exploded")
        return orig_executemany(self, sql, seq)

    _FakeCursor.executemany = _exec
    try:
        with patch.object(patrol_mod, "db", fake), \
             patch.object(patrol_mod, "db_lock", threading.Lock()), \
             patch.object(patrol_mod, "_resolve_owner", lambda rk: 0):
            n = patrol_mod.insert_findings_batch(descs)   # 不得抛出
    finally:
        _FakeCursor.executemany = orig_executemany
    assert n == 10, f"首块失败后第二块照常落库，实际 {n}"


# ====== S49【性能·分析人清单 C】keyset 分页流式拉取 ======


def test_s49_1_keyset_pagination_terminates_and_orders():
    # UC49-1：分页游标推进 + 取尽终止——3 行 chunk=2 → 页1[2行] → 页2[1行] →
    # 空探针页终止；全量产出与行序一致（audit 全历史峰值内存 O(500 行)）
    import threading
    rows = [(f"t-{i:03d}", SENTINEL_REPO, "#101", "coding", _old(60))
            for i in range(3)]
    fake = FakeDb([("FROM agent_tasks WHERE status='completed'", rows)])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        got = list(patrol_mod._iter_completed_tasks("", chunk=2))
    assert [r[0] for r in got] == ["t-000", "t-001", "t-002"]
    pages = [p for (sql, p) in fake.executed
             if "status='completed'" in sql and "ORDER BY id" in sql]
    assert [p[-2] for p in pages] == ["", "t-001", "t-002"], \
        f"游标必须逐页推进至取尽（含终止探针页），实际 {[p[-2] for p in pages]}"


def test_s49_2_detect_streaming_semantics_unchanged():
    # UC49-2：分页流式下 detect 判定语义零变化——2 页 4 候选，已知 2 条跨页
    # 分布，幸存者判定不受页边界影响（短路/回捞/产出与全量路径一致）
    import threading
    rows = [(f"t-{i:03d}", SENTINEL_REPO, "#101", "coding", _old(60))
            for i in range(4)]
    texts = [(f"t-{i:03d}", json.dumps({"next_step": "a2a-qa"}))
             for i in range(4)]
    known_rows = [("broken_chain", "t-000"), ("broken_chain", "t-002")]
    fake = FakeDb([
        ("SELECT kind, task_id FROM grape_t_patrol_findings", known_rows),
        ("FROM agent_tasks WHERE status='completed'", rows),
        ("id IN", texts),
    ])
    real_iter = patrol_mod._iter_completed_tasks
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}), \
         patch.object(patrol_mod, "_iter_completed_tasks",
                      lambda cutoff, chunk=500: real_iter(cutoff, 2)):
        out = patrol_mod.detect_broken_chains("")
    assert [f["task_id"] for f in out] == ["t-001", "t-003"], \
        "已知跨页分布，幸存判定不受页边界影响"




# ====== S50【覆盖对账·第22轮】seed 契约 + 批量落库接线（R21） ======


def test_s50_1_run_once_wires_detect_to_batch_insert():
    # UC50-1：run_patrol_once 检测产物必须经 insert_findings_batch 落库、
    # 回执 inserted=批量返回值——S48 只测了批量函数本身，接线无钉死时
    # 退回逐条 insert_finding（每条一 commit 的旧形态）所有测试照样绿
    d1 = {"kind": "broken_chain", "task_id": "t-1", "event_seq": 0,
          "repo_key": SENTINEL_REPO, "issue_id": "#1", "detail": "",
          "suggestion": ""}
    z1 = {"kind": "zombie_queued_dead", "task_id": "t-2", "event_seq": 0,
          "repo_key": SENTINEL_REPO, "issue_id": "#1", "detail": "",
          "suggestion": ""}
    seen = []

    def _batch(findings, chunk=500):
        seen.append([f["task_id"] for f in findings])
        return 5
    with patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"lookback_mode": "runtime"}), \
         patch.object(patrol_mod, "detect_broken_chains", lambda c, known=None: [d1]), \
         patch.object(patrol_mod, "detect_zombies", lambda known=None: [z1]), \
         patch.object(patrol_mod, "derive_stalled_issues", lambda b: []), \
         patch.object(patrol_mod, "insert_findings_batch", _batch), \
         patch.object(patrol_mod, "cleanup_terminal_findings", lambda d=-1: 0):
        stats = patrol_mod.run_patrol_once()
    assert seen == [["t-1", "t-2"]], "检测产物必须整批进 insert_findings_batch"
    assert stats["inserted"] == 5, "回执 inserted=批量返回值（接线钉死）"


def test_s50_2_seed_skips_existing_meta_keys():
    # UC50-2：文件 seed 只写 meta 空键——已有键不覆盖（S13/login-whitelist
    # 先例语义首次有断言）；bool 序列化为小写字符串
    set_calls = []
    import src.user.db as udb

    def _get(mkey):
        return {"patrol:interval_seconds": "999"}.get(mkey, "")
        # interval 已有值 → 不覆盖；其余三键空 → seed

    def _set(mkey, val):
        set_calls.append((mkey, val))
    with patch.object(udb, "get_meta_value", _get), \
         patch.object(udb, "set_meta_value", _set), \
         patch.object(patrol_mod, "_cfg",
                      {"interval_seconds": 300, "confirm_minutes": 10,
                       "lookback_mode": "runtime", "compensate_enabled": True}):
        n = patrol_mod._seed_meta_from_file()
    assert n == 3, "只 seed 三个空键（interval 已有值跳过）"
    assert set_calls == [
        (patrol_mod._CONFIRM_META, "10"),
        (patrol_mod._LOOKBACK_META, "runtime"),
        (patrol_mod._COMPENSATE_ENABLED_META, "true")], \
        f"已有键不得覆盖；bool 序列化小写，实际 {set_calls}"


def test_s50_3_seed_failure_swallowed():
    # UC50-3：meta 读写异常吞掉返回 0（继续以代码默认+meta 为准，不炸启动）
    import src.user.db as udb
    with patch.object(udb, "get_meta_value",
                      lambda mkey: (_ for _ in ()).throw(RuntimeError("db gone"))), \
         patch.object(patrol_mod, "_cfg", {"interval_seconds": 300}):
        assert patrol_mod._seed_meta_from_file() == 0   # 不得抛出


# ====== S51【覆盖对账·第23轮】已知集轮内共享（R22） ======


def test_s51_1_known_set_shared_within_round():
    # UC51-1：run_patrol_once 轮内 _known_task_ids 恰好调用一次、双检测收到
    # 同一集合对象（原两检测各自全表拉同一集合=轮内双拉的封口）
    seen = []

    def _broken(cutoff, known=None):
        seen.append(("broken", id(known), known))
        return []

    def _zombies(known=None):
        seen.append(("zombie", id(known), known))
        return []
    calls = []

    def _known():
        calls.append(1)
        return {("broken_chain", "t-x")}
    with patch.object(patrol_mod, "_known_task_ids", _known), \
         patch.object(patrol_mod, "detect_broken_chains", _broken), \
         patch.object(patrol_mod, "detect_zombies", _zombies), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"lookback_mode": "runtime"}), \
         patch.object(patrol_mod, "insert_findings_batch", lambda fs, chunk=500: 0), \
         patch.object(patrol_mod, "cleanup_terminal_findings", lambda d=-1: 0):
        patrol_mod.run_patrol_once()
    assert len(calls) == 1, f"轮内必须恰好一次集合拉取，实际 {len(calls)}"
    assert seen[0][2] is seen[1][2], "双检测必须共享同一集合对象（引用而非拷贝）"
    assert seen[0][2] == {("broken_chain", "t-x")}, "集合内容透传"


def test_s51_2_standalone_calls_still_compute_known():
    # UC51-2：独立调用（不传 known）自行拉取——旧签名兼容（路由/测试直调路径）
    import threading
    known_rows = [("broken_chain", "t-known")]
    fake = FakeDb([
        ("SELECT kind, task_id FROM grape_t_patrol_findings", known_rows),
        ("FROM agent_tasks WHERE status='running'", []),
        ("FROM agent_tasks WHERE status='queued'", []),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()):
        out = patrol_mod.detect_zombies()   # 不传 known：内部自行拉取
    assert out == []
    known_q = [sql for (sql, _) in fake.executed
               if "kind, task_id FROM grape_t_patrol_findings" in sql]
    assert len(known_q) == 1, "独立调用自行拉取一次"


# ====== S52【覆盖对账·第24轮】known 共享参数生效语义 + meta 坏值回落 ======


def test_s52_1_broken_detect_honors_shared_known():
    # UC52-1：known 共享参数在真 detect 函数中生效——传入的集合直接用于短路
    # （t-1 跳过），且不再自查 findings 表（s51 只验证了 mock 接收者收到参数，
    # 未验证真函数消费它）
    import threading
    fake = _scan_db([_cand_row("t-1", issue="#101",
                               result_text=json.dumps({"next_step": "a2a-qa"})),
                     _cand_row("t-2", issue="#102",
                               result_text=json.dumps({"next_step": "a2a-qa"}))])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "issue_terminal", lambda *a: ""), \
         patch.object(patrol_mod, "l1_event_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "l2_downstream_exists", lambda *a, **k: False), \
         patch.object(patrol_mod, "_subtask_parent", lambda *a: ""), \
         patch.object(patrol_mod, "patrol_runtime_config",
                      lambda: {"confirm_minutes": 10}):
        out = patrol_mod.detect_broken_chains(
            "", known={("broken_chain", "t-1")})
    assert [f["task_id"] for f in out] == ["t-2"], "传入 known 必须生效（t-1 短路）"
    known_q = [sql for (sql, _) in fake.executed
               if "kind, task_id FROM grape_t_patrol_findings" in sql]
    assert known_q == [], "传入 known 时不得再自查 findings 表（共享语义）"


def test_s52_2_zombie_detect_honors_shared_known():
    # UC52-2：detect_zombies 同款——传入 known 短路 queued 僵尸且不再自查表
    import threading
    fake = FakeDb([
        ("FROM agent_tasks WHERE status='running'", []),
        ("FROM agent_tasks WHERE status='queued'",
         [_queued_row("q-1", created_at=_old(90)),
          _queued_row("q-2", created_at=_old(90))]),
    ])
    with patch.object(patrol_mod, "db", fake), \
         patch.object(patrol_mod, "db_lock", threading.Lock()), \
         patch.object(patrol_mod, "_engine_unreachable", lambda rid: True), \
         patch.object(patrol_mod, "_scene_missing", lambda rk, iid: False):
        out = patrol_mod.detect_zombies(known={("zombie_queued_dead", "q-1")})
    assert [f["task_id"] for f in out] == ["q-2"], "传入 known 必须生效（q-1 短路）"
    known_q = [sql for (sql, _) in fake.executed
               if "kind, task_id FROM grape_t_patrol_findings" in sql]
    assert known_q == [], "传入 known 时不得再自查 findings 表"


def test_s52_3_meta_mode_bad_value_falls_back():
    # UC52-3：lookback_mode 的 meta 层坏值回落（文件层 t9 已测、meta 层缺口）——
    # 非 audit/runtime 值回落 _cfg 默认并告警，不炸配置读取
    with patch.object(patrol_mod, "_cfg",
                      {"interval_seconds": 300, "confirm_minutes": 10,
                       "lookback_mode": "runtime", "compensate_enabled": True}), \
         patch.object(patrol_mod, "_meta_value",
                      lambda k: "garbage" if k == patrol_mod._LOOKBACK_META else ""):
        rc = patrol_mod.patrol_runtime_config()
    assert rc["lookback_mode"] == "runtime", "meta 坏值必须回落 _cfg 默认"
    # 合法值照常透传
    with patch.object(patrol_mod, "_cfg",
                      {"interval_seconds": 300, "confirm_minutes": 10,
                       "lookback_mode": "runtime", "compensate_enabled": True}), \
         patch.object(patrol_mod, "_meta_value",
                      lambda k: "audit" if k == patrol_mod._LOOKBACK_META else ""):
        rc2 = patrol_mod.patrol_runtime_config()
    assert rc2["lookback_mode"] == "audit", "合法 meta 值必须透传（t10 语义回归）"


# ====== S53【外部复审·收口】shutdown cancel 补齐 + 补偿链卸载（R24） ======


def test_s53_1_main_shutdown_cancels_patrol_task():
    # UC53-1：进程关闭必须 cancel patrol 后台任务——main.py 742 行注释记录过
    # 同类真实事故（漏 cancel → 循环残留后台继续写库 → 用例成片假失败），
    # patrol_loop 是无限循环写库任务，复发封口（结构断言，先例 test_s24_1 钉法）
    src = open(os.path.join(ROOT, "src/api/main.py"), encoding="utf-8").read()
    i_task = src.find("_patrol_task = asyncio.create_task(patrol_loop())")
    i_cancel = src.find("_patrol_task.cancel()")
    i_wait = src.find("wait_for(_patrol_task, timeout=5)")
    assert 0 < i_task < i_cancel, "启动创建的 patrol 任务必须在 shutdown 被 cancel"
    assert 0 < i_cancel < i_wait, "cancel 后必须等待收口（同 obs 三循环口径）"
    # shutdown 段而非启动段：cancel 必须出现在 yield 之后的清理段
    assert src.find("yield") < i_cancel, "cancel 必须在 lifespan 清理段"


def test_s53_2_compensate_sync_chain_offloaded():
    # UC53-2：同步责任链（载入+五 handler 持锁查询）在工作线程执行（D23 卸载），
    # 派发保持 async await；短路路径不派发、完整路径派发一次
    import asyncio
    import threading
    main_thread = threading.get_ident()
    seen = {}

    class _Ctx:
        verdict = "chain_passed"

    class _H:
        name = "fake_handler"

        def handle(self, ctx):
            seen["chain_thread"] = threading.get_ident()
            return True

    class _D:
        async def handle(self, ctx):
            seen["dispatched"] = True
            return True

    with patch.object(pc, "_load_finding", lambda fid: _Ctx()), \
         patch.object(pc, "build_chain", lambda: [_H()]), \
         patch.object(pc, "DispatchHandler", _D):
        verdict = asyncio.run(pc.compensate_finding("f-1"))
    assert seen["chain_thread"] != main_thread, "同步链必须在工作线程执行（D23）"
    assert seen.get("dispatched") and verdict == "chain_passed", \
        "链跑完未短路必须派发且返回 verdict"


def test_s53_3_short_circuit_skips_dispatch():
    # UC53-3：同步链短路 → 不派发、直接返回 verdict（编排语义不因卸载改变）
    import asyncio
    seen = {}

    class _Ctx:
        verdict = "not_open"

    class _H:
        name = "gate"

        def handle(self, ctx):
            return False   # 短路

    class _D:
        async def handle(self, ctx):
            seen["dispatched"] = True
            return True

    with patch.object(pc, "_load_finding", lambda fid: _Ctx()), \
         patch.object(pc, "build_chain", lambda: [_H()]), \
         patch.object(pc, "DispatchHandler", _D):
        verdict = asyncio.run(pc.compensate_finding("f-2"))
    assert verdict == "not_open" and "dispatched" not in seen, \
        "短路路径不得派发（六段编排不变）"


def test_s53_4_missing_finding_returns_early():
    # UC53-4：finding 不存在 → "missing" 早退（载入在工作线程、零派发）
    import asyncio
    with patch.object(pc, "_load_finding", lambda fid: None):
        assert asyncio.run(pc.compensate_finding("f-404")) == "missing"
