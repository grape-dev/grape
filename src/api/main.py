"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from src.api.routes import auth, grape_auth, repos, dashboard, agents, events, chat, settings, skills, search, pipeline, webhook, scenes, templates, tasks, runtime, feishu_onboard, feishu_card, task_logs, knowledge, health, devstage, latency, issues, feedback, autopilot, orgs, community


bot_manager = None


def _startup_security_check():
    """生产环境启动安全门禁。

    校验 JWT 密钥安全性、密钥轮换配置、邮件服务配置。
    开发环境跳过所有检查。
    """
    from src.config import log
    from src.auth.security_config import is_production, get_jwt_kid_current, get_jwt_secret_keys, get_credential_keys

    if not is_production():
        return  # 开发环境跳过

    errors = []

    # 1. JWT 密钥黑名单校验
    from src.api.jwt_utils import validate_jwt_secret
    if not validate_jwt_secret():
        errors.append("JWT_SECRET 不安全：命中黑名单，请设置自定义密钥")

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
            with open(email_json_path) as _f:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_manager
    from src.config import log

    # 生产环境启动安全门禁
    try:
        _startup_security_check()
    except SystemExit as e:
        raise

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
    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")

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

    # 官方场景定时同步：每 30 分钟从 grape-skills 仓库同步场景到 DB
    try:
        from src.storage.scene_sync import start_scene_sync_scheduler
        start_scene_sync_scheduler()
    except Exception as e:
        log.warning(f"Scene sync scheduler start failed: {e}")

    # 临时上传文件定时清理：每小时清理超过 24 小时的孤儿文件（Issue-54）
    try:
        from src.storage.upload_cleanup import start_upload_cleanup_scheduler
        start_upload_cleanup_scheduler()
    except Exception as e:
        log.warning(f"Upload cleanup scheduler start failed: {e}")

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

    # 主控机指标上报：每 15 秒采集本机 CPU/内存/磁盘/负载写入 DB
    _server_metrics_task = None
    try:
        async def _server_metrics_loop():
            import socket as _sock, hashlib as _hl
            from datetime import datetime, timezone
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
        _server_metrics_task = asyncio.create_task(_server_metrics_loop())
        log.info("Server metrics loop started (15s)")
    except Exception as e:
        log.warning(f"Server metrics loop start failed: {e}")

    # Issue #85：引擎健康度定时刷新——每 5 分钟对所有引擎 force_refresh，
    # 覆盖离线引擎无任务事件触发的场景（失联引擎不会产生任务完成事件）
    try:
        from src.runtime.health_scorer import start_health_refresh_scheduler
        start_health_refresh_scheduler()
    except Exception as e:
        log.warning(f"Health refresh scheduler start failed: {e}")

    # 用户活跃状态追踪：每 30 秒批量更新 last_active_at
    _active_tracker_task = None
    try:
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
                # Issue-45 M2：会话级临时 token TTL 兜底清理（每 5 分钟）
                try:
                    _purge_i += 1
                    if _purge_i >= 10:
                        _purge_i = 0
                        from src.api.auth_token import purge_expired_session_tokens
                        await asyncio.to_thread(purge_expired_session_tokens)
                        # 事件活跃守卫 TTL 兜底清理（进程崩溃未终态释放时自愈）
                        from src.api.event_store import purge_stale_event_guards
                        await asyncio.to_thread(purge_stale_event_guards)
                except Exception as e:
                    log.warning(f"Session token purge failed: {e}")

        _active_tracker_task = asyncio.create_task(_active_user_tracker())
        log.info("Active user tracker started (30s flush)")
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
    # 官方场景同步：优雅关闭定时任务
    try:
        from src.storage.scene_sync import stop_scene_sync_scheduler
        await stop_scene_sync_scheduler()
    except Exception:
        pass
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
    version="2.1.0",
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


def _is_loopback(request: Request) -> bool:
    """判断请求是否来自内网（127.0.0.1 / ::1）。"""
    client = request.client
    if client and client.host in ("127.0.0.1", "::1", "localhost"):
        return True
    # 信任反代 X-Forwarded-For（nginx 加 X-Real-IP）
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first in ("127.0.0.1", "::1"):
            return True
    xri = request.headers.get("x-real-ip", "")
    if xri in ("127.0.0.1", "::1"):
        return True
    return False

# 可观察性：HTTP 请求指标中间件（仅 METRICS_ADDR 启用时挂载）
from src.observability import metrics_middleware, start_metrics_server, metrics_endpoint, metrics_enabled
metrics_middleware(app)
if metrics_enabled():
    start_metrics_server()

# 可观察性：request_id + HTTP 请求日志合并中间件（参考 multica middleware/request_logger.go）
# request_id：注入 X-Request-ID + ContextVar，贯穿日志 + 响应头
# 请求日志：结构化记录 method/path/status/duration/request_id，按状态码分级
import uuid as _uuid
import time as _time
from src.config import request_id_var, repo_var, user_var, log as _log


# webhook 路径中的 token 需要 redact（参考 multica redactWebhookPath）
_WEBHOOK_PREFIX = "/webhook/"


def _redact_path(path: str) -> str:
    """webhook 路径中的 token 段替换为 [redacted]。"""
    if not path.startswith(_WEBHOOK_PREFIX):
        return path
    rest = path[len(_WEBHOOK_PREFIX):]
    if "/" in rest:
        return _WEBHOOK_PREFIX + "[redacted]" + rest[rest.index("/"):]
    return _WEBHOOK_PREFIX + "[redacted]"


# 健康探针路径不记请求日志（参考 multica isHealthProbePath）
_LOG_SKIP_PATHS = frozenset({"/health", "/healthz", "/readyz", "/metrics"})
# Worker 轮询路径跳过请求日志（13 台 worker × 3s × 3 请求 = ~24 行/秒, 膨胀日志）
_LOG_SKIP_PREFIXES = ("/api/runtime/runtimes/", "/api/runtime/heartbeat", "/api/runtime/tasks/", "/api/ping")


@app.middleware("http")
async def request_id_and_log_middleware(request: Request, call_next):
    """request_id 注入 + HTTP 请求日志（合并为一个中间件，确保 ContextVar 贯穿日志）。

    按状态码分级：5xx → ERROR, 4xx → WARN, 2xx/3xx → INFO。
    健康探针路径跳过请求日志（但 request_id 仍注入）。
    """
    rid = request.headers.get("X-Request-ID") or _uuid.uuid4().hex[:12]
    request.scope["request_id"] = rid
    token_rid = request_id_var.set(rid)

    # 从请求路径提取 repo_key（如 /api/dashboard/requiredsa/hello-flask → requiredsa/hello-flask）
    path = request.url.path
    _repo = ""
    # 从请求路径提取 repo_key，不同模块路径结构不同：
    # /api/dashboard/{owner}/{repo}          → owner=parts[2], repo=parts[3]
    # /api/pipeline/{owner}/{repo}           → 同上
    # /api/agents/{agent_type}/{owner}/{repo} → owner=parts[3], repo=parts[4]
    _path_parts = path.strip("/").split("/")
    if len(_path_parts) >= 4 and _path_parts[0] == "api":
        _module = _path_parts[1]
        # agents 路由多一层 agent_type
        if _module == "agents" and len(_path_parts) >= 5:
            _owner = _path_parts[3]
            _repo_name = _path_parts[4]
            if "/" not in _owner and "." not in _owner:
                _repo = f"gitcode:{_owner}/{_repo_name}"
        elif _module in {"dashboard", "pipeline"} and len(_path_parts) >= 4:
            _owner = _path_parts[2]
            _repo_name = _path_parts[3]
            if "/" not in _owner and "." not in _owner:
                _repo = f"gitcode:{_owner}/{_repo_name}"
    # query param 中的 repo（如 ?repo=requiredsa/hello-flask）
    if not _repo:
        _qp = request.query_params.get("repo", "")
        if _qp:
            _repo = f"gitcode:{_qp}" if not _qp.startswith("gitcode:") else _qp
    token_repo = repo_var.set(_repo)

    # 从 Authorization header 提取 user_id
    _uid = ""
    _auth = request.headers.get("Authorization", "")
    if _auth.startswith("Bearer "):
        try:
            from src.user.db import validate_token
            _uid_val = validate_token(_auth[7:])
            if _uid_val:
                _uid = str(_uid_val)
        except Exception:
            pass
    token_uid = user_var.set(_uid)

    skip_log = path in _LOG_SKIP_PATHS or path.startswith(_LOG_SKIP_PREFIXES)
    start = _time.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:
        raise
    finally:
        if not skip_log:
            duration = (_time.time() - start) * 1000
            redacted_path = _redact_path(path)
            # 结构化字段通过 extra 注入，作为独立 JSON 字段输出（非 message 文本）
            extra = {
                "method": request.method,
                "path": redacted_path,
                "status": status,
                "duration_ms": round(duration),
            }
            msg = f"{request.method} {redacted_path} status={status} duration={duration:.0f}ms"
            if status >= 500:
                _log.error(msg, extra=extra)
            elif status == 401:
                # 401 降级：前端 token 过期刷新是正常行为不记 WARNING；
                # 但 runtime/worker 凭据失败保留 WARNING（运维需感知 worker 掉线）
                if path.startswith("/api/runtime/"):
                    _log.warning(msg, extra=extra)
                else:
                    _log.debug(msg, extra=extra)
            elif status in (403, 404):
                # 403/404 降级：health 外网限制、favicon、SPA 路由回退等正常行为，非错误
                _log.debug(msg, extra=extra)
            elif status >= 400:
                _log.warning(msg, extra=extra)
            else:
                _log.info(msg, extra=extra)
            # 慢请求入库（方案C）
            from src.config import SLOW_REQUEST_THRESHOLD_MS
            if duration >= SLOW_REQUEST_THRESHOLD_MS:
                try:
                    from src.session_manager import db as _sdb, db_lock as _sdbl
                    with _sdbl:
                        _sdb.execute(
                            "INSERT INTO api_latency_log (request_id, method, path, status, duration_ms, repo_key, user_id, source) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 'backend')",
                            (rid, request.method, redacted_path, status, round(duration), _repo, _uid)
                        )
                        _sdb.commit()
                except Exception:
                    pass  # 入库失败不影响请求
        request_id_var.reset(token_rid)
        repo_var.reset(token_repo)
        user_var.reset(token_uid)

@app.get("/api/ping", include_in_schema=False)
async def api_ping():
    """前端会话保活 / Failed to fetch 恢复预检的轻量探针（无鉴权、不查 DB、浏览器可达）。

    - `/health` 仅 loopback（nginx 外网 403），浏览器用不了；
    - 本端点对任何来源返回 200，零 IO，供 heartbeat 60s 与 unhandledrejection 恢复预检。
    """
    return {"ok": True, "ts": _dt.now(_tz.utc).isoformat()}

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

# ===== Issue-45: OpenAPI 定制渲染（securitySchemes + 仅对外安全路由进契约）=====
from src.api.openapi import custom_openapi
app.openapi = lambda: custom_openapi(app)
# Issue 附件静态服务（Task Center 上传的文件）
# 注意：必须在 /static 之前 mount，否则 /static 会拦截 /static/issue_attachments 请求
# 路径与 tasks.py _attach_base_dir 对齐（基于 WORKSPACE_DIR）
from src.config import WORKSPACE_DIR as _WS_DIR
_ISSUE_ATTACH_DIR = os.path.join(_WS_DIR, "..", "data", "issue_attachments")
os.makedirs(_ISSUE_ATTACH_DIR, exist_ok=True)
app.mount("/static/issue_attachments", StaticFiles(directory=_ISSUE_ATTACH_DIR), name="issue_attachments")

# Community uploads (images for markdown posts)
_COMMUNITY_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "community_uploads")
os.makedirs(_COMMUNITY_UPLOAD_DIR, exist_ok=True)
app.mount("/static/community_uploads", StaticFiles(directory=_COMMUNITY_UPLOAD_DIR), name="community_uploads")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if os.path.isdir(STATIC_DIR):
    # Static assets (JS/CSS): allow browser caching with ETag revalidation.
    # ?v= bump is the primary cache-buster; must-revalidate ensures stale files are always checked.
    # index.html itself stays no-store (see _spa_response below) so the browser always fetches the
    # latest HTML, which references the correct ?v= for each asset.
    from starlette.middleware.base import BaseHTTPMiddleware
    class NoCacheStaticMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            resp = await call_next(request)
            if request.url.path.startswith("/static/"):
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp
    app.add_middleware(NoCacheStaticMiddleware)

    from fastapi.responses import FileResponse
    def _spa_response():
        resp = FileResponse(os.path.join(STATIC_DIR, "index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.get("/")
    async def serve_spa():
        return _spa_response()

    @app.get("/register")
    async def serve_register():
        """注册页面 — 独立 HTML，不加载主应用 JS。"""
        return FileResponse(os.path.join(STATIC_DIR, "register.html"),
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/forgot")
    async def serve_forgot():
        """忘记密码页面 — 独立 HTML，不加载主应用 JS。"""
        return FileResponse(os.path.join(STATIC_DIR, "forgot.html"),
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/en")
    @app.get("/en/{path:path}")
    async def serve_spa_en(path: str = ""):
        """英文国际化路由——返回同一个 index.html，前端 i18n.js 从 URL /en 检测语言。"""
        if path.startswith("api/") or path.startswith("static/"):
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        # 认证页面独立 HTML（不是 SPA view）
        if path == "register":
            return FileResponse(os.path.join(STATIC_DIR, "register.html"),
                                headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        if path == "forgot":
            return FileResponse(os.path.join(STATIC_DIR, "forgot.html"),
                                headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        return _spa_response()

    # 注意：SPA catch-all (/{path:path}) 延迟到文件末尾注册，避免拦截 /health /healthz 等系统端点

from src.api.db_setup import init_database
init_database()

from src.config import _load_accounts
_load_accounts()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from src.config import log
    log.warning(f"API error: {exc}")
    rid = request.scope.get("request_id", "-")
    return JSONResponse(status_code=500, content={"code": 500, "message": str(exc), "request_id": rid})


# ===== 三层健康检查（参考 multica health.go）=====

import os as _os_mod
from datetime import datetime as _dt, timezone as _tz

_PROCESS_STARTED_AT = _dt.now(_tz.utc).isoformat()


@app.get("/health")
async def health(request: Request):
    """Liveness：进程存活，不检查 DB。

    K8s livenessProbe 用此端点。DB 挂了不应导致 pod 重启，只标记 NotReady。
    仅允许内网访问（外网通过 nginx 限制或返回 403）。
    """
    if not _is_loopback(request):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    return {
        "status": "ok",
        "version": "2.0.0",
        "pid": _os_mod.getpid(),
        "started_at": _PROCESS_STARTED_AT,
    }


# /healthz 3s TTL 缓存（参考 multica readinessCacheTTL）
_readiness_cache = {"result": None, "expires_at": 0}
_READINESS_CACHE_TTL = 3  # 秒


@app.get("/healthz")
@app.get("/readyz")
async def readiness(request: Request):
    """Readiness：检查 DB 连通 + 表结构完整性，3s 缓存。

    K8s readinessProbe / startupProbe 用此端点。
    仅允许内网访问。
    """
    if not _is_loopback(request):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    import time as _t
    now = _t.time()
    cached = _readiness_cache["result"]
    if cached and now < _readiness_cache["expires_at"]:
        return cached

    from src.session_manager import db, db_lock
    checks = {"db": "ok", "tables": "ok"}
    status = "ok"
    http_status = 200
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1")
            c.fetchone()
    except Exception as e:
        checks["db"] = f"error: {e}"
        checks["tables"] = "unknown"
        status = "not_ready"
        http_status = 503
    else:
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT COUNT(*) FROM agent_tasks")
                c.fetchone()
        except Exception:
            checks["tables"] = "error: agent_tasks missing"
            status = "not_ready"
            http_status = 503
    # 模板检查：project-templates.json 存在且非空
    import os as _os_mod2
    _json_path = _os_mod2.path.join(
        _os_mod2.path.dirname(_os_mod2.path.dirname(_os_mod2.path.dirname(_os_mod2.path.abspath(__file__)))),
        "configs", "project-templates.json")
    if _os_mod2.path.isfile(_json_path) and _os_mod2.path.getsize(_json_path) > 100:
        checks["templates"] = "ok"
    else:
        checks["templates"] = "missing: project-templates.json"
        # 不阻断 readiness（模板缺失不影响核心功能，但标记告警）
        if status == "ok":
            status = "degraded"
    # P1-1 修复: /readyz 检查 D32 迁移状态
    # 迁移在 lifespan yield 前同步完成，但如果迁移异常退出，服务仍会启动
    _d32_failed = getattr(app, "state", None)
    if _d32_failed and getattr(_d32_failed, "d32_migration_failed", False):
        checks["migration"] = "error: D32 migration failed"
        status = "not_ready"
        http_status = 503
    else:
        checks["migration"] = "ok"
    resp = JSONResponse(
        status_code=http_status,
        content={"status": status, "checks": checks},
    )
    _readiness_cache["result"] = resp
    _readiness_cache["expires_at"] = now + _READINESS_CACHE_TTL
    return resp


@app.get("/health/realtime")
async def health_realtime(request: Request):
    """运维诊断：暴露 IM bot 状态、任务队列深度、runtime 在线数。

    参考 multica /health/realtime（health_realtime.go）。
    仅允许内网访问——外网不暴露运维细节。
    """
    if not _is_loopback(request):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    from src.session_manager import db, db_lock
    from src.runtime.health import derive_runtime_health
    import time as _time, os as _os

    now_ts = _time.time()
    result = {
        "status": "ok",
        "version": "2.0.0",
        "pid": _os.getpid(),
    }

    # IM bot 状态
    if bot_manager:
        result["im"] = bot_manager.health_check()

    # 任务队列实时快照
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, COUNT(*) FROM agent_tasks GROUP BY status")
            result["tasks"] = {r[0]: r[1] for r in c.fetchall()}

            c.execute("SELECT COUNT(*) FROM agent_tasks WHERE status IN ('dispatched','running')")
            result["in_progress"] = c.fetchone()[0]

            c.execute("SELECT status, last_seen_at, credential_id FROM agent_runtimes")
            online = 0
            total = 0
            for r in c.fetchall():
                total += 1
                h = derive_runtime_health(r[0] or "offline", r[1], now_ts)
                if h == "online":
                    online += 1
            result["runtimes"] = {"total": total, "online": online}
    except Exception as e:
        result["db_error"] = str(e)

    return result


@app.get("/metrics")
async def metrics():
    """Prometheus 指标端点已禁用——无外部 Prometheus 消费，且无认证暴露 API 路由结构有安全风险。
    如需启用，配置 METRICS_ADDR 并添加认证后取消下方注释。
    """
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "metrics endpoint disabled"}, status_code=404)


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
# SPA catch-all：非 API/static 的路径（如 /login）回退到 index.html
# 必须放在所有 API 路由之后，否则会拦截 /health /healthz 等端点

@app.get("/admin", include_in_schema=False)
@app.get("/admin/{path:path}", include_in_schema=False)
async def serve_admin(path: str = ""):
    """Admin 独立页面：返回 src/api/static/admin.html（frontend 单页）。"""
    from fastapi.responses import FileResponse
    if path.startswith("api/") or path.startswith("static/"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    _admin_html = os.path.join(STATIC_DIR, "admin.html")
    if os.path.isfile(_admin_html):
        resp = FileResponse(_admin_html)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    raise HTTPException(status_code=404, detail="admin.html not found")

@app.get("/account", include_in_schema=False)
@app.get("/account/{path:path}", include_in_schema=False)
async def serve_account_redirect():
    """旧用户中心 URL 302 重定向到内嵌 SPA view。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/#account", status_code=302)

@app.get("/oauth-popup-callback.html", include_in_schema=False)
async def serve_oauth_popup_callback():
    """OAuth 弹窗回调中间页——极简 HTML，通过 postMessage 通知父窗口后关闭。"""
    from fastapi.responses import FileResponse
    resp = FileResponse(os.path.join(STATIC_DIR, "oauth-popup-callback.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp

@app.get("/{path:path}")
async def spa_catch_all(path: str):
    if path.startswith("api/") or path.startswith("static/"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    return _spa_response()
