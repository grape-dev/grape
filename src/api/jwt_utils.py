"""Compatibility alias (Issue #699): canonical module src.kernel.auth_sdk.jwt_utils.

模块别名（sys.modules 指向 canonical），保证 import / monkeypatch /
属性赋值全部作用在同一模块对象上（旧 re-export 复制符号会导致补丁失效）。
"""
import sys as _sys
import src.kernel.auth_sdk.jwt_utils as _impl
_sys.modules[__name__] = _impl
