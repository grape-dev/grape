"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.repo_cleanup"""
from src.kernel import repo_cleanup as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
