"""Agentic DevOps API — FastAPI REST API Server (port 3000)"""
import sys, os, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from src.api.routes import auth, repos, dashboard, agents, events, chat, settings, skills, search, pipeline, webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from src.config import log
    try:
        from src.im.handler import process_chat
        from src.im.feishu import start_ws
        import asyncio
        def _run_feishu_ws():
            try:
                asyncio.set_event_loop(asyncio.new_event_loop())
                start_ws(process_chat)
            except Exception as e:
                log.warning(f"Feishu WS error: {e}")
        t = threading.Thread(target=_run_feishu_ws, daemon=True)
        t.start()
        log.info("Feishu WS started in background thread")
    except Exception as e:
        log.warning(f"Feishu WS start skipped: {e}")
    yield
    # Shutdown
    try:
        from src.im.feishu import stop_ws
        stop_ws()
        log.info("Feishu WS stopped")
    except Exception:
        pass


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
