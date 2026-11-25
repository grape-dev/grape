"""Issue #3790 回归测试：场景未纳入 scene pool 的派发降级可观测化。

覆盖：
1. `get_scene_pool_engine_ids(source_out=)`：仓内未登记场景上抛 scene_missing，返回语义不变；
2. 告警按 (repo_key, scene_id, agent_type) 窗口收敛：窗口内首条输出、后续计数抑制、窗口滚动汇总；
3. `_scene_pool_gate` 把来源带给调用方（pool 路径），selector 路径来源恒空；
4. `_resolve_pool_engine_source`：显式 scene_missing 不被「带 scene 上下文 → scene」覆盖；
5. `_enqueue_pipeline_task` 把来源随 enqueue_task 落 agent_tasks.pool_engine_source；
6. 事件 payload 回填 pool_engine_source / scene_id / repo_key / agent_type（事件中心可筛）；
7. mgmt 写侧提示：`scene_pool_binding_warning` + `set_repo_scene` 告警但写入。

测试风格对齐 tests/runtime/test_issue_541_template_scene_id.py（真实 sqlite 测试库）。
"""
import json
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.domains.mgmt.user.db import add_managed_repo, get_repo_scene, set_repo_scene  # noqa: E402
from src.api.pipeline_config import create_custom_scene, invalidate_scene_cache  # noqa: E402

LOGGER_NAME = "devops-bot"


def _unique_repo(prefix: str = "r3790") -> str:
    return f"repo-{prefix}-{uuid.uuid4().hex[:10]}"


def _repo_key(prefix: str = "r3790") -> str:
    return f"gitcode:test3790/{_unique_repo(prefix)}"


def _unique_name(prefix: str = "s3790") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_scene(repo_key: str, nodes: list | None = None) -> str:
    scene_id = f"custom-{uuid.uuid4().hex[:8]}"
    create_custom_scene(
        scene_id=scene_id, name=_unique_name("copy3790"), description="issue 3790 test",
        repo_key=repo_key, nodes=nodes or [])
    return scene_id


def _cleanup(repo_key: str, scene_ids: list):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM events WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (repo_key,))
        db.commit()
    for sid in scene_ids:
        invalidate_scene_cache(sid, repo_key)


def _insert_event(repo_key: str) -> int:
    from src.domains.runtime.event_store import append_event
    return append_event(repo_key=repo_key, event_type="a2a_event", title="t3790",
                        issue_id="#3790", agent_type="sre", source="a2a",
                        payload={"origin": "test3790"}, status="running")


class TestEngineSelectSourceOut:
    """1. 池解析上抛 scene_missing 来源（返回语义不变）。"""

    def test_missing_scene_reports_scene_missing(self):
        from src.domains.runtime.engine.engine_select import (
            POOL_SOURCE_SCENE_MISSING, get_scene_pool_engine_ids)
        repo_key = _repo_key()
        ghost = _unique_name("ghost3790")
        src: dict = {}
        ret = get_scene_pool_engine_ids(ghost, "sre", repo_key=repo_key, source_out=src)
        # 既有返回语义不变：仓内未登记 + 无绑定 → ""（不是 None）
        assert ret == ""
        assert src.get("pool_engine_source") == POOL_SOURCE_SCENE_MISSING
        assert POOL_SOURCE_SCENE_MISSING == "scene_missing"

    def test_registered_scene_has_no_source_marker(self):
        from src.domains.runtime.engine.engine_select import get_scene_pool_engine_ids
        repo_key = _repo_key()
        scene_id = _create_scene(repo_key)
        try:
            src: dict = {}
            ret = get_scene_pool_engine_ids(scene_id, "sre", repo_key=repo_key, source_out=src)
            assert ret == ""
            assert "pool_engine_source" not in src
        finally:
            _cleanup(repo_key, [scene_id])

    def test_source_out_is_optional(self):
        """既有调用点不传 source_out 时行为零变化（不抛、返回同值）。"""
        from src.domains.runtime.engine.engine_select import get_scene_pool_engine_ids
        repo_key = _repo_key()
        ghost = _unique_name("ghost3790")
        assert get_scene_pool_engine_ids(ghost, "sre", repo_key=repo_key) == ""


class TestSceneMissingAlertConvergence:
    """2. 告警窗口收敛：连续派发不再一等一刷屏。"""

    def test_same_key_first_emits_then_suppresses(self, monkeypatch, caplog):
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "_SCENE_MISSING_ALERT_WINDOW_SECONDS", 300)
        es._scene_missing_alert_state.clear()
        key = ("gitcode:test3790/conv-a", "scene-conv-a", "sre")
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            r1 = es._alert_scene_missing(*key)
            r2 = es._alert_scene_missing(*key)
            r3 = es._alert_scene_missing(*key)
        assert r1["emitted"] is True and r1["suppressed"] == 0
        assert r2["emitted"] is False and r3["emitted"] is False
        assert r3["suppressed"] == 2 and r3["window_count"] == 2 and r3["total_count"] == 3
        lines = [r.getMessage() for r in caplog.records if "scene_missing" in r.getMessage()]
        assert len(lines) == 1, f"窗口内应只输出 1 条，实际 {len(lines)}"
        assert "pool_engine_source=scene_missing" in lines[0]
        assert "scene=scene-conv-a" in lines[0]
        assert "agent=sre" in lines[0]
        # Loki 既有查询串保持可命中（口径统一）
        assert "get_scene_pool_engine_ids" in lines[0]

    def test_window_rollover_emits_summary_with_suppressed(self, monkeypatch, caplog):
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "_SCENE_MISSING_ALERT_WINDOW_SECONDS", 300)
        es._scene_missing_alert_state.clear()
        key = ("gitcode:test3790/conv-b", "scene-conv-b", "coding")
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            r1 = es._alert_scene_missing(*key)
            r2 = es._alert_scene_missing(*key)
            r3 = es._alert_scene_missing(*key)
            # 窗口到期（把上次输出时间整体前移一个窗口）→ 下一条输出汇总 + 被抑制条数
            es._scene_missing_alert_state[key]["last_emit"] -= 300
            r4 = es._alert_scene_missing(*key)
        assert r1["emitted"] is True
        assert r2["emitted"] is False and r3["suppressed"] == 2
        assert r4["emitted"] is True
        assert r4["suppressed"] == 2, "窗口滚动应输出被抑制条数"
        assert r4["window_count"] == 3, "窗口条数 = 本窗口（含被抑制）总条数"
        assert r4["total_count"] == 4
        lines = [r.getMessage() for r in caplog.records if "scene_missing" in r.getMessage()]
        assert len(lines) == 2, f"跨两个窗口应输出 2 条，实际 {len(lines)}"
        assert "suppressed=2" in lines[1] and "window_count=3" in lines[1]

    def test_distinct_keys_do_not_share_window(self, monkeypatch, caplog):
        import src.domains.runtime.engine.engine_select as es
        monkeypatch.setattr(es, "_SCENE_MISSING_ALERT_WINDOW_SECONDS", 300)
        es._scene_missing_alert_state.clear()
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            a = es._alert_scene_missing("gitcode:test3790/conv-c", "s1", "sre")
            b = es._alert_scene_missing("gitcode:test3790/conv-c", "s2", "sre")
        assert a["emitted"] is True and b["emitted"] is True


class TestScenePoolGatePropagation:
    """3. 门禁把来源带给调用方；selector 路径来源恒空。"""

    def test_pool_payload_carries_scene_missing(self, monkeypatch):
        import src.domains.runtime.engine.engine_select as es
        import src.domains.runtime.pipeline.event_pipeline as ep
        monkeypatch.setattr(es, "get_scene_engine_selector", lambda *a, **kw: None)
        repo_key = _repo_key()
        ghost = _unique_name("ghost3790")
        mode, payload, fail = ep._scene_pool_gate(ghost, "sre", repo_key=repo_key, user_id=0)
        assert (mode, fail) == ("pool", None)
        assert payload["pool_engine_ids"] == ""
        assert payload["pool_engine_source"] == "scene_missing"

    def test_selector_payload_source_is_empty(self, monkeypatch):
        import src.domains.runtime.engine.engine_select as es
        import src.domains.runtime.pipeline.event_pipeline as ep
        monkeypatch.setattr(es, "get_scene_engine_selector",
                            lambda *a, **kw: {"tags": ["scope:repo"]})
        monkeypatch.setattr(es, "selector_enqueue_failure_reason", lambda *a, **kw: "")
        mode, payload, fail = ep._scene_pool_gate("scene-x", "sre", repo_key="gitcode:t/x", user_id=0)
        assert (mode, fail) == ("selector", None)
        assert payload["pool_engine_source"] == ""
        assert payload["pool_selector"]


class TestSourceNotOverwritten:
    """4. 显式来源不被上下文推导覆盖。"""

    def test_explicit_scene_missing_wins_over_scene_context(self):
        import src.domains.runtime.pipeline.event_pipeline as ep
        evt = {"scene_id": "custom-abc"}
        assert ep._resolve_pool_engine_source("[1,2]", "scene_missing", evt) == "scene_missing"
        assert ep._resolve_pool_engine_source("", "scene_missing", evt) == "scene_missing"

    def test_inference_unchanged_when_no_explicit_source(self):
        import src.domains.runtime.pipeline.event_pipeline as ep
        assert ep._resolve_pool_engine_source("[1]", "", {"scene_id": "s"}) == "scene"
        assert ep._resolve_pool_engine_source("[1]", "", {"_scene_id": "s"}) == "scene"
        assert ep._resolve_pool_engine_source("[1]", "", {}) == "auto"
        assert ep._resolve_pool_engine_source("", "", {"scene_id": "s"}) == ""
        assert ep._resolve_pool_engine_source("[1]", "directed", {}) == "directed"


class TestEnqueuePersistsSource:
    """5. 来源随 enqueue_task 落 agent_tasks.pool_engine_source。"""

    def test_enqueue_pipeline_task_passes_source(self, monkeypatch, tmp_path):
        import src.api.session_store as ss
        import src.domains.runtime.engine.instances as inst
        import src.domains.runtime.engine.service as svc
        import src.domains.runtime.event_store as evs
        import src.domains.runtime.pipeline.event_pipeline as ep
        import src.kernel.prompt_builder as pb

        repo_key = _repo_key()
        seq = _insert_event(repo_key)
        captured = {}

        monkeypatch.setattr(ep, "_skip_dispatch_for_takeover", lambda *a, **kw: False)
        monkeypatch.setattr(ep, "is_plugin_mode", lambda: True)
        monkeypatch.setattr(ep, "get_agent_workdir", lambda *a, **kw: str(tmp_path))
        monkeypatch.setattr(ep, "resolve_actor_account", lambda *a, **kw: "")
        monkeypatch.setattr(ep, "_inject_scene_env", lambda *a, **kw: None)
        monkeypatch.setattr(ep, "_inject_bot_agent_env", lambda *a, **kw: None)
        monkeypatch.setattr(ep, "_inject_issue_pr_env", lambda *a, **kw: None)
        monkeypatch.setattr(inst, "runtime_instance_name", lambda *a, **kw: "sre-test")
        monkeypatch.setattr(ss, "get_or_create_agent_session",
                            lambda *a, **kw: {"id": "sess-3790"})
        monkeypatch.setattr(ss, "get_or_create_issue_session",
                            lambda *a, **kw: {"id": "isess-3790"})
        monkeypatch.setattr(ss, "guard_issue_session_pointer", lambda *a, **kw: "")
        monkeypatch.setattr(evs, "update_event_config", lambda *a, **kw: None)
        monkeypatch.setattr(pb, "build_devops_agent_prompt", lambda *a, **kw: "prompt-3790")
        monkeypatch.setattr(pb, "resolve_task_server_env", lambda *a, **kw: {})
        monkeypatch.setattr("src.api.event_archive.init_event_json", lambda *a, **kw: None)

        def _fake_enqueue(**kwargs):
            captured.update(kwargs)
            return "t_3790"

        monkeypatch.setattr(svc, "enqueue_task", _fake_enqueue)

        tid = ep._enqueue_pipeline_task(
            repo_key, seq, "sre", "opencode", "", {"scene_id": "custom-ghost3790"},
            skill_id=0, skill_path="", runtime_id=0, issue_id="#3790",
            pool_engine_ids="", pool_engine_source="scene_missing")
        assert tid == "t_3790", "入队应成功返回真实 task id"
        assert captured.get("pool_engine_source") == "scene_missing", \
            f"来源被改写：{captured.get('pool_engine_source')!r}"

        # 事件 payload 同步回填（事件中心可筛）
        with db_lock:
            row = db.cursor().execute("SELECT payload FROM events WHERE seq=?", (seq,)).fetchone()
        payload = json.loads(row[0] or "{}")
        assert payload["pool_engine_source"] == "scene_missing"
        assert payload["scene_id"] == "custom-ghost3790"
        assert payload["repo_key"] == repo_key
        assert payload["agent_type"] == "sre"
        assert payload["origin"] == "test3790", "回填必须合并原 payload，不得覆盖"
        _cleanup(repo_key, [])


class TestEventPayloadMerge:
    """6. merge_event_payload 幂等 + 非法 JSON 重建。"""

    def test_merge_is_idempotent_and_preserves_existing(self):
        from src.domains.runtime.event_store import merge_event_payload
        repo_key = _repo_key()
        seq = _insert_event(repo_key)
        try:
            extra = {"pool_engine_source": "scene_missing", "scene_id": "s"}
            assert merge_event_payload(seq, extra) is True
            assert merge_event_payload(seq, extra) is True
            with db_lock:
                row = db.cursor().execute(
                    "SELECT payload FROM events WHERE seq=?", (seq,)).fetchone()
            payload = json.loads(row[0])
            assert payload["pool_engine_source"] == "scene_missing"
            assert payload["origin"] == "test3790"
        finally:
            _cleanup(repo_key, [])

    def test_merge_missing_event_returns_false(self):
        from src.domains.runtime.event_store import merge_event_payload
        assert merge_event_payload(0, {"a": 1}) is False
        assert merge_event_payload(999999999, {"a": 1}) is False


class TestMgmtBindingHint:
    """7. 场景保存/建仓写侧提示（不阻断保存）。"""

    def test_binding_warning_for_unregistered_scene(self):
        from src.domains.mgmt.pipeline_config import scene_pool_binding_warning
        repo_key = _repo_key()
        hint = scene_pool_binding_warning(_unique_name("ghost3790"), repo_key)
        assert "scene_missing" in hint
        assert repo_key in hint

    def test_binding_warning_empty_for_registered_scene(self):
        from src.domains.mgmt.pipeline_config import scene_pool_binding_warning
        repo_key = _repo_key()
        scene_id = _create_scene(repo_key)
        try:
            assert scene_pool_binding_warning(scene_id, repo_key) == ""
            assert scene_pool_binding_warning("", repo_key) == ""
        finally:
            _cleanup(repo_key, [scene_id])

    def test_set_repo_scene_warns_but_writes(self, caplog):
        repo_key = _repo_key()
        add_managed_repo(repo_key, "test3790", _unique_repo("managed"), dev_scene="")
        ghost = _unique_name("ghost3790")
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                hint = set_repo_scene(repo_key, ghost)
            assert hint and "scene_missing" in hint
            assert any("scene_missing" in r.getMessage() for r in caplog.records), \
                "写侧应输出 scene_missing 提示日志"
            with db_lock:
                row = db.cursor().execute(
                    "SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,)).fetchone()
            assert row and row[0] == ghost, "提示不阻断写入（历史直写路径仍需落库）"
        finally:
            _cleanup(repo_key, [])

    def test_set_repo_scene_registered_scene_no_hint(self):
        repo_key = _repo_key()
        add_managed_repo(repo_key, "test3790", _unique_repo("managed"), dev_scene="")
        scene_id = _create_scene(repo_key)
        try:
            assert set_repo_scene(repo_key, scene_id) == ""
            assert get_repo_scene(repo_key) == scene_id
        finally:
            _cleanup(repo_key, [scene_id])