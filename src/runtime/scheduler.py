"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.domains.runtime.engine.scheduler"""
import src.domains.runtime.engine.scheduler as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
