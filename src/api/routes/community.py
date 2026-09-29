"""Community API routes for grape-website community page."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import log
from src.session_manager import db, db_lock

router = APIRouter(prefix='/api/community', tags=['community'])

_CAT_LABELS = {'tech': 'Tech', 'bug': 'Bug', 'experience': 'Experience', 'suggestion': 'Suggestion'}


class CreatePostBody(BaseModel):
    title: str
    content: str = ''
    category: str = 'tech'


class CreateCommentBody(BaseModel):
    content: str


@router.get('/posts')
def list_posts(page: int = Query(0, ge=0), size: int = Query(15, ge=1, le=100),
               category: str = Query(''), search: str = Query('')):
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
            c.execute(
                "SELECT id, user_id, username, title, content, category, "
                "views, likes, comments_count, created_at "
                "FROM community_posts" + where +
                " ORDER BY created_at DESC LIMIT ? OFFSET ?",
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


@router.post('/posts')
def create_post(body: CreatePostBody, user: dict = Depends(get_current_user)):
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
                return {"error": "Post not found"}
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
    except Exception as e:
        log.warning(f"community get post failed: {e}")
        return {"error": str(e)}


@router.post('/posts/{pid}/like')
def toggle_like(pid: int, user: dict = Depends(get_current_user)):
    uid = user.get('id', 0)
    try:
        with db_lock:
            c = db.cursor()
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
    except Exception as e:
        log.warning(f"community toggle like failed: {e}")
        return {"error": "Failed to toggle like"}


@router.post('/posts/{pid}/comments')
def add_comment(pid: int, body: CreateCommentBody, user: dict = Depends(get_current_user)):
    if not body.content.strip():
        return {"error": "Comment content required"}
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM community_posts WHERE id=? AND is_deleted=0", (pid,))
            if not c.fetchone():
                return {"error": "Post not found"}
            c.execute(
                "INSERT INTO community_comments (post_id, user_id, username, content) "
                "VALUES (?, ?, ?, ?)",
                (pid, user.get('id', 0), user.get('username', ''), body.content.strip()))
            c.execute("UPDATE community_posts SET comments_count=comments_count+1 WHERE id=?", (pid,))
            db.commit()
            cid = c.lastrowid
        log.info(f"community: comment created id={cid} on post={pid} by user={user.get('username')}")
        return {"id": cid, "message": "ok"}
    except Exception as e:
        log.warning(f"community add comment failed: {e}")
        return {"error": "Failed to add comment"}
