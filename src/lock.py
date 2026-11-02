"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.lock"""
from src.kernel import lock as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
