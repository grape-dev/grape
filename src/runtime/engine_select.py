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
_RUNTIME_STALE_SECONDS = int(os.environ.get("RUNTIME_STALE_SECONDS", "100"))

# 默认优先级档位（小=高优先）
_PRIO_REPO = 10
_PRIO_GLOBAL = 50
_PRIO_LOCAL = 90

# ===== Issue #658 修订（2026-11-03 用户裁定）：引擎角色不按类型默认映射 =====
# 撤销 opencode/codearts/hermes 的类型刻板映射——引擎能力与引擎类型无关，
# 所有引擎默认不限角色；仅显式声明 role_tags 时才按声明硬过滤。
# （类型默认映射曾把 hermes 锁在 vod/chat/sre、opencode 锁在 coding/issue/qa，
# 误伤跨类型绑定的合法场景；保留显式声明轨，默认口径 = 不限。）
_DEFAULT_ENGINE_ROLE_TAGS: dict = {}

# 已知节点角色全集（认领侧守卫仅对角色类 pool_tag 生效；自定义路由 tag 如 "b520tag"
# 不是角色，不受角色硬过滤约束）。Issue #955：节点 id 即角色名（agent_type），
# 场景写侧/读侧统一引用该公开常量做枚举校验与 id 优先匹配（避免各模块重复定义漂移）。
KNOWN_AGENT_ROLES = {"coding", "issue", "qa", "vod", "chat", "sre"}

# 兼容别名（既有内部引用保留；外部模块请使用 KNOWN_AGENT_ROLES）
_KNOWN_AGENT_ROLES = KNOWN_AGENT_ROLES


def node_agent_role(node) -> str:
    """Issue #955：解析节点角色名（agent_type）——id 优先、tag 兜底。

    节点 id 为角色名（标准写侧口径），tag 为展示用阶段名（可为中文等任意值，
    仅存量 tag-only 节点兜底）；非 dict 节点返回空串。
    """
    if not isinstance(node, dict):
        return ""
    return (node.get("id") or node.get("tag") or "").strip()


def node_matches_agent_role(node, role) -> bool:
    """Issue #955：节点是否匹配指定角色——id 优先、tag 兜底（存量兼容）。"""
    if not isinstance(node, dict) or not role:
        return False
    return node.get("id") == role or node.get("tag") == role


def is_agent_role(tag: str) -> bool:
    """pool_tag 是否为已知节点角色（Issue #658 认领侧守卫的适用判定）。"""
    return (tag or "").strip().lower() in KNOWN_AGENT_ROLES


def engine_role_tags(engine_name: str, role_tags_json: str = "") -> list | None:
    """解析引擎有效角色集（双轨：显式 role_tags 声明 > 引擎类型默认映射兜底）。

    返回：
    - 非空 list：引擎可服务的 agent_type 集合（硬过滤口径）
    - None：不限角色（引擎类型不在默认映射且未显式声明——存量引擎零配置兼容，
      不因缺声明被全量排除）
    """
    raw = (role_tags_json or "").strip()
    if raw:
        try:
            tags = json.loads(raw)
            if isinstance(tags, list):
                tags = [str(t).strip().lower() for t in tags if str(t).strip()]
                if tags:
                    return tags
        except Exception:
            pass
    name = (engine_name or "").strip().lower()
    return _DEFAULT_ENGINE_ROLE_TAGS.get(name)


def _member_role_tags(m: dict) -> list | None:
    """从候选引擎成员 dict 取有效角色集（成员未带 role_tags 字段时按 name 解析）。"""
    return engine_role_tags(m.get("name") or "", m.get("role_tags") or "")


def _filter_by_role(members: list[dict], agent_type: str) -> list[dict]:
    """Issue #658 角色硬过滤（派发候选筛选最前置）：agent_type ∈ engine 有效角色集。

    有效角色集为 None 的引擎（未知类型且未显式声明）不参与过滤（兼容口径）。
    """
    at = (agent_type or "").strip().lower()
    if not at or not members:
        return members
    out = []
    for m in members:
        roles = _member_role_tags(m)
        if roles is None or at in roles:
            out.append(m)
    return out


def _role_no_candidates_detail(members: list[dict], agent_type: str) -> str:
    """角色硬过滤后无候选的显式错误信息（含 agent_type 与各候选引擎 role_tags）。"""
    at = (agent_type or "").strip().lower()
    parts = []
    for m in members:
        roles = _member_role_tags(m)
        parts.append(f"{m.get('name') or '?'}(id={m.get('runtime_id')}):{','.join(roles) if roles else 'any'}")
    return (f"no engine matches agent_type '{at}' by role_tags "
            f"(available engines: {'; '.join(parts) or 'none'})")


def _log_role_no_candidates(stage: str, repo_key: str, agent_type: str, scene_id: str,
                            members: list[dict]) -> None:
    """角色无候选统一留痕：结构化决策日志 + 明确错误日志（不静默降级）。"""
    detail = _role_no_candidates_detail(members, agent_type)
    _decision_log(stage, repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                  result="role_no_candidates", detail=detail)
    log.error(f"engine_select[{stage}]: {detail}")

_DEFAULT_CFG = {
    "enabled": True,
    "scopes": ["repo", "global", "local"],
    "rule": "order",
    "filters": {"only_online": True, "only_healthy": True, "only_installed": True},
    "local_priority": _PRIO_LOCAL,
    # Issue #653：场景节点 engine_runtime_ids 空值语义严格模式（默认开启）。
    # True  = 「已配置但解析为空/全部不可达/查询异常」显式报错（None，不入队），
    #         绝不静默回退全局池；未配置节点仍全量广播（行为不变）。
    # False = 兼容旧语义：解析落空回退全量广播，但必须打告警日志（可审计）。
    "strict_scene_engines": True,
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
    """健康判定：唯一真源 src.runtime.health.engine_health_ok（Issue #592 / #259 / #1487 /
    #1496 收敛）——派发与认领共用同一把尺子，禁止在此复制判定逻辑。

    status 是 agent_runtimes 在线态唯一真源；遗留 health 列是派生展示值，
    不参与派发判定（列表侧展示统一走 derive_runtime_health）。
    心跳缺失（last_seen_at 空/解析失败）与超时同判不可用——status=online 但从未
    心跳的幽灵行不得被派发放行（对齐 derive_runtime_health → recently_lost）。

    远端引擎（credential_id>0）必须 status 在线且 last_seen_at 在 _RUNTIME_STALE_SECONDS 内，
    防止 AI DevSpace 环境被删后 runtime 行残留 status=online 但实际不可达（模式 QQ）；
    本机直连（credential_id=0）按 installed+healthy，不判心跳（本机不参与 15s 心跳）。

    认领侧（service._check_pool_engines_reachable）调用同一函数——两侧口径必须一致，
    否则会出现「派发认为健康、认领认为失联」的任务入队即失败（Issue #1487）。
    """
    from src.runtime.health import engine_health_ok
    return engine_health_ok(m.get("credential_id"), m.get("status"), m.get("healthy"),
                            m.get("installed"), m.get("last_seen_at"),
                            stale_seconds=_RUNTIME_STALE_SECONDS)


# ===== 派发决策可观测性（Issue #605）=====
# engine_select 的候选构建 / 过滤 / 选中全部是静默的：hermes 持续 0 派发（Issue #605）
# 无法从服务端日志解释 17004 是否在候选、被什么条件过滤。以下三件套补齐取证能力：
# - _decision_log：单行 JSON 结构化决策日志（event + 候选 + 过滤原因 + 选中理由）
# - _candidate_reject_reasons：与 _apply_filters/_filter_by_tags/_filter_by_health
#   同一把尺子的「原因口径」（只读，不改过滤行为）
# - explain_engine_selection：主控侧取证入口（dashboard 只读接口数据源）


def _decision_log(event: str, **fields) -> None:
    """engine_select 决策结构化日志（Issue #605）：单行 JSON，便于 grep/采集。"""
    try:
        payload = {"event": event}
        payload.update(fields)
        log.info("engine_select_decision " + json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        pass


def _candidate_reject_reasons(m: dict, cfg: dict, agent_type: str = "") -> list:
    """计算单个候选引擎会被过滤掉的原因列表（与实际过滤链同一口径，只读不改行为）。

    覆盖 _apply_filters（enabled/installed/healthy/_health_ok/本机已知引擎名）、
    _filter_by_tags（engine_capabilities 标签）、_filter_by_health（健康分）三层。
    返回空列表 = 该候选可通过全部过滤。
    """
    reasons = []
    f = cfg.get("filters") or {}
    if not m.get("enabled", True):
        reasons.append("disabled")
    if f.get("only_installed") and not m.get("installed"):
        reasons.append("not_installed")
    if f.get("only_healthy") and not m.get("healthy"):
        reasons.append("healthy=0")
    if f.get("only_online") and not _health_ok(m):
        if (m.get("status") or "") != "online":
            reasons.append(f"status={m.get('status') or 'offline'}")
        else:
            reasons.append(f"stale_heartbeat>{_RUNTIME_STALE_SECONDS}s")
    if (m.get("credential_id") or 0) == 0:
        try:
            from src.user.db import is_known_engine_name
            if not is_known_engine_name(m.get("name") or ""):
                reasons.append("unknown_local_engine")
        except Exception:
            pass
    rid = m.get("runtime_id") or 0
    if rid and agent_type:
        try:
            db, lock = _db()
            with lock:
                c = db.cursor()
                c.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rid,))
                tags = [r[0] for r in c.fetchall()]
        except Exception:
            tags = []
        if agent_type not in tags and "all" not in tags:
            reasons.append(f"tag_mismatch({','.join(tags) or 'none'})")
        # Issue #658：角色硬过滤口径（role_tags/引擎类型默认映射）
        _at = (agent_type or "").strip().lower()
        _roles = _member_role_tags(m)
        if _at and _roles is not None and _at not in _roles:
            reasons.append(f"role_mismatch({','.join(_roles)})")
    if rid:
        try:
            from src.runtime.health_scorer import is_healthy as _score_ok
            if not _score_ok(rid):
                reasons.append("health_score_below_threshold")
        except Exception:
            pass
    return reasons


def explain_engine_selection(repo_key: str, agent_type: str, scene_id: str = "",
                             user_id: int = 0, issue_creator: str = "") -> dict:
    """主控侧取证入口（Issue #605）：解释某 (repo, agent_type, scene) 的引擎候选与过滤全景。

    返回：
    - mode：plugin（广播）/ legacy
    - bound_engine_ids：场景 node 绑定的 engine_runtime_ids
    - candidates：候选全集（含 scope/priority/healthy/last_seen_at/tags），每个候选带
      reasons（会被过滤的具体原因，空 = 可通过全部过滤）
    - pool_engine_ids：广播名单（get_scene_pool_engine_ids 实际结果，None = 全灭）
    - selected：legacy 模式实际会选中的 runtime_id（None = engine_unavailable）
    """
    out = {"repo": _norm_repo(repo_key), "agent_type": agent_type, "scene_id": scene_id,
           "mode": "legacy", "bound_engine_ids": [], "candidates": [],
           "pool_engine_ids": "", "selected": None}
    try:
        from src.api.event_pipeline import is_plugin_mode
        if is_plugin_mode():
            out["mode"] = "plugin"
    except Exception:
        pass
    cfg = load_engine_selection()
    repo = _norm_repo(repo_key)
    if not user_id:
        user_id = _repo_user_id(repo_key, issue_creator=issue_creator)
    if scene_id:
        try:
            from src.api.pipeline_config import get_scene_engine_ids
            engine_ids = get_scene_engine_ids(scene_id, agent_type, repo_key=repo, user_id=user_id)
        except Exception:
            engine_ids = []
    else:
        engine_ids = []
    out["bound_engine_ids"] = list(engine_ids or [])
    if engine_ids:
        members = dedupe_engines(_engines_by_ids(list(engine_ids), repo, cfg))
        for m in members:
            out["candidates"].append({
                "runtime_id": m.get("runtime_id"), "name": m.get("name") or "",
                "instance": engine_unique_key(m), "scope": m.get("scope") or "",
                "priority": m.get("priority"), "status": m.get("status") or "",
                "healthy": bool(m.get("healthy")), "installed": bool(m.get("installed")),
                "last_seen_at": m.get("last_seen_at") or "",
                "credential_id": m.get("credential_id") or 0,
                "reasons": _candidate_reject_reasons(m, cfg, agent_type),
            })
    if scene_id:
        out["pool_engine_ids"] = get_scene_pool_engine_ids(
            scene_id, agent_type, repo_key=repo_key, user_id=user_id, issue_creator=issue_creator)
    if out["mode"] == "legacy":
        out["selected"] = pick_engine(repo_key, agent_type, fallback_runtime_id=0,
                                      scene_id=scene_id, user_id=user_id, issue_creator=issue_creator)
    return out


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
    # 单选约束：同一引擎身份（唯一键）只保留一个候选，防止同名/同实例重复计入
    # （Issue #400：接入个人机器后 user/repo 双档或多行重复导致“多个引擎同时选中”）
    if len(out) > 1:
        deduped = dedupe_engines(out)
        if len(deduped) != len(out):
            log.info(f"_apply_filters: dedupe engines by unique key, merged {len(out) - len(deduped)} duplicate(s)")
        return deduped
    return out


def engine_unique_key(m: dict) -> str:
    """引擎候选的唯一键（Issue #400 单选约束 / 候选去重）。

    以**引擎身份**派生归一键，而非 owner 字符串/display 名（对齐 <canonical_account_id> canonical
    account identity 思路）：同一实例（同一 name+device+credential）只会有一个键，
    历史重复行、user/repo 双档同实例被归一为单条候选。

    - 本机直连（credential_id=0）：`local:{engine_type}@{host_name}`（本机引擎以类型名+归属主控机为键——
      多主控机同名引擎是两台不同的机器，不是重复行；host_name 缺失时退回 `local:{engine_type}`）
    - 个人/远端：`{scope}:{instance_name}`（instance_name 与 src/runtime/instances.py 同源）
    """
    cid = m.get("credential_id") or 0
    scope = (m.get("scope") or "global").strip().lower()
    name = (m.get("name") or "").strip()
    if cid == 0:
        _hn = (m.get("host_name") or "").strip()
        if _hn:
            return f"local:{name.lower()}@{_hn}"
        return f"local:{name.lower()}"
    try:
        from src.runtime.instances import instance_name
        ins = instance_name(name, m.get("device_name") or "", cid)
    except Exception:
        ins = f"{name.lower()}-cred{cid}"
    return f"{scope}:{ins.lower()}"


def dedupe_engines(members: list[dict]) -> list[dict]:
    """按唯一键去重（保留首个出现项，即查询/排序中的权威行）。

    输入顺序即权威顺序（如 query_engines 按 id 排序、_engines_by_ids 按给定 id 列表），
    首个出现的行保留，后续同名/同实例行合并。不改变成员的字段结构。
    """
    seen: set = set()
    out: list[dict] = []
    for m in members:
        if not isinstance(m, dict):
            out.append(m)
            continue
        key = engine_unique_key(m)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


# ===== 引擎档位 / 优先级（供 pick_engine 多引擎选择）=====

def _repo_user_id(repo_key: str, issue_creator: str = "") -> int:
    """从 user_repos 表反查 repo_key 对应的 user_id。

    优先按 issue_creator（GitCode 用户名）查 unified_user_mapping 获取 user_id；
    issue_creator 为空或查不到时，改查 grape_accounts/external_identities
    （平台 login 匹配 external_login，或平台数字 ID 匹配 external_id）作为二级回退；
    仍查不到时，user_repos 回退按创建者过滤（JOIN grape_accounts），而非取第一个 join 用户。

    Issue #76 修复：原逻辑按 user_id ASC 取第一条 is_join=1 的记录，
    对于多用户仓库会返回"最早加入的用户"而非 Issue 创建者，
    导致 scope=user 引擎无法通过 claim_by_tag 认领任务。

    Issue #211 增强：二级回退 + user_repos 按创建者过滤，
    确保已注册 grape 的项目组成员能正确映射而非落为 user_id=0。
    """
    if not repo_key:
        return 0
    repo = _norm_repo(repo_key)
    db, lock = _db()
    with lock:
        try:
            c = db.cursor()
            if issue_creator:
                if not issue_creator.strip():
                    return 0
                # 1. 优先查 unified_user_mapping（Issue #76 修复，OAuth 登录用户）
                # FR-CQ-1: 提取为 um_column_for_repo，消除 if/else 分支
                from src.repo_key import parse_repo_key, um_column_for_repo
                _provider, _, _ = parse_repo_key(repo_key)
                _col = um_column_for_repo(repo_key)
                c.execute(
                    f"SELECT user_id FROM unified_user_mapping "
                    f"WHERE {_col}=? AND {_col}!='' AND user_id>0 LIMIT 1",
                    (issue_creator,))
                row = c.fetchone()
                if row:
                    return row[0]
                # 2. 二级回退：查 grape_accounts（D32-补丁）
                # FR-18.3/FR-25.2: EXISTS 用 external_identities 验证平台 login，
                # 不再用 ga.username 匹配注册名（注册名≠平台 login 时会误杀）
                c.execute(
                    "SELECT ga.id FROM grape_accounts ga "
                    "WHERE ga.id>0 AND ga.status=1 "
                    "AND EXISTS (SELECT 1 FROM external_identities ei "
                    "WHERE ei.grape_user_id=ga.id AND ei.provider=? AND ei.status=1 "
                    "AND LOWER(ei.external_login)=LOWER(?) AND ei.external_login!='') "
                    "LIMIT 1",
                    (_provider, issue_creator))
                row = c.fetchone()
                if row:
                    return row[0]
                # 按 external_id 查（issue_creator 可能是平台数字 ID）
                # FR-18.3: 按 provider 动态查（原硬编码 gitcode）
                c.execute(
                    "SELECT ei.grape_user_id FROM external_identities ei "
                    "WHERE ei.provider=? AND ei.status=1 AND ei.external_id=? LIMIT 1",
                    (_provider, issue_creator))
                row = c.fetchone()
                if row:
                    return row[0]
                # 3. user_repos 回退：按创建者过滤（JOIN grape_accounts）
                # FR-18.3/FR-25.2: EXISTS 用 external_identities 验证平台 login，
                # 不再用 ga.username 匹配注册名（注册名≠平台 login 时会误杀）
                c.execute(
                    "SELECT ur.user_id FROM user_repos ur "
                    "JOIN grape_accounts ga ON ga.id = ur.user_id "
                    "WHERE ga.status=1 "
                    "AND EXISTS (SELECT 1 FROM external_identities ei "
                    "WHERE ei.grape_user_id=ga.id AND ei.provider=? AND ei.status=1 "
                    "AND LOWER(ei.external_login)=LOWER(?) AND ei.external_login!='') "
                    "AND (ur.repo_key=? OR ur.repo_key=?) AND ur.is_join=1 "
                    "LIMIT 1",
                    (_provider, issue_creator, repo, f"{_provider}:{repo}"))
                row = c.fetchone()
                return row[0] if row else 0
            # issue_creator 为空：保留原逻辑（取第一个 join 用户）
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
                " COALESCE(rc.device_name, ''), COALESCE(rc.owner_name, ''), COALESCE(r.role_tags, '') "
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
            "role_tags": r[11] or "",
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


# ===== 健康度过滤增强层（Issue #134）=====
# 健康度过滤作为派发链路的增强层插入，不改变广播协议/分发语义/A2A 事件流转。
# 仅在 pick_engine 的候选机器筛选阶段生效：过滤低健康分引擎，全部不健康时降级兜底。


def _filter_by_health(members: list[dict]) -> list[dict]:
    """健康度过滤增强层：从候选引擎中过滤出健康的。

    健康分 >= HEALTH_SCORE_THRESHOLD（默认 30）且连续失败 < MAX_CONSECUTIVE_FAILURES（默认 5）。
    全部不健康时返回空列表（调用方降级兜底）。
    """
    try:
        from src.runtime.health_scorer import filter_healthy
        rids = [m["runtime_id"] for m in members if m.get("runtime_id")]
        healthy_rids = set(filter_healthy(rids))
        return [m for m in members if m.get("runtime_id") in healthy_rids]
    except Exception as e:
        log.warning(f"_filter_by_health failed, skipping health filter: {e}")
        return members  # 模块异常时不过滤（不阻塞派发）


def _best_health_fallback(members: list[dict]) -> int | None:
    """全部不健康时的降级兜底：取健康分最高的引擎。"""
    try:
        from src.runtime.health_scorer import best_fallback
        rids = [m["runtime_id"] for m in members if m.get("runtime_id")]
        return best_fallback(rids)
    except Exception as e:
        log.warning(f"_best_health_fallback failed: {e}")
        # 兜底的兜底：取第一个候选
        return members[0]["runtime_id"] if members else None


def pick_engine(repo_key: str, agent_type: str, fallback_runtime_id: int = 0,
                scene_id: str = "", user_id: int = 0,
                issue_creator: str = "") -> int | None:
    """从场景 node 的 engine_runtime_ids 选引擎；为空回退会话主引擎（legacy）。

    返回 None = engine_unavailable（调用方跳过执行）。
    返回 0 = 无主池广播（plugin 模式），场景 engine_runtime_ids 限定广播范围（通过 pool_engine_ids 字段）。

    plugin 模式：统一返回 0（广播），不读任何绑定。
    legacy 模式：engine_runtime_ids 非空 → 按优先级选健康引擎；为空 → 回退 fallback_runtime_id。
    """
    cfg = load_engine_selection()
    if not cfg.get("enabled", True):
        _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                      result="engine_selection_disabled")
        return None
    # Plugin 模式：统一广播，engine_runtime_ids 由 get_scene_pool_engine_ids 写入 pool_engine_ids
    # （SRE 的空列表/非个人级约束在 get_scene_pool_engine_ids 内生效——那里是广播范围的唯一收口）
    try:
        from src.api.event_pipeline import is_plugin_mode
        if is_plugin_mode():
            _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                          result="plugin_broadcast")
            return 0
    except Exception:
        pass
    # Legacy 模式：从场景 node 读 engine_runtime_ids
    repo = _norm_repo(repo_key)
    if not user_id:
        user_id = _repo_user_id(repo_key, issue_creator=issue_creator)
    if scene_id:
        try:
            from src.api.pipeline_config import get_scene_engine_ids
            engine_ids = get_scene_engine_ids(scene_id, agent_type, repo_key=repo, user_id=user_id)
        except Exception:
            engine_ids = []
    else:
        engine_ids = []
    # Issue #605：候选/过滤决策留痕——bound 集合 + 每候选被过滤原因（只读口径，不改行为）
    _bound_ids = list(engine_ids or [])
    _cand_trace = []
    if _bound_ids:
        for m in dedupe_engines(_engines_by_ids(_bound_ids, repo, cfg)):
            _cand_trace.append({
                "rid": m.get("runtime_id"), "name": m.get("name") or "",
                "scope": m.get("scope") or "", "status": m.get("status") or "",
                "reasons": _candidate_reject_reasons(m, cfg, agent_type),
            })
    # SRE 派发层约束（sre-repo-worker-relax §1.1）：允许 user+repo 两档（user 优先，
    # 个人机器环境有登录态优先复用，repo 档作协作兜底）；空 = 无引擎不回退不广播
    if _sre_no_broadcast(agent_type):
        if not engine_ids:
            _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                          bound_engine_ids=_bound_ids, candidates=_cand_trace,
                          result="sre_no_bound_engines")
            return None
        members = dedupe_engines(_engines_by_ids(engine_ids, repo, cfg))
        # Issue #658：角色硬过滤（候选筛选最前置，先于 tag/scope/priority/health）
        members = _filter_by_role(members, agent_type)
        if not members:
            _log_role_no_candidates("pick_engine", repo_key, agent_type, scene_id,
                                    dedupe_engines(_engines_by_ids(_bound_ids, repo, cfg)))
            return None
        members = _filter_by_tags(members, agent_type)
        members = [m for m in members if m.get("scope") in ("user", "repo")]
        for _scope in ("user", "repo"):
            cand = _apply_filters([m for m in members if m.get("scope") == _scope], cfg)
            if not cand:
                continue
            cand = _filter_by_health(cand)
            if not cand:
                continue
            if cfg.get("rule") == "order":
                _sel = min(cand, key=lambda r: (r["priority"], r["runtime_id"]))["runtime_id"]
                _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                              bound_engine_ids=_bound_ids, candidates=_cand_trace,
                              result="selected", selected=_sel,
                              reason=f"sre scope={_scope} rule=order")
                return _sel
            _sel = random.choice(cand)["runtime_id"]
            _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                          bound_engine_ids=_bound_ids, candidates=_cand_trace,
                          result="selected", selected=_sel,
                          reason=f"sre scope={_scope} rule=random")
            return _sel
        _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                      bound_engine_ids=_bound_ids, candidates=_cand_trace,
                      result="sre_no_healthy_engine")
        return None
    if engine_ids:
        # 多引擎选择：查引擎详情，按优先级选健康引擎
        members = dedupe_engines(_engines_by_ids(engine_ids, repo, cfg))
        # Issue #658：角色硬过滤（候选筛选最前置，先于 tag/scope/priority/health；
        # 无候选报明确错误——含 agent_type 与各候选引擎 role_tags，不静默降级）
        members = _filter_by_role(members, agent_type)
        if not members:
            _log_role_no_candidates("pick_engine", repo_key, agent_type, scene_id,
                                    dedupe_engines(_engines_by_ids(_bound_ids, repo, cfg)))
            return None
        members = _filter_by_tags(members, agent_type)
        if members:
            for scope in cfg.get("scopes") or ["repo", "global", "local"]:
                cand = _apply_filters([m for m in members if _member_scope(m, repo) == scope], cfg)
                if not cand:
                    continue
                # Issue #134: 健康度过滤增强层——在候选机器筛选阶段过滤低健康分引擎
                # 不改变广播协议/分发语义：仅在 pick_engine 选机阶段生效
                cand = _filter_by_health(cand)
                if not cand:
                    continue  # 该档位全不健康，尝试下一档位
                if cfg.get("rule") == "order":
                    _sel = min(cand, key=lambda r: (r["priority"], r["runtime_id"]))["runtime_id"]
                    _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                                  bound_engine_ids=_bound_ids, candidates=_cand_trace,
                                  result="selected", selected=_sel,
                                  reason=f"scope={scope} rule=order")
                    return _sel
                _sel = random.choice(cand)["runtime_id"]
                _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                              bound_engine_ids=_bound_ids, candidates=_cand_trace,
                              result="selected", selected=_sel,
                              reason=f"scope={scope} rule=random")
                return _sel
            # 所有档位均无健康引擎 → 降级兜底：取所有候选中健康分最高的
            all_cand = _apply_filters(members, cfg)
            if all_cand:
                _best = _best_health_fallback(all_cand)
                if _best:
                    log.info(f"pick_engine: all scopes unhealthy, fallback to best={_best}")
                    _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                                  bound_engine_ids=_bound_ids, candidates=_cand_trace,
                                  result="fallback_best_health", selected=_best,
                                  reason="all_scopes_unhealthy")
                    return _best
            _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                          bound_engine_ids=_bound_ids, candidates=_cand_trace,
                          result="no_healthy_member")
            return None     # 集合存在但无健康成员 → engine_unavailable
    # engine_runtime_ids 为空 → 回退会话主引擎（legacy）
    if fallback_runtime_id:
        _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                      bound_engine_ids=_bound_ids, candidates=_cand_trace,
                      result="fallback_session_engine", selected=int(fallback_runtime_id),
                      reason="empty_scene_binding")
        return int(fallback_runtime_id)
    _decision_log("pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
                  bound_engine_ids=_bound_ids, candidates=_cand_trace,
                  result="engine_unavailable")
    return None


def _sre_no_broadcast(agent_type: str) -> bool:
    """SRE Agent 是否禁止广播（sre-repo-worker-relax-design §1.1）。

    派发层约束：SRE 任务只允许 user/repo 两档引擎（user 优先，repo 作协作兜底）；
    空列表 = 无引擎（不广播、不回退）。global 池不放行：SRE 执行部署/运维命令，
    广播到任意用户贡献的机器责任边界不清（sre-repo-worker-relax 安全边界）。
    """
    return str(agent_type or "").strip().lower() == "sre"


def get_scene_pool_engine_ids(scene_id: str, agent_type: str, repo_key: str = "", user_id: int = 0,
                              issue_creator: str = "") -> str | None:
    """获取限定广播引擎列表，返回 JSON 字符串供 agent_tasks.pool_engine_ids 使用。

    只从场景 node 的 engine_runtime_ids 读取，并按健康过滤（Issue #259）：
    离线（status!=online）或心跳超时（last_seen_at 距今 > _RUNTIME_STALE_SECONDS，默认 100s）
    的引擎不写入 pool。

    SRE 专属（sre-repo-worker-relax §1.1）：只保留 user/repo 两档引擎；
    场景没配 SRE 引擎 → None（空 = 无引擎，不广播，调用方报错）。

    Issue #653（空值语义单点收口 + 严格模式开关，默认严格）：
    通过 get_scene_engine_binding 区分节点绑定四态（no_scene/no_node/unbound/bound）：
    - bound（已配置）：候选解析为空（引擎行不存在/被删）或全部不健康 → None（显式报错，
      绝不回退全局池）；strict_scene_engines=False 时回退 ""（广播）但打告警日志。
    - unbound / no_node（未配置）：非 SRE → ""（全量广播，存量行为不变）。
    - 场景查询异常：严格模式 fail-closed → None；关闭时 ""（广播）+ 告警。

    返回值（三方语义，调用方必须用 is None 显式区分 None 与 ""）：
    - JSON 字符串：场景配了引擎且有健康的 → 限定名单（仅健康引擎）
    - ""：场景没配引擎 / node 不存在（原行为不变，SRE 除外）
    - None：场景配了引擎但全部不健康 / 解析为空 / 查询异常（严格模式）→ 全灭，
      调用方不入队并明确报错（Issue #259 方案A / #653）
    """
    import json
    if not user_id:
        user_id = _repo_user_id(repo_key, issue_creator=issue_creator)
    if scene_id:
        # Issue #541：仓内存在性兜底——scene_id 在该仓库查不到（如误绑模板场景名）
        # 时不静默回退全局模板行。SRE 直接按派发失败处理（None，调用方按
        # 「场景仓内不存在」报错）；非 SRE 维持全量广播原行为（仅告警留痕）。
        if repo_key:
            try:
                from src.api.pipeline_config import scene_exists_in_repo
                if not scene_exists_in_repo(scene_id, repo_key):
                    log.warning(f"get_scene_pool_engine_ids: scene={scene_id} not found in repo "
                                f"{repo_key}, skip global template fallback for dispatch (Issue #541)")
                    if _sre_no_broadcast(agent_type):
                        return None
            except Exception as _e541:
                log.warning(f"get_scene_pool_engine_ids: repo existence check failed: {_e541}")
        # Issue #653：严格模式开关（engine-selection.json strict_scene_engines，默认 True）
        _strict_653 = bool(load_engine_selection().get("strict_scene_engines", True))
        try:
            from src.api.pipeline_config import get_scene_engine_binding
            _binding_653 = get_scene_engine_binding(scene_id, agent_type, repo_key=repo_key, user_id=user_id)
            ids = _binding_653.get("engine_ids") or []
        except Exception as e:
            # 场景查询异常：严格模式 fail-closed（宁失败不跑飞，#253 语义）；关闭时维持广播
            log.warning(f"get_scene_pool_engine_ids: scene query failed: {e}")
            if _strict_653:
                _decision_log("pool_engine_ids", repo_key=repo_key, agent_type=agent_type,
                              scene_id=scene_id, bound_engine_ids=[], healthy_ids=[],
                              result="query_failed_strict")
                return None
            return ""
        # SRE 空 = 无引擎：不广播，直接全灭语义（Q2 裁定，区别于其他 agent 的 "" 广播）
        if _sre_no_broadcast(agent_type) and not ids:
            return None
        if ids:
            # Issue #653：bound（已配置）→ 硬约束，空候选绝不让路给全量广播
            # Issue #259：写入 pool 前按健康过滤（与 legacy pick_engine 的 _health_ok 同一把尺子）
            members = dedupe_engines(_engines_by_ids(ids, _norm_repo(repo_key), load_engine_selection()))
            if not members:
                # 配置的引擎在 agent_runtimes 已不存在（被删/revoke）→ 空候选
                if not _strict_653:
                    log.warning(f"get_scene_pool_engine_ids: scene={scene_id} agent={agent_type} "
                                f"bound engines {ids} missing in agent_runtimes, fallback to broadcast "
                                f"(strict_scene_engines=off, Issue #653)")
                    return ""
                _decision_log("pool_engine_ids", repo_key=repo_key, agent_type=agent_type,
                              scene_id=scene_id, bound_engine_ids=ids, healthy_ids=[],
                              result="bound_engines_missing")
                return None
            # SRE 派发层约束（Q3 裁定）：只允许 user/repo 档（配置后引擎 scope 被改也不放行 global）
            if _sre_no_broadcast(agent_type):
                _before = len(members)
                members = [m for m in members if m.get("scope") in ("user", "repo")]
                if len(members) < _before:
                    log.warning(f"get_scene_pool_engine_ids: SRE 过滤掉 {_before - len(members)} 个非个人/仓库级引擎")
            # Issue #658：角色硬过滤（候选筛选最前置，先于健康过滤）——
            # agent_type ∈ engine 有效角色集（显式 role_tags > 引擎类型默认映射）；
            # 无候选返回 None（调用方明确报错，不静默降级/广播给不匹配引擎）
            # SRE 档位过滤后已空时不重复报「角色不匹配」（沿用 #653 的 all_offline 语义）
            if members:
                _pre_role = list(members)
                members = _filter_by_role(members, agent_type)
                if not members:
                    _log_role_no_candidates("pool_engine_ids", repo_key, agent_type, scene_id, _pre_role)
                    return None
            # Issue #605：广播名单过滤留痕——每个被过滤引擎的 rid/name 与原因（stale 心跳/离线等）
            _dropped = []
            for m in members:
                if not _health_ok(m):
                    if (m.get("status") or "") != "online":
                        _why = f"status={m.get('status') or 'offline'}"
                    else:
                        _why = f"stale_heartbeat>{_RUNTIME_STALE_SECONDS}s"
                    _dropped.append({"rid": m.get("runtime_id"), "name": m.get("name") or "", "reason": _why})
            healthy_ids = [m["runtime_id"] for m in members if _health_ok(m)]
            if healthy_ids:
                _decision_log("pool_engine_ids", repo_key=repo_key, agent_type=agent_type,
                              scene_id=scene_id, bound_engine_ids=ids,
                              healthy_ids=healthy_ids, dropped=_dropped,
                              result="healthy_pool")
                return json.dumps(healthy_ids)
            # 配了引擎但全灭 → None（方案A：调用方报错，不放开广播）
            _decision_log("pool_engine_ids", repo_key=repo_key, agent_type=agent_type,
                          scene_id=scene_id, bound_engine_ids=ids,
                          healthy_ids=[], dropped=_dropped,
                          result="all_offline")
            return None
    return ""


def scene_dispatch_failure_reason(scene_id: str, agent_type: str, repo_key: str = "",
                                  user_id: int = 0, issue_creator: str = "") -> str:
    """Issue #541：场景派发失败原因判定（调用方在 get_scene_pool_engine_ids 返回 None 后使用）。

    区分失败（此前统一报「场景绑定的引擎全部离线」，无法定位真实原因）：
    1. 场景仓内不存在：scene_id 未绑定到该仓库（典型为误绑模板场景名，
       回退全局模板行后无引擎）→ 提示重新应用场景
    2. 场景未绑定引擎：仓内场景存在但该 Agent 节点 engine_runtime_ids 为空
    3. 引擎全部离线：节点声明了引擎但池判定全灭（在线状态/心跳不健康）
    4. Issue #653：绑定的引擎已注销（agent_runtimes 行不存在/被删）→ 空候选
    5. Issue #653：场景/引擎查询异常（严格模式 fail-closed）
    """
    if not scene_id:
        return f"场景未绑定引擎（scene=空, agent={agent_type}），请先为任务绑定场景"
    if repo_key:
        try:
            from src.api.pipeline_config import scene_exists_in_repo
            if not scene_exists_in_repo(scene_id, repo_key):
                return (f"场景 {scene_id} 仓内不存在（仓库 {repo_key}，可能绑定了模板场景名"
                        f"而非仓内副本，Issue #541），请重新应用场景后再派发（agent={agent_type}）")
        except Exception as _e:
            log.warning(f"scene_dispatch_failure_reason: repo existence check failed: {_e}")
    try:
        from src.api.pipeline_config import get_scene_engine_ids
        ids = get_scene_engine_ids(scene_id, agent_type, repo_key=repo_key, user_id=user_id) or []
    except Exception as _e:
        # Issue #653：查询异常（严格模式 fail-closed）——报查询失败而非误导性的「未绑定」
        log.warning(f"scene_dispatch_failure_reason: engine ids lookup failed: {_e}")
        return (f"场景引擎查询失败（scene={scene_id}, agent={agent_type}，{_e}），"
                f"严格模式下宁失败不派发（Issue #653），请检查数据库后重试")
    if not ids:
        return f"场景未绑定引擎（scene={scene_id}, agent={agent_type}），请在场景中为该 Agent 配置引擎"
    # Issue #653：绑定存在但引擎行全部缺失 → 与「全部离线」区分，提示引擎被注销
    try:
        members = _engines_by_ids(ids, _norm_repo(repo_key), load_engine_selection())
    except Exception:
        members = None
    if members is not None and not members:
        return (f"场景绑定的引擎已注销或不存在（scene={scene_id}, agent={agent_type}, "
                f"engines={ids}），请检查引擎注册状态后重新配置（Issue #653）")
    return f"场景绑定的引擎全部离线（scene={scene_id}, agent={agent_type}, engines={ids}），请检查引擎在线状态"


def resolve_local_engine(user_id: int = 0) -> int | None:
    """返回一个健康的本机直连引擎（credential_id=0 且 installed+healthy）；无则 None。

    用于 chat 等未显式绑定时的本机兜底（engine-selection-design.md §8.2）。
    无本机直连时回退全局健康引擎（credential_id>0 的远端 worker，chat 走队列执行），
    避免"无可用引擎"硬失败（#60：清理设备后无 cred0 引擎，chat 全挂）。

    user_id > 0（chat 场景，per-user-random-default-engine 设计）：候选限定**当前这台
    主控机**的直连引擎（host_name=本机 hostname，ELB 分流下请求落哪台就直连哪台，
    跨主机的直连行无法本机拉进程），再按 user_id 稳定取模挑选——同一用户在同一台
    机器上恒定同一引擎，不同用户自然散开，避免全员滑向同一个默认引擎（此前取
    MIN(id) 造成单引擎压力集中）。
    user_id=0（IM 等无用户上下文兜底）：维持旧行为取最小 id。

    结果缓存 30 秒——引擎列表不频繁变化，缓存可大幅减少 DB 查询。
    """
    now = _time.time()
    _ck = _resolve_local_cache.get("expires", 0)
    if _ck > now and not user_id:
        return _resolve_local_cache.get("result")
    cand = query_engines("", "local")
    cand = _apply_filters([dict(m, scope="local") for m in cand], load_engine_selection())
    if user_id:
        # 方案 A：只挑当前主控机自己的直连引擎（别的 master 的行留显示，不留执行）
        try:
            import socket as _sock
            _hn = _sock.gethostname()
            _own = [m for m in cand if (m.get("host_name") or "") == _hn]
            if _own:
                cand = _own
        except Exception:
            pass
    if not cand:
        # 回退：任意健康全局引擎（chat 未配置时可用远端 worker 引擎入队执行）
        cand = query_engines("", "global")
        cand = _apply_filters([dict(m, scope="global") for m in cand], load_engine_selection())
    if not cand:
        return None
    if user_id:
        # 稳定取模：同一用户恒定命中同一引擎（会话连续），用户间均匀散开
        _ids = sorted(r["runtime_id"] for r in cand)
        result = _ids[int(user_id) % len(_ids)]
    else:
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
    repo scope 只返回当前仓库的仓库级引擎（按 repo_key 过滤 owner_id）。"""
    repo = _norm_repo(repo_key)
    db, lock = _db()
    # 收集用户加入的仓库列表（用于 repo scope 无 repo_key 时的回退过滤）
    user_repos = set()
    if username and scope == "repo" and not repo:
        try:
            from src.session_manager import db as _udb, db_lock as _ulock
            from src.repo_key import from_repo_key
            with _ulock:
                uc = _udb.cursor()
                uc.execute(
                    "SELECT ur.repo_key FROM user_repos ur "
                    "JOIN grape_accounts ua ON ua.id = ur.user_id "
                    "WHERE LOWER(ua.username)=LOWER(?) AND ur.is_join = 1", (username,))
                for row in uc.fetchall():
                    user_repos.add(from_repo_key(row[0] or ""))
        except Exception as _e:
            log.warning(f"query_engines: get_user_repos failed: {_e}")
    with lock:
        c = db.cursor()
        try:
            if scope == "repo":
                # 按当前仓库过滤：owner_id 可能带平台前缀（gitcode:/github:）也可能不带，三种都匹配
                # （跨平台候选与 runtime.py list_runtime_tasks / deps.py check_repo_access 同款）
                if repo:
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                        " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0)"
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='repo' AND rc.status='active' "
                        "AND (rc.owner_id=? OR rc.owner_id=? OR rc.owner_id=?) ORDER BY r.id",
                        (repo, f"gitcode:{repo}", f"github:{repo}"))
                else:
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                        " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0)"
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='repo' AND rc.status='active' ORDER BY r.id")
            elif scope == "local":
                # 本机引擎（credential_id=0）对所有人可见；host_name 区分多台主控机
                # （per-user-random-default-engine 方案 A 靠它过滤"当前这台主控机"）
                c.execute(
                    "SELECT id, name, status, healthy, installed, credential_id, last_seen_at, '', '', '',"
                    " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=agent_runtimes.id AND enabled=1),"
                    " COALESCE(alias,''), COALESCE(scene_tags,''), COALESCE(org_id,0), COALESCE(host_name,'')"
                    "FROM agent_runtimes WHERE credential_id=0 ORDER BY id")
            elif scope == "user":
                if username:
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                        " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0)"
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='user' AND rc.status='active' AND rc.owner_name=? ORDER BY r.id", (username,))
                else:
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                        " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0)"
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='user' AND rc.status='active' ORDER BY r.id")
            else:
                c.execute(
                    "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                    " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                    " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                    " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0)"
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
            if repo:
                # 有 repo_key 时：只返回当前仓库的引擎
                if _repo_owner != repo:
                    continue
            elif username and user_repos and _repo_owner not in user_repos:
                # 无 repo_key 回退：只返回用户加入的仓库的引擎
                continue
        # agent_type 标签过滤（r[10] = GROUP_CONCAT(tag) from engine_capabilities, e.g. "all" or "all,coding"）
        if agent_type:
            tags_str = r[10] or ""
            tags = [t.strip() for t in tags_str.split(",") if t.strip()]
            # Issue #592：本机直连历史行（默认 'all' 标签机制引入前创建）无 capabilities 行
            # → tags 为空被过滤出弹窗列表（数量偏少）。本机直连按注册默认值 'all' 兜底
            # （与 save_agent_runtimes 新建默认标签及广播派发语义对齐；远端行仍按标签过滤）。
            if not tags and scope == "local":
                tags = ["all"]
            if agent_type not in tags and "all" not in tags:
                continue
        # scene_tags（r[12] = JSON 字符串，如 '["default","iac"]'；空则回退 ["default"]）
        try:
            _scene_tags = json.loads(r[12]) if r[12] else ["default"]
            if not isinstance(_scene_tags, list):
                _scene_tags = ["default"]
        except Exception:
            _scene_tags = ["default"]
        out.append({
            "runtime_id": r[0], "name": r[1], "status": r[2] or "offline", "healthy": bool(r[3]),
            "installed": bool(r[4]), "credential_id": r[5] or 0, "last_seen_at": r[6] or "",
            "device_name": r[7] or "", "owner_name": r[8] or "",
            "owner_id": r[9] or "",
            "scope": scope,
            "tags": [t.strip() for t in (r[10] or "").split(",") if t.strip()],
            "alias": r[11] or "",
            "scene_tags": _scene_tags,
            "org_id": r[13] or 0,
            "host_name": (r[14] or "") if len(r) > 14 else "",
        })
    return out


# ====== 引擎绑定真实性同步（docs/weekly-06/engine-binding-truth-sync-design.md）======

# 引擎离线判定为真死、触发场景绑定清理的阈值（秒）。默认 0 = 禁用自动解绑（只提示不清理）；
# 需要自动清理时显式设置 ENGINE_DETACH_AFTER_SECONDS（如 28800 = 8h：心跳 15s，8h 无恢复
# = 连续 ~1920 个心跳周期全丢，机器不会自己回来）。
_ENGINE_DETACH_AFTER_SECONDS = int(os.environ.get("ENGINE_DETACH_AFTER_SECONDS", "0"))


def cleanup_scene_engine_bindings(rid_list: list) -> int:
    """从所有活跃场景的 nodes_json 中移除对 rid_list 引擎的绑定（Issue #250 原逻辑抽出共享）。

    返回被修改的场景数。供两处调用：
    - settings.py 删除 runtime 时（原 _cleanup_scene_engine_runtime_ids，行为等价）
    - _detach_dead_engine_bindings 离线超阈值解绑（引擎绑定真实性同步）
    """
    if not rid_list:
        return 0
    import json as _json
    from datetime import datetime, timezone
    from src.session_manager import db, db_lock
    rid_set = set(rid_list)
    changed_rows = []
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT id, nodes_json FROM devops_pipeline_scene WHERE is_active=1")
            rows = c.fetchall()
        except Exception as _e:
            log.warning(f"cleanup_scene_engine_bindings: query scenes failed: {_e}")
            return 0
        now = datetime.now(timezone.utc).isoformat()
        for row_id, nodes_str in rows:
            try:
                nodes = _json.loads(nodes_str or "[]") if nodes_str else []
            except Exception:
                continue
            changed = False
            for node in nodes:
                eids = node.get("engine_runtime_ids") or []
                if not eids:
                    continue
                new_eids = [eid for eid in eids if eid not in rid_set]
                if len(new_eids) != len(eids):
                    node["engine_runtime_ids"] = new_eids
                    changed = True
            if changed:
                try:
                    c.execute(
                        "UPDATE devops_pipeline_scene SET nodes_json=?, updated_at=? WHERE id=?",
                        (_json.dumps(nodes, ensure_ascii=False), now, row_id))
                    changed_rows.append(row_id)
                except Exception as _e:
                    log.warning(f"cleanup_scene_engine_bindings: update scene {row_id} failed: {_e}")
        db.commit()
    return len(changed_rows)


def merge_duplicate_engine_rows(credential_id: int, c=None) -> dict:
    """按 (credential_id, lower(name)) 去重合并引擎行（Issue #654），保留 MAX(id)。

    与 scope 切换/重注册的「保留最新行」口径一致。被替换行的配置迁移到存活行：
    - engine_capabilities：存活行无 capabilities 行时，被替换行的 capabilities 改指向存活行
      （priority/标签/max_concurrent 继承，不静默回默认 50/'all'）；
    - tags_user_set：存活行=0 且任一被替换行=1 时置 1（用户手动配置过的标记继承）。
    返回 {old_rid: surviving_rid} 映射（无重复行时为空 dict），供调用方
    remap_engine_references 改写引擎 id 引用 + 输出结构化变更事件。
    事务边界：传入 cursor（c）时由调用方 commit；c=None 时自取 db_lock 并 commit。
    """
    from src.session_manager import db, db_lock
    _own_tx = c is None
    if _own_tx:
        # db_lock 可重入，with 只是保证互斥
        with db_lock:
            c = db.cursor()
            return _merge_duplicate_engine_rows_locked(credential_id, c, _own_tx)
    return _merge_duplicate_engine_rows_locked(credential_id, c, _own_tx)


def _merge_duplicate_engine_rows_locked(credential_id: int, c, commit: bool) -> dict:
    rid_map = {}
    try:
        rows = c.execute(
            "SELECT id, lower(name), tags_user_set FROM agent_runtimes WHERE credential_id=?",
            (credential_id,)).fetchall()
    except Exception as _e:
        log.warning(f"merge_duplicate_engine_rows: query failed: {_e}")
        return rid_map
    groups = {}
    for rid, lname, tus in rows:
        groups.setdefault(lname, []).append((rid, tus or 0))
    replaced_ids = []
    for lname, items in groups.items():
        if len(items) < 2:
            continue
        items_sorted = sorted(items, key=lambda x: x[0])
        surviving = items_sorted[-1][0]
        replaced = [rid for rid, _ in items_sorted[:-1]]
        # capabilities 迁移：存活行无 → 被替换行首个有 capabilities 的改指向存活行
        try:
            has_cap = c.execute(
                "SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1",
                (surviving,)).fetchone()
            if not has_cap:
                for rid in replaced:
                    cap = c.execute(
                        "SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1",
                        (rid,)).fetchone()
                    if cap:
                        c.execute(
                            "UPDATE engine_capabilities SET runtime_id=?, "
                            "updated_at=CURRENT_TIMESTAMP WHERE runtime_id=?",
                            (surviving, rid))
                        break
        except Exception as _e:
            log.warning(f"merge_duplicate_engine_rows: migrate capabilities failed: {_e}")
        # tags_user_set 继承
        try:
            if items_sorted[-1][1] == 0 and any(tus for _, tus in items_sorted[:-1]):
                c.execute("UPDATE agent_runtimes SET tags_user_set=1 WHERE id=?", (surviving,))
        except Exception as _e:
            log.warning(f"merge_duplicate_engine_rows: inherit tags_user_set failed: {_e}")
        for rid in replaced:
            rid_map[rid] = surviving
        replaced_ids.extend(replaced)
    if replaced_ids:
        _ph = ",".join("?" * len(replaced_ids))
        try:
            # 未迁移的 capabilities 残留一并清理（避免悬空 runtime_id）
            c.execute(f"DELETE FROM engine_capabilities WHERE runtime_id IN ({_ph})", replaced_ids)
            c.execute(f"DELETE FROM agent_runtimes WHERE id IN ({_ph})", replaced_ids)
        except Exception as _e:
            log.warning(f"merge_duplicate_engine_rows: delete replaced rows failed: {_e}")
        if commit:
            db.commit()
    return rid_map


def remap_scene_engine_bindings(rid_map: dict) -> int:
    """将所有活跃场景 nodes_json 的 engine_runtime_ids 按 rid_map {old: new} 重映射（Issue #654）。

    cleanup_scene_engine_bindings 的泛化：不是移除引用而是改指向存活引擎行，
    场景配置的引擎绑定在引擎行替换（scope 切换去重/重注册）后不悬空。
    返回被修改的场景数。
    """
    if not rid_map:
        return 0
    import json as _json
    from datetime import datetime, timezone
    from src.session_manager import db, db_lock
    changed_rows = []
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT id, nodes_json FROM devops_pipeline_scene WHERE is_active=1")
            rows = c.fetchall()
        except Exception as _e:
            log.warning(f"remap_scene_engine_bindings: query scenes failed: {_e}")
            return 0
        now = datetime.now(timezone.utc).isoformat()
        for row_id, nodes_str in rows:
            try:
                nodes = _json.loads(nodes_str or "[]") if nodes_str else []
            except Exception:
                continue
            changed = False
            for node in nodes:
                eids = node.get("engine_runtime_ids") or []
                if not eids:
                    continue
                new_eids = [rid_map.get(eid, eid) for eid in eids]
                if new_eids != eids:
                    node["engine_runtime_ids"] = new_eids
                    changed = True
            if changed:
                try:
                    c.execute(
                        "UPDATE devops_pipeline_scene SET nodes_json=?, updated_at=? WHERE id=?",
                        (_json.dumps(nodes, ensure_ascii=False), now, row_id))
                    changed_rows.append(row_id)
                except Exception as _e:
                    log.warning(f"remap_scene_engine_bindings: update scene {row_id} failed: {_e}")
        db.commit()
    return len(changed_rows)


def remap_engine_references(rid_map: dict, cid: int = 0, reason: str = "engine_row_replaced") -> dict:
    """引擎行替换后统一 remap 引擎 id 引用（Issue #654），输出结构化变更事件日志。

    覆盖的引用面（每表独立 try/except，旧库缺列/缺表不致命）：
    - devops_pipeline_scene.nodes_json 的 engine_runtime_ids（场景绑定）
    - autopilot_tasks.engine_runtime_ids（任务级意图声明）
    - devops_agent_session.runtime_id / chat_agent_session.runtime_id（agent backend 引用）
    - agent_type.default_runtime_id / agent_type_runtime_pool.runtime_id

    事件日志格式（可追溯）：JSON 单行，含 cid、reason、影响引擎数、旧→新 id 映射、
    各表命中行数。返回 {"scenes": n, "sessions": n, ...} 统计。
    """
    if not rid_map:
        return {}
    import json as _json
    from src.session_manager import db, db_lock
    stats = {"scenes": 0, "autopilot_tasks": 0, "devops_agent_session": 0,
             "chat_agent_session": 0, "agent_type": 0, "agent_type_runtime_pool": 0}
    old_ids = list(rid_map.keys())
    _ph = ",".join("?" * len(old_ids))
    with db_lock:
        c = db.cursor()
        # 1. 场景 nodes_json（结构化 JSON，逐节点替换）
        try:
            stats["scenes"] = remap_scene_engine_bindings(rid_map)
        except Exception as _e:
            log.warning(f"remap_engine_references: remap scenes failed: {_e}")
        # 2. autopilot_tasks.engine_runtime_ids（JSON 列表字符串）
        try:
            c.execute(f"SELECT id, engine_runtime_ids FROM autopilot_tasks WHERE engine_runtime_ids != ''")
            for tid, eids_str in c.fetchall():
                try:
                    eids = _json.loads(eids_str) if eids_str else []
                except Exception:
                    continue
                if not isinstance(eids, list):
                    continue
                new_eids = [rid_map.get(eid, eid) for eid in eids]
                if new_eids != eids:
                    c.execute("UPDATE autopilot_tasks SET engine_runtime_ids=? WHERE id=?",
                              (_json.dumps(new_eids), tid))
                    stats["autopilot_tasks"] += 1
        except Exception as _e:
            log.warning(f"remap_engine_references: remap autopilot_tasks failed: {_e}")
        # 3. agent backend 引用（整型列，CASE 批量替换）
        for table, col in (("devops_agent_session", "runtime_id"),
                           ("chat_agent_session", "runtime_id"),
                           ("agent_type", "default_runtime_id"),
                           ("agent_type_runtime_pool", "runtime_id")):
            try:
                case = "CASE " + " ".join(
                    f"WHEN {col}={old} THEN {new}" for old, new in rid_map.items()) + f" ELSE {col} END"
                c.execute(f"UPDATE {table} SET {col}={case} WHERE {col} IN ({_ph})", old_ids)
                stats[table] = c.rowcount if c.rowcount and c.rowcount > 0 else 0
            except Exception as _e:
                log.warning(f"remap_engine_references: remap {table}.{col} failed: {_e}")
        db.commit()
    # 结构化变更事件（Issue #654 验收：cid、影响引擎数、旧→新 id 映射）
    event = {"event": "engine_reference_remap", "credential_id": cid, "reason": reason,
             "engines_affected": len(rid_map), "id_mapping": {str(k): v for k, v in rid_map.items()},
             **stats}
    log.info("engine_change_event: " + _json.dumps(event, ensure_ascii=False))
    return stats


def _find_scene_bindings_for_runtime(c, runtime_id: int) -> list:
    """查所有活跃场景中绑定了 runtime_id 的 (row_id, repo_key, scene_id, node_tag, before_ids)。

    供 detach 审计用：先记录被移除前的绑定事实，再执行清理。
    """
    import json as _json
    hits = []
    try:
        c.execute("SELECT id, repo_key, scene_id, nodes_json FROM devops_pipeline_scene WHERE is_active=1")
        rows = c.fetchall()
    except Exception as _e:
        log.warning(f"_find_scene_bindings_for_runtime: query failed: {_e}")
        return hits
    for row_id, repo_key, scene_id, nodes_str in rows:
        try:
            nodes = _json.loads(nodes_str or "[]") if nodes_str else []
        except Exception:
            continue
        for node in nodes:
            eids = node.get("engine_runtime_ids") or []
            if runtime_id in eids:
                hits.append((row_id, repo_key or "", scene_id or "",
                             node.get("tag") or node.get("id") or "", eids))
    return hits


def _find_autopilot_tasks_for_runtime(c, runtime_id: int) -> list:
    """查仍绑定 runtime_id 的 autopilot 巡检任务 [{id, name}]（两表协同提示用，不删绑定）。"""
    import json as _json
    out = []
    try:
        c.execute("SELECT id, name, engine_runtime_ids FROM autopilot_tasks WHERE enabled=1")
        rows = c.fetchall()
    except Exception as _e:
        log.warning(f"_find_autopilot_tasks_for_runtime: query failed: {_e}")
        return out
    for tid, tname, eids_str in rows:
        try:
            eids = _json.loads(eids_str) if eids_str else []
        except Exception:
            continue
        if isinstance(eids, list) and runtime_id in eids:
            out.append({"id": tid, "name": tname or f"task-{tid}"})
    return out


def get_detached_engine_hints(scene_row_id: int = 0, runtime_ids: list | None = None,
                              repo_key: str = "") -> list:
    """查询场景/引擎的被解绑提示信息（引擎绑定真实性同步 §4.3/§5.1b 前端数据源）。

    - scene_row_id 非空：查该场景的未重绑（rebound_at 空）解绑记录 → detached_engines
    - runtime_ids 非空：查这些引擎的未重绑解绑记录 → autopilot 任务 engine_detached 提示。
      卡点④修复：必须传 repo_key（任务所属仓库）——引擎审计是全局的，任务提示只关心
      「本仓库场景」的解绑记录，否则其他仓库同引擎的审计会误报到无关任务上。
    返回 [{scene_row_id, scene_id, node_tag, runtime_id, runtime_name, detached_at, affected_autopilot_tasks}]
    """
    from src.session_manager import db, db_lock
    out = []
    with db_lock:
        c = db.cursor()
        try:
            if scene_row_id:
                c.execute(
                    "SELECT scene_row_id, scene_id, node_tag, runtime_id, runtime_name, "
                    "detached_at, affected_autopilot_tasks "
                    "FROM scene_engine_detach_log WHERE scene_row_id=? AND (rebound_at='' OR rebound_at IS NULL)",
                    (scene_row_id,))
            elif runtime_ids:
                _ph = ",".join("?" * len(runtime_ids))
                if repo_key:
                    # 卡点④：按仓库过滤（autopilot 任务提示语义 = 本仓库场景受影响）
                    c.execute(
                        "SELECT scene_row_id, scene_id, node_tag, runtime_id, runtime_name, "
                        "detached_at, affected_autopilot_tasks "
                        f"FROM scene_engine_detach_log WHERE runtime_id IN ({_ph}) AND repo_key=? "
                        "AND (rebound_at='' OR rebound_at IS NULL)",
                        (*runtime_ids, repo_key))
                else:
                    c.execute(
                        "SELECT scene_row_id, scene_id, node_tag, runtime_id, runtime_name, "
                        "detached_at, affected_autopilot_tasks "
                        f"FROM scene_engine_detach_log WHERE runtime_id IN ({_ph}) "
                        "AND (rebound_at='' OR rebound_at IS NULL)",
                        tuple(runtime_ids))
            else:
                return out
            import json as _json
            for r in c.fetchall():
                try:
                    _aff = _json.loads(r[6]) if r[6] else []
                except Exception:
                    _aff = []
                out.append({
                    "scene_row_id": r[0], "scene_id": r[1] or "", "node_tag": r[2] or "",
                    "runtime_id": r[3], "runtime_name": r[4] or "",
                    "detached_at": r[5] or "", "affected_autopilot_tasks": _aff or [],
                })
        except Exception as e:
            log.warning(f"get_detached_engine_hints: query failed: {e}")
    return out


def _detach_dead_engine_bindings() -> dict:
    """引擎绑定真实性同步核心：离线超阈值的引擎，真实清理场景绑定 + 审计留痕。

    幂等：agent_runtimes.detached_from_scenes_at 非空 = 已处理过，跳过。
    双主安全：db_lock + detached 标记在全部场景处理完后才写，失败下轮 reap 重试。
    不删的：agent_runtimes 行本身（保留 status=offline 防历史任务悬空）、
    autopilot_tasks.engine_runtime_ids（任务级意图声明，只提示不删，见设计 §5.1b）。
    """
    import json as _json
    from datetime import datetime, timezone
    from src.runtime.health import parse_dt
    from src.session_manager import db, db_lock

    if _ENGINE_DETACH_AFTER_SECONDS <= 0:
        return {"detached": 0, "disabled": True}

    now = _time.time()
    cutoff_ts = now - _ENGINE_DETACH_AFTER_SECONDS
    stats = {"detached": 0, "scenes_cleaned": 0, "audit_rows": 0}

    with db_lock:
        c = db.cursor()
        # 1. 找离线超阈值且未处理过的引擎
        try:
            c.execute(
                "SELECT id, name, last_seen_at FROM agent_runtimes "
                "WHERE (detached_from_scenes_at='' OR detached_from_scenes_at IS NULL)")
            candidates = c.fetchall()
        except Exception as e:
            log.warning(f"_detach_dead_engine_bindings: query runtimes failed: {e}")
            return stats

        dead = []
        for rid, rname, last_seen in candidates:
            ts = parse_dt(last_seen)
            # 从未心跳（last_seen NULL）的注册残留也视为死引擎
            if ts is None or ts <= cutoff_ts:
                dead.append((rid, rname))
            # ts > cutoff：8h 内有心跳（或从未有过心跳但 detached 标记为空的新行）——不动
        if not dead:
            return stats

        now_iso = datetime.now(timezone.utc).isoformat()
        for rid, rname in dead:
            # 2. 记录被移除前的绑定事实（审计 + detached_engines 提示的数据源）
            bindings = _find_scene_bindings_for_runtime(c, rid)
            affected_tasks = _find_autopilot_tasks_for_runtime(c, rid)

            # 3. 真实清理场景绑定
            try:
                cleaned = cleanup_scene_engine_bindings([rid])
                stats["scenes_cleaned"] += cleaned
            except Exception as e:
                log.warning(f"_detach_dead_engine_bindings: cleanup runtime={rid} failed: {e}")
                continue  # 清理失败不写标记，下轮重试

            # 4. 审计留痕（每 scene+node 一行）
            for row_id, repo_key, scene_id, node_tag, before_ids in bindings:
                try:
                    c.execute(
                        "INSERT INTO scene_engine_detach_log "
                        "(scene_row_id, repo_key, scene_id, node_tag, runtime_id, runtime_name, "
                        "detached_at, reason, affected_autopilot_tasks) VALUES (?,?,?,?,?,?,?,?,?)",
                        (row_id, repo_key, scene_id, node_tag, rid, rname or "",
                         now_iso, "offline_timeout",
                         _json.dumps(affected_tasks, ensure_ascii=False)))
                    stats["audit_rows"] += 1
                except Exception as e:
                    log.warning(f"_detach_dead_engine_bindings: audit insert scene={row_id} failed: {e}")

            # 5. 全部处理完后写幂等标记
            try:
                c.execute(
                    "UPDATE agent_runtimes SET detached_from_scenes_at=? WHERE id=?",
                    (now_iso, rid))
                stats["detached"] += 1
            except Exception as e:
                log.warning(f"_detach_dead_engine_bindings: mark runtime={rid} failed: {e}")
        db.commit()

    if stats["detached"]:
        log.info(f"_detach_dead_engine_bindings: {stats} (threshold={_ENGINE_DETACH_AFTER_SECONDS}s)")
    return stats

