"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from src.api.routes import auth, repos, dashboard, agents, events, chat, settings, skills, search, pipeline, webhook, scenes, templates, tasks, runtime, feishu_onboard, task_logs


bot_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_manager
    from src.config import log

    try:
        from src.im.feishu_async import BotManager
        from src.im.handler_async import process_chat_async
        from src.im.vod_handler_async import process_vod_async

        bot_manager = BotManager(max_concurrent_per_bot=10)
        await bot_manager.start()

        async def _on_message(ctx, parsed):
            if ctx.bot_type == "vod_bot":
                await process_vod_async(ctx, bot_manager.api, parsed)
            else:
                await process_chat_async(ctx, bot_manager.api, parsed)

        bot_manager.set_message_handler(_on_message)
        await bot_manager.load_from_db()

        hc = bot_manager.health_check()
        log.info(f"IM WS: {hc['total_bots']} bot(s), {hc['connected']} connected")
    except Exception as e:
        log.warning(f"IM WS startup failed: {e}")

    # sweeper：Issue done 后清理 workdir（见 multi-issue-isolation-design.md §3.4.2）
    _sweep_task = None
    try:
        from src.runtime.cleanup import sweep_loop
        _sweep_task = asyncio.create_task(sweep_loop())
        log.info("Sweeper loop started")
    except Exception as e:
        log.warning(f"Sweeper loop start failed: {e}")

    yield

    log.info("IM WS: stopping...")
    if bot_manager:
        await bot_manager.stop()
    if _sweep_task:
        _sweep_task.cancel()
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
app.include_router(templates.router)
app.include_router(tasks.router)
app.include_router(runtime.router)
app.include_router(feishu_onboard.router)
app.include_router(task_logs.router)
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
    result = {"status": "ok", "version": "2.0.0"}
    if bot_manager:
        result["im"] = bot_manager.health_check()
    return result