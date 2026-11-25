"""Issue #4440：Coding 交付核实（统一入口 + 双收尾路径）回归测试。

缺陷背景：Coding（atomcode-bug-fix 工作流）以 ACP end_turn 提前结束后仍被记为
成功，无关联 PR 也放行进下游（QA/自动流转）。修复后：

- 统一入口 ``delivery_verify.verify_coding_delivery``（无 PR / 查询失败 fail-closed，
  有 PR 放行；仅对核实清单内 skill 生效）；
- 远程 Worker 收尾 ``service.complete_task`` 与 direct 收尾
  ``direct_executor._execute_agent_inner`` 共用同一入口；
- 未通过 → 状态改记「Coding 未交付」（failed + 结构化错误码 + 原因），
  成功路径自动推进不触发；
- 其他 Coding 工作流（skill 未命中清单）行为零改动。
"""
import inspect
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_i4440_delivery.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest

SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.kernel.session_manager import db, db_lock
from src.api.db_setup import init_database

init_database()

REPO = "gitcode:i4440/dev-repo"
VERIFY_SKILL = "atomcode-bug-fix"


# ===== 工具：假 VCS client（三种场景：无 PR / 查询失败 / 有 PR） =====

class _StubVcsClient:
    def __init__(self, prs=None, error=None, boom=False):
        self._prs = prs if prs is not None else []
        self._error = error
        self._boom = boom

    def list_prs(self, state="all", **kw):
        if self._boom:
            raise RuntimeError("network unreachable")
        if self._error:
            return {"error": self._error}
        return list(self._prs)


def _pr(num, body=""):
    return {"number": num, "title": f"pr #{num}", "body": body or "fixes #4440"}


@pytest.fixture(autouse=True)
def _env_default(monkeypatch):
    monkeypatch.delenv("GRAPE_DELIVERY_VERIFY_SKILLS", raising=False)


@pytest.fixture()
def _vcs(monkeypatch):
    """替换 issues_center._pr_vcs_client；返回计数器 dict（断言外呼次数）。"""
    import src.domains.runtime.issues_center as issues_center
    state = {"n": 0}

    def _factory(repo_key, prs=None, error=None, boom=False, none=False):
        def _client(rk):
            state["n"] += 1
            return None if none else _StubVcsClient(prs, error, boom)
        monkeypatch.setattr(issues_center, "_pr_vcs_client", _client)
        return state
    return _factory


# ===== 统一入口：交付核实单元测试 =====

class TestVerifyCodingDelivery:
    def _verify(self, issue_id="#4440", skill=VERIFY_SKILL, agent_type="coding"):
        from src.domains.runtime.engine.delivery_verify import verify_coding_delivery
        return verify_coding_delivery(REPO, issue_id, agent_type, skill)

    def test_not_applies_for_other_agent(self):
        r = self._verify(agent_type="qa")
        assert r["applies"] is False and r["delivered"] is True

    def test_not_applies_for_other_skill(self):
        r = self._verify(skill="coding-cli-1")
        assert r["applies"] is False and r["delivered"] is True

    def test_skill_match_case_insensitive(self):
        assert self._verify(skill="AtomCode-Bug-Fix")["applies"] is True

    def test_env_override_skill_list(self, monkeypatch):
        monkeypatch.setenv("GRAPE_DELIVERY_VERIFY_SKILLS", "my-flow,other-flow")
        from src.domains.runtime.engine.delivery_verify import (
            delivery_verify_skills, delivery_verify_applies)
        assert delivery_verify_skills() == ("my-flow", "other-flow")
        assert delivery_verify_applies("coding", "my-flow") is True
        assert delivery_verify_applies("coding", VERIFY_SKILL) is False

    def test_no_issue_number(self, _vcs):
        calls = _vcs(REPO)
        r = self._verify(issue_id="")
        assert r["applies"] is True and r["delivered"] is False
        assert r["code"] == "no_issue_number"
        assert calls["n"] == 0, "无需求编号不得外呼查询"

    def test_pr_subscription_pseudo_issue_id_skips(self, _vcs):
        calls = _vcs(REPO)
        r = self._verify(issue_id="#pr_123_scene")
        assert r["applies"] is False and r["delivered"] is True, "PR 订阅不是新交付目标，双路径同口径跳过"
        assert calls["n"] == 0, "PR 订阅不得外呼查询"

    def test_no_linked_pr(self, _vcs):
        _vcs(REPO, prs=[])
        r = self._verify()
        assert r["delivered"] is False and r["code"] == "no_linked_pr"
        assert "无关联 PR" in r["reason"]

    def test_query_error_dict(self, _vcs):
        _vcs(REPO, error="403 forbidden")
        r = self._verify()
        assert r["delivered"] is False and r["code"] == "pr_query_failed"
        assert "403" in r["reason"]

    def test_query_exception(self, _vcs):
        _vcs(REPO, boom=True)
        r = self._verify()
        assert r["delivered"] is False and r["code"] == "pr_query_failed"

    def test_no_bot_token_is_query_failure(self, _vcs):
        _vcs(REPO, none=True)
        r = self._verify()
        assert r["delivered"] is False and r["code"] == "pr_query_failed"
        assert "bot token" in r["reason"]

    def test_delivered_with_linked_pr(self, _vcs):
        _vcs(REPO, prs=[_pr(101, "fixes #4440"), _pr(102, "unrelated")])
        r = self._verify()
        assert r["delivered"] is True and r["prs"] == [101]

    def test_pr_related_via_inner_issue_nums(self, _vcs):
        _vcs(REPO, prs=[{"number": 9, "issues": [{"number": 4440}]}])
        r = self._verify()
        assert r["delivered"] is True and r["prs"] == [9]


# ===== 远程 Worker 收尾路径：service.complete_task =====

WSEQ = 94440
WTASK = "t_i4440_worker"


def _seed_worker_task(skill=VERIFY_SKILL, issue_id="#4440", agent_type="coding"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (WTASK,))
        c.execute("DELETE FROM events WHERE seq=?", (WSEQ,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, "
            "agent_type, event_type, status, prompt, cwd, skill, event_seq, started_at) "
            "VALUES (?, 'sess-4440', 0, ?, ?, ?, 'a2a_event', 'running', 'p', '/tmp', ?, ?, ?)",
            (WTASK, REPO, issue_id, agent_type, skill, WSEQ,
             "2026-11-25T16:00:00+00:00"))
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status, "
            "summary, result) VALUES (?,?,?,?,?, 'running', 'A2A: → coding', '')",
            (WSEQ, REPO, issue_id, "a2a_event", "coding"))
        db.commit()


def _task_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, exit_code, result_text, errors FROM agent_tasks WHERE id=?",
                  (WTASK,))
        row = c.fetchone()
    return row


def _event_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, exit_code, result FROM events WHERE seq=?", (WSEQ,))
        row = c.fetchone()
    return row


@pytest.fixture()
def _worker_hooks(monkeypatch):
    """屏蔽 complete_task 的外呼/推进副作用，记录自动推进调用。"""
    import src.domains.runtime.engine.service as svc
    import src.api.event_archive as event_archive
    import src.api.issues_center as api_issues_center
    import src.domains.runtime.pipeline.event_pipeline as ep
    import src.domains.mgmt.vcs as mgmt_vcs
    import src.domains.runtime.engine.backend_health as backend_health
    advances = []
    notes = []
    monkeypatch.setattr(svc, "_schedule_auto_advance_after_complete",
                        lambda *a, **k: advances.append(a))
    monkeypatch.setattr(backend_health, "note_task_success",
                        lambda rid: notes.append(rid))
    monkeypatch.setattr(event_archive, "finalize_event_archive", lambda *a, **k: None)
    monkeypatch.setattr(api_issues_center, "finalize_issue_to_pending_review",
                        lambda *a, **k: "noop:not_last_node")
    monkeypatch.setattr(ep, "_post_result_to_issue", lambda *a, **k: None)
    monkeypatch.setattr(ep, "_sync_issue_comments_to_conversation", lambda *a, **k: None)
    monkeypatch.setattr(ep, "_post_subtask_completion_to_parent", lambda *a, **k: None)
    monkeypatch.setattr(ep, "_post_all_subtasks_done", lambda *a, **k: None)
    monkeypatch.setattr(mgmt_vcs, "create_provider_from_repo_key", lambda *a, **k: None)
    return {"advances": advances, "notes": notes}


def _complete(skill=VERIFY_SKILL, issue_id="#4440", agent_type="coding", exit_code=0):
    from src.domains.runtime.engine.service import complete_task
    _seed_worker_task(skill, issue_id, agent_type)
    complete_task(WTASK, json.dumps({"actions": ["x"], "conclusion": "done"}),
                  exit_code=exit_code)


class TestWorkerCompleteTaskPath:
    def test_no_pr_marks_not_delivered_and_blocks_advance(self, _vcs, _worker_hooks):
        _vcs(REPO, prs=[])
        _complete()
        row = _task_row()
        assert row[0] == "failed", "无关联 PR 不得记录为成功"
        assert row[1] == 1
        errs = json.loads(row[3] or "[]")
        assert any(isinstance(e, dict) and e.get("error_code") == "CODING_DELIVERY_UNVERIFIED"
                   for e in errs), f"结构化错误码缺失: {errs}"
        ev = _event_row()
        assert ev[0] == "failed"
        assert "Coding 未交付" in (ev[2] or "")
        assert not _worker_hooks["advances"], "未交付不得触发成功路径自动推进"
        assert not _worker_hooks["notes"], "未交付不得释放后端成功态（副作用须在核实后）"

    def test_query_failure_marks_not_delivered(self, _vcs, _worker_hooks):
        _vcs(REPO, error="500 server error")
        _complete()
        assert _task_row()[0] == "failed", "查询失败不静默记成功"
        assert "Coding 未交付" in (_event_row()[2] or "")
        assert not _worker_hooks["advances"]
        assert not _worker_hooks["notes"]

    def test_delivered_with_pr_records_success(self, _vcs, _worker_hooks):
        _vcs(REPO, prs=[_pr(101, "fixes #4440")])
        _complete()
        row = _task_row()
        assert row[0] == "completed" and row[1] == 0
        assert _event_row()[0] == "completed"
        assert len(_worker_hooks["advances"]) == 1, "核实通过才推进下游"
        assert _worker_hooks["notes"], "核实通过才释放后端成功态"

    def test_other_skill_unchanged_no_verify(self, _vcs, _worker_hooks):
        calls = _vcs(REPO, prs=[])
        _complete(skill="coding-cli-1")
        assert _task_row()[0] == "completed", "非核实工作流行为不变"
        assert calls["n"] == 0, "未命中清单不得外呼查询"
        assert len(_worker_hooks["advances"]) == 1

    def test_no_issue_id_skips_gate(self, _vcs, _worker_hooks):
        calls = _vcs(REPO, prs=[])
        _complete(issue_id="")
        assert _task_row()[0] == "completed"
        assert calls["n"] == 0

    def test_non_coding_agent_skips_gate(self, _vcs, _worker_hooks):
        calls = _vcs(REPO, prs=[])
        _complete(agent_type="qa", skill=VERIFY_SKILL)
        assert _task_row()[0] == "completed"
        assert calls["n"] == 0

    def test_structured_output_revival_requires_delivery(self, _vcs, _worker_hooks):
        """-15 + actions/conclusion 的「复活为 completed」通道同样要经过核实。"""
        _vcs(REPO, prs=[])
        _complete(exit_code=-15)
        row = _task_row()
        assert row[0] == "failed", "非零退出码 + 自述不是交付证据，不得复活为成功"
        assert row[1] == 1
        assert "Coding 未交付" in (_event_row()[2] or "")
        assert not _worker_hooks["advances"]
        assert not _worker_hooks["notes"]

    def test_structured_output_revival_with_pr_succeeds(self, _vcs, _worker_hooks):
        _vcs(REPO, prs=[_pr(101, "fixes #4440")])
        _complete(exit_code=-15)
        row = _task_row()
        assert row[0] == "completed" and row[1] == 0
        assert _event_row()[0] == "completed"

    def test_pr_subscription_skips_gate_symmetry(self, _vcs, _worker_hooks):
        """worker 对 PR 订阅伪 issue_id 与 direct 同口径跳过（不误判未交付）。"""
        calls = _vcs(REPO, prs=[])
        _complete(issue_id="#pr_123_scene")
        assert _task_row()[0] == "completed", "PR 订阅不是新交付目标"
        assert calls["n"] == 0, "PR 订阅不得外呼查询"
        assert len(_worker_hooks["advances"]) == 1


class TestTransientDegradeDeliveryGate:
    """transient_retry 降级为 completed 也是成功通道，须先过交付核实。"""

    def _degrade(self):
        from src.domains.runtime.engine.transient_retry import retry_transient_task
        _seed_worker_task()
        return retry_transient_task(
            WTASK, -1, "connection reset by peer", has_output=True,
            tool_uses=[{"cmd": "grape git push origin feat/issue-4440"}],
            result_text="代码已推送")

    def test_transient_degrade_blocked_without_pr(self, _vcs):
        _vcs(REPO, prs=[])
        r = self._degrade()
        assert r is False, "交付核实未通过不得降级为 completed"
        row = _task_row()
        assert row[0] == "running", "保持 running，交由调用方判死写 failed"
        errs = json.loads(row[3] or "[]")
        assert any(isinstance(e, dict) and e.get("error_code") == "CODING_DELIVERY_UNVERIFIED"
                   for e in errs), f"结构化错误码缺失: {errs}"

    def test_transient_degrade_allowed_with_pr(self, _vcs):
        _vcs(REPO, prs=[_pr(101, "fixes #4440")])
        r = self._degrade()
        assert r is True
        assert _task_row()[0] == "completed", "有可核实 PR 才允许降级完成"


# ===== direct 收尾路径：direct_executor._execute_agent_inner =====

DTASK = "t_i4440_direct"
DSEQ = 94441


@pytest.fixture()
def _direct_env(monkeypatch, tmp_path):
    import src.domains.runtime.pipeline.event_pipeline as ep
    import src.domains.runtime.engine.service as svc
    import src.domains.runtime.engine.executor as executor_mod
    import src.api.event_store as event_store
    import src.api.session_store as session_store
    import src.domains.runtime.engine.instances as instances
    import src.kernel.prompt_builder as pb
    import src.domains.runtime.issues_center as issues_center
    import src.api.issues_center as api_issues_center
    import src.domains.insight.public as insight_public
    import src.domains.runtime.engine.transient_retry as transient_retry
    import src.domains.runtime.engine.repogit as repogit

    fails = []
    # coding 是 git agent：direct 收尾链路会先建 worktree（不 mock 会真连远端）。
    monkeypatch.setattr(repogit, "ensure_issue_worktree",
                        lambda *a, **k: str(tmp_path / "ws"))
    statuses = []
    events_done = []
    advances = []
    recorded_errors = []
    idles = []

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
    monkeypatch.setattr(ep, "_post_result_to_pr", lambda *a, **k: None)
    monkeypatch.setattr(ep, "_sync_issue_comments_to_conversation", lambda *a, **k: None)
    monkeypatch.setattr(direct_executor_mod(), "_materialize_workdir_and_skill_gate",
                        lambda **kw: (kw.get("prompt", ""), True))
    monkeypatch.setattr(session_store, "get_or_create_issue_session",
                        lambda rk, at, iid, pr="": {"id": "sess-4440"})
    monkeypatch.setattr(session_store, "guard_issue_session_pointer", lambda s, backend: "")
    monkeypatch.setattr(session_store, "is_subtask_mode_enabled", lambda rk, iid: False)
    monkeypatch.setattr(event_store, "evaluate_agent_output",
                        lambda at, tools, text: (True, text or "done"))
    monkeypatch.setattr(event_store, "update_event_config", lambda *a, **k: None)

    def _evt_complete(seq, status, exit_code=0, result="", **kw):
        events_done.append({"seq": seq, "status": status, "exit_code": exit_code,
                            "result": result, "kw": kw})
    monkeypatch.setattr(event_store, "update_event_complete", _evt_complete)

    def _upd_status(tid, status, **kw):
        statuses.append((tid, status, kw))
    monkeypatch.setattr(svc, "update_task_status", _upd_status)
    monkeypatch.setattr(svc, "set_task_signal", lambda *a, **k: None)
    monkeypatch.setattr(svc, "get_task", lambda tid: {})
    monkeypatch.setattr(svc, "run_auto_advance_hook",
                        lambda *a, **k: (advances.append(a) or {"action": ""}))
    monkeypatch.setattr(pb, "build_devops_agent_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(pb, "resolve_task_server_env", lambda rid: {})
    monkeypatch.setattr(instances, "runtime_instance_name", lambda rid: "oc-test")
    monkeypatch.setattr(issues_center, "finalize_issue_to_pending_review", lambda *a, **k: None)
    monkeypatch.setattr(api_issues_center, "finalize_issue_to_pending_review",
                        lambda *a, **k: "noop:not_last_node")

    def _append_err(tid, entry):
        recorded_errors.append((tid, entry))
    monkeypatch.setattr(transient_retry, "_append_task_error", _append_err)
    monkeypatch.setattr(direct_executor_mod(), "_mark_session_idle",
                        lambda rk: idles.append(rk))

    class _StubUsage:
        def start(self):
            pass

        def feed(self, *a, **k):
            pass

        def finish(self, *a, **k):
            pass

    monkeypatch.setattr(insight_public, "master_usage_for_task", lambda *a, **k: _StubUsage())

    def _fake_stream(*a, **k):
        yield ("done", {"exit_code": 0, "text": "task done", "oc_session_id": "",
                        "errors": [], "stderr": ""})

    monkeypatch.setattr(executor_mod, "stream_devops_agent", _fake_stream)

    return {"fails": fails, "statuses": statuses, "events": events_done,
            "advances": advances, "errors": recorded_errors, "idles": idles}


def direct_executor_mod():
    import src.domains.runtime.engine.direct_executor as de
    return de


def _run_direct(skill=VERIFY_SKILL, issue_id="#4440"):
    de = direct_executor_mod()
    evt = {"repo_key": REPO, "repository": "i4440/dev-repo", "issue_id": issue_id,
           "type": "a2a_event", "payload": {}}
    de._execute_agent_inner(REPO, DSEQ, "coding", "opencode", skill, evt, 0, DTASK)


class TestDirectExecPath:
    def test_no_pr_marks_not_delivered(self, _vcs, _direct_env):
        _vcs(REPO, prs=[])
        _run_direct()
        assert not _direct_env["fails"]
        assert _direct_env["statuses"], "必须写入终态"
        tid, status, kw = _direct_env["statuses"][-1]
        assert status == "failed", "direct 无关联 PR 不得记成功"
        assert kw.get("exit_code") == 1, "direct 交付失败须与 worker 同口径 exit_code=1"
        assert any(e[0] == DTASK and e[1].get("error_code") == "CODING_DELIVERY_UNVERIFIED"
                   for e in _direct_env["errors"]), "结构化错误码必须落档"
        ev = _direct_env["events"][-1]
        assert ev["status"] == "failed"
        assert ev["exit_code"] == 1
        assert "Coding 未交付" in (ev["result"] or "")
        assert not _direct_env["advances"], "direct 未交付不得触发自动推进"
        assert not _direct_env["idles"], "未交付不得重置会话成功态（副作用须在核实后）"

    def test_query_failure_marks_not_delivered(self, _vcs, _direct_env):
        _vcs(REPO, error="403 forbidden")
        _run_direct()
        assert _direct_env["statuses"][-1][1] == "failed"
        assert "Coding 未交付" in (_direct_env["events"][-1]["result"] or "")
        assert not _direct_env["advances"]

    def test_delivered_with_pr_records_success(self, _vcs, _direct_env):
        _vcs(REPO, prs=[_pr(201, "fixes #4440")])
        _run_direct()
        assert _direct_env["statuses"][-1][1] == "completed"
        assert _direct_env["statuses"][-1][2].get("exit_code") == 0
        assert _direct_env["events"][-1]["status"] == "completed"
        assert _direct_env["advances"], "核实通过才推进（auto-advance 钩子执行）"
        assert _direct_env["idles"], "核实通过才重置会话成功态"

    def test_other_skill_unchanged(self, _vcs, _direct_env):
        calls = _vcs(REPO, prs=[])
        _run_direct(skill="coding-cli-1")
        assert _direct_env["statuses"][-1][1] == "completed", "非核实工作流行为不变"
        assert calls["n"] == 0
        assert _direct_env["advances"]

    def test_pr_subscription_skips_gate_symmetry(self, _vcs, _direct_env):
        calls = _vcs(REPO, prs=[])
        _run_direct(issue_id="#pr_123_scene")
        assert _direct_env["statuses"][-1][1] == "completed", "PR 订阅不是新交付目标"
        assert calls["n"] == 0, "PR 订阅不得外呼查询"


# ===== 双路径共用同一入口（源级守卫） =====

class TestSharedEntrySourceGuard:
    def test_both_paths_call_same_entry(self):
        """两条收尾路径必须调用同一 verify_coding_delivery（禁止两套逻辑分叉）。"""
        import src.domains.runtime.engine.service as svc
        import src.domains.runtime.engine.direct_executor as de
        svc_src = inspect.getsource(svc.complete_task)
        de_src = inspect.getsource(de._execute_agent_inner)
        assert "verify_coding_delivery" in svc_src, "worker 收尾必须调用统一核实入口"
        assert "verify_coding_delivery" in de_src, "direct 收尾必须调用统一核实入口"

    def test_worker_gate_before_auto_advance(self):
        """核实必须在成功判定/自动推进之前（exit_code 覆盖先于 completed 分支）。"""
        import src.domains.runtime.engine.service as svc
        src = inspect.getsource(svc.complete_task)
        assert src.index("verify_coding_delivery") < src.index(
            "_schedule_auto_advance_after_complete"), "核实入口必须在自动推进之前"

    def test_direct_gate_before_terminal_write(self):
        import src.domains.runtime.engine.direct_executor as de
        src = inspect.getsource(de._execute_agent_inner)
        assert src.index("verify_coding_delivery") < src.index(
            "update_task_status(task_id, new_status"), "direct 核实必须在终态写入之前"
