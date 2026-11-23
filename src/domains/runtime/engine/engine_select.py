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

from src.kernel.host_identity import engine_host  # Issue #2094: 引擎/设备身份（GRAPE_HOSTNAME 可覆盖）
from src.kernel.config import CONFIG_DIR, log
from src.kernel.session_manager import db
from src.kernel.repo_key import repo_key_in_params
from src.domains.runtime.engine.agent_tags import load_engine_tags

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
# 撤销 opencode/codearts/hermes 的类型刻板映射——引擎能力与引擎类型无关。
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

# Issue #1208：engine: 维度值域（引擎类型名）。v2 审查整改：不再硬编码固定集合，
# 动态查 agent_runtimes DISTINCT name（已注册引擎类型即为合法值域，扩容免改代码）；
# 查询异常或空结果回退默认已知类型集（保证 L1 校验永不失效）。
_DEFAULT_ENGINE_TYPES = ("opencode", "hermes", "codearts", "codex")


def known_engine_types() -> list:
    """engine: 维度值域：agent_runtimes DISTINCT name（动态值域，v2 审查整改）。

    - 查询成功且非空 → 全部引擎类型名（小写去空白去重排序）；
    - 查询异常或空库 → 回退 _DEFAULT_ENGINE_TYPES（保存端 L1 校验不因 DB 抖动失效）。
    """
    try:
        db, lock = _db()
        with lock:
            c = db.cursor()
            c.execute("SELECT DISTINCT name FROM agent_runtimes WHERE name IS NOT NULL AND name!=''")
            rows = c.fetchall()
    except Exception as e:
        log.warning(f"known_engine_types: dynamic query failed, fallback to defaults: {e}")
        rows = []
    names = {str(r[0]).strip().lower() for r in rows if str(r[0] or "").strip()}
    return sorted(names) if names else sorted(_DEFAULT_ENGINE_TYPES)


# ===== Issue #1208：selector 标签化调度（按特征圈池）=====
# 匹配语义（D14）：无 op 字段，按冒号前缀分组推导——组内 OR（同维度勾多值=任一）
# + 组间 AND（跨维度=定语叠加，不勾的组=不约束）。角色维度让给 #658（正交），
# selector 只管"是不是"（特征圈池），#658 管"会不会"（能力闸门）。

def _selector_match(selector: dict, engine_tags: set) -> bool:
    """Issue #1208：selector 匹配（组内 OR + 组间 AND）。

    - selector 为空（None/{}/tags 空）→ True（不约束）；
    - engine_tags：该引擎 agent_tags 快照（含 engine:/scope:/scene: 派生维度 + 平铺能力标签）；
    - 按冒号前缀分组，任一组无交集 → False；全部组有交集 → True；
    - 未知前缀（label: 等）按分组推导自然参与匹配（L1 保存校验负责值域，匹配层不做判断）；
    - 脏数据容错：tags 非列表按空 selector（不约束），标签统一小写归一。
    """
    if not selector:
        return True
    tags = selector.get("tags") if isinstance(selector, dict) else None
    if not isinstance(tags, list):
        return True
    groups = {}
    for tag in tags:
        if not isinstance(tag, str):
            continue
        _t = tag.strip().lower()
        if not _t:
            continue
        p, _, v = _t.partition(":")
        groups.setdefault(p, set()).add(f"{p}:{v}" if _t != p else _t)
    for _, wanted in groups.items():
        if not (wanted & {str(t or "").strip().lower() for t in (engine_tags or set())}):
            return False
    return True


def get_scene_engine_selector(scene_id: str, agent_type: str, repo_key: str = "",
                              user_id: int = 0) -> dict | None:
    """Issue #1208：读场景 node 的 engine_selector（入队分叉判据第一支）。

    返回 None = 未配 selector（uid/id 池路径或未绑定）；{"tags":[...]} = selector 路径。
    查询异常降级 None（不阻断派发，与 get_scene_engine_binding 同口径）。
    """
    try:
        from src.api.pipeline_config import get_pipeline_config
        cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    except Exception as e:
        log.warning(f"get_scene_engine_selector: scene query failed: {e}")
        return None
    if not cfg or cfg.get("fallback"):
        return None
    for n in cfg.get("nodes") or []:
        if n.get("tag") == agent_type:
            sel = n.get("engine_selector")
            return sel if isinstance(sel, dict) and sel.get("tags") else None
    return None


def get_scene_engine_uids(scene_id: str, agent_type: str, repo_key: str = "",
                          user_id: int = 0) -> list:
    """Issue #1208：读场景 node 的 engine_runtime_uids（uid 池路径分叉判据）。

    读侧 uids 优先回落 ids 的回落逻辑在 get_scene_engine_binding（engine_ids），
    本函数只返回显式 uids（空列表 = 未配 uid 模式）。
    """
    try:
        from src.api.pipeline_config import get_scene_engine_binding
        return get_scene_engine_binding(scene_id, agent_type,
                                        repo_key=repo_key, user_id=user_id).get("engine_uids") or []
    except Exception as e:
        log.warning(f"get_scene_engine_uids: scene query failed: {e}")
        return []


def resolve_redispatch_pool(*, scene_authoritative: bool, scene_engine_ids,
                            scene_selector: dict | None,
                            snapshot_engine_ids: str = "",
                            snapshot_selector: str = "") -> tuple:
    """重派（redispatch）的池列归一：三列互斥，返回 (pool_engine_ids, pool_selector)。

    背景（Issue #1208 补漏，2026-11-18）：重派终态重建路径同时「按当前场景重读 id 池」与
    「原样透传旧任务 pool_selector」，同一行挂两套圈池语义 → claim 第③级 selector 守卫排除
    池内引擎、Issue #605 池过滤排除池外引擎，没有任何引擎能认领，事件长挂 running。
    反方向同样丢语义：场景由 id/uid 池切成 selector 模式时，旧快照 selector 为空、场景 id 池
    也为空 → 新任务两列皆空退化为全量广播，圈池静默失效。

    口径：场景读成功（scene_authoritative）时以场景现行配置为准，与配置模式一一对应；
    场景读不到才回落任务快照，且快照自身也按互斥归一（id 池非空即弃 selector，
    与 enqueue_task 的单点收口同尺）。

    返回值：scene_engine_ids 为 None（场景绑定全灭，#259/#653 fail-closed）时原样透传 None，
    由调用方按既有语义报错；其余情况返回两个字符串列（selector 为 JSON 串或空）。
    """
    import json as _json
    if scene_authoritative:
        # O-1（QA 架构守护第 3 轮）：selector 判定必须早于「绑定全灭」短路。SRE 场景在
        # engine_runtime_ids/uids 皆空时 get_scene_pool_engine_ids 恒返回 None（不广播，
        # Q2 裁定），若先短路 None，则配了「按标签」的 SRE 场景重派会被判成「引擎全部离线」
        # 并 409——与首次派发 `_scene_pool_gate` 的 selector 优先口径相反（selector 模式本就
        # 不依赖 id 池）。绑定全灭只在 selector 模式不成立时才 fail-closed。
        #
        # 判据与 `_scene_pool_gate`（event_pipeline.py:3824 `sel and (sel.get("tags") or [])`）
        # 逐字一致：`{"tags": []}` 不是 selector 模式（dict 真值判定会把它当 selector，从而丢掉
        # 同节点的 id/uid 绑定，与派发侧口径相反）。
        if scene_selector and (scene_selector.get("tags") or []):
            # 场景现行 = selector 模式：id/uid 池必须清空（模式互斥，B-01 同尺）
            return "", _json.dumps(scene_selector, ensure_ascii=False)
        if scene_engine_ids is None:
            return None, ""
        return (scene_engine_ids or ""), ""
    ids = (snapshot_engine_ids or "").strip()
    if ids and ids != "[]":
        return snapshot_engine_ids, ""
    return (snapshot_engine_ids or ""), (snapshot_selector or "")


def _all_engines_snapshot() -> list:
    """全量引擎快照（selector 候选集底表）：agent_runtimes LEFT JOIN credentials。

    档位列按 ``_engine_scope_info`` 同款口径取（QA 架构守护 I-1）：凭据列优先、
    镜像列回退——claim_by_tag 读的是 runtime_credentials，只读 agent_runtimes 的
    镜像列在注册时账号域不可用等场景会漂移，候选与认领两侧分叉。
    不做 scope/健康过滤（候选分层判定在 selector_candidate_engines）；
    查询异常返回空列表（零候选 → fail-closed 语义）。
    """
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, "
                "r.last_seen_at, COALESCE(r.scope,'global'), COALESCE(r.owner_id,''), "
                "COALESCE(r.role_tags,''), COALESCE(rc.scope,''), COALESCE(rc.owner_id,'') "
                "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                "ORDER BY r.id")
            rows = c.fetchall()
        except Exception as e:
            # 旧库/精简测试 schema 缺 runtime_credentials：退回镜像列（不阻断候选集）
            log.warning(f"_all_engines_snapshot credential join failed, fallback to mirror columns: {e}")
            try:
                c.execute(
                    "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, "
                    "r.last_seen_at, COALESCE(r.scope,'global'), COALESCE(r.owner_id,''), "
                    "COALESCE(r.role_tags,'') FROM agent_runtimes r ORDER BY r.id")
                rows = [tuple(r) + ("", "") for r in c.fetchall()]
            except Exception as e2:
                log.warning(f"_all_engines_snapshot failed: {e2}")
                return []
    return [{"runtime_id": r[0], "name": r[1] or "", "status": r[2] or "offline",
             "healthy": bool(r[3]), "installed": bool(r[4]), "credential_id": r[5] or 0,
             "last_seen_at": r[6] or "", "scope": (r[10] or r[7] or "global"),
             "owner_id": (r[11] or r[8] or ""),
             "role_tags": r[9] or ""} for r in rows]


def _selector_scope_blocks(m: dict, repo_key: str = "", user_id: int = 0) -> bool:
    """Issue #1208：claim 第 ⑥ 级 scope 隔离在候选集里的同尺判定（2026-11-19）。

    背景：selector 候选/命中预览此前对所有档位引擎一律放行（仅 SRE 收紧 global），
    而 claim_by_tag 的 scope SQL 是隔离的——repo 档只认领本仓任务、user 档只认领
    owner 本人创建的任务。于是别人的个人引擎、别的仓库的仓库级引擎被算成「命中」：
    场景详情页徽章按全库候选计 6 台，弹窗按 dashboard 可见引擎计 4 台；更关键的是
    入队 fail-closed 门禁（selector_enqueue_failure_reason）同样被这批永远认领不到的
    引擎骗过，任务照常入队后没人接。

    判定与 claim 同尺（agent_runtimes 的 scope/owner_id 是凭据的镜像列）：
    - credential_id=0（本机直连）→ 不设限（claim 侧不查凭据 scope）；
    - global → 不设限（可认领任意仓库的池任务）；
    - user → owner_id 必须等于请求 user_id（claim 侧 t.user_id = owner 账号）；
    - repo → repo_key 必须落在 claim_by_tag 同款三精确候选集（owner_id /
      gitcode:owner_id / github:owner_id）内——不做去前缀归一，避免跨平台同
      owner/repo 被放宽成「候选命中、claim 永远认不到」。
    上下文缺失（user_id=0 / repo_key 空）或 owner_id 未落库时该维度不设限：调用方
    没给上下文就无法判定，宁放行不误杀（与既有 fail-open 容错口径一致）。
    """
    if (m.get("credential_id") or 0) == 0:
        return False
    scope = (m.get("scope") or "global").strip().lower()
    if scope == "global":
        return False
    owner = str(m.get("owner_id") or "").strip()
    if not owner:
        return False
    if scope == "user":
        if not user_id:
            return False
        try:
            return int(owner) != int(user_id)
        except (TypeError, ValueError):
            return owner != str(user_id)
    if scope == "repo":
        if not repo_key:
            return False
        # 与 claim_by_tag 的 repo 档 SQL 逐字同源（service.py 2090 起：owner_id /
        # gitcode:owner_id / github:owner_id 三精确候选）。去前缀归一比较会在
        # 「owner 自带平台前缀」「跨平台同 owner/repo」两种形态下比 claim 宽松
        # （QA 架构守护 I-2 指出的 fail-open 残留），故此处按三候选集合判定。
        return repo_key not in (owner, f"gitcode:{owner}", f"github:{owner}")
    return False


def selector_candidate_engines(selector: dict, agent_type: str,
                               repo_key: str = "", user_id: int = 0) -> tuple:
    """Issue #1208 入队即时校验（§2.3 步骤 2）：selector 候选集 + 分层排除原因。

    判定顺序即语义（对齐 claim 六层守卫，正交勿合并）：
      selector 匹配（agent_tags 派生快照）→ #658 角色硬过滤 → 健康尺子（#259 语义）→
      凭据 scope 隔离（第 ⑥ 级同尺）→ 能力行（Issue #3396：SRE scope 收紧档已删）。
    返回 (matched, dropped)：matched 为通过全部过滤的引擎列表；
    dropped 为 [{"runtime_id", "name", "reason"}]，reason ∈ selector/role/offline/stale/scope，
    供命中预览分层标注与配置死/瞬时死分流判定复用。
    """
    members = _all_engines_snapshot()
    if not selector or not (selector.get("tags") or []):
        return [], []
    db, lock = _db()
    matched, dropped = [], []
    with lock:
        c = db.cursor()
        for m in members:
            rid = m["runtime_id"]
            tags = load_engine_tags(c, rid)
            if not _selector_match(selector, tags):
                dropped.append({"runtime_id": rid, "name": m["name"], "reason": "selector"})
                continue
            # #658 角色硬守卫（selector 命中后叠加：角色六词不进 selector，防双入口漂移 D8）
            # Issue #3396：口径统一到 engine_capabilities（与派发/认领同一把尺子，UI 勾选即生效）；
            # 空标签 fail-closed（A7），role_tags 注册声明不再参与守卫判定（A4）。
            if is_agent_role(agent_type) and not engine_allows_role(rid, agent_type):
                dropped.append({"runtime_id": rid, "name": m["name"], "reason": "role"})
                continue
            if not _health_ok(m):
                why = "offline" if (m.get("status") or "") != "online" else "stale"
                dropped.append({"runtime_id": rid, "name": m["name"], "reason": why})
                continue
            # 第 ⑥ 级 scope 隔离（claim_by_tag 同尺）：别人的 user 档引擎、别的仓库的
            # repo 档引擎永远认领不到本场景任务，不能算命中（徽章/入队门禁同源）。
            if _selector_scope_blocks(m, repo_key=repo_key, user_id=user_id):
                dropped.append({"runtime_id": rid, "name": m["name"], "reason": "scope"})
                continue
            # 能力行匹配（claim 第 1 级同款：精确 tag OR 'all'；tags 已含平铺行）
            if agent_type and agent_type not in tags and "all" not in tags:
                dropped.append({"runtime_id": rid, "name": m["name"], "reason": "capability"})
                continue
            matched.append(m)
    return matched, dropped


def selector_enqueue_failure_reason(selector: dict, agent_type: str,
                                    repo_key: str = "", user_id: int = 0) -> str:
    """Issue #1208 入队 fail-closed 文案分流（单函数封装留切换余地，§2.3 步骤 3）。

    - 命中 ≥1（健康候选存在）→ ""（可入队，非失败）；
    - 配置死（引擎从来没带过该标签，写错永远不会好）→「无引擎满足标签 X——去改场景」；
    - 瞬时死（命中引擎当前均不在线，可能正在重启）→「稍后 redispatch 重试」（不下死刑结论）。
    redispatch 重派时重新校验（对齐 _fresh_server_url 的"重派不信旧判断"模式）。
    """
    matched, dropped = selector_candidate_engines(selector, agent_type,
                                                  repo_key=repo_key, user_id=user_id)
    if matched:
        return ""
    tags = ", ".join(str(t) for t in ((selector or {}).get("tags") or []))
    # 瞬时死：有引擎带过该标签（selector 匹配通过）但当前全离线/心跳过期——
    # 不下死刑结论；其余（含空库/标签无人携带/角色全排除）→ 配置死，去改场景
    if any(d.get("reason") in ("offline", "stale") for d in dropped):
        return (f"命中标签 [{tags}] 的引擎当前均不在线，可能正在重启——任务暂不入队，"
                f"稍后 redispatch 重试（Issue #1208）")
    return (f"无引擎满足标签 [{tags}]——请检查场景引擎配置（去场景编辑中调整标签，"
            f"Issue #1208）")


def validate_selector_tags(tags, scene_values=None) -> str:
    """Issue #1208 保存端 L1 语法校验（D13，服务端 agents.py/scenes.py 为权威）。

    - 格式规范：字符串、含冒号、全小写、值非空、不含空白；
    - 值域查表：scope:global|user|repo 固定硬编码；engine: 取引擎类型名值域；
      scene: 动态查 scene_market 活跃行（调用方传 scene_values，空回退 default）；
    - 未知前缀 → 文案含「未实现的维度，自定义请用 label: 前缀」（label: 用户 CRUD
      逃生舱维度独立 spec，v1 未上线前同样按未知前缀拒绝）；
    - 返回 "" = 合法；非空 = 人话错误文案（调用方 400 透传）。
    """
    if not tags:
        return ""
    if not isinstance(tags, list):
        return "engine_selector.tags 必须为字符串数组"
    scope_domain = ["global", "user", "repo"]
    engine_domain = known_engine_types()
    if not scene_values:
        scene_values = ["default"]
    for t in tags:
        if not isinstance(t, str):
            return f"标签 {t!r} 非字符串"
        _t = t.strip()
        if ":" not in _t:
            return f"标签格式错误：{_t!r}（应为 前缀:值 形态，如 scope:repo）"
        if _t != _t.lower() or any(ch.isspace() for ch in _t) or not _t.split(":", 1)[1].strip():
            return (f"标签格式错误：{_t!r}（需全小写、不含空格、冒号后值非空）")
        p, _, v = _t.partition(":")
        v = v.strip()
        if p == "scope":
            if v not in scope_domain:
                return f"scope 值域外：{v}（合法值：{'/'.join(scope_domain)}）"
        elif p == "engine":
            if v not in engine_domain:
                return f"engine 值域外：{v}（合法值：{'/'.join(engine_domain)}）"
        elif p == "scene":
            if v not in [str(x).strip().lower() for x in scene_values]:
                return f"scene 值域外：{v}（合法值：{'/'.join(scene_values)}，动态取自场景市场活跃行）"
        else:
            if p == "label":
                # [F10 文案对齐 2026-11-13] label: 是已预留但未实现的维度（D20 排期），
                # 特判明示，不再落「自定义请用 label: 前缀」的误导文案。
                return "label: 维度排期中（Issue #1208 D20），本期不可用"
            return (f"未实现的维度：{p}:（本期仅支持 engine:/scope:/scene:，"
                    f"自定义标签维度未开放）")
    return ""


def is_agent_role(tag: str) -> bool:
    """pool_tag 是否为已知节点角色（Issue #658 认领侧守卫的适用判定）。"""
    return (tag or "").strip().lower() in KNOWN_AGENT_ROLES

def engine_role_tags(engine_name: str, role_tags_json: str = "") -> list | None:
    """解析引擎注册声明的 role_tags（注册透传轨）。

    Issue #3396：角色守卫已统一改读 engine_capabilities（单一事实源，UI 勾选即生效），
    本函数仅供注册链路透传解析与展示，**守卫不再消费**；返回值 None = 未声明（仅注册语义）。
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

def engine_capability_tags(runtime_id: int) -> list:
    """读引擎角色标签（Issue #3396 单一事实源：engine_capabilities，UI 勾选即生效）。

    返回 enabled=1 的标签列表；runtime_id 无效或查询异常 → 空列表。
    空标签 fail-closed（A7 裁定：手动清空标签的引擎任何角色两侧均不可调度，
    绝不回退「不限角色」敞开口径）；存量未手工设标签的引擎由注册/本机发现
    自动补 'all'（A8），行为与基线一致。
    """
    rid = runtime_id or 0
    if not rid:
        return []
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return []
    try:
        db, lock = _db()
        with lock:
            c = db.cursor()
            c.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rid,))
            return [str(r[0] or "").strip().lower() for r in c.fetchall() if str(r[0] or "").strip()]
    except Exception:
        return []


def engine_allows_role(runtime_id: int, agent_type: str) -> bool:
    """Issue #3396 统一判定：引擎是否可服务指定角色（engine_capabilities 单一事实源）。

    agent_type ∈ tags 或 'all' ∈ tags 即放行；空标签/查询异常 fail-closed。
    agent_type 为空（未指定角色）不限制。
    """
    at = (agent_type or "").strip().lower()
    if not at:
        return True
    tags = engine_capability_tags(runtime_id)
    return at in tags or "all" in tags


def _member_role_tags(m: dict) -> list:
    """从候选引擎成员读有效角色集（Issue #3396：engine_capabilities 单一事实源；空 = fail-closed）。"""
    return engine_capability_tags(m.get("runtime_id") or 0)

def _filter_by_role(members: list[dict], agent_type: str) -> list[dict]:
    """Issue #658 角色硬过滤（候选筛选最前置）：agent_type ∈ engine 有效角色集。

    Issue #3396：有效角色集改读 engine_capabilities（单一事实源，UI 勾选即生效，
    与 _filter_by_tags 同口径）；空标签 fail-closed——无标签引擎不参与任何角色调度。
    """
    at = (agent_type or "").strip().lower()
    if not at or not members:
        return members
    return [m for m in members if engine_allows_role(m.get("runtime_id") or 0, at)]

def _role_no_candidates_detail(members: list[dict], agent_type: str) -> str:
    """角色硬过滤后无候选的显式错误信息（含 agent_type 与各候选引擎标签）。"""
    at = (agent_type or "").strip().lower()
    parts = []
    for m in members:
        roles = _member_role_tags(m)
        parts.append(f"{m.get('name') or '?'}(id={m.get('runtime_id')}):{','.join(roles) or 'none'}")
    return (f"no engine matches agent_type '{at}' by engine_capabilities "
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
    # Issue #653 / #2791 回归复核：场景节点已绑引擎时，候选解析为空（引擎已注销/全部离线）
    # 绝不静默回退全局池——默认严格（存量部署未写该键也是严格模式）。
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
    from src.kernel.repo_key import from_repo_key
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
    """引擎相关的表（agent_runtimes / runtime_credentials / engine_capabilities）都在 runtime 域。

    本模块同时被 mgmt 进程调用（创建需求后选引擎），三库模式下 mgmt 进程的主库是本域库，
    直连进程库会撞「表不存在」，于是选引擎失败、需求永远不入队。这里按域取连接：
    own DSN 优先（线程本地连接），单库/测试拓扑回落进程库。
    """
    from src.kernel import db_registry
    if db_registry.uses_own_dsn("runtime"):
        conn = db_registry.domain_db("runtime")
        if conn is not None:
            return conn, db_registry.domain_lock("runtime")
    from src.kernel.session_manager import db, db_lock
    return db, db_lock

def _member_scope(m: dict, repo: str) -> str:
    """成员档位：repo（仓库级专属）/ global（远端全局）/ local（本机直连）。"""
    cid = m.get("credential_id") or 0
    if cid == 0:
        return "local"                       # 本机直连（默认视为全局，但归 local 档位最后兜底）
    if m.get("scope") == "repo":
        from src.kernel.repo_key import from_repo_key
        oid = from_repo_key(m.get("owner_id") or "")
        if oid and oid == repo:
            return "repo"
    return "global"

def _health_ok(m: dict) -> bool:
    """健康判定：唯一真源 src.domains.runtime.engine.health.engine_health_ok
    （Issue #592 / #259 / #1487 / #1496 收敛，对齐 origin/weekly-10）——派发与认领共用同一把尺子，
    禁止在此复制判定逻辑（arch 分叉期间此处曾保留独立实现，导致与认领侧口径分叉）。

    本机直连（credential_id=0）→ installed+healthy，不判心跳（本机不参与 15s worker 心跳）；
    远端（credential_id>0）→ status 在线 + healthy==1 + last_seen_at 在 _RUNTIME_STALE_SECONDS 内。
    """
    from src.domains.runtime.engine.health import engine_health_ok
    return engine_health_ok(
        m.get("credential_id") or 0,
        m.get("status"),
        m.get("healthy"),
        m.get("installed"),
        m.get("last_seen_at"),
        stale_seconds=_RUNTIME_STALE_SECONDS,
    )


def _backend_claimable_map(runtime_ids: list) -> dict | None:
    """Issue #3928 R2：派发候选与认领闸门同尺（backend_status + circuit_*）。

    返回 ``{runtime_id: 是否可认领}``；空列表输入返回 ``{}``。缺列（存量库未迁移）/
    查询异常 → 返回 ``None``：调用方不参与后端过滤（fail-open 旧行为，绝不因健康态
    查询失败挡住派发）。注意 ``{}`` 与 ``None`` 语义不同——前者是「查到了，但全部不可
    认领」，后者是「查不了」。
    """
    ids = sorted({int(r) for r in runtime_ids if r})
    if not ids:
        return {}
    db, lock = _db()
    try:
        with lock:
            c = db.cursor()
            ph = ",".join("?" * len(ids))
            c.execute(
                "SELECT id, COALESCE(backend_status,''), COALESCE(circuit_open_until,''), "
                "COALESCE(circuit_half_open,0) FROM agent_runtimes "
                f"WHERE id IN ({ph})", tuple(ids))
            rows = c.fetchall()
    except Exception as e:
        log.debug(f"_backend_claimable_map failed (fail-open): {e}")
        return None
    from src.domains.runtime.engine.backend_health import claim_eligible
    now = _time.time()
    return {int(r[0]): claim_eligible(r[1], r[2], r[3], now) for r in rows}


def _backend_claimable(members: list[dict]) -> dict | None:
    """Issue #3928 R2：候选 runtime_id → 后端健康态是否可认领（``None`` = 查不了）。

    与 ``backend_health.claim_eligible`` 同一把尺子（认领 / 池可达性 / 派发候选）。
    """
    return _backend_claimable_map([m.get("runtime_id") for m in members])


def _backend_blocked(m: dict, claimable: dict | None) -> bool:
    """候选是否被后端健康态挡住。

    ``claimable=None``（缺列/查询异常）或行不存在（老库/合成候选）→ 不拦，保持旧行为；
    只有「查到了且明确不可认领」才拦。
    """
    if claimable is None:
        return False
    _rid = m.get("runtime_id")
    if not _rid:
        return False
    return claimable.get(int(_rid)) is False


def _apply_filters(members: list[dict], cfg: dict) -> list[dict]:
    f = cfg.get("filters") or {}
    _claimable = _backend_claimable(members)
    out = []
    for m in members:
        if not m.get("enabled", True):
            continue
        # Issue #3928 R2：degraded / unavailable 在冷却期内不得被派发候选选中，
        # 否则「派发选中、认领拒绝」会空转到 120s stale 判死（spec In-2 池可达性同尺）。
        if _backend_blocked(m, _claimable):
            continue
        if f.get("only_installed") and not m.get("installed"):
            continue
        if f.get("only_healthy") and not m.get("healthy"):
            continue
        if f.get("only_online") and not _health_ok(m):
            continue
        # 本机直连引擎（credential_id=0）必须为已知引擎类型（防幽灵 `default-cred0` 占位，见 engine-selection-design.md §7.3）
        if (m.get("credential_id") or 0) == 0:
            from src.domains.mgmt.public import is_known_engine_name
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
    - 个人/远端：`{scope}:{instance_name}`（instance_name 与 src/domains/runtime/engine/instances.py 同源）
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
        from src.domains.runtime.engine.instances import instance_name
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

def _repo_user_id(repo_key: str, issue_creator: str = "") -> int:
    """反查 repo_key 对应的 user_id（优先 Issue 创建者）。

    解析链全部经 mgmt public 原语（任意拓扑），本文件不持有任何 SQL：
      1. 平台登录名 → 账号：external_identities 是平台登录名的权威域，命中即权威（D31）；
      2. OAuth 映射按平台列取 user_id（Issue #76 修复）；
      3. 平台登录名 → 账号（EXISTS 校验登录名，注册名≠平台 login 也命中，D32 补丁）；
      4. 平台数字 ID → 账号；
      5. 仓库成员回退：按创建者过滤（而非取第一个 join 用户），未注册成员靠它命中。
    issue_creator 为空时保留原逻辑——取最早加入的成员。

    Issue #76 修复：原逻辑按 user_id ASC 取第一条 is_join=1 的记录，
    对于多用户仓库会返回"最早加入的用户"而非 Issue 创建者，
    导致 scope=user 引擎无法通过 claim_by_tag 认领任务。

    Issue #211 增强：二级回退 + 成员回退按创建者过滤，
    确保已注册 grape 的项目组成员能正确映射而非落为 user_id=0。
    """
    if not repo_key:
        return 0
    repo = _norm_repo(repo_key)
    try:
        from src.domains.mgmt import public as _mgmt_public

        if issue_creator:
            if not issue_creator.strip():
                return 0
            from src.kernel.repo_key import parse_repo_key
            from src.domains.mgmt.public import resolve_user_id_by_platform_login
            _provider, _, _ = parse_repo_key(repo_key)
            # M5/S51（D31）：身份优先——平台登录名权威域命中即权威；
            # 原二级回退里已有同一身份查询，此处置前并保留原链兜底
            _uid = resolve_user_id_by_platform_login(_provider, issue_creator)
            if _uid:
                return _uid
            # 1. OAuth 映射优先（Issue #76 修复）：列名由 mgmt 侧白名单挑选，本域不拼 SQL
            from src.kernel.repo_key import um_column_for_repo
            _uid = _mgmt_public.mapping_user_id_by_column(
                um_column_for_repo(repo_key), issue_creator)
            if _uid:
                return _uid
            # 2. 二级回退：按平台登录名取账号（EXISTS 校验，LOWER 比较）
            _uid = _mgmt_public.account_id_by_platform_login(_provider, issue_creator)
            if _uid:
                return _uid
            # 3. 按平台数字 ID 查（issue_creator 可能是平台数字 ID；P4 已归 mgmt 原语）
            _uid = _mgmt_public.external_user_id(_provider, issue_creator)
            if _uid is not None:
                return _uid
            # 4. 成员回退：按创建者过滤（JOIN 全在 mgmt 域内，不在本域拆表）
            _uid = _mgmt_public.repo_user_id_by_platform_login(
                _provider, issue_creator, repo, f"{_provider}:{repo}")
            return _uid if _uid is not None else 0
        # issue_creator 为空：保留原逻辑（取第一个 join 用户）
        _uid = _mgmt_public.first_join_user_id_by_repo(repo, f"gitcode:{repo}")
        return _uid if _uid is not None else 0
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
    from src.kernel.repo_key import from_repo_key
    if scope == "repo" and from_repo_key(owner_id or "") == repo:
        return _PRIO_REPO
    return int(cfg.get("local_priority", _PRIO_LOCAL)) if scope == "local" else _PRIO_GLOBAL

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


def _engines_by_uids(engine_uids: list, repo: str) -> list[dict]:
    """Issue #1011：按 engine_uid 列表查引擎详情（uid 池路径，字段同 _engines_by_ids）。

    uid 无效/未迁移（NULL uid）行不命中即排除——语义与 id 池一致；
    未迁移旧库（缺 engine_uid 列）返回空列表（调用方按全灭处理）。
    """
    if not engine_uids:
        return []
    norm = [str(u or "").strip().lower() for u in engine_uids if str(u or "").strip()]
    if not norm:
        return []
    db, lock = _db()
    members = []
    with lock:
        c = db.cursor()
        try:
            placeholders = ",".join("?" * len(norm))
            c.execute(
                "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                " r.scope, r.owner_id, LOWER(r.engine_uid), "
                " COALESCE(rc.device_name, ''), COALESCE(rc.owner_name, ''), COALESCE(r.role_tags, '') "
                "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id "
                f"WHERE LOWER(r.engine_uid) IN ({placeholders})",
                tuple(norm))
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"_engines_by_uids failed: {e}")
            return []
    for r in rows:
        members.append({
            "runtime_id": r[0], "name": r[1] or "", "status": r[2] or "offline", "healthy": bool(r[3]),
            "installed": bool(r[4]), "credential_id": r[5] or 0, "last_seen_at": r[6] or "",
            "scope": r[7] or "global", "owner_id": r[8] or "", "engine_uid": r[9] or "",
            "device_name": r[10] or "",
            "owner_name": r[11] or "", "priority": 0, "role_tags": r[12] or "",
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

def _filter_by_health(members: list[dict]) -> list[dict]:
    """健康度过滤增强层：从候选引擎中过滤出健康的。

    健康分 >= HEALTH_SCORE_THRESHOLD（默认 30）且连续失败 < MAX_CONSECUTIVE_FAILURES（默认 5）。
    全部不健康时返回空列表（调用方降级兜底）。
    """
    try:
        from src.domains.runtime.engine.health_scorer import filter_healthy
        rids = [m["runtime_id"] for m in members if m.get("runtime_id")]
        healthy_rids = set(filter_healthy(rids))
        return [m for m in members if m.get("runtime_id") in healthy_rids]
    except Exception as e:
        log.warning(f"_filter_by_health failed, skipping health filter: {e}")
        return members  # 模块异常时不过滤（不阻塞派发）

def _best_health_fallback(members: list[dict]) -> int | None:
    """全部不健康时的降级兜底：取健康分最高的引擎。"""
    try:
        from src.domains.runtime.engine.health_scorer import best_fallback
        rids = [m["runtime_id"] for m in members if m.get("runtime_id")]
        return best_fallback(rids)
    except Exception as e:
        log.warning(f"_best_health_fallback failed: {e}")
        # 兜底的兜底：取第一个候选
        return members[0]["runtime_id"] if members else None

# Issue #2791：`pool_engine_source` 取值（与 db schema 注释、local-claimer 设计的降级
# 判定共用一套词汇）：scene=场景绑定 / auto=引擎自动选择 / directed=engine_config 定向锁定。
POOL_SOURCE_SCENE = "scene"
POOL_SOURCE_AUTO = "auto"
POOL_SOURCE_DIRECTED = "directed"


def resolve_scene_user_scope(repo_key: str, issue_id, actor: str = "") -> tuple[str, int]:
    """Issue / PR 级场景 + 场景 user scope 的单点解析（派发入口共用）。

    scene_user_id 参与技能 scope 门禁与任务行 user_id；Task Center 建的 repo 级场景
    任务可能漏写（=0），此时按 actor 反查兜底。场景解析异常必须留痕（Issue #653：
    曾经静默回退「default」= 节点查不到 → 全局池的旁路）。

    返回 (scene_id, user_id)；scene_id 恒非空（回落 "default"）。
    """
    _scene_id, _scene_uid = "", 0
    try:
        from src.api.session_store import get_issue_scene
        _scene_id, _scene_uid = get_issue_scene(repo_key, issue_id) or ("", 0)
    except Exception as _se:
        log.warning(f"Pipeline emit: get_issue_scene failed for repo={repo_key} "
                    f"issue={issue_id}: {_se}")
    _scene_uid = _scene_uid or 0
    if not _scene_uid:
        try:
            _scene_uid = _repo_user_id(repo_key, issue_creator=actor) or 0
        except Exception:
            _scene_uid = 0
    return (_scene_id or "default"), _scene_uid


def log_directed_fallback(directive, runtime_id, pool_engine_ids, repo_key: str,
                          agent_type: str = "", seq=0) -> str:
    """定向未命中时的结构化 WARN（单点实现：所有派发入口共用同一行格式）。

    字段固定为 runtime_id + 回落原因 + 最终选中引擎 + pool + repo/agent/seq，
    Spec 验收「不静默换引擎」就靠这一行的可观测性。返回写出的日志正文。
    """
    _final_desc = "broadcast"
    try:
        if runtime_id:
            _final_desc = f"{engine_name(runtime_id) or '-'}(id={runtime_id})"
        elif pool_engine_ids:
            try:
                _warn_ids = [int(x) for x in (json.loads(pool_engine_ids) or [])]
            except Exception:
                _warn_ids = []
            if len(_warn_ids) == 1:
                _final_desc = f"{engine_name(_warn_ids[0]) or '-'}(id={_warn_ids[0]})"
            elif _warn_ids:
                _final_desc = "pool:" + ",".join(str(x) for x in _warn_ids)
    except Exception as _e:
        log.warning(f"dispatch_directed: describe selected engine failed: {_e}")
    _pool_desc = pool_engine_ids or "\"\""
    _body = (f"dispatch_directed: engine_config.runtime_id={directive.get('requested')} "
             f"not used, fallback to global pool; reason={directive.get('reason') or 'unavailable'}; "
             f"selected_engine={_final_desc}; pool_engine_ids={_pool_desc}; "
             f"repo={repo_key} agent={agent_type} seq={seq}")
    log.warning(_body)
    return _body


def directed_pool_engine_ids(runtime_id: int) -> str:
    """定向命中的 pool 收敛值：单元素 JSON 串（锁定引擎 = pool 就是它自己）。"""
    return json.dumps([int(runtime_id)])


def parse_engine_config_runtime_id(engine_config) -> int:
    """Issue #2791：从 A2A `engine_config` 解析定向 runtime_id（0 = 未指定/非法）。

    契约形态（cross-pipeline-handoff-design.md）：`{"type": "runtime", "runtime_id": N}` | null。
    兼容宽松形态（只给 runtime_id / 纯整数 / 数字串），但**只认 runtime 类型**——
    `{"type": "pool"}` 之类不是定向派发，一律 0（不改变池选择语义）。
    """
    if engine_config is None or isinstance(engine_config, bool):
        return 0
    _raw = engine_config
    if isinstance(engine_config, dict):
        _type = str(engine_config.get("type") or "runtime").strip().lower()
        if _type not in ("runtime", ""):
            return 0
        _raw = engine_config.get("runtime_id", 0)
    if isinstance(_raw, (int, float)):
        _rid = int(_raw)
    elif isinstance(_raw, str):
        _s = _raw.strip()
        if not _s.isdigit():
            return 0
        _rid = int(_s)
    else:
        return 0
    return _rid if _rid > 0 else 0


def resolve_directive_engine(engine_config, repo_key: str, agent_type: str = "") -> dict:
    """Issue #2791：派发入口对 `engine_config.runtime_id` 的单点解析（命中即锁定）。

    返回 dict：
    - requested: 配置指定的 runtime_id（0 = 未配置）
    - runtime_id: 命中时为该引擎 id；未命中 0
    - hit: 是否命中（已注册且健康可达）
    - reason: 未命中原因（engine_not_registered / engine_not_installed /
      engine_not_online(status=..) / engine_unhealthy / stale_heartbeat / lookup_failed）；
      命中恒为 "hit"（标签不匹配不改变命中，见 tag_mismatch）
    - engine_name: 命中或未命中时该引擎行的类型名（未注册时为空）
    - tag_mismatch: 命中但该引擎没有 agent_type 的能力标签（命中仍成立）

    「健康可达」与池选择同一把尺子（engine_health_ok，见 health.py），保证
    「池里选得到」与「定向锁得住」不会互相矛盾。

    能力标签则**故意不同尺**（评审 P4 取舍）：池选择用 `_filter_by_tags` 把无该 agent
    标签的引擎过滤掉；定向派发是显式指令（运维/上游明确点名一台引擎），再做标签否决
    等于把显式指令降级成建议。因此命中条件不含标签，但命中时会把 tag_mismatch 打成
    结构化 WARN——「池会排除它，但被显式指定」必须可观测，不能是静默旁路。
    """
    _out = {"requested": 0, "runtime_id": 0, "hit": False, "reason": "",
            "engine_name": "", "tag_mismatch": False}
    requested = parse_engine_config_runtime_id(engine_config)
    if not requested:
        return _out
    _out["requested"] = requested
    try:
        cfg = load_engine_selection()
        members = _engines_by_ids([requested], _norm_repo(repo_key), cfg)
    except Exception as e:
        _out["reason"] = f"lookup_failed: {e}"[:200]
        log.warning(f"resolve_directive_engine: lookup runtime_id={requested} failed: {e}")
        return _out
    if not members:
        _out["reason"] = "engine_not_registered"
        return _out
    m = members[0]
    _out["engine_name"] = m.get("name") or ""
    if not _health_ok(m):
        if not m.get("installed"):
            _out["reason"] = "engine_not_installed"
        elif (m.get("status") or "") not in ("online", "active"):
            _out["reason"] = f"engine_not_online(status={m.get('status') or 'offline'})"
        elif not m.get("healthy"):
            _out["reason"] = "engine_unhealthy"
        else:
            _out["reason"] = f"stale_heartbeat>{_RUNTIME_STALE_SECONDS}s"
        return _out
    _out.update({"runtime_id": requested, "hit": True, "reason": "hit"})
    # 标签口径只用于「观测」，不用于否决（见 docstring 的取舍说明）
    if agent_type:
        try:
            _tag_hit = bool(_filter_by_tags([m], agent_type))
        except Exception as _tag_e:
            log.warning(f"resolve_directive_engine: capability tag lookup failed for "
                        f"runtime_id={requested}: {_tag_e}")
            _tag_hit = True
        if not _tag_hit:
            _out["tag_mismatch"] = True
            log.warning(
                f"dispatch_directed: engine_config.runtime_id={requested} "
                f"({m.get('name') or '-'}) has no capability tag for agent={agent_type}; "
                f"explicit directive takes precedence over the pool tag filter "
                f"(pool selection would exclude it)")
    return _out


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

    def _pick_decision(result: str, selected=None, reason: str = "", members=None) -> None:
        """Issue #605：pick_engine 决策留痕（候选全集 + 每候选过滤原因 + 选中理由）。"""
        _decision_log(
            "pick_engine", repo_key=repo_key, agent_type=agent_type, scene_id=scene_id,
            result=result, selected=selected, reason=reason,
            candidates=[
                {"rid": m.get("runtime_id"),
                 "reasons": _candidate_reject_reasons(m, cfg, agent_type)}
                for m in (members or [])
            ],
        )

    if not cfg.get("enabled", True):
        _pick_decision("disabled")
        return None
    # Plugin 模式：统一广播，engine_runtime_ids 由 get_scene_pool_engine_ids 写入 pool_engine_ids
    # （Issue #3396：SRE 禁广播特例已删除，广播范围收口统一为角色/健康过滤）
    try:
        from src.domains.runtime.pipeline.event_pipeline import is_plugin_mode
        if is_plugin_mode():
            _pick_decision("plugin_broadcast")
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
    # Issue #3396：删除 SRE 禁广播特例（_sre_no_broadcast）——SRE 与其他角色同走
    # 通用选择链（role → tag → scope → health），global 池广播放开；官方机器由
    # engine_capabilities 角色守卫（派发侧 _filter_by_role + 认领侧 #658 守卫）拦截。
    if engine_ids:
        # 多引擎选择：查引擎详情，按优先级选健康引擎
        members = dedupe_engines(_engines_by_ids(engine_ids, repo, cfg))
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
                    _rid = min(cand, key=lambda r: (r["priority"], r["runtime_id"]))["runtime_id"]
                else:
                    _rid = random.choice(cand)["runtime_id"]
                _pick_decision("selected", selected=_rid,
                               reason=f"scope={scope},rule={cfg.get('rule')}", members=members)
                return _rid
            # 所有档位均无健康引擎 → 降级兜底：取所有候选中健康分最高的
            all_cand = _apply_filters(members, cfg)
            if all_cand:
                _best = _best_health_fallback(all_cand)
                if _best:
                    log.info(f"pick_engine: all scopes unhealthy, fallback to best={_best}")
                    _pick_decision("selected", selected=_best,
                                   reason="health_fallback", members=members)
                    return _best
            _pick_decision("engine_unavailable", reason="all_scopes_unhealthy", members=members)
            return None     # 集合存在但无健康成员 → engine_unavailable
    # engine_runtime_ids 为空 → 回退会话主引擎（legacy）
    if fallback_runtime_id:
        _pick_decision("fallback", selected=int(fallback_runtime_id),
                       reason="fallback_runtime_id")
        return int(fallback_runtime_id)
    return None


def get_scene_pool_engine_ids(scene_id: str, agent_type: str, repo_key: str = "", user_id: int = 0,
                              issue_creator: str = "") -> str | None:
    """获取限定广播引擎列表，返回 JSON 字符串供 agent_tasks.pool_engine_ids 使用。

    只从场景 node 的 engine_runtime_ids 读取，并按健康过滤（Issue #259）：
    离线（status!=online）或心跳超时（last_seen_at 距今 > _RUNTIME_STALE_SECONDS，默认 100s）
    的引擎不写入 pool。

    Issue #3396：删除 SRE 禁广播特例（原 sre-repo-worker-relax §1.1 只放行
    user/repo 两档、空 = 全灭）——SRE 与其他角色同口径：未配置节点 → 全量广播，
    越权机器由 engine_capabilities 角色守卫在派发侧（_filter_by_role）与认领侧
    （service.claim_by_tag #658 守卫）拦截。

    Issue #653（空值语义单点收口 + 严格模式开关，默认严格）/ Issue #2791 回归复核：
    通过 get_scene_engine_binding 区分节点绑定四态（no_scene/no_node/unbound/bound）：
    - bound（已配置）：候选解析为空（引擎行不存在/被删）或全部不健康 → None（显式报错，
      绝不回退全局池）；strict_scene_engines=False 时回退 ""（广播）但打告警日志。
    - unbound / no_node（未配置）：""（全量广播，存量行为不变）。
    - 场景查询异常：严格模式 fail-closed → None；关闭时 ""（广播）+ 告警。

    返回值（三方语义，调用方必须用 is None 显式区分 None 与 ""）：
    - JSON 字符串：场景配了引擎且有健康的 → 限定名单（仅健康引擎）
    - ""：场景没配引擎 / node 不存在（原行为不变）
    - None：场景配了引擎但全部不健康 / 解析为空 / 查询异常（严格模式）→ 全灭，
      调用方不入队并明确报错（Issue #259 方案A / #653）
    """
    import json
    if not user_id:
        user_id = _repo_user_id(repo_key, issue_creator=issue_creator)
    if scene_id:
        # Issue #541：仓内存在性兜底——scene_id 在该仓库查不到（如误绑模板场景名）
        # 时不静默回退全局模板行（仅告警留痕）。
        if repo_key:
            try:
                from src.api.pipeline_config import scene_exists_in_repo
                if not scene_exists_in_repo(scene_id, repo_key):
                    log.warning(f"get_scene_pool_engine_ids: scene={scene_id} not found in repo "
                                f"{repo_key}, skip global template fallback for dispatch (Issue #541)")
            except Exception as _e541:
                log.warning(f"get_scene_pool_engine_ids: repo existence check failed: {_e541}")
        # Issue #653：严格模式开关（engine-selection.json strict_scene_engines，默认 True）
        _strict_653 = bool(load_engine_selection().get("strict_scene_engines", True))
        try:
            from src.api.pipeline_config import get_scene_engine_binding
            _binding_653 = get_scene_engine_binding(scene_id, agent_type, repo_key=repo_key, user_id=user_id)
            ids = (_binding_653 or {}).get("engine_ids") or []
            # Issue #1208（D11/D12）：uid 池路径——uids 非空优先，按 uid 解析候选；
            # 存量 id 节点 uids 恒空 → 走 ids 原路径（行为零变化）
            _bound_uids = (_binding_653 or {}).get("engine_uids") or []
        except Exception as e:
            # 场景查询异常：严格模式 fail-closed（宁失败不跑飞，#253 语义）；关闭时维持广播
            log.warning(f"get_scene_pool_engine_ids: scene query failed: {e}")
            if _strict_653:
                _decision_log("pool_engine_ids", repo_key=repo_key, agent_type=agent_type,
                              scene_id=scene_id, bound_engine_ids=[], healthy_ids=[],
                              result="query_failed_strict")
                return None
            return ""
        if _bound_uids:
            # uid 池路径（#646 轨语义同款）：uid 解析 + 健康/角色同一把尺子（Issue #3396 去 SRE 特例）
            members = dedupe_engines(_engines_by_uids(_bound_uids, _norm_repo(repo_key)))
            _decision_log("pool_engine_uids", repo_key=repo_key, agent_type=agent_type,
                          scene_id=scene_id, bound_engine_uids=_bound_uids,
                          healthy_ids=[m["runtime_id"] for m in members if _health_ok(m)],
                          result="uid_pool_resolved")
            if not members:
                if not _strict_653:
                    log.warning(f"get_scene_pool_engine_ids: scene={scene_id} agent={agent_type} "
                                f"bound uids {_bound_uids} missing in agent_runtimes, fallback to "
                                f"broadcast (strict_scene_engines=off, Issue #653)")
                    return ""
                _decision_log("pool_engine_uids", repo_key=repo_key, agent_type=agent_type,
                              scene_id=scene_id, bound_engine_uids=_bound_uids, healthy_ids=[],
                              result="bound_engines_missing")
                return None
            if members:
                _pre_role = list(members)
                members = _filter_by_role(members, agent_type)
                if not members:
                    _log_role_no_candidates("pool_engine_uids", repo_key, agent_type, scene_id, _pre_role)
                    return None
            # Issue #3928 R2：池可达性叠加后端健康态（与 claim_allowed 同尺）
            _claimable = _backend_claimable(members)
            if _claimable is not None:
                members = [m for m in members if not _backend_blocked(m, _claimable)]
            healthy_ids = [m["runtime_id"] for m in members if _health_ok(m)]
            if healthy_ids:
                return json.dumps(healthy_ids)
            _decision_log("pool_engine_uids", repo_key=repo_key, agent_type=agent_type,
                          scene_id=scene_id, bound_engine_uids=_bound_uids, healthy_ids=[],
                          result="all_offline")
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
            # Issue #658/#3396：角色硬过滤（候选筛选最前置，先于健康过滤）——
            # agent_type ∈ engine_capabilities（单一事实源，UI 勾选即生效，空标签
            # fail-closed）；无候选返回 None（调用方明确报错，不静默降级/广播给
            # 不匹配引擎）
            _pre_role = list(members)
            members = _filter_by_role(members, agent_type)
            if not members:
                _log_role_no_candidates("pool_engine_ids", repo_key, agent_type, scene_id, _pre_role)
                return None
            # Issue #605：广播名单过滤留痕——每个被过滤引擎的 rid/name 与原因（stale 心跳/离线等）
            # Issue #3928 R2：后端健康态（degraded/unavailable 的冷却期）与认领闸门同尺，
            # 池里只剩被熔断引擎时按 all_offline 诚实失败，不再放进去空转到 stale 判死。
            _claimable = _backend_claimable(members)
            _dropped = []
            for m in members:
                if _backend_blocked(m, _claimable):
                    _dropped.append({"rid": m.get("runtime_id"), "name": m.get("name") or "",
                                     "reason": "backend_unavailable"})
                    continue
                if not _health_ok(m):
                    if (m.get("status") or "") != "online":
                        _why = f"status={m.get('status') or 'offline'}"
                    else:
                        _why = f"stale_heartbeat>{_RUNTIME_STALE_SECONDS}s"
                    _dropped.append({"rid": m.get("runtime_id"), "name": m.get("name") or "", "reason": _why})
            healthy_ids = [
                m["runtime_id"] for m in members
                if not _backend_blocked(m, _claimable) and _health_ok(m)
            ]
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

def _pick_local_engine(user_id: int = 0, agent_type: str = "") -> int | None:
    """只挑「本机直连」档的健康引擎（role 过滤 + 同主机限定 + 用户稳定取模）。

    从 resolve_local_engine 抽出（2026-11-20），供 chat 四档兜底复用：
    resolve_local_engine 语义不变（local → global），chat 走 resolve_chat_engine
    （local → user → repo → global，用户裁定）。
    """
    cand = query_engines("", "local")
    cand = _apply_filters([dict(m, scope="local") for m in cand], load_engine_selection())
    if agent_type:
        cand = [m for m in cand if engine_allows_role(m.get("runtime_id") or 0, agent_type)]
    if user_id:
        # 方案 A：只挑当前主控机自己的直连引擎（别的 master 的行留显示，不留执行）
        try:
            _hn = engine_host()
            _own = [m for m in cand if (m.get("host_name") or "") == _hn]
            if _own:
                cand = _own
        except Exception:
            pass
    if not cand:
        return None
    if user_id:
        # 稳定取模：同一用户恒定命中同一引擎（会话连续），用户间均匀散开
        _seed_src = str(user_id)
        try:
            _seed = int(_seed_src)
        except ValueError:
            import hashlib as _hl
            _seed = int(_hl.md5(_seed_src.encode("utf-8")).hexdigest()[:12], 16)
        _ids = sorted(r["runtime_id"] for r in cand)
        return _ids[_seed % len(_ids)]
    return min(cand, key=lambda r: r["runtime_id"])["runtime_id"]


def resolve_chat_engine(user_id: int = 0, username: str = "", repo_key: str = "",
                        agent_type: str = "chat") -> int | None:
    """Chat 未显式绑定时的默认引擎：**本机 → 个人 → 仓库 → 全局**（用户 2026-11-20 裁定）。

    覆盖 engine-selection-design.md §8.2 的「local → global」两档：用户的私有引擎与
    当前仓库引擎现在也是合法兜底（此前会被跳过，直接落到排队更慢的 global worker）。
    每一档都必须过角色过滤（engine_capabilities 含 agent_type 或 all；空标签 fail-closed），
    不符合的档位视为无候选、继续往后找；全都没有返回 None（调用方给明确提示）。
    """
    at = (agent_type or "chat").strip().lower()
    try:
        rid = _pick_local_engine(user_id, agent_type=at)
    except Exception:  # noqa: BLE001 - 本机档取数失败不阻断后续档位
        rid = None
    if rid:
        return rid
    plans = (
        ("user", "", {"username": username, "user_id": user_id}),
        # repo 档同样带 username：空 repo_key 时靠它把候选收紧到"用户已加入的仓库"，
        # 否则只有在明确指定 repo_key 时才有候选（别人仓库的引擎永远不可用）。
        ("repo", repo_key, {"username": username}),
        ("global", "", {}),
    )
    for scope, rk, extra in plans:
        try:
            cand = query_engines(rk, scope, agent_type=at, **extra)
            cand = _apply_filters([dict(m, scope=scope) for m in cand], load_engine_selection())
            cand = [m for m in cand if engine_allows_role(m.get("runtime_id") or 0, at)]
        except Exception:  # noqa: BLE001 - 单档异常继续下一档
            continue
        if cand:
            return min(cand, key=lambda r: r["runtime_id"])["runtime_id"]
    return None


def resolve_local_engine(user_id: int = 0, agent_type: str = "") -> int | None:
    """返回一个健康的本机直连引擎（credential_id=0 且 installed+healthy）；无则 None。

    用于 chat 等未显式绑定时的本机兜底（engine-selection-design.md §8.2）。
    无本机直连时回退全局健康引擎（credential_id>0 的远端 worker，chat 走队列执行），
    避免"无可用引擎"硬失败（#60：清理设备后无 cred0 引擎，chat 全挂）。

    Issue #3396：agent_type 非空时（chat 调用面统一传 "chat"），本机直连与
    global 回退候选均须过角色过滤（engine_capabilities 勾了 agent_type 或 all，
    空标签 fail-closed）；无合规候选返回 None（调用方给出"无可用引擎"明确提示，
    不静默调度到越出可选面的引擎），决策日志 role_no_candidates 留痕。

    user_id 非空且非哨兵（chat 场景，per-user-random-default-engine 设计；D105 后为 uuid
    str）：候选限定**当前这台
    主控机**的直连引擎（host_name=本机 hostname，ELB 分流下请求落哪台就直连哪台，
    跨主机的直连行无法本机拉进程），再按 user_id 稳定取模挑选——同一用户在同一台
    机器上恒定同一引擎，不同用户自然散开，避免全员滑向同一个默认引擎（此前取
    MIN(id) 造成单引擎压力集中）。
    user_id=0（IM 等无用户上下文兜底）：维持旧行为取最小 id。

    结果缓存 30 秒——仅 user_id=0 且未指定 agent_type 的旧语义调用使用；
    带角色过滤的结果不缓存（改标签后 30s 内不得仍落不合规引擎）。
    """
    now = _time.time()
    _ck = _resolve_local_cache.get("expires", 0)
    if _ck > now and not user_id and not agent_type:
        return _resolve_local_cache.get("result")
    # 本机档：抽到 _pick_local_engine（与 chat 四档兜底共用一份实现，避免规则漂移）
    _local_rid = _pick_local_engine(user_id, agent_type=agent_type)
    if _local_rid:
        return int(_local_rid)
    # 回退：任意健康全局引擎（chat 未配置时可用远端 worker 引擎入队执行）；
    # Issue #3396：global 回退同样必须过 agent_type 角色过滤
    cand = query_engines("", "global")
    cand = _apply_filters([dict(m, scope="global") for m in cand], load_engine_selection())
    if agent_type:
        _role_trace = list(cand)
        cand = [m for m in cand if engine_allows_role(m.get("runtime_id") or 0, agent_type)]
    if not cand:
        if agent_type and _role_trace:
            # A6：存在健康候选但均未勾角色（chat/all）→ 显式留痕，绝不静默放行
            _log_role_no_candidates("resolve_local_engine", "", agent_type, "", _role_trace)
        return None
    if user_id:
        # 稳定取模：同一用户恒定命中同一引擎（会话连续），用户间均匀散开
        # D105：user_id 为 uuid str（调用方 user.get("id")），int() 直接 ValueError →
        # 非数字串降级为稳定 md5 派生数值（同输入恒同结果，保持取模语义）
        _seed_src = str(user_id)
        try:
            _seed = int(_seed_src)
        except ValueError:
            import hashlib as _hl
            _seed = int(_hl.md5(_seed_src.encode("utf-8")).hexdigest()[:12], 16)
        _ids = sorted(r["runtime_id"] for r in cand)
        result = _ids[_seed % len(_ids)]
    else:
        result = min(cand, key=lambda r: (r["runtime_id"]))["runtime_id"]
    if not user_id and not agent_type:
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

def query_engines(repo_key: str, scope: str, agent_type: str = "", username: str = "",
                  user_id: int | str = 0) -> list[dict]:
    """返回某仓库在某档位的候选引擎：repo=仓库级；global=远端全局；local=本机直连。
    agent_type 非空时，只返回标签匹配的引擎（tags 包含 agent_type 或 "all"）。
    user scope：user_id 非空时**按 owner_id 精确过滤**（别人名下的个人引擎绝不返回）；
    owner_id 为空的存量行才用 username 兜底匹配。只给 username 时维持旧行为。
    repo scope 只返回当前仓库的仓库级引擎（按 repo_key 过滤 owner_id）；
    空 repo_key 时按当前用户加入的仓库收紧，无 username 则空集（别人仓库的引擎不可用）。"""
    repo = _norm_repo(repo_key)
    db, lock = _db()
    # 收集用户加入的仓库列表（用于 repo scope 无 repo_key 时的回退过滤）
    user_repos = set()
    if username and scope == "repo" and not repo:
        try:
            from src.kernel.repo_key import from_repo_key
            from src.domains.mgmt import public as _mgmt_public_ur
            # 成员关系 + 账号名都在 mgmt 域内：一次具名查询，本域不拆 JOIN
            for _repo_key in _mgmt_public_ur.user_repo_keys_by_username(username):
                user_repos.add(from_repo_key(_repo_key or ""))
        except Exception as _e:
            log.warning(f"query_engines: get_user_repos failed: {_e}")
    with lock:
        c = db.cursor()
        try:
            if scope == "repo":
                # 按当前仓库过滤：owner_id 可能带平台前缀也可能不带——候选集由
                # repo_key_in_params 原子产物派生（S100/D75：占位符与参数同源构造，
                # arity 由构造保证；候选面 = 全部平台卡前缀，与 knowledge.py 同口径）
                if repo:
                    _ph, _args = repo_key_in_params(repo)
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                        " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0), "
                        " '' AS host_name, COALESCE(LOWER(r.engine_uid),''), COALESCE(r.role_tags,'')"
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='repo' AND rc.status='active' "
                        f"AND rc.owner_id IN ({_ph}) ORDER BY r.id",
                        _args)
                else:
                    # 2026-11-20 用户裁定：「别人仓库的也不能用」。
                    # 空 repo_key 时**不再**返回全部仓库级引擎（那会把别人仓库的执行资源
                    # 当成本仓库候选，兜底/计费都落到别人头上）。只按当前用户加入的仓库收紧；
                    # 连 username 都没有 → 空集（fail-closed），宁可不给候选也不越界。
                    from src.kernel.repo_key import repo_key_candidates
                    _repo_cands: list[str] = []
                    for _r in sorted(user_repos):
                        _repo_cands.extend(repo_key_candidates(_r))
                    if not _repo_cands:
                        c.execute(
                            "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                            " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                            " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                            " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0), "
                            " '' AS host_name, COALESCE(LOWER(r.engine_uid),''), COALESCE(r.role_tags,'')"
                            "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                            "WHERE 1=0")
                    else:
                        _ph2 = ",".join("?" * len(_repo_cands))
                        c.execute(
                            "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                            " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                            " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                            " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0), "
                            " '' AS host_name, COALESCE(LOWER(r.engine_uid),''), COALESCE(r.role_tags,'')"
                            "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                            "WHERE r.credential_id>0 AND rc.scope='repo' AND rc.status='active' "
                            f"AND rc.owner_id IN ({_ph2}) ORDER BY r.id", tuple(_repo_cands))
            elif scope == "local":
                # 本机引擎（credential_id=0）对所有人可见；host_name 区分多台主控机
                # （per-user-random-default-engine 方案 A 靠它过滤"当前这台主控机"）
                c.execute(
                    "SELECT id, name, status, healthy, installed, credential_id, last_seen_at, '', '', '',"
                    " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=agent_runtimes.id AND enabled=1),"
                    " COALESCE(alias,''), COALESCE(scene_tags,''), COALESCE(org_id,0), COALESCE(host_name,''), "
                    " COALESCE(LOWER(engine_uid),''), COALESCE(role_tags,'')"
                    "FROM agent_runtimes WHERE credential_id=0 ORDER BY id")
            elif scope == "user":
                _uid_s = str(user_id or "").strip()
                if _uid_s:
                    # 2026-11-20 用户裁定：个人档必须按 owner_id 精确过滤（个人引擎属私有设备，
                    # 绝不能把别人的机器当着"个人档"返回）。存量行 owner_id 为空时用 owner_name 兜底。
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                        " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0), "
                        " '' AS host_name, COALESCE(LOWER(r.engine_uid),''), COALESCE(r.role_tags,'')"
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='user' AND rc.status='active' "
                        "AND (rc.owner_id=? OR (COALESCE(rc.owner_id,'')='' AND rc.owner_name=?)) ORDER BY r.id",
                        (_uid_s, username or ""))
                elif username:
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                        " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0), "
                        " '' AS host_name, COALESCE(LOWER(r.engine_uid),''), COALESCE(r.role_tags,'')"
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='user' AND rc.status='active' AND rc.owner_name=? ORDER BY r.id", (username,))
                else:
                    c.execute(
                        "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                        " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                        " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                        " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0), "
                        " '' AS host_name, COALESCE(LOWER(r.engine_uid),''), COALESCE(r.role_tags,'')"
                        "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                        "WHERE r.credential_id>0 AND rc.scope='user' AND rc.status='active' ORDER BY r.id")
            else:
                c.execute(
                    "SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at, "
                    " COALESCE(rc.device_name,''), COALESCE(rc.owner_name,''), COALESCE(rc.owner_id,''),"
                    " (SELECT GROUP_CONCAT(tag) FROM engine_capabilities WHERE runtime_id=r.id AND enabled=1),"
                    " COALESCE(r.alias,''), COALESCE(r.scene_tags,''), COALESCE(r.org_id,0), "
                        " '' AS host_name, COALESCE(LOWER(r.engine_uid),''), COALESCE(r.role_tags,'')"
                    "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                    "WHERE r.credential_id>0 AND rc.scope='global' AND rc.status='active' ORDER BY r.id")
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"query_engines({scope}) failed: {e}")
            return []
    out = []
    for r in rows:
        if scope == "repo":
            from src.kernel.repo_key import from_repo_key
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
            # Issue #1208：uid（前端 uid 输出/uid 回显预勾）+ role_tags（命中预览角色分层）
            "engine_uid": (r[15] or "") if len(r) > 15 else "",
            "role_tags_raw": (r[16] or "") if len(r) > 16 else "",
        })
    return out

# 引擎离线判定为真死、触发场景绑定清理的阈值（秒）。默认 0 = 禁用自动解绑（只提示不清理）；
# 需要自动清理时显式设置 ENGINE_DETACH_AFTER_SECONDS（如 28800 = 8h：心跳 15s，8h 无恢复
# = 连续 ~1920 个心跳周期全丢，机器不会自己回来）。
_ENGINE_DETACH_AFTER_SECONDS = int(os.environ.get("ENGINE_DETACH_AFTER_SECONDS", "0"))

def cleanup_scene_engine_bindings(rid_list: list, commit: bool = True) -> int:
    """从所有活跃场景的 nodes_json 中移除对 rid_list 引擎的绑定（Issue #250 原逻辑抽出共享）。

    返回被修改的场景数。供两处调用：
    - settings.py 删除 runtime 时（原 _cleanup_scene_engine_runtime_ids，行为等价）
    - _detach_dead_engine_bindings 离线超阈值解绑（引擎绑定真实性同步）
    """
    if not rid_list:
        return 0
    import json as _json
    from datetime import datetime, timezone
    from src.kernel.session_manager import db, db_lock
    rid_set = set(rid_list)
    changed_rows = []
    with db_lock:
        c = db.cursor()
        try:
            from src.domains.mgmt import public as _mgmt_scene_cl
            # 场景表归 mgmt：任意拓扑具名查询，列序 (id, nodes_json) 与原查询一致
            rows = _mgmt_scene_cl.list_active_scene_nodes()
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
                    # P3-4b：场景节点写走 mgmt lib（幂等意图 + expected_hash 防并发覆盖）
                    from src.domains.runtime.engine.scene_nodes_write import (
                        write_scene_nodes as _wsn_cl, content_hash as _ch_cl)
                    _rc_cl = _wsn_cl(_json.dumps(nodes, ensure_ascii=False), row_id,
                                     _ch_cl(nodes_str or ""), c, now, log)
                    if _rc_cl in ("updated", "local", "skipped"):
                        changed_rows.append(row_id)
                    elif _rc_cl == "mismatch":
                        log.warning(f"cleanup_scene_engine_bindings: scene {row_id} "
                                    f"changed concurrently, transform skipped")
                    else:
                        log.warning(f"cleanup_scene_engine_bindings: scene {row_id} "
                                    f"lib write failed, intent queued for retry")
                except Exception as _e:
                    log.warning(f"cleanup_scene_engine_bindings: update scene {row_id} failed: {_e}")
        if commit:
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
    from src.kernel.session_manager import db, db_lock
    _own_tx = c is None
    if _own_tx:
        # db_lock 可重入，with 只是保证互斥
        with db_lock:
            c = db.cursor()
            return _merge_duplicate_engine_rows_locked(credential_id, c, _own_tx)
    return _merge_duplicate_engine_rows_locked(credential_id, c, _own_tx)


def _find_scene_bindings_for_runtime(c, runtime_id: int) -> list:
    """查所有活跃场景中绑定了 runtime_id 的 (row_id, repo_key, scene_id, node_tag, before_ids)。

    供 detach 审计用：先记录被移除前的绑定事实，再执行清理。
    """
    import json as _json
    hits = []
    try:
        from src.domains.mgmt import public as _mgmt_scene_find
        # 场景表归 mgmt：任意拓扑具名查询，列序 (id, repo_key, scene_id, nodes_json)
        rows = _mgmt_scene_find.list_active_scene_bindings()
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
        from src.domains.mgmt import public as _mgmt_apb
        # 任务表归 mgmt：任意拓扑具名查询，列序 (id, name, engine_runtime_ids)
        rows = _mgmt_apb.list_enabled_autopilot_engine_ids()
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


def _engine_uid_by_rid(c, runtime_id) -> str:
    """Issue #868 前端 uid 消费：runtime_id → engine_uid（旧库缺列/行不存在降级 ''）。"""
    try:
        row = c.execute("SELECT COALESCE(engine_uid,'') FROM agent_runtimes WHERE id=?",
                        (int(runtime_id or 0),)).fetchone()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def get_detached_engine_hints(scene_row_id: int = 0, runtime_ids: list | None = None,
                              repo_key: str = "") -> list:
    """查询场景/引擎的被解绑提示信息（引擎绑定真实性同步 §4.3/§5.1b 前端数据源）。

    - scene_row_id 非空：查该场景的未重绑（rebound_at 空）解绑记录 → detached_engines
    - runtime_ids 非空：查这些引擎的未重绑解绑记录 → autopilot 任务 engine_detached 提示。
      卡点④修复：必须传 repo_key（任务所属仓库）——引擎审计是全局的，任务提示只关心
      「本仓库场景」的解绑记录，否则其他仓库同引擎的审计会误报到无关任务上。
    返回 [{scene_row_id, scene_id, node_tag, runtime_id, runtime_name, detached_at, affected_autopilot_tasks}]
    """
    from src.kernel.session_manager import db, db_lock
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
                    # Issue #868 前端 uid 消费：稳定键 uid 优先、id 兜底（旧库缺列降级 ''）
                    "engine_uid": _engine_uid_by_rid(c, r[3]),
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
    from src.domains.runtime.engine.health import parse_dt
    from src.kernel.session_manager import db, db_lock

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

# ── Issue #605：候选过滤原因（由 weekly-10 迁入，只读解释，不改选择行为） ──
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
            from src.domains.mgmt.public import is_known_engine_name
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
            # Issue #3396：角色硬过滤口径已与标签口径同源（engine_capabilities
            # 单一事实源，_filter_by_role/_filter_by_tags 同一把尺子），不再单列
            # role_mismatch——本 reason 即角色不匹配的取证口径。
            reasons.append(f"tag_mismatch({','.join(tags) or 'none'})")
    if rid:
        try:
            from src.domains.runtime.engine.health_scorer import is_healthy as _score_ok
            if not _score_ok(rid):
                reasons.append("health_score_below_threshold")
        except Exception:
            pass
    return reasons

# ── Issue #605：引擎派发解释（由 weekly-10 迁入） ──
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
        from src.domains.runtime.pipeline.event_pipeline import is_plugin_mode
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

from src.kernel.repo_key import repo_key_in_params

def _decision_log(event: str, **fields) -> None:
    """engine_select 决策结构化日志（Issue #605）：单行 JSON，便于 grep/采集。"""
    try:
        payload = {"event": event}
        payload.update(fields)
        log.info("engine_select_decision " + json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        pass

def _merge_duplicate_engine_rows_locked(credential_id: int, c, commit: bool) -> dict:
    rid_map = {}
    try:
        rows = c.execute(
            "SELECT id, lower(name), tags_user_set FROM agent_runtimes WHERE credential_id=?",
            (credential_id,)).fetchall()
    except Exception as _e:
        log.warning(f"merge_duplicate_engine_rows: query failed: {_e}")
        return rid_map
    # Issue #826（外部审查修复）：uid 轨道就绪时，capabilities 迁移须同步 engine_uid
    # 指向存活行（否则 dup 行 uid 悬挂；MySQL FK RESTRICT 会拦截删除 → 注册静默失败）。
    _uid_ready = False
    try:
        # 列探测实现归 runtime 域（refs_reconcile），同域直import，不再绕 mgmt.user.db
        from src.domains.runtime.engine.refs_reconcile import columns_exist as _columns_exist
        _uid_ready = (_columns_exist(c, "agent_runtimes", ("engine_uid",))
                      and _columns_exist(c, "engine_capabilities", ("engine_uid",)))
    except Exception:
        _uid_ready = False
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
                        if _uid_ready:
                            # capabilities 迁移顺带同步 engine_uid 镜像 → 存活行 uid
                            # （存活行 uid 为 NULL 时置 NULL，与 runtime_id 轨道口径一致，
                            # 后续注册回填（engine_uid IS NULL）可再补齐；子查询取存活行
                            # uid，MySQL 更新 engine_capabilities / 子查 agent_runtimes 无同表限制）
                            c.execute(
                                "UPDATE engine_capabilities SET runtime_id=?, "
                                "engine_uid=(SELECT engine_uid FROM agent_runtimes WHERE id=?), "
                                "updated_at=CURRENT_TIMESTAMP WHERE runtime_id=?",
                                (surviving, surviving, rid))
                        else:
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
    from src.kernel.session_manager import db, db_lock
    changed_rows = []
    with db_lock:
        c = db.cursor()
        try:
            from src.domains.mgmt import public as _mgmt_scene_rm
            # 场景表归 mgmt：任意拓扑具名查询，列序 (id, nodes_json) 与原查询一致
            rows = _mgmt_scene_rm.list_active_scene_nodes()
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
                    # P3-4b：场景节点写走 mgmt lib（幂等意图 + expected_hash 防并发覆盖）
                    from src.domains.runtime.engine.scene_nodes_write import (
                        write_scene_nodes as _wsn_rm, content_hash as _ch_rm)
                    _rc_rm = _wsn_rm(_json.dumps(nodes, ensure_ascii=False), row_id,
                                     _ch_rm(nodes_str or ""), c, now, log)
                    if _rc_rm in ("updated", "local", "skipped"):
                        changed_rows.append(row_id)
                    elif _rc_rm == "mismatch":
                        log.warning(f"remap_scene_engine_bindings: scene {row_id} "
                                    f"changed concurrently, transform skipped")
                    else:
                        log.warning(f"remap_scene_engine_bindings: scene {row_id} "
                                    f"lib write failed, intent queued for retry")
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
    from src.kernel.session_manager import db, db_lock
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
            from src.domains.mgmt import public as _mg_at_pub
            from src.kernel.domain_read import mgmt_lib as _mgmt_lib_at
            _mg_at = _mgmt_lib_at(foreign_only=True)
            for tid, eids_str in _mg_at_pub.list_autopilot_engine_ids_nonempty():
                try:
                    eids = _json.loads(eids_str) if eids_str else []
                except Exception:
                    continue
                if not isinstance(eids, list):
                    continue
                new_eids = [rid_map.get(eid, eid) for eid in eids]
                if new_eids != eids:
                    _new_json = _json.dumps(new_eids)
                    if _mg_at is not None:
                        # P3-5d：mgmt 基表，跨域写走 lib；lib 不可用时回落 owner 域原语
                        # （单库/测试拓扑由 owner 自己路由连接，本域不再持 SQL）
                        try:
                            import hashlib as _hl_at
                            from src.kernel import cross_write as _xw_at
                            _sig_at = _hl_at.sha1(_new_json.encode("utf-8")).hexdigest()[:12]
                            _res_at = _xw_at.submit(
                                "mgmt", "autopilot_engine_ids_set",
                                _xw_at.unique_key(f"autopilot-eids:{tid}:{_sig_at}"),
                                {"task_id": int(tid), "engine_runtime_ids": _new_json},
                                lambda t=tid, j=_new_json: _mg_at.set_autopilot_engine_ids(int(t), j))
                            if _res_at.get("status") == "failed":
                                log.warning(f"remap_engine_references: autopilot task {tid} "
                                            f"queued for retry: {_res_at.get('error')}")
                            else:
                                stats["autopilot_tasks"] += 1
                        except Exception as _e_at:
                            log.warning(f"remap_engine_references: autopilot task {tid} lib path "
                                        f"failed, fallback to owner primitive: {_e_at}")
                            _mg_at_pub.set_autopilot_engine_ids_any(int(tid), _new_json)
                            stats["autopilot_tasks"] += 1
                    else:
                        # 单库/测试拓扑：owner 域任意拓扑写原语（同一份 SQL，自提交）
                        _mg_at_pub.set_autopilot_engine_ids_any(int(tid), _new_json)
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
