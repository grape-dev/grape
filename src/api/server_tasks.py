"""Process-scoped background task starters for Issue #699 Phase 4 (four-process split).

The four Grape processes each run only the tasks owned by their role (design §5.1~5.4):

- ``start_mgmt_tasks``:    Mgmt      (user-facing loops, IM WS, knowledge/skill/feedback,
                                      upload/cooldown, migrations, active-user tracking)
- ``start_event_tasks``:   Event     (sweep_events_loop, bot_token_health)
- ``start_worker_tasks``:  Worker    (local_claimer, autopilot scheduler, health_refresh,
                                      local_probe, reap_stale, sweep_tasks_loop,
                                      table_maintenance agent_tasks part)
- ``start_insight_tasks``: Insight   (obs_rollup/obs_sample/obs_alert+SLO, server_metrics,
                                      install_master_logging, trace stamp,
                                      table_maintenance task_messages part)

The legacy single-process entry (``src.api.main``) calls all four groups (union mode) plus
its combined sweep_loop / table-maintenance, so behavior is unchanged. The four-process
servers call only their own group. Code moved verbatim from the legacy lifespan.
"""
import asyncio
import contextlib
import io
import os

from src.kernel.host_identity import engine_host  # Issue #2094: 引擎/设备身份（GRAPE_HOSTNAME 可覆盖）
from src.kernel.config import log


class _StructuredScriptOutput(io.TextIOBase):
    """把脚本 ``print()`` 的 stdout 按行转成结构化日志。

    迁移脚本（scripts.migrate_user_accounts_to_grape）在 mgmt 启动时会往 stdout 打大量
    纯文本（[阶段 3] / [COLLISION] / [ORPHAN] ...）。Alloy 原样采集后 Loki 无法判定级别，
    Grafana 的 grape-mgmt 面板里就出现大片 level=unknown。这里按内容分流到 INFO/WARNING，
    保证每行都是 JSON 且带 level 字段。
    """

    _WARN_HINTS = ("COLLISION", "ORPHAN", "SCAN", "WARN", "失败", "异常", "撞号")
    # 同一类明细行每次迁移最多打 20 条，其余折叠成一条汇总，避免刷屏
    _MAX_PER_TAG = 20

    def __init__(self) -> None:
        self._buf = ""
        self._tag_counts: dict[str, int] = {}

    @staticmethod
    def _tag_of(message: str) -> str:
        bracket = message.lstrip()
        if bracket.startswith("[") and "]" in bracket:
            return bracket[: bracket.index("]") + 1]
        return message.lstrip()[:16]

    def write(self, chunk: str) -> int:  # type: ignore[override]
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(chunk)

    def flush(self) -> None:  # type: ignore[override]
        if self._buf:
            self._emit(self._buf)
            self._buf = ""
        for tag, total in self._tag_counts.items():
            suppressed = total - self._MAX_PER_TAG
            if suppressed > 0:
                log.warning(f"{tag} 其余 {suppressed} 条同类明细已折叠（本次迁移共 {total} 条）")

    def _emit(self, line: str) -> None:
        line = line.rstrip()
        if not line.strip():
            return
        message = line.strip()
        tag = self._tag_of(message)
        self._tag_counts[tag] = self._tag_counts.get(tag, 0) + 1
        if self._tag_counts[tag] > self._MAX_PER_TAG:
            return
        if any(h in message for h in self._WARN_HINTS):
            log.warning(message)
        else:
            log.info(message)

__all__ = [
    "start_mgmt_tasks",
    "stop_mgmt_tasks",
    "start_event_tasks",
    "stop_event_tasks",
    "start_worker_tasks",
    "stop_worker_tasks",
    "start_insight_tasks",
    "stop_insight_tasks",
]


# ===========================================================================
# Mgmt 进程（grape_server :38080）
# ===========================================================================

async def start_mgmt_tasks(app) -> dict:
    """Mgmt 进程定时任务/后台循环（设计 §5.1）。返回关闭句柄字典。"""
    h = {}

    # Issue #455：对外基址未配置告警——OAuth 回调/安装命令依赖 server_base_url
    try:
        from src.domains.mgmt.user.db import get_server_base_url_warning
        _base_warn = get_server_base_url_warning()
        if _base_warn:
            log.warning(f"[CONFIG] {_base_warn}")
        else:
            log.info("[CONFIG] server_base_url configured OK")
    except Exception as _bw:
        log.warning(f"[CONFIG] server_base_url check failed: {_bw}")

    try:
        from src.domains.runtime.im.feishu_async import BotManager
        from src.domains.runtime.im.handler_async import process_chat_async
        from src.domains.runtime.im.vod_handler_async import process_vod_async

        # psutil CPU 基准预热：interval=None 模式需要先调一次建立基准
        try:
            import psutil as _ps_init
            _ps_init.cpu_percent(interval=None)
        except Exception:
            pass

        # 启动引擎健康探测线程（每 10 分钟对引擎发起 CLI 测试对话）
        try:
            from src.domains.insight.observability.engine_probe import start_probe
            start_probe()
        except Exception as e:
            log.warning(f"Engine probe start failed: {e}")

        # 模板自愈：启动时检查 project-templates.json + examples/ 是否存在
        try:
            from src.api import server_common
            _templates_ok = server_common.check_and_sync_templates()
            if _templates_ok:
                log.info("Templates: project-templates.json + examples/ verified OK")
            else:
                log.warning("Templates: auto-sync attempted but issues remain, check logs")
        except Exception as e:
            log.warning(f"Templates: startup check failed: {e}")

        # help.html 生成物：内容由 help/（usage + api）渲染，启动即刷新
        try:
            from src.kernel.help_page_gen import generate as _generate_help_page
            _n_help = _generate_help_page()
            log.info(f"Help: help.html regenerated ({_n_help} sources)")
        except Exception as e:
            log.warning(f"Help: help.html generation failed (kept last generated file): {e}")

        # OpenAPI 契约落盘：help/api/openapi.json（规范副本）+ static/openapi.json
        # 4 进程拆分后由 mgmt 作为契约 owner 合并四进程 spec（tasks/patrol/pipeline-handoff
        # 迁到 worker/insight，不合并会让契约驱动的 `grape` CLI 丢掉整组命令）
        try:
            from src.api.openapi_merge import dump_contract
            dump_contract(app)
        except Exception as e:
            log.warning(f"OpenAPI: contract dump failed: {e}")

        # lease 模式：默认开启（方式 A），BOT_LEASE_DISABLED=true 退回旧模式
        from src.kernel.config import BOT_LEASE_DISABLED
        lease_store = None
        if not BOT_LEASE_DISABLED:
            from src.domains.runtime.im.bot_lease import BotLeaseStore, generate_instance_id
            instance_id = generate_instance_id()
            lease_store = BotLeaseStore(instance_id=instance_id)
            log.info(f"IM WS: lease enabled, instance_id={instance_id}")

        # weekly-11 起 legacy BotManager 已退役：引擎模式 on 时由 mgmt 侧 engine_wiring
        # 统一启动新引擎，本进程不再装配 legacy WS（否则每次启动刷 BotManager ImportError 告警）。
        try:
            from src.domains.runtime.im import engine_wiring as _ew
            _engine_on = (_ew.engine_mode() == "on")
        except Exception:
            _engine_on = False
        if _engine_on:
            log.info("IM WS: legacy BotManager retired (engine mode=on), skip")
            bot_manager = None
            raise _LegacyIMRetired()
        bot_manager = BotManager(max_concurrent_per_bot=10, lease_store=lease_store)
        await bot_manager.start()

        async def _on_message(ctx, parsed):
            if ctx.bot_type == "vod_bot":
                await process_vod_async(ctx, bot_manager.api, parsed)
            else:
                await process_chat_async(ctx, bot_manager.api, parsed)

        bot_manager.set_message_handler(_on_message)
        await bot_manager.load_from_db()

        hc = bot_manager.health_check()
        log.info(f"IM WS: {hc['total_bots']} bot(s), {hc['connected']} connected")
    except _LegacyIMRetired:
        bot_manager = None
    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")
        bot_manager = None

    h["bot_manager"] = bot_manager

    # lease sweep 循环（问题 J：变量名 _lease_sweep_task 区分）
    _lease_sweep_task = None
    if bot_manager and bot_manager._lease_store:
        _lease_sweep_task = asyncio.create_task(bot_manager.lease_sweep_loop())
        log.info("Lease sweep loop started")
    h["lease_sweep_task"] = _lease_sweep_task

    # 知识库 OBS 同步：后台定时迁移老数据到 OBS（每小时一次）
    try:
        async def _knowledge_sync_wrapper():
            from src.domains.runtime.storage import get_storage
            if not get_storage():
                return  # OBS 未启用，不需要同步
            from src.api.knowledge import knowledge_sync_loop
            await knowledge_sync_loop()
        h["knowledge_sync_task"] = asyncio.create_task(_knowledge_sync_wrapper())
        log.info("Knowledge sync loop started")
    except Exception as e:
        log.warning(f"Knowledge sync loop start failed: {e}")

    # Issue #103：知识写入队列消费者——后台兜底处理 pending 任务
    try:
        from src.api.knowledge import knowledge_write_worker
        h["knowledge_write_task"] = asyncio.create_task(knowledge_write_worker())
        log.info("Knowledge write worker started")
    except Exception as e:
        log.warning(f"Knowledge write worker start failed: {e}")

    # 官方技能 OBS 自动同步：定时扫描本地技能变更，上传 OBS（每 30 分钟）
    try:
        from src.domains.runtime.storage.skill_sync import start_skill_sync_scheduler
        start_skill_sync_scheduler()
    except Exception as e:
        log.warning(f"Skill sync scheduler start failed: {e}")

    # 需求巡查（patrol）：周期巡检活跃需求并补偿停滞任务（S53 复审收口：
    # 四进程拆分后由 Mgmt 进程持有；必须是 shutdown 可 cancel 的后台任务，否则
    # 进程退出后循环残留继续写库 → 用例成片假失败）
    _patrol_task = None
    try:
        from src.api.patrol import patrol_loop
        _patrol_task = asyncio.create_task(patrol_loop())
        log.info("Patrol loop started (Mgmt)")
    except Exception as e:
        log.warning(f"Patrol loop start failed: {e}")
    h["patrol_task"] = _patrol_task

    # 反馈→Issue reconcile：Agent 自动反馈定时转官方技能仓库/所属仓库 Issue（weekly-08）
    try:
        from src.api.feedback_issue_converter import start_feedback_reconcile_scheduler
        start_feedback_reconcile_scheduler()
    except Exception as e:
        log.warning(f"Feedback reconcile scheduler start failed: {e}")

    # 临时上传文件定时清理：每小时清理超过 24 小时的孤儿文件（Issue-54）
    try:
        from src.domains.runtime.storage.upload_cleanup import start_upload_cleanup_scheduler
        start_upload_cleanup_scheduler()
    except Exception as e:
        log.warning(f"Upload cleanup scheduler start failed: {e}")

    # D7: 冷静期定时任务：每天执行一次，匿名化超 30 天的冷静期账号（需求 22）
    try:
        from src.domains.mgmt.admin.cooldown import start_cooldown_scheduler
        start_cooldown_scheduler()
    except Exception as e:
        log.warning(f"Cooldown scheduler start failed: {e}")

    # Issue #190：知识库 obs_key/file_path 归一化迁移（异步，不阻塞启动）
    try:
        async def _knowledge_migrate_wrapper():
            from src.api.db_setup import _migrate_knowledge
            await asyncio.to_thread(_migrate_knowledge)
        _migrate_task = asyncio.create_task(_knowledge_migrate_wrapper())

        def _on_migrate_done(task: asyncio.Task):
            if task.exception():
                log.error(f"knowledge_migrate task failed: {task.exception()}", exc_info=task.exception())
        _migrate_task.add_done_callback(_on_migrate_done)
        app.state.knowledge_migrate_task = _migrate_task
        log.info("knowledge_migrate started (async)")
    except Exception as e:
        log.warning(f"knowledge_migrate start failed: {e}")

    
# D32: user_accounts → grape_accounts 数据迁移（Issue #343: yield 前同步等待完成）
    try:
        from scripts.migrate_user_accounts_to_grape import run_migration

        def _run_d32_migration() -> None:
            # 迁移脚本的 print() 一律走结构化日志，避免 Loki 里出现 level=unknown 的裸文本
            with contextlib.redirect_stdout(_StructuredScriptOutput()):
                run_migration(False)

        await asyncio.to_thread(_run_d32_migration)
        log.info("D32 migration completed (sync, pre-yield)")
    except Exception as e:
        log.error(f"D32 migration failed: {e}", exc_info=e)
        app.state.d32_migration_failed = True

    # 用户活跃状态追踪：每 30 秒批量更新 last_active_at
    try:
        async def _active_user_tracker():
            from src.api.deps import get_pending_active
            from src.domains.mgmt.user import db as user_db
            _purge_i = 0
            while True:
                await asyncio.sleep(30)
                try:
                    uids = get_pending_active()
                    if uids:
                        await asyncio.to_thread(user_db.batch_update_last_active, list(uids))
                except Exception as e:
                    log.warning(f"Active user tracker flush failed: {e}")
                # Issue-45 M2：会话级临时 token TTL 兜底清理（每 5 分钟）
                try:
                    _purge_i += 1
                    if _purge_i >= 10:
                        _purge_i = 0
                        from src.kernel.auth_sdk.auth_token import purge_expired_session_tokens
                        await asyncio.to_thread(purge_expired_session_tokens)
                        from src.api.event_store import purge_stale_event_guards
                        await asyncio.to_thread(purge_stale_event_guards)
                except Exception as e:
                    log.warning(f"Session token purge failed: {e}")

        h["active_tracker_task"] = asyncio.create_task(_active_user_tracker())
        log.info("Active user tracker started (30s flush)")
    except Exception as e:
        log.warning(f"Active user tracker start failed: {e}")

    # GSB 自检（仅 remote 模式）：周期性跨域读一次，验证总线可用，
    # 同时给 Grafana「模块间流动」面板提供稳定信号（否则业务侧尚未迁移时该面板恒空）。
    try:
        from src.kernel.gsb.selftest import gsb_selftest_loop, selftest_enabled

        if selftest_enabled():
            h["gsb_selftest_task"] = asyncio.create_task(gsb_selftest_loop())
    except Exception as e:
        log.warning(f"GSB selftest start failed: {e}")

    return h


async def stop_mgmt_tasks(app, h: dict) -> None:
    """Mgmt 进程关闭：lease → IM WS → 知识写入 → 活跃追踪 → 迁移等待 → 定时线程。"""
    _gsb_selftest = h.get("gsb_selftest_task")
    if _gsb_selftest:
        _gsb_selftest.cancel()
        try:
            await _gsb_selftest
        except (asyncio.CancelledError, Exception):
            pass
    # 问题 H：先 cancel lease_sweep_task（不再抢新锁）→ 再 stop bot_manager（停 bot + 释放 lease）
    _lease_sweep_task = h.get("lease_sweep_task")
    if _lease_sweep_task:
        _lease_sweep_task.cancel()
        try:
            await asyncio.wait_for(_lease_sweep_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    bot_manager = h.get("bot_manager")
    if bot_manager:
        await bot_manager.stop()

    _patrol_task = h.get("patrol_task")
    if _patrol_task:
        _patrol_task.cancel()
        try:
            await asyncio.wait_for(_patrol_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    _active_tracker_task = h.get("active_tracker_task")
    if _active_tracker_task:
        _active_tracker_task.cancel()
        try:
            await asyncio.wait_for(_active_tracker_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    _knowledge_write_task = h.get("knowledge_write_task")
    if _knowledge_write_task:
        _knowledge_write_task.cancel()
        try:
            await asyncio.wait_for(_knowledge_write_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # knowledge_migrate：shutdown 时尝试等待完成（不 cancel，迁移中断不如跑完）
    _migrate_task = getattr(app.state, "knowledge_migrate_task", None)
    if _migrate_task and not _migrate_task.done():
        try:
            await asyncio.wait_for(_migrate_task, timeout=10)
        except asyncio.TimeoutError:
            log.warning("knowledge_migrate shutdown wait timeout, task continues in background")
        except Exception:
            pass

    # 官方技能 OBS 同步：优雅关闭定时任务
    try:
        from src.domains.runtime.storage.skill_sync import stop_skill_sync_scheduler
        await stop_skill_sync_scheduler()
    except Exception:
        pass
    # 反馈→Issue reconcile：优雅关闭定时任务（weekly-08）
    try:
        from src.api.feedback_issue_converter import stop_feedback_reconcile_scheduler
        await stop_feedback_reconcile_scheduler()
    except Exception:
        pass
    # R31：场景同步随 skill worker 一同优雅关闭（合并进 stop_skill_sync_scheduler）


# ===========================================================================
# Event 进程（grape_event_server :38081）
# ===========================================================================

class _LegacyIMRetired(Exception):
    """legacy IM 装配已退役（引擎模式 on），非错误。"""


async def start_event_tasks(app, include_sweep_events: bool = True) -> dict:
    """Event 进程定时任务（设计 §5.2）。返回关闭句柄字典。

    单进程并集模式（main.py）传 include_sweep_events=False：僵尸事件回收由
    单进程的合并 sweep_loop 负责。
    """
    h = {}
    if include_sweep_events:
        try:
            from src.domains.runtime.engine.cleanup import sweep_events_loop
            h["sweep_events_task"] = asyncio.create_task(sweep_events_loop())
            log.info("Event sweeper loop started (15min)")
        except Exception as e:
            log.warning(f"Event sweeper loop start failed: {e}")

    # Issue #345: 全局 bot token 健康探活——每 5 分钟探活，集群下分布式锁保证单实例执行
    try:
        from src.domains.runtime.engine.bot_token_health import start_bot_token_health
        start_bot_token_health()
    except Exception as e:
        log.warning(f"Bot token health check start failed: {e}")
    return h


async def stop_event_tasks(app, h: dict) -> None:
    """Event 进程关闭。"""
    _t = h.get("sweep_events_task")
    if _t:
        _t.cancel()
        try:
            await asyncio.wait_for(_t, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


# ===========================================================================
# Worker 进程（grape_worker_server :38082）
# ===========================================================================

async def start_worker_tasks(app, include_sweep_tasks: bool = True,
                             include_table_maintenance: bool = True) -> dict:
    """Worker 进程定时任务（设计 §5.3）。返回关闭句柄字典。

    单进程并集模式（main.py）传 include_sweep_tasks=False / include_table_maintenance=False：
    悬空任务回收与表维护由单进程的合并 sweep_loop / 组合表维护负责。
    """
    h = {}

    # 本机 direct 引擎一等认领器（local-claimer-first-class-broadcast-design.md）：
    # 让本机引擎与远端 worker 同级参与广播认领（同一条 claim_by_tag 检查链）。
    # GRAPE_LOCAL_CLAIMER=0 可关闭（回退为纯远端认领）。
    try:
        from src.domains.runtime.engine.local_claimer import start_local_claimer
        start_local_claimer()
    except Exception as e:
        log.warning(f"Local claimer start failed: {e}")

    # Autopilot 定时调度器：每分钟扫描到期任务，创建 Issue 触发流水线
    # （start_scheduler 幂等，统一在本组启动）
    try:
        from src.domains.runtime.engine.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        log.warning(f"Autopilot scheduler start failed: {e}")

    # Issue #85：引擎健康度定时刷新——每 5 分钟对所有引擎 force_refresh
    try:
        from src.domains.runtime.engine.health_scorer import start_health_refresh_scheduler
        start_health_refresh_scheduler()
    except Exception as e:
        log.warning(f"Health refresh scheduler start failed: {e}")

    # 本机直连引擎周期重探（方案 C，local-direct-health-probe-design.md）：每 10 分钟探测
    try:
        async def _local_probe_loop():
            import time as _t
            from src.domains.runtime.engine.registry import discover_runtimes
            while True:
                def _run():
                    from src.domains.runtime.engine.local_probe import probe_local_engines
                    from src.domains.runtime.engine.agent import _scan_local_skills_with_detail
                    from src.domains.mgmt.user import db as user_db
                    import socket as _sock
                    _hostname = engine_host()
                    probed = [r for r in probe_local_engines(discover_runtimes()) if r.get("installed")]
                    if probed:
                        for r in probed:
                            _name = (r.get("name") or "").strip().lower()
                            r["local_skills_detail"] = _scan_local_skills_with_detail(_name)
                            r["local_skills"] = [s["name"] for s in r["local_skills_detail"]]
                        user_db.save_agent_runtimes(probed, host_name=_hostname)
                    return len(probed)
                try:
                    n = await asyncio.to_thread(_run)
                    log.info(f"Local probe loop: refreshed {n} local engine(s)")
                except Exception as e:
                    log.warning(f"Local probe failed: {e}")
                await asyncio.sleep(600)

        h["probe_task"] = asyncio.create_task(_local_probe_loop())
        log.info("Local probe loop started (10min)")
    except Exception as e:
        log.warning(f"Local probe loop start failed: {e}")

    # 远端 runtime 僵尸清理（每 60 秒）：AI DevSpace 被删后 runtime 行残留 status=online
    try:
        def _reap_stale_runtimes_sync():
            from src.domains.runtime.engine.service import reap_stale_runtimes
            return reap_stale_runtimes()

        async def _reap_stale_loop():
            while True:
                await asyncio.sleep(60)
                try:
                    result = await asyncio.to_thread(_reap_stale_runtimes_sync)
                    if result.get("reaped"):
                        log.info(f"Reap loop: {result}")
                except Exception as e:
                    log.warning(f"Reap stale runtimes failed: {e}")

        h["reap_task"] = asyncio.create_task(_reap_stale_loop())
        log.info("Reap stale runtimes loop started (60s)")
    except Exception as e:
        log.warning(f"Reap stale runtimes loop start failed: {e}")

    # 悬空 agent_tasks 回收（15min）——四进程模式独立 loop
    if include_sweep_tasks:
        try:
            from src.domains.runtime.engine.cleanup import sweep_tasks_loop
            h["sweep_tasks_task"] = asyncio.create_task(sweep_tasks_loop())
            log.info("Task sweeper loop started (15min)")
        except Exception as e:
            log.warning(f"Task sweeper loop start failed: {e}")

    # 表维护 agent_tasks 部分（6h，锁 table_maintenance_tasks）——四进程模式独立 loop
    if include_table_maintenance:
        try:
            from src.domains.runtime.engine.table_maintenance import table_maintenance_tasks_loop
            h["table_maintenance_task"] = asyncio.create_task(table_maintenance_tasks_loop())
            log.info("Table maintenance (agent_tasks) loop started (6h)")
        except Exception as e:
            log.warning(f"Table maintenance (agent_tasks) loop start failed: {e}")

    return h


async def stop_worker_tasks(app, h: dict) -> None:
    """Worker 进程关闭。"""
    for _k in ("probe_task", "reap_task", "sweep_tasks_task", "table_maintenance_task"):
        _t = h.get(_k)
        if _t:
            _t.cancel()
            try:
                await asyncio.wait_for(_t, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
    # Issue #85：健康度定时刷新线程优雅关闭
    try:
        from src.domains.runtime.engine.health_scorer import stop_health_refresh_scheduler
        stop_health_refresh_scheduler()
    except Exception:
        pass
    # Issue #150: 停止 Autopilot 调度器
    try:
        from src.domains.runtime.engine.scheduler import stop_scheduler
        await stop_scheduler()
    except Exception:
        pass
    # 终止运行中的 agent 子进程（opencode），使 executor 线程尽快结束，
    # 避免 uvicorn 优雅停机等待超时被 systemd SIGKILL（grape-main timeout 重启根因，2026-08-18）
    try:
        from src.domains.runtime.engine.executor import terminate_active_agents
        terminate_active_agents(grace_seconds=5)
    except Exception as e:
        log.warning(f"terminate_active_agents failed: {e}")


# ===========================================================================
# Insight 进程（grape_insight_server :38083）
# ===========================================================================

async def start_insight_tasks(app, include_table_maintenance: bool = True) -> dict:
    """Insight 进程定时任务（设计 §5.4）。返回关闭句柄字典。

    单进程并集模式（main.py）传 include_table_maintenance=False：task_messages 清理
    由单进程的组合表维护负责。
    """
    h = {}

    # 主控机指标上报：每 15 秒采集本机 CPU/内存/磁盘/负载写入 DB
    try:
        async def _server_metrics_loop():
            import socket as _sock, hashlib as _hl
            from datetime import datetime, timezone
            _hostname = engine_host() or "unknown-server"
            _local_ip = "127.0.0.1"
            try:
                _s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                _s.connect(("8.8.8.8", 80))
                _local_ip = _s.getsockname()[0]
                _s.close()
            except Exception:
                pass
            try:
                # P3-5f-3：runtime_credentials 归 runtime 域，走任意拓扑原语
                # （自有 DSN 优先、单库回落进程库）。**同步直连、不入意图队列**——
                # 这是启动注册 + 15s 心跳，落成意图会每天写几千条日志；循环本身就是重试机制。
                from src.domains.runtime import public as _rt_public_dev
                _token_hash = _hl.sha256(_hostname.encode()).hexdigest()
                _dev = _rt_public_dev.register_device_credential_any(
                    _hostname, _token_hash, _local_ip)
                if _dev.get("action") == "inserted":
                    log.info(f"Server metrics: registered {_hostname}")
            except Exception as e:
                log.warning(f"Server metrics: register failed: {e}")
            while True:
                try:
                    _cpu = _mem = _disk = _tmp = 0.0
                    _load = ""
                    try:
                        import psutil as _ps
                        _cpu = _ps.cpu_percent(interval=None)
                        try:
                            with open("/proc/meminfo") as _mf:
                                _mi = {}
                                for _line in _mf:
                                    _p = _line.split(":")
                                    if len(_p) == 2:
                                        _mi[_p[0]] = int(_p[1].strip().split()[0]) * 1024
                            _total = _mi["MemTotal"]
                            _used = _total - _mi.get("MemFree", 0) - _mi.get("Buffers", 0) - _mi.get("Cached", 0)
                            _mem = round(_used / _total * 100, 1) if _total > 0 else _ps.virtual_memory().percent
                        except Exception:
                            _mem = _ps.virtual_memory().percent
                        _disk = _ps.disk_usage("/").percent
                        try:
                            _tmp = _ps.disk_usage("/tmp").percent
                        except Exception:
                            _tmp = 0.0
                        _load = ",".join(f"{x:.2f}" for x in _ps.getloadavg())
                        _bw_mbps = 0.0
                        _bw_pct = 0.0
                        try:
                            import time as _bt
                            _rx = _tx = 0
                            with open("/proc/net/dev") as _bf:
                                for _bl in _bf:
                                    if ":" not in _bl:
                                        continue
                                    _bn, _br = _bl.split(":", 1)
                                    _bn = _bn.strip()
                                    if _bn == "lo":
                                        continue
                                    _bf2 = _br.split()
                                    if len(_bf2) >= 9:
                                        _rx += int(_bf2[0])
                                        _tx += int(_bf2[8])
                            _bw_now = _bt.time()
                            _bw_prev = getattr(_server_metrics_loop, "_bw_prev", None)
                            if _bw_prev:
                                _bpts, _bpbytes = _bw_prev
                                _bdt = _bw_now - _bpts
                                _bdb = (_rx + _tx) - _bpbytes
                                if _bdt > 0 and _bdb >= 0:
                                    _bw_mbps = round(_bdb * 8 / _bdt / 1e6, 1)
                                    _bw_thr = float(os.environ.get("RUNTIME_BANDWIDTH_THRESHOLD_MBPS", "10"))
                                    if _bw_thr > 0:
                                        _bw_pct = round(min(_bw_mbps / _bw_thr * 100, 100.0), 1)
                            _server_metrics_loop._bw_prev = (_bw_now, _rx + _tx)
                        except Exception:
                            pass
                    except Exception:
                        pass
                    _now = datetime.now(timezone.utc).isoformat()
                    from src.domains.runtime import public as _rt_public_met
                    # 同步直连：15s 心跳落成意图会淹没日志，循环即重试机制
                    _rt_public_met.touch_device_credential_any(
                        _hostname, _now, _local_ip, _cpu, _mem, _disk, _tmp,
                        _bw_pct, _bw_mbps, _load)
                except Exception as e:
                    log.warning(f"Server metrics: upload failed: {e}")
                await asyncio.sleep(15)
        h["server_metrics_task"] = asyncio.create_task(_server_metrics_loop())
        log.info("Server metrics loop started (15s)")
    except Exception as e:
        log.warning(f"Server metrics loop start failed: {e}")

    # 可观测性告警中心：种子规则 + 每 30s 求值（设计文档 §9.2），SLO 独立周期（默认 300s）
    try:
        from src.domains.insight.observability import alerts as _obs_alerts
        from src.domains.insight.observability import slo as _obs_slo
        from src.domains.insight.observability import loops as _obs_loops

        _obs_seeded = _obs_alerts.seed_rules()
        _obs_slo_seeded = _obs_slo.seed_defaults()      # P2-1：三条默认 SLO（幂等）

        async def _obs_alert_loop():
            _tick = 0
            _slo_next = 0.0
            _loop = asyncio.get_running_loop()
            while True:
                await asyncio.sleep(30)
                _tick += 1
                try:
                    _r = await asyncio.to_thread(_obs_alerts.evaluate_rules)
                    if _r.get("fired") or _r.get("resolved"):
                        log.info(f"Observability alerts: "
                                 f"{ {k: v for k, v in _r.items() if k != 'new_events'} }")
                    _new = _r.get("new_events") or []
                    if _new:
                        _n = await _obs_alerts.notify_fired_events(_new)
                        log.info(f"Observability alert notifications: {_n}")
                    _a = await _obs_alerts.flush_aggregates()
                    if _a.get("summaries"):
                        log.info(f"Observability alert aggregates: {_a}")
                    if _tick % 120 == 0:
                        _n2 = await asyncio.to_thread(_obs_alerts.cleanup_events)
                        if _n2:
                            log.info(f"Observability alert events cleaned: {_n2} rows")
                        _obs_loops.beat("obs_alert_retention")
                    if _loop.time() >= _slo_next:
                        _slo_next = _loop.time() + _obs_slo.eval_interval_sec()
                        try:
                            _s = await asyncio.to_thread(_obs_slo.evaluate_all)
                            if _s.get("firing") or _s.get("resolved"):
                                log.info(f"Observability SLO: {_s}")
                            if _s.get("cleaned"):
                                log.info(f"Observability SLO snapshots cleaned: {_s['cleaned']} rows")
                            _obs_loops.beat("obs_slo")
                        except Exception as e:
                            log.warning(f"Observability SLO loop failed: {e}")
                    _obs_loops.beat("obs_alert")
                except Exception as e:
                    _obs_loops.fail("obs_alert", e)
                    log.warning(f"Observability alert evaluation failed: {e}")

        h["obs_alert_loop_task"] = asyncio.create_task(_obs_alert_loop())
        log.info(f"Observability alert evaluator started (30s, seeded={_obs_seeded});"
                 f" SLO eval interval {_obs_slo.eval_interval_sec()}s (decoupled, Issue #437)")
    except Exception as e:
        log.warning(f"Observability alert evaluator start failed: {e}")

    # 业务指标聚合（P1-2）：60s 追赶式补齐分钟/小时桶；每小时顺带清一次留存
    try:
        from src.domains.insight.observability import metric_rollup as _obs_mrollup
        from src.domains.insight.observability import llm_usage as _obs_llm_usage
        from src.domains.insight.observability import loops as _obs_loops

        async def _obs_rollup_loop():
            _tick = 0
            while True:
                await asyncio.sleep(60)
                _tick += 1
                try:
                    _s = await asyncio.to_thread(_obs_mrollup.catch_up)
                    if _s.get("minute_done") or _s.get("hour_done"):
                        log.info(f"Observability metric rollup: {_s}")
                    if _tick % 60 == 0:
                        await asyncio.to_thread(_obs_mrollup.cleanup)
                    if _tick % 1440 == 0:
                        _ar = await asyncio.to_thread(_obs_llm_usage.archive_old_usage)
                        if _ar.get("archived"):
                            log.info(f"Observability usage archive: {_ar}")
                        _obs_loops.beat("obs_usage_archive")
                    _obs_loops.beat("obs_rollup")
                except Exception as e:
                    _obs_loops.fail("obs_rollup", e)
                    log.warning(f"Observability metric rollup failed: {e}")

        h["obs_rollup_task"] = asyncio.create_task(_obs_rollup_loop())
        log.info("Observability metric rollup started (60s, minute 7d / hour 30d)")
    except Exception as e:
        log.warning(f"Observability metric rollup start failed: {e}")

    # 设备资源采样：每 60s 留一份历史（资源曲线用），顺带清理过期采样
    try:
        from src.domains.insight.observability import rollup as _obs_rollup
        from src.domains.insight.observability import logs as _obs_logs_mod
        from src.domains.insight.observability import loops as _obs_loops

        async def _obs_sample_loop():
            while True:
                await asyncio.sleep(60)
                try:
                    r = await asyncio.to_thread(_obs_rollup.snapshot_runtimes)
                    if r.get("inserted"):
                        log.debug(f"Observability runtime samples: {r}")
                    _nc = await asyncio.to_thread(_obs_logs_mod.maybe_cleanup)
                    if _nc:
                        log.info(f"Observability logs cleaned: {_nc} rows")
                        _obs_loops.beat("obs_log_cleanup")
                    _obs_loops.beat("obs_sample")
                except Exception as e:
                    _obs_loops.fail("obs_sample", e)
                    log.warning(f"Observability runtime sampling failed: {e}")

        h["obs_sample_task"] = asyncio.create_task(_obs_sample_loop())
        log.info("Observability runtime sampler started (60s)")
    except Exception as e:
        log.warning(f"Observability runtime sampler start failed: {e}")

    # 可观测性日志落库：主控 WARNING/ERROR → obs_log（有界队列 + 后台批量，env OBS_LOG_DB=0 关闭）
    try:
        from src.domains.insight.observability import logs as _obs_logs_install
        _st = _obs_logs_install.install_master_logging()
        log.info(f"Observability log sink: {_st}")
    except Exception as e:
        log.warning(f"Observability log sink start failed: {e}")

    # 追踪（§6.4）：给历史任务补 trace id（确定性生成，一次即可）
    try:
        from src.domains.insight.observability import trace as _obs_trace
        _nstamped = await asyncio.to_thread(_obs_trace.stamp_missing)
        log.info(f"Observability trace stamp: {_nstamped} legacy task(s) backfilled")
    except Exception as e:
        log.warning(f"Observability trace stamp failed: {e}")

    # 表维护 task_messages 部分（6h，锁 table_maintenance_messages）——四进程模式独立 loop
    if include_table_maintenance:
        try:
            from src.domains.runtime.engine.table_maintenance import table_maintenance_messages_loop
            h["table_maintenance_task"] = asyncio.create_task(table_maintenance_messages_loop())
            log.info("Table maintenance (task_messages) loop started (6h)")
        except Exception as e:
            log.warning(f"Table maintenance (task_messages) loop start failed: {e}")

    return h


async def stop_insight_tasks(app, h: dict) -> None:
    """Insight 进程关闭。

    可观测性后台循环（告警求值 / 指标聚合 / 设备采样 / 主机指标 / 表维护）**必须收掉**：
    任何在本进程里启动过 app 的场景（含 pytest 里的 TestClient）都会把这些循环留在后台，
    继续对同一个库读写 → 后续用例成片假失败（真实踩到）。
    """
    for _k in ("obs_alert_loop_task", "obs_rollup_task", "obs_sample_task",
               "server_metrics_task", "table_maintenance_task"):
        _t = h.get(_k)
        if _t:
            _t.cancel()
            try:
                await asyncio.wait_for(_t, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass