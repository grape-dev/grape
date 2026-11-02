"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.auth_sdk.jwt_utils"""
from src.kernel.auth_sdk import jwt_utils as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
