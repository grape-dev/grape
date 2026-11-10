"""Runtime API 路由。

M2：任务状态轮询 / 取消（用户 token）。
M3：/api/runtime/* 协议端点（register/heartbeat/claim/report），仅 rta_ 凭据可过（D2 凭据隔离）。
见 agent-runtime-event-full-design.md §6 / tech-impl.md §3。
"""
import hashlib, json, os, threading
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Depends, Request, Query
from pydantic import BaseModel, model_validator
from src.api.deps import require_no_session_token, require_org_member
from src.config import log
from src.api.time_utils import normalize_event_time
from src.session_manager import db, db_lock
from src.runtime import service
from src.runtime.health import apply_runtime_state, parse_dt  # Issue #1496 统一写入口 + 心跳解析
from src.runtime import health as _health_mod  # noqa: F401  (_STALE_SECONDS 经 _RUNTIME_STALE 下发)

router = APIRouter(prefix="/api/runtime", tags=["runtime"])

# Worker 包下载并发限制（防止多 worker 同时下载占满带宽）
_PKG_DOWNLOAD_LIMIT = int(os.environ.get("PKG_DOWNLOAD_MAX_CONCURRENT", "5"))
_pkg_download_count = 0
_pkg_download_lock = threading.Lock()


# ===== rta_ 凭据认证中间件（M3，D2 凭据隔离）=====
def _auth_credential(request: Request) -> int:
    """解析 Authorization: Bearer rta_... → sha256 → 查 runtime_credentials(status=active)。返回 credential_id。"""
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not token.startswith("rta_"):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "invalid runtime credential"})
    h = hashlib.sha256(token.encode()).hexdigest()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM runtime_credentials WHERE token_hash=? AND status='active'", (h,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "runtime credential revoked or not found"})
    return row[0]


def _client_ip(request: Request) -> str:
    """解析来源 IP（经 nginx 反代优先 X-Forwarded-For；仅展示用，不入唯一键，见 multi-runtime-instance-design §3.2）。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


class RuntimeRegBody(BaseModel):
    device_name: str = ""
    cli_version: str = ""
    runtimes: list[dict] = []
    local_skills: list[dict] = []


class RuntimeInfo(BaseModel):
    name: str
    version: str = ""
    healthy: bool = True
    role_tags: list[str] = []  # Issue #658：引擎显式角色声明（如 ["coding","issue"]；空=按引擎类型默认映射）
    model: str = ""  # Issue #865：配置模型名（worker 本机配置解析上报；空=未上报/解析失败，不影响注册）


class LogLineBody(BaseModel):
    ts: str = ""
    level: str = ""
    message: str = ""


class HeartbeatBody(BaseModel):
    runtime_ids: list[int] = []
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    disk_percent: float = 0.0
    tmp_percent: float = 0.0
    bandwidth_percent: float = 0.0
    bandwidth_mbps: float = 0.0
    load_avg: str = ""
    spec: str = ""
    log_lines: list[LogLineBody] = []
    probe_results: dict = {}  # {engine_name: {status, latency, last_probe, error}}


class TaskStartBody(BaseModel):
    pass


class ProgressBody(BaseModel):
    summary: str = ""
    step: int = 0
    total: int = 0


class MessagesBody(BaseModel):
    messages: list = []


class UsageBody(BaseModel):
    """LLM 用量上报。

    字段语义(设计文档 §3.2):一个任务可多轮、多模型 → 每次上报是一条记录,
    **追加**落 obs_llm_usage,不再覆盖 agent_tasks.usage_json(会话续接指针)。
    """
    provider: str = ""
    model: str = ""
    input: int = 0
    output: int = 0
    cache: int = 0            # 遗留字段,等同 cache_read(历史无调用方,保留仅为兼容)
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0        # codex 的 reasoning_output_tokens
    turn: int = 0             # 同一任务第几轮(多轮累加的基础)
    cost_usd: float | None = None
    cost_status: str = ""
    source: str = ""          # 采集来源:report/codex/opencode/hermes/hwcloud(:辅助任务)


class CompleteBody(BaseModel):
    result_text: str = ""
    exit_code: int = 0
    session_id: str = ""
    work_dir: str = ""
    engine: str = ""
    run_token: str = ""  # Step 4：认领时刻 started_at，补报守卫


class FailBody(BaseModel):
    error: str = ""
    failure_reason: str = ""
    exit_code: int = -1
    run_token: str = ""  # Step 4：认领时刻 started_at，补报守卫


# ===== 认证中间件 =====

def _require_credential(request: Request) -> int:
    return _auth_credential(request)


# ===== 协议端点（M3）=====

# Issue #700：register healthy-guard 探活新鲜窗口解析（env 校验）
def _resolve_probe_fresh_window() -> int:
    """解析 register healthy-guard 探活新鲜窗口（秒）。

    默认 2×RUNTIME_PROBE_INTERVAL（interval 默认 600s → 窗口 1200s）；
    RUNTIME_PROBE_FRESH_WINDOW 可显式覆盖。非法值（非整数/非正数/超范围）
    回退默认并告警，不抛异常——否则守卫会静默失效（#837 Suggested-1，
    对齐 acp_bootstrap._resolve_acp_check_timeout 模式）。
    """
    _interval_raw = (os.environ.get("RUNTIME_PROBE_INTERVAL") or "").strip()
    _interval = 600
    if _interval_raw:
        try:
            _val = int(_interval_raw)
        except ValueError:
            log.warning(f"RUNTIME_PROBE_INTERVAL 非法值 '{_interval_raw}'（非整数），回退默认 600s")
            _val = 600
        if _val < 1 or _val > 86400:
            log.warning(f"RUNTIME_PROBE_INTERVAL 非法值 '{_interval_raw}'（超出 1~86400 范围），回退默认 600s")
        else:
            _interval = _val
    _default = 2 * _interval
    _raw = (os.environ.get("RUNTIME_PROBE_FRESH_WINDOW") or "").strip()
    if not _raw:
        return _default
    try:
        _window = int(_raw)
    except ValueError:
        log.warning(f"RUNTIME_PROBE_FRESH_WINDOW 非法值 '{_raw}'（非整数），回退默认 {_default}s")
        return _default
    if _window < 1 or _window > 86400:
        log.warning(f"RUNTIME_PROBE_FRESH_WINDOW 非法值 '{_raw}'（超出 1~86400 范围），回退默认 {_default}s")
        return _default
    return _window


def _register_healthy_guard(c, rid: int, reported: bool) -> int:
    """Issue #700：register 降级守卫——已 healthy 且探活新鲜的行不被瞬时探测打回 0。

    震荡根因：worker re-register（每 300s）用 ACP 握手探测（15s 总闸）判
    healthy，Windows 冷启动易超时 → 上报 False；而心跳探活（每 600s）真跑
    ACP 成功后把 healthy 升回 1。两写入源打架导致 healthy 大部分时间为 0。

    守卫规则（仅作用于 UPDATE 分支的 1→0 方向）：
    - DB 行 healthy=1 且 probe_status='ok' 且 probe_last 距今 ≤ 新鲜窗口
      （默认 2×RUNTIME_PROBE_INTERVAL=1200s，env RUNTIME_PROBE_INTERVAL 可调）
      → 保持 1（探活方为更权威的端到端判定，瞬时握手超时不降级）；
    - 探活过期 / probe_status 非 ok / 已不健康 / 上报 True → 不干预
      （真实故障仍由心跳连败路径（probe_fail_count≥2 → 0）正常摘除）。

    INSERT 分支（新引擎首注册）不经此函数——上报值原样生效。
    """
    try:
        row = c.execute(
            "SELECT healthy, probe_status, probe_last FROM agent_runtimes WHERE id=?",
            (rid,)).fetchone()
        if not row:
            return 1 if reported else 0
        cur_healthy, probe_status, probe_last = (row[0] or 0), (row[1] or ""), (row[2] or "")
        if reported or not cur_healthy:
            return 1 if reported else 0
        # 仅剩「当前 healthy=1、上报 False」的降级方向需要守卫
        if (probe_status or "").strip().lower() != "ok":
            return 0
        _window = _resolve_probe_fresh_window()
        try:
            _last = datetime.fromisoformat(probe_last)
            if _last.tzinfo is None:
                _last = _last.replace(tzinfo=timezone.utc)
            fresh = (datetime.now(timezone.utc) - _last).total_seconds() <= _window
        except Exception:
            fresh = False
        if fresh:
            log.info(f"register healthy-guard: runtime={rid} probe ok within {_window}s, "
                     f"keep healthy=1 (transient ACP check downgrade suppressed)")
            return 1
        return 0
    except Exception as _ge:
        # 守卫自身异常绝不阻塞注册主链路（保守：按上报值）
        log.warning(f"register healthy-guard failed (runtime={rid}): {_ge}")
        return 1 if reported else 0


@router.post("/register")
def runtime_register(body: RuntimeRegBody, request: Request, credential_id: int = Depends(_require_credential)):
    """凭据校验 + 按实例键 upsert agent_runtimes（幂等），返回 server 权威 runtime IDs。

    多实例键（multi-runtime-instance-design §3.1）：每台 worker（凭据）对每个引擎独占一行，
    多 worker 注册同名引擎并存，互不覆盖 cli_path/version/credential_id。
    """
    registered = []
    client_ip = _client_ip(request)
    with db_lock:
        c = db.cursor()
        # Issue #417：鉴权（_auth_credential）在 db_lock 外，吊销与注册存在竞态——
        # 鉴权通过后凭据被 revoke 物理删除，此处会以失效 credential_id 重新 INSERT
        # agent_runtimes（已吊销设备被残留 worker 复活）。锁内复核凭据仍存在且 active。
        c.execute("SELECT id FROM runtime_credentials WHERE id=? AND status='active'", (credential_id,))
        if not c.fetchone():
            raise HTTPException(status_code=401, detail={"code": 401, "message": "runtime credential revoked or not found"})
        # 回填 device_name + 来源 IP
        c.execute("UPDATE runtime_credentials SET device_name=?, last_seen_at=?, last_ip=? WHERE id=?",
                  (body.device_name, datetime.now(timezone.utc).isoformat(), client_ip, credential_id))
        # 凭据作用域决定 runtime 归属：user scope 的个人 worker 注册为 user scope + owner_id；
        # repo scope 用凭据行 owner_id（仓库全名），否则 set_chat_config / Engines 视图的
        # scope 判定失效（个人/仓库级引擎永远选不中，见 engine-selection §7.1）
        c.execute("SELECT scope, owner_name, owner_id, repo_key, org_id FROM runtime_credentials WHERE id=?", (credential_id,))
        cred_row = c.fetchone()
        cred_scope = (cred_row[0] if cred_row and cred_row[0] else "global").strip().lower()
        owner_id = (cred_row[2] or "") if cred_row else ""
        cred_repo_key = (cred_row[3] or "") if cred_row else ""
        cred_org_id = (cred_row[4] or 0) if cred_row else 0
        if cred_scope == "user" and cred_row and cred_row[1]:
            # D32-补丁：owner_id 与 grape_accounts.id 对齐（按用户名）
            try:
                c.execute("SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?) LIMIT 1", (cred_row[1],))
                urow = c.fetchone()
                if urow:
                    owner_id = str(urow[0])
            except Exception:
                pass
        # local_skills：per-engine skill 列表，按 engine name 匹配 runtime 行
        skills_by_name = {}
        for ls in body.local_skills or []:
            eng = (ls.get("engine") or "").strip().lower()
            if eng:
                skills_by_name[eng] = json.dumps(ls.get("skills") or [], ensure_ascii=False)
        # Issue #654：注册前先按 (credential_id, lower(name)) 合并历史重复行
        # （保留 MAX(id)，capabilities/tags_user_set 迁移到存活行），
        # 避免 upsert 键收敛后与旧 (scope, owner_id) 唯一约束撞车；映射在
        # commit 后统一 remap 引用 + 输出结构化事件。
        from src.runtime.engine_select import merge_duplicate_engine_rows, remap_engine_references
        _rid_map = merge_duplicate_engine_rows(credential_id, c)
        for rt in body.runtimes or []:
            name = (rt.get("name") or "").strip().lower()
            if not name:
                continue
            _ls_json = skills_by_name.get(name, "[]")
            # Issue #654：upsert 查找键收敛为 (credential_id, lower(name))——
            # scope/owner_id/org_id/repo_key 是凭据派生属性，命中既有行后原地同步，
            # 引擎 id 天然稳定（scope 往返切换不再 INSERT 新行 + id 全量变更）。
            # 多行历史脏数据时取 MAX(id)（与 scope 切换去重「保留最新行」口径一致，
            # 重复行已由上方 merge_duplicate_engine_rows 合并）。
            c.execute("SELECT id, scope, owner_id, repo_key, org_id FROM agent_runtimes "
                      "WHERE credential_id=? AND lower(name)=? ORDER BY id DESC LIMIT 1",
                      (credential_id, name))
            row = c.fetchone()
            # Issue #658：role_tags 显式声明（JSON 数组）；空列表不覆盖已有手工配置
            _role_tags_json = ""
            try:
                _rt = rt.get("role_tags") or []
                if isinstance(_rt, list) and _rt:
                    _role_tags_json = json.dumps([str(t).strip().lower() for t in _rt if str(t).strip()], ensure_ascii=False)
            except Exception:
                _role_tags_json = ""
            # Issue #865：配置模型（worker 上报本机配置解析结果；空=未上报，
            # 不覆盖既有值——未升级 worker 重注册不会抹掉历史/兜底值，同 #658 模式）
            _model_name = str(rt.get("model") or "").strip()[:120]
            if row:
                rid = row[0]
                # Issue #700：降级守卫——已 healthy 且探活新鲜的行不被 worker
                # 瞬时 ACP 握手超时打回 0（UPDATE 的 1→0 方向过守卫）
                # （健康值经 _register_healthy_guard，其余字段保留 #654 role_tags/凭据同步）
                # Issue #1496：status+last_seen_at 改走统一写入口 apply_runtime_state
                # （同事务写，含遗留 health 列派生同步），消除在线态二次写窗口。
                apply_runtime_state(c, rid, "online", datetime.now(timezone.utc).isoformat())
                _upd_sql = ("UPDATE agent_runtimes SET version=?, healthy=?, installed=1, cli_path=?, local_skills=?, worker_version=?"
                            + (", role_tags=?" if _role_tags_json else "")
                            + (", model_name=?, model_source='worker_report', model_reported_at=?" if _model_name else "")
                            + " WHERE id=?")
                # 参数顺序 bug 修复（#865 基线缺陷，2026-11-10 真机实证）：rid 必须是
                # 最后一个参数（对应 WHERE id=?）——旧实现把 rid 固定在元组第 6 位再
                # 追加 role_tags/model 值，一旦带上 model 分支占位符与参数错位，
                # sqlite binding 异常 → 该引擎行整个 UPDATE 失败（表现为「检测到
                # 模型的引擎 worker_version 卡旧版且 model_name 恒空」；INSERT 新行
                # 路径参数固定无误，故首台远程 Worker 首注册侥幸成功）。
                _upd_args = [rt.get("version", ""), _register_healthy_guard(c, rid, bool(rt.get("healthy", True))),
                             rt.get("cli_path", ""), _ls_json, body.cli_version]
                if _role_tags_json:
                    _upd_args.append(_role_tags_json)
                if _model_name:
                    _upd_args += [_model_name, datetime.now(timezone.utc).isoformat()]
                _upd_args.append(rid)
                c.execute(_upd_sql, _upd_args)
                # Issue #654：凭据派生属性原地同步（纵深防御：即使 scope 切换路径
                # 漏改，注册也会把引擎行拉回与凭据一致），变化时输出结构化事件。
                if ((row[1] or "") != cred_scope or (row[2] or "") != owner_id
                        or (row[3] or "") != (cred_repo_key or "")
                        or (row[4] or 0) != cred_org_id):
                    c.execute("UPDATE agent_runtimes SET scope=?, owner_id=?, repo_key=?, org_id=? WHERE id=?",
                              (cred_scope, owner_id, cred_repo_key, cred_org_id, rid))
                    _ev = {"event": "engine_scope_synced", "credential_id": credential_id,
                           "runtime_id": rid, "engine": name,
                           "old": {"scope": row[1] or "", "owner_id": row[2] or "",
                                   "repo_key": row[3] or "", "org_id": row[4] or 0},
                           "new": {"scope": cred_scope, "owner_id": owner_id,
                                   "repo_key": cred_repo_key, "org_id": cred_org_id}}
                    log.info("engine_change_event: " + json.dumps(_ev, ensure_ascii=False))
            else:
                c.execute(
                    "INSERT INTO agent_runtimes (credential_id, name, display_name, `binary`, version, healthy, installed, status, last_seen_at, cli_path, scope, owner_id, local_skills, repo_key, worker_version, org_id, role_tags, model_name, model_source, model_reported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (credential_id, name, rt.get("display_name", name), name, rt.get("version", ""),
                     1 if rt.get("healthy", True) else 0, 1, "online",
                     datetime.now(timezone.utc).isoformat(), rt.get("cli_path", ""), cred_scope, owner_id, _ls_json, cred_repo_key, body.cli_version, cred_org_id, _role_tags_json,
                     _model_name, "worker_report" if _model_name else "", datetime.now(timezone.utc).isoformat() if _model_name else ""))
                rid = c.lastrowid
            # 自动写入默认 "all" 标签——新引擎默认可认领任意角色任务。
            # 若已有 capabilities 行则跳过（用户手动配置过标签的不覆盖）。
            # tags_user_set=1 时也跳过（用户已手动清空/设过标签，心跳不再自动补 'all'）
            c.execute("SELECT tags_user_set FROM agent_runtimes WHERE id=?", (rid,))
            _ts_row = c.fetchone()
            _user_set = (_ts_row[0] if _ts_row else 0) or 0
            c.execute("SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1", (rid,))
            if not c.fetchone() and not _user_set:
                from datetime import datetime as _dt, timezone as _tz
                _now_ec = _dt.now(_tz.utc).isoformat()
                c.execute(
                    "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                    "VALUES (?, 'all', 1, 50, 1, ?, ?)",
                    (rid, _now_ec, _now_ec))
            # capabilities 写入后同步 agent_tags 派生缓存（注册路径原先漏了这一步，
            # 导致新机器 agent_tags 为空、前端按 tags 过滤时被漏掉——sre-personal-machine 修复）
            from src.api.routes.settings import _sync_agent_tags
            _sync_agent_tags(rid, c)
            registered.append({"id": rid, "name": name})
        db.commit()
    # Issue #654：引擎行替换（历史重复行合并）后 remap 引用 + 结构化事件（锁外执行）
    if _rid_map:
        try:
            remap_engine_references(_rid_map, credential_id, reason="register_merge")
        except Exception as _e:
            log.warning(f"runtime_register: remap references failed: {_e}")
    # v3.0：probe_url——启动可达性探测（复用任一已转存技能对象签名，
    # 验证完整链路：网络+签名+读取；空库返回空，worker 声明能力 false）
    probe_url = ""
    try:
        from src.storage import get_storage
        _storage = get_storage()
        if _storage is not None:
            with db_lock:
                _c = db.cursor()
                _row = _c.execute(
                    "SELECT file_path FROM skill WHERE file_path != '' AND is_active=1 LIMIT 1").fetchone()
            if (_row and _row[0]):
                probe_url = _storage.temporary_url(_row[0], expires=int(os.environ.get("RUNTIME_PROBE_URL_TTL", "300")))
    except Exception:
        pass
    return {"runtimes": registered, "server_url": _server_url(), "probe_url": probe_url}


@router.post("/heartbeat")
def runtime_heartbeat(body: HeartbeatBody, request: Request, credential_id: int = Depends(_require_credential)):
    """更新 runtime 在线态 / last_seen_at。

    归属校验：只允许更新当前凭据名下的 runtime（credential_id 匹配），
    防止已吊销设备的旧 runtime id 被其它 active 凭据的心跳刷回 online
    （revoke 物理删除 agent_runtimes 行；无校验则吊销的设备行会被残留 agent 复活）。

    响应携带 cancelled_tasks 列表——worker 收到后立即杀引擎进程，
    实现任务取消实时感知（延迟 15 秒，覆盖 step 很长时 report_messages 410 不触发的场景）。
    """
    now = datetime.now(timezone.utc).isoformat()
    client_ip = _client_ip(request)
    cancelled_tasks = []
    with db_lock:
        c = db.cursor()
        # spec 为空时不覆盖（兼容旧版 worker 不上报 spec）
        if body.spec:
            c.execute("UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, memory_percent=?, disk_percent=?, tmp_percent=?, bandwidth_percent=?, bandwidth_mbps=?, load_avg=?, spec=? WHERE id=?",
                      (now, client_ip, body.cpu_percent, body.memory_percent, body.disk_percent, body.tmp_percent, body.bandwidth_percent, body.bandwidth_mbps, body.load_avg, body.spec, credential_id))
        else:
            c.execute("UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, memory_percent=?, disk_percent=?, tmp_percent=?, bandwidth_percent=?, bandwidth_mbps=?, load_avg=? WHERE id=?",
                      (now, client_ip, body.cpu_percent, body.memory_percent, body.disk_percent, body.tmp_percent, body.bandwidth_percent, body.bandwidth_mbps, body.load_avg, credential_id))
        if body.runtime_ids:
            ph = ",".join("?" for _ in body.runtime_ids)
            # Issue #1496：在线态统一写入口（status+last_seen_at 同事务，含遗留
            # health 列派生同步）。本机直连引擎（credential_id=0）不受远端心跳
            # 管辖（不做在线判定，active/online 视图另有口径），只同步其
            # last_seen_at 观测时间，不翻转 status（保住 #1487 的认领侧豁免语义）。
            # 远端引擎：仅 status='online' 的行刷新心跳；status≠online（已被
            # reap/revoke 置 offline）不得被旧 worker 兜底路径"复活"——对齐
            # derive_runtime_health / engine_select._health_ok 的唯一真源口径。
            c.execute(f"SELECT id, status, credential_id FROM agent_runtimes WHERE id IN ({ph})",
                      body.runtime_ids)
            _stale_cut = datetime.now(timezone.utc).timestamp() - _health_mod._STALE_SECONDS
            for _rid, _st, _cid in c.fetchall():
                if _cid != credential_id:
                    continue  # 归属校验：只允许刷新当前凭据名下 runtime
                _fresh = False
                _cur = c.execute("SELECT last_seen_at FROM agent_runtimes WHERE id=?", (_rid,)).fetchone()
                if _cur and _cur[0]:
                    _ts = parse_dt(_cur[0])
                    _fresh = _ts is not None and _ts >= _stale_cut
                if (_st or "") == "online" or _fresh:
                    apply_runtime_state(c, _rid, "online", now)
            # worker 上报的引擎探测结果写入 DB（probe_* 列已由 db_setup.py 自动迁移添加）
            # Issue #490：按 last_probe 幂等去重——同一观测（相同 last_probe）重放只刷新状态，
            # 不递增/重置 probe_fail_count、不动 healthy，避免心跳重放导致计数虚增与 Runtime 误摘除。
            if body.probe_results:
                for _pname, _pdata in body.probe_results.items():
                    _pstatus = (_pdata.get("status") or "pending") if isinstance(_pdata, dict) else "pending"
                    _platency = (_pdata.get("latency") or 0) if isinstance(_pdata, dict) else 0
                    _plast = (_pdata.get("last_probe") or now) if isinstance(_pdata, dict) else now
                    _perror = (_pdata.get("error") or "") if isinstance(_pdata, dict) else ""
                    # 取已存 probe_last：相同观测重放 → 幂等（仅刷新字段，不动计数/健康）
                    _existing_last = c.execute(
                        "SELECT probe_last FROM agent_runtimes WHERE name=? AND credential_id=?",
                        (_pname, credential_id)).fetchone()
                    _existing_last = (_existing_last[0] if _existing_last else "") or ""
                    if _plast and _plast == _existing_last:
                        # 相同观测重放：只刷新 probe_* 字段（值相同，幂等），不触碰 probe_fail_count / healthy
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=? "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
                        continue
                    # 新观测（last_probe 不同 / 旧 Worker 无 last_probe 回退 now 每次不同）：
                    # probe_status 联动 healthy：ok→升级（纠正 --version 误判），fail→降级（实际不能干活）
                    # skip/pending/refused 不动 healthy，保留 --version 的结果
                    if _pstatus == "ok":
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=?, "
                            "probe_fail_count=0, healthy=1 "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
                    elif _pstatus == "fail":
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=?, "
                            "probe_fail_count=probe_fail_count+1, "
                            "healthy=CASE WHEN probe_fail_count+1>=2 THEN 0 ELSE healthy END "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
                    else:
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=? "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
            # 查该 worker 的已取消任务——通知 worker 立即杀进程
            ph2 = ",".join("?" for _ in body.runtime_ids)
            rows = c.execute(
                f"SELECT id FROM agent_tasks WHERE runtime_id IN ({ph2}) AND status='cancelled'",
                body.runtime_ids
            ).fetchall()
            cancelled_tasks = [r[0] for r in rows]

            # busy_slots 对账（2026-08-29 修复）：
            # 原代码只在 claim 成功时对账，但 claim 不到任务时永远不对账 → 泄漏永久不修复。
            # 在心跳中定期对账：busy_slots 与实际 running/dispatched 任务数不一致时重置。
            try:
                for _rid in body.runtime_ids:
                    c.execute(
                        "SELECT COUNT(*) FROM agent_tasks "
                        "WHERE runtime_id=? AND status IN ('dispatched','running')",
                        (_rid,))
                    _actual = c.fetchone()[0] or 0
                    c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (_rid,))
                    _busy = c.fetchone()[0] or 0
                    if _busy != _actual:
                        log.info(f"heartbeat busy_slots reconcile: runtime={_rid} "
                                 f"busy={_busy} actual={_actual} -> reset")
                        c.execute("UPDATE agent_runtimes SET busy_slots=? WHERE id=?",
                                  (_actual, _rid))
            except Exception as _bre:
                log.warning(f"heartbeat busy_slots reconcile failed: {_bre}")
        db.commit()

    # 写入 worker 上报的日志到 data/logs/workers/{credential_id}.log
    if body.log_lines:
        import os as _os, json as _json
        from src.config import LOG_DIR
        _worker_log_dir = _os.path.join(LOG_DIR, "workers")
        _os.makedirs(_worker_log_dir, exist_ok=True)
        # 查 worker 名称
        _wname = ""
        try:
            with db_lock:
                c2 = db.cursor()
                c2.execute("SELECT name, device_name FROM runtime_credentials WHERE id=?", (credential_id,))
                row = c2.fetchone()
                if row:
                    # Issue #701：worker_name 优先凭据自定义名 rc.name（空回退 device_name → id 兜底）
                    _wname = (row[0] or "").strip() or row[1] or f"worker-{credential_id}"
        except Exception:
            _wname = f"worker-{credential_id}"
        _wf = _os.path.join(_worker_log_dir, f"{credential_id}.log")
        with open(_wf, "a", encoding="utf-8") as _wf_fp:
            for ll in body.log_lines:
                _entry = {
                    "ts": ll.ts or now,
                    "level": ll.level,
                    "component": "worker",
                    "request_id": "-",
                    "message": ll.message,
                    "worker_id": credential_id,
                    "worker_name": _wname,
                }
                _wf_fp.write(_json.dumps(_entry, ensure_ascii=False) + "\n")
        # 同时落 obs_log（跨机可见、按仓库过滤的用户面日志）——
        # worker 侧无需改动：它本来就在心跳里上报最近 WARNING+ 日志行。
        # task_id / repo_key 由 logs.ingest 从正文里抽取（形如
        # `[task t_xxx] [seq=N|issue:#M|agent:x|repo:gitcode:o/r] ...`），抽不到的行
        # 因没有仓库维度而不会出现在用户面（平台/进程类日志归 L2/L3）。
        try:
            from src.observability import logs as _obs_logs
            _recs = [{"ts": (ll.ts or now), "level": ll.level, "component": "worker",
                      "msg": ll.message, "source": _wname or f"worker-{credential_id}",
                      "runtime_id": 0} for ll in body.log_lines]
            _obs_logs.ingest(_recs, default_source=_wname or f"worker-{credential_id}")
        except Exception as _ole:
            log.warning(f"obs_log worker ingest failed: {_ole}")

    # 返回 worker 代码版本（仅 git commit，不含 wheels/python 指纹）
    # worker 端对比此版本号决定是否自动升级
    from src.worker_package import worker_code_version
    _latest_ver = worker_code_version()

    return {"status": "ok", "cancelled_tasks": cancelled_tasks, "latest_version": _latest_ver}


@router.get("/admin/event-bus", operation_id="runtime_event_bus", summary="Event bus debug")
def event_bus_debug(user: dict = Depends(require_no_session_token)):
    """Debug endpoint: show event bus history and listeners."""
    from src.api.event_bus import get_event_bus
    bus = get_event_bus()
    return {
        "history": bus.get_history(),
        "listeners": {k: len(v) for k, v in bus._listeners.items()},
        "total_events": len(bus.get_history()),
    }


# ===== Issue #134: 机器健康度查询 API =====

@router.get("/admin/machine-health", operation_id="runtime_machine_health", summary="Machine health overview", description="Machine health overview for all engines: {runtime_id, name, health_score, total_tasks, failed_tasks, consecutive_failures, failure_rate, health_abnormal, health_reason, last_failure_at, ...}. Issue #498: health_abnormal/health_reason expose the unified abnormality verdict (heartbeat_lost > consecutive_failures > low_score) with small-sample exemption and failure decay self-heal.")
def machine_health_overview(user: dict = Depends(require_no_session_token)):
    """获取所有引擎的机器健康度概览（供前端/运维查看）。

    返回 [{runtime_id, name, health_score, total_tasks, failed_tasks,
           consecutive_failures, failure_rate, health_abnormal,
           health_reason, last_failure_at, ...}]
    """
    from src.runtime.health_scorer import get_all_health_scores
    from src.runtime.instances import runtime_instance_name
    scores = get_all_health_scores()
    # 补充引擎名
    for s in scores:
        s["engine_name"] = runtime_instance_name(s.get("runtime_id", 0))
    return {"engines": scores}


@router.get("/admin/machine-health/{runtime_id}", operation_id="runtime_machine_health_detail", summary="Single runtime health detail", description="Health detail for a single engine. Issue #498: includes health_abnormal/health_reason (unified verdict), failed_tasks_effective (decayed-window failures) and last_failure_at for self-heal observability.")
def machine_health_detail(runtime_id: int, user: dict = Depends(require_no_session_token)):
    """获取单个引擎的健康度详情。

    Issue #1104（契约 AT-RNT-52）：详情语义——不存在的 runtime_id 返回 404
    （统一错误结构），不再回退 get_health_score 现算通路返回 200 全量结构。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={
                "code": 404, "message": "runtime not found"})
    from src.runtime.health_scorer import get_health_score
    from src.runtime.instances import runtime_instance_name
    data = get_health_score(runtime_id, force_refresh=True)
    data["engine_name"] = runtime_instance_name(runtime_id)
    return data


@router.post("/admin/batch-recover", operation_id="runtime_batch_recover", summary="Batch recover stale tasks")
def batch_recover_stale_tasks_api(user: dict = Depends(require_no_session_token)):
    """Manually trigger batch stale task recovery (Phase 5)."""
    from src.runtime.service import batch_recover_stale_tasks
    result = batch_recover_stale_tasks()
    return result


class RuntimeClaimBody(BaseModel):
    """v3.0：worker 能力动态声明（零落库）——claim 每次携带"""
    supports_obs_url: bool = False   # 本 worker 支持 OBS 直连（启动探测结果）
    prefer_fallback: bool = False    # 直连不稳，服务端直接给代拉模式
    # Issue #520 bound claim：expected_task_id + expected_event_seq 成对可选字段。
    # 二者必须成对提供（只传其一 → 422 明确拒绝）；提供时服务端在 db_lock 临界区内
    # 原子校验目标任务身份，未命中返回无任务，绝不回退 FIFO。
    expected_task_id: str = ""
    expected_event_seq: int = 0

    @model_validator(mode="after")
    def _expected_pair(self):
        if bool(self.expected_task_id) != bool(self.expected_event_seq):
            raise ValueError(
                "expected_task_id and expected_event_seq must be provided together")
        return self


def _require_runtime_owned(runtime_id: int, credential_id: int) -> None:
    """Issue #520：校验 runtime 归属当前 rta_ 凭据（agent_runtimes.credential_id）。

    他人 runtime_id → 403（fail-closed，不泄露任务信息）。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=? AND credential_id=?",
                  (runtime_id, credential_id))
        if not c.fetchone():
            raise HTTPException(status_code=403, detail={
                "code": 403, "message": "runtime not owned by this credential"})


def _attach_skill_obs(task: dict) -> None:
    """v3.0：任务附 OBS 直连信息（claim 现签 URL——file_path 权威、永远最新）。

    skill_obs_url：预签名 URL（TTL env RUNTIME_SKILL_URL_TTL，默认 1800s）
    skill_key：对象键（worker 侧可记录/排查用）
    mode：obs（直连）/ ""（无 OBS——worker 走 skill_files）
    """
    try:
        from src.storage import get_storage
        _storage = get_storage()
        if _storage is None:
            return
        skill = (task.get("skill") or "").strip()
        if not skill:
            return
        from src.user.db import resolve_skill_id, get_skill_object_key, get_skill_by_id
        _skill_id = task.get("skill_id") or 0
        _task_repo = task.get("repo_key") or ""
        _task_uid = task.get("user_id") or ""
        if _skill_id:
            _sk = get_skill_by_id(_skill_id)
            if _sk:
                _okey = _sk.get("file_path") or ""
            else:
                # stale skill_id → 回退按名字解析
                _sid = resolve_skill_id(skill, repo_key=_task_repo, user_id=_task_uid)
                _okey = get_skill_object_key(_sid) if _sid else ""
        else:
            _sid = resolve_skill_id(skill, repo_key=_task_repo, user_id=_task_uid)
            _okey = get_skill_object_key(_sid) if _sid else ""
        if not _okey:
            # Issue #617：skill 按名未命中 → 按 agent_type 回退官方检查器（worker 无库，
            # 服务端解析后以 skill_fallback 下发：名字 + id，worker 物化其 zip 并提示
            # `grape skills detail --skill_id <id>`）
            try:
                from src.user.db import resolve_official_checker_skill
                _fb = resolve_official_checker_skill(task.get("agent_type") or "")
                if _fb and _fb.get("file_path"):
                    _okey = _fb.get("file_path")
                    task["skill_fallback"] = {
                        "skill_name": _fb.get("skill_name"),
                        "skill_id": _fb.get("id") or 0,
                    }
                    log.info(f"claim skill fallback: '{skill}' not found, agent_type="
                             f"{task.get('agent_type')} -> official checker "
                             f"'{_fb.get('skill_name')}' (id={_fb.get('id')})")
            except Exception as _fb_e:
                log.warning(f"claim skill fallback resolve failed: {_fb_e}")
        if not _okey:
            return
        ttl = int(os.environ.get("RUNTIME_SKILL_URL_TTL", "1800"))
        task["skill_key"] = _okey
        task["skill_obs_url"] = _storage.temporary_url(_okey, expires=ttl)
        task["mode"] = "obs"
    except Exception as e:
        log.warning(f"attach skill_obs failed: {e}")


@router.post("/runtimes/{runtime_id}/tasks/claim")
def runtime_claim(runtime_id: int, body: RuntimeClaimBody | None = None,
                        credential_id: int = Depends(_require_credential)):
    """按 runtime_id 拉取队首任务或 null（确定路由 D3 + 串行槽 D9）。

    v3.0：能力声明（supports_obs_url/prefer_fallback）动态携带；
    任务附 skill_obs_url（claim 现签预签名 URL）——prefer_fallback
    worker 直接给 mode=server-fetch（跳过 URL 直连尝试）。

    Issue #520：body 成对携带 expected_task_id/expected_event_seq 时透传服务层，
    在 db_lock 临界区内原子校验目标身份（未命中返回无任务，不回退 FIFO）；
    未提供 expected 保持原 FIFO 路径零改动（旧客户端零影响）。
    """
    task = service.claim(
        runtime_id,
        expected_task_id=(body.expected_task_id or None) if body else None,
        expected_event_seq=(body.expected_event_seq or None) if body else None,
    )
    # 检查是否有 pending skill scan 指令
    scan_request = False
    if not task:
        # 空闲时检查 scan 指令（有 task 时不打扰执行流程，下次空闲 claim 再带）
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT pending_skill_scan FROM agent_runtimes WHERE id=? AND credential_id=?",
                          (runtime_id, credential_id))
                _row = c.fetchone()
                if _row and _row[0]:
                    scan_request = True
        except Exception:
            pass
    if task:
        if body and body.prefer_fallback:
            task["mode"] = "server-fetch"
        else:
            _attach_skill_obs(task)
    return {"task": task, "scan_request": scan_request}


@router.get("/runtimes/{runtime_id}/tags")
def runtime_get_tags(runtime_id: int, credential_id: int = Depends(_require_credential)):
    """Return this engine's claimable tags from DB (worker polls this instead of ENGINE_TAGS env).

    Tags are managed by users via the console (settings.py CRUD endpoints).
    Workers call this endpoint to get their current tag list, enabling real-time
    tag updates without restart.
    """
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag",
                (runtime_id,)
            )
            tags = [r[0] for r in c.fetchall()]
        except Exception:
            tags = []
    return {"tags": tags}


@router.get("/tasks/{task_id}/env")
def runtime_task_env(task_id: str, credential_id: int = Depends(_require_credential)):
    """Return latest env (AK/SK + credentials) for a task from DB.

    Workers call this before executing to get fresh credentials,
    instead of relying on the env_extra snapshot from task creation time.

    Issue #990: this endpoint is idempotent for a valid snapshot token, but may
    rotate the session token and persist it back to ``agent_tasks.env_extra``
    when the snapshot token is expired or out of scope. The returned env and
    the persisted snapshot are committed atomically by ``sync_task_session_token``.
    """
    from src.session_manager import db, db_lock
    from src.runtime.service import _collect_server_env, sync_task_session_token
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, account, runtime_id FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        return {"env": {}}
    user_id, account, runtime_id = row[0] or 0, row[1] or "", row[2] or 0
    env = _collect_server_env(user_id, account)
    # Issue #1858：worker 执行前重拉任务 env 时，按任务 runtime 下发三面基址，
    # 覆盖历史任务/旧快照（此前只下发凭据，旁路路径仍可能缺 GRAPE_A2A_BASE_URL）。
    try:
        from src.prompt_builder import resolve_task_server_env
        env.update(resolve_task_server_env(runtime_id))
    except Exception as _base_e:
        log.warning(f"runtime_task_env service base resolve failed for task={task_id}: {_base_e}")
    # Issue #990：会话 token 与 shell 环境快照同步——valid 时复用快照 token，
    # 失效/轮换时签发新 token 并回写 agent_tasks.env_extra，随后返回给 worker。
    # Issue #990：只同步 GRAPE_SESSION_TOKEN，不覆盖任务快照中的其它显式 env
    # （如 GRAPE_CLI_MODE），避免超出 Spec 范围改变 CLI 作用域语义。
    session_token = sync_task_session_token(task_id)
    if session_token:
        env["GRAPE_SESSION_TOKEN"] = session_token
    # Issue #1745：跨场景转交后，任务快照中的 GRAPE_SCENE_ID 可能仍是源场景；
    # 执行前按 Issue 当前归属场景（issues detail.scene_id 同源）校正，PR 订阅任务保留订阅语义。
    try:
        from src.api.event_pipeline import reconcile_task_scene_env
        reconcile_task_scene_env(task_id, env)
    except Exception as _scene_e:
        log.warning(f"runtime_task_env scene reconcile failed for task={task_id}: {_scene_e}")
    return {"env": env}


@router.post("/runtimes/{runtime_id}/tasks/claim-by-tag/{tag}")
def runtime_claim_by_tag(runtime_id: int, tag: str,
                               body: RuntimeClaimBody | None = None,
                               credential_id: int = Depends(_require_credential)):
    """Claim task by tag + scope isolation (event-dispatch-redesign-v2.md 2.4).

    Workers with engine tags can claim tasks matching their tag and scope.

    Issue #520：body 成对携带 expected_task_id/expected_event_seq 时透传服务层
    原子校验（未命中无任务，不回退 FIFO）；未提供保持 FIFO。
    """
    task = service.claim_by_tag(
        tag, runtime_id,
        expected_task_id=(body.expected_task_id or None) if body else None,
        expected_event_seq=(body.expected_event_seq or None) if body else None,
    )
    # 检查是否有 pending skill scan 指令（与 claim 端点一致）
    scan_request = False
    if not task:
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT pending_skill_scan FROM agent_runtimes WHERE id=? AND credential_id=?",
                          (runtime_id, credential_id))
                _row = c.fetchone()
                if _row and _row[0]:
                    scan_request = True
        except Exception:
            pass
    if task:
        if body and body.prefer_fallback:
            task["mode"] = "server-fetch"
        else:
            _attach_skill_obs(task)
    return {"task": task, "scan_request": scan_request}


# ===== Issue #520：bound claim / 能力协商 / 许可对号 lookup（四件套，fail-closed）=====

def _pending_scan_request(runtime_id: int, credential_id: int) -> bool:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT pending_skill_scan FROM agent_runtimes WHERE id=? AND credential_id=?",
                  (runtime_id, credential_id))
        _row = c.fetchone()
        return bool(_row and _row[0])


@router.post("/runtimes/{runtime_id}/tasks/claim-bound")
def runtime_claim_bound(runtime_id: int, body: RuntimeClaimBody,
                        credential_id: int = Depends(_require_credential)):
    """Issue #520 bound claim：按 expected_task_id + expected_event_seq 原子领取指定任务。

    - 鉴权：rta_ 凭据 + runtime 归属校验（他人 runtime → 403）。
    - body 必须成对携带 expected 字段（缺 → 400；只传其一由模型校验 422 拒绝）。
    - 未匹配返回 {"task": null}，不领取/不修改任何其他任务，绝不回退 FIFO。
    - 未部署节点对本路径 404 → 客户端保持许可关闭（不回退通用 claim）。
    """
    _require_runtime_owned(runtime_id, credential_id)
    if not body.expected_task_id or not body.expected_event_seq:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "claim-bound requires expected_task_id and expected_event_seq"})
    task = service.claim(runtime_id,
                         expected_task_id=body.expected_task_id,
                         expected_event_seq=body.expected_event_seq)
    scan_request = False
    if not task:
        scan_request = _pending_scan_request(runtime_id, credential_id)
    else:
        if body.prefer_fallback:
            task["mode"] = "server-fetch"
        else:
            _attach_skill_obs(task)
    return {"task": task, "scan_request": scan_request}


@router.post("/runtimes/{runtime_id}/tasks/claim-by-tag-bound/{tag}")
def runtime_claim_by_tag_bound(runtime_id: int, tag: str,
                               body: RuntimeClaimBody,
                               credential_id: int = Depends(_require_credential)):
    """Issue #520 bound claim（tag 版）：tag + scope 条件不变，追加 expected 原子校验。

    未匹配返回 {"task": null}，不回退 FIFO；归属校验同 claim-bound。
    """
    _require_runtime_owned(runtime_id, credential_id)
    if not body.expected_task_id or not body.expected_event_seq:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "claim-by-tag-bound requires expected_task_id and expected_event_seq"})
    task = service.claim_by_tag(tag, runtime_id,
                                expected_task_id=body.expected_task_id,
                                expected_event_seq=body.expected_event_seq)
    scan_request = False
    if not task:
        scan_request = _pending_scan_request(runtime_id, credential_id)
    else:
        if body.prefer_fallback:
            task["mode"] = "server-fetch"
        else:
            _attach_skill_obs(task)
    return {"task": task, "scan_request": scan_request}


@router.get("/runtimes/{runtime_id}/tasks/claim-capabilities")
def runtime_claim_capabilities(runtime_id: int,
                               credential_id: int = Depends(_require_credential)):
    """Issue #520 能力协商（只读）：客户端 POST bound claim 前确认服务端能力。

    能力位：bound_claim（claim-bound / claim-by-tag-bound）+ task_lookup（lookup）。
    未部署节点 404 → 客户端保持许可关闭，不回退通用 claim。
    """
    _require_runtime_owned(runtime_id, credential_id)
    return {
        "runtime_id": runtime_id,
        "capabilities": {
            "bound_claim": True,
            "task_lookup": True,
        },
    }


@router.get("/runtimes/{runtime_id}/tasks/lookup")
def runtime_task_lookup(runtime_id: int,
                        expected_event_seq: int = 0,
                        credential_id: int = Depends(_require_credential)):
    """Issue #520 许可对号 lookup：按 expected_event_seq 精确查询许可目标任务。

    - 鉴权：与 /runtimes/{id}/tags 同款（rta_ + 归属校验，他人 runtime → 403）。
    - expected_event_seq 缺失或 <=0 → 400（旧任务 event_seq 空/0 不支持）。
    - 纯验证性 SELECT：命中返回身份字段 {task_id, issue_id, event_seq, agent_type,
      created_at}；未命中 {"task": null}（许可保留，等待目标入队）。
    """
    _require_runtime_owned(runtime_id, credential_id)
    if not expected_event_seq or expected_event_seq <= 0:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "lookup requires a positive expected_event_seq"})
    task = service.lookup_task_by_event_seq(runtime_id, expected_event_seq)
    return {"task": task}


@router.post("/tasks/{task_id}/skill-zip")
def runtime_task_skill_zip(task_id: str, credential_id: int = Depends(_require_credential)):
    """v3.0 服务端代拉（降级链 ②）：worker URL 直连失败的兜底。

    服务端现查 file_path → get_skill_zip_bytes（自动走 zip_cache：
    命中零 OBS / miss 拉 OBS 更新缓存）→ base64 返回。
    """
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    skill = (task.get("skill") or "").strip()
    if not skill:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "task has no skill"})
    try:
        from src.user.db import resolve_skill_id, get_skill_object_key, get_skill_by_id
        from src.storage import get_skill_zip_bytes
        _skill_id = task.get("skill_id") or 0
        _task_repo = task.get("repo_key") or ""
        _task_uid = task.get("user_id") or ""
        if _skill_id:
            _sk = get_skill_by_id(_skill_id)
            if _sk:
                _okey = _sk.get("file_path") or ""
            else:
                _sid = resolve_skill_id(skill, repo_key=_task_repo, user_id=_task_uid)
                _okey = get_skill_object_key(_sid) if _sid else ""
        else:
            _sid = resolve_skill_id(skill, repo_key=_task_repo, user_id=_task_uid)
            _okey = get_skill_object_key(_sid) if _sid else ""
        if not _okey:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "skill not on OBS"})
        _zip = get_skill_zip_bytes(_okey)
        if not _zip:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "OBS fetch failed"})
        import base64 as _b64
        return {"zip_base64": _b64.b64encode(_zip).decode()}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"skill-zip fetch failed: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"fetch failed: {e}"})


@router.post("/tasks/{task_id}/start")
def runtime_task_start(task_id: str, credential_id: int = Depends(_require_credential)):
    t = service.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    service.update_task_status(task_id, "running")
    return {"status": "running"}


@router.post("/tasks/{task_id}/progress")
def runtime_task_progress(task_id: str, body: ProgressBody, credential_id: int = Depends(_require_credential)):
    service.enqueue_async_report("progress", task_id, {"msg": body.summary, "step": body.step, "total": body.total})
    return {"status": "ok"}


@router.post("/tasks/{task_id}/messages")
def runtime_task_messages(task_id: str, body: MessagesBody, credential_id: int = Depends(_require_credential)):
    # task 已终态（failed/cancelled/completed）时拒绝消息，返回 410 Gone。
    # worker 端 _execute_task 检测到 410 后中断执行（用户终止后残留进程不再继续产出）。
    task = service.get_task(task_id)
    if task and task.get("status") in ("failed", "cancelled", "completed"):
        raise HTTPException(status_code=410, detail={"code": 410, "message": f"task already {task.get('status')}"})
    # 自动修复：worker 上报了消息说明已在执行，但 task_start 可能调用失败
    # 导致 status 仍为 queued/dispatched → 自动更新为 running
    if task and task.get("status") in ("queued", "dispatched"):
        try:
            service.update_task_status(task_id, "running")
            log.info(f"auto-fix: task {task_id} status {task.get('status')} -> running (messages reported)")
        except Exception as e:
            log.warning(f"auto-fix: failed to update task {task_id} to running: {e}")
    # Issue #975（QA 复审 Spec #1）：批次回执带 seq 范围——只对 dict 消息预留/计数
    # （service.report_messages 跳过非 dict，不消耗 seq，计入即假回执）；同步兜底路径
    # 直接采用实际落库回执，异步路径返回预约范围（实际范围以服务端落库日志为准）
    _count = sum(1 for m in (body.messages or []) if isinstance(m, dict))
    _seq_start = service.peek_next_seq(task_id)
    _receipt = service.enqueue_async_report("messages", task_id, body.messages, seq_start=_seq_start)
    if isinstance(_receipt, dict) and _receipt.get("count"):
        return {"status": "ok", **_receipt}
    return {"status": "ok", "count": _count, "seq_start": _seq_start,
            "seq_end": _seq_start + _count - 1 if _count > 0 else _seq_start - 1}


@router.post("/tasks/{task_id}/usage")
def runtime_task_usage(task_id: str, body: UsageBody, credential_id: int = Depends(_require_credential)):
    """LLM 用量上报(追加台账)。

    历史实现是 `UPDATE agent_tasks SET usage_json=?` —— 那是**破坏性**的:
    usage_json 存的是会话续接指针 {session_id, work_dir, engine}(由 /complete →
    complete_task 写,被 routes/events.py / im/handler_async.py / routes/chat.py 消费),
    一旦真有引擎上报用量就会冲掉指针、破坏多轮续接与 B2 引擎守卫。
    现改为追加落 obs_llm_usage,按 (task_id, seq) 幂等去重,不碰 usage_json。
    见 docs/weekly-07/observability-llm-usage-design.md §3.2/§4.2。
    """
    usage = {
        "provider": body.provider, "model": body.model,
        "input": body.input, "output": body.output,
        "cache_read": body.cache_read or body.cache,   # 遗留 cache ≡ cache_read
        "cache_write": body.cache_write, "reasoning": body.reasoning,
        "turn": body.turn, "cost_usd": body.cost_usd, "cost_status": body.cost_status,
    }
    try:
        from src.observability.llm_usage import record_usage
        record_usage(task_id, [usage], source=body.source or "report")
    except Exception as e:      # 用量采集绝不能拖垮任务主链路
        log.warning(f"record usage ledger failed task={task_id}: {e}")
    # Prometheus 埋点(与台账同源,保留供外部 Prometheus 抓取)
    try:
        from src.observability import record_llm_usage
        record_llm_usage(body.provider, body.model, body.input, body.output,
                         body.cache_read or body.cache)
    except Exception:
        pass
    return {"status": "ok"}


@router.post("/tasks/{task_id}/complete")
def runtime_task_complete(task_id: str, body: CompleteBody, credential_id: int = Depends(_require_credential)):
    """完成（幂等）：解析结构化 output → finalize_event_archive → update_event_complete。

    C（stream-end-finalize-single-signal-design.md）：worker 进程结束 = 流结束，单信号终态。
    落 stream_ended（仪器）+ finalize_by_stream_end（优先已显式 self_report 的结构化结论）；
    不再区分自报/普通技能、不再等双信号。
    """
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    # Step 4：补报守卫（回炉/重跑后旧实例不得误 complete 新执行）
    if not body.run_token:
        # client 旧版未带 run_token：仅在任务活动态放行，已终态幂等，其它状态保守拒绝
        pass
    else:
        _guard = service.validate_run_token(task, body.run_token)
        if _guard == "stale":
            raise HTTPException(status_code=409, detail={"code": 409,
                                                         "message": "stale report (run_token mismatch), task likely requeued or rerun"})
    _usage = {"session_id": body.session_id, "work_dir": body.work_dir, "engine": body.engine}
    service.set_task_signal(task_id, "stream_ended")
    service.finalize_by_stream_end(task_id, body.result_text or "", body.exit_code, usage=_usage)
    return {"status": "completed"}


@router.post("/tasks/{task_id}/fail")
def runtime_task_fail(task_id: str, body: FailBody, credential_id: int = Depends(_require_credential)):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    # Step 4：补报守卫
    if body.run_token:
        _guard = service.validate_run_token(task, body.run_token)
        if _guard == "stale":
            raise HTTPException(status_code=409, detail={"code": 409,
                                                         "message": "stale report (run_token mismatch), task likely requeued or rerun"})
    # 透传 worker 上报的真实 exit_code（旧硬编码 1 → 死 session 检测的"连续3次负数"
    # 永不触发，坏指针清不掉）；旧 worker 不传则默认 -1，与 fail_task 签名一致。
    service.fail_task(task_id, body.error or body.failure_reason or "unknown error", exit_code=body.exit_code)
    return {"status": "failed"}


@router.post("/tasks/{task_id}/requeue")
def runtime_task_requeue(task_id: str, credential_id: int = Depends(_require_credential)):
    """客户端并发冲突跳过时回滚任务为 queued（避免 dispatched 悬空卡死，见 seq#622 教训）。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "task not found"})
    ok = service.requeue_task(task_id)
    return {"status": "requeued", "recycled": ok}


@router.post("/runtimes/{runtime_id}/recover-orphans")
def runtime_recover_orphans(runtime_id: int, credential_id: int = Depends(_require_credential)):
    """启动/重注册时清理该 runtime 遗留的 dispatched/running 任务（置回 queued）。

    2026-10-08 自噬循环修复（生产 exec#310 实锤）：
    部署类任务流程中包含"重启 master"——master 重启导致 worker 心跳断 3 次
    触发 _reregister → recover_orphans 一刀切把**正在执行**的任务置回 queued
    → worker 重新 claim 从头再跑 → 又重启 master → 循环。任务的本地进程
    从头到尾没死过，不是孤儿。

    修复（与 service.py claim 兜底回收的活跃度检查同一思路）：
    1. 活跃度检查：task_messages 最近 RECOVER_ACTIVITY_WINDOW（1200s，兼容
       hermes 长命令静默窗口 + 分次等待策略）内有新消息 → agent 还在跑，
       跳过回炉（真孤儿不会有新消息）。
    2. attempts 封顶：回炉时 attempts+1，attempts>=3 的任务直接置 failed
       （此前 recover_orphans 不维护 attempts，无限回炉无封顶）。

    2026-10-19 Issue #370：任务年龄宽限期闸门（在活跃度检查之前）。
    刚派发的新任务引擎尚未输出首条 task_messages，活跃度检查必然判 0 消息
    → 误判孤儿 → 每轮 worker 注册/心跳触发的回收都 attempts+1 → 封顶强制
    failed（事件 #13485：28 秒判死，同类失败已 10 次）。加年龄闸门：started_at
    （NULL 兜底 created_at）距 now 不足 RECOVER_GRACE_SECONDS（默认 300s，
    覆盖队列出队+引擎冷启动上界）直接 skipped，attempts 不变、不回炉。
    """
    RECOVER_MAX_ATTEMPTS = int(os.environ.get("RECOVER_MAX_ATTEMPTS", "3"))
    RECOVER_GRACE_SECONDS = int(os.environ.get("RECOVER_GRACE_SECONDS", "300"))
    # Issue #1495：活跃窗口单点收敛到 task_activity 具名常量（数值沿用 1200s 不变，
    # 原 RECOVER_ACTIVITY_WINDOW 环境变量口除名——「一处定义」硬约束）；
    # 判定改走 has_recent_activity（Python 侧解析 epoch 比较），消除 TEXT 列逐字节
    # 比较对 created_at 写入格式的隐含依赖（表默认值空格格式行 / 无微秒 ISO 行）。
    from src.runtime.task_activity import (RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS,
                                           has_recent_activity)
    RECOVER_ACTIVITY_WINDOW = RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS
    _now = datetime.now(timezone.utc)
    _grace_cutoff_dt = _now - timedelta(seconds=RECOVER_GRACE_SECONDS)
    recovered, skipped_active, force_failed, skipped_young = [], [], [], []
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, attempts, runtime_id, event_seq, started_at, created_at "
            "FROM agent_tasks WHERE runtime_id=? AND status IN ('dispatched','running')",
            (runtime_id,))
        rows = c.fetchall()
        _seq_by_tid = {}
        for tid, attempts, rid, ev_seq, _sa, _ca in rows:
            _seq_by_tid[tid] = ev_seq
        for tid, attempts, rid, _ev_seq, started_at, created_at in rows:
            # 年龄闸门（Issue #370）：started_at（NULL 兜底 created_at）距 now 不足
            # 宽限期 → 直接 skipped，防止刚派发的新任务被误判孤儿反复回炉。
            # 时间戳解析失败保守跳过闸门，继续后续活跃度检查（保持旧行为：回收）。
            _age_base = started_at or created_at
            if _age_base:
                try:
                    _age_dt = datetime.fromisoformat(_age_base)
                    if _age_dt.tzinfo is None:
                        _age_dt = _age_dt.replace(tzinfo=timezone.utc)
                    if _age_dt > _grace_cutoff_dt:
                        skipped_young.append(tid)
                        continue
                except Exception:
                    pass
            # 活跃度检查：近期有 task_messages → 任务在跑，不是孤儿
            # Issue #1495：改走 task_activity.has_recent_activity（按 task_id 索引
            # 取行 + Python 解析 epoch 比较），替代 `created_at > ?` 逐字节比较；
            # 解析失败行不计活跃（保守：宁可误回收也不留死任务）。
            try:
                if has_recent_activity(c, tid, RECOVER_ACTIVITY_WINDOW):
                    skipped_active.append(tid)
                    continue
            except Exception:
                pass  # 查询失败保守跳过检查（保持旧行为：回收）
            # attempts 封顶：循环回炉保护
            # 2026-10-08 follow-up：SQLite 后端不支持 CONCAT → no such function 导致
            # 整个事务回滚、端点 500、封顶安全网失效。改用 service.py 同款纯 SQL CASE
            # 写法（attempts=attempts+1，CASE 判定 status，说明写 result_text 非 errors）。
            # 2026-10-09 #253 合并融合：回炉（queued）分支并入 runtime_id 归零 +
            # pool_agent_type 回填（对齐 master 侧回收；引擎删重建后防死定向）。
            _att = (attempts or 0) + 1
            # 归零回填参数：先读 agent_type（回炉转广播后同 tag 引擎可再领）
            _pool_fill = ""
            try:
                c.execute("SELECT agent_type FROM agent_tasks WHERE id=?", (tid,))
                _pr = c.fetchone()
                if _pr and _pr[0]:
                    _pool_fill = _pr[0]
            except Exception:
                pass
            # 2026-10-20 MySQL attempts 语义修复：原写法 SET attempts=attempts+1 且
            # CASE 里再写 attempts+1 —— MySQL 同一 UPDATE 后面的赋值读的是刚更新的
            # 新值（等效一次 +2），SQLite 读旧值（+1），两库语义分叉：MySQL 提前一轮
            # 判死而 Python 侧 _att 仍按旧值算 → 误入 recovered 分支 → 事件终态回写
            # 被跳过 → 事件永远 running（生产 34 条悬空）。改为 attempts 与全部判死
            # 条件均参数化传 _att，两种数据库读到同一个值，语义强制一致。
            c.execute(
                "UPDATE agent_tasks SET attempts=?, "
                "status=CASE WHEN ? >= ? THEN 'failed' ELSE 'queued' END, "
                "started_at=CASE WHEN ? >= ? THEN started_at ELSE NULL END, "
                "result_text=CASE WHEN ? >= ? "
                "THEN 'recover-orphans attempts cap reached (possible self-restart loop)' "
                "ELSE result_text END, "
                "runtime_id=CASE WHEN ? >= ? THEN runtime_id ELSE 0 END, "
                "pool_tag=CASE WHEN ? < ? AND pool_tag='' "
                "THEN ? ELSE pool_tag END, "
                "pool_agent_type=CASE WHEN ? < ? AND pool_agent_type='' "
                "THEN ? ELSE pool_agent_type END "
                "WHERE id=?",
                (_att,
                 _att, RECOVER_MAX_ATTEMPTS,
                 _att, RECOVER_MAX_ATTEMPTS,
                 _att, RECOVER_MAX_ATTEMPTS,
                 _att, RECOVER_MAX_ATTEMPTS,
                 _att, RECOVER_MAX_ATTEMPTS, _pool_fill,
                 _att, RECOVER_MAX_ATTEMPTS, _pool_fill, tid))
            if _att >= RECOVER_MAX_ATTEMPTS:
                force_failed.append(tid)
            else:
                recovered.append(tid)
            # 槽位释放（与 service.py 回收/update_task_status/requeue_task 同款防负数写法）：
            # 旧代码两分支都不减 busy_slots → 每次回炉净漏 1 槽、force-fail 永久泄漏；
            # claim_by_tag 满载即拒领（busy>=max → None），漏 4 槽后引擎哑死。
            if rid and rid > 0:
                from src.runtime.service import release_busy_slot
                release_busy_slot(rid, cursor=c)
        db.commit()
    # Issue #369：attempts 封顶判死 → 完整回写关联事件诊断字段
    # （result/exit_code/completed_at，对齐 event_pipeline._fail_event 语义；
    # update_event_complete 终态保护天然幂等，双主 scp 同步两侧重复回写安全）
    for tid in force_failed:
        _seq = _seq_by_tid.get(tid)
        if not _seq:
            continue
        try:
            from src.api.event_store import update_event_complete
            update_event_complete(
                _seq, "failed", exit_code=1,
                completed_at=datetime.now(timezone.utc).isoformat(),
                result="recover-orphans attempts cap reached "
                       "(possible self-restart loop), 任务判死")
        except Exception as e:
            log.warning(f"recover-orphans event writeback failed tid={tid}: {e}")
    if recovered or skipped_active or force_failed or skipped_young:
        log.info(f"recover-orphans runtime={runtime_id}: recovered={recovered} "
                 f"skipped_active={skipped_active} skipped_young={skipped_young} force_failed={force_failed}")
    return {"message": "orphans recovered", "recovered": recovered,
            "skipped_active": skipped_active, "skipped_young": skipped_young,
            "force_failed": force_failed}


# ===== 用户态端点（M2，保留）=====

@router.get("/tasks/{task_id}/status", operation_id="runtime_task_status", summary="Task status polling", description="Task status polling (chat front-end D5 typewriter fallback). Issue #539: also the authorized query path referenced by pipeline segment output.task_ref.query_path — returns full result_text for authorized callers; pipeline summaries only carry a redacted result_ref summary.")
def get_task_status(task_id: str, user: dict = Depends(require_no_session_token)):
    """任务状态轮询（chat 前端用，D5 打字机降级）。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    return {
        "id": task["id"],
        "status": task["status"],
        "result_text": task.get("result_text") or "",
        "exit_code": task.get("exit_code"),
        "errors": task.get("errors") or "",
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }


@router.get("/issues/takeover-context", operation_id="runtime_takeover_context", summary="Takeover context for an issue", description="Returns the running task's full context (prompt, env_extra, messages) for interactive takeover in dsh-web.")
def takeover_context(repo_key: str = Query(..., description="Repository key (e.g. gitcode:owner/repo)"),
                     issue_number: str = Query(..., description="Issue number (without #)"),
                     user: dict = Depends(require_no_session_token)):
    """接管上下文：按 Issue 号查正在运行的任务，返回完整字段 + 最近 200 条消息。

    一次调用拿到全部续接所需数据：
    - prompt / env_extra / skill / cwd → 重建执行环境
    - messages（最近 200 条）→ agent 的思考和操作历史
    - task_id → 用于后续 cancel
    """
    from src.session_manager import db, db_lock
    _issue_id = f"#{issue_number.lstrip('#')}"
    # ① 查 running/dispatched 的 task（跨所有 worker，不限 runtime_id）
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, status, agent_type, skill, prompt, env_extra, cwd, runtime_id, repo_key, issue_id "
            "FROM agent_tasks "
            "WHERE repo_key=? AND issue_id=? AND status IN ('running','dispatched') "
            "ORDER BY created_at DESC LIMIT 1",
            (repo_key, _issue_id))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"没有找到 Issue #{issue_number} 正在运行的任务"})
    task_id, status, agent_type, skill, prompt, env_extra, cwd, runtime_id, task_repo_key, task_issue_id = row
    # ② 查最近 200 条消息（必须取"最新"而非"最早"，否则长任务接管时看到的
    #    是几小时前的任务开头，而不是当前进展。先按 seq DESC 取，再反转为正序阅读）
    messages = service.list_task_messages_recent(task_id, limit=200)
    return {
        "task_id": task_id,
        "status": status,
        "agent_type": agent_type or "",
        "skill": skill or "",
        "prompt": prompt or "",
        "env_extra": env_extra or "{}",
        "cwd": cwd or "",
        "runtime_id": runtime_id or 0,
        "repo_key": task_repo_key or "",
        "issue_id": task_issue_id or "",
        "messages": messages,
    }


@router.get("/tasks/{task_id}/detail", operation_id="runtime_task_detail", summary="Task detail (takeover)", description="Returns full task fields for interactive takeover: prompt, env_extra, skill, cwd, etc.")
def get_task_detail(task_id: str, user: dict = Depends(require_no_session_token)):
    """任务详情（接管用）：返回完整字段供 dsh-web 创建交互式 session。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    return {
        "id": task["id"],
        "status": task["status"],
        "agent_type": task.get("agent_type") or "",
        "skill": task.get("skill") or "",
        "prompt": task.get("prompt") or "",
        "env_extra": task.get("env_extra") or "{}",
        "cwd": task.get("cwd") or "",
        "repo_key": task.get("repo_key") or "",
        "issue_id": task.get("issue_id") or "",
        "runtime_id": task.get("runtime_id") or 0,
        "event_seq": task.get("event_seq") or 0,
        "server_url": task.get("server_url") or "",
    }


@router.get("/tasks/{task_id}/state")
def runtime_task_state(task_id: str, credential_id: int = Depends(_require_credential)):
    """worker 端轻量状态查询：只返回 status，用于 sid 看门狗检测幽灵任务。

    worker 的 active_sessions 是内存 set，_wrap 的 finally 在 opencode 进程
    hang 住时永远不执行 → sid 永久泄漏 → 同 sid 后续任务全部被假冲突挡住。
    worker 主循环定期调本端点查 active_tasks 里的 task 是否已在服务端终态，
    是则主动清理 active_sessions（seq#1485 根因）。
    """
    task = service.get_task(task_id)
    if not task:
        return {"status": "not_found"}
    return {"status": task.get("status") or ""}


@router.post("/tasks/{task_id}/cancel", operation_id="runtime_task_cancel", summary="Cancel task", description="Cancels a task (status=cancelled); the worker exits on the next heartbeat.")
def cancel_task(task_id: str, user: dict = Depends(require_no_session_token)):
    """取消任务（M2-5）：置 cancelled；执行侧由 worker 感知退出。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    service.cancel_task(task_id)
    # autopilot 联动：杀死只写 agent_tasks，autopilot_executions 无人回写会永远停在
    # running —— event_type=autopilot_event 时同步 exec 终态（cancelled）
    if (task.get("event_type") or "") == "autopilot_event" and task.get("event_seq"):
        try:
            from src.runtime.scheduler import _update_execution_result
            _update_execution_result(int(task["event_seq"]), "cancelled", error="用户手动终止")
        except Exception as e:
            log.warning(f"Cancel: autopilot exec sync failed (task={task_id}, event_seq={task.get('event_seq')}): {e}")
    return {"message": f"任务 {task_id} 已取消", "status": "cancelled"}


@router.post("/tasks/{task_id}/converge-event", operation_id="runtime_task_converge_event",
             summary="Converge event state to task terminal state",
             description="Issue #587 compensation entry: task already terminal (completed/failed/cancelled) "
                         "but associated event stuck running/pending (report lost). Converges event to the "
                         "task's terminal status. Idempotent; safe to call repeatedly.")
def converge_event(task_id: str, user: dict = Depends(require_no_session_token)):
    """Issue #587：回报丢失补偿重试入口——按任务终态强制收敛关联事件（幂等）。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    _res = service.converge_event_to_task(task_id)
    log.info(f"converge-event: task={task_id} by={user.get('username', '')} result={_res.get('status')}")
    # Issue #587 审查修复：补偿收敛同为管理端修复操作，与 terminate/redispatch 一致强制审计留痕
    if task.get("event_seq"):
        try:
            from src.api.routes.events import _audit_admin_action
            _audit_admin_action(task.get("repo_key", "") or "", int(task["event_seq"]),
                                "converge-event", user, task_id=task_id)
        except Exception as _ae:
            log.warning(f"converge-event: audit failed for task={task_id}: {_ae}")
    return _res


@router.get("/runtimes/{runtime_id}/tasks", operation_id="runtime_tasks_list", summary="List runtime tasks", description="Tasks of a device (front-end device view).")
def list_runtime_tasks(runtime_id: int, user: dict = Depends(require_no_session_token),
                             status: str = "",
                             limit: int = Query(50, ge=1, le=500, description="最多返回条数，1-500"),
                             repo: str = ""):
    """设备任务列表（M4 前端设备 [查看任务]）。"""
    from src.session_manager import db, db_lock
    clauses = ["runtime_id=?"]
    params = [runtime_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        repo_key = f"gitcode:{_base}"
        _rk_list = [f"gitcode:{_base}", f"github:{_base}"]
        clauses.append(f"repo_key IN ({','.join('?' * len(_rk_list))})")
        params.extend(_rk_list)
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT id, repo_key, agent_type, skill, status, exit_code, result_text, created_at, started_at, completed_at, issue_id, event_seq FROM agent_tasks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
                  params + [limit])
        return {"tasks": [
            {"id": r[0], "repo_key": r[1], "agent_type": r[2], "skill": r[3],
             "status": r[4], "exit_code": r[5], "result_text": (r[6] or "")[:200],
             "created_at": normalize_event_time(r[7]), "started_at": normalize_event_time(r[8]), "completed_at": normalize_event_time(r[9]),
             "issue_id": r[10] or "", "event_seq": r[11] or 0}
            for r in c.fetchall()
        ]}


@router.get("/runtimes/{runtime_id}/sessions", operation_id="runtime_sessions_list", summary="List runtime sessions", description="Engine sessions filtered by agent_tasks.runtime_id; only sessions with matching tasks are returned.")
def list_runtime_sessions(runtime_id: int, user: dict = Depends(require_no_session_token),
                                repo: str = ""):
    """引擎 session 列表。

    按 agent_tasks.runtime_id 过滤——同一个 session（repo+agent_type）的
    不同 task 可能在不同引擎上执行，因此不按 session.runtime_id 过滤，
    而是查所有 session 后按 task 的 runtime_id 过滤 tasks。
    只返回有匹配 task 的 session。
    """
    from src.session_manager import db, db_lock

    repo_clause = ""
    params: list = []
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        _rk_list = [f"gitcode:{_base}", f"github:{_base}"]
        repo_clause = f" AND repo_key IN ({','.join('?' * len(_rk_list))})"
        params.extend(_rk_list)

    with db_lock:
        c = db.cursor()
        # 查所有 session（按 repo 过滤），不按 session.runtime_id 过滤
        c.execute(
            f"SELECT id, agent_type, repo_key, status, skill, created_at, last_active_at, "
            f"runtime_session_id "
            f"FROM devops_agent_session WHERE 1=1{repo_clause} ORDER BY last_active_at DESC",
            params
        )
        sessions = []
        for r in c.fetchall():
            sid = r[0]
            agent_type = r[1] or ""
            repo_key_raw = r[2] or ""
            # 查该 session 当前正在运行的 task（按 runtime_id 过滤）
            c.execute(
                "SELECT id, issue_id, event_seq, agent_type, status, exit_code, created_at, started_at "
                "FROM agent_tasks WHERE agent_session_id=? AND runtime_id=? "
                "AND status IN ('running','queued','dispatched') ORDER BY created_at DESC LIMIT 1",
                (sid, runtime_id)
            )
            current = None
            cr = c.fetchone()
            if cr:
                current = {
                    "id": cr[0], "issue_id": cr[1] or "", "event_seq": cr[2] or 0,
                    "agent_type": cr[3] or "", "status": cr[4] or "",
                    "created_at": normalize_event_time(cr[5]),
                    "started_at": normalize_event_time(cr[6]),
                }
            # 查该 session 最近已完成的任务（按 runtime_id 过滤）
            c.execute(
                "SELECT id, issue_id, event_seq, agent_type, status, exit_code, created_at, started_at, completed_at "
                "FROM agent_tasks WHERE agent_session_id=? AND runtime_id=? "
                "AND status IN ('completed','failed','cancelled') ORDER BY event_seq DESC LIMIT 20",
                (sid, runtime_id)
            )
            tasks = []
            for tr in c.fetchall():
                tasks.append({
                    "id": tr[0], "issue_id": tr[1] or "", "event_seq": tr[2] or 0,
                    "agent_type": tr[3] or "", "status": tr[4] or "",
                    "exit_code": tr[5], "created_at": normalize_event_time(tr[6]),
                    "started_at": normalize_event_time(tr[7]),
                    "completed_at": normalize_event_time(tr[8]),
                })
            # 只返回有匹配 task 的 session（current_task 或 tasks 非空）
            if not current and not tasks:
                continue
            sessions.append({
                "id": sid,
                "agent_type": agent_type,
                "repo_key": repo_key_raw.split(":", 1)[1] if ":" in repo_key_raw else repo_key_raw,
                "status": "running" if current else "idle",
                "skill": r[4] or "",
                "created_at": normalize_event_time(r[5]),
                "last_active_at": normalize_event_time(r[6]),
                "runtime_session_id": r[7] or "",
                "current_task": current,
                "tasks": tasks,
            })
        return {"sessions": sessions, "total": len(sessions)}


@router.get("/credentials/{cid}/tasks", operation_id="runtime_credential_tasks", summary="Tasks aggregated by credential", description="Tasks aggregated by credential across all its engine instances.")
def list_credential_tasks(cid: int, user: dict = Depends(require_no_session_token),
                                status: str = "", limit: int = 50):
    """按凭据聚合任务（该凭据所有引擎实例的 agent_tasks，见 runtime-admin-two-view-design.md §4.3）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM agent_runtimes WHERE credential_id=?", (cid,))
        rid_rows = c.fetchall()
    if not rid_rows:
        return {"tasks": []}
    rids = [r[0] for r in rid_rows]
    ph = ",".join("?" for _ in rids)
    clauses = [f"runtime_id IN ({ph})"]
    params = list(rids)
    if status:
        clauses.append("status=?")
        params.append(status)
    params.append(limit)
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT id, repo_key, agent_type, skill, status, exit_code, result_text, created_at, started_at, completed_at FROM agent_tasks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
                  params)
        return {"tasks": [
            {"id": r[0], "repo_key": r[1], "agent_type": r[2], "skill": r[3],
             "status": r[4], "exit_code": r[5], "result_text": (r[6] or "")[:200],
             "created_at": normalize_event_time(r[7]), "started_at": normalize_event_time(r[8]), "completed_at": normalize_event_time(r[9])}
            for r in c.fetchall()
        ]}


def _server_url() -> str:
    try:
        # Issue #471 E1（QA 建议）：register 响应的 server_url 是 worker 回连地址
        # （设备/CLI 回连服务端用），与 devstage RUNTIME_SERVER_URL / 任务 server_url
        # 同语义——对齐 get_worker_connect_url（未配置回退 server_base_url）。
        from src.user.db import get_worker_connect_url
        return get_worker_connect_url()
    except Exception:
        return ""


@router.get("/install")
def runtime_install_script():
    """返回 Worker 一键安装脚本（只读，无敏感信息；token 由调用方以 --token 注入）。

    见 worker-server-delivery-design.md。用法：
      curl -sSL <server>/api/runtime/install | bash -s -- --token <rta_...>
    """
    from fastapi.responses import PlainTextResponse
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                               "scripts", "install-worker.sh")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "install script not found"})
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "read install script failed"})
    return PlainTextResponse(content, media_type="text/plain")


@router.get("/install.ps1")
def runtime_install_script_ps1():
    """返回 Windows Worker PowerShell 一键安装脚本（只读，无敏感信息）。

    用法（Windows 设备上执行）：
      irm <server>/api/runtime/install.ps1 | iex
    或带参数：
      & ([scriptblock]::Create((irm <server>/api/runtime/install.ps1))) -Token <rta_...> -Server <server>

    BOM 策略（Issue #298 GBK 乱码复盘）：本端点不预置 UTF-8 BOM——
    字符串执行路径（irm | iex / [scriptblock]::Create）中 BOM 会被解码为
    U+FEFF 字符残留在脚本开头，导致 param() 解析失败（实测 2026-09-29）。
    写文件执行路径（_perform_upgrade 下载到临时 .ps1）由下载方补 BOM
    （agent.py 已有 startswith(bom) 检测），两场景互不干扰。
    GBK 防护靠脚本本体 ASCII 化日志 + charset=utf-8 响应头。
    """
    from fastapi.responses import PlainTextResponse
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                               "scripts", "install-worker.ps1")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "install script not found"})
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "read install script failed"})
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@router.get("/start-worker.ps1")
def runtime_start_worker_script_ps1():
    """返回 Windows Worker 启动脚本 start-worker.ps1（只读）。

    供 install-worker.ps1 下载后写入文件，避免 here-string 转义问题。
    与 install.ps1 不同：本端点消费方是 irm -OutFile（写文件后 -File 执行），
    无字符串直传场景——预置 UTF-8 BOM 落盘后 PS5.1 可正确识别编码，
    中文注释不乱码。脚本本体含中文注释（60行），必须带 BOM。
    """
    from fastapi.responses import PlainTextResponse
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                               "scripts", "start-worker.ps1")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "start-worker script not found"})
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "read start-worker script failed"})
    body = b"\xef\xbb\xbf" + content.encode("utf-8")
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@router.get("/install/latest")
def runtime_install_latest(credential_id: int = Depends(_require_credential)):
    """返回当前 Worker 代码版本号（rta_ 鉴权）。

    供 worker 启动自检（见 worker-server-delivery-design.md §2.4）。
    返回 worker_code_version（仅 git commit），与 heartbeat latest_version 一致。
    """
    from src.worker_package import worker_code_version
    return {"version": worker_code_version()}


@router.get("/grape-worker.tar.gz")
def runtime_worker_package_public():
    """公开下发 Worker 绿色安装包 tar.gz（无需鉴权）。

    包内仅含 worker 运行代码（无任何敏感信息，凭据由安装命令 --token 注入），
    供「凭据申请页一键安装命令」直接 curl 下载：
      curl -fsSL <server>/api/runtime/grape-worker.tar.gz -o grape-worker.tar.gz
    首次请求动态打包 + 缓存（代码变更自动重建，见 src/worker_package.py）。

    并发限制：同时下载不超过 PKG_DOWNLOAD_MAX_CONCURRENT（默认 3），
    超出返回 429 让 worker 重试。

    P2-D (Issue #357): When GRAPE_WORKER_NGINX_REDIRECT=1 is set, returns
    X-Accel-Redirect header so nginx serves the file directly (offloads API process).
    """
    global _pkg_download_count
    with _pkg_download_lock:
        if _pkg_download_count >= _PKG_DOWNLOAD_LIMIT:
            raise HTTPException(status_code=429, detail={"code": 429, "message": "too many concurrent downloads, retry later"})
        _pkg_download_count += 1
    try:
        from fastapi.responses import FileResponse, Response
        from src.worker_package import build_package
        try:
            pkg = build_package()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"build worker package failed: {e}"})
        if not pkg or not os.path.isfile(pkg):
            raise HTTPException(status_code=500, detail={"code": 500, "message": "worker package not found"})
        # P2-D: nginx X-Accel-Redirect mode — let nginx serve the file directly
        if os.environ.get("GRAPE_WORKER_NGINX_REDIRECT", "").lower() in ("1", "true", "yes"):
            # Convert absolute path to nginx-internal path (configured in nginx.conf)
            # nginx location: location /_internal/ { internal; alias /; }
            _nginx_path = pkg[1:] if pkg.startswith("/") else pkg
            return Response(
                headers={
                    "Content-Type": "application/gzip",
                    "Content-Disposition": f'attachment; filename="grape-worker.tar.gz"',
                    "X-Accel-Redirect": f"/_internal/{_nginx_path}",
                },
                status_code=200,
            )
        return FileResponse(pkg, media_type="application/gzip", filename="grape-worker.tar.gz")
    finally:
        with _pkg_download_lock:
            _pkg_download_count -= 1


@router.get("/install/package")
def runtime_install_package(credential_id: int = Depends(_require_credential)):
    """下发 Worker 安装包 tar.gz（rta_ 鉴权，首次请求动态打包 + 缓存）。

    见 worker-server-delivery-design.md §2.1。用法（安装脚本内部调用）：
      curl -fsSL -H "Authorization: Bearer <rta_...>" <server>/api/runtime/install/package
    """
    from fastapi.responses import FileResponse
    from src.worker_package import build_package
    try:
        pkg = build_package()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"build worker package failed: {e}"})
    if not pkg or not os.path.isfile(pkg):
        raise HTTPException(status_code=500, detail={"code": 500, "message": "worker package not found"})
    return FileResponse(pkg, media_type="application/gzip", filename="grape-worker.tar.gz")


# ===== Engine local_skills 刷新 =====


def engine_onboarding_repo_key(repo: str) -> str:
    """engine-onboarding 的 repo 匹配值：契约单一，只接受规范带前缀形态。

    - 带 provider 前缀（gitcode:owner/repo，即前端 currentRepo().repoKey，
      DB 直出）→ to_repo_key 归一后返回，SQL 等值匹配
    - 裸 owner/repo / 未知形态 → 返回空串（视为无仓库上下文，走全局保守判定），
      **不做前缀猜测**——查表定前缀（LIMIT 1 无序）在双平台同名仓库时命中
      不确定，是 IN 撒网问题的换皮；本端点是读取路径，调用方拿得到的始终是
      DB 直出的规范形态，裸形态输入在本语境不存在，防御它只会引入错误面。

    返回空串表示无仓库上下文。
    """
    raw = (repo or "").strip()
    if not raw or ":" not in raw:
        return ""
    from src.repo_key import to_repo_key
    return to_repo_key(raw)


@router.get("/engine-onboarding", operation_id="runtime_engine_onboarding",
            summary="Engine onboarding status for one-click deployments",
            description="Returns server_base_url (auto-healed via Host header), one-click worker install commands, "
                        "local engine discovery results, and registered remote engines. needs_worker=true when no "
                        "usable engine exists (only the default offline opencode record), guiding the user to attach "
                        "an engine machine via an rta_ credential instead of installing engines in-place.")
def runtime_engine_onboarding(repo: str = "", user: dict = Depends(require_org_member)):
    """一键部署引擎接入引导（编排入口，零新机制：全部复用既有凭据/安装/注册协议）。

    解决的问题：AI 一键部署 Grape 到平台沙箱（PATH 隔离）后，discover_backends
    扫不到任何引擎，引擎列表只剩默认 opencode 离线记录，用户无从知晓正确
    接入方式是「在装了引擎的机器上跑 worker 拿 rta_ 凭据注册」而非本地装引擎。

    可见性语义（对齐 dashboard.py 仓库级/全局分层惯例；repo 参数契约：只接受
    规范带前缀形态 gitcode:owner/repo，即前端 currentRepo().repoKey——裸形态
    视为无仓库上下文走全局判定，不做前缀猜测）：
    - 带 repo（仓库上下文）：global + 自己的 user-scope + 该 repo 的 worker
    - 不带 repo（全局引导页/横幅）：global + 自己的 user-scope

    返回：
    - server_base_url：get_server_base_url()（Host 头自愈后即为正确公网地址）
    - install_cmd_linux / install_cmd_windows：--server 已代入的一键安装命令
    - local_engines：discover_backends() 本机扫描（沙箱内通常全空）
    - remote_engines：agent_runtimes 在线引擎（按上述可见性过滤）
    - needs_worker：无任何可用引擎（本地全未装 且 可见远端零在线）时 true
    """
    from src.user.db import get_server_base_url
    from src.agent.executor import discover_backends
    from src.repo_key import from_repo_key

    base = (get_server_base_url() or "").rstrip("/")
    local = discover_backends()
    remote = []
    _user_visible = []  # 预置：查询异常时保守按"无可用引擎"处理（引导继续提示）
    _user = user.get("username") or ""
    _uid = str(user.get("id") or "")
    _org_id = user.get("org_id", 0) or 0
    # repo 匹配值：单一真源等值匹配（engine_onboarding_repo_key）——带前缀原样、
    # 裸形态查 managed_repos 定唯一前缀。不做多形态撒网 IN（跨平台同名仓库泄漏）。
    _rk = engine_onboarding_repo_key(repo or "")
    try:
        with db_lock:
            c = db.cursor()
            # 判定语义：本端点回答"该用户是否还有可用的执行引擎"——
            # global（本组织内免提示）+ 本人 user-scope + 本人名下所有 repo-scope
            # （不限当前 repo）。repo_key 用于 remote_engines 的上下文过滤（展示）。
            #
            # 列名修复：此前误用 runtime_credentials 的列名（device_name/owner_name）
            # 查 agent_runtimes——SQLite 抛 no such column 被 except 吞掉，
            # remote 恒空、有在线 worker 的部署也被误报 needs_worker=true。
            # 归属三态（注册链路 runtime.py:160-202）：user-scope 行
            # owner_id=str(grape_accounts.id)；repo-scope 行 owner_id=仓库全名
            # （须 JOIN runtime_credentials.owner_name 才能按用户名覆盖）；
            # global 行 owner_id=''。org 隔离与 dashboard/settings 引擎列表同惯例。
            c.execute(
                "SELECT ar.id, ar.name, ar.status, "
                "COALESCE(NULLIF(ar.host_name,''), ar.display_name, ar.name) AS device_name, "
                "ar.scope, ar.repo_key "
                "FROM agent_runtimes ar "
                "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
                "WHERE ar.status='online' "
                "AND (ar.org_id=0 OR ar.org_id=?) "
                "AND (ar.scope='global' OR ar.owner_id=? OR LOWER(rc.owner_name)=LOWER(?)) "
                "ORDER BY ar.id",
                (_org_id, _uid, _user),
            )
            # 列名取值（不按列序）——SELECT 列调整不会静默错位（复盘 #3）
            _cols = [d[0] for d in (c.description or [])]
            _all = [{k: row[i] for i, k in enumerate(_cols)} for row in (c.fetchall() or [])]
            # needs_worker 判定：用户视角全部（global + 本人一切 scope）
            _user_visible = _all
            # 展示列表：repo 上下文时按该 repo 过滤；全局上下文不含他人 repo-scope
            if _rk:
                remote = [e for e in _all if e.get("scope") != "repo" or e.get("repo_key") == _rk]
            else:
                remote = [e for e in _all if e.get("scope") != "repo"]
    except Exception as e:
        log.warning(f"engine-onboarding: query agent_runtimes failed: {e}")

    local_usable = [e["name"] for e in local if e.get("installed")]
    needs_worker = not local_usable and not _user_visible
    return {
        "server_base_url": base,
        "install_cmd_linux": f"curl -fsSL {base}/api/runtime/install | bash -s -- --server {base} --token <rta_凭据>",
        "install_cmd_windows": f"irm {base}/api/runtime/install.ps1 | iex  # -Token <rta_凭据> -Server {base}",
        "credential_api": f"POST {base}/api/settings/runtime-credentials",
        "local_engines": local,
        "remote_engines": remote,
        "needs_worker": needs_worker,
    }


def _scan_engine_skills_detail(engine_name: str) -> list[dict]:
    """本机扫描指定 engine 的 skill 详情（description / 路径 / 文件数）。"""
    from src.runtime.agent import _scan_local_skills
    import os as _os

    all_skills = _scan_local_skills()
    engine_key = (engine_name or "").strip().lower()
    skill_names = []
    for entry in all_skills:
        if (entry.get("engine") or "").strip().lower() == engine_key:
            skill_names = entry.get("skills") or []
            break

    from src.runtime.os_adapter import engine_skill_dirs, extra_skill_dirs
    scan_dirs = list(engine_skill_dirs(engine_key))
    scan_dirs.extend(extra_skill_dirs())

    skill_details = []
    for sname in skill_names:
        detail = {"name": sname, "description": "", "path": "", "file_count": 0, "provider": engine_name}
        for base_dir in scan_dirs:
            p1 = _os.path.join(base_dir, sname)
            if _os.path.isfile(_os.path.join(p1, "SKILL.md")):
                detail["path"] = p1
                break
            if _os.path.isdir(base_dir):
                for cat in _os.listdir(base_dir):
                    p2 = _os.path.join(base_dir, cat, sname)
                    if _os.path.isfile(_os.path.join(p2, "SKILL.md")):
                        detail["path"] = p2
                        break
                if detail["path"]:
                    break
        if detail["path"]:
            try:
                with open(_os.path.join(detail["path"], "SKILL.md"), "r", encoding="utf-8") as f:
                    content = f.read()
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        fm = content[3:end]
                        for line in fm.split("\n"):
                            if line.strip().startswith("description:"):
                                desc = line.split("description:", 1)[1].strip()
                                desc = desc.strip("\"'")
                                if desc:
                                    detail["description"] = desc[:200]
                                break
            except Exception:
                pass
            try:
                detail["file_count"] = sum(1 for _ in _os.walk(detail["path"]))
            except Exception:
                pass
        skill_details.append(detail)
    return skill_details


@router.post("/runtimes/{runtime_id}/refresh-skills", operation_id="runtime_refresh_skills", summary="Rescan runtime skills", description="Rescans an engine's local skills and returns the detail list. Local engines scan directly; remote workers set pending_skill_scan and report on the next claim.")
def runtime_refresh_skills(runtime_id: int, force: bool = False, user: dict = Depends(require_no_session_token)):
    """重新扫描指定 engine 的本地 skill 并返回详情列表。

    本机直连引擎（credential_id=0）：直接扫描本机目录，立即返回结果。
    远端 worker 引擎（credential_id>0）：标记 pending_skill_scan=1，
    worker 下次 claim 时收到 scan_request 指令，执行扫描后回报。
    前端需轮询本接口获取结果（返回 status=pending 时继续轮询）。
    """
    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("SELECT name, credential_id, pending_skill_scan, skill_scan_result FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
    engine_name, cred_id, pending, scan_result = row[0], row[1], row[2], row[3]

    # 本机直连：直接扫描
    if cred_id == 0:
        skill_details = _scan_engine_skills_detail(engine_name)
        _ls_json = json.dumps([s["name"] for s in skill_details], ensure_ascii=False)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET local_skills=?, skill_scan_result=?, pending_skill_scan=0 WHERE id=?",
                      (_ls_json, json.dumps(skill_details, ensure_ascii=False), runtime_id))
            db.commit()
        return {"skills": skill_details, "count": len(skill_details), "status": "done"}

    # 远端 worker：标记扫描指令，等待 worker 回报
    if pending:
        # 已有 pending 标记 — 检查是否已有结果
        if scan_result:
            return {"skills": json.loads(scan_result), "count": len(json.loads(scan_result)),
                    "status": "done", "cached": True}
        return {"status": "pending", "skills": [], "count": 0}

    # pending=0：如果已有缓存结果且非 force，直接返回（轮询场景下 worker 已回报）
    if scan_result and not force:
        _cached = json.loads(scan_result)
        return {"skills": _cached, "count": len(_cached), "status": "done", "cached": True}

    # 首次扫描或 force 重新扫描：标记 pending
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET pending_skill_scan=1, skill_scan_result='' WHERE id=?", (runtime_id,))
        db.commit()
    return {"status": "pending", "skills": [], "count": 0}


@router.post("/runtimes/{runtime_id}/skills-report")
def runtime_skills_report(runtime_id: int, body: dict, credential_id: int = Depends(_require_credential)):
    """worker 回报 skill 扫描结果。

    worker claim 时收到 scan_request → 执行扫描 → 调本接口回报。
    """
    from src.session_manager import db, db_lock

    # 校验 runtime 归属
    with db_lock:
        c = db.cursor()
        c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row or row[0] != credential_id:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "runtime not owned by credential"})

    skills = body.get("skills") or []
    skill_names = [s.get("name", "") for s in skills if s.get("name")]
    _ls_json = json.dumps(skill_names, ensure_ascii=False)
    _result_json = json.dumps(skills, ensure_ascii=False)

    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET local_skills=?, skill_scan_result=?, pending_skill_scan=0 WHERE id=?",
                  (_ls_json, _result_json, runtime_id))
        db.commit()
    return {"status": "ok"}


# ===== Engine 角色标签 =====

def _apply_tag_rules(tags: list[str]) -> list[str]:
    """应用标签互斥规则。
    - 全能与其他互斥：有其他标签时去掉全能。
    - 5 个角色全选时自动替换为全能。
    - 允许空标签（不自动回全能）。
    """
    AGENT_TAGS = {"vod", "issue", "coding", "qa", "sre"}
    tag_set = set(tags)
    has_all = "all" in tag_set
    has_others = tag_set & AGENT_TAGS

    if has_all and has_others:
        tag_set.discard("all")
    elif has_others and len(has_others) == len(AGENT_TAGS):
        tag_set = {"all"}

    return sorted(tag_set)


@router.get("/runtimes/{runtime_id}/role-tags", operation_id="runtime_role_tags", summary="Runtime role tags", description="Role tags of an engine (web admin view).")
def get_runtime_tags(runtime_id: int, user: dict = Depends(require_no_session_token)):
    """返回 engine 的角色标签列表（web 管理视图；worker 取 tags 用 GET /runtimes/{id}/tags）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT agent_tags FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
    return {"tags": json.loads(row[0]) if row[0] else []}


@router.put("/runtimes/{runtime_id}/tags", operation_id="runtime_tags_set", summary="Set role tags", description="Sets an engine's role tags (mutual-exclusion applied). engine_capabilities is the single source; agent_tags is the derived cache.")
def set_runtime_tags(runtime_id: int, body: dict, user: dict = Depends(require_no_session_token)):
    """设置 engine 的角色标签。应用互斥规则后写入。

    以 engine_capabilities 为单一数据源，agent_tags 列为派生缓存。
    写入顺序：先写 engine_capabilities，再 _sync_agent_tags 回写 agent_tags。
    """
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    from src.api.routes.settings import _sync_agent_tags
    tags = _apply_tag_rules(body.get("tags") or [])
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        # 先写 engine_capabilities：先删旧标签，再插新标签
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (runtime_id,))
        for tag in tags:
            try:
                c.execute(
                    "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                    "VALUES (?,?,1,50,1,?,?)",
                    (runtime_id, tag, now, now)
                )
            except Exception:
                pass
        # 标记用户已手动设过标签，心跳注册不再自动补 'all'
        c.execute("UPDATE agent_runtimes SET tags_user_set=1 WHERE id=?", (runtime_id,))
        # 再回写 agent_tags（派生缓存，保持与 engine_capabilities 一致）
        _sync_agent_tags(runtime_id, c)
        db.commit()
    log.info(f"Runtime tags set: rid={runtime_id} tags={tags} (engine_capabilities → agent_tags synced)")
    return {"tags": tags}


class MaxConcurrentBody(BaseModel):
    max_concurrent: int = 20


@router.put("/runtimes/{runtime_id}/max-concurrent", operation_id="runtime_max_concurrent_set", summary="Set max concurrent", description="Sets the engine-level max concurrency.")
def set_runtime_max_concurrent(runtime_id: int, body: MaxConcurrentBody,
                                user: dict = Depends(require_no_session_token)):
    """设置引擎级最大并发数（方案一：并发纯引擎级，标签只做路由）。"""
    from src.session_manager import db, db_lock
    if body.max_concurrent < 1 or body.max_concurrent > 100:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "max_concurrent 必须在 1-100 之间"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        c.execute("UPDATE agent_runtimes SET max_concurrent=? WHERE id=?", (body.max_concurrent, runtime_id))
        db.commit()
    log.info(f"Runtime max_concurrent set: rid={runtime_id} max_concurrent={body.max_concurrent}")
    return {"max_concurrent": body.max_concurrent}


# ===== 场景标签 =====

@router.get("/scene-tags", operation_id="runtime_scene_tags_list", summary="List scene tags", description="All scenes and their available role tags for the scene picker: [{name, display_name, roles: [...]}].")
def list_scene_tags(user: dict = Depends(require_no_session_token)):
    """返回场景市场所有场景及其可用角色标签（供前端场景选择器使用）。

    返回格式：[{name, display_name, roles: ["vod","issue","coding","qa","sre"]}, ...]
    """
    from src.user.db import list_scene_market
    scenes, _ = list_scene_market(limit=0)
    result = []
    for s in scenes:
        nodes = json.loads(s.get("nodes_json") or "[]") if isinstance(s.get("nodes_json"), str) else (s.get("nodes_json") or [])
        roles = []
        for n in nodes:
            if isinstance(n, dict) and n.get("tag") and n["tag"] not in roles:
                roles.append(n["tag"])
        result.append({
            "name": s["name"],
            "display_name": s.get("display_name") or s["name"],
            "roles": roles,
        })
    return {"scenes": result}


@router.get("/runtimes/{runtime_id}/scene-tags", operation_id="runtime_scene_tags_get", summary="Runtime scene tags", description="Scene tags of an engine.")
def get_runtime_scene_tags(runtime_id: int, user: dict = Depends(require_no_session_token)):
    """返回引擎的场景标签列表。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT scene_tags FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
    tags = json.loads(row[0]) if row[0] else ["default"]
    return {"scene_tags": tags}


class SceneTagsBody(BaseModel):
    scene_tags: list[str] = ["default"]


@router.put("/runtimes/{runtime_id}/scene-tags", operation_id="runtime_scene_tags_set", summary="Set runtime scene tags", description="Sets an engine's scene tags (metadata constraining selectable role tags).")
def set_runtime_scene_tags(runtime_id: int, body: SceneTagsBody,
                            user: dict = Depends(require_no_session_token)):
    """设置引擎的场景标签。

    场景标签是元数据，约束可选角色标签范围。
    保存时不修改角色标签——前端根据场景的可用角色自动过滤角色标签 checkbox。
    """
    tags = body.scene_tags or ["default"]
    # 去重保序
    seen = set()
    deduped = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        c.execute("UPDATE agent_runtimes SET scene_tags=? WHERE id=?", (json.dumps(deduped, ensure_ascii=False), runtime_id))
        db.commit()
    log.info(f"Runtime scene_tags set: rid={runtime_id} scene_tags={deduped}")
    return {"scene_tags": deduped}


# ===== 引擎别名 =====

class AliasBody(BaseModel):
    alias: str = ""


@router.put("/runtimes/{runtime_id}/alias", operation_id="runtime_alias_set", summary="Set runtime alias", description="Sets an engine alias; an empty string clears it.")
def set_runtime_alias(runtime_id: int, body: AliasBody,
                       user: dict = Depends(require_no_session_token)):
    """设置引擎别名。空字符串表示清除别名。"""
    alias = (body.alias or "").strip()
    if len(alias) > 64:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "alias 最长 64 字符"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
        c.execute("UPDATE agent_runtimes SET alias=? WHERE id=?", (alias, runtime_id))
        db.commit()
    log.info(f"Runtime alias set: rid={runtime_id} alias={alias!r}")
    return {"alias": alias}


# ===== MR-2b（2026-11-05）：worker trace 中继（LGTM 不对 worker 开放，经主控 C 进程转发）=====
# 设计：docs/weekly-10/worker-trace-relay-layer2-design.md
# - worker 的 OTLP/HTTP exporter 指向 {RUNTIME_SERVER_URL}/api/runtime/traces（既有通道+Bearer）
# - 本端点透传 body（protobuf/json/gzip 原样，零解析）→ OTLP_RELAY_TARGET/v1/traces
# - 鉴权复用 _require_credential（与 claim 同款）；失败透传状态码，业务零影响
OTLP_RELAY_TARGET = os.environ.get("OTLP_RELAY_TARGET", "").strip()

from fastapi.responses import Response as _RelayResponse

_relay_total = None
try:
    from prometheus_client import Counter as _PC
    from src.observability import registry as _obs_registry
    _relay_total = _PC("grape_trace_relay_requests_total",
                       "Trace relay requests forwarded to LGTM",
                       ["status"], registry=_obs_registry)
except Exception:
    _relay_total = None


@router.post("/traces", operation_id="runtime_traces_relay",
             summary="Relay worker OTLP traces to LGTM",
             description="Transparently forwards worker OTLP/HTTP trace exports to the LGTM "
                         "OTLP endpoint (env OTLP_RELAY_TARGET). Workers authenticate with "
                         "their existing runtime token. Failures pass through the upstream "
                         "status; LGTM unreachability returns 502 (worker exporters retry/drop "
                         "asynchronously, zero business impact).")
async def runtime_traces_relay(request: Request,
                               credential_id: int = Depends(_require_credential)):
    if not OTLP_RELAY_TARGET:
        return _RelayResponse(status_code=503,
                              content='{"detail":"trace relay disabled"}',
                              media_type="application/json")
    # 公开端点（虽要求 runtime token）仍需限体：OTLP 批量包通常 <512KB。
    # 架构守护 Medium-1：先按 content-length 拒绝，避免超大包整块读入内存造成尖峰。
    _max_body = int(os.environ.get("GRAPE_TRACE_RELAY_MAX_BODY", str(2 * 1024 * 1024)))
    if int(request.headers.get("content-length") or 0) > _max_body:
        return _RelayResponse(status_code=413,
                              content='{"detail":"payload too large"}',
                              media_type="application/json")
    body = await request.body()
    if len(body) > _max_body:
        return _RelayResponse(status_code=413,
                              content='{"detail":"payload too large"}',
                              media_type="application/json")
    ctype = request.headers.get("content-type", "application/x-protobuf")
    cenc = request.headers.get("content-encoding", "")
    fwd_headers = {"Content-Type": ctype}
    if cenc:
        fwd_headers["Content-Encoding"] = cenc
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as hc:
            r = await hc.post(OTLP_RELAY_TARGET.rstrip("/") + "/v1/traces",
                              content=body, headers=fwd_headers)
        if _relay_total is not None:
            try:
                _relay_total.labels(status=str(r.status_code)).inc()
            except Exception:
                pass
        return _RelayResponse(status_code=r.status_code, content=r.content,
                              media_type=r.headers.get("content-type", "application/x-protobuf"))
    except Exception as e:
        if _relay_total is not None:
            try:
                _relay_total.labels(status="relay_unreachable").inc()
            except Exception:
                pass
        log.warning(f"trace relay unreachable: {type(e).__name__}")
        return _RelayResponse(status_code=502,
                              content='{"detail":"lgtm unreachable"}',
                              media_type="application/json")
