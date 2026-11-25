"""IM 引擎装配层（Issue #825 P1 接线收尾）。

main.py 与新引擎（im_engine + im_channel + feishu 适配器）之间的装配层：
Router 六个注入点在这里换成真实实现，Supervisor/Provider/Registry 在这里起连接。
旧 `BotManager` 传输层已于 2026-11-10（A2 M1/M2）下线，不存在回滚链路——
`engine_mode()` 现在只是新引擎的**总开关**（on=跑 IM，off=完全不起 IM）。

依赖边界（Issue #742 §2.1 铁律）：
  - im_engine 包核心只 import im_channel —— 本模块不是引擎核心，是装配层，
    允许 import 适配器（注册进 Registry）与 legacy handler（process_chat_async /
    process_vod_async / process_command_async），并允许读 InboundMessage.raw
    （仅用于卡片 action_value 提取等平台语义翻译，引擎核心仍禁读）。

开关（秒级）：
  - env GRAPE_IM_ENGINE=on/off（默认 on，Spec 建议初始新引擎开、legacy 保底）；
  - 覆盖文件 GRAPE_IM_ENGINE_MODE_FILE（默认 <repo>/data/im_engine_mode，内容
    on/off）：运行期改文件即切（main.py _im_mode_watcher 5s 轮询）→ 秒级生效；
  - Issue #1008 出站收敛：env GRAPE_IM_EGRESS_MODE=replier/legacy（默认 replier）
    + 覆盖文件 GRAPE_IM_EGRESS_MODE_FILE（data/im_egress_mode）。
    replier=handler 出站统一 OutboundMessage→Replier→Channel.send（核心零平台名）；
    legacy=1:1 透传平台 API（改造前行为，秒级回滚）。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from typing import Awaitable, Callable, Optional

# 渠道安装表（grape_t_channel_installation）归 mgmt：语句在该域 public，
# 由调用方传入自己的连接（3921a54c6 归位时漏了这行 import，运行期 NameError 被
# except 吞成 "读不到安装 → None"，IM 推送静默失效）。
from src.domains.mgmt import public as _mgmt_public
# 渠道族 DDL 归 mgmt 域（2026-11-15）。2026-11-18 修正：原先这里 import mgmt 的
# _CHANNEL_INBOUND_MESSAGE_DDL 并在本进程 conn.execute() 建表 —— 该表归 mgmt
# （configs/table_ownership.json），runtime 进程在三库拓扑下会把它建到 **自己的库**，
# 既是越权建表、又会造出一张空的同名表。那张单表已删。
#
# 注意：渠道族其余 7 张表（grape_t_channel_*）同样归 mgmt，仍由本文件的
# ensure_channel_tables 调用建/补，但**按归属逐次判断**（table_ownership.should_skip）：
# 只有 owner 域进程（mgmt）才真正执行 DDL，runtime/insight 进程跳过。这样注释与行为一致，
# 也不再往别域库里造空壳表。


def _im_now() -> str:
    """UTC 时间戳（'YYYY-MM-DD HH:MM:SS'）：绑定行的 last_active_at/updated_at。

    与 bot_lease 同一口径（Python UTC，不用数据库时间函数）——MySQL NOW() 是服务器本地
    时间（CST），与读侧 UTC 比较会差 8 小时。
    """
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

# 数据库异常口径：SQLite 后端 sqlite3.Error；MySQL 后端 pymysql 异常。
# 仓储层是"失败降级"语义（去重读不到就放行、绑定读不到就当未绑定），统一捕获即可。
_DB_ERRORS: tuple = (sqlite3.Error,)
try:  # pragma: no cover - pymysql 缺失时（纯 sqlite 环境）不影响
    from pymysql import err as _pymysql_err
    _DB_ERRORS = _DB_ERRORS + (_pymysql_err.MySQLError,)
except Exception:  # pragma: no cover
    pass

_MYSQL_BACKEND_CACHE: Optional[bool] = None


def _safe_app_ref(app_id: str, channel_type: str = "") -> str:
    """日志里的 app 标识：Slack 的 app_id 位存 Bot Token → 只输出不可逆摘要。

    非 Slack 渠道保持既有前缀（便于排查）；Slack 渠道或形如 xoxb-/xapp- 的值
    一律摘要，绝不把令牌片段写进日志。
    """
    value = str(app_id or "")
    kind = str(channel_type or "").strip().lower()
    if kind == "slack" or value.startswith(("xoxb-", "xapp-")):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"slack:{digest}"
    if kind == "telegram" or _looks_like_telegram_token(value):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"telegram:{digest}"
    return value[:12]


def _looks_like_telegram_token(value: str) -> bool:
    """Telegram Bot Token 形态 `123456:ABC-DEF…` → 日志只留摘要，不落明文片段。"""
    head, sep, tail = str(value or "").partition(":")
    return bool(sep and head.isdigit() and len(tail) >= 10)

from src.domains.runtime.im.channel.capability import Capability  # noqa: F401  (re-export convenience)
from src.domains.runtime.im.channel.card import self_check_card_capabilities  # Issue #1095 启动自检
from src.domains.runtime.im.channel.channel import ChannelConfig
from src.domains.runtime.im.channel.message import InboundMessage
from src.domains.runtime.im.channel.registry import ChannelRegistry, default_registry
from src.domains.runtime.im.channel.types import MsgType, Scope, Type

from src.domains.runtime.im.engine.dedup import InboundDedup
from src.domains.runtime.im.engine.egress import EGRESS_ENV, HandlerEgress, egress_mode, legacy_send_card_reply
from src.domains.runtime.im.engine.metrics import CountingSender, EngineMetrics
from src.domains.runtime.im.engine.router import Identity, Installation, Router
from src.domains.runtime.im.engine.session import SessionBinding
from src.domains.runtime.im.engine.replier import Replier
from src.domains.runtime.im.engine.supervisor import Supervisor

log = logging.getLogger(__name__)

ENGINE_ENV = "GRAPE_IM_ENGINE"
MODE_FILE_ENV = "GRAPE_IM_ENGINE_MODE_FILE"
# Issue #1008：出站收敛开关（replier=OutboundMessage→Replier→Channel.send；legacy=直连平台 API）
EGRESS_MODE_FILE_ENV = "GRAPE_IM_EGRESS_MODE_FILE"
MODE_WATCH_INTERVAL = 5.0      # main.py 侧 mode 轮询周期（秒级切回粒度）

def _repo_root(start: str) -> str:
    """向上找含 scripts/migrations 的仓库根（模块搬到 domains/ 深层后仍稳）。

    Issue #1690：原实现固定三次 dirname，模块迁到 src/domains/runtime/im/ 后
    解析成 src/domains/scripts/...（不存在），IM 引擎建表迁移静默失效。
    """
    cur = os.path.dirname(os.path.abspath(start))
    while cur and cur != "/":
        if os.path.isdir(os.path.join(cur, "scripts", "migrations")):
            return cur
        cur = os.path.dirname(cur)
    return os.path.dirname(os.path.abspath(start))


_MIGRATION_PATH = os.path.join(
    _repo_root(__file__), "scripts", "migrations", "001_channel_tables.py",
)

_MIGRATION_004_PATH = os.path.join(
    _repo_root(__file__), "scripts", "migrations", "004_notify_outbox.py",
)

_MIGRATION_005_PATH = os.path.join(
    _repo_root(__file__), "scripts", "migrations", "005_outbox_purge_index.py",
)

# Issue #2261 A：引擎句柄全局注册点。
# 三进程拆分后引擎实例挂在各入口模块（grape_server/main）的模块属性上，
# notify._engine() 写死 src.api.main.im_engine 在 A/B/C 进程下恒 None → engine_down。
# 所有入口都经 start_im_engine()，这里作为唯一注册点；stop() 置空。
# 注：类型标注为 Optional[object]（ImEngine 定义在下方，前向引用避免 import 环）。
_CURRENT_ENGINE: "Optional[object]" = None


def set_current_engine(engine) -> None:
    """登记当前进程持有的引擎句柄（start 成功后调用；stop 时置 None）。"""
    global _CURRENT_ENGINE
    _CURRENT_ENGINE = engine


def get_current_engine():
    """取当前进程注册的引擎句柄（未启动/已停止 → None）。"""
    return _CURRENT_ENGINE


def kick_engine_scan() -> bool:
    """安装行变更后立刻唤醒 Supervisor 扫描（同步、绝不抛）。

    Supervisor 默认 30s 一轮扫描；凭证保存若只等窗口，新渠道要最多 30s 才建连，
    这期间的首条通知必然撞 "long connection is not established"/no binding。
    返回值仅用于日志/自检，失败不阻断保存主流程。
    """
    kick = getattr(get_current_engine(), "kick_scan", None)
    if kick is None:
        return False
    try:
        return bool(kick())
    except Exception as e:
        log.warning("im engine kick_scan failed: %s", e)
        return False


_APP_SEEN_TABLE = (
    "CREATE TABLE IF NOT EXISTS bot_seen_messages ("
    " app_id TEXT, message_id TEXT, seen_at REAL, UNIQUE (app_id, message_id))"
)


# ---------------------------------------------------------------- 开关

def _mode_file_path() -> str:
    path = os.environ.get(MODE_FILE_ENV)
    if path:
        return path
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "im_engine_mode",
    )


def engine_mode() -> str:
    """当前 IM 运行状态：'on'（新引擎跑）| 'off'（IM 总开关关闭，不起任何链路）。

    旧 BotManager 已下线（A2 M2），off 不再代表"回退到 legacy"。
    优先读覆盖文件（运行期可改 → 秒级生效），回落 env GRAPE_IM_ENGINE（默认 on）。
    """
    try:
        with open(_mode_file_path(), encoding="utf-8") as f:
            value = f.read().strip().lower()
        if value in ("on", "off"):
            return value
    except Exception:
        pass
    env = os.environ.get(ENGINE_ENV, "on").strip().lower()
    return "on" if env in ("on", "1", "true", "yes") else "off"




# ---------------------------------------------------------------- 迁移接线

def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migrations_001", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("migrations_001", module)
    spec.loader.exec_module(module)
    return module


def _load_migration_004():
    """加载 004_notify_outbox（延迟加载，避免模块导入期文件 IO）。"""
    spec = importlib.util.spec_from_file_location("migrations_004", _MIGRATION_004_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("migrations_004", module)
    spec.loader.exec_module(module)
    return module


def _load_migration_005():
    """加载 005_outbox_purge_index（延迟加载，避免模块导入期文件 IO）。"""
    spec = importlib.util.spec_from_file_location("migrations_005", _MIGRATION_005_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("migrations_005", module)
    spec.loader.exec_module(module)
    return module


def run_startup_migration(db_path: str) -> dict:
    """启动迁移（幂等，可重复执行）：001 建渠道表 + 004 outbox 补列 + wiring 自有消息表。

    A2 收尾（Phase B）：从 legacy `im_*` 列回填安装行已移到一次性迁移
    `scripts/migrations/002_drop_legacy_im.py`（先回填再 DROP 列/表）——
    旧列不存在后启动再跑回填会直接 no such column。

    Issue #2261 B1：串接 004（grape_t_channel_outbound_card 补列，通知 outbox）。

    必须在 worker 线程内调用；连接由 _connect 按后端选择（MySQL 走池化共享连接，
    SQLite 走本地文件连接），方言统一经 src.kernel.mysql_compat 翻译。
    """
    migrations = _load_migration_module()
    conn = _connect(db_path)
    try:
        # bot_seen_messages 归 runtime：mgmt 进程走 MySQL 时 _connect 返回的就是进程主库，
        # 这行会在 mgmt 库里建出空壳（2026-11-15 实测：每次 mgmt 重启 3 分钟内长回来）。
        # 渠道 4 表归 mgmt，照常建；只跳过这一张别家的表。
        from src.kernel import table_ownership as _own_tbl
        if _own_tbl.should_skip("bot_seen_messages"):
            log.info("[DDL_SKIP] `bot_seen_messages`: owner=runtime，非本域进程跳过")
        else:
            conn.execute(_APP_SEEN_TABLE)     # 测试库无则补空表
        # 渠道族 7 表全归 mgmt：仅 owner 域进程执行，别域跳过（见文件头说明）。
        if not _own_tbl.should_skip("grape_t_channel_installation"):
            migrations.ensure_channel_tables(conn)
        # Issue #3799：渠道健康状态表归 runtime（别域只读，不建表）。
        if not _own_tbl.should_skip("grape_t_channel_health"):
            from src.domains.runtime.schema_ddl import _CHANNEL_HEALTH_DDL
            conn.execute(_CHANNEL_HEALTH_DDL)
        try:
            mig004 = _load_migration_004()
            if mig004 is not None:
                mig004.ensure_notify_outbox_columns(conn)
        except Exception as e:            # outbox 补列失败不阻断启动（可重跑）
            log.warning("[startup-migration] 004 notify outbox columns failed: %s", e)
        try:
            mig005 = _load_migration_005()
            if mig005 is not None:
                mig005.ensure_purge_index(conn)
        except Exception as e:            # 索引缺失只影响性能，不阻断启动
            log.warning("[startup-migration] 005 purge index failed: %s", e)
        # （已移除）conn.execute(_MESSAGE_TABLE) —— 见文件头说明：渠道入站消息表归 mgmt，
        # 本进程不再建表；此处 commit 保留，用于上面 004/005 两个本域迁移的落库。
        conn.commit()
        return {"user": 0, "repo": 0, "installations": [],
                "backfill": "moved-to-002_drop_legacy_im"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def default_db_path() -> str:
    """与 src.kernel.session_manager 同源的 sqlite 库路径（env DEVOPS_DB_PATH）。

    Issue #3251：本函数曾自拼 dirname 上三级，模块迁到 src/domains/runtime/im/
    后解析成 src/domains/data/sessions.db（不存在）→ 裸启动 outbox 每轮报
    unable to open database file 并拖死事件循环。现委托 session_manager 单一
    出口（env 优先 + import 期快照 _db_path，与主库口径天然一致）；
    函数名与 __all__ 导出保持不变（调用方零改动）。
    """
    env = os.environ.get("DEVOPS_DB_PATH")
    if env:
        return env
    from src.kernel import session_manager as _sm  # 延迟导入：避免与 kernel 层循环
    return _sm._db_path


# Issue #3251：DB 打开失败按解析路径只报一次 ERROR——裸启动路径错位时 outbox
# 每轮重试（实测 2.5h 刷 5005 条 unable to open database file），既淹没日志
# 又拖慢事件循环（最终 event loop stalled 7211s）。路径恢复或变化后会重新告警。
_DB_OPEN_ERROR_LOGGED: set = set()


def _note_db_open_failure(db_path: str, exc: sqlite3.Error) -> None:
    """记录 DB 打开失败：同一解析路径只发一次 ERROR，异常打 _db_open_logged 标记后原样抛出。

    外层循环（如 outbox consumer）看到 _db_open_logged 即可静默降级，
    避免逐轮 WARNING 刷屏；其余异常语义不变。
    """
    key = os.path.abspath(db_path)
    if key not in _DB_OPEN_ERROR_LOGGED:
        _DB_OPEN_ERROR_LOGGED.add(key)
        log.error(
            "[db] cannot open sqlite db path=%s (DEVOPS_DB_PATH %s): %s "
            "— reported once per path, retries stay silent until recovered",
            db_path,
            "set" if os.environ.get("DEVOPS_DB_PATH") else "not set",
            exc,
        )
    exc._db_open_logged = True


def _connect(db_path: str):
    """取一条数据库连接：MySQL 后端复用 session_manager 的池化连接，SQLite 后端开本地连接。

    Issue #825 MySQL 适配：仓储层原本写死 sqlite3（PRAGMA/?/datetime 等），
    统一交给 src.kernel.mysql_compat 方言翻译（? → %s、INSERT OR IGNORE、ON CONFLICT
    upsert、datetime('now')、CREATE TABLE 的 TEXT 主键 → VARCHAR(191) 等）。
    返回的连接统一支持 execute/cursor/commit/rollback/close；共享连接由
    _SharedConn 代理，close() 为空操作（不能关掉全局单例）。
    """
    if _is_mysql_backend():
        # 四进程拆分后每个进程绑定各自域库，但 IM 渠道表（installation/binding/
        # dedup/inbound_message/lease）跨进程共享 → 用 GRAPE_IM_DB_NAME 钉到单一域库
        # （dev/prod 均 runtime），并配 GRAPE_IM_DB_USER/PASSWORD 换成目标库账号
        # （域库账号按库授权，跨域访问会 CREATE/DML denied）。
        global _IM_DB_CONN
        _im_db = (os.environ.get("GRAPE_IM_DB_NAME") or "").strip()
        _cur_db = (os.environ.get("DEVOPS_DB_NAME") or "").strip()
        if _im_db and _im_db != _cur_db:
            if _IM_DB_CONN is None:
                from src.kernel.mysql_compat import MySQLCompatDB, mysql_config
                _cfg = dict(mysql_config())
                _cfg["database"] = _im_db
                _u = (os.environ.get("GRAPE_IM_DB_USER") or "").strip()
                _p = os.environ.get("GRAPE_IM_DB_PASSWORD") or ""
                if _u:
                    _cfg["user"] = _u
                if _p:
                    _cfg["password"] = _p
                _IM_DB_CONN = MySQLCompatDB(**_cfg)
            return _SharedConn(_IM_DB_CONN)
        return _SharedConn(_shared_db())
    # Issue #3588：SQLite 短连接包 stale-aware 代理——overlayfs 下 WAL/SHM 被删旧
    # 建新替换时，execute 捕获 disk I/O error 等失效异常后自动换新连接重放语句，
    # outbox 仓储函数（enqueue/claim_batch/mark_* 等）零改动获得失效重连。
    # 连接打开失败仍按 base 行为留痕后抛出（保留 #3251 的可观测语义）。
    try:
        from src.kernel.db_health import SelfHealingSQLiteDB

        def _new_conn():
            c = sqlite3.connect(db_path)
            c.execute("PRAGMA busy_timeout=5000")
            return c

        return SelfHealingSQLiteDB(_new_conn, db_path)
    except sqlite3.Error as e:
        _note_db_open_failure(db_path, e)
        raise


def _is_mysql_backend() -> bool:
    """当前是否 MySQL 后端（延迟探测，避免模块导入期的环形依赖）。

    口径与 src.kernel.session_manager._is_mysql_backend 完全一致：
    env DEVOPS_DB_BACKEND > configs/db-backend.json > 默认 sqlite。
    """
    global _MYSQL_BACKEND_CACHE
    if _MYSQL_BACKEND_CACHE is None:
        try:
            from src.kernel.session_manager import _is_mysql_backend as _probe
            _MYSQL_BACKEND_CACHE = bool(_probe())
        except Exception:
            _MYSQL_BACKEND_CACHE = False
    return _MYSQL_BACKEND_CACHE


def _reset_backend_cache() -> None:
    """清掉后端探测缓存（仅供测试在切换 DEVOPS_DB_BACKEND 后调用）。"""
    global _MYSQL_BACKEND_CACHE
    _MYSQL_BACKEND_CACHE = None
_IM_DB_CONN = None  # IM 域库独立连接（GRAPE_IM_DB_NAME 覆盖时）


def _shared_db():
    from src.kernel.session_manager import db
    return db


class _SharedConn:
    """session_manager.db 的只读代理：接口同 sqlite3.Connection 最小子集，close() 空操作。"""

    __slots__ = ("_db",)

    def __init__(self, db) -> None:
        self._db = db

    def execute(self, sql, params=None):
        return self._db.execute(sql, params)

    def executescript(self, sql):
        return self._db.executescript(sql)

    def cursor(self):
        return self._db.cursor()

    def commit(self):
        return self._db.commit()

    def rollback(self):
        return self._db.rollback()

    def close(self) -> None:  # 共享单例不能关
        return None


def ensure_channel_schema(conn) -> None:
    """建/补齐 IM 渠道族 7 表（幂等）。db_setup 启动建表与引擎启动迁移共用同一份 DDL。

    渠道族表归 mgmt（configs/table_ownership.json）：非 owner 域进程直接返回，
    避免在三库拓扑下把表建进自己的库。返回值无意义，调用方只依赖幂等性。
    """
    from src.kernel import table_ownership as _own
    if _own.should_skip("grape_t_channel_installation"):
        return
    _load_migration_module().ensure_channel_tables(conn)


def ensure_notify_outbox_schema(conn) -> list:
    """幂等补 outbox 状态列（Issue #2261 迁移 004）。

    4 进程口径：挂在 db_setup 的 ensure_channel_schema 旁边，四个进程启动都会跑——
    与引擎开关解耦（A 进程 mode=off 时 run_startup_migration 不会执行，B/C 进程仍要能入队）。
    """
    mig004 = _load_migration_004()
    added = mig004.ensure_notify_outbox_columns(conn) if mig004 is not None else []
    # 005：保留期清理/未送达查询所需的 (status, updated_at) 索引（幂等，失败只告警）
    try:
        mig005 = _load_migration_005()
        if mig005 is not None:
            mig005.ensure_purge_index(conn)
    except Exception as e:
        log.warning("[startup-migration] 005 purge index failed: %s", e)
    return added


# ---------------------------------------------------------------- UI 写入桥（Issue #871 写方向反转）

def _legacy_installation_key(scope: str, owner_user_id: str, repo_key: str) -> str:
    """与 001 迁移/唯一索引同口径的归属键：repo 轨用 repo_key，user 轨用 owner_user_id。"""
    return repo_key if scope == "repo" else str(owner_user_id)


_feishu_api_cache: dict = {}


async def _close_api_quietly(api, app_id: str = "") -> None:
    """关闭装配层持有的平台 api（FeishuAsync 等）——失败只 WARNING，绝不抛。

    Issue #3735：FeishuAsync 懒建 `aiohttp.ClientSession`，装配层丢弃引用而不 close
    时 session 只能等 GC，由 aiohttp 在回收时打 `Unclosed client session`（fd /
    连接池随渠道重建次数累积）。收尾关闭属「关灯动作」，不能反向影响渠道切换与主流程。
    """
    close = getattr(api, "close", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception as e:
        log.warning("[im-engine] im api close failed app=%s: %r", app_id, e)


def _schedule_api_close(api, app_id: str = "") -> None:
    """在运行中的 loop 上异步关闭 api（同步装配点用，不阻塞扫描/建连）。

    无运行 loop（如 UI 线程直改缓存）→ 不调度：跨 loop await aiohttp session 会报
    "attached to a different loop"，此时留给 `ImEngine.stop()` 统一收尾。
    """
    if api is None or getattr(api, "close", None) is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_close_api_quietly(api, app_id))


def feishu_api(app_id: str = ""):
    """按 app_id 取（缓存的）FeishuAsync 客户端（旧 IM 下线 A2 M2）。

    通知/推送类功能原先借 legacy BotManager 的 `bm.api`，现在统一从新表拿凭证后
    直接用 API 客户端；同一进程内按 app_id 复用一份（aiohttp session 绑定主循环）。
    """
    api = _feishu_api_cache.get(app_id)
    if api is None:
        from src.domains.runtime.im.feishu_async import FeishuAsync
        api = FeishuAsync()
        _feishu_api_cache[app_id] = api
    return api


def get_active_installation(scope: str = "user", *, owner_user_id: str = "", repo_key: str = "",
                            channel_type: str = "", app_id: str = "") -> Optional[dict]:
    """读 grape_t_channel_installation 的活跃安装（旧 IM 下线 A2 M2）。

    业务侧（如 web→飞书推送）原本经 legacy BotManager 拿 ctx/api，现在统一从这里取
    安装凭证；返回 None 表示该轨没有活跃安装。
    ``app_id`` 非空时按安装的 legacy_app_id 精确过滤——同一仓库同渠道绑定多个 bot 时，
    避免取「最新活跃安装」而与目标群/绑定不匹配（Issue #2432）。
    """
    scope = scope or "user"
    key = _legacy_installation_key(scope, owner_user_id, repo_key)
    try:
        conn = _connect(default_db_path())
        try:
            row = _mgmt_public.installation_active_row(conn, scope, key, channel_type)
            if not row:
                return None
            try:
                cfg = json.loads(row[5] or "{}")
            except Exception:
                cfg = {}
            return {"installation_id": row[0], "channel_type": row[1], "scope": row[2],
                    "owner_user_id": row[3], "repo_key": row[4], "config": cfg}
        finally:
            conn.close()
    except Exception as e:
        log.warning("get_active_installation failed (scope=%s): %s", scope, e)
        return None


def sync_installation(scope: str, *, owner_user_id: str = "", repo_key: str = "",
                      app_id: str = "", app_secret: str = "",
                      channel_type: str = "feishu",
                      extra_config: Optional[dict] = None) -> bool:
    """UI 保存/换 app 统一写入口：**只写 grape_t_channel_installation**（旧 IM 下线 A2 M2）。

    旧的双轨反向回写（chat_agent_session / devops_agent_session / global_im_bot_config
    全量镜像）已下线；本函数只做一件事：
      幂等 upsert 新表（secret 快照进 config），状态置 active —— 引擎 ≤30s 扫描接管。
    A2 M3 起租约落在独立的 grape_t_channel_lease，不再需要往 legacy 表登记"租约载体"行。

    secret 兜底链：入参 → 新表已有 config；都空则不建行（空秘密不算可用安装）。
    失败只告警，不阻断 UI 主流程。
    extra_config：渠道专属 config 键（Slack 双令牌 bot_token/app_token 等），合并落库不覆盖标准键。
    """
    if not app_id:
        return False
    scope = scope or "user"
    key = _legacy_installation_key(scope, owner_user_id, repo_key)
    try:
        conn = _connect(default_db_path())
        try:
            _cfg_raw = _mgmt_public.installation_config_by_app(conn, app_id, scope, key)
            existing = {}
            if _cfg_raw:
                try:
                    existing = json.loads(_cfg_raw) or {}
                except Exception:
                    existing = {}
            secret = app_secret or str(existing.get("app_secret", ""))
            if not secret:
                return False
            installation_config: dict = {"app_id": app_id, "app_secret": secret}
            for cfg_key, value in (extra_config or {}).items():
                if value and cfg_key not in installation_config:
                    installation_config[cfg_key] = value
            _load_migration_module().upsert_installation(
                conn, channel_type, scope, str(owner_user_id or ""), repo_key or "",
                installation_config, app_id)
            _mgmt_public.installation_activate(conn, app_id, scope, key)
            conn.commit()
        finally:
            conn.close()
        # 写成功后立刻唤醒扫描：否则新渠道要等满一个 30s 窗口才建连
        # （企微首条通知就撞 "long connection is not established"）。
        kick_engine_scan()
        return True
    except Exception as e:
        log.warning("grape_t_channel_installation sync failed (scope=%s app=%s): %s",
                    scope, _safe_app_ref(app_id, channel_type), e)
        return False


def revoke_installation(scope: str, *, owner_user_id: str = "", repo_key: str = "",
                        app_id: str = "", channel_type: str = "feishu") -> bool:
    """UI 解绑/换 app：grape_t_channel_installation 置 revoked（不硬删，留审计线索）。

    旧 IM 下线 A2 M2/M3 后只动 grape_t_channel_installation：不再回写 legacy 双轨表、也不再清理
    legacy 登记行（A2 收尾起独占校验改查安装行，登记表已无写入方）。
    新引擎扫描只枚举 status='active'，置 revoked 后下一轮扫描（≤30s）即优雅让渡。
    """
    if not app_id:
        return False
    scope = scope or "user"
    key = _legacy_installation_key(scope, owner_user_id, repo_key)
    try:
        conn = _connect(default_db_path())
        try:
            _n = _mgmt_public.installation_revoke(conn, channel_type, scope, app_id, key)
            conn.commit()
        finally:
            conn.close()
        # 解绑也要立刻收敛：让渡/停连接不必等扫描窗口
        if _n > 0:
            kick_engine_scan()
        return _n > 0
    except Exception as e:
        log.warning("grape_t_channel_installation revoke failed (scope=%s app=%s): %s",
                    scope, _safe_app_ref(app_id, channel_type), e)
        return False


# ---------------------------------------------------------------- sqlite 仓储（同步、每操作新连接）

class SqliteDedupStore:
    """grape_t_channel_inbound_dedup 两阶段去重持久化（DedupStore 协议实现）。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self):
        return _connect(self._db_path)

    def seen(self, installation_id: str, message_id: str) -> bool:
        """去重位查询（渠道表归 mgmt，连接由 owner 原语按拓扑选）。"""
        try:
            return _mgmt_public.inbound_dedup_seen(installation_id, message_id)
        except _DB_ERRORS:
            return False

    def try_claim(self, installation_id: str, message_id: str, token: str) -> bool:
        try:
            return _mgmt_public.inbound_dedup_claim(installation_id, message_id, token)
        except _DB_ERRORS:
            return False

    def mark(self, installation_id: str, message_id: str) -> bool:
        try:
            return _mgmt_public.inbound_dedup_set_state(installation_id, message_id, "marked")
        except _DB_ERRORS:
            return False

    def release(self, installation_id: str, message_id: str) -> None:
        try:
            _mgmt_public.inbound_dedup_release(installation_id, message_id)
        except _DB_ERRORS:
            pass


class SqliteBindingStore:
    """grape_t_channel_chat_session_binding 落库（BindingStore 协议实现）。

    合并 repo_im_group_binding + vod_conversations 语义（Issue #742 §5.1）。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def get(self, installation_id: str, key: str) -> Optional[SessionBinding]:
        try:
            row = _mgmt_public.chat_binding_get(installation_id, key)
        except _DB_ERRORS:
            return None
        if not row:
            return None
        return SessionBinding(
            installation_id=row[0],
            binding_key=row[1],
            chat_type=row[2] or "p2p",
            scope=Scope(row[3] or "user"),
            repo_key=row[4] or "",
            agent_type=row[5] or "chat",
            runtime_session_id=row[6] or "",
            config=json.loads(row[7] or "{}"),
        )

    def upsert(self, binding: SessionBinding, channel_type: str = "feishu") -> SessionBinding:
        """channel_type：绑定行渠道列（Issue #936 P2 钉钉接入后真实化，默认 feishu 兼容旧调用）。"""
        scope_value = binding.scope.value if hasattr(binding.scope, "value") else str(binding.scope)
        # 绑定行归 mgmt：定位 + 更新/插入两段式在 owner 域内同一事务完成（原先是
        # sqlite 专有的 ON CONFLICT(id) DO UPDATE，MySQL 下不可用）
        _mgmt_public.chat_binding_upsert(
            binding.installation_id, binding.binding_key, str(uuid.uuid4()), channel_type,
            binding.chat_type, scope_value, binding.repo_key, binding.agent_type,
            binding.runtime_session_id, json.dumps(binding.config, ensure_ascii=False),
            _im_now())
        return binding


class _TypedBindingStore:
    """多平台绑定表包装（Issue #936 P2）：upsert 落真实 channel_type（feishu 之外的平台）。

    Router 协议（get/upsert）不变；绑定行 grape_t_channel_chat_session_binding.channel_type
    与安装渠道一致，双安装会话隔离按 (installation_id, channel_chat_id) 主键天然成立。
    """

    def __init__(self, inner, channel_type: str) -> None:
        self._inner = inner
        self._type = channel_type

    def get(self, installation_id: str, key: str) -> Optional[SessionBinding]:
        return self._inner.get(installation_id, key)

    def upsert(self, binding: SessionBinding) -> SessionBinding:
        return self._inner.upsert(binding, channel_type=self._type)


def _append_message_sync(db_path: str, installation_id: str, msg: InboundMessage) -> None:
    """⑦ 持久化 + dedup mark 同一事务（崩溃重投不丢不重）。worker 线程内执行。"""
    mid = msg.message_id or msg.event_id
    # 落库 + 去重位推进必须同事务（崩溃重投不丢不重）：两句一起下沉到 owner 域，
    # 事务边界由原语持有（原实现在 sqlite 下手工 BEGIN、MySQL 下靠 autocommit=False）
    try:
        _mgmt_public.inbound_message_save_and_mark(
            installation_id, mid, msg.source.chat_id, msg.source.sender_id, msg.text or "")
    except _DB_ERRORS:
        pass


# ---------------------------------------------------------------- 身份解析

def _lookup_user_sync(db_path: str, installation_id: str, open_id: str,
                      channel_type: str = "feishu") -> str:
    """sender → grape 用户：grape_t_channel_sender_identity 优先，回退 legacy 统一映射。

    统一映射按渠道取列：钉钉是 dingtalk_user_id，其余维持 feishu_open_id 兼容旧行为。
    列名只来自白名单，不拼接外部输入。
    """
    if not open_id:
        return ""
    # 渠道身份表与统一映射表都归 mgmt：列名白名单（钉钉/飞书）在 owner 域内决定
    try:
        _uid = _mgmt_public.sender_identity_user(installation_id, open_id)
        if _uid:
            return _uid
    except _DB_ERRORS:
        pass
    try:
        _uid = _mgmt_public.mapped_user_by_channel(open_id, channel_type)
        if _uid:
            return _uid
    except _DB_ERRORS:
        pass
    return ""


_BIND_WORDS = ("/绑定", "/bind me", "/bindme", "绑定我", "绑定账号")


def _bind_identity_sync(db_path: str, installation_id: str, scope: str,
                        owner_user_id: str, platform_user_id: str,
                        stable_id: str = "") -> str:
    """平台用户 → 安装 owner 的身份绑定（user 轨自助绑定）。

    只允许 user 轨（自己的机器人）；repo 轨群装不做个人身份绑定。
    返回绑定的 grape 用户 id，失败/不允许返回空串。
    """
    if scope != "user" or not str(owner_user_id) or not platform_user_id:
        return ""
    try:
        _mgmt_public.sender_identity_bind(
            installation_id, platform_user_id, stable_id, str(owner_user_id))
        return str(owner_user_id)
    except _DB_ERRORS as e:
        log.warning("im engine bind identity failed: %r", e)
        return ""


def _update_installation_config_sync(db_path: str, app_id: str,
                                       mutate: Callable[[dict], None]) -> bool:
    """按 config.app_id 定位活跃安装行并就地更新 config（offset 等运行时状态）。"""
    # 安装表归 mgmt：连接由原语按拓扑选（原先用 IM 库的 _connect()，
    # 三库独立后会查到 runtime 库、必然 1146）
    try:
        rows = _mgmt_public.installation_active_configs_any()
    except _DB_ERRORS:
        return False
    for row in rows:
        try:
            config = json.loads(row[1] or "{}")
        except Exception:
            config = {}
        if str(config.get("app_id", "")) != app_id:
            continue
        mutate(config)
        try:
            _mgmt_public.installation_set_config_any(
                json.dumps(config, ensure_ascii=False), row[0])
        except _DB_ERRORS:
            return False
        return True
    return False


def _telegram_offset_loader(db_path: str, app_id: str) -> Callable[[], int]:
    """读取持久化的 getUpdates offset（重启后从 last+1 继续，不重投）。"""
    def load() -> int:
        inst = _lookup_installation_by_app_sync(db_path, app_id)
        config = (inst or {}).get("config") or {}
        try:
            return int(config.get("telegram_offset", 0) or 0)
        except Exception:
            return 0
    return load


def _telegram_offset_persist(db_path: str, app_id: str) -> Callable[[int], None]:
    def persist(value: int) -> None:
        def mutate(config: dict) -> None:
            config["telegram_offset"] = int(value)
        _update_installation_config_sync(db_path, app_id, mutate)
    return persist


def _wechat_cursor_loader(db_path: str, app_id: str) -> Callable[[], str]:
    """读取持久化的 getupdates 游标（`get_updates_buf`，重启续接不重投）。"""
    def load() -> str:
        inst = _lookup_installation_by_app_sync(db_path, app_id)
        config = (inst or {}).get("config") or {}
        return str(config.get("cursor", "") or "")
    return load


def _wechat_cursor_persist(db_path: str, app_id: str) -> Callable[[str], None]:
    def persist(value: str) -> None:
        def mutate(config: dict) -> None:
            config["cursor"] = str(value or "")
        _update_installation_config_sync(db_path, app_id, mutate)
    return persist


def _lookup_installation_by_app_sync(db_path: str, app_id: str) -> Optional[dict]:
    """grape_t_channel_installation 活跃行里按 config.app_id 找安装（user 轨唯一；repo 轨取首行）。"""
    # 同上：安装表归 mgmt，连接由原语按拓扑选
    try:
        rows = _mgmt_public.installation_active_installs_any()
    except _DB_ERRORS:
        return None
    for row in rows:
        try:
            config = json.loads(row[4] or "{}")
        except Exception:
            config = {}
        if str(config.get("app_id", "")) == app_id:
            return {
                "installation_id": row[0], "scope": row[1], "owner_user_id": row[2],
                "repo_key": row[3], "config": config, "status": row[5],
            }
    return None


# ---------------------------------------------------------------- 装配

def _spawn(coro) -> None:
    """fire-and-forget（A4/A7 纪律）：出站不阻塞入站流水线。"""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return

    def _log(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("im engine outbound task failed: %r", exc)

    task.add_done_callback(_log)


def _raw_card_context(msg: InboundMessage) -> tuple:
    """卡片回调上下文：(真实回复目标 message_id, action_value, action_token)。"""
    reply_id = msg.reply_to.message_id if msg.reply_to else ""
    raw = msg.raw if isinstance(msg.raw, dict) else {}
    ev = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    ctx_ = ev.get("context") if isinstance(ev.get("context"), dict) else {}
    if not reply_id:
        reply_id = str(ctx_.get("open_message_id") or "")
    value = None
    if isinstance(ev.get("action"), dict):
        v = ev["action"].get("value")
        value = v if isinstance(v, dict) else None
    if value is None and isinstance(raw.get("action_value"), dict):
        value = raw["action_value"]
    token = str(ev.get("token") or raw.get("token") or "")
    return reply_id, value, token[:200]


def _reply_msg_id(msg: InboundMessage) -> str:
    """回复/原地更新要用的消息 ID：卡片回调取 open_message_id，普通消息即 message_id。"""
    return _raw_card_context(msg)[0] or msg.message_id


def _msg_id_validator_for(channel_type: str):
    """回复目标可用性校验器（Issue #1008：om_ 前缀假设下沉适配器声明）。

    飞书：om_ 形态校验；钉钉：conversationId 路由恒可用（msgId 形态无关）。
    """
    if str(channel_type).lower() in ("dingtalk", "wecom", "slack", "wechat"):
        from src.domains.runtime.im.engine.egress import _always_valid
        return _always_valid
    from src.domains.runtime.im.engine.egress import _is_feishu_id_valid
    return _is_feishu_id_valid


def dingtalk_card_mode(raw: dict) -> tuple[str, str]:
    """钉钉卡片模式决策：(mode, template_id)。

    interactive 必须三件齐：模板 ID + 该安装 `cards_enabled` + 全局开关（运维灰度/急停）。
    任何一件缺失都回落 action_card（今天的"文字卡 + 回数字"），能力位不虚报。
    """
    raw = raw if isinstance(raw, dict) else {}
    cards = raw.get("dingtalk_cards")
    tpl = str((cards or {}).get("card") or "") if isinstance(cards, dict) else ""
    if not tpl:
        return "action_card", ""
    try:
        from src.domains.runtime.im.adapters.dingtalk.cards import card_mode_enabled
        switched_on = card_mode_enabled()
    except Exception:
        switched_on = False
    if not (raw.get("cards_enabled") and switched_on):
        return "action_card", tpl
    return "interactive", tpl


def _card_renderer_for(channel_type: str):
    """渠道 CardRenderer（Issue #1095）：声明不协商——每渠道一个实现，wiring 注入。"""
    from src.domains.runtime.im.channel.card import BUILTIN_CARD_RENDERERS
    if str(channel_type).lower() in BUILTIN_CARD_RENDERERS:
        if str(channel_type).lower() == "dingtalk":
            from src.domains.runtime.im.channel.renderers.dingtalk import DingtalkCardRenderer
            return DingtalkCardRenderer()
        if str(channel_type).lower() == "wecom":
            from src.domains.runtime.im.channel.renderers.wecom import WecomCardRenderer
            return WecomCardRenderer()
        if str(channel_type).lower() == "slack":
            # Issue #1309：Slack 是唯一「真回调 + 原地更新 + 线程」齐全的渠道
            from src.domains.runtime.im.channel.renderers.slack import SlackCardRenderer
            return SlackCardRenderer()
        if str(channel_type).lower() == "telegram":
            # Issue #1613：文本 + InlineKeyboardMarkup（callback_data 64B 短码）
            from src.domains.runtime.im.channel.renderers.telegram import TelegramCardRenderer
            return TelegramCardRenderer()
        if str(channel_type).lower() == "wechat":
            # Issue #1728：纯文本 + 编号选项（无按钮/无回调，数字选择复用 pending）
            from src.domains.runtime.im.channel.renderers.wechat import WechatCardRenderer
            return WechatCardRenderer()
        from src.domains.runtime.im.channel.renderers.feishu import FeishuCardRenderer
        return FeishuCardRenderer()
    return None


def _inbound_to_parsed(msg: InboundMessage) -> dict:
    """InboundMessage → legacy parsed dict 契约（handler_async 消费的字段集）。

    装配层翻译：媒体引用回填 image_key/file_key（引擎核心不参与）。
    """
    image_key = ""
    file_key = ""
    for ref in msg.media_refs or []:
        if ref.type == MsgType.IMAGE:
            image_key = ref.storage_key
        elif ref.type in (MsgType.FILE, MsgType.MEDIA):
            file_key = ref.storage_key
    parsed = {
        "type": "message",
        "chat_id": msg.source.chat_id,
        "chat_type": msg.source.chat_type.value,
        "sender_open_id": msg.source.sender_id,
        "sender_id": msg.source.sender_id,
        "sender_union_id": msg.source.sender_stable_id,
        "message_id": msg.message_id,
        "msg_type": msg.type.value if isinstance(msg.type, MsgType) else "text",
        "text": msg.text,
        "image_key": image_key,
        "file_key": file_key,
        "command_text": msg.command_text,
        "reply_to_message_id": (msg.reply_to.message_id if msg.reply_to else ""),
    }
    raw = msg.raw if isinstance(msg.raw, dict) else {}
    ev = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    ev_ctx = ev.get("context") if isinstance(ev.get("context"), dict) else {}
    # 卡片按钮回调：真实回复目标在 context.open_message_id（event_id 只用于去重），
    # 按钮上下文在 event.action.value，原地更新令牌在 event.token。
    reply_id = (msg.reply_to.message_id if msg.reply_to else "") or str(ev_ctx.get("open_message_id") or "")
    if reply_id:
        parsed["_reply_id"] = reply_id
    card_value = (ev.get("action") or {}).get("value") if isinstance(ev.get("action"), dict) else None
    if not isinstance(card_value, dict):
        card_value = raw.get("action_value")
    if isinstance(card_value, dict) and card_value:
        parsed["_card_value"] = card_value
    token = str(ev.get("token") or raw.get("token") or "")
    if token:
        parsed["_action_token"] = token[:200]
    return parsed


class ImEngine:
    """新引擎运行时句柄：装配 + 生命周期 + 对账。

    可注入项（默认 lazy 取 legacy 实现）供单测替换：
      ctx_factory / api_factory / process_chat / process_vod / process_command /
      handle_selection / get_pending_selection / lease_store。
    """

    def __init__(
        self,
        db_path: str,
        *,
        instance_id: str = "",
        registry: Optional[ChannelRegistry] = None,
        lease_store=None,
        api_factory: Optional[Callable] = None,
        ctx_factory: Optional[Callable] = None,
        process_chat: Optional[Callable] = None,
        process_vod: Optional[Callable] = None,
        process_command: Optional[Callable] = None,
        handle_selection: Optional[Callable] = None,
        get_pending_selection: Optional[Callable] = None,
        channel_builder: Optional[Callable] = None,
        scan_interval: float = 30.0,
    ) -> None:
        self.db_path = db_path
        # instance_id 必须逐实例唯一：固定名会让哈希环只剩一个节点，多实例互斥退化成
        # 抢锁抖动（2026-11-10 四主滚动升级实证：四台同名 im-engine-local，35 个安装
        # 只有 18 个拿到租约，其余 17 个无人建连）。留空则按 hostname-随机后缀生成。
        if not instance_id:
            from src.domains.runtime.im.bot_lease import generate_instance_id
            self.instance_id = generate_instance_id()
        else:
            self.instance_id = instance_id
        self._registry = registry
        self._lease_store = lease_store
        self._api_factory = api_factory
        self._ctx_factory = ctx_factory
        self._process_chat = process_chat
        self._process_vod = process_vod
        self._process_command = process_command
        self._handle_selection = handle_selection
        self._get_pending_selection = get_pending_selection
        self._channel_builder = channel_builder
        self._scan_interval = scan_interval
        self.supervisor: Optional[Supervisor] = None
        self._running = False
        # app_id → installation_id：repo 轨同一 app 只建一条 WS 连接的去重表。
        # 只记"当前真是活渠道"的（见 _prune_built_apps）；抢租约失败/渠道让渡后自动释放，
        # 不再像旧实现那样永久黑名单（移除后同 app 再加回来必须重启才能连）。
        self._built_apps: dict[str, str] = {}
        self._ctx_cache: dict[str, tuple] = {}
        # app_id → 平台侧 api 覆盖（钉钉：下载/身份走 DingtalkAdapterApi，不用 FeishuAsync）
        self._api_overrides: dict[str, object] = {}
        # Issue #1008 出站收敛：app_id → (Replier, channel_type)。
        # 渠道工厂建连时登记，_dispatch_agent 按消息构造 HandlerEgress 复用。
        self._repliers: dict[str, tuple] = {}
        self._dedup = InboundDedup(SqliteDedupStore(db_path))
        self._bindings = SqliteBindingStore(db_path)
        # Issue #870：legacy 对齐健康计数器（received/sent/failed/reconnects/flap）
        self._metrics = EngineMetrics()
        # Issue #3799：渠道健康状态机（ret=-14 风控暂停 → 状态/告警/降级共用同一真源）。
        # store 懒取 IM 域库连接（与 outbox/租约同源），渠道侧暂停/恢复时写入，
        # 管理台经 runtime public 读取。
        from src.domains.runtime.im.channel_health import ChannelHealthStore, ChannelHealthTracker
        self.health_tracker = ChannelHealthTracker(
            store=ChannelHealthStore(),
            metrics=self._metrics,
            instance_id=self.instance_id,
            installations_provider=self._channel_installations,
        )

    def _channel_installations(self) -> list:
        """安装全集（判「全实例均暂停」用；读失败 → 空列表，退化为单实例阈值）。"""
        try:
            from src.domains.runtime.im.engine.installations import _list_installations_with_factory
            return _list_installations_with_factory(lambda: _connect(self.db_path))
        except Exception as e:
            log.warning("[im-engine] health installations snapshot failed: %s", e)
            return []

    # ---------------- 生命周期

    async def start(self) -> None:
        if self._running:
            return
        stats = await asyncio.to_thread(run_startup_migration, self.db_path)
        log.info("IM engine: migration ok (user=%s repo=%s)",
                 stats.get("user", 0), stats.get("repo", 0))

        registry = self._registry or default_registry()
        lease = self._lease_store
        if lease is None:
            from src.domains.runtime.im.bot_lease import BotLeaseStore, generate_instance_id
            self.instance_id = self.instance_id or generate_instance_id()
            lease = BotLeaseStore(instance_id=self.instance_id)

        from src.domains.runtime.im.engine.installations import SqliteInstallationProvider
        provider = SqliteInstallationProvider(lambda: _connect(self.db_path))
        self.supervisor = Supervisor(
            registry=registry,
            instance_id=self.instance_id,
            lease_store=lease,
            installation_provider=provider,
            scan_interval=self._scan_interval,
            metrics=self._metrics,
        )
        registry.register(Type.FEISHU, self._feishu_factory)
        # Issue #936 P2：钉钉接入 = 注册工厂（引擎核心零改动）
        registry.register(Type.DINGTALK, self._dingtalk_factory)
        # Issue #1254：企微智能机器人（长连接）接入 = 同款注册，引擎核心零改动
        registry.register(Type.WECOM, self._wecom_factory)
        # Issue #1309：Slack（Socket Mode + Web API）接入 = 同款注册，引擎核心零改动
        registry.register(Type.SLACK, self._slack_factory)
        # Issue #1613：Telegram（getUpdates 长轮询 + Bot HTTP API）接入 = 同款注册
        registry.register(Type.TELEGRAM, self._telegram_factory)
        # Issue #1728：微信 ilink Bot（getupdates 长轮询 + 扫码绑定）接入 = 同款注册
        registry.register(Type.WECHAT, self._wechat_factory)
        # Issue #1095：启动自检——声明 RICH_CARD 必有 CardRenderer（告警不阻断）
        try:
            from src.domains.runtime.im.adapters.dingtalk.outbound import DINGTALK_CAPABILITIES
            from src.domains.runtime.im.adapters.feishu.outbound import FEISHU_CAPABILITIES
            from src.domains.runtime.im.adapters.slack.outbound import SLACK_CAPABILITIES
            from src.domains.runtime.im.adapters.telegram.outbound import TELEGRAM_CAPABILITIES
            from src.domains.runtime.im.adapters.wechat.outbound import WECHAT_CAPABILITIES
            from src.domains.runtime.im.adapters.wecom.outbound import WECOM_CAPABILITIES
            problems = self_check_card_capabilities({
                "feishu": FEISHU_CAPABILITIES, "dingtalk": DINGTALK_CAPABILITIES,
                "wecom": WECOM_CAPABILITIES, "slack": SLACK_CAPABILITIES,
                "telegram": TELEGRAM_CAPABILITIES, "wechat": WECHAT_CAPABILITIES})
            for problem in problems:
                log.warning("[im-engine] card capability self-check: %s", problem)
        except Exception as e:
            log.warning("[im-engine] card capability self-check failed: %s", e)
        self._running = True
        await self.supervisor.start()
        log.info("IM engine: started (instance_id=%s)", self.instance_id)

    async def stop(self) -> None:
        self._running = False
        if self.supervisor:
            await self.supervisor.stop()
            self.supervisor = None
        self._built_apps.clear()
        # Issue #3735：渠道全停后关闭装配层缓存的 FeishuAsync session（不能只 clear，
        # 否则 aiohttp 在 GC 时才打 Unclosed client session）。
        await self._close_cached_apis()
        self._repliers.clear()
        # Issue #2261 A：注销全局注册点（引擎停止后 notify 不得再取到死句柄）
        if get_current_engine() is self:
            set_current_engine(None)
        log.info("IM engine: stopped")

    def kick_scan(self) -> bool:
        """立刻唤醒 Supervisor 扫描（凭证保存/解绑后秒级建连，不等 30s 窗口）。"""
        sup = self.supervisor
        kick = getattr(sup, "kick", None)
        if kick is None:
            return False
        try:
            return bool(kick())
        except Exception as e:
            log.warning("[im-engine] kick_scan failed: %s", e)
            return False

    def health(self) -> dict:
        sup = self.supervisor
        snap = self._metrics.snapshot()
        return {
            "engine": "on",
            "instance_id": self.instance_id,
            "running": self._running,
            "channels": len(sup._channels) if sup else 0,
            # Issue #1008：出站模式（replier/legacy）+ 降级计数（/health/realtime im 段）
            "egress_mode": egress_mode(),
            # Issue #870：legacy 对齐指标（缺失语义 = 0，不省略键）
            **snap,
        }

    # ---------------- legacy 依赖（lazy，可注入替身）

    def _ctx_for(self, inst: dict) -> tuple:
        app_id = str((inst.get("config") or {}).get("app_id", ""))
        cached = self._ctx_cache.get(app_id)
        if cached is not None:
            return cached
        if self._ctx_factory is not None:
            pair = (self._ctx_factory(inst), self._api_factory() if self._api_factory else None)
        else:
            from src.domains.runtime.im.feishu_async import BotContext, FeishuAsync
            scope = inst.get("scope", "user")
            bot_type = "vod_bot" if scope == "repo" else "chat_bot"
            ctx = BotContext(
                app_id=app_id,
                app_secret=str((inst.get("config") or {}).get("app_secret", "")),
                bot_type=bot_type,
            )
            api = (self._api_overrides.get(app_id)
                   or (FeishuAsync() if (self._api_factory is None) else self._api_factory()))
            pair = (ctx, api)
        self._ctx_cache[app_id] = pair
        return pair

    def _dispatch_agent(self, inst: dict, msg: InboundMessage) -> Awaitable:
        """grape 双轨路由：ctx.bot_type（scope 决定）→ chat / vod handler。

        Issue #1008：egress_mode=replier 时 handler 收到 HandlerEgress 门面
        （出站统一 OutboundMessage → Replier → Channel.send，核心零平台名）；
        legacy 模式透传 legacy api（与改造前一致，秒级回滚）。
        """
        ctx, api = self._ctx_for(inst)
        parsed = _inbound_to_parsed(msg)
        handler_arg = self._handler_arg(inst, ctx, api, msg)
        if ctx.bot_type == "vod_bot":
            fn = self._process_vod or self._legacy_process_vod()
            return fn(ctx, handler_arg, parsed)
        fn = self._process_chat or self._legacy_process_chat()
        return fn(ctx, handler_arg, parsed)

    def _handler_arg(self, inst: dict, ctx, api, msg: InboundMessage | None = None):
        """handler 出站参数：replier 模式给 HandlerEgress，legacy 给平台 api。"""
        if egress_mode() != "replier":
            return api
        route = msg.source.chat_id if msg is not None else ""
        return self._egress_for(inst, ctx, api, route=route)

    def _egress_for(self, inst: dict, ctx, api, route: str = "") -> HandlerEgress:
        """按安装构造 HandlerEgress（每次分发新建：default_route 是消息级路由）。

        replier 未就绪（渠道未建连/注入替身）→ HandlerEgress 自动降级 legacy 透传，
        出站行为不因引擎装配顺序而缺。
        """
        app_id = str((inst.get("config") or {}).get("app_id", ""))
        entry = self._repliers.get(app_id)
        replier = entry[0] if entry else None
        channel_type = entry[1] if entry else "feishu"
        return HandlerEgress(api, replier, metrics=self._metrics,
                             msg_id_validator=_msg_id_validator_for(channel_type),
                             default_route=route,
                             card_renderer=_card_renderer_for(channel_type))

    def _legacy_process_chat(self):
        from src.domains.runtime.im.handler_async import process_chat_async
        return process_chat_async

    def _legacy_process_vod(self):
        from src.domains.runtime.im.vod_handler_async import process_vod_async
        return process_vod_async

    def _legacy_process_command(self):
        from src.domains.runtime.im.handler_async import process_command_async
        return process_command_async

    def _legacy_handle_selection(self):
        from src.domains.runtime.im.handler_async import _handle_selection
        return _handle_selection

    def _legacy_get_pending_selection(self):
        from src.domains.runtime.im.handler_async import _get_pending_selection
        return _get_pending_selection

    # ---------------- 渠道工厂（per installation Router 六注入点，多平台通用）

    async def _close_cached_apis(self) -> None:
        """关闭并清空装配层缓存的所有平台 api（Issue #3735 生命周期收尾）。

        覆盖两处缓存：`_ctx_cache`（handler 出站/媒体用的 ctx/api 对，api 可能是
        FeishuAsync）与进程级 `_feishu_api_cache`（通知/推送共享客户端）。
        逐个 await 关闭——关闭失败由 `_close_api_quietly` 降级 WARNING，不阻断 stop。
        """
        pairs = list(self._ctx_cache.values())
        self._ctx_cache.clear()
        for pair in pairs:
            if not (isinstance(pair, tuple) and len(pair) > 1):
                continue
            ctx, api = pair[0], pair[1]
            await _close_api_quietly(api, str(getattr(ctx, "app_id", "") or ""))
        for app_id, api in list(_feishu_api_cache.items()):
            _feishu_api_cache.pop(app_id, None)
            await _close_api_quietly(api, app_id)

    def _drop_cached_ctx(self, app_id: str) -> None:
        """丢掉某 app 的 (ctx, api) 缓存并收尾关闭其 session（Issue #3735）。

        调用点都是「凭证覆盖重建」：换新 api 后旧 FeishuAsync 的 session 不再有人用，
        必须关闭；调度成 task 异步执行（调用点在渠道工厂/扫描 loop 内，不能 await）。
        """
        pair = self._ctx_cache.pop(app_id, None)
        if pair is None:
            return
        api = pair[1] if isinstance(pair, tuple) and len(pair) > 1 else None
        _schedule_api_close(api, app_id)

    def _prune_built_apps(self) -> None:
        """把去重表收敛到"当前活着的渠道"：渠道不在 Supervisor 里就释放该 app_id。

        旧实现是纯记账（建渠道即 add、永不移除），一旦工厂建完渠道后抢租约失败
        （例如 global_im_bot_config 还没这行），这个 app_id 就被永久拉黑，之后每次
        扫描 registry 都"静默拒绝"，直到进程重启——表现为渠道删了再加回来不重连。
        现在以 Supervisor._channels（installation_id 集合）为准，失败/让渡/热重建
        都会自然把 app_id 释放，下一轮扫描可重试。
        """
        sup = self.supervisor
        if not self._built_apps:
            return
        live = set(sup._channels) if sup is not None else set()
        for app_id, inst_id in list(self._built_apps.items()):
            if inst_id not in live:
                del self._built_apps[app_id]
                # Issue #3735：渠道已让渡/重建 → 该 app 缓存的 FeishuAsync session
                # 不再有人用，必须收尾关闭（否则每次重建漏一个 ClientSession）。
                self._drop_cached_ctx(app_id)

    def _feishu_factory(self, cfg: ChannelConfig):
        from src.domains.runtime.im.adapters.feishu.outbound import FeishuSender

        def _build_channel(cfg_, sender):
            from src.domains.runtime.im.adapters.feishu.channel import FeishuChannel
            return FeishuChannel(cfg_, sender)

        return self._channel_factory(cfg, "feishu", FeishuSender, _build_channel)

    def _dingtalk_factory(self, cfg: ChannelConfig):
        """钉钉工厂（Issue #936 P2）：与飞书同一套 Router 装配，仅渠道实现/发送器不同。"""
        from src.domains.runtime.im.adapters.dingtalk.outbound import DingtalkSender

        def _build_channel(cfg_, sender):
            from src.domains.runtime.im.adapters.dingtalk.channel import DingtalkChannel
            return DingtalkChannel(cfg_, sender)

        return self._channel_factory(cfg, "dingtalk", DingtalkSender, _build_channel)

    def _wecom_factory(self, cfg: ChannelConfig):
        """企微工厂（Issue #1254）：与钉钉同族（长连接），仅渠道/发送器实现不同。

        出站不走 legacy 传输闭包：WecomSender 直接持有本安装的 WecomTransport
        （channel 建连后 attach 同一条 WS），因此经 sender_factory 注入；
        媒体入站已在收帧时下载解密，handler 侧 api 覆盖为缓存读取。
        """
        from src.domains.runtime.im.adapters.wecom.cards import CardLedger
        from src.domains.runtime.im.adapters.wecom.channel import WecomChannel
        from src.domains.runtime.im.adapters.wecom.outbound import (
            WecomAdapterApi,
            WecomReplyRegistry,
            WecomSender,
            WecomTransport,
        )
        from src.domains.runtime.im.adapters.wecom.resolvers import WecomMediaResolver

        bot_id = str(cfg.raw.get("bot_id") or cfg.raw.get("app_id") or "")
        transport = WecomTransport(bot_id)
        reply_registry = WecomReplyRegistry()   # 回复令牌 + 会话类型台账（sender/channel 共用）
        media_resolver = WecomMediaResolver()
        # 卡片台账按安装独立：多 bot 的 task_id 不互相淘汰（默认单例会跨安装共享）
        card_ledger = CardLedger()

        engine = self

        def _make_sender(app_id: str, raw: dict):
            # handler 的下载能力（媒体两跳第一跳已在收帧完成）→ 读进程内缓存
            sender = WecomSender(transport, reply_registry, ledger=card_ledger)
            # 主动推送（无回复令牌）也要走同一个发送器：企微没有 card-to-chat 通道，
            # 缺 send_card_to_chat 会让"绑定引导卡/未绑定卡"抛 AttributeError（11-06 实障）。
            engine._api_overrides[app_id] = WecomAdapterApi(media_resolver.cache, sender=sender)
            engine._drop_cached_ctx(app_id)   # 覆盖后重建 ctx/api 对（旧 api 收尾关闭）
            return sender

        def _build_channel(cfg_, sender):
            return WecomChannel(cfg_, sender, transport=transport,
                                reply_registry=reply_registry,
                                media_resolver=media_resolver)

        return self._channel_factory(cfg, "wecom", None, _build_channel,
                                     sender_factory=_make_sender)

    def _slack_factory(self, cfg: ChannelConfig):
        """Slack 工厂（Issue #1309）：Socket Mode（xapp）+ Web API（xoxb）。

        与飞书/钉钉/企微同一套 Router 装配（`_channel_factory`）：
          - 接入用 App Token（`apps.connections.open`），出站用 Bot Token（chat.postMessage）；
          - 媒体入站只存 `url_private_download` 引用，handler 侧经 SlackAdapterApi 带 xoxb 下载；
          - `ts → channel` 台账由 channel 与 api 共用（chat.update / reactions.add 只有 ts）。

        凭证落库沿用既有「一 app 一安装」链路：`im_app_id` = Bot Token、`im_app_secret` =
        App Token；config 里同时补齐 `bot_token`/`app_token` 语义键与 `app_id`/`app_secret`
        兼容键（`_channel_factory` 以 app_id 做安装反查与去重）。
        """
        from src.domains.runtime.im.adapters.slack.channel import SlackChannel
        from src.domains.runtime.im.adapters.slack.outbound import SlackAdapterApi, SlackClient, SlackSender
        from src.domains.runtime.im.adapters.slack.resolvers import (
            SlackMediaFetcher,
            SlackRouteRegistry,
            SlackUserResolver,
        )

        raw = dict(cfg.raw or {})
        bot_token = str(raw.get("bot_token") or raw.get("app_id") or "")
        app_token = str(raw.get("app_token") or raw.get("app_secret") or "")
        raw.update({
            "bot_token": bot_token,
            "app_token": app_token,
            "app_id": bot_token,
            "app_secret": app_token,
        })
        cfg.raw = raw

        client = SlackClient(bot_token)
        route_registry = SlackRouteRegistry()
        engine = self

        def _make_sender(app_id: str, _raw: dict):
            sender = SlackSender(client)
            media = SlackMediaFetcher(
                bot_token, http_get=lambda url, _token: client.download(url))
            users = SlackUserResolver(
                fetch=lambda uid: client.call("users.info", {"user": uid}))
            engine._api_overrides[app_id] = SlackAdapterApi(
                bot_token, client=client, sender=sender,
                route_registry=route_registry, media=media, users=users)
            engine._drop_cached_ctx(app_id)   # 覆盖后重建 ctx/api 对（旧 api 收尾关闭）
            return sender

        def _build_channel(cfg_, sender):
            return SlackChannel(cfg_, sender, route_registry=route_registry)

        return self._channel_factory(cfg, "slack", None, _build_channel,
                                     sender_factory=_make_sender)

    def _telegram_factory(self, cfg: ChannelConfig):
        """Telegram 工厂（Issue #1613）：getUpdates 长轮询（单 Bot Token）。

        与飞书/钉钉/企微/Slack 同一套 Router 装配（`_channel_factory`）：
          - 单 Bot Token 位存 `im_app_id`；config 补齐 `bot_token` 语义键；
          - 入站媒体只存 `file_id` 引用，handler 侧经 TelegramAdapterApi 走 getFile 下载；
          - `message_id → chat_id` 台账由 channel 与 api 共用（edit/reaction 只有 message_id）；
          - offset（`update_id + 1`）持久化在 grape_t_channel_installation.config，重启不重投。
        """
        from src.domains.runtime.im.adapters.telegram.cards import default_ledger
        from src.domains.runtime.im.adapters.telegram.channel import TelegramChannel
        from src.domains.runtime.im.adapters.telegram.outbound import (
            TelegramAdapterApi,
            TelegramClient,
            TelegramSender,
        )
        from src.domains.runtime.im.adapters.telegram.resolvers import (
            TelegramMediaFetcher,
            TelegramRouteRegistry,
            TelegramUserResolver,
        )

        raw = dict(cfg.raw or {})
        token = str(raw.get("bot_token") or raw.get("app_id") or "")
        raw.update({"bot_token": token, "app_id": token})
        cfg.raw = raw

        app_id = str(raw.get("app_id") or "")
        db_path = self.db_path
        client = TelegramClient(token)
        route_registry = TelegramRouteRegistry()
        users = TelegramUserResolver()
        ledger = default_ledger()
        engine = self

        def _make_sender(inst_app_id: str, _raw: dict):
            sender = TelegramSender(client)
            media = TelegramMediaFetcher(client)
            engine._api_overrides[inst_app_id] = TelegramAdapterApi(
                token, client=client, sender=sender,
                route_registry=route_registry, media=media, users=users)
            engine._drop_cached_ctx(inst_app_id)
            return sender

        def _build_channel(cfg_, sender):
            return TelegramChannel(
                cfg_, sender, client=client, route_registry=route_registry,
                users=users, ledger=ledger,
                offset_loader=_telegram_offset_loader(db_path, app_id),
                offset_persist=_telegram_offset_persist(db_path, app_id),
            )

        return self._channel_factory(cfg, "telegram", None, _build_channel,
                                     sender_factory=_make_sender)

    def _wechat_factory(self, cfg: ChannelConfig):
        """微信 ilink Bot 工厂（Issue #1728）：getupdates 长轮询 + 游标持久化。

        与既有五渠道同一套 Router 装配（`_channel_factory`）：
          - 扫码绑定产出落 `grape_t_channel_installation.config`（app_id = ilink_bot_id，
            bot_token/baseurl/ilink_user_id 在 config 内）；
          - `get_updates_buf` 游标持久化在 config["cursor"]，重启续接不重投；
          - `context_token` 由 channel 入站缓存、sender 出站回传；
          - 卡片无按钮/回调，renderer 降级为「标题 + 正文 + 编号」。
        """
        from src.domains.runtime.im.adapters.wechat.channel import WechatChannel
        from src.domains.runtime.im.adapters.wechat.media import WechatMediaUploader
        from src.domains.runtime.im.adapters.wechat.outbound import (
            WechatAdapterApi,
            WechatClient,
            WechatSender,
        )
        from src.domains.runtime.im.adapters.wechat.protocol import DEFAULT_BASE_URL
        from src.domains.runtime.im.adapters.wechat.resolvers import (
            WechatMediaFetcher,
            WechatRouteRegistry,
            WechatTypingCache,
            WechatUserResolver,
        )

        raw = dict(cfg.raw or {})
        token = str(raw.get("bot_token") or raw.get("app_secret") or "")
        base_url = str(raw.get("baseurl") or raw.get("base_url") or DEFAULT_BASE_URL)
        app_id = str(raw.get("app_id") or raw.get("ilink_bot_id") or "")
        raw.update({"bot_token": token, "app_id": app_id, "baseurl": base_url})
        cfg.raw = raw

        db_path = self.db_path
        client = WechatClient(token, base_url=base_url)
        route_registry = WechatRouteRegistry()
        users = WechatUserResolver()
        typing = WechatTypingCache()
        uploader = WechatMediaUploader(client)
        media = WechatMediaFetcher(client, uploader)
        engine = self

        def _make_sender(inst_app_id: str, _raw: dict):
            sender = WechatSender(client, routes=route_registry, users=users,
                                  typing=typing, uploader=uploader,
                                  metrics=engine._metrics)
            engine._api_overrides[inst_app_id] = WechatAdapterApi(
                token, client=client, sender=sender, routes=route_registry,
                users=users, media=media, metrics=engine._metrics)
            engine._drop_cached_ctx(inst_app_id)
            return sender

        def _build_channel(cfg_, sender):
            return WechatChannel(
                cfg_, sender, client=client, route_registry=route_registry,
                users=users, metrics=engine._metrics,
                health=engine.health_tracker,        # Issue #3799：暂停状态机/告警
                cursor_loader=_wechat_cursor_loader(db_path, app_id),
                cursor_persist=_wechat_cursor_persist(db_path, app_id),
            )

        return self._channel_factory(cfg, "wechat", None, _build_channel,
                                     sender_factory=_make_sender)

    def _channel_factory(self, cfg: ChannelConfig, channel_type: str,
                         sender_cls, build_channel, sender_factory=None):
        """通用 Router 装配（Issue #936 P2 抽取）：feishu / dingtalk 工厂共用。"""
        app_id = str(cfg.raw.get("app_id", ""))
        self._prune_built_apps()
        if app_id and app_id in self._built_apps:
            # repo 轨 app 跨仓库共享：同一 app 只建一条 WS 连接（单 bot 灰度语义）
            return None
        inst = _lookup_installation_by_app_sync(self.db_path, app_id)
        if inst is None:
            # 兜底：用 cfg 原料构造最小安装视图（不落库）
            inst = {
                "installation_id": app_id, "scope": cfg.scope.value,
                "owner_user_id": "", "repo_key": "",
                "config": dict(cfg.raw), "status": "active",
            }
        else:
            scope = cfg.scope.value or inst.get("scope", "user")
            inst = dict(inst)
            inst["scope"] = scope
            if scope == "repo" and not inst.get("repo_key"):
                inst["repo_key"] = ""
        self._built_apps[app_id] = str(cfg.installation_id)

        engine = self
        installation_view = Installation(
            installation_id=str(inst["installation_id"]),
            channel_type=channel_type,
            scope=Scope(str(inst.get("scope", "user"))),  # Scope(str enum)：Router 落 SessionBinding 需枚举
            owner_user_id=str(inst.get("owner_user_id", "")),
            repo_key=str(inst.get("repo_key", "")),
        )

        # ---- ② installations：grape_t_channel_installation 枚举来源的固定视图
        async def installations(msg: InboundMessage) -> Installation:
            return installation_view

        # ---- ⑤ identity：grape_t_channel_sender_identity + legacy 回退
        async def identity(inst_view: Installation, msg: InboundMessage) -> Identity:
            user_id = await asyncio.to_thread(
                _lookup_user_sync, engine.db_path, inst_view.installation_id,
                msg.source.sender_id, inst_view.channel_type)
            return Identity(user_id=user_id, unbound=not user_id)

        # ---- ⑤' 身份逃生口：未绑定时先认绑定指令，写 grape_t_channel_sender_identity
        async def identity_bind(inst_view: Installation, msg: InboundMessage) -> bool:
            text = (msg.command_text or msg.text or "").strip().lower()
            if text not in _BIND_WORDS:
                return False
            user_id = await asyncio.to_thread(
                _bind_identity_sync, engine.db_path, inst_view.installation_id,
                inst_view.scope, inst_view.owner_user_id, msg.source.sender_id,
                msg.source.sender_stable_id)
            if not user_id:
                await _reply_text(msg.source.chat_id,
                                  "绑定失败：这个渠道没开个人身份绑定，或安装没有归属用户。")
                return True
            await _reply_text(msg.source.chat_id,
                              f"✅ 已绑定：你的账号现在是 grape 用户 {user_id}。再发一条消息即可开始使用。")
            return True

        # ---- 出站：绑定引导卡（fire-and-forget，chat 定向卡片）
        # Issue #1008：出站传输闭包按平台分支——飞书走 legacy reply/send API，
        # 钉钉走 sessionWebhook 回复目标复用（conversationId 路由）。
        async def _send_card(chat_id: str, text: str, msg_id: str = "",
                             title: str = "DevOps Bot", template: str = "blue",
                             raw_card: dict | None = None, target_user: str = "",
                             kind: str = "", update_multi: bool = False, **_kw) -> str:
            # update_multi：飞书适配器显式传的"多段更新"开关，legacy 传输闭包内部构建
            # card config 不需要；接受并忽略，避免 TypeError 打断卡片下发。
            # **_kw：兜底各渠道新增参数，防适配器/传输签名漂移直接 500。
            ctx, api = engine._ctx_for(inst)
            if api is None:
                return ""
            try:
                if raw_card is not None:
                    if not msg_id:
                        # 主动通知（待审核/待澄清/完成）没有可回复的入站消息：走 reply API 会把
                        # URL 拼成 `messages//reply` → 404，且历史上还被记成 sent（2026-11-18 实障：
                        # 待审核通知在飞书静默丢失）。无 msg_id 一律 proactive 发送。
                        _raw_sender = getattr(api, "send_card_raw_to_chat", None)
                        if _raw_sender is not None:
                            return await _raw_sender(ctx, chat_id, raw_card,
                                                     fallback_open_id=target_user)
                        # 通用兜底（钉钉/企微/Slack/Telegram 的 AdapterApi 没有单发卡片方法）：
                        # 直接交给本渠道 Sender —— 各渠道 Sender.send 都自己处理 metadata.raw_card
                        # （2026-11-18 实障：钉钉报 DingtalkAdapterApi has no attribute send_card_to_chat）。
                        _inst_id = str((inst or {}).get("installation_id") or (inst or {}).get("id") or "")
                        # 走 Channel 公开契约（channel_for + send），不读 supervisor/_sender 私有属性
                        _lookup = getattr(getattr(engine, "supervisor", None), "channel_for", None)
                        _channel = _lookup(_inst_id) if _lookup is not None else None
                        if _channel is not None:
                            from src.domains.runtime.im.channel.message import OutboundMessage
                            _res = await _channel.send(OutboundMessage(
                                chat_id=chat_id, text=text or "",
                                metadata={"raw_card": raw_card, "kind": kind or "card"}))
                            if getattr(_res, "ok", False):
                                return getattr(_res, "message_id", "") or "ok"
                            log.warning(f"im engine proactive card via sender failed: "
                                        f"{getattr(_res, 'error', '')}")
                            return ""
                        _fallback = getattr(api, "send_card_to_chat", None)
                        if _fallback is not None:
                            return await _fallback(ctx, chat_id, text, fallback_open_id=target_user,
                                                   title=title, template=template)
                        return ""
                    return "ok" if await legacy_send_card_reply(ctx, api, msg_id, raw_card) else ""
                if target_user and not chat_id:
                    _f = getattr(api, "send_text_to_user", None)
                    return await _f(ctx, target_user, text) if _f is not None else ""
                if msg_id and kind == "text":
                    _f = getattr(api, "reply_text", None)
                    return await _f(ctx, msg_id, text) if _f is not None else ""
                if msg_id:
                    _f = getattr(api, "reply_card", None)
                    return (await _f(ctx, msg_id, text, title=title, template=template)
                            if _f is not None else "")
                # 主动卡片（非 raw_card 路径）：各渠道 AdapterApi 方法名不一致，缺方法就交回 sender
                _f = getattr(api, "send_card_to_chat", None)
                if _f is not None:
                    return await _f(ctx, chat_id, text, fallback_open_id=target_user,
                                    title=title, template=template)
                _inst_id2 = str((inst or {}).get("installation_id") or (inst or {}).get("id") or "")
                _lookup2 = getattr(getattr(engine, "supervisor", None), "channel_for", None)
                _channel2 = _lookup2(_inst_id2) if _lookup2 is not None else None
                if _channel2 is not None:
                    from src.domains.runtime.im.channel.message import OutboundMessage
                    _res2 = await _channel2.send(OutboundMessage(chat_id=chat_id, text=text,
                                                                 metadata={"kind": kind or "card"}))
                    return (getattr(_res2, "message_id", "") or "ok") if getattr(_res2, "ok", False) else ""
                return ""
            except Exception as e:
                log.warning("im engine deliver failed: %s", e)
                return ""

        async def _reply_text(chat_id: str, text: str, **kw):
            kw.setdefault("kind", "text")
            return await _send_card(chat_id, text, **kw)

        async def _reply_card(chat_id: str, text: str, **kw):
            return await _send_card(chat_id, text, **kw)

        if sender_factory is not None:
            # 渠道自带发送器（企微：持有长连接 transport，不经 legacy 传输闭包）
            sender = sender_factory(app_id, cfg.raw)
        elif channel_type == "dingtalk":
            # 钉钉传输：sessionWebhook（入站登记，出站复用）；缺失/过期回落机器人 API
            from src.domains.runtime.im.adapters.dingtalk.outbound import (
                DingtalkAdapterApi, DingtalkRobotApi,
                send_action_card_with_fallback, send_markdown_with_fallback,
                send_text_with_fallback,
            )

            _robot = DingtalkRobotApi(app_id, str(cfg.raw.get("app_secret", "")))
            # handler 的下载/身份能力（媒体两跳）走钉钉侧 api，而不是 FeishuAsync
            engine._api_overrides[app_id] = DingtalkAdapterApi(
                app_id, str(cfg.raw.get("app_secret", "")))
            engine._drop_cached_ctx(app_id)   # 覆盖后重建 ctx/api 对（旧 api 收尾关闭）

            async def _dt_text(chat_id: str, text: str, **kw):
                return await send_text_with_fallback(chat_id, text, robot=_robot)

            async def _dt_md(chat_id: str, text: str, chunk_marker: str = "", **kw):
                return await send_markdown_with_fallback(
                    chat_id, text, chunk_marker=chunk_marker, robot=_robot)

            async def _dt_action(chat_id: str, title: str, text: str,
                                 btns: list | None = None, **kw):
                # webhook 缺失/过期 → 回落机器人 API（markdown 承载卡片内容）
                return await send_action_card_with_fallback(
                    chat_id, title, text, btns=btns or [], robot=_robot)

            # 批 5：安装了互动卡片模板（dingtalk_cards.select/result）才申报 CARD_ACTION，
            # 否则保持"文字卡 + 回数字"，能力位不虚报。
            _card_mode, _tpl = dingtalk_card_mode(cfg.raw)
            _card_client = None
            if _card_mode == "interactive":
                from src.domains.runtime.im.adapters.dingtalk.cards import DingtalkCardClient
                _card_client = DingtalkCardClient(app_id, str(cfg.raw.get("app_secret", "")))
            sender = sender_cls(_dt_text, _dt_md, _dt_action, card_mode=_card_mode,
                                card_template_id=_tpl, card_client=_card_client)
        else:
            sender = sender_cls(_reply_text, _reply_card)
        # Issue #870：发送器结果钩子（ok → sent / 失败 → failed）
        sender = CountingSender(sender, engine._metrics)
        replier = Replier(sender)
        # Issue #1008：登记本安装的 Replier（_dispatch_agent 构造 HandlerEgress 复用）
        engine._repliers[app_id] = (replier, channel_type)

        # ---- ⑥ bindings：SqliteBindingStore（闭包捕获 engine 共享实例）
        # 多平台（Issue #936 P2）：绑定行 channel_type 与安装渠道一致
        if channel_type == "feishu":
            bindings = engine._bindings
        else:
            bindings = _TypedBindingStore(engine._bindings, channel_type)
        dedup = engine._dedup

        # ---- ⑦ append_message：持久化 + dedup mark 同事务
        async def append_message(session: SessionBinding, msg: InboundMessage) -> None:
            await asyncio.to_thread(
                _append_message_sync, engine.db_path,
                session.installation_id, msg)

        # ---- ⑧ 命令（含 pending selection）
        def parse_command(text: str) -> Optional[dict]:
            text = (text or "").strip()
            if text.startswith("/"):
                return {"kind": "command", "text": text}
            if text.isdigit():
                return {"kind": "selection", "num": text}
            return None

        async def apply_command(cmd: dict, inst_view: Installation,
                                session: SessionBinding, msg: InboundMessage):
            ctx, api = engine._ctx_for(inst)
            chat_id = msg.source.chat_id
            msg_id = _reply_msg_id(msg)      # 卡片回调必须用 open_message_id，不能用 event_id
            eg = engine._handler_arg(inst, ctx, api, msg)
            if cmd.get("kind") == "selection":
                getter = engine._get_pending_selection or engine._legacy_get_pending_selection()
                pending = getter(chat_id)
                log.info(f"[selection] num={cmd.get('num')} "
                         f"pending={'found' if pending else 'NONE'} chat={chat_id[-12:]}")
                if pending:
                    handler = engine._handle_selection or engine._legacy_handle_selection()
                    await handler(ctx, eg, chat_id, msg_id, cmd.get("num", ""), pending)
                    return None
                # 无 pending：回落 agent（legacy 语义：纯数字无 pending 走对话）
                await engine._dispatch_agent(inst, msg)
                return None
            text = str(cmd.get("text", ""))
            fn = engine._process_command or engine._legacy_process_command()
            try:
                # 本文件属 runtime 域、由 mgmt 进程执行：命令处理用到的
                # repo_im_group_binding / issue / chat_agent_session 都是 runtime 基表，
                # 必须传 runtime owner 连接（单库拓扑自动回落 session_manager.db）。
                from src.kernel.owner_db import db_pair
                legacy_db, _ = db_pair("runtime")
            except Exception:
                legacy_db = None
            result = await fn(
                ctx, eg, text, chat_id, msg.source.sender_id, legacy_db,
                account=ctx.app_id, msg_id=msg_id,
                sender_open_id=msg.source.sender_id,
                card_value=_card_value_of(msg), action_token=_action_token_of(msg),
            )
            if result:
                await eg.reply_text(ctx, msg_id, result)
            return None

        def _card_value_of(msg: InboundMessage) -> Optional[dict]:
            return _raw_card_context(msg)[1]

        def _action_token_of(msg: InboundMessage) -> str:
            return _raw_card_context(msg)[2]

        # ---- ⑨ agent_run：双轨路由 process_chat_async / process_vod_async
        async def agent_run(route, messages) -> None:
            for m in messages or []:
                try:
                    await engine._dispatch_agent(inst, m)
                except Exception as e:
                    log.warning("im engine agent_run failed: %r", e)

        # ---- OK reaction（agent 执行前受理回执；能力位缺失渠道显式降级计 degraded）
        async def add_reaction(msg: InboundMessage) -> None:
            ctx, api = engine._ctx_for(inst)
            if api is None:
                return
            eg = engine._handler_arg(inst, ctx, api, msg)
            try:
                await eg.add_reaction(ctx, msg.message_id or msg.event_id, "OK")
            except Exception as e:
                log.warning("im engine add_reaction failed: %s", e)

        router = Router(
            installations=installations,
            identity=identity,
            identity_bind=identity_bind,
            bindings=bindings,
            dedup=dedup,
            replier=replier,
            append_message=append_message,
            parse_command=parse_command,
            apply_command=apply_command,
            agent_run=agent_run,
            add_reaction=add_reaction,
        )

        router_handle = router.handle

        async def _counting_inbound(ctx, msg):
            # Issue #870：入站分发钩子计数（适配器 seen 去重后，等价 legacy run_bot 位置）
            engine._metrics.record_received()
            return await router_handle(ctx, msg)

        cfg.inbound_handler = _counting_inbound
        if self._channel_builder is not None:
            return self._channel_builder(cfg, sender)
        return build_channel(cfg, sender)


# ---------------------------------------------------------------- main.py 入口助手

async def start_im_engine(db_path: Optional[str] = None) -> Optional[ImEngine]:
    """main.py / grape_server.py 启动入口：装配并启动新引擎；失败返回 None（调用方保持 legacy 兜底）。

    MySQL 后端走 session_manager 的池化连接（方言由 src.kernel.mysql_compat 翻译）；
    渠道 4 表由 run_startup_migration → ensure_channel_tables 幂等建/补。

    Issue #2261 A：启动成功后登记到全局注册点（set_current_engine），
    notify.resolve_engine() 的第一优先级来源。
    """
    try:
        path = db_path or default_db_path()
        engine = ImEngine(path)
        await engine.start()
        set_current_engine(engine)
        return engine
    except Exception as e:
        log.warning("IM engine start failed: %s", e)
        return None


__all__ = [
    "ImEngine", "engine_mode", "egress_mode", "start_im_engine", "run_startup_migration",
    "default_db_path", "SqliteDedupStore", "SqliteBindingStore",
    "set_current_engine", "get_current_engine",
    "MODE_WATCH_INTERVAL", "ENGINE_ENV",
    "EGRESS_ENV", "EGRESS_MODE_FILE_ENV", "feishu_api", "get_active_installation",
    "sync_installation", "revoke_installation",
]
