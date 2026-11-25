"""Pipeline configuration loader — DB-backed (Scene Center).

Reads scene configuration from devops_pipeline_scene table (repo-scoped only).
Official scenes live in scene_market(source='official'); repo scenes are copies
applied to a repo (issue-39 方案 B: scenes only bound to repo_key).
Backward compatible: if DB table missing/no scenes, falls back to configs/pipelines.json.
"""
import json
import os
import time
import threading
from typing import Optional

from src.kernel.config import CONFIG_DIR, log

# File-based fallback (used during migration or if DB not ready)
_PIPELINES_PATH = os.path.join(CONFIG_DIR, "pipelines.json")
_LEGACY_PATH = os.path.join(CONFIG_DIR, "agent-skill-mapping.json")

# Node color mapping (same as frontend agentColors)
NODE_COLORS = {
    "vod": "var(--pk)",
    "issue": "var(--r)",
    "coding": "var(--a)",
    "qa": "var(--pu)",
    "sre": "var(--or)",
    "chat": "var(--sk)",
}

# ====== Process-level cache (P1-B, Issue #357) ======
# Eliminates N+1: get_pipeline_config called per-card in _query_tasks (348×),
# each doing SHOW COLUMNS + scene query + market fallback = ~1000 DB round-trips.
# Cache TTL 60s (scenes are low-frequency config; write paths invalidate immediately).

_SCENE_CACHE_TTL = 60.0  # seconds
_SCENE_CACHE: dict[tuple, tuple] = {}  # (scene_id, repo_key) → (result_dict|None, expiry_ts)
_SCENE_CACHE_LOCK = threading.Lock()

# SHOW COLUMNS result cached once per process (table structure only changes on migration)
_SCENE_COLUMNS_CACHE: set[str] | None = None
_SCENE_COLUMNS_LOCK = threading.Lock()


def _get_scene_columns() -> set[str]:
    """Return column names of devops_pipeline_scene (cached once per process).

    Lock order fix (deadlock 2026-11-03): previously acquired _SCENE_COLUMNS_LOCK
    first and then db_lock while holding it, which inverted against callers that
    already hold db_lock (e.g. dashboard sync paths) -> ABBA deadlock. Now the DB
    query runs with db_lock ONLY, and _SCENE_COLUMNS_LOCK guards just the cache
    write, so the two locks are never held simultaneously.
    """
    global _SCENE_COLUMNS_CACHE
    if _SCENE_COLUMNS_CACHE is not None:
        return _SCENE_COLUMNS_CACHE

    # 1) DB query guarded by db_lock only (never nested with _SCENE_COLUMNS_LOCK).
    cols: set[str] = set()
    db, lock = _db()
    acquired = lock.acquire(timeout=30)
    try:
        if acquired:
            c = db.cursor()
            try:
                try:
                    c.execute("SHOW COLUMNS FROM devops_pipeline_scene")
                    cols = {r[0] for r in c.fetchall()}
                except Exception:
                    cols = {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
            except Exception:
                cols = set()
        else:
            log.warning("_get_scene_columns: db_lock acquire timed out (30s), returning empty column set")
    finally:
        if acquired:
            lock.release()

    # 2) Publish to cache under the columns lock (no DB / no db_lock inside).
    with _SCENE_COLUMNS_LOCK:
        if _SCENE_COLUMNS_CACHE is None:
            _SCENE_COLUMNS_CACHE = cols
        return _SCENE_COLUMNS_CACHE


def _cache_get(scene_id: str, repo_key: str):
    """Get cached pipeline config. Returns (result, hit) tuple."""
    key = (scene_id, repo_key)
    now = time.time()
    with _SCENE_CACHE_LOCK:
        entry = _SCENE_CACHE.get(key)
        if entry is not None and entry[1] > now:
            return entry[0], True
        if entry is not None:
            del _SCENE_CACHE[key]
    return None, False


def _cache_put(scene_id: str, repo_key: str, result):
    """Store pipeline config in cache with TTL."""
    key = (scene_id, repo_key)
    expiry = time.time() + _SCENE_CACHE_TTL
    with _SCENE_CACHE_LOCK:
        _SCENE_CACHE[key] = (result, expiry)


def invalidate_scene_cache(scene_id: str = "", repo_key: str = ""):
    """Invalidate cache entries. Called by write paths (create/update/delete scene).

    - scene_id + repo_key both given: invalidate that specific key.
    - Only repo_key: invalidate all entries for that repo.
    - Neither: clear entire cache (safe fallback).
    """
    with _SCENE_CACHE_LOCK:
        if not scene_id and not repo_key:
            _SCENE_CACHE.clear()
            return
        keys_to_del = []
        for key in _SCENE_CACHE:
            if scene_id and repo_key:
                if key == (scene_id, repo_key):
                    keys_to_del.append(key)
            elif repo_key:
                if key[1] == repo_key:
                    keys_to_del.append(key)
            elif scene_id:
                if key[0] == scene_id:
                    keys_to_del.append(key)
        for key in keys_to_del:
            del _SCENE_CACHE[key]


def _db():
    from src.kernel.owner_db import db_pair
    db, db_lock = db_pair("mgmt")
    return db, db_lock


def _load_pipelines_file() -> dict:
    """Load pipelines.json directly from file (fallback only)."""
    try:
        with open(_PIPELINES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"pipelines.json load failed ({e})")
        return {"scenes": {}}


def _load_legacy() -> dict:
    """Convert agent-skill-mapping.json to pipelines format (legacy fallback)."""
    try:
        with open(_LEGACY_PATH, "r", encoding="utf-8") as f:
            legacy = json.load(f)
        scenes = {}
        for scene_id, scene in legacy.get("scenes", {}).items():
            mapping = scene.get("mapping", {})
            node_ids = list(mapping.keys())
            nodes = []
            for i, nid in enumerate(node_ids):
                next_id = node_ids[i + 1] if i + 1 < len(node_ids) else "done"
                nodes.append({
                    "id": nid, "tag": nid, "skill": mapping[nid],
                    "mode": "emit", "next": next_id,
                    "handoff": "a2a" if next_id != "done" else "complete",
                })
            scenes[scene_id] = {"name": scene.get("name", scene_id), "nodes": nodes,
                                "onboarding": scene.get("onboarding", "repo-onboarding")}
        return {"scenes": scenes}
    except Exception as e:
        log.warning(f"legacy mapping load failed: {e}")
        return {"scenes": {}}


def _load_official_scene_from_market(scene_id: str) -> Optional[dict]:
    """Look up an official scene (template) from scene_market(source='official').

    Used as build-time / fallback source when a repo has no applied scene copy.

    Issue #2118：组织模板以 scope='org' 隔离，不属于普通官方模板。这里只读取
    scope 为 official/空的历史行，避免普通仓库按 scene_id 直接解析到组织模板。
    """
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT name, display_name, description, nodes_json, onboarding, scope FROM scene_market "
                "WHERE name=? AND source='official' AND is_active=1 LIMIT 1", (scene_id,))
            row = c.fetchone()
            if row:
                scope = (row[5] or "official").lower()
                if scope == "org":
                    return None
                nodes = json.loads(row[3] or "[]") if row[3] else []
                return {
                    "scene_id": scene_id, "name": row[0],
                    "display_name": row[1] or row[0], "description": row[2] or "",
                    "nodes": nodes, "onboarding": row[4] or "repo-onboarding",
                    "scene_type": "standard",
                }
        except Exception as e:
            log.warning(f"_load_official_scene_from_market failed: {e}")
    return None


def _repo_is_org_repo(repo_key: str) -> bool:
    """仓库是否为组织 provisioning 生成的仓库（is_org_repo=1）。"""
    if not repo_key:
        return False
    try:
        from src.domains.mgmt.user.org_db import get_managed_repo_binding
        return bool((get_managed_repo_binding(repo_key) or {}).get("is_org_repo"))
    except Exception as e:
        # 查询失败时不放行组织模板（fail-closed），但普通标准场景不受影响。
        log.warning(f"_repo_is_org_repo lookup failed (treat as non-org): repo={repo_key} err={e}")
        return False


def scene_is_org_scoped(scene_id: str) -> bool:
    """判定 scene_id 是否指向组织专用场景（不依赖 repo_key）。

    Issue #2118 复审：普通仓库直接提交组织模板（如官方模板 ``org``）时，仓库作用域
    解析会落空并回退 default；入口校验/预校验若只看「当前仓库的场景行」会漏判，从而
    产出指向不可执行场景的死单。这里按「模板来源」统一判定 org 归属，覆盖三处来源：
      1. devops_pipeline_scene：任意仓库行 scene_type='org'（含跨仓库引用）；
      2. scene_market：source='official' 且 scope='org'；
      3. pipelines.json 文件模板 scene_type='org'。
    """
    sid = (scene_id or "").strip()
    if not sid:
        return False
    db, lock = _db()
    try:
        with lock:
            c = db.cursor()
            c.execute(
                "SELECT 1 FROM devops_pipeline_scene "
                "WHERE scene_id=? AND is_active=1 AND scene_type='org' LIMIT 1",
                (sid,))
            if c.fetchone():
                return True
    except Exception as e:
        log.warning(f"scene_is_org_scoped: repo scene lookup failed scene={sid}: {e}")
    try:
        with lock:
            c = db.cursor()
            c.execute(
                "SELECT scope FROM scene_market "
                "WHERE name=? AND source='official' AND is_active=1 LIMIT 1",
                (sid,))
            row = c.fetchone()
        if row and str(row[0] or "").strip().lower() == "org":
            return True
    except Exception as e:
        log.warning(f"scene_is_org_scoped: market lookup failed scene={sid}: {e}")
    try:
        scene = (_load_pipelines_file().get("scenes") or {}).get(sid)
        if isinstance(scene, dict) and str(scene.get("scene_type") or "").strip().lower() == "org":
            return True
    except Exception as e:
        log.warning(f"scene_is_org_scoped: file lookup failed scene={sid}: {e}")
    return False


def _scene_search_where(q: str, cols: set[str]) -> tuple[str, list]:
    """Issue #501 检索 WHERE 片段单一来源（评审遗留提醒3）：
    q 非空时按 name/description/display_name（列存在时）三字段 LIKE 构建过滤，
    返回 (" AND (...)", [like 参数...])；_query_scenes_from_db 与
    _count_scenes_from_db 共用——改检索字段只需改这一处。
    cols 为调用方探测的列集合（query 用行内探测，count 用进程级缓存）。
    """
    if not q:
        return "", []
    like = f"%{q}%"
    fields = ["name", "description"]
    if "display_name" in cols:
        fields.append("display_name")
    where = " AND (" + " OR ".join(f"{f} LIKE ?" for f in fields) + ")"
    return where, [like] * len(fields)


def _query_scenes_from_db(repo_key: str = "", limit: int = None, offset: int = 0,
                          q: str = "", sort: str = "id", minimal: bool = False) -> list[dict]:
    """Query repo-scoped scenes from DB (scenes only bound to repo_key).

    Official scenes are templates in scene_market, not shown as repo scenes.
    Issue #501 分页：limit 非 None 时 SQL LIMIT/OFFSET（ORDER BY id 稳定排序，
    UNIQUE(scene_id, repo_key) 保证无重复行，页边界不漂移）；不带 limit 全量
    （其余 9 个共享调用点零改动）。LIMIT/OFFSET 为 SQLite/MySQL 通用语法。
    Issue #501 增量（场景检索）：q 非空时 name/display_name/description 三字段
    LIKE 过滤（与 _count_scenes_from_db 同一 WHERE 片段，两处必须同步）；
    sort: id（默认，稳定序）/ latest（created_at DESC, id DESC 兜底并列）。
    q 与 sort 仅由路由层传入，其余调用点默认值零改动。
    Issue #501 增量（C 方案）：minimal=True 时跳过 nodes_json 反序列化——
    只要名字/图标的调用方（需求中心下拉）不再为大 nodes 付解析成本；
    列仍被 SELECT（列索引稳定），仅不 parse。
    """
    db, lock = _db()
    rows = []
    _has_dn = False
    _has_ao = False
    _has_cb = False
    _has_icon = False
    _has_st = False
    _has_pr_sub = False
    _has_linked_repos = False
    _has_ca = False
    _has_pin = False
    _has_pinat = False
    with lock:
        c = db.cursor()
        try:
            if repo_key:
                # PRAGMA 在 MySQL 上列不全，用 SHOW COLUMNS 替代
                try:
                    c.execute("SHOW COLUMNS FROM devops_pipeline_scene")
                    _cols = {r[0] for r in c.fetchall()}
                except Exception:
                    _cols = {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
                _has_dn = "display_name" in _cols
                _has_ao = "advanced_options" in _cols
                _has_cb = "created_by" in _cols
                _has_icon = "icon" in _cols
                _has_st = "scene_type" in _cols
                _has_pr_sub = "pr_subscription_enabled" in _cols
                _has_linked_repos = "linked_repos" in _cols
                _has_ca = "created_at" in _cols
                _has_pin = "is_pinned" in _cols
                _has_pinat = "pinned_at" in _cols
                _extra = ""
                if _has_dn: _extra += ", display_name"
                if _has_ao: _extra += ", advanced_options"
                if _has_cb: _extra += ", created_by"
                if _has_icon: _extra += ", icon"
                if _has_st: _extra += ", scene_type"
                if _has_pr_sub: _extra += ", pr_subscription_enabled"
                if _has_linked_repos: _extra += ", linked_repos"
                if _has_pin: _extra += ", is_pinned"
                if _has_pinat: _extra += ", pinned_at"
                if _has_ca and sort == "latest": _extra += ", created_at"
                # 检索 WHERE：共享构建器（与 _count_scenes_from_db 单一来源）
                _where_extra, _q_params = _scene_search_where(q, _cols)
                _params = [repo_key] + _q_params
                if _has_pin:
                    if _has_pinat:
                        _order = ("ORDER BY is_pinned DESC, pinned_at DESC, created_at DESC, id DESC"
                                  if sort == "latest" else "ORDER BY is_pinned DESC, pinned_at DESC, id")
                    else:
                        _order = ("ORDER BY is_pinned DESC, created_at DESC, id DESC"
                                  if sort == "latest" else "ORDER BY is_pinned DESC, id")
                else:
                    _order = "ORDER BY id" if sort != "latest" else "ORDER BY created_at DESC, id DESC"
                # LIMIT/OFFSET 走占位符（评审遗留提醒2：与同函数参数化风格一致；
                # int() 强转保留为双保险）
                _limit_sql = " LIMIT ? OFFSET ?" if limit is not None else ""
                if limit is not None:
                    _params = _params + [int(limit), int(offset)]
                c.execute(
                    f"SELECT scene_id, name, description, nodes_json, onboarding{_extra} FROM devops_pipeline_scene "
                    f"WHERE is_active=1 AND repo_key=?{_where_extra} {_order}{_limit_sql}",
                    tuple(_params))
                rows.extend(c.fetchall())
        except Exception as e:
            log.warning(f"_query_scenes_from_db failed: {e}")
    result = []
    seen_ids = set()
    for r in rows:
        scene_id = r[0]
        if scene_id in seen_ids:
            continue
        seen_ids.add(scene_id)
        if minimal:
            nodes = []
        else:
            try:
                nodes = json.loads(r[3] or "[]") if r[3] else []
            except Exception:
                nodes = []
        # Parse advanced_options for work_branch
        work_branch = "main"
        col_offset = 5  # columns after onboarding (index 4)
        if _has_dn and len(r) > col_offset:
            col_offset += 1  # display_name
        if _has_ao and len(r) > col_offset:
            try:
                opts = json.loads(r[col_offset] or "{}")
                work_branch = opts.get("work_branch", "main") or "main"
            except Exception:
                pass
            col_offset += 1
        created_by = ""
        if _has_cb and len(r) > col_offset:
            created_by = r[col_offset] or ""
            col_offset += 1
        icon = ""
        if _has_icon and len(r) > col_offset:
            icon = r[col_offset] or ""
            col_offset += 1
        scene_type = "standard"
        if _has_st and len(r) > col_offset:
            scene_type = r[col_offset] or "standard"
            col_offset += 1
        pr_subscription_enabled = 0
        if _has_pr_sub and len(r) > col_offset:
            pr_subscription_enabled = int(r[col_offset] or 0)
            col_offset += 1
        linked_repos = []
        if _has_linked_repos and len(r) > col_offset:
            try:
                linked_repos = json.loads(r[col_offset] or "[]") if r[col_offset] else []
            except Exception:
                linked_repos = []
            col_offset += 1
        is_pinned = 0
        if _has_pin and len(r) > col_offset:
            is_pinned = int(r[col_offset] or 0)
            col_offset += 1
        pinned_at = ""
        if _has_pinat and len(r) > col_offset:
            pinned_at = r[col_offset] or ""
            col_offset += 1
        result.append({
            "scene_id": scene_id, "name": r[1], "description": r[2],
            "display_name": (r[5] if _has_dn and len(r) > 5 else "") or r[1],
            "nodes": nodes, "onboarding": r[4] or "repo-onboarding",
            "work_branch": work_branch, "created_by": created_by,
            "icon": icon, "scene_type": scene_type,
            "pr_subscription_enabled": pr_subscription_enabled,
            "linked_repos": linked_repos,
            "is_pinned": is_pinned,
            "pinned_at": pinned_at,
        })
    return result


def _query_scene_by_id(scene_id: str, repo_key: str = "") -> Optional[dict]:
    """Query a specific repo-scoped scene from DB by scene_id + repo_key.

    仅仓库场景；找不到时回退到 scene_market 官方模板（_load_official_scene_from_market）。
    Uses cached column set (P1-B: SHOW COLUMNS only once per process).
    """
    _cols = _get_scene_columns()
    _has_dn = "display_name" in _cols
    _has_st = "scene_type" in _cols
    _has_pr_sub = "pr_subscription_enabled" in _cols
    _has_linked_repos = "linked_repos" in _cols
    _extra = ", display_name" if _has_dn else ""
    if _has_st:
        _extra += ", scene_type"
    if _has_pr_sub:
        _extra += ", pr_subscription_enabled"
    if _has_linked_repos:
        _extra += ", linked_repos"
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT scene_id, name, description, nodes_json, onboarding{_extra} FROM devops_pipeline_scene "
                "WHERE is_active=1 AND scene_id=? AND repo_key=? LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
            if row:
                nodes = json.loads(row[3] or "[]") if row[3] else []
                _scene_type = "standard"
                _pr_sub = 0
                _linked_repos = []
                _col_idx = 5  # after onboarding (index 4)
                if _has_dn:
                    _col_idx += 1  # display_name at index 5
                if _has_st and len(row) > _col_idx:
                    _scene_type = row[_col_idx] or "standard"
                    _col_idx += 1
                if _has_pr_sub and len(row) > _col_idx:
                    _pr_sub = int(row[_col_idx] or 0)
                    _col_idx += 1
                if _has_linked_repos and len(row) > _col_idx:
                    try:
                        _linked_repos = json.loads(row[_col_idx] or "[]") if row[_col_idx] else []
                    except Exception:
                        _linked_repos = []
                return {
                    "scene_id": row[0], "name": row[1], "description": row[2],
                    "display_name": (row[5] if _has_dn and len(row) > 5 else "") or row[1],
                    "nodes": nodes, "onboarding": row[4] or "repo-onboarding",
                    "scene_type": _scene_type,
                    "pr_subscription_enabled": _pr_sub,
                    "linked_repos": _linked_repos,
                }
        except Exception as e:
            log.warning(f"_query_scene_by_id failed: {e}")
    return None


def _count_scenes_from_db(repo_key: str = "", q: str = "") -> int:
    """Count active repo-scoped scenes (Issue #501: 分页 total，不拉 nodes_json).

    与 _query_scenes_from_db 同一 WHERE 条件（q 过滤两处必须同步），仅 COUNT；
    查不到/异常返回 0。
    """
    if not repo_key:
        return 0
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            # 共享构建器（列集合走进程级缓存 _get_scene_columns，与列表降级口径一致）
            _where_extra, _q_params = _scene_search_where(q, _get_scene_columns())
            c.execute(
                "SELECT COUNT(*) FROM devops_pipeline_scene "
                f"WHERE is_active=1 AND repo_key=?{_where_extra}",
                tuple([repo_key] + _q_params))
            row = c.fetchone()
            return int(row[0]) if row else 0
        except Exception as e:
            log.warning(f"_count_scenes_from_db failed: {e}")
            return 0


def get_scenes(repo_key: str = "", limit: int = None, offset: int = 0,
               q: str = "", sort: str = "id", minimal: bool = False) -> list[dict]:
    """Return scene list [{id, name, display_name, scope, description, work_branch, created_by, icon, nodes, onboarding}] for this repo.

    Issue #501: limit 非 None 时 SQL 层 LIMIT/OFFSET（分页调用方），
    不带 limit 保持全量语义（向后兼容，其余调用点零改动）。
    Issue #501 增量：q（name/display_name/description LIKE 检索）与
    sort（id/latest）由路由层可选传入，默认行为不变。
    """
    db_scenes = _query_scenes_from_db(repo_key=repo_key, limit=limit, offset=offset, q=q, sort=sort, minimal=minimal)
    return [{"id": s["scene_id"], "name": s["name"],
             "display_name": s.get("display_name") or s["name"],
             "scope": "repo",
             "description": s.get("description") or "",
             "work_branch": s.get("work_branch") or "main",
             "created_by": s.get("created_by") or "",
             "icon": s.get("icon") or "",
             "nodes": s.get("nodes") or [],
             "onboarding": s.get("onboarding") or "repo-onboarding",
             "scene_type": s.get("scene_type") or "standard",
             "pr_subscription_enabled": s.get("pr_subscription_enabled", 0),
             "linked_repos": s.get("linked_repos") or [],
             "is_pinned": int(s.get("is_pinned") or 0),
             "pinned_at": s.get("pinned_at") or ""} for s in db_scenes]


def get_scenes_for_repos(repo_keys: list[str], *, minimal: bool = False) -> dict[str, list[dict]]:
    """批量读取多个仓库的仓内场景（一次查询，供组织下派目录等 N 仓场景）。

    与 ``get_scenes`` 的单仓口径一致（is_active=1、仓内副本），仅去掉分页/检索/
    市场兜底——调用方只关心每个仓库自己的场景集合。列集合走进程内缓存
    ``_get_scene_columns()``，不再逐仓 ``SHOW COLUMNS``。
    """
    keys = sorted({str(k or "").strip() for k in (repo_keys or []) if str(k or "").strip()})
    if not keys:
        return {}
    cols = _get_scene_columns()
    extra = [name for name in (
        "display_name", "advanced_options", "created_by", "icon", "scene_type",
        "pr_subscription_enabled", "linked_repos", "is_pinned", "pinned_at",
    ) if name in cols]
    select = ["scene_id", "repo_key", "name", "description", "nodes_json",
              "onboarding", *extra]
    db, lock = _db()
    rows = []
    with lock:
        c = db.cursor()
        placeholders = ",".join("?" * len(keys))
        try:
            c.execute(
                f"SELECT {', '.join(select)} FROM devops_pipeline_scene "
                f"WHERE is_active=1 AND repo_key IN ({placeholders}) "
                "ORDER BY repo_key, scene_id",
                tuple(keys),
            )
            rows = c.fetchall()
        except Exception as exc:  # 查询失败按空场景处理，目录仍返回其它仓库
            log.warning(f"get_scenes_for_repos failed repos={len(keys)}: {exc}")
            return {}
    out: dict[str, list[dict]] = {key: [] for key in keys}
    for row in rows:
        data = dict(zip(select, row))
        repo_key = str(data.pop("repo_key") or "")
        if not repo_key:
            continue
        try:
            nodes = [] if minimal else (json.loads(data.get("nodes_json") or "[]") or [])
        except Exception:
            nodes = []
        work_branch = "main"
        try:
            options = json.loads(data.get("advanced_options") or "{}") if "advanced_options" in data else {}
            work_branch = str(options.get("work_branch") or "main")
        except Exception:
            pass
        try:
            linked_repos = (json.loads(data.get("linked_repos") or "[]") or []) if "linked_repos" in data else []
        except Exception:
            linked_repos = []
        out.setdefault(repo_key, []).append({
            "id": data.get("scene_id"), "name": data.get("name"),
            "display_name": data.get("display_name") or data.get("name"),
            "scope": "repo",
            "description": data.get("description") or "",
            "work_branch": work_branch or "main",
            "created_by": data.get("created_by") or "",
            "icon": data.get("icon") or "",
            "nodes": nodes,
            "onboarding": data.get("onboarding") or "repo-onboarding",
            "scene_type": data.get("scene_type") or "standard",
            "pr_subscription_enabled": int(data.get("pr_subscription_enabled") or 0),
            "linked_repos": linked_repos,
            "is_pinned": int(data.get("is_pinned") or 0),
            "pinned_at": data.get("pinned_at") or "",
        })
    return out


def get_pipeline_config(scene_id: str = "default", repo_key: str = "",
                        use_cache: bool = True) -> Optional[dict]:
    """Return pipeline config for a repo-scoped scene: {name, nodes: [...], onboarding}.

    Resolution priority (Issue #502, aligns docs/new-version3/scene-center-design.md):
      1. repo 作用域：显式仓库上下文时，仓库已应用的场景副本优先
         （保证详情与 list --repo 一致、保留仓库自定义，不回退覆盖）。
      2. 全局/官方作用域：repo_key 为空也允许按 scene_id 查 devops_pipeline_scene
         中 repo_key='' 的全局/官方行（原实现 repo_key 为空直接跳过 DB）。
      3. scene_market 官方模板（source='official'）。
      4. pipelines.json 文件：先精确按 scene_id 匹配；兜底 default 时结果带
         fallback=true + fallback_source 标记（供读侧接口拒绝静默回退，内部执行链路保持兼容）。

    返回结果统一携带真实 scene_id（scene.get('scene_id')），并附 fallback / fallback_source
    元数据；调用方可根据 fallback 决定是否视为「未解析到」。

    Results are cached at process level (TTL 60s) to eliminate N+1 in _query_tasks
    (P1-B, Issue #357). Write paths (create/update/delete_custom_scene) invalidate
    immediately via invalidate_scene_cache().

    use_cache=False 只给「必须反映此刻真实配置」的调用方用（建单前场景预校验、
    dev_scene 悬空探测）：缓存是进程级的，双主各自持有，另一台删除/修改场景后
    60s 窗口内仍会读到旧节点，用缓存做可执行性判定会拿幽灵场景拦单。
    """
    # Check cache first
    if use_cache:
        cached, hit = _cache_get(scene_id, repo_key)
        if hit:
            return cached

    scene = None
    fallback = False
    fallback_source = {}
    if repo_key:
        # 1. repo 作用域（仓库已应用的场景副本优先，保留自定义）
        scene = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not scene:
        # 2. 全局/官方作用域：repo_key 为空也允许按 scene_id 查全局行
        scene = _query_scene_by_id(scene_id, repo_key="")
    if not scene:
        # 3. scene_market 官方模板
        scene = _load_official_scene_from_market(scene_id)
    if not scene:
        # 4. pipelines.json 文件：精确 scene_id → 否则 default（标记 fallback）
        data = _load_pipelines_file()
        file_scene = data.get("scenes", {}).get(scene_id)
        if not file_scene:
            file_scene = data.get("scenes", {}).get("default")
            if file_scene:
                fallback = True
                fallback_source = {"scene_id": "default", "name": file_scene.get("name", "default")}
        if file_scene:
            _name = file_scene.get("name", scene_id)
            result = {
                "name": _name,
                "display_name": _name,
                "nodes": file_scene.get("nodes", []),
                "onboarding": file_scene.get("onboarding", "repo-onboarding"),
                "scene_type": file_scene.get("scene_type", "standard"),
                "scene_id": fallback_source.get("scene_id") or scene_id,
                "fallback": fallback,
                "fallback_source": fallback_source,
            }
            # Issue #2118：组织模板（scene_type=org）只允许组织仓库解析，
            # 普通仓库直接传 scene_id=org 时视为未命中，而不是拿到组织节点。
            if result.get("scene_type") == "org" and repo_key and not _repo_is_org_repo(repo_key):
                log.info(f"get_pipeline_config: org scene '{scene_id}' rejected for non-org repo={repo_key}")
                _cache_put(scene_id, repo_key, None)
                return None
            _cache_put(scene_id, repo_key, result)
            return result
        _cache_put(scene_id, repo_key, None)  # cache None to avoid repeated misses
        return None
    result = {
        "name": scene.get("name", scene_id) or scene_id,
        "display_name": scene.get("display_name") or scene.get("name", scene_id) or scene_id,
        "nodes": scene.get("nodes") or [],
        "onboarding": scene.get("onboarding", "repo-onboarding"),
        "scene_type": scene.get("scene_type", "standard"),
        "scene_id": scene.get("scene_id") or scene_id,
        "fallback": fallback,
        "fallback_source": fallback_source,
    }
    _cache_put(scene_id, repo_key, result)
    return result


def _query_user_market_scene_nodes(scene_id: str, user_id: int) -> Optional[list]:
    """Issue #979: 查用户个人场景（scene_market scope='user'）的 nodes。

    个人场景不落 devops_pipeline_scene（仅 repo/global 行），标准解析链路
    （repo 副本 → 全局行 → 官方市场模板 → pipelines.json）查不到；命中返回
    nodes 列表，未命中/异常返回 None（调用方继续按未解析处理）。
    """
    if not scene_id or not user_id:
        return None
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT nodes_json FROM scene_market "
                "WHERE name=? AND scope='user' AND source='user' "
                "AND source_user_id=? AND is_active=1 LIMIT 1",
                (scene_id, user_id))
            row = c.fetchone()
            if row and row[0]:
                return json.loads(row[0])
        except Exception as e:
            log.warning(f"_query_user_market_scene_nodes failed: {e}")
    return None


def get_node_by_tag(scene_id: str, tag: str, repo_key: str = "", user_id: int = 0) -> Optional[dict]:
    """Find a node by tag in a scene's pipeline.

    Issue #955：id 优先、tag 兜底——节点 id 即角色名（agent_type），
    tag 为展示用阶段名（可为中文等任意值）；存量非标准 tag 场景按 id 匹配。

    Issue #979: user_id>0 时，标准解析未命中 tag 节点（或仅命中 pipelines.json
    default 兜底）后补查用户个人场景（scene_market scope='user'），避免个人
    场景节点查不到而静默走名字降级（user_id 此前为死参数）。
    """
    from src.domains.runtime.public import node_matches_agent_role
    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    node = None
    if cfg:
        for _n in cfg.get("nodes", []):
            if node_matches_agent_role(_n, tag):
                node = _n
                break
    # 真实命中（非 pipelines.json default 兜底）→ 直接返回
    if node and cfg and not (cfg.get("fallback") or cfg.get("fallback_source")):
        return node
    # 用户个人场景（scene_market scope='user'）优先于 default 兜底节点
    if user_id and scene_id:
        nodes = _query_user_market_scene_nodes(scene_id, user_id)
        if nodes is not None:
            for _n in nodes:
                if isinstance(_n, dict) and node_matches_agent_role(_n, tag):
                    return _n
    return node


def get_first_node(scene_id: str = "default", repo_key: str = "") -> Optional[dict]:
    """Return the first node of a scene's pipeline (entry point).

    传 repo_key 时优先查仓库级场景（含自定义场景），不传则回退到场景市场/pipelines.json。
    """
    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not cfg or not cfg.get("nodes"):
        return None
    return cfg["nodes"][0]


def get_next_node(scene_id: str, current_node_id: str, repo_key: str = "") -> Optional[dict]:
    """Return the next node after the current one, respecting skip list."""
    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not cfg:
        return None
    nodes = cfg.get("nodes", [])
    found = False
    for node in nodes:
        if found:
            return node
        if node.get("id") == current_node_id:
            found = True
            next_id = node.get("next", "done")
            if next_id == "done":
                return None
    for node in nodes:
        if node.get("id") == current_node_id:
            next_id = node.get("next", "done")
            if next_id == "done":
                return None
            for n2 in nodes:
                if n2.get("id") == next_id:
                    return n2
    return None


def is_node_skipped(scene_id: str, node_id: str, repo_key: str = "") -> bool:
    """Check if a node is in any other node's skip list."""
    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not cfg:
        return False
    for node in cfg.get("nodes", []):
        skip = node.get("skip", [])
        if node_id in skip:
            return True
    return False


def node_role(node: dict) -> str:
    """节点对外角色名：tag（阶段/agent_type）优先，缺省回退 id。

    Issue #2118：组织节点 id=org_intake/org_dispatch/org_aggregate，但对外仍复用
    issue/coding/qa tag。所有阶段的推进、A2A 目标与 pending_review 判定都必须用
    tag 作为角色键，不能只按 id 比对。
    """
    if not isinstance(node, dict):
        return ""
    return str(node.get("tag") or node.get("id") or "").strip()


def get_pipeline_transition(scene_id: str, agent_role: str,
                            repo_key: str = "") -> Optional[dict]:
    """按 agent 角色（id 或 tag）解析节点与下一个节点的推进信息。

    返回 {"node", "next_node", "next_id", "next_role", "terminal", "handoff"}；
    场景/节点缺失返回 None。terminal=True 表示 next 已是 done/complete/end。

    供 complete_task 的自动推进与 pending_review 判定复用，保证 org 场景
    （id != tag）与标准场景（id == tag）同一口径。
    """
    from src.domains.runtime.public import node_matches_agent_role

    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not cfg:
        return None
    nodes = [n for n in (cfg.get("nodes") or []) if isinstance(n, dict)]
    node = next((n for n in nodes if node_matches_agent_role(n, agent_role)), None)
    if not node:
        return None
    next_id = str(node.get("next") or "").strip()
    terminal = (not next_id) or next_id.lower() in ("done", "complete", "end")
    next_node = None
    if not terminal:
        next_node = next((n for n in nodes if str(n.get("id") or "") == next_id), None)
    return {
        "node": node,
        "next_node": next_node,
        "next_id": next_id,
        "next_role": node_role(next_node) if next_node else next_id,
        "terminal": terminal,
        "handoff": str(node.get("handoff") or "").strip().lower(),
    }


# ===== Scene CRUD (for custom scenes) =====

ORG_SCENE_NODES = [
    {
        "id": "org_intake",
        "tag": "issue",
        "skill": "org-intake-cli",
        "name": "组织需求接收",
        "mode": "emit",
        "next": "org_dispatch",
        "handoff": "a2a",
        "skip": [],
        "enabled": True,
        "auto_advance": True,
        "engine_runtime_ids": [],
    },
    {
        "id": "org_dispatch",
        "tag": "coding",
        "skill": "org-dispatch-cli",
        "name": "组织任务派发",
        "mode": "emit",
        "next": "org_aggregate",
        "handoff": "a2a",
        "skip": [],
        "enabled": True,
        "auto_advance": True,
        "engine_runtime_ids": [],
    },
    {
        "id": "org_aggregate",
        "tag": "qa",
        "skill": "org-aggregate-cli",
        "name": "组织结果聚合",
        "mode": "emit",
        "next": "done",
        "handoff": "complete",
        "skip": [],
        "enabled": True,
        "auto_advance": True,
        "engine_runtime_ids": [],
    },
]


def fill_scene_nodes_skill_id(nodes: list, repo_key: str = "", user_id: int = 0,
                              resolver=None) -> list:
    """Issue #3914 E：场景节点「存名必存 id」——返回补齐 skill_id 后的节点列表。

    背景：节点只落 ``skill`` 名时，派发侧只能按名解析（user→repo→official 三档同名
    无法锁定用户选择档位）；落 id 后走 #979 的权威 id 直查。

    语义：
    - 节点已有**非 0** skill_id → 保留（不覆盖前端显式选择）；``skill_id=0`` 或缺省
      → 视为「未存 id」按名解析回填（存量/老前端只落 0 的节点正需回填）；
    - 无技能名 / 非 dict 节点 → 原样返回；
    - 有技能名无 id → 按 name 解析回填；解析不到保持原样（派发时按名降级 + warning，
      与 #979 存量回填同口径）。
    ``resolver`` 仅供测试注入（默认 mgmt.user.db.resolve_skill_id）。
    纯内存变换：不就地改入参、异常不外抛（保存链不得因解析失败被阻断）。
    """
    _resolve = resolver
    if _resolve is None:
        from src.domains.mgmt.user.db import resolve_skill_id as _resolve
    out: list = []
    for n in nodes or []:
        if not isinstance(n, dict):
            out.append(n)
            continue
        _name = str(n.get("skill") or "").strip()
        # 只认 truthy 的既有 skill_id 为「已存」：0/缺省 → 按名回填（0 不是有效选择，
        # 与 docstring 语义一致）；非数值的脏值保持原样不覆盖（不猜前端意图）。
        if not _name or n.get("skill_id"):
            out.append(n)
            continue
        try:
            _sid = int(_resolve(_name, repo_key=repo_key or "",
                                user_id=user_id or 0) or 0)
        except Exception as e:
            log.warning(f"scene node skill resolve failed (skill={_name}): {e}")
            _sid = 0
        if _sid:
            n = dict(n)
            n["skill_id"] = _sid
        out.append(n)
    return out


def create_custom_scene(scene_id: str, name: str, description: str,
                        repo_key: str, nodes: list, onboarding: str = "repo-onboarding",
                        display_name: str = "", created_by: str = "", icon: str = "",
                        scene_type: str = "standard", pr_subscription_enabled: int = 0,
                        linked_repos: list = None) -> int:
    """Create or reactivate a repo-scoped custom scene in DB. Returns DB id."""
    db, lock = _db()
    # Issue #3914 E：存名必存 id（仓库级场景只允许 repo/official 档 → user_id=0）
    nodes_json = json.dumps(fill_scene_nodes_skill_id(nodes, repo_key), ensure_ascii=False)
    linked_repos_json = json.dumps(linked_repos or [], ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        c = db.cursor()
        # Check if record exists (硬删除后不会有 is_active=0 残留)
        c.execute(
            "SELECT id FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=? LIMIT 1",
            (scene_id, repo_key))
        existing = c.fetchone()
        if existing:
            # 已存在 → 更新
            c.execute(
                "UPDATE devops_pipeline_scene SET name=?, description=?, nodes_json=?, onboarding=?, is_active=1, updated_at=? WHERE id=?",
                (name, description, nodes_json, onboarding, now, existing[0]))
            _try_set_display_name(c, existing[0], display_name or name)
            _try_set_created_by(c, existing[0], created_by)
            _try_set_icon(c, existing[0], icon)
            _try_set_scene_type(c, existing[0], scene_type)
            _try_set_pr_sub_enabled(c, existing[0], pr_subscription_enabled)
            _try_set_linked_repos(c, existing[0], linked_repos_json)
            db.commit()
            invalidate_scene_cache(scene_id, repo_key)
            return existing[0]
        # Insert new record
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, description, repo_key, nodes_json, onboarding, is_active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (scene_id, name, description, repo_key, nodes_json, onboarding, now, now))
        new_id = c.lastrowid
        _try_set_display_name(c, new_id, display_name or name)
        _try_set_created_by(c, new_id, created_by)
        _try_set_icon(c, new_id, icon)
        _try_set_scene_type(c, new_id, scene_type)
        _try_set_pr_sub_enabled(c, new_id, pr_subscription_enabled)
        _try_set_linked_repos(c, new_id, linked_repos_json)
        db.commit()
        invalidate_scene_cache(scene_id, repo_key)
        return new_id


def create_org_scene(scene_id: str, org_name: str, repo_key: str,
                     created_by: str = "", org_id: str = "",
                     work_branch: str = "main") -> int:
    """Create or update the default organization scene definition.

    ``work_branch`` 必须是组织仓库可用的工作分支（新仓先 main，provisioning
    建好 grape-org 后回填为汇总分支）；调用方无法解析时默认 main。
    """
    db_id = create_custom_scene(
        scene_id=scene_id,
        name=f"组织任务下派 - {org_name or 'Organization'}",
        description="组织需求接收、任务下派与结果聚合",
        repo_key=repo_key,
        nodes=ORG_SCENE_NODES,
        onboarding="repo-onboarding",
        created_by=created_by,
        scene_type="org",
    )
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            row = c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? LIMIT 1",
                (scene_id, repo_key),
            ).fetchone()
            try:
                opts = json.loads((row[0] if row else "") or "{}")
            except Exception:
                opts = {}
            if not isinstance(opts, dict):
                opts = {}
            opts["work_branch"] = str(work_branch or "main").strip() or "main"
            c.execute(
                "UPDATE devops_pipeline_scene SET advanced_options=?, updated_at=datetime('now') "
                "WHERE scene_id=? AND repo_key=?",
                (json.dumps(opts, ensure_ascii=False), scene_id, repo_key),
            )
            if org_id:
                c.execute(
                    "UPDATE devops_pipeline_scene SET org_id=? "
                    "WHERE scene_id=? AND repo_key=?",
                    (str(org_id), scene_id, repo_key),
                )
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"create_org_scene advanced_options failed: {exc}") from exc
    invalidate_scene_cache(scene_id, repo_key)
    return db_id


def set_org_scene_work_branch(scene_id: str, repo_key: str, work_branch: str) -> bool:
    """幂等校正组织场景工作分支（同步/默认分支落定后回填）。"""
    scene_id = str(scene_id or "").strip()
    repo_key = str(repo_key or "").strip()
    branch = str(work_branch or "").strip()
    if not (scene_id and repo_key and branch):
        return False
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            row = c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? LIMIT 1",
                (scene_id, repo_key),
            ).fetchone()
            if not row:
                return False
            try:
                opts = json.loads((row[0] if row else "") or "{}")
            except Exception:
                opts = {}
            if not isinstance(opts, dict):
                opts = {}
            if str(opts.get("work_branch") or "") == branch:
                return True
            opts["work_branch"] = branch
            c.execute(
                "UPDATE devops_pipeline_scene SET advanced_options=?, updated_at=datetime('now') "
                "WHERE scene_id=? AND repo_key=?",
                (json.dumps(opts, ensure_ascii=False), scene_id, repo_key),
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            log.warning(f"set_org_scene_work_branch failed scene={scene_id} repo={repo_key}: {exc}")
            return False
    invalidate_scene_cache(scene_id, repo_key)
    return True


def _try_set_created_by(c, row_id: int, created_by: str):
    """幂等写入 created_by（列不存在时忽略）。"""
    if not created_by:
        return
    try:
        c.execute("UPDATE devops_pipeline_scene SET created_by=? WHERE id=?", (created_by, row_id))
    except Exception:
        pass


def _try_set_icon(c, row_id: int, icon: str):
    """幂等写入 icon（列不存在时忽略）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET icon=? WHERE id=?", (icon, row_id))
    except Exception:
        pass


def _try_set_scene_type(c, row_id: int, scene_type: str):
    """幂等写入 scene_type（列不存在时忽略）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET scene_type=? WHERE id=?", (scene_type, row_id))
    except Exception:
        pass

def _try_set_pr_sub_enabled(c, row_id: int, enabled: int):
    """幂等写入 pr_subscription_enabled（列不存在时忽略）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET pr_subscription_enabled=? WHERE id=?", (int(enabled), row_id))
    except Exception:
        pass


def _try_set_linked_repos(c, row_id: int, linked_repos_json: str):
    """幂等写入 linked_repos（列不存在时忽略）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET linked_repos=? WHERE id=?", (linked_repos_json, row_id))
    except Exception:
        pass


def _try_set_display_name(c, row_id: int, display_name: str):
    """幂等写入 display_name（列不存在时忽略，兼容旧库）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET display_name=? WHERE id=?", (display_name, row_id))
    except Exception:
        pass


def update_custom_scene(scene_id: str, repo_key: str,
                        nodes: list, name: str | None = None, description: str | None = None,
                        icon: str | None = None, pr_subscription_enabled: int | None = None,
                        linked_repos: list | None = None):
    """Update a repo-scoped custom scene."""
    db, lock = _db()
    # Issue #3914 E：存名必存 id（保存链路单点收口，复制/组织场景同样受益）
    nodes_json = json.dumps(fill_scene_nodes_skill_id(nodes, repo_key), ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        c = db.cursor()
        sets = ["nodes_json=?", "updated_at=?"]
        params = [nodes_json, now]
        if name:  # 空字符串不覆盖已有名称
            sets.append("name=?")
            params.append(name)
            sets.append("display_name=?")
            params.append(name)
        if description is not None:  # None=不更新，空字符串=清空描述
            sets.append("description=?")
            params.append(description)
        if icon is not None:
            sets.append("icon=?")
            params.append(icon)
        if pr_subscription_enabled is not None:
            sets.append("pr_subscription_enabled=?")
            params.append(int(pr_subscription_enabled))
        if linked_repos is not None:
            sets.append("linked_repos=?")
            params.append(json.dumps(linked_repos, ensure_ascii=False))
        params.extend([scene_id, repo_key])
        c.execute(
            f"UPDATE devops_pipeline_scene SET {','.join(sets)} WHERE scene_id=? AND repo_key=? AND is_active=1",
            tuple(params))
        db.commit()
    invalidate_scene_cache(scene_id, repo_key)


def set_scene_pinned(scene_id: str, repo_key: str, pinned: bool) -> int:
    """设置仓库场景置顶状态（仓库共享；重复置顶不刷新 pinned_at）。

    返回受影响行数。MySQL 未启用 CLIENT_FOUND_ROWS 时，匹配但值未变的 UPDATE
    会返回 0（如重复置顶/重复取消），因此调用方必须先做存在性校验，不能把
    rowcount==0 当作「场景不存在」。
    """
    db, lock = _db()
    from datetime import datetime, timezone
    with lock:
        c = db.cursor()
        if pinned:
            now = datetime.now(timezone.utc).isoformat()
            c.execute(
                "UPDATE devops_pipeline_scene SET "
                "pinned_at=CASE WHEN is_pinned=1 THEN pinned_at ELSE ? END, "
                "is_pinned=1 WHERE scene_id=? AND repo_key=? AND is_active=1",
                (now, scene_id, repo_key))
        else:
            c.execute(
                "UPDATE devops_pipeline_scene SET is_pinned=0, pinned_at=NULL "
                "WHERE scene_id=? AND repo_key=? AND is_active=1",
                (scene_id, repo_key))
        rowcount = c.rowcount
        db.commit()
    return int(rowcount or 0)


def delete_custom_scene(scene_id: str, repo_key: str):
    """Hard-delete a repo-scoped custom scene. 精确匹配 scene_id + repo_key，不影响其他仓库。"""
    db, lock = _db()
    with lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
            (scene_id, repo_key))
        db.commit()
    invalidate_scene_cache(scene_id, repo_key)

    # 自检默认场景：仓库 dev_scene 正好指向被删场景 → 清空（回到智能推荐）。
    # 不清就会留悬空指针：建单预校验 / 派发读到幽灵 id，或回落内置 default
    # 被其 sre 节点拦死（beta 实测：删场景 17 秒后建单被拦）。
    try:
        from src.domains.mgmt.user.db import set_repo_scene
        _dbr, _lockr = _db()
        _cur_dev = ""
        with _lockr:
            _cr = _dbr.cursor()
            _cr.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            _rowr = _cr.fetchone()
            _cur_dev = (_rowr[0] or "") if _rowr else ""
        if _cur_dev == scene_id:
            set_repo_scene(repo_key, "")
            log.info(f"RepoScene: cleared dangling dev_scene={scene_id} for {repo_key}")
    except Exception as _e:
        log.warning(f"RepoScene: clear dangling dev_scene failed ({repo_key}/{scene_id}): {_e}")

    # 自检推荐配置：清理指向被删场景的规则/兜底
    try:
        from src.kernel.prompt_builder import _sanitize_recommend_config
        _sanitize_recommend_config(repo_key)
    except Exception:
        pass


def apply_market_scene_by_name(repo_key: str, scene_name: str, user_id: int = 0) -> Optional[str]:
    """按场景名从 scene_market 查 market_id 并应用到仓库，返回仓内副本真实 scene_id。

    供模板创建流程按模板 scene_id 覆盖默认场景使用（Issue #541：回传真实 ID，
    调用方写 managed_repos.dev_scene 时不再误写模板场景名）。
    幂等：仓库已有同 market_scene_id 的场景则不重复创建。
    返回 None 表示失败（market 模板名查不到 / repo_key 无效 / 应用被拒）；
    truthiness 语义与旧 bool 返回兼容（session_store 布尔用法不受影响）。
    """
    from src.kernel.repo_key import from_repo_key

    # 按 name 查 scene_market 拿 market_id
    db, lock = _db()
    with lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM scene_market WHERE name=? AND source='official' AND is_active=1 LIMIT 1",
            (scene_name,),
        )
        row = c.fetchone()
    if not row:
        log.warning(f"apply_market_scene_by_name: market scene '{scene_name}' not found for {repo_key}")
        return None
    market_id = row[0]

    # 从 repo_key 拆 owner/repo（apply_market_scene 需要这两个参数）
    full_name = from_repo_key(repo_key)  # "XOcto/repo-name"
    parts = full_name.split("/", 1)
    if len(parts) != 2:
        log.warning(f"apply_market_scene_by_name: invalid repo_key '{repo_key}'")
        return None
    owner, repo_name = parts

    # 调 apply_market_scene，走场景中心完整逻辑
    # 传入 repo_key 避免 apply_market_scene 内部用 to_repo_key 默认 gitcode: 前缀
    from src.domains.mgmt.routes.scenes import apply_market_scene
    result = apply_market_scene(market_id, user={"id": user_id}, owner=owner, repo=repo_name, repo_key=repo_key)
    log.info(f"apply_market_scene_by_name: applied market scene {market_id} ({scene_name}) to {repo_key}: {result}")
    # Issue #541：提取仓内副本真实 scene_id（weekly-09：每次应用生成 custom-{uuid}）
    real_scene_id = ""
    if isinstance(result, dict):
        real_scene_id = str(result.get("scene_id") or "")
    if not real_scene_id:
        log.warning(f"apply_market_scene_by_name: no scene_id in apply result for {repo_key}: {result}")
        return None
    return real_scene_id


def scene_exists_in_repo(scene_id: str, repo_key: str) -> bool:
    """校验 scene_id 是否为该仓库内已存在的场景副本（devops_pipeline_scene 行）。

    Issue #541 派发侧/写侧共用兜底：仓内查不到的 scene_id 不应被静默当作
    有效绑定（模板名误写场景的判定依据）。空值返回 False。
    """
    if not scene_id or not repo_key:
        return False
    from src.domains.mgmt import public as _public
    try:
        # 走本域任意拓扑原语：api / runtime 进程调用时进程主库没有本表（视图 0），
        # 原先直发 SQL 会 1146 被吞成 False，把「仓内存在」误判成「不存在」。
        return bool(_public.scene_id_exists_any(scene_id, repo_key))
    except Exception as e:
        log.warning(f"scene_exists_in_repo: query failed: {e}")
        return False


def scene_pool_binding_warning(scene_id: str, repo_key: str) -> str:
    """Issue #3790：场景未在仓内 scene pool 登记时的保存/建仓提示（空串 = 已登记）。

    与派发侧 `get_scene_pool_engine_ids` 的 scene_missing 判定同源（同一个
    `scene_exists_in_repo`）：场景里绑的 scene_id 不是该仓库的副本（典型为误绑模板
    场景名）时，派发会跳过全局模板回退并降级为 scene_missing，运营只能看到「引擎离线」。
    写侧只提示不阻断——历史直写路径仍需落库（派发侧另有 fail-closed 防线）。
    """
    if not scene_id or not repo_key:
        return ""
    try:
        if scene_exists_in_repo(scene_id, repo_key):
            return ""
    except Exception as e:
        log.warning(f"scene_pool_binding_warning: check failed ({scene_id}/{repo_key}): {e}")
        return ""
    return (f"场景 {scene_id} 未在仓库 {repo_key} 的 scene pool 登记（scene_missing）："
            f"派发将跳过全局模板回退并降级为无引擎派发，请先应用/创建仓内场景副本")


def _query_official_scene_ids() -> set[str]:
    """Return set of official scene_ids available in scene_market(source='official')."""
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT DISTINCT name FROM scene_market "
                "WHERE source='official' AND is_active=1")
            return {r[0] for r in c.fetchall()}
        except Exception as e:
            log.warning(f"_query_official_scene_ids failed: {e}")
            return set()


# Issue #3564：实例绑定优先级档位常量（唯一真源；service / routes / copy_scene 共用）。
ENGINE_PRIORITY_MIN = 1   # 最高档
ENGINE_PRIORITY_MAX = 9  # 最低档
ENGINE_PRIORITY_DEFAULT = 9  # 未配置优先级的引擎默认按最低档处理


def normalize_engine_priorities(raw, allowed_ids=None, strict: bool = False) -> dict[str, int]:
    """Issue #3564：engine_priorities 归一化唯一真源（4 处调用收敛：写侧校验 /
    get_scene_engine_binding 读侧 / copy_scene 清洗 / runtime service 任务侧）。

    - raw 非 dict：strict 抛 ValueError，非 strict 返回 {}；
    - 键必须可 int 化，且（allowed_ids 非 None 时）在绑定引擎集合内：孤儿键不产生
      派发语义；allowed_ids 显式传空集合 = 不允许任何键（节点无绑定引擎时优先级整体拒绝/丢弃）；
    - 值必须为 ENGINE_PRIORITY_MIN..ENGINE_PRIORITY_MAX 整数（1 最高）；
    - strict=True：任一非法条目抛 ValueError（保存侧拒绝，避免落库后静默失效）；
      strict=False：非法条目静默丢弃（读侧 / 复制清洗兼容手写与历史 JSON）。
    返回 {str(engine_id): priority}，空 dict 表示未配置（旧行为）。
    """
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("engine_priorities 必须为对象（engine_id → 1..9）")
        return {}
    filter_keys = allowed_ids is not None
    allowed: set[str] = set()
    for _a in (allowed_ids or []):
        try:
            allowed.add(str(int(_a)))
        except (TypeError, ValueError):
            log.warning("normalize_engine_priorities: skip non-numeric allowed id %r", _a)
            continue
    out: dict[str, int] = {}
    for _k, _v in raw.items():
        try:
            _eid, _p = int(_k), int(_v)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"engine_priorities 含非法键/值：{_k!r}={_v!r}")
            continue
        if filter_keys and str(_eid) not in allowed:
            if strict:
                raise ValueError(f"优先级引擎 {_eid} 不在已绑定引擎列表中")
            continue
        if not (ENGINE_PRIORITY_MIN <= _p <= ENGINE_PRIORITY_MAX):
            if strict:
                raise ValueError(f"引擎 {_eid} 优先级必须为 1..9（1 最高）")
            continue
        out[str(_eid)] = _p
    return out


def get_scene_engine_binding(scene_id: str, agent_type: str, repo_key: str = "",
                             user_id: int = 0) -> dict:
    """Issue #653：解析场景节点的 engine_runtime_ids 绑定状态（空值语义单点收口）。

    返回 {"status": <状态>, "engine_ids": [...], "engine_uids": [...],
    "engine_priorities": {...}}，status 四态：
    - "no_scene"：场景配置整体不存在（查询失败 / 未创建）
    - "no_node" ：场景存在但无该 agent 节点
    - "unbound" ：节点存在但引擎绑定字段全空（未配置 → 保持全量广播）
    - "bound"  ：节点显式配置了非空绑定（ids 或 uids，派发硬约束）

    Issue #1208 读侧（D11/D12）：engine_runtime_uids 非空优先（uid 池路径，ids 忽略
    ——双非空由保存端拒绝，此处以 uids 为权威防双轨漂移），空则回落 engine_runtime_ids
    （legacy 只读兼容）。存量 id 节点 uids 恒空 → 读侧行为零变化。

    Issue #3564：engine_priorities 为节点旁挂的实例绑定优先级映射
    （{str(engine_id): 1..9}，1 最高）；空 dict = 未配置 = 旧行为，调用方不得
    据此改变认领顺序。优先级仅对 engine_runtime_ids 实例绑定生效（uid 池不产
    生机器粒度排序，恒为空）。

    此前 get_scene_engine_ids 对「节点不存在/查询失败」与「未配置」均返回 []，
    派发侧无法区分，已配置节点解析落空时被静默当作未配置回退全局池（#653 根因）。
    """
    _empty = {"status": "no_scene", "engine_ids": [], "engine_uids": [],
              "engine_priorities": {}}
    try:
        cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    except Exception as e:
        log.warning(f"get_scene_engine_binding: scene query failed: {e}")
        return _empty
    if not cfg:
        return _empty
    # pipelines.json default 兜底（fallback=True）：请求的 scene_id 未解析到真实场景
    # → 视为 no_scene（Issue #653：不得把兜底 default 节点当作真实绑定语义）
    if cfg.get("fallback"):
        return _empty
    node = None
    # Issue #955：id 优先、tag 兜底——节点 id 即角色名（agent_type），tag 为展示
    # 阶段名；非标准 tag（如中文阶段名）的存量场景按 id 匹配恢复引擎绑定
    from src.domains.runtime.public import node_matches_agent_role
    for n in cfg.get("nodes") or []:
        if node_matches_agent_role(n, agent_type):
            node = n
            break
    if node is None:
        return {"status": "no_node", "engine_ids": [], "engine_uids": [],
                "engine_priorities": {}}
    uids = [str(u or "").strip().lower() for u in (node.get("engine_runtime_uids") or [])
            if str(u or "").strip()]
    out_ids = []
    for _x in (node.get("engine_runtime_ids") or []):
        try:
            _xi = int(_x)
        except (TypeError, ValueError):
            log.warning("resolve_node_engines: skip non-numeric engine_runtime_ids entry %r", _x)
            continue
        if _xi:
            out_ids.append(_xi)
    if not uids and not out_ids:
        return {"status": "unbound", "engine_ids": [], "engine_uids": [],
                "engine_priorities": {}}
    if uids:
        return {"status": "bound", "engine_ids": [], "engine_uids": uids,
                "engine_priorities": {}}
    priorities = normalize_engine_priorities(
        node.get("engine_priorities"), set(out_ids))
    return {"status": "bound", "engine_ids": out_ids, "engine_uids": [],
            "engine_priorities": priorities}


def get_scene_engine_ids(scene_id: str, agent_type: str, repo_key: str = "", user_id: int = 0) -> list[int]:
    """Get engine_runtime_ids from a scene's node for a specific agent_type.

    Returns empty list if no scene found, node not found, or node has no engine binding.
    （Issue #653：需要区分空值语义时请改用 get_scene_engine_binding。）
    """
    return get_scene_engine_binding(scene_id, agent_type, repo_key=repo_key, user_id=user_id)["engine_ids"]
