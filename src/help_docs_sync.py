"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.help_docs_sync"""
from src.kernel import help_docs_sync as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
