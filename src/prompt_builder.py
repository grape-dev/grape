"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.prompt_builder"""
from src.kernel import prompt_builder as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
