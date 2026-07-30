"""Feishu Bot — 独立的飞书机器人 WS 连接管理器（不依赖 lark_oapi WS 客户端）"""
import json, asyncio, time, uuid
from urllib.request import Request, urlopen
from urllib.error import URLError
from src.config import log

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuBot:
    """单个飞书机器人的完整生命周期管理"""

    def __init__(self, app_id: str, app_secret: str, name: str, on_message):
        self.app_id = app_id
        self.app_secret = app_secret
        self.name = name
        self.on_message = on_message  # async callable(message_dict)
        self.token = ""
        self.ws = None
        self._running = False

    async def _get_tenant_token(self) -> str:
        """获取 tenant_access_token"""
        url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
        try:
            resp = urlopen(Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=10)
            data = json.loads(resp.read())
            token = data.get("tenant_access_token", "")
            if token:
                return token
        except Exception as e:
            log.warning(f"[{self.name}] token error: {e}")
        return ""

    async def _get_ws_endpoint(self) -> str:
        """获取飞书 WebSocket 动态端点 URL（不在 open-apis 下）"""
        url = f"https://open.feishu.cn/callback/ws/endpoint"
        payload = json.dumps({"AppID": self.app_id, "AppSecret": self.app_secret}).encode()
        try:
            resp = urlopen(Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": "devops-bot/1.0",
            }), timeout=10)
            data = json.loads(resp.read())
            if data.get("code") == 0:
                return data.get("data", {}).get("URL", "")
            log.warning(f"[{self.name}] endpoint api error: {data}")
        except Exception as e:
            log.warning(f"[{self.name}] endpoint error: {e}")
        return ""

    async def run(self):
        """生命周期主循环：鉴权 → 获取端点 → 连接 WS → 接收消息 → 重连"""
        self._running = True
        import asyncio
        import websockets

        while self._running:
            try:
                self.token = await self._get_tenant_token()
                ws_url = await self._get_ws_endpoint()
                if not ws_url:
                    log.warning(f"[{self.name}] no ws endpoint, retry in 10s")
                    await asyncio.sleep(10)
                    continue

                log.info(f"[{self.name}] connecting...")
                async with websockets.connect(ws_url, ping_interval=20) as ws:
                    self.ws = ws
                    log.info(f"[{self.name}] connected")

                    async for raw in ws:
                        if not isinstance(raw, str):
                            continue
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        msg_type = data.get("type", "")

                        if msg_type == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue

                        if msg_type == "pong":
                            continue

                        if msg_type == "event":
                            event = data.get("event", {})
                            header = event.get("header", {})
                            event_type = header.get("event_type", "")

                            if event_type == "im.message.receive_v1":
                                message = event.get("event", {}).get("message", {})
                                sender = event.get("event", {}).get("sender", {})
                                message_dict = {
                                    "event": {
                                        "message": message,
                                        "sender": sender,
                                    }
                                }
                                # 异步处理消息（不阻塞 WS 接收）
                                asyncio.create_task(self._safe_handle(message_dict))

            except asyncio.CancelledError:
                self._running = False
                log.info(f"[{self.name}] cancelled")
                break
            except Exception as e:
                log.warning(f"[{self.name}] connection error: {e}, reconnect in 5s")
                await asyncio.sleep(5)

    async def _safe_handle(self, message_dict: dict):
        """安全地处理消息，异常不扩散"""
        try:
            if self.on_message:
                if asyncio.iscoroutinefunction(self.on_message):
                    await self.on_message(message_dict)
                else:
                    self.on_message(message_dict)
        except Exception as e:
            log.warning(f"[{self.name}] handle error: {e}")

    async def reply_text(self, chat_id: str, text: str):
        """发送文本消息到群聊"""
        if not self.token:
            return
        url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id"
        payload = json.dumps({
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text[:2000]}, ensure_ascii=False),
        }).encode()
        try:
            resp = urlopen(Request(url, data=payload, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }), timeout=10)
            log.info(f"[{self.name}] reply sent to {chat_id}, status={resp.status}")
        except Exception as e:
            log.warning(f"[{self.name}] reply failed: {e}")

    async def reply_card(self, chat_id: str, text: str):
        """发送卡片消息到群聊"""
        if not self.token:
            return
        card = {
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": text[:2000]}
            }],
            "header": {"title": {"tag": "plain_text", "content": "DevOps Bot"}, "template": "blue"}
        }
        url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id"
        payload = json.dumps({
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }).encode()
        try:
            resp = urlopen(Request(url, data=payload, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }), timeout=10)
            log.info(f"[{self.name}] card reply sent to {chat_id}, status={resp.status}")
        except Exception as e:
            log.warning(f"[{self.name}] card reply failed: {e}")

    def stop(self):
        """停止机器人"""
        self._running = False
