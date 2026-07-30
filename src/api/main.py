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
    import threading, time

    # 启动所有已配置的 IM Bot WebSocket（线程方式）
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT DISTINCT bot_app_id, bot_app_secret, bot_type FROM im_bot_config")
            rows = c.fetchall()
        from src.im.feishu import start_ws_thread, start_vod_ws_thread
        from src.im.handler import process_chat

        started = 0
        for bot_app_id, bot_app_secret, bot_type in rows:
            try:
                if bot_type == "chat_bot":
                    t = threading.Thread(target=start_ws_thread,
                                         args=(process_chat, bot_app_id, bot_app_secret),
                                         daemon=True, name=f"chat-ws-{bot_app_id[:20]}")
                    t.start()
                    log.info(f"Chat WS thread started for {bot_app_id[:40]}")
                    started += 1
                elif bot_type == "vod_bot":
                    t = threading.Thread(target=start_vod_ws_thread,
                                         args=(bot_app_id, bot_app_secret),
                                         daemon=True, name=f"vod-ws-{bot_app_id[:20]}")
                    t.start()
                    log.info(f"VOD WS thread started for {bot_app_id[:40]}")
                    started += 1
                time.sleep(0.5)
            except Exception as e:
                log.warning(f"WS thread start failed for {bot_app_id[:20]}: {e}")

        log.info(f"IM Bot WS: {started}/{len(rows)} threads started")

    except Exception as e:
        log.warning(f"IM Bot WS startup failed: {e}")

    yield

    # Shutdown
    try:
        from src.im.feishu import stop_ws
        stop_ws()
        log.info("IM Bot WS stopped")
    except Exception:
        pass