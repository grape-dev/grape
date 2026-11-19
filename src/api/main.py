"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""

import sys, os, asyncio

# Issue #455：主服务要求 Python >= 3.10。防御直接 `uvicorn src.api.main:app` 的启动方式，
# 在业务模块导入链触发前显式提示并退出。
from src.kernel.host_identity import engine_host  # Issue #2094: 引擎/设备身份（GRAPE_HOSTNAME 可覆盖）
from src.api.version_check import ensure_python_version

ensure_python_version()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api import server_common
from src.domains.mgmt.routes import auth, grape_auth, repos, agents, settings, skills, scenes, templates, feishu_onboard, feishu_card, devstage, feedback, autopilot, orgs, org_repos, org_dispatches, community, org_context_store, org_knowledge, org_audit, org_analysis
from src.domains.mgmt.routes import categories as categories_routes
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
    from src.kernel.security_config import is_production, get_jwt_kid_current, get_jwt_secret_keys, get_credential_keys

    errors = []

    # 1. JWT 密钥校验——无条件执行（开发/测试随机兜底态放行，其余不安全一律拒绝）
    from src.kernel.auth_sdk.jwt_utils import validate_jwt_secret
    if not validate_jwt_secret():
        errors.append("JWT_SECRET 不安全：未配置强密钥（生产必须设置 JWT_SECRET 或密钥轮换池）")

    # 1b. 数据库加密密钥（沙箱平台 api_key / 用户 LLM 凭据 api_key）——无条件探测。
    # 缺密钥时：沙箱凭据的平台登记会失败（凭据建好、平台没绑，详情按钮消失），
    # LLM 凭据也无法保存。历史上这条失败在调用处被吞成 WARNING，用户完全看不出原因。
    # 生产环境按启动门禁处理（fail-closed）；开发/测试只告警不拦。
    from src.kernel.crypto import key_status as _key_status, crypto_config_path as _crypto_path
    _ks = _key_status()
    if not _ks.get("configured"):
        _key_hint = (
            "数据库加密密钥未配置（沙箱平台 api_key / 用户 LLM 凭据的加密密钥）。"
            "配置方式二选一："
            f"① 写 {_crypto_path()}，内容 "
            '{"encryption_key": "<32字节base64url>"}，权限必须 0600（chmod 600）；'
            "② 或设环境变量 GRAPE_DB_ENCRYPTION_KEY=<32字节base64url>（systemd 部署写在 "
            "/etc/grape/grape.env，改完 daemon-reload 后重启 grape 相关服务）。"
            "生成密钥：python3 -c \"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"。"
            "多主控实例必须使用同一把密钥。"
            f"当前探测错误：{_ks.get('error')}"
        )
        if is_production():
            errors.append(_key_hint)
        else:
            log.warning(f"[SECURITY] {_key_hint}")

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

def _start_server_metrics_task():
    """主控机指标上报循环（15s）——启动并返回 task。"""
    from datetime import datetime, timezone
    from src.kernel.config import log

    async def _server_metrics_loop():
        import socket as _sock, hashlib as _hl
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
            # （自有 DSN 优先、单库回落进程库），SQL 只留 runtime public 一份。
            # **同步直连、不入意图队列**——这是启动注册 + 15s 心跳，落成意图会每天
            # 写几千条日志；循环本身就是重试机制。
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
                from src.domains.runtime import public as _rt_public_met
                # 同步直连：15s 心跳落成意图会淹没日志，循环即重试机制
                _rt_public_met.touch_device_credential_any(
                    _hostname, _now, _local_ip, _cpu, _mem, _disk, _tmp,
                    _bw_pct, _bw_mbps, _load)
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
                _nc = await asyncio.to_thread(_obs_logs.maybe_cleanup)
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
                    from src.kernel.auth_sdk.auth_token import purge_expired_session_tokens
                    await asyncio.to_thread(purge_expired_session_tokens)
                    from src.api.event_store import purge_stale_event_guards
                    await asyncio.to_thread(purge_stale_event_guards)
            except Exception as e:
                log.warning(f"Session token purge failed: {e}")

    _t = asyncio.create_task(_active_user_tracker())
    log.info("Active user tracker started (30s flush)")
    return _t

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_manager, im_engine
    from src.kernel.config import log

    # 卡死诊断（2026-11-05 事故）：SIGUSR1 手动 dump + 事件循环 stall 自动 dump
    try:
        from src.api.diagnostics import install as _diag_install
        _diag_install(log)
    except Exception as _diag_err:
        log.warning(f"[diag] install failed: {_diag_err}")

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

    # Issue #2261 B1：通知 outbox 消费者（单进程并集模式与四进程 A 进程同款接线）。
    # 先停消费者、再停引擎（关停侧见下方 lifespan 尾部）。无运行 loop/无引擎时静默跳过。
    try:
        from src.domains.runtime.im import outbox as _outbox_mod
        _outbox_mod.attach_outbox_consumer(app, getattr(app.state, "im_engine", None))
    except Exception as _oe:
        log.warning(f"IM outbox consumer start failed: {_oe}")
    _event_h = await server_tasks.start_event_tasks(app, include_sweep_events=False)
    _worker_h = await server_tasks.start_worker_tasks(app, include_sweep_tasks=False, include_table_maintenance=False)
    _insight_h = await server_tasks.start_insight_tasks(app, include_table_maintenance=False)

    # D102-P：历史 PAT-as-ei-token 一次性回填（幂等，PAT 归位凭据域；
    # 迁移竞态无虞——哈希比对精确命中才清，双实例并发最坏双写同值）。
    try:
        from src.domains.mgmt.auth.grape_account_db import backfill_pat_identity_tokens
        backfill_pat_identity_tokens()
    except Exception as _e:
        from src.kernel.config import log as _log
        _log.warning(f"pat identity backfill skipped (non-fatal): {_e}")

    # D109（平台对象模型）：扫描导入 → 一次性凭据迁移/关系回填 → 标记 migrated。
    # 失败 fail-open：读路径回落文件+meta，不影响登录/托管。
    try:
        from src.api.d109_startup import d109_platform_object_startup

        d109_platform_object_startup(log)
    except Exception as _d109_err:
        log.warning(f"D109 platform-object startup skipped (non-fatal, 回落旧存储): {_d109_err}")

    # ===== IM 引擎（2026-11-13 补齐）=====
    # weekly-11 退役 legacy BotManager 后，server_tasks 在 engine_mode=on 时直接 skip；
    # 而新引擎原先只在 grape_server.py（四进程 A 进程）里启动 → 单进程入口出现
    # "谁也不起" 的空档（安装行 active 但 connected 恒 false）。
    # 这里与 grape_server 对齐补启，失败只告警不阻断主服务。
    try:
        from src.domains.runtime.im import engine_wiring as _im_wiring

        if _im_wiring.engine_mode() == "on":
            _im_engine = await _im_wiring.start_im_engine()
            if _im_engine is not None:
                # 路由侧（routes/im.py:_engine）优先读模块全局，再读 app.state → 两处都挂
                im_engine = _im_engine
                app.state.im_engine = _im_engine
                log.info("IM engine started (mode=on, instance=%s)",
                         getattr(_im_engine, "instance_id", ""))
            else:
                log.warning("IM engine: start failed (legacy fallback retired)")
        else:
            log.info("IM engine mode=off: IM 总开关关闭，不启动任何 IM 链路")
    except Exception as _im_err:
        log.warning(f"IM engine startup failed: {_im_err}")

    # Issue #455：对外基址未配置告警——OAuth 回调/安装命令依赖 server_base_url，
    # 未显式配置时默认 localhost，第三方平台无法回访（登录/托管不可用）。
    try:
        from src.domains.mgmt.user.db import get_server_base_url_warning
        _base_warn = get_server_base_url_warning()
        if _base_warn:
            log.warning(f"[CONFIG] {_base_warn}")
        else:
            log.info("[CONFIG] server_base_url configured OK")
    except Exception as _bw:
        log.warning(f"[CONFIG] server_base_url check failed: {_bw}")

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
        from src.api.patrol import patrol_loop
        _patrol_task = asyncio.create_task(patrol_loop())
        log.info("Patrol loop started")
    except Exception as e:
        log.warning(f"Patrol loop start failed: {e}")

    # 知识库 OBS 同步：后台定时迁移老数据到 OBS（每小时一次）
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
        from src.domains.runtime.storage.skill_sync import start_skill_sync_scheduler
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
        from src.domains.runtime.storage.upload_cleanup import start_upload_cleanup_scheduler
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
    # Issue #2261 B1：先停通知 outbox 消费者（cancel+await），再停各域任务/引擎——
    # 反序会让消费者在 await 途中拿到已停的引擎（发给死渠道）。
    try:
        from src.domains.runtime.im import outbox as _outbox_mod
        if getattr(app.state, "outbox_consumer", None) is not None:
            await app.state.outbox_consumer.stop()
        _outbox_mod.stop_outbox_consumer(app)
    except Exception as _oe:
        log.warning(f"IM outbox consumer stop failed: {_oe}")
    await server_tasks.stop_insight_tasks(app, _insight_h)
    await server_tasks.stop_worker_tasks(app, _worker_h)
    await server_tasks.stop_event_tasks(app, _event_h)
    await server_tasks.stop_mgmt_tasks(app, _mgmt_h)
    if _sweep_task:
        _sweep_task.cancel()
    # IM 引擎优雅停机（渠道长轮询/Socket 协程跟着收）
    try:
        _im = getattr(app.state, "im_engine", None)
        if _im is not None:
            await _im.stop()
            log.info("IM engine stopped")
    except Exception as _e:
        log.warning(f"IM engine stop failed: {_e}")
    log.info("Process tasks stopped")


from src.api.json_safe import SafeJSONResponse  # Issue #684：统一 JSON 序列化出口（控制字符转义）

app = server_common.build_app(
    title="Agentic DevOps API",
    version="2.2.0",
    lifespan=lifespan,
    default_response_class=SafeJSONResponse,
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

app.include_router(categories_routes.router)

app.include_router(templates.router)

app.include_router(tasks.router)

app.include_router(issues.router)

app.include_router(runtime.router)

# 接管会话：任务接管是一等资源，路由在管理域
from src.domains.mgmt.routes import takeover as takeover_api
app.include_router(takeover_api.router)

app.include_router(feishu_onboard.router)

app.include_router(feishu_card.router)

app.include_router(task_logs.router)

app.include_router(knowledge.router)

app.include_router(health.router)

app.include_router(devstage.router)

app.include_router(latency.router)

app.include_router(feedback.router)

app.include_router(autopilot.router)
# [沙箱迁移] 沙箱管理后台路由（/api/admin/sandbox/*，i7 移植）
from src.domains.mgmt.routes import sandbox_admin
app.include_router(sandbox_admin.router)
app.include_router(orgs.router)
app.include_router(org_repos.router)
app.include_router(org_dispatches.router)
# 组织上下文 + 组织知识库（Issue #2150）
app.include_router(org_context_store.router)
app.include_router(org_knowledge.router)
# 组织审计只读查询（Issue #2260）
app.include_router(org_audit.router)
app.include_router(org_analysis.router)
app.include_router(community.router)

app.include_router(observability.router)

app.include_router(patrol.router)

from src.api.openapi import custom_openapi

app.openapi = lambda: custom_openapi(app)

server_common.init_process_db()

server_common.attach_health(app, service_name="server", readiness=True)

# Issue #825 修复（PR #1231 深度审查）：im 路由必须在 SPA catch-all（/{path:path} GET）
# 之前注册，否则 GET /api/im/*（status/installations/dingtalk/card-setup）被
# catch_all 吞掉返回 404。import 在此处无环（im.py 顶层仅依赖 api.deps/kernel.config）。
# 单进程入口与四进程 A 面同一份 IM 路由（含 /outbound、/retry 与 status 的 outbox 段）；
# 此前挂 runtime 版，两个新增端点只在四进程 A 上存在（架构守护 Medium-2）。
from src.domains.mgmt.routes import im as im_api
app.include_router(im_api.router)
server_common.attach_frontend(app)

from fastapi.middleware.cors import CORSMiddleware

from src.api.json_safe import SafeJSONResponse  # Issue #684：统一 JSON 序列化出口

from src.domains.mgmt.routes import auth, grape_auth, repos, agents, settings, skills, scenes, templates, feishu_onboard, feishu_card, knowledge, devstage, feedback, autopilot, orgs, community, patrol

from src.domains.insight.routes import dashboard, search, health, latency, observability

from src.domains.runtime.routes import events, chat, pipeline, webhook, tasks, runtime, task_logs, issues


# 可观察性：HTTP 请求指标中间件（仅 METRICS_ADDR 启用时挂载）
from src.domains.insight.observability import metrics_middleware, start_metrics_server, metrics_endpoint, metrics_enabled

# Issue #596 P1：request_id + HTTP 请求日志中间件抽至 src/api/middleware.py（A/B/C 三入口复用）
from src.api.middleware import install_request_middleware

# /api/ping（Issue #596 P1：随 health_endpoints 安装，含 /health /healthz /readyz /health/realtime）
from src.api.health_endpoints import install_health_endpoints

# ===== Issue-45: OpenAPI 定制渲染（securitySchemes + 仅对外安全路由进契约）=====
from src.api.openapi import custom_openapi

# ===== Issue #596 P1：静态资源 + SPA 路由抽至 src/api/spa.py（A 进程复用）=====
from src.api.spa import install_spa_routes

from src.api.db_setup import init_database

from src.kernel.config import _load_accounts

# 全局异常处理器（Issue #596 P1：抽至 src/api/exception_handlers.py）
from src.api.exception_handlers import install_exception_handlers

# ===== 三层健康检查（Issue #596 P1：抽至 src/api/health_endpoints.py）=====
# /health /healthz /readyz /health/realtime /api/ping 已在上方 install_health_endpoints(app) 处注册。
@app.get("/metrics")
async def metrics():
    """Prometheus 指标端点已禁用——无外部 Prometheus 消费，且无认证暴露 API 路由结构有安全风险。
    如需启用，配置 METRICS_ADDR 并添加认证后取消下方注释。
    """
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "metrics endpoint disabled"}, status_code=404)

def _check_and_sync_templates() -> bool:
    """检查 project-templates.json + examples/ 是否存在，缺失则自动从 grape-template 仓库拉取。

    防止 git merge/pull 后模板文件被删除导致模板页变空（commit 838f2c98 事故）。
    调用 scripts/sync_templates.sh 执行实际同步。
    """
    import os as _os
    from src.kernel.config import log

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
            encoding="utf-8", errors="replace",  # Windows 宿主：控制台输出可能是 GBK，固定 utf-8+replace 防 reader 线程解码崩溃
            # D17 扫雷四轮：尊重外部已设 TEMPLATES_REPO（原写死覆盖导致内网镜像配置失效）
            env={**_os.environ, "TEMPLATES_REPO": _os.environ.get("TEMPLATES_REPO")
                 or "https://gitcode.com/grape-dev/grape-template.git"}
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

def _start_vcs_exists_backfill_task():
    """Issue #995：存量 issue.vcs_exists 异步回填（幂等，启动后延迟执行一次）。

    不阻塞启动；失败仅告警，列表请求仍按 fail-open 语义工作。
    """
    from src.kernel.config import log

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

im_engine = None  # 新引擎运行时句柄（2026-11-10 起 IM 只有这一条路）

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

metrics_middleware(app)

if metrics_enabled():
    start_metrics_server()

install_request_middleware(app)

install_health_endpoints(app, service_name="server")


install_spa_routes(app)

init_database()

_load_accounts()

install_exception_handlers(app)
