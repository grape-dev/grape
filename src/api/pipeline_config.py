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

from src.config import CONFIG_DIR, log

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
    from src.session_manager import db, db_lock
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
    """
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT name, display_name, description, nodes_json, onboarding FROM scene_market "
                "WHERE name=? AND source='official' AND is_active=1 LIMIT 1", (scene_id,))
            row = c.fetchone()
            if row:
                nodes = json.loads(row[3] or "[]") if row[3] else []
                return {
                    "scene_id": scene_id, "name": row[0],
                    "display_name": row[1] or row[0], "description": row[2] or "",
                    "nodes": nodes, "onboarding": row[4] or "repo-onboarding",
                }
        except Exception as e:
            log.warning(f"_load_official_scene_from_market failed: {e}")
    return None


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


def get_pipeline_config(scene_id: str = "default", repo_key: str = "") -> Optional[dict]:
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
    """
    # Check cache first
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
                "scene_id": fallback_source.get("scene_id") or scene_id,
                "fallback": fallback,
                "fallback_source": fallback_source,
            }
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
    from src.runtime.engine_select import node_matches_agent_role
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


def get_next_node(scene_id: str, current_node_id: str) -> Optional[dict]:
    """Return the next node after the current one, respecting skip list."""
    cfg = get_pipeline_config(scene_id)
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


def is_node_skipped(scene_id: str, node_id: str) -> bool:
    """Check if a node is in any other node's skip list."""
    cfg = get_pipeline_config(scene_id)
    if not cfg:
        return False
    for node in cfg.get("nodes", []):
        skip = node.get("skip", [])
        if node_id in skip:
            return True
    return False


# ===== Scene CRUD (for custom scenes) =====

def create_custom_scene(scene_id: str, name: str, description: str,
                        repo_key: str, nodes: list, onboarding: str = "repo-onboarding",
                        display_name: str = "", created_by: str = "", icon: str = "",
                        scene_type: str = "standard", pr_subscription_enabled: int = 0,
                        linked_repos: list = None) -> int:
    """Create or reactivate a repo-scoped custom scene in DB. Returns DB id."""
    db, lock = _db()
    nodes_json = json.dumps(nodes, ensure_ascii=False)
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
    nodes_json = json.dumps(nodes, ensure_ascii=False)
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

    # 自检推荐配置：清理指向被删场景的规则/兜底
    try:
        from src.prompt_builder import _sanitize_recommend_config
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
    from src.repo_key import from_repo_key

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
    from src.api.routes.scenes import apply_market_scene
    try:
        result = apply_market_scene(market_id, user={"id": user_id}, owner=owner, repo=repo_name, repo_key=repo_key)
    except Exception as _e:
        # apply_market_scene 以 HTTPException 表达业务拒绝（如技能缺失/重名 409）
        log.warning(f"apply_market_scene_by_name: apply market scene {market_id} ({scene_name}) "
                    f"to {repo_key} rejected: {_e}")
        return None
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
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT 1 FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            return c.fetchone() is not None
        except Exception as e:
            log.warning(f"scene_exists_in_repo: query failed: {e}")
            return False


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


def get_scene_engine_binding(scene_id: str, agent_type: str, repo_key: str = "",
                             user_id: int = 0) -> dict:
    """Issue #653：解析场景节点的 engine_runtime_ids 绑定状态（空值语义单点收口）。

    返回 {"status": <状态>, "engine_ids": [...]}，status 四态：
    - "no_scene"：场景配置整体不存在（查询失败 / 未创建）
    - "no_node" ：场景存在但无该 agent 节点
    - "unbound" ：节点存在但 engine_runtime_ids 为空（未配置 → 保持全量广播）
    - "bound"  ：节点显式配置了非空 engine_runtime_ids（派发硬约束）

    此前 get_scene_engine_ids 对「节点不存在/查询失败」与「未配置」均返回 []，
    派发侧无法区分，已配置节点解析落空时被静默当作未配置回退全局池（#653 根因）。
    """
    try:
        cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    except Exception as e:
        log.warning(f"get_scene_engine_binding: scene query failed: {e}")
        return {"status": "no_scene", "engine_ids": []}
    if not cfg:
        return {"status": "no_scene", "engine_ids": []}
    # pipelines.json default 兜底（fallback=True）：请求的 scene_id 未解析到真实场景
    # → 视为 no_scene（Issue #653：不得把兜底 default 节点当作真实绑定语义）
    if cfg.get("fallback"):
        return {"status": "no_scene", "engine_ids": []}
    node = None
    # Issue #955：id 优先、tag 兜底——节点 id 即角色名（agent_type），tag 为展示
    # 阶段名；非标准 tag（如中文阶段名）的存量场景按 id 匹配恢复引擎绑定
    from src.runtime.engine_select import node_matches_agent_role
    for n in cfg.get("nodes") or []:
        if node_matches_agent_role(n, agent_type):
            node = n
            break
    if node is None:
        return {"status": "no_node", "engine_ids": []}
    ids = node.get("engine_runtime_ids") or []
    if not ids:
        return {"status": "unbound", "engine_ids": []}
    out_ids = []
    for _x in ids:
        try:
            _xi = int(_x)
        except (TypeError, ValueError):
            continue
        if _xi:
            out_ids.append(_xi)
    return {"status": "bound", "engine_ids": out_ids}


def get_scene_engine_ids(scene_id: str, agent_type: str, repo_key: str = "", user_id: int = 0) -> list[int]:
    """Get engine_runtime_ids from a scene's node for a specific agent_type.

    Returns empty list if no scene found, node not found, or node has no engine binding.
    （Issue #653：需要区分空值语义时请改用 get_scene_engine_binding。）
    """
    return get_scene_engine_binding(scene_id, agent_type, repo_key=repo_key, user_id=user_id)["engine_ids"]
