"""Community API routes for grape-website community page.

Posts CRUD + comments + likes + file upload. Auth via get_current_user (JWT).
Tables created in db_setup.py init_database().
"""
import os, uuid
from fastapi import APIRouter, Depends, Query, UploadFile, File, HTTPException
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import log
from src.session_manager import db, db_lock

router = APIRouter(prefix='/api/community', tags=['community'])

_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'community_uploads')
_ALLOWED_IMAGE = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
_ALLOWED_MD = {'.md', '.markdown'}
_MAX_IMG_SIZE = 5 * 1024 * 1024  # 5MB
_MAX_MD_SIZE = 2 * 1024 * 1024    # 2MB

_CAT_LABELS = {
    'tech': 'Tech',
    'bug': 'Bug',
    'experience': 'Experience',
    'suggestion': 'Suggestion',
}


class CreatePostBody(BaseModel):
    title: str
    content: str = ''
    category: str = 'tech'


class CreateCommentBody(BaseModel):
    content: str


@router.get('/posts')
def list_posts(page: int = Query(0, ge=0), size: int = Query(15, ge=1, le=100),
               category: str = Query(''), search: str = Query(''),
               sort: str = Query('recent')):
    """List posts with pagination, category filter, search, and sort."""
    try:
        with db_lock:
            c = db.cursor()
            conditions = ["is_deleted=0"]
            params = []
            if category:
                conditions.append("category=?")
                params.append(category)
            if search:
                conditions.append("(title LIKE ? OR content LIKE ?)")
                params.extend(["%" + search + "%", "%" + search + "%"])
            where = " WHERE " + " AND ".join(conditions)

            c.execute("SELECT COUNT(*) FROM community_posts" + where, tuple(params))
            total = c.fetchone()[0]

            order = "likes DESC, comments_count DESC" if sort == 'popular' else "created_at DESC"
            c.execute(
                "SELECT id, user_id, username, title, content, category, "
                "views, likes, comments_count, created_at "
                "FROM community_posts" + where +
                " ORDER BY " + order + " LIMIT ? OFFSET ?",
                tuple(params + [size, page * size]))
            rows = c.fetchall()

        posts = []
        for r in rows:
            posts.append({
                "id": r[0], "user_id": r[1], "username": r[2],
                "title": r[3], "content": r[4], "category": r[5],
                "category_label": _CAT_LABELS.get(r[5], r[5]),
                "views": r[6], "likes": r[7], "comments_count": r[8],
                "created_at": r[9]
            })
        return {"posts": posts, "total": total, "page": page}
    except Exception as e:
        log.warning(f"community list posts failed: {e}")
        return {"posts": [], "total": 0, "error": str(e)}


@router.get('/stats')
def get_stats():
    """Get community statistics: total posts, total likes, total comments, hot posts."""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM community_posts WHERE is_deleted=0")
            total_posts = c.fetchone()[0]
            c.execute("SELECT COALESCE(SUM(likes),0) FROM community_posts WHERE is_deleted=0")
            total_likes = c.fetchone()[0]
            c.execute("SELECT COALESCE(SUM(comments_count),0) FROM community_posts WHERE is_deleted=0")
            total_comments = c.fetchone()[0]
            c.execute(
                "SELECT id, title, likes, comments_count FROM community_posts "
                "WHERE is_deleted=0 ORDER BY likes DESC, comments_count DESC LIMIT 5")
            hot = [{"id": r[0], "title": r[1], "likes": r[2], "comments_count": r[3]}
                   for r in c.fetchall()]
        return {
            "total_posts": total_posts,
            "total_likes": total_likes,
            "total_comments": total_comments,
            "hot_posts": hot
        }
    except Exception as e:
        log.warning(f"community stats failed: {e}")
        return {"total_posts": 0, "total_likes": 0, "total_comments": 0, "hot_posts": []}


@router.post('/posts')
def create_post(body: CreatePostBody, user: dict = Depends(get_current_user)):
    """Create a new post. Requires login."""
    if not body.title.strip():
        return {"error": "Title required"}
    if body.category not in _CAT_LABELS:
        body.category = 'tech'
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO community_posts (user_id, username, title, content, category) "
                "VALUES (?, ?, ?, ?, ?)",
                (user.get('id', 0), user.get('username', ''),
                 body.title.strip(), body.content.strip(), body.category))
            db.commit()
            pid = c.lastrowid
        log.info(f"community: post created id={pid} by user={user.get('username')}")
        return {"id": pid, "message": "ok"}
    except Exception as e:
        log.warning(f"community create post failed: {e}")
        return {"error": "Failed to create post"}


@router.get('/posts/{pid}')
def get_post(pid: int):
    """Get a single post with comments. Increments view count."""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE community_posts SET views=views+1 WHERE id=? AND is_deleted=0", (pid,))
            db.commit()
            c.execute(
                "SELECT id, user_id, username, title, content, category, "
                "views, likes, comments_count, created_at "
                "FROM community_posts WHERE id=? AND is_deleted=0", (pid,))
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Post not found")
            c.execute(
                "SELECT id, user_id, username, content, created_at "
                "FROM community_comments WHERE post_id=? AND is_deleted=0 "
                "ORDER BY created_at ASC", (pid,))
            comment_rows = c.fetchall()
        post = {
            "id": row[0], "user_id": row[1], "username": row[2],
            "title": row[3], "content": row[4], "category": row[5],
            "category_label": _CAT_LABELS.get(row[5], row[5]),
            "views": row[6], "likes": row[7], "comments_count": row[8],
            "created_at": row[9]
        }
        comments = []
        for cr in comment_rows:
            comments.append({
                "id": cr[0], "user_id": cr[1], "username": cr[2],
                "content": cr[3], "created_at": cr[4]
            })
        return {**post, "comments": comments}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"community get post failed: {e}")
        return {"error": str(e)}


@router.post('/posts/{pid}/like')
def toggle_like(pid: int, user: dict = Depends(get_current_user)):
    """Toggle like on a post. Requires login."""
    uid = user.get('id', 0)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM community_posts WHERE id=? AND is_deleted=0", (pid,))
            if not c.fetchone():
                raise HTTPException(status_code=404, detail="Post not found")
            c.execute("SELECT id FROM community_post_likes WHERE post_id=? AND user_id=?", (pid, uid))
            existing = c.fetchone()
            if existing:
                c.execute("DELETE FROM community_post_likes WHERE post_id=? AND user_id=?", (pid, uid))
                c.execute("UPDATE community_posts SET likes=likes-1 WHERE id=? AND likes>0", (pid,))
                db.commit()
                liked = False
            else:
                c.execute("INSERT INTO community_post_likes (post_id, user_id) VALUES (?, ?)", (pid, uid))
                c.execute("UPDATE community_posts SET likes=likes+1 WHERE id=?", (pid,))
                db.commit()
                liked = True
            c.execute("SELECT likes FROM community_posts WHERE id=?", (pid,))
            likes = c.fetchone()[0]
        return {"liked": liked, "likes": likes}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"community toggle like failed: {e}")
        return {"error": "Failed to toggle like"}


@router.post('/posts/{pid}/comments')
def add_comment(pid: int, body: CreateCommentBody, user: dict = Depends(get_current_user)):
    """Add a comment to a post. Requires login."""
    if not body.content.strip():
        return {"error": "Comment content required"}
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM community_posts WHERE id=? AND is_deleted=0", (pid,))
            if not c.fetchone():
                raise HTTPException(status_code=404, detail="Post not found")
            c.execute(
                "INSERT INTO community_comments (post_id, user_id, username, content) "
                "VALUES (?, ?, ?, ?)",
                (pid, user.get('id', 0), user.get('username', ''), body.content.strip()))
            c.execute("UPDATE community_posts SET comments_count=comments_count+1 WHERE id=?", (pid,))
            db.commit()
            cid = c.lastrowid
        log.info(f"community: comment created id={cid} on post={pid} by user={user.get('username')}")
        return {"id": cid, "message": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"community add comment failed: {e}")
        return {"error": "Failed to add comment"}


@router.post('/upload/image')
async def upload_image(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Upload an image for community posts. Returns URL for Markdown embedding."""
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in _ALLOWED_IMAGE:
        raise HTTPException(400, detail=f"Only {', '.join(_ALLOWED_IMAGE)} files allowed")
    content = await file.read()
    if len(content) > _MAX_IMG_SIZE:
        raise HTTPException(400, detail="Image too large (max 5MB)")
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(_UPLOAD_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(content)
    url = f"/static/community_uploads/{filename}"
    log.info(f"community: image uploaded {filename} by user={user.get('username')}")
    return {"url": url, "filename": filename}


@router.post('/upload/md')
async def upload_md(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Upload a .md file and return its text content for the post editor."""
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in _ALLOWED_MD:
        raise HTTPException(400, detail="Only .md files allowed")
    content = await file.read()
    if len(content) > _MAX_MD_SIZE:
        raise HTTPException(400, detail="Markdown file too large (max 2MB)")
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        text = content.decode('gbk', errors='replace')
    log.info(f"community: md uploaded {file.filename} ({len(text)} chars) by user={user.get('username')}")
    return {"content": text, "filename": file.filename}
