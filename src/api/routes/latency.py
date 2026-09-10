"""前端 API 时延上报接口（方案B）。

无认证——时延数据不敏感，但单次上限 100 条防滥用。
数据写入 api_latency_log 表，source='frontend'，与方案C（backend）共用同一张表。
"""
from fastapi import APIRouter, Request
from src.session_manager import db, db_lock
from src.config import log

router = APIRouter(prefix="/api/latency", tags=["latency"])


@router.post("/report")
async def report_latency(request: Request):
    """前端批量上报 API 请求耗时。

    请求体：{"entries": [{"url": "/dashboard/...", "method": "GET", "status": 200, "durationMs": 45, "ts": 1234567890}]}
    返回：{"ok": true, "count": N}
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    entries = body.get("entries", [])
    if not entries or len(entries) > 100:
        return {"ok": False, "error": "empty or too many entries"}

    inserted = 0
    try:
        with db_lock:
            c = db.cursor()
            for e in entries:
                try:
                    c.execute(
                        "INSERT INTO api_latency_log (method, path, status, duration_ms, source) "
                        "VALUES (?, ?, ?, ?, 'frontend')",
                        (str(e.get("method", "")), str(e.get("url", "")), int(e.get("status", 0)), int(e.get("durationMs", 0)))
                    )
                    inserted += 1
                except Exception:
                    pass
            db.commit()
        log.info(f"Latency report: {inserted}/{len(entries)} entries inserted (frontend)")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, "count": inserted}
