"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os, asyncio

# Issue #455：主服务要求 Python >= 3.10。防御直接 `uvicorn src.api.main:app` 的启动方式，
# 在业务模块导入链触发前显式提示并退出。
from src.api.version_check import ensure_python_version
ensure_python_version()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.api import server_common
from src.domains.mgmt.routes import auth, grape_auth, repos, agents, settings, skills, scenes, templates, feishu_onboard, feishu_card, devstage, feedback, autopilot, orgs, community
from src.domains.runtime.routes import events, chat, pipeline, webhook, tasks, runtime, task_logs, issues
from src.domains.insight.routes import dashboard, search, health, latency, observability
from src.api.routes import knowledge


bot_manager = None


def _startup_security_check():
    """启动安全门禁（fail-closed）。

    JWT 密钥校验无条件执行（生产必须强密钥；开发/测试允许进程内随机兜底）；
    其余检查（密钥轮换池、邮件、凭据加密、cookie、redirect_uri）保留生产门禁。
    """
    from src.kernel.config import log
    from src.domains.mgmt.auth.security_config import is_production, get_jwt_kid_current, get_jwt_secret_keys, get_credential_keys

    errors = []

    # 1. JWT 密钥校验——无条件执行（开发/测试随机兜底态放行，其余不安全一律拒绝）
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
    except Exception:
        pass
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
    global bot_manager
    from src.kernel.config import log

    # 生产环境启动安全门禁
    try:
        _startup_security_check()
    except SystemExit as e:
        raise

    # 四组进程任务启动器（设计 §5.1~5.4）。单进程并集模式：
    # - 僵尸事件回收/悬空任务回收/表维护由下方合并 sweep_loop + 组合表维护负责
    #   （与拆分 loop 同一事实源，行为等价）
    # - 其余任务全部收敛到 server_tasks 四组函数
    from src.api import server_tasks

    _mgmt_h = await server_tasks.start_mgmt_tasks(app)
    bot_manager = _mgmt_h.get("bot_manager")
    _event_h = await server_tasks.start_event_tasks(app, include_sweep_events=False)
    _worker_h = await server_tasks.start_worker_tasks(app, include_sweep_tasks=False, include_table_maintenance=False)
    _insight_h = await server_tasks.start_insight_tasks(app, include_table_maintenance=False)

    # sweeper：Issue done 后清理 workdir（见 multi-issue-isolation-design.md §3.4.2）——
    # 单进程合并版（僵尸事件 + 悬空任务 + workdir + 临时目录清理）
    _sweep_task = None
    try:
        from src.domains.runtime.engine.cleanup import sweep_loop
        _sweep_task = asyncio.create_task(sweep_loop())
        log.info("Sweeper loop started")
    except Exception as e:
        log.warning(f"Sweeper loop start failed: {e}")

    # P2-C (Issue #357): Large table maintenance — 单进程组合版
    # (agent_tasks 字段置空 + 90 天删除 + task_messages 清理)，分布式锁 key=table_maintenance
    try:
        from src.domains.runtime.engine.task_lock import run_with_distributed_lock

        async def _table_maintenance_worker():
            await asyncio.sleep(600)  # 10 min after startup (avoid migration race)
            while True:
                try:
                    async with run_with_distributed_lock("table_maintenance", ttl=3600):
                        from src.domains.runtime.engine.table_maintenance import (
                            cleanup_old_agent_tasks_fields,
                            cleanup_old_task_messages,
                            delete_old_agent_tasks,
                        )
                        cleanup_old_agent_tasks_fields()
                        delete_old_agent_tasks()
                        cleanup_old_task_messages()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("table maintenance run failed")
                await asyncio.sleep(6 * 3600)  # every 6 hours

        asyncio.create_task(_table_maintenance_worker(), name="table-maintenance")
    except Exception as e:
        log.warning(f"Table maintenance scheduler start failed: {e}")

    yield

    log.info("Process tasks: stopping...")
    await server_tasks.stop_insight_tasks(app, _insight_h)
    await server_tasks.stop_worker_tasks(app, _worker_h)
    await server_tasks.stop_event_tasks(app, _event_h)
    await server_tasks.stop_mgmt_tasks(app, _mgmt_h)
    if _sweep_task:
        _sweep_task.cancel()
    log.info("Process tasks stopped")


app = server_common.build_app(
    title="Agentic DevOps API",
    version="2.2.0",
    lifespan=lifespan,
)

app.include_router(grape_auth.router)
app.include_router(auth.router)
app.include_router(repos.router)
# Admin 管理后台（/api/admin/*）
from src.domains.mgmt.admin.routes import router as admin_router
app.include_router(admin_router)
# Issue-45 M3：Git 仓库统一接口（provider 白名单资源端点；prefix=/api/git-repo，与 grape 托管 repos.py 区分）
from src.api.git_repo_api import router as git_repo_router
app.include_router(git_repo_router)
app.include_router(dashboard.router)
app.include_router(agents.router)
app.include_router(events.router)
app.include_router(chat.router)
app.include_router(settings.router)
app.include_router(skills.router)
app.include_router(search.router)
app.include_router(pipeline.router)
app.include_router(webhook.router)
app.include_router(scenes.router)
app.include_router(templates.router)
app.include_router(tasks.router)
app.include_router(issues.router)
app.include_router(runtime.router)
app.include_router(feishu_onboard.router)
app.include_router(feishu_card.router)
app.include_router(task_logs.router)
app.include_router(knowledge.router)
app.include_router(health.router)
app.include_router(devstage.router)
app.include_router(latency.router)
app.include_router(feedback.router)
app.include_router(autopilot.router)
app.include_router(orgs.router)
app.include_router(community.router)
app.include_router(observability.router)
app.include_router(patrol.router)

# ===== Issue-45: OpenAPI 定制渲染（securitySchemes + 仅对外安全路由进契约）=====

from src.api.openapi import custom_openapi
app.openapi = lambda: custom_openapi(app)

server_common.init_process_db()

server_common.attach_health(app, service_name="server", readiness=True)

server_common.attach_frontend(app)
