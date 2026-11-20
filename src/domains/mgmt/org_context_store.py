"""组织上下文（brief/policy/decision）领域逻辑 + 发布安全工具（Issue #2150）。

存储：正文直接落 DB 列 `content`（上下文片段，非知识库正文）。
状态流转：draft → active → deprecated。发布时事务内降级同 (org, kind, ctx_key)
的旧 active 行，保证「每 org 每 kind 每 key 仅 1 条 active」，非 409。

命名与 `src/api/org_context.py`（请求级组织解析）刻意区分。
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import time
import uuid

from fastapi import HTTPException

from src.kernel.config import log
from src.kernel.session_manager import db, db_lock

VALID_CTX_KINDS = ("brief", "policy", "decision")
VALID_LIFECYCLE = ("draft", "active", "deprecated")

# 发布串行化：同 (org, kind, key) 的多进程并发发布必须串行，否则可能同时产生多条 active。
# rationale（数值登记 docs/org-pipeline/org-runtime-budget.md §2）：发布正文上限 5MB，
# 单次发布含一次版本查询 + 一次降级 UPDATE + 一次置顶 UPDATE，均走 (org,kind,ctx_key)
# 索引；10s 覆盖慢盘/锁等待的最坏路径，超时即失败而不是把请求无限挂住。
_PUBLISH_LOCK_TIMEOUT = 10  # rationale: docs/org-pipeline/org-runtime-budget.md §2


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


# ---- 发布安全：正文上限 + 明显凭据扫描 ----

# 明显凭据模式：token/AK-SK 类 + 私钥块 + 通用敏感赋值。命中即拒绝发布。
_SENSITIVE_PATTERNS = (
    ("private_key_block",
     re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{15,}\b")),
    ("generic_secret",
     re.compile(
         r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key)\b"
         r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+.]{8,}")),
)


def scan_sensitive_content(text: str) -> list[str]:
    """返回命中的明显凭据模式名列表（空列表=无命中）。不返回正文，避免二次泄漏。"""
    if not text:
        return []
    hits: list[str] = []
    for name, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            hits.append(name)
    return hits


def content_limit_bytes() -> int:
    """复用 `routes/knowledge.py` 的 MAX_CONTENT_BYTES（默认 5MB、下限 1KB）。

    调用时读取模块属性，便于测试 monkeypatch（也随环境变量在导入时解析）。
    """
    from src.domains.mgmt.routes import knowledge as _kr

    return int(getattr(_kr, "MAX_CONTENT_BYTES", 5 * 1024 * 1024))


def enforce_content_limit(content: str) -> None:
    """正文超限 → 400 CONTENT_TOO_LARGE（沿用知识库正文上限口径）。"""
    limit = content_limit_bytes()
    if len((content or "").encode("utf-8")) > limit:
        from src.domains.mgmt.routes import knowledge as _kr

        raise HTTPException(
            status_code=400,
            detail={
                "code": 400,
                "error_code": "CONTENT_TOO_LARGE",
                "message": (f"正文不能超过 {_kr._format_content_limit(limit)}"
                            f"{_kr._CONTENT_LIMIT_CONFIG_HINT}"),
            },
        )


def ensure_publishable(*texts: str) -> None:
    """发布前扫描：任一展示/正文字段命中明显凭据 → 400 SENSITIVE_CONTENT_REJECTED。"""
    hits: list[str] = []
    for text in texts:
        for name in scan_sensitive_content(text or ""):
            if name not in hits:
                hits.append(name)
    if hits:
        raise HTTPException(
            status_code=400,
            detail={
                "code": 400,
                "error_code": "SENSITIVE_CONTENT_REJECTED",
                "message": f"内容疑似包含明显凭据（{', '.join(hits)}），拒绝发布",
            },
        )


# ---- 发布串行化守卫 ----


_LOCK_TABLE_TAGS = {
    "org_context": "ctx",
    "agent_shared_knowledge": "kn",
}


def _publish_lock_name(table: str, org_id: str, group: str) -> str:
    """MySQL 命名锁名（≤64 字符上限）：短表标签 + org + 组键短哈希。"""
    digest = hashlib.sha1((group or "").encode("utf-8")).hexdigest()[:16]
    tag = _LOCK_TABLE_TAGS.get(table, table[:3])
    return f"gop:{tag}:{str(org_id)}:{digest}"


@contextlib.contextmanager
def publish_guard(table: str, org_id: str, group: str):
    """跨进程串行化发布（同 table + org + group）。

    `SELECT ... FOR UPDATE` 在本仓库不可用：mysql_compat 对只读语句执行后自动
    commit（src/kernel/mysql_compat.py），事务内行锁会被立即释放，多进程并发发布
    仍可各自成功 → 同 key 多条 active。改为 MySQL 命名锁 GET_LOCK：
    - 锁是**连接级**而非事务级，不受只读语句自动 commit 影响；
    - 获取/释放必须同一条物理连接，故用池外专用连接（`dedicated_connection`），
      with 结束即断开，命名锁随之释放（防异常路径锁泄漏，见 db_setup 迁移锁注释）。
    SQLite 为单进程部署（db_lock 串行）且无命名锁，直接放行。
    """
    lock_name = _publish_lock_name(table, org_id, group)
    from src.kernel.session_manager import _is_mysql_backend

    dedicated = getattr(db, "dedicated_connection", None) if _is_mysql_backend() else None
    if dedicated is None:
        yield lock_name
        return

    conn = None
    try:
        conn = dedicated()
        cur = conn.cursor()
        cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, _PUBLISH_LOCK_TIMEOUT))
        row = cur.fetchone()
        if not row or int(row[0] or 0) != 1:
            raise HTTPException(status_code=503, detail={
                "code": 503, "error_code": "PUBLISH_BUSY",
                "message": "该组织同类内容正在发布中，请稍后重试"})
        yield lock_name
    finally:
        if conn is not None:
            try:
                cur = conn.cursor()
                cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                cur.fetchone()
            except Exception as e:  # RELEASE 失败不阻断；close 断开连接也会释放
                log.warning(f"[org_publish] RELEASE_LOCK failed: {e}")
            conn.close()


# ---- DB 访问 ----

_CTX_COLS = (
    "id", "org_id", "kind", "ctx_key", "title", "content", "lifecycle_status",
    "version", "owner_id", "valid_until", "created_by", "updated_by",
    "created_at", "updated_at",
)


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return {col: row[i] for i, col in enumerate(_CTX_COLS)}


def normalize_ctx_key(kind: str, ctx_key: str) -> str:
    """brief 强制单 key（仅 1 条 active）；policy/decision 用传入 key，缺省 default。"""
    if kind == "brief":
        return "brief"
    key = (ctx_key or "").strip()
    return key or "default"


def _schedule_repo_sync(org_id: str) -> None:
    try:
        from src.domains.mgmt.org_context_repo import schedule_sync
        schedule_sync(org_id)
    except Exception as e:
        log.warning(f"schedule org-context repo sync failed org={org_id}: {e}")


def upsert_context(org_id: str, kind: str, ctx_key: str, title: str, content: str,
                   *, owner_id: str = "", user_id: str = "") -> dict | None:
    """幂等写入/更新并发布一条组织上下文（系统自动沉淀用，Issue #2269）。

    - brief 走单 key（normalize 后固定 brief），重复调用只更新当前 draft/active；
    - decision/policy 用传入 key（如 dispatch-5），同 key 更新后重新发布；
    - 任何失败由调用方兜住：自动沉淀不能影响主流程。
    """
    key = normalize_ctx_key(kind, ctx_key)
    rows = find_contexts_by_key(org_id, kind, key)
    row = next((r for r in rows if r.get("ctx_key") == key
                and r.get("lifecycle_status") in ("active", "draft")), None)
    if row is None:
        row = next((r for r in rows if r.get("ctx_key") == key), None)
    if row:
        cid = str(row["id"] or "")
        if (row.get("title") or "") != title or (row.get("content") or "") != content:
            row = update_context(org_id, cid, title=title, content=content, user_id=user_id) or row
        if (row.get("lifecycle_status") or "") != "active":
            row = publish_context(org_id, cid, user_id=user_id) or row
        _schedule_repo_sync(org_id)
        return row
    created = create_context(org_id, kind, key, title, content,
                             owner_id=owner_id, user_id=user_id)
    if created and created.get("id"):
        created = publish_context(org_id, str(created["id"] or ""), user_id=user_id) or created
    _schedule_repo_sync(org_id)
    return created


def _select_sql(where: str) -> str:
    # 注意：不要用 SELECT ... FOR UPDATE 做发布锁——mysql_compat 对只读语句会自动
    # commit（事务内行锁立即释放），发布串行化改由 publish_guard 的命名锁负责。
    return f"SELECT {', '.join(_CTX_COLS)} FROM grape_t_org_context WHERE {where}"


def list_contexts(org_id: str, kind: str = "", status: str = "",
                  limit: int = 0, offset: int = 0) -> list[dict]:
    """列出组织上下文；limit=0 表示内部调用不限制（API 必须传上限）。"""
    clauses = ["org_id=?"]
    params: list = [org_id]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if status:
        clauses.append("lifecycle_status=?")
        params.append(status)
    sql = _select_sql(" AND ".join(clauses)) + " ORDER BY created_at DESC, version DESC"
    if limit and int(limit) > 0:
        sql += " LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset or 0))])
    with db_lock:
        rows = db.cursor().execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_contexts_by_key(org_id: str, kind: str, ctx_key: str) -> list[dict]:
    """按 (org, kind, key) 精确取版本行（避免 upsert 时全量扫描历史）。"""
    with db_lock:
        rows = db.cursor().execute(
            _select_sql("org_id=? AND kind=? AND ctx_key=?") + " ORDER BY version DESC, created_at DESC",
            (org_id, kind, ctx_key),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_context(org_id: str, ctx_id: str) -> dict | None:
    with db_lock:
        row = db.cursor().execute(
            _select_sql("org_id=? AND id=?"), (org_id, ctx_id)
        ).fetchone()
    return _row_to_dict(row) if row else None


def create_context(org_id: str, kind: str, ctx_key: str, title: str, content: str,
                   owner_id: str = "", valid_until: str = "", user_id: str = "") -> dict:
    if kind not in VALID_CTX_KINDS:
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "INVALID_CONTEXT_KIND",
            "message": f"kind 必须是 {list(VALID_CTX_KINDS)} 之一"})
    enforce_content_limit(content)
    key = normalize_ctx_key(kind, ctx_key)
    now = _now()
    with db_lock:
        c = db.cursor()
        version = c.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM grape_t_org_context "
            "WHERE org_id=? AND kind=? AND ctx_key=?",
            (org_id, kind, key),
        ).fetchone()[0]
        row_id = uuid.uuid4().hex
        c.execute(
            "INSERT INTO grape_t_org_context "
            "(id, org_id, kind, ctx_key, title, content, lifecycle_status, version, "
            "owner_id, valid_until, created_by, updated_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?, 'draft', ?,?,?,?,?,?,?)",
            (row_id, org_id, kind, key, title or "", content or "", version,
             str(owner_id or ""), valid_until or "", str(user_id or ""),
             str(user_id or ""), now, now),
        )
        cid = str(row_id)
        db.commit()
    return get_context(org_id, cid)


def update_context(org_id: str, ctx_id: str, *, title: str | None = None,
                   content: str | None = None, owner_id: str | None = None,
                   valid_until: str | None = None, user_id: str = "") -> dict | None:
    row = get_context(org_id, ctx_id)
    if not row:
        return None
    if row["lifecycle_status"] == "deprecated":
        raise HTTPException(status_code=409, detail={
            "code": 409, "error_code": "INVALID_STATUS_TRANSITION",
            "message": "已废弃的上下文不可再更新"})
    if content is not None:
        enforce_content_limit(content)
        # 已 active 的条目改正文等价于绕过发布扫描，故同步校验
        if row["lifecycle_status"] == "active":
            ensure_publishable(content)
    sets = []
    params: list = []
    if title is not None:
        sets.append("title=?"); params.append(title or "")
    if content is not None:
        sets.append("content=?"); params.append(content or "")
    if owner_id is not None:
        sets.append("owner_id=?"); params.append(str(owner_id or ""))
    if valid_until is not None:
        sets.append("valid_until=?"); params.append(valid_until or "")
    sets.append("updated_by=?"); params.append(str(user_id or ""))
    sets.append("updated_at=?"); params.append(_now())
    params.extend([org_id, ctx_id])
    with db_lock:
        db.cursor().execute(
            f"UPDATE grape_t_org_context SET {', '.join(sets)} WHERE org_id=? AND id=?",
            params,
        )
        db.commit()
    return get_context(org_id, ctx_id)


def _assert_publishable_status(status: str) -> None:
    """发布只允许 draft/active；deprecated/archived 为终态，禁止复活（防绕过 update 的 409）。"""
    if status in ("deprecated", "archived"):
        raise HTTPException(status_code=409, detail={
            "code": 409, "error_code": "INVALID_STATUS_TRANSITION",
            "message": f"状态为 {status} 的内容不可再发布"})


def publish_context(org_id: str, ctx_id: str, user_id: str = "") -> dict | None:
    row = get_context(org_id, ctx_id)
    if not row:
        return None
    _assert_publishable_status(row["lifecycle_status"])

    # 先拿跨进程串行化锁，再在锁内复查状态（拿锁前可能已被并发降级/删除）
    with publish_guard("org_context", org_id, f"{row['kind']}\x00{row['ctx_key']}"):
        with db_lock:
            c = db.cursor()
            current = c.execute(
                _select_sql("org_id=? AND id=?"), (org_id, ctx_id)).fetchone()
            if not current:
                return None
            ctx = _row_to_dict(current)
            _assert_publishable_status(ctx["lifecycle_status"])
            ensure_publishable(ctx.get("title", ""), ctx["content"])
            now = _now()
            # 锁内先降级同 key 的旧 active，再置顶本条（非 409）
            c.execute(
                "UPDATE grape_t_org_context SET lifecycle_status='deprecated', updated_at=? "
                "WHERE org_id=? AND kind=? AND ctx_key=? AND lifecycle_status='active' AND id<>?",
                (now, org_id, ctx["kind"], ctx["ctx_key"], ctx_id),
            )
            c.execute(
                "UPDATE grape_t_org_context SET lifecycle_status='active', updated_by=?, updated_at=? "
                "WHERE id=?",
                (str(user_id or ""), now, ctx_id),
            )
            db.commit()
    return get_context(org_id, ctx_id)


def deprecate_context(org_id: str, ctx_id: str, user_id: str = "") -> dict | None:
    row = get_context(org_id, ctx_id)
    if not row:
        return None
    with db_lock:
        db.cursor().execute(
            "UPDATE grape_t_org_context SET lifecycle_status='deprecated', updated_by=?, "
            "updated_at=? WHERE org_id=? AND id=?",
            (str(user_id or ""), _now(), org_id, ctx_id),
        )
        db.commit()
    return get_context(org_id, ctx_id)


def delete_context(org_id: str, ctx_id: str) -> bool:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_org_context WHERE org_id=? AND id=?", (org_id, ctx_id))
        deleted = c.rowcount > 0
        db.commit()
    return deleted
