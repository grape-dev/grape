"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os, asyncio
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

        from src.im.feishu import start_ws_async, start_vod_ws_async
        from src.im.handler import process_chat

        # 测试 async task 是否运行
        async def _test():
            log.info("WS_TEST: test task running!")
            await asyncio.sleep(1)
            log.info("WS_TEST: test task done, ws tasks should follow")
        t = asyncio.get_event_loop().create_task(_test())
        ws_tasks.append(t)

        for bot_app_id, bot_app_secret, bot_type in rows:
            try:
                if bot_type == "chat_bot":
                    class WsTask:
                        pass
                    task = asyncio.get_event_loop().create_task(
                        start_ws_async(process_chat, bot_app_id, bot_app_secret)
                    )
                    ws_tasks.append(task)
                    log.info(f"Chat WS task created: {bot_app_id[:40]}")

                elif bot_type == "vod_bot":
                    task = asyncio.get_event_loop().create_task(
                        start_vod_ws_async(bot_app_id, bot_app_secret)
                    )
                    ws_tasks.append(task)
                    log.info(f"VOD WS task created: {bot_app_id[:40]}")

            except Exception as e:
                log.warning(f"WS task create failed for {bot_app_id[:20]}: {e}")

        log.info(f"IM WS: {len(ws_tasks)} task(s) created")

    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")

    yield

    # Shutdown
    log.info("Shutting down IM WS...")
    for task in ws_tasks:
        task.cancel()
    for task in ws_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    from src.im.feishu import stop_ws
    stop_ws()
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

# Re-load accounts after DB init (fix circular import issue)
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