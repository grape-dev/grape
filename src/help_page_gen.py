"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.help_page_gen"""
from src.kernel import help_page_gen as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
