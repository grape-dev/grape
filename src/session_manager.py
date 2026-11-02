"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.session_manager"""
from src.kernel import session_manager as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
