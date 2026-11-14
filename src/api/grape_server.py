"""Mgmt 进程（grape_server :38080）——用户面 API、身份认证、配置管理、IM WS、前端托管。

Issue #699 Phase 4 四进程拆分：本进程把 main.py 的「用户面」职责独立出来，
只注册 Mgmt 域路由 + 跨进程路由的用户面端点（issues/chat/events/runtime 只读），
只启动 Mgmt 进程定时任务（设计 §5.1 / §5.5 / §10）。
"""

import sys, os

from src.kernel.config import log

# Issue #455：主服务要求 Python >= 3.10。防御直接 `uvicorn src.api.grape_server:app` 的启动方式。
from src.api.version_check import ensure_python_version

ensure_python_version()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api import server_common, server_tasks

bot_manager = None

def _startup_security_check():
    """启动安全门禁（fail-closed），与 legacy main.py 同一实现。

    JWT 密钥校验无条件执行（生产必须强密钥；开发/测试允许进程内随机兜底）；
    其余检查（密钥轮换池、邮件、凭据加密、cookie、redirect_uri）保留生产门禁。
    """
    from src.kernel.config import log
    from src.domains.mgmt.auth.security_config import is_production, get_jwt_kid_current, get_jwt_secret_keys, get_credential_keys

    errors = []

    # 1. JWT 密钥校验——无条件执行
    from src.kernel.auth_sdk.jwt_utils import validate_jwt_secret
    if not validate_jwt_secret():
        errors.append("JWT_SECRET 不安全：未配置强密钥（生产必须设置 JWT_SECRET 或密钥轮换池）")

    if not is_production():
        if errors:
            for e in errors:
                log.error(f"[SECURITY] {e}")
            raise SystemExit(f"安全门禁失败：{'; '.join(errors)}")
        return  # 开发环境其余检查跳过

    # 2. JWT 密钥轮换配置校验
    if not get_jwt_kid_current():
        errors.append("生产环境必须配置 JWT 密钥轮换（security.json jwt.kid_current）")
    if not get_jwt_secret_keys():
        errors.append("生产环境必须配置 JWT 密钥池（security.json jwt.secret_keys，格式 kid:secret,...）")

    # 3. 邮件服务配置校验（P2-1: 同时检查 email.json 配置）
    _has_email_env = os.environ.get("SMTP_HOST") or os.environ.get("RESEND_API_KEY")
    _has_email_json = False
    try:
        email_json_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "configs", "email.json")
        if os.path.exists(email_json_path):
            import json as _json
            with open(email_json_path, encoding="utf-8") as _f:
                _email_cfg = _json.load(_f)
            _has_email_json = bool(_email_cfg.get("smtp", {}).get("host") or _email_cfg.get("resend", {}).get("api_key"))
    except Exception as _e:
        log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    if not _has_email_env and not _has_email_json:
        errors.append("生产环境必须配置邮件服务（环境变量 SMTP_HOST/RESEND_API_KEY 或 configs/email.json）")

    # 4. 第三方凭据加密密钥校验（D10）
    _cred_keys = get_credential_keys()
    if not _cred_keys or _cred_keys.startswith("dev:"):
        errors.append("生产环境必须配置凭据加密密钥（security.json credential_keys，格式 kid:base64key）")

    # 5. COOKIE_SECURE 校验（BUG-NEW-1: 生产环境必须启用 Secure cookie）
    if os.environ.get("COOKIE_SECURE", "false").lower() not in ("true", "1", "yes"):
        errors.append("生产环境必须设置 COOKIE_SECURE=true（refresh_token cookie 需要 Secure 标志）")

    # 6. OAuth redirect_uri 配置校验（需求 11：每个 provider 必须配置 redirect_uri）
    from src.domains.mgmt.auth.providers import get_provider, list_providers
    missing_redirect = [p for p in list_providers() if not (get_provider(p) or {}).get("redirect_uri")]
    if missing_redirect:
        errors.append(f"生产环境必须配置 redirect_uri，缺少: {', '.join(missing_redirect)}")

    if errors:
        for e in errors:
            log.error(f"[SECURITY] {e}")
        raise SystemExit(f"生产环境安全门禁失败：{'; '.join(errors)}")

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
    global bot_manager
    from src.kernel.config import log

    # 生产环境启动安全门禁
    try:
        _startup_security_check()
    except SystemExit as e:
        raise

    # GSB：注册本进程 + 起 30s 心跳（失败只告警，不影响 mgmt 启动）
    _gsb = getattr(app.state, "gsb", None)
    if _gsb is not None:
        await _gsb.startup()

    # Mgmt 进程定时任务（设计 §5.1）
    _mgmt_h = await server_tasks.start_mgmt_tasks(app)
    bot_manager = _mgmt_h.get("bot_manager")
    log.info("Grape Mgmt Server lifespan started")

    # 2026-11-12 结构修复：此处原本残留了一次 yield + 关闭段（合并时把 weekly 的
    # "先 yield 再起 IM" 段落拼了进来），导致后面的启动段（IM 引擎、engine probe、
    # 巡检/知识库/技能同步/反馈/清理/冷静期调度、D32 迁移、OpenAPI 契约落盘）
    # 全部在**关机**时才执行。改为：启动段全部前置 → 单次 yield → 统一关闭。
    # 对外基址未配置告警（与 main.py 同款）
    try:
        from src.domains.mgmt.user.db import get_server_base_url_warning
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
        from src.domains.runtime.im import engine_wiring

        # psutil CPU 基准预热
        try:
            import psutil as _ps_init
            _ps_init.cpu_percent(interval=None)
        except Exception as _e:
            log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")

        # 启动引擎健康探测线程（每 10 分钟，observability——A 职责）
        try:
            from src.domains.insight.observability.engine_probe import start_probe
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

        # Issue #2261 B1：通知 outbox 消费者（跨进程投递——持有租约的安装由本实例投）。
        # engine 为 None 时也起：resolve_engine 多源解析兜底（消费者引擎为 None 跳过本轮）。
        # 接线收敛到 outbox.attach_outbox_consumer（QA #1314 S3：双入口不再各写 40 行）。
        _outbox_consumer = None
        try:
            from src.domains.runtime.im import outbox
            _outbox_consumer = outbox.attach_outbox_consumer(app, im_engine)
        except Exception as e:
            log.warning(f"IM outbox consumer start failed: {e}")
    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")

    # ===== A 归属定时任务（设计文档 §6 分配表）=====


    # 知识库 OBS 同步：后台定时迁移老数据到 OBS（每小时）
    _knowledge_sync_task = None
    try:
        async def _knowledge_sync_wrapper():
            from src.domains.runtime.storage import get_storage
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
        from src.domains.runtime.storage.skill_sync import start_skill_sync_scheduler
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
        from src.domains.runtime.storage.upload_cleanup import start_upload_cleanup_scheduler
        start_upload_cleanup_scheduler()
    except Exception as e:
        log.warning(f"Upload cleanup scheduler start failed: {e}")

    # D7: 冷静期定时任务（每天）
    try:
        from src.domains.mgmt.admin.cooldown import start_cooldown_scheduler
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
        from src.domains.insight.observability import logs as _obs_logs_install
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
        from src.domains.runtime.engine.task_lock import run_with_distributed_lock

        async def _table_maintenance_messages_worker():
            import asyncio as _aio
            await _aio.sleep(600)  # 10 min after startup (avoid migration race)
            while True:
                try:
                    async with run_with_distributed_lock("table_maintenance_messages", ttl=3600):
                        from src.domains.runtime.engine.table_maintenance import cleanup_old_task_messages
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
        from src.kernel.help_page_gen import generate as _generate_help_page
        _n_help = _generate_help_page()
        log.info(f"Help: help.html regenerated ({_n_help} sources)")
    except Exception as e:
        log.warning(f"Help: help.html generation failed (kept last generated file): {e}")

    try:
        # 2026-11-12 修：此处原用 custom_openapi(app)（仅本进程 265 条），会覆盖
        # server_tasks 里 build_contract() 写好的**四进程合并契约**（269 条，含
        # /api/tasks/*、/api/pipeline/handoff 等侧进程端点）——导致契约驱动的
        # `grape` CLI 丢掉 tasks 命令组 / a2a 之外的交办命令。
        # 统一走 build_contract：mgmt 身份下自动合并 event/worker/insight 的 spec。
        from src.api.openapi_merge import dump_contract
        dump_contract(app)
    except Exception as e:
        log.warning(f"OpenAPI: contract dump failed: {e}")

    yield

    # ===== 优雅关闭 =====
    log.info("A process (server) stopping...")
    # Issue #2261 B1：先停 outbox 消费者（cancel+await），再停引擎——
    # 反过来会在消费者 await 途中拿到已 stop 的引擎（竞态：发给死渠道）。
    _consumer = getattr(app.state, "outbox_consumer", None)
    if _consumer is not None:
        try:
            await _consumer.stop()
        except Exception as _e:
            log.warning(f"IM outbox consumer stop failed: {_e}")
        try:
            app.state.outbox_consumer = None
        except Exception as _e:
            log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    try:
        from src.domains.runtime.im.notify import set_running_app
        set_running_app(None)
    except Exception as _e:
        log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    if im_engine is not None:
        try:
            await im_engine.stop()
        except Exception as _e:
            log.warning(f"IM engine stop failed: {_e}")
        try:
            app.state.im_engine = None
        except Exception as _e:
            log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    if _knowledge_write_task:
        _knowledge_write_task.cancel()
        try:
            await asyncio.wait_for(_knowledge_write_task, timeout=5)
        except Exception as _e:
            log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    for _t in (_obs_alert_loop_task, _obs_rollup_task, _obs_sample_task):
        if _t:
            _t.cancel()
            try:
                await asyncio.wait_for(_t, timeout=5)
            except Exception as _e:
                log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    # knowledge_migrate：shutdown 时等待完成（不 cancel，迁移中断不如跑完）
    _migrate_task = getattr(app.state, "knowledge_migrate_task", None)
    if _migrate_task and not _migrate_task.done():
        try:
            await asyncio.wait_for(_migrate_task, timeout=10)
        except asyncio.TimeoutError:
            log.warning("knowledge_migrate shutdown wait timeout, task continues in background")
        except Exception as _e:
            log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    # 官方技能 OBS 同步 / 反馈 reconcile：优雅关闭
    try:
        from src.domains.runtime.storage.skill_sync import stop_skill_sync_scheduler
        await stop_skill_sync_scheduler()
    except Exception as _e:
        log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    try:
        from src.api.feedback_issue_converter import stop_feedback_reconcile_scheduler
        await stop_feedback_reconcile_scheduler()
    except Exception as _e:
        log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")
    # GSB 注销 + 定时任务收口（原先被误放在第一次 yield 之后，现归还 shutdown 段）
    if _gsb is not None:
        try:
            await _gsb.shutdown()
        except Exception as _gsd:
            log.warning(f"GSB shutdown failed: {_gsd}")
    try:
        await server_tasks.stop_mgmt_tasks(app, _mgmt_h)
    except Exception as _sm:
        log.warning(f"stop_mgmt_tasks failed: {_sm}")
    log.info("Grape Mgmt Server lifespan stopped")
    log.info("A process (server) stopped")


app = server_common.build_app(
    title="Grape Mgmt Server",
    version="2.2.2",
    lifespan=lifespan,
)

# 管理域用户面路由（设计 §5.1 清单）
from src.domains.mgmt.routes import (
    auth, grape_auth, repos, agents, settings, skills, scenes, templates,
    feishu_onboard, feishu_card, devstage, feedback, autopilot, orgs, community,
    patrol,
)

app.include_router(grape_auth.router)

app.include_router(auth.router)

app.include_router(repos.router)

app.include_router(agents.router)

app.include_router(settings.router)

app.include_router(skills.router)

app.include_router(scenes.router)

app.include_router(templates.router)

app.include_router(feishu_onboard.router)

app.include_router(feishu_card.router)

app.include_router(devstage.router)

app.include_router(feedback.router)

app.include_router(autopilot.router)

app.include_router(orgs.router)

app.include_router(community.router)

# 巡检（Issue: 4 进程拆分时漏挂，导致 /api/patrol/* 404 且契约丢失）
app.include_router(patrol.router)

# IM 渠道路由（/api/im/*）——4 进程拆分时同样漏挂：唯一挂载点曾留在单进程 main.py，
# 四进程部署下整张 IM 渠道页 404（Issue #2261 移植时补挂；引擎/outbox 状态只属 A 进程）。
from src.domains.mgmt.routes import im as im_router

app.include_router(im_router.router)

# Admin 管理后台（/api/admin/*）
from src.domains.mgmt.admin.routes import router as admin_router

app.include_router(admin_router)

# Issue-45 M3：Git 仓库统一接口（provider 白名单资源端点；prefix=/api/git-repo）
from src.api.git_repo_api import router as git_repo_router

app.include_router(git_repo_router)

# 运行域用户面路由（设计 §5.5 跨进程分配）
from src.domains.runtime.routes import events, chat, issues, pipeline, task_logs, runtime

app.include_router(events.router)        # 事件查看/操作（GET 查看 + 用户面操作）

app.include_router(issues.router)        # Issue 生命周期/需求中心（GET/PUT + 用户面操作）

app.include_router(chat.router)          # 聊天对话、SSE 流式发送

app.include_router(pipeline.router)      # 仓库流水线展示

app.include_router(task_logs.router)     # 任务日志查看 / SSE 流（TC-4.10）

# runtime 只读端点（runtime_view 语义：Mgmt 查看 install / machine-health / task-status）
server_common.include_selected(app, runtime.router, methods={"GET"})
# 接管会话（weekly-11）：任务接管是一等资源，路由在管理域，只有 mgmt 进程提供
from src.domains.mgmt.routes import takeover as takeover_api  # noqa: E402
app.include_router(takeover_api.router)

# 数据域用户面路由（设计 §5.1 清单；nginx 主路由到 Insight 进程，此处作直连/内部兜底）
from src.domains.insight.routes import dashboard, search, latency, health

app.include_router(dashboard.router)

app.include_router(search.router)

app.include_router(latency.router)

app.include_router(health.router)

# 知识库 CRUD
from src.domains.mgmt.routes import knowledge

app.include_router(knowledge.router)

# ===== Issue-45: OpenAPI 定制渲染 =====
from src.api.openapi import custom_openapi

app.openapi = lambda: custom_openapi(app)

server_common.init_process_db()

# mgmt 是管理态服务：只挂 InternalAuthMiddleware（保护 /internal/gsb/*），
# 不挂载业务型 /internal/* 契约——其它域服务不得通过 HTTP 反向调用 mgmt。
server_common.attach_internal_routes(app, "mgmt")

# GSB 服务注册表端点（/internal/gsb/*）
from src.kernel.gsb.router import init_gsb

init_gsb(app, "mgmt")

# 未匹配的 /internal/* 返回 JSON 404（否则会落到下面的 SPA catch-all 变成 200+HTML）
server_common.attach_internal_guard(app)

server_common.attach_health(app, service_name="server", readiness=True)

server_common.attach_frontend(app)

import sys, os, asyncio

# Issue #455：主服务要求 Python >= 3.10（与 main.py 一致的防御）
from src.api.version_check import ensure_python_version

from fastapi import FastAPI, Request

from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import JSONResponse

from fastapi.staticfiles import StaticFiles

from src.domains.mgmt.routes import (
    auth, grape_auth, repos, agents, settings, skills, scenes, templates, feishu_onboard, feishu_card, knowledge, devstage, feedback, autopilot, orgs, community, patrol,
)

from src.domains.insight.routes import (
    dashboard, search, health, latency, observability,
)

from src.domains.runtime.routes import (
    events, chat, pipeline, issues, task_logs,
)

from src.domains.mgmt.admin.routes import router as admin_router

from src.api.git_repo_api import router as git_repo_router

# runtime.py 的用户面端点（GET machine-health/tasks/install + PUT 标签设置）——A 加载
from src.domains.runtime.routes.runtime_view import view_router as runtime_view_router

# ===== 可观察性中间件 / 请求日志（与 main.py 同款，单一实现源）=====
from src.domains.insight.observability import metrics_middleware, start_metrics_server, metrics_enabled

from src.api.middleware import install_request_middleware

from src.api.db_setup import init_database

from src.kernel.config import _load_accounts

from src.api.exception_handlers import install_exception_handlers

from src.api.health_endpoints import install_health_endpoints

from src.api.spa import install_spa_routes

def _check_and_sync_templates() -> bool:
    """模板自愈——复用 main.py 同名实现（单一实现源）。"""
    from src.api.main import _check_and_sync_templates as _check
    return _check()

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

im_engine = None

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

metrics_middleware(app)

if metrics_enabled():
    start_metrics_server()

# OTel HTTP spans（2026-11-05 MR-1）：LGTM Traces 接入，A 进程入口
try:
    from src.domains.insight.observability.otel import instrument_fastapi
    instrument_fastapi(app, "grape-server-a")
except Exception as _e:
    log.debug(f"src/api/grape_server.py best-effort skipped: {_e}")

install_request_middleware(app)

init_database()

_load_accounts()

install_exception_handlers(app)

install_health_endpoints(app, service_name="server")

install_spa_routes(app)

if __name__ == "__main__":
    import uvicorn
    _port = int(os.environ.get("GRAPE_PORT", "38080"))
    uvicorn.run("src.api.grape_server:app", host="0.0.0.0", port=_port, workers=1)
