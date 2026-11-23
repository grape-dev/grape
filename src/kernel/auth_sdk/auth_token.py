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

from src.kernel.config import log
from src.kernel.ports import tokens as _tokens

# ---- 常量 ----
SESSION_TOKEN_PREFIX = "tk_"
API_KEY_PREFIX = "grape_"
TAKEOVER_TOKEN_PREFIX = "tko_"
TAKEOVER_SESSION_KEY_PREFIX = "takeover:"
SESSION_TOKEN_TTL = 6 * 3600       # 会话级临时 token 默认 TTL（秒）——执行超时兜底
TAKEOVER_TOKEN_TTL = 12 * 3600      # 接管 token 默认 TTL（12h）
TAKEOVER_TOKEN_MAX_TTL = 24 * 3600  # 续期上限（从首次签发起算）
TAKEOVER_RENEW_WINDOW = 2 * 3600    # 剩余不足该值时允许续期

# 预留 scopes（方式 B，未来 RBAC 扩展）
_API_KEY_SCOPES = '["public"]'


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


# ===================== 方式 A：会话级临时 token =====================

def issue_session_token(*, session_key: str, repo_key: str, agent_type: str,
                        user_id: str = "", ttl: int = SESSION_TOKEN_TTL) -> str:
    """签发会话级临时 token，返回明文（仅注入 env，不落盘）。

    明文的 sha256 落 `session_api_tokens`；`session_key` 记录了会话（task_id / conv_id）。
    """
    token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
    try:
        # P2-7: 过期时间直接存整数（避免类型转换与 MySQL 索引问题）
        _tokens.session_token_insert(session_key, repo_key, agent_type, user_id,
                                     _sha256(token), _now() + ttl)
        return token
    except Exception as e:
        log.warning(f"Issue session token failed: {e}")
        raise


def verify_session_token(token: str) -> dict | None:
    """校验会话级临时 token：sha256 查表 → 行存在且未过期即有效。

    SELECT 与过期判断单源收敛到 ``inspect_session_token``（Issue #2508），
    本函数只把 ``valid`` 状态映射为绑定上下文；返回 session_key / repo_key /
    agent_type / user_id，供调用方做作用域判断。
    """
    if not token or not token.startswith(SESSION_TOKEN_PREFIX):
        return None
    state = inspect_session_token(token)
    if state.get("status") == "valid":
        return state.get("info")
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


SESSION_TOKEN_REVOKE_GRACE = 180  # 任务终态后延迟撤销秒数（agent complete → A2A 仍需 token）

# Issue #2508：认证失败三类细分（替代 #990 统一恢复提示）
TOKEN_REVOKED_OR_TASK_FINISHED = "TOKEN_REVOKED_OR_TASK_FINISHED"
SNAPSHOT_EXPIRED = "SNAPSHOT_EXPIRED"
MISSING_TOKEN = "MISSING_TOKEN"

# 各类失败的建议动作（CLI / 调用端按 code 读取后渲染差异化文案）
SESSION_TOKEN_FAILURE_ACTIONS = {
    TOKEN_REVOKED_OR_TASK_FINISHED: "stop_task_no_recovery",
    SNAPSHOT_EXPIRED: "reload_runtime_env",
    MISSING_TOKEN: "report_missing_injection",
}

SESSION_TOKEN_FAILURE_HINTS = {
    TOKEN_REVOKED_OR_TASK_FINISHED: (
        "Session token was revoked because the task has already finished; "
        "terminate the task and do not retry or restore it"
    ),
    SNAPSHOT_EXPIRED: (
        "Invalid or expired session token; the shell environment snapshot is stale, "
        "re-read GRAPE_SESSION_TOKEN from the agent runtime process environment and retry"
    ),
    MISSING_TOKEN: (
        "Missing session token; GRAPE_SESSION_TOKEN was not injected by the dispatcher, "
        "report the missing injection to the scheduling layer"
    ),
}

def _finished_task_statuses() -> tuple:
    """任务终态集合（用于判定 token 被撤销/任务已结束）。

    单源在 ``src.kernel.task_states``：kernel 不能 import domains（check-arch R1），
    所以词表放在 kernel，runtime 域反向引用，避免两套词表漂移。
    """
    from src.kernel.task_states import TERMINAL_TASK_STATUSES
    return tuple(TERMINAL_TASK_STATUSES)

# 仅对 tk_ 前缀做细分；其它前缀（grape_ API Key / JWT）沿用原有错误码。
_NOT_SESSION_TOKEN = "not_session_token"


def inspect_session_token(token: str) -> dict:
    """检查会话 token 状态（Issue #2508），不改变 verify 的既有语义。

    返回 ``{"status": ...}``：
    - ``missing``：未提供 token；
    - ``not_session_token``：非 tk_ 前缀；
    - ``unknown``：tk_ 前缀但查无此行（已轮换/已清理/伪造）；
    - ``expired``：行存在但已过期（含撤销宽限到期）；
    - ``valid``：行存在且未过期（附 ``info``）。
    """
    if not token:
        return {"status": "missing"}
    if not token.startswith(SESSION_TOKEN_PREFIX):
        return {"status": _NOT_SESSION_TOKEN}
    try:
        # 令牌表（session_api_tokens / api_tokens）归 mgmt 域：SQL 在该域 public，
        # kernel 经 tokens 端口调用（端口在装配层绑定，kernel 不 import domains）。
        row = _tokens.session_token_by_sha(_sha256(token))
    except Exception as e:
        log.warning(f"Inspect session token failed: {e}")
        return {"status": "unknown"}
    if not row:
        return {"status": "unknown"}
    info = {
        "session_token_id": row[0],
        "session_key": row[1],
        "repo_key": row[2],
        "agent_type": row[3],
        "user_id": row[4],
    }
    try:
        expired = int(row[5]) <= _now()
    except (TypeError, ValueError):
        expired = True
    if expired:
        return {"status": "expired", "info": info, "session_key": row[1]}
    return {"status": "valid", "info": info, "session_key": row[1]}


def _is_task_finished(task_status: str) -> bool:
    """任务状态是否已终态（token 可能已被撤销）。

    Issue #2508 review: agent_tasks is owned by the runtime domain, so kernel no
    longer queries it. Callers (src/api/deps.py) resolve the status through the
    runtime public primitive and pass it in.
    """
    return str(task_status or "").strip() in _finished_task_statuses()


def classify_session_token_failure(token: str, task_status: str = "") -> str:
    """把失效会话 token 细分为三类错误码（Issue #2508）。

    - 未提供 token → ``MISSING_TOKEN``；
    - token 关联任务已终态 → ``TOKEN_REVOKED_OR_TASK_FINISHED``；
    - 其余失效（过期/轮换/查无此行但任务未终态）→ ``SNAPSHOT_EXPIRED``。
    token 仍有效时返回空串。
    """
    state = inspect_session_token(token)
    status = state.get("status")
    if status == "missing":
        return MISSING_TOKEN
    if status == "valid":
        return ""
    if _is_task_finished(task_status):
        return TOKEN_REVOKED_OR_TASK_FINISHED
    return SNAPSHOT_EXPIRED


def revoke_session_token(session_key: str) -> bool:
    """会话结束延迟失效：把 expires_at 缩短到 SESSION_TOKEN_REVOKE_GRACE 秒后过期，
    而非立即 DELETE。agent complete 后仍需调 A2A（token mismatch 根因，seq#5613），
    宽限期内 verify_session_token 仍通过，过期后由 purge_expired_session_tokens 清理。"""
    try:
        return _tokens.session_token_set_expiry(
            session_key, _now() + SESSION_TOKEN_REVOKE_GRACE) > 0
    except Exception as e:
        log.warning(f"Revoke session token failed: {e}")
        return False


def purge_expired_session_tokens() -> int:
    """TTL 兜底清理过期行，返回清理行数。"""
    try:
        return _tokens.session_token_delete_expired(_now())
    except Exception as e:
        log.warning(f"Purge expired session tokens failed: {e}")
        return 0


SESSION_TOKEN_RECOVERY_HINT = (
    "Invalid or expired session token; if the shell environment snapshot is stale, "
    "re-read GRAPE_SESSION_TOKEN from the agent runtime process environment and retry"
)


def _utc_iso() -> str:
    """当前 UTC 时刻的 ISO 字符串（秒级列 created_at 的口径，Issue #1477）。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def rotate_session_token(*, session_key: str, repo_key: str, agent_type: str,
                         user_id: int = 0, ttl: int = SESSION_TOKEN_TTL,
                         commit: bool = True) -> str:
    """轮换会话 token：同一事务内先作废旧 token、再签发新 token。

    Issue #990：会话 token 轮换时若只签发新 token、不刷新 shell 环境快照
    （agent_tasks.env_extra），Agent 会继续用旧快照 token 调 grape → 全量 401。
    调用方（service.sync_task_session_token）用 ``commit=False`` 把「轮换 + 快照回写」
    放进同一事务，避免过期/签发/回写分步提交导致跨进程交错误伤新 token。

    任一步失败即回滚并抛出，保证不会出现「旧 token 已作废、新 token 未落库」的半状态。
    """
    token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = _now()
    try:
        # 作废旧 + 插新在本域同一事务内完成。跨域说明：commit=False 原本让调用方把
        # 「token 轮换 + agent_tasks.env_extra 快照回写」放进同一事务，三库独立后两张表
        # 分属 mgmt/runtime，跨库事务不可得 —— 本原语先落新 token（自提交），
        # 调用方随后回写快照，并靠自己的实例锁串行化、失败重试收敛。
        _tokens.session_token_rotate(session_key, repo_key, agent_type, user_id,
                                     _sha256(token), now + ttl, now)
    except Exception as e:
        log.warning(f"Rotate session token failed for {session_key}: {e}")
        raise
    _ = commit  # 参数保留以兼容既有调用方（跨库后不再控制事务边界）
    return token


# ===================== 方式 C：接管会话 token（tko_）=====================
#
# 语义：用户在本地交互式会话里接手一条正在跑的任务（见设计文档 §2）。
# - 前缀 `tko_` + kind='takeover'，与 worker 的 `tk_`（kind='worker'）**互不通用**：
#   两个 verify 各自带 kind 过滤，拿错前缀一律 None。
# - 但「互不通用」只约束 verify 的 kind 过滤，不约束能力位：两者都是 repo 域会话凭据，
#   `src/api/auth_caps.CAPABILITIES` 里 repo_session 均为 True（agent_self 仅 tk_）。
#   因此 git-credential / workspace / git-repo 传输等 repo 域端点统一按
#   `has_capability(user, REPO_SESSION)` 判定，**禁止再硬编码 auth_type == 'session_token'**
#   （Issue #3620：该硬编码曾把 tko_ 挡在 git-credential 外）。tko_ 另做 repo_key 仓级校验。
# - session_key = 'takeover:<task_id>'：独立命名空间 —— cancel 时服务的
#   revoke_session_token(task_id) 只命中 worker 行，不会把接管 token 缩短到 180 秒。
# - 作用域与任务同 repo/agent；TTL 12h，可续到 24h；finish/abort 立即 DELETE。

def takeover_session_key(task_id: str) -> str:
    """接管 token 的 session_key（独立命名空间，勿与 worker 的 task_id 混用）。"""
    tid = str(task_id or "")
    if tid.startswith(TAKEOVER_SESSION_KEY_PREFIX):
        return tid
    return f"{TAKEOVER_SESSION_KEY_PREFIX}{tid}"


def issue_takeover_token(*, task_id: str, repo_key: str, agent_type: str,
                         user_id: int = 0, ttl: int = TAKEOVER_TOKEN_TTL) -> dict | None:
    """签发接管 token，返回 {token, token_sha256, session_key, expires_at}。

    明文仅此一次返回；库内只存 sha256。失败返回 None（调用方应回滚整个 start 事务）。
    """
    token = TAKEOVER_TOKEN_PREFIX + secrets.token_urlsafe(32)
    key = takeover_session_key(task_id)
    expires_at = _now() + int(ttl)
    try:
        _tokens.session_token_insert(key, repo_key or "", agent_type or "", str(user_id or 0),
                                     _sha256(token), expires_at, kind="takeover")
        return {"token": token, "token_sha256": _sha256(token),
                "session_key": key, "expires_at": expires_at}
    except Exception as e:
        log.warning(f"Issue takeover token failed (task={task_id}): {e}")
        return None


def verify_takeover_token(token: str) -> dict | None:
    """校验接管 token：sha256 + kind='takeover' + 未过期。"""
    if not token.startswith(TAKEOVER_TOKEN_PREFIX):
        return None
    try:
        row = _tokens.session_token_by_sha(_sha256(token), kind="takeover")
        if not row or int(row[5] or 0) <= _now():
            return None
        return {
            "session_token_id": row[0],
            "session_key": row[1],
            "repo_key": row[2],
            "agent_type": row[3],
            "user_id": row[4],
            "kind": "takeover",
        }
    except Exception as e:
        log.warning(f"Verify takeover token failed: {e}")
        return None


def revoke_takeover_token(task_id_or_key: str) -> bool:
    """立即吊销接管 token（finish / abort 用）：DELETE，不留宽限期。"""
    key = takeover_session_key(task_id_or_key)
    try:
        return _tokens.session_token_delete_by_session(key, kind="takeover") > 0
    except Exception as e:
        log.warning(f"Revoke takeover token failed (key={key}): {e}")
        return False


def renew_takeover_token(task_id_or_key: str, new_expires_at: int) -> bool:
    """续期（由调用方按 issued_at + 上限算好目标时间，见设计文档 §2.5）。"""
    key = takeover_session_key(task_id_or_key)
    try:
        return _tokens.session_token_renew(key, "takeover", int(new_expires_at)) > 0
    except Exception as e:
        log.warning(f"Renew takeover token failed (key={key}): {e}")
        return False


# ===================== 方式 B：用户级长期 API Key =====================

def generate_api_token(user_id: int, name: str = "") -> dict | None:
    """生成用户级 API Key，返回 {token(明文一次), id}；None 表示失败。

    明文仅创建返回一次；落库 sha256；同名（user_id, name）冲突时返回 None。
    """
    token = API_KEY_PREFIX + secrets.token_urlsafe(40)
    name = (name or "").strip()
    try:
        # created_at 用 UTC ISO（与其它表时间列一致，settings.py 经 normalize_event_time
        # 归一后给前端 +00:00 口径）；last_used_at 仍走 _now() 的秒级时间戳
        _new_id = _tokens.api_token_insert(user_id, _sha256(token), name, _utc_iso())
        return {"token": token, "id": _new_id}
    except Exception as e:
        log.warning(f"Generate api token failed: {e}")
        return None


def verify_api_token(token: str) -> dict | None:
    """校验用户级 API Key：sha256 查表 → 未吊销即有效。更新 last_used_at。"""
    if not token.startswith(API_KEY_PREFIX):
        return None
    try:
        row = _tokens.api_token_by_sha(_sha256(token))
        if not row or row[4] != 0:
            return None
        try:
            _tokens.api_token_touch(row[0], str(_now()))
        except Exception:
            pass
        return {"api_token_id": row[0], "user_id": row[1], "name": row[2], "scopes": row[3]}
    except Exception as e:
        log.warning(f"Verify api token failed: {e}")
        return None


def revoke_api_token(user_id: int, token_id: int) -> bool:
    """吊销用户级 API Key（软删：revoked=1，保审计）。"""
    try:
        return _tokens.api_token_revoke(token_id, user_id) > 0
    except Exception as e:
        log.warning(f"Revoke api token failed: {e}")
        return False


def list_api_tokens(user_id: int) -> list[dict]:
    """列出用户全部未吊销 API Key（不含明文/sha256）。"""
    try:
        rows = _tokens.api_token_list(user_id)
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
