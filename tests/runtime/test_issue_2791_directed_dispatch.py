"""Issue #2791（P1-8）：`engine_config.runtime_id` 定向派发优先解析。

用户症状（母单 #2747 实测）：CodeArts 容器里 `engine_config.runtime_id=17364`
被忽略，任务落到 `codearts-worker-devkit-test`（场景池里另一台引擎）。

约定（Spec「派发解析（优先级收口）」）：
- 命中：`engine_config.runtime_id` 指向的引擎已注册且健康 → **锁定该引擎、跳过池选择**，
  任务确定性入队到该引擎（agent_tasks.runtime_id = 指定值）。
- 未命中：回落全局池选择，但必须留结构化 WARN（含 runtime_id、回落原因、最终选中引擎），
  **不静默换引擎**。

测试 seam：统一入队口 `_enqueue_pipeline_task` → `enqueue_task(runtime_id, pool_engine_ids)`。
这是派发层唯一的公开收敛点（issue / PR 订阅 / A2A / fallback 全路径都经它入队），
断言写在它交给执行层的确定性路由参数上，而不是内部选择函数。
"""
import json
import sys
import os
import uuid
import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock  # noqa: E402

REPO = "gitcode:o/r2791"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h" + name, name, "dev-" + name, "global", "", "tester"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, status="online", healthy=1, installed=1,
               scope="global", last_seen=None):
    ls = last_seen if last_seen is not None else _now()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, ls, scope, ""))
        rid = c.lastrowid
        c.execute(
            "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)",
            (rid,))
        db.commit()
        return rid


def _mk_scene(scene_id, agent_type, engine_ids, user_id=1, repo_key="gitcode:o/r2791"):
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) "
                      "VALUES (?, 'gitcode:o/r2791', 'o', 'r2791', 1)", (user_id,))
        except Exception:
            pass
        db.commit()
    node = {"id": agent_type, "tag": agent_type, "skill": "test-skill",
            "mode": "emit", "next": "done", "handoff": "complete",
            "engine_runtime_ids": engine_ids}
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                        repo_key=repo_key, nodes=[node])


def _mk_issue(scene_id=""):
    with db_lock:
        c = db.cursor()
        num = 9000 + (uuid.uuid4().int % 900)
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, scene_id) "
            "VALUES (1, 'tester', ?, ?, 't2791', 'active', ?)",
            (REPO, num, scene_id))
        db.commit()
    return num


def _set_tags(rid, tags):
    """覆写引擎能力标签（池选择按 tag 过滤 agent_type；定向派发不按它否决）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
        for t in tags:
            c.execute("INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) "
                      "VALUES (?,?,1)", (rid, t))
        db.commit()
    return rid


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    # 文件顺序无关：`is_plugin_mode()` 的优先级是 meta.pipeline_mode > env，
    # 同批其它用例写过的 meta 值会盖掉本用例的 env，让 legacy/plugin 两条路径互相串味
    # （legacy 用例会静默走进 plugin 分支）。这里临时清空 meta、用例结束写回，
    # 派发模式只由本用例的 env 决定。
    from src.domains.mgmt.user.db import get_meta_value, set_meta_value
    _prev_mode = get_meta_value("pipeline_mode")
    _prev_exec = get_meta_value("execution_mode")
    set_meta_value("pipeline_mode", "")
    set_meta_value("execution_mode", "")
    # 本文件断言「执行层拿到的确定性路由参数」；执行通道固定为 auto
    # （远端引擎 → queue 入队），不受同批用例切到 direct 的影响。
    monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "auto")
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "plugin")
    monkeypatch.setattr("src.kernel.db_registry.uses_own_dsn", lambda *a, **k: False)
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "devops_agent_session", "agent_runtimes",
                  "runtime_credentials", "devops_pipeline_scene", "unified_user_mapping",
                  "user_repos", "engine_capabilities", "issue", "issue_state",
                  "grape_t_issue", "grape_t_issue_state"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE '%2791%' OR name LIKE '%2791%'")
            except Exception:
                pass
        db.commit()
    yield
    set_meta_value("pipeline_mode", _prev_mode or "")
    set_meta_value("execution_mode", _prev_exec or "")


def _capture_enqueue(monkeypatch, tmp_path):
    """把统一入队口下沉到「交给执行层的确定性路由参数」这一层来断言。"""
    from src.api import event_pipeline as ep

    captured = []
    monkeypatch.setattr("src.api.event_archive.init_event_json", lambda *a, **k: None)
    monkeypatch.setattr(ep, "get_agent_workdir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr("src.api.session_store.get_or_create_agent_session",
                        lambda *a, **k: {"id": "sess-2791"})
    monkeypatch.setattr("src.api.session_store.get_or_create_issue_session",
                        lambda *a, **k: {"id": "is-2791"})
    monkeypatch.setattr("src.api.session_store.guard_issue_session_pointer", lambda *a, **k: None)
    monkeypatch.setattr("src.api.event_store.update_event_config", lambda *a, **k: None)
    monkeypatch.setattr("src.domains.runtime.engine.instances.runtime_instance_name",
                        lambda *a, **k: "opencode-1")
    monkeypatch.setattr("src.kernel.prompt_builder.build_devops_agent_prompt", lambda *a, **k: "p-2791")
    monkeypatch.setattr("src.kernel.prompt_builder._resolve_server_url", lambda *a, **k: "http://srv")
    monkeypatch.setattr("src.domains.runtime.engine.service.collect_knowledge_files",
                        lambda *a, **k: {"project_info": {}, "meta": {}, "knowledge": {}})
    monkeypatch.setattr(ep, "_evaluate_skill_scope", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("src.domains.runtime.engine.service.verify_skill_materializable",
                        lambda *a, **k: (True, "", 0))
    failed = []
    monkeypatch.setattr(ep, "_fail_event", lambda seq, msg, **k: failed.append((seq, msg)))
    monkeypatch.setattr("src.domains.runtime.engine.service.enqueue_task",
                        lambda **k: captured.append(k) or "t_2791")
    return ep, captured, failed


def _evt(engine_config, issue_number):
    return {
        "type": "a2a_event",
        "repository": "o/r2791",
        "source": "a2a",
        "actor": "tester",
        "account": "tester",
        "issue_id": str(issue_number),
        "payload": {
            "repository": "o/r2791",
            "account": "tester",
            "issue_number": str(issue_number),
            "engine_config": engine_config,
        },
    }


def _dispatch(ep, issue_number, evt, seq=88011, scene_id=""):
    """走 A2A 派发入口（dispatch_node → _enqueue_via_pipeline），与线上 A2A 同一条路径。"""
    from src.api.session_store import set_issue_scene
    from src.api.pipeline_executor import _enqueue_via_pipeline
    set_issue_scene(REPO, f"#{issue_number}", scene_id or "default")
    result = _enqueue_via_pipeline(
        REPO, seq, "coding", "test-skill", evt, 0, f"#{issue_number}", "coding")
    return result


class TestDirectedDispatchHit:
    """命中：指定引擎成为实际执行引擎，池选择被跳过。"""

    def test_hit_locks_engine_and_skips_pool_selection(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "plugin")
        ep, captured, failed = _capture_enqueue(monkeypatch, tmp_path)
        cid = _mk_cred("c-2791-hit")
        target = _mk_engine("codearts-2791-hit", credential_id=cid, scope="repo")
        other = _mk_engine("opencode-2791-hit", credential_id=cid, scope="repo")
        num = _mk_issue()
        # 场景池绑定的是**另一台**引擎——定向派发必须在池选择之前锁定 target
        _mk_scene(f"s2791-hit-{num}", "coding", [other])
        _dispatch(ep, num, _evt({"type": "runtime", "runtime_id": target}, num),
                  scene_id=f"s2791-hit-{num}")

        assert not failed, f"定向命中不应派发失败: {failed}"
        assert captured, "定向命中必须入队"
        job = captured[-1]
        assert job["runtime_id"] == target, (
            f"engine_config.runtime_id={target} 必须成为确定性路由，实际 {job['runtime_id']}")
        assert json.loads(job["pool_engine_ids"] or "[]") == [target], (
            "命中后 pool 必须收敛为指定引擎单元素（跳过池选择）")

    def test_hit_does_not_fail_when_scene_pool_all_offline(self, monkeypatch, tmp_path):
        """命中即锁定，不因场景绑定全灭而失败（未被池选择否决）。"""
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "plugin")
        ep, captured, failed = _capture_enqueue(monkeypatch, tmp_path)
        cid = _mk_cred("c-2791-hit2")
        target = _mk_engine("codearts-2791-hit2", credential_id=cid, scope="repo")
        dead = _mk_engine("opencode-2791-dead", credential_id=cid, status="offline", healthy=0,
                          last_seen="2020-01-01T00:00:00+00:00", scope="repo")
        num = _mk_issue()
        _mk_scene(f"s2791-hit2-{num}", "coding", [dead])
        _dispatch(ep, num, _evt({"type": "runtime", "runtime_id": target}, num),
                  scene_id=f"s2791-hit2-{num}")

        assert not failed, f"定向命中不得被场景全灭阻断: {failed}"
        assert captured and captured[-1]["runtime_id"] == target


class TestDirectedDispatchFallback:
    """未命中：回落全局池选择，但必须留结构化 WARN（不静默换引擎）。"""

    def test_unregistered_runtime_id_falls_back_with_structured_warn(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "plugin")
        ep, captured, failed = _capture_enqueue(monkeypatch, tmp_path)
        cid = _mk_cred("c-2791-miss")
        fallback = _mk_engine("opencode-2791-fb", credential_id=cid, scope="repo")
        num = _mk_issue()
        _mk_scene(f"s2791-miss-{num}", "coding", [fallback])
        missing_rid = 987654
        with caplog.at_level("WARNING"):
            _dispatch(ep, num, _evt({"type": "runtime", "runtime_id": missing_rid}, num),
                      scene_id=f"s2791-miss-{num}")

        assert not failed, f"未命中应回落派发而不是失败: {failed}"
        assert captured, "未命中回落必须照常入队（不阻塞流水线）"
        warn = [r for r in caplog.records if "engine_config.runtime_id" in r.getMessage()]
        assert warn, "未命中必须留 WARN，不得静默换引擎"
        body = warn[-1].getMessage()
        assert str(missing_rid) in body, f"WARN 必须带 runtime_id: {body}"
        assert "engine_not_registered" in body, f"WARN 必须带回落原因: {body}"
        assert "selected_engine=" in body, f"WARN 必须带最终选中引擎: {body}"
        assert "opencode-2791-fb" in body, f"WARN 应记录实际回落到的引擎: {body}"

    def test_offline_runtime_id_falls_back_with_reason(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "plugin")
        ep, captured, failed = _capture_enqueue(monkeypatch, tmp_path)
        cid = _mk_cred("c-2791-off")
        offline = _mk_engine("codearts-2791-off", credential_id=cid, status="offline", healthy=0,
                             last_seen="2020-01-01T00:00:00+00:00", scope="repo")
        healthy = _mk_engine("opencode-2791-off", credential_id=cid, scope="repo")
        num = _mk_issue()
        _mk_scene(f"s2791-off-{num}", "coding", [healthy])
        with caplog.at_level("WARNING"):
            _dispatch(ep, num, _evt({"type": "runtime", "runtime_id": offline}, num),
                      scene_id=f"s2791-off-{num}")

        assert not failed
        assert captured
        body = [r.getMessage() for r in caplog.records if "engine_config.runtime_id" in r.getMessage()][-1]
        assert "engine_not_online" in body, body

    def test_no_engine_config_keeps_pool_selection_without_warn(self, monkeypatch, tmp_path, caplog):
        """未配置 engine_config：行为不变、无定向 WARN（避免把老路径变成噪音）。"""
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "plugin")
        ep, captured, failed = _capture_enqueue(monkeypatch, tmp_path)
        cid = _mk_cred("c-2791-none")
        eng = _mk_engine("opencode-2791-none", credential_id=cid, scope="repo")
        num = _mk_issue()
        _mk_scene(f"s2791-none-{num}", "coding", [eng])
        with caplog.at_level("WARNING"):
            _dispatch(ep, num, _evt(None, num), scene_id=f"s2791-none-{num}")

        assert not failed and captured
        assert not [r for r in caplog.records if "engine_config.runtime_id" in r.getMessage()]
        assert json.loads(captured[-1]["pool_engine_ids"] or "[]") == [eng]


class TestLegacyA2ADispatchDirected:
    """legacy A2A 兜底路径（非 plugin / plugin 事件驱动异常回退）同样先认定向引擎。

    评审阻断项：`a2a_dispatch.run_a2a_dispatch` 的 legacy 段先 `pick_engine`，
    返回 None 就 `failed` 返回、场景池门也在定向解析之前——于是「场景池全灭 +
    `engine_config.runtime_id` 指向健康引擎」在该路径仍然派发失败，Spec 的
    「命中即派发、场景池全灭也不否决」不成立。

    测试 seam：公开入口 `run_a2a_dispatch` → `enqueue_task(runtime_id, pool_engine_ids)`，
    与 plugin 路径断言同一层（执行层拿到的确定性路由参数）。
    """

    def _legacy_dispatch(self, monkeypatch, evt, num):
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
        from src.api.session_store import set_issue_scene
        from src.api.a2a_dispatch import run_a2a_dispatch
        set_issue_scene(REPO, f"#{num}", "default")
        return run_a2a_dispatch(
            REPO, 88021, "coding", evt, skill_name="test-skill", skill_id=0,
            skill_path="", runtime_id=0, backend="opencode",
            issue_number=str(num), repository="o/r2791")

    def test_directed_hit_survives_scene_pool_all_offline(self, monkeypatch, tmp_path):
        ep, captured, failed = _capture_enqueue(monkeypatch, tmp_path)
        cid = _mk_cred("c-2791-legacy")
        target = _mk_engine("codearts-2791-lg", credential_id=cid, scope="repo")
        dead = _mk_engine("opencode-2791-lgdead", credential_id=cid, status="offline",
                          healthy=0, last_seen="2020-01-01T00:00:00+00:00", scope="repo")
        num = _mk_issue()
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
        from src.api.session_store import set_issue_scene
        set_issue_scene(REPO, f"#{num}", f"s2791-lg-{num}")
        _mk_scene(f"s2791-lg-{num}", "coding", [dead])

        evt = _evt({"type": "runtime", "runtime_id": target}, num)
        result = self._legacy_dispatch(monkeypatch, evt, num)

        assert result.get("status") != "failed", f"定向命中不得被场景池全灭否决: {result}"
        assert not failed, f"定向命中不应走失败回写: {failed}"
        assert captured, "定向命中必须入队"
        job = captured[-1]
        assert job["runtime_id"] == target, (
            f"legacy A2A 路径必须锁定 engine_config.runtime_id={target}，实际 {job['runtime_id']}")
        assert json.loads(job["pool_engine_ids"] or "[]") == [target], (
            "命中后 pool 必须收敛为指定引擎单元素")

    def test_directed_hit_survives_no_scene_binding(self, monkeypatch, tmp_path):
        """无场景绑定（pick_engine 无池可取 → None）时，定向引擎仍必须派发。"""
        ep, captured, failed = _capture_enqueue(monkeypatch, tmp_path)
        cid = _mk_cred("c-2791-legacy2")
        target = _mk_engine("codearts-2791-lg2", credential_id=cid, scope="repo")
        num = _mk_issue()
        evt = _evt({"type": "runtime", "runtime_id": target}, num)
        result = self._legacy_dispatch(monkeypatch, evt, num)

        assert result.get("status") != "failed", f"定向命中不得因无池而失败: {result}"
        assert captured and captured[-1]["runtime_id"] == target


class TestDirectedTagPrecedence:
    """评审 P4 取舍：定向命中 = 显式指令，**优先于池选择的能力标签过滤**。

    池选择会用 `_filter_by_tags` 把「没有该 agent 标签」的引擎过滤掉；定向派发复用了
    健康口径（`engine_health_ok`，与池选择同一把尺子），但**不**复用标签口径——
    显式指定即视为运维/上游的明确决定。为避免变成静默旁路，命中同时留结构化 WARN
    （`tag_mismatch`），把「池会排除它、但被显式指定」这件事留在日志里。
    """

    def test_tag_mismatch_directed_hit_is_visible_not_silent(self, monkeypatch, tmp_path, caplog):
        ep, captured, failed = _capture_enqueue(monkeypatch, tmp_path)
        cid = _mk_cred("c-2791-tag")
        vod_only = _mk_engine("codearts-2791-vod", credential_id=cid, scope="repo")
        _set_tags(vod_only, ["vod"])
        coding_pool = _mk_engine("opencode-2791-coding", credential_id=cid, scope="repo")
        _set_tags(coding_pool, ["coding", "all"])
        num = _mk_issue()
        _mk_scene(f"s2791-tag-{num}", "coding", [coding_pool])

        with caplog.at_level("WARNING"):
            _dispatch(ep, num, _evt({"type": "runtime", "runtime_id": vod_only}, num),
                      scene_id=f"s2791-tag-{num}")

        assert not failed and captured, "定向命中不得被标签口径否决（显式指定优先）"
        assert captured[-1]["runtime_id"] == vod_only, (
            "定向命中应锁定指定引擎，而不是回落到池里 tag 匹配的引擎")
        body = " ".join(r.getMessage() for r in caplog.records
                        if "engine_config.runtime_id" in r.getMessage())
        assert "no capability tag" in body and "takes precedence" in body, (
            f"标签越权必须留可见 WARN（不静默旁路），实际日志: {body}")

    def test_resolve_reports_tag_mismatch_flag(self, monkeypatch):
        from src.domains.runtime.engine.engine_select import resolve_directive_engine
        cid = _mk_cred("c-2791-tag2")
        rag = _mk_engine("codearts-2791-rag", credential_id=cid, scope="repo")
        _set_tags(rag, ["rag"])
        out = resolve_directive_engine({"type": "runtime", "runtime_id": rag}, REPO, "coding")
        assert out["hit"] is True and out["runtime_id"] == rag
        assert out["tag_mismatch"] is True, f"未命中标签必须显式标记，实际 {out}"


class TestEngineConfigParsing:
    """`engine_config` 形态收敛（A2A 契约：{"type":"runtime","runtime_id":N} | null）。"""

    def test_parse_shapes(self):
        from src.domains.runtime.engine.engine_select import parse_engine_config_runtime_id
        assert parse_engine_config_runtime_id({"type": "runtime", "runtime_id": 17364}) == 17364
        assert parse_engine_config_runtime_id({"runtime_id": 17364}) == 17364
        assert parse_engine_config_runtime_id({"runtime_id": "17364"}) == 17364
        assert parse_engine_config_runtime_id(17364) == 17364
        assert parse_engine_config_runtime_id({"type": "pool"}) == 0
        assert parse_engine_config_runtime_id(None) == 0
        assert parse_engine_config_runtime_id({}) == 0
        assert parse_engine_config_runtime_id({"runtime_id": 0}) == 0
        assert parse_engine_config_runtime_id({"runtime_id": -3}) == 0
        assert parse_engine_config_runtime_id("not-an-id") == 0
