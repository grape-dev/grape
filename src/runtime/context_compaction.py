"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.domains.runtime.engine.context_compaction"""
import src.domains.runtime.engine.context_compaction as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
