"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.domains.mgmt.routes.templates"""
import src.domains.mgmt.routes.templates as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
