"""Issue #1245: 建单前场景可用性预校验（静态只读）。

目标：在创建 Issue 之前，对所选场景中「存在且 enabled」的节点做四检：
1) 绑定引擎在 agent_runtimes 存在；
2) 至少一台可用（沿用派发链路的在线/心跳判定 + 健康分阈值）；
3) 引擎归属合法（user 档属创建者本人，repo 档属当前仓库）；
4) 节点技能对创建者可解析（skill_id 有效或 resolve_skill_id 命中）。

另有结构校验（next 悬空 / 节点 id 重复）与三类豁免（非 SRE 空绑定、
enabled=false、skill 为空）。查询/配置异常一律 fail-open，避免误拦正常建单。
"""
import json
import os
import uuid

from src.config import CONFIG_DIR, log

REASON_CODES = (
    "engine_missing",
    "engine_all_offline",
    "foreign_personal_engine",
    "skill_unresolvable",
    "scene_node_dangling",
    "scene_node_duplicate",
)

_PREHECK_CONFIG_PATH = os.path.join(CONFIG_DIR, "scene-precheck.json")
_TERMINAL_NEXT = {"done", "complete", "end"}
_HEALTH_THRESHOLD = 30


def _default_config() -> dict:
    return {"enforce": True, "reasons": {code: True for code in REASON_CODES},
            "fail_open": False}


def load_precheck_config() -> dict:
    """读取预校验开关；文件缺失/解析失败/字段异常一律回落默认全开。"""
    cfg = _default_config()
    try:
        with open(_PREHECK_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config root is not an object")
        if "enforce" in raw:
            cfg["enforce"] = bool(raw.get("enforce"))
        reasons = raw.get("reasons") or {}
        if not isinstance(reasons, dict):
            reasons = {}
        for code in REASON_CODES:
            # 支持嵌套 reasons 与顶层 reason_code 两种写法
            if code in reasons:
                cfg["reasons"][code] = bool(reasons.get(code))
            elif code in raw:
                cfg["reasons"][code] = bool(raw.get(code))
    except FileNotFoundError:
        # 配置文件缺失 → fail-open（不因配置缺失误拦建单）
        log.warning("scene_precheck: config missing, fail-open")
        cfg["fail_open"] = True
    except Exception as e:
        log.warning(f"scene_precheck: config load failed, fail-open: {e}")
        cfg["fail_open"] = True
    return cfg


def _normalize_engine_ids(raw) -> list[int]:
    out = []
    for item in raw or []:
        try:
            rid = int(item)
        except (TypeError, ValueError):
            continue
        if rid and rid not in out:
            out.append(rid)
    return out


def _node_key(node: dict) -> str:
    return str(node.get("id") or node.get("tag") or "").strip()


def _user_identities(user) -> tuple[set[str], str]:
    """返回可用于 user 档归属比对的用户名集合与 user_id。"""
    user = user or {}
    names: set[str] = set()
    for key in ("username", "external_login", "login", "name"):
        val = str(user.get(key) or "").strip()
        if val:
            names.add(val)
    uid = str(user.get("id") or "").strip()
    if uid:
        try:
            from src.auth.grape_account_db import get_grape_account_by_id
            acc = get_grape_account_by_id(int(uid))
            if acc and acc.get("username"):
                names.add(str(acc["username"]).strip())
        except Exception:
            pass
    names.discard("")
    return names, uid


def _lookup_engine_rows(engine_ids: list[int]) -> dict[int, dict]:
    """单次批量查询引擎存在性与归属（1 次 DB 查询/节点）。"""
    if not engine_ids:
        return {}
    from src.session_manager import db, db_lock
    placeholders = ",".join("?" * len(engine_ids))
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT r.id, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
            "COALESCE(rc.scope, r.scope, ''), COALESCE(rc.owner_id, r.owner_id, '') "
            "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
            f"WHERE r.id IN ({placeholders})",
            tuple(engine_ids))
        rows = c.fetchall()
    out = {}
    for r in rows:
        out[int(r[0])] = {
            "runtime_id": int(r[0]),
            "status": r[1] or "",
            "healthy": bool(r[2]),
            "installed": bool(r[3]),
            "credential_id": r[4] or 0,
            "last_seen_at": r[5] or "",
            "scope": (r[6] or "").strip().lower(),
            "owner_id": str(r[7] or "").strip(),
        }
    return out


def _read_health_scores(runtime_ids: list[int]) -> dict[int, int]:
    """只读查询健康分（不触发 health_scorer 的现算落库，保持预校验零副作用）。"""
    if not runtime_ids:
        return {}
    from src.session_manager import db, db_lock
    placeholders = ",".join("?" * len(runtime_ids))
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT runtime_id, health_score FROM runtime_health_scores "
            f"WHERE runtime_id IN ({placeholders})",
            tuple(runtime_ids))
        return {int(r[0]): int(r[1] or 0) for r in c.fetchall()}


def _available_engines(rows: list[dict]) -> list[dict]:
    """按派发链路同一把尺子判定可用：status/心跳 + 健康分阈值（只读）。"""
    if not rows:
        return []
    from src.runtime.engine_select import _health_ok
    healthy = [r for r in rows if _health_ok(r)]
    if not healthy:
        return []
    try:
        try:
            from src.runtime.health_scorer import _HEALTH_THRESHOLD as _score_threshold
        except Exception:
            _score_threshold = _HEALTH_THRESHOLD
        scores = _read_health_scores([r["runtime_id"] for r in healthy])
        return [r for r in healthy
                if scores.get(r["runtime_id"], 100) >= _score_threshold]
    except Exception as e:
        # 健康分查询异常 → 只按在线/心跳判定（fail-open，宁可放行不误拦）
        log.warning(f"scene_precheck: health score lookup failed, degrade to online check: {e}")
        return healthy


def _skill_resolvable(node: dict, repo_key: str, user_id: str) -> bool:
    skill_name = str(node.get("skill") or "").strip()
    skill_id = 0
    try:
        skill_id = int(node.get("skill_id") or 0)
    except (TypeError, ValueError):
        skill_id = 0
    if skill_id:
        try:
            from src.user.db import get_skill_by_id
            if get_skill_by_id(skill_id):
                return True
        except Exception as e:
            log.warning(f"scene_precheck: get_skill_by_id failed (fail-open): {e}")
            return True
    if not skill_name:
        return False
    try:
        from src.user.db import resolve_skill_id
        return bool(resolve_skill_id(skill_name, repo_key=repo_key, user_id=user_id or ""))
    except Exception as e:
        log.warning(f"scene_precheck: resolve_skill_id failed (fail-open): {e}")
        return True


def _ownership_problem(rows: list[dict], repo_key: str, usernames: set[str]) -> dict | None:
    """返回首个归属非法引擎信息；合法返回 None。"""
    from src.repo_key import from_repo_key
    current_repo = (from_repo_key(repo_key or "") or "").strip()
    names_lower = {n.lower() for n in usernames}
    for row in rows:
        scope = row.get("scope") or ""
        owner = row.get("owner_id") or ""
        if scope == "user":
            if owner and owner.lower() not in names_lower:
                return {"engine_id": row["runtime_id"], "reason": "user", "owner": owner}
        elif scope == "repo":
            owner_norm = (from_repo_key(owner) or "").strip()
            if owner_norm and current_repo and owner_norm != current_repo:
                return {"engine_id": row["runtime_id"], "reason": "repo", "owner": owner_norm}
    return None


def _append_problem(problems: list, cfg: dict, reason_code: str, node_tag: str,
                    engine_ids=None, message: str = "", suggestion: str = "") -> None:
    if not cfg["reasons"].get(reason_code, True):
        return
    problems.append({
        "node_tag": node_tag or "",
        "engine_ids": list(engine_ids or []),
        "reason_code": reason_code,
        "message": message,
        "suggestion": suggestion,
    })


def _check_structure(nodes: list, cfg: dict, problems: list) -> None:
    targets = set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        for key in ("id", "tag"):
            val = str(n.get(key) or "").strip()
            if val:
                targets.add(val)

    seen: set[str] = set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        key = _node_key(n)
        if not key:
            continue
        if key in seen:
            _append_problem(
                problems, cfg, "scene_node_duplicate", key,
                message=f"场景节点 id 重复：{key}",
                suggestion="删除或重命名重复节点后再建单")
        seen.add(key)

    for n in nodes:
        if not isinstance(n, dict):
            continue
        key = _node_key(n)
        nxt = str(n.get("next") or "").strip()
        if not nxt or nxt.lower() in _TERMINAL_NEXT:
            continue
        if nxt not in targets:
            _append_problem(
                problems, cfg, "scene_node_dangling", key,
                message=f"节点 {key} 的 next 指向不存在节点：{nxt}",
                suggestion="修正节点流向或删除悬空引用后再建单")


def _check_nodes(nodes: list, scene_id: str, repo_key: str, user, cfg: dict,
                 problems: list) -> None:
    _names, user_id = _user_identities(user)
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("enabled") is False:
            continue
        tag = _node_key(n)
        is_sre = str(n.get("id") or n.get("tag") or "").strip().lower() == "sre"
        eids = _normalize_engine_ids(n.get("engine_runtime_ids"))
        if not eids:
            if is_sre:
                _append_problem(
                    problems, cfg, "engine_missing", tag, [],
                    message=f"SRE 节点 {tag} 未绑定机器（SRE 不支持广播）",
                    suggestion="为 SRE 节点选择个人或仓库级机器后重试")
            continue

        rows_map = _lookup_engine_rows(eids)
        existing = [rows_map[eid] for eid in eids if eid in rows_map]
        missing = [eid for eid in eids if eid not in rows_map]
        if not existing:
            _append_problem(
                problems, cfg, "engine_missing", tag, eids,
                message=f"节点 {tag} 绑定的引擎不存在（已删除/注销）：{eids}",
                suggestion="在场景配置中重新选择可用引擎后重试")
        elif missing:
            log.warning(f"scene_precheck: scene={scene_id} node={tag} bound engines missing: {missing}")

        if existing:
            ownership = _ownership_problem(existing, repo_key, _names)
            if ownership:
                _append_problem(
                    problems, cfg, "foreign_personal_engine", tag,
                    [ownership["engine_id"]],
                    message=f"节点 {tag} 的引擎 {ownership['engine_id']} 归属非法"
                            f"（{ownership['reason']} 档，owner={ownership['owner']}）",
                    suggestion="仅可选择本人个人机器或当前仓库机器")

            available = _available_engines(existing)
            if not available:
                _append_problem(
                    problems, cfg, "engine_all_offline", tag, [r["runtime_id"] for r in existing],
                    message=f"节点 {tag} 绑定的引擎全部离线或健康分不足："
                            f"{[r['runtime_id'] for r in existing]}",
                    suggestion="启动引擎或重新选择在线机器后重试")

        skill_name = str(n.get("skill") or "").strip()
        try:
            skill_id = int(n.get("skill_id") or 0)
        except (TypeError, ValueError):
            skill_id = 0
        if (skill_name or skill_id) and not _skill_resolvable(n, repo_key, user_id):
            _append_problem(
                problems, cfg, "skill_unresolvable", tag, [],
                message=f"节点 {tag} 的技能不可解析：{skill_name or skill_id}",
                suggestion="重新选择技能或确认技能对当前用户/仓库可见")


def precheck_scene(scene_id: str, repo_key: str, user=None) -> dict:
    """建单前场景可用性预校验。

    返回 {"ok", "problems", "warnings", "trace_id"}；problems 非空即 ok=False。
    配置 enforce=false、场景不存在、配置/查询异常一律放行（fail-open）。
    """
    trace_id = "pc_" + uuid.uuid4().hex[:16]
    out = {"ok": True, "problems": [], "warnings": [], "trace_id": trace_id}
    cfg = load_precheck_config()
    if cfg.get("fail_open"):
        out["warnings"].append("scene_precheck config failed, fail-open")
        return out
    if not cfg.get("enforce", True):
        out["warnings"].append("scene_precheck disabled by config")
        return out
    if not scene_id:
        out["warnings"].append("no scene selected, skip precheck")
        return out
    try:
        from src.api.pipeline_config import get_pipeline_config
        scene = get_pipeline_config(scene_id, repo_key=repo_key)
        if not scene or scene.get("fallback") or (
                scene.get("scene_id") and scene.get("scene_id") != scene_id):
            out["warnings"].append(f"scene not found: {scene_id}")
            return out
        nodes = scene.get("nodes") or []
        if not isinstance(nodes, list):
            out["warnings"].append("scene nodes is not a list, skip precheck")
            return out

        problems = out["problems"]
        _check_structure(nodes, cfg, problems)
        _check_nodes(nodes, scene_id, repo_key, user, cfg, problems)
        out["ok"] = not problems
    except Exception as e:
        # fail-open：预校验自身的异常不得误拦正常建单
        log.warning(f"scene_precheck: precheck failed, fail-open (scene={scene_id}): {e}")
        out["warnings"].append(f"precheck failed, fail-open: {e}")
        out["ok"] = True
        out["problems"] = []
    return out