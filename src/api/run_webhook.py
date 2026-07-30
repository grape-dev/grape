"""Webhook-only server — port 8080"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI
from src.api.routes.webhook import router as webhook_router
from src.api.db_setup import init_database
from src.config import _load_accounts, log

app = FastAPI(title="Agentic DevOps Webhook", version="2.0.0")
app.include_router(webhook_router)

init_database()
_load_accounts()


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    log.warning(f"Webhook error: {exc}")
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"code": 500, "message": str(exc)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.run_webhook:app", host="0.0.0.0", port=8080)
