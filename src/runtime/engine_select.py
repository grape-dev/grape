"""引擎选择 / 派发。

核心模型：每个场景 node 的 engine_runtime_ids 指定该 (场景, agent) 可用的引擎列表。
- pick_engine：plugin 模式统一返回 0（广播），engine_runtime_ids 写入 pool_engine_ids 限定范围；
  legacy 模式按「专属（仓库级）→ 全局 → 本机直连」优先级 + 健康过滤从列表选一个引擎；
  列表为空时回退会话主引擎（session.runtime_id），无则 None（engine_unavailable）。
- 本机直连（credential_id=0）为最低优先级成员：选中即 direct；远端引擎入队该引擎（确定路由）。
"""
import os
import json
import random
import time as _time
from src.config import CONFIG_DIR, log

_ENGINE_SELECTION_PATH = os.path.join(CONFIG_DIR, "engine-selection.json")

# resolve_local_engine 结果缓存（30s TTL）——避免每条飞书消息都查 DB agent_runtimes 表
# （handler_async + chat.py 两处各调一次，叠加 _remote_runtime_ready 再查，单条消息 5+ 次 DB 查询）
_resolve_local_cache: dict = {}  # {"result": int|None, "expires": float}
_RESOLVE_LOCAL_TTL = 30.0

# 远端引擎心跳超时阈值（秒）：last_seen_at 超过此值视为死引擎（AI DevSpace 被删后残留 runtime）。
# 与 service.py _RUNTIME_STALE_SECONDS 保持一致。
_RUNTIME_STALE_SECONDS = int(os.environ.get("RUNTIME_STALE_SECONDS", "300"))

# 默认优先级档位（小=高优先）
_PRIO_REPO = 10
_PRIO_GLOBAL = 50
_PRIO_LOCAL = 90

_DEFAULT_CFG = {
    "enabled": True,
    "scopes": ["repo", "global", "local"],
    "rule": "order",
    "filters": {"only_online": True, "only_healthy": True, "only_installed": True},
    "local_priority": _PRIO_LOCAL,
}


def load_engine_selection() -> dict:
    """读 configs/engine-selection.json；缺失/损坏回默认（默认也是"必选已注册引擎"，不回落 opencode）。"""
    try:
        if os.path.exists(_ENGINE_SELECTION_PATH):
            with open(_ENGINE_SELECTION_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg = dict(_DEFAULT_CFG)
                cfg.update(data)
                return cfg
    except Exception as e:
        log.warning(f"load_engine_selection failed: {e}")
    return dict(_DEFAULT_CFG)


def _norm_repo(repo_key: str) -> str:
    """仓库标识归一化为 owner/repo（与 runtime_credentials.owner_id 对齐）。"""
    from src.repo_key import from_repo_key
    return (from_repo_key(repo_key or "") or "").strip()


def engine_name(runtime_id: int) -> str:
    """按已注册引擎 id 反查类型名（agent_runtimes.name）；查不到返回空串。

    派发选中引擎后，执行 backend 必须取该已注册引擎的类型名（engine-selection-design.md v2），
    而非会话/调用方传来的字面默认。
    """
    if not runtime_id:
        return ""
    db, lock = _db()
    with lock:
        try:
            c = db.cursor()
            c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
            row = c.fetchone()
        except Exception:
            return ""
    return row[0] or "" if row else ""


def _db():
    from src.session_manager import db, db_lock
    return db, db_lock


def _member_scope(m: dict, repo: str) -> str:
    """成员档位：repo（仓库级专属）/ global（远端全局）/ local（本机直连）。"""
    cid = m.get("credential_id") or 0
    if cid == 0:
        return "local"                       # 本机直连（默认视为全局，但归 local 档位最后兜底）
    if m.get("scope") == "repo":
        from src.repo_key import from_repo_key
        oid = from_repo_key(m.get("owner_id") or "")
        if oid and oid == repo:
            return "repo"
    return "global"


def _health_ok(m: dict) -> bool:
    """健康判定：远端按 status='online' + last_seen_at 新鲜；本机按 installed+healthy。

    远端引擎（credential_id>0）必须 last_seen_at 在 _RUNTIME_STALE_SECONDS 内才算健康，
    防止 AI DevSpace 环境被删后 runtime 行残留 status=online 但实际不可达（模式 QQ）。
    """
    cid = m.get("credential_id") or 0
    if cid == 0:
        return bool(m.get("installed")) and bool(m.get("healthy"))
    if (m.get("status") or "") != "online":
        return False
    # 远端引擎检查心跳新鲜度
    last_seen = m.get("last_seen_at") or ""
    if last_seen:
        from src.runtime.health import parse_dt
        import time as _time
        ts = parse_dt(last_seen)
        if ts is not None and _time.time() - ts > _RUNTIME_STALE_SECONDS:
            return False
    return True


def _apply_filters(members: list[dict], cfg: dict) -> list[dict]:
    f = cfg.get("filters") or {}
    out = []
    for m in members:
        if not m.get("enabled", True):
            continue
        if f.get("only_installed") and not m.get("installed"):
            continue
        if f.get("only_healthy") and not m.get("healthy"):
            continue
        if f.get("only_online") and not _health_ok(m):
            continue
        # 本机直连引擎（credential_id=0）必须为已知引擎类型（防幽灵 `default-cred0` 占位，见 engine-selection-design.md §7.3）
        if (m.get("credential_id") or 0) == 0:
            from src.user.db import is_known_engine_name
            if not is_known_engine_name(m.get("name") or ""):
                continue
        out.append(m)
    return out


# ===== 引擎档位 / 优先级（供 pick_engine 多引擎选择）=====

def _repo_user_id(repo_key: str) -> int:
    """从 user_repos 表反查 repo_key 对应的 user_id（取第一个 join 的用户）。"""
    if not repo_key:
        return 0
    repo = _norm_repo(repo_key)
    db, lock = _db()
    with lock:
        try:
            c = db.cursor()
            # repo_key 在 user_repos 里可能带 gitcode: 前缀，用 LIKE 匹配
            c.execute(
                "SELECT user_id FROM user_repos WHERE (repo_key=? OR repo_key=?) AND is_join=1 ORDER BY user_id LIMIT 1",
                (repo, f"gitcode:{repo}"))
            row = c.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0


def _engine_scope_info(runtime_id: int) -> tuple[str, str]:
    """返回引擎行档位 (scope, owner_id)：本机直连（credential_id=0）→ local；失败返回 ('global', '')。"""
    db, lock = _db()
    with lock:
        try:
            c = db.cursor()
            c.execute(
                "SELECT r.scope, r.owner_id, r.credential_id, COALESCE(rc.scope,''), COALESCE(rc.owner_id,'') "
                "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id WHERE r.id=?",
                (runtime_id,))
            row = c.fetchone()
        except Exception:
            return "global", ""
    if not row:
        return "global", ""
    if (row[2] or 0) == 0:
        return "local", ""
    return row[3] or row[0] or "global", row[4] or row[1] or ""


def _default_priority(runtime_id: int, repo: str, cfg: dict) -> int:
    """按档位派生默认优先级：repo=10 / global=50 / local=local_priority。"""
    scope, owner_id = _engine_scope_info(runtime_id)
    from src.repo_key import from_repo_key
    if scope == "repo" and from_repo_key(owner_id or "") == repo:
        return _PRIO_REPO
    return int(cfg.get("local_priority", _PRIO_LOCAL)) if scope == "local" else _PRIO_GLOBAL


# ===== 派发选择 =====

def _engines_by_ids(engine_ids: list[int], repo: str, cfg: dict) -> list[dict]:
    """按 runtime_id 列表查引擎详情（含 scope/priority），供 pick_engine 多引擎选择。"""
    if not engine_ids:
        return []
    db, lock = _db()
    members = []
    with lock:
        c = db.cursor()
        try:
            placeholders = ",".join("?" * len(engine_ids))
            c.execute(
                "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                " r.scope, r.owner_id, "
                " COALESCE(rc.device_name, ''), COALESCE(rc.owner_name, '') "
                "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id "
                f"WHERE r.id IN ({placeholders})",
                tuple(engine_ids))
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"_engines_by_ids failed: {e}")
            return []
    for r in rows:
        rid = r[0]
        members.append({
            "runtime_id": rid, "name": r[1] or "", "status": r[2] or "offline", "healthy": bool(r[3]),
            "installed": bool(r[4]), "credential_id": r[5] or 0, "last_seen_at": r[6] or "",
            "scope": r[7] or "global", "owner_id": r[8] or "", "device_name": r[9] or "",
            "owner_name": r[10] or "", "priority": _default_priority(rid, repo, cfg),
        })
    return members


def _filter_by_tags(members: list[dict], agent_type: str) -> list[dict]:
    """过滤绑定集合，只保留标签匹配 agent_type 的引擎。"""
    if not agent_type or not members:
        return members
    db, lock = _db()
    result = []
    with lock:
        c = db.cursor()
        for m in members:
            rid = m.get("runtime_id")
            if not rid:
                continue
            c.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rid,))
            tags = [r[0] for r in c.fetchall()]
            if agent_type in tags or "all" in tags:
                result.append(m)
    return result


def pick_engine(repo_key: str, agent_type: str, fallback_runtime_id: int = 0,
                scene_id: str = "", user_id: int = 0) -> int | None:
    """从场景 node 的 engine_runtime_ids 选引擎；为空回退会话主引擎（legacy）。

    返回 None = engine_unavailable（调用方跳过执行）。
    返回 0 = 无主池广播（plugin 模式），场景 engine_runtime_ids 限定广播范围（通过 pool_engine_ids 字段）。

    plugin 模式：统一返回 0（广播），不读任何绑定。
    legacy 模式：engine_runtime_ids 非空 → 按优先级选健康引擎；为空 → 回退 fallback_runtime_id。
    """
    cfg = load_engine_selection()
    if not cfg.get("enabled", True):
        return None
    # Plugin 模式：统一广播，engine_runtime_ids 由 get_scene_pool_engine_ids 写入 pool_engine_ids
    try:
        from src.api.event_pipeline import is_plugin_mode
        if is_plugin_mode():
            return 0
    except Exception:
        pass
    # Legacy 模式：从场景 node 读 engine_runtime_ids
    repo = _norm_repo(repo_key)
    if not user_id:
        user_id = _repo_user_id(repo_key)
    if scene_id:
        try:
            from src.api.pipeline_config import get_scene_engine_ids
            engine_ids = get_scene_engine_ids(scene_id, agent_type, repo_key=repo, user_id=user_id)
        except Exception:
            engine_ids = []
    else:
        engine_ids = []
    if engine_ids:
        # 多引擎选择：查引擎详情，按优先级选健康引擎
        members = _engines_by_ids(engine_ids, repo, cfg)
        members = _filter_by_tags(members, agent_type)
        if members:
            for scope in cfg.get("scopes") or ["repo", "global", "local"]:
                cand = _apply_filters([m for m in members if _member_scope(m, repo) == scope], cfg)
                if not cand:
                    continue
                if cfg.get("rule") == "order":
                    return min(cand, key=lambda r: (r["priority"], r["runtime_id"]))["runtime_id"]
                return random.choice(cand)["runtime_id"]
            return None     # 集合存在但无健康成员 → engine_unavailable
    # engine_runtime_ids 为空 → 回退会话主引擎（legacy）
    if fallback_runtime_id:
        return int(fallback_runtime_id)
    return None


def get_scene_pool_engine_ids(scene_id: str, agent_type: str, repo_key: str = "", user_id: int = 0) -> str:
    """获取限定广播引擎列表，返回 JSON 字符串供 agent_tasks.pool_engine_ids 使用。

    只从场景 node 的 engine_runtime_ids 读取。
    node 不存在或 engine_runtime_ids 为空 → 空字符串（全量广播）。
    """
    import json
    if not user_id:
        user_id = _repo_user_id(repo_key)
    if scene_id:
        try:
            from src.api.pipeline_config import get_scene_engine_ids
            ids = get_scene_engine_ids(scene_id, agent_type, repo_key=repo_key, user_id=user_id)
            if ids:
                return json.dumps(ids)
        except Exception as e:
            log.warning(f"get_scene_pool_engine_ids: scene query failed: {e}")
    return ""


def resolve_local_engine() -> int | None:
    """返回一个健康的本机直连引擎（credential_id=0 且 installed+healthy）；无则 None。

    用于 chat 等未显式绑定时的本机兜底（engine-selection-design.md §8.2）。
    无本机直连时回退全局健康引擎（credential_id>0 的远端 worker，chat 走队列执行），
    避免"无可用引擎"硬失败（#60：清理设备后无 cred0 引擎，chat 全挂）。

    结果缓存 30 秒——引擎列表不频繁变化，缓存可大幅减少 DB 查询。
    """
    now = _time.time()
    cached = _resolve_local_cache.get("expires", 0)
    if cached > now:
        return _resolve_local_cache.get("result")
    cand = query_engines("", "local")
    cand = _apply_filters([dict(m, scope="local") for m in cand], load_engine_selection())
    if not cand:
        # 回退：任意健康全局引擎（chat 未配置时可用远端 worker 引擎入队执行）
        cand = query_engines("", "global")
        cand = _apply_filters([dict(m, scope="global") for m in cand], load_engine_selection())
    if not cand:
        return None
    result = min(cand, key=lambda r: (r["runtime_id"]))["runtime_id"]
    _resolve_local_cache["result"] = result
    _resolve_local_cache["expires"] = _time.time() + _RESOLVE_LOCAL_TTL
    return result


def resolve_default_engine(repo_key: str, agent_type: str = "") -> int | None:
    """托管默认选择：按 专属→全局→本机直连 优先级从仓库可选引擎中选一个（用于种子绑定集合）。

    不依赖既有绑定集合；无候选返回 None（集合留空，agent 标识"无 engine，不可用"）。
    """
    cfg = load_engine_selection()
    if not cfg.get("enabled", True):
        return None
    for scope in cfg.get("scopes") or ["repo", "global", "local"]:
        cand = query_engines(repo_key, scope, agent_type=agent_type)
        cand = [dict(m, scope=scope) for m in cand]
        cand = _apply_filters(cand, cfg)
        if not cand:
            continue
        if cfg.get("rule") == "order":
            return min(cand, key=lambda r: (r["runtime_id"]))["runtime_id"]
        return random.choice(cand)["runtime_id"]
    return None


# ===== 候选引擎查询（供绑定配置下拉 / 展示）=====

def query_engines(repo_key: str, scope: str, agent_type: str = "", username: str = "") -> list[dict]:
    """返回某仓库在某档位的候选引擎：repo=仓库级；global=远端全局；local=本机直连。
    agent_type 非空时，只返回标签匹配的引擎（tags 包含 agent_type 或 "all"）。
    username 非空时，user scope 只返回该用户的个人引擎。
    repo scope 返回用户加入的所有仓库的仓库级引擎（不只当前仓库）。"""
    repo = _norm_repo(repo_key)
    db, lock = _db()
    # 收集用户加入的仓库列表（用于 repo scope 过滤）
    user_repos = set()
    if username and scope == "repo":
        try:
            from src.session_manager import db as _udb, db_lock as _ulock
            from src.repo_key import from_repo_key
            with _ulock:
                uc = _udb.cursor()
                uc.execute(
                    "SELECT ur.repo_key FROM user_repos ur "
                    "JOIN user_accounts ua ON ua.id = ur.user_id "
                    "WHERE ua.username = ? AND ur.is_join = 1", (username,))
                for row in uc.fetchall():
                    user_repos.add(from_repo_key(row[0] or ""))
        except Exception as _e:
            log.warning(f"query_engines: get_user_repos failed: {_e}")
    with lock:
        c = db.cursor()
        try:
            if scope == "repo":
                c.execute(
                    "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                    " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                    " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1) "
                    "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                    "WHERE r.credential_id>0 AND rc.scope='repo' AND rc.status='active' ORDER BY r.id")
            elif scope == "local":
                # 本机引擎（credential_id=0）对所有人可见
                c.execute(
                    "SELECT id, name, status, healthy, installed, credential_id, last_seen_at, '', '', '',"
                    " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=agent_runtimes.id AND enabled=1) "
                    "FROM agent_runtimes WHERE credential_id=0 ORDER BY id")
            elif scope == "user":
                if username:
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1) "
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='user' AND rc.status='active' AND rc.owner_name=? ORDER BY r.id", (username,))
                else:
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1) "
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='user' AND rc.status='active' ORDER BY r.id")
            else:
                c.execute(
                    "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                    " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                    " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1) "
                    "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                    "WHERE r.credential_id>0 AND rc.scope='global' AND rc.status='active' ORDER BY r.id")
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"query_engines({scope}) failed: {e}")
            return []
    out = []
    for r in rows:
        if scope == "repo":
            from src.repo_key import from_repo_key
            _repo_owner = from_repo_key(r[9] or "")
            # 只返回用户加入的仓库的仓库级引擎
            if username and user_repos and _repo_owner not in user_repos:
                continue
        # agent_type 标签过滤（r[10] = GROUP_CONCAT(tag) from engine_capabilities, e.g. "all" or "all,coding"）
        if agent_type:
            tags_str = r[10] or ""
            tags = [t.strip() for t in tags_str.split(",") if t.strip()]
            if agent_type not in tags and "all" not in tags:
                continue
        out.append({
            "runtime_id": r[0], "name": r[1], "status": r[2] or "offline", "healthy": bool(r[3]),
            "installed": bool(r[4]), "credential_id": r[5] or 0, "last_seen_at": r[6] or "",
            "device_name": r[7] or "", "owner_name": r[8] or "",
            "owner_id": r[9] or "",
            "scope": scope,
            "tags": [t.strip() for t in (r[10] or "").split(",") if t.strip()],
        })
    return out
