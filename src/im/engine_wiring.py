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
import uuid
from typing import Awaitable, Callable, Optional

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

from src.im.channel.capability import Capability  # noqa: F401  (re-export convenience)
from src.im.channel.card import self_check_card_capabilities  # Issue #1095 启动自检
from src.im.channel.channel import ChannelConfig
from src.im.channel.message import InboundMessage
from src.im.channel.registry import ChannelRegistry, default_registry
from src.im.channel.types import MsgType, Scope, Type

from src.im.engine.dedup import InboundDedup
from src.im.engine.egress import EGRESS_ENV, HandlerEgress, egress_mode, legacy_send_card_reply
from src.im.engine.metrics import CountingSender, EngineMetrics
from src.im.engine.router import Identity, Installation, Router
from src.im.engine.session import SessionBinding
from src.im.engine.replier import Replier
from src.im.engine.supervisor import Supervisor

log = logging.getLogger(__name__)

ENGINE_ENV = "GRAPE_IM_ENGINE"
MODE_FILE_ENV = "GRAPE_IM_ENGINE_MODE_FILE"
# Issue #1008：出站收敛开关（replier=OutboundMessage→Replier→Channel.send；legacy=直连平台 API）
EGRESS_MODE_FILE_ENV = "GRAPE_IM_EGRESS_MODE_FILE"
MODE_WATCH_INTERVAL = 5.0      # main.py 侧 mode 轮询周期（秒级切回粒度）

_MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "migrations", "001_channel_tables.py",
)

_APP_SEEN_TABLE = (
    "CREATE TABLE IF NOT EXISTS bot_seen_messages ("
    " app_id TEXT, message_id TEXT, seen_at REAL, UNIQUE (app_id, message_id))"
)

_MESSAGE_TABLE = (
    "CREATE TABLE IF NOT EXISTS grape_t_channel_inbound_message ("
    " installation_id TEXT NOT NULL, message_id TEXT NOT NULL,"
    " chat_id TEXT NOT NULL DEFAULT '', sender_id TEXT NOT NULL DEFAULT '',"
    " text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " PRIMARY KEY (installation_id, message_id))"
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


def run_startup_migration(db_path: str) -> dict:
    """启动迁移（幂等，可重复执行）：001 建渠道表 + wiring 自有消息表。

    A2 收尾（Phase B）：从 legacy `im_*` 列回填安装行已移到一次性迁移
    `scripts/migrations/002_drop_legacy_im.py`（先回填再 DROP 列/表）——
    旧列不存在后启动再跑回填会直接 no such column。

    必须在 worker 线程内调用；连接由 _connect 按后端选择（MySQL 走池化共享连接，
    SQLite 走本地文件连接），方言统一经 src.mysql_compat 翻译。
    """
    migrations = _load_migration_module()
    conn = _connect(db_path)
    try:
        conn.execute(_APP_SEEN_TABLE)     # 测试库无则补空表
        migrations.ensure_channel_tables(conn)
        conn.execute(_MESSAGE_TABLE)
        conn.commit()
        return {"user": 0, "repo": 0, "installations": [],
                "backfill": "moved-to-002_drop_legacy_im"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def default_db_path() -> str:
    """与 src.session_manager 同源的 sqlite 库路径（env DEVOPS_DB_PATH）。"""
    return os.environ.get(
        "DEVOPS_DB_PATH",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "sessions.db",
        ),
    )


def _connect(db_path: str):
    """取一条数据库连接：MySQL 后端复用 session_manager 的池化连接，SQLite 后端开本地连接。

    Issue #825 MySQL 适配：仓储层原本写死 sqlite3（PRAGMA/?/datetime 等），
    统一交给 src.mysql_compat 方言翻译（? → %s、INSERT OR IGNORE、ON CONFLICT
    upsert、datetime('now')、CREATE TABLE 的 TEXT 主键 → VARCHAR(191) 等）。
    返回的连接统一支持 execute/cursor/commit/rollback/close；共享连接由
    _SharedConn 代理，close() 为空操作（不能关掉全局单例）。
    """
    if _is_mysql_backend():
        return _SharedConn(_shared_db())
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _is_mysql_backend() -> bool:
    """当前是否 MySQL 后端（延迟探测，避免模块导入期的环形依赖）。

    口径与 src.session_manager._is_mysql_backend 完全一致：
    env DEVOPS_DB_BACKEND > configs/db-backend.json > 默认 sqlite。
    """
    global _MYSQL_BACKEND_CACHE
    if _MYSQL_BACKEND_CACHE is None:
        try:
            from src.session_manager import _is_mysql_backend as _probe
            _MYSQL_BACKEND_CACHE = bool(_probe())
        except Exception:
            _MYSQL_BACKEND_CACHE = False
    return _MYSQL_BACKEND_CACHE


def _reset_backend_cache() -> None:
    """清掉后端探测缓存（仅供测试在切换 DEVOPS_DB_BACKEND 后调用）。"""
    global _MYSQL_BACKEND_CACHE
    _MYSQL_BACKEND_CACHE = None


def _shared_db():
    from src.session_manager import db
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
    """建/补齐 IM 渠道 4 表（幂等）。db_setup 启动建表与引擎启动迁移共用同一份 DDL。"""
    _load_migration_module().ensure_channel_tables(conn)


# ---------------------------------------------------------------- UI 写入桥（Issue #871 写方向反转）

def _legacy_installation_key(scope: str, owner_user_id: str, repo_key: str) -> str:
    """与 001 迁移/唯一索引同口径的归属键：repo 轨用 repo_key，user 轨用 owner_user_id。"""
    return repo_key if scope == "repo" else str(owner_user_id)


_feishu_api_cache: dict = {}


def feishu_api(app_id: str = ""):
    """按 app_id 取（缓存的）FeishuAsync 客户端（旧 IM 下线 A2 M2）。

    通知/推送类功能原先借 legacy BotManager 的 `bm.api`，现在统一从新表拿凭证后
    直接用 API 客户端；同一进程内按 app_id 复用一份（aiohttp session 绑定主循环）。
    """
    api = _feishu_api_cache.get(app_id)
    if api is None:
        from src.im.feishu_async import FeishuAsync
        api = FeishuAsync()
        _feishu_api_cache[app_id] = api
    return api


def get_active_installation(scope: str = "user", *, owner_user_id: str = "", repo_key: str = "",
                            channel_type: str = "") -> Optional[dict]:
    """读 grape_t_channel_installation 的活跃安装（旧 IM 下线 A2 M2）。

    业务侧（如 web→飞书推送）原本经 legacy BotManager 拿 ctx/api，现在统一从这里取
    安装凭证；返回 None 表示该轨没有活跃安装。
    """
    scope = scope or "user"
    key = _legacy_installation_key(scope, owner_user_id, repo_key)
    try:
        conn = _connect(default_db_path())
        try:
            sql = ("SELECT id, channel_type, scope, owner_user_id, repo_key, config "
                   "FROM grape_t_channel_installation WHERE status='active' AND scope=? "
                   "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)")
            params: list = [scope, key, key]
            if channel_type:
                sql += " AND channel_type=?"
                params.append(channel_type)
            row = conn.execute(sql + " ORDER BY updated_at DESC LIMIT 1", tuple(params)).fetchone()
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
            row = conn.execute(
                "SELECT config FROM grape_t_channel_installation WHERE legacy_app_id=? AND scope=? "
                "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)",
                (app_id, scope, key, key),
            ).fetchone()
            existing = {}
            if row and row[0]:
                try:
                    existing = json.loads(row[0]) or {}
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
            conn.execute(
                "UPDATE grape_t_channel_installation SET status='active', updated_at=datetime('now') "
                "WHERE legacy_app_id=? AND scope=? "
                "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)",
                (app_id, scope, key, key),
            )
            conn.commit()
            return True
        finally:
            conn.close()
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
            cur = conn.execute(
                "UPDATE grape_t_channel_installation SET status='revoked', updated_at=datetime('now') "
                "WHERE channel_type=? AND scope=? AND legacy_app_id=? "
                "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)",
                (channel_type, scope, app_id, key, key),
            )
            conn.commit()
            return (getattr(cur, "rowcount", 0) or 0) > 0
        finally:
            conn.close()
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
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM grape_t_channel_inbound_dedup WHERE installation_id=? AND message_id=?",
                (installation_id, message_id),
            ).fetchone()
            return row is not None
        except _DB_ERRORS:
            return False
        finally:
            conn.close()

    def try_claim(self, installation_id: str, message_id: str, token: str) -> bool:
        conn = self._conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO grape_t_channel_inbound_dedup "
                "(installation_id, message_id, state, claim_token) VALUES (?,?, 'claimed', ?)",
                (installation_id, message_id, token),
            )
            conn.commit()
            return cur.rowcount > 0
        except _DB_ERRORS:
            return False
        finally:
            conn.close()

    def mark(self, installation_id: str, message_id: str) -> bool:
        conn = self._conn()
        try:
            cur = conn.execute(
                "UPDATE grape_t_channel_inbound_dedup SET state='marked' "
                "WHERE installation_id=? AND message_id=?",
                (installation_id, message_id),
            )
            conn.commit()
            return cur.rowcount > 0
        except _DB_ERRORS:
            return False
        finally:
            conn.close()

    def release(self, installation_id: str, message_id: str) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM grape_t_channel_inbound_dedup WHERE installation_id=? AND message_id=?",
                (installation_id, message_id),
            )
            conn.commit()
        except _DB_ERRORS:
            pass
        finally:
            conn.close()


class SqliteBindingStore:
    """grape_t_channel_chat_session_binding 落库（BindingStore 协议实现）。

    合并 repo_im_group_binding + vod_conversations 语义（Issue #742 §5.1）。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def get(self, installation_id: str, key: str) -> Optional[SessionBinding]:
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT installation_id, channel_chat_id, chat_type, scope, repo_key, "
                "agent_type, runtime_session_id, config "
                "FROM grape_t_channel_chat_session_binding WHERE installation_id=? AND channel_chat_id=?",
                (installation_id, key),
            ).fetchone()
        except _DB_ERRORS:
            return None
        finally:
            conn.close()
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
        conn = _connect(self._db_path)
        try:
            existing = conn.execute(
                "SELECT id FROM grape_t_channel_chat_session_binding "
                "WHERE installation_id=? AND channel_chat_id=?",
                (binding.installation_id, binding.binding_key),
            ).fetchone()
            row_id = existing[0] if existing else str(uuid.uuid4())
            conn.execute(
                "INSERT INTO grape_t_channel_chat_session_binding "
                "(id, installation_id, channel_type, channel_chat_id, chat_type, scope, "
                "repo_key, agent_type, runtime_session_id, config, last_active_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET chat_type=excluded.chat_type, "
                "scope=excluded.scope, repo_key=excluded.repo_key, "
                "agent_type=excluded.agent_type, config=excluded.config, "
                "last_active_at=datetime('now'), updated_at=datetime('now')",
                (row_id, binding.installation_id, channel_type, binding.binding_key,
                 binding.chat_type, scope_value, binding.repo_key,
                 binding.agent_type, binding.runtime_session_id,
                 json.dumps(binding.config, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
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
    conn = _connect(db_path)
    try:
        # MySQL 后端 autocommit=False，事务由 commit/rollback 界定；显式 BEGIN 会额外
        # 触发一次隐式提交（MySQL 语义），因此只在 sqlite 后端下发。
        if not _is_mysql_backend():
            conn.execute("BEGIN")
        conn.execute(
            "INSERT OR IGNORE INTO grape_t_channel_inbound_message "
            "(installation_id, message_id, chat_id, sender_id, text) VALUES (?,?,?,?,?)",
            (installation_id, mid, msg.source.chat_id, msg.source.sender_id,
             (msg.text or "")[:8000]),
        )
        conn.execute(
            "INSERT INTO grape_t_channel_inbound_dedup (installation_id, message_id, state, claim_token) "
            "VALUES (?, ?, 'marked', '') ON CONFLICT(installation_id, message_id) "
            "DO UPDATE SET state='marked'",
            (installation_id, mid),
        )
        conn.commit()
    except _DB_ERRORS:
        conn.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------- 身份解析

def _lookup_user_sync(db_path: str, installation_id: str, open_id: str,
                      channel_type: str = "feishu") -> str:
    """sender → grape 用户：grape_t_channel_sender_identity 优先，回退 legacy 统一映射。

    统一映射按渠道取列：钉钉是 dingtalk_user_id，其余维持 feishu_open_id 兼容旧行为。
    列名只来自白名单，不拼接外部输入。
    """
    if not open_id:
        return ""
    mapping_col = {"dingtalk": "dingtalk_user_id"}.get(channel_type or "", "feishu_open_id")
    conn = _connect(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT user_id FROM grape_t_channel_sender_identity "
                "WHERE installation_id=? AND platform_user_id=?",
                (installation_id, open_id),
            ).fetchone()
            if row and str(row[0]):
                return str(row[0])
        except _DB_ERRORS:
            pass
        try:
            row = conn.execute(
                "SELECT user_id FROM unified_user_mapping "
                f"WHERE {mapping_col}=? LIMIT 1",
                (open_id,),
            ).fetchone()
            if row and row[0]:
                return str(row[0])
        except _DB_ERRORS:
            pass
    finally:
        conn.close()
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
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO grape_t_channel_sender_identity "
            "(installation_id, platform_user_id, platform_stable_user_id, user_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(installation_id, platform_user_id) "
            "DO UPDATE SET user_id=excluded.user_id, "
            "platform_stable_user_id=excluded.platform_stable_user_id",
            (installation_id, platform_user_id, stable_id or "", str(owner_user_id)),
        )
        conn.commit()
        return str(owner_user_id)
    except _DB_ERRORS as e:
        log.warning("im engine bind identity failed: %r", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return ""
    finally:
        conn.close()


def _update_installation_config_sync(db_path: str, app_id: str,
                                       mutate: Callable[[dict], None]) -> bool:
    """按 config.app_id 定位活跃安装行并就地更新 config（offset 等运行时状态）。"""
    conn = _connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT id, config FROM grape_t_channel_installation WHERE status='active'"
            ).fetchall()
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
                conn.execute(
                    "UPDATE grape_t_channel_installation SET config=? WHERE id=?",
                    (json.dumps(config, ensure_ascii=False), row[0]))
                conn.commit()
            except _DB_ERRORS:
                return False
            return True
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, scope, owner_user_id, repo_key, config, status "
            "FROM grape_t_channel_installation WHERE status='active'"
        ).fetchall()
    except _DB_ERRORS:
        return None
    finally:
        conn.close()
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
        from src.im.engine.egress import _always_valid
        return _always_valid
    from src.im.engine.egress import _is_feishu_id_valid
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
        from src.im.adapters.dingtalk.cards import card_mode_enabled
        switched_on = card_mode_enabled()
    except Exception:
        switched_on = False
    if not (raw.get("cards_enabled") and switched_on):
        return "action_card", tpl
    return "interactive", tpl


def _card_renderer_for(channel_type: str):
    """渠道 CardRenderer（Issue #1095）：声明不协商——每渠道一个实现，wiring 注入。"""
    from src.im.channel.card import BUILTIN_CARD_RENDERERS
    if str(channel_type).lower() in BUILTIN_CARD_RENDERERS:
        if str(channel_type).lower() == "dingtalk":
            from src.im.channel.renderers.dingtalk import DingtalkCardRenderer
            return DingtalkCardRenderer()
        if str(channel_type).lower() == "wecom":
            from src.im.channel.renderers.wecom import WecomCardRenderer
            return WecomCardRenderer()
        if str(channel_type).lower() == "slack":
            # Issue #1309：Slack 是唯一「真回调 + 原地更新 + 线程」齐全的渠道
            from src.im.channel.renderers.slack import SlackCardRenderer
            return SlackCardRenderer()
        if str(channel_type).lower() == "telegram":
            # Issue #1613：文本 + InlineKeyboardMarkup（callback_data 64B 短码）
            from src.im.channel.renderers.telegram import TelegramCardRenderer
            return TelegramCardRenderer()
        if str(channel_type).lower() == "wechat":
            # Issue #1728：纯文本 + 编号选项（无按钮/无回调，数字选择复用 pending）
            from src.im.channel.renderers.wechat import WechatCardRenderer
            return WechatCardRenderer()
        from src.im.channel.renderers.feishu import FeishuCardRenderer
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
            from src.im.bot_lease import generate_instance_id
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
            from src.im.bot_lease import BotLeaseStore, generate_instance_id
            self.instance_id = self.instance_id or generate_instance_id()
            lease = BotLeaseStore(instance_id=self.instance_id)

        from src.im.engine.installations import SqliteInstallationProvider
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
            from src.im.adapters.dingtalk.outbound import DINGTALK_CAPABILITIES
            from src.im.adapters.feishu.outbound import FEISHU_CAPABILITIES
            from src.im.adapters.slack.outbound import SLACK_CAPABILITIES
            from src.im.adapters.telegram.outbound import TELEGRAM_CAPABILITIES
            from src.im.adapters.wechat.outbound import WECHAT_CAPABILITIES
            from src.im.adapters.wecom.outbound import WECOM_CAPABILITIES
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
        self._ctx_cache.clear()
        self._repliers.clear()
        log.info("IM engine: stopped")

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
            from src.im.feishu_async import BotContext, FeishuAsync
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
        from src.im.handler_async import process_chat_async
        return process_chat_async

    def _legacy_process_vod(self):
        from src.im.vod_handler_async import process_vod_async
        return process_vod_async

    def _legacy_process_command(self):
        from src.im.handler_async import process_command_async
        return process_command_async

    def _legacy_handle_selection(self):
        from src.im.handler_async import _handle_selection
        return _handle_selection

    def _legacy_get_pending_selection(self):
        from src.im.handler_async import _get_pending_selection
        return _get_pending_selection

    # ---------------- 渠道工厂（per installation Router 六注入点，多平台通用）

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

    def _feishu_factory(self, cfg: ChannelConfig):
        from src.im.adapters.feishu.outbound import FeishuSender

        def _build_channel(cfg_, sender):
            from src.im.adapters.feishu.channel import FeishuChannel
            return FeishuChannel(cfg_, sender)

        return self._channel_factory(cfg, "feishu", FeishuSender, _build_channel)

    def _dingtalk_factory(self, cfg: ChannelConfig):
        """钉钉工厂（Issue #936 P2）：与飞书同一套 Router 装配，仅渠道实现/发送器不同。"""
        from src.im.adapters.dingtalk.outbound import DingtalkSender

        def _build_channel(cfg_, sender):
            from src.im.adapters.dingtalk.channel import DingtalkChannel
            return DingtalkChannel(cfg_, sender)

        return self._channel_factory(cfg, "dingtalk", DingtalkSender, _build_channel)

    def _wecom_factory(self, cfg: ChannelConfig):
        """企微工厂（Issue #1254）：与钉钉同族（长连接），仅渠道/发送器实现不同。

        出站不走 legacy 传输闭包：WecomSender 直接持有本安装的 WecomTransport
        （channel 建连后 attach 同一条 WS），因此经 sender_factory 注入；
        媒体入站已在收帧时下载解密，handler 侧 api 覆盖为缓存读取。
        """
        from src.im.adapters.wecom.cards import CardLedger
        from src.im.adapters.wecom.channel import WecomChannel
        from src.im.adapters.wecom.outbound import (
            WecomAdapterApi,
            WecomReplyRegistry,
            WecomSender,
            WecomTransport,
        )
        from src.im.adapters.wecom.resolvers import WecomMediaResolver

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
            engine._ctx_cache.pop(app_id, None)   # 覆盖后重建 ctx/api 对
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
        from src.im.adapters.slack.channel import SlackChannel
        from src.im.adapters.slack.outbound import SlackAdapterApi, SlackClient, SlackSender
        from src.im.adapters.slack.resolvers import (
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
            engine._ctx_cache.pop(app_id, None)   # 覆盖后重建 ctx/api 对
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
        from src.im.adapters.telegram.cards import default_ledger
        from src.im.adapters.telegram.channel import TelegramChannel
        from src.im.adapters.telegram.outbound import (
            TelegramAdapterApi,
            TelegramClient,
            TelegramSender,
        )
        from src.im.adapters.telegram.resolvers import (
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
            engine._ctx_cache.pop(inst_app_id, None)
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
        from src.im.adapters.wechat.channel import WechatChannel
        from src.im.adapters.wechat.media import WechatMediaUploader
        from src.im.adapters.wechat.outbound import (
            WechatAdapterApi,
            WechatClient,
            WechatSender,
        )
        from src.im.adapters.wechat.protocol import DEFAULT_BASE_URL
        from src.im.adapters.wechat.resolvers import (
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
            engine._ctx_cache.pop(inst_app_id, None)
            return sender

        def _build_channel(cfg_, sender):
            return WechatChannel(
                cfg_, sender, client=client, route_registry=route_registry,
                users=users, metrics=engine._metrics,
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
                             kind: str = "") -> str:
            ctx, api = engine._ctx_for(inst)
            if api is None:
                return ""
            try:
                if raw_card is not None:
                    return "ok" if await legacy_send_card_reply(ctx, api, msg_id, raw_card) else ""
                if target_user and not chat_id:
                    return await api.send_text_to_user(ctx, target_user, text)
                if msg_id and kind == "text":
                    return await api.reply_text(ctx, msg_id, text)
                if msg_id:
                    return await api.reply_card(ctx, msg_id, text, title=title, template=template)
                return await api.send_card_to_chat(
                    ctx, chat_id, text, fallback_open_id=target_user,
                    title=title, template=template)
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
            from src.im.adapters.dingtalk.outbound import (
                DingtalkAdapterApi, DingtalkRobotApi,
                send_action_card_with_fallback, send_markdown_with_fallback,
                send_text_with_fallback,
            )

            _robot = DingtalkRobotApi(app_id, str(cfg.raw.get("app_secret", "")))
            # handler 的下载/身份能力（媒体两跳）走钉钉侧 api，而不是 FeishuAsync
            engine._api_overrides[app_id] = DingtalkAdapterApi(
                app_id, str(cfg.raw.get("app_secret", "")))
            engine._ctx_cache.pop(app_id, None)   # 覆盖后重建 ctx/api 对

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
                from src.im.adapters.dingtalk.cards import DingtalkCardClient
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
                from src.session_manager import db as legacy_db
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
    """main.py 启动入口：装配并启动新引擎；失败返回 None（调用方保持 legacy 兜底）。

    MySQL 后端走 session_manager 的池化连接（方言由 src.mysql_compat 翻译）；
    渠道 4 表由 run_startup_migration → ensure_channel_tables 幂等建/补。
    """
    try:
        path = db_path or default_db_path()
        engine = ImEngine(path)
        await engine.start()
        return engine
    except Exception as e:
        log.warning("IM engine start failed: %s", e)
        return None


__all__ = [
    "ImEngine", "engine_mode", "egress_mode", "start_im_engine", "run_startup_migration",
    "default_db_path", "SqliteDedupStore", "SqliteBindingStore",
    "MODE_WATCH_INTERVAL", "ENGINE_ENV",
    "EGRESS_ENV", "EGRESS_MODE_FILE_ENV", "feishu_api", "get_active_installation",
    "sync_installation", "revoke_installation",
]
