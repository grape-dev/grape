"""Runtime API 路由。

M2：任务状态轮询 / 取消（用户 token）。
M3：/api/runtime/* 协议端点（register/heartbeat/claim/report），仅 rta_ 凭据可过（D2 凭据隔离）。
见 agent-runtime-event-full-design.md §6 / tech-impl.md §3。
"""

import asyncio, contextlib, hashlib, json, os, threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Depends, Request, Query
from pydantic import BaseModel, model_validator
from src.api.deps import require_no_session_token, require_org_member
from src.domains.runtime.engine.health import apply_runtime_state
from src.kernel.config import log
from src.kernel.domain_db import cursor as _domain_cursor

from src.api.time_utils import normalize_event_time
from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import service
from src.domains.runtime.engine.health import apply_runtime_state, parse_dt  # Issue #1496 unified writer

# Issue #1982 复审：spawn_failed 独立计数兜底阈值——连续 N 次 spawn_failed（资源类
# 环境故障长期不恢复）后心跳降级 healthy=0 并在 probe_error 附修复指引；
# ok/fail/model_unreachable 清零（进程能 spawn 即链断）。env 可调。
_PROBE_SPAWN_FAIL_TRIP = max(1, int(os.environ.get("RUNTIME_PROBE_SPAWN_FAIL_TRIP", "50")))

from src.kernel import db_registry as _dbreg


@contextlib.contextmanager
def _runtime_cursor():
    """runtime 域独立连接（三库拓扑）；单库/测试拓扑回落进程主库 + db_lock。

    agent 的高频端点（心跳、标签、认领）不能挂在进程级 db_lock 上：sweeper 持锁做
    整表扫描时，这些请求全部排队到客户端 sock_read 超时，表现为「引擎经常失联」
    （心跳连续失败三次即 offline），而不是“服务变慢”。实测 2026-11-15：
    远端两个凭据下的四个引擎心跳长期停在 137s（实测）。
    """
    if _dbreg.uses_own_dsn("runtime"):
        # 必须是**独立连接**：db_registry.cursor() 在同域时复用 session_manager.db
        # 这个进程主连接，等于仍然和持锁代码抢同一个 socket，改造形同虚设。
        conn = _dbreg.new_connection("runtime", autocommit=True)
        try:
            yield conn.cursor()
        finally:
            try:
                conn.close()
            except Exception as _ce:
                log.debug(f"runtime 独立连接关闭失败: {_ce}")
        return
    with db_lock:
        yield db.cursor()

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
      （真实故障仍由心跳连败路径（probe_fail_count≥2 → 0）正常摘除；
      spawn_failed 另有独立计数 probe_spawn_fail_count 兜底，见 runtime_heartbeat）。

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
        # 凭据校验失败要留痕：worker 侧只看到 401，服务端此前完全无记录，
        # 设备一直"离线"时无法判断是 token 抄错、还是根本没连上来。
        try:
            log.warning(f"runtime credential rejected: prefix={token[:10]}... ip={_client_ip(request)}")
        except Exception:
            pass
        raise HTTPException(status_code=401, detail={"code": 401, "message": "runtime credential revoked or not found"})
    return row[0]


@dataclass
class CredentialContext:
    """[沙箱迁移] 认证层返回的凭证对象——一次查表，认证 + 授权两层消费（i7 移植）。

    scope/owner 等字段来自 runtime_credentials 表（token 签发时写死），
    不是来自注册请求体——注册请求不能篡改 scope。
    """
    id: int
    scope: str            # global / repo / user
    owner_name: str       # 用户名（user scope）或空
    owner_id: str         # user scope 时对齐 user_accounts.id；repo scope 时为 repo_key
    repo_key: str         # repo scope 时绑定的仓库全名


def _require_credential_ctx(request: Request) -> CredentialContext:
    """[沙箱迁移] 完整凭证上下文（新调用方）。

    解析 Authorization: Bearer rta_*** → sha256 → 查 runtime_credentials(status=active)。
    user scope 时 LEFT JOIN user_accounts 解析 owner_id 为数值 ID。
    """
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not token.startswith("rta_"):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "invalid runtime credential"})
    h = hashlib.sha256(token.encode()).hexdigest()
    with db_lock:
        c = db.cursor()
        # 三库拓扑：user_accounts 归 mgmt，不能 JOIN（JOIN 要求同库）。
        # 先读本域凭据行，再按 username 通过 mgmt public 解析数值 owner id。
        c.execute(
            "SELECT id, scope, owner_name, owner_id, repo_key "
            "FROM runtime_credentials WHERE token_hash=? AND status='active'", (h,))
        row = c.fetchone()
        _ua_id = None
        if row and (row[1] or "").strip().lower() == "user" and (row[2] or ""):
            try:
                from src.domains.mgmt import public as _mg_ua
                _ua_id = _mg_ua.active_account_id_by_username(row[2]) or None
            except Exception as _e_ua:  # noqa: BLE001
                # 解析失败不该拦住鉴权：下游按 fail-closed 处理（owner_id 退回自带列）
                log.warning(f"whoami ctx: owner id resolution failed ({_e_ua})")
        if row:
            row = (row[0], row[1], row[2], row[3], row[4], _ua_id)
    if not row:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "runtime credential revoked or not found"})
    _scope = (row[1] or "global").strip().lower()
    if _scope == "user" and row[5]:
        _owner_id = str(row[5])
    else:
        _owner_id = row[3] or ""
    return CredentialContext(
        id=row[0],
        scope=_scope,
        owner_name=row[2] or "",
        owner_id=_owner_id,
        repo_key=row[4] or "",
    )


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


class RecoverOrphansBody(BaseModel):
    """Issue #1750：worker 自报活跃任务白名单（可选）。

    调用方（worker）在请求体里显式携带内存 active_tasks 的 task id；命中白名单的
    任务视为「worker 自认仍在执行」，直接 skipped_active，优先于年龄闸门与活跃窗口。
    旧版 worker 发空 body → 默认空列表，行为等同现状（字段可选、平滑兼容）。
    """
    active_task_ids: list[str] = []


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



def _parse_ri_platform(ri_raw) -> str:
    """[沙箱迁移] 从 runtime_info JSON 提取 platform（cloud_worker 注册的云引擎元数据，i7 移植）。"""
    try:
        import json as _json
        _ri = _json.loads(ri_raw) if isinstance(ri_raw, str) and ri_raw.strip() else (ri_raw or {})
        return (_ri.get("platform") or "").strip() if isinstance(_ri, dict) else ""
    except Exception:
        return ""

@router.post("/register", operation_id="runtime_register", summary="Register worker engines", description="Worker credential auth + idempotent agent_runtimes upsert by instance key; returns authoritative runtime ids.")
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
        # org_id 在 agent_runtimes / runtime_credentials 里是 int 列（NULL 默认为 0），
        # 而凭据可能在未归属组织时落空串。MySQL STRICT_TRANS_TABLES 下把 '' 写进 int 列
        # 直接 1366，引擎注册整条失败（表现为「添加设备生成凭据」报 Incorrect integer
        # value: '' for column 'org_id'）。这里统一归一为 int，空/非数字 → 0（未归属）。
        _raw_org = cred_row[4] if cred_row else None
        try:
            _org_txt = "" if _raw_org is None else str(_raw_org).strip()
            cred_org_id = int(_org_txt) if _org_txt not in ("", "None") else 0
        except (TypeError, ValueError):
            cred_org_id = 0
        if cred_scope == "user" and cred_row and cred_row[1]:
            # D32-补丁：owner_id 与 grape_accounts.id 对齐（按用户名）
            # 账号表归 mgmt：走 owner 域薄原语（任意拓扑同一份语句）。原实现是
            # 「mgmt lib → foreign-only lib → 本进程库直发」三段兜底，后两段的 SQL
            # 正是跨域引用来源，且三库拓扑下必 1146（被下面的 except 吞成「owner_id
            # 保持凭据行原值」）。游标见 `account_id_by_username_first_any` 的 `cursor`
            # 语义：单库/测试拓扑复用这里的 `c`，自有 DSN 忽略它走 mgmt 连接。
            try:
                from src.domains.mgmt import public as _mgmt_public_acc
                _aid = _mgmt_public_acc.account_id_by_username_first_any(cred_row[1], cursor=c)
                if _aid:
                    owner_id = _aid
            except Exception as _acc_e:
                # 账号域不可用不阻断注册主链路（owner_id 保持凭据行原值），但要留痕
                log.debug(f"register owner_id align by username skipped ({cred_row[1]}): {_acc_e}")
        # Issue #646：注册前确保凭据有 credential_uid（UUIDv4，一次性终身不变）；
        # engine_uid 由服务端按自然键（credential_uid|name|scope|owner_id|host_name）
        # 确定性派生（UUIDv5）——本机 worker 零配置，重复注册/表重建 uid 不漂移。
        # 这两条链路碰的表全归 runtime 域（configs/table_ownership.json），原语在
        # 本域 public 一份；此前绕 mgmt.user.db 属跨域 import 别域内部模块。
        from src.domains.runtime.public import ensure_credential_uid, rebase_engine_uid_references
        from src.kernel.engine_uid import derive_engine_uid, cloud_anchor_from_runtime_info
        _cred_uid = ensure_credential_uid(credential_id, c)
        # local_skills：per-engine skill 列表，按 engine name 匹配 runtime 行
        skills_by_name = {}
        for ls in body.local_skills or []:
            eng = (ls.get("engine") or "").strip().lower()
            if eng:
                skills_by_name[eng] = json.dumps(ls.get("skills") or [], ensure_ascii=False)
        from src.domains.runtime.engine.engine_select import merge_duplicate_engine_rows
        _rid_map = merge_duplicate_engine_rows(credential_id, c)
        for rt in body.runtimes or []:
            name = (rt.get("name") or "").strip().lower()
            if not name:
                continue
            # CLI 同名防误识别（2026-11-10，QA #1190 I3 整改）：显式 installed 为假值
            # 的引擎（如经能力判别排除的非 dsh 版同名 aishell 工具）不建行、不点亮——
            # 旧循环会把它 UPDATE installed=1 status=online，假行成为派单黑洞（DevSpace
            # 假 aishell-dsh 实证）。字面 False/0/"false" 三形态都命中；已有同名行顺手
            # 置 installed=0 收敛（假行点亮的存量状态在下次注册时被压回，无需等
            # reap_stale_runtimes 的 ~160s 窗口）。缺 installed 字段的旧 worker 上报
            # 不在此列（向后兼容——旧包误报 True 需 worker 升级后才被此层拦截）。
            _inst_raw = rt.get("installed")
            if _inst_raw is not None and str(_inst_raw).strip().lower() in ("false", "0"):
                c.execute("UPDATE agent_runtimes SET installed=0 "
                          "WHERE credential_id=? AND lower(name)=?",
                          (credential_id, name))
                continue
            _ls_json = skills_by_name.get(name, "[]")
            # Issue #646：engine_uid 优先查找（UUIDv5 稳定键，表重建 uid 不漂移）；
            # 未迁移旧库（缺 engine_uid 列）回退 #654 legacy 匹配。
            # Issue #868 交叉预警修正：云引擎（i5 四段名，engine_type/provider_id/
            # template_id 不可变锚透传）派生时 name 不参与——模板/Provider 改名
            # （name 随 refresh 刷新）uid 不漂移；普通引擎无锚走 legacy 公式零回归。
            # Issue #868 种子分量 id→uid 升格：i5 透传 provider_uid/template_uid
            # （UUIDv4 终身不变）时种子切 uid 公式（id/name 均不参与，表重建/
            # 库恢复零漂移）；仅 id 锚（老 worker）沿用 id 公式（过渡）。
            _anchor = cloud_anchor_from_runtime_info(rt)
            _engine_uid = derive_engine_uid(_cred_uid, name, cred_scope, owner_id, "",
                                             engine_type=_anchor[0],
                                             provider_id=_anchor[1],
                                             template_id=_anchor[2],
                                             provider_uid=_anchor[3],
                                             template_uid=_anchor[4])
            _old_uid = ""
            try:
                c.execute("SELECT id, scope, owner_id, repo_key, org_id FROM agent_runtimes "
                          "WHERE engine_uid=? LIMIT 1", (_engine_uid,))
                row = c.fetchone()
            except Exception:
                row = None   # 未迁移旧库（缺 engine_uid 列）回退 legacy 匹配
            if row is None and any(_anchor):
                # 锚定过渡（双读兼容）：锚在场 + uid-first 未命中（存量行 uid 按旧键
                # 含四段名派生，或库缺列）时，取 legacy 行存量旧键 uid 供改绑；
                # 缺列/无行容错为 ''
                try:
                    _ou = c.execute("SELECT engine_uid FROM agent_runtimes "
                                    "WHERE credential_id=? AND lower(name)=? "
                                    "ORDER BY id DESC LIMIT 1",
                                    (credential_id, name)).fetchone()
                    _old_uid = (_ou[0] or "") if _ou else ""
                except Exception:
                    _old_uid = ""
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
            # 注意：UPDATE/INSERT 两个分支都要用，定义必须提到分支外
            _model_name = str(rt.get("model") or "").strip()[:120]
            _ri_val = rt.get("runtime_info") or "{}"
            _ri_platform = (rt.get("provider") or "").strip() or _parse_ri_platform(_ri_val)
            _ri_type = (rt.get("runtime_type") or "").strip() or ("cloud" if _ri_platform else "local")
            if row:
                rid = row[0]

                # Issue #700：降级守卫——已 healthy 且探活新鲜的行不被 worker
                # 瞬时 ACP 握手超时打回 0（UPDATE 的 1→0 方向过守卫）
                # （健康值经 _register_healthy_guard，其余字段保留 #654/#658/凭据同步）；
                # Issue #646：注册回写 engine_uid/credential_uid 稳定键。
                # [2026-11-15 空壳修复] 云元数据三值定义已提升至 if row 分支外（本函数
                # 上方），INSERT 分支同用——原只在 UPDATE try 内定义，新引擎行首插
                # 落列默认 local/'{}'/''，M1 E2E 引擎 30 空壳根因。
                try:
                    # [沙箱迁移 e1ab9a6bc] 注册回写 runtime_type/runtime_info/provider——
                    # 云引擎（cloud_worker）的平台/模板元数据；本地引擎保持 local/{} 零变化
                    # Issue #1496：status+last_seen_at 改走统一写入口 apply_runtime_state
                    # （同事务写，含遗留 health 列派生同步），消除在线态二次写窗口。
                    apply_runtime_state(c, rid, "online", datetime.now(timezone.utc).isoformat())
                    # Issue #865：配置模型（worker 上报本机配置解析结果；空=未上报，
                    # 不覆盖既有值——同 #658 模式）。参数顺序：rid 必须最后（#865 基线
                    # 缺陷修复——占位符与参数错位会让整个 UPDATE 失败）。
                    _model_name = str(rt.get("model") or "").strip()[:120]
                    c.execute("UPDATE agent_runtimes SET version=?, healthy=?, installed=1, cli_path=?, local_skills=?, worker_version=?, engine_uid=?, credential_uid=?, runtime_info=?, provider=?, runtime_type=?"
                              + (", role_tags=?" if _role_tags_json else "")
                              + (", model_name=?, model_source='worker_report', model_reported_at=?" if _model_name else "")
                              + " WHERE id=?",
                              (rt.get("version", ""), _register_healthy_guard(c, rid, bool(rt.get("healthy", True))),
                               rt.get("cli_path", ""), _ls_json, body.cli_version,
                               _engine_uid, _cred_uid,
                               _ri_val if isinstance(_ri_val, str) and _ri_val.strip() else "{}",
                               _ri_platform, _ri_type)
                              + ((_role_tags_json,) if _role_tags_json else ())
                              + (( _model_name, datetime.now(timezone.utc).isoformat()) if _model_name else ())
                              + (rid,))
                except Exception:
                    # 未迁移旧库（缺 uid 列）降级 legacy 更新（保留 #654/#658/#700 语义）
                    apply_runtime_state(c, rid, "online", datetime.now(timezone.utc).isoformat())
                    _model_name = str(rt.get("model") or "").strip()[:120]
                    c.execute("UPDATE agent_runtimes SET version=?, healthy=?, installed=1, cli_path=?, local_skills=?, worker_version=?"
                              + (", role_tags=?" if _role_tags_json else "")
                              + (", model_name=?, model_source='worker_report', model_reported_at=?" if _model_name else "")
                              + " WHERE id=?",
                              (rt.get("version", ""), _register_healthy_guard(c, rid, bool(rt.get("healthy", True))),
                               rt.get("cli_path", ""), _ls_json, body.cli_version)
                              + ((_role_tags_json,) if _role_tags_json else ())
                              + (( _model_name, datetime.now(timezone.utc).isoformat()) if _model_name else ())
                              + (rid,))
                else:
                    # Issue #868 锚定过渡（双读兼容）：legacy 命中行存量旧键 uid（含
                    # 四段名派生）与新锚 uid 不一致 → 注册即改绑（引擎行 uid + 池/
                    # 能力/任务/会话引用轨道整体 rebase，不留孤儿，改名/合流零漂移）
                    if _old_uid and _old_uid != _engine_uid:
                        _rb = rebase_engine_uid_references(c, _old_uid, _engine_uid)
                        log.info("engine_change_event: " + json.dumps(
                            {"event": "engine_uid_anchor_rebased", "runtime_id": rid,
                             "engine": name, "old_engine_uid": _old_uid,
                             "new_engine_uid": _engine_uid, "report": _rb},
                            ensure_ascii=False))

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
                try:
                    c.execute(
                        # 合并: HEAD #865 模型三字段 + #646 uid 稳定键 + role_tags
                        # [2026-11-15 空壳修复] 首次 INSERT 补 runtime_info/provider/
                        # runtime_type 三列——与 UPDATE 分支（:441-470 云元数据回写）同源
                        # 同值。原 INSERT 不含这三列 → 新引擎行落列默认 local/'{}'/''；
                        # 若 worker 的 hash-diff 下一轮判定「未变化」跳过注册（
                        # _refresh_templates 的 _changed 按 runtime_info 对比，但
                        # hash 含本地列表全量），空壳永不回填 → 认领时 build_context
                        # 读到 provider='' 误判本地引擎 → dispatch_execution only for
                        # cloud sandbox, got BwrapSandbox（M1 E2E 引擎 30 实证：
                        # INSERT 空壳，靠第二轮 UPDATE 或手工修才转 cloud）。
                        "INSERT INTO agent_runtimes (credential_id, name, display_name, `binary`, version, healthy, installed, status, last_seen_at, cli_path, scope, owner_id, local_skills, repo_key, worker_version, org_id, role_tags, engine_uid, credential_uid, runtime_info, provider, runtime_type, model_name, model_source, model_reported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (credential_id, name, rt.get("display_name", name), name, rt.get("version", ""),
                         1 if rt.get("healthy", True) else 0, 1, "online",
                         datetime.now(timezone.utc).isoformat(), rt.get("cli_path", ""), cred_scope, owner_id, _ls_json, cred_repo_key, body.cli_version, cred_org_id, _role_tags_json, _engine_uid, _cred_uid,
                         _ri_val if isinstance(_ri_val, str) and _ri_val.strip() else "{}",
                         _ri_platform, _ri_type,
                         _model_name, "worker_report" if _model_name else "",
                         datetime.now(timezone.utc).isoformat() if _model_name else ""))
                except Exception:
                    # 未迁移旧库（缺 uid 列）降级 legacy 插入（保留 #654/#658/#865 语义）
                    c.execute(
                        "INSERT INTO agent_runtimes (credential_id, name, display_name, `binary`, version, healthy, installed, status, last_seen_at, cli_path, scope, owner_id, local_skills, repo_key, worker_version, org_id, role_tags, model_name, model_source, model_reported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (credential_id, name, rt.get("display_name", name), name, rt.get("version", ""),
                         1 if rt.get("healthy", True) else 0, 1, "online",
                         datetime.now(timezone.utc).isoformat(), rt.get("cli_path", ""), cred_scope, owner_id, _ls_json, cred_repo_key, body.cli_version, cred_org_id, _role_tags_json,
                         _model_name, "worker_report" if _model_name else "",
                         datetime.now(timezone.utc).isoformat() if _model_name else ""))
                rid = c.lastrowid
            # Issue #1011：锚快照持久化（engine_type/provider_id/template_id/
            # provider_uid/template_uid 五列，001 迁移加列）——派生源修正后
            # 存量行可离线重算（002 重派生迁移消费）；未迁移旧库缺列时降级跳过。
            try:
                c.execute(
                    "UPDATE agent_runtimes SET engine_type=?, provider_id=?, template_id=?, "
                    "provider_uid=?, template_uid=? WHERE id=?",
                    (_anchor[0] or "", _anchor[1] or "", _anchor[2] or "",
                     _anchor[3] or "", _anchor[4] or "", rid))
            except Exception:
                pass
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
                # Issue #646：默认标签行同步 engine_uid（引用切换，孤儿防护）
                try:
                    c.execute(
                        "UPDATE engine_capabilities SET engine_uid=? WHERE runtime_id=? AND (engine_uid IS NULL OR engine_uid='')",
                        (_engine_uid, rid))
                except Exception:
                    pass
            # capabilities 写入后同步 agent_tags 派生缓存（注册路径原先漏了这一步，
            # 导致新机器 agent_tags 为空、前端按 tags 过滤时被漏掉——sre-personal-machine 修复）
            from src.domains.mgmt.public import sync_agent_tags
            sync_agent_tags(rid)
            # Issue #1208：三维度派生标签（engine:/scope:/scene:）落 capabilities 冒号行
            # + JSON 快照刷新（selector 圈池数据底座；注册链先写，心跳链幂等收敛）
            try:
                from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags as _sdat
                _st_row = c.execute("SELECT scene_tags FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
                # [云引擎标签修复] engine: 维度按 engine_type 派生（云四段名不入标签）
                _et = ""
                try:
                    _et = (json.loads(rt.get("runtime_info") or "{}").get("engine_type") or "")
                except Exception:
                    _et = ""
                _sdat(c, rid, name, cred_scope, _st_row[0] if _st_row else "", engine_uid=_engine_uid,
                      engine_type=_et)
            except Exception as _at_e:
                log.warning(f"runtime_register: agent_tags derive failed for {rid}: {_at_e}")
            # Issue #646：返回双带 id+uid（新 worker 用 uid 落历史；旧版 worker 忽略 uid，混跑兼容）
            registered.append({"id": rid, "name": name, "engine_uid": _engine_uid})
        db.commit()
    # v3.0：probe_url——启动可达性探测（复用任一已转存技能对象签名，
    # 验证完整链路：网络+签名+读取；空库返回空，worker 声明能力 false）
    probe_url = ""
    try:
        from src.domains.runtime.storage import get_storage
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
async def runtime_heartbeat(body: HeartbeatBody, request: Request, credential_id: int = Depends(_require_credential)):
    """心跳入口（async 包装）。

    心跳必须走 async：uvicorn 把 sync 端点放进 anyio 线程池，而 messages 批量上报、
    recover-orphans 这类长请求会把 40 个槽位吃满，心跳只能排队到客户端 40s 超时 ——
    实测 116 侧连续三次 TimeoutError(40s) 之后突然 0.05s，正是池子被长请求占住的波形。
    真正的 DB 写入交给线程池，既不阻塞事件循环，也不占请求线程池的槽。
    """
    return await asyncio.to_thread(_heartbeat_impl, body, request, credential_id)


def _heartbeat_impl(body: HeartbeatBody, request: Request, credential_id: int):
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
    with _runtime_cursor() as c:
        # spec 为空时不覆盖（兼容旧版 worker 不上报 spec）
        if body.spec:
            c.execute("UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, memory_percent=?, disk_percent=?, tmp_percent=?, bandwidth_percent=?, bandwidth_mbps=?, load_avg=?, spec=? WHERE id=?",
                      (now, client_ip, body.cpu_percent, body.memory_percent, body.disk_percent, body.tmp_percent, body.bandwidth_percent, body.bandwidth_mbps, body.load_avg, body.spec, credential_id))
        else:
            c.execute("UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, memory_percent=?, disk_percent=?, tmp_percent=?, bandwidth_percent=?, bandwidth_mbps=?, load_avg=? WHERE id=?",
                      (now, client_ip, body.cpu_percent, body.memory_percent, body.disk_percent, body.tmp_percent, body.bandwidth_percent, body.bandwidth_mbps, body.load_avg, credential_id))
        if body.runtime_ids:
            ph = ",".join("?" for _ in body.runtime_ids)
            c.execute(f"UPDATE agent_runtimes SET status='online', last_seen_at=? WHERE id IN ({ph}) AND credential_id=?",
                      [now] + body.runtime_ids + [credential_id])
            # worker 上报的引擎探测结果写入 DB（probe_* 列已由 db_setup.py 自动迁移添加）
            # Issue #490：按 last_probe 幂等去重——同一观测（相同 last_probe）重放只刷新状态，
            # 不递增/重置 probe_fail_count、不动 healthy，避免心跳重放导致计数虚增与 Runtime 误摘除。
            if body.probe_results:
                for _pname, _pdata in body.probe_results.items():
                    _pstatus = (_pdata.get("status") or "pending") if isinstance(_pdata, dict) else "pending"
                    _platency = (_pdata.get("latency") or 0) if isinstance(_pdata, dict) else 0
                    _plast = (_pdata.get("last_probe") or now) if isinstance(_pdata, dict) else now
                    _perror = (_pdata.get("error") or "") if isinstance(_pdata, dict) else ""
                    # Issue #2133：拿 runtime_id 供时间窗追踪/恢复加回（按 name+credential 唯一定位）
                    _prid_row = c.execute(
                        "SELECT id FROM agent_runtimes WHERE name=? AND credential_id=?",
                        (_pname, credential_id)).fetchone()
                    _prid = _prid_row[0] if _prid_row else 0
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
                            "probe_fail_count=0, probe_spawn_fail_count=0, healthy=1 "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
                        # Issue #2133：连续 ok 计数 + 达 M 次且此前有告警/降级/解绑事实 → 自动加回场景 + 对账
                        if _prid:
                            try:
                                from src.domains.runtime.engine import recovery as _recovery
                                _recovery.note_probe_ok(c, _prid)
                            except Exception as _rce:
                                log.warning(f"runtime probe ok recovery hook failed (runtime={_prid}): {_rce}")
                    elif _pstatus in ("fail", "model_unreachable"):
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=?, "
                            "probe_fail_count=probe_fail_count+1, probe_spawn_fail_count=0, "
                            "healthy=CASE WHEN probe_fail_count+1>=2 THEN 0 ELSE healthy END "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
                        if _pstatus == "model_unreachable":
                            log.warning(f"runtime probe model_unreachable: engine={_pname} "
                                        f"credential={credential_id} error={_perror[:200]}")
                        if _prid:
                            try:
                                from src.domains.runtime.engine import recovery as _recovery
                                _recovery.note_probe_break(c, _prid)
                            except Exception as _rce:
                                log.warning(f"runtime probe break hook failed (runtime={_prid}): {_rce}")
                    elif _pstatus == "spawn_failed":
                        # Issue #1982：子进程起不来（fd 打满/资源不足）= 环境故障，
                        # 不计入「探活连续失败」（probe_fail_count），偶发资源紧张不摘除引擎。
                        # Issue #1982 复审：但持续资源类故障不能让引擎永远 healthy 继续接单——
                        # 独立计数 probe_spawn_fail_count，连续达到 _PROBE_SPAWN_FAIL_TRIP 次
                        # （env RUNTIME_PROBE_SPAWN_FAIL_TRIP 可调，默认 50）后降级 healthy=0，
                        # probe_error 附修复指引；下次 ok 探活清零计数并恢复 healthy=1。
                        # fail/model_unreachable 说明进程已成功 spawn，连败链断开 → 同步清零。
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=?, "
                            "probe_spawn_fail_count=probe_spawn_fail_count+1, "
                            "healthy=CASE WHEN probe_spawn_fail_count+1>=? THEN 0 ELSE healthy END "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _PROBE_SPAWN_FAIL_TRIP,
                             _pname, credential_id)
                        )
                        _sf = c.execute(
                            "SELECT probe_spawn_fail_count FROM agent_runtimes WHERE name=? AND credential_id=?",
                            (_pname, credential_id)).fetchone()
                        _sf_count = (_sf[0] or 0) if _sf else 0
                        if _sf_count >= _PROBE_SPAWN_FAIL_TRIP:
                            _sf_hint = (
                                f" | spawn_failed x{_sf_count}: persistent resource exhaustion "
                                "(EMFILE/ENOMEM/EAGAIN); fix: raise fd limit (LimitNOFILE=524288 / ulimit -n), "
                                "free memory, restart worker; engine re-enabled automatically on next ok probe")
                            c.execute(
                                "UPDATE agent_runtimes SET probe_error=?, healthy=0 "
                                "WHERE name=? AND credential_id=?",
                                (_perror + _sf_hint, _pname, credential_id)
                            )
                        log.warning(f"runtime probe spawn_failed (not counted as consecutive failure, "
                                    f"spawn_fail_count={_sf_count}/{_PROBE_SPAWN_FAIL_TRIP}): "
                                    f"engine={_pname} credential={credential_id} error={_perror[:200]}")
                        # Issue #2133：spawn_failed 连续计数 + 持续时长 → 强告警 / degraded 标记（不摘除）
                        if _prid:
                            try:
                                from src.domains.runtime.engine import recovery as _recovery
                                _recovery.note_spawn_failed(c, _prid)
                            except Exception as _rce:
                                log.warning(f"runtime spawn_failed window hook failed (runtime={_prid}): {_rce}")
                    else:
                        c.execute(
                            "UPDATE agent_runtimes SET probe_status=?, probe_latency=?, probe_last=?, probe_error=? "
                            "WHERE name=? AND credential_id=?",
                            (_pstatus, _platency, _plast, _perror, _pname, credential_id)
                        )
                        if _prid:
                            try:
                                from src.domains.runtime.engine import recovery as _recovery
                                _recovery.note_probe_break(c, _prid)
                            except Exception as _rce:
                                log.warning(f"runtime probe break hook failed (runtime={_prid}): {_rce}")
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
            # Issue #1208：心跳链派生标签刷新（scene_tags 维护/scope 切换随心跳幂等收敛；
            # 只动冒号派生行，用户手设能力行不触碰）
            try:
                from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags as _sdat_hb
                for _rid in body.runtime_ids:
                    _hb_row = c.execute(
                        "SELECT r.name, r.scene_tags, r.engine_uid, COALESCE(rc.scope,'global'), r.runtime_info "
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.id=? AND r.credential_id=?", (_rid, credential_id)).fetchone()
                    if _hb_row:
                        _et_hb = ""
                        try:
                            _et_hb = (json.loads(_hb_row[4] or "{}").get("engine_type") or "")
                        except Exception:
                            _et_hb = ""
                        # [云引擎标签修复] engine: 维度按 engine_type 派生（云四段名不入标签）
                        _sdat_hb(c, _rid, _hb_row[0], _hb_row[3], _hb_row[1], engine_uid=_hb_row[2] or "",
                                 engine_type=_et_hb)
            except Exception as _at_e:
                log.warning(f"heartbeat agent_tags derive failed: {_at_e}")
        try:
            c.connection.commit()
        except Exception:
            db.commit()

    # 写入 worker 上报的日志到 data/logs/workers/{credential_id}.log
    if body.log_lines:
        import os as _os, json as _json
        from src.kernel.config import LOG_DIR
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
                    _wname = row[1] or row[0] or f"worker-{credential_id}"
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
            from src.domains.insight import public as _obs_public
            _recs = [{"ts": (ll.ts or now), "level": ll.level, "component": "worker",
                      "msg": ll.message, "source": _wname or f"worker-{credential_id}",
                      "runtime_id": 0} for ll in body.log_lines]
            _obs_public.ingest_logs(_recs, default_source=_wname or f"worker-{credential_id}")
        except Exception as _ole:
            log.warning(f"obs_log worker ingest failed: {_ole}")

    # 返回 worker 代码版本（仅 git commit，不含 wheels/python 指纹）
    # worker 端对比此版本号决定是否自动升级
    from src.kernel.worker_package import worker_code_version
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
    from src.domains.runtime.engine.health_scorer import get_all_health_scores
    from src.domains.runtime.engine.instances import runtime_instance_name
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
    with _domain_cursor("runtime") as c:
        c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (runtime_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={
                "code": 404, "message": "runtime not found"})
    from src.domains.runtime.engine.health_scorer import get_health_score
    from src.domains.runtime.engine.instances import runtime_instance_name
    data = get_health_score(runtime_id, force_refresh=True)
    data["engine_name"] = runtime_instance_name(runtime_id)
    return data


@router.post("/admin/batch-recover", operation_id="runtime_batch_recover", summary="Batch recover stale tasks")
def batch_recover_stale_tasks_api(user: dict = Depends(require_no_session_token)):
    """Manually trigger batch stale task recovery (Phase 5)."""
    from src.domains.runtime.engine.service import batch_recover_stale_tasks
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


def _require_runtime_admin(runtime_id: int, user: dict) -> None:
    """扫雷⑨-H3：用户通道（JWT/API Key）写引擎配置的归属校验。

    - session_token / api_key：作用域已限定，与 check_repo_access 同口径豁免；
    - scope=user：仅 owner 本人（agent_runtimes.owner_id == 用户 id，Issue #654 口径）；
    - scope=repo：仓库托管人（check_repo_access → managed_repos 归属）；
    - scope=global：平台级共享引擎，维持现状放行（历史行为，不收窄）。
    [rebase 并入 2026-11-12] 基线 post-fork 新增（62568298e）。
    """
    auth_type = user.get("auth_type", "jwt")
    if auth_type in ("session_token", "api_key"):
        return
    with db_lock:
        c = db.cursor()
        c.execute("SELECT scope, owner_id, repo_key FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
    scope, owner_id, repo_key = row[0] or "global", row[1] or "", row[2] or ""
    if scope == "user":
        if owner_id and owner_id == str(user.get("id") or ""):
            return
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅引擎 owner 可修改该引擎配置"})
    if scope == "repo":
        try:
            from src.api.deps import check_repo_access
            # check_repo_access 契约：有权 → return None；无权 → raise HTTPException(403)。
            # 不得当布尔判定——`if check_repo_access(...)` 会把成功的 None 当 False，
            # 仓库托管人恒落 403（aw11 E-02 实测：托管人账号保不了 s2 云引擎的场景标签）。
            check_repo_access(user, repo_key)
            return
        except Exception:
            pass
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅仓库托管人可修改该引擎配置"})


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
        from src.domains.runtime.storage import get_storage
        _storage = get_storage()
        if _storage is None:
            return
        skill = (task.get("skill") or "").strip()
        if not skill:
            return
        from src.domains.mgmt.public import resolve_skill_id, get_skill_object_key, get_skill_by_id
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
            # [rebase 并入 2026-11-12] 基线 #617：skill 按名未命中 → 按 agent_type 回退
            # 官方检查器（worker 无库，服务端解析后以 skill_fallback 下发：名字 + id，
            # worker 物化其 zip 并提示 `grape skills detail --skill_id <id>`）
            try:
                from src.domains.mgmt.public import resolve_official_checker_skill
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
async def runtime_get_tags(runtime_id: int, credential_id: int = Depends(_require_credential)):
    """claim 前必经的标签查询：同样不能占 anyio 线程池槽（否则被长请求挤到超时）。"""
    return await asyncio.to_thread(_get_tags_impl, runtime_id, credential_id)


def _get_tags_impl(runtime_id: int, credential_id: int):
    """Return this engine's claimable tags from DB (worker polls this instead of ENGINE_TAGS env).

    Tags are managed by users via the console (settings.py CRUD endpoints).
    Workers call this endpoint to get their current tag list, enabling real-time
    tag updates without restart.
    """
    from src.kernel import db_registry as _dbreg
    # 这条端点是 agent 每次 claim 前的必经之路，不能和进程内长查询抢同一把 db_lock：
    # sweeper / recover-orphans 持锁做批量扫描时，/tags、心跳、/messages 会一起排队到
    # socket 超时（实测表现为「engine probe ok 但 claim 一直超时、任务永远 queued」）。
    # 三库拓扑走 runtime 域独立连接；单库/测试拓扑没有分域 DSN，回落进程主库 + db_lock。
    tags = []
    try:
        with _dbreg.cursor("runtime") as c:
            c.execute(
                "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                "AND tag NOT LIKE '%:%' ORDER BY tag",
                (runtime_id,)
            )
            tags = [r[0] for r in c.fetchall()]
    except RuntimeError:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                    "AND tag NOT LIKE '%:%' ORDER BY tag",
                    (runtime_id,)
                )
                tags = [r[0] for r in c.fetchall()]
            except Exception:
                tags = []
    except Exception:
        tags = []
    return {"tags": tags}


@router.get("/tasks/{task_id}/env")
def runtime_task_env(task_id: str, credential_id: int = Depends(_require_credential)):
    """Return latest env (AK/SK + credentials) for a task from DB.

    Workers call this before executing to get fresh credentials,
    instead of relying on the env_extra snapshot from task creation time.

    [沙箱迁移 i7 漏移植恢复 2026-11-11] cloud_worker: 返回 LLM 凭据（B 机器无 DB，
    不能调 get_llm_credentials）——按 engine_type 两级查询: 引擎专属 → 通用兜底；
    按 Provider 凭据 scope 路由来源（global/repo → 仓库 LLM 凭据，user → 用户 LLM 凭据）。
    取不到返回 llm_cred_missing 让 worker 不建箱、任务 fail 带指引（spec A）。
    """
    import json as _json
    from src.kernel.session_manager import db, db_lock
    from src.domains.runtime.engine.service import _collect_server_env
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, account, repo_key FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        return {"env": {}}
    user_id, account, task_repo_key = row[0] or 0, row[1] or "", row[2] or ""
    env = _collect_server_env(user_id, account)
    # [rebase 并入 2026-11-12] 基线 #1858/#990/#1745 三段（我方 SELECT 带 repo_key，
    # runtime_id 单独补查供三面基址解析）
    _task_runtime_id = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id FROM agent_tasks WHERE id=?", (task_id,))
            _r = c.fetchone()
            _task_runtime_id = (_r[0] or 0) if _r else 0
    except Exception:
        pass
    # Issue #1858：worker 执行前重拉任务 env 时，按任务 runtime 下发三面基址，
    # 覆盖历史任务/旧快照（此前只下发凭据，旁路路径仍可能缺 GRAPE_A2A_BASE_URL）。
    try:
        from src.kernel.prompt_builder import resolve_task_server_env
        env.update(resolve_task_server_env(_task_runtime_id))
    except Exception as _base_e:
        log.warning(f"runtime_task_env service base resolve failed for task={task_id}: {_base_e}")
    # Issue #990：会话 token 与 shell 环境快照同步——valid 时复用快照 token，
    # 失效/轮换时签发新 token 并回写 agent_tasks.env_extra，随后返回给 worker。
    # Issue #990：只同步 GRAPE_SESSION_TOKEN，不覆盖任务快照中的其它显式 env
    # （如 GRAPE_CLI_MODE），避免超出 Spec 范围改变 CLI 作用域语义。
    from src.domains.runtime.engine.service import sync_task_session_token
    session_token = sync_task_session_token(task_id)
    if session_token:
        env["GRAPE_SESSION_TOKEN"] = session_token
    # Issue #1745：跨场景转交后，任务快照中的 GRAPE_SCENE_ID 可能仍是源场景；
    # 执行前按 Issue 当前归属场景（issues detail.scene_id 同源）校正，PR 订阅任务保留订阅语义。
    try:
        from src.domains.runtime.pipeline.event_pipeline import reconcile_task_scene_env
        reconcile_task_scene_env(task_id, env)
    except Exception as _scene_e:
        log.warning(f"runtime_task_env scene reconcile failed for task={task_id}: {_scene_e}")

    # ── LLM 凭据注入（i7 runtime.py:1060-1123 段恢复，canonical import）──
    try:
        engine_type = ""
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT r.runtime_info FROM agent_tasks t "
                "LEFT JOIN agent_runtimes r ON t.runtime_id=r.id "
                "WHERE t.id=?",
                (task_id,),
            )
            _ri_row = c.fetchone()
        if _ri_row and _ri_row[0]:
            try:
                _ri = _json.loads(_ri_row[0])
                engine_type = _ri.get("engine_type", "") or ""
            except Exception:
                pass

        # [repo-llm-credential 用户裁定 2026-10-30] 按沙箱平台凭据 scope 路由来源：
        # 引擎所在 Provider 的关联凭据 scope=global/repo → 仓库 LLM 凭据（仓库资产；
        # global 暂走仓库——组织界面就绪后迁移）；scope=user → 用户 LLM 凭据（原行为）。
        # 跨级零回退；取不到返回 llm_cred_missing 让 worker 不建箱、任务 fail 带指引
        # （spec A——消灭「静默跳过引擎死半路」盲区）。
        _llm_mode, _llm_repo_key = "user", ""
        try:
            # 原来是四表 JOIN 且用 json_extract（SQLite 方言）—— 三库/MySQL 下必然失败，
            # 一直被 except 吞掉等于从未生效。改为分步读 + Python 解析 JSON。
            with db_lock:
                c = db.cursor()
                c.execute("SELECT runtime_id FROM agent_tasks WHERE id=?", (task_id,))
                _t_row = c.fetchone()
                _prov_id = ""
                if _t_row and _t_row[0]:
                    c.execute("SELECT runtime_info FROM agent_runtimes WHERE id=?", (_t_row[0],))
                    _r_row = c.fetchone()
                    if _r_row and _r_row[0]:
                        try:
                            _prov_id = str(json.loads(_r_row[0]).get("provider_id") or "")
                        except (ValueError, TypeError):
                            _prov_id = ""
                _scope_row = None
                if _prov_id:
                    from src.domains.mgmt import public as _mg_prov
                    _cred_id = _mg_prov.sandbox_provider_credential_id(_prov_id)
                    if _cred_id:
                        c.execute("SELECT scope, repo_key FROM runtime_credentials WHERE id=?",
                                  (_cred_id,))
                        _scope_row = c.fetchone()
            if _scope_row and (_scope_row[0] in ("global", "repo")):
                _llm_mode = "repo"
                _llm_repo_key = task_repo_key or (_scope_row[1] or "")
        except Exception:
            pass
        from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
        with db_lock:
            c = db.cursor()
            _llm_row = select_llm_cred(c, user_id, engine_type, mode=_llm_mode, repo_key=_llm_repo_key)
        if _llm_row:
            env["_LLM_PROVIDER"], env["_LLM_API_KEY"], env["_LLM_BASE_URL"], env["_LLM_MODEL"] = _llm_row
        else:
            # spec A：显式失败标记 + 指引（worker 据此不建箱）
            _src_zh = "仓库设置" if _llm_mode == "repo" else "用户设置"
            return {"env": env, "llm_cred_missing": True, "llm_cred_source": _llm_mode,
                    "llm_cred_hint": f"缺少 LLM 凭据：该任务使用{'仓库级' if _llm_mode == 'repo' else '个人级'}沙箱平台，"
                                     f"请在【{_src_zh} → LLM 凭据】为引擎 {engine_type or 'common'} 配置后重试"}
    except Exception as _e:
        # Previously this only logged a WARNING and returned an empty env, which the
        # worker reported as "no LLM credentials, config skipped" and the engine then
        # turned into "No LLM provider configured" -- unreadable even for a user who
        # had credentials configured. The env is now failed explicitly instead of
        # silently empty, but the attribution stays honest: this block covers env
        # assembly (task/runtime lookups, scene reconcile, credential selection), so
        # the user-facing hint must not claim the credentials are unreadable, and the
        # raw exception (which can carry schema/table names) stays in the server log.
        log.warning(f"task_env: env assembly failed: {_e}")
        return {"env": env, "llm_cred_missing": True, "llm_cred_source": "env_error",
                "llm_cred_hint": "任务环境准备失败（LLM 凭据注入阶段），请查看平台日志后重试"}
    return {"env": env}


@router.post("/runtimes/{runtime_id}/tasks/claim-by-tag/{tag}")
async def runtime_claim_by_tag(runtime_id: int, tag: str,
                               body: RuntimeClaimBody | None = None,
                               credential_id: int = Depends(_require_credential)):
    """认领高频路径：async 包装，避免与长请求争线程池。"""
    return await asyncio.to_thread(_claim_by_tag_impl, runtime_id, tag, body, credential_id)


def _claim_by_tag_impl(runtime_id: int, tag: str,
                       body: "RuntimeClaimBody | None", credential_id: int):
    """Claim task by tag + scope isolation (event-dispatch-redesign-v2.md 2.4).

    Workers with engine tags can claim tasks matching their tag and scope.
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
            with _runtime_cursor() as c:
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


# ===== Issue #1011：claim-by-uid 协议 v2 端点（双端点并存期）=====
# 设计（docs/issue-868/claim-by-uid-v2-protocol-spec.md §2.1 勘误后口径）：
# - v2 路由只做寻址键转换（uid → runtime_id），随后**直接调用 v1 路由函数**，
#   服务层逻辑与 v1 完全共用（语义零漂移）；
# - uid 命中 → 按该引擎行认领；uid 失效 + body.runtime_id 命中 → 回退 id 认领
#   （并存期兜底）；双失效 → 404 UNKNOWN_ENGINE_UID（绝不静默 200 空任务）；
# - v1 端点保留不变（旧 worker 唯一路径），v1 流量归零后阶段 5 统一下线。

class RuntimeClaimBodyV2(RuntimeClaimBody):
    """v2 claim body：v1 字段 + runtime_id 兜底锚（uid 失效时回退 id 认领）。"""
    runtime_id: int = 0


class HeartbeatBodyV2(HeartbeatBody):
    """v2 心跳 body：v1 字段 + engine_uids 数组（uid 优先解析，失效回退 runtime_ids）。"""
    engine_uids: list[str] = []


class CompleteBodyV2(CompleteBody):
    engine_uid: str = ""


class FailBodyV2(FailBody):
    engine_uid: str = ""


class ProgressBodyV2(ProgressBody):
    engine_uid: str = ""


def _resolve_runtime_by_uid(engine_uid: str, fallback_runtime_id: int,
                            credential_id: int) -> int:
    """v2 寻址解析：uid → runtime_id（限本 rta_ 凭据），双失效 404 UNKNOWN_ENGINE_UID。

    - uid 命中（agent_runtimes.engine_uid 且 credential_id 匹配）→ 返回该行 id；
    - uid 未命中（失效：引擎删除/合流重派生窗口/未迁移 NULL uid）且
      fallback_runtime_id 非 0 且命中本凭据 → 回退该行 id（并存期兜底）；
    - 双失效 → 404（fail-fast，不静默造行；message 含 NULL uid 引擎的重注册提示）。
    """
    uid = (engine_uid or "").strip().lower()
    if uid:
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT id FROM agent_runtimes WHERE engine_uid=? AND credential_id=? LIMIT 1",
                (uid, credential_id)).fetchone()
        if row:
            return row[0]
        log.warning(f"v2 resolve: engine_uid not found (stale?): uid={uid} "
                    f"credential={credential_id} fallback_runtime_id={fallback_runtime_id}")
    if fallback_runtime_id:
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT id FROM agent_runtimes WHERE id=? AND credential_id=? LIMIT 1",
                (fallback_runtime_id, credential_id)).fetchone()
        if row:
            return row[0]
    raise HTTPException(status_code=404, detail={
        "code": "UNKNOWN_ENGINE_UID",
        "message": f"engine_uid '{uid or '(empty)'}' not found"
                   f"{' and runtime_id fallback missed' if fallback_runtime_id else ''}"
                   " (engine deleted or uid pending backfill; re-register to obtain a fresh uid)"})


def _require_runtime_owned_uid(engine_uid: str, credential_id: int) -> int:
    """v2 归属校验（claim-bound 系）：上报 engine_uid 必须属于该 rta_ 凭据，否则 403。

    对应 v1 的 _require_runtime_owned（runtime_id 口径）；返回解析出的 runtime_id。
    """
    uid = (engine_uid or "").strip().lower()
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id FROM agent_runtimes WHERE engine_uid=? AND credential_id=? LIMIT 1",
            (uid, credential_id)).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail={
            "code": 403,
            "message": "engine_uid not owned by this credential"})
    return row[0]


def _claim_body_v1(body: RuntimeClaimBodyV2 | None) -> RuntimeClaimBody | None:
    """v2 claim body → v1 body（剥离 runtime_id 兜底锚，其余字段逐字段透传）。"""
    if body is None:
        return None
    return RuntimeClaimBody(
        supports_obs_url=body.supports_obs_url,
        prefer_fallback=body.prefer_fallback,
        expected_task_id=body.expected_task_id,
        expected_event_seq=body.expected_event_seq)


@router.post("/runtimes/uid/{engine_uid}/tasks/claim")
def runtime_claim_v2(engine_uid: str, body: RuntimeClaimBodyV2 | None = None,
                     credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 claim：按 engine_uid 认领（uid 失效回退 body.runtime_id）。

    寻址键转换后直接调 v1 runtime_claim（服务层语义零漂移）；
    payload 与 v1 一致双带 id+uid 三键（engine_uid/engine_name_snapshot/pool_engine_uids）。
    """
    rid = _resolve_runtime_by_uid(engine_uid, body.runtime_id if body else 0, credential_id)
    return runtime_claim(rid, _claim_body_v1(body), credential_id)


@router.post("/runtimes/uid/{engine_uid}/tasks/claim-by-tag/{tag}")
async def runtime_claim_by_tag_v2(engine_uid: str, tag: str,
                            body: RuntimeClaimBodyV2 | None = None,
                            credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 claim-by-tag：uid 寻址 + v1 服务层（tag 匹配/scope 隔离零漂移）。

    Bugfix（同 heartbeat-v2 家族，2026-11-16 E2E 实测线上 500）：v1
    runtime_claim_by_tag 是 async（to_thread 高频路径），v2 必须 async + await。
    """
    rid = _resolve_runtime_by_uid(engine_uid, body.runtime_id if body else 0, credential_id)
    return await runtime_claim_by_tag(rid, tag, _claim_body_v1(body), credential_id)


@router.post("/runtimes/uid/{engine_uid}/tasks/claim-bound")
def runtime_claim_bound_v2(engine_uid: str, body: RuntimeClaimBodyV2,
                           credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 claim-bound：归属校验改 uid 口径（他人 uid → 403），其余同 v1。"""
    rid = _require_runtime_owned_uid(engine_uid, credential_id)
    return runtime_claim_bound(rid, _claim_body_v1(body), credential_id)


@router.post("/runtimes/uid/{engine_uid}/tasks/claim-by-tag-bound/{tag}")
def runtime_claim_by_tag_bound_v2(engine_uid: str, tag: str,
                                  body: RuntimeClaimBodyV2,
                                  credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 claim-by-tag-bound：归属校验 uid 口径，其余同 v1。"""
    rid = _require_runtime_owned_uid(engine_uid, credential_id)
    return runtime_claim_by_tag_bound(rid, tag, _claim_body_v1(body), credential_id)


@router.get("/runtimes/uid/{engine_uid}/tasks/claim-capabilities")
def runtime_claim_capabilities_v2(engine_uid: str,
                                  credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 能力协商（只读）：uid 形态 + uid_claim 能力位。

    uid_claim=True 是 worker v2-first 的探测锚点（旧服务端该路径 404 空白 →
    worker 判定对端不支持 v2 切换 v1 端点并记忆）；旧客户端 GET v1 路径行为不变。
    """
    rid = _require_runtime_owned_uid(engine_uid, credential_id)
    return {
        "runtime_id": rid,
        "engine_uid": (engine_uid or "").strip().lower(),
        "capabilities": {
            "bound_claim": True,
            "task_lookup": True,
            "uid_claim": True,
        },
    }


@router.post("/heartbeat-v2")
async def runtime_heartbeat_v2(body: HeartbeatBodyV2, request: Request,
                         credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 心跳：engine_uids 优先解析、失效回退 runtime_ids、全失效不造行。

    解析后委托 v1 runtime_heartbeat（心跳全逻辑共用，语义零漂移）；
    uid 失效条目仅日志取证（不造行，防 -cred0 孤儿），回退语义见 v2 spec §2.3。

    Bugfix（E2E 2026-11-16 实测 6884/6884 全 500）：v1 runtime_heartbeat 是 async
    （asyncio.to_thread 包装），sync def + 裸调返回 coroutine → resp[...] 赋值
    TypeError → 500。本函数必须 async 并 await 委托调用；uid 解析段照 v1 口径
    交线程池，不占事件循环。
    """

    def _resolve_uids() -> tuple[list[int], list[str]]:
        resolved_ids: list[int] = []
        stale_uids: list[str] = []
        with db_lock:
            c = db.cursor()
            for _uid in (body.engine_uids or []):
                uid = str(_uid or "").strip().lower()
                if not uid:
                    continue
                row = c.execute(
                    "SELECT id FROM agent_runtimes WHERE engine_uid=? AND credential_id=? LIMIT 1",
                    (uid, credential_id)).fetchone()
                if row:
                    resolved_ids.append(row[0])
                else:
                    stale_uids.append(uid)
        return resolved_ids, stale_uids

    resolved_ids, stale_uids = await asyncio.to_thread(_resolve_uids)
    for uid in stale_uids:
        log.warning(f"heartbeat-v2: engine_uid not found (stale, no row created): "
                    f"uid={uid} credential={credential_id}")
    merged = list(resolved_ids)
    for rid in (body.runtime_ids or []):
        if rid not in merged:
            merged.append(rid)
    v1_body = HeartbeatBody(
        runtime_ids=merged,
        cpu_percent=body.cpu_percent,
        memory_percent=body.memory_percent,
        disk_percent=body.disk_percent,
        tmp_percent=body.tmp_percent,
        bandwidth_percent=body.bandwidth_percent,
        bandwidth_mbps=body.bandwidth_mbps,
        load_avg=body.load_avg,
        spec=body.spec,
        log_lines=body.log_lines,
        probe_results=body.probe_results)
    resp = await runtime_heartbeat(v1_body, request, credential_id)
    resp["engine_uids_resolved"] = len(resolved_ids)
    resp["engine_uids_stale"] = len(stale_uids)
    return resp


def _validate_task_engine_uid(task_id: str, engine_uid: str) -> None:
    """Issue #1011 上报 v2：按 uid 校验任务归属（任务行 engine_uid 快照 vs 上报引擎 uid）。

    - 上报 uid 与任务行快照不一致 → 403 TASK_ENGINE_UID_MISMATCH（明确拒绝，
      替代 v1 仅按 runtime_id 归属的口径）；
    - 任务行 uid 为空（legacy 任务/未迁移）→ 放行走 v1 语义（兼容期）。
    """
    uid = (engine_uid or "").strip().lower()
    if not uid:
        return
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT engine_uid FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    task_uid = (row[0] or "").strip().lower() if row else ""
    if task_uid and task_uid != uid:
        raise HTTPException(status_code=403, detail={
            "code": "TASK_ENGINE_UID_MISMATCH",
            "message": f"task '{task_id}' belongs to engine_uid '{task_uid}', "
                       f"reported by '{uid}'"})


@router.post("/tasks/{task_id}/complete-v2")
def runtime_task_complete_v2(task_id: str, body: CompleteBodyV2,
                             credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 完成（幂等）：按 uid 校验任务归属后委托 v1 逻辑。"""
    _validate_task_engine_uid(task_id, body.engine_uid)
    v1_body = CompleteBody(result_text=body.result_text, exit_code=body.exit_code,
                           session_id=body.session_id, work_dir=body.work_dir,
                           engine=body.engine, run_token=body.run_token)
    return runtime_task_complete(task_id, v1_body, credential_id)


@router.post("/tasks/{task_id}/fail-v2")
def runtime_task_fail_v2(task_id: str, body: FailBodyV2,
                         credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 失败：按 uid 校验任务归属后委托 v1 逻辑。"""
    _validate_task_engine_uid(task_id, body.engine_uid)
    v1_body = FailBody(error=body.error, failure_reason=body.failure_reason,
                       exit_code=body.exit_code, run_token=body.run_token)
    return runtime_task_fail(task_id, v1_body, credential_id)


@router.post("/tasks/{task_id}/progress-v2")
def runtime_task_progress_v2(task_id: str, body: ProgressBodyV2,
                             credential_id: int = Depends(_require_credential)):
    """Issue #1011 v2 进度：按 uid 校验任务归属后委托 v1 逻辑。"""
    _validate_task_engine_uid(task_id, body.engine_uid)
    v1_body = ProgressBody(summary=body.summary, step=body.step, total=body.total)
    return runtime_task_progress(task_id, v1_body, credential_id)


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
        from src.domains.mgmt.public import resolve_skill_id, get_skill_object_key, get_skill_by_id
        from src.domains.runtime.storage import get_skill_zip_bytes
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
        from src.domains.insight.public import record_usage
        record_usage(task_id, [usage], source=body.source or "report")
    except Exception as e:      # 用量采集绝不能拖垮任务主链路
        log.warning(f"record usage ledger failed task={task_id}: {e}")
    # Prometheus 埋点(与台账同源,保留供外部 Prometheus 抓取)
    try:
        from src.domains.insight.public import record_llm_usage
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
def runtime_recover_orphans(runtime_id: int, body: RecoverOrphansBody | None = None,
                            credential_id: int = Depends(_require_credential)):
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
    # Issue #1495：活跃窗口单点收敛到 task_activity 具名常量（数值沿用 1200s 不变，
    # 原 RECOVER_ACTIVITY_WINDOW 环境变量口除名——「一处定义」硬约束）；
    # 判定改走 has_recent_activity（Python 侧解析 epoch 比较），消除 TEXT 列逐字节
    # 比较对 created_at 写入格式的隐含依赖（表默认值空格格式行 / 无微秒 ISO 行）。
    from src.domains.runtime.engine.task_activity import (RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS,
                                       has_recent_activity)
    RECOVER_ACTIVITY_WINDOW = RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS
    RECOVER_MAX_ATTEMPTS = int(os.environ.get("RECOVER_MAX_ATTEMPTS", "3"))
    RECOVER_GRACE_SECONDS = int(os.environ.get("RECOVER_GRACE_SECONDS", "300"))
    _now = datetime.now(timezone.utc)
    _active_cutoff = (_now - timedelta(seconds=RECOVER_ACTIVITY_WINDOW)).isoformat()
    _grace_cutoff_dt = _now - timedelta(seconds=RECOVER_GRACE_SECONDS)
    # Issue #1750 P1-A：worker 自报活跃白名单（请求体可选，缺省空集合）。
    # 白名单是「调用方主动声明」的正向信号，优先级最高——命中即跳过往返，
    # 静默型长任务（长时间无 task_messages）不再被误判孤儿。
    _active_whitelist = set((body.active_task_ids if body else []) or [])
    # 「认领了但从未启动引擎会话」判死阈值（事件 #9733）：worker 把任务 claim 成 running，
    # 却始终没有写 runtime_session_id（引擎进程压根没起来），此时回炉 queued 只会被同一个
    # worker 再 claim 一次再卡一遍 —— 用户看到的就是永久 running 且 agent 没有任何输出。
    # 用比 300s 宽限期更短的阈值单独判死，让故障更早暴露（引擎冷启动上界 90s 足够）。
    CLAIM_NO_SESSION_FAIL_SECONDS = int(os.environ.get("CLAIM_NO_SESSION_FAIL_SECONDS", "90"))
    # 近期仍在上报消息的任务视为活跃，不参与孤儿回收（窗口需大于回收判定的静默容忍度）
    RECENT_MESSAGE_ACTIVE_SECONDS = int(os.environ.get("RUNTIME_RECENT_MESSAGE_ACTIVE_SECONDS", "180"))
    recovered, skipped_active, force_failed, skipped_young = [], [], [], []
    _fail_reason_by_tid: dict[str, str] = {}
    skipped_active_reason = {}
    with db_lock:
        c = db.cursor()
        # 一并取出「最近一次上报消息的时刻」：引擎说得出话就说明它还活着。
        # 缺这一条时，长任务在两步之间静默超过回收窗口就被判孤儿、回炉重认领，
        # 再卡再回炉，循环里每轮都去抢同一行 events（实测 1205 lock wait timeout），
        # 事件因此永远停在 running，前端表现为「处理中」但没有任何产出。
        c.execute(
            "SELECT t.id, t.attempts, t.runtime_id, t.event_seq, t.started_at, t.created_at, "
            "COALESCE(t.runtime_session_id,''), "
            "(SELECT MAX(m.created_at) FROM task_messages m WHERE m.task_id=t.id) "
            "FROM agent_tasks t WHERE t.runtime_id=? AND t.status IN ('dispatched','running')",
            (runtime_id,))
        rows = c.fetchall()
        _seq_by_tid = {}
        for tid, attempts, rid, ev_seq, _sa, _ca, _rs, _lm in rows:
            _seq_by_tid[tid] = ev_seq
        for tid, attempts, rid, _ev_seq, started_at, created_at, _rsid, _last_msg in rows:
            # Issue #1750 P1-A：白名单命中 → worker 自报仍在跑，直接 skipped_active。
            # 位置在年龄闸门与活跃窗口之前（白名单优先级最高）：静默型长任务即使
            # 超龄且窗口内无消息，也绝不会被回炉。
            if tid in _active_whitelist:
                skipped_active.append(tid)
                skipped_active_reason[tid] = "active_whitelist"
                continue
            # 近期仍在上报消息 = 引擎确实在跑，不能当孤儿回炉。
            # （回收 → 重认领 → 再回收的循环会让事件永远 running，见上方查询注释。）
            _lm_ts = parse_dt(_last_msg) if _last_msg else None
            if _lm_ts is not None and (_now.timestamp() - _lm_ts) <= RECENT_MESSAGE_ACTIVE_SECONDS:
                skipped_active.append(tid)
                skipped_active_reason[tid] = "recent_message"
                continue
            # 年龄基准（started_at，NULL 兜底 created_at）
            _age_base = started_at or created_at
            _age_seconds = None
            if _age_base:
                try:
                    _age_dt0 = datetime.fromisoformat(_age_base)
                    if _age_dt0.tzinfo is None:
                        _age_dt0 = _age_dt0.replace(tzinfo=timezone.utc)
                    _age_seconds = (_now - _age_dt0).total_seconds()
                except Exception:
                    _age_seconds = None
            # 「认领但从未启动会话」→ 直接判死，不进无限回炉（事件 #9733 实锤：
            # worker 认领后引擎进程没起来，runtime_session_id 恒空、task_messages 恒 0，
            # 回炉后同一 worker 再次认领再次卡住，用户侧表现为永久 running 且无任何输出）。
            # 只在「已经回炉过一次（attempts>=1）仍无会话」时判死：首次无会话保留原有
            # 孤儿回收语义（回炉 + attempts+1），避免改变 Issue #1750 基线锁定的行为。
            if (not (_rsid or "").strip()) and (attempts or 0) >= 1 \
                    and _age_seconds is not None \
                    and _age_seconds >= CLAIM_NO_SESSION_FAIL_SECONDS:
                c.execute(
                    "UPDATE agent_tasks SET status='failed', attempts=?, "
                    "result_text=?, completed_at=? WHERE id=? AND status IN ('dispatched','running')",
                    ((attempts or 0) + 1,
                     "claimed but the engine session never started "
                     "(runtime_session_id empty; the worker never spawned the engine process)",
                     _now.isoformat(), tid))
                if rid and rid > 0:
                    from src.domains.runtime.engine.service import release_busy_slot
                    release_busy_slot(rid, cursor=c)
                force_failed.append(tid)
                _fail_reason_by_tid[tid] = (
                    "claimed but the engine session never started "
                    "(runtime_session_id empty)")
                continue
            # 年龄闸门（Issue #370）：started_at（NULL 兜底 created_at）距 now 不足
            # 宽限期 → 直接 skipped，防止刚派发的新任务被误判孤儿反复回炉。
            # 时间戳解析失败保守跳过闸门，继续后续活跃度检查（保持旧行为：回收）。
            if _age_seconds is not None:
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
            try:
                c.execute(
                    "SELECT 1 FROM task_messages WHERE task_id=? AND created_at > ? LIMIT 1",
                    (tid, _active_cutoff))
                if c.fetchone():
                    skipped_active.append(tid)
                    skipped_active_reason[tid] = "recent_activity"
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
                _fail_reason_by_tid[tid] = (
                    "recover-orphans attempts cap reached (possible self-restart loop)")
            else:
                recovered.append(tid)
            # 槽位释放（与 service.py 回收/update_task_status/requeue_task 同款防负数写法）：
            # 旧代码两分支都不减 busy_slots → 每次回炉净漏 1 槽、force-fail 永久泄漏；
            # claim_by_tag 满载即拒领（busy>=max → None），漏 4 槽后引擎哑死。
            if rid and rid > 0:
                from src.domains.runtime.engine.service import release_busy_slot
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
            _reason = _fail_reason_by_tid.get(tid) or (
                "recover-orphans attempts cap reached (possible self-restart loop)")
            update_event_complete(
                _seq, "failed", exit_code=1,
                completed_at=datetime.now(timezone.utc).isoformat(),
                result=f"{_reason}, 任务判死")
        except Exception as e:
            log.warning(f"recover-orphans event writeback failed tid={tid}: {e}")
    if recovered or skipped_active or force_failed or skipped_young:
        log.info(f"recover-orphans runtime={runtime_id}: recovered={recovered} "
                 f"skipped_active={skipped_active} skipped_active_reason={skipped_active_reason} "
                 f"skipped_young={skipped_young} force_failed={force_failed}")
    return {"message": "orphans recovered", "recovered": recovered,
            "skipped_active": skipped_active, "skipped_active_reason": skipped_active_reason,
            "skipped_young": skipped_young, "force_failed": force_failed}


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

def _cwd_or_derived(cwd: str, repo_key: str, agent_type: str, issue_id: str, task_id: str) -> str:
    """任务工作目录：原值为空时按平台同一规则推导。

    `agent_tasks.cwd` 只有部分入队路径会回填；走 claim 的任务常常留空——worker 执行时
    自己按 get_agent_workdir 算路径，但不回写库。接管要靠这个路径在本地建工作区，
    空值会让接管直接失败（本地环境实测就是空字符串）。
    """
    if cwd or not repo_key or not agent_type:
        return cwd
    try:
        from src.domains.runtime.pipeline.event_pipeline import get_agent_workdir
        return get_agent_workdir(repo_key, agent_type, issue_id or "")
    except Exception as e:
        log.warning(f"cwd fallback failed (task={task_id}): {e}")
        return cwd

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
    from src.kernel.session_manager import db, db_lock
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
    _cwd = _cwd_or_derived(task.get("cwd") or "", task.get("repo_key") or "",
                           task.get("agent_type") or "", task.get("issue_id") or "", task_id)
    return {
        "id": task["id"],
        "status": task["status"],
        "agent_type": task.get("agent_type") or "",
        "skill": task.get("skill") or "",
        "prompt": task.get("prompt") or "",
        "env_extra": task.get("env_extra") or "{}",
        "cwd": _cwd,
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
            from src.domains.runtime.engine.scheduler import _update_execution_result
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
            from src.domains.runtime.routes.events import _audit_admin_action
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
    from src.kernel.session_manager import db, db_lock
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
    with _domain_cursor("runtime") as c:
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
    from src.kernel.session_manager import db, db_lock

    repo_clause = ""
    params: list = []
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        _rk_list = [f"gitcode:{_base}", f"github:{_base}"]
        repo_clause = f" AND repo_key IN ({','.join('?' * len(_rk_list))})"
        params.extend(_rk_list)

    with _domain_cursor("runtime") as c:
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
    """按凭据聚合任务（该凭据所有引擎实例的 agent_tasks，见 runtime-admin-two-view-design.md §4.3）。

    P7：聚合范围 = 该凭据下挂载的全部引擎，与设备视图（_build_runtime_devices）
    同一挂载口径（engine_mounts_to_credential，单一来源）：除 credential_id=cid
    的引擎外，还含本机直连引擎（credential_id=0）中 host_name（或本机 hostname）
    等于该凭据 device_name 的引擎——否则设备「查看任务」错误显示「暂无任务」。
    """
    from src.kernel.session_manager import db, db_lock
    from src.domains.runtime.engine.instances import engine_mounts_to_credential
    with _domain_cursor("runtime") as c:
        c.execute("SELECT id FROM agent_runtimes WHERE credential_id=?", (cid,))
        rid_rows = c.fetchall()
        c.execute("SELECT device_name FROM runtime_credentials WHERE id=?", (cid,))
        dev_row = c.fetchone()
    rids = [r[0] for r in rid_rows]
    dev_name = (dev_row[0] or "") if dev_row else ""
    if (dev_name or "").strip():
        # 本机直连引擎按设备视图同口径挂载计入
        with _domain_cursor("runtime") as c:
            c.execute("SELECT id, host_name FROM agent_runtimes WHERE credential_id=0")
            local_rows = c.fetchall()
        rids.extend(rid for rid, hn in local_rows
                    if engine_mounts_to_credential(0, hn, dev_name))
    if not rids:
        return {"tasks": []}
    ph = ",".join("?" for _ in rids)
    clauses = [f"runtime_id IN ({ph})"]
    params = list(rids)
    if status:
        clauses.append("status=?")
        params.append(status)
    params.append(limit)
    with _domain_cursor("runtime") as c:
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
        from src.domains.mgmt.public import get_worker_connect_url
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
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
                               "scripts", "install-worker.sh")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "install script not found"})
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "read install script failed"})
    # 默认 --server 注入本机回连地址：用户漏写 --server 时脚本原来会退回占位域名
    # grape.example.com，worker 永远连不上、状态一直离线（flaskb_11061758 事故）。
    _base = _server_url()
    if _base:
        content = content.replace('SERVER="https://grape.example.com"', f'SERVER="{_base}"')
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
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
                               "scripts", "install-worker.ps1")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "install script not found"})
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "read install script failed"})
    _base = _server_url()
    if _base:
        content = content.replace('$Server = "https://grape.example.com"', f'$Server = "{_base}"')
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
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
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
    from src.kernel.worker_package import worker_code_version
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
        from src.kernel.worker_package import build_package
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
    from src.kernel.worker_package import build_package
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
    from src.kernel.repo_key import to_repo_key
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
    from src.domains.mgmt.public import get_server_base_url  # 跨域走 owner public
    from src.domains.runtime.engine.executor import discover_backends
    from src.kernel.repo_key import from_repo_key

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
    from src.domains.runtime.engine.agent import _scan_local_skills
    import os as _os

    all_skills = _scan_local_skills()
    engine_key = (engine_name or "").strip().lower()
    skill_names = []
    for entry in all_skills:
        if (entry.get("engine") or "").strip().lower() == engine_key:
            skill_names = entry.get("skills") or []
            break

    from src.domains.runtime.engine.os_adapter import engine_skill_dirs, extra_skill_dirs
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
    from src.kernel.session_manager import db, db_lock

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
    from src.kernel.session_manager import db, db_lock

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
    from src.kernel.session_manager import db, db_lock
    with _domain_cursor("runtime") as c:
        c.execute("SELECT agent_tags FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "runtime not found"})
    return {"tags": json.loads(row[0]) if row[0] else []}


@router.put("/runtimes/{runtime_id}/tags", operation_id="runtime_tags_set", summary="Set role tags", description="Sets an engine's role tags (mutual-exclusion applied). engine_capabilities is the single source; agent_tags is the derived cache.")
def set_runtime_tags(runtime_id: int, body: dict, user: dict = Depends(require_no_session_token)):
    """设置 engine 的角色标签。应用互斥规则后写入。

    以 engine_capabilities 为单一数据源，agent_tags 列为派生缓存。
    写入顺序：先写 engine_capabilities，再 sync_agent_tags 回写 agent_tags。
    """
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    from src.domains.mgmt.public import sync_agent_tags
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
        sync_agent_tags(runtime_id)
        db.commit()
    log.info(f"Runtime tags set: rid={runtime_id} tags={tags} (engine_capabilities → agent_tags synced)")
    return {"tags": tags}


class MaxConcurrentBody(BaseModel):
    max_concurrent: int = 20


@router.put("/runtimes/{runtime_id}/max-concurrent", operation_id="runtime_max_concurrent_set", summary="Set max concurrent", description="Sets the engine-level max concurrency.")
def set_runtime_max_concurrent(runtime_id: int, body: MaxConcurrentBody,
                                user: dict = Depends(require_no_session_token)):
    """设置引擎级最大并发数（方案一：并发纯引擎级，标签只做路由）。"""
    _require_runtime_admin(runtime_id, user)  # 扫雷⑨-H3：归属校验
    from src.kernel.session_manager import db, db_lock
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
    from src.domains.mgmt.public import list_scene_market
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
    with _domain_cursor("runtime") as c:
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
    # 扫雷⑨-H3：归属校验
    _require_runtime_admin(runtime_id, user)
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
        # Issue #1208：场景标签变更必须同步派生标签（scene: 冒号行 + agent_tags 快照），
        # 否则 selector 的 scene: 维度仍读到旧的 engine_capabilities 行，出现「改了场景却 0 命中」。
        row = c.execute("SELECT r.name, COALESCE(r.credential_id,0), COALESCE(r.scope,'global'), "
                        "COALESCE(r.engine_uid,''), COALESCE(rc.scope,''), COALESCE(r.runtime_info,'') "
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.id=?", (runtime_id,)).fetchone()
        c.execute("UPDATE agent_runtimes SET scene_tags=? WHERE id=?", (json.dumps(deduped, ensure_ascii=False), runtime_id))
        if row:
            from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
            _cid = row[1] or 0
            _cred_scope = (row[4] or row[2] or "global") if _cid else (row[2] or "global")
            _et_c = ""
            try:
                _et_c = (json.loads(row[5] or "{}").get("engine_type") or "")
            except Exception:
                _et_c = ""
            # [云引擎标签修复] engine: 维度按 engine_type 派生（云四段名不入标签）
            sync_derived_agent_tags(c, runtime_id, row[0], _cred_scope, deduped, row[3] or "",
                                    engine_type=_et_c)
        db.commit()
    log.info(f"Runtime scene_tags set: rid={runtime_id} scene_tags={deduped} (derived tags synced)")
    return {"scene_tags": deduped}


# ===== 引擎别名 =====

class AliasBody(BaseModel):
    alias: str = ""


@router.put("/runtimes/{runtime_id}/alias", operation_id="runtime_alias_set", summary="Set runtime alias", description="Sets an engine alias; an empty string clears it.")
def set_runtime_alias(runtime_id: int, body: AliasBody,
                       user: dict = Depends(require_no_session_token)):
    """设置引擎别名。空字符串表示清除别名。"""
    _require_runtime_admin(runtime_id, user)  # 扫雷⑨-H3：归属校验
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
    from src.domains.insight import public as _obs_public
    _relay_total = _PC("grape_trace_relay_requests_total",
                       "Trace relay requests forwarded to LGTM",
                       ["status"], registry=_obs_public.prometheus_registry())
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


@router.get("/whoami")
def runtime_whoami(cred: CredentialContext = Depends(_require_credential_ctx)):
    """凭据自述（rta 鉴权）——[同机多 cloud_worker spec §4.1]

    返回 token 对应的凭据身份 + 主控集群指纹。两类 worker（普通/cloud）共用的
    凭据自述入口：安装脚本据此派生实例目录/服务名/安装形态；cloud_worker 运行时
    据此做改名跟随与身份守护；升级链路据此恢复安装形态。

    credential_uid：凭据稳定键（UUIDv4，#646 语义）——安装脚本据此派生
    目录键（credential_uid 前 8 位，同机多 worker 单锚，跨主控天然不撞）。

    设计不变量：本响应永不包含凭证类字段（token/api_key）——泄露面为零。
    凭据吊销后本端点 401（安装脚本据此在第一步报"凭据无效或已吊销"）。
    """
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT name, cred_type, platform, credential_uid FROM runtime_credentials WHERE id=? AND status='active'",
            (cred.id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "runtime credential revoked or not found"})
    name, cred_type, platform = row[0] or "", (row[1] or "worker").lower(), (row[2] or "").lower()
    # [锚简化 2026-11-04 用户裁定] server_instance 去除（复合锚 instance 段唯一存在理由=
    # credential_id 跨主控撞号，credential_uid 全局唯一后消解）；credential_uid 替位——
    # 安装脚本据此派生目录键 CRED_KEY（同机多 worker 单锚）。旧库缺列降级 ''（迁移前启动不塌）。
    _cred_uid = ""
    try:
        _cred_uid = row[3] or ""
    except IndexError:
        _cred_uid = ""
    return {
        "credential_id": cred.id,
        "name": name,
        "cred_type": cred_type,          # worker | sandbox
        "platform": platform,            # sandbox 必有, worker 为空
        "scope": cred.scope,
        "credential_uid": _cred_uid,
    }


# =============================================================================
# ===== [沙箱迁移] 云沙箱 / Provider 下发 / 白名单 API（自旧分支移植） =====
# =============================================================================

class SandboxStatusBody(BaseModel):
    issue_id: str = ""
    repo_key: str = ""
    sandbox_id: str = ""
    provider: str = ""
    state: str = ""
    agent_installed: int = 0
    template_id: str = ""
    template_name: str = ""
    engine_type: str = ""
    provider_id: str = ""
    error: str = ""   # [沙箱迁移] state=setup_failed 时的失败原因（建箱/引擎初始化异常）


def _get_task_user_id(task_id: str) -> int:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT user_id FROM agent_tasks WHERE task_id=?", (task_id,))
            row = c.fetchone()
            return int(row[0]) if row and row[0] else 0
    except Exception:
        return 0


def _get_task_repo_key(task_id: str) -> str:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key FROM agent_tasks WHERE task_id=?", (task_id,))
            row = c.fetchone()
            return row[0] or "" if row else ""
    except Exception:
        return ""


def _check_task_access(task_id: str, cred: CredentialContext) -> int:
    """[沙箱迁移] BOLA 校验。global 放行，repo/user 校验后放行。

    核心原则：worker 认领了任务（claim_by_tag 已做 scope 过滤），
    后续操作（start/complete/fail/progress）必须放行，否则任务卡死。
    """
    if cred.scope == 'global':
        return _get_task_user_id(task_id)

    if cred.scope == 'repo':
        _task_repo = _get_task_repo_key(task_id)
        if cred.repo_key and _task_repo:
            try:
                from src.kernel.repo_key import from_repo_key
                _cred_norm = (from_repo_key(cred.repo_key) or cred.repo_key).strip()
                _task_norm = (from_repo_key(_task_repo) or _task_repo).strip()
            except Exception:
                _cred_norm, _task_norm = cred.repo_key.strip(), _task_repo.strip()
            if _cred_norm != _task_norm:
                raise HTTPException(status_code=403, detail={"code": 403, "message": "forbidden"})
        return _get_task_user_id(task_id)

    if cred.scope == 'user':
        _task_user_id = _get_task_user_id(task_id)
        try:
            _cred_user_id = int(cred.owner_id or 0)
        except (ValueError, TypeError):
            _cred_user_id = 0
        if _cred_user_id and _task_user_id and _cred_user_id != _task_user_id:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "forbidden"})
        return _task_user_id

    return _get_task_user_id(task_id)


@router.post("/tasks/{task_id}/sandbox-status")
def runtime_sandbox_status(task_id: str, body: SandboxStatusBody, cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] cloud_worker 上报沙箱创建/状态变更（无 DB，通过此端点写 grape_t_cloud_sandboxes 表）。

    - state=killed → UPDATE state='killed'（P2-14：不 DELETE，防 fire-and-forget 乱序复活）
    - 新沙箱创建 → 同 task_id 旧 running/creating 标 stale
    - INSERT OR IGNORE + UPDATE（killed 保护）
    """
    _task_user_id = _check_task_access(task_id, cred)
    _now = datetime.now(timezone.utc).isoformat()
    try:
        with db_lock:
            c = db.cursor()
            if body.state == 'killed':
                c.execute(
                    "UPDATE grape_t_cloud_sandboxes SET state='killed', last_refresh_at=? WHERE sandbox_id=?",
                    (_now, body.sandbox_id),
                )
                db.commit()
                return {"status": "ok"}
            c.execute(
                "UPDATE grape_t_cloud_sandboxes SET state='stale' "
                "WHERE task_id=? AND state IN ('running', 'creating') AND sandbox_id != ?",
                (task_id, body.sandbox_id))
            from src.kernel.sandbox_uid import new_row_uid as _nru_cs
            c.execute(
                "INSERT OR IGNORE INTO grape_t_cloud_sandboxes "
                "(id, issue_id, repo_key, sandbox_id, provider, provider_id, state, created_at, last_refresh_at, agent_installed, task_id, created_by, "
                "template_id, template_name, engine_type, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_nru_cs(), body.issue_id, body.repo_key, body.sandbox_id, body.provider,
                 body.provider_id or "",
                 body.state, _now, _now, body.agent_installed, task_id, _task_user_id or 0,
                 body.template_id, body.template_name, body.engine_type, body.error or ""),
            )
            # 模板/引擎快照列不随 UPDATE 覆盖（建箱时固化；老上报无值时补写）
            c.execute(
                "UPDATE grape_t_cloud_sandboxes SET provider=?, state=?, last_refresh_at=?, agent_installed=?, "
                "template_id=CASE WHEN template_id='' THEN ? ELSE template_id END, "
                "template_name=CASE WHEN template_name='' THEN ? ELSE template_name END, "
                "engine_type=CASE WHEN engine_type='' THEN ? ELSE engine_type END, "
                "last_error=CASE WHEN ?!='' THEN ? ELSE last_error END "
                "WHERE sandbox_id=? AND state != 'killed'",
                (body.provider, body.state, _now, body.agent_installed,
                 body.template_id, body.template_name, body.engine_type,
                 body.error or "", body.error or "", body.sandbox_id),
            )
            db.commit()
    except Exception as e:
        log.warning(f"sandbox-status write failed: {e}")
        return {"status": "error", "detail": str(e)}
    return {"status": "ok"}


def _fetch_across_domains(sql: str, params=(), domains=("runtime", "mgmt")) -> list:
    """按域依次尝试执行一条只读 SQL，返回全部行。

    三库拓扑下同一张表在不同环境可能落在不同域（例：table_ownership 声明
    ``grape_t_cloud_sandboxes`` 归 runtime，而 beta 实际存在 grape_beta_mgmt）。
    只在「表不存在」（1146 / doesn't exist）时换下一个域重试；其余异常原样抛出，
    不静默降级成空列表（否则查询错误会被读成「没有数据」）。
    """
    from src.kernel.bulk_read import reader

    last = None
    for dom in domains:
        try:
            with reader(dom) as c:
                c.execute(sql, params)
                return c.fetchall() or []
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "1146" not in msg and "doesn't exist" not in msg:
                raise
            last = e
    if last is not None:
        raise last
    return []


@router.get("/cloud-sandboxes", operation_id="runtime_cloud_sandboxes", summary="List cloud sandboxes",
            description="Lists all cloud sandboxes with state, provider, kill_attempts. Supports pagination, search, and filtering.")
def runtime_list_cloud_sandboxes(
    repo_key: str = "",
    provider: str = "",
    provider_id: str = "",
    state: str = "",
    q: str = "",
    page: int = 1,
    page_size: int = 20,
    user: dict = Depends(require_no_session_token),
):
    """[沙箱迁移] 列出云沙箱。只返回当前用户有权限的仓库的沙箱（BOLA 防越权）。"""
    from src.api.deps import check_repo_access

    extra_conditions = []
    extra_params = []
    from src.kernel.bulk_read import reader

    # 三库拓扑：grape_t_cloud_sandboxes / user_accounts / grape_t_sandbox_provider_config
    # 归 mgmt，agent_tasks / agent_runtimes / runtime_credentials 归 runtime。
    # 单条跨域 JOIN 在分库下必然 1146（beta 实测 grape_beta_runtime 无 sandboxes 表），
    # 改为「mgmt 分页查询 + runtime 侧富化」两段读：计数/分页/排序只由 mgmt 单表决定。
    from src.kernel.bulk_read import reader

    # 三库拓扑：grape_t_cloud_sandboxes / user_accounts / grape_t_sandbox_provider_config
    # 归 mgmt，agent_tasks / agent_runtimes / runtime_credentials 归 runtime。
    # 单条跨域 JOIN 在分库下必然 1146（beta 实测 grape_beta_runtime 无 sandboxes 表），
    # 改为「mgmt 分页查询 + runtime 侧富化」两段读：计数/分页/排序只由 mgmt 单表决定。
    # 三库拓扑：沙箱/owner/provider 配置与 agent_tasks/agent_runtimes/runtime_credentials
    # 分属不同域库，单条跨域 JOIN 必然 1146（beta 实测：进程库是 grape_beta_runtime）。
    # 改为「沙箱分页查询 + runtime 侧富化」两段读，分页/计数/排序只由沙箱单表决定；
    # 每段用 _fetch_across_domains 按域重试，避免把表的实际落库位置写死。
    q = (q or "").strip()
    if q:
        # 事件号（t.event_seq）不在沙箱表上：先在任务域解析成 task_id 集合，再回沙箱表过滤。
        # 语义与原 (event_seq = ? OR task_id = ?) 等价；task_id 只在完整 ID（t_ 前缀）时
        # 精确参与（16 位 hash 模糊匹配会命中一堆无关行）。
        if q.startswith("t_"):
            extra_conditions.append("s.task_id = ?")
            extra_params.append(q)
        else:
            _q_rows = _fetch_across_domains("SELECT id FROM agent_tasks WHERE event_seq=?",
                                            (q,), ("runtime", "mgmt"))
            _q_task_ids = [r[0] for r in _q_rows if r[0]]
            if not _q_task_ids:
                return {"total": 0, "page": page, "page_size": page_size, "sandboxes": []}
            extra_conditions.append("s.task_id IN (" + ",".join(["?"] * len(_q_task_ids)) + ")")
            extra_params.extend(_q_task_ids)
    if provider:
        extra_conditions.append("s.provider=?")
        extra_params.append(provider)
    if provider_id:
        extra_conditions.append("s.provider_id=?")
        extra_params.append(provider_id)
    if state:
        extra_conditions.append("s.state=?")
        extra_params.append(state)
    extra_where = (" AND " + " AND ".join(extra_conditions)) if extra_conditions else ""
    _domains = ("mgmt", "runtime")
    # 原 SQL 有两个跨域 LEFT JOIN（user_accounts / sandbox_provider_config 都归 mgmt）。
    # JOIN 要求同库，三库拓扑下必须拆：这里只查本域单表，两个 mgmt 列留 NULL 占位，
    # 由下面的富化段批量补齐（列数与顺序保持不变，下游下标不动）。
    _sel = (
        "SELECT s.sandbox_id, s.issue_id, s.repo_key, s.provider, s.provider_id, s.state, "
        "s.agent_installed, s.kill_attempts, s.created_at, s.last_refresh_at, "
        "s.task_id, s.created_by, NULL, "
        "s.template_id, s.template_name, s.engine_type, s.last_error, "
        "NULL "
        "FROM grape_t_cloud_sandboxes s "
    )
    offset = (page - 1) * page_size
    if repo_key:
        check_repo_access(user, repo_key)
        params = [repo_key] + extra_params
        total = _fetch_across_domains(
            "SELECT COUNT(*) FROM grape_t_cloud_sandboxes s WHERE s.repo_key=?" + extra_where,
            tuple(params), _domains)[0][0]
        _rows = _fetch_across_domains(
            _sel + "WHERE s.repo_key=?" + extra_where + " ORDER BY s.created_at DESC LIMIT ? OFFSET ?",
            tuple(params + [page_size, offset]), _domains)
    else:
        _where = (" WHERE " + " AND ".join(extra_conditions)) if extra_conditions else ""
        # 计数只依赖沙箱单表（原实现的 LEFT JOIN 不改变行数：一沙箱一个 task_id）
        total = _fetch_across_domains(
            "SELECT COUNT(*) FROM grape_t_cloud_sandboxes s" + _where,
            tuple(extra_params), _domains)[0][0]
        _rows = _fetch_across_domains(
            _sel + _where + " ORDER BY s.created_at DESC LIMIT ? OFFSET ?",
            tuple(extra_params + [page_size, offset]), _domains)

    # runtime 侧富化：event_seq / 引擎名 / runtime_info / 凭据名（provider 跟随改名）
    _task_ids = [r[10] for r in _rows if r[10]]
    _cred_ids = [r[17] for r in _rows if r[17]]
    _t_map: dict = {}
    _cr_map: dict = {}
    if _task_ids:
        for _r in _fetch_across_domains(
                "SELECT t.id, t.event_seq, ar.name, ar.runtime_info "
                "FROM agent_tasks t LEFT JOIN agent_runtimes ar ON ar.id = t.runtime_id "
                "WHERE t.id IN (" + ",".join(["?"] * len(_task_ids)) + ")",
                tuple(_task_ids), ("runtime", "mgmt")):
            _t_map[_r[0]] = {"event_seq": _r[1], "name": _r[2], "runtime_info": _r[3]}
    if _cred_ids:
        for _r in _fetch_across_domains(
                "SELECT id, name FROM runtime_credentials WHERE id IN ("
                + ",".join(["?"] * len(_cred_ids)) + ")",
                tuple(_cred_ids), ("runtime", "mgmt")):
            _cr_map[_r[0]] = _r[1] or ""
    # 两个跨域列改为批量补齐（原 LEFT JOIN 的等价物；无匹配 → 空，保持 LEFT 语义）
    _u_map: dict = {}
    _pcred_map: dict = {}
    _owner_ids = [r[11] for r in _rows if r[11]]
    _prov_ids = [r[4] for r in _rows if r[4]]
    if _owner_ids or _prov_ids:
        from src.domains.mgmt import public as _mg_sb
        if _owner_ids:
            _u_map = _mg_sb.usernames_by_ids(_owner_ids)
        if _prov_ids:
            _pcred_map = _mg_sb.sandbox_provider_credential_ids(_prov_ids)

    def _p_ri(_raw):
        try:
            import json as _j
            _d = _j.loads(_raw or "{}")
            return _d if isinstance(_d, dict) else {}
        except Exception:
            return {}

    sandboxes = []
    for r in _rows:
        _t = _t_map.get(r[10]) or {}
        _ri = _p_ri(_t.get("runtime_info"))
        _ar_name = _t.get("name") or ""
        sandboxes.append({
            # provider 显示 = 凭据名（跟随改名；无凭据/空名回落建箱快照 s.provider）
            "sandbox_id": r[0], "issue_id": r[1], "repo_key": r[2],
            "provider": _cr_map.get(_pcred_map.get(r[4]) or r[17]) or r[3],
            "provider_id": r[4], "state": r[5], "agent_installed": bool(r[6]),
            "kill_attempts": r[7], "created_at": r[8], "last_refresh_at": r[9],
            "task_id": r[10], "created_by": r[11],
            "owner_name": (_u_map.get(r[11]) if _u_map else None) or r[12] or "",
            "event_seq": _t.get("event_seq"),
            "engine": ((r[15] or _ri.get("engine_type", "") or _ar_name.split(":")[0])
                       or _ar_name.split(":")[0]),
            "template_id": (r[13] or _ri.get("template_id", "")),
            "template_name": (r[14] or _ri.get("template_name", "")),
            "last_error": (r[16] or ""),
        })

    return {"total": total, "page": page, "page_size": page_size, "sandboxes": sandboxes}


@router.delete("/cloud-sandboxes/{sandbox_id}", operation_id="runtime_cloud_sandbox_kill",
               summary="Kill cloud sandbox", description="Force kill a cloud sandbox by sandbox_id.")
def runtime_kill_cloud_sandbox(sandbox_id: str, user: dict = Depends(require_no_session_token)):
    """[沙箱迁移] 强杀云沙箱。BOLA 校验后调 provider.kill_sandbox。"""
    from src.api.deps import check_repo_access
    with db_lock:
        c = db.cursor()
        # [provider 列语义修复 2026-10-30] grape_t_cloud_sandboxes.provider 列存引擎名第三段
        # (=凭据名规整化, 实测 'cubesandbox-s1')——非 registry 的 platform 键。
        # 统一经 provider_id JOIN spc.platform 取键(与模板检查链路同款修法);
        # provider_id 缺失的旧行回退 provider 列原文(可能仍失败, 报错可见)。
        # 原 SQL LEFT JOIN spc.platform（跨域）→ 拆成本域单表 + mgmt public 补 platform
        c.execute("SELECT provider, state, repo_key, provider_id "
                  "FROM grape_t_cloud_sandboxes WHERE sandbox_id=?", (sandbox_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "sandbox not found"})
    _platform_key = ""
    if row[3]:
        try:
            from src.domains.mgmt import public as _mg_plat
            _platform_key = _mg_plat.sandbox_provider_platform(row[3])
        except Exception as _e:  # noqa: BLE001 - 补不到就回退 provider 列原文
            log.warning(f"kill cloud sandbox: platform lookup failed ({_e})")
    provider_name, state, sandbox_repo_key = _platform_key or row[0], row[1], row[2] or ""
    if sandbox_repo_key:
        check_repo_access(user, sandbox_repo_key)
    if state in ("killed", "kill_failed"):
        return {"status": "already_killed", "state": state}
    from src.domains.runtime.agents.sandbox.backends import default_registry
    registry = default_registry()
    factory = registry.get(provider_name)
    if not factory:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"provider '{provider_name}' not in registry"})
    try:
        provider = factory(None, None, None, None)
        if hasattr(provider, 'kill_sandbox'):
            success = provider.kill_sandbox(sandbox_id)
            if success:
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE grape_t_cloud_sandboxes SET state='killed', last_refresh_at=? WHERE sandbox_id=?",
                              (datetime.now(timezone.utc).isoformat(), sandbox_id))
                    db.commit()
                return {"status": "killed"}
            return {"status": "kill_failed"}
        raise HTTPException(status_code=500, detail={"code": 500, "message": "provider has no kill_sandbox method"})
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"kill cloud sandbox {sandbox_id} failed: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})


@router.post("/tasks/{task_id}/proxy-token")
def runtime_issue_proxy_token(task_id: str, cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] cloud_worker 签发 proxy token（无 DB，通过 HTTP API 写 grape_t_task_proxy_tokens 表）。"""
    _check_task_access(task_id, cred)
    try:
        from src.domains.runtime.agents.sandbox.token import issue_proxy_token, TokenIssueError
        task = service.get_task(task_id)
        _backend = "opencode"
        if task:
            _rn = task.get("runtime_name") or ""
            if _rn:
                from src.domains.runtime.engine.agent import _backend_for_engine
                _backend = _backend_for_engine(_rn) or "opencode"
        token = issue_proxy_token(task_id, _backend)
        return {"token": token}
    except Exception as e:
        log.warning(f"proxy-token issue failed: {e}")
        return {"token": "", "error": str(e)}


@router.delete("/tasks/{task_id}/proxy-token")
def runtime_revoke_proxy_token(task_id: str, cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] cloud_worker 撤销 proxy token。"""
    _check_task_access(task_id, cred)
    try:
        from src.domains.runtime.agents.sandbox.token import revoke_proxy_token
        revoke_proxy_token(task_id)
        return {"status": "ok"}
    except Exception as e:
        log.warning(f"proxy-token revoke failed: {e}")
        return {"status": "error", "detail": str(e)}


@router.get("/provider-configs", operation_id="runtime_provider_configs",
            summary="Dispatch provider configs to cloud_worker",
            description="Returns provider configs with decrypted api_key, scoped to the credential's own providers only. rta_ token can only see providers bound to itself (runtime_credential_id=cred id). v3: returns {providers: [...]} by provider_id.")
def runtime_provider_configs(cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] v3：下发 provider 配置给 cloud_worker（api_key 已解密为明文）。

    [安全修正·用户裁定] rta_ token 一律只能查自己关联的 Provider——
    按 runtime_credential_id=凭据id 过滤，无系统级全量信任。
    （历史：曾有 source==system 门禁，但凭据生成不写 source 列致默认
    'system' 全员放行，形同虚设——source 机制已随安全修正整体移除，
    凭据隔离由 runtime_credential_id 单一维度承担。）
    无凭据来源（cred_id=0）的 Provider 只能 WebUI 管理，不下发任何
    worker；老 worker 要用需将其关联到自己的凭据。
    """
    from src.kernel.crypto import decrypt
    providers = []
    # 原 SQL 把 mgmt 的 provider 配置和本域 runtime_credentials 做跨库 LEFT JOIN。
    # 拆成：mgmt public 取 provider 行 + 本域按 cred.id 取凭据列（rc.id 恒等于该凭据 id）。
    from src.domains.mgmt import public as _mg_pv
    from src.kernel.bulk_read import reader
    _prov_rows = _mg_pv.sandbox_providers_by_credential(cred.id)
    with reader() as c:
        c.execute("SELECT name, scope, credential_uid FROM runtime_credentials WHERE id=?", (cred.id,))
        _rc_row = c.fetchone() or ("", "", "")
    _rc_name, _rc_scope, _rc_uid = _rc_row[0] or "", _rc_row[1] or "", _rc_row[2] or ""
    rows = [(p_[0], _rc_name, p_[1], p_[2], p_[3], p_[4], p_[5], _rc_scope, _rc_uid)
            for p_ in _prov_rows]
    for pid, name, platform, endpoint, api_key_enc, extra_json, max_sandboxes, credential_scope, _cred_uid in rows:
        try:
            extra = json.loads(extra_json) if extra_json else {}
            api_key = decrypt(api_key_enc) if api_key_enc else ""
            providers.append({
                "id": pid,
                "name": name or platform or "",
                "platform": platform or "",
                "endpoint": endpoint,
                "api_key": api_key,
                "extra_config": extra,
                "max_sandboxes": max_sandboxes or 10,
                "credential_scope": credential_scope or "",
                "credential_uid": _cred_uid or "",
            })
        except Exception as e:
            log.warning(f"provider-configs: parse failed for id={pid}: {e}")
    return {"providers": providers}


@router.get("/provider-configs/{provider_id}/active-sandboxes",
            operation_id="runtime_provider_active_sandboxes",
            summary="Get active sandbox count for a provider",
            description="Returns active_count and max_sandboxes for throttling. System-scope credential only.")
def runtime_provider_active_sandboxes(provider_id: str,
                                      cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] v3 F4：查指定 Provider 的活跃沙箱数（流控校验用）。

    [安全修正] 与 provider-configs 同口径（用户裁定：rta_ 一律只查自己）——
    只能查自己凭据关联的 Provider。
    """
    # 表归 mgmt：SQL 在 mgmt public 一份
    from src.domains.mgmt import public as _mg_own
    if not _mg_own.sandbox_provider_owned_by(provider_id, cred.id):
        raise HTTPException(status_code=403,
                            detail={"code": 403, "message": "provider not bound to this credential"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM grape_t_cloud_sandboxes WHERE provider_id=? AND state IN ('creating','running')",
                  (provider_id,))
        active_count = c.fetchone()[0] or 0
        from src.domains.mgmt import public as _mg_max
        max_sandboxes = _mg_max.sandbox_provider_max_sandboxes(provider_id) or 10
    return {"active_count": active_count, "max_sandboxes": max_sandboxes or 10}


@router.get("/cloud-runtimes", operation_id="runtime_cloud_runtimes",
            summary="List existing cloud runtimes for this credential",
            description="Returns cloud runtimes for _refresh_templates orphan detection. System-scope credential only.")
def runtime_cloud_runtimes(cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] v3 R16：查当前 credential 下已有的 cloud 类型 runtime。"""
    runtimes = []
    from src.kernel.bulk_read import reader
    with reader() as c:
        c.execute(
            "SELECT name, version, healthy, installed, cli_path, provider, runtime_type, runtime_info "
            "FROM agent_runtimes WHERE credential_id=? AND runtime_type='cloud'",
            (cred.id,))
        for row in c.fetchall():
            runtimes.append({
                "name": row[0] or "", "version": row[1] or "", "healthy": bool(row[2]),
                "installed": bool(row[3]), "cli_path": row[4] or "",
                "provider": row[5] or "", "runtime_type": row[6] or "cloud",
                "runtime_info": row[7] or "{}",
            })
    return {"runtimes": runtimes}


@router.get("/whitelist", operation_id="runtime_whitelist",
            summary="Dispatch sandbox whitelist domains to worker",
            description="Returns distinct domains from grape_t_engine_sandbox_whitelist table. Worker uses this to build NetworkProxy whitelist.")
def runtime_whitelist(cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] 下发沙箱白名单域名给 worker（worker 不直接查 DB）。"""
    from src.domains.mgmt import public as _mg_wl
    domains = [d for d in _mg_wl.engine_sandbox_whitelist_domains("*") if d]
    return {"domains": domains}


@router.get("/sandbox-paths", operation_id="runtime_sandbox_paths",
            summary="Dispatch sandbox paths to worker",
            description="Returns grape_t_engine_sandbox_paths for a given backend_name. Worker uses this to build bwrap mount paths.")
def runtime_sandbox_paths(backend_name: str = "",
                          cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] 下发沙箱路径配置给 worker。"""
    from src.domains.mgmt import public as _mg_paths
    paths = [{"path": p, "access": a} for p, a in _mg_paths.engine_sandbox_paths_of(backend_name)]
    return {"paths": paths}


@router.get("/skill-egress", operation_id="runtime_skill_egress",
            summary="Dispatch skill egress policy to worker",
            description="Returns content_hash + admin_policy + admin_exceptions for a skill. Worker uses this for PolicyEngine.")
def runtime_skill_egress(skill_name: str = "", skill_id: int = 0,
                         cred: CredentialContext = Depends(_require_credential_ctx)):
    """[沙箱迁移] 下发 skill egress 策略给 worker。"""
    result = {"content_hash": "", "admin_policy": "", "admin_exceptions": []}
    from src.kernel.bulk_read import reader
    with reader() as c:
        if skill_id:
            try:
                c.execute("SELECT content_hash_package FROM skill WHERE id=? AND is_active=1", (int(skill_id),))
                row = c.fetchone()
                if row:
                    result["content_hash"] = row[0] or ""
            except Exception:
                pass
        if skill_name:
            try:
                # 表归 mgmt：SQL 在 mgmt public 一份
                from src.domains.mgmt import public as _mg_eg
                _pol = _mg_eg.egress_policy_for_skill(skill_name) or _mg_eg.egress_policy_global()
                if _pol:
                    result["admin_policy"] = _pol
            except Exception:
                pass
        try:
            from src.domains.mgmt import public as _mg_exc
            for row in _mg_exc.egress_admin_exceptions():
                result["admin_exceptions"].append({
                    "host": row[0], "host_type": row[1], "ports": row[2],
                    "methods": row[3], "scope": row[4], "scope_target": row[5],
                    "skill_digest": row[6],
                })
        except Exception:
            pass
    return result


@router.get("/admin/sandbox/engines", operation_id="runtime_sandbox_engines",
            summary="查询引擎维度沙箱开关")
def get_engine_sandbox_config(user: dict = Depends(require_no_session_token)):
    """[沙箱迁移] 查询沙箱开关配置（全局 + 引擎维度，2e13b015d）。"""
    _global_on = True
    _engines = []
    try:
        from src.domains.mgmt import public as _mg_tg
        for _et, _on in _mg_tg.engine_sandbox_toggles():
            if _et == "global":
                _global_on = bool(_on)
            else:
                _engines.append({"engine_type": _et, "sandbox_on": bool(_on)})
    except Exception:
        _global_on = os.environ.get("GRAPE_AGENT_SANDBOX", "on").lower() in ("1", "true", "on", "yes")
    return {"global_sandbox_on": _global_on, "engines": _engines}


# ===== [沙箱迁移] 沙箱 worker 上报：端点/SSL/模板列表（rta_ 鉴权） =====

class SandboxProviderReportBody(BaseModel):
    """「生成沙箱凭据」一键安装的 cloud_worker 启动上报体。

    对接效果与普通 worker 的 register 对称：沙箱 worker 启动时把
    平台端点、API key、SSL 证书、模板列表上报上来，服务端 UPSERT
    Provider + 登记模板——用户装完即见沙箱页有数据，无需手工配。
    """
    platform: str = ""
    endpoint: str = ""
    api_key: str = ""           # 平台 API key（明文传输经 Bearer rta_ 通道，服务端加密落库）
    ssl_cert: str = ""          # PEM 全文（可空）
    device_name: str = ""
    templates: list = []        # [{template_id, alias, description}]（可空——模板可后扫）
    # [引擎探测 spec] 每模板预装引擎探测结果（worker 建箱实测）：
    # [{template_id, engines: [{engine, version, source}]}]
    # → binding UPSERT status='detected'（enabled=1 直接参与调度；manual 恒赢不降级）
    template_engines: list = []


@router.post("/sandbox-provider-report", operation_id="runtime_sandbox_provider_report",
             summary="Sandbox worker reports platform info (endpoint/SSL/templates)")
def sandbox_provider_report(body: SandboxProviderReportBody,
                            request: Request, credential_id: int = Depends(_require_credential)):
    """[沙箱迁移] 沙箱 worker 上报（幂等 UPSERT）。

    归属：凭据生成者（credential → runtime_credentials.owner_name/user）；
    级别：与凭据 scope 一致（repo→仓库级；user/global→用户级）。
    已有同 platform+scope+owner 的 Provider → 更新 endpoint/key/证书（保留 enabled）；
    无 → 创建（enabled=1）。
    模板列表非空时逐条 UPSERT（按 provider_id+template_id 唯一语义）。
    """
    from src.kernel.session_manager import db, db_lock
    from src.kernel.crypto import encrypt as _enc
    platform = (body.platform or "").strip()
    endpoint = (body.endpoint or "").strip()
    if not platform:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "platform 不能为空"})
    # 平台合法性：后端注册表校验（不硬编码）
    from src.domains.runtime.agents.sandbox.backends import get_cloud_registry
    _cloud = get_cloud_registry()
    if platform not in _cloud:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"未知平台: {platform}（支持: {', '.join(sorted(_cloud))}）"})

    # 凭据归属（生成沙箱凭据的用户）
    _owner_name, _owner_id, _scope, _repo_key = "", 0, "user", ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT scope, owner_name, repo_key FROM runtime_credentials WHERE id=?", (credential_id,))
            _row = c.fetchone()
        if _row:
            _owner_name = _row[1] or ""
            _scope = _row[0] or "user"
            # 凭据 scope → Provider 级别映射：repo→user+repo_key；user/global→user
            if _scope == "repo":
                _repo_key = _row[2] or ""
        # owner_name → 数字 user_id：owner_name 是 grape 登录账号，
        # 先查 grape_accounts.username（本地账号），回退 gitcode 平台映射
        # （平台账号）——与 WebUI 创建 Provider 的 user.get("id") 对齐，
        # UPSERT 才能命中同一条。
        if _owner_name:
            from src.domains.mgmt import public as _mg_acc
            _owner_id = _mg_acc.active_account_id_by_username(_owner_name)
            if not _owner_id:
                from src.api.issues_center import _get_user_id_by_gitcode
                _uid = _get_user_id_by_gitcode(_owner_name)
                if _uid:
                    _owner_id = _uid
    except Exception as _e:
        log.warning(f"sandbox report: credential lookup failed: {_e}")

    _extra = {"ssl_cert_content": body.ssl_cert} if body.ssl_cert else {}
    _extra_json = json.dumps(_extra, ensure_ascii=False)

    with db_lock:
        c = db.cursor()
        # UPSERT：按凭据精确命中（一凭据一 Provider）。[key 机制废弃 2026-10-29]
        # 原 cred_id=0 兜底分支（cloud-local.key 时代无凭据上报）删除——端点本身
        # 已 _require_credential（必须 rta_），credential_id 恒>0，兜底为死代码；
        # Provider 只能由「生成沙箱凭据」诞生，生命周期=凭据生命周期。
        from src.domains.mgmt import public as _mg_pc
        row = _mg_pc.sandbox_provider_by_credential(credential_id) or None
        if row:
            pid, _old_key, _old_extra = row[0], row[1], (row[2] or "")
            _new_key = _enc(body.api_key) if body.api_key else _old_key
            # extra 合并：保留 DB 已有非空项（proxy_node_ip 等），证书覆盖
            try:
                _merged = json.loads(_old_extra) if _old_extra else {}
            except Exception:
                _merged = {}
            _merged.update(_extra)
            # [endpoint 直接入库] 空值不覆盖——endpoint/api_key 权威源在库（生成时入库/
            # 页面编辑），worker 上报已降级为模板补报（不带 endpoint/api_key）
            # 表归 mgmt：SQL 在 mgmt public 一份
            from src.domains.mgmt import public as _mg_pub
            _old_ep = _mg_pub.sandbox_provider_endpoint(pid)
            _mg_pub.update_sandbox_provider(
                pid, (endpoint or _old_ep), _new_key,
                json.dumps(_merged, ensure_ascii=False), _owner_name or "sandbox-worker")
        else:
            from src.kernel.sandbox_uid import new_row_uid as _nru_pc
            pid = _nru_pc()
            # 表归 mgmt：INSERT 走 mgmt public（INSERT 不带 scope/user_id/repo_key，范围由凭据承担）
            from src.domains.mgmt import public as _mg_pub_ins
            _mg_pub_ins.insert_sandbox_provider(
                pid, platform, endpoint, _enc(body.api_key) if body.api_key else "",
                _extra_json, 1, 10, _owner_name or "sandbox-worker", credential_id)
        # 模板登记（可空）
        _tpl_upserted = 0
        for t in (body.templates or []):
            if not isinstance(t, dict):
                continue
            _tid = (t.get("template_id") or "").strip()
            if not _tid:
                continue
            from src.domains.mgmt import public as _mg_pub_tpl
            if _mg_pub_tpl.sandbox_template_id(pid, _tid):
                continue
            from src.kernel.sandbox_uid import new_row_uid as _nru_tp
            _mg_pub_tpl.insert_sandbox_template(
                _nru_tp(), t.get("alias") or _tid, pid, _tid,
                t.get("alias") or "", t.get("description") or "", 1,
                _owner_name or "sandbox-worker", datetime.now(timezone.utc).isoformat())
            _tpl_upserted += 1
        # [引擎探测 spec] 模板引擎探测结果 → binding UPSERT（detected 语义）
        # manual 恒赢：已有 manual binding 的 (provider_id, engine_type) 不降级不改写；
        # detected 直接 enabled=1 参与调度（用户裁定 2026-11-02：无需前端勾选）。
        # 引擎类型校验 = ENGINE_REGISTRY（白名单边界与探测侧一致）。
        _engines_bound = 0
        _now_iso = datetime.now(timezone.utc).isoformat()
        for te in (body.template_engines or []):
            if not isinstance(te, dict):
                continue
            _te_tid = (te.get("template_id") or "").strip()
            if not _te_tid:
                continue
            # 模板 db id（binding 关联的是 grape_t_sandbox_templates.id 不是平台 hash）
            # [F14 禁用传导 2026-11-13] 一并取 enabled——模板禁用（settings toggle）后，
            # 引擎不注册/绑定不回 detected、存量引擎行下线。此前 toggle 只改
            # grape_t_sandbox_templates.enabled，注册链路不读它 → 禁用 75s+ 引擎仍 online
            # （E2E 实证），「绑定引擎将下线」承诺无兑现端（凭据卡/引擎卡不同步根因）。
            from src.domains.mgmt import public as _mg_pub_te
            _tr = _mg_pub_te.sandbox_template_row(pid, _te_tid)
            if not _tr:
                continue  # 模板未登记（本轮 templates 未带上）——跳过
            _tpl_db_id = _tr[0]
            _tpl_enabled = bool(_tr[1])
            if not _tpl_enabled:
                # 禁用模板：binding 降级 disabled + 该模板云引擎行下线（幂等，摘除即终态）
                # [1292 修复 2026-11-14] agent_runtimes.provider_id/template_id 与
                # grape_t_engine_template_binding 的写侧均为字符串锚（注册链 UPDATE ... SET
                # provider_id=? 传 str，见 :531）；读侧比较必须同为字符串——传 int 参数
                # 会触发 MariaDB 把 TEXT 列 cast 成 DECIMAL，存量 '' 行即 1292
                # "Truncated incorrect DECIMAL value: ''"（M1 E2E 实测：worker 探测批
                # provider-report 500，cloud 引擎注册链整链阻断）。binding 表列型为
                # int 时字符串比较 MariaDB 仍数值兼容，不受影响。
                from src.domains.mgmt import public as _mg_pub_bd
                _mg_pub_bd.disable_sandbox_template_bindings(pid, _tpl_db_id, _now_iso)
                c.execute(
                    "UPDATE agent_runtimes SET status='offline', updated_at=? "
                    "WHERE runtime_type='cloud' AND provider_id=? AND template_id=?",
                    (_now_iso, str(pid), str(_tpl_db_id)))
                continue
            from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY
            for eng in (te.get("engines") or []):
                if not isinstance(eng, dict):
                    continue
                _et = (eng.get("engine") or "").strip()
                if not _et or _et not in ENGINE_REGISTRY:
                    continue  # 白名单外引擎不上 binding
                # manual 恒赢：同 (provider_id, engine_type) 存在任意非 detected binding → 跳过
                # （manual 语义按引擎级——手工绑过的引擎整体不被探测改写）
                from src.domains.mgmt import public as _mg_pub_mh
                _manual_hits = _mg_pub_mh.count_manual_sandbox_bindings(pid, _et)
                if _manual_hits:
                    continue
                # [多模板共用引擎修复 2026-11-04] UPSERT 按 (provider, engine, template)
                # 三元组——UNIQUE 约束本就允许同引擎绑多模板（多模板共用引擎迁移语义，
                # db_setup 5383 起），此前实现退化成按 (provider, engine) 单行改写：
                # 上报 [tpl-A: hermes, tpl-B: hermes, ...] 时循环内每条 UPDATE 同一行，
                # 最后遍历的模板胜出，其余模板引擎列恒空（实测 s1/sg：4 模板同装
                # hermes 只落 1 行 binding）。manual 路径（bind-engine API）本就按
                # 三元组，仅探测上报这条窄了。
                from src.domains.mgmt import public as _mg_pub_up
                _mg_pub_up.upsert_sandbox_template_binding(
                    pid, _et, _tpl_db_id, (eng.get("version") or "")[:64], _now_iso)
                _engines_bound += 1
        db.commit()
    log.info(f"sandbox provider report: cred={credential_id} platform={platform} provider_id={pid} "
             f"templates={_tpl_upserted} engines_detected={_engines_bound} owner={_owner_name}")
    return {"provider_id": pid, "templates_upserted": _tpl_upserted,
            "engines_detected": _engines_bound, "status": "ok"}

