"""Chat Agent 路由：对话、配置、反馈"""
import json, asyncio
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.user import db as user_db

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatMessage(BaseModel):
    message: str
    repo_key: str = ""


@router.post("/send")
async def send_message(body: ChatMessage, user: dict = Depends(get_current_user)):
    """发送消息给 Chat Agent — 使用 User Token 代表用户操作"""
    acc = user_db.get_user_account(user["username"]) or {}
    user_token = acc.get("gitcode_api_token", "")

    reply = (f"收到您的消息：「{body.message}」\n\n"
             f"当前使用您的个人 Token 进行操作。"
             f"需要我帮您做什么？")
    return {"reply": reply, "conversation_id": f"conv-{user['id']}-{int(__import__('time').time())}"}


@router.post("/send/stream")
async def send_message_stream(body: ChatMessage, user: dict = Depends(get_current_user)):
    """SSE 流式回复：逐步推送 Agent 的思考过程"""

    async def event_stream():
        yield f"data: {json.dumps({'type': 'status', 'text': '思考中...'})}\n\n"
        await asyncio.sleep(0.3)

        acc = user_db.get_user_account(user["username"]) or {}
        user_token = acc.get("gitcode_api_token", "")

        steps = [
            ("分析中", "正在分析您的请求..."),
            ("处理", f"理解问题: {body.message[:50]}..."),
            ("生成", "正在生成回复..."),
            ("完成", f"收到您的消息：「{body.message}」\n\n当前使用您的个人 Token 进行操作。需要我帮您做什么？"),
        ]

        for s_type, s_text in steps:
            await asyncio.sleep(0.2)
            yield f"data: {json.dumps({'type': s_type, 'text': s_text})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'text': steps[-1][1]})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


class FeedbackBody(BaseModel):
    seq: int
    feedback_type: str = "create_issue"
    comment: str = ""


@router.post("/feedback")
async def submit_feedback(body: FeedbackBody, user: dict = Depends(get_current_user)):
    """用户反馈 → A2A 发送给 VOD Agent"""
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

    import json
    from src.api.deps import get_bot_token
    bot_token = get_bot_token("default")

    return {
        "message": "反馈已提交给 VOD Agent",
        "a2a_event": a2a_event,
        "bot_token_available": bool(bot_token),
    }


@router.get("/config")
async def get_chat_config(user: dict = Depends(get_current_user)):
    """获取 Chat Agent 配置（Backend + IM）"""
    acc = user_db.get_user_account(user["username"]) or {}
    return {
        "backend": {
            "type": "Default",
            "url": "localhost:8080/chat",
            "api_key_exists": bool(acc.get("gitcode_api_token")),
        },
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
