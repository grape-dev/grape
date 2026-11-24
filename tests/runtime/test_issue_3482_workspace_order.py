# -*- coding: utf-8 -*-
"""Issue #3482（A/C）：direct 与 worker 两条执行路径的建树时序 / fail-closed 回归。

根因不是「目录被 .skills 占住」，而是 **direct 路径物化早于建树**（顺序缺陷）：
- direct（direct_executor._execute_agent_inner）此前 `_materialize_direct_workdir`
  （写 .skills/.project-info/.knowledge）先于 `ensure_issue_worktree`；
- worker（agent._execute_task）被写死为「必须先建 worktree 再落地技能」（#711 方案 A）。

顺序不一致 → 目标目录先被物化占住（无 `.git`）→ `git worktree add` 报 already exists。
本用例对两条路径做源码级顺序看护（顺序回归会直接失败），并看护 C 的 fail-closed：
建树失败必须显式 failed，不得 fail-open 跑成「只读 completed」。
"""
import inspect
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine import agent, direct_executor


def test_direct_materialize_runs_after_worktree_create():
    src = inspect.getsource(direct_executor._execute_agent_inner)
    i_wt = src.index("ensure_issue_worktree(")
    i_mat = src.index("_materialize_workdir_and_skill_gate(")
    assert i_wt < i_mat, (
        "Issue #3482（A）：direct 端必须先建 worktree 再物化，"
        "否则目标目录被 .skills 占住 → worktree add 报 already exists")


def test_materialize_gate_moves_skills_and_consistency_together():
    """物化需整体下移，不能只移一半：技能/知识/project-info 物化与 _skill_consistency
    校验必须在同一函数内（校验依赖物化结果）。"""
    src = inspect.getsource(direct_executor._materialize_workdir_and_skill_gate)
    assert "_materialize_direct_workdir" in src
    assert "SKILL_CONSISTENCY_KEY" in src or "_skill_consistency" in src
    assert "prompt writeback" in src


def test_direct_worktree_failure_is_fail_closed():
    """验收 3：建树失败 → 任务 failed（workspace 不可用），不得 fail-open 只读 completed。"""
    src = inspect.getsource(direct_executor._execute_agent_inner)
    assert "_wt_wanted and not _wt" in src, "建树失败必须有显式分支"
    assert "workspace_unavailable" in src, "失败原因需机器可读（completed_reason/result_text）"
    assert "fail_task" in src, "任务需置 failed（含 completed_reason），不是仅 log.warning"


def test_worker_worktree_failure_is_fail_closed():
    """验收 4：direct 与 worker 两条路径同语义——worker 建树失败同样显式失败。"""
    src = inspect.getsource(agent._execute_task_impl)
    assert "_wt_wanted" in src and "workspace_unavailable" in src
    assert '"_started": False' in src
    # worker 侧仍保持「先建 worktree 再落地技能」的既有顺序（#711 方案 A）
    i_wt = src.index("ensure_issue_worktree(")
    i_skill = src.index("skill_dir = _os.path.join(cwd", i_wt)
    assert i_wt < i_skill, "worker 端顺序不得回归为「先技能后 worktree」"


def test_pipeline_exec_agent_delegates_to_direct_executor():
    """Issue #3482 复审（Optional）：direct 执行入口的唯一实现在 direct_executor，
    canonical event_pipeline 不再保留同名/同序副本（旧副本 = 物化早于建树的
    过期顺序在 a2a_dispatch / redispatch 入口复活的根源）。旧 import 路径经
    src.api.event_pipeline 兼容层注入同对象；剩余调用方已改为 direct_executor 直连。"""
    import ast
    import src.api.event_pipeline  # noqa: F401 — 兼容层导入：触发迁移实现注入 canonical
    import src.domains.runtime.engine.direct_executor as de
    import src.domains.runtime.pipeline.event_pipeline as ep
    # 兼容层 re-export 同对象：旧路径取到的就是 direct_executor 的唯一实现
    assert ep._execute_agent is de._execute_agent
    assert ep._execute_agent_inner is de._execute_agent_inner
    assert ep.start_direct_agent is de.start_direct_agent
    # canonical 源码不再定义这三个函数（AST 顶层 defs 无同名副本）
    tree = ast.parse(inspect.getsource(ep))
    top_defs = {n.name for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.col_offset == 0}
    assert not top_defs & {"start_direct_agent", "_execute_agent", "_execute_agent_inner"}
    # 调用方已切换 direct_executor 直连（不再从 canonical 导入迁移实现）
    for path, needle in (
        ("src/domains/runtime/routes/events.py",
         "from src.domains.runtime.engine.direct_executor import _execute_agent, start_direct_agent"),
        ("src/domains/runtime/routes/webhook.py",
         "from src.domains.runtime.engine.direct_executor import _execute_agent"),
        ("src/api/a2a_dispatch.py",
         "from src.domains.runtime.engine.direct_executor import start_direct_agent, _execute_agent"),
    ):
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            src = f.read()
        assert needle in src, f"{path} 应从 direct_executor 导入迁移实现"


class TestSkillGateAgentLogAnnotations:
    """Issue #3482 复审 Required（读侧随写侧下移）：#564/#887 的 agent_logs 标注
    必须在物化**之后**读取——此前读侧先于写侧执行，evt 标注恒为空，direct 会话
    事件里两行标注静默丢失（worker 端 agent.py 是物化后上报，两端口径分叉）。"""

    @staticmethod
    def _run_gate(monkeypatch, evt, logs, verdict_ok=True):
        import src.domains.runtime.pipeline.event_pipeline as ep
        from src.domains.runtime.engine.skill_materialize import SKILL_CONSISTENCY_KEY

        def _fake_materialize(ws, repo_key, agent_type, skill_name, evt, issue_id,
                              prompt, engine_name=""):
            # 物化副作用：#564/#887 标注只在物化发生时写入 evt（真实链路语义）
            evt["_skill_materialize"] = {
                "status": "fallback", "detail": "checker fallback via mock",
                "source": {"skill_id": 7, "scope": "repo"}, "drift": {"extra": ["x.md"]},
            }
            evt[SKILL_CONSISTENCY_KEY] = {
                "bound": "coding", "actual": "coding", "source": "repo",
                "ok": verdict_ok, "reason": "" if verdict_ok else "bound skill not materialized",
            }
            return prompt + "-materialized"

        monkeypatch.setattr(ep, "_materialize_direct_workdir", _fake_materialize)
        fails = []
        monkeypatch.setattr(ep, "_fail_event", lambda *a, **k: fails.append((a, k)))
        prompt, ok = direct_executor._materialize_workdir_and_skill_gate(
            ws="/tmp/ws-3482-gate", repo_key="gitcode:owner/repo", agent_type="coding",
            skill_name="coding", evt=evt, issue_id="#3482", prompt="p",
            engine_name="oc-test", seq=99, task_id="", agent_logs=logs)
        return prompt, ok, fails

    def test_fallback_and_source_annotations_after_materialize(self, monkeypatch):
        """验收：物化 fallback + 来源正常时，direct 任务事件 agent_logs 含
        `skill_materialize` / `skill_source` 两行（读侧在物化之后才有值）。"""
        logs = []
        prompt, ok, fails = self._run_gate(monkeypatch, {}, logs, verdict_ok=True)
        assert ok is True and not fails
        assert prompt.endswith("-materialized")
        types = [entry["type"] for entry in logs]
        assert "skill_materialize" in types, f"缺 #564 降级标注: {logs}"
        assert "skill_source" in types, f"缺 #887 来源标注: {logs}"
        mat = next(e for e in logs if e["type"] == "skill_materialize")
        assert "fallback" in mat["msg"] and "checker fallback via mock" in mat["msg"]
        assert mat.get("time")
        src_line = next(e for e in logs if e["type"] == "skill_source")
        assert "bound=coding" in src_line["msg"] and "actual=coding" in src_line["msg"]
        assert src_line.get("time")

    def test_no_annotation_without_materialize_status(self, monkeypatch):
        """物化正常（无 fallback/degraded）→ 只落 #887 来源行，不静默空注入 #564。"""
        import src.domains.runtime.pipeline.event_pipeline as ep
        from src.domains.runtime.engine.skill_materialize import SKILL_CONSISTENCY_KEY

        def _fake_ok(ws, repo_key, agent_type, skill_name, evt, issue_id, prompt, engine_name=""):
            evt["_skill_materialize"] = {"status": "ok", "detail": ""}
            evt[SKILL_CONSISTENCY_KEY] = {"bound": "coding", "actual": "coding",
                                          "source": "repo", "ok": True, "reason": ""}
            return prompt

        monkeypatch.setattr(ep, "_materialize_direct_workdir", _fake_ok)
        monkeypatch.setattr(ep, "_fail_event", lambda *a, **k: None)
        logs = []
        prompt, ok = direct_executor._materialize_workdir_and_skill_gate(
            ws="/tmp/ws-3482-gate", repo_key="gitcode:owner/repo", agent_type="coding",
            skill_name="coding", evt={}, issue_id="#3482", prompt="p",
            engine_name="oc-test", seq=99, task_id="", agent_logs=logs)
        assert ok is True
        assert [e["type"] for e in logs] == ["skill_source"]

    def test_refusal_still_annotates_skill_source(self, monkeypatch):
        """#887 判定 ok=False 拒绝执行时：来源标注仍先落入 agent_logs（审计留痕），
        事件置 failed（skill_mismatch），与 worker 端「先上报再拒绝」同口径。"""
        logs = []
        prompt, ok, fails = self._run_gate(monkeypatch, {}, logs, verdict_ok=False)
        assert ok is False
        assert fails, "拒绝执行必须置事件 failed"
        diag = (fails[0][1] or {}).get("diag") or {}
        assert diag.get("code") == "skill_mismatch"
        assert "skill_source" in [e["type"] for e in logs]


class TestFailClosedBehavior:
    """Issue #3482 复审（Optional，验收 4 行为级）：direct 与 worker 两条路径对
    「ensure_issue_worktree 返回 None」同为显式失败，不 fail-open 只读 completed。"""

    def _patch_direct_env(self, monkeypatch, tmp_path, fails, tasks):
        import src.domains.runtime.pipeline.event_pipeline as ep
        import src.domains.runtime.engine.service as svc
        import src.domains.runtime.engine.repogit as repogit
        import src.domains.runtime.engine.instances as instances
        import src.kernel.prompt_builder as pb
        import src.api.session_store as session_store
        # 复审建议的注入点：建树失败（返回 None）
        monkeypatch.setattr(repogit, "ensure_issue_worktree", lambda *a, **k: None)
        monkeypatch.setattr(ep, "_fail_event",
                            lambda seq, reason, exit_code=1, diag=None: fails.append(
                                (seq, reason, diag)))
        monkeypatch.setattr(svc, "fail_task",
                            lambda tid, msg, exit_code=1: tasks.append((tid, msg, exit_code)))
        monkeypatch.setattr(svc, "update_task_status", lambda *a, **k: None)
        monkeypatch.setattr(instances, "runtime_instance_name", lambda rid: "oc-test")
        monkeypatch.setattr(ep, "get_agent_workdir", lambda *a, **k: str(tmp_path))
        monkeypatch.setattr(pb, "build_devops_agent_prompt", lambda *a, **k: "p")
        monkeypatch.setattr(pb, "resolve_task_server_env", lambda rid: {})
        monkeypatch.setattr(ep, "resolve_actor_account", lambda evt, rk: "tester")
        monkeypatch.setattr(ep, "_inject_scene_env", lambda *a, **k: None)
        monkeypatch.setattr(ep, "_inject_issue_pr_env", lambda *a, **k: None)
        monkeypatch.setattr(ep, "_inject_bot_agent_env", lambda *a, **k: None)
        monkeypatch.setattr(session_store, "get_or_create_issue_session",
                            lambda rk, at, iid: {"id": "sess-3482"})
        monkeypatch.setattr(session_store, "guard_issue_session_pointer",
                            lambda s, backend: "oc-1")

    def test_direct_fail_closed_on_worktree_failure(self, monkeypatch, tmp_path):
        """direct：断言 fail_task + 事件 failed（diag.code=workspace_unavailable）。"""
        fails, tasks = [], []
        self._patch_direct_env(monkeypatch, tmp_path, fails, tasks)
        evt = {"repo_key": "gitcode:owner/repo", "repository": "owner/repo",
                "issue_id": "#3482", "type": "a2a_event", "payload": {}}
        ret = direct_executor._execute_agent_inner(
            "gitcode:owner/repo", 777, "coding", "opencode", "coding", evt, 0, "t-3482")
        assert ret is None, "建树失败必须显式返回，不得继续执行"
        assert tasks, "任务需 fail_task 置 failed（completed_reason 写明 workspace 不可用）"
        assert "workspace_unavailable" in tasks[0][1]
        assert fails, "事件需置 failed（_fail_event）"
        seq, reason, diag = fails[0]
        assert seq == 777
        assert "workspace_unavailable" in reason
        assert (diag or {}).get("code") == "workspace_unavailable"

    def test_worker_fail_closed_on_worktree_failure(self, monkeypatch, tmp_path):
        """worker：断言 `_error`（含 workspace_unavailable）+ `_started=False`。"""
        import asyncio
        import src.domains.runtime.engine.repogit as repogit
        import src.domains.runtime.engine.usage_collect as usage_collect
        import src.domains.runtime.engine.dependency_check as dependency_check
        import src.domains.runtime.engine.session_tmpdir as session_tmpdir
        monkeypatch.setattr(repogit, "ensure_issue_worktree", lambda *a, **k: None)
        monkeypatch.setattr(usage_collect, "UsageCollector",
                            lambda *a, **k: type("UC", (), {"start": lambda s: None})())
        monkeypatch.setattr(dependency_check, "validate_task_dependencies", lambda *a, **k: None)
        monkeypatch.setattr(session_tmpdir, "ensure_session_tmpdir", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_worker_server_url_reachable", lambda *a, **k: False)
        # rebase note: base-side #3713 credential attribution takes precedence over the
        # #3482 fallback; credentials unblocked here so this case pins the fail-closed path.
        monkeypatch.setattr(agent, "credential_block_result", lambda *a, **k: None)

        class _StubClient:
            async def get_task_env(self, tid):
                return {}

        (tmp_path / "cwd").mkdir()
        task = {"id": "t-3482-w", "cwd": str(tmp_path / "cwd"), "agent_type": "coding",
                "repo_key": "gitcode:owner/repo", "issue_id": "#3482",
                "issue_session_id": "sess-3482", "skill": "coding", "env_extra": "{}",
                "server_url": "http://127.0.0.1:3000"}
        result = asyncio.run(agent._execute_task_impl(_StubClient(), task, "oc-worker"))
        assert result.get("_started") is False
        assert "workspace_unavailable" in (result.get("_error") or "")


class TestDirectLastNodePendingReviewHook:
    """Issue #3482 第四轮 Required（收尾钩子随迁）：event_pipeline 旧副本删除后，
    「末节点完成 → finalize_issue_to_pending_review(source="exec")」人工闸随
    direct 入口唯一实现搬进 direct_executor 收尾清单——与 worker 端
    complete_task() 的 pending_review 钩子同口径，a2a_dispatch / events(redispatch) /
    webhook 三入口的末节点待人工确认不再断链。"""

    def test_direct_completed_triggers_last_node_finalize(self, monkeypatch, tmp_path):
        """direct 末节点 completed → 收尾必须调用 finalize_issue_to_pending_review，
        且 source="exec"（幂等语义与旧副本一致），非 completed 收尾不触发。"""
        import src.domains.runtime.pipeline.event_pipeline as ep
        import src.domains.runtime.engine.service as svc
        import src.domains.runtime.engine.executor as executor_mod
        import src.api.event_store as event_store
        import src.api.session_store as session_store
        import src.domains.insight.public as insight_public
        import src.domains.runtime.issues_center as issues_center
        import src.domains.runtime.engine.instances as instances
        import src.kernel.prompt_builder as pb

        fails = []
        monkeypatch.setattr(ep, "_fail_event",
                            lambda seq, reason, exit_code=1, diag=None: fails.append(reason))
        monkeypatch.setattr(ep, "get_agent_workdir", lambda *a, **k: str(tmp_path))
        monkeypatch.setattr(ep, "resolve_actor_account", lambda evt, rk: "tester")
        monkeypatch.setattr(ep, "get_agent_display_name", lambda at: at)
        monkeypatch.setattr(ep, "_inject_scene_env", lambda *a, **k: None)
        monkeypatch.setattr(ep, "_inject_issue_pr_env", lambda *a, **k: None)
        monkeypatch.setattr(ep, "_inject_bot_agent_env", lambda *a, **k: None)
        monkeypatch.setattr(ep, "_read_scene_linked_repos", lambda *a, **k: "")
        monkeypatch.setattr(ep, "_post_result_to_issue", lambda *a, **k: None)
        monkeypatch.setattr(ep, "_sync_issue_comments_to_conversation", lambda *a, **k: None)
        monkeypatch.setattr(direct_executor, "_materialize_workdir_and_skill_gate",
                            lambda **kw: (kw.get("prompt", ""), True))
        monkeypatch.setattr(session_store, "get_or_create_issue_session",
                            lambda rk, at, iid: {"id": "sess-3482"})
        monkeypatch.setattr(session_store, "guard_issue_session_pointer",
                            lambda s, backend: "")
        monkeypatch.setattr(session_store, "is_subtask_mode_enabled",
                            lambda rk, iid: False)
        monkeypatch.setattr(event_store, "evaluate_agent_output",
                            lambda at, tools, text: (True, text or "done"))
        monkeypatch.setattr(event_store, "update_event_config", lambda *a, **k: None)
        monkeypatch.setattr(event_store, "update_event_complete", lambda *a, **k: None)
        monkeypatch.setattr(svc, "update_task_status", lambda *a, **k: None)
        monkeypatch.setattr(svc, "set_task_signal", lambda *a, **k: None)
        monkeypatch.setattr(pb, "build_devops_agent_prompt", lambda *a, **k: "p")
        monkeypatch.setattr(pb, "resolve_task_server_env", lambda rid: {})
        monkeypatch.setattr(instances, "runtime_instance_name", lambda rid: "oc-test")

        class _StubUsage:
            def start(self):
                pass

            def feed(self, *a, **k):
                pass

            def finish(self, *a, **k):
                pass

        monkeypatch.setattr(insight_public, "master_usage_for_task",
                            lambda *a, **k: _StubUsage())

        def _fake_stream(*a, **k):
            yield ("done", {"exit_code": 0, "text": "task done", "oc_session_id": "",
                            "errors": [], "stderr": ""})

        monkeypatch.setattr(executor_mod, "stream_devops_agent", _fake_stream)

        finals = []

        def _fake_finalize(rk, iid, at, source="pipeline"):
            finals.append((rk, iid, at, source))
            return "pending_review"

        monkeypatch.setattr(issues_center, "finalize_issue_to_pending_review", _fake_finalize)

        evt = {"repo_key": "gitcode:owner/repo", "repository": "owner/repo",
               "issue_id": "#3482", "type": "a2a_event", "payload": {}}
        direct_executor._execute_agent_inner(
            "gitcode:owner/repo", 778, "issue", "opencode", "issue", evt, 0, "t-3482")
        assert not fails, f"unexpected fail_event during completed run: {fails}"
        assert finals == [("gitcode:owner/repo", "#3482", "issue", "exec")], (
            "direct 末节点完成必须触发 finalize_issue_to_pending_review(source=\"exec\") "
            "人工闸（与 worker complete_task() 同口径），不得随旧副本删除而断链")