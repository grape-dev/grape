"""引擎选择 / 派发（见 engine-selection-design.md v2）。

核心模型：每个 (repo_key, agent_type) 绑定一组引擎（引擎集合，存 devops_agent_runtime_binding）。
- 集合成员可选面：仓库级引擎（owner_id=该仓库）∪ 全局引擎（含本机直连 credential_id=0）。
- pick_engine：按「专属（仓库级）→ 全局 → 本机直连」优先级 + 健康过滤，从集合选一个引擎派发；
  集合为空时回退会话主引擎（legacy），否则返回 None（engine_unavailable，绝不回落硬编码默认）。
- 本机直连（credential_id=0）为最低优先级成员：选中即 direct；远端引擎入队该引擎（确定路由）。
"""
import os
import json
import random
from src.config import CONFIG_DIR, log

_ENGINE_SELECTION_PATH = os.path.join(CONFIG_DIR, "engine-selection.json")

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
    """健康判定：远端按 status='online'；本机按 installed+healthy（settings._runtime_health 同口径）。"""
    cid = m.get("credential_id") or 0
    if cid == 0:
        return bool(m.get("installed")) and bool(m.get("healthy"))
    return (m.get("status") or "") == "online"


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


# ===== 引擎集合（绑定表）读写 =====

def get_binding(repo_key: str, agent_type: str) -> list[dict]:
    """返回 (repo, agent) 的引擎集合（含引擎实时信息与 scope/priority）。"""
    repo = _norm_repo(repo_key)
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT b.runtime_id, b.priority, b.enabled, "
                " r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, r.scope, r.owner_id, "
                " COALESCE(rc.device_name, ''), COALESCE(rc.owner_name, '') "
                "FROM devops_agent_runtime_binding b "
                "LEFT JOIN agent_runtimes r ON r.id = b.runtime_id "
                "LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id "
                "WHERE b.repo_key=? AND b.agent_type=? ORDER BY b.priority ASC, b.runtime_id ASC",
                (repo, agent_type))
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"get_binding failed: {e}")
            return []
    return [{
        "runtime_id": r[0], "priority": r[1] or 0, "enabled": bool(r[2]),
        "name": r[3] or "", "status": r[4] or "offline", "healthy": bool(r[5]),
        "installed": bool(r[6]), "credential_id": r[7] or 0, "last_seen_at": r[8] or "",
        "scope": r[9] or "global", "owner_id": r[10] or "", "device_name": r[11] or "",
        "owner_name": r[12] or "",
    } for r in rows]


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


def _busy_slots(runtime_id: int) -> int:
    """返回引擎当前实际活跃任务数（实时 COUNT，与 claim_by_tag 并发检查一致）。

    不使用 busy_slots 字段（该字段在 kill -9/崩溃/快速失败场景下只增不减导致脏数据）。
    """
    db, lock = _db()
    with lock:
        try:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM agent_tasks WHERE runtime_id=? AND status IN ('dispatched','running')",
                (runtime_id,)
            )
            row = c.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0


def _pick_least_busy(cand: list[dict]) -> dict:
    """从候选引擎中选繁忙度最低的（同档位内负载均衡）。

    排序键：(priority, actual_busy, runtime_id)
    - priority 优先（repo < global < local 档位）
    - actual_busy 次之（实时 COUNT 活跃任务数，选空闲最多的）
    - runtime_id 末位（稳定排序兜底）
    """
    return min(cand, key=lambda r: (r.get("priority", 0), _busy_slots(r["runtime_id"]), r["runtime_id"]))


def set_binding(repo_key: str, agent_type: str, runtime_ids: list[int], priorities: dict | None = None):
    """替换 (repo, agent) 的引擎集合（先删后插，单事务）。空列表 = 清空集合。"""
    repo = _norm_repo(repo_key)
    cfg = load_engine_selection()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db, lock = _db()
    with lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_runtime_binding WHERE repo_key=? AND agent_type=?", (repo, agent_type))
        for rid in (runtime_ids or []):
            rid = int(rid)
            if priorities and rid in priorities:
                pr = int(priorities[rid])
            else:
                pr = _default_priority(rid, repo, cfg)
            c.execute(
                "INSERT OR IGNORE INTO devops_agent_runtime_binding (repo_key, agent_type, runtime_id, priority, enabled, created_at) VALUES (?,?,?,?,1,?)",
                (repo, agent_type, rid, pr, now))
        db.commit()
    log.info(f"Binding set: {repo}:{agent_type} -> {runtime_ids or []}")


def clear_binding(repo_key: str, agent_type: str):
    """清空 (repo, agent) 的引擎集合。"""
    set_binding(repo_key, agent_type, [])


def backfill_existing_bindings() -> int:
    """把存量 `devops_agent_session.runtime_id>0` 的会话回填为绑定集合（单成员），幂等。

    engine-selection-design.md §4.4/§12：新模型上线前托管的存量仓库没有 `devops_agent_runtime_binding`
    行，导致 Configure/展示读不到引擎。仅作**一次性迁移**（meta 标志 `bindings_backfilled` 防重跑，
    避免覆盖用户后续改动）；priority 按引擎 scope 派生（repo=10/global=50/local=90）。
    """
    from src.repo_key import from_repo_key
    from src.user.db import get_meta_value, set_meta_value
    if get_meta_value(_BACKFILL_FLAG) == "1":
        return 0
    db, lock = _db()
    cfg = load_engine_selection()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    with lock:
        c = db.cursor()
        try:
            rows = c.execute(
                "SELECT repo_key, agent_type, runtime_id FROM devops_agent_session WHERE runtime_id IS NOT NULL AND runtime_id>0"
            ).fetchall()
        except Exception as e:
            log.warning(f"backfill_existing_bindings: query failed: {e}")
            return 0
        for repo_key, agent_type, runtime_id in rows:
            repo = from_repo_key(repo_key or "")
            if not repo or not agent_type:
                continue
            try:
                exists = c.execute(
                    "SELECT 1 FROM devops_agent_runtime_binding WHERE repo_key=? AND agent_type=? LIMIT 1",
                    (repo, agent_type)).fetchone()
                if exists:
                    continue
                pr = _default_priority(runtime_id, repo, cfg)
                c.execute(
                    "INSERT OR IGNORE INTO devops_agent_runtime_binding (repo_key, agent_type, runtime_id, priority, enabled, created_at) VALUES (?,?,?,?,1,?)",
                    (repo, agent_type, runtime_id, pr, now))
                added += 1
            except Exception as e:
                log.warning(f"backfill_existing_bindings: insert {repo}:{agent_type} failed: {e}")
        db.commit()
    try:
        set_meta_value(_BACKFILL_FLAG, "1")
    except Exception as e:
        log.warning(f"backfill_existing_bindings: mark flag failed: {e}")
    if added:
        log.info(f"backfill_existing_bindings: added {added} binding rows from existing sessions")
    return added


_BACKFILL_FLAG = "bindings_backfilled"


# ===== 派发选择 =====

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
            tags = [r[0] for r in c.fetchall()] or ["all"]
            if agent_type in tags or "all" in tags:
                result.append(m)
    return result


def pick_engine(repo_key: str, agent_type: str, fallback_runtime_id: int = 0) -> int | None:
    """按 专属→全局→本机直连 优先级从引擎集合选健康引擎；集合空回退会话主引擎（legacy）。

    返回 None = engine_unavailable（调用方跳过执行，禁止回落硬编码默认引擎）。

    Plugin 模式下绑定表为空时优先走广播（return 0），不走 fallback_runtime_id：
    fallback 来自 devops_agent_session.runtime_id（历史会话引擎），会绕过广播直接定向
    到旧引擎（如 0003 的 rt=54 有 121 个历史会话），导致 tag 限制失效。
    """
    cfg = load_engine_selection()
    if not cfg.get("enabled", True):
        return None
    repo = _norm_repo(repo_key)
    members = get_binding(repo_key, agent_type)
    if members:
        # 标签过滤：排除不匹配 agent_type 的引擎
        members = _filter_by_tags(members, agent_type)
    if members:
        for scope in cfg.get("scopes") or ["repo", "global", "local"]:
            cand = _apply_filters([m for m in members if _member_scope(m, repo) == scope], cfg)
            if not cand:
                continue
            if cfg.get("rule") == "order":
                return _pick_least_busy(cand)["runtime_id"]
            # random 模式：优先选实际空闲引擎（active=0），无空闲则选最少的
            _idle = [m for m in cand if _busy_slots(m["runtime_id"]) == 0]
            if _idle:
                return random.choice(_idle)["runtime_id"]
            return _pick_least_busy(cand)["runtime_id"]
        return None     # 集合存在但无健康成员 → engine_unavailable（不回退 fallback）
    # Plugin 模式：绑定表为空 → 直接走广播（return 0），不走 fallback_runtime_id。
    # fallback 来自历史会话，会绕过 tag 限制定向到旧引擎，破坏广播负载均衡。
    try:
        from src.api.event_pipeline import is_plugin_mode
        if is_plugin_mode():
            return 0
    except Exception:
        pass
    # 传统模式：无绑定 → 回退会话主引擎（legacy），无则 engine_unavailable
    if fallback_runtime_id:
        return int(fallback_runtime_id)
    return None


def resolve_local_engine() -> int | None:
    """返回一个健康的本机直连引擎（credential_id=0 且 installed+healthy）；无则 None。

    用于 chat 等未显式绑定时的本机兜底（engine-selection-design.md §8.2）。
    无本机直连时回退全局健康引擎（credential_id>0 的远端 worker，chat 走队列执行），
    避免"无可用引擎"硬失败（#60：清理设备后无 cred0 引擎，chat 全挂）。
    """
    cand = query_engines("", "local")
    cand = _apply_filters([dict(m, scope="local") for m in cand], load_engine_selection())
    if not cand:
        # 回退：任意健康全局引擎（chat 未配置时可用远端 worker 引擎入队执行）
        cand = query_engines("", "global")
        cand = _apply_filters([dict(m, scope="global") for m in cand], load_engine_selection())
    if not cand:
        return None
    return min(cand, key=lambda r: (r["runtime_id"]))["runtime_id"]


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
            return _pick_least_busy(cand)["runtime_id"]
        return random.choice(cand)["runtime_id"]
    return None


# ===== 候选引擎查询（供绑定配置下拉 / 展示）=====

def query_engines(repo_key: str, scope: str, agent_type: str = "") -> list[dict]:
    """返回某仓库在某档位的候选引擎：repo=仓库级；global=远端全局；local=本机直连。
    agent_type 非空时，只返回标签匹配的引擎（tags 包含 agent_type 或 "all"）。"""
    repo = _norm_repo(repo_key)
    db, lock = _db()
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
                c.execute(
                    "SELECT id, name, status, healthy, installed, credential_id, last_seen_at, '', '', '',"
                    " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=agent_runtimes.id AND enabled=1) "
                    "FROM agent_runtimes WHERE credential_id=0 ORDER BY id")
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
            if from_repo_key(r[9] or "") != repo:
                continue
        # agent_type 标签过滤（r[10] = GROUP_CONCAT(tag) from engine_capabilities, e.g. "all" or "all,coding"）
        if agent_type:
            tags_str = r[10] or ""
            tags = [t.strip() for t in tags_str.split(",") if t.strip()] or ["all"]
            if agent_type not in tags and "all" not in tags:
                continue
        out.append({
            "runtime_id": r[0], "name": r[1], "status": r[2] or "offline", "healthy": bool(r[3]),
            "installed": bool(r[4]), "credential_id": r[5] or 0, "last_seen_at": r[6] or "",
            "device_name": r[7] or "", "owner_name": r[8] or "",
        })
    return out
