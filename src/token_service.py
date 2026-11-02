"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.token_service"""
from src.kernel import token_service as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
