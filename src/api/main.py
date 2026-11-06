"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os, asyncio

# Issue #455：主服务要求 Python >= 3.10。防御直接 `uvicorn src.api.main:app` 的启动方式，
# 在业务模块导入链触发前显式提示并退出。
from src.api.version_check import ensure_python_version
ensure_python_version()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from src.api.json_safe import SafeJSONResponse  # Issue #684：统一 JSON 序列化出口

from src.api.routes import auth, grape_auth, repos, dashboard, agents, events, chat, settings, skills, search, pipeline, webhook, scenes, templates, tasks, runtime, feishu_onboard, feishu_card, task_logs, knowledge, health, devstage, latency, issues, feedback, autopilot, orgs, community, observability, patrol


bot_manager = None


def _startup_security_check():
    """启动安全门禁（fail-closed）。

    JWT 密钥校验无条件执行（生产必须强密钥；开发/测试允许进程内随机兜底）；
    其余检查（密钥轮换池、邮件、凭据加密、cookie、redirect_uri）保留生产门禁。
    """
    from src.config import log
    from src.auth.security_config import is_production, get_jwt_kid_current, get_jwt_secret_keys, get_credential_keys

    errors = []

    # 1. JWT 密钥校验——无条件执行（开发/测试随机兜底态放行，其余不安全一律拒绝）
    from src.api.jwt_utils import validate_jwt_secret
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
    from src.auth.providers import get_provider, list_providers
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
    from src.config import log

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
            from src.session_manager import db, db_lock
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
                from src.session_manager import db, db_lock
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
    from src.config import log
    from src.observability import alerts as _obs_alerts
    from src.observability import slo as _obs_slo
    from src.observability import loops as _obs_loops

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
    from src.config import log
    from src.observability import metric_rollup as _obs_mrollup
    from src.observability import llm_usage as _obs_llm_usage
    from src.observability import loops as _obs_loops

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
    from src.config import log
    from src.observability import rollup as _obs_rollup
    from src.observability import logs as _obs_logs
    from src.observability import loops as _obs_loops

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
    from src.config import log
    async def _active_user_tracker():
        from src.api.deps import get_pending_active
        from src.user import db as user_db
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


def _start_vcs_exists_backfill_task():
    """Issue #995：存量 issue.vcs_exists 异步回填（幂等，启动后延迟执行一次）。

    不阻塞启动；失败仅告警，列表请求仍按 fail-open 语义工作。
    """
    from src.config import log

    async def _worker():
        await asyncio.sleep(60)
        try:
            from src.api.issue_vcs_index import backfill_vcs_exists
            summary = await asyncio.to_thread(backfill_vcs_exists)
            log.info(f"VCS existence backfill: {summary}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"VCS existence backfill failed: {e}")

    return asyncio.create_task(_worker(), name="vcs-exists-backfill")


# ===== main.py lifespan（单进程兼容模式，Issue #596 后保留）=====
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_manager
    from src.config import log

    # 生产环境启动安全门禁
    try:
        _startup_security_check()
    except SystemExit as e:
        raise

    # Issue #455：对外基址未配置告警——OAuth 回调/安装命令依赖 server_base_url，
    # 未显式配置时默认 localhost，第三方平台无法回访（登录/托管不可用）。
    try:
        from src.user.db import get_server_base_url_warning
        _base_warn = get_server_base_url_warning()
        if _base_warn:
            log.warning(f"[CONFIG] {_base_warn}")
        else:
            log.info("[CONFIG] server_base_url configured OK")
    except Exception as _bw:
        log.warning(f"[CONFIG] server_base_url check failed: {_bw}")

    try:
        from src.im.feishu_async import BotManager
        from src.im.handler_async import process_chat_async
        from src.im.vod_handler_async import process_vod_async

        # psutil CPU 基准预热：interval=None 模式需要先调一次建立基准
        try:
            import psutil as _ps_init
            _ps_init.cpu_percent(interval=None)
        except Exception:
            pass

        # 启动引擎健康探测线程（每 10 分钟对引擎发起 CLI 测试对话）
        try:
            from src.observability.engine_probe import start_probe
            start_probe()
        except Exception as e:
            log.warning(f"Engine probe start failed: {e}")

        # lease 模式：默认开启（方式 A），BOT_LEASE_DISABLED=true 退回旧模式
        from src.config import BOT_LEASE_DISABLED
        lease_store = None
        if not BOT_LEASE_DISABLED:
            from src.im.bot_lease import BotLeaseStore, generate_instance_id
            instance_id = generate_instance_id()
            lease_store = BotLeaseStore(instance_id=instance_id)
            log.info(f"IM WS: lease enabled, instance_id={instance_id}")

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
        # Issue #596 P1：注册进进程级 registry（跨模块 IM 访问统一走 get_bot_manager）
        from src.api.bot_manager_registry import set_bot_manager
        set_bot_manager(bot_manager)
    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")

    # 模板自愈：启动时检查 project-templates.json + examples/ 是否存在，
    # 缺失则自动从 grape-template 仓库拉取（防 git merge 删除后模板页变空）
    try:
        _templates_ok = _check_and_sync_templates()
        if _templates_ok:
            log.info("Templates: project-templates.json + examples/ verified OK")
        else:
            log.warning("Templates: auto-sync attempted but issues remain, check logs")
    except Exception as e:
        log.warning(f"Templates: startup check failed: {e}")

    # Issue #541：存量场景错误绑定幂等修复——dev_scene / issue.scene_id 曾误写
    # 模板场景名，扫描后按 market_scene_id 改绑仓内副本真实 scene_id（可安全重跑）
    try:
        from src.api.scene_binding_repair import repair_scene_bindings
        _rep_541 = repair_scene_bindings()
        if _rep_541["repos_fixed"] or _rep_541["issues_fixed"]:
            log.info(f"Scene binding repair: fixed {_rep_541['repos_fixed']} repos, "
                     f"{_rep_541['issues_fixed']} issues (Issue #541)")
    except Exception as e:
        log.warning(f"Scene binding repair failed: {e}")

    # help.html 生成物：内容由 help/（usage + api）渲染，启动即刷新（改 md 后重启生效）
    try:
        from src.help_page_gen import generate as _generate_help_page
        _n_help = _generate_help_page()
        log.info(f"Help: help.html regenerated ({_n_help} sources)")
    except Exception as e:
        log.warning(f"Help: help.html generation failed (kept last generated file): {e}")

    # OpenAPI 契约落盘：help/api/openapi.json（规范副本）+ static/openapi.json（固定下载 URL）
    try:
        from src.api.openapi import custom_openapi
        from src.help_page_gen import write_openapi_contract
        _schema = custom_openapi(app)
        _n_paths = write_openapi_contract(_schema)
        log.info(f"OpenAPI: contract dumped ({_n_paths} paths) -> /static/openapi.json")
    except Exception as e:
        log.warning(f"OpenAPI: contract dump failed: {e}")

    # lease sweep 循环（问题 J：变量名 _lease_sweep_task 区分）
    _lease_sweep_task = None
    if bot_manager and bot_manager._lease_store:
        _lease_sweep_task = asyncio.create_task(bot_manager.lease_sweep_loop())
        log.info("Lease sweep loop started")

    # sweeper：Issue done 后清理 workdir（见 multi-issue-isolation-design.md §3.4.2）
    _sweep_task = None
    try:
        from src.runtime.cleanup import sweep_loop
        _sweep_task = asyncio.create_task(sweep_loop())
        log.info("Sweeper loop started")
    except Exception as e:
        log.warning(f"Sweeper loop start failed: {e}")

    # A2A 链路巡检器（Phase 1 只读，docs/weekly-09/a2a-chain-patrol-design.md）：
    # 拉模型兜底，谓词 UPDATE 选主，仅写 grape_t_patrol_findings，零既有表写路径
    _patrol_task = None
    try:
        from src.api.patrol import patrol_loop
        _patrol_task = asyncio.create_task(patrol_loop())
        log.info("Patrol loop started")
    except Exception as e:
        log.warning(f"Patrol loop start failed: {e}")

    # 知识库 OBS 同步：后台定时迁移老数据到 OBS（每小时一次）
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

    # Issue #103：知识写入队列消费者——后台兜底处理 pending 任务
    _knowledge_write_task = None
    try:
        from src.api.knowledge import knowledge_write_worker
        _knowledge_write_task = asyncio.create_task(knowledge_write_worker())
        log.info("Knowledge write worker started")
    except Exception as e:
        log.warning(f"Knowledge write worker start failed: {e}")

    # 官方技能 OBS 自动同步：定时扫描本地技能变更，上传 OBS（每 30 分钟）
    try:
        from src.storage.skill_sync import start_skill_sync_scheduler
        start_skill_sync_scheduler()
    except Exception as e:
        log.warning(f"Skill sync scheduler start failed: {e}")

    # 反馈→Issue reconcile：Agent 自动反馈定时转官方技能仓库/所属仓库 Issue（weekly-08）
    try:
        from src.api.feedback_issue_converter import start_feedback_reconcile_scheduler
        start_feedback_reconcile_scheduler()
    except Exception as e:
        log.warning(f"Feedback reconcile scheduler start failed: {e}")

    # 官方技能+场景合并定时同步：每 30 分钟串行同步（R31：skill 完成后自动触发场景同步，
    # 场景同步已合并进 sync_official_skills，不再有独立的 scene_sync scheduler）

    # 临时上传文件定时清理：每小时清理超过 24 小时的孤儿文件（Issue-54）
    try:
        from src.storage.upload_cleanup import start_upload_cleanup_scheduler
        start_upload_cleanup_scheduler()
    except Exception as e:
        log.warning(f"Upload cleanup scheduler start failed: {e}")

    # P2-C (Issue #357): Large table maintenance — nullify old agent_tasks fields
    # and delete old task_messages. Runs every 6 hours with distributed lock.
    # Issue #463 P1: also delete terminal agent_tasks older than 90 days.
    # Issue #596 P3 (§6.2 拆分)：单进程模式同样按两个独立锁 key 执行
    # （table_maintenance_tasks → agent_tasks 清理；table_maintenance_messages →
    # task_messages 清理），与多进程模式 A/C 各持一锁的语义一致——
    # 多进程部署时 A/C 各自抢自己的锁，单进程两个锁都在本进程内顺序拿。
    try:
        from src.runtime.task_lock import run_with_distributed_lock

        async def _table_maintenance_worker():
            import asyncio
            await asyncio.sleep(600)  # 10 min after startup (avoid migration race)
            while True:
                try:
                    async with run_with_distributed_lock("table_maintenance_tasks", ttl=3600):
                        from src.runtime.table_maintenance import (
                            cleanup_old_agent_tasks_fields,
                            delete_old_agent_tasks,
                        )
                        cleanup_old_agent_tasks_fields()
                        delete_old_agent_tasks()
                    async with run_with_distributed_lock("table_maintenance_messages", ttl=3600):
                        from src.runtime.table_maintenance import cleanup_old_task_messages
                        cleanup_old_task_messages()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("table maintenance run failed")
                await asyncio.sleep(6 * 3600)  # every 6 hours

        asyncio.create_task(_table_maintenance_worker(), name="table-maintenance")
    except Exception as e:
        log.warning(f"Table maintenance scheduler start failed: {e}")

    # Autopilot 定时调度器：每分钟扫描到期任务，创建 Issue 触发流水线
    # 注：调度器在本 lifespan 下方 IM WS 启动段统一以 start_scheduler() 启动（幂等）；
    # 此处历史上的 asyncio.create_task(start_scheduler()) 是双重启动——start_scheduler()
    # 是同步函数返回 None，create_task(None) 抛 "a coroutine was expected"，靠下方幂等
    # 挡住才没有真双跑，但每次启动刷一条假 warning（9-17 引入，2026-10-14 移除）。
    try:
        from src.runtime.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        log.warning(f"Autopilot scheduler start failed: {e}")

    # 本机 direct 引擎一等认领器（local-claimer-first-class-broadcast-design.md）：
    # 让本机引擎与远端 worker 同级参与广播认领（同一条 claim_by_tag 检查链）。
    # GRAPE_LOCAL_CLAIMER=0 可关闭（回退为纯远端认领）。
    try:
        from src.runtime.local_claimer import start_local_claimer
        start_local_claimer()
    except Exception as e:
        log.warning(f"Local claimer start failed: {e}")

    # Issue #345: 全局 bot token 健康探活——每 5 分钟探活，集群下分布式锁保证单实例执行
    try:
        from src.runtime.bot_token_health import start_bot_token_health
        start_bot_token_health()
    except Exception as e:
        log.warning(f"Bot token health check start failed: {e}")

    # D7: 冷静期定时任务：每天执行一次，匿名化超 30 天的冷静期账号（需求 22）
    try:
        from src.admin.cooldown import start_cooldown_scheduler
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

    # Issue #995：存量 VCS 存在性回填（启动后异步执行，不阻塞，幂等可重跑）
    try:
        app.state.vcs_exists_backfill_task = _start_vcs_exists_backfill_task()
        log.info("VCS existence backfill started (async)")
    except Exception as e:
        log.warning(f"VCS existence backfill start failed: {e}")

    # D32: user_accounts → grape_accounts 数据迁移（Issue #343: yield 前同步等待完成）
    # P1-1 修复: 原异步执行不等待就 yield，迁移期间用户请求读到旧 user_id 导致数据错位。
    # 改为 yield 前同步等待完成（幂等，已迁移环境秒返回）。
    try:
        from scripts.migrate_user_accounts_to_grape import run_migration
        await asyncio.to_thread(run_migration, False)
        log.info("D32 migration completed (sync, pre-yield)")
    except Exception as e:
        log.error(f"D32 migration failed: {e}", exc_info=e)
        app.state.d32_migration_failed = True

    # 本机直连引擎周期重探（方案 C，local-direct-health-probe-design.md）：每 10 分钟真实探测 --version，
    # 刷新 agent_runtimes.healthy，防二进制变更/被删后 healthy 过期
    _probe_task = None
    try:
        async def _local_probe_loop():
            import time as _t
            from src.runtime.registry import discover_runtimes
            while True:
                def _run():
                    from src.runtime.local_probe import probe_local_engines
                    from src.runtime.agent import _scan_local_skills_with_detail
                    from src.user import db as user_db
                    import socket as _sock
                    _hostname = _sock.gethostname()
                    probed = [r for r in probe_local_engines(discover_runtimes()) if r.get("installed")]
                    if probed:
                        # 扫描本机 local_skills 详情并注入
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

        _probe_task = asyncio.create_task(_local_probe_loop())
        log.info("Local probe loop started (10min)")
    except Exception as e:
        log.warning(f"Local probe loop start failed: {e}")

    # 远端 runtime 僵尸清理（每 60 秒）：AI DevSpace 被删后 runtime 行残留 status=online
    # 但 worker 不再心跳。reap_stale_runtimes 把 last_seen_at 过期的标 offline +
    # 重新派发其 queued 任务，防止任务永久卡死。
    _reap_task = None
    try:
        async def _reap_stale_loop():
            while True:
                await asyncio.sleep(60)
                try:
                    result = await asyncio.to_thread(_reap_stale_runtimes_sync)
                    if result.get("reaped"):
                        log.info(f"Reap loop: {result}")
                except Exception as e:
                    log.warning(f"Reap stale runtimes failed: {e}")

        def _reap_stale_runtimes_sync():
            from src.runtime.service import reap_stale_runtimes
            return reap_stale_runtimes()

        _reap_task = asyncio.create_task(_reap_stale_loop())
        log.info("Reap stale runtimes loop started (60s)")
    except Exception as e:
        log.warning(f"Reap stale runtimes loop start failed: {e}")

    # 主控机指标上报（Issue #596 P1：复用 _start_server_metrics_task 启动器）
    _server_metrics_task = None
    try:
        _server_metrics_task = _start_server_metrics_task()
    except Exception as e:
        log.warning(f"Server metrics loop start failed: {e}")

    # Issue #85：引擎健康度定时刷新——每 5 分钟对所有引擎 force_refresh，
    # 覆盖离线引擎无任务事件触发的场景（失联引擎不会产生任务完成事件）
    try:
        from src.runtime.health_scorer import start_health_refresh_scheduler
        start_health_refresh_scheduler()
    except Exception as e:
        log.warning(f"Health refresh scheduler start failed: {e}")

    # 可观测性告警中心（Issue #596 P1：复用 _start_obs_alert_task 启动器）
    _obs_alert_loop_task = None
    try:
        _obs_alert_loop_task = _start_obs_alert_task()
    except Exception as e:
        log.warning(f"Observability alert evaluator start failed: {e}")

    # 业务指标聚合（Issue #596 P1：复用 _start_obs_rollup_task 启动器）
    _obs_rollup_task = None
    try:
        _obs_rollup_task = _start_obs_rollup_task()
    except Exception as e:
        log.warning(f"Observability metric rollup start failed: {e}")

    # 设备资源采样（Issue #596 P1：复用 _start_obs_sample_task 启动器）
    _obs_sample_task = None
    try:
        _obs_sample_task = _start_obs_sample_task()
    except Exception as e:
        log.warning(f"Observability runtime sampler start failed: {e}")

    # 可观测性日志落库：主控 WARNING/ERROR → obs_log（有界队列 + 后台批量，env OBS_LOG_DB=0 关闭）
    try:
        from src.observability import logs as _obs_logs_install
        _st = _obs_logs_install.install_master_logging()
        log.info(f"Observability log sink: {_st}")
    except Exception as e:
        log.warning(f"Observability log sink start failed: {e}")

    # 追踪（§6.4）：给历史任务补 trace id（确定性生成，一次即可；新任务由查询时惰性补齐）
    try:
        from src.observability import trace as _obs_trace
        _nstamped = await asyncio.to_thread(_obs_trace.stamp_missing)
        log.info(f"Observability trace stamp: {_nstamped} legacy task(s) backfilled")
    except Exception as e:
        log.warning(f"Observability trace stamp failed: {e}")

    # 用户活跃状态追踪（Issue #596 P1：复用 _start_active_user_tracker_task 启动器）
    _active_tracker_task = None
    try:
        _active_tracker_task = _start_active_user_tracker_task()
    except Exception as e:
        log.warning(f"Active user tracker start failed: {e}")

    # Issue #150: Autopilot 定时调度引擎
    try:
        from src.runtime.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        log.warning(f"Autopilot scheduler start failed: {e}")

    yield

    log.info("IM WS: stopping...")
    # 问题 H：先 cancel lease_sweep_task（不再抢新锁）→ 再 stop bot_manager（停 bot + 释放 lease）
    if _lease_sweep_task:
        _lease_sweep_task.cancel()
        try:
            await asyncio.wait_for(_lease_sweep_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    if bot_manager:
        await bot_manager.stop()
    if _sweep_task:
        _sweep_task.cancel()
    if _probe_task:
        _probe_task.cancel()
    if _reap_task:
        _reap_task.cancel()
    if _knowledge_write_task:
        _knowledge_write_task.cancel()
        try:
            await asyncio.wait_for(_knowledge_write_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    # 巡检后台循环（S53/R24，外部复审发现 1）：与 obs 三循环同款收口——
    # 此前漏 cancel，patrol_loop 是无限循环写库任务，任何在本进程启动过 app
    # 的场景（含 pytest TestClient）都会把它留在后台继续写库 → 用例成片假失败
    # （main.py 本处注释记录过的同类真实事故，复发封口）。
    if _patrol_task:
        _patrol_task.cancel()
        try:
            await asyncio.wait_for(_patrol_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    # 可观测性后台循环（告警求值 / 指标聚合 / 设备采样）：**必须一起收掉**。
    # 之前漏了这三个 cancel —— 任何在本进程里启动过 app 的场景（含 pytest 里的 TestClient）
    # 都会把这些循环留在后台，继续对同一个库读写 → 后续用例成片假失败（真实踩到）。
    for _t in (_obs_alert_loop_task, _obs_rollup_task, _obs_sample_task):
        if _t:
            _t.cancel()
            try:
                await asyncio.wait_for(_t, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
    # knowledge_migrate：shutdown 时尝试等待完成（不 cancel，因为迁移中断不如跑完）
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
        from src.storage.skill_sync import stop_skill_sync_scheduler
        await stop_skill_sync_scheduler()
    except Exception:
        pass
    # 反馈→Issue reconcile：优雅关闭定时任务（weekly-08）
    try:
        from src.api.feedback_issue_converter import stop_feedback_reconcile_scheduler
        await stop_feedback_reconcile_scheduler()
    except Exception:
        pass
    # R31：场景同步随 skill worker 一同优雅关闭（合并进 stop_skill_sync_scheduler），
    # stop_scene_sync_scheduler 已随 standalone worker 删除（决策 A）
    # Issue #85：健康度定时刷新线程优雅关闭
    try:
        from src.runtime.health_scorer import stop_health_refresh_scheduler
        stop_health_refresh_scheduler()
    except Exception:
        pass
    # Issue #150: 停止 Autopilot 调度器
    try:
        from src.runtime.scheduler import stop_scheduler
        await stop_scheduler()
    except Exception:
        pass
    # 终止运行中的 agent 子进程（opencode），使 executor 线程尽快结束，
    # 避免 uvicorn 优雅停机等待超时被 systemd SIGKILL（grape-main timeout 重启根因，2026-08-18）
    try:
        from src.agent.executor import terminate_active_agents
        terminate_active_agents(grace_seconds=5)
    except Exception as e:
        log.warning(f"terminate_active_agents failed: {e}")
    log.info("IM WS stopped")


app = FastAPI(
    title="Agentic DevOps API",
    version="2.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    default_response_class=SafeJSONResponse,  # Issue #684：统一 JSON 序列化出口（控制字符转义）
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 可观察性：HTTP 请求指标中间件（仅 METRICS_ADDR 启用时挂载）
from src.observability import metrics_middleware, start_metrics_server, metrics_enabled
metrics_middleware(app)
if metrics_enabled():
    start_metrics_server()

# Issue #596 P1：request_id + HTTP 请求日志中间件抽至 src/api/middleware.py（A/B/C 三入口复用）
from src.api.middleware import install_request_middleware
install_request_middleware(app)

# /api/ping（Issue #596 P1：随 health_endpoints 安装，含 /health /healthz /readyz）
from src.api.health_endpoints import install_health_endpoints
install_health_endpoints(app, service_name="server")

app.include_router(grape_auth.router)
app.include_router(auth.router)
app.include_router(repos.router)
# Admin 管理后台（/api/admin/*）
from src.admin.routes import router as admin_router
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
# ===== Issue #596 P1：静态资源 + SPA 路由抽至 src/api/spa.py（A 进程复用）=====
from src.api.spa import install_spa_routes
install_spa_routes(app)

from src.api.db_setup import init_database
init_database()

from src.config import _load_accounts
_load_accounts()

# 全局异常处理器（Issue #596 P1：抽至 src/api/exception_handlers.py）
from src.api.exception_handlers import install_exception_handlers
install_exception_handlers(app)

# ===== 三层健康检查（Issue #596 P1：抽至 src/api/health_endpoints.py）=====
# /health /healthz /readyz /health/realtime /api/ping 已在上方
# install_health_endpoints(app, service_name="server") 处注册。

# ===== 模板自愈：启动时检查 + 自动同步 =====

def _check_and_sync_templates() -> bool:
    """检查 project-templates.json + examples/ 是否存在，缺失则自动从 grape-template 仓库拉取。

    防止 git merge/pull 后模板文件被删除导致模板页变空（commit 838f2c98 事故）。
    调用 scripts/sync_templates.sh 执行实际同步。
    """
    import os as _os
    from src.config import log

    _repo_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    _json_path = _os.path.join(_repo_root, "configs", "project-templates.json")
    _examples_dir = _os.path.join(_repo_root, "examples")

    # 1. 检查 JSON 文件
    json_ok = _os.path.isfile(_json_path) and _os.path.getsize(_json_path) > 100
    # 2. 检查 examples/ 目录有内容（至少有 1 个子目录）
    examples_ok = _os.path.isdir(_examples_dir) and \
        any(_os.path.isdir(_os.path.join(_examples_dir, d)) for d in _os.listdir(_examples_dir) if not d.startswith('.'))

    if json_ok and examples_ok:
        return True  # 一切正常

    # 3. 缺失 → 执行 sync_templates.sh 自动拉取
    missing = []
    if not json_ok:
        missing.append("configs/project-templates.json")
    if not examples_ok:
        missing.append("examples/")
    log.warning(f"Templates: missing {missing}, auto-syncing from grape-template repo...")

    _sync_script = _os.path.join(_repo_root, "scripts", "sync_templates.sh")
    if not _os.path.isfile(_sync_script):
        log.error(f"Templates: sync script not found at {_sync_script}")
        return False

    import subprocess as _sp
    try:
        _result = _sp.run(
            ["bash", _sync_script],
            capture_output=True, text=True, timeout=60,
            env={**_os.environ, "TEMPLATES_REPO": "https://gitcode.com/grape-dev/grape-template.git"}
        )
        if _result.returncode == 0:
            log.info(f"Templates: auto-sync OK\n{_result.stdout}")
        else:
            log.error(f"Templates: auto-sync failed (exit={_result.returncode})\n{_result.stderr}")
    except Exception as e:
        log.error(f"Templates: auto-sync exception: {e}")
        return False

    # 4. 再次检查
    json_ok = _os.path.isfile(_json_path) and _os.path.getsize(_json_path) > 100
    examples_ok = _os.path.isdir(_examples_dir) and \
        any(_os.path.isdir(_os.path.join(_examples_dir, d)) for d in _os.listdir(_examples_dir) if not d.startswith('.'))
    return json_ok and examples_ok
