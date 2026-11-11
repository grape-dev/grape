"""Mgmt 进程（grape_server :38080）——用户面 API、身份认证、配置管理、IM WS、前端托管。

Issue #699 Phase 4 四进程拆分：本进程把 main.py 的「用户面」职责独立出来，
只注册 Mgmt 域路由 + 跨进程路由的用户面端点（issues/chat/events/runtime 只读），
只启动 Mgmt 进程定时任务（设计 §5.1 / §5.5 / §10）。
"""
import sys, os

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

    # GSB：注册本进程 + 起 30s 心跳（失败只告警，不影响 mgmt 启动）
    _gsb = getattr(app.state, "gsb", None)
    if _gsb is not None:
        await _gsb.startup()

    # Mgmt 进程定时任务（设计 §5.1）
    _mgmt_h = await server_tasks.start_mgmt_tasks(app)
    bot_manager = _mgmt_h.get("bot_manager")
    log.info("Grape Mgmt Server lifespan started")

    yield

    if _gsb is not None:
        await _gsb.shutdown()
    await server_tasks.stop_mgmt_tasks(app, _mgmt_h)
    log.info("Grape Mgmt Server lifespan stopped")


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
