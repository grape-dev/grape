"""飞书卡片按钮点击回调（Card Action Callback） — HTTP 兜底

WS 断连期间，飞书会向卡片回调地址 POST card.action.trigger 事件。
本端点作为 WS 的 fallback，确保历史消息的按钮始终可点击。

配置方式（飞书开发者后台 → 应用 → 卡片 → 卡片请求地址）：
  https://<你的域名>/api/feishu/card-action/<app_id>

注意：
  - WS 正常时，按钮事件走 WS 通道（实时推送）
  - WS 断连时，飞书自动走 HTTP 回调（本端点）
  - 两个通道同时存在，通过 _seen_msg_ids 去重
"""
import json
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.config import log

router = APIRouter(prefix="/api/feishu/card-action", tags=["feishu-card"])


@router.post("/{app_id}")
async def handle_card_action(app_id: str, request: Request):
    """接收飞书卡片按钮点击回调（HTTP fallback for WS card.action.trigger）"""
    try:
        from src.api.main import bot_manager
        if bot_manager is None:
            return JSONResponse(
                status_code=503,
                content={"code": 503, "msg": "bot_manager not ready"},
            )

        body = await request.json()
        log.info(f"[card-callback] app_id={app_id} body_keys={list(body.keys())}")

        # 找 BotContext
        ctx = bot_manager._bots.get(app_id)
        if not ctx:
            log.warning(f"[card-callback] bot not found: app_id={app_id}")
            return JSONResponse(
                status_code=404,
                content={"code": 404, "msg": "bot not found"},
            )

        # 用 _parse_card_action 解析（兼容直接 POST 和 WS 两种格式）
        from src.im.feishu_async import _parse_card_action
        parsed = _parse_card_action(body)

        if not parsed or not parsed.get("text"):
            log.warning(f"[card-callback] parse failed: {json.dumps(body, ensure_ascii=False)[:300]}")
            return JSONResponse(
                status_code=200,
                content={
                    "code": 0,
                    "msg": "ok",
                    "data": {},
                },
            )

        msg_id = parsed.get("message_id", "")
        if msg_id and msg_id in ctx._seen_msg_ids:
            log.info(f"[card-callback] dedup: msg_id={msg_id}")
            return JSONResponse(
                status_code=200,
                content={
                    "code": 0,
                    "msg": "ok",
                    "data": {},
                },
            )

        if msg_id:
            ctx._seen_msg_ids.add(msg_id)

        # 获取消息处理器
        from src.api.main import bot_manager as bm  # noqa: F811
        on_message = bm._on_message if hasattr(bm, '_on_message') else None
        if on_message is None:
            log.warning("[card-callback] no message handler registered")
            return JSONResponse(
                status_code=200,
                content={
                    "code": 0,
                    "msg": "ok",
                    "data": {},
                },
            )

        # 异步处理（非阻塞回调响应）
        asyncio.create_task(
            _safe_handle_card(ctx, on_message, parsed),
            name=f"card-{ctx.name}",
        )

        # 返回飞书要求的卡片回调格式
        return JSONResponse(
            status_code=200,
            content={
                "code": 0,
                "msg": "ok",
                "data": {},
            },
        )

    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "invalid json"},
        )
    except Exception as e:
        log.warning(f"[card-callback] error: {e}")
        return JSONResponse(
            status_code=200,
            content={
                "code": 0,
                "msg": "ok",
                "data": {},
            },
        )


async def _safe_handle_card(ctx, on_message, parsed):
    """安全处理卡片回调消息 — 防止处理异常影响回调响应"""
    try:
        text = parsed.get("text", "")[:50]
        log.info(f"[card-callback] handling: cmd={text}")
        await on_message(ctx, parsed)
    except Exception as e:
        log.warning(f"[card-callback] handle error: {e}")