"""Chat Agent 路由：对话、消息、配置、反馈"""
import json, asyncio, tempfile, os, threading

from src.config import WORKSPACE_DIR, log
from src.prompt_builder import build_general_chat_prompt

# Chat 工作目录下发的逻辑基准：worker 端 src/runtime/agent.py `_remap_cwd` 的默认映射契约
# （/home/grape → 该 worker 用户 home）。服务端必须用逻辑基准下发而非本机 WORKSPACE_DIR，
# 否则非 root 用户 worker（如 mlc）拿 /opt/grape-workspaces/... 映射不到 → 写 /root → Permission denied。
CHAT_CWD_BASE = "/home/grape"

def _get_chat_cwd(user_id: int, conv_id: str) -> str:
    """每个 conversation 独立工作目录，避免相互影响。

    返回逻辑路径（/home/grape/chat/...），远端 worker 执行时按 _remap_cwd 映射到
    各自用户 home；本机 direct 执行（root）下 /home/grape 亦可写。
    """
    path = os.path.join(CHAT_CWD_BASE, "chat", str(user_id), conv_id)
    os.makedirs(path, exist_ok=True)
    return path
# 限制同时运行的 opencode 子进程数（防止大量用户并发导致服务器崩溃）
_CHAT_AGENT_SEMAPHORE = threading.Semaphore(20)
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.user import db as user_db
from src.session_manager import (
    create_conversation, get_user_conversations, get_conversation,
    add_message, get_messages, touch_conversation, update_conversation,
    delete_messages_by_conv, delete_conversation,
    get_chat_config as _get_chat_config,
    set_chat_config as _set_chat_config,
    mark_task_processing_by_conversation,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatMessage(BaseModel):
    message: str
    repo_key: str = ""
    conversation_id: str = ""


class FeedbackBody(BaseModel):
    seq: int
    feedback_type: str = "create_issue"
    comment: str = ""


# ====== Conversations ======

@router.get("/conversations")
async def list_conversations(repo_key: str = Query(default=""), user: dict = Depends(get_current_user)):
    convs = get_user_conversations(user["id"], repo_key=repo_key)
    return {"conversations": convs}


class NewConversationBody(BaseModel):
    repo_key: str = ""


@router.post("/conversations")
async def new_conversation(body: NewConversationBody, user: dict = Depends(get_current_user)):
    cid = create_conversation(user["id"], repo_key=body.repo_key)
    return {"conversation_id": cid}


@router.delete("/conversations/{conv_id}")
async def remove_conversation(conv_id: str, user: dict = Depends(get_current_user)):
    conv = get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "对话未找到"})
    delete_messages_by_conv(conv_id)
    delete_conversation(conv_id)
    return {"message": "对话已删除"}


class EnsureIssueConvBody(BaseModel):
    repo_key: str
    issue_number: int


@router.post("/conversations/ensure-issue")
async def ensure_issue_conversation(body: EnsureIssueConvBody, user: dict = Depends(get_current_user)):
    """基于 repo_key + issue_number 查找或新建任务会话（Task Center 跳转用）。

    会话被删除后，Task Center 双击跳转时调用此接口重建「Issue #N」会话，
    并写入任务上下文消息 + 从 GitCode Issue 补拉 agent 评论。
    """
    from src.api.routes.tasks import _ensure_task_conversation, _get_issue_title
    from src.repo_key import to_repo_key
    rk = to_repo_key(body.repo_key)
    title = ""
    try:
        title = _get_issue_title(rk, body.issue_number)
    except Exception:
        pass
    conv_id = _ensure_task_conversation(user["id"], rk, body.issue_number, title=title)
    if not conv_id:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "创建会话失败"})
    # 更新 task 表中的 conversation_id 关联
    try:
        from src.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            c.execute("UPDATE tasks SET conversation_id=? WHERE repo_key=? AND issue_number=?",
                      (conv_id, rk, body.issue_number))
            _db.commit()
    except Exception:
        pass
    # 从 GitCode Issue 补拉 agent 评论到新会话
    try:
        from src.api.event_pipeline import _sync_issue_comments_to_conversation
        from src.user.db import get_gitcode_token
        from src.repo_key import from_repo_key
        _repo = from_repo_key(rk)
        _token = get_gitcode_token(repo_key=rk) or ""
        if _repo and _token:
            _sync_issue_comments_to_conversation(rk, body.issue_number, _token, repository=_repo)
    except Exception:
        pass
    return {"conversation_id": conv_id}


# ====== Messages ======

@router.get("/messages/{conv_id}")
async def list_messages(conv_id: str, before: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    conv = get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "对话未找到"})
    # 任务会话：打开时自动从 GitCode Issue 补拉 agent 评论到会话
    # （agent 通过技能 curl 在 Issue 上发的澄清追问等评论，可能未同步到会话）
    if not before and conv.get("conv_type") == "task" and conv.get("issue_number"):
        try:
            from src.api.event_pipeline import _sync_issue_comments_to_conversation
            from src.user.db import get_gitcode_token
            from src.repo_key import from_repo_key
            _rk = conv.get("repo_key", "")
            _repo = from_repo_key(_rk) if _rk else ""
            _token = get_gitcode_token(repo_key=_rk) or ""
            if _repo and _token:
                _sync_issue_comments_to_conversation(_rk, conv["issue_number"], _token, repository=_repo)
        except Exception:
            pass  # 旁路同步，不影响消息加载
    msgs = get_messages(conv_id, before_id=before)
    return {"messages": msgs, "conversation": conv}


# ====== Send ======

def _auto_title(conv: dict, message: str):
    """首次消息时自动生成对话标题"""
    if conv and not conv.get("title") and message:
        title = message.strip()[:30]
        if len(message) > 30:
            title += "..."
        update_conversation(conv["id"], title=title)


async def _push_reply_to_feishu(conv: dict | None, user: dict, text: str):
    """web 答复推送到飞书（feishu-web-chat-unified-design.md §3.2）。

    conversation 已被飞书接入（im_channel_id 非空）时，用该用户配置的 chat_bot
    主动推送答复给其飞书 open_id。P2P 场景 im_channel_id 即用户 open_id。
    """
    try:
        im_channel_id = (conv or {}).get("im_channel_id", "")
        if not im_channel_id:
            return
        from src.session_manager import db, db_lock
        from src.api.routes.tasks import _get_feishu_open_id
        open_id = _get_feishu_open_id(user.get("username", ""))
        if not open_id and im_channel_id.startswith("ou_"):
            open_id = im_channel_id
        if not open_id:
            return
        from src.api import main as api_main
        bm = api_main.bot_manager
        if not bm or not bm.api:
            return
        app_id = ""
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT im_app_id FROM chat_agent_session WHERE user_id=? AND im_app_id!='' LIMIT 1", (user.get("id") or 0,)).fetchone()
            if row:
                app_id = row[0]
        if not app_id:
            return
        ctx = bm.get_bot(app_id)
        if not ctx:
            return
        await bm.api.send_card_to_user(ctx, open_id, f"[来自Web] {text[:1800]}", title="Chat Agent")
        from src.config import log
        log.info(f"Chat web->feishu push: conv={(conv or {}).get('id')} open_id={open_id[:20]} text_len={len(text)}")
    except Exception as e:
        from src.config import log
        log.warning(f"Chat web->feishu push failed: {e}")


def _resolve_gitcode_token(conv: dict | None, conv_id: str, user_token: str, repo_key: str = "") -> str:
    """解析注入 agent 的 GitCode token：优先用户当前 token（user_accounts），
    并在会话缓存过期/为空时刷新 conversation.user_gitcode_token。

    修复：会话创建时缓存旧 token（曾 401 Bad credentials），非空缓存永不被回填刷新
    （chat.py 原逻辑仅空值才覆盖）。见 2026-08-16 修复。

    兜底：用户 token 与会话缓存均为空时，取仓库 api token
    （get_gitcode_token：devops_agent_session.repo_gitcode_token → managed_repos.bot_gitcode_token
    → 全局 bot token），避免 chat agent 拿空 USER_GITCODE_TOKEN 调 GitCode API 报 401。
    """
    if user_token:
        cached = (conv or {}).get("user_gitcode_token", "") if conv else ""
        if conv_id and cached != user_token:
            try:
                update_conversation(conv_id, user_gitcode_token=user_token)
            except Exception as e:
                log.warning(f"Chat resolve token refresh failed: {e}")
        return user_token
    cached = (conv or {}).get("user_gitcode_token", "") if conv else ""
    if cached:
        return cached
    if repo_key:
        try:
            from src.user.db import get_gitcode_token
            repo_tok = get_gitcode_token(repo_key=repo_key) or ""
            if repo_tok:
                log.warning(f"Chat resolve token: user token empty, fallback to repo token (repo={repo_key})")
                return repo_tok
        except Exception as e:
            log.warning(f"Chat resolve token fallback failed: {e}")
    return ""


def _build_web_user_info(user: dict, acc: dict | None = None) -> str:
    """构建 Web Chat prompt 的用户基本信息（与飞书 user_info 格式一致）。

    user 为 FastAPI 当前用户 dict（username 即 GitCode 登录名）；
    acc 为 user_accounts 行（含 nickname/gitcode_id）。注入后使 Web Agent
    感知说话者身份（chat-prompt-overview.md §2.1，补 Web Chat 无 user_info 缺口）。
    """
    try:
        username = (user.get("username") or "").strip()
        if not username:
            return ""
        lines = ["## 用户基本信息"]
        lines.append(f"  GitCode login: {username}")
        nickname = (acc or {}).get("nickname") or ""
        if nickname and nickname != username:
            lines.append(f"  GitCode 昵称：{nickname}")
        return "\n".join(lines)
    except Exception:
        return ""


@router.post("/send")
async def send_message(body: ChatMessage, user: dict = Depends(get_current_user)):
    conv_id = body.conversation_id
    acc = user_db.get_user_account(user["username"]) or {}
    user_token = acc.get("gitcode_api_token", "")

    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
        if not conv.get("user_gitcode_token") and user_token:
            update_conversation(conv_id, user_gitcode_token=user_token)
            conv = get_conversation(conv_id)
    else:
        conv_id = create_conversation(user["id"], body.repo_key, user_gitcode_token=user_token)
        conv = get_conversation(conv_id)

    add_message(conv_id, "user", body.message)
    # 任务会话收到真实用户消息 → active→processing（task-status-processing-trigger-fix.md §3.1）
    mark_task_processing_by_conversation(conv_id)

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    skill_name = cfg.get("skill", "") or "chat"
    skill_id = cfg.get("skill_id", 0) or 0
    history = get_messages(conv_id, limit=20) if body.conversation_id else []
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    gitcode_token = _resolve_gitcode_token(conv, conv_id, user_token, repo_key)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    # 任务会话走独立 prompt（注入 Issue 上下文 + 场景: task）；一般会话走通用 prompt
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        prompt = build_task_chat_prompt(body.message, repo_key, history, conv, account=user["username"], runtime_id=cfg.get("runtime_id", 0), has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(body.message, repo_key, history, skill_name, skill_id,
                                           backend_name=backend_name, user_info=_build_web_user_info(user, acc),
                                           has_valid_session=has_valid_session)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""

    from src.config import log
    log.info(f"Chat send: backend={backend_name or '(none)'}, skill={skill_name}, has_token={bool(gitcode_token)}, user_msg={body.message[:100]}")

    # 无任何引擎（未配置本机直连/远端）→ 明确不可用，绝不隐式跑 opencode（engine-selection-design.md v2）
    if not backend_name:
        _warn = "⚠️ 无可用引擎（未配置本机直连或远端引擎）"
        add_message(conv_id, "agent", _warn)
        await _push_reply_to_feishu(conv, user, _warn)
        return {"reply": "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎", "conversation_id": conv_id}

    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
    kind = runtime_execution_kind(cfg.get("runtime_id") or 0)
    if kind != EXEC_MODE_DIRECT:
        # M2 D5：入队 + 前端轮询任务状态（打字机效果降级）
        return _enqueue_chat(user, conv_id, body.message, repo_key, skill_name, skill_id, gitcode_token)

    from src.runtime.executor import stream_agent
    loop = asyncio.get_event_loop()
    collected = []
    new_runtime_session_id = ""

    def run_agent():
        nonlocal new_runtime_session_id
        with _CHAT_AGENT_SEMAPHORE:
            for evt_type, data in stream_agent(
                prompt=prompt, cwd=_get_chat_cwd(user["id"], conv_id), session_id=runtime_session_id,
                backend_name=backend_name, user_id=user["id"],
                account=user["username"], user_token=gitcode_token, agent_name="Chat Agent",
                agent_label=f"[Agent:chat|user:{user['username']}]",
            ):
                # 日志输出
                if evt_type == "step_start":
                    log.info(f"  ▶ {data}")
                elif evt_type == "text":
                    log.info(f"  💬 {data[:120]}")
                elif evt_type == "reasoning":
                    log.info(f"  🤔 {data[:120]}")
                elif evt_type == "tool_use":
                    log.info(f"  🔧 {str(data)[:150]}")
                elif evt_type == "step_finish":
                    log.info(f"  ✅ {data}")
                elif evt_type == "done":
                    log.info(f"  ✅ Done: exit={data.get('exit_code')}, text_len={len(data.get('text',''))}")

                if evt_type == "text":
                    text = data.get("text", "") if isinstance(data, dict) else str(data)
                    if text:
                        collected.append(text)
                elif evt_type == "done":
                    if isinstance(data, dict):
                        nsid = data.get("oc_session_id", "")
                        if nsid:
                            new_runtime_session_id = nsid
                        if not collected:
                            done_text = data.get("text", "")
                            if done_text:
                                collected.append(done_text)
                elif evt_type in ("error"):
                    break
        return "".join(collected)

    reply = await loop.run_in_executor(None, run_agent)
    if not reply:
        reply = f"收到您的消息：「{body.message}」\n\n当前使用 {backend_name} 后端处理。"

    add_message(conv_id, "agent", reply)
    touch_conversation(conv_id)
    # web→飞书推送：该对话已被飞书接入时推送给用户（feishu-web-chat-unified-design.md §3.2）
    await _push_reply_to_feishu(conv, user, reply)
    # Save opencode session ID for continuity：恒回写新 session
    # （旧值可能已过期（opencode 静默 EOF），必须用本次成功的新 session 覆盖）
    if new_runtime_session_id:
        update_conversation(conv_id, runtime_session_id=new_runtime_session_id)
    return {"reply": reply, "conversation_id": conv_id}


@router.post("/send/stream")
async def send_message_stream(body: ChatMessage, user: dict = Depends(get_current_user)):
    conv_id = body.conversation_id
    acc = user_db.get_user_account(user["username"]) or {}
    user_token = acc.get("gitcode_api_token", "")

    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
        if not conv.get("user_gitcode_token") and user_token:
            update_conversation(conv_id, user_gitcode_token=user_token)
            conv = get_conversation(conv_id)
    else:
        conv_id = create_conversation(user["id"], body.repo_key, user_gitcode_token=user_token)
    conv = get_conversation(conv_id)
    _auto_title(conv, body.message)

    add_message(conv_id, "user", body.message)
    # 任务会话收到真实用户消息 → active→processing（task-status-processing-trigger-fix.md §3.1）
    mark_task_processing_by_conversation(conv_id)

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    skill_name = cfg.get("skill", "") or "chat"
    skill_id = cfg.get("skill_id", 0) or 0
    history = get_messages(conv_id, limit=20)
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    gitcode_token = _resolve_gitcode_token(conv, conv_id, user_token, repo_key)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    # 任务会话走独立 prompt（注入 Issue 上下文 + 场景: task）；一般会话走通用 prompt
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        prompt = build_task_chat_prompt(body.message, repo_key, history, conv, account=user["username"], runtime_id=cfg.get("runtime_id", 0), has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(body.message, repo_key, history, skill_name, skill_id,
                                           backend_name=backend_name, user_info=_build_web_user_info(user, acc),
                                           has_valid_session=has_valid_session)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""

    from src.config import log
    log.info(f"Chat send/stream: backend={backend_name or '(none)'}, skill={skill_name}, has_token={bool(gitcode_token)}, user_msg={body.message[:100]}")

    # 无引擎 → 明确不可用，绝不隐式跑 opencode（engine-selection-design.md v2）
    if not backend_name:
        _reply = "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎"
        add_message(conv_id, "agent", _reply)
        await _push_reply_to_feishu(conv, user, _reply)
        return StreamingResponse(
            iter([f"data: {json.dumps({'type': 'done', 'text': _reply, 'conversation_id': conv_id})}\n\n"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
    # chat 默认兜底本机直连（engine-selection-design.md §8.2）：未显式配置引擎时
    # 选健康本机引擎（credential_id=0），恒走 direct；不再按名现挑/建 opencode 默认。
    rid = cfg.get("runtime_id") or 0
    if not rid:
        try:
            from src.runtime.engine_select import resolve_local_engine
            rid = resolve_local_engine() or 0
        except Exception:
            rid = 0
    kind = runtime_execution_kind(rid)
    if kind != EXEC_MODE_DIRECT:
        # 本机直连行（credential_id=0）在 queue_remote 下无 claim 方：
        # 入队 = 永久 queued 黑洞（chat-task-blackhole-local-direct-2026-08.md）
        # rid=0 同样拦截（runtime_credential_id(0)=None → (None or 0)==0）
        from src.runtime.service import runtime_credential_id
        if (runtime_credential_id(rid) or 0) == 0 and backend_name:
            # 配置行残留本机直连/未绑定 → queue_remote 下按 backend 名解析在线远端引擎
            #（存量 sess-cfg-* 行被 onboarding 写死 1 的兜底，解析不到才真拦截）
            from src.session_manager import _resolve_chat_runtime_id
            _rid2 = _resolve_chat_runtime_id(backend_name) or 0
            if _rid2:
                rid = _rid2
                kind = runtime_execution_kind(rid)
        # 远端引擎离线/心跳超时 → 同样解析在线远端引擎兜底（与 handler_async 同源修复）
        if kind != EXEC_MODE_DIRECT:
            from src.runtime.service import _remote_runtime_ready
            if not _remote_runtime_ready(rid) and backend_name:
                from src.session_manager import _resolve_chat_runtime_id
                _rid3 = _resolve_chat_runtime_id(backend_name) or 0
                if _rid3 and _remote_runtime_ready(_rid3):
                    rid = _rid3
                    kind = runtime_execution_kind(rid)
        if (runtime_credential_id(rid) or 0) == 0:
            _reply = "⚠️ 未配置远端引擎，任务会话需先在 Agent 配置中绑定远端引擎"
            add_message(conv_id, "agent", _reply)
            await _push_reply_to_feishu(conv, user, _reply)
            return StreamingResponse(
                iter([f"data: {json.dumps({'type': 'done', 'text': _reply, 'conversation_id': conv_id})}\n\n"]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # M2 D5 入队 + 轮询：任务由远端 RuntimeAgent claim 执行，SSE 以终态结果返回
        import json as _json
        from src.runtime.service import enqueue_task, wait_task_terminal
        from src.api.session_store import get_or_create_agent_session

        session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name)
        runtime_id = rid  # 已由本机直连兜底解析（§8.2）；远端引擎才走队列

        async def event_stream():
            yield f"data: {json.dumps({'type': '思考中', 'text': '正在连接后端...', 'conversation_id': conv_id})}\n\n"
            final_text = ""
            new_sid = runtime_session_id
            if runtime_id:
                tid = enqueue_task(
                    agent_session_id=session.get("id") or "",
                    runtime_id=runtime_id,
                    event_seq=None, issue_id="",
                    repo_key=repo_key or f"chat:{user['id']}", agent_type="chat",
                    skill=skill_name or "chat",
                    skill_id=skill_id,
                    prompt=prompt, cwd=_get_chat_cwd(user["id"], conv_id),
                    runtime_session_id=runtime_session_id,
                    event_type="chat",
                    account=user.get("username") or "", user_id=user["id"],
                    repo_gitcode_token=gitcode_token,
                    env_extra={"USER_ID": str(user["id"]), "CONVERSATION_ID": conv_id},
                )
                log.info(f"Chat stream enqueued: task={tid} user={user['id']} conv={conv_id}")
                task = await wait_task_terminal(tid)
                final_text = task.get("result_text") or ""
                try:
                    usage = _json.loads(task.get("usage_json") or "{}") or {}
                    new_sid = usage.get("session_id") or runtime_session_id
                except Exception:
                    new_sid = runtime_session_id
                if final_text:
                    yield f"data: {json.dumps({'type': '生成中', 'text': final_text, 'conversation_id': conv_id})}\n\n"
            else:
                log.warning(f"Chat stream: no runtime for backend={backend_name}, fallback reply")

            if not final_text:
                final_text = f"收到您的消息：「{body.message}」\n\n当前使用 {backend_name} 后端处理。"
            add_message(conv_id, "agent", final_text)
            touch_conversation(conv_id)
            # web→飞书推送（feishu-web-chat-unified-design.md §3.2）
            await _push_reply_to_feishu(conv, user, final_text)
            if new_sid and not runtime_session_id:
                update_conversation(conv_id, runtime_session_id=new_sid)
            yield f"data: {json.dumps({'type': 'done', 'text': final_text, 'conversation_id': conv_id})}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    from src.runtime.executor import stream_agent

    async def event_stream():
        q = asyncio.Queue()
        loop = asyncio.get_event_loop()
        captured_session = ""

        def agent_worker():
            nonlocal captured_session
            if not _CHAT_AGENT_SEMAPHORE.acquire(blocking=True):
                log.warning(f"[Agent:chat|user:{user['username']}] semaphore NOT acquired (exhausted)")
                return
            try:
                for evt_type, data in stream_agent(
                    prompt=prompt, cwd=_get_chat_cwd(user["id"], conv_id), session_id=runtime_session_id,
                    backend_name=backend_name, user_id=user["id"],
                    account=user["username"], user_token=gitcode_token, agent_name="Chat Agent",
                    agent_label=f"[Agent:chat|user:{user['username']}]",
                ):
                    # 日志输出（对齐 master 分支 im/handler.py）
                    if evt_type == "step_start":
                        log.info(f"  ▶ {data}")
                    elif evt_type == "text":
                        log.info(f"  💬 {data[:120]}")
                    elif evt_type == "reasoning":
                        log.info(f"  🤔 {data[:120]}")
                    elif evt_type == "tool_use":
                        log.info(f"  🔧 {str(data)[:150]}")
                    elif evt_type == "step_finish":
                        log.info(f"  ✅ {data}")
                    elif evt_type == "done":
                        log.info(f"  ✅ Done: exit={data.get('exit_code')}, text_len={len(data.get('text',''))}")

                    if evt_type == "done" and isinstance(data, dict):
                        nsid = data.get("oc_session_id", "")
                        if nsid:
                            captured_session = nsid
                    q.put_nowait((evt_type, data))
            except Exception as e:
                import traceback as _tb
                log.warning(f"[Agent:chat|user:{user['username']}] agent_worker EXC: {e}")
                _tb.print_exc()
                q.put_nowait(("error", str(e)))
            finally:
                log.info(f"[Agent:chat|user:{user['username']}] agent_worker finished")
                q.put_nowait(None)
                _CHAT_AGENT_SEMAPHORE.release()

        loop.run_in_executor(None, agent_worker)

        yield f"data: {json.dumps({'type': '思考中', 'text': '正在连接后端...', 'conversation_id': conv_id})}\n\n"

        final_text = ""
        while True:
            # 看门狗：Agent 长时间无输出（如 opencode 卡住/超慢）时主动报错，
            # 避免 SSE 流一直挂着直到连接被代理/浏览器掐断 → 前端落到无意义兜底
            try:
                item = await asyncio.wait_for(q.get(), timeout=60)
            except asyncio.TimeoutError:
                if not final_text:
                    yield f"data: {json.dumps({'type': 'error', 'text': '⚠️ Agent 长时间无响应，未收到回复，请稍后重试。', 'conversation_id': conv_id})}\n\n"
                return
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                text = data.get("text", "") if isinstance(data, dict) else str(data)
                if text:
                    final_text += text
                    yield f"data: {json.dumps({'type': '生成中', 'text': final_text, 'conversation_id': conv_id})}\n\n"
            elif evt_type == "error":
                err_msg = str(data)
                yield f"data: {json.dumps({'type': 'error', 'text': f'后端错误: {err_msg}', 'conversation_id': conv_id})}\n\n"
                return
            elif evt_type == "done":
                if not final_text:
                    done_text = data.get("text", "") if isinstance(data, dict) else ""
                    if done_text:
                        final_text = done_text
                break

        if not final_text:
            final_text = f"收到您的消息：「{body.message}」\n\n当前使用 {backend_name} 后端处理。"

        add_message(conv_id, "agent", final_text)
        touch_conversation(conv_id)
        # web→飞书推送（feishu-web-chat-unified-design.md §3.2）
        await _push_reply_to_feishu(conv, user, final_text)
        # Save opencode session ID for continuity：恒回写新 session（覆盖可能过期的旧值）
        if captured_session:
            update_conversation(conv_id, runtime_session_id=captured_session)
        yield f"data: {json.dumps({'type': 'done', 'text': final_text, 'conversation_id': conv_id})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


def _enqueue_chat(user, conv_id, message, repo_key, skill_name, skill_id, gitcode_token):
    """M2 D5：chat 入队 + 轮询任务状态（打字机效果降级，见 implementation-plan §三）。

    返回 {task_id, reply(占位), conversation_id}；前端轮询 GET /api/runtime/tasks/{id}/status。
    """
    from src.runtime.service import enqueue_task, get_execution_mode
    from src.api.session_store import get_or_create_agent_session
    from src.config import log as _log
    import os as _os
    import src.user.db as _user_db
    acc = _user_db.get_user_account(user.get("username") or "") or {}

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    # chat 会话：user_id + repo_key 绑定（chat_agent_session 是 chat 专用；此处用 devops_agent_session 保证 D9 串行）
    session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name)
    # 无引擎 → 明确不可用（engine-selection-design.md v2），不隐式跑 opencode
    if not backend_name:
        return {"reply": "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎", "conversation_id": conv_id}
    # 优先用用户配置的 runtime_id（chat_agent_session），未配置按本机直连兜底（engine-selection-design.md §8.2）
    runtime_id = cfg.get("runtime_id") or 0
    if not runtime_id:
        try:
            from src.runtime.engine_select import resolve_local_engine
            runtime_id = resolve_local_engine() or 0
        except Exception:
            runtime_id = 0
    if not runtime_id:
        return {"reply": "⚠️ 无可用 runtime，请先在 Agent Runtimes 页刷新", "conversation_id": conv_id}
    # 本机直连（credential_id=0）无 claim 方：队列任务端点不能投递到本机，需走 direct 会话
    from src.runtime.service import runtime_credential_id
    if (runtime_credential_id(runtime_id) or 0) == 0 and backend_name:
        # 配置行残留本机直连/未绑定 → 按 backend 名解析在线远端引擎（与 send/stream 同款兜底）
        from src.session_manager import _resolve_chat_runtime_id
        _rid2 = _resolve_chat_runtime_id(backend_name) or 0
        if _rid2:
            runtime_id = _rid2
    if (runtime_credential_id(runtime_id) or 0) == 0:
        return {"reply": "⚠️ 未配置远端引擎，任务会话需先在 Agent 配置中绑定远端引擎", "conversation_id": conv_id}

    cwd = _get_chat_cwd(user["id"], conv_id)
    _os.makedirs(cwd, exist_ok=True)
    # 任务会话走独立 prompt（注入 Issue 上下文 + 场景: task + 任务描述 + 历史）；
    # 一般会话走通用 prompt（queue 模式也带最近历史，与 direct 一致）
    #（见 task-conversation-prompt-align-design.md §3.2）
    conv = get_conversation(conv_id) if conv_id else None
    history = get_messages(conv_id, limit=10) if conv_id else []
    _rsid = session.get("runtime_session_id") or ""
    has_valid_session = bool(_rsid.startswith("ses_"))
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        prompt = build_task_chat_prompt(message, repo_key, history, conv, account=user.get("username") or "", runtime_id=runtime_id, has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(message, repo_key, history, skill_name, skill_id,
                                           user_info=_build_web_user_info(user, acc), has_valid_session=has_valid_session)
    tid = enqueue_task(
        agent_session_id=session.get("id") or "",
        runtime_id=runtime_id,
        event_seq=None, issue_id="",
        repo_key=f"chat:{user['id']}", agent_type="chat", skill=skill_name or "chat",
        skill_id=skill_id,
        prompt=prompt, cwd=cwd,
        runtime_session_id=session.get("runtime_session_id") or "",
        event_type="chat", account=user.get("username") or "", user_id=user["id"],
        repo_gitcode_token=gitcode_token,
        env_extra={"USER_ID": str(user["id"]), "CONVERSATION_ID": conv_id},
    )
    _log.info(f"Chat enqueued: task={tid} user={user['id']} conv={conv_id} mode={get_execution_mode()}")
    return {"task_id": tid, "reply": "", "conversation_id": conv_id, "queued": True}


# ====== Feedback ======

@router.post("/feedback")
async def submit_feedback(body: FeedbackBody, user: dict = Depends(get_current_user)):
    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT repo_key, event_type, title, payload FROM events WHERE seq=?", (body.seq,))
            row = c.fetchone()
        except Exception:
            row = None

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件未找到"})

    repo_key = row[0]
    a2a_event = {
        "type": "user_feedback",
        "source": "chat_agent",
        "target": "vod",
        "payload": {
            "source_seq": body.seq,
            "feedback_type": body.feedback_type,
            "comment": body.comment,
            "user": user["username"],
        }
    }

    from src.user.db import get_bot_token_by_repo
    from src.repo_key import split_owner_repo
    _owner, _repo = split_owner_repo(repo_key)
    bot_token = get_bot_token_by_repo(_owner, _repo) if _owner and _repo else ""

    return {
        "message": "反馈已提交（⚠️ 已废弃：请改用 Event 对话 POST /api/events/{seq}/chat）",
        "deprecated": True,
        "a2a_event": a2a_event,
        "bot_token_available": bool(bot_token),
    }


# ====== Config ======

@router.get("/config")
async def get_chat_config(user: dict = Depends(get_current_user), repo: str = ""):
    cfg = _get_chat_config(user["id"])
    # Chat 后端可选面：本机直连（默认兜底，engine-selection-design.md §8.2）+ 个人引擎（scope=user，显式选择）
    from src.runtime.instances import instance_name
    from src.user.db import is_known_engine_name
    from src.session_manager import db, db_lock
    backends = []
    with db_lock:
        c = db.cursor()
        # 本机直连引擎（credential_id=0，已知类型且已装且健康——与执行 resolve_local_engine 口径一致，
        # local-direct-health-probe-design.md 方案 C，避免"页面有、执行无"）
        try:
            c.execute("SELECT id, name, version FROM agent_runtimes WHERE credential_id=0 AND installed=1 AND healthy=1 ORDER BY id")
            for r in c.fetchall():
                _rid, ename, ver = r
                if not is_known_engine_name(ename or ""):
                    continue
                backends.append({"name": ename, "display_name": f"{ename} v{ver or ''}（本机直连）", "scope": "local", "owner": "", "runtime_id": _rid})
        except Exception:
            pass
        # 个人引擎（scope=user）
        try:
            c.execute("""
                SELECT ar.id, ar.name, ar.version, rc.device_name, rc.owner_name, rc.id
                FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
                WHERE rc.scope = 'user'
            """)
            for r in c.fetchall():
                _rid, ename, ver, dev, owner, cid = r
                ins = instance_name(ename, dev or "", cid or 0)
                backends.append({"name": ins, "display_name": f"{ins} · {ename} v{ver or ''}", "scope": "user", "owner": owner or "", "runtime_id": _rid})
        except Exception:
            pass
    # Load chat skills from skill table（官方 + 个人 + 当前仓库，agent_type='chat'）
    chat_skills = []
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo) if repo else ""
    with db_lock:
        c = db.cursor()
        try:
            if repo_key:
                c.execute(
                    "SELECT skill_name, display_name, description, skill_path, scope FROM skill WHERE is_active=1 AND agent_type='chat' AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?)) ORDER BY scope, skill_name",
                    (str(user["id"]), repo_key)
                )
            else:
                c.execute(
                    "SELECT skill_name, display_name, description, skill_path, scope FROM skill WHERE is_active=1 AND agent_type='chat' AND (scope='official' OR (scope='user' AND owner_id=?)) ORDER BY scope, skill_name",
                    (str(user["id"]),)
                )
            chat_skills = [{"name": r[0], "display_name": r[1] or r[0], "description": r[2], "skill_path": r[3] or r[0], "scope": r[4] or ""} for r in c.fetchall()]
        except Exception:
            pass

    # 从用户级 chat_agent_session 读取 Chat Agent 凭证状态（每用户独立）
    # 不再查 global_im_bot_config（该表仅作类型注册，不承载用户 IM 配置）
    im_app_id = cfg.get("im_app_id", "") or ""
    im_app_secret = cfg.get("im_app_secret", "") or ""

    # 当前配置技能的 scope（用于前端下拉回显同名技能）
    skill_scope = cfg.get("skill_scope", "")
    if not skill_scope and cfg.get("skill_id"):
        try:
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT scope FROM skill WHERE id=? AND is_active=1", (cfg["skill_id"],)).fetchone()
                skill_scope = row[0] if row else ""
        except Exception:
            pass

    return {
        "backend": {
            "type": cfg.get("backend", ""),
        },
        "skill": cfg.get("skill", ""),
        "skill_id": cfg.get("skill_id", 0) or 0,
        "skill_scope": skill_scope,
        "_backends": backends,
        "_skills": chat_skills,
        "im": {
            "feishu": {
                "connected": bool(im_app_id),
                "app_id": im_app_id,
                "app_secret_exists": bool(im_app_secret),
            },
            "wecom": {"connected": False, "app_id": ""},
            "dingtalk": {"connected": False, "app_id": ""},
        }
    }


@router.put("/config")
async def update_chat_config(body: dict, user: dict = Depends(get_current_user), repo: str = ""):
    im = body.get("im", {})
    feishu = im.get("feishu", {})
    kwargs = {}
    if feishu.get("app_id"):
        app_id = feishu["app_id"]
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # 强约束：一个 app_id 全局唯一。类型注册表校验 VOD 占用 + 其他用户占用
            c.execute("SELECT bot_type FROM global_im_bot_config WHERE bot_app_id=? AND is_active=1", (app_id,))
            row = c.fetchone()
            if row and row[0] == "vod_bot":
                raise HTTPException(status_code=400, detail={"code": 400, "message": "该凭证已被 VOD Agent 使用，不能同时绑定 Chat Agent 和 VOD Agent"})
            # 其他用户是否已占用（app_id 全局唯一）
            c.execute("SELECT user_id FROM chat_agent_session WHERE im_app_id=? AND user_id>0 AND user_id!=? AND im_app_id!='' LIMIT 1", (app_id, user["id"]))
            other = c.fetchone()
            if other:
                raise HTTPException(status_code=400, detail={"code": 400, "message": "该凭证已被其他用户使用，不能重复绑定"})

            old_cfg = _get_chat_config(user["id"])
            old_app_id = old_cfg.get("im_app_id", "")
            if old_app_id and old_app_id != app_id:
                # 更换 app_id：检查旧 app_id 是否还被其他用户/其他记录引用
                c.execute("SELECT COUNT(*) FROM chat_agent_session WHERE im_app_id=? AND user_id!=? AND im_app_id!=''", (old_app_id, user["id"]))
                other_refs = c.fetchone()[0]
                if other_refs == 0:
                    c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=? AND bot_type='chat_bot'", (old_app_id,))
                c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (old_app_id,))
                log.info(f"Chat IM: replaced old app_id={old_app_id[:30]} with new app_id={app_id[:30]}")

            app_secret = feishu.get("app_secret", "")
            if app_secret and app_secret != "********":
                c.execute(
                    "INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?,?)",
                    (app_id, "chat_bot")
                )
                db.commit()
                kwargs["im_app_id"] = app_id
                kwargs["im_app_secret"] = app_secret
                log.info(f"Chat IM: saved im config for user={user['id']} app_id={app_id[:30]}")
            else:
                kwargs["im_app_id"] = app_id
    else:
        old_cfg = _get_chat_config(user["id"])
        old_app_id = old_cfg.get("im_app_id", "")
        kwargs["im_app_id"] = ""
        kwargs["im_app_secret"] = ""
    if feishu.get("app_secret") and feishu["app_secret"] != "********":
        kwargs["im_app_secret"] = feishu["app_secret"]
    if body.get("backend"):
        kwargs["backend"] = body["backend"]
    if body.get("runtime_id") is not None:
        kwargs["runtime_id"] = body["runtime_id"]
    if body.get("skill_id") is not None:
        # 新方式：前端直传 skill_id
        skill_id = body["skill_id"]
        skill_name = ""
        skill_scope = ""
        if skill_id:
            from src.user.db import get_skill_by_id
            _sk = get_skill_by_id(skill_id)
            if _sk:
                skill_name = _sk.get("skill_name") or ""
                skill_scope = _sk.get("scope") or ""
            else:
                skill_id = 0
        kwargs["skill"] = skill_name
        kwargs["skill_id"] = skill_id
        kwargs["skill_scope"] = skill_scope
    elif body.get("skill") is not None:
        # 旧方式兼容：按名称+scope 查 id
        kwargs["skill"] = body["skill"]
        skill_name = body["skill"]
        skill_scope = body.get("skill_scope", "")
        skill_id = 0
        if skill_name:
            from src.session_manager import db as _db, db_lock as _db_lock
            def _find_skill(scope, owner_id=""):
                """精确按 scope(+owner) 查 skill_id，不走 official 优先"""
                with _db_lock:
                    c = _db.cursor()
                    if owner_id:
                        row = c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? AND is_active=1 LIMIT 1",
                                        (skill_name, scope, owner_id)).fetchone()
                    else:
                        row = c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND is_active=1 LIMIT 1",
                                        (skill_name, scope)).fetchone()
                    return row[0] if row else 0

            if skill_scope == "repo" and repo:
                from src.repo_key import to_repo_key
                skill_id = _find_skill("repo", to_repo_key(repo))
            elif skill_scope == "user":
                skill_id = _find_skill("user", str(user["id"]))
            elif skill_scope == "official":
                skill_id = _find_skill("official")
            else:
                if repo:
                    from src.repo_key import to_repo_key
                    skill_id = _find_skill("repo", to_repo_key(repo))
                if not skill_id:
                    skill_id = _find_skill("user", str(user["id"]))
                if not skill_id:
                    skill_id = _find_skill("official")
        kwargs["skill_id"] = skill_id
        kwargs["skill_scope"] = skill_scope
    _set_chat_config(user["id"], **kwargs)

    # 手动配置换 app：对账 unified_user_mapping（feishu-app-scoped-openid-fix.md §2.3）
    # 新旧 im_app_id 不同 → 刷新 app_id 并清空 open_id/feishu_name，要求用户重新 p2p 确认
    _new_app = kwargs.get("im_app_id") or ""
    if _new_app and old_app_id and old_app_id != _new_app:
        try:
            from src.session_manager import update_unified_user_info
            update_unified_user_info(user.get("username", ""), open_id="", app_id=_new_app)
            log.info(f"Chat IM: app changed {old_app_id[:30]} -> {_new_app[:30]}, reconciled unified_user_mapping (open_id cleared)")
        except Exception as e:
            log.warning(f"Chat IM: reconcile unified_user_mapping failed: {e}")

    # Chat IM：通过 BotManager 动态增删 Bot
    try:
        from src.api.main import bot_manager
        if bot_manager and bot_manager.api:
            if feishu.get("app_id") and feishu.get("app_id") == app_id:
                new_secret = kwargs.get("im_app_secret", "")
                if new_secret and new_secret != "********":
                    if old_app_id and old_app_id != app_id:
                        await bot_manager.remove_bot(old_app_id)
                        log.info(f"Chat IM: removed old bot {old_app_id[:30]} from BotManager")
                    if app_id in bot_manager._bots:
                        await bot_manager.remove_bot(app_id)
                    await bot_manager.add_bot(app_id, new_secret, bot_type="chat_bot")
                    log.info(f"Chat IM: added bot {app_id[:30]} to BotManager")
            elif not feishu.get("app_id") and old_app_id:
                await bot_manager.remove_bot(old_app_id)
                log.info(f"Chat IM: removed bot {old_app_id[:30]} from BotManager")
    except Exception as e:
        log.warning(f"Chat BotManager update failed: {e}")

    return {"message": "配置已更新"}