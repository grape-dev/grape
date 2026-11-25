"""组织上下文快照：组织级 L0/L1 + 目标仓库目录 + 需求 L3 + Top-K 知识。

设计口径（org-context-design §9.2/9.3/9.4）：
- 快照正文落在平台快照目录（WORKSPACE_DIR/context-snapshots/...）；
- `org_context_snapshot` 只存 manifest（路径/大小/哈希）与统计，不存正文；
- 下派时生成一次，同一批次子任务共用；
- 有预算、凭证过滤、按目标仓库选取，不注入其他仓库上下文。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from enum import IntEnum

from src.kernel.config import WORKSPACE_DIR, log
from src.kernel.issue_ident import native_issue_number


class DispatchContextStatus(IntEnum):
    """批次 context 读取的 HTTP 语义化结果（Issue #4352）。"""

    OK = 200
    FORBIDDEN = 403
    NOT_FOUND = 404

_SECTION_CAPS = {
    "l0": 8 * 1024,
    "l1": 32 * 1024,
    "l2": 64 * 1024,
    "l3": 32 * 1024,
    "knowledge": 32 * 1024,
}
_TOTAL_CAP = 256 * 1024
_FILE_CAP = 64 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default) or default))
    except Exception:
        return default


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _repo_path(repo_key: str) -> str:
    from src.kernel.repo_key import parse_repo_key

    provider, owner, name = parse_repo_key(repo_key)
    return f"org-context/repos/{provider}/{owner}/{name}/context.md"


def _select_files(files: dict[str, str], targets: list[str],
                  org_issue_number) -> list[tuple[str, str, str]]:
    """按 L0/L1/L2/L3/知识顺序选取，返回 [(section, rel, content)]。"""
    selected: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def _add(section: str, rel: str) -> None:
        if rel in seen or rel not in files:
            return
        seen.add(rel)
        selected.append((section, rel, files[rel]))

    # L0：组织硬规则总是注入
    _add("l0", "org-context/rules.md")
    # L1：组织 overview / profile / standards
    for rel in ("org-context/profile.md", "org-context/architecture.md"):
        _add("l1", rel)
    for rel in sorted(files):
        if rel.startswith("org-context/standards/"):
            _add("l1", rel)
    # L2：仅目标仓库目录条目，不注入其他仓库
    for repo_key in targets:
        if repo_key:
            _add("l2", _repo_path(repo_key))
    # L3：本次需求的 brief / dispatch-plan / acceptance
    prefix = f"org-context/demands/{native_issue_number(org_issue_number)}/"
    for rel in sorted(files):
        if rel.startswith(prefix):
            _add("l3", rel)
    # Top-K 组织知识：按文件名字典序稳定选取，受预算限制
    for rel in sorted(files):
        if rel.startswith("org-context/knowledge/"):
            _add("knowledge", rel)
    return selected


def _filter_credentials(files: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    try:
        from src.api.knowledge import _is_credential_file

        return [item for item in files if not _is_credential_file(item[1])]
    except Exception:
        return files


def _cap_files(files: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    used: dict[str, int] = {}
    total = 0
    for section, rel, content in files:
        data = str(content or "")
        size = len(data.encode("utf-8"))
        section_cap = _SECTION_CAPS.get(section, _FILE_CAP)
        truncated_file = False
        if size > min(section_cap, _FILE_CAP):
            data = data.encode("utf-8")[:min(section_cap, _FILE_CAP)].decode(
                "utf-8", "ignore") + "\n\n> （快照超预算，已截断）\n"
            size = len(data.encode("utf-8"))
            truncated_file = True
        if used.get(section, 0) + size > section_cap and not truncated_file:
            continue
        if total + size > _TOTAL_CAP:
            break
        used[section] = used.get(section, 0) + size
        total += size
        out.append((section, rel, data))
    return out


def _snapshot_dir(org_id: str, demand_id: int, dispatch_id: str) -> str:
    return os.path.join(
        WORKSPACE_DIR, "context-snapshots", f"org-{org_id}",
        f"demand-{native_issue_number(demand_id)}", f"batch-{dispatch_id}")


def build_org_snapshot(org_id: str, dispatch: dict, *, force: bool = False) -> str:
    """为一次下派构建 (或复用) 组织上下文快照，返回 snapshot id。"""
    from src.domains.mgmt import org_context_repo as repo_mod
    from src.kernel.session_manager import db, db_lock

    org_id = str(org_id or "")
    dispatch_id = str((dispatch or {}).get("id") or "")
    org_issue = native_issue_number((dispatch or {}).get("org_issue_number"))
    if not org_id or not dispatch_id:
        return ""
    with db_lock:
        c = db.cursor()
        if not force:
            row = c.execute(
                "SELECT id FROM grape_t_org_context_snapshot WHERE org_id=? AND dispatch_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (org_id, dispatch_id)).fetchone()
            if row:
                return str(row[0] or "")
    files = repo_mod._render_files(org_id)
    targets = [str(t) for t in ((dispatch or {}).get("targets_requested") or [])]
    if not targets:
        try:
            from src.domains.mgmt.user.org_db import list_org_repos
            targets = [r.get("repo_key") or "" for r in list_org_repos(org_id)]
        except Exception:
            targets = []
    selected = _cap_files(_filter_credentials(_select_files(files, targets, org_issue)))
    if not selected:
        return ""

    rel_dir = os.path.join(
        "context-snapshots", f"org-{org_id}", f"demand-{org_issue}",
        f"batch-{dispatch_id}")
    abs_dir = os.path.join(WORKSPACE_DIR, rel_dir)
    files_dir = os.path.join(abs_dir, "files")
    os.makedirs(files_dir, exist_ok=True)
    manifest: dict[str, dict] = {}
    for section, rel, content in selected:
        target = os.path.join(files_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        manifest[rel] = {
            "section": section,
            "size": len(content.encode("utf-8")),
            "hash": _sha(content),
            "storage": os.path.relpath(target, abs_dir),
        }
    manifest_path = os.path.join(abs_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    content_hash = _sha(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    total_bytes = sum(v["size"] for v in manifest.values())
    with db_lock:
        c = db.cursor()
        if force:
            c.execute(
                "DELETE FROM grape_t_org_context_snapshot WHERE org_id=? AND dispatch_id=?",
                (org_id, dispatch_id))
        _snap_id = uuid.uuid4().hex
        c.execute(
            "INSERT INTO grape_t_org_context_snapshot "
            "(id, org_id, demand_id, dispatch_id, storage_path, files_json, "
            "content_hash, total_bytes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (_snap_id, org_id, org_issue, dispatch_id, rel_dir,
             json.dumps(manifest, ensure_ascii=False, sort_keys=True),
             content_hash, total_bytes, time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        snap_id = _snap_id
        db.commit()
    log.info(f"org context snapshot built id={snap_id} org={org_id} "
             f"dispatch={dispatch_id} files={len(manifest)} bytes={total_bytes}")
    return snap_id


def get_snapshot(snapshot_id: str) -> dict:
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        row = db.cursor().execute(
            "SELECT id, org_id, demand_id, dispatch_id, storage_path, files_json, "
            "content_hash, total_bytes, created_at FROM grape_t_org_context_snapshot WHERE id=?",
            (str(snapshot_id or ""),)).fetchone()
    if not row:
        return {}
    try:
        manifest = json.loads(row[5] or "{}")
    except Exception:
        manifest = {}
    return {
        "id": str(row[0] or ""),
        "org_id": str(row[1] or ""),
        "demand_id": int(row[2] or 0),
        "dispatch_id": str(row[3] or ""),
        "storage_path": str(row[4] or ""),
        "manifest": manifest,
        "content_hash": str(row[6] or ""),
        "total_bytes": int(row[7] or 0),
        "created_at": str(row[8] or ""),
    }


def load_snapshot_files(snapshot_id: str) -> dict[str, str]:
    snap = get_snapshot(snapshot_id)
    if not snap:
        return {}
    base = os.path.join(WORKSPACE_DIR, snap["storage_path"])
    files: dict[str, str] = {}
    for rel, meta in (snap.get("manifest") or {}).items():
        storage = (meta or {}).get("storage") or ""
        if not storage:
            continue
        path = os.path.join(base, storage)
        try:
            with open(path, "r", encoding="utf-8") as f:
                files[rel] = f.read()
        except Exception as e:
            log.warning(f"org snapshot read failed id={snapshot_id} rel={rel}: {e}")
    return files


def _dispatch_visible_org_ids(dispatch: dict, targets: list[dict]) -> list[str]:
    """批次可见组织集合：dispatch 归属组织 + 各目标仓库（子 Issue）当前所属组织。

    子 Issue 的 ``issue.org_id`` 来自 ``managed_repos.org_id``；仓库跨组织重绑定后，
    子 Issue 正文内嵌的是新的组织，而批次仍记在下派时的组织下。两者都应能读取同一
    批次快照，避免把「跨组织可见」误判为「资源不存在」。
    """
    orgs: list[str] = []
    dispatch_org = str((dispatch or {}).get("org_id") or "")
    if dispatch_org:
        orgs.append(dispatch_org)
    try:
        from src.domains.mgmt.user.org_db import get_repo_org_id
    except Exception:  # noqa: BLE001 - 解析失败仅按 dispatch 组织判定可见性
        get_repo_org_id = None  # type: ignore[assignment]
    for target in targets or []:
        repo_key = str((target or {}).get("repo_key") or "")
        if not repo_key or get_repo_org_id is None:
            continue
        try:
            org_id = str(get_repo_org_id(repo_key) or "")
        except Exception:  # noqa: BLE001 - 单仓解析失败不阻断其它目标
            continue
        if org_id and org_id not in orgs:
            orgs.append(org_id)
    return orgs


def _load_dispatch_bundle(request_org_id: str, dispatch: dict, targets: list[dict],
                          *, snapshot_org: str = "") -> dict:
    """按 (快照归属组织, dispatch_id) 读取或重建批次快照，返回 CLI/API 出参。

    ``request_org_id`` 仅用于回显调用组织；``snapshot_org`` 决定快照取数/重建的组织，
    缺省取批次归属组织。
    """
    from src.kernel.session_manager import db, db_lock

    request_org_id = str(request_org_id or "")
    dispatch_id = str((dispatch or {}).get("id") or "")
    snapshot_org = str(snapshot_org or (dispatch or {}).get("org_id") or "")
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id FROM grape_t_org_context_snapshot WHERE org_id=? AND dispatch_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (snapshot_org, dispatch_id)).fetchone()
    snapshot_id = str(row[0] or "") if row else ""
    if not snapshot_id:
        payload = dict(dispatch)
        payload["targets_requested"] = [
            t.get("repo_key") for t in targets if t.get("repo_key")]
        snapshot_id = build_org_snapshot(snapshot_org, payload)
    snap = get_snapshot(snapshot_id) if snapshot_id else {}
    files = load_snapshot_files(snapshot_id) if snapshot_id else {}
    # 快照对执行侧是不可变口径；CLI 人工读取时叠加当前 L3 需求文件，
    # 让验收后能看到最新 acceptance，而不是下派瞬间的空文件。
    try:
        from src.domains.mgmt import org_context_repo as repo_mod

        _prefix = f"org-context/demands/{native_issue_number(dispatch.get('org_issue_number'))}/"
        for _rel, _content in (repo_mod._render_files(snapshot_org) or {}).items():
            if str(_rel).startswith(_prefix):
                files[_rel] = _content
    except Exception as _e:  # noqa: BLE001 - 叠加失败时保留快照原文
        log.warning(f"overlay current demand files failed dispatch={dispatch_id}: {_e}")
    return {
        "org_id": request_org_id,
        "dispatch_org_id": str(dispatch.get("org_id") or ""),
        "snapshot_org_id": snapshot_org,
        "dispatch_id": dispatch_id,
        "org_issue_number": native_issue_number(dispatch.get("org_issue_number")),
        "status": dispatch.get("status") or "",
        "title": (dispatch.get("plan_json") and _json_title(dispatch.get("plan_json"))) or "",
        "snapshot": snap,
        "files": files,
        "targets": [
            {
                "repo_key": t.get("repo_key") or "",
                "child_issue_number": native_issue_number(t.get("child_issue_number")),
                "status": t.get("status") or "",
                "pr_number": int(t.get("pr_number") or 0),
                "error": t.get("error") or "",
            }
            for t in targets
        ],
    }


def dispatch_context_read(org_id: str,
                          dispatch_id: str) -> tuple[dict, DispatchContextStatus]:
    """HTTP 语义化读取入口：返回 (bundle, status)。

    - NOT_FOUND：dispatch 不存在（资源不存在）；
    - FORBIDDEN：dispatch 存在，但调用组织既非批次归属组织、也非任一目标仓库所属组织
      （跨组织不可见），不再混报 ``Organization not found``；
    - OK：可见，返回批次 + 快照 manifest + 快照正文。
    """
    from src.domains.mgmt import org_dispatch as od
    from src.kernel.session_manager import db, db_lock

    org_id = str(org_id or "")
    dispatch_id = str(dispatch_id or "")
    with db_lock:
        c = db.cursor()
        d = od._get_dispatch_row(c, dispatch_id)
        targets = od._get_target_rows(c, dispatch_id) if d else []
    if not d:
        return {}, DispatchContextStatus.NOT_FOUND
    if org_id not in _dispatch_visible_org_ids(d, targets):
        return {}, DispatchContextStatus.FORBIDDEN
    bundle = _load_dispatch_bundle(org_id, d, targets)
    # 批次归属组织已不可渲染（组织迁移/删除）时，回退到当前可见的请求组织重建快照，
    # 保证子 Issue 内嵌 org_id 仍能取到非空上下文。
    if not (bundle.get("files") or {}):
        dispatch_org = str(d.get("org_id") or "")
        if org_id and org_id != dispatch_org:
            fallback = _load_dispatch_bundle(org_id, d, targets, snapshot_org=org_id)
            if fallback.get("files") or {}:
                bundle = fallback
    return bundle, 200


def build_dispatch_context_bundle(org_id: str, dispatch_id: str) -> dict:
    """CLI/API 读取入口：返回批次 + 快照 manifest + 快照正文（不可见/不存在返回 {}）。"""
    bundle, status = dispatch_context_read(org_id, dispatch_id)
    return bundle if status == DispatchContextStatus.OK else {}


def _json_title(raw: str) -> str:
    try:
        return str((json.loads(raw or "{}") or {}).get("title") or "")
    except Exception:
        return ""