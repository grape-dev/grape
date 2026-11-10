"""Issue #1245: 建单前场景可用性预校验（静态只读）。

目标：在创建 Issue 之前，对所选场景中「存在且 enabled」的节点做四检：
1) 绑定引擎在 agent_runtimes 存在；
2) 至少一台可用（沿用派发链路的在线/心跳判定 + 健康分阈值）；
3) 引擎归属合法（user 档属创建者本人，repo 档属当前仓库）；
4) 节点技能与 repo 级场景 scope 兼容（Issue #1758 统一委托
   `scene_skill_scope.evaluate`；user scope 一律不可解析）。

另有结构校验（next 悬空 / 节点 id 重复）与三类豁免（非 SRE 空绑定、
enabled=false、skill 为空）。配置缺失/解析失败回落默认全开（不因缺配置跳过
校验）；引擎查询/探测异常仍 fail-open，技能 scope 查询异常按单一判官
fail-closed，避免与保存/派发结论漂移。
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
    """读取预校验开关。

    文件缺失/解析失败/字段异常一律回落默认全开（enforce=true + 全部 reason 开启），
    即「缺配置仍校验」，不因配置文件缺失静默跳过预校验。
    """
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
            if code in reasons:
                cfg["reasons"][code] = bool(reasons.get(code))
    except FileNotFoundError:
        log.warning("scene_precheck: config missing, use default (all reasons enabled)")
    except Exception as e:
        log.warning(f"scene_precheck: config load failed, use default (all reasons enabled): {e}")
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


def _node_skill_id(node: dict) -> int:
    try:
        return int(node.get("skill_id") or 0)
    except (TypeError, ValueError):
        return 0


def _user_identities(user) -> tuple[set[str], set[str]]:
    """返回可用于 user 档归属比对的用户名集合与数字 uid 集合。

    生产数据形态：user 凭据的 runtime_credentials.owner_id 恒为 ''（用户名写在
    owner_name），用户引擎 agent_runtimes.owner_id 存 grape_accounts.id 数字串。
    因此这里同时产出「用户名」与「数字 uid」两套标识，双向按需解析。
    """
    user = user or {}
    names: set[str] = set()
    uids: set[str] = set()
    for key in ("username", "external_login", "login", "name"):
        val = str(user.get(key) or "").strip()
        if val:
            names.add(val)
    uid = str(user.get("id") or "").strip()
    if uid.isdigit() and int(uid) > 0:
        uids.add(uid)
    try:
        from src.auth.grape_account_db import (
            get_grape_account_by_id,
            get_grape_account_by_username,
        )
        for _uid in list(uids):
            try:
                acc = get_grape_account_by_id(int(_uid))
                if acc and acc.get("username"):
                    names.add(str(acc["username"]).strip())
            except Exception as e:
                log.warning(f"scene_precheck: resolve uid->username failed ({_uid}): {e}")
        for _name in list(names):
            try:
                acc = get_grape_account_by_username(_name)
                if acc and acc.get("id"):
                    uids.add(str(acc["id"]).strip())
            except Exception as e:
                log.warning(f"scene_precheck: resolve username->uid failed ({_name}): {e}")
    except Exception as e:
        log.warning(f"scene_precheck: identity resolution unavailable: {e}")
    names.discard("")
    uids.discard("")
    return names, uids


def _lookup_engine_rows(engine_ids: list[int]) -> dict[int, dict]:
    """单次批量查询引擎存在性、归属与在线信息（1 次 DB 查询/节点）。"""
    if not engine_ids:
        return {}
    from src.session_manager import db, db_lock
    placeholders = ",".join("?" * len(engine_ids))
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT r.id, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
            "COALESCE(rc.scope, r.scope, ''), "
            "COALESCE(NULLIF(rc.owner_id, ''), r.owner_id, ''), "
            "COALESCE(rc.owner_name, '') "
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
            "owner_name": str(r[8] or "").strip(),
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
    """节点技能对 repo 级场景可解析（Issue #1758：统一委托单一 scope 判官）。

    不再 fail-open：技能 scope 查询异常按判官默认 fail-closed 处理，
    避免前置检查与保存/派发结论漂移。
    """
    from src.api.scene_skill_scope import evaluate
    return bool(evaluate(node, repo_key, "repo",
                         {"user_id": user_id})["ok"])


def _ownership_problem(rows: list[dict], repo_key: str,
                       names: set[str], uids: set[str]) -> dict | None:
    """返回首个归属非法引擎信息；合法/无法判定返回 None。"""
    from src.repo_key import from_repo_key
    current_repo = (from_repo_key(repo_key or "") or "").strip()
    names_lower = {n.lower() for n in names if n}
    uid_set = {u for u in uids if u}
    for row in rows:
        scope = row.get("scope") or ""
        owner = row.get("owner_id") or ""
        owner_name = row.get("owner_name") or ""
        if scope == "user":
            candidates = [c for c in (owner, owner_name) if c]
            if not candidates:
                # 归属字段全空 → 无法判定，fail-open
                continue
            if any(c.lower() in names_lower or c in uid_set for c in candidates):
                continue
            return {"engine_id": row["runtime_id"], "reason": "user",
                    "owner": owner or owner_name}
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


def summarize_problems(problems: list, limit: int = 3) -> str:
    """把 problems 压成一行摘要（API / webhook 共用，避免重复实现）。"""
    return "；".join(
        str(p.get("message") or p.get("reason_code") or "")
        for p in (problems or [])[:limit])


def problem_reason_codes(problems: list) -> list[str]:
    """按出现顺序去重 reason_code（API / webhook 共用）。"""
    codes = []
    for p in problems or []:
        code = p.get("reason_code") or ""
        if code and code not in codes:
            codes.append(code)
    return codes


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


def _check_engine_binding(tag: str, is_sre: bool, eids: list[int], scene_id: str,
                          repo_key: str, names: set[str], uids: set[str],
                          cfg: dict, problems: list) -> None:
    if not eids:
        if is_sre:
            _append_problem(
                problems, cfg, "engine_missing", tag, [],
                message=f"SRE 节点 {tag} 未绑定机器（SRE 不支持广播）",
                suggestion="为 SRE 节点选择个人或仓库级机器后重试")
        return

    rows_map = _lookup_engine_rows(eids)
    existing = [rows_map[eid] for eid in eids if eid in rows_map]
    missing = [eid for eid in eids if eid not in rows_map]
    if missing:
        # 逐台校验：部分缺失也不能静默放行
        _append_problem(
            problems, cfg, "engine_missing", tag, missing,
            message=f"节点 {tag} 绑定的引擎不存在（已删除/注销）：{missing}",
            suggestion="在场景配置中重新选择可用引擎后重试")
        log.warning(f"scene_precheck: scene={scene_id} node={tag} bound engines missing: {missing}")

    if not existing:
        return

    ownership = _ownership_problem(existing, repo_key, names, uids)
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


def _check_skill(node: dict, tag: str, repo_key: str, user_id: str,
                 cfg: dict, problems: list) -> None:
    from src.api.scene_skill_scope import evaluate
    skill_name = str(node.get("skill") or "").strip()
    skill_id = _node_skill_id(node)
    if not (skill_name or skill_id):
        return
    result = evaluate(node, repo_key, "repo", {"user_id": user_id})
    if result["ok"]:
        return
    _suggestion = "重新选择技能或确认技能对当前用户/仓库可见"
    _cand = list(result.get("rebind_candidates") or [])
    if _cand:
        _names = ", ".join(f"{c.get('skill_name')}({c.get('scope')})" for c in _cand)
        _suggestion = f"repo 级场景不可引用 user 级技能，请改绑为：{_names}"
    _append_problem(
        problems, cfg, "skill_unresolvable", tag, [],
        message=f"节点 {tag} 的技能不可解析：{skill_name or skill_id}"
                f"（{result.get('reason') or 'skill scope 不匹配'}）",
        suggestion=_suggestion)


def _check_nodes(nodes: list, scene_id: str, repo_key: str, user,
                 cfg: dict, problems: list) -> None:
    names, uids = _user_identities(user)
    user_id = next(iter(sorted(uids)), "")
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("enabled") is False:
            continue
        tag = _node_key(n)
        is_sre = str(n.get("id") or n.get("tag") or "").strip().lower() == "sre"
        eids = _normalize_engine_ids(n.get("engine_runtime_ids"))
        _check_engine_binding(tag, is_sre, eids, scene_id, repo_key, names, uids,
                              cfg, problems)
        _check_skill(n, tag, repo_key, user_id, cfg, problems)


def _scene_unresolved(scene: dict | None, scene_id: str) -> bool:
    return (not scene or scene.get("fallback")
            or bool(scene.get("scene_id") and scene.get("scene_id") != scene_id))


def _load_user_scene_nodes(scene_id: str, uids: set[str]):
    """标准解析未命中时补查用户个人场景（Issue #979 / #1245）。"""
    from src.api.pipeline_config import _query_user_market_scene_nodes
    for uid in sorted(uids):
        try:
            numeric_uid = int(uid)
        except (TypeError, ValueError):
            continue
        try:
            nodes = _query_user_market_scene_nodes(scene_id, numeric_uid)
        except Exception as e:
            log.warning(f"scene_precheck: user scene lookup failed (scene={scene_id}, uid={uid}): {e}")
            continue
        if nodes is not None:
            return nodes
    return None


def precheck_scene(scene_id: str, repo_key: str, user=None) -> dict:
    """建单前场景可用性预校验。

    返回 {"ok", "problems", "warnings", "trace_id"}；problems 非空即 ok=False。
    配置 enforce=false、场景不存在、查询异常一律放行（fail-open）。
    标准解析未命中时会补查 user 个人场景（scene_market scope='user'）。
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
        if _scene_unresolved(scene, scene_id):
            _names, uids = _user_identities(user)
            personal_nodes = _load_user_scene_nodes(scene_id, uids) if uids else None
            if personal_nodes is not None:
                scene = {"scene_id": scene_id, "nodes": personal_nodes}
        if _scene_unresolved(scene, scene_id):
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