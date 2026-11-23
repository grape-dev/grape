"""Scenes 路由：场景列表获取、CRUD、应用"""

import json, os, re, threading, time
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Query, Request, Response

from pydantic import BaseModel

from src.kernel.domain_db import cursor as _domain_cursor
from src.api.deps import get_current_user_or_api_key, require_no_session_token
from src.api.oauth_token import with_oauth_token

from src.kernel.config import log
from src.kernel.tags import MAX_TAGS, encode_tags, prepare_tags, suggest_tags

# 技能/引擎/任务表归运行域：查询走该域 public（三库独立后 mgmt 连接看不到这些表）
from src.domains.runtime import public as _rt_scenes

router = APIRouter(prefix="/api/scenes", tags=["scenes"])

# Issue #862：体验模板创建总闸（#357 P1-C 引入，原 30s）。实测 domain-swarm
# 全流程 ≈30s 压线撞闸（shallow clone ~3s + git add ~3s + force push ~16s +
# 建仓/托管 API），模板工作区 ≈126MB。放宽至 45s 覆盖慢尾分布，仍远低于
# 外层 nginx 300s 与前端 300s；内层 git 操作超时（clone 120s / push 180s）不变。
# 根治（异步化/模板瘦身）见 Issue #862 M2。
EXPERIENCE_TEMPLATE_CREATE_TIMEOUT = 45

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "configs", "agent-skill-mapping.json")

# host 失败详情：只提取面向用户的 message，dict detail 不直接透出；原始异常仅落日志。
_HOST_WARNING_PREFIX = "托管配置失败："


def _host_error_message(exc) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return str(detail.get("message") or "")
    if isinstance(detail, str):
        return detail
    return ""

# Issue #542：branches 列表进程内 TTL 缓存（分支变更低频，可接受短暂陈旧；
# TTL 取分析文档建议区间 60~120s 的中值 90s；refresh=1 绕过，建分支主动失效）
_BRANCHES_CACHE_TTL = 90.0

_branches_cache: dict = {}

_branches_cache_lock = threading.Lock()

def _branches_cache_key(provider: str, owner: str, repo: str) -> str:
    return f"{provider}:{owner}/{repo}"

def _branches_cache_get(key: str):
    with _branches_cache_lock:
        entry = _branches_cache.get(key)
        if entry and time.time() - entry[0] < _BRANCHES_CACHE_TTL:
            return entry[1]
    return None

def _branches_cache_put(key: str, payload: dict):
    with _branches_cache_lock:
        _branches_cache[key] = (time.time(), payload)

def _branches_cache_invalidate(owner: str, repo: str):
    """按 owner/repo 失效两平台缓存键（建分支后立即可见）。"""
    with _branches_cache_lock:
        try:
            from src.domains.runtime.public import list_vcs_platforms as _lvp
            _pros = _lvp() or []
        except Exception:
            _pros = []
        for prov in _pros:
            _branches_cache.pop(_branches_cache_key(prov, owner, repo), None)

def _resolve_repo_key(repo: str) -> str:
    """查 DB 确定 repo_key 实际前缀（S104/D78 候选集卡派生，含 gitee/gitlab）。"""
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(repo)
    if ":" in repo:
        return rk
    try:
        from src.kernel.repo_key import repo_key_candidates
        # 2026-11-19：候选集必须在 db_lock **之前**算完。它内部要读平台卡
        # （list_vcs_platforms → platform_card），而平台卡构建会走 DB 拿 db_lock；
        # 「db_lock 里算候选集」与「平台卡构建里要 db_lock」反序 = 整进程 ABBA 死锁
        # （2026-11-19 两次全站卡死）。锁内现在只剩一条纯 SELECT。
        _rks = tuple(repo_key_candidates(repo))
        from src.kernel.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            row = c.execute(f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({','.join('?' * len(_rks))}) LIMIT 1",
                            _rks).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return rk


class _SceneUserIdentityMissing(RuntimeError):
    """平台返回空 login（用户平台身份缺失/无效）——客户端状态问题，非上游故障。

    继承 RuntimeError 以保持 with_oauth_token 的 401(token) 刷新重试语义（按消息
    文本判定）不变；刷新后仍拿不到 login 时传播到调用方，按 4xx 处理而非 502。
    """


def _scene_user_provider(user: dict) -> str:
    """场景体验的用户平台选择：仓库平台卡目录 ∩ 用户已绑定平台。

    login_provider 命中的平台优先，其余按卡目录顺序；无可用平台返回 ""。
    禁止平台名字面量（gitee/gitlab/华为等一律由卡目录派生）。
    """
    try:
        from src.domains.runtime.public import list_vcs_platforms
        _catalog = list(list_vcs_platforms())
    except Exception:
        _catalog = []
    try:
        from src.domains.mgmt.auth.grape_account_db import list_platform_links
        # A 批：场景可用平台 = 身份行 ∪ PAT 凭据域（只配 PAT 也视为该平台可用）
        _bound = {i.get("provider") for i in (list_platform_links(str(user.get("id") or "")) or [])
                  if i.get("provider")}
    except Exception:
        _bound = set()
    if not _catalog or not _bound:
        return ""
    _login = (user.get("login_provider") or "").strip()
    _ordered = [p for p in _catalog if p in _bound]
    _ordered.sort(key=lambda p: 0 if p == _login else 1)  # 稳定排序：login_provider 优先
    return _ordered[0] if _ordered else ""


def _scene_user_login_via_card(user: dict, provider: str) -> str:
    """token → 平台 login（卡工厂端口 create_provider(p).get_user_login）。

    经 with_oauth_token 包一层：401/403(token) 预刷新+重试语义与存量一致
    （provider 无关包装器）。login 为空时按 401 抛错触发刷新（存量口径）。
    """
    from src.domains.mgmt.vcs import create_provider
    from src.domains.runtime.public import get_platform_card

    _prov = create_provider(provider)
    _card = get_platform_card(provider)
    _name = _card.name if _card is not None else provider

    def _get_login(token):
        login = _prov.get_user_login(token)
        if not login:
            # P3 小项二：消息不再伪造 "(401)"——HTTP 401（token 失效）由 _http 的
            # RuntimeError 自带 (401) 触发 with_oauth_token 刷新重试；而 API 200 但
            # login 为空 = 身份缺失（客户端状态），不触发刷新，直接传播给调用方
            # 按 409 MISSING_IDENTITY 处理。
            raise _SceneUserIdentityMissing(f"{_name} API GET /user returned empty login: no usable user identity")
        return login

    return with_oauth_token(user, provider, _get_login)


def _resolve_scene_repo_key(repo: str, user: dict) -> str:
    """解析场景查询的 repo_key（Issue #534）。

    优先级：显式 repo 参数 > 会话级 token（tk_）绑定的 repo_key > 请求中间件 repo_var 上下文。
    替换失效的 app_current_repo() placeholder（固定返回 None）导致的缺省 repo_key="" 问题。
    解析结果仍作为 get_scenes / get_pipeline_config 的 repo 作用域入参，走既有归属校验，权限不放松。
    """
    if repo:
        # 显式 --repo 优先（向后兼容，行为不变）
        return _resolve_repo_key(repo)
    _u = user or {}
    if _u.get("auth_type") == "session_token" and _u.get("repo_key"):
        # 缺省回落 1：会话级 token 绑定的 repo_key（token 绑定仓库，自带前缀）
        return _u["repo_key"]
    # 缺省回落 2：请求中间件解析的 repo 上下文（query param / 路径推导）
    try:
        from src.kernel.config import repo_var
        _ctx_repo = repo_var.get()
        if _ctx_repo:
            return _ctx_repo if ":" in _ctx_repo else _resolve_repo_key(_ctx_repo)
    except Exception:
        pass
    return ""

def _scene_row_exists(scene_id: str, repo_key: str) -> bool:
    """场景 scene_id 在 repo_key 下是否存在有效 DB 行（Issue #568 二级解析触发判据）。"""
    if not scene_id:
        return False
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT 1 FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            return c.fetchone() is not None
    except Exception:
        return False

def _resolve_session_bound_scene(scene_id: str, user: dict):
    """会话绑定场景解析（Issue #568：scenes detail 与 a2a handoff 可见性统一的第二级）。

    a2a handoff 的 `_scene_node_check` 以 issue 绑定场景（get_issue_scene）为权威；
    scenes detail 原仅按 repo 作用域（--repo / 会话 token 绑定 repo_key / repo_var）解析，
    会话绑定仓库与场景归属仓库不一致（跨流水线/关联仓库信任域会话）时 404，与 a2a
    409 携带 nodes 的口径分歧。此处补齐同一解析口径：session token →
    agent_tasks(session_key) → issue_id → get_issue_scene，命中请求 scene_id 时
    返回其 `get_pipeline_config` 结果（节点集合与 a2a 一致）。

    仅会话级 token 启用；不放宽权限——issue 必须归属会话绑定仓库（agent_tasks.repo_key
    优先，缺省回落会话绑定 repo_key），显式 --repo 路径不经过本函数。
    未命中返回 None（调用方维持 404）。
    """
    _u = user or {}
    if _u.get("auth_type") != "session_token" or not _u.get("session_key"):
        return None
    _session_key = _u["session_key"]
    _repo_key = _u.get("repo_key") or ""
    _issue_id = ""
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            from src.kernel.domain_read import runtime_lib as _ru_lib_6e9804_f
            _ru_lib_6e9804 = _ru_lib_6e9804_f(foreign_only=True)
            if _ru_lib_6e9804 is not None:
                _row = _ru_lib_6e9804.read_one("agent_tasks_6e9804", (_session_key,))
            else:
                _row = _rt_scenes.task_issue_repo(_session_key)
        if _row:
            _issue_id = _row[0] or ""
            # agent_tasks 绑定仓库为权威（issue 归属仓库；跨流水线/关联仓库信任域
            # 会话的 user.repo_key 可能与 issue 归属不一致）
            _repo_key = _row[1] or _repo_key
    except Exception:
        return None
    if not _issue_id or not _repo_key:
        return None
    try:
        from src.api.session_store import get_issue_scene
        _bound_scene_id, _ = get_issue_scene(_repo_key, _issue_id)
    except Exception:
        return None
    if not _bound_scene_id or _bound_scene_id != scene_id:
        return None
    from src.api.pipeline_config import get_pipeline_config
    return get_pipeline_config(scene_id, repo_key=_repo_key)

def _load_scenes() -> dict:
    """读取 agent-skill-mapping.json 的场景配置（legacy fallback for apply）"""
    if not os.path.exists(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("scenes", {})

@router.get("", operation_id="scenes_list", summary="List scenes", description="Lists all available scenes for a repo (repo-scoped, non-official). Supports page/page_size (page omitted -> full list, backward compatible) and optional q (name/display_name/description LIKE search) + sort (id/latest); always returns total.")
def list_scenes(user: dict = Depends(require_no_session_token), repo: str = "",
                page: int = Query(default=None, ge=1),
                page_size: int = Query(default=20, ge=1, le=100),
                q: str = Query(default="", max_length=100),
                sort: str = Query(default="id", pattern="^(id|latest)$"),
                fields: str = Query(default="", pattern="^(|minimal)$")):
    """返回所有可用场景列表（repo，不含 official）。

    分页（场景数量增长后项目场景页不再全量拉取，Issue #501）：
    - 不带 page 参数：全量返回（老调用方零改动，向后兼容）
    - page>=1：SQL LIMIT/OFFSET（ORDER BY id 稳定排序），page_size 上限 100
    - total 始终返回：COUNT 查询（不带 page 时也与数组长度一致）
    - 检索（Issue #501 增量）：q 非空按 name/display_name/description LIKE 过滤，
      total 为命中数；sort=id 稳定序 / latest 按创建时间倒序。q/sort 均可选，默认行为不变。
    """
    from src.api.pipeline_config import get_scenes, _count_scenes_from_db
    repo_key = _resolve_scene_repo_key(repo, user)
    # FastAPI 直调（测试）时 Query 默认值是 Query 实例而非 None/int——规范化
    if not isinstance(page, int) or page < 1:
        page = None
    if not isinstance(page_size, int) or page_size < 1:
        page_size = 20
    if not isinstance(q, str):
        q = ""
    if sort not in ("id", "latest"):
        sort = "id"
    _minimal = (fields == "minimal")
    # Issue #501: total 走 COUNT（不拉 nodes_json），带 page 时 SQL 层分页；q 过滤两处同 WHERE
    # fields=minimal（C 方案）：跳过 nodes_json 解析，供只要名字/图标的下拉类调用方
    total = _count_scenes_from_db(repo_key, q=q)
    if page is not None:
        _start = (page - 1) * page_size
        scenes = get_scenes(repo_key=repo_key, limit=page_size, offset=_start, q=q, sort=sort, minimal=_minimal)
    else:
        scenes = get_scenes(repo_key=repo_key, q=q, sort=sort, minimal=_minimal)
    return {"scenes": scenes, "total": total}


@router.get("/with-pipeline", operation_id="scenes_with_pipeline", summary="List scenes with pipeline", description="Scenes with their pipeline config (description, work branch, creator). Reuses nodes_json from the single DB query. Supports page/page_size (page omitted -> full list) and optional q/sort like /scenes; always returns total; carries card fields (icon/scope/created_by/pin) so the dashboard can use it as its single scene-list source; enrichment (skill display names) runs only for the returned page.")
def list_scenes_with_pipeline(user: dict = Depends(require_no_session_token), repo: str = "",
                              page: Optional[int] = Query(default=None, ge=1),
                              page_size: int = Query(default=20, ge=1, le=100),
                              q: str = Query(default="", max_length=100),
                              sort: str = Query(default="id", pattern="^(id|latest)$")):
    """返回场景列表 + 各场景流水线配置（含描述、工作分支、创建人）。

    优化：get_scenes 已在单次 DB 查询中返回 nodes_json，直接复用，
    不再逐场景调 get_pipeline_config 做二次 DB 查询。

    分页（与 /scenes 同参数同语义，前端项目场景页的翻页依赖这里）：
    - 不带 page：全量返回（老调用方零改动，向后兼容）
    - page>=1：SQL LIMIT/OFFSET + ORDER BY（含置顶序），total 走 COUNT
    - 技能显示名解析（enrich）只跑当前页，不再为看不到的页付解析成本
    - q/sort 与 /scenes 完全同参同语义（name/display_name/description LIKE + 置顶序）
    """
    from src.api.pipeline_config import get_scenes, get_pipeline_config, _count_scenes_from_db
    from src.kernel.repo_key import to_repo_key
    repo_key = _resolve_scene_repo_key(repo, user)
    # FastAPI 直调（测试）时 Query 默认值是 Query 实例而非 int/None——规范化
    if not isinstance(page, int) or page < 1:
        page = None
    if not isinstance(page_size, int) or page_size < 1:
        page_size = 20
    if not isinstance(q, str):
        q = ""
    if sort not in ("id", "latest"):
        sort = "id"
    # total 恒返回（COUNT 不拉 nodes_json）；不带 page 时与数组长度一致
    _total = _count_scenes_from_db(repo_key, q=q)
    if page is not None:
        _start = (page - 1) * page_size
        scene_list = get_scenes(repo_key=repo_key, limit=page_size, offset=_start,
                                q=q, sort=sort)
    else:
        scene_list = get_scenes(repo_key=repo_key, q=q, sort=sort)

    # Issue #980：显示名解析统一走共用 skill_display 层（skill_id 优先 →
    # (scope, skill_name) → user/repo 上下文 → official 兜底），三档同名技能
    # 不再被 official 映射一律覆盖成官方名。
    from src.api.skill_display import build_skill_display_map, resolve_node_skill_display
    _uid_ctx = user.get("id", "")
    skill_display_map = build_skill_display_map(repo_key=repo_key, user_id=_uid_ctx)

    result = []
    for s in scene_list:
        # get_scenes 已返回 nodes，直接用；DB 查不到的（official 模板）回退 get_pipeline_config
        nodes = s.get("nodes")
        if nodes is not None:
            cfg = {"name": s["name"], "nodes": nodes, "onboarding": s.get("onboarding") or "repo-onboarding"}
        else:
            cfg = get_pipeline_config(s["id"], repo_key=repo_key) or {}
        # 给每个 node 补 skill_display_name（Issue #980：node 带 skill_id 时
        # 精确解析；否则按共用映射（含 user/repo/official 档位优先级））
        for n in (cfg.get("nodes") or []):
            _sk = n.get("skill", "")
            if not _sk:
                n["skill_display_name"] = ""
                continue
            _sid = n.get("skill_id") or 0
            n["skill_display_name"] = resolve_node_skill_display(
                _sk, _sid, skill_display_map, repo_key=repo_key, user_id=_uid_ctx)
        # Issue #501: 引擎在线探测已从本接口移出——N 场景 × M 节点的探测改为
        # 按需接口 GET /scenes/{scene_id}/engine-status（需求中心弹窗选中场景时才探测）。
        # 本接口不再返回 offline_nodes/sre_unbound/all_offline。
        result.append({"id": s["id"], "name": s["name"],
                       "display_name": s.get("display_name") or s["name"],
                       "description": s.get("description") or "",
                       "work_branch": s.get("work_branch") or "main",
                       "created_by": s.get("created_by") or "",
                       "scene_type": s.get("scene_type") or "standard",
                       "pr_subscription_enabled": bool(s.get("pr_subscription_enabled", 0)),
                       "linked_repos": s.get("linked_repos") or [],
                       "is_pinned": int(s.get("is_pinned") or 0),
                       "pinned_at": s.get("pinned_at") or "",
                       "pipeline": cfg})
    # total 用 COUNT 的结果（不带 page 时与数组长度一致；带 page 时是全量总数，
    # 不能再取 len(result) —— 那是当前页条数，会让前端分页条永远只显示 1 页）
    if not _total:
        _total = len(result)
    return {"scenes": result, "total": _total}


class PinSceneRequest(BaseModel):
    pinned: bool

@router.post("/{scene_id}/pin", operation_id="scenes_pin", summary="Pin scene", description="Sets the repo-shared pinned state of a scene (pinned scenes sort first, ordered by pinned_at desc).")
def pin_scene(scene_id: str, body: PinSceneRequest, request: Request,
              user: dict = Depends(require_no_session_token), repo: str = ""):
    """设置/取消场景置顶（仓库共享，不区分用户）。

    Issue #1735 复审（MR !1145 Critical-2 / Medium-1）：
    - 必须带仓库上下文：repo_key 为空时直接 400，禁止落到 `repo_key=''` 的全局/官方行；
    - 访问权无条件校验（不再以 repo_key 非空为前提）；
    - 写操作落 audit_logs（scene_pin），可按 actor/target 追溯。
    [rebase 并入 2026-11-12] 基线 #1735 端点（我方 c4 解算时误随重复块丢弃）。
    """
    from src.api.pipeline_config import set_scene_pinned, _query_scene_by_id
    from src.api.deps import check_repo_access

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    if not repo_key:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "请指定仓库（--repo owner/repo）；置顶状态按仓库共享，不接受空仓库上下文"})
    check_repo_access(user, repo_key)

    existing = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    # 存在性已校验；MySQL 对「值未变」的 UPDATE 返回 changed=0，不能据此再判 404
    set_scene_pinned(scene_id, repo_key, body.pinned)

    from src.domains.mgmt.auth import audit as auth_audit
    auth_audit.log_audit(
        "scene_pin", operator_id=user.get("id") or 0,
        ip=(request.client.host if request and request.client else ""),
        detail={"scene_id": scene_id, "repo_key": repo_key, "pinned": bool(body.pinned)})
    log.info(f"Scene pinned: id={scene_id} pinned={body.pinned} by user={user.get('id')}")
    return {"message": "置顶状态已更新", "scene_id": scene_id,
            "is_pinned": 1 if body.pinned else 0}


def _scene_engine_status(scene_id: str, cfg_nodes: list, repo_key: str = "",
                         user_id: int = 0) -> dict:
    """计算单个场景的引擎在线状态（Issue #501：从 with-pipeline 抽出，按需探测）。

    语义与原 with-pipeline 内联版完全一致：
    - 对每个绑定 engine_runtime_ids 的启用节点跑 get_scene_pool_engine_ids，
      返回 None = 该节点绑定引擎全部离线 → 记入 offline_nodes
    - SRE 特例（sre-repo-worker-relax Q2）：SRE 空绑定=禁止广播（无引擎），
      与「配了引擎但全灭」区分开——sre_unbound 单独标记，前端提示「未选择机器」
    - 校验失败降级为不标记（不让提示功能本身挡住需求创建入口）

    Issue #1208（L2 可见性徽标，复用入队校验查询同一把尺子）：
    - selector 节点 0 命中（全库无引擎携带该标签，配置死）→ selector_dead_nodes（红点）
    - selector 节点命中引擎当前全离线（瞬时死）→ selector_offline_nodes（黄点）
    - all_offline 聚合含两类 selector 异常（既有消费方警告条不回归）
    """
    offline_nodes: list[str] = []
    sre_unbound: bool = False
    selector_dead_nodes: list[str] = []
    selector_offline_nodes: list[str] = []
    try:
        from src.domains.runtime.public import (get_scene_pool_engine_ids as _gspei,
                                               get_scene_engine_selector,
                                               selector_candidate_engines)
        for n in (cfg_nodes or []):
            if not isinstance(n, dict):
                continue
            _tag = n.get("tag") or n.get("id") or ""
            _eids = n.get("engine_runtime_ids") or []
            _uids = n.get("engine_runtime_uids") or []
            _sel = n.get("engine_selector") or {}
            if not _tag:
                continue
            if n.get("enabled") is False:
                continue
            if (_sel.get("tags") or []) and not _eids and not _uids:
                # selector 节点：复用入队候选集查询（同一把尺子），红/黄分层
                try:
                    _matched, _dropped = selector_candidate_engines(
                        _sel, _tag, repo_key=repo_key, user_id=user_id)
                    if _matched:
                        pass  # 有健康命中 → 不标记
                    elif any(d.get("reason") in ("offline", "stale") for d in _dropped):
                        selector_offline_nodes.append(str(_tag))
                    else:
                        selector_dead_nodes.append(str(_tag))
                except Exception:
                    pass
                continue
            if not _eids and not _uids:
                # SRE 没选机器：无绑定即无引擎（不广播），提示用户去绑
                if _tag and str(_tag).lower() == "sre":
                    sre_unbound = True
                continue
            try:
                if _gspei(scene_id, _tag, repo_key=repo_key) is None:
                    offline_nodes.append(str(_tag))
            except Exception:
                pass
    except Exception:
        pass
    return {"offline_nodes": offline_nodes,
            "sre_unbound": sre_unbound,
            "selector_dead_nodes": selector_dead_nodes,
            "selector_offline_nodes": selector_offline_nodes,
            "all_offline": bool(offline_nodes) or sre_unbound
                           or bool(selector_dead_nodes) or bool(selector_offline_nodes)}


@router.get("/{scene_id}/engine-status", operation_id="scenes_engine_status",
            summary="Scene engine online status",
            description="On-demand engine probe for one scene (moved out of /scenes/with-pipeline, Issue #501). Returns offline_nodes / sre_unbound / all_offline for the given scene; probe failures degrade to no-warning.")
def get_scene_engine_status(scene_id: str, user: dict = Depends(require_no_session_token), repo: str = ""):
    """返回单个场景的引擎在线状态（需求中心弹窗选中场景时按需调用，1 场景 × M 节点）。

    节点来源与 with-pipeline 一致：DB 场景（get_scenes）优先，查不到回退
    get_pipeline_config（official 模板 / pipelines.json）。
    场景不存在时返回 all_offline=False（不拦创建入口）。
    """
    from src.api.pipeline_config import get_pipeline_config
    from src.kernel.repo_key import to_repo_key

    # P3（Issue #501 深审）：与 /scenes 列表同一会话级 repo 解析（#534），
    # 不再只认显式 repo 参数——避免无 repo 上下文时退化到全局作用域探测错库。
    repo_key = _resolve_scene_repo_key(repo, user)
    # 单场景单行查询（DB-first，走 60s 场景配置缓存），不再全量拉场景列表
    cfg = get_pipeline_config(scene_id, repo_key=repo_key) or {}
    # P1（Issue #501 深审）：get_pipeline_config 第 4 级兜底会静默回退
    # pipelines.json 的 default 场景——不存在的场景 id 会拿别人的节点探测，
    # 返回误导性 sre_unbound 警告。回退（fallback=True 或解析出的 scene_id
    # 与请求不一致）时按「场景不存在」处理：不标记、不拦创建入口。
    if cfg.get("fallback") or (cfg.get("scene_id") and cfg.get("scene_id") != scene_id):
        return {"scene_id": scene_id, "offline_nodes": [], "sre_unbound": False, "all_offline": False}
    cfg_nodes = cfg.get("nodes") or []
    status = _scene_engine_status(scene_id, cfg_nodes, repo_key=repo_key,
                                  user_id=(user or {}).get("id", 0) or 0)
    status["scene_id"] = scene_id
    return status


# ===== Issue #1208 v3：selector 命中预览（前端与后端判定同源同尺）=====
class EngineSelectorPreviewBody(BaseModel):
    tags: list[str] = []
    agent_type: str = ""
    repo: str = ""


@router.post("/engine-selector-preview", operation_id="scenes_engine_selector_preview",
             summary="Preview engine selector matches",
             description="Issue #1208 v3: returns the authoritative hit/miss map for a selector by reusing selector_candidate_engines (same source as enqueue/claim), so the frontend preview never diverges from backend dispatch.")
def engine_selector_preview(body: EngineSelectorPreviewBody,
                            user: dict = Depends(require_no_session_token)):
    """Selector 命中预览：直接复用 selector_candidate_engines 判定（同源同尺）。

    返回 engines：{runtime_id(str): {hit, reason}}，reason ∈
    selector/role/offline/stale/scope/capability（空 selector 时不做约束，由前端全命中处理）。
    """
    from src.domains.runtime.public import selector_candidate_engines
    repo_key = _resolve_repo_key(body.repo) if body.repo else ""
    matched, dropped = selector_candidate_engines(
        {"tags": body.tags or []}, body.agent_type or "",
        repo_key=repo_key, user_id=(user or {}).get("id", 0) or 0)
    engines = {}
    for m in matched:
        engines[str(m["runtime_id"])] = {"hit": True, "reason": ""}
    for d in dropped:
        engines[str(d["runtime_id"])] = {"hit": False, "reason": d.get("reason", "")}
    return {"matched": [m["runtime_id"] for m in matched],
            "dropped": dropped, "engines": engines}


# ===== Scene Market API（场景市场）=====

def _scene_cat_icon(category_id: int, cat_map: dict | None = None) -> str:
    """分类 emoji 兜底图标（Issue #2761 性能）。

    列表页先用 ``list_scene_categories()`` 建一次 map 传进来，避免每行一次
    ``scene_category`` 单条查询（N+1）；map 未命中（例如停用分类）再回落到
    原单条查询，保持既有语义不变。
    """
    cat = None
    if cat_map is not None and category_id:
        try:
            cat = cat_map.get(int(category_id))
        except (TypeError, ValueError):
            cat = None
    if cat is None and category_id:
        from src.domains.mgmt.user.db import get_scene_category
        cat = get_scene_category(category_id)
    return cat.get('icon', '⚙') if cat else '⚙'


def _resolve_scene_icon(icon: str, category_id: int, obs_icon_key: str = "",
                        cat_map: dict | None = None) -> str:
    """解析场景图标为可访问的 URL 或 emoji。
    obs_icon_key 非空 → OBS 模式，动态生成签名 URL。
    icon 非空且是本地路径（/static/ 开头）→ 本地路径。
    icon 非空且是 emoji → 直接返回。
    都为空或 OBS 失败 → 查 scene_category 表拿分类 emoji 图标兜底
    （``cat_map`` 为该请求的预取分类表 id→分类 dict，省掉逐行查询）。
    """
    if obs_icon_key:
        try:
            from src.domains.runtime.public import get_storage
            storage = get_storage()
            if storage is not None:
                return storage.temporary_url(obs_icon_key)
        except Exception:
            pass
        # OBS 失败时先尝试 icon 字段（可能是有效的本地路径/emoji），再 emoji 兜底
        if icon:
            return icon
        return _scene_cat_icon(category_id, cat_map)
    if icon:
        return icon
    return _scene_cat_icon(category_id, cat_map)

def _scene_category_payload(cat_l1_id: int = 0, cat_l2_id: int = 0) -> dict:
    """分类路径 {l1, l2}（Issue #2761）。"""
    from src.domains.mgmt.user.db import category_path
    return category_path(int(cat_l1_id or 0), int(cat_l2_id or 0))


def _scene_tag_suggestions(name: str = "", description: str = "") -> list[str]:
    """规则型标签候选：仅返回真实命中项（最多 3 个）。"""
    return suggest_tags(name, description, candidates=(
        "开发", "运维", "测试", "部署", "自动化", "AI", "数据", "安全", "文档", "监控"))


def _normalize_scene_category(cat_l1_id: int, cat_l2_id: int) -> tuple[int, int]:
    """校验并归一化场景分类（Issue #2761）。

    统一口径见 `_category.normalize_category_pair`（与技能写路径共用）：
    - 一级/二级 ID 必须存在且层级正确（一级 level=1、二级 level=2）；
    - 二级必须属于所给一级（只给二级时由 parent_id 反推一级）。
    """
    from src.domains.mgmt.routes._category import normalize_category_pair
    return normalize_category_pair(cat_l1_id, cat_l2_id)


def _collect_plugin_credential_requirements(nodes: list, user_id: int | str = 0) -> list[dict]:
    """收集场景插件中"凭据未配置"的协商清单（Issue #1089 挂点①，应用场景时弹凭据配置）。

    继承链合并生效（Issue #1089 验收3，A4 #1082 契约 seam）：
    - 插件集合 = merge_plugin_scopes(node=节点级多选, agent=Agent 类型种子默认
      （按节点 tag 读取 agent-description.json agent.plugins，挂点③）)，
      高优先级作用域标记来源；repo/personal 级插件声明源归 A4 #1082；
    - 凭据值 = resolve_plugin_credentials(name, personal=user_credentials
      provider=plugin:{name}) 键级覆盖合并（repo/agent/node 级凭据源暂无
      存储入口，缺省为空 → 等价个人级直取，为后续作用域来源预留统一合并点）。

    - 跨作用域/跨节点去重；未注册插件（插件市场未上架）视为软引用，不阻塞应用；
    - 逐字段对比插件 credentials 声明与合并后配置，给出 missing_fields
      （含可选字段，供弹窗完整展示）与 required_fields。
    """
    from src.domains.mgmt.user.db import get_credential
    from src.domains.mgmt.plugins.registry import get_plugin
    from src.domains.mgmt.plugins.resolver import (
        get_agent_plugins, merge_plugin_scopes, resolve_plugin_credentials)
    node_plugins: list[str] = []
    agent_plugins: list[str] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        for p in node.get("plugins") or []:
            if isinstance(p, str) and p.strip():
                node_plugins.append(p.strip())
        tag = (node.get("tag") or "").strip().lower()
        if tag:
            agent_plugins.extend(get_agent_plugins(tag))
    requirements = []
    for entry in merge_plugin_scopes(node=node_plugins, agent=agent_plugins):
        name = entry["name"]
        plugin = get_plugin(name)
        if not plugin:
            continue  # 软引用：未注册插件不阻塞
        declared = plugin.get("credentials") or []
        if not declared:
            continue
        configured = resolve_plugin_credentials(
            name, personal=get_credential(user_id, f"plugin:{name}") or {})
        missing = [
            {"key": f["key"], "label": f.get("label") or f["key"],
             "required": bool(f.get("required", False))}
            for f in declared
            if not str(configured.get(f["key"], "") or "").strip()
        ]
        if missing:
            requirements.append({
                "plugin": name,
                "display_name": plugin.get("display_name") or name,
                "missing_fields": missing,
                "required_fields": [f["key"] for f in missing if f["required"]],
            })
    return requirements


def _parse_scene_skills(nodes_json: str) -> list[str]:
    """从 nodes_json 提取技能名称列表"""
    try:
        nodes = json.loads(nodes_json) if nodes_json else []
    except Exception:
        return []
    return [n.get("skill", "") for n in nodes if n.get("skill")]

@router.get("/market/categories", operation_id="scenes_market_categories", summary="List scene market categories", description="Scene categories ordered by order_num; only is_active=1.")
def list_scene_categories(user: dict = Depends(require_no_session_token)):
    """场景分类列表：保持旧一级列表（旧分类 tab），并附统一分类树（Issue #2761）。"""
    from src.domains.mgmt.user.db import list_scene_categories as _list_cats
    from src.domains.mgmt.user.db import list_category_tree
    return {"categories": _list_cats(), "tree": list_category_tree("scene")}

@router.get("/market", operation_id="scenes_market_list", summary="List scene market", description="Scene market list with search/category/sort/featured/paging/scope filters. limit>0 skips paging.")
def list_scenes_market(user: dict = Depends(require_no_session_token),
                       q: str = "", category_id: int = 0, sort: str = "latest",
                       scope: str = "", repo: str = "",
                       featured_only: int = 0, limit: int = 0,
                       page: int = Query(default=1, ge=1),
                       page_size: int = Query(default=20, ge=1),
                       cat_l1_id: int = 0, cat_l2_id: int = 0):
    """场景市场列表：支持搜索 + 分类过滤 + 排序 + 精选过滤 + 分页 + scope 过滤。
    limit>0 时不分页（精选场景用），否则按 page/page_size 分页。
    scope: official / repo / org；空=普通市场（不返回 org 系统模板）。
    repo: scope=repo 时按 repo_key 过滤（owner/repo 格式）。
    """
    from src.domains.mgmt.user.db import (list_scene_market, batch_scene_liked,
                             batch_scene_repo_count, list_category_dict,
                             category_path_from_map, list_scene_categories)
    from src.kernel.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    elif scope in ("repo",):
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    scenes, total = list_scene_market(q=q, category_id=category_id, sort=sort,
                               featured_only=featured_only, limit=limit,
                               page=page, page_size=page_size,
                               scope=scope, repo_key=repo_key,
                               user_id=user.get("id", 0),
                               cat_l1_id=cat_l1_id, cat_l2_id=cat_l2_id)
    if not scenes:
        return {"scenes": [], "total": total, "page": page, "page_size": page_size}

    # 批量查 liked_by_me
    scene_ids = [s["id"] for s in scenes]
    liked_map = batch_scene_liked(user.get("id", 0), scene_ids)
    # 批量查 repo_count
    repo_count_map = batch_scene_repo_count(scene_ids)

    try:
        _cat_map = {c["id"]: c for c in list_category_dict("scene")}
    except Exception:
        _cat_map = {}
    # Issue #2761 性能：分类 emoji 兜底一次取全表，替代逐行 scene_category 单条查询（N+1）。
    try:
        _icon_map = {c["id"]: c for c in list_scene_categories()}
    except Exception:
        _icon_map = {}
    result = []
    for s in scenes:
        result.append({
            "id": s["id"],
            "name": s["name"],
            "display_name": s["display_name"],
            "version": s["version"],
            "market_description": s["market_description"],
            "icon": _resolve_scene_icon(s["icon"], s["category_id"], s.get("obs_icon_key", ""),
                                        cat_map=_icon_map),
            "category_id": s["category_id"],
            "cat_l1_id": s.get("cat_l1_id", 0),
            "cat_l2_id": s.get("cat_l2_id", 0),
            "category": category_path_from_map(
                _cat_map, s.get("cat_l1_id", 0), s.get("cat_l2_id", 0)),
            "tags": s.get("tags", [])[:MAX_TAGS],
            "is_featured": s["is_featured"],
            "like_count": s["like_count"],
            "liked_by_me": liked_map.get(s["id"], False),
            "skills": _parse_scene_skills(s["nodes_json"]),
            "recommended_template": s["recommended_template"],
            "repo_count": repo_count_map.get(s["id"], 0),
            "scope": s.get("scope", "official"),
        })
    # 精选场景按 admin 管理台设置的顺序排列
    if featured_only and result:
        try:
            from src.kernel.featured_order import get_featured_order
            _order = get_featured_order("scene")
            if _order:
                _order_map = {sid: i for i, sid in enumerate(_order)}
                result.sort(key=lambda x: _order_map.get(x["id"], 9999))
        except Exception:
            pass
    return {"scenes": result, "total": total, "page": page, "page_size": page_size}

def _pick_node_skill_id(node: dict, ref_map: dict) -> int:
    """节点技能 id 的唯一取法（三档同名防覆盖，2026-11-13）。

    节点自带 skill_id 即用户实际选中的档位（#979 权威）；没有才回退 scene_skill_ref
    （老场景/老前端产物）。ref 表是按名字重建的，可能已被解析成别的档位，
    所以**不能**反过来用它覆盖节点上的 id。
    """
    node = node if isinstance(node, dict) else {}
    try:
        sid = int(node.get("skill_id") or 0)
    except (TypeError, ValueError):
        sid = 0
    if sid:
        return sid
    try:
        return int((ref_map or {}).get(node.get("skill", ""), 0) or 0)
    except (TypeError, ValueError):
        return 0


@router.get("/tag-suggest", operation_id="scenes_tag_suggest",
            summary="Suggest scene tags", description="Rule-based tag candidates (max 3) for scene name/description.")
def suggest_scene_tags(name: str = "", description: str = "",
                       user: dict = Depends(require_no_session_token)):
    return {"tags": _scene_tag_suggestions(name, description)}


@router.get("/market/{market_id}", operation_id="scenes_market_detail", summary="Get market scene detail", description="Scene market detail: full intro + skill chain + recommended template + used repo list.")
def get_scene_market_detail(market_id: int, user: dict = Depends(require_no_session_token)):
    """场景详情：完整介绍 + 技能链路详解 + 推荐模板 + 在用仓库列表"""
    from src.domains.mgmt.user.db import (get_scene_market_by_id, is_scene_liked,
                             get_scene_repos, get_scene_category)
    from src.kernel.session_manager import db, db_lock

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    # 解析 nodes_json，批量查 scene_skill_ref + skill 表（避免 N+1）
    skills_detail = []
    try:
        nodes = json.loads(scene["nodes_json"]) if scene["nodes_json"] else []
    except Exception:
        nodes = []
    # scene_skill_ref 归 mgmt 域（本地连接）；skill 归运行域（owner 连接）——分开取
    with db_lock:
        _c_ref = db.cursor()
        _c_ref.execute(
            "SELECT skill_name, skill_id FROM scene_skill_ref WHERE scene_id=?",
            (scene["id"],),
        )
        ref_map = {r[0]: r[1] for r in _c_ref.fetchall()}

    skill_by_id = {}
    ref_skill_ids = [sid for sid in ref_map.values() if sid]
    if ref_skill_ids:
        for row in _rt_scenes.skills_by_ids(ref_skill_ids):
            skill_by_id[row[0]] = row

    # 组装结果（基于 scene_skill_ref 的 skill_id 精确查询）
    for node in nodes:
        skill_name = node.get("skill", "")
        if not skill_name:
            continue
        skill_id = _pick_node_skill_id(node, ref_map)
        row = skill_by_id.get(skill_id) if skill_id else None
        skills_detail.append({
            "skill_id": (row[0] if row else skill_id),
            "name": skill_name,
            "display_name": (row[2] if row and row[2] else skill_name),
            "agent_type": (row[3] if row else "") or node.get("tag", ""),
            "role_desc": (row[4] if row else "") or "",
            "scope": (row[5] if row else "official") or "official",
            "skill_deleted": not bool(row),
        })

    # 查 recommended_template 对应的模板信息
    template_info = None
    if scene["recommended_template"]:
        try:
            from src.domains.mgmt.routes.templates import _find_template
            ctx = _find_template(scene["recommended_template"])
            if ctx:
                tpl = ctx["template"]
                template_info = {
                    "id": tpl.get("id", ""),
                    "name": tpl.get("name", ""),
                    "description": tpl.get("description", ""),
                    "language": tpl.get("language", "") or tpl.get("tags", [""])[0] if isinstance(tpl.get("tags"), list) else "",
                    "stars": tpl.get("stats", {}).get("stars", 0) if isinstance(tpl.get("stats"), dict) else 0,
                    "source": tpl.get("source", ""),
                    # S103/UC10：下发 source_platform——前端 scene-market.js 模板跳转
                    # 走 _repoWebHost(source_platform+':'+repo) 派生 host 的前提字段
                    "source_platform": tpl.get("source_platform", ""),
                    "source_repo": tpl.get("source_repo", ""),
                    "icon": tpl.get("icon", ""),
                }
        except Exception as e:
            log.warning(f"get_scene_market_detail: template lookup failed: {e}")

    # 查在用仓库列表 + 标记当前用户是否有权限访问
    repos = get_scene_repos(market_id)
    if repos and user.get("id"):
        from src.domains.mgmt.user.db import get_user_repos
        user_repo_names = {r["full_name"] for r in get_user_repos(user["id"])}
        for r in repos:
            r["accessible"] = r.get("full_name", "") in user_repo_names
    else:
        for r in repos:
            r["accessible"] = False

    # 分类名称
    cat = get_scene_category(scene["category_id"])
    category_name = cat["name"] if cat else ""

    return {
        "id": scene["id"],
        "name": scene["name"],
        "display_name": scene["display_name"],
        "version": scene["version"],
        "market_description": scene["market_description"],
        "icon": _resolve_scene_icon(scene["icon"], scene["category_id"], scene.get("obs_icon_key", "")),
        "category_id": scene["category_id"],
        "category_name": category_name,
        "cat_l1_id": scene.get("cat_l1_id", 0),
        "cat_l2_id": scene.get("cat_l2_id", 0),
        "category": _scene_category_payload(scene.get("cat_l1_id", 0), scene.get("cat_l2_id", 0)),
        "tags": (scene.get("tags") or [])[:MAX_TAGS],
        "is_featured": scene["is_featured"],
        "like_count": scene["like_count"],
        "liked_by_me": is_scene_liked(user.get("id", 0), market_id),
        "skills": skills_detail,
        "template": template_info,
        "repos": repos,
        "recommended_template": scene["recommended_template"],
        "scope": scene.get("scope", "official"),
        "nodes_raw": nodes,
        "preset_custom_fields": scene.get("preset_custom_fields", "[]"),
    }

@router.get("/market/{market_id}/applied", operation_id="scenes_market_applied", summary="Check market scene applied", description="Checks whether a market scene is already applied to the current repo: {applied, scene_name}.")
def check_market_scene_applied(market_id: int, user: dict = Depends(require_no_session_token),
                                owner: str = "", repo: str = ""):
    """检查市场场景是否已应用到当前仓库。

    返回: {"applied": bool, "scene_name": str}
    """
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock

    if not owner or not repo:
        return {"applied": False, "scene_name": ""}

    repo_key = _resolve_repo_key(f"{owner}/{repo}")
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT name FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (market_id, repo_key),
        )
        row = c.fetchone()

    return {"applied": bool(row), "scene_name": row[0] if row else ""}

class MarketApplyBody(BaseModel):
    display_name: str = ""

@router.post("/market/{market_id}/apply", operation_id="scenes_market_apply", summary="Apply market scene", description="Applies a market scene to the repo-level pipeline scene. Idempotent per market_scene_id + repo_key. Issue weekly-09: scene_id is a fresh custom-{uuid} per application so multiple users can apply the same template independently; display_name is user-editable (falls back to template display_name).")
def apply_market_scene(market_id: int, user: dict = Depends(require_no_session_token),
                       owner: str = "", repo: str = "", repo_key: str = "",
                       body: MarketApplyBody | None = None):
    """应用场景：从市场复制配置到仓库级 devops_pipeline_scene。

    - scope='repo'（方案 B：场景只与仓库关联，无 user 级）
    - 幂等：同一仓库同一市场场景不重复创建（按 market_scene_id + repo_key 查重）
    - scene_id 用 name 或 custom-{uuid}
    - 同时调 apply_scene 更新当前仓库的 agent 配置
    """
    import uuid
    from src.domains.mgmt.user.db import get_scene_market_by_id
    from src.api.pipeline_config import create_custom_scene, update_custom_scene
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock

    if not owner or not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请先选择仓库"})

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})
    if (scene.get("scope") or "").lower() == "org":
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "ORG_SCENE_PROTECTED",
            "message": "组织流水线模板由组织 provisioning 自动创建，不能从场景市场应用",
        })

    # repo_key：优先用传入的（保留 provider 前缀），否则默认 gitcode:
    if not repo_key:
        repo_key = _resolve_repo_key(f"{owner}/{repo}")
    nodes_json = scene["nodes_json"]
    try:
        nodes = json.loads(nodes_json) if nodes_json else []
    except Exception:
        nodes = []

    # Issue #1758：应用市场模板前复核技能 scope——repo 级场景引用 user 技能
    # 一律拒绝（含创建者本人）。
    _validate_scene_skill_scope(nodes, repo_key)

    # 检查场景引用的 skill 是否都存在（基于 scene_skill_ref 的 skill_id 查 skill 表）
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name, skill_id FROM scene_skill_ref WHERE scene_id=?", (market_id,))
        ref_rows = c.fetchall()
        if ref_rows:
            ref_map = {r[0]: r[1] for r in ref_rows}
            ref_skill_ids = [sid for sid in ref_map.values() if sid]
            existing_ids = set(_rt_scenes.skill_ids_active(ref_skill_ids))
            missing = [name for name, sid in ref_map.items() if sid not in existing_ids]
            if missing:
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"场景引用了已删除的技能: {', '.join(sorted(missing))}，无法应用"})

    # weekly-09 修订 + P3 契约对齐：scope=repo 的场景无 user 维度，同一仓库对同一
    # 市场模板只需一份——OpenAPI 声明的幂等（见下）即为最终语义；market_scene_id
    # 兼作来源标记与幂等键。

    # P3 小项一 契约对齐（OpenAPI 声明 Idempotent per market_scene_id + repo_key）：
    # 同一 market_scene_id + repo_key 已存在活跃场景 → 200 复用既有场景，不重复创建。
    # weekly-09 的"每次应用全新 custom-{uuid}"仍适用于首次应用及不同模板/不同仓库。
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id, name FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (market_id, repo_key),
        )
        existing = c.fetchone()
    if existing:
        log.info(f"Market scene idempotent reuse: market_id={market_id}, scene_id={existing[0]}, repo={repo_key}")
        return {"message": f"场景 '{existing[1]}' 已应用于该仓库，复用既有场景",
                "scene_id": existing[0], "reused": True}

    # display_name 来源：用户输入（应用弹窗可编辑）→ 模板 display_name → 模板 name。
    # weekly-09：每次应用都弹窗让用户确认/改名；撞名 409 让用户改名重试。
    display_name = ((body.display_name if body else "") or "").strip() or (scene["display_name"] or scene["name"])
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT name FROM devops_pipeline_scene "
            "WHERE name=? AND repo_key=? AND is_active=1 LIMIT 1",
            (display_name, repo_key),
        )
        conflict = c.fetchone()
    if conflict:
        # P3 小项一：明确这是「同名冲突」（另一个场景已占用该名称），而非重复 apply 同一市场场景
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"同名冲突：仓库中已存在名为'{display_name}'的场景（非重复应用，重复 apply 同一市场场景会幂等复用），请更换名称后重试", "field": "display_name"})

    # 新建（scope='repo'）。
    # weekly-09：scene_id 不再用模板 name（扁平命名空间会互相覆盖/409 挡路），
    # 每次应用生成独立 custom-{uuid}，多个用户应用同一模板互不影响。
    new_scene_id = f"custom-{uuid.uuid4().hex[:8]}"
    db_id = create_custom_scene(
        scene_id=new_scene_id, name=display_name or new_scene_id,
        description=scene["description"] or "",
        repo_key=repo_key, nodes=nodes, onboarding=scene["onboarding"] or "repo-onboarding",
        created_by=user.get("username", ""))
    scene_id = new_scene_id
    # 回写 market_scene_id 关联（仅来源标记，不再参与幂等判断）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE devops_pipeline_scene SET market_scene_id=? WHERE id=?", (market_id, db_id))
            db.commit()
            log.info(f"apply_market_scene: UPDATE market_scene_id={market_id} WHERE id={db_id}, rowcount={c.rowcount}")
    except Exception as _e:
        log.warning(f"apply_market_scene: link market_scene_id failed: {_e}")

    # 合并预置自定义参数（Issue #246）：仅追加缺失项，不覆盖用户已有配置
    try:
        _merge_preset_custom_fields(scene_id, repo_key, scene.get("preset_custom_fields", "[]"))
    except Exception as _e:
        log.warning(f"apply_market_scene: merge preset custom fields failed: {_e}")

    log.info(f"Market scene applied: market_id={market_id}, scene_id={scene_id}, repo={repo_key}")
    # Issue #1089 挂点①：应用市场场景时按模板 nodes.plugins 协商插件凭据
    try:
        credential_required = _collect_plugin_credential_requirements(nodes, user.get("id", 0))
    except Exception as _e:
        log.warning(f"apply_market_scene: collect plugin credential requirements failed: {_e}")
        credential_required = []
    return {"message": f"场景 '{scene['display_name']}' 已创建", "scene_id": scene_id,
            "credential_required": credential_required}

@router.get("/market/{market_id}/experience-check", operation_id="scenes_market_experience_check", summary="Market scene experience pre-check", description="Pre-check before experiencing: {repo_exists, scene_applied, full_name, scene_name}.")
def check_market_scene_experience(market_id: int, user: dict = Depends(require_no_session_token)):
    """体验前检查：项目仓库是否已存在 + 是否已应用过此场景。

    返回: {"repo_exists": bool, "scene_applied": bool, "full_name": str, "scene_name": str}
    """
    from src.domains.mgmt.user.db import get_scene_market_by_id
    from src.kernel.session_manager import db, db_lock
    from src.kernel.repo_key import to_repo_key
    from src.domains.mgmt.routes.templates import _find_template

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    if not scene["recommended_template"]:
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}

    tpl_ctx = _find_template(scene["recommended_template"])
    if not tpl_ctx:
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}

    tpl = tpl_ctx["template"]
    repo_name = tpl.get("default_repo_name", tpl.get("id", ""))

    # Issue #343 修复: 不吞 401 异常——token 过期时正确抛给前端，不返回假数据
    # 卡驱动：平台 = 卡目录 ∩ 已绑定平台（login_provider 优先），
    # login 走 create_provider(p).get_user_login（无平台名分支）。
    _provider = _scene_user_provider(user)
    if not _provider:
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}
    try:
        user_login = _scene_user_login_via_card(user, _provider)
    except HTTPException:
        raise
    except Exception as _e:
        log.warning(f"experience-check: resolve {_provider} login failed: {_e}")
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}
    if not user_login:
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}

    full_name = f"{user_login}/{repo_name}"
    repo_key = _resolve_repo_key(full_name)
    user_id = user.get("id", 0)

    repo_exists = False
    scene_applied = False

    with db_lock:
        c = db.cursor()
        # 仓库是否已托管
        c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
        if c.fetchone():
            repo_exists = True
        else:
            # user_repos 里有记录也算已存在
            c.execute(
                "SELECT 1 FROM user_repos WHERE user_id=? AND owner=? AND name=? LIMIT 1",
                (user_id, user_login, repo_name),
            )
            if c.fetchone():
                repo_exists = True

        # 该仓库是否已应用过此市场场景
        c.execute(
            "SELECT name FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (market_id, repo_key),
        )
        row = c.fetchone()
        if row:
            scene_applied = True
            scene_name = row[0]
        else:
            scene_name = ""

    return {"repo_exists": repo_exists, "scene_applied": scene_applied,
            "full_name": full_name, "scene_name": scene_name}

@router.post("/market/{market_id}/experience", operation_id="scenes_market_experience", summary="Experience market scene", description="Experiences a scene: creates/reuses the recommended-template project and applies the scene.")
async def experience_market_scene(market_id: int, user: dict = Depends(require_no_session_token)):
    """体验场景：基于场景关联的推荐模板创建/复用项目并应用场景。

    流程：
    1. 查 scene_market 拿 recommended_template + nodes_json + onboarding
    2. 查模板的 default_repo_name + 用户的平台 login（卡目录 ∩ 绑定平台）
    3. 查 user_repos 里是否已有 {平台 login}/{default_repo_name}
       - 已有仓库 → 检查是否已托管，未托管则先托管
       - 没有仓库 → 调 create_from_template 创建+托管
    4. 创建/复用用户级 devops_pipeline_scene（scope='user', scene_id=custom-{uuid}）
    5. 调 apply_scene 应用场景配置到仓库的 agent_session
    6. 返回 {full_name, scene_id, already_exists, message}

    P1-C (Issue #357): External HTTP calls run in thread pool with 10s timeout
    to prevent 124s single-request queue blocking. 平台 login 走 with_oauth_token（预刷新语义不变）。
    """
    import asyncio
    import uuid
    from src.domains.mgmt.user.db import get_scene_market_by_id
    from src.kernel.session_manager import db, db_lock
    from src.kernel.repo_key import to_repo_key
    from src.domains.mgmt.routes.templates import _find_template

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    if not scene["recommended_template"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "该场景暂无体验模板，无法体验"})

    # 拿模板的 default_repo_name
    tpl_ctx = _find_template(scene["recommended_template"])
    if not tpl_ctx:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"模板 '{scene['recommended_template']}' 不存在"})
    tpl = tpl_ctx["template"]
    repo_name = tpl.get("default_repo_name", tpl.get("id", ""))

    # 拿用户的平台 login（卡驱动：平台 = 卡目录 ∩ 已绑定平台，login_provider 优先）
    # P1-1 返工：login 为空按 (401) 抛错，让 with_oauth_token 触发刷新重试。
    # P1-C: Run in thread pool with 10s timeout to avoid blocking event loop.
    _provider = _scene_user_provider(user)
    if not _provider:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "当前账号没有可用的仓库平台：请先在「设置 → 凭据」绑定平台（GitCode/GitHub/Gitee/GitLab 等）",
        })
    try:
        user_login = await asyncio.wait_for(
            asyncio.to_thread(_scene_user_login_via_card, user, _provider),
            timeout=10,
        )
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail={"code": 504, "message": "获取平台用户身份超时，请重试"})
    except _SceneUserIdentityMissing as _e:
        # P3 小项二：身份缺失是客户端状态问题（无可用平台身份），非网关/上游故障
        # ——语义纠正为 409 MISSING_IDENTITY 并附明确指引；上游真故障仍走 502 分支。
        log.warning(f"experience_market_scene: {_provider} identity missing: {_e}")
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "error_code": "MISSING_IDENTITY",
            "message": f"无法获取当前用户 {_provider} 身份：请先在「设置 → 凭据」绑定/重新授权 {_provider} 平台身份后重试",
        })
    except Exception as _e:
        log.warning(f"experience_market_scene: resolve {_provider} login failed: {_e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"无法获取当前用户 {_provider} 身份"})
    if not user_login:
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "error_code": "MISSING_IDENTITY",
            "message": f"无法获取当前用户 {_provider} 身份：请先在「设置 → 凭据」绑定/重新授权 {_provider} 平台身份后重试",
        })

    full_name = f"{user_login}/{repo_name}"
    user_id = user.get("id", 0)
    already_exists = False
    # 托管结果显式回传：host 失败不再被吞成"成功"，前端据此提示并可重新托管
    hosted = False
    host_error = ""

    # 查 managed_repos 里是否已托管
    repo_key = _resolve_repo_key(full_name)
    already_hosted = False
    repo_in_user_repos = False
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
        already_hosted = c.fetchone() is not None
        if not already_hosted:
            c.execute(
                "SELECT 1 FROM user_repos WHERE user_id=? AND owner=? AND name=? LIMIT 1",
                (user_id, user_login, repo_name),
            )
            repo_in_user_repos = c.fetchone() is not None

    if already_hosted:
        # 已托管 → 直接复用
        already_exists = True
        hosted = True
    elif repo_in_user_repos:
        # 仓库已存在（user_repos 有记录）但未托管 → 直接托管
        already_exists = True
        try:
            from src.domains.mgmt.routes.repos import RepoAction, host_repo
            host_repo(RepoAction(full_name=full_name), user)
            hosted = True
        except Exception as e:
            host_error = _host_error_message(e)
            log.warning(f"experience_market_scene: host_repo failed (non-fatal): {e}")
    else:
        # 仓库不存在 → 调 create_from_template（建仓库+推代码+托管）
        # 如果仓库在 GitCode 上存在但 user_repos 未同步，push 可能失败——不视为致命错误
        # P1-C (#357): Wrap external HTTP call with timeout (create_from_template is
        # async but internally calls blocking GitCode API; the timeout caps total
        # wait). #862: raised 30s -> 45s, full pipeline measures ~30s and trips
        # the old gate; see EXPERIENCE_TEMPLATE_CREATE_TIMEOUT above.
        try:
            from src.domains.mgmt.routes.templates import create_from_template, CreateTemplateRequest
            result = await asyncio.wait_for(
                create_from_template(
                    CreateTemplateRequest(template_id=scene["recommended_template"]), user),
                timeout=EXPERIENCE_TEMPLATE_CREATE_TIMEOUT,
            )
            full_name = result.get("full_name", full_name)
            hosted = bool(result.get("webhook_id"))
            if result.get("host_warning"):
                host_error = result.get("host_warning")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail={"code": 504, "message": "创建模板仓库超时，请重试"})
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else (e.detail.get("message", "") if isinstance(e.detail, dict) else str(e.detail))
            # 仓库已存在有内容 / 源模板不存在 / push 失败 → 仓库已创建，继续托管+应用场景
            if "已存在" in detail or "已有内容" in detail or "push" in detail.lower() or "模板代码写入失败" in detail:
                already_exists = True
                log.warning(f"experience_market_scene: template push failed, trying host only: {detail}")
                try:
                    from src.domains.mgmt.routes.repos import RepoAction, host_repo
                    host_repo(RepoAction(full_name=full_name), user)
                    hosted = True
                except Exception as he:
                    host_error = _host_error_message(he)
                    log.warning(f"experience_market_scene: host_repo failed (non-fatal): {he}")
            else:
                raise

    # 创建/复用仓库级 devops_pipeline_scene（只与仓库关联，方案 B）
    # 幂等：同一仓库同一市场场景不重复创建（按 market_scene_id + repo_key 查重）
    experience_repo_key = _resolve_repo_key(full_name) if full_name else ""
    existing_scene_id = ""
    existing_row_id = 0
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id, id FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (market_id, experience_repo_key),
        )
        existing = c.fetchone()
        if existing:
            existing_scene_id = existing[0]
            existing_row_id = existing[1]

    try:
        nodes = json.loads(scene["nodes_json"]) if scene["nodes_json"] else []
    except Exception:
        nodes = []
    nodes_json_str = json.dumps(nodes, ensure_ascii=False)

    # display_name 查重：仓库内不能有同名场景（排除自己）
    display_name = scene["display_name"] or scene["name"]
    with db_lock:
        c = db.cursor()
        if existing_row_id:
            c.execute(
                "SELECT name FROM devops_pipeline_scene "
                "WHERE name=? AND repo_key=? AND is_active=1 AND id!=? LIMIT 1",
                (display_name, experience_repo_key, existing_row_id),
            )
        else:
            c.execute(
                "SELECT name FROM devops_pipeline_scene "
                "WHERE name=? AND repo_key=? AND is_active=1 LIMIT 1",
                (display_name, experience_repo_key),
            )
        conflict = c.fetchone()
    if conflict:
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"仓库中已存在名为'{display_name}'的场景，请先修改已有场景名称后再应用", "full_name": full_name})

    if existing_scene_id:
        # 已有 repo 场景 → 更新
        scene_id = existing_scene_id
        from src.api.pipeline_config import update_custom_scene
        update_custom_scene(
            scene_id=scene_id, repo_key=experience_repo_key, nodes=nodes,
            name=scene["display_name"], description=scene["description"],
        )
    else:
        # 不存在 → 新建
        scene_id = str(uuid.uuid4())
        with db_lock:
            c = db.cursor()
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            c.execute(
                "INSERT INTO devops_pipeline_scene "
                "(scene_id, name, description, repo_key, nodes_json, onboarding, "
                "is_active, created_at, updated_at, market_scene_id) "
                "VALUES (?,?,?,?,?,?,1,?,?,?)",
                (scene_id, scene["display_name"], scene["description"],
                 experience_repo_key, nodes_json_str,
                 scene["onboarding"], now, now, market_id),
            )
            db.commit()

    log.info(f"Market scene experienced: market_id={market_id}, repo={full_name}, "
             f"already_exists={already_exists}, hosted={hosted}, host_error={host_error}")
    msg = f"场景 '{scene['display_name']}' 已创建"
    if already_exists:
        msg = f"已跳转到现有项目 {full_name}，场景 '{scene['display_name']}' 已创建"
    if not hosted:
        # Host 失败不再静默：显式告知仓库已创建但未托管，可到仓库管理页重新托管
        _reason = host_error or "未知原因"
        # create_from_template 的 host_warning 自带「托管配置失败：」前缀，避免二次包装
        if _reason.startswith(_HOST_WARNING_PREFIX):
            _reason = _reason[len(_HOST_WARNING_PREFIX):]
        msg = f"仓库 {full_name} 已创建，托管失败：{_reason}，可在仓库管理页重新托管"
        log.warning(f"experience_market_scene: host not completed for {full_name}: {_reason}")
    return {"full_name": full_name, "scene_id": scene_id,
            "already_exists": already_exists, "hosted": hosted,
            "host_error": host_error, "message": msg}

@router.post("/market/{market_id}/like", operation_id="scenes_market_like_toggle", summary="Toggle scene like", description="Toggles a scene like.")
def toggle_scene_like(market_id: int, user: dict = Depends(require_no_session_token)):
    """场景点赞 toggle：已赞→取消，未赞→点赞"""
    from src.domains.mgmt.user.db import toggle_scene_like as _toggle, get_scene_market_by_id

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    liked, like_count = _toggle(user.get("id", 0), market_id)
    return {"liked": liked, "like_count": like_count}

class CreateBranchRequest(BaseModel):
    branch_name: str
    refs: str = "main"

def _normalize_owner_repo(owner: str, repo: str) -> tuple[str, str]:
    """清洗路径参数中的 provider 前缀污染（如 owner="github:foo"）。

    to_repo_key 对已带冒号的串幂等返回，会掩盖污染并让 DB 查询
    "碰巧"命中；必须在拼外部 API URL 前把前缀剥掉。
    """
    # 前缀集由平台卡目录派生（strip_provider_prefix）——gitee/gitlab 等新平台
    # 与存量双平台同口径，禁止平台名字面量枚举
    from src.kernel.repo_key import strip_provider_prefix
    return strip_provider_prefix(owner), repo

def _detect_repo_provider(owner: str, repo: str) -> str:
    """判断 owner/repo 属于哪个平台：managed_repos 事实源 → user_repos 无歧义命中。

    平台集合唯一权威 = 仓库平台卡目录（list_vcs_platforms），不再按
    gitcode/github 双前缀硬编码（gitee/gitlab 仓同样可判定）。

    两轮干净结构（2026-10-24 修复，生产 github 同名碰撞事故）：
    - 第一轮**只查 managed_repos**（平台托管事实源），全部在册平台都查，命中即返回；
    - 第二轮才查 user_repos，且**同名冲突（多平台都有用户绑定记录）时不返回、
      回退卡目录首个平台**——用户绑定记录没有资格裁决平台归属的歧义。

    旧实现按 provider 外层循环、managed/user 内层交错，导致"user_repos 里 github: 同名
    记录"压过"managed_repos 里 gitcode: 记录"：GitHub 上存在别人建的同名仓（只有一个
    main 分支）时，分支下拉只显示 main。实测（生产 grape-dev/grape，2026-10-24）：
    GitCode 侧 133 个分支被误判成 GitHub 仓库后只剩 main。
    """
    from src.domains.mgmt.user.db import db_lock, db
    from src.kernel.repo_key import to_repo_key
    from src.domains.runtime.public import list_vcs_platforms

    try:
        _provs = list(list_vcs_platforms())
    except Exception:
        _provs = []

    def _hit(table: str, rk: str) -> bool:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(f"SELECT 1 FROM {table} WHERE repo_key=? LIMIT 1", (rk,))
                return bool(c.fetchone())
        except Exception:
            return False

    if not _provs:
        return ""

    # 第一轮：managed_repos（托管事实源，查到即返回）
    for prov in _provs:
        rk = to_repo_key(f"{owner}/{repo}", provider=prov)
        if _hit("managed_repos", rk):
            return prov

    # 第二轮：user_repos（仅无歧义时采用；同名冲突回退卡目录首个平台）
    _hits = [prov for prov in _provs
             if _hit("user_repos", to_repo_key(f"{owner}/{repo}", provider=prov))]
    if len(_hits) == 1:
        return _hits[0]
    return _provs[0]

@router.get("/branches/{owner}/{repo}", operation_id="scenes_branches", summary="List repo branches", description="Lists all repo branches (for the advanced-options work-branch picker). Optional provider/repo_key query param overrides platform detection (2026-10-24: same-name GitHub repos could shadow managed GitCode repos). Issue #542: results are cached in-process for 90s; pass refresh=1 to bypass the cache (response carries a `cached` flag).")
def list_repo_branches(owner: str, repo: str, provider: str = "", repo_key: str = "",
                       refresh: int = Query(0, description="1=绕过缓存强制刷新（手动刷新入口）"),
                       user: dict = Depends(require_no_session_token)):
    """列出仓库所有分支（供高级选项工作分支选择）

    provider/repo_key（可选，2026-10-24 新增）：显式指定平台，跳过猜测逻辑。
    前端托管仓场景应直接传（/repos/managed 返回的 repo_key 自带前缀）。
    Issue #542：TTL 缓存 90s（分支变更低频），refresh=1 强制穿透；仅缓存成功结果。
    """
    from src.kernel.repo_key import parse_repo_key

    owner, repo = _normalize_owner_repo(owner, repo)
    if repo_key:
        # repo_key 优先：自带前缀，直接解析（S85/D60③：统一走 parse_repo_key，
        # 原内联三元把 gitee:/gitlab: 仓折叠成 gitcode）
        _prov = parse_repo_key(repo_key)[0]
        provider = provider or _prov
    # 显式 provider：平台卡在册即采信（不再被双平台白名单打回猜测逻辑，
    # gitee/gitlab 显式传参生效）；否则按托管事实源判定。
    from src.domains.runtime.public import get_platform_card
    if provider and get_platform_card(provider) is not None:
        _detected = provider
    else:
        _detected = _detect_repo_provider(owner, repo)
        if not _detected:
            raise HTTPException(status_code=400, detail={"code": 400,
                    "message": "无法识别仓库所属平台：请通过 provider/repo_key 显式指定"})
    provider = _detected

    # Issue #542：缓存命中直接返回（不穿透外部 API；响应附 cached 标记）
    _cache_key = _branches_cache_key(provider, owner, repo)
    if not refresh:
        _cached = _branches_cache_get(_cache_key)
        if _cached is not None:
            return {**_cached, "cached": True}

    from src.domains.mgmt.user.db import get_repo_token_with_refresh
    from src.kernel.repo_key import to_repo_key
    from src.domains.runtime.public import VcsApiClient
    from src.domains.runtime.public import get_platform_card
    _card = get_platform_card(provider)
    _pname = _card.name if _card is not None else provider
    _rk = to_repo_key(f"{owner}/{repo}", provider=provider)
    # R12.13: get_repo_token_with_refresh（路径 B 仓库返回用户 token，不会 403）
    token = get_repo_token_with_refresh(_rk) or ""
    if not token:
        raise HTTPException(status_code=503, detail={"code": 503, "message": f"{_pname} Bot Token 未配置，无法查询分支"})

    # 卡驱动（VcsApiClient）：dialect 决定路径（gitlab 走 /projects/{o}%2F{r}/repository/branches），
    # api_base/api_auth 来自平台卡；不再有"非 github ⇒ gitcode.com/api/v5"的分叉。
    _client = VcsApiClient(provider, token, owner, repo)
    _path = _client._generic_path(f"/repos/{owner}/{repo}/branches", "/repository/branches")
    branches = []
    try:
        page = 1
        while page <= 5:
            data = _client._card_or_legacy_get(f"{_path}?per_page=100&page={page}")
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"{data.get('error')} {data.get('detail', '')}".strip())
            if not isinstance(data, list) or not data:
                break
            for b in data:
                branches.append({
                    "name": b.get("name", ""),
                    "protected": bool(b.get("protected", False)),
                })
            if len(data) < 100:
                break
            page += 1
    except Exception as e:
        log.warning(f"List branches failed for {_pname} {owner}/{repo}: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"查询分支失败: {e}"})

    payload = {"branches": branches}
    _branches_cache_put(_cache_key, payload)
    return {**payload, "cached": False}

@router.post("/branches/{owner}/{repo}", operation_id="scenes_branch_create", summary="Create repo branch", description="Creates a new branch in the repo. Optional provider/repo_key query param overrides platform detection (2026-10-24).")
def create_repo_branch(owner: str, repo: str, body: CreateBranchRequest,
                        provider: str = "", repo_key: str = "",
                        user: dict = Depends(require_no_session_token)):
    """在仓库中创建新分支

    provider/repo_key（可选，2026-10-24 新增）：显式指定平台，跳过猜测逻辑。
    """
    from src.kernel.repo_key import parse_repo_key

    owner, repo = _normalize_owner_repo(owner, repo)
    if repo_key:
        # S85/D60③：统一走 parse_repo_key（原内联三元折叠非 gitcode 平台）
        _prov = parse_repo_key(repo_key)[0]
        provider = provider or _prov
    # 显式 provider：平台卡在册即采信（不再被双平台白名单打回猜测逻辑，
    # gitee/gitlab 显式传参生效）；否则按托管事实源判定。
    from src.domains.runtime.public import get_platform_card
    if provider and get_platform_card(provider) is not None:
        _detected = provider
    else:
        _detected = _detect_repo_provider(owner, repo)
        if not _detected:
            raise HTTPException(status_code=400, detail={"code": 400,
                    "message": "无法识别仓库所属平台：请通过 provider/repo_key 显式指定"})
    provider = _detected

    from src.domains.mgmt.user.db import get_repo_token_with_refresh
    from src.kernel.repo_key import to_repo_key
    from src.domains.runtime.public import VcsApiClient
    from src.domains.runtime.public import get_platform_card
    _card = get_platform_card(provider)
    _pname = _card.name if _card is not None else provider

    if not body.branch_name or not body.branch_name.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "分支名不能为空"})

    _rk = to_repo_key(f"{owner}/{repo}", provider=provider)
    # R12.13: get_repo_token_with_refresh（路径 B 仓库返回用户 token，不会 403）
    token = get_repo_token_with_refresh(_rk) or ""
    if not token:
        raise HTTPException(status_code=503, detail={"code": 503, "message": f"{_pname} Bot Token 未配置，无法创建分支"})

    # 卡驱动（VcsApiClient.create_branch）：gitlab /projects 形态、refs_sha 形态、
    # form 形态全部由卡能力位（dialect / branch_create_form）决定。
    _client = VcsApiClient(provider, token, owner, repo)
    try:
        result = _client.create_branch(body.branch_name.strip(), body.refs or "main")
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建分支失败: {e}"})
    if isinstance(result, dict) and result.get("error"):
        _code = result.get("status_code") or 0
        _detail = str(result.get("detail") or result.get("error") or "")
        if _code in (409, 422) or "already exists" in _detail or "已存在" in _detail:
            raise HTTPException(status_code=409, detail={"code": 409, "message": "分支已存在"})
        if _code == 403:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "需要维护者权限才能创建分支"})
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建分支失败: {result.get('error')}"})

    log.info(f"Branch created: {body.branch_name} in {_pname} {owner}/{repo}")
    # Issue #542：建分支成功后失效 branches 缓存（新分支立即可见）
    _branches_cache_invalidate(owner, repo)
    return {"name": body.branch_name.strip(), "message": "分支已创建"}


class CreateMarketSceneRequest(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    scope: str = "repo"  # repo
    nodes: list = []
    icon: str = ""
    obs_icon_key: str = ""
    file_id: int = 0  # uploaded_files 表 id（图标临时文件）
    category_id: int = 1
    cat_l1_id: int = 0
    cat_l2_id: int = 0
    tags: Optional[list] = None
    recommended_template: str = ""
    linked_repos: list = []  # [{repo_key, branch}] 关联仓库（跨仓库协同开发）

@router.post("/market", operation_id="scenes_market_create", summary="Create market scene", description="Creates a user custom scene in scene_market. scope=repo: repo scene visible to all repo users.")
def create_market_scene(body: CreateMarketSceneRequest, user: dict = Depends(require_no_session_token),
                        repo: str = ""):
    """创建用户自定义场景（写入 scene_market 表）。

    scope=repo: 仓库场景（仓库所有用户可见）
    """
    import uuid as _uuid
    from src.domains.mgmt.user.db import (create_scene_market, commit_uploaded_file,
                             delete_uploaded_file, get_uploaded_file)
    from src.kernel.repo_key import to_repo_key
    from src.domains.runtime.public import get_storage

    if body.scope not in ("repo",):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 repo"})

    # 获取 repo_key
    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请先选择仓库"})

    # 校验 name 是否重复
    from src.domains.mgmt.user.db import check_scene_name_exists
    if len(body.name) > 40:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景标识不能超过40个字符"})
    if len(body.display_name) > 40:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景名称不能超过40个字符"})
    if check_scene_name_exists(name=body.name, scope=body.scope, repo_key=repo_key):
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 '{body.name}' 已存在"})

    # Issue #955：市场场景写侧同样校验节点 id 角色枚举（market apply 会拷贝
    # 节点进 repo 场景，不能留脏数据入库通道）
    _validate_scene_node_roles(body.nodes)

    # Issue #1564 R1：市场模板为全仓共享配置，保存即强制拒绝 user scope（含创建者本人）
    _validate_scene_skill_scope(body.nodes, repo_key)

    # Issue #2761：一/二级分类必须存在且层级匹配（二级归属一级）
    cat_l1_id, cat_l2_id = _normalize_scene_category(body.cat_l1_id, body.cat_l2_id)

    nodes_json = json.dumps(body.nodes, ensure_ascii=False)
    tag_items, overflow = prepare_tags(body.tags)
    if overflow:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": f"标签最多 {MAX_TAGS} 个，超出：{'、'.join(overflow)}",
            "overflow": overflow})

    try:
        market_id = create_scene_market(
            name=body.name, display_name=body.display_name, description=body.description,
            scope=body.scope, repo_key=repo_key, source_user_id=user.get("id", 0),
            nodes_json=nodes_json, icon=body.icon, obs_icon_key=body.obs_icon_key,
            category_id=body.category_id, recommended_template=body.recommended_template,
            created_by=user.get("username", ""), cat_l1_id=cat_l1_id,
            cat_l2_id=cat_l2_id, tags=encode_tags(tag_items),
        )
    except Exception as e:
        err_upper = str(e).upper()
        if "UNIQUE" in err_upper or "DUPLICATE" in err_upper:
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 '{body.name}' 已存在"})
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})

    # 图标文件落定：临时 key → 正式 key
    if body.file_id:
        final_icon, final_obs_key = _finalize_scene_icon(body.file_id, market_id, body.obs_icon_key, user)
        # 更新 scene_market 的 icon 为正式路径
        if final_icon:
            from src.kernel.session_manager import db, db_lock
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE scene_market SET icon=?, obs_icon_key=? WHERE id=?", (final_icon, final_obs_key, market_id))
                    db.commit()
            except Exception as e:
                log.warning(f"create_market_scene: update icon failed: {e}")

    log.info(f"Market scene created: id={market_id} name={body.name} scope={body.scope} repo={repo_key} by user={user.get('id')}")

    # 重建 scene_skill_ref（仓库场景也需要）
    try:
        from src.domains.runtime.public import _rebuild_scene_skill_ref
        _rebuild_scene_skill_ref(market_id, body.nodes, body.name, repo_key)
    except Exception as e:
        log.warning(f"create_market_scene: rebuild scene_skill_ref failed: {e}")

    return {"id": market_id, "message": "场景创建成功"}

class UpdateMarketSceneRequest(BaseModel):
    name: str = ""
    display_name: str = ""
    description: str = ""
    nodes: list = []
    icon: str = ""
    obs_icon_key: str = ""
    file_id: int = 0  # 新上传的图标临时文件 id（0=未更换图标）
    category_id: int = 0
    cat_l1_id: int = 0
    cat_l2_id: int = 0
    clear_category: bool = False  # Issue #2761：显式清空一/二级分类
    tags: Optional[list] = None
    recommended_template: str = ""
    linked_repos: list = []  # [{repo_key, branch}] 关联仓库（跨仓库协同开发）

@router.put("/market/{market_id}", operation_id="scenes_market_update", summary="Update market scene", description="Updates a custom market scene (scope != official).")
def update_market_scene(market_id: int, body: UpdateMarketSceneRequest,
                        user: dict = Depends(require_no_session_token)):
    """更新用户自定义场景（source != 'official'）。"""
    from src.domains.mgmt.user.db import (update_scene_market, get_scene_market_by_id,
                             delete_uploaded_file, get_uploaded_files_by_ref)

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})
    if scene.get("source") == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方/系统场景不可编辑"})

    # Issue #955：市场场景更新同样校验节点 id 角色枚举（与 create 口径一致）
    if body.nodes:
        _validate_scene_node_roles(body.nodes)

    # Issue #1564 R1：市场模板保存强制拒绝 user scope（含创建者本人）；
    # update 无 repo 入参，repo_key 取市场行自身归属。
    if body.nodes:
        _validate_scene_skill_scope(body.nodes, scene.get("repo_key", ""))

    nodes_json = json.dumps(body.nodes, ensure_ascii=False) if body.nodes else ""

    # 图标文件落定
    if body.file_id:
        # 删除旧图标文件
        old_files = get_uploaded_files_by_ref("scene_market", market_id)
        for old_f in old_files:
            if old_f.get("file_type") == "scene_icon":
                _delete_scene_icon_file(delete_uploaded_file(old_f["id"]))
        # _finalize_scene_icon 只做文件操作，返回正式路径
        final_icon, final_obs_key = _finalize_scene_icon(body.file_id, market_id, body.obs_icon_key, user)
        body.icon = final_icon
        body.obs_icon_key = final_obs_key

    # name 变更时校验重复
    if body.display_name and len(body.display_name) > 40:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景名称不能超过40个字符"})
    if body.name and len(body.name) > 40:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景标识不能超过40个字符"})
    if body.name and body.name != scene.get("name"):
        from src.domains.mgmt.user.db import check_scene_name_exists
        if check_scene_name_exists(
            name=body.name, scope=scene.get("scope", "repo"),
            repo_key=scene.get("repo_key", ""),
            source_user_id=scene.get("source_user_id", 0),
            exclude_id=market_id,
        ):
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 '{body.name}' 已存在"})

    tags_json = None
    if body.tags is not None:
        tag_items, overflow = prepare_tags(body.tags)
        if overflow:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"标签最多 {MAX_TAGS} 个，超出：{'、'.join(overflow)}",
                "overflow": overflow})
        tags_json = encode_tags(tag_items)

    # Issue #2761：分类校验（存在 + 二级归属一级）；clear_category 显式清空
    cat_l1_id, cat_l2_id = (0, 0)
    if not body.clear_category and (body.cat_l1_id or body.cat_l2_id):
        cat_l1_id, cat_l2_id = _normalize_scene_category(body.cat_l1_id, body.cat_l2_id)

    success = update_scene_market(
        market_id=market_id, name=body.name, display_name=body.display_name,
        description=body.description, nodes_json=nodes_json,
        icon=body.icon, obs_icon_key=body.obs_icon_key,
        category_id=body.category_id, recommended_template=body.recommended_template,
        updated_by=user.get("username", ""), cat_l1_id=cat_l1_id,
        cat_l2_id=cat_l2_id, tags=tags_json, clear_category=body.clear_category,
    )
    if not success:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在或无更新"})

    # 重建 scene_skill_ref
    try:
        from src.domains.runtime.public import _rebuild_scene_skill_ref
        _rebuild_scene_skill_ref(market_id, body.nodes, body.name or scene.get("name", ""),
                                  scene.get("repo_key", "") or "")
    except Exception as e:
        log.warning(f"update_market_scene: rebuild scene_skill_ref failed: {e}")

    log.info(f"Market scene updated: id={market_id} by user={user.get('id')}")
    return {"message": "场景更新成功"}

@router.delete("/market/{market_id}", operation_id="scenes_market_delete", summary="Delete market scene", description="Hard-deletes a custom market scene (scope != official).")
def delete_market_scene(market_id: int, user: dict = Depends(require_no_session_token)):
    """硬删除用户自定义场景（source != 'official'）。"""
    from src.domains.mgmt.user.db import (delete_scene_market, get_scene_market_by_id,
                             delete_uploaded_file, get_uploaded_files_by_ref)

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})
    if scene.get("source") == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方/系统场景不可删除"})

    # 删除关联的图标文件
    old_files = get_uploaded_files_by_ref("scene_market", market_id)
    for old_f in old_files:
        if old_f.get("file_type") == "scene_icon":
            _delete_scene_icon_file(delete_uploaded_file(old_f["id"]))

    success = delete_scene_market(market_id)
    if not success:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在或不可删除"})
    log.info(f"Market scene deleted: id={market_id} by user={user.get('id')}")
    return {"message": "场景已删除"}

@router.get("/check-name", operation_id="scenes_check_name", summary="Check scene name availability", description="Checks scene name uniqueness within the same scope/user/repo.")
def check_scene_name(name: str, scope: str, user: dict = Depends(require_no_session_token),
                     repo: str = "", exclude_id: int = 0):
    """检查场景名称是否重复（同 scope 同用户同仓库下名称唯一）。"""
    from src.domains.mgmt.user.db import check_scene_name_exists
    from src.kernel.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    exists = check_scene_name_exists(
        name=name, scope=scope, repo_key=repo_key,
        source_user_id=user.get("id", 0), exclude_id=exclude_id,
    )
    return {"exists": exists}

@router.post("/market/icon-upload", operation_id="scenes_market_icon_upload", summary="Upload scene icon", description="Uploads a scene icon (temporary file). Allowed jpg/png/webp/gif/svg <= 2MB. Returns {file_id, icon, obs_icon_key}.")
async def upload_scene_icon(file: UploadFile = File(...),
                            user: dict = Depends(require_no_session_token)):
    """上传场景图标（临时文件，保存时落定）。

    限制：格式 jpg/png/webp/gif/svg，大小 ≤ 2MB。
    返回 {file_id, icon, obs_icon_key}。
    """
    import uuid as _uuid
    from src.domains.mgmt.user.db import insert_uploaded_file
    from src.domains.runtime.public import get_storage

    # 校验格式
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/svg+xml"}
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
    content_type = file.content_type or ""
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()

    if content_type not in allowed_types and ext not in allowed_exts:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仅支持 jpg/png/webp/gif/svg 格式"})

    # 读取并校验大小
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "图标文件不能超过 2MB"})

    if not ext:
        ext = ".png"  # 兜底

    # 生成临时 key
    tmp_uuid = _uuid.uuid4().hex[:12]
    file_key = f"scene-icons/tmp-{tmp_uuid}{ext}"

    storage = get_storage()
    if storage:
        # OBS 模式
        storage.save_bytes(file_key, content)
        icon_url = storage.temporary_url(file_key)
        obs_icon_key = file_key
    else:
        # 非 OBS 模式：写本地
        static_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "api", "frontend", "shell", "uploads", "scene-icons")
        os.makedirs(static_dir, exist_ok=True)
        local_path = os.path.join(static_dir, f"tmp-{tmp_uuid}{ext}")
        with open(local_path, "wb") as f:
            f.write(content)
        icon_url = f"/static/uploads/scene-icons/tmp-{tmp_uuid}{ext}"
        obs_icon_key = ""

    # 插入 uploaded_files 记录
    file_id = insert_uploaded_file(
        file_key=file_key, original_name=filename,
        file_size=len(content), mime_type=content_type,
        file_type="scene_icon", created_by=user.get("username", ""),
    )

    return {"file_id": file_id, "icon": icon_url, "obs_icon_key": obs_icon_key}

def _finalize_scene_icon(file_id: int, market_id: int, obs_icon_key: str, user: dict) -> tuple:
    """将临时图标文件落定为正式文件（重命名 key + 更新 uploaded_files + 更新 scene_market icon 字段）。"""
    from src.domains.mgmt.user.db import (get_uploaded_file, commit_uploaded_file,
                             delete_uploaded_file)
    from src.domains.runtime.public import get_storage
    from src.kernel.session_manager import db, db_lock

    file_rec = get_uploaded_file(file_id)
    if not file_rec:
        return "", ""

    old_key = file_rec["file_key"]
    ext = os.path.splitext(old_key)[1] or ".png"
    new_key = f"scene-icons/{market_id}{ext}"

    storage = get_storage()
    if storage:
        # OBS 模式：copy 到正式 key + 删临时 key
        try:
            content = storage.read_bytes(old_key)
            if content:
                storage.save_bytes(new_key, content)
                storage.delete_object(old_key)
        except Exception as e:
            log.warning(f"Scene icon finalize (OBS): {e}")
        # 更新 scene_market 的 icon 和 obs_icon_key
        final_icon = storage.temporary_url(new_key) if hasattr(storage, 'temporary_url') else ""
        final_obs_key = new_key
    else:
        # 非 OBS 模式：重命名本地文件
        static_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "api", "frontend", "shell", "uploads", "scene-icons")
        old_path = os.path.join(static_dir, os.path.basename(old_key))
        new_path = os.path.join(static_dir, f"{market_id}{ext}")
        try:
            if os.path.exists(old_path):
                os.rename(old_path, new_path)
        except Exception as e:
            log.warning(f"Scene icon finalize (local): {e}")
        # 更新 scene_market 的 icon 为正式本地路径
        final_icon = f"/static/uploads/scene-icons/{market_id}{ext}"
        final_obs_key = ""

    commit_uploaded_file(file_id, "scene_market", market_id)

    # 返回正式路径，由调用方设置到 body 让 update_scene_market 统一更新
    return final_icon, final_obs_key

def _delete_scene_icon_file(file_key: str):
    """删除图标物理文件（OBS 或本地）。"""
    if not file_key:
        return
    from src.domains.runtime.public import get_storage

    storage = get_storage()
    if storage:
        try:
            storage.delete_object(file_key)
        except Exception as e:
            log.warning(f"Scene icon delete (OBS): {e}")
    else:
        # 非 OBS 模式
        static_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "api", "frontend", "shell", "uploads", "scene-icons")
        local_path = os.path.join(static_dir, os.path.basename(file_key))
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception as e:
            log.warning(f"Scene icon delete (local): {e}")

@router.get("/skill-scope-audit", operation_id="scenes_skill_scope_audit", summary="Audit scene skill scope", description="Read-only audit (Issue #1564/#1721): scans active repo scenes and reports nodes whose skill scope conflicts (user-scope skills referenced by repo-shared scenes). Accepts session token (tk_) restricted to its bound repo, read-only. Returns violations[{scene_id,scene_name,node_id,node_tag,skill,actual_scope,expected_scope,suggested_skills}] plus summary counts; never writes.")
def audit_scene_skill_scope(repo: str = "", limit: int = Query(default=200, ge=1, le=1000),
                            user: dict = Depends(get_current_user_or_api_key)):
    """存量场景技能 scope 只读体检（Issue #1564 R5 / #1721）。

    遍历该仓库 is_active=1 的 devops_pipeline_scene，逐节点校验技能 scope，
    输出冲突清单与汇总计数。只读，不改写任何数据；维护者可按清单逐个修正。
    审计口径取严格模式（user scope 一律计冲突），因为场景为全仓共享配置，
    他人应用/派发时必然无法解析。

    Issue #1721：允许会话级 token（tk_）只读体检——限定其绑定 repo_key，
    显式指向其它仓库一律 403；API Key / JWT 维持既有语义。
    """
    if (user or {}).get("auth_type") == "session_token":
        _bound_repo_key = (user or {}).get("repo_key") or ""
        if not _bound_repo_key:
            raise HTTPException(status_code=403, detail={"code": 403,
                                                         "message": "会话 token 仅可体检绑定仓库的技能 scope"})
        if repo:
            _explicit_repo_key = _resolve_repo_key(repo)
            if _explicit_repo_key != _bound_repo_key:
                raise HTTPException(status_code=403, detail={"code": 403,
                                                             "message": "会话 token 仅可体检本仓技能 scope，越权仓库已拒绝"})
            repo_key = _explicit_repo_key
        else:
            repo_key = _bound_repo_key
    else:
        repo_key = _resolve_scene_repo_key(repo, user)
    if not repo_key:
        raise HTTPException(status_code=400,
                            detail={"code": 400, "message": "请先选择仓库"})
    if not isinstance(limit, int) or limit < 1:
        limit = 200
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT scene_id, name, display_name, nodes_json FROM devops_pipeline_scene "
                "WHERE repo_key=? AND is_active=1 ORDER BY id LIMIT ?",
                (repo_key, limit))
            rows = c.fetchall()
        except Exception as e:
            # 仅对旧库缺 display_name 列做定向兜底；其余 DB 异常照常上抛，避免被吞成空结果
            if "no such column" not in str(e).lower():
                raise
            c.execute(
                "SELECT scene_id, name, nodes_json FROM devops_pipeline_scene "
                "WHERE repo_key=? AND is_active=1 ORDER BY id LIMIT ?",
                (repo_key, limit))
            rows = [(r[0], r[1], "", r[2]) for r in c.fetchall()]
    violations = []
    conflict_scenes = set()
    for scene_id, name, display_name, nodes_json in rows:
        try:
            nodes = json.loads(nodes_json) if nodes_json else []
        except Exception:
            nodes = []
        scene_name = display_name or name or scene_id
        for v in _collect_scene_skill_scope_violations(nodes, repo_key):
            item = _project_scene_scope_violation(v)
            item["scene_id"] = scene_id
            item["scene_name"] = scene_name
            item["suggested_skills"] = list(v.get("rebind_candidates") or [])
            violations.append(item)
            conflict_scenes.add(scene_id)
    return {
        "repo": repo_key,
        "read_only": True,
        "checked_scenes": len(rows),
        "conflict_scenes": len(conflict_scenes),
        "total_conflicts": len(violations),
        "violations": violations,
        "note": (
            "只读体检：下列场景节点引用了 user 级技能，应用/派发时可能无法解析。"
            "请改用 repo/official 级技能后重新保存场景；本接口不会改写任何数据。"
            "suggested_skills 仅为改绑指引，需仓库管理员显式改绑。"
        ) if violations else "只读体检：未发现技能 scope 冲突。",
    }

def _scene_node_scope_check(node: dict, repo_key: str, scene_id: str = "",
                            cache: dict | None = None) -> dict:
    """单节点技能可解析性前置判定（Issue #1721 / #1758）。

    判定统一委托给 ``scene_skill_scope.evaluate``：显式 skill_id 仍需 scope
    校验（user scope 即使由本人显式绑定也不放行），失效 id 回退按名解析，
    查询异常按统一 fail-closed 策略处理。detail 只做结果投影，不再 import
    其它模块的私有判定函数。
    返回 {resolvable, scope_check{ok,skill,actual_scope,expected_scope,reason}}。
    `cache` 可选，用于同一响应内按 skill_id/技能名去重，避免重复查询。
    """
    from src.api.scene_skill_scope import evaluate
    result = evaluate(node, repo_key, "repo", scene_id=scene_id,
                      cache=cache)
    return {
        "resolvable": bool(result["ok"]),
        "scope_check": {
            "ok": bool(result["ok"]),
            "skill": result.get("skill") or "",
            "actual_scope": result.get("actual_scope") or "",
            "expected_scope": result.get("expected_scope") or "repo",
            "reason": result.get("reason") or "",
        },
    }

def _attach_scene_node_scope_checks(nodes: list, repo_key: str,
                                    scene_id: str = "") -> list:
    """为 scenes detail 节点附加 resolvable/scope_check（Issue #1721，纯增量）。

    复制每个节点后附加字段，确保不修改 get_pipeline_config 的缓存对象。
    同一响应内按 skill_id/技能名缓存判定结果，避免重复节点触发多次查询。
    """
    result = []
    _cache = {}
    for node in nodes or []:
        if not isinstance(node, dict):
            result.append(node)
            continue
        item = dict(node)
        item.update(_scene_node_scope_check(node, repo_key, scene_id, cache=_cache))
        result.append(item)
    return result


@router.get("/{scene_id}", operation_id="scenes_detail", summary="Get scene detail", description="Scene detail with nodes + engine info. Dual: user (JWT/API key) and session token (tk_, agent reads its current scene pipeline on demand). Issue #1721/#1758: each node additionally exposes resolvable (bool) and scope_check{ok,skill,actual_scope,expected_scope,reason}; resolvable=false means the node skill conflicts with the repo scene scope under the single judge (scene_skill_scope.evaluate): a user-scope skill is rejected even when explicitly bound by an active skill_id or owned by the caller, and an invalid/inactive skill_id falls back to name resolution. Re-bind the node to a repo/official skill before dispatch.")
def get_scene_detail(scene_id: str, repo: str = "", user: dict = Depends(get_current_user_or_api_key)):
    """返回场景详情（nodes + engine 信息）。

    Issue #533/#534: 未显式传 repo 时按会话上下文解析场景归属——
    会话级 token（tk_）优先取其绑定 repo_key，其次回落请求中间件的 repo 上下文；
    显式 --repo 优先、行为不变。解析出的 repo_key 仍走 get_pipeline_config
    既有 repo 作用域归属校验，权限不放松。
    Issue #502 读侧修复（weekly-09 侧已在）：
    - 解析不到时返回 404，不再静默回退默认场景并回显请求 id；
    - 命中 pipelines.json default 兜底（cfg.fallback）同样视为未解析到，禁止静默回退；
    - 响应 id 恒为解析到的真实 scene_id（cfg['scene_id']），不回显请求值。

    会话级补充（fix: agent runbook 场景核验 404 断链）：
    - ?repo= 为空且认证为会话 token（tk_）时，用 token 绑定的 repo_key（session_api_tokens
      行自带，格式 "gitcode:owner/repo"）——agent 的 `grape scenes detail` 未传 --repo
      也能解析到本仓库的仓库级场景副本，不再 404；
    - JWT / grape_ Key（用户侧调用）行为不变：仍需显式 ?repo=，解析不到照旧 404。
    """
    from src.api.pipeline_config import get_pipeline_config
    repo_key = _resolve_scene_repo_key(repo, user)
    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not repo and user and user.get("auth_type") == "session_token":
        # Issue #568: 未显式传 repo 的会话 token，repo 作用域未命中该 scene_id 的 DB 行
        # （cfg None，或来自 market/pipelines 兜底而非该 scene_id 本身）→ 会话绑定
        # （issue 绑定场景）二级解析，与 a2a handoff 的场景可见性口径对齐；
        # 未命中回落原解析结果（含既有兜底/404 行为），权限不放松
        if cfg is None or not _scene_row_exists(scene_id, repo_key):
            cfg = _resolve_session_bound_scene(scene_id, user) or cfg
    if not cfg:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在（跨仓库场景请附 --repo <owner/repo> 后重试）"})
    if cfg.get("fallback"):
        # 命中 pipelines.json default 兜底（与请求 scene_id 不一致）→ 视为未解析到，禁止静默回退
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})
    resolved_id = cfg.get("scene_id") or scene_id
    # Issue #1721：节点级技能可解析性前置暴露（与派发侧同一判定口径：显式 skill_id
    # 有效即授权，否则按技能名判定），纯增量字段；复制节点后附加，避免污染
    # get_pipeline_config 的进程级缓存。
    nodes = _attach_scene_node_scope_checks(
        cfg.get("nodes") or [], repo_key, resolved_id)
    return {"id": resolved_id, "name": cfg["name"], "nodes": nodes,
            "display_name": cfg.get("display_name") or cfg["name"],
            "onboarding": cfg.get("onboarding", ""),
            "scene_type": cfg.get("scene_type", "standard")}
@router.get("/skills/{agent_type}", operation_id="scenes_skills_for_agent", summary="List skills for agent type", description="Lists selectable skills for an agent type (search + scope filter + paging). Response contract: each skill item exposes (skill_id, scope, skill_name) which together form the unique selection key for frontend consumers — official/repo/user scoped skills may share the same skill_name, so skill_name alone MUST NOT be used as a selection/cache key.")
def list_skills_for_agent(agent_type: str, user: dict = Depends(require_no_session_token),
                          q: str = "", scope: str = "", repo: str = "",
                          page: int = Query(1, ge=1, description="页码，从 1 开始"),
                          page_size: int = Query(20, ge=1, le=200, description="每页条数，1-200")):
    """返回该 agent_type 可选的 skill 列表（职责过滤 + 搜索 + scope 过滤 + 分页）。

    职责过滤口径：只返回 agent_type 相同的技能，以及 agent_type 为空的历史通用技能；
    过滤在 owner 域（scene_skill_search）的 SQL 里做——原实现造了 where/params 却从未使用，
    导致任意角色都能选到其它角色的技能（绑定后技能会跑在错误的节点里）。
    """
    from src.kernel.session_manager import db, db_lock
    skills = []

    # skill 可见范围：official + 个人 user skill + 当前仓库的 repo skill
    repo_key = ""
    if repo:
        from src.kernel.repo_key import to_repo_key
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                from src.kernel.repo_key import to_repo_key
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass
    user_id = str(user.get("id", ""))
    total, _skill_rows = _rt_scenes.scene_skill_search(
        user_id, repo_key, q=q or "", scope=scope or "", page=page, page_size=page_size,
        agent_type=(agent_type or "").strip().lower())
    for r in _skill_rows:
        # Issue #978 契约固化：skill 表 UNIQUE(skill_name, scope, owner_id) 允许
        # official/repo/user 三档同名技能共存；本响应中 (skill_id, scope, skill_name)
        # 共同构成前端选择的唯一键（skill_id 优先，缺失时回退 (scope, skill_name)）。
        # 前端（dashboard.js 技能下拉）不得再用裸 skill_name 作选中/缓存 key。
        skills.append({"skill_id": r[0], "skill_name": r[1], "display_name": r[2] or r[1], "scope": r[3], "owner_id": r[4] or "", "description": r[5] or "", "agent_type": r[6] or ""})
    return {"skills": skills, "total": total}

class SceneNodeInput(BaseModel):
    id: str
    tag: str = ""
    skill: str = ""
    skill_id: int = 0  # Issue #979：权威技能标识（三档同名派发锁定）；0=仅名字（老场景，派发按名降级）
    skill_pending: str = ""  # Issue #1844：复制时技能 scope 校验不通过，保留原名待重选
    mode: str = "emit"
    next: str = "done"
    handoff: str = "a2a"
    skip: list = []
    enabled: bool = True
    engine_runtime_ids: list = []
    plugins: list = []  # Issue #1089 挂点①：节点级插件多选（软引用，应用场景时协商凭据）
    # Issue #1208：uid 池（D12 姊妹字段，与 ids 双非空拒绝）+ selector 标签池
    # （三模式同 node 互斥，D7；保存端为权威校验点）
    engine_runtime_uids: list = []
    engine_selector: dict = {}

class CreateSceneRequest(BaseModel):
    scene_id: str = ""
    name: str
    description: str = ""
    scope: str = "repo"  # repo
    nodes: list[SceneNodeInput] = []
    icon: str = ""
    scene_type: str = "standard"  # deprecated, 保留向后兼容
    pr_subscription_enabled: bool = False
    linked_repos: list = []  # [{repo_key, branch}] 关联仓库（跨仓库协同开发）

class UpdateSceneRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    nodes: list[SceneNodeInput] = []
    icon: str | None = None
    pr_subscription_enabled: bool | None = None
    linked_repos: list | None = None  # None=不更新；[] = 清空关联仓库

class ApplySceneRequest(BaseModel):
    scene_id: str
    strict: bool = False  # Issue #524: true=任一节点 skipped（声明引擎不健康）时整单拒绝；默认部分应用

def _validate_scene_node_roles(nodes: list):
    """Issue #955：场景节点 id（角色名）枚举校验。

    节点 id 必须属于已知 Agent 角色全集（engine_select.KNOWN_AGENT_ROLES，
    含 chat），非法值 → 400（报错含非法值与合法全集），防止非标准角色名落库
    导致派发引擎绑定静默失效（读侧按 id 优先匹配）。tag 保留为展示用阶段名
    （可为中文等任意展示值），不参与角色匹配，存量场景不受影响。

    Spec 注明（Issue #955）：空 id / 非 dict 节点静默放行——为兼容存量
    tag-only 节点与历史数据（此类节点无角色名可校验，读侧走 tag 兜底）。
    """
    from src.domains.runtime.public import known_agent_roles
    _roles = known_agent_roles()
    for node in nodes:
        _id = (node.get("id") or "").strip() if isinstance(node, dict) else ""
        if _id and _id not in _roles:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"节点角色非法：'{_id}'（节点 id 必须为角色名），"
                           f"合法角色：{', '.join(sorted(_roles))}"})

def _selector_scene_values() -> list:
    """Issue #1208：scene: 维度值域（动态查 scene_market 活跃行，空回退 default）。

    值域动态非白名单（D17）：场景市场新增活跃场景名即时合法；查询失败降级
    ["default"]（引擎侧 scene_tags 空回退同口径）。
    """
    try:
        from src.kernel.bulk_read import reader
        with reader() as c:
            rows = c.execute("SELECT DISTINCT name FROM scene_market WHERE is_active=1").fetchall()
        vals = sorted({str(r[0]).strip().lower() for r in rows if r and str(r[0] or "").strip()})
        return vals + ["default"] if "default" not in vals else vals
    except Exception as e:
        log.warning(f"_selector_scene_values: scene_market lookup failed: {e}")
        return ["default"]


def _validate_scene_engine_ids(nodes: list, repo_key: str, username: str = ""):
    """校验场景 nodes 的引擎绑定（Issue #1208 扩展：三模式互斥 + selector L1）：
    1. 引擎 ID 必须存在（失效 ID 优雅降级——跳过而非报错，Issue #250）
    2. scope=repo 的引擎 owner_id 必须匹配当前仓库
    3. 引擎必须有对应节点角色的能力标签（engine_capabilities.tag 或 'all' 通配，
       Issue #955 follow-up：恢复 'all' 通配——存量引擎仅登记 'all' 一行，严格匹配
       会误杀其场景引擎配置（实证：用户场景保存 sre 节点全量被拒）；#955 的节点
       id 角色枚举校验保留，仅推翻"堵通配"这一点）
    4. SRE 节点：引擎必须为个人级或仓库级（scope=user 且 owner_id==当前用户，或
       scope=repo 且归属当前仓库——repo 归属由下方通用校验覆盖）
       （sre-repo-worker-relax §1.2，取代 sre-personal-machine §3.7 Q2/Q3）
    5. SRE 节点 enabled 且三模式绑定全空 → 400（SRE 空 ≠ 广播，= 无引擎无法派发）
    6. Issue #1208 D7：三模式同 node 互斥——ids/uids 双非空 400；
       selector 与 ids/uids 任一非空双非空 400（保存端双非空拒绝）
    7. Issue #1208 D13 L1：selector 标签语法/值域硬阻断 400（未知前缀文案指引 label:；
       engine/scope 固定值域；scene 动态查 scene_market 活跃行）
    8. SRE 节点 selector 模式：tags 必须含 scope:user 或 scope:repo → 否则 400
       （claim 侧没有 SRE 专属 scope 层，边界只由 selector 的 scope 标签承担；与前端
       SRE 弹窗只给「个人/仓库」两档对齐）

    校验失败抛 HTTPException(400)；失效 runtime_id 跳过（优雅降级）。
    """
    from src.kernel.session_manager import db, db_lock
    from src.kernel.repo_key import from_repo_key
    from src.domains.runtime.public import node_agent_role, validate_selector_tags
    _repo_norm = (from_repo_key(repo_key) or "").strip()
    for node in nodes:
        # Issue #955：节点 id 即角色名（id 优先、tag 兜底——兼容历史仅含 tag 的纯函数调用）
        tag = node_agent_role(node)
        eids = node.get("engine_runtime_ids") or []
        uids = [str(u or "").strip() for u in (node.get("engine_runtime_uids") or [])
                if str(u or "").strip()]
        selector = node.get("engine_selector") or {}
        _sel_tags = (selector.get("tags") or []) if isinstance(selector, dict) else []
        if not tag:
            continue
        # Issue #1208 D7：三模式互斥（保存端双非空拒绝，先于一切逐项校验）
        # 迁移遗留双写归一（202611050002 升格保留 ids 整改）：uids 全部可解析且解析出的
        # runtime_id 集合与 ids 一致 → 视为 T11 升格遗留双写，uids 为权威、忽略 ids 继续
        # 校验（不 400）；集合不一致 / uid 不可解析 → 用户显式双轨，维持 400。
        if eids and uids:
            _legacy_dual = False
            if not _sel_tags:
                try:
                    from src.kernel.engine_uid import is_uid_str as _is_uid
                    from src.domains.mgmt.user.db import get_runtime_id_by_engine_uid as _rid_of
                    _uid_rids = set()
                    for _u in uids:
                        if not _is_uid(_u):
                            break
                        _r = _rid_of(_u.lower())
                        if not _r:
                            break
                        _uid_rids.add(int(_r))
                    else:
                        try:
                            _eid_set = {int(e) for e in eids}
                        except (ValueError, TypeError):
                            _eid_set = None
                        _legacy_dual = _eid_set is not None and _uid_rids == _eid_set
                except Exception as _le:
                    log.warning(f"_validate_scene_engine_ids: legacy dual-write probe failed: {_le}")
                    _legacy_dual = False
            if _legacy_dual:
                log.info(f"节点 {tag} 检测到 T11 升格遗留 ids/uids 双写（集合一致），"
                         f"按 uids 权威归一后继续校验")
                eids = []
            else:
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"节点 {tag} 的 engine_runtime_ids 与 engine_runtime_uids 不可同时非空"
                               f"（三模式互斥，请二选一保存）"})
        if _sel_tags and (eids or uids):
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"节点 {tag} 的 engine_selector 与引擎清单绑定不可同时非空"
                           f"（按标签筛选 / 按引擎选择二选一，切换模式将清空另一模式）"})
        # Issue #1208 D13 L1：selector 语法/值域硬阻断（合法值列表随 400 返回）
        if _sel_tags:
            _err = validate_selector_tags(_sel_tags, scene_values=_selector_scene_values())
            if _err:
                raise HTTPException(status_code=400, detail={"code": 400, "message": _err})
        # SRE 空 = 无引擎：启用状态下不允许保存（Q2/Q5 裁定；selector 非空视为已绑定）
        if tag == "sre" and node.get("enabled") is not False and not eids and not uids and not _sel_tags:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": "SRE Agent 不支持广播模式，请选择个人或仓库级机器（引擎列表不能为空）"})
        # SRE 的 user/repo 边界在 claim 侧没有独立一层（_DEFAULT_ENGINE_ROLE_TAGS 空表、
        # claim_by_tag 无 sre/scope 层、_selector_guard_blocks 只做标签 AND 匹配），实际边界
        # 完全落在 selector 的 scope 标签上：不带 scope 的 selector 会被 global 档引擎认领。
        # 保存端在此收紧，与前端 SRE 弹窗只给「个人/仓库」两档对齐（早于派发/认领，成本最低）。
        if tag == "sre" and node.get("enabled") is not False and _sel_tags:
            _sre_sel_scopes = {str(t or "").strip().lower() for t in _sel_tags}
            if not (_sre_sel_scopes & {"scope:user", "scope:repo"}):
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": "SRE Agent 的引擎筛选必须包含 scope:user 或 scope:repo"
                               "（SRE 禁止使用全局引擎，缺少 scope 标签的筛选会被全局引擎认领）"})
        if not eids and not uids:
            continue
        # Issue #646：绑定入口支持 uid（字符串）——先解析为 id（服务端校验），
        # 无效 uid fail-fast 400（防手抄拼错静默丢失）；数字 id 维持原语义。
        from src.kernel.engine_uid import is_uid_str
        _resolved_eids = []
        for eid in eids:
            if isinstance(eid, str) and is_uid_str(eid.strip()):
                from src.domains.mgmt.user.db import get_runtime_id_by_engine_uid
                _rid = get_runtime_id_by_engine_uid(eid.strip().lower())
                if not _rid:
                    raise HTTPException(status_code=400, detail={
                        "code": 400,
                        "message": f"节点 {tag} 绑定的引擎 uid 无效: {eid.strip()}（请从引擎详情面板复制有效 uid）"})
                _resolved_eids.append(_rid)
            else:
                _resolved_eids.append(eid)
        # Issue #1208：显式 uids 逐个校验存在性（fail-fast 同 ids 口径），
        # 解析出的 runtime_id 并入 ids 同款校验循环（scope/SRE/能力校验不因 uid 形态绕过）
        for u in uids:
            if is_uid_str(u):
                from src.domains.mgmt.user.db import get_runtime_id_by_engine_uid
                _uid_rid = get_runtime_id_by_engine_uid(u.lower())
                if not _uid_rid:
                    raise HTTPException(status_code=400, detail={
                        "code": 400,
                        "message": f"节点 {tag} 绑定的引擎 uid 无效: {u}（请从引擎详情面板复制有效 uid）"})
                _resolved_eids.append(_uid_rid)
            else:
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"节点 {tag} 绑定的引擎 uid 无效: {u}（请从引擎详情面板复制有效 uid）"})
        eids = _resolved_eids
        for eid in eids:
            try:
                _eid = int(eid)
            except (ValueError, TypeError):
                continue
            with db_lock:
                # 引擎行 + 凭据 scope/owner_id 在运行域一次取回（两表同属 runtime）
                row = _rt_scenes.runtime_with_credential(_eid)
            if not row:
                # Issue #250: 失效 runtime_id 优雅降级——跳过而非 400
                log.warning(f"_validate_scene_engine_ids: 节点 {tag} 的引擎 ID {_eid} 不存在（已跳过）")
                continue
            _cred_id, _scope, _owner_id = row[0] or 0, row[1] or "global", row[2] or ""
            # SRE 专属（sre-repo-worker-relax §1.2）：引擎必须为 user 档（且属于当前用户）
            # 或 repo 档（归属校验由下方通用 repo 检查覆盖）；global/local 不放行
            if tag == "sre":
                if _scope not in ("user", "repo"):
                    raise HTTPException(status_code=400, detail={
                        "code": 400,
                        "message": f"SRE Agent 只能使用个人或仓库级机器，引擎 {_eid} 不是个人/仓库级（scope={_scope}）"})
                if _scope == "user" and _owner_id and username and _owner_id != username:
                    raise HTTPException(status_code=400, detail={
                        "code": 400,
                        "message": f"SRE Agent 只能使用当前用户自己的个人机器，引擎 {_eid} 属于 {_owner_id}"})
            # scope=repo 时校验仓库归属
            if _scope == "repo" and _cred_id > 0:
                _owner_norm = (from_repo_key(_owner_id) or "").strip()
                if _owner_norm and _repo_norm and _owner_norm != _repo_norm:
                    raise HTTPException(status_code=400, detail={
                        "code": 400,
                        "message": f"节点 {tag} 的引擎 {_eid} 属于仓库 {_owner_norm}，不是当前仓库 {_repo_norm}"})
            # 校验角色能力（Issue #955 follow-up：按节点角色匹配，'all' 通配放行——
            # 存量引擎能力表仅 'all' 行，严格匹配误杀，恢复宽匹配待角色数据面补齐后再收紧）
            # P4：删视图后能力校验走 lib 读（白名单查询 capability_tag_hit）
            _cap_hit = _rt_scenes.runtime_capability_match(_eid, tag)
            if not _cap_hit:
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"节点 {tag} 的引擎 {_eid} 没有该角色能力标签（{tag}），请重新选择"})


def _upgrade_scene_nodes_uid(nodes: list):
    """Issue #1208 D11 保存端透明升格：绑定值一律写 engine_runtime_uids 并清空 ids。

    - selector 节点不动（三模式互斥，selector 为权威）；
    - 全部 id 均可解析 uid → 写 uids + 清 ids（前端回读 uid 形态，单向过渡不回退）；
    - 任一 id 悬空（引擎行不存在/uid 缺失）→ 保留原 ids 不猜不静默（与 T11 脚本
      悬空跳过口径一致，悬空清理由 detach 链负责）。
    纯内存变换（就地改 nodes），调用方在校验通过后、落库前调用。
    """
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        eids = node.get("engine_runtime_ids") or []
        uids = node.get("engine_runtime_uids") or []
        if (node.get("engine_selector") or {}).get("tags"):
            continue
        if uids:
            # T11 升格遗留双写归一：uids 为权威，清空 ids（uids 形态节点保存后落库归一）
            if eids:
                node["engine_runtime_ids"] = []
            continue
        try:
            from src.domains.mgmt.user.db import get_engine_uid_by_runtime_id
            resolved = [get_engine_uid_by_runtime_id(int(x)) for x in eids]
        except (TypeError, ValueError):
            continue
        if not resolved or any(not u for u in resolved):
            # 悬空 id：保留原 ids（不猜不静默；引擎缺失属 detach/清理链职责）
            continue
        node["engine_runtime_uids"] = [str(u).strip().lower() for u in resolved if u]
        node["engine_runtime_ids"] = []


@router.post("", operation_id="scenes_create", summary="Create scene", description="Creates a custom scene (associated only with the repo, no user-level).")
def create_scene(body: CreateSceneRequest, user: dict = Depends(require_no_session_token), repo: str = ""):
    """创建自定义场景（方案 B：场景只与仓库关联，无 user 级）"""
    from src.api.pipeline_config import create_custom_scene
    from src.kernel.repo_key import to_repo_key
    import uuid

    if body.scope == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "不可创建官方场景"})
    # 方案 B：user scope 已废弃，强制 repo
    if body.scope != "repo":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仅支持仓库级场景"})

    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        repo_key = ""
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            repo_key = ""
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库级场景需要先选择仓库"})

    # 校验 engine_runtime_ids：scope=repo 的引擎必须属于当前仓库，且 tag 必须包含节点标签
    _validate_scene_node_roles(nodes_json := [n.model_dump() for n in body.nodes])
    _validate_scene_engine_ids(nodes_json, repo_key, user.get("username") or "")
    # Issue #1208 D11：保存端透明升格（绑定值一律写 uids 清 ids，前端回读 uid 形态）
    _upgrade_scene_nodes_uid(nodes_json)

    # Issue #1758: 保存即拦截——repo 级场景引用 user 技能一律拒绝（含创建者本人）→ 400
    _validate_scene_skill_scope(nodes_json, repo_key)

    # PR 订阅开启时校验：QA 节点必须启用且有 skill
    if body.pr_subscription_enabled:
        qa_node = next((n for n in body.nodes if n.tag == "qa"), None)
        if not qa_node or not qa_node.enabled or not qa_node.skill:
            raise HTTPException(status_code=400, detail={
                "code": 400, "message": "开启 PR 订阅需要先配置 QA Agent 并启用技能"})

    scene_id = body.scene_id or str(uuid.uuid4())

    try:
        db_id = create_custom_scene(
            scene_id=scene_id, name=body.name, description=body.description,
            repo_key=repo_key, nodes=nodes_json, created_by=user.get("username") or "",
            icon=body.icon or "", scene_type="standard",
            pr_subscription_enabled=1 if body.pr_subscription_enabled else 0,
            linked_repos=body.linked_repos or [])
    except Exception as e:
        err_upper = str(e).upper()
        if "UNIQUE" in err_upper or "DUPLICATE" in err_upper:
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 ID '{scene_id}' 已存在"})
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})

    log.info(f"Scene created: id={scene_id} repo={repo_key} by user={user.get('id')}")
    return {"scene_id": scene_id, "db_id": db_id, "message": "场景创建成功"}

def _scene_copy_source_meta(scene_id: str, repo_key: str):
    """读取复制源场景的 created_by / advanced_options / icon（列缺失时降级为空）。

    created_by 为空（老场景）由调用方按「非创建者」从严处理。
    返回 ``(created_by, advanced_options, icon, parse_error)``；老场景
    ``advanced_options`` 非法 JSON / 非对象时按空配置处理，并以 parse_error=True
    计入复制响应明细（Spec 风险表要求）。
    """
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT created_by, advanced_options, icon FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
    except Exception as e:
        log.warning(f"scene copy: read source meta failed: {e}")
        return "", {}, "", False
    if not row:
        return "", {}, "", False
    created_by = row[0] or ""
    opts, parse_error = {}, False
    if row[1]:
        try:
            _o = json.loads(row[1])
            if isinstance(_o, dict):
                opts = _o
            else:
                parse_error = True
        except Exception:
            opts, parse_error = {}, True
    return created_by, opts, (row[2] or ""), parse_error

def _scene_copy_allowed_engines(repo_key: str, username: str, org_id: str = ""):
    """返回「当前用户在该仓库可选引擎 id（按角色）」查询闭包。

    口径对齐 dashboard agent_engines：SRE 仅 user+repo 两档；其它角色
    global+user+repo+local 四档；非个人组织按 org_id 隔离。
    """
    from src.domains.runtime.public import query_engines
    _filter_org = 0
    try:
        from src.domains.mgmt.user.org_db import is_org_personal
        if org_id and not is_org_personal(org_id):
            _filter_org = str(org_id)
    except Exception:
        _filter_org = 0
    _cache: dict = {}

    def _ids(role: str) -> list:
        if role in _cache:
            return _cache[role]
        if not role:
            _cache[role] = []
            return _cache[role]
        try:
            if role == "sre":
                engines = (query_engines(repo_key, "user", agent_type="sre", username=username)
                           + query_engines(repo_key, "repo", agent_type="sre"))
            else:
                engines = (query_engines(repo_key, "global", agent_type=role)
                           + query_engines(repo_key, "user", agent_type=role, username=username)
                           + query_engines(repo_key, "repo", agent_type=role)
                           + query_engines(repo_key, "local", agent_type=role, username=username))
        except Exception as e:
            log.warning(f"scene copy: query engines failed for role {role}: {e}")
            engines = []
        _ids_out, _seen = [], set()
        for e in engines:
            _rid = e.get("runtime_id")
            if _rid in _seen:
                continue
            _seen.add(_rid)
            # Issue #2265：global 引擎（org_id=''）平台共享，团队组织场景也可绑定
            if _filter_org and str(e.get("org_id") or "") and str(e.get("org_id")) != _filter_org:
                continue
            _ids_out.append(_rid)
        _cache[role] = _ids_out
        return _ids_out

    return _ids

def _scene_copy_repo_accessible(user: dict, repo_key: str) -> bool:
    """当前用户是否有权访问关联仓库（口径对齐 check_repo_access，仅不抛异常）。"""
    if not repo_key:
        return False
    if (user.get("auth_type") or "jwt") in ("session_token", "api_key"):
        return True
    uid = user.get("id", 0)
    if not uid:
        return False
    try:
        from src.kernel.repo_key import to_repo_key
        from src.domains.mgmt.user import db as user_db
        rk = to_repo_key(repo_key)
        # D16/S25：候选集统一走 repo_key_candidates（平台卡派生，含 gitee/gitlab
        # 等非主流平台），替代此前的 gitcode/github 双前缀字面量——非主流平台仓
        # 原实现静默查空（视作无权访问，副本丢关联仓）。
        from src.kernel.repo_key import repo_key_candidates
        candidates = set(repo_key_candidates(rk))
        return any((r or {}).get("repo_key") in candidates
                   for r in user_db.get_managed_repos(uid))
    except Exception as e:
        log.warning(f"scene copy: linked repo access check failed: {e}")
        return False

def _scene_copy_name(requested: str, source_name: str, existing_names) -> str:
    """副本命名：缺省 <源名>-copy；冲突追加序号；长度 ≤40。"""
    base = (requested or "").strip() or f"{(source_name or 'scene').strip()}-copy"
    base = base[:40]
    taken = {str(n).strip() for n in (existing_names or []) if n}
    if base not in taken:
        return base
    for i in range(2, 100):
        suf = str(i)
        cand = base[:40 - len(suf)] + suf
        if cand not in taken:
            return cand
    return base

class CopySceneRequest(BaseModel):
    name: str = ""

@router.post("/{scene_id}/copy", operation_id="scenes_copy", summary="Copy a scene", description="Copies a repo scene. The creator copies all custom-field values; other users only copy the field declarations (values cleared) and lose access to unavailable engines/skills/linked repos.")
def copy_scene(scene_id: str, body: CopySceneRequest | None = None,
               user: dict = Depends(require_no_session_token), repo: str = ""):
    """复制场景（Issue #1844）。

    创建者：全量复制（含 custom_fields 明文值与 secret 明文）。
    非创建者：custom_fields 只复制声明（value=""），技能/引擎/关联仓库按当前用户
    可达性清洗；不中断复制，响应回传各清洗明细。
    """
    from src.api.pipeline_config import (
        create_custom_scene, _query_scene_by_id, _query_scenes_from_db)
    from src.kernel.scene_custom_env import normalize_custom_field
    import uuid

    repo_key = _resolve_repo_key(repo) if repo else ""
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "需要选择仓库"})

    # 复制目标仓库归属校验（对齐 pin / deploy-url / reveal 端点），
    # 防止 JWT 用户在无归属仓库通过 repo 参数创建场景（session_token/api_key
    # 作用域已限定，由 check_repo_access 内部放行）。
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

    existing = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})

    username = user.get("username") or ""
    source_created_by, source_opts, source_icon, source_parse_error = _scene_copy_source_meta(scene_id, repo_key)
    # 老场景 created_by 为空 → 从严按「非创建者」处理
    is_creator = bool(source_created_by) and username == source_created_by

    nodes = json.loads(json.dumps(existing.get("nodes") or [], ensure_ascii=False))
    _allowed = _scene_copy_allowed_engines(repo_key, username, user.get("org_id") or 0)
    dropped_engines, cleared_skills = [], []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        role = (node.get("id") or node.get("tag") or "").strip()
        _ok_ids = set(_allowed(role))
        _kept = []
        for eid in (node.get("engine_runtime_ids") or []):
            try:
                _eid = int(eid)
            except (TypeError, ValueError):
                _eid = eid
            if _eid in _ok_ids:
                _kept.append(_eid)
            else:
                dropped_engines.append({"node_id": role, "engine_runtime_id": _eid})
        node["engine_runtime_ids"] = _kept
        # SRE 启用但清洗后无引擎 → 停用（与前端既有规则一致，避免创建被拒绝）
        if role == "sre" and node.get("enabled") is not False and not _kept:
            node["enabled"] = False

    # 技能：复用 arch 侧既有 scope 判官（_collect_scene_skill_scope_violations，repo 场景口径），
    # 冲突节点保留原名到 skill_pending，复制不中断（不再自建第二份判定）
    _skill_violations = _collect_scene_skill_scope_violations(
        nodes, repo_key, username=username, user_id=user.get("id") or "",
        allow_own_user=True, expected_scope="repo")
    for _v in _skill_violations:
        _tag = (_v.get("node_tag") or "").strip()
        _target = next((n for n in nodes if isinstance(n, dict)
                        and (n.get("id") or n.get("tag") or "").strip() == _tag), None)
        if _target is None:
            continue
        _orig_skill = _target.get("skill") or ""
        _target["skill_pending"] = _orig_skill
        _target["skill"] = ""
        _target["skill_id"] = 0
        if "skill_display_name" in _target:
            _target["skill_display_name"] = ""
        cleared_skills.append({"node_id": _tag, "skill": _orig_skill})

    work_branch = source_opts.get("work_branch") or "main"
    redacted_fields, custom_fields = [], []
    for f in (source_opts.get("custom_fields") or []):
        if not isinstance(f, dict):
            continue
        nf = normalize_custom_field(f)
        if not nf.get("name"):
            continue
        _value = nf.get("value") or ""
        if not is_creator and _value:
            redacted_fields.append(nf["name"])
        custom_fields.append({
            "name": nf["name"],
            "value": _value if is_creator else "",
            "is_secret": bool(nf.get("is_secret")),
            "scope": nf.get("scope") or ["all"],
            "preset": bool(nf.get("preset")),
            "default_value": nf.get("default_value") or "",
            "description": nf.get("description") or "",
        })
    preset_fields = [json.loads(json.dumps(f, ensure_ascii=False))
                     for f in (source_opts.get("preset_custom_fields") or [])
                     if isinstance(f, dict)]

    source_linked = source_opts.get("linked_repos")
    if source_linked is None:
        source_linked = existing.get("linked_repos") or []
    linked_repos, dropped_linked_repos = [], []
    for lr in (source_linked or []):
        if not isinstance(lr, dict):
            continue
        _rk = (lr.get("repo_key") or "").strip()
        if not _rk:
            continue
        _item = {"repo_key": _rk, "branch": (lr.get("branch") or "").strip() or "main"}
        if not is_creator and not _scene_copy_repo_accessible(user, _rk):
            dropped_linked_repos.append(_rk)
            continue
        linked_repos.append(_item)

    # PR 订阅：QA 节点不满足「启用 + 有技能」→ 降级为 0（否则后续保存/派发不成立）
    pr_sub = int(existing.get("pr_subscription_enabled") or 0)
    pr_disabled = False
    if pr_sub:
        _qa = next((n for n in nodes if isinstance(n, dict)
                    and (n.get("id") == "qa" or n.get("tag") == "qa")), None)
        if not _qa or _qa.get("enabled") is False or not (_qa.get("skill") or ""):
            pr_sub = 0
            pr_disabled = True

    advanced_options = {
        "work_branch": work_branch,
        "custom_fields": custom_fields,
        "linked_repos": linked_repos,
    }
    if preset_fields:
        advanced_options["preset_custom_fields"] = preset_fields

    _existing_names = []
    try:
        for s in (_query_scenes_from_db(repo_key=repo_key) or []):
            _existing_names.append(s.get("name") or "")
            _existing_names.append(s.get("display_name") or "")
    except Exception:
        pass
    final_name = _scene_copy_name(
        (body.name if body else "") or "",
        existing.get("display_name") or existing.get("name") or "",
        _existing_names)

    new_scene_id = str(uuid.uuid4())
    try:
        db_id = create_custom_scene(
            scene_id=new_scene_id, name=final_name,
            description=existing.get("description") or "",
            repo_key=repo_key, nodes=nodes, display_name=final_name,
            created_by=username, icon=source_icon or "",
            scene_type=existing.get("scene_type") or "standard",
            pr_subscription_enabled=pr_sub,
            linked_repos=linked_repos, advanced_options=advanced_options)
    except Exception as e:
        err_upper = str(e).upper()
        if "UNIQUE" in err_upper or "DUPLICATE" in err_upper:
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 ID '{new_scene_id}' 已存在"})
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})

    log.info(f"Scene copied: src={scene_id} new={new_scene_id} repo={repo_key} "
             f"by={username} creator={is_creator}")
    # Issue #1844 复审：创建者复制会带走 secret 明文，必须留痕（与同文件 pin 端点同口径）。
    # detail 只记标识与清洗计数，不落任何变量值。
    try:
        from src.domains.mgmt.auth import audit as auth_audit
        auth_audit.log_audit(
            "scene_copy", operator_id=user.get("id") or 0,
            detail={"scene_id": scene_id, "new_scene_id": new_scene_id, "repo_key": repo_key,
                    "operator": username, "is_creator": is_creator,
                    "redacted_field_count": len(redacted_fields),
                    "secret_field_count": sum(1 for f in custom_fields if f.get("is_secret")),
                    "dropped_engine_count": len(dropped_engines),
                    "cleared_skill_count": len(cleared_skills),
                    "dropped_linked_repo_count": len(dropped_linked_repos),
                    "pr_subscription_disabled": pr_disabled})
    except Exception as _audit_err:
        log.warning(f"Scene copy audit failed src={scene_id} new={new_scene_id}: {_audit_err}")
    return {
        "scene_id": new_scene_id,
        "db_id": db_id,
        "name": final_name,
        "is_creator": is_creator,
        "redacted_fields": redacted_fields,
        "cleared_skills": cleared_skills,
        "dropped_engines": dropped_engines,
        "dropped_linked_repos": dropped_linked_repos,
        "pr_subscription_disabled": pr_disabled,
        "source_config_parse_error": source_parse_error,
    }

@router.put("/{scene_id}", operation_id="scenes_update", summary="Update scene", description="Updates a scene (repo/user scope directly; official immutable).")
def update_scene(scene_id: str, body: UpdateSceneRequest, user: dict = Depends(require_no_session_token), repo: str = ""):
    """更新场景（repo/user scope 直接更新，official 不可修改）"""
    from src.api.pipeline_config import update_custom_scene, _query_scene_by_id
    from src.kernel.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    existing = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})

    nodes_json = [n.model_dump() for n in body.nodes]
    # Issue #955：节点 id 角色枚举校验（非法角色 400）
    _validate_scene_node_roles(nodes_json)
    if repo_key:
        _validate_scene_engine_ids(nodes_json, repo_key, user.get("username") or "")
    # Issue #1208 D11：保存端透明升格（绑定值一律写 uids 清 ids，前端回读 uid 形态）
    _upgrade_scene_nodes_uid(nodes_json)

    # PR 订阅开启时校验：QA 节点必须启用且有 skill
    _pr_sub_on = body.pr_subscription_enabled
    if _pr_sub_on is None:
        _pr_sub_on = bool(existing.get("pr_subscription_enabled", 0))
    if _pr_sub_on:
        # body.nodes 为空时从数据库已有场景查 QA 节点
        _check_nodes = body.nodes if body.nodes else existing.get("nodes", [])
        qa_node = next((n for n in _check_nodes if getattr(n, 'tag', n.get('tag') if isinstance(n, dict) else '') == "qa"), None)
        if qa_node:
            _qa_enabled = getattr(qa_node, 'enabled', True) if not isinstance(qa_node, dict) else qa_node.get("enabled", True)
            _qa_skill = getattr(qa_node, 'skill', '') if not isinstance(qa_node, dict) else qa_node.get("skill", "")
        else:
            _qa_enabled = False
            _qa_skill = ""
        if not _qa_enabled or not _qa_skill:
            raise HTTPException(status_code=400, detail={
                "code": 400, "message": "开启 PR 订阅需要先配置 QA Agent 并启用技能"})

    # body.nodes 为空时不覆盖已有 nodes（只更新 pr_subscription_enabled 等字段）
    _update_nodes = nodes_json if body.nodes else existing.get("nodes", [])
    # Issue #1758: 保存即拦截——repo 级场景引用 user 技能一律拒绝（含创建者本人）→ 400
    if repo_key and body.nodes:
        _validate_scene_skill_scope(_update_nodes, repo_key)
    update_custom_scene(
        scene_id=scene_id, repo_key=repo_key, nodes=_update_nodes,
        name=body.name, description=body.description, icon=body.icon,
        pr_subscription_enabled=1 if _pr_sub_on else 0 if _pr_sub_on is not None else None,
        linked_repos=body.linked_repos)

    log.info(f"Scene updated: id={scene_id} by user={user.get('id')}")
    return {"message": "场景更新成功"}

@router.delete("/{scene_id}", operation_id="scenes_delete", summary="Delete scene", description="Deletes a custom scene (soft delete; official undeletable).")
def delete_scene(scene_id: str, user: dict = Depends(require_no_session_token), repo: str = ""):
    """删除自定义场景（软删除，官方场景不可删除）"""
    from src.api.pipeline_config import delete_custom_scene, _query_scene_by_id
    from src.kernel.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    existing = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})

    delete_custom_scene(scene_id=scene_id, repo_key=repo_key)

    log.info(f"Scene deleted: id={scene_id} by user={user.get('id')}")
    return {"message": "场景已删除"}

def _collect_scene_skill_scope_violations(nodes: list, repo_key: str,
                                          expected_scope: str = "repo",
                                          username: str | None = None,
                                          user_id: str | int = "",
                                          allow_own_user: bool = False,
                                          cache: dict | None = None) -> list:
    """收集场景节点技能 scope 冲突（Issue #657 / #1564 / #1758），不抛异常、一次返回全部冲突。

    Issue #1758：判定收敛为单一判官 ``scene_skill_scope.evaluate``，四个入口
    （保存/apply、audit、派发门禁、scene_precheck）共用同一结论。固定口径：
    repo 级场景引用 user 技能一律冲突（含创建者本人）。
    技能不在 skill 表（官方内置包形态）不计冲突（存量兼容）。
    """
    from src.api.scene_skill_scope import evaluate
    violations = []
    cache: dict = {}
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        result = evaluate(node, repo_key, expected_scope or "repo",
                          cache=cache)
        if result["ok"]:
            continue
        violations.append({
            "node_id": result.get("node_id") or result.get("node_tag") or "",
            "node_tag": result.get("node_tag") or result.get("node_id") or "",
            "skill": result.get("skill") or "",
            "actual_scope": result.get("actual_scope") or "",
            "expected_scope": result.get("expected_scope") or expected_scope,
            "owner_id": result.get("owner_id") or "",
            "reason": result.get("reason") or "",
            "skill_id": result.get("skill_id") or 0,
            "source": result.get("source") or "",
            "rebind_candidates": list(result.get("rebind_candidates") or []),
        })
    return violations

_SCENE_SCOPE_VIOLATION_FIELDS = ("node_id", "node_tag", "skill",
                                "actual_scope", "expected_scope")

def _project_scene_scope_violation(v: dict) -> dict:
    """对外 violations[] 的唯一裁剪点（Issue #1564）：去掉内部 owner_id 等字段。"""
    return {k: v[k] for k in _SCENE_SCOPE_VIOLATION_FIELDS}

def _validate_scene_skill_scope(nodes: list, repo_key: str, expected_scope: str = "repo"):
    """场景节点技能 scope 校验（Issue #657 / #1564 / #1758）：冲突时抛 HTTPException(400)。

    Issue #1564：收集式校验，400 detail 一次性返回全部冲突节点
    violations[{node_id,node_tag,skill,actual_scope,expected_scope}]（供前端整表单标红），
    同时保留顶层 skill/actual_scope/expected_scope（兼容既有调用方）。
    Issue #1758：判定统一走单一判官，user scope 一律拒绝（含创建者本人）；
    400 额外返回 skill_scope / scene_scope / rebind_candidates / rebinds，
    rebinds 为逐节点改绑建议。
    """
    from src.api.scene_skill_scope import format_rebind_hint
    violations = _collect_scene_skill_scope_violations(
        nodes, repo_key, expected_scope=expected_scope)
    if not violations:
        return
    _msgs = []
    _rebind = []
    for v in violations:
        _cand = list(v.get("rebind_candidates") or [])
        _m = (f"节点 {v['node_tag']} 引用技能 '{v['skill']}' scope 不匹配："
              f"技能 scope={v['actual_scope'] or '-'}，场景 scope={v['expected_scope'] or 'repo'}，"
              f"repo 级场景不可引用 user 级技能（含创建者本人）")
        _m += f"；{format_rebind_hint(_cand)}"
        _msgs.append(_m)
        _rebind.append({
            "node_id": v.get("node_id") or "",
            "node_tag": v.get("node_tag") or "",
            "skill": v.get("skill") or "",
            "skill_scope": v.get("actual_scope") or "",
            "scene_scope": v.get("expected_scope") or "repo",
            "rebind_candidates": _cand,
        })
    _first = violations[0]
    raise HTTPException(status_code=400, detail={
        "code": 400,
        "message": "；".join(_msgs),
        "skill": _first["skill"],
        "actual_scope": _first["actual_scope"],
        "expected_scope": _first["expected_scope"],
        "skill_scope": _first["actual_scope"],
        "scene_scope": _first["expected_scope"],
        "rebind_candidates": _rebind[0]["rebind_candidates"],
        "rebinds": _rebind,
        "violations": [_project_scene_scope_violation(v) for v in violations],
    })

@router.post("/apply/{owner}/{repo}", operation_id="scenes_apply", summary="Apply scene to repository", description="Applies a scene to a repository.")
def apply_scene(owner: str, repo: str, body: ApplySceneRequest,
                      user: dict = Depends(require_no_session_token)):
    """为仓库应用指定场景"""
    from src.api.pipeline_config import get_pipeline_config, _query_scene_by_id
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock
    from src.api.session_store import upsert_agent_session
    from src.domains.runtime.public import node_agent_role

    repo_key = _resolve_repo_key(f"{owner}/{repo}")
    cfg = get_pipeline_config(body.scene_id, repo_key=repo_key)
    if not cfg:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{body.scene_id}' 不存在"})

    nodes = cfg.get("nodes", [])
    if not nodes:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景无 Agent 节点配置"})

    # Issue #657: 节点技能 scope 校验（repo 级场景不可引用 user 级技能），
    # Pass 1 只读阶段拦截，落库前直接拒绝
    _validate_scene_skill_scope(nodes, repo_key)

    # R25.9 回声治理：应用场景不触碰 token（原 get_repo_token_with_refresh 把 bot/用户级
    # token 回填 session，第 1 级命中 bot token → 自循环检测放行 bot 评论 → 回声）。
    # upsert 传 None = 保留既有 session token。
    updated = 0
    node_receipts = []
    skipped_receipts = []
    with db_lock:
        c = db.cursor()
        # Issue #524: Pass 1——先逐节点解析引擎选择（只读，不落库），产出结构化回执
        _pending = []  # (node_receipt, agent_type, skill, runtime_id)
        for node in nodes:
            # Issue #955：节点 id 即角色名（id 优先、tag 兜底）——tag 为中文阶段名
            # 的节点在 apply 路径也按 id 角色解析引擎绑定，不再取展示 tag
            agent_type = node_agent_role(node)
            skill = node.get("skill", "")
            if not agent_type or not skill:
                continue
            # 跳过场景中 enabled=false 的 agent
            if node.get("enabled") is False:
                continue
            declared_ids = list(node.get("engine_runtime_ids") or [])
            receipt = {
                "agent_type": agent_type,
                "skill": skill,
                "status": "failed",
                "effective_runtime_id": 0,
                "declared_runtime_ids": declared_ids,
                "reason": "",
            }
            try:
                runtime_id = 0
                # 优先使用场景 node 配置 engine_runtime_ids：选第一个 online 且 healthy 的
                if declared_ids:
                    _declared_state = ""
                    for eid in declared_ids:
                        try:
                            _row = _rt_scenes.runtime_status_healthy(eid)
                            if _row and _row[0] == "online" and _row[1] == 1:
                                runtime_id = eid
                                break
                            if _row and not _declared_state:
                                _declared_state = f"engine {eid} {_row[0]} healthy={_row[1]}"
                        except Exception:
                            pass
                    # Issue #524: 声明引擎存在但全部不健康/离线 → skipped（不再静默回退旧绑定）
                    if not runtime_id:
                        _cur_rid = 0
                        try:
                            _rid = _rt_scenes.devops_session_runtime_id(repo_key, agent_type)
                            if _rid:
                                _cur_rid = int(_rid)
                        except Exception:
                            _cur_rid = 0
                        _reason = _declared_state or f"declared engines {declared_ids} unavailable"
                        receipt.update({
                            "status": "skipped",
                            "effective_runtime_id": _cur_rid,
                            "reason": (f"{_reason}, keep runtime "
                                       f"{_cur_rid if _cur_rid else 'none (no effective engine)'}"),
                        })
                        node_receipts.append(receipt)
                        skipped_receipts.append(receipt)
                        continue
                else:
                    # 场景未声明引擎 → 旧回退链：session 旧绑定 → 默认引擎
                    try:
                        _rid = _rt_scenes.devops_session_runtime_id(repo_key, agent_type)
                        runtime_id = int(_rid) if _rid else 0
                    except Exception:
                        runtime_id = 0
                    if not runtime_id:
                        from src.domains.runtime.public import resolve_default_engine
                        runtime_id = resolve_default_engine(repo_key, agent_type) or 0
                receipt.update({
                    "status": "applied",
                    "effective_runtime_id": runtime_id,
                    "reason": (f"engine {runtime_id} applied" if runtime_id
                               else "applied (broadcast/default engine)"),
                })
                _pending.append((receipt, agent_type, skill, runtime_id))
            except Exception as e:
                log.warning(f"Scene apply: resolve failed for {agent_type}: {e}")
                receipt.update({"status": "failed", "reason": str(e)})
            node_receipts.append(receipt)

        # Issue #524: strict 模式——任一声明引擎不健康的节点 skipped → 整单拒绝（Pass 1 未落库）
        if body.strict and skipped_receipts:
            _skip_desc = "; ".join(
                f"{r['agent_type']}: {r['reason']}" for r in skipped_receipts)
            raise HTTPException(status_code=409, detail={
                "code": 409,
                "message": (f"场景 '{cfg.get('name', body.scene_id)}' 应用被拒绝（strict 模式，"
                            f"{len(skipped_receipts)} 个节点声明引擎不健康）：{_skip_desc}"),
                "nodes": node_receipts,
            })

        # Pass 2——对 applied 节点落库（strict 已在落库前拒绝）
        from src.domains.mgmt.user.db import resolve_skill_id
        for receipt, agent_type, skill, runtime_id in _pending:
            try:
                skill_id = resolve_skill_id(skill, repo_key=repo_key, user_id=user.get("id", ""))
                upsert_agent_session(
                    repo_key=repo_key, agent_type=agent_type,
                    runtime_id=runtime_id, skill=skill, skill_id=skill_id,
                    status="idle",
                    enable=None, subscribe_events=None,
                )
                updated += 1
            except Exception as e:
                log.warning(f"Scene apply: failed for {agent_type}: {e}")
                receipt.update({"status": "failed", "reason": str(e)})
        db.commit()

    applied_cnt = sum(1 for r in node_receipts if r["status"] == "applied")
    skipped_cnt = sum(1 for r in node_receipts if r["status"] == "skipped")
    failed_cnt = sum(1 for r in node_receipts if r["status"] == "failed")

    log.info(f"Scene applied: scene={body.scene_id}, repo={repo_key}, updated={updated} agents")

    # Issue #1089 挂点①：节点插件凭据协商（应用场景时弹凭据配置；软引用不阻塞）
    try:
        credential_required = _collect_plugin_credential_requirements(nodes, user.get("id", 0))
    except Exception as _e:
        log.warning(f"apply_scene: collect plugin credential requirements failed: {_e}")
        credential_required = []

    return {
        "message": f"场景 '{cfg.get('name', body.scene_id)}' 已应用",
        "updated": updated,
        "nodes": node_receipts,
        "applied": applied_cnt,
        "skipped": skipped_cnt,
        "failed": failed_cnt,
        "credential_required": credential_required,
    }

def app_current_repo():
    """获取当前选中仓库（从前端 header 上下文，这里用 placeholder）"""
    # FastAPI 无 session 级仓库选择；前端通过 query param 或 header 传
    # 这里返回 None，调用方会用 user 信息或其他方式获取 repo
    return None

def _merge_preset_custom_fields(scene_id: str, repo_key: str, preset_json: str = "[]") -> None:
    """将场景预置自定义参数声明合并到 devops_pipeline_scene.advanced_options（Issue #246）。

    - preset 声明来自 scene_market.preset_custom_fields（scene.json custom_fields 数组）
    - 仅在 custom_fields 中不存在同名项时追加（不覆盖用户已有配置）
    - preset 项标注 preset=true，preyset 声明持久化为 advanced_options.preset_custom_fields
    """
    import json as _j
    from src.kernel.session_manager import db, db_lock
    from src.kernel.scene_custom_env import normalize_custom_field

    try:
        preset_decls = _j.loads(preset_json) if preset_json else []
    except Exception:
        preset_decls = []
    if not preset_decls:
        return

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT advanced_options FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (scene_id, repo_key))
        row = c.fetchone()
    opts = _j.loads(row[0]) if (row and row[0]) else {}

    existing = {(f.get("name") or "").strip() for f in (opts.get("custom_fields", []) or [])}
    _normalized_presets = []
    appended = 0
    for _p in preset_decls:
        _pf = normalize_custom_field(_p)
        _pf["preset"] = True
        _pf["value"] = _pf.get("default_value", "")
        _normalized_presets.append(_pf)
        if not _pf.get("name") or _pf["name"] in existing:
            continue
        opts.setdefault("custom_fields", []).append(_pf)
        existing.add(_pf["name"])
        appended += 1
    # 声明持久化，供 preset 变更/重启用
    opts["preset_custom_fields"] = preset_decls

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE devops_pipeline_scene SET advanced_options=?, updated_at=? "
            "WHERE scene_id=? AND repo_key=? AND is_active=1",
            (_j.dumps(opts, ensure_ascii=False), now, scene_id, repo_key))
        db.commit()
    if appended:
        log.info(f"merge_preset_custom_fields: scene={scene_id} repo={repo_key} appended={appended}")

class AdvancedOptionsRequest(BaseModel):
    work_branch: str = "main"
    custom_fields: list = []   # [{name: str, value: str, is_secret: bool?, scope?: list}]
    linked_repos: list = []    # [{repo_key: str, branch: str}] — Issue #242

@router.get("/{scene_id}/advanced-options", operation_id="scenes_advanced_options_get", summary="Get scene advanced options", description="Reads scene advanced options (work branch + custom key:value).")
def get_advanced_options(scene_id: str, user: dict = Depends(require_no_session_token), repo: str = ""):
    """读取场景高级选项（工作分支 + 自定义 key:value）"""
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock
    import json as _json

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "需要选择仓库"})

    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
    except Exception:
        row = None

    if row and row[0]:
        try:
            opts = _json.loads(row[0])
        except Exception:
            opts = {}
        from src.kernel.scene_custom_env import normalize_custom_field
        _fields = []
        for f in (opts.get("custom_fields", []) or []):
            _nf = normalize_custom_field(f)
            if not _nf.get("name"):
                continue
            # Issue #885: is_secret 字段不再明文返回——占位（value:"" + has_value 标记），
            # 按需取回明文走 POST /{scene_id}/advanced-options/reveal（对齐 #474 模式）；
            # 非 secret 字段保持明文返回（原行为）。
            _is_sec = bool(_nf.get("is_secret"))
            _fields.append({
                "name": _nf["name"],
                "value": "" if _is_sec else _nf.get("value", ""),
                "is_secret": _is_sec,
                "has_value": bool(_nf.get("value")),
                "scope": _nf.get("scope", ["all"]),
                "preset": _nf.get("preset", False),
                "description": _nf.get("description", ""),
            })
        return {
            "work_branch": opts.get("work_branch", "main") or "main",
            "custom_fields": _fields,
            "linked_repos": opts.get("linked_repos", []) or [],
        }
    # linked_repos 也可能存在独立列中（create_scene 直接写入）
    _linked_repos = []
    try:
        from src.api.pipeline_config import _query_scene_by_id
        _scene = _query_scene_by_id(scene_id, repo_key=repo_key)
        if _scene:
            _linked_repos = _scene.get("linked_repos") or []
    except Exception:
        pass
    return {"work_branch": "main", "custom_fields": [], "linked_repos": _linked_repos}

# ===== Issue #885: 场景自定义变量密钥字段按需取回明文（对齐 #474 reveal 模式）=====
# GET advanced-options 已收敛为占位（is_secret → value:"" + has_value）；
# 需要查看明文时走本接口：密码二次确认 + 审计日志（不落明文）+ 当次返回
# （Cache-Control: no-store）+ 高频取回限流（复用 #474 的 reveal 日志表，429）。
class SceneAdvancedOptionsRevealBody(BaseModel):
    name: str = ""       # custom_field 变量名（大写）
    password: str = ""   # 二次确认：当前 Grape 密码

@router.post("/{scene_id}/advanced-options/reveal", operation_id="scenes_advanced_options_reveal", summary="Reveal a scene custom-field secret once", description="Returns the plaintext value of one is_secret custom field once, after the user re-enters their Grape password. The response carries Cache-Control: no-store and a successful reveal is written to the audit log (scene/repo/field only, never the secret). Repeated reveals within a short window are rate-limited (429).")
def reveal_advanced_options_secret(scene_id: str, body: SceneAdvancedOptionsRevealBody,
                                   user: dict = Depends(require_no_session_token),
                                   repo: str = "", request: Request = None, response: Response = None):
    """按需取回场景自定义变量密钥明文（一次性，Issue #885）。

    - 前置：repo 归属校验（check_repo_access）+ 场景存在 + 字段存在且 is_secret
    - 二次确认：重新输入当前 Grape 密码（未设密码 → 400 + need_set_password）
    - 审计：scene_secret_reveal，detail 仅含 scene_id/repo_key/name，不落明文值
    - 明文仅当次返回：Cache-Control: no-store
    - 高频告警：窗口内取回次数达到阈值 → 429（复用 #474 reveal 日志表计数）
    """
    from src.kernel.session_manager import db, db_lock
    from src.kernel.scene_custom_env import normalize_custom_field
    from src.domains.mgmt.routes.settings import (
        _count_reveals_in_window, _record_reveal, _REVEAL_MAX_PER_WINDOW)
    import json as _json

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "需要选择仓库"})
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "变量名不能为空"})

    # 二次确认：重新输入当前密码（与 #474 reveal 一致，密码校验前置于字段枚举之前，
    # 防止无正确密码探测密钥字段存在性/配置状态）
    from src.domains.mgmt.auth.password import verify_password
    from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
    account = get_grape_account_by_id(user["id"]) or {}
    password_hash = account.get("password_hash") or ""
    if not password_hash:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "请先设置 Grape 密码后再取回明文", "need_set_password": True})
    if not verify_password(body.password or "", password_hash):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "当前密码错误"})

    # 读取场景存储的 custom_fields（明文在库内，接口层不下发）
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
    except Exception:
        row = None
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})
    try:
        opts = _json.loads(row[0]) or {}
    except Exception:
        opts = {}

    target = None
    for f in (opts.get("custom_fields", []) or []):
        _nf = normalize_custom_field(f) if isinstance(f, dict) else {}
        if _nf.get("name") == name:
            target = _nf
            break
    if not target:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"配置项 '{name}' 不存在"})
    if not target.get("is_secret"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非密钥字段无需按需取回"})
    value = target.get("value") or ""
    if not value:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "该配置项未配置值"})

    # 高频取回告警：窗口内已达阈值 → 拒绝并提示（复用 #474 计数表）
    if _count_reveals_in_window(user["id"]) >= _REVEAL_MAX_PER_WINDOW:
        ip = request.client.host if request and request.client else ""
        log.warning(f"scene secret reveal high-frequency: user={user['id']} scene={scene_id} "
                    f"repo={repo_key} field={name} ip={ip}")
        raise HTTPException(status_code=429, detail={
            "code": 429, "message": "明文取回过于频繁，请稍后再试"})

    # 审计：谁/何时/取了哪个场景的哪个字段（detail 不含明文）
    ip = request.client.host if request and request.client else ""
    from src.domains.mgmt.auth import audit as auth_audit
    auth_audit.log_audit("scene_secret_reveal", user_id=user["id"], ip=ip,
                         detail={"scene_id": scene_id, "repo_key": repo_key, "name": name})
    _record_reveal(user["id"], "scene_advanced_options", name, ip)

    if response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return {"name": name, "value": value}

@router.put("/{scene_id}/advanced-options", operation_id="scenes_advanced_options_set", summary="Save scene advanced options", description="Saves scene advanced options (work branch + custom key:value).")
def save_advanced_options(scene_id: str, body: AdvancedOptionsRequest,
                          user: dict = Depends(require_no_session_token), repo: str = ""):
    """保存场景高级选项（工作分支 + 自定义 key:value）"""
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock
    from src.api.pipeline_config import _query_scene_by_id
    import json as _json
    from datetime import datetime, timezone

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "需要选择仓库"})

    # 校验 work_branch 非空（强制字段）
    if not body.work_branch or not body.work_branch.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "工作分支不能为空"})

    # 校验场景存在
    existing = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})

    # 校验 custom_fields name 非空/大写/保留冲突且不重复
    from src.kernel.scene_custom_env import custom_env_error, normalize_custom_field
    seen_names = set()
    for f in (body.custom_fields or []):
        _n = (f.get("name") or "").strip() if isinstance(f, dict) else ""
        _err = custom_env_error(_n)
        if _err:
            raise HTTPException(status_code=400, detail={"code": 400, "message": _err})
        if _n in seen_names:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"配置项 name '{_n}' 重复"})
        seen_names.add(_n)
        # scope 校验：数组内每项必须为合法枚举；全部非法 → 400（部分非法在 normalize 时剔除）
        _raw_scope = f.get("scope") if isinstance(f, dict) else None
        if _raw_scope:
            from src.kernel.scene_custom_env import _SCOPE_SET
            _items = _raw_scope if isinstance(_raw_scope, (list, tuple, set)) else [_raw_scope]
            _valid_items = {(str(x) or "").strip().lower() for x in _items if x not in (None, "")}
            if _valid_items and not (_valid_items & _SCOPE_SET):
                raise HTTPException(
                    status_code=400,
                    detail={"code": 400, "message": f"配置项 '{_n}' 的 scope 非法，可选值: all/vod/issue/coding/qa/sre"})

    # 校验 linked_repos 格式：[{repo_key, branch}]，上限 5 个
    _linked_repos = []
    for lr in (getattr(body, 'linked_repos', None) or []):
        if not isinstance(lr, dict):
            continue
        _rk = (lr.get("repo_key") or "").strip()
        _br = (lr.get("branch") or "").strip()
        if _rk:
            _linked_repos.append({"repo_key": _rk, "branch": _br or "main"})
    if len(_linked_repos) > 5:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "关联仓库数量不能超过 5 个"})

    _custom_fields = [normalize_custom_field(f) for f in (body.custom_fields or []) if isinstance(f, dict)]
    _custom_fields = [f for f in _custom_fields if f.get("name")]
    # 读库内现有 advanced_options（preset 声明 + 既有值），预置参数保护
    from src.kernel.scene_custom_env import normalize_scope as _normalize_scope
    _old_ao = {}
    _old_cf_map = {}      # name -> 完整旧项
    _preset_decl_map = {} # name -> preset 声明（preset_custom_fields 来源）
    try:
        with db_lock:
            _c2 = db.cursor()
            _c2.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            _row2 = _c2.fetchone()
        if _row2 and _row2[0]:
            _old_ao = _json.loads(_row2[0]) or {}
        for _f in (_old_ao.get("custom_fields", []) or []):
            _nf = normalize_custom_field(_f)
            if _nf.get("name"):
                _old_cf_map[_nf["name"]] = _nf
        # preset 声明：优先库内 preset_custom_fields，其次 custom_fields 里的 preset:true 项
        for _pf in (_old_ao.get("preset_custom_fields", []) or []):
            _pf = normalize_custom_field(_pf) if isinstance(_pf, dict) else {}
            if _pf.get("name"):
                _pf["preset"] = True
                _preset_decl_map[_pf["name"]] = _pf
        for _f in (_old_ao.get("custom_fields", []) or []):
            _nf = normalize_custom_field(_f)
            if _nf.get("preset") and _nf.get("name") and _nf["name"] not in _preset_decl_map:
                _nf["preset"] = True
                _preset_decl_map[_nf["name"]] = _nf
    except Exception:
        pass

    # 前端掩码回显（value=********）→ keep_value 标记：value 视为未修改，保留旧值（不受 is_secret 限制）
    if _custom_fields:
        try:
            with db_lock:
                _c = db.cursor()
                _c.execute(
                    "SELECT advanced_options FROM devops_pipeline_scene "
                    "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                    (scene_id, repo_key))
                _row = _c.fetchone()
            _old = _json.loads(_row[0]) if (_row and _row[0]) else {}
            _old_map = {f.get("name"): f.get("value", "") for f in (_old.get("custom_fields", []) or []) if f.get("name")}
            _keep_map = {
                (f.get("name") or "").strip()
                for f in (body.custom_fields or []) if isinstance(f, dict) and f.get("keep_value")
            }
            for _f in _custom_fields:
                # keep_value（掩码未改）/ secret 空值或 "********" 掩码回显（清空无效；
                # Issue #885 前端占位掩码回传）→ 保留旧值
                if ((_f["name"] in _keep_map)
                        or (_f.get("is_secret") and (not _f.get("value") or _f.get("value") == "********"))) \
                        and _f["name"] in _old_map:
                    _f["value"] = _old_map[_f["name"]]
        except Exception:
            pass

    # ── preset 保护：合并声明、锁定字段、禁止丢失 ──
    # 1) 库内 preset 声明全部保留（前端未提交也不能删）
    for _pname, _pdecl in _preset_decl_map.items():
        if _pname not in {f.get("name") for f in _custom_fields}:
            _existing = _old_cf_map.get(_pname)
            _custom_fields.append({
                "name": _pname,
                "value": (_existing.get("value", "") if _existing else _pdecl.get("default_value", "")),
                "is_secret": _pdecl.get("is_secret", False),
                "scope": _pdecl.get("scope", ["all"]) or ["all"],
                "preset": True,
                "default_value": _pdecl.get("default_value", ""),
                "description": _pdecl.get("description", ""),
            })
    # 2) preset 项字段锁定：name/scope/is_secret/preset 以声明为准，仅 value 取前端提交（或旧值）
    for _f in _custom_fields:
        _pdecl = _preset_decl_map.get(_f.get("name"))
        if not _pdecl:
            continue
        _old_v = _old_cf_map.get(_f["name"], {}).get("value", "")
        _f["preset"] = True
        _f["name"] = _pdecl["name"]
        _f["scope"] = _pdecl.get("scope", ["all"]) or ["all"]
        if "is_secret" in _pdecl:
            _f["is_secret"] = _pdecl.get("is_secret", False)
        _f["description"] = _pdecl.get("description", "")
        if not _f.get("value") or _f.get("value") in ("********", "#####"):
            _f["value"] = _old_v or _pdecl.get("default_value", "")
    # 3) 非 preset 项显式标注 preset=false
    for _f in _custom_fields:
        if _f.get("name") not in _preset_decl_map:
            _f["preset"] = False

    opts = {
        "work_branch": body.work_branch.strip(),
        "custom_fields": _custom_fields,
        "linked_repos": _linked_repos,
    }
    opts_json = _json.dumps(opts, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()

    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE devops_pipeline_scene SET advanced_options=?, updated_at=? "
                "WHERE scene_id=? AND repo_key=? AND is_active=1",
                (opts_json, now, scene_id, repo_key))
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在或未激活"})
            # Issue #242: 同步写入独立 linked_repos 列（便于查询时直接读取）
            try:
                c.execute(
                    "UPDATE devops_pipeline_scene SET linked_repos=? WHERE scene_id=? AND repo_key=? AND is_active=1",
                    (_json.dumps(_linked_repos, ensure_ascii=False), scene_id, repo_key))
            except Exception:
                pass  # 列不存在时忽略（兼容旧库）
            db.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})

    log.info(f"Advanced options saved: scene={scene_id} repo={repo_key} work_branch={body.work_branch} linked_repos={len(_linked_repos)}")
    return {"message": "高级选项已保存"}

class SceneDeployUrlBody(BaseModel):
    deploy_url: str = ""

def _scene_deploy_url_guard(scene_id: str, repo: str, user: dict) -> str:
    """场景级 deploy-url 端点公共前置：repo 解析 + 归属校验，返回 repo_key。"""
    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "需要选择仓库"})
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    return repo_key

@router.get("/{scene_id}/deploy-url", operation_id="scenes_deploy_url_get", summary="Get scene deployment URL", description="Returns the scene-level access URL registered on the scene detail page.")
def get_scene_deploy_url(scene_id: str, user: dict = Depends(require_no_session_token), repo: str = ""):
    """获取场景级访问地址（每场景独立一条；不影响需求中心仓库级 deploy_url）"""
    from src.kernel.session_manager import db, db_lock
    repo_key = _scene_deploy_url_guard(scene_id, repo, user)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT deploy_url FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
    except Exception:
        row = None
    return {"deploy_url": (row[0] if row else "") or ""}

@router.put("/{scene_id}/deploy-url", operation_id="scenes_deploy_url_set", summary="Set scene deployment URL", description="Registers/updates the scene-level access URL (validated like the repo deploy URL).")
def set_scene_deploy_url(scene_id: str, body: SceneDeployUrlBody,
                         user: dict = Depends(require_no_session_token), repo: str = ""):
    """登记/编辑场景级访问地址（校验与需求中心一致：http(s) 前缀 + ≤512；空串清除）"""
    from src.kernel.session_manager import db, db_lock
    # 复用 #446 的 XSS 防护校验（与 src/domains/mgmt/routes/repos.py set_deploy_url 同一规则）
    from src.domains.mgmt.routes.repos import _validate_deploy_url
    deploy_url = _validate_deploy_url(body.deploy_url)
    repo_key = _scene_deploy_url_guard(scene_id, repo, user)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE devops_pipeline_scene SET deploy_url=? "
            "WHERE scene_id=? AND repo_key=? AND is_active=1",
            (deploy_url, scene_id, repo_key))
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在或未激活"})
        db.commit()
    log.info(f"Scene deploy url saved: scene={scene_id} repo={repo_key}")
    return {"message": "场景访问地址已更新", "deploy_url": deploy_url}

# ── Issue #501：单场景引擎在线状态（按需探测，从 with-pipeline 抽出） ──────────
# 由 weekly-10 迁入（原实现 src/api/routes/scenes.py）。
