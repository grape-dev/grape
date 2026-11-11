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
from src.domains.mgmt.routes import knowledge
from src.domains.mgmt.routes import patrol


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
    from src.domains.runtime.vcs.providers import get_provider, list_providers
    missing_redirect = [p for p in list_providers() if not (get_provider(p) or {}).get("redirect_uri")]
    if missing_redirect:
        errors.append(f"生产环境必须配置 redirect_uri，缺少: {', '.join(missing_redirect)}")

    if errors:
        for e in errors:
            log.error(f"[SECURITY] {e}")
        raise SystemExit(f"生产环境安全门禁失败：{'; '.join(errors)}")


# ===== 定时任务启动器（Issue #596 P1 抽出，A 进程 grape_server.py 复用）=====
# 这些函数把 main.py lifespan 内的巨型闭包循环抽为可复用启动器，
# 单进程模式（main.py）与 A 进程（grape_server.py）共用同一实现源。

def _start_server_metrics_task():
    """主控机指标上报循环（15s）——启动并返回 task。"""
    from datetime import datetime, timezone
    from src.kernel.config import log

    async def _server_metrics_loop():
        import socket as _sock, hashlib as _hl
        _hostname = _sock.gethostname() or "unknown-server"
        _local_ip = "127.0.0.1"
        try:
            _s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            _s.connect(("8.8.8.8", 80))
            _local_ip = _s.getsockname()[0]
            _s.close()
        except Exception:
            pass
        try:
            from src.kernel.session_manager import db, db_lock
            _token_hash = _hl.sha256(_hostname.encode()).hexdigest()
            with db_lock:
                c = db.cursor()
                c.execute("SELECT id FROM runtime_credentials WHERE device_name=? AND scope='global'", (_hostname,))
                if not c.fetchone():
                    c.execute(
                        "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status, last_ip) "
                        "VALUES (?, ?, ?, 'global', 'active', ?)",
                        (_token_hash, _hostname, _hostname, _local_ip))
                    db.commit()
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
                    # BANDWIDTH: 读 /proc/net/dev，两次采样差分算速率
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
                from src.kernel.session_manager import db, db_lock
                with db_lock:
                    c = db.cursor()
                    c.execute(
                        "UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, "
                        "cpu_percent=?, memory_percent=?, disk_percent=?, tmp_percent=?, "
                        "bandwidth_percent=?, bandwidth_mbps=?, load_avg=? "
                        "WHERE device_name=? AND scope='global'",
                        (_now, _local_ip, _cpu, _mem, _disk, _tmp, _bw_pct, _bw_mbps, _load, _hostname))
                    db.commit()
            except Exception as e:
                log.warning(f"Server metrics: upload failed: {e}")
            await asyncio.sleep(15)

    _t = asyncio.create_task(_server_metrics_loop())
    log.info("Server metrics loop started (15s)")
    return _t


def _start_obs_alert_task():
    """可观测性告警中心循环（30s + SLO 独立周期）——启动并返回 task。"""
    from src.kernel.config import log
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

    _t = asyncio.create_task(_obs_alert_loop())
    log.info(f"Observability alert evaluator started (30s, seeded={_obs_seeded});"
             f" SLO eval interval {_obs_slo.eval_interval_sec()}s (decoupled, Issue #437)")
    return _t


def _start_obs_rollup_task():
    """业务指标聚合循环（60s）——启动并返回 task。"""
    from src.kernel.config import log
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

    _t = asyncio.create_task(_obs_rollup_loop())
    log.info("Observability metric rollup started (60s, minute 7d / hour 30d)")
    return _t


def _start_obs_sample_task():
    """设备资源采样循环（60s）——启动并返回 task。"""
    from src.kernel.config import log
    from src.domains.insight.observability import rollup as _obs_rollup
    from src.domains.insight.observability import logs as _obs_logs
    from src.domains.insight.observability import loops as _obs_loops

    async def _obs_sample_loop():
        while True:
            await asyncio.sleep(60)
            try:
                r = await asyncio.to_thread(_obs_rollup.snapshot_runtimes)
                if r.get("inserted"):
                    log.debug(f"Observability runtime samples: {r}")
                _nc = await asyncio.to_thread(_obs_log.maybe_cleanup)
                if _nc:
                    log.info(f"Observability logs cleaned: {_nc} rows")
                    _obs_loops.beat("obs_log_cleanup")
                _obs_loops.beat("obs_sample")
            except Exception as e:
                _obs_loops.fail("obs_sample", e)
                log.warning(f"Observability runtime sampling failed: {e}")

    _t = asyncio.create_task(_obs_sample_loop())
    log.info("Observability runtime sampler started (60s)")
    return _t


def _start_active_user_tracker_task():
    """用户活跃状态追踪循环（30s flush + 5min token/guard 清理）——启动并返回 task。"""
    from src.kernel.config import log
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
            try:
                _purge_i += 1
                if _purge_i >= 10:
                    _purge_i = 0
                    from src.api.auth_token import purge_expired_session_tokens
                    await asyncio.to_thread(purge_expired_session_tokens)
                    from src.api.event_store import purge_stale_event_guards
                    await asyncio.to_thread(purge_stale_event_guards)
            except Exception as e:
                log.warning(f"Session token purge failed: {e}")

    _t = asyncio.create_task(_active_user_tracker())
    log.info("Active user tracker started (30s flush)")
    return _t


# ===== main.py lifespan（单进程兼容模式，Issue #596 后保留）=====
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
                            delete_old_agent_tasks,
                        )
                        cleanup_old_agent_tasks_fields()
                        delete_old_agent_tasks()
                    async with run_with_distributed_lock("table_maintenance_messages", ttl=3600):
                        from src.domains.runtime.engine.table_maintenance import cleanup_old_task_messages
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
