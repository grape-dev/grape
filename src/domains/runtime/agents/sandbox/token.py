"""task_scoped_token 生命周期管理。

纯函数模块，不依赖 FastAPI/HTTP 上下文。
Worker 和服务端都能调。

签发：executor.py 在 task 启动时调 issue_proxy_token
校验：proxy.py 在 /api/proxy 请求时调 verify_proxy_token
失效：executor.py 在 task 结束时调 revoke_proxy_token
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone, timedelta

_TOKEN_TTL = 3900  # token 有效期 65 分钟（≥ RUNTIME_TASK_TIMEOUT 3600s + 300s 缓冲）

from src.kernel.config import log


class TokenIssueError(Exception):
    """token 签发失败。"""


class TokenError(Exception):
    """token 校验失败。"""


class TokenExpiredError(TokenError):
    """token 过期。"""


class TokenManager:
    """task_scoped_token 生命周期管理。

    封装签发/校验/失效 + DB 操作。
    不依赖 FastAPI——verify 抛 TokenError/TokenExpiredError，调用方决定 HTTP 状态码。
    """

    def __init__(self, db=None, db_lock=None):
        self._db = db
        self._lock = db_lock

    def _get_db(self):
        """延迟获取 db——构造时可能未初始化。"""
        if self._db is not None and self._lock is not None:
            return self._db, self._lock
        from src.kernel.session_manager import db, db_lock
        return db, db_lock

    def issue(self, task_id: str, backend_name: str) -> str:
        """签发 token，存 DB hash，返回明文。

        executor.py 在 task 启动时调用，token 通过 env GRAPE_PROXY_TOKEN 传给 Agent。
        失败 raise TokenIssueError——不返回空字符串（避免静默失败）。

        P0-9 修复：设 expires_at（默认 1800s），防止泄漏 token 永久有效。
        """
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        _expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_TOKEN_TTL)).isoformat()
        try:
            # 表归 mgmt：SQL 在 mgmt public 一份
            from src.domains.mgmt import public as _mg_tk
            from src.kernel.sandbox_uid import new_row_uid as _nru
            _mg_tk.upsert_task_proxy_token(_nru(), task_id, token_hash, backend_name, _expires_at)
        except Exception as e:
            log.error(f"issue proxy token failed: {e}")
            raise TokenIssueError(f"failed to issue proxy token: {e}") from e
        return token

    def verify(self, token: str) -> tuple[str, str]:
        """校验 token，返回 (task_id, backend_name)。

        失败 raise TokenError（无效）或 TokenExpiredError（过期）。
        调用方决定 HTTP 状态码——proxy.py 映射为 401。
        """
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            from src.domains.mgmt import public as _mg_tk2
            row = _mg_tk2.task_proxy_token_row(token_hash) or None
            if not row:
                raise TokenError("Invalid proxy token")
            task_id, backend_name, expires_at = row[0], row[1], row[2]
            # 过期校验
            # P0-9 修复：NULL expires_at 视为过期（不再跳过）
            if not expires_at:
                raise TokenExpiredError("Proxy token has no expiry (invalid)")
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    raise TokenExpiredError("Proxy token expired")
            except ValueError:
                raise TokenExpiredError(f"Proxy token has invalid expires_at format")
            return task_id, backend_name
        except (TokenError, TokenExpiredError):
            raise
        except Exception as e:
            log.error(f"proxy token verify failed: {e}")
            raise TokenError("Token verification error")

    def revoke(self, task_id: str) -> None:
        """失效 token。executor.py 在 task 结束时调用。

        P0-9 修复：revoke 失败时 raise（不再静默吞，调用方知道 token 未失效）。
        """
        try:
            db, db_lock = self._get_db()
            with db_lock:
                db.execute("DELETE FROM grape_t_task_proxy_tokens WHERE task_id=?", (task_id,))
                db.commit()
        except Exception as e:
            log.error(f"revoke proxy token failed: {e}")
            raise  # 不再静默吞——调用方需要知道 token 仍然有效


# 全局实例——main.py 构造后注入 app.state。
# 向后兼容：模块级函数保留，内部委托全局 _token_manager。
_token_manager = TokenManager()


def issue_proxy_token(task_id: str, backend_name: str) -> str:
    """签发 task_scoped_token。失败 raise TokenIssueError。"""
    return _token_manager.issue(task_id, backend_name)


def revoke_proxy_token(task_id: str) -> None:
    """失效 task_scoped_token。"""
    _token_manager.revoke(task_id)


def verify_proxy_token(token: str) -> tuple[str, str]:
    """校验 task_scoped_token。raise TokenError/TokenExpiredError。"""
    return _token_manager.verify(token)
