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
from typing import Awaitable, Callable, Optional

from ..channel.channel import (
    CancelContext, Channel, ChannelConfig, ChannelError, PermanentChannelError,
)
from ..channel.registry import ChannelRegistry
from ..channel.types import Scope, Type
from .hashring import build_ring, owner
from .lease import lease_key
from .metrics import EngineMetrics

BACKOFF_MIN = 2.0   # 指数退避 2s→15s（继承 run_bot）
BACKOFF_MAX = 15.0

log = logging.getLogger(__name__)


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
        self.instances: list = [instance_id]  # 扫描前由 list_active_instances 刷新

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.get_running_loop().create_task(self._scan_loop())

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
            await asyncio.sleep(self._scan_interval)

    async def _scan_once(self) -> None:
        if not self._running:
            return
        self._refresh_instances()
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

        async def _run() -> None:
            backoff = BACKOFF_MIN
            try:
                while self._running and not ctx.cancelled:
                    try:
                        await channel.connect(ctx)
                        backoff = BACKOFF_MIN
                    except PermanentChannelError:      # A5：永久错误 → revoked 不再重试
                        self._revoked.add(inst_id)
                        self._channels.pop(inst_id, None)
                        self._release_lease(inst_id)
                        return
                    except ChannelError:
                        # 链路断开 → 退避重连（Issue #870：reconnects +1，抖动归因 ws_transport）
                        # Issue #1728：微信 ilink 长轮询断线单独归因 ilink_longpoll，便于看板区分。
                        self._record_reconnect(
                            "ilink_longpoll" if channel_type == Type.WECHAT.value
                            else "ws_transport")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, BACKOFF_MAX)
                    except Exception:
                        # 非契约异常（适配器 bug / 意外 payload 形态）：绝不能让任务
                        # 就此静默死亡（2026-11-05 钉钉 data 为字符串 → AttributeError
                        # 打死 connect 任务，channels 恒为 1 但永不收消息）。按瞬时故障
                        # 计 ws_transport 抖动并退避重连，异常栈留在日志。
                        log.exception("[im-engine] channel crashed (unexpected): inst=%s type=%s",
                                      inst_id, channel_type)
                        self._record_reconnect(
                            "ilink_longpoll" if channel_type == Type.WECHAT.value
                            else "ws_transport")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, BACKOFF_MAX)
                    else:
                        # 正常 return：ctx 取消 = 停机；否则 = lease 失配优雅让渡
                        if ctx.cancelled:
                            self._record_disconnect("shutdown")
                        else:
                            self._record_disconnect("lease")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, BACKOFF_MAX)
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
        if self._leases is None:
            return
        channel_type = self._channel_types.pop(inst_id, "feishu")
        token = self._lease_tokens.pop(inst_id, None)
        try:
            self._leases.release(lease_key(inst_id, channel_type), token)
        except Exception:
            pass


__all__ = ["Supervisor", "fingerprint", "BACKOFF_MIN", "BACKOFF_MAX"]
