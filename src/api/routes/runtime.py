"""Runtime API 路由。

M2：任务状态轮询端点（chat 前端打字机降级用）。
M3：完整 /api/runtime/* 协议（register/heartbeat/claim/report + rta_ 认证）在此基础上扩展。
"""
from fastapi import APIRouter, HTTPException, Depends
from src.api.deps import get_current_user
from src.runtime import service

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/tasks/{task_id}/status")
async def get_task_status(task_id: str, user: dict = Depends(get_current_user)):
    """任务状态轮询（chat 前端用，D5 打字机降级）。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    return {
        "id": task["id"],
        "status": task["status"],
        "result_text": task.get("result_text") or "",
        "exit_code": task.get("exit_code"),
        "errors": task.get("errors") or "",
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: dict = Depends(get_current_user)):
    """取消任务（M2-5）：置 cancelled；执行侧由 worker 感知退出。"""
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    service.cancel_task(task_id)
    return {"message": f"任务 {task_id} 已取消", "status": "cancelled"}
