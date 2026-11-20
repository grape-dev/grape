"""占位账号「是否有业务数据」判定（Issue #3097 第 2 层）——三库四进程合规实现。

**它解决什么**：占位账号自动合并（`is_placeholder_account_mergeable`）要求「该账号
没有业务数据」。判定原本在 `grape_account_db.py` 里内联遍历 11 张表，且把「表查不到
（MySQL 1146 / 旧库未迁移）」当成「保守认为有数据 → return True」。三库拆分后
`agent_tasks` / `chat_conversations` / `chat_agent_session` / `issue` 只在 runtime 库，
mgmt 连接查它们必然 1146 → 判定恒为「有数据」→ **占位账号自动合并对所有用户、所有
渠道永久失效**，冲突一律被推去走转移确认分支。

**域边界（三库四进程不变量 #1）**：

- 本域（mgmt）表：用**调用方游标**读（同域直连本域库，保住 db_lock 内的事务快照）。
- 外域（runtime）表：只走 runtime 域 public 的具名读原语
  `user_business_table_counts(user_id)`（SQL 与表名留在 runtime 域，A3 白名单登记；
  本域不拼外域表名，也不跨域直连）。

**定稿语义**（Issue #3054 QA 收紧）：

- 表**不存在**（MySQL 1146 / SQLite no such table）= **无数据**：三库拆分拓扑下这是
  正常态，判「有数据」会让占位账号合并永久失效；
- 表**读不成但确实存在**（连接断开、超时、权限、通道整体不可用）= **不可判定**，按
  **保守方向当「有数据」**（`indeterminate=True`）→ 不自动合并，改走转移确认。一次
  网络抖动不得把「有数据的占位账号」判成无数据并自动合并；
- 两种情况都记结构化 warn（`event=placeholder_business_data_table_unreadable`，带表名/
  归属域/错误码/处置）；
- 有数据 = 任一表 COUNT>0；判定结果带证据（命中表、未读成的表、是否不可判定）。

模块边界：值对象（表规格、判定结果、读失败）+ 服务类（`BusinessDataProbe`）。
`grape_account_db.py` 只保留一行委托。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Callable, ContextManager

log = logging.getLogger(__name__)

# 结构化告警事件名（日志检索/告警规则按此聚合）
WARN_EVENT = "placeholder_business_data_table_unreadable"


@dataclass(frozen=True)
class BusinessTableSpec:
    """一张「业务数据」表：归属域 + 归属列 + 额外过滤 + 计数 SQL（仅本域表用）。"""

    table: str
    domain: str
    user_column: str = "user_id"
    extra_where: str = ""

    @property
    def count_sql(self) -> str:
        where = f"{self.user_column}=?"
        if self.extra_where:
            where += f" AND {self.extra_where}"
        return f"SELECT COUNT(*) FROM {self.table} WHERE {where}"


@dataclass(frozen=True)
class TableReadFailure:
    """一张表没读成（缺表/无权限/连接异常）——按定稿语义视为无数据。"""

    table: str
    domain: str
    error: str
    error_code: str = ""
    missing_table: bool = False

    @property
    def action(self) -> str:
        # 缺表 = 拓扑正常态 → 无数据；读不成但存在 → 不可判定（保守当有数据）。
        return "missing-table->no-data" if self.missing_table else "unreadable->indeterminate"

    def as_warn_payload(self) -> dict:
        return {
            "event": WARN_EVENT,
            "table": self.table,
            "domain": self.domain,
            "error_code": self.error_code,
            "missing_table": self.missing_table,
            "action": self.action,
            "error": self.error[:300],
        }


@dataclass(frozen=True)
class BusinessDataVerdict:
    """判定结果（带证据）。`has_data=False` 是「可自动合并」的必要条件。"""

    has_data: bool
    hits: tuple[str, ...] = ()
    failures: tuple[TableReadFailure, ...] = ()
    # 有表存在但读不成 → 无数据不可证 → has_data 按保守方向置 True
    indeterminate: bool = False

    @property
    def checked_tables(self) -> int:
        return len(self.hits) + len(self.failures)

    @property
    def degraded(self) -> bool:
        """是否有表没读成（判定仍有效，但需要观测）。"""
        return bool(self.failures)

    @property
    def reason(self) -> str:
        """判定原因（日志/审计用）：has-data / no-data / indeterminate。"""
        if self.indeterminate:
            return "indeterminate"
        return "has-data" if self.has_data else "no-data"


# 本域（mgmt）表清单（owner = mgmt，见 configs/table_ownership.json）。
# user_repos 只查托管仓库（is_admin=1）：普通缓存可重建，不算业务数据。
MGMT_TABLES: tuple[BusinessTableSpec, ...] = (
    BusinessTableSpec("user_repos", "mgmt", extra_where="is_admin=1"),
    BusinessTableSpec("user_feedback", "mgmt"),
    BusinessTableSpec("user_credentials", "mgmt"),
    BusinessTableSpec("api_tokens", "mgmt"),
    BusinessTableSpec("session_api_tokens", "mgmt"),
    BusinessTableSpec("scene_likes", "mgmt"),
    BusinessTableSpec("skill_likes", "mgmt"),
)

# 外域（runtime）表的表名只出现在 runtime 域（public 具名原语内），本域仅持归属域标记。
RUNTIME_DOMAIN = "runtime"


def legacy_table_tuples() -> list[tuple[str, str, str]]:
    """兼容旧结构 `(table, user_id_column, where_clause)` —— **仅本域（mgmt）表**。

    Issue #3097：runtime 归属表的转移改走契约 `RuntimeService.transfer_user_business_data`，
    本函数不再把外域表名交回 mgmt（表名不出域）。
    """
    return [(s.table, s.user_column, s.extra_where) for s in MGMT_TABLES]


def classify_read_error(table: str, domain: str, exc: BaseException) -> TableReadFailure:
    """把异常归一成「读失败」值对象（识别缺表：MySQL 1146 / SQLite no such table）。"""
    code = ""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        code = str(args[0])
    text = f"{exc}"
    lowered = text.lower()
    missing = (
        code == "1146"
        or "1146" in text
        or "doesn't exist" in lowered
        or "no such table" in lowered
        or "unknown table" in lowered
    )
    return TableReadFailure(table=table, domain=domain, error=text,
                            error_code=code, missing_table=missing)


class BusinessDataProbe:
    """判定某账号是否有业务数据（值对象进、值对象出；两条读通道均可注入以便测试）。

    :param cursor: 调用方游标（本域 mgmt 表直连，默认 None = 走本域读通道）。
    :param cursor_factory: 无调用方游标时的本域读通道（默认 `bulk_read.reader("")`）。
    :param runtime_counts: 跨域读通道（默认 runtime public `user_business_table_counts`）。
    :param warn_sink: 结构化告警出口（默认写 logger；测试可注入收集器）。
    """

    def __init__(self, cursor=None,
                 cursor_factory: Callable[[], ContextManager] | None = None,
                 runtime_counts: Callable[[object], dict] | None = None,
                 warn_sink: Callable[[dict], None] | None = None):
        self._cursor = cursor
        self._cursor_factory = cursor_factory
        self._counts_provider = runtime_counts
        self._warn_sink = warn_sink

    # ── 读通道 ────────────────────────────────────────────────────────────
    def _local_cursor(self):
        if self._cursor is not None:
            return _shared(self._cursor)
        if self._cursor_factory is not None:
            return self._cursor_factory()
        from src.kernel.bulk_read import reader
        return reader()

    def _runtime_counts(self, user_id) -> dict:
        if self._counts_provider is not None:
            return self._counts_provider(user_id)
        # 跨域访问一律走契约（RuntimeService Local/Remote provider 由 GSB 按方法路由，
        # Issue #2479）：本域不 import 外域 public、不拼外域表名、不跨域直连。
        from src.kernel.provider import get_runtime_service
        return get_runtime_service().user_business_table_counts(user_id)

    def _warn(self, failure: TableReadFailure) -> None:
        payload = failure.as_warn_payload()
        try:
            if self._warn_sink is not None:
                self._warn_sink(payload)
            else:
                log.warning(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        except Exception as exc:  # noqa: BLE001  # 告警通道故障不得影响判定
            log.warning(f"{WARN_EVENT}: warn sink failed: {exc} (payload={payload})")

    # ── 判定 ──────────────────────────────────────────────────────────────
    def _count_local(self, spec: BusinessTableSpec, user_id) -> int:
        with self._local_cursor() as c:
            row = c.execute(spec.count_sql, (user_id,)).fetchone()
        return int(row[0] or 0) if row else 0

    def probe(self, user_id) -> BusinessDataVerdict:
        """先本域后外域逐表计数；第一张有数据的表即可定论（其余不再读）。"""
        hits: list[str] = []
        failures: list[TableReadFailure] = []
        for spec in MGMT_TABLES:
            try:
                if self._count_local(spec, user_id) > 0:
                    hits.append(spec.table)
                    return BusinessDataVerdict(has_data=True, hits=tuple(hits),
                                               failures=tuple(failures))
            except Exception as exc:  # noqa: BLE001  # 缺表=无数据；存在但读不成=不可判定
                failure = classify_read_error(spec.table, spec.domain, exc)
                failures.append(failure)
                self._warn(failure)
                if not failure.missing_table:
                    return BusinessDataVerdict(has_data=True, hits=tuple(hits),
                                               failures=tuple(failures), indeterminate=True)

        try:
            counts = self._runtime_counts(user_id) or {}
        except Exception as exc:  # noqa: BLE001  # 通道整体不可用 = 不可判定（保守当有数据）
            self._warn(classify_read_error(RUNTIME_DOMAIN, RUNTIME_DOMAIN, exc))
            return BusinessDataVerdict(has_data=True, hits=tuple(hits),
                                       failures=tuple(failures), indeterminate=True)

        for table, info in counts.items():
            count = info.get("count")
            if count is None:
                failure = TableReadFailure(
                    table=table, domain=RUNTIME_DOMAIN,
                    error=str(info.get("error") or ""),
                    error_code=str(info.get("code") or ""),
                    missing_table=bool(info.get("missing_table")),
                )
                failures.append(failure)
                self._warn(failure)
                if not failure.missing_table:
                    return BusinessDataVerdict(has_data=True, hits=tuple(hits),
                                               failures=tuple(failures), indeterminate=True)
                continue
            if int(count) > 0:
                hits.append(table)
                return BusinessDataVerdict(has_data=True, hits=tuple(hits),
                                           failures=tuple(failures))
        return BusinessDataVerdict(has_data=False, hits=tuple(hits), failures=tuple(failures))

    def has_business_data(self, user_id) -> bool:
        """True = 有数据**或不可判定**（两者都不允许自动合并）。"""
        return self.probe(user_id).has_data


@contextmanager
def _shared(cursor):
    """把调用方游标包成上下文（不关闭调用方游标）。"""
    yield cursor