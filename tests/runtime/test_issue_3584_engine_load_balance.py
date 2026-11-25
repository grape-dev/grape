"""Issue #3584：pick_engine 多引擎负载分摊 / 轮转 / 日志 / 单点塌陷测试。

覆盖 Spec 验收项：
- 最小负载优先（不再钉死最小 runtime_id）
- 负载并列时按 seq 取模轮转，分布覆盖全部绑定引擎
- 优先级档位仍先于负载（repo 档优先于 global 档）
- 单台满载时其余绑定引擎承接（不再整场景单点塌陷）
- _pick_decision 决策日志含候选负载（load 字段）与选中理由（load/rotation）
- explain_engine_selection 候选输出含 load
"""
import datetime
import json
import logging

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import engine_select as es


_PURGE_SQL = (
    "DELETE FROM agent_tasks",
    "DELETE FROM agent_runtimes",
    "DELETE FROM runtime_credentials",
    "DELETE FROM engine_capabilities",
    "DELETE FROM devops_pipeline_scene",
    "DELETE FROM user_repos",
    "DELETE FROM runtime_health_scores",
)


def _purge_tables():
    """清空本套用例触及的表（yield 前后复用，避免 DELETE 语句重复）。"""
    with db_lock:
        c = db.cursor()
        for sql in _PURGE_SQL:
            c.execute(sql)
        db.commit()


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
    _purge_tables()
    # 每用例替换为全新轮转池，保证轮转断言确定性（测试内注入，不依赖生产复位钩子）
    monkeypatch.setattr(es, "_PICK_ROTATION_POOL", es._RotationPool())
    yield
    _purge_tables()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global", owner_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h" + name, name, "dev-" + name, scope, owner_id, "tester"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, status="online", healthy=1, installed=1,
               scope="global", owner_id="") -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, _now(), scope, owner_id))
        rid = c.lastrowid
        c.execute(
            "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)",
            (rid,))
        db.commit()
        return rid


def _set_tags(runtime_id, tags):
    """显式覆盖引擎能力标签（默认 _mk_engine 建了 'all'）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (runtime_id,))
        for tag in tags:
            c.execute(
                "INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
                (runtime_id, tag))
        db.commit()


def _mk_task(runtime_id, status="running", tid=1):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, issue_id) VALUES (?,?,?, 'owner/repo', 'coding', "
            "'a2a_event', ?, 'x', '#0')",
            (tid, f"sess-{tid}", runtime_id, status))
        db.commit()


def _set_health(runtime_id, score=0, consecutive_failures=0):
    """写入健康分表并清缓存（健康分 < 阈值 30 → _filter_by_health 过滤）。"""
    from src.domains.runtime.engine import health_scorer as hs
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO runtime_health_scores "
            "(runtime_id, health_score, total_tasks, failed_tasks, completed_tasks, "
            "consecutive_failures, failure_rate, last_status, window_days, updated_at) "
            "VALUES (?,?,0,0,0,?,0,'fail',7,?)",
            (runtime_id, score, consecutive_failures, _now()))
        db.commit()
    hs._cache.clear()


def _mk_scene(scene_id, agent_type, engine_ids, repo_key="owner/repo", user_id=1):
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) VALUES (?, 'gitcode:owner/repo', 'owner', 'repo', 1)", (user_id,))
        except Exception:
            pass
        db.commit()
    nodes = [{"id": agent_type, "tag": agent_type, "skill": "test-skill",
              "mode": "emit", "next": "done", "handoff": "complete",
              "engine_runtime_ids": engine_ids}]
    from src.api.pipeline_config import create_custom_scene
    _rk = repo_key.split(":")[-1] if ":" in repo_key else repo_key
    create_custom_scene(
        scene_id=scene_id, name=scene_id, description="",
        repo_key=_rk,
        nodes=nodes)


def _pick(scene_id):
    return es.pick_engine("owner/repo", "coding", scene_id=scene_id)


class TestLoadBalance:
    def test_least_load_preferred_over_smallest_id(self):
        """旧缺陷回归：不再恒选最小 runtime_id，而是选最小负载的引擎。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _mk_task(e1, "running", tid=1)
        _mk_task(e1, "dispatched", tid=2)
        _mk_scene("t3584-load", "coding", [e1, e2])
        assert e1 < e2
        assert _pick("t3584-load") == e2

    def test_equal_load_rotates_and_covers_all_bound_engines(self):
        """负载并列时按 seq 取模轮转：连续派发覆盖全部绑定引擎（分布验收）。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        e3 = _mk_engine("opencode-c", credential_id=_mk_cred("c3"))
        _mk_scene("t3584-rot", "coding", [e1, e2, e3])
        picks = [_pick("t3584-rot") for _ in range(6)]
        assert set(picks) == {e1, e2, e3}, f"存在引擎全程空转: {picks}"
        # 轮转顺序：seq 取模按 runtime_id 升序集合
        assert picks[:3] == [e1, e2, e3]

    def test_busy_engine_recovers_after_load_release(self):
        """单点塌陷修复：满载引擎被绕开，负载回落后重新参与分摊。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _mk_scene("t3584-recover", "coding", [e1, e2])
        _mk_task(e2, "running", tid=1)
        assert _pick("t3584-recover") == e1  # 单候选不推进 seq
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_tasks SET status='done' WHERE id=1")
            db.commit()
        assert _pick("t3584-recover") == e1  # 负载均为 0，轮转从 seq=0 起算（未被单候选消耗）
        assert _pick("t3584-recover") == e2

    def test_priority_scope_still_wins_over_load(self):
        """档位（priority）仍先于负载：repo 档满载也优先于 global 档空载。"""
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        e_repo = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        e_glob = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        _mk_task(e_repo, "running", tid=1)
        _mk_scene("t3584-prio", "coding", [e_glob, e_repo])
        assert _pick("t3584-prio") == e_repo

    def test_sre_branch_syncs_load_rotation(self):
        """SRE 专用分支（user/repo 档）同步策略：并列负载轮转分摊。"""
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        e1 = _mk_engine("opencode-r1", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        e2 = _mk_engine("opencode-r2", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        _mk_scene("t3584-sre", "sre", [e1, e2])
        picks = [es.pick_engine("owner/repo", "sre", scene_id="t3584-sre") for _ in range(4)]
        assert set(picks) == {e1, e2}


class TestDecisionLog:
    def test_pick_decision_log_contains_load_and_reason(self, caplog):
        """决策日志候选含 load 字段，reason 标注 load/rotation 依据。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _mk_task(e1, "running", tid=1)
        _mk_scene("t3584-log", "coding", [e1, e2])
        assert _pick("t3584-log") == e2
        recs = [r.getMessage() for r in caplog.records
                if "engine_select_decision" in r.getMessage()
                and '"event": "pick_engine"' in r.getMessage()]
        assert recs, "缺少 pick_engine 决策日志"
        payload = json.loads(recs[-1].split("engine_select_decision ", 1)[1])
        assert payload["result"] == "selected"
        assert payload["selected"] == e2
        assert "load=" in payload["reason"]
        cand = {c["rid"]: c for c in payload["candidates"]}
        assert cand[e1]["load"] == 1
        assert cand[e2]["load"] == 0

    def test_expose_load_in_explain(self):
        """explain_engine_selection 候选输出含 load，与决策日志口径一致。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _mk_task(e1, "running", tid=1)
        _mk_scene("t3584-explain", "coding", [e1, e2])
        out = es.explain_engine_selection("owner/repo", "coding", scene_id="t3584-explain")
        cand = {c["runtime_id"]: c for c in out["candidates"]}
        assert cand[e1]["load"] == 1
        assert cand[e2]["load"] == 0
        assert out["selected"] == e2


class TestReviewFixes:
    """QA 审查意见（PR #1870 评论 202440848）回归。"""

    def test_unknown_rule_falls_back_to_random_with_warning(self, caplog, monkeypatch):
        """未知 rule 值显式回退旧 random 语义并告警，不静默落入 order 负载轮转。

        通过替换 random.choice 锁定 random 分支：若误落 order，会按 seq=0 选最小 id e1，
        且不会调用 random.choice。
        """
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _mk_scene("t3584-unknown-rule", "coding", [e1, e2])
        cand = [{"runtime_id": e1, "priority": 50, "load": 0},
                {"runtime_id": e2, "priority": 50, "load": 0}]
        calls = []
        monkeypatch.setattr(es.random, "choice",
                            lambda seq: (calls.append(list(seq)), seq[-1])[1])
        caplog.set_level(logging.WARNING, logger="devops-bot")
        picked = es._pick_decision(cand, repo_key="owner/repo", agent_type="coding",
                                   scene_id="t3584-unknown-rule", scope="global",
                                   rule="ordre")  # 拼写错误（应为 order）
        assert picked["runtime_id"] == e2
        assert len(calls) == 1, "未知 rule 必须走 random.choice 分支"
        assert any("unknown rule" in r.getMessage() for r in caplog.records)

    def test_pick_decision_passes_cfg_no_config_reload(self, monkeypatch, caplog):
        """传入 cfg 后热路径不再每个候选重复 load_engine_selection()。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _mk_task(e1, "running", tid=1)
        _mk_scene("t3584-cfg", "coding", [e1, e2])
        calls = []
        real_load = es.load_engine_selection
        monkeypatch.setattr(es, "load_engine_selection",
                            lambda: (calls.append(1), real_load())[1])
        assert _pick("t3584-cfg") == e2
        # pick_engine 自身读取 1 次配置，_pick_decision 收到 cfg 后不再重复读取
        assert len(calls) <= 1

    def test_attach_loads_returns_copies(self):
        """_attach_loads 返回副本，不就地改写调用方 dict（load 键不外泄）。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        _mk_task(e1, "running", tid=1)
        originals = [{"runtime_id": e1, "priority": 50}]
        out = es._attach_loads(originals)
        assert out[0]["load"] == 1
        assert "load" not in originals[0]

    def test_explain_selected_uses_same_snapshot(self, monkeypatch):
        """explain 的 selected 与 candidates 同一 load 快照（不再二次 pick_engine）。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _mk_task(e1, "running", tid=1)
        _mk_scene("t3584-explain2", "coding", [e1, e2])
        monkeypatch.setattr(es, "pick_engine",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("explain must not call pick_engine")))
        out = es.explain_engine_selection("owner/repo", "coding", scene_id="t3584-explain2")
        assert out["selected"] == e2

    def test_engine_loads_fail_open_logs_decision(self, monkeypatch, caplog):
        """_engine_loads 异常 fail-open 时输出决策日志（取证可见），返回空负载。"""
        import threading as _th
        caplog.set_level(logging.INFO, logger="devops-bot")
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))

        class _BoomDB:
            def cursor(self):
                raise RuntimeError("db down")
        monkeypatch.setattr(es, "_db", lambda: (_BoomDB(), _th.Lock()))
        assert es._engine_loads([e1]) == {}
        assert any('"event": "engine_loads"' in r.getMessage()
                   and '"result": "fail_open"' in r.getMessage()
                   for r in caplog.records)


class TestSecondReviewFixes:
    """QA 复审意见（PR #1870 评论 202443640）回归。"""

    def test_sre_all_filtered_follows_common_fallback(self):
        """Issue #3396 删除 SRE 特例后，SRE 与其它角色同走通用链。

        绑定引擎标签全不匹配 → 候选全灭，按通用语义回退会话主引擎（不再强制 None）。
        """
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        e1 = _mk_engine("opencode-r1", credential_id=repo_cid, scope="repo",
                        owner_id="owner/repo")
        _set_tags(e1, ["coding"])  # 与 sre 不匹配，且无 'all'
        _mk_scene("t3584-sre-nofallback", "sre", [e1])
        assert es.pick_engine("owner/repo", "sre", fallback_runtime_id=9999,
                              scene_id="t3584-sre-nofallback") == 9999

    def test_explain_does_not_advance_rotation_seq(self):
        """explain 只读 peek，不推进轮转序号；selected 与下一次真实派发一致。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        e3 = _mk_engine("opencode-c", credential_id=_mk_cred("c3"))
        _mk_scene("t3584-explain3", "coding", [e1, e2, e3])
        assert _pick("t3584-explain3") == e1  # 推进一次真实轮转
        bucket = es._rotation_bucket("owner/repo", "coding", "t3584-explain3", [e1, e2, e3])
        before = es._peek_rotation_seq(bucket)
        out = es.explain_engine_selection("owner/repo", "coding",
                                          scene_id="t3584-explain3")
        assert es._peek_rotation_seq(bucket) == before, "explain 不应推进轮转序号"
        assert out["selected"] == _pick("t3584-explain3")

    def test_decision_candidates_gated_by_info_level(self, monkeypatch):
        """INFO 未开时不构造候选全集，避免热路径逐候选健康/标签查询。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        cand = [{"runtime_id": e1, "priority": 50, "load": 0}]
        monkeypatch.setattr(es.log, "isEnabledFor", lambda level: False)
        assert es._decision_candidates(cand, {}, "coding") == []


class TestThirdReviewFixes:
    """QA rebase 复审（PR #1870，head=e534db661 轮）should-fix 回归。

    1. 决策日志候选全集口径：含被标签过滤掉的绑定引擎（Issue #605 可观测性契约）。
    2. #605 零派发留痕回归：SRE 无健康 / 全档位不健康兜底选中 / 全档位不健康无兜底
       三类决策均须输出 engine_select_decision（含 members 与 reason）。
    """

    def _decision_payloads(self, caplog):
        recs = [r.getMessage() for r in caplog.records
                if "engine_select_decision" in r.getMessage()
                and '"event": "pick_engine"' in r.getMessage()]
        return [json.loads(r.split("engine_select_decision ", 1)[1]) for r in recs]

    def test_decision_log_candidates_include_tag_filtered_members(self, caplog):
        """候选全集含被过滤成员：标签不匹配的绑定引擎也出现在 candidates 及过滤原因。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _set_tags(e2, ["sre"])  # e2 与 coding 不匹配，被 _filter_by_tags 过滤
        _mk_scene("t3584-fullset", "coding", [e1, e2])
        assert _pick("t3584-fullset") == e1
        payloads = self._decision_payloads(caplog)
        assert payloads, "缺少 pick_engine 决策日志"
        rids = {c["rid"] for c in payloads[-1]["candidates"]}
        assert rids == {e1, e2}, f"候选全集应含被过滤成员 e2: {rids}"

    def test_sre_no_healthy_logs_decision(self, caplog):
        """SRE 与通用链一致：绑定引擎全不健康 → 零派发留痕 reason=all_scopes_unhealthy。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        e1 = _mk_engine("opencode-r1", credential_id=repo_cid, scope="repo",
                        owner_id="owner/repo", healthy=0)
        e2 = _mk_engine("opencode-r2", credential_id=repo_cid, scope="repo",
                        owner_id="owner/repo", healthy=0)
        _mk_scene("t3584-sre-nolog", "sre", [e1, e2])
        assert es.pick_engine("owner/repo", "sre", fallback_runtime_id=9999,
                              scene_id="t3584-sre-nolog") is None
        payloads = self._decision_payloads(caplog)
        assert any(p["result"] == "engine_unavailable"
                   and p.get("reason") == "all_scopes_unhealthy"
                   and {c["rid"] for c in p["candidates"]} == {e1, e2}
                   for p in payloads), "SRE 无健康零派发缺少决策留痕"

    def test_all_scopes_unhealthy_health_fallback_logs_decision(self, caplog, monkeypatch):
        """全档位不健康、健康兜底选中 → 留痕 result=selected / reason=health_fallback。

        引擎本体 healthy=1/online（过 _apply_filters 的 only_healthy/only_online），
        但健康分 < 阈值 → 各档位 _filter_by_health 全灭，走 best_fallback 兜底。
        """
        caplog.set_level(logging.INFO, logger="devops-bot")
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _set_health(e1, score=0)
        _set_health(e2, score=0)
        _mk_scene("t3584-hfb", "coding", [e1, e2])
        monkeypatch.setattr(es, "_best_health_fallback", lambda members: e2)
        assert _pick("t3584-hfb") == e2
        payloads = self._decision_payloads(caplog)
        assert any(p["result"] == "selected"
                   and p.get("reason") == "health_fallback"
                   and p.get("selected") == e2
                   and {c["rid"] for c in p["candidates"]} == {e1, e2}
                   for p in payloads), "健康兜底选中缺少决策留痕"

    def test_all_scopes_unhealthy_no_fallback_logs_decision(self, caplog, monkeypatch):
        """全档位不健康且无兜底 → None 且留痕 reason=all_scopes_unhealthy。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _set_health(e1, score=0)
        _set_health(e2, score=0)
        _mk_scene("t3584-nofb", "coding", [e1, e2])
        monkeypatch.setattr(es, "_best_health_fallback", lambda members: None)
        assert _pick("t3584-nofb") is None
        payloads = self._decision_payloads(caplog)
        assert any(p["result"] == "engine_unavailable"
                   and p.get("reason") == "all_scopes_unhealthy"
                   and {c["rid"] for c in p["candidates"]} == {e1, e2}
                   for p in payloads), "全档位不健康零派发缺少决策留痕"


class TestFourthReviewFixes:
    """QA rebase 复审（PR #1870，head=3d6cc9d4a）should-fix 回归。"""

    def _decision_payloads(self, caplog):
        recs = [r.getMessage() for r in caplog.records
                if "engine_select_decision" in r.getMessage()
                and '"event": "pick_engine"' in r.getMessage()]
        return [json.loads(r.split("engine_select_decision ", 1)[1]) for r in recs]

    def test_rotation_seq_bucketed_per_scene(self):
        """轮转按场景分桶：两场景绑定同一组引擎，交织派发各自独立轮转（AC1）。"""
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _mk_scene("t3584-bucket-a", "coding", [e1, e2])
        _mk_scene("t3584-bucket-b", "coding", [e1, e2])
        a1 = _pick("t3584-bucket-a")
        b1 = _pick("t3584-bucket-b")
        a2 = _pick("t3584-bucket-a")
        b2 = _pick("t3584-bucket-b")
        assert (a1, a2) == (e1, e2), f"场景 A 未按自身 seq 轮转: {(a1, a2)}"
        assert (b1, b2) == (e1, e2), f"场景 B 被其它场景 seq 干扰: {(b1, b2)}"

    def test_fallback_decision_log_unified_candidates_schema(self, caplog):
        """fallback 决策日志复用 _decision_candidates，candidates 为统一 schema 列表（AC4）。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        assert es.pick_engine("owner/repo", "coding", fallback_runtime_id=9999) == 9999
        payloads = self._decision_payloads(caplog)
        fb = [p for p in payloads if p.get("result") == "fallback"]
        assert fb, "fallback 缺少决策日志"
        assert isinstance(fb[-1]["candidates"], list)


class TestFifthReviewFixes:
    """QA rebase 复审（PR #1870，head=c24de4edc）should-fix/nit 回归。

    1. AC4 fallback 留痕：绑定引擎被标签全过滤回落主引擎时，candidates 须含
       已加载候选（rid/priority/load），不再恒为空列表。
    2. 轮转池桶数量有界：超过上限按 FIFO 淘汰，避免长生命周期进程桶字典无限增长。
    """

    def _decision_payloads(self, caplog):
        recs = [r.getMessage() for r in caplog.records
                if "engine_select_decision" in r.getMessage()
                and '"event": "pick_engine"' in r.getMessage()]
        return [json.loads(r.split("engine_select_decision ", 1)[1]) for r in recs]

    def test_fallback_decision_log_includes_tag_filtered_candidates(self, caplog):
        """标签全过滤 → fallback 主引擎，决策日志仍可还原候选与 load（AC4）。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        e1 = _mk_engine("opencode-a", credential_id=_mk_cred("c1"))
        e2 = _mk_engine("opencode-b", credential_id=_mk_cred("c2"))
        _set_tags(e1, ["sre"])
        _set_tags(e2, ["sre"])  # 两台均与 coding 不匹配，绑定候选全被标签过滤
        _mk_task(e2, status="running", tid=11)  # 给 e2 制造负载，验证 load 也被带入
        _mk_scene("t3584-ac4-fallback", "coding", [e1, e2])
        assert es.pick_engine("owner/repo", "coding", fallback_runtime_id=9999,
                              scene_id="t3584-ac4-fallback") == 9999
        payloads = self._decision_payloads(caplog)
        fb = [p for p in payloads if p.get("result") == "fallback"]
        assert fb, "fallback 缺少决策日志"
        cands = {c["rid"]: c for c in fb[-1]["candidates"]}
        assert set(cands) == {e1, e2}, f"fallback 日志未带入被过滤候选: {cands}"
        assert all("load" in c and "priority" in c for c in cands.values())
        assert cands[e2]["load"] == 1, f"fallback 日志 load 未还原: {cands[e2]}"

    def test_rotation_pool_buckets_bounded(self, monkeypatch):
        """轮转池桶数量有上限：超出按 FIFO 淘汰，避免永久桶无限增长。"""
        monkeypatch.setattr(es, "_ROTATION_POOL_MAX_BUCKETS", 3)
        pool = es._RotationPool()
        for i in range(10):
            pool.advance(("owner/repo", "coding", f"scene-{i}", (1, 2)))
        assert len(pool._seqs) <= 3, f"轮转池桶未设上限: {len(pool._seqs)}"
        # 最早桶被淘汰后重新从 0 计数
        assert pool.peek(("owner/repo", "coding", "scene-0", (1, 2))) == 0
