"""引擎工厂：按 backend 建 SQLAlchemy Engine（桥接模式 dialect 承接方）。

- sqlite：复用 session_manager 的 DB_PATH（与裸 SQL 路径同库同文件）；
- mysql：复用 mysql_compat.mysql_config()（host/user/passwd/database）；
- pool_pre_ping：多进程/长连接防陈旧连接（业界默认，评审采纳）；
- 域感知（Issue #3732）：get_domain_engine(domain)——跨域按该域 DSN 建 engine
  （三库拓扑下跨域 ORM 读写禁止复用进程全局 engine）；本域/单库回落 get_engine()，
  跨域缺 DSN 显式抛 DomainEngineUnavailable（绝不回落主库）。
"""
from __future__ import annotations

import os
import threading

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from src.kernel.config import log


from src.kernel.db_registry import DomainUnavailable as DomainEngineUnavailable  # noqa: E402


def _mysql_url(user, password, host, port=None, database=None, charset="utf8mb4") -> str:
    """mysql 连接 URL 的唯一拼装点（D105k 防线只有一份实现）。

    user/password 必须 URL 编码——现网密码可含 URL 保留字符（'@'/':' 等）时未编码
    URL 会把 host 解析错位（59 实锤 P1：全部 _uow 身份域写入连不上）；charset 走
    query string 不参与编码。get_db_url（进程主库）与 _dsn_to_url（域 DSN）共用本函数，
    避免「改一处漏一处」。
    """
    from urllib.parse import quote_plus
    return (f"mysql+pymysql://{quote_plus(str(user or ''))}:"
            f"{quote_plus(str(password or ''))}"
            f"@{host}:{port or 3306}/{database or ''}"
            f"?charset={charset or 'utf8mb4'}")


def get_db_url() -> str:
    """按 backend 返回连接 URL（与裸 SQL 路径同库）。"""
    from src.kernel.mysql_compat import get_db_backend, mysql_config
    if get_db_backend() == "mysql":
        cfg = mysql_config()
        return _mysql_url(cfg.get("user"), cfg.get("password"), cfg.get("host"),
                          cfg.get("port", 3306), cfg.get("database"))
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


def _dsn_to_url(dsn: str) -> str:
    """域 DSN → SQLAlchemy URL。

    - mysql://user:***@host:port/db（生产唯一形态）→ mysql+pymysql://…
      （复用 db_registry.parse_dsn 的解析 + quote_plus 的 URL 编码，密码含保留字符
      不会把 host 解析错位，与 get_db_url 同防线）；
    - sqlite: scheme 直通——双库 fixture 测试通道（表达「另一个库」），生产 DSN 仅
      mysql（db_registry.parse_dsn 对其它 scheme 直接拒绝）。
    """
    from urllib.parse import urlparse
    scheme = (urlparse(dsn).scheme or "").lower()
    if scheme == "sqlite":
        # 仅测试通道（双库 fixture 表达「另一个库」）：sqlite 绕过 parse_dsn 的
        # scheme 白名单，生产误配成 sqlite 路径会拿到一个空库、读路径静默返回
        # 「无绑定」。此处显式告警，便于在日志里第一时间看出配错了 scheme。
        log.warning("persistence domain engine: 域 DSN 使用 sqlite scheme"
                    "（仅测试通道，生产必须 mysql）：scheme=sqlite")
        return dsn
    from src.kernel import db_registry
    cfg = db_registry.parse_dsn(dsn)
    return _mysql_url(cfg.get("user"), cfg.get("password"), cfg.get("host"),
                      cfg.get("port", 3306), cfg.get("database"),
                      cfg.get("charset") or "utf8mb4")


def get_domain_engine(domain: str) -> Engine:
    """域感知 Engine 工厂（Issue #3732）：按域取 Engine，跨域不复用进程主库。

    为什么必须存在：ORM 层原先是「单库」的（get_engine 永远绑进程主库）。三库四进程下，
    worker/event 进程的全局 engine 指向 runtime 库；mgmt 域表（external_identities /
    user_credentials 等，归属见 configs/table_ownership.json）的 ORM 查询若复用全局
    engine，会打到 runtime 库触发 1146，再被调用方 except 吞成「用户无第三方绑定」——
    生产 7 天 6218 条 WARNING（Issue #3732）。

    连接归属只问一处：`db_registry.route_of(domain)`——本域进程/单库拓扑 → 主库引擎
    （零回归）；跨域且有 GRAPE_DSN_<DOMAIN> → 按该 DSN 建域引擎并缓存；跨域缺 DSN
    → 抛 DomainUnavailable（DomainEngineUnavailable），**绝不回落进程主库**（回落会把
    该域的读静默打到本进程库，读到的是另一张库里的同名表/空壳，与「数据不存在」不可区分）。

    判据不含连接探活：create_engine 懒连接不触发真实握手；若用 uses_own_dsn()
    （内含 domain_db() 探活），临时连接失败会带着 engine 选择回落进程库，把 1146
    重新制造出来。
    """
    # 局部导入 create_engine 作 seam：用例以 monkeypatch.setattr("sqlalchemy.create_engine", …)
    # 断言「跨域按 DSN 建引擎」；模块级别名会让该补丁打不到实际调用点。
    from sqlalchemy import create_engine

    from src.kernel import db_registry

    key_domain = (domain or "").lower()
    if not key_domain:
        return get_engine()
    # 连接归属只问一处：本域/单库 → 主库；跨域有 DSN → 域引擎；跨域缺 DSN → 抛（不回落）。
    if db_registry.route_of(key_domain) == "local":
        return get_engine()
    url = _dsn_to_url(db_registry.dsn_of(key_domain))
    key = ("domain", key_domain, url)
    with _engine_cache_lock:
        eng = _engine_cache.get(key)
        if eng is None:
            eng = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2, future=True)
            _engine_cache[key] = eng
            log.info(f"persistence domain engine ready: {key_domain} {url.split('@')[0].split('//')[0]}://***")
        return eng
