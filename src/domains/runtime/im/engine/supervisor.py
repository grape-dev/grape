"""Supervisor —— 连接生命周期治理（Issue #742 §4.2，继承 grape 资产 A1/A2/A5/A6/A7）。

P0/P1 双轨迁移（§5.3）：
  P0：installation_provider = load_eligible_bots 包装（照常跑，同步生成
       grape_t_channel_installation 行 = 双写）；
  P1：provider 改从 grape_t_channel_installation 枚举，load_eligible_bots 降级对账。

依赖铁律：只 import im_channel；适配器启动时注册进 Registry，本模块
通过 Registry.build() 间接构建，永不 import 适配器。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Awaitable, Callable, Optional

from ..channel.channel import (
    CancelContext, Channel, ChannelConfig, ChannelError,
    ChannelRegistrationConflict, PermanentChannelError,
)
from ..channel.registry import ChannelRegistry
from ..channel.types import Scope, Type
from .hashring import build_ring, owner
from .lease import lease_key
from .metrics import EngineMetrics

BACKOFF_MIN = 2.0   # 指数退避 2s→15s（继承 run_bot）
BACKOFF_MAX = 15.0

# Issue #3698：并发注册冲突（飞书 WS 关闭码 3001 too many connections）不是普通链路
# 抖动——服务端对同一 app 的并发长连接注册数已满，15s 短退避只会持续撞上限并刷屏。
# 单独一档更长退避（5s→300s），并在连续多次失败后升级为可告警的 ERROR。
REGISTER_CONFLICT_BACKOFF_MIN = 5.0
REGISTER_CONFLICT_BACKOFF_MAX = 300.0
REGISTER_CONFLICT_ALERT_AFTER = 5
# 一次连接健康存活 ≥ 该时长 → 视为已恢复，重置并发注册冲突的连续计数。
REGISTER_CONFLICT_STABLE_SECS = 60.0

# Issue #3897：传输层断连异常族——「链路抖动」，不是适配器 bug。
# 生产现场 `channel crashed (unexpected)` 的异常栈即 asyncio selector_events 写 socket 时
# 抛的 BrokenPipeError（1h 11 条）。这类异常必须按传输抖动 WARNING 归因 + 既有指数退避，
# 不能落通用 except 打成全栈 ERROR（既污染 error 计数，也让 `channel crashed` 验收无法归零）。
_TRANSPORT_ERRORS = (
    BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError,
    asyncio.IncompleteReadError, TimeoutError, asyncio.TimeoutError,
)

log = logging.getLogger(__name__)


def next_backoff(current: float, *, register_conflict: bool = False) -> float:
    """退避递推（Issue #3698）：普通抖动 2s→15s 封顶；并发注册冲突 5s→300s 封顶。"""
    if register_conflict:
        floor, cap = REGISTER_CONFLICT_BACKOFF_MIN, REGISTER_CONFLICT_BACKOFF_MAX
    else:
        floor, cap = BACKOFF_MIN, BACKOFF_MAX
    return min(max(current, floor) * 2, cap)


def app_identity(raw: dict) -> str:
    """安装配置里的 app 身份（同 app 多安装去重键）。取不到 → 空串（不去重）。

    只探测各适配器真正读取的身份键（adapters/*/channel.py）：app_id（飞书/钉钉）、
    bot_id（企微）、ilink_bot_id（微信）、bot_token（Slack/Telegram）。
    历史遗留的 `token` 键没有任何适配器读取，探测它等于把凭据值当 app 身份去重
    并让其进入日志（PR #1947 审查意见 Optional）。
    """
    for key in ("app_id", "bot_id", "ilink_bot_id", "bot_token"):
        value = str((raw or {}).get(key) or "")
        if value:
            return value
    return ""


# 只有「已知的公开 app 标识形态」可明文入日志（飞书 cli_xxx / 钉钉 dingxxx），
# 其余（企微 bot_id、微信 ilink_bot_id、任意凭据形态）一律摘要。
_PUBLIC_APP_REF_RE = re.compile(r"^(?:cli_[0-9A-Za-z]{6,64}|ding[0-9A-Za-z]{6,64})$")


def _is_public_app_ref(value: str) -> bool:
    """是否为已知的公开 app 标识（白名单形态，其余一律视为不可明文）。"""
    return bool(_PUBLIC_APP_REF_RE.match(value))


def _app_log_ref(app_key: str, channel_type: str = "") -> str:
    """日志里的 app 标识：白名单外一律输出不可逆摘要，日志永不出现凭据片段。

    PR #1947 审查意见 Optional：原实现对非 slack/telegram 类型明文截取前 12 字符 ——
    只要安装 config 的 app 身份位承载凭据（企微 bot_id、微信 ilink_bot_id 等），就会把
    凭据片段写进日志。改为默认摘要、仅飞书/钉钉的公开 app_id 形态明文。
    """
    value = str(app_key or "")
    if not value:
        return ""
    kind = str(channel_type or "").strip().lower()
    head = value.split(":", 1)[0]
    telegramish = ":" in value and head.isdigit()
    if (kind in ("slack", "telegram")
            or telegramish
            or not _is_public_app_ref(value)):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"{kind or 'app'}:{digest}"
    return value


async def _backoff_sleep(delay: float) -> None:
    """退避睡眠（测试 seam：用例可替身以跳过真实等待，不改变生产语义）。"""
    await asyncio.sleep(delay)


# config 里的运行期状态键（不是凭证）：改了也不该拆连接重建。
# 例：微信 ilink 把 get_updates_buf 游标持久化在 config["cursor"]，每次轮询推进都会
# 变指纹 → Supervisor 每轮扫描都热重建渠道 → connect/disconnect 抖动、channels 归零。
_VOLATILE_CONFIG_KEYS = ("cursor",)


def fingerprint(raw: dict) -> str:
    """凭证指纹（multica 增量，A5 升级）：config 变化 → 拆旧连接按新配置重建。

    运行期状态（cursors 等）不参与指纹，否则游标一推进就会自我触发重建。
    """
    stable = {k: v for k, v in (raw or {}).items() if k not in _VOLATILE_CONFIG_KEYS}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


class Supervisor:
    """每个活跃安装一个 asyncio task。

    步骤：周期扫描安装（不按平台过滤——multica MUL-3620 教训）
    → 哈希环归属预筛 → 租约 CAS → Registry.build → connect；
    PermanentChannelError → 置 revoked；环迁移 → 优雅让渡（不续约随 TTL 释放）。
    """

    def __init__(
        self,
        *,
        registry: ChannelRegistry,
        instance_id: str,
        lease_store=None,
        installation_provider: Callable[[], Awaitable[list]],
        scan_interval: float = 30.0,
        metrics: Optional[EngineMetrics] = None,
    ) -> None:
        self._registry = registry
        self._metrics = metrics
        self.instance_id = instance_id
        self._leases = lease_store
        self._provider = installation_provider
        self._scan_interval = scan_interval
        self._channels: dict[str, tuple[Channel, CancelContext]] = {}
        self._fingerprints: dict[str, str] = {}
        self._channel_types: dict[str, str] = {}   # inst_id -> channel_type（构租约键用）
        self._lease_tokens: dict[str, str] = {}    # inst_id -> acquire token（release 用，不探测实现私有状态）
        self._revoked: set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._run_tasks: dict[str, asyncio.Task] = {}   # inst_id → 连接循环任务（stop 收敛用）
        self._running = False
        # 配置保存后打断「等 30s 扫描」的唤醒闸（kick）：Event 在 loop 内等，
        # 保存接口在任何线程都能唤醒（call_soon_threadsafe 兜底）。
        self._wake = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # inst_id → 已并入 metrics 的渠道侧接管计数（软处理事件后仍要可观测，A13）
        self._takeover_seen: dict[str, int] = {}
        # Issue #3698：同 app_id 单连接/单租约。同一 app 的多个安装行（或旧连接残留）
        # 并发注册会撞服务端上限（飞书 3001）；这里按 app 身份去重，只允许一条连接，
        # 被跳过的安装显式记录（不静默删安装行），owner 让渡后下一轮自然接上。
        self._app_refs: dict[str, str] = {}         # app 身份 → 持有连接的 inst_id
        self._app_ref_by_inst: dict[str, str] = {}  # inst_id → app 身份（释放用）
        self._duplicate_installs: dict[str, str] = {}   # 被跳过的 inst_id → owner inst_id
        self.instances: list = [instance_id]  # 扫描前由 list_active_instances 刷新

    async def start(self) -> None:
        self._running = True
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._task = asyncio.get_running_loop().create_task(self._scan_loop())

    def kick(self) -> bool:
        """立刻打断扫描等待（凭证保存/解绑后秒级建连或让渡，不等 30s 窗口）。

        同步、绝不抛：Event.set 必须在持有 loop 的线程上执行，跨线程走
        call_soon_threadsafe；loop 未起（start 前）或已停 → 留待自然扫描。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wake.set()
            log.info("[supervisor] scan kicked (config change)")
            return True
        try:
            loop.call_soon_threadsafe(self._wake.set)
            log.info("[supervisor] scan kicked (config change, cross-thread)")
            return True
        except RuntimeError:
            return False

    async def stop(self) -> None:
        """A7：stop → release_all + 入口 _running 检查。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for inst_id, (channel, ctx) in list(self._channels.items()):
            ctx.cancel()
            await channel.disconnect()
            run_task = self._run_tasks.get(inst_id)
            if run_task is not None:
                # 收敛连接循环：让 shutdown 抖动归因即时落账（Issue #870）
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._release_lease(inst_id)
        self._channels.clear()

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 单轮扫描异常不能让整轮静默消失（2026-11-06 实障：企微安装行
                # 一直没被建连，日志里连一行都没有——异常在这里被 pass 吞掉了，
                # 排查只能靠猜）。记栈 + 退避后下一轮继续。
                log.warning("[supervisor] scan round failed: %s", exc, exc_info=True)
            # 等下一轮，但配置保存（kick）可以立刻打断：否则新保存的凭证最多要等
            # 一整个扫描窗口才建连（企微首条通知就撞上 "long connection is not
            # established"）。Event 语义：被 kick → 立即下一轮；否则睡满窗口。
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._scan_interval)
                # 只在真被唤醒时清位：避免清了之后 kick 恰好落在 wait 之前被吃掉
                # （多扫一轮无副作用，漏掉一次 kick 就是 30s 窗口）。
                self._wake.clear()
            except asyncio.TimeoutError:
                log.debug("[supervisor] scan window elapsed (%.0fs)", self._scan_interval)

    async def _scan_once(self) -> None:
        if not self._running:
            return
        self._refresh_instances()
        self._sync_channel_counters()
        await self._renew_held_leases()
        installations = await self._provider()
        ring = build_ring(self.instances)
        seen: set[str] = set()
        for inst in installations:
            inst_id = str(inst.get("installation_id") or inst.get("id"))
            if not inst_id or inst_id in self._revoked:
                continue
            seen.add(inst_id)
            # 哈希环归属预筛（A1）：非归属实例不 try_acquire
            if ring and owner(ring, inst_id) != self.instance_id:
                continue
            await self._ensure_channel(inst_id, inst)
        # 优雅让渡：不再归属的安装随 release 释放（A7 / L1750 语义）
        for inst_id in list(self._channels):
            if inst_id not in seen:
                await self._yield_channel(inst_id)
        # 被跳过的安装已不在 provider 里 → 清掉记账，避免无界增长
        for inst_id in list(self._duplicate_installs):
            if inst_id not in seen:
                self._duplicate_installs.pop(inst_id, None)

    def _refresh_instances(self) -> None:
        """每轮扫描前刷新活跃实例集（grape_im_instance 心跳 + im_enabled=1）。

        过滤语义由 BotLeaseStore.list_active_instances 保证（心跳上下界 +
        im_enabled=1，CST 脏行不进环）；本实例自身恒在环中（自己必然在跑）。
        store 未实现该方法（旧测试替身/旧装配）→ 保留现状不动。

        先上报自身心跳再读活跃集：新引擎此前从不 heartbeat，`grape_im_instance`
        里没有它的行，`list_active_instances()` 恒为空 → 环只剩自己，多实例仍不分片
        （#869 复核补丁）。
        """
        if self._leases is None:
            return
        heartbeat = getattr(self._leases, "heartbeat", None)
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:
                pass
        list_active = getattr(self._leases, "list_active_instances", None)
        if list_active is None:
            return
        try:
            active = list_active()
        except Exception:
            return
        instances = set(active or [])
        instances.add(self.instance_id)
        self.instances = sorted(instances)

    async def _renew_held_leases(self) -> None:
        """每轮对已持租约 batch_renew（TTL 60s / 扫描 30s → 2 倍余量，#869）。

        续约失败（被抢占/失主）→ 走优雅让渡路径拆连接释放，不硬持锁。
        store 异常/返回口径不识别 → 本轮跳过（下一轮再续，TTL 余量兜底）。
        """
        if self._leases is None or not self._lease_tokens:
            return
        # A2 M3：租约键就是 "<channel_type>:<installation_id>"（store 侧不再做键翻译），
        # 所以续约必须按 lease_key 提交、按 lease_key 读结果；用裸 inst_id 会永远查不到
        # → 每轮判 False → 优雅让渡 → 连接反复重建（M3 首轮踩过）。
        held = {
            lease_key(inst_id, self._channel_types.get(inst_id, "feishu")): token
            for inst_id, token in self._lease_tokens.items()
        }
        try:
            results = self._leases.batch_renew(held)
        except Exception:
            return
        if not isinstance(results, dict):
            return
        for inst_id in list(self._lease_tokens):
            key = lease_key(inst_id, self._channel_types.get(inst_id, "feishu"))
            if results.get(key) is False:
                await self._yield_channel(inst_id)

    async def _yield_channel(self, inst_id: str) -> None:
        """优雅让渡：拆连接 + 释放租约（A7 语义，供环迁移/续约失败复用）。"""
        entry = self._channels.pop(inst_id, None)
        if entry:
            channel, ctx = entry
            ctx.cancel()
            await channel.disconnect()
        self._release_lease(inst_id)

    async def _ensure_channel(self, inst_id: str, inst: dict) -> None:
        raw = inst.get("config") or {}
        fp = fingerprint(raw)
        channel_type = str(inst.get("channel_type", "feishu"))
        app_key = app_identity(raw)
        if app_key:
            holder = self._app_refs.get(app_key)
            if holder and holder != inst_id and holder in self._channels:
                # Issue #3698：同一 app 并发注册会撞服务端上限（飞书 3001）——同一时刻
                # 只保留一条连接。被跳过的安装显式记录（不静默删安装行）；owner 让渡/
                # 断连释放归属后，下一轮扫描自然接上。
                if self._duplicate_installs.get(inst_id) != holder:
                    log.warning(
                        "[im-engine] duplicate app installation skipped: app=%s "
                        "inst=%s owner=%s type=%s",
                        _app_log_ref(app_key, channel_type), inst_id, holder, channel_type)
                self._duplicate_installs[inst_id] = holder
                return
            self._duplicate_installs.pop(inst_id, None)
        if inst_id in self._channels:
            if (self._fingerprints.get(inst_id) == fp
                    and self._channel_types.get(inst_id) == channel_type):
                return
            # 凭证轮转热重建：拆旧连接按新配置重建
            # （channel_type 也要参与比较：安装行被纠正/回填成正确渠道时，
            #  config 未变但必须换适配器重建，否则旧类型渠道会一直带着错适配器跑）
            old_channel, old_ctx = self._channels.pop(inst_id)
            old_ctx.cancel()
            await old_channel.disconnect()
        self._fingerprints[inst_id] = fp
        self._channel_types[inst_id] = channel_type

        cfg = ChannelConfig(
            type=Type(channel_type),
            raw=raw,
            installation_id=inst_id,
            scope=Scope(inst.get("scope", "user")),
        )
        channel = self._registry.build(cfg)
        if channel is None:
            return
        if self._leases is not None:
            key = lease_key(inst_id, channel_type)   # §4.2 跨平台租约键契约
            token = self._leases.try_acquire(key)
            if not token:
                return
            self._lease_tokens[inst_id] = token
        ctx = CancelContext()
        self._channels[inst_id] = (channel, ctx)
        if app_key:
            self._app_refs[app_key] = inst_id
            self._app_ref_by_inst[inst_id] = app_key

        async def _run() -> None:
            backoff = BACKOFF_MIN
            conflict_backoff = REGISTER_CONFLICT_BACKOFF_MIN
            consecutive_conflicts = 0
            # Issue #3698（PR #1947 审查返修）：上一轮 connect() 的真实存活时长。
            # connect() 只在会话结束时才返回/抛出（正常断开或链路断开），故该值 =
            # "上一次连接健康运行了多久"。并发注册冲突分支自身的 connect() 是握手期
            # 快速失败（毫秒级），用本轮 elapsed 判断"是否已恢复"恒为否 —— 健康长连接
            # 断开（ChannelError）后再注册撞 3001 时，计数/退避会跨健康期残留：非连续
            # 冲突误升级 ERROR 告警、恢复后首次冲突仍沿用最长 300s 退避。
            # 因此记录上一会话存活时长，由冲突分支据此重置。
            last_session_secs = 0.0
            try:
                while self._running and not ctx.cancelled:
                    started = time.monotonic()
                    try:
                        await channel.connect(ctx)
                        last_session_secs = time.monotonic() - started
                        backoff = BACKOFF_MIN
                        conflict_backoff = REGISTER_CONFLICT_BACKOFF_MIN
                        consecutive_conflicts = 0
                    except PermanentChannelError:      # A5：永久错误 → revoked 不再重试
                        self._revoked.add(inst_id)
                        self._channels.pop(inst_id, None)
                        self._release_lease(inst_id)
                        return
                    except ChannelRegistrationConflict:
                        # Issue #3698：服务端并发注册上限（飞书 3001）。可重试但同一时刻
                        # 重连只会继续撞上限——单独归因、长退避，连续多次后升级告警。
                        if last_session_secs >= REGISTER_CONFLICT_STABLE_SECS:
                            # 上一轮连接已健康存活 → 这是一轮新冲突，重新计数/退回最短退避
                            conflict_backoff = REGISTER_CONFLICT_BACKOFF_MIN
                            consecutive_conflicts = 0
                        # 冲突是快速失败：把"上一会话存活时长"换成本次尝试的时长，避免陈旧
                        # 健康标记让后续每次冲突都重复触发重置（计数恒为 1、告警失灵）。
                        last_session_secs = time.monotonic() - started
                        consecutive_conflicts += 1
                        self._record_reconnect("ws_register_conflict")
                        if consecutive_conflicts >= REGISTER_CONFLICT_ALERT_AFTER:
                            log.error(
                                "[im-engine] channel registration conflict escalated: "
                                "inst=%s type=%s consecutive=%d retry_in=%.0fs "
                                "cause=ws_register_conflict "
                                "（同一 app 并发注册上限持续被撞，需核对重复安装/僵尸连接）",
                                inst_id, channel_type, consecutive_conflicts,
                                conflict_backoff)
                        else:
                            log.warning(
                                "[im-engine] channel registration conflict: inst=%s "
                                "type=%s consecutive=%d retry_in=%.0fs",
                                inst_id, channel_type, consecutive_conflicts,
                                conflict_backoff)
                        await _backoff_sleep(conflict_backoff)
                        conflict_backoff = next_backoff(
                            conflict_backoff, register_conflict=True)
                    except ChannelError:
                        # 链路断开 → 退避重连（Issue #870：reconnects +1，抖动归因 ws_transport）
                        # Issue #1728：微信 ilink 长轮询断线单独归因 ilink_longpoll，便于看板区分。
                        # 记录本会话存活时长：下一轮若撞并发注册冲突，据此判断是否已恢复。
                        last_session_secs = time.monotonic() - started
                        self._record_reconnect(
                            "ilink_longpoll" if channel_type == Type.WECHAT.value
                            else "ws_transport")
                        await _backoff_sleep(backoff)
                        backoff = next_backoff(backoff)
                    except _TRANSPORT_ERRORS as e:
                        # Issue #3897：传输层断连（BrokenPipe/ConnectionReset/超时…）就是
                        # 「链路抖动」——WARNING 单行归因（inst/type/cause/errno），走既有
                        # 指数退避重连；不进通用 except，避免 `channel crashed (unexpected)`
                        # 全栈 ERROR 刷屏（生产 1h 11 条）与 error 计数污染。
                        last_session_secs = time.monotonic() - started
                        _cause = ("ilink_longpoll" if channel_type == Type.WECHAT.value
                                  else "ws_transport")
                        log.warning(
                            "[im-engine] channel transport error: inst=%s type=%s "
                            "cause=%s errtype=%s errno=%s retry_in=%.0fs: %s",
                            inst_id, channel_type, _cause, type(e).__name__,
                            getattr(e, "errno", None), backoff, e)
                        self._record_reconnect(_cause)
                        await _backoff_sleep(backoff)
                        backoff = next_backoff(backoff)
                    except Exception:
                        # 非契约异常（适配器 bug / 意外 payload 形态）：绝不能让任务
                        # 就此静默死亡（2026-11-05 钉钉 data 为字符串 → AttributeError
                        # 打死 connect 任务，channels 恒为 1 但永不收消息）。按瞬时故障
                        # 计 ws_transport 抖动并退避重连，异常栈留在日志。
                        last_session_secs = time.monotonic() - started
                        log.exception("[im-engine] channel crashed (unexpected): inst=%s type=%s",
                                      inst_id, channel_type)
                        self._record_reconnect(
                            "ilink_longpoll" if channel_type == Type.WECHAT.value
                            else "ws_transport")
                        await _backoff_sleep(backoff)
                        backoff = next_backoff(backoff)
                    else:
                        # 正常 return：ctx 取消 = 停机；否则 = lease 失配优雅让渡
                        if ctx.cancelled:
                            self._record_disconnect("shutdown")
                        else:
                            self._record_disconnect("lease")
                        await _backoff_sleep(backoff)
                        backoff = next_backoff(backoff)
            except asyncio.CancelledError:
                # stop() 收敛 / 外部取消：停机归因（退避睡眠中被取消也落账，Issue #870）
                self._record_disconnect("shutdown")
                return

        task = asyncio.get_running_loop().create_task(_run())
        self._run_tasks[inst_id] = task

        def _done(t: asyncio.Task, _inst_id: str = inst_id) -> None:
            if self._run_tasks.get(_inst_id) is t:
                self._run_tasks.pop(_inst_id, None)

        task.add_done_callback(_done)

    def channel_for(self, inst_id: str):
        """取某安装当前活着的渠道（未建连/已让渡 → None）。

        装配层要按渠道发消息时走这里 + `Channel.send()`（公开契约），
        不要再读 `_channels[...][0]._sender` 这类私有属性（架构审查 A8）。
        """
        entry = self._channels.get(str(inst_id or ""))
        if not entry:
            return None
        return entry[0] if isinstance(entry, tuple) else entry

    def _sync_channel_counters(self) -> None:
        """把渠道侧自计数并入既有 flap 归因桶（架构审查 A13）。

        企微 `disconnected_event` 被软处理（保留 socket、不重连）后不再走
        `except ChannelError` 路径，`reconnects`/`flap_by_cause` 会归零 ——
        「同一个 bot 被别的会话持续顶掉」会变成完全不可见的静默失效。
        渠道只暴露累计只读计数（`takeover_count`），归因仍由本层写入既有桶，
        保持"指标只有一个写入方"的分层；按增量记账，渠道重建后计数归零也不会重复计。
        """
        if self._metrics is None:
            return
        for inst_id, entry in list(self._channels.items()):
            channel = entry[0] if isinstance(entry, tuple) else entry
            try:
                current = int(getattr(channel, "takeover_count", 0) or 0)
            except Exception:
                continue
            seen = self._takeover_seen.get(inst_id, 0)
            if current < seen:
                # 渠道重建（换个连接对象）→ 计数从 0 重新累计，按重置处理，
                # 否则重建后的新事件会被旧基线吞掉。
                seen = 0
            if current > seen:
                for _ in range(current - seen):
                    self._metrics.record_disconnect("takeover")
            self._takeover_seen[inst_id] = current

    def _record_reconnect(self, cause: str) -> None:
        """Issue #870：重连计数 + 按原因抖动（metrics 未注入时零开销）。"""
        if self._metrics is not None:
            self._metrics.record_reconnect(cause)

    def _record_disconnect(self, cause: str) -> None:
        """Issue #870：不重连的断开仅记抖动归因。"""
        if self._metrics is not None:
            self._metrics.record_disconnect(cause)

    def _release_lease(self, inst_id: str) -> None:
        """按 §4.2 租约键契约释放；token 由本方持有（不探测 LeaseStore 私有属性）。"""
        self._drop_app_ref(inst_id)
        if self._leases is None:
            return
        channel_type = self._channel_types.pop(inst_id, "feishu")
        token = self._lease_tokens.pop(inst_id, None)
        try:
            self._leases.release(lease_key(inst_id, channel_type), token)
        except Exception:
            pass

    def _drop_app_ref(self, inst_id: str) -> None:
        """释放 app 身份归属（让渡/revoke/停机）：同 app 的其他安装可在下一轮接上。"""
        app_key = self._app_ref_by_inst.pop(inst_id, None)
        self._duplicate_installs.pop(inst_id, None)
        if app_key and self._app_refs.get(app_key) == inst_id:
            self._app_refs.pop(app_key, None)


__all__ = [
    "Supervisor", "fingerprint", "app_identity", "next_backoff",
    "BACKOFF_MIN", "BACKOFF_MAX",
    "REGISTER_CONFLICT_BACKOFF_MIN", "REGISTER_CONFLICT_BACKOFF_MAX",
    "REGISTER_CONFLICT_ALERT_AFTER",
]
