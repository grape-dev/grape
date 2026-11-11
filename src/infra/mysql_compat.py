"""Compatibility alias (Issue #699): canonical module src.kernel.mysql_compat.

模块别名（sys.modules 指向 canonical），保证 import / monkeypatch /
属性赋值全部作用在同一模块对象上（旧 re-export 复制符号会导致补丁失效）。
"""
import sys as _sys
import src.kernel.mysql_compat as _impl
_sys.modules[__name__] = _impl
# Issue #2010: relocated from src/mysql_compat.py to src/infra/mysql_compat.py (top-level layout cleanup).
