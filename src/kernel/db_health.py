"""SQLite WAL/SHM 失效检测与自动重连（Issue #3588）。

背景（overlayfs 容器）：copy-up 以「删除旧文件 + 新建同名文件」替换
``sessions.db-wal`` / ``sessions.db-shm``；进程内长连接仍持有指向**已删除
inode** 的 fd → 任何读写报 ``disk I/O error``，接口持续 500，且旧连接永不
自愈（事故报告：fd 5/6 指向 ``sessions.db-wal (deleted)``）。新开短连接可
恢复（bulk_read「偶发 200」即此），长连接必须重建。

本模块提供：

- :func:`is_stale_error`：异常驱动失效判据（disk I/O error / malformed /
  unable to open database file / file is not a database）。
- :func:`record_wal_files` / :func:`wal_files_changed`：轻量周期校验——
  连接建立时记录 ``${db}-wal`` / ``${db}-shm`` 的 (存在性, inode)，任一已
  记录文件消失或 inode 变化即判定失效（fd 已指向已删除 inode）。
- :class:`SelfHealingSQLiteDB`：连接代理。`execute/cursor/commit/rollback`
  前过周期校验（TTL 节流），捕获 stale 异常后关旧连接、经 factory 重建
  （connect + PRAGMA 重放 + 上层包装）、同语句重试一次。因调用方均为
  ``from session_manager import db`` 的**对象引用**传递，代理一经建立即
  全局生效，无需改动任何调用点。
- :func:`db_health` / :func:`reset_health`：失效/恢复信号（db_status、
  wal_valid、reconnects），供 /healthz 与结构化日志使用。

不重复投递：outbox 行认领态（pending → sending）持久化在库中，重连后
状态仍在，不会重复投递；本模块不触碰该语义。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

from src.kernel.config import log

# 异常驱动判据：sqlite 消息小写子串（sqlite3 错误消息跨版本稳定）。
# 「closed database」：close() 后旧连接的 next 访问异常——代理自愈语义下同样
# 走重建路径（docstring「下次访问自动重建」）。
_STALE_MARKERS = (
    "disk i/o error",
    "database disk image is malformed",
    "unable to open database file",
    "file is not a database",
    "cannot operate on a closed database",
)


def _stale_guarded(run: Callable[[], Any], rebuild: Callable[[BaseException], Any]) -> Any:
    """共享 stale-guard 样板：常规执行；命中失效判据 → 走 rebuild 路径。

    execute/executemany/commit/rollback 等全部连接接口经此收敛，消除逐处
    「try → is_stale_error → _rebuild_and_retry」的六处重复。
    """
    try:
        result = run()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        if not is_stale_error(e):
            raise
        return rebuild(e)
    return result

# 轻量周期校验默认节流：两次 os.stat 之间至少间隔（秒）；可按实例覆盖（测试用 0）
DEFAULT_CHECK_TTL = 5.0


def is_stale_error(exc: BaseException) -> bool:
    """异常是否为 SQLite 文件/存储层失效（overlayfs WAL/SHM 被替换的典型症状）。"""
    if not isinstance(exc, (sqlite3.OperationalError, sqlite3.DatabaseError)):
        return False
    msg = str(exc).lower()
    return any(m in msg for m in _STALE_MARKERS)


def is_sqlite_conn(conn: Any) -> bool:
    """SQLite 连接判别（含 SelfHealingSQLiteDB 代理）。

    迁移脚本等按 ``isinstance(conn, sqlite3.Connection)`` 分叉双方言；自愈代理
    接入后 engine_wiring._connect 对 SQLite 返回代理对象，老判据会把 SQLite
    误判成 MySQL 走 information_schema → no such table。统一经此判别。
    """
    return isinstance(conn, (sqlite3.Connection, SelfHealingSQLiteDB))


# 可安全重放的读语句前缀：SELECT / PRAGMA / EXPLAIN。WITH 可能导出写
# （WITH ... INSERT），保守归入写；空/无法判别同样归入写（宁可抛错不重放）。
_READ_SQL_PREFIXES = ("SELECT", "PRAGMA", "EXPLAIN")


def is_read_sql(sql: Any) -> bool:
    """语句是否为可安全失效重放的**读**语句（首个关键字判断，前导注释剥离）。

    写语句（INSERT/UPDATE/DELETE/REPLACE/CREATE/WITH…）失效重放可能双写
    （WAL 已落盘、fsync 阶段报错的情形）；executemany 部分行成功后重放
    范围不可界定；executescript 有隐式 COMMIT 前缀语义——一律不重放。
    """
    try:
        text = str(sql).strip()
    except Exception:
        return False
    while True:
        if text.startswith("/*"):
            end = text.find("*/")
            if end < 0:
                return False
            text = text[end + 2:].lstrip()
        elif text.startswith("--") or text.startswith("#"):
            end = text.find("\n")
            if end < 0:
                return False
            text = text[end + 1:].lstrip()
        else:
            break
    first = text.split(None, 1)[0].upper() if text else ""
    return first in _READ_SQL_PREFIXES


def record_wal_files(db_path: str) -> dict:
    """记录 WAL/SHM 文件快照：{路径: (存在, inode)}。

    连接建立时调用——此时连接刚打开 WAL，快照即该连接实际持有的文件身份。
    """
    snap = {}
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        try:
            st = os.stat(p)
            snap[p] = (True, st.st_ino)
        except OSError:
            # 未知态：连接建立时文件尚不存在（全新库）。WAL/SHM 会在首次写入时
            # 创建——「从未出现」不等于「失效」，快照仍记录，但标记为未知，
            # 由 wal_files_changed 跳过（见下）；首次成功写入后会刷新快照。
            snap[p] = (False, 0)
    return snap


def _db_label(db_path: str) -> str:
    """指标 label：只保留文件名，避免把部署主目录路径写进 Prometheus label。"""
    try:
        return os.path.basename((db_path or "").rstrip("/")) or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _absorb_wal_snapshot(inst) -> None:
    """写语句成功后把当前 WAL/SHM 身份吸收为基线（Issue #3588 审查 F3①）。

    只在快照仍处未知态（连接建立时文件不存在）时执行一次——覆盖「全新库
    首写创建 WAL/SHM」的正常生命周期，使后续 inode 漂移检测有正确基线。
    已有基线的连接不受影响（漂移仍按失效判定）。
    """
    snap = inst.__dict__["_wal_snap"]
    if snap and not any(existed for existed, _ in snap.values()):
        fresh = record_wal_files(inst.__dict__["_db_path"])
        if any(existed for existed, _ in fresh.values()):
            object.__setattr__(inst, "_wal_snap", fresh)


def wal_files_changed(db_path: str, recorded: dict) -> bool:
    """磁盘上的 WAL/SHM 是否已偏离连接建立时的快照（= 连接持有已删除 inode）。

    判定只对「快照时已存在」的文件生效：全新库首写时 WAL/SHM 才被创建，
    快照里 (False, 0) 是未知态而非失效（Issue #3588 审查 F3——否则空库建表
    即误报 wal_valid=False）。快照在每次重建时刷新，重建后新文件身份成为基线。
    """
    if not recorded:
        return False
    for p, (existed, ino) in recorded.items():
        try:
            st = os.stat(p)
        except OSError:
            if existed:
                return True  # 已记录文件被删除（copy-up 删旧建新的删除半边）
            continue        # 快照未知且仍不存在：维持未知态
        if not existed:
            # 快照为未知态（连接建立时文件尚不存在）。首写创建 WAL/SHM 属正常
            # 生命周期（不计失效）；真正的失效必须由「已存在→消失/替换」刻画。
            # 检查侧跳过，吸收由写语句成功后的快照刷新完成（见 _absorb_wal_snapshot）。
            continue
        if st.st_ino != ino:
            return True  # inode 被替换
    return False


class _StaleAwareCursor:
    """游标包装：execute 遇 stale 异常时触发连接重建并重放语句。

    重建后本包装切换到新连接上的新真实游标，fetch 委托最新游标——调用方
    持有的包装对象语义连续（`c.execute(...); c.fetchall()` 全程可用）。
    """

    def __init__(self, owner: "SelfHealingSQLiteDB", cur):
        self._owner = owner
        self._cur = cur

    def _swap(self, run: Callable[[], Any], replay: Callable[[Any], Any],
              sql: Any) -> None:
        """执行 run；stale 时经 owner 重建，读语句在新连接上重放（replay）。

        写语句不重放（防双写/范围不可界定）：重建后重抛原 stale 异常，交上层
        重试；事务中即使读语句也被 protect_txn 兜底为重抛（防掩盖丢写）。
        """
        def _rebuild(e: BaseException):
            return self._owner._rebuild_and_retry(
                replay, retry=is_read_sql(sql), original=e)

        self._cur = _stale_guarded(run, _rebuild)

    def execute(self, sql: str, params=()):
        self._swap(
            lambda: self._owner._inner().execute(sql, params),
            lambda conn: conn.execute(sql, params), sql)
        return self

    def executemany(self, sql: str, seq_of_params):
        self._swap(
            lambda: self._owner._inner().executemany(sql, seq_of_params),
            lambda conn: conn.executemany(sql, seq_of_params), sql)
        return self

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_cur"], name)


class SelfHealingSQLiteDB:
    """SQLite 长连接自愈代理：失效检测 → 关旧连接 → factory 重建 → 语句重放。

    :param factory: 无参函数，返回一条**完整初始化**的新连接（connect +
        PRAGMA 重放 + 上层包装如 SchemaSQLiteDB）。失效重建即再次调用它。
    :param db_path: 库文件路径（用于 WAL/SHM inode 快照周期校验）。
    :param check_ttl: 周期校验节流间隔（秒），测试可传 0 强制每次校验。

    未记录到 WAL/SHM 快照（如内存库）或周期校验未到 TTL 时不做 stat，
    失效仍由异常驱动兜底。重建持模块级锁串行化，防并发风暴。
    """

    def __init__(self, factory: Callable[[], Any], db_path: str = "",
                 check_ttl: float = DEFAULT_CHECK_TTL):
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_db_path", db_path)
        object.__setattr__(self, "_check_ttl", float(check_ttl))
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_inner_conn", None)
        object.__setattr__(self, "_wal_snap", {})
        object.__setattr__(self, "_last_check", 0.0)
        object.__setattr__(self, "_stale_since", 0.0)
        object.__setattr__(self, "_reconnects", 0)
        object.__setattr__(self, "_last_status", "ok")
        self._rebuild(reason="init")

    # ------------------------------------------------------------ 状态/信号
    def health(self) -> dict:
        """失效/恢复信号：db_status(ok|stale|recovered)、wal_valid、reconnects。

        force 校验：健康检查是运维实时信号，stat 两条 WAL/SHM 足够轻
        （HTTP 层 /healthz 自带 3s 缓存，不会放大为热路径 stat）。
        """
        return {
            "db_status": self.__dict__["_last_status"],
            "wal_valid": not self._check_stale(force=True),
            "last_stale_at": self.__dict__["_stale_since"],
            "reconnects": self.__dict__["_reconnects"],
        }

    # ------------------------------------------------------------ 内部
    def _inner(self) -> Any:
        return self.__dict__["_inner_conn"]

    def _rebuild(self, reason: str) -> Any:
        """关旧连接 → factory 重建（PRAGMA 等初始化重放）→ 刷新 WAL 快照。"""
        with self.__dict__["_lock"]:
            old = self.__dict__["_inner_conn"]
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001  # best-effort：旧连接可能已坏
                    pass
            conn = self.__dict__["_factory"]()
            object.__setattr__(self, "_inner_conn", conn)
            object.__setattr__(self, "_wal_snap",
                               record_wal_files(self.__dict__["_db_path"]))
            object.__setattr__(self, "_last_check", time.time())
            # reconnects 只计真实重建（失效触发）；构造期首次建连（reason=init）
            # 不计——否则计数从 1 起算，运维会把健康实例当失效（审查 F3②）。
            if reason != "init":
                object.__setattr__(self, "_reconnects",
                                    self.__dict__["_reconnects"] + 1)
                log.info("db file invalid (reason=%s) -> connection rebuilt (reconnects=%d)",
                         reason, self.__dict__["_reconnects"])
                # A13：指标上报（未 install 时为 no-op；label 用文件名掩码路径）
                from src.kernel.org_metrics import observe_db_reconnect
                observe_db_reconnect(_db_label(self.__dict__["_db_path"]))
            return conn

    def _rebuild_and_retry(self, op: Callable[[Any], Any], *,
                           retry: bool = True,
                           original: Optional[BaseException] = None,
                           protect_txn: bool = True) -> Any:
        """失效异常路径：结构化日志 + 重建 +（默认）在新连接上重放同一操作（一次）。

        retry=False（commit 专用 / 写语句 / 事务中）：旧事务的未提交写入已随失效
        连接丢弃，在新连接上重放会**静默丢写并误报 recovered**——改为重建后重抛
        原异常，由上层重试整笔事务。

        protect_txn=True（默认）时，即使调用方请求重放，只要旧连接存在未提交
        事务（in_transaction），同样强制转为重抛——事务已随失效连接丢失，重放
        任何语句（含读）都会掩盖丢写；rollback 例外（丢弃本就是回滚语义）传
        protect_txn=False。
        """
        if retry and protect_txn and self._in_transaction():
            retry = False
            if original is None:
                original = sqlite3.OperationalError(
                    "stale connection detected with open transaction; "
                    "transaction lost after reconnect")
        log.warning("event=db_stale_detected reason=wal_shm_replaced_or_missing db=%s",
                    self.__dict__["_db_path"])
        with self.__dict__["_lock"]:
            object.__setattr__(self, "_stale_since", time.time())
            object.__setattr__(self, "_last_status", "stale")
            self._rebuild(reason="stale_error")
        if not retry:
            log.warning("event=db_txn_lost_after_reconnect db=%s（事务随失效连接丢弃，请重试整笔）",
                        self.__dict__["_db_path"])
            raise original if original is not None else sqlite3.OperationalError(
                "stale connection; transaction lost after reconnect")
        try:
            result = op(self.__dict__["_inner_conn"])
        except Exception as e:
            if is_stale_error(e):
                # 一次重建后仍失效：磁盘上库本体不可用，留给上层重试
                log.warning("event=db_reconnect_failed db=%s err=%s",
                            self.__dict__["_db_path"], e)
            raise
        object.__setattr__(self, "_last_status", "recovered")
        log.info("event=db_reconnect_ok db=%s", self.__dict__["_db_path"])
        from src.kernel.org_metrics import set_db_wal_valid
        set_db_wal_valid(_db_label(self.__dict__["_db_path"]), True)
        return result

    def _in_transaction(self) -> bool:
        """旧连接是否存在未提交事务（连接坏到读不出属性时按无事务处理）。"""
        try:
            return bool(self._inner().in_transaction)
        except Exception:
            return False

    def _guarded(self, run: Callable[[], Any], replay: Callable[[Any], Any],
                 *, check: bool = True, retry: bool = True,
                 protect_txn: bool = True) -> Any:
        """连接接口共享 stale-guard：预检（可选）→ 常规执行 → 失效则重建重放。

        check=False（commit/rollback）：事务进行中做预检重建等于静默丢弃未提交
        写入；retry=False（commit / 写语句）：重建后重抛，见
        ``_rebuild_and_retry``；protect_txn=False（rollback）：丢弃即回滚语义，
        不做事务保护强制重抛。
        """

        def _rebuild(e: BaseException):
            return self._rebuild_and_retry(replay, retry=retry, original=e,
                                           protect_txn=protect_txn)

        def _run_checked():
            if check:
                self._maybe_check_and_rebuild()
            return run()

        return _stale_guarded(_run_checked, _rebuild)

    def _check_stale(self, force: bool = False) -> bool:
        """轻量周期校验（TTL 节流）：WAL/SHM 偏离连接建立快照 → 已失效。

        force=True 跳过节流（health() 实时信号用，HTTP 层已有缓存）。
        """
        now = time.time()
        if not force and now - self.__dict__["_last_check"] < self.__dict__["_check_ttl"]:
            return False
        object.__setattr__(self, "_last_check", now)
        changed = wal_files_changed(self.__dict__["_db_path"],
                                    self.__dict__["_wal_snap"])
        if changed:
            if self.__dict__["_last_status"] != "stale":
                object.__setattr__(self, "_stale_since", now)
                object.__setattr__(self, "_last_status", "stale")
                log.warning("event=db_stale_detected reason=wal_inode_mismatch db=%s",
                            self.__dict__["_db_path"])
            from src.kernel.org_metrics import set_db_wal_valid
            set_db_wal_valid(_db_label(self.__dict__["_db_path"]), False)
        else:
            if self.__dict__["_last_status"] == "stale":
                from src.kernel.org_metrics import set_db_wal_valid
                set_db_wal_valid(_db_label(self.__dict__["_db_path"]), True)
        return changed

    def _maybe_check_and_rebuild(self) -> None:
        """周期预检命中失效：未提交事务中**重建后抛错**（事务已随旧连接丢失，
        静默重建会让后续 commit 变成空提交、静默丢写），无事务则静默重建。
        """
        if self._check_stale():
            self._rebuild_and_retry(
                lambda conn: None,
                retry=not self._in_transaction(),
                original=sqlite3.OperationalError(
                    "stale connection detected with open transaction; "
                    "transaction lost after reconnect"))

    # ------------------------------------------------------------ 连接接口
    def execute(self, sql: str, params=()):
        # 读语句失效可在新连接上安全重放；写语句重放可能双写——重建后重抛
        #（交上层重试整笔）。事务中的读同样被 protect_txn 兜底为重抛。
        cur = self._guarded(
            lambda: self._inner().execute(sql, params),
            lambda conn: conn.execute(sql, params),
            retry=is_read_sql(sql))
        _absorb_wal_snapshot(self)  # 首写创建 WAL/SHM 后吸收基线（F3①，幂等）
        return _StaleAwareCursor(self, cur)

    def executemany(self, sql: str, seq_of_params):
        cur = self._guarded(
            lambda: self._inner().executemany(sql, seq_of_params),
            lambda conn: conn.executemany(sql, seq_of_params),
            retry=is_read_sql(sql))
        _absorb_wal_snapshot(self)
        return _StaleAwareCursor(self, cur)

    def executescript(self, sql: str):
        # executescript 有隐式 COMMIT 前缀语义，部分已提交后重放会重复执行——
        # 一律不重放。
        return self._guarded(
            lambda: self._inner().executescript(sql),
            lambda conn: conn.executescript(sql),
            retry=False)

    def cursor(self, *args: Any, **kwargs: Any) -> _StaleAwareCursor:
        self._maybe_check_and_rebuild()
        return _StaleAwareCursor(self, self._inner().cursor(*args, **kwargs))

    def commit(self):
        """提交事务。

        事务进行中不做周期预检（预检重建会静默丢弃未提交写入）；commit 命中
        stale 时**重建后重抛原异常**——旧事务写入已随失效连接丢弃，绝不静默
        丢写或误报 recovered，由上层重试整笔事务。
        """
        self._guarded(lambda: self._inner().commit(),
                      lambda conn: conn.commit(), check=False, retry=False)

    def rollback(self):
        """回滚事务。

        rollback 命中 stale 时重建即达到回滚语义（旧事务已随失效连接丢弃），
        新连接上的空 rollback 是安全的收尾；不做周期预检（同 commit）；事务
        保护豁免（protect_txn=False）——丢弃即回滚，不强制重抛。
        """
        self._guarded(lambda: self._inner().rollback(),
                      lambda conn: conn.rollback(), check=False,
                      protect_txn=False)

    def close(self):
        """关闭底层连接（下次访问自动重建——自愈语义，防止误关致死）。"""
        try:
            self._inner().close()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_inner_conn"], name)

    def __setattr__(self, name: str, value: Any):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self.__dict__["_inner_conn"], name, value)


# ---------------------------------------------------------------- 模块级信号

_HEALTH_REGISTRY: dict = {"instances": []}
_REGISTRY_LOCK = threading.Lock()


def register(instance: SelfHealingSQLiteDB) -> None:
    """登记自愈连接实例（db_health() 聚合它们的状态）。"""
    with _REGISTRY_LOCK:
        if instance not in _HEALTH_REGISTRY["instances"]:
            _HEALTH_REGISTRY["instances"].append(instance)


def db_health() -> dict:
    """聚合信号：任一实例失效 → db_status=stale / wal_valid=False。"""
    with _REGISTRY_LOCK:
        insts = list(_HEALTH_REGISTRY["instances"])
    if not insts:
        return {"db_status": "unknown", "wal_valid": True,
                "last_stale_at": 0.0, "reconnects": 0}
    status = "ok"
    wal_valid = True
    last_stale_at = 0.0
    reconnects = 0
    for inst in insts:
        h = inst.health()
        if h["db_status"] == "stale":
            status = "stale"
            wal_valid = False
        elif status == "ok" and h["db_status"] == "recovered":
            status = "recovered"
        if not h["wal_valid"]:
            wal_valid = False
        last_stale_at = max(last_stale_at, h.get("last_stale_at") or 0.0)
        reconnects += h.get("reconnects") or 0
    return {"db_status": status, "wal_valid": wal_valid,
            "last_stale_at": last_stale_at, "reconnects": reconnects}


def reset_health() -> None:
    """清空实例登记（测试隔离用）。"""
    with _REGISTRY_LOCK:
        _HEALTH_REGISTRY["instances"] = []


def wrap_with_health_tracking(factory: Callable[[], Any], db_path: str) -> SelfHealingSQLiteDB:
    """建自愈连接并登记到模块级信号（session_manager 主连接入口）。"""
    inst = SelfHealingSQLiteDB(factory, db_path)
    register(inst)
    return inst
