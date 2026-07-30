"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from src.api.routes import auth, repos, dashboard, agents, events, chat, settings, skills, search, pipeline, webhook, scenes


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.config import log

    # 启动 IM Bot WebSocket（线程 + asyncio.run 隔离 loop）
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT DISTINCT bot_app_id, bot_app_secret, bot_type FROM im_bot_config")
            rows = c.fetchall()
        from src.im.feishu import start_ws_async, start_vod_ws_async
        from src.im.handler import process_chat
        import asyncio

        for bot_app_id, bot_app_secret, bot_type in rows:
            try:
                if bot_type == "chat_bot":
                    threading.Thread(
                        target=lambda: asyncio.run(start_ws_async(process_chat, bot_app_id, bot_app_secret)),
                        daemon=True, name=f"chat-ws-{bot_app_id[:20]}"
                    ).start()
                    log.info(f"Chat WS thread: {bot_app_id[:40]}")
                elif bot_type == "vod_bot":
                    threading.Thread(
                        target=lambda: asyncio.run(start_vod_ws_async(bot_app_id, bot_app_secret)),
                        daemon=True, name=f"vod-ws-{bot_app_id[:20]}"
                    ).start()
                    log.info(f"VOD WS thread: {bot_app_id[:40]}")
            except Exception as e:
                log.warning(f"WS failed for {bot_app_id[:20]}: {e}")
    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")

    yield

    log.info("IM WS: stopping...")
    try:
        from src.im.feishu import stop_ws
        stop_ws()
    except Exception:
        pass
    log.info("IM WS stopped")


app = FastAPI(title="Agentic DevOps API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(repos.router)
app.include_router(dashboard.router)
app.include_router(agents.router)
app.include_router(events.router)
app.include_router(chat.router)
app.include_router(settings.router)
app.include_router(skills.router)
app.include_router(search.router)
app.include_router(pipeline.router)
app.include_router(webhook.router)
app.include_router(scenes.router)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from fastapi.responses import FileResponse
    @app.get("/")
    async def serve_spa():
        from fastapi.responses import FileResponse
        resp = FileResponse(os.path.join(STATIC_DIR, "index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

from src.api.db_setup import init_database
init_database()

from src.config import _load_accounts
_load_accounts()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from src.config import log
    log.warning(f"API error: {exc}")
    return JSONResponse(status_code=500, content={"code": 500, "message": str(exc)})


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}
