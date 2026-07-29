"""Chat Agent 路由：对话、消息、配置、反馈"""
import json, asyncio, uuid, os
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.user import db as user_db
from src.session_manager import (
    create_conversation, get_user_conversations, get_conversation,
    add_message, get_messages, touch_conversation, update_conversation,
    get_chat_config as _get_chat_config,
    set_chat_config as _set_chat_config,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])

CHAT_SYSTEM_PROMPT = """You are a helpful DevOps assistant integrated with GitCode.
Answer questions about repositories, issues, pull requests, and DevOps workflows.
Keep responses concise and actionable."""


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


# ====== Messages ======

@router.get("/messages/{conv_id}")
async def list_messages(conv_id: str, before: int = Query(0, ge=0), user: dict = Depends(get_current_user)):
    conv = get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "对话未找到"})
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


@router.post("/send")
async def send_message(body: ChatMessage, user: dict = Depends(get_current_user)):
    conv_id = body.conversation_id
    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
    else:
        conv_id = create_conversation(user["id"], body.repo_key)
    conv = get_conversation(conv_id)
    _auto_title(conv, body.message)

    add_message(conv_id, "user", body.message)

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "opencode")
    history = get_messages(conv_id, limit=20) if body.conversation_id else []
    prompt = _build_chat_prompt(body.message, body.repo_key, history)
    session_id = conv.get("session_id", "") if conv else ""

    from src.agent.executor import stream_agent
    loop = asyncio.get_event_loop()
    collected = []

    def run_agent():
        for evt_type, data in stream_agent(
            prompt=prompt, cwd=os.getcwd(), session_id=session_id,
            backend_name=backend_name, user_id=user["id"],
        ):
            if evt_type == "text":
                text = data.get("text", "") if isinstance(data, dict) else str(data)
                if text:
                    collected.append(text)
            elif evt_type == "done":
                if not collected:
                    done_text = data.get("text", "") if isinstance(data, dict) else ""
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
    return {"reply": reply, "conversation_id": conv_id}


@router.post("/send/stream")
async def send_message_stream(body: ChatMessage, user: dict = Depends(get_current_user)):
    conv_id = body.conversation_id
    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
    else:
        conv_id = create_conversation(user["id"], body.repo_key)
    conv = get_conversation(conv_id)
    _auto_title(conv, body.message)

    add_message(conv_id, "user", body.message)

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "opencode")
    history = get_messages(conv_id, limit=20)
    prompt = _build_chat_prompt(body.message, body.repo_key, history)
    session_id = conv.get("session_id", "") if conv else ""

    from src.agent.executor import stream_agent

    async def event_stream():
        q = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def agent_worker():
            try:
                for evt_type, data in stream_agent(
                    prompt=prompt, cwd=os.getcwd(), session_id=session_id,
                    backend_name=backend_name, user_id=user["id"],
                ):
                    q.put_nowait((evt_type, data))
            except Exception as e:
                q.put_nowait(("error", str(e)))
            finally:
                q.put_nowait(None)

        loop.run_in_executor(None, agent_worker)

        yield f"data: {json.dumps({'type': '思考中', 'text': '正在连接后端...', 'conversation_id': conv_id})}\n\n"

        final_text = ""
        while True:
            item = await q.get()
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
        yield f"data: {json.dumps({'type': 'done', 'text': final_text, 'conversation_id': conv_id})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _build_chat_prompt(message: str, repo_key: str, history: list) -> str:
    parts = [CHAT_SYSTEM_PROMPT]
    if repo_key:
        parts.append(f"\nCurrent repository: {repo_key}")
    # Include recent chat history for context
    chat_history = [m for m in history if m.get("role") in ("user", "agent")][-10:]
    if chat_history:
        parts.append("\nConversation history:")
        for m in chat_history:
            role = "User" if m["role"] == "user" else "Assistant"
            parts.append(f"{role}: {m['content'][:200]}")
    parts.append(f"\nUser: {message}\nAssistant:")
    return "\n".join(parts)


# ====== Feedback ======


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

    from src.api.deps import get_bot_token
    bot_token = get_bot_token("default")

    return {
        "message": "反馈已提交给 VOD Agent",
        "a2a_event": a2a_event,
        "bot_token_available": bool(bot_token),
    }


# ====== Config ======

@router.get("/config")
async def get_chat_config(user: dict = Depends(get_current_user)):
    cfg = _get_chat_config(user["id"])
    from src.user.db import get_backend_agents as _get_cached_backends
    backends = _get_cached_backends()
    return {
        "backend": {
            "type": cfg.get("backend", "opencode"),
        },
        "_backends": backends,
        "im": {
            "feishu": {
                "connected": bool(cfg.get("feishu_app_id")),
                "app_id": cfg.get("feishu_app_id", ""),
                "app_secret_exists": bool(cfg.get("feishu_app_secret")),
            },
            "wecom": {"connected": False, "app_id": ""},
            "dingtalk": {"connected": False, "app_id": ""},
        }
    }


@router.put("/config")
async def update_chat_config(body: dict, user: dict = Depends(get_current_user)):
    im = body.get("im", {})
    feishu = im.get("feishu", {})
    kwargs = {}
    if feishu.get("app_id"):
        kwargs["feishu_app_id"] = feishu["app_id"]
    if feishu.get("app_secret"):
        kwargs["feishu_app_secret"] = feishu["app_secret"]
    if body.get("backend"):
        kwargs["backend"] = body["backend"]
    _set_chat_config(user["id"], **kwargs)
    return {"message": "配置已更新"}