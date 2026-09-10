"""API Token 认证内核（Issue-45 方式 A / 方式 B）。

- 方式 A（会话级临时 token，前缀 `tk_`）：按 (repo_key, agent_type[, user_id for chat]) 签发，
  经 `session_key`（task_id / conv_id）定位；**存在即有效，删除即失效**。
- 方式 B（用户级长期 API Key，前缀 `grape_`）：绑定单用户，sha256 落库（不存明文），可吊销。

安全约定：
- 明文仅生成时返回一次，落库一律 SHA-256。
- 传输/查询时按 token 前缀分流（auth_type=session_token / api_key）。
"""
import hashlib
import secrets
import time

from src.session_manager import db, db_lock
from src.config import log

# ---- 常量 ----
SESSION_TOKEN_PREFIX = "tk_"
API_KEY_PREFIX = "grape_"
SESSION_TOKEN_TTL = 6 * 3600  # 会话级临时 token 默认 TTL（秒）——执行超时兜底

# 预留 scopes（方式 B，未来 RBAC 扩展）
_API_KEY_SCOPES = '["public"]'


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


# ===================== 方式 A：会话级临时 token =====================

def issue_session_token(*, session_key: str, repo_key: str, agent_type: str,
                        user_id: int = 0, ttl: int = SESSION_TOKEN_TTL) -> str:
    """签发会话级临时 token，返回明文（仅注入 env，不落盘）。

    明文的 sha256 落 `session_api_tokens`；`session_key` 记录了会话（task_id / conv_id）。
    """
    token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO session_api_tokens (session_key, repo_key, agent_type, user_id, token_sha256, expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_key, repo_key, agent_type, user_id, _sha256(token), str(_now() + ttl)),
            )
            db.commit()
        return token
    except Exception as e:
        log.warning(f"Issue session token failed: {e}")
        raise


def verify_session_token(token: str) -> dict | None:
    """校验会话级临时 token：sha256 查表 → 行存在且未过期即有效。

    返回绑定上下文（session_key / repo_key / agent_type / user_id），供调用方做作用域判断。
    """
    if not token.startswith(SESSION_TOKEN_PREFIX):
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, session_key, repo_key, agent_type, user_id, expires_at FROM session_api_tokens "
                "WHERE token_sha256=?",
                (_sha256(token),),
            )
            row = c.fetchone()
        if not row:
            return None
        if int(row[5]) <= _now():
            return None
        return {
            "session_token_id": row[0],
            "session_key": row[1],
            "repo_key": row[2],
            "agent_type": row[3],
            "user_id": row[4],
        }
    except Exception as e:
        log.warning(f"Verify session token failed: {e}")
        return None


def verify_session_token_for_repo(token: str, repo_key: str) -> dict | None:
    """会话级临时 token 按仓库作用域校验（A2A / events 内部 agent 端点用）。

    会话 token 绑定了 (repo_key, agent_type)，本次只校验仓库匹配——
    即「该仓库执行期的 agent 凭据」才能调用该仓库的内部协议端点。
    """
    info = verify_session_token(token)
    if not info or not repo_key:
        return None
    if info.get("repo_key") and info["repo_key"] == repo_key:
        return info
    return None


def revoke_session_token(session_key: str) -> bool:
    """会话结束即失效：按 session_key 删除对应 token（不误删并发同类会话）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM session_api_tokens WHERE session_key=?", (session_key,))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Revoke session token failed: {e}")
        return False


def purge_expired_session_tokens() -> int:
    """TTL 兜底清理过期行，返回清理行数。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM session_api_tokens WHERE CAST(expires_at AS INTEGER) <= ?", (_now(),))
            db.commit()
            return c.rowcount
    except Exception as e:
        log.warning(f"Purge expired session tokens failed: {e}")
        return 0


# ===================== 方式 B：用户级长期 API Key =====================

def generate_api_token(user_id: int, name: str = "") -> dict | None:
    """生成用户级 API Key，返回 {token(明文一次), id}；None 表示失败。

    明文仅创建返回一次；落库 sha256；同名（user_id, name）冲突时返回 None。
    """
    token = API_KEY_PREFIX + secrets.token_urlsafe(40)
    name = (name or "").strip()
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO api_tokens (user_id, token_sha256, name) VALUES (?,?,?)",
                (user_id, _sha256(token), name),
            )
            db.commit()
            return {"token": token, "id": c.lastrowid}
    except Exception as e:
        log.warning(f"Generate api token failed: {e}")
        return None


def verify_api_token(token: str) -> dict | None:
    """校验用户级 API Key：sha256 查表 → 未吊销即有效。更新 last_used_at。"""
    if not token.startswith(API_KEY_PREFIX):
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, user_id, name, scopes, revoked FROM api_tokens WHERE token_sha256=?",
                (_sha256(token),),
            )
            row = c.fetchone()
        if not row or row[4] != 0:
            return None
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE api_tokens SET last_used_at=? WHERE id=?",
                    (str(_now()), row[0]),
                )
                db.commit()
        except Exception:
            pass
        return {"api_token_id": row[0], "user_id": row[1], "name": row[2], "scopes": row[3]}
    except Exception as e:
        log.warning(f"Verify api token failed: {e}")
        return None


def revoke_api_token(user_id: int, token_id: int) -> bool:
    """吊销用户级 API Key（软删：revoked=1，保审计）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE api_tokens SET revoked=1 WHERE id=? AND user_id=?", (token_id, user_id))
            db.commit()
            return c.rowcount > 0
    except Exception as e:
        log.warning(f"Revoke api token failed: {e}")
        return False


def list_api_tokens(user_id: int) -> list[dict]:
    """列出用户全部未吊销 API Key（不含明文/sha256）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, name, scopes, created_at, last_used_at FROM api_tokens "
                "WHERE user_id=? AND revoked=0 ORDER BY id DESC",
                (user_id,),
            )
            rows = c.fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "scopes": r[2],
                "created_at": r[3],
                "last_used_at": r[4],
            }
            for r in rows
        ]
    except Exception as e:
        log.warning(f"List api tokens failed: {e}")
        return []