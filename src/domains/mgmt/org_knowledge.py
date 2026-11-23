"""组织知识库领域逻辑（Issue #2150）。

复用既有 `agent_shared_knowledge` 表：正文沿用 file_path/obs_key 索引
（正文落工作区文件/OBS，DB 仅索引），`repo_key` = 组织仓库；新增 lifecycle_status
等扩展列承载组织知识与 draft/active/deprecated 生命周期，不改既有 status/archived。
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid

from fastapi import HTTPException

from src.kernel.config import WORKSPACE_DIR, log
from src.kernel.repo_key import knowledge_obs_prefix, workspace_repo_dir
from src.kernel.session_manager import db, db_lock
from src.domains.runtime import public as runtime_public

from src.domains.mgmt.org_context_store import (
    enforce_content_limit,
    ensure_publishable,
    publish_guard,
)

VALID_KNOWLEDGE_STATUSES = ("draft", "active", "deprecated", "archived")


def _max_name_len() -> int:
    """复用旧知识接口的 MAX_NAME_LEN（默认 50），防 DB VARCHAR 1406 → 500。"""
    from src.domains.mgmt.routes import knowledge as _kr

    return int(getattr(_kr, "MAX_NAME_LEN", 50))


def _assert_name_len(name: str) -> None:
    limit = _max_name_len()
    if len((name or "").strip()) > limit:
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "INVALID_NAME",
            "message": f"name 不能超过 {limit} 字符"})


def _assert_publishable_status(status: str) -> None:
    """发布只允许 draft/active；deprecated/archived 为终态（撤回内容不可复活）。"""
    if status in ("deprecated", "archived"):
        raise HTTPException(status_code=409, detail={
            "code": 409, "error_code": "INVALID_STATUS_TRANSITION",
            "message": f"状态为 {status} 的知识不可再发布"})

_KN_COLS = (
    "id", "org_id", "kind", "name", "description", "tags", "applies_to",
    "valid_until", "review_interval_days", "lifecycle_status", "version",
    "owner_id", "last_reviewed_at", "repo_key", "file_path", "obs_key",
    "content_hash", "created_by", "source", "created_at", "updated_at",
)
_KN_SELECT = ", ".join(_KN_COLS)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _parse_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    data = {col: row[i] for i, col in enumerate(_KN_COLS)}
    data["tags"] = _parse_tags(data.get("tags") or "")
    return data


def read_knowledge_content(row: dict) -> str:
    """读取知识正文（OBS 优先，本地文件兜底）。"""
    from src.api.knowledge import read_entry_content

    return read_entry_content(
        row.get("repo_key", ""), row.get("obs_key", ""), row.get("file_path", ""),
        row.get("content_hash", ""),
    )


def _resolve_repo_key(org: dict, org_id: str) -> str:
    """组织知识正文落组织仓库；未 provisioning 时用合成前缀兜底，避免路径逃逸。"""
    key = (org.get("org_repo_key") or "").strip()
    return key or f"gitcode:org-{org_id}/context"


def _write_content(org_id: str, repo_key: str, content: str) -> tuple[str, str, str]:
    """把正文写工作区文件（可选同步 OBS），返回 (file_path, obs_key, content_hash)。"""
    from src.kernel.localfs import write_text as _atomic_write

    base = workspace_repo_dir(repo_key)
    filename = f"entry_{int(time.time())}_{uuid.uuid4().hex[:6]}.md"
    file_path = os.path.join(base, "project-info", "org-knowledge", filename)
    _atomic_write(file_path, content.strip())
    rel = os.path.relpath(file_path, WORKSPACE_DIR)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    obs_key = ""
    try:
        obs_key = f"{knowledge_obs_prefix(repo_key)}/org-knowledge/{filename}"
        with open(file_path, "rb") as f:
            if not runtime_public.storage_save_bytes(obs_key, f.read()):
                obs_key = ""
    except Exception as e:  # OBS 失败降级本地文件，不影响落库
        log.warning(f"[org_knowledge] OBS save failed: {e}")
    return rel, obs_key, content_hash


def list_knowledge(org_id: str, *, kind: str = "", tag: str = "", q: str = "",
                   status: str = "", limit: int = 0, offset: int = 0) -> list[dict]:
    """列出组织知识；limit=0 表示内部调用不限制（API 必须传上限）。"""
    clauses = ["org_id=?"]
    params: list = [org_id]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if status:
        clauses.append("lifecycle_status=?")
        params.append(status)
    sql = (f"SELECT {_KN_SELECT} FROM agent_shared_knowledge WHERE "
           + " AND ".join(clauses) + " ORDER BY id DESC")
    # tag/q 是内存内精确过滤（tag 需按逗号项精确命中），不能先 SQL LIMIT
    # 再过滤，否则会漏掉较早的匹配项；无过滤时由 SQL 直接收敛上限。
    sql_limit = bool(limit and int(limit) > 0 and not tag and not q)
    if sql_limit:
        sql += " LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset or 0))])
    with db_lock:
        rows = db.cursor().execute(sql, params).fetchall()
    items = [_row_to_dict(r) for r in rows]
    if tag:
        needle = tag.strip().lower()
        items = [x for x in items if needle in [t.lower() for t in x["tags"]]]
    if q:
        needle = q.strip().lower()
        def _hit(x: dict) -> bool:
            blob = " ".join([
                str(x.get("name") or ""), str(x.get("description") or ""),
                str(x.get("kind") or ""), ",".join(x.get("tags") or []),
            ]).lower()
            return needle in blob
        items = [x for x in items if _hit(x)]
    if limit and int(limit) > 0 and not sql_limit:
        start = max(0, int(offset or 0))
        items = items[start:start + max(1, int(limit))]
    return items


def find_knowledge_by_name(org_id: str, kind: str, name: str) -> list[dict]:
    """按 (org, kind, name) 精确取版本行（避免 upsert 时全量扫描历史）。"""
    with db_lock:
        rows = db.cursor().execute(
            f"SELECT {_KN_SELECT} FROM agent_shared_knowledge "
            "WHERE org_id=? AND kind=? AND name=? ORDER BY id DESC",
            (org_id, kind or "", name),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_knowledge(org_id: str, knowledge_id: int) -> dict | None:
    with db_lock:
        row = db.cursor().execute(
            f"SELECT {_KN_SELECT} FROM agent_shared_knowledge WHERE org_id=? AND id=?",
            (org_id, knowledge_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def create_knowledge(org_id: str, repo_key: str, *, kind: str, name: str,
                     description: str, content: str, tags: list[str],
                     applies_to: str = "", valid_until: str = "",
                     review_interval_days: int = 0, owner_id: str = "",
                     created_by: str = "", source: str = "manual") -> dict:
    if not (name or "").strip():
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "INVALID_NAME", "message": "name 必填"})
    _assert_name_len(name)
    if not (content or "").strip():
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "INVALID_CONTENT", "message": "content 必填"})
    enforce_content_limit(content)
    rel, obs_key, content_hash = _write_content(org_id, repo_key, content)
    tag_str = ",".join(t for t in (tags or []) if str(t).strip())
    now = _now()
    with db_lock:
        c = db.cursor()
        version = c.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM agent_shared_knowledge "
            "WHERE org_id=? AND kind=? AND name=?",
            (org_id, kind or "", name.strip()),
        ).fetchone()[0]
        c.execute(
            "INSERT INTO agent_shared_knowledge "
            "(scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, "
            "status, source_seq, created_by, source, description, name, "
            "lifecycle_status, org_id, kind, tags, applies_to, valid_until, "
            "review_interval_days, version, owner_id, created_at, updated_at) "
            "VALUES ('org',?, '', 0, ?, ?, ?, 'accepted', 0, ?, ?, ?, ?, "
            "'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_key, rel, obs_key, content_hash, created_by, source or "manual",
             description or "", name.strip(), org_id, kind or "", tag_str,
             applies_to or "", valid_until or "", int(review_interval_days or 0),
             version, str(owner_id or ""), now, now),
        )
        kid = c.lastrowid
        db.commit()
    return get_knowledge(org_id, kid)


def update_knowledge(org_id: str, knowledge_id: int, *, name: str | None = None,
                     description: str | None = None, content: str | None = None,
                     tags: list[str] | None = None, applies_to: str | None = None,
                     valid_until: str | None = None,
                     review_interval_days: int | None = None,
                     owner_id: str | None = None,
                     source: str | None = None) -> dict | None:
    row = get_knowledge(org_id, knowledge_id)
    if not row:
        return None
    if row["lifecycle_status"] in ("deprecated", "archived"):
        raise HTTPException(status_code=409, detail={
            "code": 409, "error_code": "INVALID_STATUS_TRANSITION",
            "message": f"状态为 {row['lifecycle_status']} 的知识不可再更新"})
    if name is not None:
        _assert_name_len(name)
    sets = []
    params: list = []
    if name is not None and name.strip():
        sets.append("name=?"); params.append(name.strip())
    if description is not None:
        sets.append("description=?"); params.append(description or "")
    if applies_to is not None:
        sets.append("applies_to=?"); params.append(applies_to or "")
    if valid_until is not None:
        sets.append("valid_until=?"); params.append(valid_until or "")
    if review_interval_days is not None:
        sets.append("review_interval_days=?"); params.append(int(review_interval_days or 0))
    if owner_id is not None:
        sets.append("owner_id=?"); params.append(str(owner_id or ""))
    if source is not None:
        sets.append("source=?"); params.append(source or "manual")
    if tags is not None:
        sets.append("tags=?"); params.append(",".join(t for t in tags if str(t).strip()))
    if content is not None:
        enforce_content_limit(content)
        if row["lifecycle_status"] == "active":
            ensure_publishable(content)
        rel, obs_key, content_hash = _write_content(org_id, row["repo_key"], content)
        sets.extend(["file_path=?", "obs_key=?", "content_hash=?"])
        params.extend([rel, obs_key, content_hash])
    if not sets:
        return row
    sets.append("updated_at=?"); params.append(_now())
    params.extend([org_id, knowledge_id])
    with db_lock:
        db.cursor().execute(
            f"UPDATE agent_shared_knowledge SET {', '.join(sets)} WHERE org_id=? AND id=?",
            params,
        )
        db.commit()
    return get_knowledge(org_id, knowledge_id)


def _schedule_repo_sync(org_id: str) -> None:
    try:
        from src.domains.mgmt.org_context_repo import schedule_sync
        schedule_sync(org_id)
    except Exception as e:
        log.warning(f"schedule org-context repo sync failed org={org_id}: {e}")


def upsert_knowledge(org_id: str, repo_key: str, *, kind: str, name: str,
                     content: str, description: str = "", tags: list[str] | None = None,
                     applies_to: str = "", owner_id: str = "",
                     created_by: str = "", source: str = "agent",
                     publish: bool = True) -> dict | None:
    """幂等写入/更新组织知识（系统自动沉淀用，Issue #2269）。

    publish=False 时保持 draft（Agent 自动沉淀先进待审核，§9.6 治理要求）；
    publish=True 时沿用旧行为，内容变化更新后发布。
    """
    rows = find_knowledge_by_name(org_id, kind, name)
    row = rows[0] if rows else None
    if row:
        kid = int(row["id"])
        try:
            current = read_knowledge_content(row) or ""
        except Exception:
            current = ""
        _content_changed = (current or "").strip() != (content or "").strip()
        _source_changed = bool(source and (row.get("source") or "") != source)
        if _content_changed or _source_changed:
            row = update_knowledge(org_id, kid, content=content,
                                   description=description,
                                   source=source) or row
        if publish and (row.get("lifecycle_status") or "") != "active":
            row = publish_knowledge(org_id, kid) or row
        _schedule_repo_sync(org_id)
        return row
    created = create_knowledge(
        org_id, repo_key, kind=kind, name=name, description=description,
        content=content, tags=tags or [], applies_to=applies_to,
        owner_id=owner_id, created_by=created_by, source=source)
    if publish and created and created.get("id"):
        created = publish_knowledge(org_id, int(created["id"])) or created
    _schedule_repo_sync(org_id)
    return created


def publish_knowledge(org_id: str, knowledge_id: int, reviewed_at: str = "") -> dict | None:
    item = get_knowledge(org_id, knowledge_id)
    if not item:
        return None
    _assert_publishable_status(item["lifecycle_status"])
    # 正文/OBS 读取是网络 IO：放在锁与 db_lock 之外，避免长时间占用全局锁（QA Optional）
    content = read_knowledge_content(item)
    ensure_publishable(item.get("name", ""), item.get("description", ""), content)

    # 跨进程串行化：同 (org, kind, name) 的并发发布依次执行，保证仅 1 条 active
    with publish_guard("agent_shared_knowledge", org_id,
                       f"{item.get('kind', '')}\x00{item.get('name', '')}"):
        with db_lock:
            c = db.cursor()
            row = c.execute(
                f"SELECT {_KN_SELECT} FROM agent_shared_knowledge WHERE org_id=? AND id=?",
                (org_id, knowledge_id),
            ).fetchone()
            if not row:
                return None
            item = _row_to_dict(row)
            _assert_publishable_status(item["lifecycle_status"])
            now = _now()
            # 同 (org, kind, name) 的旧 active 自动降级，非 409
            c.execute(
                "UPDATE agent_shared_knowledge SET lifecycle_status='deprecated', updated_at=? "
                "WHERE org_id=? AND kind=? AND name=? AND lifecycle_status='active' AND id<>?",
                (now, org_id, item["kind"], item["name"], knowledge_id),
            )
            c.execute(
                "UPDATE agent_shared_knowledge SET lifecycle_status='active', "
                "last_reviewed_at=?, updated_at=? WHERE id=?",
                (reviewed_at or now, now, knowledge_id),
            )
            db.commit()
    return get_knowledge(org_id, knowledge_id)


def archive_knowledge(org_id: str, knowledge_id: int) -> dict | None:
    """归档（终态）：Issue #2150 契约 status 含 archived。"""
    row = get_knowledge(org_id, knowledge_id)
    if not row:
        return None
    if row["lifecycle_status"] == "archived":
        return row
    if row["lifecycle_status"] == "deprecated":
        raise HTTPException(status_code=409, detail={
            "code": 409, "error_code": "INVALID_STATUS_TRANSITION",
            "message": "已废弃的知识不可归档"})
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_shared_knowledge SET lifecycle_status='archived', updated_at=? "
            "WHERE org_id=? AND id=?",
            (_now(), org_id, knowledge_id),
        )
        db.commit()
    return get_knowledge(org_id, knowledge_id)


def deprecate_knowledge(org_id: str, knowledge_id: int) -> dict | None:
    row = get_knowledge(org_id, knowledge_id)
    if not row:
        return None
    if row["lifecycle_status"] == "archived":
        raise HTTPException(status_code=409, detail={
            "code": 409, "error_code": "INVALID_STATUS_TRANSITION",
            "message": "已归档的知识不可废弃"})
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_shared_knowledge SET lifecycle_status='deprecated', updated_at=? "
            "WHERE org_id=? AND id=?",
            (_now(), org_id, knowledge_id),
        )
        db.commit()
    return get_knowledge(org_id, knowledge_id)


def delete_knowledge(org_id: str, knowledge_id: int) -> bool:
    row = get_knowledge(org_id, knowledge_id)
    if not row:
        return False
    # 清理正文：OBS 对象（对齐旧 _delete_knowledge_full）+ 本地文件；失败不阻断
    obs_key = row.get("obs_key") or ""
    if obs_key:
        try:
            runtime_public.storage_delete_object(obs_key)
        except Exception as e:
            log.warning(f"[org_knowledge] delete OBS object failed: {e}")
    try:
        from src.kernel.localfs import safe_workspace_path

        fp = row.get("file_path") or ""
        if fp:
            try:
                full = safe_workspace_path(fp)
            except ValueError:
                full = os.path.normpath(os.path.join(WORKSPACE_DIR, fp))
            if os.path.isfile(full):
                os.remove(full)
    except Exception as e:
        log.warning(f"[org_knowledge] delete file failed: {e}")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_shared_knowledge WHERE org_id=? AND id=?",
                  (org_id, knowledge_id))
        deleted = c.rowcount > 0
        db.commit()
    return deleted
