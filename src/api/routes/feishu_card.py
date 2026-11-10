"""飞书卡片按钮点击回调（HTTP 兜底）—— 已随旧 IM 传输层下线（A2 M2）。

卡片点击现在只走 WS 的 `card.action.trigger`（FeishuChannel）；WS 断线由引擎按退避重连，
不再需要本 HTTP 兜底。保留路由是为了让飞书回调不再重试：统一返回 code=0。

如需恢复 HTTP 兜底，应基于新引擎（grape_t_channel_installation + FeishuChannel）重做，而不是旧 BotManager。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.config import log

router = APIRouter(prefix="/api/feishu/card-action", tags=["feishu-card"])


@router.post("/{app_id}")
async def handle_card_action(app_id: str, request: Request):
    """旧 HTTP 兜底：直接 ack（不处理），点击由 WS 通道处理。"""
    log.info(f"[card-callback] retired (legacy BotManager removed) app_id={app_id}")
    return JSONResponse(status_code=200, content={"code": 0, "msg": "ok", "data": {}})
