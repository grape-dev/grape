"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.featured_order"""
from src.kernel import featured_order as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
