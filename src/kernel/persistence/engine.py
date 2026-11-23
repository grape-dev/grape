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


from src.kernel.db_registry import DomainUnavailable as DomainEngineUnavailable  # noqa: E402
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


def get_domain_engine(domain: str) -> Engine:
    """按域取 Engine（跨域 ORM 读用）：域 == 当前进程域时复用 get_engine()。

    为什么需要：ORM 层原先是「单库」的（get_engine 永远绑进程主库）。三库四进程下，
    runtime 进程经契约读 mgmt 表时若仍用进程主库，会在**错误的库**上查（表不存在/空表），
    这正是裸 SQL 时代踩过的 1146 坑的 ORM 版。这里按 `db_registry.dsn_of(domain)`
    建域引擎并缓存，调用方（Repository）无需感知拓扑。
    """
    from sqlalchemy import create_engine
    from urllib.parse import quote_plus

    from src.kernel import db_registry

    key_domain = (domain or "").lower()
    if not key_domain:
        return get_engine()
    # 连接归属只问一处：本域/单库 → 主库；跨域有 DSN → 域引擎；跨域缺 DSN → 抛（不回落）。
    if db_registry.route_of(key_domain) == "local":
        return get_engine()
    dsn_url = db_registry.dsn_of(key_domain)
    # DSN 形态 mysql://user:pass@host:port/db → SQLAlchemy URL
    # 注意：DSN 里的 user/pass 可能已做 URL 编码（现网密码含 @ / : 等），
    # 必须先 unquote 再 quote_plus，否则会二次编码导致认证失败（实测 1045）。
    from urllib.parse import unquote, urlsplit
    parts = urlsplit(dsn_url.replace("mysql://", "mysql+pymysql://", 1))
    url = (f"mysql+pymysql://{quote_plus(unquote(parts.username or ''))}:"
           f"{quote_plus(unquote(parts.password or ''))}@{parts.hostname}:{parts.port or 3306}"
           f"{parts.path}?charset=utf8mb4")
    key = ("domain", key_domain, url)
    with _engine_cache_lock:
        eng = _engine_cache.get(key)
        if eng is None:
            eng = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2, future=True)
            _engine_cache[key] = eng
            log.info(f"persistence domain engine ready: {key_domain} {url.split('@')[0].split('//')[0]}://***")
        return eng
