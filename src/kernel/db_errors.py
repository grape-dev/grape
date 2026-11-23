"""Schema-drift error classification shared by every layer.

A worker process keeps a **local** SQLite database that is built by whoever
shipped that worker: it lags behind the code by design (agent never touches the
master DB). Queries against a table or column that this local DB does not have
are therefore not failures — they are "this installation has no such data".
Logging them at WARNING makes a healthy worker look broken (measured: 59
warnings per sweeper round, forever).

Only genuinely unexpected errors keep the WARNING level.
"""
from __future__ import annotations

_MISSING_TABLE_MARKERS = (
    "no such table",        # SQLite
    "no such column",       # SQLite
    "doesn't exist",        # MySQL 1146 (table) 表述
    "unknown column",       # MySQL 1054
    "unknown table",        # MySQL 1051
)


def missing_schema(exc: BaseException) -> bool:
    """True when *exc* means「表/列不存在」rather than a real runtime fault.

    Covers SQLite (`no such table` / `no such column`) and MySQL
    (1146 / 1054 / 1051). Accepts pymysql exceptions whose ``args[0]`` is the
    numeric error code as well as wrapped messages.
    """
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], int) and str(args[0]) in ("1051", "1054", "1146"):
        return True
    lowered = str(exc).lower()
    return any(marker in lowered for marker in _MISSING_TABLE_MARKERS)


__all__ = ["missing_schema"]