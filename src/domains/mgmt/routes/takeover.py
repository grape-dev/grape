"""接管（takeover）HTTP 接口。

设计说明：接口约定见仓库内 takeover 文档。
5 个端点：
  POST /api/takeover/sessions                 开始（原子：cancel + mark + 签发 tko_ + bundle）
  GET  /api/takeover/sessions/{id}            状态 / 续期（?renew=1）
  POST /api/takeover/sessions/{id}/progress   进展写回 task_messages（actor=username）
  POST /api/takeover/sessions/{id}/finish     结束本节点（事件终态 + stage 推进 + 可选派发）
  POST /api/takeover/sessions/{id}/abort      放弃（只停，不重派）

鉴权：全部 user_only（`require_no_session_token` → user_scope 能力位）。
start 额外做 `check_repo_access`（接管 token 本身不走 admin 豁免）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.deps import check_repo_access, require_no_session_token, require_user_or_takeover
from src.kernel.config import log
# 接管服务走 owner 域 public 门面（原 import runtime.engine.takeover 内部模块）
from src.domains.runtime import public as svc

router = APIRouter(prefix="/api/takeover", tags=["takeover"])


# ----------------------------------------------------------------------------- bodies

class StartBody(BaseModel):
    repo_key: str = ""          # gitcode:owner/repo
    owner: str = ""
    repo: str = ""
    issue_number: str = ""
    agent_type: str = ""
    platform: str = ""          # dsh / claude-code / codex / ...
    host: str = ""
    client_version: str = ""
    supports: dict = Field(default_factory=dict)
    # 跨平台适配：宿主 OS / 架构 / shell（缺省 linux+bash，旧客户端不受影响）
    host_os: str = ""
    host_arch: str = ""
    host_shell: str = ""


class ProgressBody(BaseModel):
    messages: list = Field(default_factory=list)
    summary: str = ""


class RequestFinishBody(BaseModel):
    summary: str = ""
    evidence: str = ""
    files_written: list = Field(default_factory=list)
    # 结构化结论（与 worker self-complete 同构）：动作清单 / 产出清单 / 下一步
    actions: list = Field(default_factory=list)
    artifacts: list = Field(default_factory=list)
    next_step: str = ""
    issue_summary: str = ""


class AttachBody(BaseModel):
    client_session: str = ""
    host: str = ""
    client_version: str = ""


class FinishBody(BaseModel):
    outcome: str = "completed"          # completed | failed
    summary: str = ""
    evidence: str = ""
    files_written: list = Field(default_factory=list)
    handoff: str = "a2a"                # a2a(默认) | none
    next_agent: str = ""
    actions: list = Field(default_factory=list)
    artifacts: list = Field(default_factory=list)
    issue_summary: str = ""


class AbortBody(BaseModel):
    redispatch: bool = True
    reason: str = ""


# ----------------------------------------------------------------------------- helpers

def _repo_key(body: StartBody) -> str:
    rk = (body.repo_key or "").strip()
    if rk:
        return rk if ":" in rk else f"gitcode:{rk}"
    if body.owner and body.repo:
        return f"gitcode:{body.owner}/{body.repo}"
    return ""


def _auth_headers(request: Request) -> dict:
    """把调用方自己的凭据透传给内部 A2A 派发（保持同一用户身份）。"""
    headers: dict[str, str] = {}
    if request.headers.get("authorization"):
        headers["authorization"] = request.headers["authorization"]
    if request.headers.get("private-token"):
        headers["private-token"] = request.headers["private-token"]
    return headers


def _err(e: svc.TakeoverError) -> JSONResponse:
    return JSONResponse(status_code=e.status,
                        content={"ok": False, "code": e.code, "message": e.message, **e.extra})


# ----------------------------------------------------------------------------- endpoints

@router.post("/sessions", operation_id="takeover_start",
             summary="Start takeover (atomic hand-off of a running task)")
def start_takeover(body: StartBody, request: Request,
                   user: dict = Depends(require_no_session_token)):
    """开始接管：找任务 → 取消 → 标记 takeover → 签发 tko_ → 返回 bundle。"""
    repo_key = _repo_key(body)
    try:
        if not repo_key:
            raise svc.TakeoverError(400, "bad_request", "需要 repo_key 或 owner+repo")
        check_repo_access(user, repo_key)
        res = svc.start_takeover(
            repo_key=repo_key, issue_number=body.issue_number, user=user,
            agent_type=body.agent_type, platform=body.platform, host=body.host,
            client_version=body.client_version, supports=body.supports,
            base_url=str(request.base_url),
            host_os=body.host_os, host_arch=body.host_arch, host_shell=body.host_shell,
        )
    except svc.TakeoverError as e:
        return _err(e)
    sess = res.get("session") or {}
    return {
        "ok": True,
        "takeover_id": sess.get("id") or "",
        "reused": bool(res.get("reused")),
        "bundle": res.get("bundle"),
        "warnings": (res.get("bundle") or {}).get("warnings") or [],
        "cancel_ok": res.get("cancel_ok", True),
        "cancel_error": res.get("cancel_error", ""),
        "mark_status": res.get("mark_status", ""),
    }


@router.get("/sessions", operation_id="takeover_list",
            summary="List takeover sessions for a repo/issue (active by default)")
def list_takeovers(repo_key: str, issue_id: str = "", status: str = "active", limit: int = 20,
                   user: dict = Depends(require_no_session_token)):
    """面板"接管中"状态的服务端来源：浏览器刷新/换标签后仍能拿到 active 会话。"""
    try:
        check_repo_access(user, repo_key)
        rows = svc.list_takeovers(repo_key, issue_id=issue_id, status=status, limit=limit)
        return {"ok": True, "sessions": rows}
    except svc.TakeoverError as e:
        return _err(e)


@router.get("/sessions/{session_id}", operation_id="takeover_get",
            summary="Get takeover session (optionally renew token)")
def get_takeover(session_id: str, renew: int = 0,
                 user: dict = Depends(require_no_session_token)):
    try:
        sess = svc.get_session(session_id)
        if not sess:
            raise svc.TakeoverError(404, "not_found", "接管会话不存在")
        if int(sess.get("user_id") or 0) != int(user.get("id") or 0):
            raise svc.TakeoverError(403, "not_owner", "该接管会话不属于当前用户")
        out = {"ok": True, "session": {k: v for k, v in sess.items() if k != "bundle_json"},
               "bundle": json.loads(sess.get("bundle_json") or "{}") if sess.get("bundle_json") else None,
               "token_expires_at": sess.get("token_expires_at") or 0}
        if renew:
            out["renew"] = svc.renew_takeover_session(session_id=session_id, user=user)
        return out
    except svc.TakeoverError as e:
        return _err(e)


@router.post("/sessions/{session_id}/progress", operation_id="takeover_progress",
             summary="Report progress from the local session (written to task_messages)")
def takeover_progress(session_id: str, body: ProgressBody,
                      user: dict = Depends(require_user_or_takeover)):
    try:
        sess = svc.get_session(session_id)
        if not sess:
            raise svc.TakeoverError(404, "not_found", "接管会话不存在")
        # 两类调用方：
        #  ① 用户（面板/grape_ key）：必须是会话所属用户；
        #  ② 本地会话自己（tko_ token）：只允许上报"自己这条接管"的进展
        #     —— 否则 agent 无法把进展写回平台（它没有 user_scope，这是设计使然）。
        if str(user.get("auth_type") or "") == "takeover":
            from src.kernel.auth_sdk.auth_token import takeover_session_key as _tko_key
            if str(user.get("session_key") or "") != _tko_key(sess.get("task_id") or ""):
                raise svc.TakeoverError(403, "not_owner", "该接管 token 不属于此会话")
        elif int(sess.get("user_id") or 0) != int(user.get("id") or 0):
            raise svc.TakeoverError(403, "not_owner", "该接管会话不属于当前用户")
        if sess.get("status") != svc.takeover_active_status():
            raise svc.TakeoverError(409, "not_active", f"接管会话已结束（{sess.get('status')}）")
        res = svc.append_takeover_messages(task_id=sess.get("task_id") or "",
                                           entries=body.messages,
                                           actor=user.get("username") or str(user.get("id") or "user"))
        # 心跳：面板用它显示"本地最后活动 X 分钟前"（接管期间平台侧唯一的活性信号）
        # 表归运行域：走 owner public 的具名写原语（单库拓扑回落进程主库并提交），
        # 失败不阻断进展上报（与原实现同一姿态）。
        try:
            from src.domains.runtime import public as _rt_takeover  # noqa: PLC0415
            _rt_takeover.takeover_touch_updated_at_any(session_id, svc.now_iso())
        except Exception as _e:
            log.warning(f"takeover progress: touch updated_at failed: {_e}")
        if body.summary:
            log.info(f"takeover {session_id} progress: {body.summary[:160]}")
        return {"ok": True, **res}
    except svc.TakeoverError as e:
        return _err(e)


@router.post("/sessions/{session_id}/attach", operation_id="takeover_attach",
             summary="Attach the local session id (so the panel can jump back to it)")
def takeover_attach(session_id: str, body: "AttachBody",
                    user: dict = Depends(require_no_session_token)):
    try:
        res = svc.attach_client_session(session_id=session_id, user=user,
                                        client_session=body.client_session, host=body.host,
                                        client_version=body.client_version)
        return {"ok": True, **res}
    except svc.TakeoverError as e:
        return _err(e)


@router.post("/sessions/{session_id}/request-finish", operation_id="takeover_request_finish",
             summary="Local agent reports completion (content only; user still confirms the hand-off)")
def takeover_request_finish(session_id: str, body: RequestFinishBody,
                            user: dict = Depends(require_user_or_takeover)):
    """agent（tko_）提交完成内容；推进仍由用户点 finish（user_scope）授权。"""
    try:
        res = svc.request_finish(session_id=session_id, user=user, summary=body.summary,
                                 evidence=body.evidence, files_written=body.files_written,
                                 actions=body.actions, artifacts=body.artifacts,
                                 next_step=body.next_step, issue_summary=body.issue_summary)
        return {"ok": True, **res}
    except svc.TakeoverError as e:
        return _err(e)


@router.post("/sessions/{session_id}/finish", operation_id="takeover_finish",
             summary="Finish the taken-over node (event finalize + stage advance + optional handoff)")
def takeover_finish(session_id: str, body: FinishBody, request: Request,
                    user: dict = Depends(require_no_session_token)):
    try:
        res = svc.finish_takeover(
            session_id=session_id, user=user, outcome=body.outcome, summary=body.summary,
            evidence=body.evidence, files_written=body.files_written,
            handoff=body.handoff, next_agent=body.next_agent,
            actions=body.actions, artifacts=body.artifacts, issue_summary=body.issue_summary,
            auth_headers=_auth_headers(request),
            base_url=str(request.base_url),
        )
        return {"ok": True, **res}
    except svc.TakeoverError as e:
        return _err(e)


@router.post("/sessions/{session_id}/abort", operation_id="takeover_abort",
             summary="Abort takeover (stop only; never re-dispatch the same node)")
def takeover_abort(session_id: str, body: AbortBody,
                   user: dict = Depends(require_no_session_token)):
    try:
        res = svc.abort_takeover(session_id=session_id, user=user, reason=body.reason,
                                 redispatch=bool(getattr(body, 'redispatch', True)))
        return {"ok": True, **res}
    except svc.TakeoverError as e:
        return _err(e)