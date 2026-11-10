"""Grape 工作台服务 — 用户面向 API（A 进程，Issue #596 P1）。

三进程拆分（docs/weekly-09/grape-three-process-split-design.md §4.1.1）：
- A（本文件，:38080）：用户交互、配置管理、数据展示、IM 集成、可观测性面板；
- B（grape_event_server.py，:38081）：Webhook 接收、事件归一化、路由判定、任务入队；
- C（grape_worker_server.py，:38082）：任务认领、Agent 执行、心跳、结果回传。

A 进程职责：
1. 加载 25 个用户面路由 + admin + git-repo + runtime 用户面视图端点
   （runtime_view.py：GET machine-health/tasks 列表/install 下载 + PUT 标签设置）；
2. 启动 A 归属的定时任务（设计文档 §6 分配表：lease sweep / knowledge sync /
   skill sync / feedback reconcile / upload cleanup / cooldown / active user
   tracker / server metrics / obs 三循环 + master logging / knowledge migrate /
   D32 迁移 / table_maintenance 的 task_messages 部分）；
3. IM WebSocket BotManager（飞书长连接，§10.7：归 A）；
4. 启动安全门禁、模板自愈、help.html、OpenAPI 契约落盘；
5. SPA / 静态文件服务（与原 main.py 一致）。

单进程兼容模式：`src/api/main.py` 保留（加载全部路由 + 全部定时任务），
`uvicorn src.api.main:app` 行为不变。多进程模式用本入口。

启动：
    uvicorn src.api.grape_server:app --host 0.0.0.0 --port 38080 --workers 1
"""
import sys, os, asyncio

# Issue #455：主服务要求 Python >= 3.10（与 main.py 一致的防御）
from src.api.version_check import ensure_python_version
ensure_python_version()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from src.api.routes import (
    auth, grape_auth, repos, dashboard, agents, events, chat, settings,
    skills, search, pipeline, scenes, templates, issues, feishu_onboard,
    feishu_card, task_logs, knowledge, health, devstage, latency,
    feedback, autopilot, orgs, community, observability, patrol,
)
from src.admin.routes import router as admin_router
from src.api.git_repo_api import router as git_repo_router
# runtime.py 的用户面端点（GET machine-health/tasks/install + PUT 标签设置）——A 加载
from src.api.routes.runtime_view import view_router as runtime_view_router


im_engine = None


def _startup_security_check():
    """启动安全门禁（fail-closed）——复用 main.py 同名逻辑（单一实现源）。

    main.py 的 `_startup_security_check` 是模块级函数；A 进程与单进程
    共用同一校验，避免双份漂移。此处直接 import 复用。
    """
    from src.api.main import _startup_security_check as _check
    _check()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """A 进程 lifespan：仅启动 A 归属的定时任务（设计文档 §6 分配表）。

    与 main.py 的差异（拆分原则）：
    - 不启动 webhook/A2A 相关（B 职责——A 不加载 webhook 路由）；
    - 不启动 local_claimer / scheduler / health_refresh / local_probe /
      reap_stale / bot_token_health / sweep_tasks（C 或 B 职责）；
    - sweep_loop 拆分：A 不参与事件/任务回收（B/C 各自跑）；
    - table_maintenance 拆分：A 只清 task_messages，agent_tasks 部分归 C。
    """
    global im_engine
    from src.config import log

    # 生产环境启动安全门禁
    try:
        _startup_security_check()
    except SystemExit:
        raise

    # 对外基址未配置告警（与 main.py 同款）
    try:
        from src.user.db import get_server_base_url_warning
        _base_warn = get_server_base_url_warning()
        if _base_warn:
            log.warning(f"[CONFIG] {_base_warn}")
        else:
            log.info("[CONFIG] server_base_url configured OK")
    except Exception as _bw:
        log.warning(f"[CONFIG] server_base_url check failed: {_bw}")

    # ===== IM 引擎（§10.7：归 A——用户面聊天依赖 IM 消息处理）=====
    # A2（2026-11-10）：legacy BotManager（飞书 WS + bot lease sweep）已下线，
    # A 进程与单进程入口一致，改起新引擎（Supervisor + 渠道适配器）。
    im_engine = None
    try:
        from src.im import engine_wiring

        # psutil CPU 基准预热
        try:
            import psutil as _ps_init
            _ps_init.cpu_percent(interval=None)
        except Exception:
            pass

        # 启动引擎健康探测线程（每 10 分钟，observability——A 职责）
        try:
            from src.observability.engine_probe import start_probe
            start_probe()
        except Exception as e:
            log.warning(f"Engine probe start failed: {e}")

        # A2A 链路巡检器（Phase 1 只读，与 main.py 同款；A 职责——路由已含 patrol.router，
        # 2026-11-03 补：路由与巡检循环在 #596 三进程拆分时双双遗漏，多进程入口 404/空表）
        try:
            from src.api.patrol import patrol_loop
            asyncio.create_task(patrol_loop())
            log.info("Patrol loop started")
        except Exception as e:
            log.warning(f"Patrol loop start failed: {e}")

        _im_mode = engine_wiring.engine_mode()
        if _im_mode == "on":
            im_engine = await engine_wiring.start_im_engine()
            if im_engine is not None:
                app.state.im_engine = im_engine
                log.info("IM engine started (mode=on, instance=%s)",
                         getattr(im_engine, "instance_id", ""))
            else:
                log.warning("IM engine: start failed (legacy fallback retired)")
        else:
            log.info("IM engine mode=off: IM 总开关关闭，不启动任何 IM 链路")
    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")

    # ===== A 归属定时任务（设计文档 §6 分配表）=====


    # 知识库 OBS 同步：后台定时迁移老数据到 OBS（每小时）
    _knowledge_sync_task = None
    try:
        async def _knowledge_sync_wrapper():
            from src.storage import get_storage
            if not get_storage():
                return  # OBS 未启用，不需要同步
            from src.api.knowledge import knowledge_sync_loop
            await knowledge_sync_loop()
        _knowledge_sync_task = asyncio.create_task(_knowledge_sync_wrapper())
        log.info("Knowledge sync loop started")
    except Exception as e:
        log.warning(f"Knowledge sync loop start failed: {e}")

    # 知识写入队列消费者（Issue #103）
    _knowledge_write_task = None
    try:
        from src.api.knowledge import knowledge_write_worker
        _knowledge_write_task = asyncio.create_task(knowledge_write_worker())
        log.info("Knowledge write worker started")
    except Exception as e:
        log.warning(f"Knowledge write worker start failed: {e}")

    # 官方技能 OBS 自动同步（30min）
    try:
        from src.storage.skill_sync import start_skill_sync_scheduler
        start_skill_sync_scheduler()
    except Exception as e:
        log.warning(f"Skill sync scheduler start failed: {e}")

    # 反馈→Issue reconcile
    try:
        from src.api.feedback_issue_converter import start_feedback_reconcile_scheduler
        start_feedback_reconcile_scheduler()
    except Exception as e:
        log.warning(f"Feedback reconcile scheduler start failed: {e}")

    # 临时上传文件定时清理（每小时）
    try:
        from src.storage.upload_cleanup import start_upload_cleanup_scheduler
        start_upload_cleanup_scheduler()
    except Exception as e:
        log.warning(f"Upload cleanup scheduler start failed: {e}")

    # D7: 冷静期定时任务（每天）
    try:
        from src.admin.cooldown import start_cooldown_scheduler
        start_cooldown_scheduler()
    except Exception as e:
        log.warning(f"Cooldown scheduler start failed: {e}")

    # 知识库归一化迁移（一次性，异步）
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

    # Issue #995：存量 VCS 存在性回填（启动后异步执行，不阻塞，幂等可重跑）
    try:
        from src.api.main import _start_vcs_exists_backfill_task
        app.state.vcs_exists_backfill_task = _start_vcs_exists_backfill_task()
        log.info("VCS existence backfill started (async)")
    except Exception as e:
        log.warning(f"VCS existence backfill start failed: {e}")

    # D32: user_accounts → grape_accounts 数据迁移（yield 前同步等待，幂等）
    try:
        from scripts.migrate_user_accounts_to_grape import run_migration
        await asyncio.to_thread(run_migration, False)
        log.info("D32 migration completed (sync, pre-yield)")
    except Exception as e:
        log.error(f"D32 migration failed: {e}", exc_info=e)
        app.state.d32_migration_failed = True

    # 主控机指标上报：每 15 秒采集本机 CPU/内存/磁盘/负载写入 DB（psutil——A）
    _server_metrics_task = None
    try:
        from src.api.main import _start_server_metrics_task
        _server_metrics_task = _start_server_metrics_task()
    except Exception as e:
        log.warning(f"Server metrics loop start failed: {e}")

    # 可观测性告警中心：种子规则 + 每 30s 求值（§6：_obs_alert_loop 归 A）
    _obs_alert_loop_task = None
    try:
        from src.api.main import _start_obs_alert_task
        _obs_alert_loop_task = _start_obs_alert_task()
    except Exception as e:
        log.warning(f"Observability alert evaluator start failed: {e}")

    # 业务指标聚合（60s）——A
    _obs_rollup_task = None
    try:
        from src.api.main import _start_obs_rollup_task
        _obs_rollup_task = _start_obs_rollup_task()
    except Exception as e:
        log.warning(f"Observability metric rollup start failed: {e}")

    # 设备资源采样（60s）——A
    _obs_sample_task = None
    try:
        from src.api.main import _start_obs_sample_task
        _obs_sample_task = _start_obs_sample_task()
    except Exception as e:
        log.warning(f"Observability runtime sampler start failed: {e}")

    # 可观测性日志落库：主控 WARNING/ERROR → obs_log（OBS_LOG_DB=1 仅 A 落库）
    try:
        from src.observability import logs as _obs_logs_install
        _st = _obs_logs_install.install_master_logging()
        log.info(f"Observability log sink: {_st}")
    except Exception as e:
        log.warning(f"Observability log sink start failed: {e}")

    # 用户活跃状态追踪：每 30 秒批量更新 last_active_at——A
    _active_tracker_task = None
    try:
        from src.api.main import _start_active_user_tracker_task
        _active_tracker_task = _start_active_user_tracker_task()
    except Exception as e:
        log.warning(f"Active user tracker start failed: {e}")

    # table_maintenance（A 部分，§6.2 拆分）：只清 task_messages，
    # 锁 key 独立（与 C 侧 agent_tasks 清理的锁 key 互不影响，
    # A/C 可并行执行各自的清理）。
    try:
        from src.runtime.task_lock import run_with_distributed_lock

        async def _table_maintenance_messages_worker():
            import asyncio as _aio
            await _aio.sleep(600)  # 10 min after startup (avoid migration race)
            while True:
                try:
                    async with run_with_distributed_lock("table_maintenance_messages", ttl=3600):
                        from src.runtime.table_maintenance import cleanup_old_task_messages
                        cleanup_old_task_messages()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("table maintenance (messages) run failed")
                await asyncio.sleep(6 * 3600)  # every 6 hours

        asyncio.create_task(_table_maintenance_messages_worker(), name="table-maintenance-messages")
    except Exception as e:
        log.warning(f"Table maintenance (messages) scheduler start failed: {e}")

    # 模板自愈 + help.html + OpenAPI 契约落盘（A 职责：用户面产物）
    try:
        _templates_ok = _check_and_sync_templates()
        if _templates_ok:
            log.info("Templates: project-templates.json + examples/ verified OK")
        else:
            log.warning("Templates: auto-sync attempted but issues remain, check logs")
    except Exception as e:
        log.warning(f"Templates: startup check failed: {e}")

    try:
        from src.help_page_gen import generate as _generate_help_page
        _n_help = _generate_help_page()
        log.info(f"Help: help.html regenerated ({_n_help} sources)")
    except Exception as e:
        log.warning(f"Help: help.html generation failed (kept last generated file): {e}")

    try:
        from src.api.openapi import custom_openapi
        from src.help_page_gen import write_openapi_contract
        _schema = custom_openapi(app)
        _n_paths = write_openapi_contract(_schema)
        log.info(f"OpenAPI: contract dumped ({_n_paths} paths) -> /static/openapi.json")
    except Exception as e:
        log.warning(f"OpenAPI: contract dump failed: {e}")

    yield

    # ===== 优雅关闭 =====
    log.info("A process (server) stopping...")
    if im_engine is not None:
        try:
            await im_engine.stop()
        except Exception as _e:
            log.warning(f"IM engine stop failed: {_e}")
        try:
            app.state.im_engine = None
        except Exception:
            pass
    if _knowledge_write_task:
        _knowledge_write_task.cancel()
        try:
            await asyncio.wait_for(_knowledge_write_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    for _t in (_obs_alert_loop_task, _obs_rollup_task, _obs_sample_task):
        if _t:
            _t.cancel()
            try:
                await asyncio.wait_for(_t, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
    # knowledge_migrate：shutdown 时等待完成（不 cancel，迁移中断不如跑完）
    _migrate_task = getattr(app.state, "knowledge_migrate_task", None)
    if _migrate_task and not _migrate_task.done():
        try:
            await asyncio.wait_for(_migrate_task, timeout=10)
        except asyncio.TimeoutError:
            log.warning("knowledge_migrate shutdown wait timeout, task continues in background")
        except Exception:
            pass
    # 官方技能 OBS 同步 / 反馈 reconcile：优雅关闭
    try:
        from src.storage.skill_sync import stop_skill_sync_scheduler
        await stop_skill_sync_scheduler()
    except Exception:
        pass
    try:
        from src.api.feedback_issue_converter import stop_feedback_reconcile_scheduler
        await stop_feedback_reconcile_scheduler()
    except Exception:
        pass
    log.info("A process (server) stopped")


app = FastAPI(
    title="Agentic DevOps API",
    version="2.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== 用户面路由注册（25 个 + admin + git-repo + runtime 视图）=====
for r in [
    grape_auth.router, auth.router, repos.router, admin_router, git_repo_router,
    dashboard.router, agents.router, events.router, chat.router, settings.router,
    skills.router, search.router, pipeline.router, scenes.router, templates.router,
    issues.router, feishu_onboard.router, feishu_card.router, task_logs.router,
    knowledge.router, health.router, devstage.router, latency.router, feedback.router,
    autopilot.router, orgs.router, community.router, observability.router, patrol.router,
    runtime_view_router,
]:
    app.include_router(r)

# ===== 可观察性中间件 / 请求日志（与 main.py 同款，单一实现源）=====
from src.observability import metrics_middleware, start_metrics_server, metrics_enabled
metrics_middleware(app)
if metrics_enabled():
    start_metrics_server()

# OTel HTTP spans（2026-11-05 MR-1）：LGTM Traces 接入，A 进程入口
try:
    from src.observability.otel import instrument_fastapi
    instrument_fastapi(app, "grape-server-a")
except Exception:
    pass

from src.api.middleware import install_request_middleware
install_request_middleware(app)

from src.api.db_setup import init_database
init_database()

from src.config import _load_accounts
_load_accounts()

from src.api.exception_handlers import install_exception_handlers
install_exception_handlers(app)

from src.api.health_endpoints import install_health_endpoints
install_health_endpoints(app, service_name="server")

from src.api.spa import install_spa_routes
install_spa_routes(app)


def _check_and_sync_templates() -> bool:
    """模板自愈——复用 main.py 同名实现（单一实现源）。"""
    from src.api.main import _check_and_sync_templates as _check
    return _check()


if __name__ == "__main__":
    import uvicorn
    _port = int(os.environ.get("GRAPE_PORT", "38080"))
    uvicorn.run("src.api.grape_server:app", host="0.0.0.0", port=_port, workers=1)
