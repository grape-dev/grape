"""P0 tests for Issue #596: _execute_agent migration to direct_executor.py.

Verifies:
- direct_executor.py module imports correctly (no circular import)
- Migrated symbols are defined in direct_executor and re-exported by event_pipeline
- event_pipeline no longer defines the migrated functions
- local_claimer imports from direct_executor (not event_pipeline)
- event_pipeline no longer calls start_direct_agent(_execute_agent, ...)
"""
import ast
import importlib
import inspect


class TestDirectExecutorModule:

    def test_direct_executor_importable(self):
        mod = importlib.import_module("src.runtime.direct_executor")
        assert mod is not None

    def test_direct_executor_has_pool_vars(self):
        from src.runtime.direct_executor import _DIRECT_POOL, _DIRECT_FUTURES, _DIRECT_FUTURES_LOCK
        assert _DIRECT_POOL is not None
        assert _DIRECT_FUTURES is not None
        assert _DIRECT_FUTURES_LOCK is not None

    def test_direct_executor_has_start_direct_agent(self):
        from src.runtime.direct_executor import start_direct_agent
        assert callable(start_direct_agent)

    def test_direct_executor_has_execute_agent(self):
        from src.runtime.direct_executor import _execute_agent
        assert callable(_execute_agent)

    def test_direct_executor_has_execute_agent_inner(self):
        from src.runtime.direct_executor import _execute_agent_inner
        assert callable(_execute_agent_inner)

    def test_no_module_level_event_pipeline_import(self):
        import src.runtime.direct_executor as de
        source = inspect.getsource(de)
        tree = ast.parse(source)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom):
                assert "event_pipeline" not in (node.module or ""), \
                    "direct_executor imports event_pipeline at module level"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "event_pipeline" not in alias.name


class TestEventPipelineReExport:

    def test_re_export_same_objects(self):
        from src.runtime.direct_executor import (
            _DIRECT_POOL as de_pool, _DIRECT_FUTURES as de_futures,
            _DIRECT_FUTURES_LOCK as de_lock, start_direct_agent as de_sda,
            _execute_agent as de_ea, _execute_agent_inner as de_eai,
        )
        from src.api.event_pipeline import (
            _DIRECT_POOL as ep_pool, _DIRECT_FUTURES as ep_futures,
            _DIRECT_FUTURES_LOCK as ep_lock, start_direct_agent as ep_sda,
            _execute_agent as ep_ea, _execute_agent_inner as ep_eai,
        )
        assert ep_pool is de_pool
        assert ep_futures is de_futures
        assert ep_lock is de_lock
        assert ep_sda is de_sda
        assert ep_ea is de_ea
        assert ep_eai is de_eai

    def test_event_pipeline_no_definitions(self):
        import src.api.event_pipeline as ep
        source = inspect.getsource(ep)
        tree = ast.parse(source)
        defined_funcs = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.col_offset == 0:
                defined_funcs.add(node.name)
        assert "start_direct_agent" not in defined_funcs
        assert "_execute_agent" not in defined_funcs
        assert "_execute_agent_inner" not in defined_funcs

    def test_no_direct_pool_definition(self):
        import src.api.event_pipeline as ep
        source = inspect.getsource(ep)
        assert "ThreadPoolExecutor(" not in source


class TestLocalClaimerImport:

    def test_local_claimer_imports_from_direct_executor(self):
        import src.runtime.local_claimer as lc
        source = inspect.getsource(lc._execute_claimed_locally)
        assert "from src.runtime.direct_executor import" in source
        assert "from src.api.event_pipeline import _execute_agent" not in source


class TestCallSiteMigration:

    def test_no_start_direct_agent_calls_in_run_pipeline(self):
        import src.api.event_pipeline as ep
        source = inspect.getsource(ep)
        assert "start_direct_agent(_execute_agent" not in source


class TestBackwardCompatibility:

    def test_import_from_event_pipeline_still_works(self):
        from src.api.event_pipeline import _execute_agent, start_direct_agent
        assert callable(_execute_agent)
        assert callable(start_direct_agent)

    def test_import_from_direct_executor_works(self):
        from src.runtime.direct_executor import _execute_agent, start_direct_agent
        assert callable(_execute_agent)
        assert callable(start_direct_agent)

    def test_cancel_direct_task_uses_reexported_futures(self):
        from src.api.event_pipeline import cancel_direct_task, _DIRECT_FUTURES, _DIRECT_FUTURES_LOCK
        assert callable(cancel_direct_task)
        assert _DIRECT_FUTURES is not None
        assert _DIRECT_FUTURES_LOCK is not None


class TestFailureFinalizationRuntimePath:
    """Runtime failure-finalization path tests (PR #781 review Required).

    The prior 14 tests are import/AST static assertions and never execute the
    _execute_agent_inner failure-finalization path, so they missed the NameError
    caused by verbatim migration omitting original module-level imports.

    - :599 is_auth_401_error (not in try) -> NameError -> exec crash
    - :300/:669 json, :545/:717 db/db_lock (silent except) -> silent failure

    These tests construct an exit_code!=0 done event, run the finalization path
    with mocked external deps, and assert no NameError escapes.
    """

    def test_failure_done_event_finalization_no_name_error(self):
        """Construct exit_code=1 done event, run finalization, assert no NameError."""
        import sys
        from unittest import mock

        done_data = {
            "exit_code": 1,
            "text": "execution failed",
            "errors": ["HTTP 401 Unauthorized"],
            "stderr": "auth token expired",
        }

        def fake_stream(*a, **kw):
            yield "done", done_data

        _mu = mock.MagicMock()
        ep = mock.MagicMock(
            get_agent_workdir=lambda *a, **k: "/tmp/test_de_finalization",
            get_agent_display_name=lambda *a, **k: "test-agent",
            _read_scene_linked_repos=lambda *a, **k: [],
            _inject_scene_env=lambda *a, **k: None,
            _inject_bot_agent_env=lambda *a, **k: None,
            _inject_issue_pr_env=lambda *a, **k: None,
            _inject_linked_repo_tokens=lambda *a, **k: None,
            _materialize_direct_workdir=lambda ws, rk, at, sn, e, iid, p, engine_name="": p,
            _fail_event=lambda *a, **k: None,
            _issue_num_from=lambda s: "",
            _post_result_to_issue=lambda *a, **k: None,
            _post_result_to_pr=lambda *a, **k: None,
            _post_all_subtasks_done=lambda *a, **k: None,
            _post_subtask_completion_to_parent=lambda *a, **k: None,
            _read_scene_work_branch=None,
            _redispatch_vod_after_recommend=lambda *a, **k: None,
            _submit_direct_linked_prs=lambda *a, **k: None,
            _sync_issue_comments_to_conversation=lambda *a, **k: None,
            _update_direct_task=lambda *a, **k: None,
            register_scene_deploy_url=lambda *a, **k: None,
            register_sre_deploy_url=lambda *a, **k: None,
        )
        es = mock.MagicMock(
            evaluate_agent_output=lambda *a, **k: (False, "execution failed"),
            update_event_config=lambda *a, **k: None,
            update_event_complete=lambda *a, **k: None,
            update_event_status=lambda *a, **k: None,
            append_skipped_event=lambda *a, **k: None,
            _extract_tool_command=lambda *a, **k: "",
        )
        sm = mock.MagicMock(db=mock.MagicMock(), db_lock=mock.MagicMock())
        _is401 = mock.MagicMock(return_value=False)
        ts = mock.MagicMock(
            is_auth_401_error=_is401,
            detect_and_mark_expired=lambda *a, **k: "",
        )
        rk = mock.MagicMock(
            from_repo_key=lambda k: "test/repo",
            split_owner_repo=lambda s: ("test", "repo"),
        )
        udb = mock.MagicMock(
            get_repo_token_with_refresh=lambda *a, **k: "",
            get_host_user_id=lambda *a, **k: 0,
        )

        fake_modules = {
            "src.api.event_pipeline": ep,
            "src.runtime.executor": mock.MagicMock(stream_devops_agent=fake_stream),
            "src.prompt_builder": mock.MagicMock(
                build_devops_agent_prompt=lambda *a, **k: "test prompt"
            ),
            "src.api.event_store": es,
            "src.runtime.instances": mock.MagicMock(
                runtime_instance_name=lambda *a, **k: "test-engine"
            ),
            "src.session_manager": sm,
            "src.token_service": ts,
            "src.repo_key": rk,
            "src.user.db": udb,
            "src.auth.grape_account_db": mock.MagicMock(),
            "src.api.deps": mock.MagicMock(),
            "src.runtime.repogit": mock.MagicMock(),
            "src.api.auth_token": mock.MagicMock(
                issue_session_token=lambda *a, **k: "tk_test"
            ),
            "src.observability.master_usage": mock.MagicMock(
                for_task=lambda *a, **k: _mu
            ),
            "src.runtime.service": mock.MagicMock(),
            "src.api.session_store": mock.MagicMock(),
        }

        from src.runtime import direct_executor as de
        original = {k: sys.modules.get(k) for k in fake_modules}
        try:
            sys.modules.update(fake_modules)
            evt = {
                "repository": "test/repo",
                "issue_id": "",
                "type": "issue",
                "payload": {},
            }
            try:
                de._execute_agent_inner(
                    "test/repo", 1, "coding", "test", "coding", evt,
                    runtime_id=0, task_id="",
                )
            except NameError as e:
                import pytest
                pytest.fail(
                    f"NameError in failure finalization path "
                    f"(missing lazy import): {e}"
                )
            except Exception:
                pass
            ts.is_auth_401_error.assert_called()
        finally:
            for k, v in original.items():
                if v is not None:
                    sys.modules[k] = v
                else:
                    sys.modules.pop(k, None)

    def test_failure_finalization_imports_present_in_inner(self):
        """Verify lazy imports present in _execute_agent_inner source."""
        import inspect
        from src.runtime.direct_executor import _execute_agent_inner
        src = inspect.getsource(_execute_agent_inner)
        assert "from src.token_service import is_auth_401_error" in src, \
            "is_auth_401_error lazy import missing in _execute_agent_inner"
        assert "import json" in src, \
            "json lazy import missing in _execute_agent_inner"
        assert "from src.session_manager import db, db_lock" in src, \
            "db/db_lock lazy import missing in _execute_agent_inner"
