"""外部身份解绑：单语句条件 UPDATE 的语句构造与执行（Issue #3054 P1/P3 整合）。

## 架构归属（三库四进程，`docs/weekly-11/table-owner.tsv`）

`external_identities` 与 `grape_accounts` 都归 **mgmt** 库 / grape_server(mgmt 路由)
进程。本模块只发 mgmt 域内 SQL：解绑的目标行与「至少保留一种登录方式」判据都在同库，
不需要跨域读（目标域 public）也不需要 GSB `/internal/*`，也不在调用方直写外域表。

## 为什么把判据放进同一条 UPDATE

P2 的实现是「读-判-写」：两次 SELECT + 单行 UPDATE。无密码账号并发解绑 gitcode /
github 时两端都能通过「还剩一种登录方式」的判定 → 账号锁死（status=4 死锁同族）。
现在判据与写入在同一条语句里求值，由数据库语句原子性裁决：并发下至少一端
`rowcount=0`（拒绝）。

## 对象模型

    UnbindTarget          目标行只读快照（墓碑/占位值取数来源，不参与判定）
    KeepOnePredicate      「至少保留一种登录方式」判据（密码 OR 除本行外仍有活跃绑定）
    AtomicUnbindStatement 语句基线（抽象基类，`build()` → (sql, params)）
      ├─ SqliteUnbindStatement
      └─ MySqlUnbindStatement   1093 规避：COUNT 套一层派生表
    IdentityUnbindGateway 只依赖 (db, lock) 的执行器：load_target / execute

`IdentityLifecycleService.unbind` 只做编排（取快照 → 选方言语句 → 执行 → 记日志），
不持有 SQL 文本；方言差异只落在 statement 子类，新增后端 = 新增一个子类。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

#: 目标行状态（与 grape_account_db.ACTIVE_STATUS / EXPIRED_STATUS 同值，
#: 不在本模块 import 以免与其形成循环依赖）
ACTIVE_STATUS = 1
EXPIRED_STATUS = 4
UNBOUND_STATUS = 0
LIVE_STATUSES: Tuple[int, ...] = (ACTIVE_STATUS, EXPIRED_STATUS)


@dataclass(frozen=True)
class UnbindTarget:
    """解绑目标行的只读快照。

    只用于取写入值（墓碑 JSON、占位 external_id）——**不**用于判定能否解绑；
    判定在 UPDATE 的 WHERE 里（并发安全）。
    """

    identity_id: str
    status: int
    external_id: str
    metadata: str

    @property
    def is_expired(self) -> bool:
        return int(self.status or 0) == EXPIRED_STATUS


class KeepOnePredicate:
    """「至少保留一种登录方式」判据（同一条 UPDATE 内求值）。

    - 账号 `password_hash` 非空 → 密码仍可登录；
    - 或该用户除本行外仍有 status 1/4 的绑定 → 第三方仍可登录。
    """

    def __init__(self, grape_user_id: str):
        self._user_id = str(grape_user_id)

    def sql_and_params(self) -> Tuple[str, List[Any]]:
        sql = (
            "(EXISTS(SELECT 1 FROM grape_accounts WHERE id=?"
            " AND password_hash IS NOT NULL AND password_hash<>'')"
            " OR " + self._remaining_bindings_sql() + " > 1)"
        )
        return sql, [self._user_id, self._user_id]

    def _remaining_bindings_sql(self) -> str:
        """剩余绑定计数子查询（默认方言）。MySQL 见子类覆盖。"""
        return (
            "(SELECT COUNT(*) FROM external_identities"
            " WHERE grape_user_id=? AND status IN (1,4))"
        )


class MySqlKeepOnePredicate(KeepOnePredicate):
    """MySQL 1093（UPDATE 目标表出现在子查询里）规避：COUNT 套一层派生表。"""

    def _remaining_bindings_sql(self) -> str:
        return (
            "(SELECT COUNT(*) FROM (SELECT id FROM external_identities"
            " WHERE grape_user_id=? AND status IN (1,4)) _remain)"
        )


class AtomicUnbindStatement(abc.ABC):
    """解绑语句基线：一条带判据的 UPDATE。

    子类只负责方言差异；SET / WHERE 的语义在基类固定：

    * 只命中 `status=1 OR status=4`（P2：过期态与活跃态同等可解绑）；
    * status=4 的行写入墓碑 metadata + 行级占位 external_id（释放
      `UNIQUE(provider, external_id)`），status=1 的行保持历史口径（保留 external_id）；
    * `require_keep_one` 时追加 KeepOnePredicate（原子裁决）。
    """

    def __init__(self, target: UnbindTarget, grape_user_id: str, *,
                 now: str, keep_one: bool = True,
                 new_external_id: str = "", new_metadata: str = ""):
        self.target = target
        self._user_id = str(grape_user_id)
        self._now = now
        self._keep_one = bool(keep_one)
        self._new_external_id = new_external_id or target.external_id
        self._new_metadata = new_metadata or target.metadata or "{}"

    # ── 方言钩子 ───────────────────────────────────────────────────────────
    @abc.abstractmethod
    def keep_one_predicate(self) -> KeepOnePredicate:
        """返回本方言的判据对象。"""

    def release_key_clause(self) -> str:
        """status=4 行写占位 external_id 的 CASE 子句（各方言一致）。"""
        return "external_id=CASE WHEN status=4 THEN ? ELSE external_id END"

    # ── 语句组装 ───────────────────────────────────────────────────────────
    def build(self) -> Tuple[str, List[Any]]:
        # MySQL 单表 UPDATE 的 SET 从左到右求值：status=0 必须排在两个 CASE **之后**，
        # 否则 CASE 读到的 status 已是 0，release_key / 墓碑两处都走 ELSE（Issue #3054 QA）。
        sql = (
            "UPDATE external_identities SET access_token='', "
            "refresh_token='', token_expires_at='', updated_at=?, "
            + self.release_key_clause() + ", "
            "metadata=CASE WHEN status=4 THEN ? ELSE metadata END, "
            "status=0 "
            "WHERE id=? AND (status=1 OR status=4)"
        )
        params: List[Any] = [self._now, self._new_external_id,
                             self._new_metadata, str(self.target.identity_id)]
        if self._keep_one:
            clause, extra = self.keep_one_predicate().sql_and_params()
            sql += " AND " + clause
            params += extra
        return sql, params


class SqliteUnbindStatement(AtomicUnbindStatement):
    """SQLite：单库单文件，子查询可直接引用目标表。"""

    def keep_one_predicate(self) -> KeepOnePredicate:
        return KeepOnePredicate(self._user_id)


class MySqlUnbindStatement(AtomicUnbindStatement):
    """MySQL：判据里的计数子查询必须套派生表（错误 1093）。"""

    def keep_one_predicate(self) -> KeepOnePredicate:
        return MySqlKeepOnePredicate(self._user_id)


def statement_for_backend(target: UnbindTarget, grape_user_id: str, **kwargs) -> AtomicUnbindStatement:
    """按当前后端挑语句实现（未知后端按 SQLite 口径，单库部署不涉及 1093）。"""
    backend = ""
    try:
        from src.kernel.mysql_compat import get_db_backend

        backend = str(get_db_backend() or "")
    except Exception:            # 后端判定失败不阻断解绑：按 SQLite 口径（无 1093 风险）
        backend = ""
    cls = MySqlUnbindStatement if backend.lower() == "mysql" else SqliteUnbindStatement
    return cls(target, grape_user_id, **kwargs)


class IdentityUnbindGateway:
    """解绑执行器：只依赖 (db, lock)，与仓储实现无关。

    依赖注入：service 传入自己的 `_db_access()` 结果即可；测试可传替身连接。
    """

    def __init__(self, db: Any, lock: Any):
        self._db = db
        self._lock = lock

    def load_target(self, grape_user_id: str, provider: str) -> Optional[UnbindTarget]:
        """读目标行快照（活跃优先）。判定不在这里，只取写入值。"""
        with self._lock:
            cursor = self._db.cursor()
            cursor.execute(
                "SELECT id, status, external_id, metadata FROM external_identities "
                "WHERE grape_user_id=? AND provider=? AND status IN (1,4) "
                "ORDER BY CASE status WHEN 1 THEN 0 ELSE 1 END LIMIT 1",
                (str(grape_user_id), provider))
            row = cursor.fetchone()
        if not row:
            return None
        return UnbindTarget(identity_id=str(row[0]), status=int(row[1] or 0),
                            external_id=str(row[2] or ""), metadata=row[3] or "{}")

    def execute(self, statement: AtomicUnbindStatement) -> int:
        """执行语句并返回受影响行数（0 = 被判据拒绝或目标已变）。"""
        sql, params = statement.build()
        with self._lock:
            cursor = self._db.cursor()
            cursor.execute(sql, tuple(params))
            affected = int(cursor.rowcount or 0)
            self._db.commit()
        return affected