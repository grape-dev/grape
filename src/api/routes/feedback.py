"""问题反馈路由（Issue #174）。

数据存储在 grape_db 的 user_feedback 表中，由工作台直接读写。
Admin 管理台通过 grapeJdbc 直接查同一张表。
"""
from fastapi import APIRouter, Depends, Query, UploadFile, File, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
import os, uuid
from src.api.deps import get_current_user
from src.config import log
from src.session_manager import db, db_lock

router = APIRouter(prefix='/api', tags=['feedback'])


class FeedbackBody(BaseModel):
    category: str
    title: str
    description: str
    page_url: str = ''
    repo_key: str = ''
    attachment_url: str = ''
    attachment_name: str = ''


@router.post('/feedback')
def submit_feedback(body: FeedbackBody, user: dict = Depends(get_current_user)):
    """提交问题反馈（写入 grape_db user_feedback 表）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO user_feedback "
                "(user_id, username, category, title, description, page_url, repo_key, attachment_url, attachment_name, status) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'open')",
                (user.get('id') or 0, user.get('username') or '',
                 body.category, body.title, body.description,
                 body.page_url or '', body.repo_key or '',
                 body.attachment_url or '', body.attachment_name or ''))
            db.commit()
            fid = c.lastrowid
        log.info(f"feedback: submitted id={fid} by user={user.get('username')} category={body.category}")
        return {"id": fid, "status": "open", "message": "反馈已提交"}
    except Exception as e:
        log.warning(f"feedback submit failed: {e}")
        return {"error": "反馈提交失败，请稍后重试"}


@router.get('/feedback/list')
def list_feedback(
    status: str = Query('', description='筛选状态'),
    category: str = Query('', description='筛选类型'),
    search: str = Query('', description='搜索标题/描述'),
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
):
    """反馈列表（供 admin 管理台调用，JWT 或 API Key 认证）。"""
    try:
        with db_lock:
            c = db.cursor()
            conditions = []
            params = []
            if status:
                conditions.append("status=?")
                params.append(status)
            if category:
                conditions.append("category=?")
                params.append(category)
            if search:
                conditions.append("(title LIKE ? OR description LIKE ?)")
                params.extend([f"%{search}%", f"%{search}%"])
            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

            c.execute(f"SELECT COUNT(*) FROM user_feedback{where}", tuple(params))
            total = c.fetchone()[0]

            c.execute(
                f"SELECT id, user_id, username, category, title, description, "
                f"page_url, repo_key, attachment_url, attachment_name, status, priority, "
                f"handler_name, handler_remark, handled_at, created_at, updated_at "
                f"FROM user_feedback{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                tuple(params + [size, page * size]))
            rows = c.fetchall()

        items = []
        for r in rows:
            items.append({
                "id": r[0], "user_id": r[1], "username": r[2],
                "category": r[3], "title": r[4], "description": r[5],
                "page_url": r[6] or '', "repo_key": r[7] or '',
                "attachment_url": r[8] or '', "attachment_name": r[9] or '',
                "status": r[10], "priority": r[11],
                "handler_name": r[12] or '', "handler_remark": r[13] or '',
                "handled_at": r[14] or '', "created_at": r[15] or '',
                "updated_at": r[16] or '',
            })

        # 统计
        stats = _get_stats()

        return {"items": items, "total": total, "page": page, "stats": stats}
    except Exception as e:
        log.warning(f"feedback list failed: {e}")
        return {"items": [], "total": 0, "error": str(e)}


@router.put('/feedback/{fid}/status')
def update_feedback_status(fid: int, body: dict):
    """更新反馈状态（admin 管理台调用）。"""
    new_status = body.get("status", "")
    priority = body.get("priority")
    handler_name = body.get("handler_name", "")
    handler_remark = body.get("handler_remark", "")
    if new_status not in ("open", "in_progress", "resolved", "closed"):
        return {"error": "无效状态"}
    try:
        with db_lock:
            c = db.cursor()
            sets = ["status=?", "handler_name=?", "handler_remark=?"]
            params = [new_status, handler_name, handler_remark]
            if priority is not None:
                sets.append("priority=?")
                params.append(int(priority))
            if new_status in ("resolved", "closed"):
                sets.append("handled_at=?")
                params.append(datetime.now(timezone.utc).isoformat())
            params.append(fid)
            c.execute(
                f"UPDATE user_feedback SET {', '.join(sets)} WHERE id=?",
                tuple(params))
            db.commit()
            if c.rowcount == 0:
                return {"error": "反馈记录不存在"}
        log.info(f"feedback: updated id={fid} status={new_status} by={handler_name}")
        return {"success": True, "message": "状态已更新"}
    except Exception as e:
        log.warning(f"feedback update failed: {e}")
        return {"error": "更新失败"}


@router.get('/feedback/stats')
def feedback_stats():
    """反馈统计（供 admin 管理台调用）。"""
    return _get_stats()


@router.get('/feedback/mine')
def my_feedback(
    user: dict = Depends(get_current_user),
    page: int = Query(0, ge=0),
    size: int = Query(10, ge=1, le=100),
):
    """查看当前用户提交的反馈列表（分页）。"""
    uid = user.get('id') or 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM user_feedback WHERE user_id=?", (uid,))
            total = c.fetchone()[0]
            c.execute(
                "SELECT id, category, title, description, attachment_url, attachment_name, "
                "status, priority, handler_name, handler_remark, created_at, updated_at "
                "FROM user_feedback WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (uid, size, page * size))
            rows = c.fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r[0], "category": r[1], "title": r[2], "description": r[3],
                "attachment_url": r[4] or '', "attachment_name": r[5] or '',
                "status": r[6], "priority": r[7],
                "handler_name": r[8] or '', "handler_remark": r[9] or '',
                "created_at": r[10] or '', "updated_at": r[11] or '',
            })
        return {"items": items, "total": total, "page": page,
                "total_pages": max(1, (total + size - 1) // size)}
    except Exception as e:
        log.warning(f"feedback mine failed: {e}")
        return {"items": [], "total": 0, "error": str(e)}


def _get_stats():
    """查询反馈统计数据。"""
    try:
        with db_lock:
            c = db.cursor()
            result = {"byStatus": {}, "byCategory": {}}
            for st in ("open", "in_progress", "resolved", "closed"):
                c.execute("SELECT COUNT(*) FROM user_feedback WHERE status=?", (st,))
                result["byStatus"][st] = c.fetchone()[0]
            result["byStatus"]["total"] = sum(result["byStatus"].values())
            for cat in ("bug", "suggestion", "question", "other"):
                c.execute("SELECT COUNT(*) FROM user_feedback WHERE category=?", (cat,))
                result["byCategory"][cat] = c.fetchone()[0]
            result["byCategory"]["total"] = sum(result["byCategory"].values())
            return result
    except Exception as e:
        log.warning(f"feedback stats failed: {e}")
        return {"byStatus": {}, "byCategory": {}}


@router.post('/feedback/upload')
async def upload_feedback_attachment(file: UploadFile = File(...),
                                     user: dict = Depends(get_current_user)):
    """上传反馈附件（图片或 zip 文件）。

    OBS 模式下存到 OBS 桶（feedback/ 目录），返回签名 URL。
    非 OBS 模式下存到本地 static/uploads/feedback/，返回本地路径。
    """
    # 校验文件类型：图片 + zip
    allowed_types = {
        "image/jpeg", "image/png", "image/webp", "image/gif", "image/svg+xml",
        "application/zip", "application/x-zip-compressed", "application/octet-stream",
    }
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".zip"}
    content_type = file.content_type or ""
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if content_type not in allowed_types and ext not in allowed_exts:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仅支持 jpg/png/webp/gif/svg/zip 格式"})
    if not ext:
        ext = ".png"

    # 读取并校验大小（5MB）
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "文件不能超过 5MB"})

    # 生成唯一 key
    file_key = f"feedback/{uuid.uuid4().hex[:16]}{ext}"

    from src.storage import get_storage
    storage = get_storage()
    if storage:
        # OBS 模式：存到 OBS 桶
        storage.save_bytes(file_key, content)
        url = storage.temporary_url(file_key, expires=3600 * 24 * 7)  # 7 天有效
        log.info(f"feedback upload: OBS key={file_key} size={len(content)} by={user.get('username')}")
    else:
        # 非 OBS 模式：存到本地
        static_dir = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "feedback")
        os.makedirs(static_dir, exist_ok=True)
        local_name = file_key.replace("feedback/", "")
        local_path = os.path.join(static_dir, local_name)
        with open(local_path, "wb") as f:
            f.write(content)
        url = f"/static/uploads/feedback/{local_name}"
        log.info(f"feedback upload: local path={url} size={len(content)} by={user.get('username')}")

    return {"url": url, "file_key": file_key, "is_zip": ext == ".zip", "original_name": filename}
