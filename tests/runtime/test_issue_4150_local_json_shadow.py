"""Issue #4150 回归：`_execute_agent_inner` 内局部 `import json` 遮蔽模块级 json。

Python 编译期把「函数体内出现 `import json`」判定为函数局部名，于是该 import
**之前**的所有 `json.xxx` 必抛 `UnboundLocalError`。direct_executor.py 因此在
两处静默失效：

1. `GRAPE_LINKED_REPOS` 注入（被外层 `except Exception: pass` 吞掉）→ direct 路径
   永不注入关联仓列表，#466 的关联仓 token 分发一并被跳过，且无任何日志；
2. `_fail_errors_json = json.dumps(_merged)`（仅留 WARNING）→ failed 任务的
   `agent_tasks.errors` 不落库，#494 失败归因双写失效。

验收对应关系：
- 断言 A：含 linked_repos 的 direct 任务，传给 executor 的 env 必须含
  `GRAPE_LINKED_REPOS`（且内容为关联仓列表 JSON）→ `TestLinkedReposEnvInjection`。
- 断言 B：`new_status=failed` 且 done 事件带 errors 时，`update_task_status`
  必须以非 None 的 `errors` 落库（并保留既有诊断记录）→ `TestFailedErrorsPersist`。
- AST 扫描：`src/` 下不再存在「函数内无别名局部 `import json` 且此前已使用 json」
  的反模式 → `TestNoLocalJsonShadow`。
"""
import ast
import importlib
import json
import os
import sys
import threading
from unittest import mock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(ROOT, "src")
DE_PATH = os.path.join(SRC_DIR, "domains", "runtime", "engine", "direct_executor.py")

_LINKED_REPOS = [{"repo_key": "gitcode:linked/backend", "branch": "main"}]


# ===========================================================================
# AST 反模式扫描（AC1）
# ===========================================================================

def _own_scope_nodes(node):
    """遍历 node 自身作用域内的语句，不进入嵌套函数/类（局部 import 只遮蔽本作用域）。"""
    stack = list(node.body)
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield cur
        stack.extend(ast.iter_child_nodes(cur))


def _json_shadow_hits(tree):
    """返回 [(函数名, 局部 import 行号, 此前 json. 使用行号)]：无别名局部 import json 且此前已用 json。"""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = list(_own_scope_nodes(node))
        bare = [n for n in own if isinstance(n, ast.Import)
                and any(a.name == "json" and a.asname is None for a in n.names)]
        if not bare:
            continue
        first = min(n.lineno for n in bare)
        used = sorted({n.lineno for n in own if isinstance(n, ast.Attribute)
                       and isinstance(n.value, ast.Name) and n.value.id == "json"
                       and n.lineno < first})
        if used:
            hits.append((node.name, first, used))
    return hits


class TestNoLocalJsonShadow:
    """AC1：局部无别名 import json 遮蔽全局 json 的反模式必须清零。"""

    def test_execute_agent_inner_has_no_local_json_import(self):
        """修复目标函数不再声明无别名局部 import json（模块级 L14 已提供）。"""
        tree = ast.parse(open(DE_PATH, encoding="utf-8").read())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_execute_agent_inner")
        own = list(_own_scope_nodes(func))
        bare = [n.lineno for n in own if isinstance(n, ast.Import)
                and any(a.name == "json" and a.asname is None for a in n.names)]
        assert bare == [], (
            "_execute_agent_inner 不得再有局部 import json（Issue #4150）：行 %s；"
            "它会把 json 变成函数局部名，导致此前 json.xxx 抛 UnboundLocalError" % bare
        )

    def test_module_level_json_import_present(self):
        """模块级 import json 仍在（删除局部 import 后函数内 json.xxx 依赖它）。"""
        mod = importlib.import_module("src.domains.runtime.engine.direct_executor")
        assert mod.json is json

    def test_src_has_no_json_shadow_pattern(self):
        """src/ 全量 AST 扫描：无「局部 import json 且此前用 json」的反模式。"""
        offenders = []
        for base, _dirs, files in os.walk(SRC_DIR):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                try:
                    tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
                except SyntaxError:
                    continue
                for func_name, import_line, used in _json_shadow_hits(tree):
                    offenders.append(f"{os.path.relpath(path, ROOT)}:{func_name}"
                                     f"(import@L{import_line}, json.@L{used})")
        assert offenders == [], "存在局部 import json 遮蔽反模式：\n" + "\n".join(offenders)


# ===========================================================================
# 依赖替身：驱动真实 _execute_agent_inner（生产同一路径）
# ===========================================================================

def _install_module(monkeypatch, name, fake):
    """替换模块替身：sys.modules + 父包属性（`from pkg import sub` 读父包属性）。"""
    monkeypatch.setitem(sys.modules, name, fake)
    parent, _, leaf = name.rpartition(".")
    if not parent:
        return
    try:
        pkg = importlib.import_module(parent)
    except Exception:  # pragma: no cover - 父包不可导入时仅走 sys.modules
        return
    monkeypatch.setattr(pkg, leaf, fake, raising=False)


class _FakeDB:
    """只回答「无行」的假库：本用例不校验 DB 读写，只校验写库调用参数。"""

    def __init__(self):
        self.cursor_obj = mock.MagicMock()
        self.cursor_obj.fetchone.return_value = None
        self.cursor_obj.fetchall.return_value = []
        self.committed = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1


def _fake_modules(tmp_path, captured, *, linked_repos, task_errors, done_event):
    """构造 _execute_agent_inner 全部函数级依赖的替身。"""
    pipeline = mock.MagicMock(name="event_pipeline")
    pipeline.get_agent_workdir.return_value = str(tmp_path)
    pipeline.get_agent_display_name.return_value = "test-agent"
    pipeline._read_scene_linked_repos.return_value = list(linked_repos)
    pipeline._materialize_direct_workdir.side_effect = (
        lambda ws, rk, at, sn, e, iid, p, engine_name="": p)
    pipeline._issue_num_from.return_value = ""
    pipeline.resolve_actor_account.return_value = "test-account"

    def _stream(prompt, ws, **kw):
        captured["env_extra"] = dict(kw.get("env_extra") or {})
        captured["stream_kwargs"] = kw
        yield done_event

    executor = mock.MagicMock(name="executor")
    executor.stream_devops_agent.side_effect = _stream

    service = mock.MagicMock(name="service")
    service.get_task.return_value = {}
    service._task_errors_json.return_value = list(task_errors)
    service.run_auto_advance_hook.return_value = {}
    service.update_task_status.side_effect = (
        lambda tid, status, **kw: captured.setdefault("task_status_calls", []).append(
            {"task_id": tid, "status": status, **kw}))

    event_store = mock.MagicMock(name="event_store")
    event_store.evaluate_agent_output.return_value = (False, "")
    event_store._extract_tool_command.return_value = ""

    repogit = mock.MagicMock(name="repogit")
    repogit.WorkspaceBaselineError = type("WorkspaceBaselineError", (Exception,), {})
    repogit.is_git_agent.return_value = False
    repogit.issue_num_from.return_value = ""
    repogit._read_scene_work_branch.return_value = ""
    repogit.ensure_linked_repo_worktrees.return_value = []
    repogit.inject_git_write_context.side_effect = lambda task, env, prompt: (prompt, {})

    worktree_guard = mock.MagicMock(name="worktree_guard")
    worktree_guard.inject_reset_context.side_effect = lambda task, env, prompt: (prompt, False)
    worktree_guard.check_delivery_alignment.return_value = {"ok": True}

    prompt_builder = mock.MagicMock(name="prompt_builder")
    prompt_builder.build_devops_agent_prompt.return_value = "test prompt"
    prompt_builder.resolve_task_server_env.return_value = {}

    repo_key_mod = mock.MagicMock(name="repo_key")
    repo_key_mod.derive_repo_identity.return_value = ("test/repo", "test", "repo")
    repo_key_mod.from_repo_key.return_value = "test/repo"
    repo_key_mod.split_owner_repo.return_value = ("test", "repo")
    repo_key_mod.to_repo_key.return_value = "github:test/repo"

    token_service = mock.MagicMock(name="token_service")
    token_service.is_auth_401_error.return_value = False
    token_service.detect_and_mark_repo_token_expired.return_value = ""

    transient_retry = mock.MagicMock(name="transient_retry")
    transient_retry.retry_transient_task.return_value = False
    transient_retry.format_stream_failure_result.return_value = ""

    failure_attribution = mock.MagicMock(name="failure_attribution")
    failure_attribution.failure_summary.return_value = "test failure summary"

    skill_materialize = mock.MagicMock(name="skill_materialize")
    skill_materialize.SKILL_CONSISTENCY_KEY = "_skill_consistency"
    skill_materialize.skill_mismatch_error.return_value = "[skill_mismatch] test"

    mgmt_public = mock.MagicMock(name="mgmt_public")
    mgmt_public.get_repo_token_with_refresh.return_value = "repo-token-test"
    mgmt_public.get_github_token.return_value = ""
    mgmt_public.get_host_user_id.return_value = 0
    mgmt_public.get_grape_account_by_id.return_value = None

    usage = mock.MagicMock(name="usage")
    insight_public = mock.MagicMock(name="insight_public")
    insight_public.master_usage_for_task.return_value = usage

    return {
        "src.domains.runtime.pipeline.event_pipeline": pipeline,
        "src.domains.runtime.engine.executor": executor,
        "src.domains.runtime.engine.service": service,
        "src.domains.runtime.engine.instances": mock.MagicMock(
            runtime_instance_name=mock.MagicMock(return_value="test-engine")),
        "src.domains.runtime.engine.repogit": repogit,
        "src.domains.runtime.engine.worktree_guard": worktree_guard,
        "src.domains.runtime.engine.session_tmpdir": mock.MagicMock(name="session_tmpdir"),
        "src.domains.runtime.engine.transient_retry": transient_retry,
        "src.domains.runtime.engine.skill_materialize": skill_materialize,
        "src.domains.runtime.engine.task_log_stream": mock.MagicMock(name="task_log_stream"),
        "src.domains.runtime.engine.engine_select": mock.MagicMock(name="engine_select"),
        "src.domains.runtime.engine.agent": mock.MagicMock(name="agent_mod"),
        "src.domains.runtime.agents.failure_attribution": failure_attribution,
        "src.domains.mgmt.public": mgmt_public,
        "src.domains.insight.public": insight_public,
        "src.api.event_store": event_store,
        "src.api.session_store": mock.MagicMock(name="session_store"),
        "src.api.deps": mock.MagicMock(name="deps"),
        "src.api.pipeline_config": mock.MagicMock(name="pipeline_config"),
        "src.api.issues_center": mock.MagicMock(name="issues_center"),
        "src.kernel.prompt_builder": prompt_builder,
        "src.kernel.repo_key": repo_key_mod,
        "src.kernel.token_service": token_service,
    }


@pytest.fixture()
def run_inner(monkeypatch, tmp_path):
    """返回 runner：以替身依赖跑真实 _execute_agent_inner，并回传捕获的 env/写库调用。"""

    def _run(*, linked_repos=(), task_errors=(), done_event=None, task_id="", agent_type="coding"):
        captured = {}
        for name, fake in _fake_modules(
                tmp_path, captured, linked_repos=linked_repos,
                task_errors=task_errors,
                done_event=done_event or ("done", {"exit_code": 0, "text": "", "errors": []})).items():
            _install_module(monkeypatch, name, fake)

        fake_db = _FakeDB()
        import src.kernel.session_manager as sm
        monkeypatch.setattr(sm, "db", fake_db, raising=False)
        monkeypatch.setattr(sm, "db_lock", threading.RLock(), raising=False)
        monkeypatch.setattr(sm, "add_message", mock.MagicMock(), raising=False)

        from src.domains.runtime.engine.direct_executor import _execute_agent_inner
        evt = {"repository": "test/repo", "issue_id": "", "type": "issue", "payload": {}}
        _execute_agent_inner("github:test/repo", 4150001, agent_type, "hermes",
                             "coding-cli", evt, runtime_id=0, task_id=task_id)
        return captured

    return _run


# ===========================================================================
# 断言 A：linked_repos 场景 → direct 任务 env 必含 GRAPE_LINKED_REPOS
# ===========================================================================

class TestLinkedReposEnvInjection:
    """AC-A：含 linked_repos 的场景，direct 任务 env 必须含 GRAPE_LINKED_REPOS。"""

    def test_env_carries_linked_repos_json(self, run_inner):
        captured = run_inner(linked_repos=_LINKED_REPOS)
        env = captured["env_extra"]
        assert "GRAPE_LINKED_REPOS" in env, (
            "direct 任务 env 缺少 GRAPE_LINKED_REPOS（关联仓注入被静默吞掉，Issue #4150）"
        )
        assert json.loads(env["GRAPE_LINKED_REPOS"]) == _LINKED_REPOS

    def test_linked_repo_tokens_injected_with_env(self, run_inner, monkeypatch):
        """#466 关联仓 token 分发随 GRAPE_LINKED_REPOS 一起恢复（同段链路，此前被一并跳过）。"""
        captured = run_inner(linked_repos=_LINKED_REPOS)
        pipeline = sys.modules["src.domains.runtime.pipeline.event_pipeline"]
        assert pipeline._inject_linked_repo_tokens.called, (
            "关联仓 token 注入未被调用（GRAPE_LINKED_REPOS 注入失败导致整段被 except 吞掉）"
        )
        args = pipeline._inject_linked_repo_tokens.call_args[0]
        assert args[1] == captured["env_extra"]["GRAPE_LINKED_REPOS"]

    def test_no_linked_repos_keeps_env_clean(self, run_inner):
        """无关联仓场景不注入该变量（不误注入空串）。"""
        env = run_inner(linked_repos=())["env_extra"]
        assert "GRAPE_LINKED_REPOS" not in env


# ===========================================================================
# 断言 B：failed + done.errors 非空 → agent_tasks.errors 落库（非 None）
# ===========================================================================

class TestFailedErrorsPersist:
    """AC-B：failed 任务 errors 必须落库，且合并不覆盖既有诊断记录。"""

    _DONE_FAILED = ("done", {
        "exit_code": 1,
        "text": "",
        "errors": ["boom: cannot access local variable 'json' where it is not associated "
                   "with a value"],
        "stderr": "",
    })

    def test_errors_passed_to_update_task_status(self, run_inner):
        captured = run_inner(task_id="t_4150_failed", done_event=self._DONE_FAILED,
                             agent_type="coding")
        failed = [c for c in captured.get("task_status_calls", []) if c["status"] == "failed"]
        assert failed, f"failed 终态未写库，calls={captured.get('task_status_calls')}"
        errors = failed[-1].get("errors")
        assert errors is not None, (
            "failed 任务 errors 为 None —— json.dumps 抛 UnboundLocalError 致 #494 双写失效"
        )
        decoded = json.loads(errors)
        assert any("boom" in str(e) for e in decoded), decoded

    def test_errors_merge_keeps_existing_records(self, run_inner):
        """合并而非覆盖：既有 #403 诊断记录必须与 done.errors 一并落库。"""
        captured = run_inner(
            task_id="t_4150_merge", done_event=self._DONE_FAILED, agent_type="coding",
            task_errors=[{"error_code": "TRANSIENT_RETRY", "reason": "upstream 502"}])
        failed = [c for c in captured.get("task_status_calls", []) if c["status"] == "failed"]
        assert failed, "failed 终态未写库"
        decoded = json.loads(failed[-1]["errors"])
        assert len(decoded) == 2, decoded
        assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY"
                   for e in decoded), decoded
        assert any("boom" in str(e) for e in decoded), decoded