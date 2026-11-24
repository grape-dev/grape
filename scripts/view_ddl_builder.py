"""兼容外壳：DDL 构造的唯一实现已上收到 `src/kernel/domain_views.py`。

保留此文件是为了 `scripts/domain_view_freshness.py`、`scripts/sync_domain_views.py`
与文档里的路径不变；禁止在此再实现一份 DDL（那正是本文件当初要消灭的漂移）。
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.kernel.domain_views import (  # noqa: E402,F401
    IDENT_RE,
    ViewDDLError,
    build_view_ddl,
    check_ident,
)

__all__ = ["IDENT_RE", "ViewDDLError", "build_view_ddl", "check_ident"]