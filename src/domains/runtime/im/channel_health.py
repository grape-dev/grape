"""IM 渠道健康状态机 + 风控暂停告警 + 失败日志小时聚合（Issue #3799）。

背景（Issue #3799）：微信 ilink 渠道被 `ret=-14`（session timeout，账号级风控）暂停时，
原实现只打一条 WARNING 再 `sleep(3600)`：不进管理台、不告警、上层无「渠道不可用」感知，
11 个实例 24h 309 条静默暂停；`notifystart/notifystop` 失败又逐条 WARNING 刷屏。

本模块提供三件东西：

1. **状态机 + 持久化**（`ChannelHealthTracker` + `ChannelHealthStore`）：
   `active/paused` 两态，逐安装记录 `last_pause_at` / `next_retry_at` / 连续暂停次数 /
   告警态，落 `grape_t_channel_health`（owner=runtime）。进程重启/多主控共享同一行，
   管理台与告警读同一真源。**不复用 installation.config**：config 参与 Supervisor 指纹
   （任一非 volatile 键变化即热重建连接），写健康态会自我触发重建。

2. **告警判定**（可配置常量）：
   - 单实例「连续 ≥2 次」= 距上次暂停 ≤ `PAUSE_REPEAT_WINDOW_SECONDS` 内再次暂停
     （覆盖「暂停 1h → 恢复 → 再暂停」的小时级循环，这类循环里 consecutive 永不累积）；
   - 或全实例 1h 内全部暂停（≥2 个同渠道安装，覆盖本次事故形态）→ 立即告警；
   - 首次单实例暂停只记事件（WARNING + 状态行 + `alert_state=pending`），抑制单次抖动。
   告警可检索：ERROR 行带 `alert=<rule>`，状态行 `alert_state/alerted_at/alert_reason`
   由管理台 API 直接返回。

3. **日志聚合**（`HourlyLogAggregator`）：`(instance, reason)` 键的小时窗口，
   窗口内只累加，**窗口滚动时**输出上窗口 1 条汇总（每实例每小时 ≤1 条）。

时间口径与 outbox/bot_lease 一致：库内存 UTC epoch，展示/API 输出
`YYYY-MM-DD HH:MM:SS`（UTC），不经 SQL `datetime('now')`。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

TABLE = "grape_t_channel_health"
ALERT_RULE = "wechat_channel_unavailable"

# 单实例「连续 ≥2 次」的判定窗口：小时级风控循环（暂停 1h → 恢复 → 再暂停）落在窗口内。
PAUSE_ALERT_CONSECUTIVE = 2
PAUSE_REPEAT_WINDOW_SECONDS = 7200.0
# 全实例判定窗口（Spec：全实例 1h 内均暂停 → 立即告警）。
PAUSE_ALERT_WINDOW_SECONDS = 3600.0
# 暂停态有效期：超过该时长没有任何恢复记录（进程崩溃遗留）不再当作暂停，
# 否则通知会被永久降级。
PAUSE_STATE_TTL_SECONDS = 7200.0
# 失败日志聚合窗口（Spec：每实例每小时 ≤1 条汇总）。
LOG_WINDOW_SECONDS = 3600.0
# 多主控下 peer 暂停态的刷新周期（PR #1983 审查 Required）：「全实例 1h 内均暂停」
# 判定按该 TTL 重读 store——长跑进程的内存快照只到启动那一刻，跨主控 peer 之后
# 写入的暂停行看不到会漏告警。单实例阈值仍走内存（本进程写侧，真源）。
PEER_STATE_REFRESH_SECONDS = 30.0

RECOVER_ACTIONS = ("relogin", "clear_pause", "retry_now")

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _iso(ts: float) -> str:
    """epoch 秒 → UTC 'YYYY-MM-DD HH:MM:SS'（0/None → 空串，管理台按空串显示「-」）。"""
    try:
        value = float(ts or 0.0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime(_TIME_FMT, time.gmtime(value))


@dataclass
class ChannelHealthState:
    """单安装的健康状态（内存视图；落库口径见 `ChannelHealthStore`）。"""

    installation_id: str = ""
    channel_type: str = ""
    instance_id: str = ""
    paused: bool = False
    last_pause_at: float = 0.0
    next_retry_at: float = 0.0
    pause_count: int = 0
    consecutive_pauses: int = 0
    last_pause_reason: str = ""
    last_resume_at: float = 0.0
    alert_state: str = ""            # "" | pending | firing | resolved
    alerted_at: float = 0.0
    alert_reason: str = ""
    degraded_count: int = 0
    last_degraded_at: float = 0.0
    last_degraded_to: str = ""
    last_degraded_ok: bool = False
    last_recover_action: str = ""
    last_recover_at: float = 0.0
    updated_at: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self, now: Optional[float] = None) -> dict:
        """对外快照：ISO 时间串 + `paused` + `stale`（暂停态超 TTL 视为遗留）。

        `paused` 与 `is_paused()` 同口径（PR #1983 R2 复审 Nit）：超 TTL 的遗留暂停
        行对外 `paused=False`，只保留 `stale=True` 标记——否则管理台 paused 计数与
        渠道卡片长期显示「暂停」，而通知侧早已放过（崩溃进程遗留行永不消散）。
        """
        now = float(now if now is not None else time.time())
        stale = bool(self.paused and self.last_pause_at
                     and now - self.last_pause_at > PAUSE_STATE_TTL_SECONDS)
        out = {
            "installation_id": self.installation_id,
            "channel_type": self.channel_type,
            "instance_id": self.instance_id,
            "paused": bool(self.paused and not stale),
            "stale": stale,
            "last_pause_at": _iso(self.last_pause_at),
            "last_pause_at_ts": float(self.last_pause_at or 0.0),
            "next_retry_at": _iso(self.next_retry_at),
            "next_retry_at_ts": float(self.next_retry_at or 0.0),
            "pause_count": int(self.pause_count or 0),
            "consecutive_pauses": int(self.consecutive_pauses or 0),
            "last_pause_reason": self.last_pause_reason,
            "last_resume_at": _iso(self.last_resume_at),
            "alert_state": self.alert_state,
            "alerted_at": _iso(self.alerted_at),
            "alert_reason": self.alert_reason,
            "degraded_count": int(self.degraded_count or 0),
            "last_degraded_at": _iso(self.last_degraded_at),
            "last_degraded_to": self.last_degraded_to,
            "last_degraded_ok": bool(self.last_degraded_ok),
            "last_recover_action": self.last_recover_action,
            "last_recover_at": _iso(self.last_recover_at),
            "updated_at": _iso(self.updated_at),
        }
        if self.extra:
            out["extra"] = dict(self.extra)
        return out


# ---------------------------------------------------------------- 仓储

_COLUMNS = (
    "installation_id", "channel_type", "instance_id", "paused",
    "last_pause_at", "next_retry_at", "pause_count", "consecutive_pauses",
    "last_pause_reason", "last_resume_at", "alert_state", "alerted_at",
    "alert_reason", "degraded_count", "last_degraded_at", "last_degraded_to",
    "last_degraded_ok", "last_recover_action", "last_recover_at", "updated_at",
)

_INSERT = (
    "INSERT OR REPLACE INTO " + TABLE + " (" + ", ".join(_COLUMNS) + ") VALUES ("
    + ", ".join(["?"] * len(_COLUMNS)) + ")"
)


def _default_conn_factory() -> Any:
    """IM 域库连接（跨进程共享的 runtime 库，与 outbox/租约同源）。"""
    from src.domains.runtime.im.engine_wiring import _connect, default_db_path
    return _connect(default_db_path())


class ChannelHealthStore:
    """`grape_t_channel_health` 的最小仓储（同步；调用方决定线程）。

    `ensure_ddl`：写侧（引擎里的渠道健康状态机）建表兜底——本表 owner=runtime，
    IM 库由 runtime 侧写入时幂等建表；读侧（管理台快照）默认不建表，缺表返回空，
    不因跨库 DDL 权限刷告警、也不影响其它页面。
    """

    def __init__(self, conn_factory: Optional[Callable[[], Any]] = None,
                 ensure_ddl: bool = True) -> None:
        self._conn_factory = conn_factory or _default_conn_factory
        self._ensure_ddl = bool(ensure_ddl)
        self._ensured = False

    def _conn(self):
        conn = self._conn_factory()
        if self._ensure_ddl and not self._ensured:
            try:
                from src.domains.runtime.schema_ddl import _CHANNEL_HEALTH_DDL
                conn.execute(_CHANNEL_HEALTH_DDL)
                conn.commit()
            except Exception as e:      # 老库/权限问题：读失败由调用方降级，不阻断主链路
                log.warning("[im-channel-health] ensure table skipped: %s", e)
            else:
                self._ensured = True
        return conn

    def load(self) -> dict[str, ChannelHealthState]:
        """全表状态（读失败 → 空 dict，健康检查按 fail-open 处理）。"""
        try:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT " + ", ".join(_COLUMNS) + " FROM " + TABLE).fetchall()
            finally:
                try:
                    conn.close()
                except Exception as e:   # 共享连接 close 为空操作；关闭失败不掩盖正常返回
                    log.debug("[im-channel-health] close skipped: %s", e)
        except Exception as e:
            log.warning("[im-channel-health] load failed: %s", e)
            return {}
        out: dict[str, ChannelHealthState] = {}
        for row in rows or []:
            try:
                state = _row_to_state(row)
            except Exception as e:
                log.warning("[im-channel-health] bad row skipped: %s", e)
                continue
            out[state.installation_id] = state
        return out

    def get(self, installation_id: str) -> Optional[ChannelHealthState]:
        return self.load().get(str(installation_id or ""))

    def upsert(self, state: ChannelHealthState) -> bool:
        try:
            conn = self._conn()
            try:
                conn.execute(_INSERT, tuple(_state_to_row(state)))
                conn.commit()
            finally:
                try:
                    conn.close()
                except Exception as e:
                    log.debug("[im-channel-health] close skipped: %s", e)
            return True
        except Exception as e:
            # 写失败不阻断渠道主链路（暂停/恢复语义在内存里已生效），但必须可见。
            log.warning("[im-channel-health] persist failed inst=%s: %s",
                        state.installation_id, e)
            return False


def _state_to_row(state: ChannelHealthState) -> list:
    return [
        state.installation_id, state.channel_type, state.instance_id,
        1 if state.paused else 0,
        float(state.last_pause_at or 0.0), float(state.next_retry_at or 0.0),
        int(state.pause_count or 0), int(state.consecutive_pauses or 0),
        state.last_pause_reason, float(state.last_resume_at or 0.0),
        state.alert_state, float(state.alerted_at or 0.0), state.alert_reason,
        int(state.degraded_count or 0), float(state.last_degraded_at or 0.0),
        state.last_degraded_to, 1 if state.last_degraded_ok else 0,
        state.last_recover_action, float(state.last_recover_at or 0.0),
        float(state.updated_at or 0.0),
    ]


def _row_to_state(row) -> ChannelHealthState:
    data = dict(zip(_COLUMNS, row))
    return ChannelHealthState(
        installation_id=str(data.get("installation_id") or ""),
        channel_type=str(data.get("channel_type") or ""),
        instance_id=str(data.get("instance_id") or ""),
        paused=bool(int(data.get("paused") or 0)),
        last_pause_at=float(data.get("last_pause_at") or 0.0),
        next_retry_at=float(data.get("next_retry_at") or 0.0),
        pause_count=int(data.get("pause_count") or 0),
        consecutive_pauses=int(data.get("consecutive_pauses") or 0),
        last_pause_reason=str(data.get("last_pause_reason") or ""),
        last_resume_at=float(data.get("last_resume_at") or 0.0),
        alert_state=str(data.get("alert_state") or ""),
        alerted_at=float(data.get("alerted_at") or 0.0),
        alert_reason=str(data.get("alert_reason") or ""),
        degraded_count=int(data.get("degraded_count") or 0),
        last_degraded_at=float(data.get("last_degraded_at") or 0.0),
        last_degraded_to=str(data.get("last_degraded_to") or ""),
        last_degraded_ok=bool(int(data.get("last_degraded_ok") or 0)),
        last_recover_action=str(data.get("last_recover_action") or ""),
        last_recover_at=float(data.get("last_recover_at") or 0.0),
        updated_at=float(data.get("updated_at") or 0.0),
    )


# ---------------------------------------------------------------- 日志聚合

class HourlyLogAggregator:
    """按 `(instance, reason)` 的小时窗口聚合失败日志。

    契约：同一窗口内 `note()` 只计数、不输出；窗口滚动时返回**上窗口**的 1 条汇总
    （count / 首末时间 / 首条样本）。`flush()` 在渠道断开/停机时补输出当前窗口
    （count>=1：单次失败也立即落日志——PR #1983 审查 Required，原先 count>1
    才输出会静默丢单次失败）。
    """

    def __init__(self, window_seconds: float = LOG_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        self._window = float(window_seconds or LOG_WINDOW_SECONDS)
        self._clock = clock
        self._buckets: dict[tuple[str, str], dict] = {}

    def note(self, instance: str, reason: str, detail: str = "") -> Optional[dict]:
        now = self._clock()
        key = (str(instance or ""), str(reason or ""))
        window = int(now // self._window)
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = {"window": window, "count": 1, "first_at": now,
                                  "last_at": now, "detail": str(detail or "")[:200]}
            return None
        if bucket["window"] != window:
            summary = self._summary(key, bucket)
            self._buckets[key] = {"window": window, "count": 1, "first_at": now,
                                  "last_at": now, "detail": str(detail or "")[:200]}
            return summary
        bucket["count"] += 1
        bucket["last_at"] = now
        if not bucket["detail"] and detail:
            bucket["detail"] = str(detail)[:200]
        return None

    def pending(self, instance: str, reason: str) -> int:
        bucket = self._buckets.get((str(instance or ""), str(reason or "")))
        return int(bucket["count"]) if bucket else 0

    def flush(self) -> list[dict]:
        """输出并清空所有未滚动窗口（count>=1，单次失败也立即落日志不丢）。"""
        out = []
        for key, bucket in list(self._buckets.items()):
            if bucket["count"] >= 1:
                out.append(self._summary(key, bucket))
            self._buckets.pop(key, None)
        return out

    def _summary(self, key: tuple[str, str], bucket: dict) -> dict:
        window_start = float(bucket["window"]) * self._window
        return {
            "instance": key[0],
            "reason": key[1],
            "count": int(bucket["count"]),
            "window_start": _iso(window_start),
            "window_start_ts": window_start,
            "first_at": _iso(bucket["first_at"]),
            "last_at": _iso(bucket["last_at"]),
            "detail": bucket.get("detail", ""),
        }


def log_summary(prefix: str, summary: dict) -> None:
    """统一的汇总日志行（4 个失败点共用，便于 Loki 按 `failures aggregated` 检索）。"""
    log.warning(
        "%s failures aggregated for channel state: inst=%s reason=%s count=%d "
        "window_start=%s last_at=%s sample=%s",
        prefix, summary.get("instance", ""), summary.get("reason", ""),
        int(summary.get("count") or 0), summary.get("window_start", ""),
        summary.get("last_at", ""), summary.get("detail", ""))


# ---------------------------------------------------------------- 状态机

class ChannelHealthTracker:
    """渠道健康状态机 + 指标 + 告警（引擎级共享，逐安装记录）。

    注入项全部可选（测试替身 / 非引擎场景）：`store` 持久化、`metrics` 计数、
    `clock` 时钟、`alert_sink` 告警外发、`installations_provider` 安装全集
    （判「全实例均暂停」）。
    """

    def __init__(self, *, store: Optional[ChannelHealthStore] = None,
                 metrics: Any = None, instance_id: str = "",
                 clock: Callable[[], float] = time.time,
                 alert_sink: Optional[Callable[[dict, str], None]] = None,
                 installations_provider: Optional[Callable[[], list]] = None) -> None:
        self._store = store
        self._metrics = metrics
        self._instance_id = str(instance_id or "")
        self._clock = clock
        self._alert_sink = alert_sink
        self._installations_provider = installations_provider
        self._states: dict[str, ChannelHealthState] = {}
        self._loaded = False
        # 本进程写过的安装（写侧真源，peer 刷新不覆盖）；上次 peer 重读 store 的时刻
        self._local_ids: set[str] = set()
        self._peer_refreshed_at = float("-inf")

    # ---------------- 状态读取

    def _load(self) -> None:
        if self._loaded or self._store is None:
            self._loaded = True
            return
        try:
            self._states = self._store.load()
        except Exception as e:      # 读失败按空态继续（不因为看板读不出而阻断渠道）
            log.warning("[im-channel-health] state load failed: %s", e)
            self._states = {}
        self._loaded = True

    def _state(self, installation_id: str, channel_type: str = "") -> ChannelHealthState:
        self._load()
        key = str(installation_id or "")
        state = self._states.get(key)
        if state is None:
            state = ChannelHealthState(installation_id=key, channel_type=channel_type,
                                       instance_id=self._instance_id)
            self._states[key] = state
        elif channel_type and not state.channel_type:
            state.channel_type = channel_type
        return state

    def state(self, installation_id: str, channel_type: str = "") -> dict:
        now = self._clock()
        return self._state(installation_id, channel_type).to_dict(now)

    def snapshot(self) -> list[dict]:
        self._load()
        now = self._clock()
        rows = [s.to_dict(now) for s in self._states.values() if s.installation_id]
        rows.sort(key=lambda r: r.get("last_pause_at_ts") or 0.0, reverse=True)
        return rows

    def is_paused(self, installation_id: str, now: Optional[float] = None) -> bool:
        key = str(installation_id or "")
        if not key:
            return False
        now = float(now if now is not None else self._clock())
        state = self._state(key)
        if not state.paused:
            return False
        if state.last_pause_at and now - state.last_pause_at > PAUSE_STATE_TTL_SECONDS:
            return False        # 遗留暂停态（进程崩溃）不再拦通知
        return True

    def paused_ids(self, now: Optional[float] = None) -> set[str]:
        self._load()
        now = float(now if now is not None else self._clock())
        return {inst for inst in list(self._states) if self.is_paused(inst, now)}

    # ---------------- 事件写入

    def record_pause(self, installation_id: str, channel_type: str = "",
                     reason: str = "", next_retry_at: Optional[float] = None) -> dict:
        """记一次暂停：状态 → 连续计数 → 阈值判定 → 指标/告警/持久化。"""
        now = self._clock()
        state = self._state(installation_id, channel_type)
        # 登记在 `_should_alert`（内部可能 `_refresh_peers`）之前：否则首次暂停时本安装
        # 还没进 `_local_ids`，peer 重读会用 store 旧行新建的对象替换掉本次正在修改的
        # 状态对象 → 库 paused=1 而内存 paused=0（PR #1983 R2 复审 Critical）。
        self._mark_local(state.installation_id)
        repeat = bool(state.last_pause_at
                      and now - state.last_pause_at <= PAUSE_REPEAT_WINDOW_SECONDS)
        state.consecutive_pauses = (state.consecutive_pauses + 1) if repeat else 1
        state.paused = True
        state.last_pause_at = now
        state.next_retry_at = float(next_retry_at if next_retry_at is not None
                                    else now)
        state.pause_count += 1
        state.last_pause_reason = str(reason or "")[:200]
        state.instance_id = self._instance_id or state.instance_id
        state.updated_at = now

        alert, alert_reason = self._should_alert(state, channel_type, now)
        if alert:
            state.alert_state = "firing"
            state.alerted_at = now
            state.alert_reason = alert_reason
            log.error(
                "[im-channel-health] alert=%s inst=%s type=%s instance=%s reason=%s "
                "consecutive=%d next_retry_at=%s —— 微信渠道不可用（账号级风控暂停），"
                "需人工重登/重试；通知已按渠道健康降级",
                ALERT_RULE, state.installation_id, state.channel_type or channel_type,
                state.instance_id, alert_reason, state.consecutive_pauses,
                _iso(state.next_retry_at))
            self._count_alert()
            if self._alert_sink is not None:
                try:
                    self._alert_sink(state.to_dict(now), alert_reason)
                except Exception as e:      # 外发失败不影响状态与主链路
                    log.warning("[im-channel-health] alert sink failed: %s", e)
        else:
            state.alert_state = "pending"
            log.warning(
                "[im-channel-health] channel paused inst=%s type=%s instance=%s "
                "reason=%s consecutive=%d next_retry_at=%s alert=pending",
                state.installation_id, state.channel_type or channel_type,
                state.instance_id, state.last_pause_reason,
                state.consecutive_pauses, _iso(state.next_retry_at))

        self._count_paused(state.installation_id)
        self._persist(state)
        return state.to_dict(now)

    def record_resume(self, installation_id: str, channel_type: str = "") -> Optional[dict]:
        """成功轮询即恢复：清 paused，告警落 resolved。未暂停 → None（不写库）。

        `consecutive_pauses` 语义 = 「repeat 窗口内的暂停次数」（不是"本次连续暂停时长"）：
        小时级风控循环里「暂停 1h → 恢复 → 再暂停」，若在这里清零，阈值永远停在 1、
        系统性失效永远不会升级为告警。窗口外的长健康期才会自然归零（见 record_pause）。
        """
        state = self._state(installation_id, channel_type)
        if not state.paused:
            return None
        now = self._clock()
        paused_for = max(0.0, now - state.last_pause_at) if state.last_pause_at else 0.0
        state.paused = False
        state.last_resume_at = now
        if state.alert_state in ("firing", "pending"):
            state.alert_state = "resolved"
            log.info("[im-channel-health] alert=%s resolved inst=%s type=%s "
                     "paused_for=%.0fs", ALERT_RULE, state.installation_id,
                     state.channel_type or channel_type, paused_for)
        state.updated_at = now
        self._persist(state)
        return state.to_dict(now)

    def clear_pause(self, installation_id: str, channel_type: str = "") -> dict:
        """人工清除暂停态（不动连接）：复位看板与连续计数。"""
        now = self._clock()
        state = self._state(installation_id, channel_type)
        state.paused = False
        state.consecutive_pauses = 0
        state.alert_state = "resolved" if state.alert_state else state.alert_state
        state.next_retry_at = 0.0
        state.last_recover_action = "clear_pause"
        state.last_recover_at = now
        state.updated_at = now
        self._persist(state)
        log.info("[im-channel-health] pause cleared by operator inst=%s type=%s",
                 state.installation_id, state.channel_type or channel_type)
        return state.to_dict(now)

    def mark_recover(self, installation_id: str, action: str,
                     channel_type: str = "") -> dict:
        """记录一次人工恢复动作：`next_retry_at=now`（立即重试）并标记动作。"""
        now = self._clock()
        state = self._state(installation_id, channel_type)
        state.next_retry_at = now
        state.last_recover_action = str(action or "")
        state.last_recover_at = now
        state.updated_at = now
        self._persist(state)
        log.info("[im-channel-health] recover requested inst=%s type=%s action=%s",
                 state.installation_id, state.channel_type or channel_type, action)
        return state.to_dict(now)

    def record_degraded_delivery(self, installation_id: str, channel_type: str,
                                 to: str = "", ok: bool = False) -> dict:
        """记一次「暂停期间的通知降级」：改投哪个渠道、是否成功（空 = 无可用降级渠道）。"""
        now = self._clock()
        state = self._state(installation_id, channel_type)
        state.degraded_count += 1
        state.last_degraded_at = now
        state.last_degraded_to = str(to or "")
        state.last_degraded_ok = bool(ok)
        state.updated_at = now
        self._persist(state)
        log.info("[im-channel-health] notify degraded inst=%s from=%s to=%s ok=%s",
                 state.installation_id, state.channel_type or channel_type,
                 state.last_degraded_to or "<none>", state.last_degraded_ok)
        return state.to_dict(now)

    # ---------------- 内部

    def _mark_local(self, installation_id: str) -> None:
        """登记「本进程写侧」安装：peer 刷新不得覆盖其内存态（PR #1983 R2 复审）。

        写侧真源 = 内存：`record_pause` 等入口在**修改前**登记，`_refresh_peers`
        就不会把这台安装正在写入的状态对象换成 store 里读出来的新对象。
        """
        key = str(installation_id or "")
        if key:
            self._local_ids.add(key)

    def _refresh_peers(self, now: float, keep: str = "") -> None:
        """按短 TTL 重读 store 里的 peer 状态（PR #1983 审查 Required）。

        多主控/跨进程下 `_load()` 只在首次执行：别的实例之后写入的暂停行
        内存快照看不到，「全实例均暂停」规则会漏告警。判该规则前按
        `PEER_STATE_REFRESH_SECONDS` 重读 store；本进程写过的安装
        （`_local_ids`）与调用栈上正在写入的安装（`keep`，PR #1983 R2 复审
        Critical）以内存为准，不被 store 旧值覆盖。
        """
        if self._store is None:
            return
        if now - self._peer_refreshed_at <= PEER_STATE_REFRESH_SECONDS:
            return
        self._peer_refreshed_at = now
        try:
            peers = self._store.load()
        except Exception as e:
            log.warning("[im-channel-health] peer state refresh failed: %s", e)
            return
        keep = str(keep or "")
        for inst, peer in peers.items():
            if inst in self._local_ids or inst == keep:
                continue
            self._states[inst] = peer

    def _should_alert(self, state: ChannelHealthState, channel_type: str,
                      now: float) -> tuple[bool, str]:
        if state.consecutive_pauses >= PAUSE_ALERT_CONSECUTIVE:
            return True, (f"consecutive_pauses={state.consecutive_pauses} "
                          f"within={int(PAUSE_REPEAT_WINDOW_SECONDS)}s")
        # peer 状态短 TTL 重读（多主控漏告警修复）；跳过本次正在写入的安装
        self._refresh_peers(now, keep=state.installation_id)
        population = self._population(channel_type)
        if len(population) >= 2:
            recent = 0
            for inst in population:
                peer = self._states.get(inst)
                if peer is None or not peer.paused:
                    continue
                if now - float(peer.last_pause_at or 0.0) <= PAUSE_ALERT_WINDOW_SECONDS:
                    recent += 1
            if recent >= len(population):
                return True, (f"all_instances_paused={recent}/{len(population)} "
                              f"within={int(PAUSE_ALERT_WINDOW_SECONDS)}s")
        return False, ""

    def _population(self, channel_type: str) -> list[str]:
        """同渠道的活跃安装全集（无 provider → 空，退化为只看单实例阈值）。"""
        if self._installations_provider is None:
            return []
        kind = str(channel_type or "").strip().lower()
        try:
            rows = self._installations_provider() or []
        except Exception as e:
            log.warning("[im-channel-health] installations provider failed: %s", e)
            return []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            inst = str(row.get("installation_id") or row.get("id") or "")
            if not inst:
                continue
            if kind and str(row.get("channel_type") or kind).strip().lower() != kind:
                continue
            if str(row.get("status") or "active").lower() != "active":
                continue
            out.append(inst)
        return out

    def _count_paused(self, installation_id: str) -> None:
        fn = getattr(self._metrics, "record_channel_paused", None)
        if fn is None:
            return
        try:
            fn(installation_id)
        except Exception as e:
            log.debug("[im-channel-health] metrics record skipped: %s", e)

    def _count_alert(self) -> None:
        fn = getattr(self._metrics, "record_channel_alert", None)
        if fn is None:
            return
        try:
            fn(ALERT_RULE)
        except Exception as e:
            log.debug("[im-channel-health] metrics alert skipped: %s", e)

    def _persist(self, state: ChannelHealthState) -> None:
        if self._store is None:
            return
        # 写过的安装登记为「本进程写侧」：peer 刷新（_refresh_peers）不覆盖其内存态
        self._local_ids.add(state.installation_id)
        try:
            self._store.upsert(state)
        except Exception as e:      # 仓储自身已兜底；这里再保一层，绝不打断渠道
            log.warning("[im-channel-health] persist error inst=%s: %s",
                        state.installation_id, e)


# ---------------------------------------------------------------- 默认实例 / 对外契约

_STORE: Optional[ChannelHealthStore] = None


def default_store() -> ChannelHealthStore:
    """进程级默认仓储（DB 路径在调用期解析，测试可换 env）。

    读侧（管理台/告警面板）不建表：缺表就返回空，DDL 责任在写侧与启动迁移。
    """
    global _STORE
    if _STORE is None:
        _STORE = ChannelHealthStore(ensure_ddl=False)
    return _STORE


def _reset_for_tests() -> None:
    global _STORE
    _STORE = None


def health_rows() -> list[dict]:
    """全渠道健康快照（管理台渠道卡片 / 告警看板读口；读失败 → 空列表）。"""
    try:
        states = default_store().load()
    except Exception as e:
        log.warning("[im-channel-health] health_rows failed: %s", e)
        return []
    now = time.time()
    rows = [s.to_dict(now) for s in states.values() if s.installation_id]
    rows.sort(key=lambda r: r.get("last_pause_at_ts") or 0.0, reverse=True)
    return rows


def _wake_pending_rows(installation_id: str) -> None:
    """恢复成功后立即补投：清掉该安装 pending 行的暂停退避（best-effort）。

    配套 `outbox.release_sending(retry_after=...)`：暂停行会被推到渠道
    `next_retry_at`（最长 1h），人工恢复要把它拉回「立刻可认领」。
    """
    try:
        from src.domains.runtime.im.outbox import wake_pending_rows
        wake_pending_rows(installation_id)
    except Exception as e:      # outbox 不可用/未迁移不影响恢复结果
        log.debug("[im-channel-health] outbox wake skipped inst=%s: %s", installation_id, e)


def recover_channel(installation_id: str, action: str,
                    engine: Any = None, tracker: Optional[ChannelHealthTracker] = None) -> dict:
    """人工恢复入口（管理台）：`relogin / clear_pause / retry_now`。

    返回 `{ok, action, installation_id, reason, health}`：
      - `retry_now`：唤醒渠道的暂停等待 → 立即重试 getupdates（成功即 record_resume）；
      - `relogin`：同 retry_now + 打断 Supervisor 扫描窗口（重连会重新执行 notifystart）；
      - `clear_pause`：只清状态（不动连接），用于人工确认已另行处理。
    无引擎（B/C/D 进程）→ `engine_unavailable`，**不改状态**（不假装成功）。
    """
    inst = str(installation_id or "")
    act = str(action or "").strip().lower()
    if act not in RECOVER_ACTIONS:
        raise ValueError(f"unknown recover action: {action}")
    if tracker is None and engine is not None:
        tracker = getattr(engine, "health_tracker", None)
    if tracker is None:
        return {"ok": False, "reason": "engine_unavailable", "action": act,
                "installation_id": inst}
    if act == "clear_pause":
        state = tracker.clear_pause(inst)
        _wake_pending_rows(inst)
        return {"ok": True, "action": act, "installation_id": inst, "reason": "",
                "health": state}

    supervisor = getattr(engine, "supervisor", None) if engine is not None else None
    channel = None
    if supervisor is not None:
        getter = getattr(supervisor, "channel_for", None)
        if getter is not None:
            try:
                channel = getter(inst)
            except Exception as e:
                log.warning("[im-channel-health] channel lookup failed inst=%s: %s", inst, e)
    woken = False
    if channel is not None:
        request = getattr(channel, "request_retry", None)
        if request is not None:
            try:
                woken = bool(request())
            except Exception as e:
                log.warning("[im-channel-health] request_retry failed inst=%s: %s", inst, e)
    if not woken:
        # 本进程没有这条安装的连接循环在跑（典型：该安装租约在别的主控）→
        # 不改状态，明确报 channel_not_running（管理台可重试/换机执行）。
        return {"ok": False, "action": act, "installation_id": inst,
                "reason": "channel_not_running"}
    state = tracker.mark_recover(inst, act)
    _wake_pending_rows(inst)     # 提前唤醒：恢复后 pending 行不再等暂停窗口
    if act == "relogin" and supervisor is not None:
        kick = getattr(supervisor, "kick", None)
        if kick is not None:
            try:
                kick()
            except Exception as e:
                log.warning("[im-channel-health] supervisor kick failed: %s", e)
    return {"ok": woken, "action": act, "installation_id": inst,
            "reason": "" if woken else "channel_not_running", "health": state}


__all__ = [
    "TABLE", "ALERT_RULE", "PAUSE_ALERT_CONSECUTIVE", "PAUSE_REPEAT_WINDOW_SECONDS",
    "PAUSE_ALERT_WINDOW_SECONDS", "PAUSE_STATE_TTL_SECONDS", "LOG_WINDOW_SECONDS",
    "PEER_STATE_REFRESH_SECONDS",
    "RECOVER_ACTIONS", "ChannelHealthState", "ChannelHealthStore",
    "ChannelHealthTracker", "HourlyLogAggregator", "log_summary",
    "default_store", "health_rows", "recover_channel",
]