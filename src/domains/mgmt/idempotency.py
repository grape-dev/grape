"""Issue #2084：写操作幂等键（Idempotency-Key 请求头 + grape_t_idempotency_keys 表）。

背景（eval-F10 残留，chat send 重复通知 id 9/10 实证）：写超时（504 uncertain）后
调用方按回读指引核对，但「确认未落地后带原键重试」与并发重复提交缺服务端防重复
防线——重复执行上游写产生重复评论/重复 Issue。

设计（Spec v2，comment 200325014）：
- 持久层：沿 session_manager 的 db/db_lock 新建 ``grape_t_idempotency_keys`` 表，
  PK(repo_key, method, path, key) + request_fingerprint / status(in_flight|done) /
  response_status / response_body / created_at，done 记录 TTL 24h（幂等回放窗口）。
- 语义：
  * done + 同指纹 → 回放首次响应（``X-Idempotency-Replayed: true`` 头），不触达上游；
  * in_flight（进行中）→ 409 ``idempotency_in_flight``（并发防重复副作用）；
  * 同键不同指纹 → 422 ``idempotency_key_conflict``（键被复用到不同请求）；
  * 写 504 uncertain → 记录保持 in_flight（盲重试 409 第二道防线，504 body 回读指引
    为第一道）；超过接管窗口（``_IN_FLIGHT_STALE_SECONDS``，默认 120s > 上游超时上限）
    后同键同指纹可重执行——「先回读确认未落地，再带原键重试」的落地路径；
  * 确定性 4xx（上游透传/本地校验，无落地风险）→ 释放键（重试可安全再执行）。
- 接入层：``IdempotentAPIRoute``（挂载在 git-repo 路由组）——缺省（无头/GET/非 tk_
  凭据）行为零变更；repo_key 由 tk_ 会话 token 派生（与鉴权单一信息源一致）。
- 失败安全：幂等层任何内部异常一律 fail-open 透传（防重复是增强防线，不阻断正常写）。

Out of scope：#544 A2A 链路幂等；#1883 读接口默认超时；IM 通道去重。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import NamedTuple

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse, Response

from src.kernel.config import log

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 幂等记录状态（QA 判断项：裸字符串收敛为常量，契约示例/运行时同源引用）
STATUS_IN_FLIGHT = "in_flight"
STATUS_DONE = "done"

# 409/422 detail.message 单一来源（QA 判断项：此前契约示例与运行时文案四处漂移，
# 契约示例缺「接管窗口外…」半句；git_repo_api 契约示例与本模块运行时统一引用此处）
MSG_IDEMPOTENCY_IN_FLIGHT = (
    "同键请求正在执行中：请等待本次完成，或按 504 uncertain 回读指引先核对落地状态"
    "（禁止盲重试；接管窗口外的 in_flight 记录可带原键重试）")
MSG_IDEMPOTENCY_KEY_CONFLICT = (
    "Idempotency-Key 已被不同请求体使用：同一键只能绑定同一逻辑操作"
    "（同 body 重试请保持请求体一致，或换新键）")


class IdemScope(NamedTuple):
    """幂等键命名空间（QA 判断项：收敛 (repo_key, method, path, key) 四参成串的数据泥团）。"""
    repo_key: str
    method: str
    path: str
    key: str

# done 记录保留 24h（Spec：TTL 24h；过期后同键按新请求处理）
_TTL_SECONDS = 24 * 3600
# in_flight 接管窗口：超过即视为本次尝试已终止（写超时/进程崩溃残留），同键同指纹
# 可重执行。默认 120s 远大于上游超时（默认 25s），期间盲重试仍被 409 拦截。
_IN_FLIGHT_STALE_SECONDS = 120

# 建表一次性标记（对齐 outbound_ledger._ensure_table 的连接绑定模式）：
# 同一 db 连接跳过重复 DDL，测试隔离层换新连接后自动失效重跑
_TABLE_READY_CONN = None
_last_cleanup_day = ""


def _ensure_table():
    """幂等建表（跟随 session_manager 现有 db/db_lock 连接）。"""
    global _TABLE_READY_CONN
    from src.kernel.session_manager import db, db_lock
    if _TABLE_READY_CONN is db:
        return
    with db_lock:
        db.execute("""
            CREATE TABLE IF NOT EXISTS grape_t_idempotency_keys (
                repo_key TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                idem_key TEXT NOT NULL,
                request_fingerprint TEXT DEFAULT '',
                status TEXT DEFAULT 'in_flight',
                response_status INTEGER DEFAULT 0,
                response_body TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                PRIMARY KEY (repo_key, method, path, idem_key)
            )
        """)
        db.commit()
        _TABLE_READY_CONN = db


# 公开别名（测试隔离/运维预建表用）
ensure_table = _ensure_table


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(s: str) -> float:
    """'%Y-%m-%d %H:%M:%S' → epoch 秒；解析失败返回 0（按过期处理，安全接管）。"""
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def _cleanup_expired():
    """TTL 24h 过期清理（best-effort，每天最多一次，失败不影响主流程）。"""
    global _last_cleanup_day
    _today = time.strftime("%Y-%m-%d")
    if _today == _last_cleanup_day:
        return
    _last_cleanup_day = _today
    try:
        from datetime import datetime, timedelta
        from src.kernel.session_manager import db, db_lock
        _cutoff = (datetime.now() - timedelta(seconds=_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        with db_lock:
            db.execute("DELETE FROM grape_t_idempotency_keys WHERE created_at < ?", (_cutoff,))
            db.commit()
    except Exception as e:
        log.warning(f"idempotency cleanup failed: {e}")


def fingerprint_request_body(raw: bytes) -> str:
    """请求体指纹（sha256）：同键同 body 重放判定；空体（DELETE 等）为常量指纹。"""
    return hashlib.sha256(raw or b"").hexdigest()


def begin_write(scope: IdemScope, fingerprint: str) -> tuple[str, dict | None]:
    """写操作幂等预检 + 登记（db_lock 内原子完成）。

    返回 (state, stored)：
    - ("new", None)       → 本次尝试登记 in_flight，调用方执行写；done/TTL 过期/接管均归此态
    - ("replay", stored)  → 已完成且同指纹：回放 stored={status, body}，不触达上游
    - ("in_flight", None) → 进行中（接管窗口内）：409
    - ("conflict", None)  → 同键不同指纹：422
    """
    _ensure_table()
    _cleanup_expired()
    from src.kernel.session_manager import db, db_lock
    now = time.time()
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT request_fingerprint, status, response_status, response_body, created_at "
            "FROM grape_t_idempotency_keys WHERE repo_key=? AND method=? AND path=? AND idem_key=?",
            scope)
        row = c.fetchone()
        if row:
            row_fp, row_status, row_rs, row_rb, row_created = row
            created_ts = _parse_ts(row_created or "")
            if (row_fp or "") != fingerprint:
                return "conflict", None
            if row_status == STATUS_DONE:
                if now - created_ts <= _TTL_SECONDS:
                    return "replay", {"status": int(row_rs or 200), "body": row_rb or ""}
                # TTL 过期：按新请求处理（删旧行后走登记）
                c.execute("DELETE FROM grape_t_idempotency_keys "
                          "WHERE repo_key=? AND method=? AND path=? AND idem_key=?",
                          scope)
            else:  # in_flight
                if now - created_ts <= _IN_FLIGHT_STALE_SECONDS:
                    return STATUS_IN_FLIGHT, None
                # 接管窗口外（写超时/进程崩溃残留）：本次尝试接管，时间戳刷新
                c.execute("UPDATE grape_t_idempotency_keys SET created_at=?, updated_at=? "
                          "WHERE repo_key=? AND method=? AND path=? AND idem_key=?",
                          (_now_str(), _now_str(), *scope))
                db.commit()
                return "new", None
        c.execute(
            "INSERT INTO grape_t_idempotency_keys "
            "(repo_key, method, path, idem_key, request_fingerprint, status, "
            " response_status, response_body, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'in_flight',0,'',?,?)",
            (*scope, fingerprint, _now_str(), _now_str()))
        db.commit()
        return "new", None


def complete_write(scope: IdemScope, status_code: int, body_text: str):
    """写成功（2xx）→ done + 存储首次响应（后续同键回放）。best-effort 不阻断。"""
    try:
        _ensure_table()
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            db.execute(
                "UPDATE grape_t_idempotency_keys SET status='done', response_status=?, response_body=?, "
                "updated_at=? WHERE repo_key=? AND method=? AND path=? AND idem_key=?",
                (int(status_code), body_text or "", _now_str(), *scope))
            db.commit()
    except Exception as e:
        log.warning(f"idempotency complete failed (repo={scope.repo_key} "
                    f"key={scope.key[:16]}): {e}")


def release_write(scope: IdemScope):
    """确定性失败（4xx：无落地风险）→ 释放键，重试可安全再执行。best-effort 不阻断。"""
    try:
        _ensure_table()
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            db.execute("DELETE FROM grape_t_idempotency_keys "
                       "WHERE repo_key=? AND method=? AND path=? AND idem_key=?",
                       scope)
            db.commit()
    except Exception as e:
        log.warning(f"idempotency release failed (repo={scope.repo_key} "
                    f"key={scope.key[:16]}): {e}")


def _peek_repo_key(request: Request) -> str:
    """按 tk_ 会话 token 派生 repo_key（与鉴权层单一信息源一致；非 tk_/解析失败返回空）。

    git-repo 写端点仅接受会话级临时 token（_repo_token 强制，其余 403），故空返回
    即「本次请求不可能走通 git-repo 写链路」→ 幂等层跳过、交由既有鉴权路径判定。
    """
    try:
        from src.api.deps import extract_api_token
        token = extract_api_token(request)
        if not token.startswith("tk_"):
            return ""
        from src.kernel.auth_sdk.auth_token import verify_session_token
        info = verify_session_token(token) or {}
        return info.get("repo_key") or ""
    except Exception:
        return ""


def _replay_response(stored: dict) -> Response:
    """回放首次响应（X-Idempotency-Replayed: true 显式标记）。"""
    body_text = stored.get("body") or ""
    try:
        content = json.loads(body_text) if body_text else {}
    except Exception:
        content = None  # 非结构化残留（防御）：原样字节回放
    headers = {"X-Idempotency-Replayed": "true"}
    if content is None:
        return Response(content=body_text.encode(), status_code=int(stored.get("status") or 200),
                        media_type="application/json", headers=headers)
    return JSONResponse(content=content, status_code=int(stored.get("status") or 200),
                        headers=headers)


class IdempotentAPIRoute(APIRoute):
    """git-repo 写路由幂等包装（Issue #2084）。

    挂载于 git-repo 路由组（route_class）——对每个进入的请求：
    1. 非 POST/PATCH/PUT/DELETE、无 Idempotency-Key、非 tk_ 凭据 → 原样透传（零变更）；
    2. begin_write 预检：replay → 回放首次响应；in_flight → 409；conflict → 422；
       new → 执行原 handler；
    3. 执行结果落账：2xx → complete_write（done + 回放源）；4xx（确定性失败）→
       release_write（键可复用）；5xx/504（落地状态不确定）→ 保持 in_flight
       （盲重试 409 防线，接管窗口后按回读确认路径放行）。
    4. 幂等层自身异常一律 fail-open 透传（防重复是增强防线，不阻断正常写）。
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def idempotent_handler(request: Request) -> Response:
            try:
                key = (request.headers.get(IDEMPOTENCY_KEY_HEADER) or "").strip()
            except Exception:
                key = ""
            if (request.method not in WRITE_METHODS) or not key:
                return await original(request)
            repo_key = _peek_repo_key(request)
            if not repo_key:
                return await original(request)
            try:
                from src.kernel.config import request_id_var
                raw = await request.body()
                fp = fingerprint_request_body(raw)
                scope = IdemScope(repo_key, request.method, request.url.path, key)
                state, stored = begin_write(scope, fp)
                if state == "replay":
                    log.info(f"idempotency replay: {request.method} {request.url.path} "
                             f"key={key[:24]} (repo={repo_key})")
                    return _replay_response(stored)
                if state == STATUS_IN_FLIGHT:
                    raise HTTPException(status_code=409, detail={
                        "code": 409,
                        "reason": "idempotency_in_flight",
                        "request_id": request_id_var.get(),
                        "message": MSG_IDEMPOTENCY_IN_FLIGHT})
                if state == "conflict":
                    raise HTTPException(status_code=422, detail={
                        "code": 422,
                        "reason": "idempotency_key_conflict",
                        "request_id": request_id_var.get(),
                        "message": MSG_IDEMPOTENCY_KEY_CONFLICT})
                # state == "new"：执行原 handler 并按结果落账
                try:
                    response = await original(request)
                except RequestValidationError:
                    # 本地请求校验失败（body/path/query 类型不符，请求未触达上游，
                    # 确定性失败无落地风险）→ 释放键：同键修正 body 重试可安全再执行
                    # （QA 复审键毒化修复：否则键绑定坏 body 指纹保持 in_flight 至 TTL，
                    # 修正重试被 422 idempotency_key_conflict 拒绝，违背键释放承诺）
                    release_write(scope)
                    raise
                except HTTPException as e:
                    # ≥500（含 504 uncertain）：落地状态不确定 → 保持 in_flight（防盲重试）
                    if e.status_code < 500:
                        release_write(scope)
                    raise
                except Exception:
                    # 内部异常（500）：结果未知 → 保守保持 in_flight
                    raise
                if response.status_code < 300:
                    try:
                        body_bytes = getattr(response, "body", b"") or b""
                        complete_write(scope, response.status_code,
                                       body_bytes.decode("utf-8", "replace"))
                    except Exception as e:
                        log.warning(f"idempotency capture failed: {e}")
                elif response.status_code < 500:
                    # 4xx 确定性失败（无落地风险）→ 释放键
                    release_write(scope)
                # ≥500 保持 in_flight（接管窗口后按回读确认路径放行）
                return response
            except HTTPException:
                raise
            except RequestValidationError:
                # R2（QA Round 2 判断级残余）：内层已释放键并 re-raise 的本地校验 422——
                # 非 HTTPException，若落入 except Exception 会 fail-open 误报日志且
                # original(request) 校验双执行；直通 FastAPI 422 处理
                raise
            except Exception as e:
                # 幂等层自身故障：fail-open 透传（不阻断正常写）
                log.warning(f"idempotency layer failed (fail-open): {e}")
                return await original(request)

        return idempotent_handler
