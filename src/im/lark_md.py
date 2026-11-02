"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.domains.runtime.im.lark_md"""
import src.domains.runtime.im.lark_md as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
