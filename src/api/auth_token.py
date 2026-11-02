"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.auth_sdk.auth_token"""
from src.kernel.auth_sdk import auth_token as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
