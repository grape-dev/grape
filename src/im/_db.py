"""IM 层数据库入口（跨域库统一）。

四进程拆分后每个进程绑定自己的域库（mgmt/event/worker/insight），但 IM 渠道数据
（安装/身份绑定/会话绑定/去重/消息/租约）是**跨进程共享**的，统一落在 runtime 域库
（`GRAPE_IM_DB_NAME`，见 engine_wiring）。IM 模块若直接用 `src.session_manager.db`
就会读到本进程域库（mgmt）→ 安装/身份表是空的 → 命令层报"未找到关联平台用户"。

一律 `from src.im._db import db, db_lock`。
"""
import threading as _threading

db_lock = _threading.RLock()
_conn = None
_conn_lock = _threading.RLock()


def _get():
    """惰性取 IM 域库连接（单例；MySQL 走池化/共享连接，SQLite 复用同一文件连接）。"""
    global _conn
    if _conn is None:
        with _conn_lock:
            if _conn is None:
                from src.im.engine_wiring import _connect, default_db_path
                _conn = _connect(default_db_path())
    return _conn


class _DBProxy:
    """把属性访问转发到 IM 域库连接，接口与 sqlite3.Connection 一致。"""

    def __getattr__(self, name):
        return getattr(_get(), name)

    def __repr__(self) -> str:  # 便于日志排查
        return f"<im_db proxy -> {_get()!r}>"


db = _DBProxy()
