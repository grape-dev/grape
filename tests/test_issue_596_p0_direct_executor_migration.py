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
