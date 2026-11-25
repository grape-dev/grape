"""Session 管理：DB 初始化、Session CRUD、飞书绑定、Feishu 用户映射"""
from __future__ import annotations

import sqlite3, json, threading, os, time, shutil
from datetime import datetime
from pathlib import Path
from src.kernel.config import log
from src.kernel.issue_ident import native_issue_number  # Issue #3055
from src.api.time_utils import normalize_event_time
# 能力端口：kernel 不 import src.domains；域实现在装配层 src/api/contracts_wiring.py 绑定。
# 只在调用点解析（_accounts.x / _engines.x / _tokens.x），不在模块级取符号。
from src.kernel.ports import _base as _ports_base
from src.kernel.ports import accounts as _accounts
from src.kernel.ports import engines as _engines
from src.kernel.ports import im as _im
from src.kernel.ports import tokens as _tokens

# 兼容别名：DDL 快照只在 owner 域维护，这里保留可读入口（不要在此写建表语句）。
# 仿照端口做法，别名只在被访问时解析——模块 import 期不触碰端口，
# 否则装配层尚未绑定时会直接抛 PortNotBound（进程启动早期会 import 本模块）。
_DDL_ALIASES = {
    "REPO_IM_GROUP_BINDING_TABLE_SQL": (_im, "repo_im_binding_ddl"),
    "UNIFIED_USER_MAPPING_TABLE_SQL": (_accounts, "unified_user_mapping_ddl"),
}


def default_db_path() -> str:
    """默认 sqlite 主库路径（单一出口，Issue #3251）。

    env DEVOPS_DB_PATH 优先；否则按本文件位置推仓库根：
    src/kernel/session_manager.py -> parents[0]=src/kernel, [1]=src, [2]=仓库根
    -> <repo>/data/sessions.db（与原 ../../data/sessions.db 口径等价）。
    IM 装配层等复用本函数，消除各处自拼 dirname 层级的双口径
    （目录再调整时不再复发 src/domains/data/sessions.db 型错位）。
    """
    env_path = os.environ.get("DEVOPS_DB_PATH")
    if env_path:
        return env_path
    return str(Path(__file__).resolve().parents[2] / "data" / "sessions.db")


def __getattr__(name: str):
    """惰性兼容别名（PEP 562）：owner 域 DDL 快照只经端口取，kernel 不 import domains。"""
    alias = _DDL_ALIASES.get(name)
    if alias is not None:
        port, symbol = alias
        return getattr(port, symbol)()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# 备份/迁移用的库文件路径（import 期定型）。注意：这**不是**连接用的路径——
# 连接在 _LazyDB._build() 里按当时的 DEVOPS_DB_PATH 解析（见下方）。两者分开是
# 有意的：备份要的是「文件在哪」，连接要的是「此刻该连哪个库」，测试会改 env。
_db_path = default_db_path()
# 进程级 DB 互斥锁：改用 TrackedRLock（接口与 threading.RLock 一致），
# 记录等锁/持锁时长与当前持有者；慢等待/慢持有会打进日志，
# 「一条重查询拿住锁、后续请求全部排队」这类现场靠这两类信号定位。
from src.kernel.db_lock_metrics import TrackedRLock  # noqa: E402

db_lock = TrackedRLock()


def _is_mysql_backend() -> bool:
    """是否启用 MySQL（云 RDS）后端。

    开关优先级：环境变量 DEVOPS_DB_BACKEND > configs/db-backend.json 的 backend 字段 > 默认 sqlite。
    """
    try:
        from src.kernel.mysql_compat import get_db_backend
        return get_db_backend() == "mysql"
    except Exception:
        env = os.environ.get("DEVOPS_DB_BACKEND", "").lower()
        return env in ("mysql", "rds", "1", "true")


def _backup_db(path: str, tag: str = "") -> str:
    """迁移前备份 DB（仅在有实际数据且首次时），返回备份路径。失败不阻断（仅告警）。

    MySQL 后端下文件备份无意义（数据在远端），直接返回空串。
    """
    if _is_mysql_backend():
        return ""
    try:
        if not os.path.exists(path):
            return ""
        backup_dir = os.path.join(os.path.dirname(path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{os.path.basename(path)}{('_' + tag) if tag else ''}.{stamp}.bak"
        dest = os.path.join(backup_dir, name)
        if not os.path.exists(dest):
            shutil.copy2(path, dest)
            log.info(f"DB backed up: {path} -> {dest}")
        # 保留最近 N 份，删除更早（防 data/backups 无限膨胀打满磁盘）
        prune_db_backups(path)
        return dest
    except Exception as e:
        log.warning(f"DB backup failed: {e}")
        return ""


def prune_db_backups(path: str, keep: int | None = None) -> int:
    """保留最近 keep 份迁移前 DB 备份，删除更早的（防 data/backups 无限膨胀打满磁盘）。

    默认保留 DB_BACKUP_KEEP（5）；MySQL 后端为远端无文件，跳过。返回删除数量（幂等）。
    """
    if _is_mysql_backend():
        return 0
    keep = int(os.environ.get("DB_BACKUP_KEEP", "5")) if keep is None else int(keep)
    try:
        backup_dir = os.path.join(os.path.dirname(path), "backups")
        if not os.path.isdir(backup_dir):
            return 0
        base = os.path.basename(path)
        rows = []
        for f in os.listdir(backup_dir):
            if f.startswith(base + ".") and f.endswith(".bak"):
                fp = os.path.join(backup_dir, f)
                try:
                    rows.append((os.path.getmtime(fp), fp))
                except Exception:
                    continue
        rows.sort(key=lambda r: r[0], reverse=True)
        removed = 0
        for _, fp in rows[keep:]:
            try:
                os.remove(fp)
                removed += 1
            except Exception:
                pass
        if removed:
            log.info(f"Pruned {removed} old DB backups (keep {keep}) in {backup_dir}")
        return removed
    except Exception as e:
        log.warning(f"prune_db_backups failed: {e}")
        return 0


def _sqlite_journal_mode() -> str:
    """SQLite journal 模式（Issue #3588 overlayfs 兼容选项）。

    环境变量 DEVOPS_DB_JOURNAL_MODE 可选 wal|truncate|delete|memory 等
    sqlite 合法值，默认 wal（与历史行为一致，不改默认）。overlayfs 容器
    若 WAL/SHM copy-up 失效反复触发，可显式改为 truncate/delete 规避。
    """
    mode = (os.environ.get("DEVOPS_DB_JOURNAL_MODE") or "").strip().lower()
    if mode:
        log.info("SQLite journal_mode override via DEVOPS_DB_JOURNAL_MODE=%s", mode)
        return mode
    return "wal"


def _build_sqlite_db(path: str):
    """构建一条完整初始化的 SQLite 主连接（connect + PRAGMA 重放 + schema 包装）。

    接受路径参数：惰性代理在建连时解析 DEVOPS_DB_PATH 后传入。

    db_health 自愈代理在 WAL/SHM 失效（overlayfs 删旧建新替换）后用本工厂
    重建连接：PRAGMA 与 SchemaSQLiteDB 包装必须随连接重放，否则重建后的
    连接行为与启动时不一致（Issue #3588）。
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    # WAL + busy_timeout + synchronous=FULL：跨进程写安全，commit 时 fsync WAL
    try:
        conn.execute(f"PRAGMA journal_mode={_sqlite_journal_mode()}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")  # FULL: commit 时 fsync WAL 到磁盘
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception as e:
        log.warning(f"SQLite pragma setup failed: {e}")
    # 分 Schema 灰度开关（Issue #699 Phase 2）：启用后 SQLite 表名带 mgmt_/rt_/ins_ 前缀，
    # 业务代码经 kernel/db.qualify_sql 运行时改写，零改动无感。
    try:
        from src.kernel.db import schema_mode_enabled, SchemaSQLiteDB
        if schema_mode_enabled():
            conn = SchemaSQLiteDB(conn)
    except Exception as e:
        log.warning(f"Schema mode wrap failed (fallback to plain sqlite): {e}")
    return conn


# ── 惰性连接（PEP 562 风格的代理）───────────────────────────────────────────────
#
# 历史实现在这里**模块级**建连：`import src.kernel.session_manager` 的瞬间就打开
# 数据库并把 inode 绑死。全仓 103 个文件在 module level 写
# `from src.kernel.session_manager import db`，于是：
#
#   1) 测试里 conftest 重建 data/test_sessions.db 之后，那 103 处早先绑定的连接
#      仍指向**已 unlink 的旧 inode**，seed 进新库的行查不出来 → 表现成「单跑绿、
#      整域跑红」的顺序敏感（tests/mgmt 整域 762 failed，单文件全绿）。
#   2) DEVOPS_DB_PATH / DEVOPS_DB_BACKEND 必须在**任何**模块 import 之前定型，
#      否则后续改环境变量不生效 —— 测试只能在 conftest 顶部靠「先删库再导入」
#      这种脆弱顺序来规避。
#
# 改为惰性代理：模块级 `db` 是一个永不持有连接的壳，首次**真正使用**（execute /
# cursor / commit …）时才建连。这样模块级 `from ... import db` 拿到的是代理，
# 重建库/换 env 之后下一次访问自然拿到新连接，顺序不再敏感。
#
# 兼容性：代理转发全部属性，`db.execute(...)` / `with db_lock:` / `db.close()` 等
# 既有写法一字不改；`isinstance(db, sqlite3.Connection)` 这类反射判断会失败，
# 全仓无此用法（有则用 db.raw 取底层连接）。


class _LazyDB:
    """惰性数据库代理：首次访问属性时才建立真实连接。

    线程安全：双检锁（模块内只有一个实例，竞争仅发生在首次访问）。
    reset() 用于显式丢弃当前连接（测试重建库后调用，下次访问自动重连）。
    """

    __slots__ = ("_real", "_lock", "_building")

    def __init__(self) -> None:
        object.__setattr__(self, "_real", None)
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_building", False)

    def _build(self):
        """建立真实连接（MySQL RDS 或 SQLite），按当前环境变量决定。"""
        if _is_mysql_backend():
            from src.kernel.mysql_compat import MySQLCompatDB, mysql_config
            real = MySQLCompatDB(**mysql_config())
            log.info(
                "DB backend: MySQL RDS (%s:%s/%s)",
                mysql_config()["host"], mysql_config()["port"], mysql_config()["database"],
            )
            return real

        # 路径在**建连时**读，而不是 import 时——测试换 DEVOPS_DB_PATH 才有效。
        path = os.environ.get(
            "DEVOPS_DB_PATH",
            os.path.join(os.path.dirname(__file__), "..", "..", "data/sessions.db"),
        )
        path = os.path.abspath(path)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception:
                pass
        # Issue #3588：主连接包自愈代理——WAL/SHM 被 overlayfs 删旧建新替换后，
        # 持久 fd 指向已删除 inode → disk I/O error 持续 500；代理检测（异常 +
        # inode 周期校验双条件）后自动重建连接，factory=完整初始化（PRAGMA 与
        # Schema 包装随连接重放）。446 处调用点均为 `from session_manager import
        # db` 的对象引用传递，代理一经建立即全局生效。
        from src.kernel.db_health import wrap_with_health_tracking
        return wrap_with_health_tracking(lambda: _build_sqlite_db(path), path)

    def get(self):
        """返回真实连接（必要时建立）。"""
        real = object.__getattribute__(self, "_real")
        if real is not None:
            return real
        lock = object.__getattribute__(self, "_lock")
        with lock:
            real = object.__getattribute__(self, "_real")
            if real is None:
                real = self._build()
                object.__setattr__(self, "_real", real)
        return real

    @property
    def raw(self):
        """底层真实连接（仅供需要 isinstance 判断的极少数调用方）。"""
        return self.get()

    def __getattr__(self, name: str):
        # __slots__ 下 _real/_lock 走 object.__getattribute__，不会落到这里
        return getattr(self.get(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self.get(), name, value)

    def __repr__(self) -> str:
        real = object.__getattribute__(self, "_real")
        state = "connected" if real is not None else "not-yet-connected"
        return f"<LazyDB {state}>"


db = _LazyDB()


def reset_db_connection() -> None:
    """丢弃当前连接，下次访问自动重连（测试重建库后调用）。"""
    lock = object.__getattribute__(db, "_lock")
    with lock:
        real = object.__getattribute__(db, "_real")
        object.__setattr__(db, "_real", None)
    if real is not None:
        try:
            real.close()
        except Exception:
            pass


def db_connection_state() -> str:
    """诊断用：'connected' / 'not-yet-connected'。"""
    return "connected" if object.__getattribute__(db, "_real") is not None else "not-yet-connected"
# chat_agent_session / repo_im_group_binding 归运行域：旧表迁移、建表、兼容列补齐语句只在
# runtime public 一份。
# unified_user_mapping 归 mgmt 域：同上，由该域 public 引导。
#
# 三库独立（GRAPE_DOMAIN_LIB=1）时必须按**本进程所属域**分流：历史实现不分进程，把三张表
# 全建到本进程库里，于是 mgmt 的表在 runtime / insight 库里也各留一份旧 schema
# （user_id int）空表 —— 表归属排查会被它误导，删掉重启又会重新长回来。
# 单库 / 测试拓扑（无当前域）保持原行为：全建，避免测试库缺表。
try:
    from src.kernel import db_registry as _dbreg
    _proc_dom = _dbreg.current_domain() if _dbreg.lib_enabled() else None
except Exception:
    _proc_dom = None

_schema_bootstrapped = False


def _bootstrap_domain_schema(*_hook_args) -> None:
    """按本进程所属域引导会话 / 群绑 / 用户映射三张表。

    建表、旧表改名、兼容列补齐语句只在 owner 域（runtime / mgmt public）各一份，
    这里经 kernel 端口调用，kernel 不 import domains。

    历史实现是 import 期直接调域函数；端口化后 import 期解析端口会撞 PortNotBound
    （装配层尚未绑定时），故改为由 _base.on_bind 在端口绑定后驱动，并自带幂等闸：
    装配层重复 bind（多进程 / 重复启动路径 / 测试）不会重复建表。
    """
    global _schema_bootstrapped
    if _schema_bootstrapped:
        return
    if not (_im.is_bound() and _accounts.is_bound()):
        return
    if _proc_dom in (None, "", "runtime"):
        _im.ensure_chat_session_schema(db)
        _im.ensure_repo_im_binding_schema(db)
    if _proc_dom in (None, "", "mgmt"):
        _accounts.ensure_user_mapping_schema(db)
    db.commit()
    _schema_bootstrapped = True


# import 顺序不定：本模块可能早于装配（常见，db_setup 顶层就 import db）也可能晚于装配。
# 两条路径都覆盖：注册在绑钩子 + 立即试跑一次（未绑定则空转，绑定后钩子再补）。
_ports_base.on_bind(_bootstrap_domain_schema)
_bootstrap_domain_schema()

MAX_HISTORY = 100


def get_or_create_session(repo_key, source, repository, backend=""):
    """获取或创建 repo 级 Chat 会话（chat_agent_session，飞书 Chat 用）"""
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    # chat_agent_session 归运行域：读/写走具名查询与写原语（任意拓扑）
    row = _im.read_one_any("chat_session_by_repo", (repo_key,))
    if row:
        sid, hist_json, bsid, status = row
        history = []
        if hist_json:
            try:
                history = json.loads(hist_json)
            except json.JSONDecodeError:
                log.warning(f"session {sid} event_history corrupted (len={len(hist_json)}), resetting to []")
                _im.exec_any("chat_session_history_reset", ("[]", sid))
        if status == "failed":
            _im.exec_any("chat_session_set_status", ("idle", sid))
        return sid, history, bsid or "", "", False
    sid = f"sess-{repo_key}-{int(time.time()*1000000)}"
    now = datetime.now().isoformat()
    _im.exec_any("chat_session_insert",
                 (sid, repo_key, source, repository, "idle", "[]", now, now))
    return sid, [], "", "", True


def update_session(sid, history, status, oc_session_id=None, backend=""):
    """更新 Chat 会话（runtime_session_id 存当前 backend 会话）"""
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    if oc_session_id:
        _im.exec_any("chat_session_update_state",
                     (json.dumps(history), status, oc_session_id,
                      datetime.now().isoformat(), sid))
    else:
        _im.exec_any("chat_session_update_state_noreid",
                     (json.dumps(history), status, datetime.now().isoformat(), sid))


def dump_history_to_file(repo_key: str, history: list, account: str = ""):
    """将 event_history 写入 workspace 目录（按 account 分目录，每文件最多 10 条）"""
    from src.kernel.config import WORKSPACE_DIR
    from src.kernel.repo_key import parse_repo_key
    clean = []
    for r in history:
        entry = {k: v for k, v in r.items() if k != "oc_session_id"}
        clean.append(entry)
    _provider, _owner, _repo = parse_repo_key(repo_key)
    repo_name = f"{_owner}_{_repo}" if _owner and _repo else repo_key.replace("gitcode:", "").replace("github:", "").replace("/", "_")
    account_dir = account or "default"
    hist_dir = os.path.join(WORKSPACE_DIR, "history", account_dir, _provider or "gitcode", repo_name)
    try:
        os.makedirs(hist_dir, exist_ok=True)
        # Write most recent 10 to base file, older chunks to numbered files
        remaining = clean[:]
        chunk_idx = 0
        while remaining:
            chunk = remaining[-10:]
            remaining = remaining[:-10]
            fname = "session_history.json" if chunk_idx == 0 else f"session_history_{chunk_idx}.json"
            with open(os.path.join(hist_dir, fname), "w", encoding="utf-8") as f:
                json.dump(chunk, f, indent=2, ensure_ascii=False)
            chunk_idx += 1
        # Clean up stale numbered files beyond what we just wrote
        for fname in os.listdir(hist_dir):
            if fname.startswith("session_history_") and fname.endswith(".json"):
                idx = fname.replace("session_history_", "").replace(".json", "")
                if idx.isdigit() and int(idx) >= chunk_idx:
                    os.remove(os.path.join(hist_dir, fname))
    except Exception as e:
        log.warning(f"Failed to dump history: {e}")


def get_history_file_list(repo_key: str, account: str = "") -> list[str]:
    """返回可用的历史文件路径列表（最新在前）"""
    from src.kernel.config import WORKSPACE_DIR
    from src.kernel.repo_key import parse_repo_key
    _provider, _owner, _repo = parse_repo_key(repo_key)
    repo_name = f"{_owner}_{_repo}" if _owner and _repo else repo_key.replace("gitcode:", "").replace("github:", "").replace("/", "_")
    account_dir = account or "default"
    hist_dir = os.path.join(WORKSPACE_DIR, "history", account_dir, _provider or "gitcode", repo_name)
    files = []
    try:
        if os.path.isdir(hist_dir):
            for f in sorted(os.listdir(hist_dir), reverse=True):
                if f.endswith(".json"):
                    files.append(os.path.join(hist_dir, f))
    except:
        pass
    return files or []


def _u2m_conn():
    """unified_user_mapping 所在连接（该表归 mgmt 域）。

    - 单库/视图期（mgmt 无自有 DSN）：返回本模块 db —— 表就在本库，锁/事务语义不变；
    - 三库独立且本进程不是 mgmt 域：返回 None → 由 mgmt public 原语走自有 DSN；
    - 本进程就是 mgmt 域：返回本模块 db（同域写必须留在本地事务，与 domain_read 口径一致）。
    """
    try:
        from src.kernel import db_registry
        if db_registry.uses_own_dsn("mgmt") and db_registry.current_domain() != "mgmt":
            return None
    except Exception:
        pass
    return db


def _u2m_call(fn, *args, **kwargs):
    """按拓扑调用 mgmt public 的 um 原语：单库复用本模块连接（持 db_lock），三库走自有 DSN。"""
    _conn = _u2m_conn()
    if _conn is None:
        return fn(*args, **kwargs)
    with db_lock:
        return fn(*args, conn=_conn, **kwargs)


def get_feishu_open_id(gitcode_user, app_id=""):
    """获取 GitCode 用户名对应的飞书 open_id（按 gitcode_user + app_id 过滤，见 feishu-app-scoped-openid-fix.md）。

    app_id 非空 → 仅返回该 App 的 open_id；为空 → 向后兼容（不按 app 过滤）。
    回退链（FR-20.8 gitcode_user→github_login / FR-22.5 注册名→user_id）由 mgmt 域原语实现，
    本函数只做拓扑选择（见 src/domains/mgmt/public.resolve_feishu_open_id）。
    """
    if not gitcode_user:
        return None
    return _u2m_call(_accounts.resolve_feishu_open_id, gitcode_user, app_id)


def bind_feishu_identity(user_id, open_id, feishu_name="", app_id=""):
    """FR-18.7: 注册名语境——只写飞书三列，不碰平台列（gitcode_user/github_login）。

    供 feishu_onboard / handler_async p2p 确认调用。
    扫码绑定飞书凭什么动 gitcode_user？语义上就是错的。
    """
    if not user_id:
        return False
    # P4：unified_user_mapping 归 mgmt 域 —— 写语句在 mgmt public，本函数只做拓扑选择 + 提交
    _args = (user_id, open_id, feishu_name, app_id, datetime.now().isoformat())
    _conn = _u2m_conn()
    if _conn is None:
        _accounts.bind_feishu_mapping_row(*_args)
        return True
    with db_lock:
        _accounts.bind_feishu_mapping_row(*_args, conn=_conn)
        db.commit()
        return True


def update_unified_user_info(gitcode_user, open_id="", feishu_name="", gitcode_nickname="", app_id=""):
    """更新 GitCode → Feishu 用户映射（user_id = grape_accounts.id，按 gitcode_user 关联）。

    管理 unified_user_mapping 整行：gitcode_user/gitcode_nickname/feishu_open_id/feishu_name/app_id/updated_at。
    gitcode_nickname 优先取传入值，否则从 grape_accounts.nickname 获取。
    user_id 始终为真实 grape_accounts.id：若该 gitcode_user 未在 grape_accounts 中，
    则尝试按已有记录更新；两者都不可得时跳过（不写入负数占位）。

    判定逻辑（feishu-app-scoped-openid-fix.md §2.2，"清空"由 app_id 变化触发，而非 open_id 为空）：
      - app_id 非空 且 != 存储 app_id → 刷新 app_id，清空 open_id/feishu_name（换 app 需重确认）；
      - open_id 非空 → 写 open_id + feishu_name(有则写) + app_id(有则写)；
      - 否则（open_id 空且 app 未变，如 OAuth 登录）→ 仅刷 gitcode_nickname/updated_at，保留 open_id/app_id。
    """
    # D32: 从 grape_accounts 读 user_id + nickname（表归 mgmt 域：走该域原语，按拓扑选连接）
    user_id = 0
    try:
        row = _u2m_call(_accounts.account_auth_row, gitcode_user) if gitcode_user else None
        if row:
            user_id = row[0]
            if not gitcode_nickname:
                gitcode_nickname = row[1] or ""
    except Exception as exc:  # noqa: BLE001 - 账号表不可用时不阻断映射更新（原实现同样吞掉）
        log.debug(f"account lookup for {gitcode_user} skipped: {exc}")

    # FR-11.2: 空入参早退，防止空串命中空串列行
    if not gitcode_user:
        return False
    # P4：判定 + 写语句都在 mgmt public（sync_user_mapping），本函数只做拓扑选择与提交
    _args = (gitcode_user, open_id, feishu_name, gitcode_nickname, app_id, user_id,
             datetime.now().isoformat())
    _conn = _u2m_conn()
    if _conn is None:
        _res = _accounts.sync_user_mapping(*_args)
    else:
        with db_lock:
            _res = _accounts.sync_user_mapping(*_args, conn=_conn)
            db.commit()
    if _res.get("action") == "skipped":
        # 无 user_id 也无已有记录：跳过（user_id 必须为真实 grape_accounts.id）
        log.info(f"update_unified_user_info: skip mapping for {gitcode_user} (no grape_accounts record)")
        return False
    return True


def set_feishu_open_id(repo_key, gitcode_user, open_id, feishu_name="", gitcode_nickname=""):
    """[deprecated 兼容别名] 旧名，语义见 update_unified_user_info；repo_key 参数已废弃忽略。"""
    return update_unified_user_info(gitcode_user, open_id=open_id, feishu_name=feishu_name,
                                    gitcode_nickname=gitcode_nickname)


def get_feishu_mapping(repo_key):
    """获取所有已知映射 {gitcode_user: {open_id, name, gitcode_nickname}}"""
    rows = _u2m_call(_accounts.list_user_mappings)
    return {row[0] or row[1]: {"open_id": row[2], "name": row[3], "gitcode_nickname": row[4] or ""} for row in rows}


def get_feishu_group_name_map(repo_key):
    """获取飞书群成员 {name: open_id}（从 unified_user_mapping 聚合得到）"""
    return {row[0]: row[1] for row in _u2m_call(_accounts.list_feishu_name_open_id_pairs)}


def get_auto_impl_enabled(repo_key):
    """查询仓库是否开启了自动实现"""
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    # repo_im_group_binding 归运行域：读走具名查询（任意拓扑）
    row = _im.read_one_any("repo_im_auto_impl", (repo_key,))
    return bool(row[0]) if row else False


def set_auto_impl_enabled(repo_key, enabled):
    """设置仓库的自动实现开关"""
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    _im.exec_any("repo_im_set_auto_impl", (int(enabled), repo_key))


def get_notify_enabled(repo_key):
    """查询仓库是否开启了飞书通知"""
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    row = _im.read_one_any("repo_im_notify", (repo_key,))
    # default to 0 (False) if column is NULL
    return bool(row[0]) if row and row[0] is not None else False


def set_notify_enabled(repo_key, enabled):
    """设置仓库的飞书通知开关"""
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    _im.exec_any("repo_im_set_notify", (int(enabled), repo_key))


# ====== repo_im_group_binding UPSERT (FR-CQ-2) ======

def upsert_repo_im_binding(chat_id: str, repo_key: str, source: str,
                           bind_by: str, app_id: str):
    """群绑定仓库 UPSERT（owner 域原语自提交，调用方无需再 commit）。

    FR-14.1: ON CONFLICT 只更新绑定列，保留 notify_enabled/auto_impl_enabled 用户配置。
    原实现要求调用方持 db_lock 且负责提交；三库独立后语句落在运行域连接上（autocommit），
    故改为自提交。调用方仍可在外层持 db_lock（无害），但不再是本函数正确性的前提。
    """
    _im.upsert_repo_im_binding(chat_id, repo_key, source, bind_by, app_id)


def _is_constraint_error(exc: Exception) -> bool:
    """FR-20.17: 判断异常是否为约束冲突（兼容 SQLite IntegrityError / MySQL 1062）。"""
    msg = str(exc).lower()
    return 'integrity' in msg or 'constraint' in msg or 'duplicate' in msg or '1062' in str(exc)


# ====== chat_conversations CRUD ======

class ConversationDuplicateUnresolved(RuntimeError):
    """Issue #4183：写会话撞唯一键（1062）后按业务键仍查不到既有行。

    属不可静默的数据不一致（部分唯一索引与业务键查询结果互斥），调用方据此升级为
    可观测事件并保留旁路；不再复用「最近 N 条」窗口做二次查找。
    """

    def __init__(self, message: str = "", *, user_id: int = 0, repo_key: str = "",
                 issue_number=0, conv_type: str = ""):
        super().__init__(message or "conversation duplicate unresolved")
        self.user_id = user_id
        self.repo_key = repo_key
        self.issue_number = issue_number
        self.conv_type = conv_type


def _conv_row_to_dict(r) -> dict:
    return {
        "id": r[0], "user_id": r[1],
        "agent_session_id": r[2] or "", "runtime_session_id": r[3] or "",
        "user_gitcode_token": r[4] or "", "repo_key": r[5] or "",
        "im_channel_id": r[6], "title": r[7], "status": r[8],
        "created_at": normalize_event_time(r[9]),
        "updated_at": normalize_event_time(r[10]),
        "conv_type": r[11] or "general", "issue_number": r[12] or 0,
    }


def get_conversation_by_repo_issue_type(user_id: int, repo_key: str = "",
                                        issue_number=0, conv_type: str = "task") -> dict | None:
    """按 (user_id, repo_key, issue_number, conv_type) 精确取任务会话（Issue #4183）。

    存在性判断不能用「最近 N 条」窗口：老 Issue 重派时会话早已滑出窗口，
    导致误判为不存在并撞部分唯一索引。业务键与 `idx_chat_conv_repo_issue`
    列序一致，issue_number 统一 native 归一（#3055：不 int() 强转 ident）。
    """
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(repo_key) if repo_key else ""
    if not rk:
        return None
    row = _im.read_one_any(
        "conversation_by_repo_issue_type",
        (user_id, rk, native_issue_number(issue_number or 0), conv_type or "task"))
    return _conv_row_to_dict(row) if row else None


def create_conversation(user_id: int, repo_key: str = "", im_channel_id: str = "", title: str = "",
                        user_gitcode_token: str = "", agent_session_id: str = "",
                        conv_type: str = "general", issue_number: int = 0) -> str:
    from src.kernel.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    import uuid
    cid = str(uuid.uuid4())
    _conv_type = conv_type or "general"
    _issue_no = issue_number or 0
    try:
        _im.exec_any("conversation_insert",
                     (cid, user_id, agent_session_id, "", repo_key, im_channel_id, title,
                      user_gitcode_token, _conv_type, _issue_no))
        return cid
    except Exception as exc:
        # MySQL 上 (user_id, repo_key, issue_number, conv_type) 是唯一键（sqlite 测试库无此约束）：
        # 命中即「会话已存在」，返回既有 id —— 创建会话本就是 get-or-create 语义。
        msg = str(exc)
        if not ("1062" in msg or "Duplicate" in msg or "UNIQUE" in msg):
            raise
        # Issue #4183：1062 兜底复用同一具名查询（业务键精确检索），不得再走
        # get_user_conversations(limit=50) 窗口——旧 Issue 的会话已滑出窗口，
        # 复用窗口重查必然再次查不到，会把原本可恢复的 get-or-create 变成失败。
        _want = native_issue_number(_issue_no)
        row = _im.read_one_any(
            "conversation_by_repo_issue_type", (user_id, repo_key, _want, _conv_type))
        if row:
            return str(row[0])
        raise ConversationDuplicateUnresolved(
            f"duplicate conversation not found by key "
            f"(user_id={user_id}, repo_key={repo_key}, "
            f"issue_number={_want}, conv_type={_conv_type})",
            user_id=user_id, repo_key=repo_key,
            issue_number=_want, conv_type=_conv_type)


def get_user_conversations(user_id: int, repo_key: str = "", limit: int = 20) -> list[dict]:
    from src.kernel.repo_key import to_repo_key
    if repo_key:
        repo_key = to_repo_key(repo_key)
    # chat_conversations 归运行域：具名查询走任意拓扑（三库独立后无视图可读，也不再保留本地兜底 SQL）
    if repo_key:
        rows = _im.read_all_any("conversations_by_user_repo", (user_id, repo_key, limit))
    else:
        rows = _im.read_all_any("conversations_by_user", (user_id, limit))
    return [_conv_row_to_dict(r) for r in rows]


def get_conversation(conv_id: str) -> dict | None:
    row = _im.read_one_any("conversation_by_id", (conv_id,))
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "agent_session_id": row[2] or "", "runtime_session_id": row[3] or "", "user_gitcode_token": row[4] or "", "repo_key": row[5] or "",
            "im_channel_id": row[6], "title": row[7], "status": row[8], "created_at": normalize_event_time(row[9]), "updated_at": normalize_event_time(row[10]),
            "conv_type": row[11] or "general", "issue_number": row[12] or 0}


def get_conversation_by_im(im_channel_id: str, user_id: str = "") -> dict | None:
    """按 IM 渠道 + 用户查询会话（IM chat 场景用，幂等复用）"""
    if str(user_id or "") not in ("", "0"):  # D105：uuid 字符串哨兵（>0 会 TypeError）
        row = _im.read_one_any("conversation_by_im_user", (im_channel_id, user_id))
    else:
        row = _im.read_one_any("conversation_by_im", (im_channel_id,))
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "agent_session_id": row[2] or "", "runtime_session_id": row[3] or "", "user_gitcode_token": row[4] or "", "repo_key": row[5] or "", "im_channel_id": row[6],
            "title": row[7], "status": row[8], "created_at": normalize_event_time(row[9]), "updated_at": normalize_event_time(row[10]),
            "conv_type": row[11] or "general", "issue_number": row[12] or 0}


def update_conversation(conv_id: str, **kwargs):
    # 可写列白名单与「updated_at 同步刷新」语义在 owner 域原语里（见 runtime public）
    _im.update_conversation_fields(conv_id, kwargs)


def touch_conversation(conv_id: str):
    _im.exec_any("conversation_touch", (conv_id,))


def mark_task_processing_by_conversation(conv_id: str):
    """任务会话收到真实用户消息 → tasks.active → processing（task-status-processing-trigger-fix.md §3.1）。

    幂等、仅 active→processing、限定 conv 属主本人任务、不回翻终态。
    优先按 conversation_id 匹配，无则按 repo_key+issue_number 回退。
    """
    if not conv_id:
        return
    from src.kernel.repo_key import to_repo_key
    row = _im.read_one_any("conversation_brief", (conv_id,))
    if not row or row[2] != "task" or not row[3]:
        return
    conv_user_id, repo_key, _ct, issue_number = row
    _im.mark_issue_processing(conv_id, conv_user_id, to_repo_key(repo_key), issue_number)


def join_feishu_chat(chat_id: str, conv_id: str, user_id: "int | str") -> bool:
    """原子切换飞书接入指针（feishu-web-chat-unified-design.md §3.1）。

    im_channel_id 从"永久绑定"降级为"当前飞书接入指针"：任一时刻一个飞书接入点
    （chat_id）只能指向一条 conversation。校验属主 + 清旧 + 设新在同一事务内完成，防竞态。
    """
    ok = _im.join_feishu_chat(chat_id, conv_id, user_id)
    if not ok:
        log.warning(
            f"join_feishu_chat deny: conv={conv_id[:40] if conv_id else ''} "
            f"uid={user_id} chat={chat_id[:20] if chat_id else ''}"
        )
    return ok


# ====== chat_messages CRUD ======

def add_message(conversation_id: str, role: str, content: str, task_id: str = "", generate_status: int = None) -> int:
    # chat_messages 归运行域：写原语返回自增主键（任意拓扑，两条插入分支保持在 owner 域）
    return _im.append_chat_message(conversation_id, role, content, task_id, generate_status)


_MSG_COLS = "id, conversation_id, role, content, created_at, task_id, generate_status, deleted"


def _row_to_msg(r) -> dict:
    return {"id": r[0], "conversation_id": r[1], "role": r[2], "content": r[3],
            "created_at": normalize_event_time(r[4]), "task_id": r[5] or "",
            "generate_status": r[6], "deleted": int(r[7] or 0)}


def get_message(msg_id: int) -> dict | None:
    """按主键取单条消息（Issue #442 撤回用）。"""
    row = _im.read_one_any("message_by_id", (msg_id,))
    return _row_to_msg(row) if row else None


def recall_message(msg_id: int) -> bool:
    """软删除一条消息（Issue #442：撤回 = deleted=1，保留原文供重新编辑）。"""
    return _im.exec_any("message_recall", (msg_id,)) > 0


def recall_agent_replies_by_task(task_id: str) -> int:
    """软删除同一轮（task_id 相同）的 agent 回复消息（Issue #442）。"""
    if not task_id:
        return 0
    return _im.exec_any("message_recall_by_task", (task_id,))


def is_task_recalled(task_id: str) -> bool:
    """该轮次用户消息是否已撤回（agent_fn 落库前校验，Issue #442）。"""
    if not task_id:
        return False
    row = _im.read_one_any("message_user_deleted", (task_id,))
    return bool(row and row[0])


def get_messages(conversation_id: str, limit: int = 50, before_id: int = 0) -> list[dict]:
    if before_id > 0:
        rows = _im.read_all_any("messages_page_before", (conversation_id, before_id, limit))
    else:
        rows = _im.read_all_any("messages_page", (conversation_id, limit))
    rows = list(rows or [])
    rows.reverse()
    return [_row_to_msg(r) for r in rows]


def delete_messages_by_conv(conversation_id: str):
    _im.exec_any("messages_delete_by_conversation", (conversation_id,))


def delete_conversation(conv_id: str):
    """删除会话 DB 记录 + 清理文件系统工作目录（防孤儿目录堆积）。"""
    # 先查 user_id（目录路径需要），删 DB 后就查不到了
    conv = get_conversation(conv_id)
    _im.exec_any("conversation_delete", (conv_id,))
    # 清理 chat 工作目录：{CHAT_CWD_BASE}/chat/{user_id}/{conv_id}/
    if conv and conv.get("user_id"):
        try:
            from src.kernel.config import CHAT_CWD_BASE
            wsd = os.path.join(CHAT_CWD_BASE, "chat", str(conv["user_id"]), conv_id)
            if os.path.isdir(wsd):
                shutil.rmtree(wsd, ignore_errors=True)
        except Exception as e:
            log.warning(f"delete_conversation cleanup wsd failed: {e}")


# ====== chat_config CRUD（合并进 chat_agent_session）======

def _chat_config_candidate_rows(user_id: int):
    """按优先级返回该用户的全局 Chat 配置候选行。

    优先级：① 写路径那一行（`id='sess-cfg-{user_id}'`）② 历史遗留行（`user_id` 匹配但 id 错位，
    按 `last_active_at DESC, id DESC` 明确排序）。

    背景：本表语义上"一人一行"，但历史上移除过 `UNIQUE(repo_key)`，且经历过多次迁移
    （id 是字符串，不随 user_id 重映射变化），因此同一用户可能有多行并存；写路径只写 ①，
    而原先的读路径是 `... WHERE user_id=? ... LIMIT 1`（**无 ORDER BY**）→ 读到哪行由存储顺序
    决定，出现"界面改了配置读不回来"（读路径按写入的主键行取，避免同一用户多行时读到旧行）。

    返回 [(row_id, (runtime_id, skill, skill_id, engine_uid)), ...]

    A2 收尾：im_* 三列已不是凭证真相源（改读 grape_t_channel_installation），这里不再 SELECT。
    Issue #868 (T10)：末位补 engine_uid（uid 优先、runtime_id 兜底双读）；
    旧库缺列（迁移未跑）时降级为不含 engine_uid 的列集，行元组末位补 ''。
    """
    seen, out = set(), []
    # 必须**同时**校验 user_id：云端存在 id 与 user_id 跨空间错位的行（如 id='sess-cfg-37' 却属于 user_id=1），
    # 只按 id 取会把**别人的配置（含飞书 app_id/secret）**当成自己的（实测 user 37 读到 user 1 的行）。
    # 两条具名查询都在 owner 域（chat_agent_session 归运行域），列序与原 SQL 一致。
    # Issue #868 (T10)：uid 版查询优先；旧库缺列降级 legacy 查询（行尾补空 uid 占位）
    for rows in (
        _chat_config_query_rows("chat_config_row_canonical", "chat_config_row_canonical_legacy",
                                (f"sess-cfg-{user_id}", user_id)),
        _chat_config_query_rows("chat_config_rows_by_user", "chat_config_rows_by_user_legacy",
                                (user_id,)),
    ):
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                out.append((r[0], tuple(r[1:])))
    return out


def _chat_config_query_rows(uid_name: str, legacy_name: str, params: tuple) -> list:
    """uid 版具名查询失败（旧库缺 engine_uid 列）时降级 legacy 版，行尾补 '' 占位。"""
    try:
        return [tuple(r) for r in _im.read_all_any(uid_name, params)]
    except Exception:
        return [tuple(r) + ("",) for r in _im.read_all_any(legacy_name, params)]


# Issue #4045：悬空 runtime_id 的 WARNING 按 id 去重（进程内，有界）。
# 迁移 009 上线后存量归零，残留只有每实例首条；后续命中降级 debug，避免每请求刷屏
# 掩盖真正的「未知引擎」告警。容量上限 256，超限整体清空以保持内存有界。
_DANGLING_WARN_SEEN: dict = {}
_DANGLING_WARN_LIMIT = 256
_DANGLING_WARN_LOCK = threading.Lock()


def _mark_dangling_warned(runtime_id: int) -> bool:
    """首次命中返回 True（打 WARNING），其后返回 False（打 debug）。"""
    with _DANGLING_WARN_LOCK:
        if runtime_id in _DANGLING_WARN_SEEN:
            return False
        if len(_DANGLING_WARN_SEEN) >= _DANGLING_WARN_LIMIT:
            _DANGLING_WARN_SEEN.clear()
        _DANGLING_WARN_SEEN[runtime_id] = 1
        return True


def _chat_config_backfill_runtime_id(row_id: str, runtime_id: int) -> None:
    """Issue #4045：uid 重解析命中后把 runtime_id 回写配置行（一次写，之后行内已一致不再写）。"""
    try:
        _im.exec_any("chat_config_runtime_id_backfill", (int(runtime_id), row_id))
    except Exception as e:  # noqa: BLE001  # 回写失败不影响本次读路径结果（下次读会再试）
        log.warning(f"get_chat_config: backfill runtime_id failed (row={row_id}): {e}")


def _chat_config_row_engine(runtime_id: int, engine_uid: str = "", c=None, row_id: str = "") -> tuple:
    """解析配置行指向的引擎，返回 (backend_name, usable, reason, engine_runtime_id)。

    Issue #868 (T10)：engine_uid 优先（uid 命中且引擎可用即用 uid 轨道），
    uid 失效/未命中回退 runtime_id（id 兜底）。
    usable=False 覆盖既有的两种"按未配置处理"情形：引擎行已不存在、引擎名不是已知引擎。

    Issue #4045 三态 + 自愈：
      · uid 命中可用引擎 → usable=True、reason=""，且命中 id 与行内 runtime_id 不一致时
        （且调用方给了 `row_id`）**回写 runtime_id**——重注册/换行后引用自动收敛，不再悬空；
        第 4 位 engine_runtime_id 回传**重解析后的真实引擎 id**，避免调用方继续用旧 id；
      · 悬空且无可解析 uid → usable=False、reason="engine_missing"；
      · `runtime_id=0` 且无 uid → usable=True（沿用「未配置」候选语义）、reason="unbound"。
    """
    reason_unbound = "unbound"
    reason_missing = "engine_missing"
    _uid_dangling = False
    if engine_uid:
        urow = None
        _uid_query_ok = True
        try:
            urow = _im.read_one_any("agent_runtime_row_by_uid", (engine_uid,))
        except Exception as e:  # noqa: BLE001  # 旧库缺 agent_runtimes.engine_uid 列：uid 轨道降级
            _uid_query_ok = False
            log.debug(f"get_chat_config: engine_uid track unavailable ({e})")
        if urow and urow[1]:
            _un = (urow[1] or "").strip().lower()
            if _engines.is_known_engine_name(_un):
                _uid_rid = int(urow[0] or 0)
                if row_id and _uid_rid and _uid_rid != int(runtime_id or 0):
                    _chat_config_backfill_runtime_id(row_id, _uid_rid)
                return urow[1], True, "", _uid_rid
            log.warning(f"get_chat_config: engine_uid={engine_uid} 指向未知引擎 '{urow[1]}'，回退 runtime_id={runtime_id}")
        elif _uid_query_ok:
            _uid_dangling = True
            log.warning(f"get_chat_config: engine_uid={engine_uid} 失效（引擎行已删除），回退 runtime_id={runtime_id}")
    if not runtime_id:
        # 真未配置：既没有 uid 线索也没有 runtime_id；uid 悬空则归因 engine_missing
        return "", True, (reason_missing if _uid_dangling else reason_unbound), 0
    brow = _im.read_one_any("agent_runtime_name_by_id", (runtime_id,))
    if not brow or not brow[0]:
        # runtime_id 指向的引擎行已不存在（被删除）：视为未绑定，走本机直连兜底。
        # Issue #4045：WARNING 按 runtime_id 去重（首条 warning，其后 debug），不再每请求刷屏。
        if _mark_dangling_warned(int(runtime_id)):
            log.warning(f"get_chat_config: runtime_id={runtime_id} 不存在（引擎行已删除），按未配置处理")
        else:
            log.debug(f"get_chat_config: runtime_id={runtime_id} 不存在（引擎行已删除），按未配置处理")
        return "", False, reason_missing, 0
    _n = (brow[0] or "").strip().lower()
    if not _engines.is_known_engine_name(_n):
        # 幽灵/未知引擎（如历史遗留 'default'）：视为未绑定，走本机直连/空兜底
        log.warning(f"get_chat_config: runtime_id={runtime_id} 指向未知引擎 '{brow[0]}'，按未配置处理")
        return "", False, reason_missing, 0
    return brow[0], True, "", int(runtime_id)


def _pick_chat_config_row(user_id: int) -> tuple:
    """按"写路径优先 + 不降级可用配置"挑出该用户的生效配置行。

    返回 (row_id, row, backend_name, reason)：
      · 有候选且其中一行引擎可用 → 该行（`_chat_config_candidate_rows` 的顺序 = 写路径行优先），
        reason 取该候选的解析结果（`""` 或 `"unbound"`）；
      · 候选全不可用           → 首个候选，runtime_id 置零（保持既有"按未配置处理"的兜底语义），
        reason="engine_missing"；
      · 无候选                 → (None, None, "", "")

    单独抽出来是为了让**诊断脚本/离线核查**能复用同一份逻辑（避免口径漂移）。
    """
    cands = _chat_config_candidate_rows(user_id)
    if not cands:
        return None, None, "", ""
    first_reason = ""
    for _idx, (rid, cand) in enumerate(cands):
        # Issue #868 (T10)：uid 优先（cand 末位 engine_uid），失效回退 runtime_id
        name, usable, reason, resolved_rid = _chat_config_row_engine(
            cand[0], cand[3] if len(cand) > 3 else "", row_id=rid)
        if usable:
            # Issue #4045：uid 重解析到另一台引擎时，返回行里的 runtime_id 用**解析结果**
            # （与已回写 DB 的值一致），避免上层继续拿旧悬空 id 去调度。
            if resolved_rid and int(cand[0] or 0) != int(resolved_rid):
                cand = (int(resolved_rid),) + cand[1:]
            return rid, cand, name, reason
        if _idx == 0:
            first_reason = reason
    rid, cand = cands[0]
    return rid, (0,) + cand[1:], "", (first_reason or "engine_missing")


def get_chat_config(user_id: int) -> dict:
    """获取用户全局 Chat 配置（repo_key='' 表示全局配置行）。

    engine-selection-design.md v2：不再写死 backend='opencode' 默认。
    无配置/未知引擎时，backend 从已注册引擎派生——优先本机直连（resolve_local_engine），
    无任何引擎则 backend=''（前端/执行侧显示"无 engine，不可用"，绝不隐式跑 opencode）。

    多行并存时（见 _chat_config_candidate_rows）：**优先读写路径那一行**，保证"写进去的配置一定
    读得回来"；该行引擎不可用时**不把可用配置降级**，退回最近活跃的遗留行；全部不可用才走兜底。

    Issue #4045：出参新增 `bound` / `reason`，让「配置绑定的引擎已失效」与「真未绑定」可区分：
      · `bound` = **配置行绑定是否有效**（与兜底 `runtime_id` 解耦：引擎失效行仍会走本机直连兜底，
        此时 `runtime_id` 可能是兜底引擎，但 `bound=False / reason=engine_missing` 如实归因）；
      · `reason` ∈ {"", "engine_missing", "unbound"}。
    """
    # 读侧止血（本仓 功能分支 保留的实现，见 ba7faf3e1e）：先精确取写路径那行
    # id='sess-cfg-{user_id}'（并校验 user_id，云端存在跨 id 空间错位行），取不到才退化。
    # 备注：weekly-08 线上另有一套等价实现（「已配置行优先 + id ASC」，见
    # chat-config-duplicate-row-unbind-fix-design.md）——同一问题的两种解法，本分支沿用下面这套
    # （canonical-first + 不降级；有 test_chat_config_row_selection.py 5 项钉住）。
    picked_id, row, backend_name, reason = _pick_chat_config_row(user_id)
    if row is None:
        # 无配置：本机直连兜底 + 幂等物化默认 runtime_id（chat-agent-backend-resolution-fix.md §3.1）
        cfg = _chat_config_with_local_backend("", 0, "", 0, "", user_id=user_id)
        if cfg.get("runtime_id"):
            try:
                _write_chat_config_row(user_id, runtime_id=cfg["runtime_id"])
            except Exception as e:
                log.warning(f"get_chat_config materialize default failed: {e}")
        cfg["bound"] = bool(cfg.get("runtime_id"))
        cfg["reason"] = "" if cfg["bound"] else "unbound"
        return cfg
    if picked_id != f"sess-cfg-{user_id}" and backend_name:
        # 命中历史遗留行（写路径碰不到它）——仍需迁移合并，见诊断报告第三步
        log.info(f"get_chat_config: user_id={user_id} 命中历史遗留配置行 '{picked_id}'"
                 f"（写路径行为 'sess-cfg-{user_id}'），建议迁移合并")
    cfg = _chat_config_with_local_backend(backend_name, row[0] or 0, row[1] or "", row[2] or 0,
                                          "", engine_uid=(row[3] or "") if len(row) > 3 else "",
                                          user_id=user_id)
    if reason in ("engine_missing", "unbound"):
        cfg["bound"] = False
        cfg["reason"] = reason
    else:
        cfg["bound"] = bool(cfg.get("runtime_id"))
        cfg["reason"] = "" if cfg["bound"] else "unbound"
    return cfg


def _chat_config_with_local_backend(backend, runtime_id, skill, skill_id,
                                    updated_at, engine_uid: str = "", user_id: int = 0) -> dict:
    """backend 为空的配置行：尝试本机直连引擎兜底（engine-selection-design.md §8.2）。

    有本机直连引擎 → backend=其类型名、runtime_id=其 id（恒 direct）；
    无 → backend=''、runtime_id=0（前端"无 engine，不可用"）。

    user_id>0 时按 per-user-random-default-engine 选：限定当前主控机直连引擎，
    按 user_id 稳定取模散开，避免全员滑向同一个默认引擎。

    注意：如果 runtime_id 已显式配置（>0），即使 backend 未知也不覆盖，
    由上游 get_chat_config 自行决定是否用 runtime_id 直接执行。
    """
    if not backend and not runtime_id:
        try:
            # Issue #3396：chat 兜底候选必须过 chat 角色过滤（engine_capabilities
            # 勾了 chat/all；无合规候选返回 0 = 前端"无 engine，不可用"，明确不静默）
            _rid = _engines.resolve_local_engine(user_id, agent_type="chat") or 0
            if _rid:
                backend = _engines.engine_name(_rid) or ""
                runtime_id = _rid
        except Exception:
            pass
    return {"backend": backend, "runtime_id": runtime_id, "skill": skill, "skill_id": skill_id,
            "engine_uid": engine_uid, "updated_at": updated_at}


def _resolve_chat_runtime_id(backend: str) -> int:
    """Chat backend → runtime_id（chat-agent-backend-resolution-fix.md §方案A）。

    实例名（含 `-` 后缀）→ scope=user 个人引擎；裸引擎名 → 优先本机直连（credential_id=0）；
    兜底按名/默认。修复：裸名 'opencode' 被解析成同名的共享全局 worker。
    queue_remote 强制模式下本机直连行（credential_id=0）无 claim 方=黑洞
    （chat-task-blackhole-local-direct-2026-08.md），跳过本机解析直接按名找远端 worker。

    Issue #3396：各级解析结果必须过 chat 角色过滤（engine_capabilities 勾了
    chat 或 all，空标签 fail-closed），不合规落下一级；全不合规返回 0——
    调用方给出明确提示，绝不静默调度到越出可选面的引擎。
    """
    if not backend:
        return 0
    _rid = _engines.resolve_instance_id(backend, scope="user")
    if _rid is not None and _engines.scope_of_runtime(_rid) == "user" \
            and _engines.engine_allows_role(_rid, "chat"):
        return _rid
    if "-" not in backend:
        if _engines.get_execution_mode() != _engines.EXEC_MODE_REMOTE:
            _local = _engines.get_local_direct_runtime_id(backend)
            if _local and _engines.engine_allows_role(_local, "chat"):
                return _local
    _by_name = _engines.get_runtime_id_by_name(backend) or 0
    if _by_name and _engines.engine_allows_role(_by_name, "chat"):
        return _by_name
    _default = _engines.ensure_default_backend(backend) or 0
    if _default and _engines.engine_allows_role(_default, "chat"):
        return _default
    if _by_name or _default:
        # 末级兜底命中但未勾 chat/all → 明确拒绝（A6），留痕不静默
        log.warning(f"_resolve_chat_runtime_id: backend '{backend}' resolved to engine(s) without "
                    f"chat capability (by_name={_by_name}, default={_default}), "
                    f"chat fallback rejected (Issue #3396)")
    return 0


def _chat_config_engine_uid_rt(runtime_id: int) -> str:
    """Issue #868 (T10) 双写：runtime_id → engine_uid（任意拓扑具名查询；行不存在/未迁移返回 ''）。"""
    if not runtime_id:
        return ""
    try:
        row = _im.read_one_any("agent_runtime_engine_uid_by_id", (int(runtime_id),))
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def _write_chat_config_row(user_id: int, runtime_id: int = 0, skill: str = "", skill_id: int = 0):
    # 写前收敛重复 + upsert 在 owner 域同一事务内（见 runtime public.upsert_chat_config_row）：
    # 同 user 的 repo_key='' 行只保留 'sess-cfg-{user_id}'（账号迁移残留行一并清理）。
    # Issue #868 (T10)：engine_uid 双写（runtime_id 的 uid 镜像，任意拓扑具名查询）。
    _im.upsert_chat_config_row(user_id, runtime_id=runtime_id, skill=skill, skill_id=skill_id,
                               engine_uid=_chat_config_engine_uid_rt(runtime_id))


def set_chat_config(user_id: int, **kwargs):
    """写入用户全局 Chat 配置（合并进 chat_agent_session，repo_key=''）。

    runtime_id 优先级：显式传入（前端带 id，方案 B）> 由 backend 名解析（方案 A）。
    """
    # A2 收尾：im_* 不再经 chat 配置落库（凭证只走 grape_t_channel_installation）
    allowed = {"backend", "runtime_id", "skill", "skill_id"}
    data = get_chat_config(user_id)
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            data[k] = v
    explicit_rid = kwargs.get("runtime_id")
    if explicit_rid:
        try:
            runtime_id = int(explicit_rid)
        except (TypeError, ValueError):
            runtime_id = 0
    else:
        runtime_id = _resolve_chat_runtime_id(data.get("backend") or "")
    # skill name → skill_id（若未显式传 skill_id）
    if data.get("skill") and not data.get("skill_id"):
        data["skill_id"] = _tokens.resolve_skill_id(data["skill"], user_id=user_id)
    _write_chat_config_row(user_id, runtime_id=runtime_id, skill=data["skill"],
                           skill_id=data.get("skill_id", 0))
