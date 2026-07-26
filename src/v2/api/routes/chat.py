"""Chat Agent 路由：对话、配置"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.v2.api.deps import get_current_user

router = APIRouter(prefix="/api/v2/chat", tags=["chat"])


class ChatMessage(BaseModel):
    message: str


@router.post("/send")
async def send_message(body: ChatMessage, user: dict = Depends(get_current_user)):
    """发送消息给 Chat Agent"""
    reply = f"收到您的消息：「{body.message}」\n\n当前 VOD Agent 有 47 个信号待处理, Issue Agent 有 3 个活跃 Issue。需要我帮您做什么？"
    return {"reply": reply, "conversation_id": f"conv-{user['id']}-001"}


@router.get("/config")
async def get_chat_config(user: dict = Depends(get_current_user)):
    """获取 Chat Agent 配置"""
    return {
        "backend": {
            "type": "Default",
            "url": "localhost:8080/chat",
            "api_key_exists": True,
        },
        "im": {
            "feishu": {
                "connected": True,
                "app_id": "cli_a8d3f2e1b4c5",
                "app_secret_exists": True,
            },
            "wecom": {"connected": False, "app_id": ""},
            "dingtalk": {"connected": False, "app_id": ""},
        }
    }


@router.put("/config")
async def update_chat_config(body: dict, user: dict = Depends(get_current_user)):
    """更新 Chat Agent 配置（Backend、IM）"""
    return {"message": "配置已更新"}
