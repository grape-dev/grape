"""Compatibility alias (Issue #699): canonical module src.domains.runtime.pipeline.event_pipeline.

模块别名（sys.modules 指向 canonical），保证 import / monkeypatch /
属性赋值全部作用在同一模块对象上（旧 re-export 复制符号会导致补丁失效）。
"""
import sys as _sys
import src.domains.runtime.pipeline.event_pipeline as _impl
# Phase 3 (#699): _execute_agent 系列迁往 direct_executor，旧属性路径继续可用
# （注入到 canonical 模块命名空间，保证 patch(ep, "_execute_agent") 对同模块对象生效）
import src.domains.runtime.engine.direct_executor as _de
for _n in ("_execute_agent", "_execute_agent_inner", "start_direct_agent", "_DIRECT_POOL",
           "_DIRECT_FUTURES", "_DIRECT_FUTURES_LOCK"):
    if hasattr(_de, _n) and not hasattr(_impl, _n):
        setattr(_impl, _n, getattr(_de, _n))
_sys.modules[__name__] = _impl
