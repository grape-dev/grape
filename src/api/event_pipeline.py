"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.domains.runtime.pipeline.event_pipeline"""
import src.domains.runtime.pipeline.event_pipeline as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
# Phase 3 (#699): _execute_agent 系列迁往 direct_executor，旧 import 路径继续可用
import src.domains.runtime.engine.direct_executor as _de
globals().update({k: v for k, v in vars(_de).items() if not k.startswith("__")})
del _impl, _de
