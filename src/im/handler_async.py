"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.domains.runtime.im.handler_async"""
import src.domains.runtime.im.handler_async as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
