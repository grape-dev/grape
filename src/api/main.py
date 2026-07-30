"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from src.api.routes import auth, repos, dashboard, agents, events, chat, settings, skills, search, pipeline, webhook, scenes


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from src.config import log
    ws_tasks = []
    chat_bots = []

    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT DISTINCT bot_app_id, bot_app_secret, bot_type FROM im_bot_config")
            rows = c.fetchall()

        from src.im.feishu_bot import FeishuBot
        from src.im.feishu import _handle_message
        from src.im.handler import process_chat
        from src.im.vod_handler import process_vod_message

        for bot_app_id, bot_app_secret, bot_type in rows:
            try:
                if bot_type == "chat_bot":
                    def _make_chat_handler():
                        import concurrent.futures
                        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)

                        async def _handler(msg):
                            event = msg.get("event", {})
                            message = event.get("message", {})
                            msg_type = message.get("message_type", "text")
                            content_str = message.get("content", "{}")
                            chat_id = message.get("chat_id", "")
                            sender_open_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")

                            text = ""
                            if msg_type == "text":
                                try:
                                    text = json.loads(content_str).get("text", "")
                                except Exception:
                                    text = content_str

                            if not text or not chat_id:
                                return

                            parsed = {
                                "type": "message", "text": text,
                                "chat_id": chat_id, "sender_id": sender_open_id,
                                "sender_open_id": sender_open_id,
                                "message_id": message.get("message_id", ""),
                                "message_type": msg_type, "account": bot_app_id,
                            }
                            loop = asyncio.get_event_loop()
                            await loop.run_in_executor(_pool, process_chat, parsed)

                        return _handler

                    bot = FeishuBot(bot_app_id, bot_app_secret, f"chat-{bot_app_id[:12]}",
                                    _make_chat_handler())
                    chat_bots.append(bot)
                    task = asyncio.create_task(bot.run(), name=f"chat-{bot_app_id[:20]}")
                    ws_tasks.append(task)
                    log.info(f"Chat Bot task created: {bot_app_id[:40]}")

                elif bot_type == "vod_bot":
                    bot = FeishuBot(bot_app_id, bot_app_secret, f"vod-{bot_app_id[:12]}",
                                    process_vod_message)
                    chat_bots.append(bot)
                    task = asyncio.create_task(bot.run(), name=f"vod-{bot_app_id[:20]}")
                    ws_tasks.append(task)
                    log.info(f"VOD Bot task created: {bot_app_id[:40]}")

            except Exception as e:
                log.warning(f"Bot init failed for {bot_app_id[:20]}: {e}")

        log.info(f"IM Bots: {len(ws_tasks)} task(s) created")

    except Exception as e:
        log.warning(f"IM Bots startup failed: {e}")

    yield

    # Shutdown
    log.info("Shutting down IM Bots...")
    for bot in chat_bots:
        bot.stop()
    for task in ws_tasks:
        task.cancel()
    for task in ws_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    log.info("IM Bots stopped")