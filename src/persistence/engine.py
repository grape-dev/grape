"""引擎工厂：按 backend 建 SQLAlchemy Engine（桥接模式 dialect 承接方）。

- sqlite：复用 session_manager 的 DB_PATH（与裸 SQL 路径同库同文件）；
- mysql：复用 mysql_compat.mysql_config()（host/user/passwd/database）；
- pool_pre_ping：多进程/长连接防陈旧连接（业界默认，评审采纳）。
"""
from __future__ import annotations

import os
import threading

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from src.kernel.config import log


def get_db_url() -> str:
    """按 backend 返回连接 URL（与裸 SQL 路径同库）。"""
    from src.kernel.mysql_compat import get_db_backend, mysql_config
    if get_db_backend() == "mysql":
        from urllib.parse import quote_plus
        cfg = mysql_config()
        # D105k：user/password 必须 URL 编码——现网密码可含 URL 保留字符（'@'/':' 等）
        # 时未编码 URL 会把 host 解析错位（59 实锤 P1：全部 _uow 身份域
        # 写入连不上）。charset 参数继续走 query string 不参与编码。
        return (f"mysql+pymysql://{quote_plus(str(cfg.get('user') or ''))}:"
                f"{quote_plus(str(cfg.get('password') or ''))}"
                f"@{cfg.get('host')}:{cfg.get('port', 3306)}/{cfg.get('database')}"
                f"?charset=utf8mb4")
    from src.kernel.session_manager import _db_path
    return f"sqlite:///{_db_path}"


_engine_cache: "dict[tuple, Engine]" = {}
# 扫雷⑩ S3：双线程同时 miss 会各自 create_engine（sqlite StaticPool 侧虽同连接
# 无害，但 URL 侧会双建连接池并泄漏一个）——miss 分支整体加锁串行化。
_engine_cache_lock = threading.Lock()

def cache_clear() -> None:
    """测试/重绑辅助：清空引擎缓存（原 lru_cache API 兼容，S78 fixture 使用）。"""
    _engine_cache.clear()


def get_engine() -> Engine:
    """进程级 Engine 工厂（按 session_manager 当前连接键控缓存；UoW session 从此创建）。

    环境变量 GRAPE_PERSISTENCE_URL 可覆盖（测试注入内存库等）。

    S91/D67 重绑跟随修复：原 lru_cache 单例在首次调用时捕获 session_manager.db
    的连接引用（StaticPool 挂接）——conftest/运行期重绑连接对象后，ORM 仍停留在
    旧 inode（D54"幽灵 inode"世界分裂的残留盲区：同一文件路径、两个世界）。
    修正 = 缓存键含"当前连接对象 id + 路径"，重绑即换引擎；creator 每次解析
    模块属性（_sm.db）而非早捕获引用。
    """
    import src.kernel.session_manager as _sm
    url = os.environ.get("GRAPE_PERSISTENCE_URL") or get_db_url()
    if url.startswith("sqlite"):
        # S78 现场教训：creator + StaticPool 直接挂接 session_manager 的同一
        # sqlite3 连接——ORM 与裸 SQL 同一事务世界，天然对账。
        from sqlalchemy.pool import StaticPool
        key = ("sqlite", id(_sm.db), getattr(_sm, "_db_path", ""))
        with _engine_cache_lock:
            eng = _engine_cache.get(key)
            if eng is None:
                eng = create_engine("sqlite://", creator=lambda: _sm.db,
                                    poolclass=StaticPool, future=True)
                _engine_cache[key] = eng
                log.info("persistence engine ready: sqlite (shared session_manager conn)")
            return eng
    key = ("url", url)
    with _engine_cache_lock:
        eng = _engine_cache.get(key)
        if eng is None:
            eng = create_engine(url, pool_pre_ping=True, future=True)
            _engine_cache[key] = eng
            log.info(f"persistence engine ready: {url.split('@')[0].split('//')[0]}://***")
        return eng


get_engine.cache_clear = cache_clear  # lru_cache API 兼容（S78 fixture）
