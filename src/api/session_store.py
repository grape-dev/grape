"""Agent Session 管理 — per (repo_key, agent_type) 独立会话"""
import uuid, json
from datetime import datetime
from src.kernel.session_manager import db, db_lock  # noqa: F401  (db_lock 供既有调用面保留)
from src.domains.runtime import public as rt
from src.domains.mgmt import public as mgmt
from src.kernel.config import log
from src.api.time_utils import normalize_event_time


def get_agent_type_enable(agent_type: str) -> int:
    """读 agent_type.enable（全局默认，sre 默认关）。无记录返回 1（默认开）。"""
    try:
        row = rt.read_one_any("agent_type_enable", (agent_type,))
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    return 1


def _session_row_read(repo_key: str, agent_type: str):
    """Issue #868 (T10)：会话行读取——uid 版查询优先，旧库缺列降级 legacy 版（行尾补空 uid）。"""
    try:
        return rt.read_one_any("devops_agent_session_row", (repo_key, agent_type))
    except Exception:
        try:
            row = rt.read_one_any("devops_agent_session_row_legacy", (repo_key, agent_type))
            return tuple(row) + ("",) if row is not None else None
        except Exception:
            return None


def is_agent_enabled(repo_key: str, agent_type: str) -> bool:
    """仓库级 Agent 是否启用：优先 devops_agent_session.enable，无会话回退 agent_type.enable。

    见 agent-enable-toggle-design.md §五 拦截语义。
    """
    try:
        row = _session_row_read(repo_key, agent_type)
        if row and row[4] is not None:
            return bool(int(row[4]))
    except Exception:
        pass
    return bool(get_agent_type_enable(agent_type))


# ===== Issue 生命周期状态机（issue_state，见 vod-skill-upgrade-plan.md §3.0.2）=====

def get_issue_stage(repo_key: str, issue_id: str) -> dict | None:
    """读 Issue 当前阶段。无记录返回 None。

    Phase C3：`issue` 单表为权威（已收编 + 停写 issue_state），直接读 issue 卡片行；
    `issue_state` 仅作停写过渡期的兜底（存量未清理前的兼容）。
    """
    # 1. issue 单表（权威）
    _num = _issue_num_or_ident(issue_id)
    if _num:
        # 走 provider 通道（GSB）：同域 → Local 实例直接查；跨域且 GRAPE_PROVIDER_MODE=remote
        # → Remote 实例打 /internal/issues/.../card（一次 HTTP hop，同机实测 +0.3~0.8ms）。
        # 任何异常都回落到下面的 *_any 直连，迁移不引入新的失败模式。
        try:
            from src.kernel.gsb.provider import get_service as _get_service
            _card = _get_service("runtime").get_issue_card(repo_key, _num)
            if _card:
                return _card
        except Exception as _ge:  # noqa: BLE001
            # 模块顶部已 import log，直接记；不要再套一层 try/except（那会多出一个静默 except，
            # 棘轮会按口径漂移拦下）
            log.debug(f"session_store.get_issue_stage: provider failed, fallback to _any: {_ge}")
        try:
            row = rt.read_one_any("issue_card_row", (repo_key, _num))
            if row:
                return {"id": None, "stage": row[0], "owner_agent": row[1],
                        "a2a_seq": row[2], "updated_at": row[3], "assignee": row[4] or "",
                        "scene_id": row[5] or "",
                        "scene_user_id": row[6] or 0,
                        "parent_issue_id": row[7] or "",
                        "is_subtask_mode": row[8] or 0,
                        "allow_scene_override": row[9] or 0}
        except Exception:
            pass
    # 2. issue_state 兜底（过渡期兼容）
    try:
        row = rt.read_one_any("issue_state_legacy_row", (repo_key, issue_id))
        if row:
            return {"id": row[0], "stage": row[1], "owner_agent": row[2],
                    "a2a_seq": row[3], "updated_at": row[4], "assignee": row[5] or "",
                    "scene_id": row[6] or "",
                    "scene_user_id": row[7] or 0,
                    "parent_issue_id": row[8] or "",
                    "is_subtask_mode": row[9] or 0,
                    "allow_scene_override": row[10] or 0}
    except Exception:
        pass
    return None


def _sync_issue_fields(repo_key: str, issue_id: str, fields: dict, touch_updated_at: bool = True,
                       ensure_row: bool = False, assignee: str = ""):
    """Phase B2.5/C3：将收编字段写回 `issue` 卡片行（按 issue_number）。

    - ensure_row=True：issue 无卡片行时**建档最小行**（status='active'，保证 ensure_issue_task 幂等复用；
      建行后按 fields 更新）。用于 stage/scene/parent 等写入路径，为 C3 停写 issue_state 铺路。
    - ensure_row=False：仅 UPDATE 已有行（默认，保持既有行为）。
    幂等：UPDATE 按 repo_key+issue_number，无行建档仅一次。
    """
    _num = _issue_num_or_ident(issue_id)
    if not _num:
        return
    if not fields:
        return
    try:
        if ensure_row:
            # 建档最小行（幂等：仅当无该卡片行时 INSERT；后再 UPDATE 收编字段）。
            # FR-17.1/FR-18.2: 按 repo_key provider 分查对应列，不 OR 扫两列。
            # assignee 为空 → user_id=0；非空 → 按 provider 查 gitcode_user / github_login。
            _assignee = assignee or (fields.get("assignee") or "")
            from src.kernel.repo_key import um_column_for_repo
            _col = um_column_for_repo(repo_key) if _assignee else "gitcode_user"
            rt.ensure_issue_card(repo_key, _num, _assignee, _col)
        rt.update_issue_card_fields(repo_key, _num, dict(fields),
                                    touch_updated_at=touch_updated_at)
    except Exception as e:
        from src.kernel.config import log as _log2
        _log2.warning(f"sync issue table failed for {repo_key} #{_num}: {e}")


def set_issue_stage(repo_key: str, issue_id: str, stage: str, owner_agent: str,
                    a2a_seq: int | None = None, assignee: str = ""):
    """写入/更新 Issue 阶段（幂等 upsert，兼容 SQLite/MySQL）。assignee 为 Issue 责任人（GitCode 用户名）。

    Phase C3：**只写 `issue` 单表**（权威），不再写 issue_state（已由 B2 收编 + 孤儿清理后数据对齐）。
    建档由 `_sync_issue_fields` 的 ensure_row 保证（无卡片行建最小行）。
    scene_id 初始为空，由 Task Center set_issue_scene 或智能推荐写入。
    """
    # Phase C3：写 issue 单表（无卡片行则建档最小行）
    # Issue #3646：assignee 只在**显式传入**时写——A2A 推进路径（`set_issue_stage(rk, iid, next, next)`）
    # 只带角色不带责任人，原实现 `assignee or ""` 会把真实责任人一次推进就抹成空串，
    # 流水线「当前责任人」随之退化为内部账号推断。
    _fields = {"stage": stage, "owner_agent": owner_agent, "a2a_seq": a2a_seq}
    if assignee:
        _fields["assignee"] = assignee
    _sync_issue_fields(repo_key, issue_id, _fields, ensure_row=True, assignee=assignee)
    from src.kernel.config import log
    log.info(f"IssueState: repo={repo_key} issue={issue_id} stage={stage} owner={owner_agent} assignee={assignee}")

    # Issue #85: stage 推进时同步 task.status（不覆盖 awaiting_input/completed/cancelled/pending_review）
    _STAGE_TO_TASK_STATUS = {
        "vod": "processing", "issue": "processing", "coding": "processing",
        "qa": "processing", "sre": "processing",
        "pending_review": "pending_review", "done": "completed",
    }
    _task_status = _STAGE_TO_TASK_STATUS.get(stage)
    if _task_status:
        try:
            _issue_num = _issue_num_or_ident(issue_id)
            rt.exec_any("issue_status_sync", (_task_status, repo_key, _issue_num))
        except Exception as _e:
            log.warning(f"set_issue_stage: sync task.status failed: {_e}")


def get_issue_owner(repo_key: str, issue_id: str) -> str:
    """返回 Issue 当前归属 agent（无记录默认 'vod'）。"""
    st = get_issue_stage(repo_key, issue_id)
    return st["owner_agent"] if st else "vod"


def get_issue_stage_name(repo_key: str, issue_id: str) -> str:
    """返回 Issue 当前阶段名（无记录默认 'vod'）。"""
    st = get_issue_stage(repo_key, issue_id)
    return st["stage"] if st else "vod"


def get_issue_assignee(repo_key: str, issue_id: str) -> str:
    """返回 Issue 责任人（GitCode assignee 用户名；无记录空）。"""
    st = get_issue_stage(repo_key, issue_id)
    return (st["assignee"] if st else "") or ""


def sync_issue_assignee(repo_key: str, issue_id: str, assignee: str) -> bool:
    """指派/更新事件把 VCS 当前 assignee 回填 Issue 卡片（Issue #3646）。

    历史实现只在 `issue_opened` 初始化 assignee，而 A2A 推进路径又不携带责任人，
    卡片 assignee 因此长期为空，流水线「当前责任人」只能靠 event actor 推断——
    内部 Agent 账号（`gcw_*`）就是这样被展示出来的。

    口径：
    - 只写 `assignee` 单字段（不动 stage / owner_agent / updated_at，也不动 user_id 归属）；
    - **仅空值回填**：已有非空责任人（人类或机器人接管值）一律不覆盖，人类责任人优先；
    - 空 assignee / 非法 issue 标识 → 不写，返回 False。
    """
    _asg = str(assignee or "").strip()
    _num = _issue_num_or_ident(issue_id)
    if not _asg or not _num:
        return False
    try:
        _st = get_issue_stage(repo_key, issue_id) or {}
        if (_st.get("assignee") or "").strip():
            return False
        # 返回值取真实写入行数：无卡片行时不假装回填成功（对账口径）。
        return bool(rt.update_issue_card_fields(repo_key, _num, {"assignee": _asg},
                                                touch_updated_at=False))
    except Exception as e:  # noqa: BLE001 - 回填是旁路，失败不阻断事件主流程
        from src.kernel.config import log as _log
        _log.warning(f"sync_issue_assignee failed for {repo_key} #{_num}: {e}")
        return False


def _scene_uid_or_zero(value):
    """scene_user_id 归一：D105 哨兵 ''/'0'/'none'/'null' 一律视为 0。

    MySQL 列是 varchar，历史行默认值是字符串 '0'——直接 `or 0` 会保留 truthy 的
    '0'，让调用方误以为已有归属用户，跳过 Issue 创建人回退。
    """
    raw = str(value or "").strip()
    return 0 if raw.lower() in ("", "0", "none", "null") else value


def get_issue_scene(repo_key: str, issue_id: str) -> tuple:
    """读取 Issue 级开发场景。issue_state.scene_id 为空时回退到 issue 表。

    返回 (scene_id, scene_user_id)：
    - get_issue_stage 返回 scene_id → 用该值 + scene_user_id
    - 否则从 issue 表回退读 scene_id/scene_user_id
    - 都没有 → 回退仓库默认场景（managed_repos.dev_scene，Issue #415）
    - 再没有 → ("", 0)，由调用方决定走智能推荐
    """
    # PR 订阅伪 issue_id（#pr_<PR号>_<scene_id>，event_pipeline L~1706 构造）：
    # issue 表查不到 → 原先回退 managed_repos.dev_scene（仓库默认场景），
    # 导致 PR 订阅任务的场景 custom_fields（如 BUILD0007_PASSWORD）不注入。
    # 场景 ID 就编码在伪 id 里，直接解析返回（2026-10-21）。
    import re as _re
    _m = _re.match(r"^#?pr_\d+_(.+)$", str(issue_id or ""))
    if _m:
        return _m.group(1), 0
    st = get_issue_stage(repo_key, issue_id)
    if st and st.get("scene_id"):
        return st["scene_id"], _scene_uid_or_zero(st.get("scene_user_id", 0))
    # 回退：从 issue 表直接读 scene_id（Phase C3 权威源）
    _num = _issue_num_or_ident(issue_id)
    if _num:
        try:
            r = rt.read_one_any("issue_card_row", (repo_key, _num))
            if r and r[5]:
                return r[5], _scene_uid_or_zero(r[6])
        except Exception:
            pass
    # Issue #415: Issue 级场景为空 → 回退仓库默认场景（managed_repos.dev_scene），
    # 与 _resolve_scene_id / repogit / prompt_builder 的仓库默认场景语义一致；
    # 仓库未配置默认场景时保持 ("", 0)，由调用方决定走智能推荐。
    try:
        from src.domains.mgmt.user.db import get_repo_scene
        _repo_scene = get_repo_scene(repo_key) or ""
        if _repo_scene:
            return _repo_scene, 0
    except Exception:
        pass
    return "", 0


def get_issue_bound_scene(repo_key: str, issue_id: str) -> tuple:
    """读取 Issue 真实绑定的场景（Issue #681，推荐入口专用，勿用于展示/A2A）。

    与 get_issue_scene 的区别：**不回退 managed_repos.dev_scene**，只查两层真实绑定：
    1. issue_state.scene_id（get_issue_stage）
    2. issue 表 scene_id（Phase C3 权威源）

    返回 (scene_id, scene_user_id)：
    - 命中任一层真实绑定 → 该值（GitCode 详情页选择 / Task Center 创建时写入 /
      VOD 智能推荐完成后写回，均为真实绑定，应跳过推荐）
    - 均未命中 → ("", 0)（无真实绑定，应走智能推荐；dev_scene 仅作展示兜底，
      不构成跳过推荐的强绑定，见 Issue #681 / #415 spec）

    PR 订阅伪 issue_id（#pr_<PR号>_<scene_id>）场景 ID 编码在伪 id 内，视为真实绑定。
    """
    import re as _re
    _m = _re.match(r"^#?pr_\d+_(.+)$", str(issue_id or ""))
    if _m:
        return _m.group(1), 0
    st = get_issue_stage(repo_key, issue_id)
    if st and st.get("scene_id"):
        return st["scene_id"], _scene_uid_or_zero(st.get("scene_user_id", 0))
    _num = _issue_num_or_ident(issue_id)
    if _num:
        try:
            r = rt.read_one_any("issue_card_row", (repo_key, _num))
            if r and r[5]:
                return r[5], _scene_uid_or_zero(r[6])
        except Exception:
            pass
    return "", 0


def set_issue_scene(repo_key: str, issue_id: str, scene_id: str, scene_user_id: int | None = None):
    """写入 Issue 级开发场景（VOD 判定后调用，幂等）。Phase C3：仅写 issue 单表。

    scene_user_id=None（默认）= **不覆盖**已有归属人，只更新 scene_id。
    原因：交办刷新场景的调用方拿不到新场景归属人时，原实现默认写 0，会把已绑定的
    归属人静默冲成 0；随后按 Issue 场景取归属人建任务 → agent_tasks.user_id=0 →
    scope=user 的个人引擎在 claim_by_tag 的 scope 过滤（`t.user_id=<owner id>`）
    里永远领不到该任务（无日志、任务永久 queued）。
    与 `set_issue_stage` 的 assignee「只在显式传入时写」（Issue #3646）同款口径：
    显式传 0 仍是「确实没有归属人」，照写；不传则保留旧值。
    """
    _fields: dict = {"scene_id": scene_id or ""}
    if scene_user_id is not None:
        _fields["scene_user_id"] = scene_user_id
    _sync_issue_fields(repo_key, issue_id, _fields, ensure_row=True)
    from src.kernel.config import log
    _uid_desc = "preserve" if scene_user_id is None else scene_user_id
    log.info(f"IssueScene: repo={repo_key} issue={issue_id} scene={scene_id} uid={_uid_desc}")


def get_carried_context(repo_key: str, issue_number: int) -> dict:
    """Issue #329: 读取 issue 表 carried_context 列，返回解析后的 dict（无记录/异常返回 {}）。

    集中在此避免路由层裸 SQL 绕数据访问层。
    """
    import json as _json
    try:
        row = rt.read_one_any("issue_card_row", (repo_key, issue_number))
        if row and row[10]:
            _cc = row[10]
            return _json.loads(_cc) if isinstance(_cc, str) else (_cc or {})
    except Exception:
        pass
    return {}


def set_scene_override_flag(repo_key: str, issue_id: str, allowed: bool):
    """设置 Issue 是否允许 VOD 覆盖场景（allow_scene_override）。Phase C3：仅写 issue 单表。"""
    val = 1 if allowed else 0
    _sync_issue_fields(repo_key, issue_id, {"allow_scene_override": val}, ensure_row=True)
    log.info(f"SceneOverride: repo={repo_key} issue={issue_id} allowed={val}")


def get_scene_override_flag(repo_key: str, issue_id: str) -> bool:
    """读取 Issue 是否允许 VOD 覆盖场景。默认 False。Phase C3：读 issue 单表。"""
    _num = _issue_num_or_ident(issue_id)
    if _num:
        try:
            row = rt.read_one_any("issue_card_row", (repo_key, _num))
            return bool(row[9]) if row and row[9] else False
        except Exception:
            return False
    return False


def set_parent_issue(repo_key: str, issue_id: str, parent_issue_id: str):
    """标记 Issue 为子任务（设置 parent_issue_id）。Phase C3：仅写 issue 单表。"""
    _sync_issue_fields(repo_key, issue_id, {"parent_issue_id": parent_issue_id or ""}, ensure_row=True)
    from src.kernel.config import log
    log.info(f"ParentIssue: repo={repo_key} issue={issue_id} parent={parent_issue_id}")


def get_parent_issue(repo_key: str, issue_id: str) -> str:
    """返回 Issue 的 parent_issue_id（空=普通任务）。"""
    st = get_issue_stage(repo_key, issue_id)
    return (st or {}).get("parent_issue_id", "")


def set_subtask_mode(repo_key: str, issue_id: str, enabled: bool = True):
    """标记 Issue 为多任务模式（父任务）。Phase C3：仅写 issue 单表。"""
    val = 1 if enabled else 0
    _sync_issue_fields(repo_key, issue_id, {"is_subtask_mode": val}, ensure_row=True)
    from src.kernel.config import log
    log.info(f"SubtaskMode: repo={repo_key} issue={issue_id} enabled={enabled}")


def is_subtask(repo_key: str, issue_id: str) -> bool:
    """判断 Issue 是否为子任务（parent_issue_id 非空）。"""
    return bool(get_parent_issue(repo_key, issue_id))


def is_subtask_mode_enabled(repo_key: str, issue_id: str) -> bool:
    """判断 Issue 是否启用了多任务模式（父任务标记）。"""
    st = get_issue_stage(repo_key, issue_id)
    return bool((st or {}).get("is_subtask_mode", 0))


def get_subtask_issues(repo_key: str, parent_issue_id: str) -> list[dict]:
    """查询父任务的所有子任务（issue 单表）。返回 issue_id/stage/owner_agent。"""
    _pid = parent_issue_id or ""
    if _pid and not str(_pid).startswith("#"):
        _pid = "#" + str(_pid)
    rows = rt.read_all_any("issue_children_by_parent", (repo_key, _pid))
    return [{"issue_id": "#" + str(r[0]), "stage": r[1], "owner_agent": r[2]} for r in rows]


def list_release_subissues(repo_key: str, parent_issue_number: int) -> list[dict]:
    """查询源需求下的发布子单列表（方案 C：子单为 issue 行，release_target_env 非空即发布子单）。

    返回最新在前，含绑定要素 / 生命周期状态 / 结果三分支 / 失败原因。
    """
    # Issue #3055：父单号同样走内核原生形态（数字→int，ident→str）
    _parent = _issue_num_or_ident(parent_issue_number)
    rows = rt.read_all_any("issue_release_children", (repo_key, _parent))
    out = []
    for r in rows:
        out.append({
            "issue_id": r[0],
            "sub_issue_number": r[1],
            "parent_issue_number": _parent,
            "title": r[2] or "",
            "status": r[3] or "active",
            "stage": r[4] or "vod",
            "candidate_sha": r[5] or "",
            "target_env": r[6] or "",
            "evidence_ref": r[7] or "",
            "result": r[8] or "",
            "failure_reason": r[9] or "",
            "approved_by": r[10] or 0,
            "approved_at": r[11] or "",
            "created_at": r[12] or "",
            "completed_at": r[13] or "",
        })
    return out


def _session_engine_uid_rt(runtime_id) -> str:
    """Issue #868 (T10) 双写：runtime_id → engine_uid（任意拓扑具名查询；行不存在/未迁移返回 ''）。"""
    try:
        rid = int(runtime_id or 0)
    except (TypeError, ValueError):
        rid = 0
    if not rid:
        return ""
    try:
        row = rt.read_one_any("agent_runtime_engine_uid_by_id", (rid,))
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def get_or_create_agent_session(repo_key: str, agent_type: str,
                                 backend: str = "", runtime_id: int = 0) -> dict:
    """获取或创建 Agent 会话，每个 (repo_key, agent_type) 独立（backend 无字面默认，见 engine-selection-design.md v2）。

    Issue #400：显式传入 runtime_id（会话引擎指针）时，新建会话直接采用该指针，
    不再走 pick_engine / 按名解析——保证"会话引擎指针 == 实际执行引擎"
    （与 guard_issue_session_pointer 的指针语义对齐）。未传（0）时保持既有行为。

    Issue #868 (T10)：新建/更新会话双写 engine_uid（runtime_id 的 uid 镜像）；
    读取返回 engine_uid（uid 优先、runtime_id 兜底由消费方解析）；
    旧库缺列（迁移未跑）自动降级 legacy SQL，不报错。
    """
    row = None
    # Issue #868 (T10)：uid 版查询优先；旧库缺列降级 legacy 查询（行尾补空 uid 占位）
    row = _session_row_read(repo_key, agent_type)

    if row:
        return {
            "id": row[0],
            "runtime_session_id": row[1] or "",
            "status": row[2] or "idle",
            "last_active_at": row[3] or "",
            "enable": bool(row[4]) if row[4] is not None else True,
            # Issue #868 (T10)：engine_uid 读取轨道（named query 已扩第 6 列）
            "engine_uid": (row[5] or "") if len(row) > 5 else "",
            "new": False,
            "repo_key": repo_key,
            "agent_type": agent_type,
        }

    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    runtime_id = int(runtime_id or 0)
    if not runtime_id and backend:
        # 引擎集合优先（engine-selection-design.md v2）；无集合回退按类型名解析（legacy）
        from src.domains.runtime.engine.engine_select import pick_engine
        runtime_id = pick_engine(repo_key, agent_type, fallback_runtime_id=0) or 0
        if not runtime_id:
            from src.domains.mgmt.user.db import get_runtime_id_by_name, ensure_default_backend
            runtime_id = get_runtime_id_by_name(backend) or ensure_default_backend(backend) or 0
    _enable = get_agent_type_enable(agent_type)
    # Issue #868 (T10)：新建会话双写 engine_uid（runtime_id 的 uid 镜像，任意拓扑查询）
    _sess_uid = _session_engine_uid_rt(runtime_id)
    _uid_written: list = []
    try:
        rt.ensure_agent_session(sid, repo_key, agent_type, runtime_id, status="idle",
                                enable=_enable, subscribe_events="[]", engine_uid=_sess_uid,
                                uid_written=_uid_written)
    except Exception as e:
        log.warning(f"Failed to create agent session: {e}")
        # 旧库缺列降级：engine_uid 未落库则按空返回（与 legacy 读路径口径一致）
        _ret_uid = _sess_uid if (_uid_written and _uid_written[0]) else ""
        return {"id": sid, "runtime_session_id": "", "status": "idle",
                "last_active_at": now, "enable": bool(_enable), "engine_uid": _ret_uid,
                "new": True, "repo_key": repo_key, "agent_type": agent_type}

    # 旧库缺列降级：engine_uid 未落库则按空返回（与 legacy 读路径口径一致）
    _ret_uid_ok = _sess_uid if (_uid_written and _uid_written[0]) else ""
    return {"id": sid, "runtime_session_id": "", "status": "idle",
            "last_active_at": now, "enable": bool(_enable), "engine_uid": _ret_uid_ok,
            "new": True, "repo_key": repo_key, "agent_type": agent_type}


def upsert_agent_session(*, repo_key: str, agent_type: str,
                         runtime_id: int = 0, skill: str = "", skill_id: int = 0,
                         repo_gitcode_token: str | None = None, status: str = "idle",
                         enable: int | None = None, subscribe_events: str | None = None) -> str:
    """创建或更新 Agent 会话，保留既有主键 id。

    背景（2026-08 事故）：id 是 TEXT 主键 + UNIQUE(repo_key, agent_type)。旧实现每次用
    新 uuid + INSERT OR REPLACE，REPLACE 遇 UNIQUE 冲突会删旧行插新行 → id 变化，
    导致 vod_conversations.agent_session_id 悬空（"agent_session_id not found, auto-rebind"）。
    修复：已存在 → UPDATE 保留 id；不存在 → INSERT。

    enable/subscribe_events/repo_gitcode_token 传 None = 保留既有值（UPDATE 不改；INSERT 用默认）；
    传具体值 = 覆盖。
    A2 收尾：IM 凭证不再落 devops_agent_session（im_* 三列改读 grape_t_channel_installation）。
    repo_gitcode_token 同样 None=保留（R25.9 回声治理：session 行只允许存用户显式配置的
    agent 专属 token，低级别 token 永不回填；清空走 unbind 的专门 SQL 路径）。
    Issue #868 (T10)：runtime_id 更新时同步镜像 engine_uid（双写；旧库缺列降级跳过）。
    返回会话 id。
    """
    row = _session_row_read(repo_key, agent_type)
    if row:
        sid = row[0]
        fields: dict = {"runtime_id": runtime_id, "skill": skill, "skill_id": skill_id,
                        "status": status}
        if repo_gitcode_token is not None:
            fields["repo_gitcode_token"] = repo_gitcode_token
        if enable is not None:
            fields["enable"] = enable
        if subscribe_events is not None:
            fields["subscribe_events"] = subscribe_events
        # Issue #868 (T10)：uid 轨道镜像跟随 runtime_id（双写；旧库缺列降级跳过）
        _mirror_uid = _session_engine_uid_rt(runtime_id)
        if _mirror_uid:
            rt.update_agent_session_fields(
                sid, {k: v for k, v in fields.items()})
            try:
                rt.update_agent_session_fields(sid, {"engine_uid": _mirror_uid},
                                               touch_last_active=False)
            except Exception:
                pass  # 旧库缺列降级：uid 镜像跳过，不影响主字段更新
            return sid
        rt.update_agent_session_fields(sid, fields)
        return sid
    sid = str(uuid.uuid4())
    _enable = enable if enable is not None else get_agent_type_enable(agent_type)
    _sub = subscribe_events if subscribe_events is not None else "[]"
    rt.ensure_agent_session(sid, repo_key, agent_type, runtime_id, skill=skill,
                            skill_id=skill_id, repo_gitcode_token=repo_gitcode_token or "",
                            status=status, enable=_enable, subscribe_events=_sub,
                            engine_uid=_session_engine_uid_rt(runtime_id))
    return sid


def update_agent_session(session_id: str, status: str = "idle",
                         oc_session_id: str = None):
    """更新 Agent 会话状态"""
    try:
        fields: dict = {"status": status}
        if oc_session_id:
            fields["runtime_session_id"] = oc_session_id
        rt.update_agent_session_fields(session_id, fields)
    except Exception as e:
        log.warning(f"Failed to update session {session_id}: {e}")


def list_agent_sessions(repo_key: str) -> list[dict]:
    """列出仓库的所有 Agent 会话"""
    try:
        rows = rt.read_all_any("devops_agent_sessions_by_repo", (repo_key,))
        result = []
        for r in rows:
            backend_name = ""
            if r[2]:
                _brow = rt.read_one_any("agent_runtime_name_by_id", (r[2],))
                if _brow and _brow[0]:
                    backend_name = _brow[0]
            result.append({"id": r[0], "agent_type": r[1], "backend": backend_name,
                           "runtime_session_id": r[3] or "", "status": r[4],
                           "last_active_at": normalize_event_time(r[5] or ""),
                           "enable": bool(r[6]) if r[6] is not None else True})
        return result
    except Exception:
        return []


def delete_agent_session(repo_key: str, agent_type: str) -> bool:
    """删除单个 Agent 会话"""
    try:
        return rt.delete_agent_sessions(repo_key, agent_type) > 0
    except Exception:
        return False


def ensure_default_agent_sessions(repo_key: str):
    """基于 agent-skill-mapping.json 的 default 场景，为仓库创建默认 Agent 会话"""
    # 先确保仓库有默认场景（幂等：已有场景则跳过）
    # devops_pipeline_scene 归 mgmt：计数走 mgmt public（api 库无本表，原直发 SQL 在三库
    # 拓扑下必 1146 被外层吞成 0，导致每次托管都白跑一遍建场景链）。
    try:
        _scene_count = mgmt.count_active_scenes_any(repo_key)
        if _scene_count == 0:
            from src.api.pipeline_config import apply_market_scene_by_name
            apply_market_scene_by_name(repo_key, "default")
            log.info(f"ensure_default_agent_sessions: created default scene for {repo_key}")
    except Exception as _e:
        log.warning(f"ensure_default_agent_sessions: scene creation skipped: {_e}")

    import os
    _path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-skill-mapping.json")
    if not os.path.exists(_path):
        log.warning(f"ensure_default_agent_sessions: {_path} not found, skip")
        return

    with open(_path, "r", encoding="utf-8") as _f:
        _cfg = json.load(_f)
    _mapping = _cfg.get("scenes", {}).get("default", {}).get("mapping", {})
    # 读仓库开发场景（managed_repos.dev_scene），按场景读映射——skill-dev 仓库自动创建 skill-creator/skill-tester/skill-publish
    try:
        from src.domains.mgmt.user.db import get_repo_scene
        _scene_id = get_repo_scene(repo_key) or "default"
        _scene_mapping = _cfg.get("scenes", {}).get(_scene_id, {}).get("mapping", {})
        if _scene_mapping:
            _mapping = _scene_mapping
    except Exception:
        pass
    if not _mapping:
        return

    # R25.9 回声治理：绑定时 session 表必然为空，read_stored_token 只可能解析出
    # bot/用户级 token——session 行只允许存用户显式配置的 agent 专属 token，
    # 低级别值永不回填（否则第 1 级命中 bot token，自循环检测放行 bot 评论 → 回声）。
    # 执行链实时取已足够（claim 注入 + _get_task_token）。
    with db_lock:
        c = db.cursor()
        from src.domains.runtime.engine.engine_select import resolve_default_engine
        for _at, _sk in _mapping.items():
            try:
                from src.domains.mgmt.user.db import resolve_skill_id
                _skill_id = resolve_skill_id(_sk, repo_key=repo_key)
                # 默认引擎：按 专属→全局→本机 选择，写入 session 主引擎；
                # 无候选则 runtime_id=0（agent 标识"无 engine，不可用"）
                _rid = resolve_default_engine(repo_key, _at) or 0
                # 保留既有主键 id 与用户配置（enable/订阅/飞书绑定/token），不因重建换 uuid
                #（见 upsert_agent_session 背景注释：INSERT OR REPLACE 新 uuid 会悬空 vod_conversations）
                upsert_agent_session(
                    repo_key=repo_key, agent_type=_at,
                    runtime_id=_rid, skill=_sk, skill_id=_skill_id,
                    repo_gitcode_token=None, status="idle",
                    enable=None, subscribe_events=None,
                )
            except Exception as e:
                log.warning(f"Failed to create session for {_at}: {e}")
        db.commit()

    log.info(f"Auto-created agent sessions for repo={repo_key} from default scene")


def remove_agent_sessions(repo_key: str) -> int:
    """删除仓库的所有 Agent 会话"""
    try:
        _cnt = rt.delete_agent_sessions(repo_key)
        if _cnt:
            log.info(f"Removed {_cnt} agent session(s) for repo={repo_key}")
        return _cnt
    except Exception as e:
        log.warning(f"Failed to remove sessions for {repo_key}: {e}")
        return 0


# ===== Issue 级执行会话（多 Issue 并行，见 multi-issue-isolation-design.md §3.3）=====
# 与 devops_agent_session（repo+agent 配置）分离：issue_agent_session 只承载 Issue 级
# 会话续接指针（runtime_session_id）+ 状态，作为 agent_tasks.issue_session_id 的串行粒度。

_ISSUE_SESSION_PR_SEP = "::pr"


def issue_session_key(issue_id: str, pr_number="") -> str:
    """Issue 级会话键（Issue #3375）：有 PR 时追加 `::pr<n>`，同 Issue 多 PR 各持会话。

    无 PR 时与原键（issue_id）完全一致，历史 Issue 会话零改动。会话行的 issue_id
    保持真实编号 + PR 后缀，读取侧用 split_issue_session_key 还原真实 Issue 号。
    """
    from src.kernel.pr_scope import normalize_pr_number
    base = issue_id or ""
    _pr = normalize_pr_number(pr_number)
    if not _pr or base.endswith(f"{_ISSUE_SESSION_PR_SEP}{_pr}"):
        return base
    return f"{base}{_ISSUE_SESSION_PR_SEP}{_pr}"


def split_issue_session_key(key: str) -> tuple[str, str]:
    """拆分会话键 → (真实 issue_id, pr_number)；无 PR 后缀时 pr_number=''。"""
    from src.kernel.pr_scope import normalize_pr_number
    text = str(key or "")
    idx = text.rfind(_ISSUE_SESSION_PR_SEP)
    if idx < 0:
        return text, ""
    _pr = normalize_pr_number(text[idx + len(_ISSUE_SESSION_PR_SEP):])
    if not _pr:
        return text, ""
    return text[:idx], _pr


def get_or_create_issue_session(repo_key: str, agent_type: str, issue_id: str,
                                pr_number="") -> dict:
    """获取或创建 Issue 级执行会话，per (repo_key, agent_type, issue_id[, pr]) 独立。

    每个 Issue 拥有完全独立的 opencode session：新 Issue 首次创建时 runtime_session_id 为空
    （不继承 repo 级 devops_agent_session 的指针，避免多 Issue 共用同一 session 导致 cwd/上下文错位，
    见 vod-agent-hang-rootcause-fix.md 方案 A）；第一次执行由 opencode 生成全新 session 后回写本行。

    Issue #3375：pr_number 有值时按 PR 维度再隔离（同 Issue 多 PR 不共享 oc_session_id）。
    """
    issue_id = issue_session_key(issue_id, pr_number)
    row = None
    try:
        row = rt.read_one_any("issue_agent_session_row", (repo_key, agent_type, issue_id))
    except Exception:
        row = None
    if row:
        return {"id": row[0], "runtime_session_id": row[1] or "",
                "status": row[2] or "idle", "last_active_at": row[3] or "",
                "session_engine": row[4] or "", "new": False}

    # 每 Issue 独立 session：新 Issue 不继承 repo 级 session 指针，_seed 恒为空
    _seed = ""
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    try:
        rt.exec_any("issue_agent_session_insert",
                    (sid, repo_key, agent_type, issue_id, _seed, "", "idle", now))
    except Exception as e:
        log.warning(f"Failed to create issue session: {e}")
        return {"id": sid, "runtime_session_id": _seed, "status": "idle",
                "last_active_at": now, "session_engine": "", "new": True}
    return {"id": sid, "runtime_session_id": _seed, "status": "idle",
            "last_active_at": now, "session_engine": "", "new": True}


def update_issue_session(session_id: str, runtime_session_id: str = None, status: str = None,
                         session_engine: str = None):
    """更新 Issue 级执行会话：续接指针 / 状态 / 引擎名。指针为空则不覆盖（避免丢上下文）。"""
    if not session_id:
        return
    fields: dict = {}
    if runtime_session_id is not None:
        fields["runtime_session_id"] = runtime_session_id
    if session_engine is not None:
        fields["session_engine"] = session_engine
    if status is not None:
        fields["status"] = status
    if not fields:
        return
    try:
        rt.update_issue_agent_session_fields(session_id, fields)
    except Exception as e:
        # 文案避开「关键字+表名」形态：静态扫描（scripts/foreign_table_refs.py）按
        # 「FROM/JOIN/INTO/UPDATE+下一个标识符」判定表引用，原句里的 update 与 issue
        # 相邻会被记成一次外域引用（假阳性）。改成不含 SQL 关键字紧邻词的写法。
        log.warning(f"Failed to persist session {session_id}: {e}")


def get_issue_session(repo_key: str, agent_type: str, issue_id: str,
                      pr_number="") -> dict | None:
    """按 (repo, agent, issue[, pr]) 查 Issue 级执行会话；无记录返回 None。"""
    issue_id = issue_session_key(issue_id, pr_number)
    row = rt.read_one_any("issue_agent_session_row", (repo_key, agent_type, issue_id))
    if not row:
        return None
    return {"id": row[0], "runtime_session_id": row[1] or "",
            "status": row[2] or "idle", "last_active_at": row[3] or "",
            "session_engine": row[4] or ""}


def guard_issue_session_pointer(isess: dict, current_engine: str) -> str:
    """session 引擎守卫：校验续接指针的产出引擎与本次执行引擎是否同家。

    - 指针为空 / 引擎名缺失（旧行、反查失败）→ 放行原值（fail-safe，不 block 任务）；
    - 指针引擎与本次引擎不一致 → 返回 ""（弃用指针，引擎开新会话），指针本身不动，
      由死 session 检测（连续 3 次负数退出码）或下一次同家引擎回写覆盖。
    防引擎池混跑时跨引擎续接（hermes resume opencode 的会话号 → 假成功 → prompt 静默挂死到超时）。
    """
    sid = (isess or {}).get("runtime_session_id") or ""
    if not sid:
        return ""
    ptr_engine = (isess or {}).get("session_engine") or ""
    if not ptr_engine or not current_engine:
        return sid  # 引擎未知 → 不过滤
    if ptr_engine != current_engine:
        log.info(f"session engine guard: pointer engine '{ptr_engine}' != current '{current_engine}', "
                 f"drop cross-engine session pointer (sid={sid[:12]}...)")
        return ""
    return sid


def list_issue_sessions(repo_key: str, agent_type: str = "") -> list[dict]:
    """列出仓库的 Issue 级执行会话（运维/前端展示并行情况）。"""
    try:
        if agent_type:
            rows = rt.read_all_any("issue_agent_sessions_by_repo_agent", (repo_key, agent_type))
        else:
            rows = rt.read_all_any("issue_agent_sessions_by_repo", (repo_key,))
    except Exception:
        return []
    out = []
    for r in rows:
        _issue_id, _pr = split_issue_session_key(r[3])
        out.append({"id": r[0], "repo_key": r[1], "agent_type": r[2],
                    "issue_id": _issue_id, "pr_number": _pr,
                    "runtime_session_id": r[4] or "", "status": r[5],
                    "last_active_at": normalize_event_time(r[6]),
                    "created_at": normalize_event_time(r[7])})
    return out


def remove_issue_sessions(repo_key: str):
    """删除仓库的所有 Issue 级执行会话（随仓库移除清理）。"""
    try:
        return rt.exec_any("issue_agent_session_delete_by_repo", (repo_key,))
    except Exception as e:
        log.warning(f"Failed to remove issue sessions for {repo_key}: {e}")
        return 0


TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH = "target_scene_work_branch"
# 承接场景未配置 work_branch（含多跳链残留）时，历史权威标记显式失效为该值。
TARGET_BRANCH_SOURCE_UNCONFIRMED_NO_SCENE_WORK_BRANCH = "unconfirmed_no_target_scene_work_branch"


def _issue_num_or_ident(issue_id) -> "int | str | None":
    """D105 字符串 ident（gitee IK*）：issue_id 归一为平台原生形态——
    数字串→int（基线 #995 int 契约不变），非数字 ident（IK*，容忍 # 前缀）
    原样 str 返回；空/None 返回 None。此前 set_issue_stage/_sync_issue_fields
    等保存路径一律 int() 强转，字符串 ident 必炸被 except 吞掉 → stage/scene/
    status 静默不落库（gitee confirm-done 后 stage 恒 'vod' 的写入点）。

    Issue #4065：卡片主键口径收敛到 `card_ident_number`——`autopilot-*` 合成键
    （issue 表真实主键）照常可查/可写；`pr_*` 订阅伪 id / 空 / 含分隔文本一律
    None（此前原样透传，使 get_issue_stage 拿伪 id 打卡片端点 → 244 条/小时 422）。
    """
    # Issue #3055：判定/强转统一走内核唯一入口（数字→int，ident→str）
    from src.kernel.issue_ident import card_ident_number
    return card_ident_number(issue_id)


def _scene_binding_active(repo_key: str, scene_id: str) -> bool:
    """绑定场景是否仍可用（Issue #1745：场景行存在但全部 is_active=0 → 失效）。

    行不存在不算失效（内置/场景市场未落库的绑定沿用 #568 既有契约）；查询异常按有效处理，
    不因 DB 抖动误清空场景。
    """
    if not scene_id:
        return False
    # devops_pipeline_scene 归 mgmt：原语句在三库拓扑下打 api 库必 1146，异常分支把它
    # 当作「有效」放行 → 幽灵 scene_id 一路传到 scenes detail 404。查询本身改走 mgmt public。
    try:
        rows = mgmt.scene_active_flags_any(scene_id, repo_key)
    except Exception as _e:
        log.debug(f"_scene_binding_active: query skipped for {scene_id}: {_e}")
        return True
    if not rows:
        return True
    return any((r[0] if r else 0) for r in rows)


def resolve_scene_id(repo_key: str, issue_id: str = "") -> str:
    """解析 Issue 当前绑定开发场景 ID（Issue 级快照优先，空则回退仓库默认场景）。

    Issue #1753：统一 webhook / runtime.repogit / prompt_builder 的场景解析口径。
    Issue #1745：绑定场景已停用（幽灵 scene_id 会让 scenes detail 必 404）时回退仓库
    默认场景；仍不可用则按「未判定=空串」返回。未选定返回空串。
    """
    try:
        scene_id = (get_issue_scene(repo_key, issue_id)[0] or "")
    except Exception:
        return ""
    if not scene_id:
        return ""
    if _scene_binding_active(repo_key, scene_id):
        return scene_id
    try:
        from src.domains.mgmt.user.db import get_repo_scene
        fallback = get_repo_scene(repo_key) or ""
    except Exception:
        fallback = ""
    if fallback and fallback != scene_id and _scene_binding_active(repo_key, fallback):
        return fallback
    return ""


def get_scene_advanced_options(repo_key: str, scene_id: str) -> dict:
    """读取场景高级选项（devops_pipeline_scene.advanced_options，未配置/失败返回 {}）。

    Issue #1753：统一 webhook / runtime.repogit / prompt_builder 的场景配置读取口径，
    避免三处重复裸 SQL 导致字段口径漂移。
    """
    if not repo_key or not scene_id:
        return {}
    # devops_pipeline_scene 归 mgmt：复用既有「按仓库键取场景 advanced_options 原文」原语
    # （单键经 IN(?) 传，与 repo_key=? 等价；谓词/过滤位/LIMIT 1 逐字一致），
    # JSON 解析与类型归一仍留在调用方，口径未动。
    try:
        _raw = mgmt.pipeline_scene_advanced_options(scene_id, [repo_key])
        if not _raw:
            return {}
        opts = json.loads(_raw) if isinstance(_raw, str) else (_raw or {})
        return opts if isinstance(opts, dict) else {}
    except Exception:
        return {}


def get_scene_work_branch(repo_key: str, scene_id: str = "", issue_id: str = "") -> str:
    """读取场景权威开发基线 work_branch（与运行时 GRAPE_WORK_BRANCH 同源）。

    scene_id 显式给出时直接读取（如跨流水线承接目标场景）；否则按 issue_id 解析当前
    绑定场景。未配置/查询失败返回空串。
    """
    if not scene_id:
        scene_id = resolve_scene_id(repo_key, issue_id)
    if not scene_id:
        return ""
    return str(get_scene_advanced_options(repo_key, scene_id).get("work_branch", "") or "").strip()


def get_issue_status(repo_key: str, issue_id: str) -> str:
    """读 Issue 当前 status（只读 helper）。无记录或异常返回 ""。

    与 `get_issue_stage` 分离：其返回结构不含 status，需要判 status 的路径
    （如 PR merged 视同审核通过）单独读取，避免改动既有返回契约。
    """
    _num = _issue_num_or_ident(issue_id)
    if _num is None:
        return ""
    try:
        row = rt.read_one_any("issue_card_row", (repo_key, _num))
        return (row[11] if row and row[11] else "") or ""
    except Exception as _e:
        from src.kernel.config import log as _log3
        _log3.warning(f"get_issue_status failed for {repo_key} #{_num}: {_e}")
        return ""


# ==== merged from weekly-08 (symbols referenced by decouple modules) ====
