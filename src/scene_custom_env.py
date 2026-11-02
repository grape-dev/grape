"""Compatibility re-export (Issue #699 Phase 1). Canonical: src.kernel.scene_custom_env"""
from src.kernel import scene_custom_env as _impl
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
del _impl
