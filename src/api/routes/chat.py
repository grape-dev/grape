"""Chat Agent 路由：对话、消息、配置、反馈"""
import json, asyncio, uuid
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.user import db as user_db
from src.session_manager import (
    create_conversation, get_user_conversations, get_conversation,
    add_message, get_messages, touch_conversation, update_conversation,
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
async def list_conversations(user: dict = Depends(get_current_user)):
    convs = get_user_conversations(user["id"])
    return {"conversations": convs}


@router.post("/conversations")
async def new_conversation(user: dict = Depends(get_current_user)):
    cid = create_conversation(user["id"])
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

@router.post("/send")
async def send_message(body: ChatMessage, user: dict = Depends(get_current_user)):
    # 自动创建或复用对话
    conv_id = body.conversation_id
    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
    else:
        conv_id = create_conversation(user["id"], body.repo_key)

    # 保存用户消息
    add_message(conv_id, "user", body.message)

    # mock 回复
    reply = (f"收到您的消息：「{body.message}」\n\n"
             f"当前使用您的个人 Token 进行操作。"
             f"需要我帮您做什么？")
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

    add_message(conv_id, "user", body.message)

    async def event_stream():
        yield f"data: {json.dumps({'type': 'status', 'text': '思考中...', 'conversation_id': conv_id})}\n\n"
        await asyncio.sleep(0.3)

        steps = [
            ("分析中", "正在分析您的请求..."),
            ("处理", f"理解问题: {body.message[:50]}..."),
            ("生成", "正在生成回复..."),
            ("完成", f"收到您的消息：「{body.message}」\n\n当前使用您的个人 Token 进行操作。需要我帮您做什么？"),
        ]

        for s_type, s_text in steps:
            await asyncio.sleep(0.2)
            yield f"data: {json.dumps({'type': s_type, 'text': s_text, 'conversation_id': conv_id})}\n\n"

        add_message(conv_id, "agent", steps[-1][1])
        touch_conversation(conv_id)
        yield f"data: {json.dumps({'type': 'done', 'text': steps[-1][1], 'conversation_id': conv_id})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


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
    acc = user_db.get_user_account(user["username"]) or {}
    from src.user.db import get_backend_agents as _get_cached_backends
    backends = _get_cached_backends()
    return {
        "backend": {
            "type": acc.get("chat_backend_type", "Default"),
            "api_key_exists": bool(acc.get("gitcode_api_token")),
        },
        "_backends": backends,
        "im": {
            "feishu": {
                "connected": bool(acc.get("feishu_app_id")),
                "app_id": acc.get("feishu_app_id", ""),
                "app_secret_exists": bool(acc.get("feishu_app_secret")),
            },
            "wecom": {"connected": False, "app_id": ""},
            "dingtalk": {"connected": False, "app_id": ""},
        }
    }


@router.put("/config")
async def update_chat_config(body: dict, user: dict = Depends(get_current_user)):
    acc = user_db.get_user_account(user["username"]) or {}
    im = body.get("im", {})
    feishu = im.get("feishu", {})
    if feishu.get("app_id"):
        acc["feishu_app_id"] = feishu["app_id"]
    if feishu.get("app_secret"):
        acc["feishu_app_secret"] = feishu["app_secret"]
    user_db.save_user_account(user["username"], acc)
    return {"message": "配置已更新"}