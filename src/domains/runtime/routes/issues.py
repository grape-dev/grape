"""Issues 路由：以 Issue 为中心对外整体开放（阶段 + 事件 + pipeline）。

与 /api/tasks、/api/events 不同，本路由按 「Issue 视角」聚合：
- issue 基本信息 + pipeline 阶段（issue_state.stage/owner_agent/scene）+ 任务中心状态（tasks.status）
- 该 Issue 的事件流（events 表，按 issue_id 聚类）
- Issue pipeline 路线图（event.json 结构化链）

认证：`get_current_user_or_api_key`（JWT / tk_ 会话 token / grape_ API Key 三通道）。
- 读类（列表/详情/事件/pipeline/GET status）→ dual 可达。
- 写/操作类（create/start/confirm-done/cancel/resume/PUT status/clarify/attachments 写）→ `require_no_session_token`
  （用户仅限 user_only，`tk_` → 403；避免 Agent 任意改状态干扰 pipeline）。
Design: docs/weekly-02/issues-api-design.md
"""

import os

import re

import asyncio

import copy as _copy_cache
import functools as _ft_cache
import threading as _th_cache
import time as _time_cache

from fastapi import APIRouter, HTTPException, Depends, Query, File, UploadFile, Request

from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key, require_no_session_token, require_org_member
from src.api.pipeline_constants import DISPATCHABLE_AGENTS as _DEV_OPS_AGENTS

from src.kernel.config import log
from src.kernel.issue_ident import (
    native_issue_number, normalize_issue_ident, is_actionable_issue_number,
)  # Issue #3055


class _OwnedCursor:
    """自带连接的游标：close 时把连接一并关掉（每次查询独立建连的收尾）。"""

    def __init__(self, conn, cursor):
        self._conn = conn
        self._cur = cursor

    def __getattr__(self, item):
        return getattr(self._cur, item)

    def close(self):
        try:
            self._cur.close()
        finally:
            try:
                self._conn.close()
            except Exception as _e:
                log.debug(f"cursor close failed: {_e}")


class _DomainDbProxy:
    """把 `db.cursor()` / `db.commit()` 代理到 runtime 域连接。

    需求中心的 issue / issue_state / events / agent_tasks 都在 runtime 域，但该路由由
    mgmt 进程服务。三库模式下 mgmt 进程的主库是本域库，直连进程库会撞「表不存在」；
    这里按域取连接（own DSN 优先，单库回落进程库），写路径仍应走 runtime public 原语。
    """

    def cursor(self, *a, **kw):
        from src.kernel import db_registry
        # 本进程主库已经是 runtime 库时直接用主库：session_manager.db 是成熟的
        # MySQL 兼容层（方言翻译 + 连接池），换轻量连接只会引入方言差异。
        if db_registry.current_domain() == "runtime":
            from src.kernel.session_manager import db as _main
            return _main.cursor(*a, **kw)
        if db_registry.uses_own_dsn("runtime"):
            # 每次取一条独立连接：共享连接绑线程，跨线程复用会 InterfaceError(0, '')
            _conn = db_registry.new_connection("runtime")
            return _OwnedCursor(_conn, _conn.cursor(*a, **kw))
        from src.kernel.session_manager import db as _main
        return _main.cursor(*a, **kw)

    def commit(self):
        from src.kernel import db_registry
        # 必须与 cursor() 的取连接分支一致：runtime 域进程走 session_manager.db 的
        # autocommit=False 线程级连接，这里若 return None 会让写事务一直悬着——行锁不释放、
        # 新写入的行（如 enqueue 的任务）对其它连接不可见，随后任何 rollback 直接丢掉。
        if db_registry.current_domain() == "runtime":
            from src.kernel.session_manager import db as _main
            return _main.commit()
        if db_registry.uses_own_dsn("runtime"):
            return None
        from src.kernel.session_manager import db as _main
        return _main.commit()

    def rollback(self):
        from src.kernel import db_registry
        if db_registry.current_domain() == "runtime":
            from src.kernel.session_manager import db as _main
            return _main.rollback()
        if db_registry.uses_own_dsn("runtime"):
            try:
                db_registry.domain_db("runtime").rollback()
            except Exception as _e:
                log.debug(f"cursor close failed: {_e}")
            return None
        from src.kernel.session_manager import db as _main
        return _main.rollback()

    def __getattr__(self, item):
        from src.kernel.session_manager import db as _main
        return getattr(_main, item)


_rt_db = _DomainDbProxy()


from src.api.time_utils import normalize_event_time

from src.api.issues_center import (TaskAwaitingInputBody, TaskPendingReviewBody,
                                   _review_pass, _review_reject, _set_issue_pending_review,
                                   _read_issue_status)

router = APIRouter(prefix="/api/issues", tags=["issues"])

def _num_from_issue_id(issue_id: str) -> str:
    """从 issue_id 提取编号。

    Issue #252: #pr_ 前缀的 PR 订阅伪 ID 不参与 issue 事件分组
    （它们由 _query_pr_subscription_events 独立处理，不应混入同编号的 issue 分组）。
    """
    _raw = str(issue_id or "")
    if _raw.startswith("#pr_"):
        return ""
    m = re.search(r"(\d+)", _raw)
    if m:
        return m.group(1)
    # D105 字符串 ident（gitee IK*）：无数字时返回去 # 原始 ident——
    # 此前回退 ''，gitee 事件被全量归到空键组，events/pipeline/detail 查不到
    # （2026-11-10 部署实例实测：IKUDWW events 返回空、pipeline 422）。
    # Issue #3055：ident 判据转发内核唯一实现（禁止在此复制正则）
    return normalize_issue_ident(_raw)

def _enforce_session_repo_scope(user: dict, repo_keys: list[str]):
    """tk_ 会话 token 限定其绑定仓库：请求 repo_keys 含该 repo 才放行（防 agent 跨仓库读事件）。"""
    if user.get("auth_type") == "session_token":
        _tok_repo = user.get("repo_key") or ""
        if _tok_repo and _tok_repo not in repo_keys:
            raise HTTPException(
                status_code=403,
                detail={"code": 403, "message": "会话 token 无权访问该仓库事件"})

def _query_pr_subscription_events(repo_keys: list[str]) -> list[dict]:
    """查 PR 订阅事件（issue_id LIKE '#pr_%'），按 PR 号聚合为伪卡片。"""
    from src.kernel.session_manager import db, db_lock
    items = []
    placeholders = ",".join("?" * len(repo_keys))
    with db_lock:
        c = _rt_db.cursor()
        try:
            c.execute(
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, result, scene_id "
                f"FROM events WHERE repo_key IN ({placeholders}) AND issue_id LIKE ? ORDER BY seq DESC",
                (*repo_keys, "#pr_%"))
            rows = c.fetchall()
            # Issue #1938：补偿归因（PR 订阅卡片同口径标注「系统补偿，原始回报丢失」）
            from src.domains.runtime.engine import cascade as _cascade
            _cascade_map = _cascade.fetch_display_meta(
                c, [r[0] for r in rows], {r[0]: (r[9] or "") for r in rows}) if rows else {}
        except Exception:
            rows = []
            _cascade_map = {}
    if not rows:
        return items
    # 按 PR 号聚合（取最新一条作为卡片状态，同时保留全量审查历史）
    _pr_map = {}  # pr_num → latest event
    _pr_history = {}  # pr_num → [event, ...]
    for r in rows:
        iid = r[1] or ""
        # #pr_{pr_num}_{scene_id} → 取 pr_num
        parts = iid.replace("#pr_", "").split("_", 1)
        pr_num = int(parts[0]) if parts and parts[0].isdigit() else 0
        if not pr_num:
            continue
        _ev = {
            "seq": r[0], "issue_id": iid, "event_type": r[2],
            "agent_type": r[3] or "", "title": r[4] or "",
            "actor": r[5] or "", "summary": r[6] or "",
            "status": r[7], "created_at": normalize_event_time(r[8]),
            "result": r[9] or "",
            "scene_id": r[10] if len(r) > 10 else "",
        }
        # Issue #1938：补偿记录显式标注，不计入 Agent 答复结论口径
        _cm = _cascade_map.get(r[0]) or {}
        _ev["cascade"] = _cm
        _ev["result_kind"] = _cm.get("result_kind", "agent")
        _ev["is_agent_conclusion"] = _cm.get("is_agent_conclusion", True)
        if pr_num not in _pr_map:
            _pr_map[pr_num] = _ev
        _pr_history.setdefault(pr_num, []).append(_ev)
    # 查场景名缓存
    from src.api.pipeline_config import get_pipeline_config
    _scene_name_cache = {}
    def _get_scene_name(sid):
        if not sid:
            return ""
        if sid not in _scene_name_cache:
            cfg = get_pipeline_config(sid, repo_key=repo_keys[0] if repo_keys else "")
            _scene_name_cache[sid] = (cfg or {}).get("display_name") or (cfg or {}).get("name", sid)
        return _scene_name_cache[sid]

    for pr_num, ev in _pr_map.items():
        _status = ev["status"]
        # 映射到需求中心状态
        if _status == "running":
            _task_status = "processing"
        elif _status == "completed":
            _task_status = "completed"
        elif _status == "failed":
            _task_status = "cancelled"
        else:
            _task_status = "pending"
        # Issue #542：qa_history 截最近 50 条（rows 按 seq DESC，最新在前），
        # 控制响应体积；events_count 仍为全量计数
        _qa_hist = _pr_history.get(pr_num, [])[:50]
        items.append({
            "id": -pr_num,  # 负数避免跟真实 task id 冲突
            "issue_number": pr_num,
            "title": ev["title"],
            "task_description": "",
            "conversation_id": "",
            "status": _task_status,
            "completed_reason": "",
            "created_at": ev["created_at"],
            "completed_at": "",
            "is_subtask_mode": 0,
            "parent_issue_number": 0,
            "parent_issue_id": "",
            "scene_id": "",
            "scene_name": "",
            "scene_source": "",
            "stage": "",
            "is_running": _status == "running",
            "agent_statuses": [],
            "repo_key": repo_keys[0] if repo_keys else "",
            "is_pr_event": True,
            "pr_seq": ev["seq"],
            "qa_history": [{**qa, "scene_name": _get_scene_name(qa.get("scene_id", ""))} for qa in _qa_hist],
            "events_count": sum(1 for r in rows if r[1] and r[1].replace("#pr_", "").split("_", 1)[0] == str(pr_num)),
            "latest_event": {"seq": ev["seq"], "status": ev["status"], "title": ev["title"], "created_at": ev["created_at"]},
            "assignee": ev["actor"],
        })
    return items

def _repo_key_of(owner: str, repo: str) -> str:
    from src.kernel.repo_key import to_repo_key
    return to_repo_key(f"{owner}/{repo}")

def _repo_keys_of(owner: str, repo: str) -> list[str]:
    """跨 provider 候选键全集（S104/D78：repo_key_candidates 卡派生）。

    用户可能经任意平台 Webhook 接入仓库，DB 前缀不定——手写双平台互查
    （gitcode↔github）会漏 gitee/gitlab；候选集含裸键+全平台前缀，
    repo_key 名空间全局唯一，超集查询安全。
    """
    from src.kernel.repo_key import repo_key_candidates
    return repo_key_candidates(f"{owner}/{repo}")


def _issue_stage_map(repo_keys: list[str], numbers: list[str] = None) -> dict:
    """仓库内 issue number → issue 阶段摘要（stage/owner/scene/assignee/subtask）。

    Phase C1/C3：`issue` 单表为权威（已收编 + 停写 issue_state），直接读 issue 卡片行；
    `issue_state` 仅作停写过渡期兜底。
    numbers（Issue #542）：可选 issue_number 集合，非空时按集合收窄（列表页只富化返回集合）。
    """
    from src.kernel.session_manager import db, db_lock
    out = {}
    placeholders = ",".join("?" * len(repo_keys))
    _num_clause = ""
    _num_params: list = []
    if numbers:
        _num_clause = f" AND issue_number IN ({','.join('?' * len(numbers))})"
        _num_params = list(numbers)
    # 1. issue 单表（权威）
    try:
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                f"SELECT id, issue_number, stage, owner_agent, assignee, a2a_seq, scene_id, "
                f"parent_issue_id, is_subtask_mode, scene_user_id, allow_scene_override "
                f"FROM issue WHERE repo_key IN ({placeholders}){_num_clause}",
                repo_keys + _num_params)
            for r in c.fetchall():
                n = str(r[1])
                if not n:
                    continue
                out[n] = {
                    "issue_id": f"#{n}", "internal_id": r[0], "stage": r[2], "owner_agent": r[3],
                    "assignee": r[4] or "", "a2a_seq": r[5],
                    "scene_id": r[6] or "", "parent_issue_id": r[7] or "",
                    "is_subtask_mode": r[8] or 0, "scene_user_id": r[9] or 0,
                    "allow_scene_override": r[10] or 0,
                }
    except Exception as e:
        log.warning(f"issues: load issue table failed: {e}")
    # 2. issue_state 兜底（无 issue 卡片行但仍有 issue_state 的过渡期数据）
    _iid_clause = ""
    _iid_params: list = []
    if numbers:
        _iids = [f"#{n}" for n in numbers]
        _iid_clause = f" AND issue_id IN ({','.join('?' * len(_iids))})"
        _iid_params = _iids
    try:
        with db_lock:
            c = _rt_db.cursor()
            c.execute(
                f"SELECT issue_id, stage, owner_agent, assignee, a2a_seq, scene_id, "
                f"parent_issue_id, is_subtask_mode, scene_user_id, allow_scene_override "
                f"FROM issue_state WHERE repo_key IN ({placeholders}){_iid_clause}",
                repo_keys + _iid_params)
            for r in c.fetchall():
                n = _num_from_issue_id(r[0])
                if not n or n in out:
                    continue
                out[n] = {
                    "issue_id": r[0], "stage": r[1], "owner_agent": r[2],
                    "assignee": r[3] or "", "a2a_seq": r[4],
                    "scene_id": r[5] or "", "parent_issue_id": r[6] or "",
                    "is_subtask_mode": r[7] or 0, "scene_user_id": r[8] or 0,
                    "allow_scene_override": r[9] or 0,
                }
    except Exception as e:
        log.warning(f"issues: load issue_state failed: {e}")
    return out

def _task_status_map(repo_keys: list[str]) -> dict:
    """仓库内 issue_number → tasks.status（任务中心状态）。"""
    from src.kernel.session_manager import db, db_lock
    out = {}
    placeholders = ",".join("?" * len(repo_keys))
    with db_lock:
        c = _rt_db.cursor()
        try:
            c.execute(f"SELECT issue_number, status FROM issue WHERE repo_key IN ({placeholders})", repo_keys)
            for r in c.fetchall():
                out[str(r[0])] = r[1]
        except Exception as e:
            log.warning(f"issues: load tasks status failed: {e}")
    return out

def _group_events_by_issue(repo_keys: list[str], numbers: list[str] = None) -> dict:
    """仓库内 issue_id → events 列表（seq 升序），带时间格式化。

    Issue #252: 排除 entity_type='pull_request' 的事件，PR 事件不混入 issue 展示
    （编号相同时 PR 事件与 issue 事件分离，避免关联混淆）。
    向后兼容：旧事件无 entity_type（空串），视为 issue 事件正常分组。
    numbers（Issue #542）：可选 issue number 集合，非空时 SQL 按 `issue_id IN ('#n',...)`
    收窄（events 表 issue_id 规范写路径恒为 `#N`；`#pr_` 伪 ID 由
    _query_pr_subscription_events 独立处理，不受影响）。
    """
    from src.kernel.session_manager import db, db_lock
    import json as _json
    out = {}
    placeholders = ",".join("?" * len(repo_keys))
    _num_clause = ""
    _num_params: list = []
    if numbers:
        _iids = [f"#{n}" for n in numbers]
        _num_clause = f" AND issue_id IN ({','.join('?' * len(_iids))})"
        _num_params = _iids
    with db_lock:
        c = _rt_db.cursor()
        try:
            # Issue #1938：补取 result 列（与事件列表/详情/PR 订阅三处调用点同口径），
            # 供 fetch_display_meta 兜底识别存量（#587 时代）补偿记录的历史固定文案。
            # 列序固定：0 seq … 8 created_at、9 entity_type（#252 PR 事件过滤）、
            # 10 runtime、11 skill、12 result、13 completed_at（Issue #2050：
            # 详情 view 的停滞判定要读末段完成时间，缺这列会让 stall_state 恒为 unknown）。
            # 本视图不取 structured_output/agent_logs（Issue 列表一次加载全仓库事件，
            # 不加载整份执行日志）。
            c.execute(
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, "
                f"created_at, entity_type, runtime, skill, result, completed_at "
                f"FROM events WHERE repo_key IN ({placeholders}) AND issue_id!=''{_num_clause} ORDER BY seq ASC",
                repo_keys + _num_params)
            rows = c.fetchall()
            # Issue #1938：补偿归因（Issue 流水线/处理记录同口径标注）
            from src.domains.runtime.engine import cascade as _cascade
            _cascade_map = _cascade.fetch_display_meta(
                c, [r[0] for r in rows], {r[0]: (r[12] or "") for r in rows}) if rows else {}
        except Exception:
            rows = []
            _cascade_map = {}
    for r in rows:
        iid = r[1]
        _entity_type = r[9] if len(r) > 9 else ""
        # Issue #252: PR 事件不混入 issue 分组
        if _entity_type == "pull_request":
            continue
        n = _num_from_issue_id(iid)
        if not n:
            continue
        item = {
            "seq": r[0], "event_type": r[2], "agent_type": r[3] or "",
            "title": r[4] or r[2], "actor": r[5] or "",
            "summary": r[6] or r[4] or r[2],
            "status": r[7], "created_at": normalize_event_time(r[8]),
            "runtime": r[10] if len(r) > 10 else "",
            "skill": r[11] if len(r) > 11 else "",
            # Issue #2050：末段完成时间（停滞判定输入；缺失时保持空串，
            # 由 compute_stall_state 明确判为 unknown，不回退 created_at）。
            "completed_at": (r[13] or "") if len(r) > 13 else "",
        }
        # Issue #1938：补偿记录标注 + 「Agent 答复结论」口径（合成补偿不计入）
        _cm = _cascade_map.get(r[0]) or {}
        item["cascade"] = _cm
        item["result_kind"] = _cm.get("result_kind", "agent")
        item["is_agent_conclusion"] = _cm.get("is_agent_conclusion", True)
        out.setdefault(n, []).append(item)
    return out

def _issue_title_map(repo_keys: list[str]) -> dict:
    """仓库内 issue_number → 标题（从 events 首条带 title 的事件取）。"""
    g = _group_events_by_issue(repo_keys)
    out = {}
    for n, evs in g.items():
        for ev in evs:
            if ev.get("title"):
                out[n] = ev["title"]
                break
    return out

def _assemble_issue(repo_key: str, number: str, stage_map: dict, status_map: dict,
                    group_map: dict, title_map: dict) -> dict:
    """聚合单个 Issue 展示对象（列表条目）。"""
    stage = stage_map.get(number) or {}
    events = group_map.get(number) or []
    latest = events[-1] if events else None
    # D105 字符串 ident：数字 ident 保持 int 契约不变；平台字符串 ident（gitee IK*）
    # 原样透传——对 IK* 裸强转 int 会抛 ValueError（2026-11-10 :3001 实测 500）。
    # Issue #3055：数字→int（#995/#2072 契约），平台 ident 原样 str
    return {
        "number": native_issue_number(number),
        # Issue #569：内部需求卡片 id（issue 表主键）——awaiting-input/pending-review/review-pass
        # 的 --issue_id 用此值；与 GitCode 侧 issue number 语义不同，与 git-repo issues get 的
        # id（GitCode 侧 id）不可混用。无 issue 卡片行（过渡期兜底）时为 null。
        "issue_id": stage.get("internal_id"),
        "title": title_map.get(number) or stage.get("issue_id") or f"Issue #{number}",
        # Issue #2111（#1214 移植）：枚举字段出口归一（or 兜 None/空串）——与 /mine 出口（_query_tasks
        # 空值归一 vod）同口径同值，消除 detail/mine 出口 stage 分歧（#1214 遗留）。
        "stage": stage.get("stage") or "vod",
        "task_status": status_map.get(number, ""),
        "owner_agent": stage.get("owner_agent") or "vod",
        "scene_id": stage.get("scene_id", ""),
        "assignee": stage.get("assignee", ""),
        "is_subtask_mode": stage.get("is_subtask_mode") or 0,
        "parent_issue_id": stage.get("parent_issue_id", ""),
        "events_count": len(events),
        "latest_event": latest,
    }

# 需求中心列表按状态分组并发取数（看板模式 6 个请求/次刷新），每个请求都会调
# _issue_attachments_batch；而它每次都要向 OBS 做前缀枚举（含 cursor 分页循环），
# 实测约 500ms 且是纯网络 IO。6 并发各自枚举一次 = 3.2s，叠加 db_lock 串行后
# 直接把首屏推过前端 8s 看门狗阈值，表现就是「需求中心一直加载中 → 加载失败」。
# temporary_url 有效期 7 天，缓存 15 秒既不会让新附件不可见，又能把并发打回 1 次。
# 锁内 double-check：避免 6 个线程同时 miss 后各自去枚举（缓存击穿）。
# （此缓存层曾在 clarify to_thread 提交中被误删，QA G1 判 BLOCKED；此处恢复。）
_ATTACH_CACHE: dict[tuple, tuple[float, dict]] = {}
_ATTACH_LOCK = __import__("threading").Lock()
_ATTACH_TTL = 15.0


def _issue_attachments_batch(repo_keys: list[str]) -> dict[str, list[dict]]:
    import copy as _copy
    import time as _time
    _ck = tuple(sorted(repo_keys or []))
    _now = _time.time()
    _hit = _ATTACH_CACHE.get(_ck)
    if _hit and (_now - _hit[0]) < _ATTACH_TTL:
        return _copy.deepcopy(_hit[1])
    with _ATTACH_LOCK:
        _hit = _ATTACH_CACHE.get(_ck)
        _now = _time.time()
        if _hit and (_now - _hit[0]) < _ATTACH_TTL:
            return _copy.deepcopy(_hit[1])
        _fresh = _issue_attachments_batch_uncached(repo_keys)
        _ATTACH_CACHE[_ck] = (_now, _copy.deepcopy(_fresh))
        if len(_ATTACH_CACHE) > 32:
            for _k in list(_ATTACH_CACHE)[:-32]:
                _ATTACH_CACHE.pop(_k, None)
        return _fresh


def _issue_attachments_batch_uncached(repo_keys: list[str]) -> dict[str, list[dict]]:
    """Issue #648 性能修复：按仓库前缀一次 ListObjects 拉全量附件清单，内存按 issue 编号分组。

    原实现（_issue_attachments）逐卡调一次 OBS list（~16-110ms），几百卡串行 = 十几秒，
    是需求中心列表接口慢的根因（#648 引入）。OBS/本地两种后端均走 storage 一次前缀枚举。
    返回 {issue_number_str: [ {filename,url,size,is_image}, ... ]}，无附件的编号不在 map 中。
    分页 cursor 循环拉全量（OBS 单次 max_keys=1000）。
    """
    result: dict[str, list[dict]] = {}
    for rk in repo_keys or []:
        try:
            from src.kernel.repo_key import split_owner_repo
            owner, rname = split_owner_repo(rk)
        except Exception as e:
            log.warning(f"issues: attachments batch skip bad repo_key={rk}: {e}")
            continue
        try:
            from src.domains.runtime.storage import get_storage
            from src.api.issues_center import _ATTACH_IMAGE_EXTS
            import os as _os
            storage = get_storage()
            if storage:
                from src.api.issues_center import _attach_obs_key
                # 仓库级前缀：{PREFIX}/issue_attachments/{owner}/{repo}/
                # list_objects_with_meta 返回的 key 已剥 OBS_PREFIX，形如
                # issue_attachments/{owner}/{repo}/{issue_number}/{filename}
                prefix_key = _attach_obs_key(owner, rname, "", "").rstrip("/")
                _prefix = prefix_key.rsplit("issue_attachments/", 1)[-1]
                _prefix = f"issue_attachments/{_prefix}".rstrip("/") + "/"
                _items: list[dict] = []
                _cursor: str | None = ""
                while _cursor is not None:
                    page, _cursor = storage.list_objects_with_meta(_prefix, cursor=_cursor, limit=1000)
                    _items.extend(page or [])
                for item in _items:
                    # key 形如 issue_attachments/{owner}/{repo}/{issue_number}/{filename}
                    rel = (item.get("key") or "")
                    if rel.startswith(_prefix):
                        rel = rel[len(_prefix):]
                    parts = rel.split("/", 1)
                    if len(parts) != 2 or not parts[0] or not parts[1]:
                        continue
                    num, fname = parts
                    ext = _os.path.splitext(fname)[1].lower()
                    result.setdefault(num, []).append({
                        "filename": fname,
                        "url": storage.temporary_url(item["key"], expires=7 * 24 * 3600),
                        "size": item.get("size", 0),
                        "is_image": ext in _ATTACH_IMAGE_EXTS,
                    })
            else:
                # 本地磁盘后端：一次 listdir 顶层目录 = 全部 issue 编号
                from src.api.issues_center import _attach_base_dir
                from src.domains.mgmt.public import get_browser_callback_url as _bcb
                _base_url = (_bcb() or "").rstrip("/")
                base = _attach_base_dir(owner, rname, "")
                if _os.path.isdir(base):
                    for num in _os.listdir(base):
                        d = _os.path.join(base, num)
                        if not _os.path.isdir(d):
                            continue
                        for fname in sorted(_os.listdir(d)):
                            fpath = _os.path.join(d, fname)
                            if not _os.path.isfile(fpath):
                                continue
                            ext = _os.path.splitext(fname)[1].lower()
                            result.setdefault(str(num), []).append({
                                "filename": fname,
                                "url": f"{_base_url}/static/issue_attachments/{owner}/{rname}/{num}/{fname}",
                                "size": _os.path.getsize(fpath),
                                "is_image": ext in _ATTACH_IMAGE_EXTS,
                            })
        except Exception as e:
            log.warning(f"issues: attachments batch failed repo={rk}: {e}")
            continue
    return result

def _issue_attachments(repo_keys: list[str], issue_number) -> list[dict]:
    """Issue #648：聚合 issue 附件（复用 issues_center._attach_list，不新建存储）。

    只取已建单附件目录（str(issue_number)），pending 为建单前临时目录不入 issue 维度，
    避免跨 issue 污染。按 repo_key 顺序探测，首个命中即返回；异常/无附件降级为空列表，
    不阻塞详情/列表主流程。
    """
    if issue_number is None or str(issue_number).strip() == "":
        return []
    from src.api.issues_center import _attach_list
    from src.kernel.repo_key import split_owner_repo
    for rk in repo_keys or []:
        try:
            owner, rname = split_owner_repo(rk)
            atts = _attach_list(owner, rname, str(issue_number))
        except Exception as e:
            log.warning(f"issues: list attachments failed repo={rk} #{issue_number}: {e}")
            continue
        if atts:
            return atts
    return []

def _issue_attachments_count(repo_keys: list[str], issue_number) -> int:
    """Issue #926：仅取附件数量（分页瘦身专用）。

    与 _issue_attachments 同探测口径（按 repo_key 顺序，首个命中即返回），
    但跳过 _attach_list 的 URL 预签名/元数据构造，深列表页 I/O 显著低于全量取清单。
    """
    if issue_number is None or str(issue_number).strip() == "":
        return 0
    from src.api.issues_center import _attach_count
    from src.kernel.repo_key import split_owner_repo
    for rk in repo_keys or []:
        try:
            owner, rname = split_owner_repo(rk)
            n = _attach_count(owner, rname, str(issue_number))
        except Exception as e:
            log.warning(f"issues: count attachments failed repo={rk} #{issue_number}: {e}")
            continue
        if n:
            return n
    return 0

def _pipeline_from_archive(repo_keys: list[str], number: str) -> dict | None:
    """读 event.json 组装 pipeline 路线图（跨 provider 双查）。"""
    from src.api.event_archive import load_event_json
    for rk in repo_keys:
        try:
            arch = load_event_json(rk, number)
            if not arch:
                continue
            segments = []
            for seg in arch.get("pipeline", []):
                op = seg.get("output", {}) or {}
                segments.append({
                    "seq": seg.get("seq"),
                    "agent": seg.get("agent", ""),
                    "event_type": seg.get("event_type", ""),
                    "status": seg.get("status", ""),
                    "runtime": seg.get("runtime", ""),
                    "skill": seg.get("skill", ""),
                    "output": {
                        "conclusion": op.get("conclusion", ""),
                        "next_step": op.get("next_step", ""),
                        "actions": op.get("actions", []),
                        "artifacts": op.get("artifacts", []),
                        "issue_summary": op.get("issue_summary", ""),
                    },
                })
            return {
                "issue": arch.get("issue", {}),
                "summary": arch.get("summary", ""),
                "status": arch.get("status", ""),
                "segments": segments,
            }
        except Exception as e:
            log.warning(f"issues: load pipeline archive failed repo={rk} #{number}: {e}")
    return None

def _pipeline_from_events(repo_key: str, number: str, events: list[dict]) -> dict:
    """由 events 表重构 pipeline（weekly-05 主路径），output 用 structured_output。"""
    segments = []
    for ev in events:
        seg = {
            "seq": ev["seq"], "agent": ev.get("agent_type", ""),
            "event_type": ev.get("event_type", ""),
            "status": ev.get("status", ""),
            "created_at": ev.get("created_at", ""),
            "completed_at": ev.get("completed_at", ""),
            "runtime": ev.get("runtime", ""), "skill": ev.get("skill", ""),
            "output": {
                "conclusion": ev.get("summary", ""),
                "next_step": "",
                "actions": [], "artifacts": [],
                "issue_summary": ev.get("summary", ""),
            },
            "logs": ev.get("logs", []),
        }
        _out = ev.get("output")
        if isinstance(_out, dict):
            seg["output"] = {
                "conclusion": _out.get("conclusion", ev.get("summary", "")),
                "next_step": _out.get("next_step", ""),
                "actions": _out.get("actions", []),
                "artifacts": _out.get("artifacts", []),
                "issue_summary": _out.get("issue_summary", ev.get("summary", "")),
            }
        # Issue #1938：补偿记录不得伪装成 Agent 答复——
        # ① 结构化补偿归因随 segment 下发；② 合成补偿的 conclusion 显式标注「系统补偿，原始回报丢失」
        _cm = ev.get("cascade") or {}
        if _cm.get("system_cascade"):
            seg["cascade"] = _cm
            seg["result_kind"] = _cm.get("result_kind", "")
            seg["is_agent_conclusion"] = _cm.get("is_agent_conclusion", False)
            if not _cm.get("is_agent_conclusion"):
                _label = _cm.get("cascade_label") or "系统补偿，原始回报丢失"
                seg["output"]["conclusion"] = _label
                seg["output"]["issue_summary"] = _label
        else:
            seg["is_agent_conclusion"] = True
        segments.append(seg)
    from src.domains.runtime.vcs.api_client import _native_issue_number
    return {"issue": {"number": _native_issue_number(number)},
            "summary": (segments[-1]["output"]["issue_summary"] if segments else ""),
            "status": (segments[-1]["status"] or "unknown") if segments else "unknown",
            "segments": segments}

@router.post("/{issue_id}/start", operation_id="issues_start", summary="Open issue session", description="Opens the Issue conversation entry: validates ownership and returns the current status without flipping it. User-only (front-end navigation preflight).")
def start_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """打开 Issue 会话（"去对话"）——仅校验归属并返回当前状态，不翻转状态。

    user_only：纯前端导航预检（确认可访问 + 取当前状态），Agent 执行期（tk_ 会话）无此场景；
    与周边操作类端点（confirm-done/cancel/resume/clarify 等）保持一致，收缩暴露面。
    """
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT user_id, status FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        # D105: user["id"] is str; MySQL rows return int — normalize before compare
        if str(row[0]) != str(user["id"]):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作该需求"})
        return {"status": row[1]}

@router.get("/{issue_id}/status", operation_id="issues_status_get", summary="Query issue status (polling)", description="Returns the Issue status with context for CLI 'grape issues wait' polling.")
def get_issue_status(issue_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """返回 Issue 当前状态（含 repo/issue 上下文），供 CLI `grape issues wait` 轮询至终态。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "SELECT id, status, repo_key, issue_number, title, completed_reason, completed_at "
            "FROM issue WHERE id=?",
            (issue_id,),
        )
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    # Issue #2072: 数值形态归一为 int，字母 ident（gitee IKSQ4O）原样透传
    # （与 _query_tasks / 详情 _assemble_issue 同口径，禁止无条件 int() 强转）
    from src.domains.runtime.vcs.api_client import _native_issue_number
    return {
        "id": row[0], "status": row[1], "repo_key": row[2],
        "issue_number": _native_issue_number(row[3]),
        "title": row[4], "completed_reason": row[5] or "", "completed_at": row[6],
    }

class _IssueStatusBody(BaseModel):
    status: str = ""

def _write_issue_status(c, issue_id: int, ns: str, revert_to_review: bool) -> None:
    """落库 issue 状态（调用方已持 db_lock 并负责 commit）。

    Issue #1408：completed → pending_review 为受控撤销出口，同一条 UPDATE 内回退
    status/stage 并清空完成信息；仅写这几列，保留 assignee/owner_agent/a2a_seq 等归属与
    平台同步字段（避免抹掉 GitCode 责任人并造成审计归属失真）。
    """
    if revert_to_review:
        # 带 status 守卫：多进程/并发下若该需求已被改成其它状态，UPDATE 不命中，
        # 按 409 返回并跳过审计（避免覆盖并发写入，与 session_store 状态写路径一致）。
        c.execute(
            "UPDATE issue SET status='pending_review', stage='pending_review', "
            "completed_at=NULL, completed_reason=NULL, updated_at=datetime('now') "
            "WHERE id=? AND status='completed'",
            (issue_id,)
        )
        if c.rowcount == 0:
            from src.kernel.session_manager import db as _db
            _rt_db.rollback()
            raise HTTPException(
                status_code=409,
                detail={"code": 409, "message": "需求状态已变更，请刷新后重试"})
        return
    c.execute(
        "UPDATE issue SET status=?, "
        "completed_at=CASE WHEN ? IN ('completed','cancelled') THEN datetime('now') ELSE completed_at END, "
        "completed_reason=CASE WHEN ?='completed' THEN 'manual' WHEN ?='cancelled' THEN 'cancelled' ELSE completed_reason END "
        "WHERE id=?",
        (ns, ns, ns, ns, issue_id)
    )

@router.put("/{issue_id}/status", operation_id="issues_status", summary="Update issue status", description="Transitions Issue status (active/processing/awaiting_input/pending_review/completed/cancelled flow). User-only: user-facing status is mutable by the owner, and the completed->pending_review revert is additionally open to repository Owner/Admin members (real repo role check); session token -> 403.")
def change_issue_status(issue_id: int, body: _IssueStatusBody, user: dict = Depends(require_no_session_token)):
    """Issue 状态迁移（active/processing/awaiting_input/pending_review/completed/cancelled 流转规则）。

    user_only：状态是**需求中心视角（用户可读）**，且 awaiting_input/completed/cancelled 允许用户直接
    设置（高优先级覆盖）；若放行 tk_ 会话 token，Agent 执行期可随意改状态，与 pipeline 驱动的 stage
    脱钩→冲突/不一致，干扰任务执行。故仅用户（JWT / grape_）可改；读（GET /status，轮询 wait 用）保持 dual。

    Issue #1408：`completed → pending_review`（撤销完成）是唯一放行**仓库管理员**的迁移——需求本人照旧
    可撤销，仓库 Owner/Admin 也可撤销**他人**误完成的需求（参照 admin-confirm-done 的
    `get_repo_admin_identity` fail-closed 真实角色校验）。其余迁移仍严格仅限本人。
    """
    from src.api.issues_center import _TASK_STATUS_FLOWS
    from src.kernel.session_manager import db, db_lock
    ns = (body.status or "").strip()
    if ns not in _TASK_STATUS_FLOWS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"非法状态: {ns}"})
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "SELECT user_id, status, repo_key, issue_number, stage, completed_at, completed_reason "
            "FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        cur = row[1]
        # Issue #1408：撤销完成是唯一的管理员例外出口（completed → pending_review）
        _revert_to_review = (cur == "completed" and ns == "pending_review")
        # D105: user["id"] is str; MySQL rows return int — normalize before compare
        _is_owner = str(row[0]) in (str(user["id"]), "0")
        if not _is_owner and not _revert_to_review:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作该需求"})
        if cur != ns and ns not in _TASK_STATUS_FLOWS.get(cur, ()):
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"不允许从 {cur} 迁移到 {ns}"})
        # 本人：保持原语义——读改写在同一把 db_lock 内完成（单进程原子）。
        # 非本人：需先做仓库角色校验（VCS 网络调用），故仅此时出锁鉴权，再带 status 守卫写回。
        if _is_owner:
            _write_issue_status(c, issue_id, ns, _revert_to_review)
            _rt_db.commit()
    _admin_ident = None
    if not _is_owner:
        # 仓库访问 + 真实角色校验（fail-closed：未绑定/查询失败均视为非 admin），
        # 与 admin-confirm-done 同款口径；VCS 权限查询可能耗时，故在 db_lock 之外执行。
        from src.api.deps import check_repo_access
        from src.domains.mgmt.public import get_repo_admin_identity
        check_repo_access(user, row[2])
        _admin_ident = get_repo_admin_identity(user, row[2])
        if not _admin_ident:
            raise HTTPException(status_code=403, detail={
                "code": 403,
                "message": "无权操作该需求：仅需求本人或仓库 Owner/Admin 可撤销已完成需求"})
        with db_lock:
            _write_issue_status(_rt_db.cursor(), issue_id, ns, _revert_to_review)
            _rt_db.commit()
    if _revert_to_review:
        _actor_role = "admin" if _admin_ident else "owner"
        _actor_name = (_admin_ident or {}).get("username") or user.get("username", "")
        try:
            from src.domains.mgmt.public import log_audit
            log_audit("issue_reverted_to_pending_review", operator_id=user.get("id", 0) or 0,
                      detail={
                          "repo_key": row[2],
                          "issue_id": issue_id,
                          "issue_number": row[3],
                          "username": _actor_name,
                          "actor_role": _actor_role,
                          "role_source": (_admin_ident or {}).get("source", ""),
                          "from_status": cur,
                          "to_status": ns,
                          "from_stage": row[4] or "",
                          "to_stage": "pending_review",
                          "prev_completed_at": row[5] or "",
                          "prev_completed_reason": row[6] or "",
                      })
        except Exception as _e:
            log.warning(f"issue-status: revert audit log failed (issue={issue_id}): {_e}")
        log.info(f"Issue #{issue_id} reverted completed → pending_review by user {user.get('id', 0)} "
                 f"(role={_actor_role})")
    # Issue #2419 修复项 2：状态真实迁移入审核点（pending_review / awaiting_input）→ 推飞书卡片
    # （同状态重写 cur==ns 非真实变更，不通知——幂等守卫）
    if ns in ("pending_review", "awaiting_input") and cur != ns:
        from src.domains.runtime.issues_center import maybe_notify_review_point_status
        maybe_notify_review_point_status(row[2], row[3], ns)
    return {"ok": True, "issue_id": issue_id, "status": ns}

class _IssueConfirmBody(BaseModel):
    confirmation: str = "user_confirmed"

def _close_vcs_issues(repo_key: str, issue_number, user: dict) -> dict:
    """confirm-done 后同步关闭各平台 Issue（降级不阻断）。

    Returns:
        {<provider>: "success|failed|skipped|bot_token_failed", ..., "message": str}
        — 主平台（repo_key 自身平台）始终尝试关闭（无 issue_number 时 skipped）；
        同仓多平台镜像绑定逐一尝试关闭（S104/D78 候选集）。
        bot_token_failed: bot token 可能失效，issue 未关闭（方向3 降级提示）。
        键按 provider 派生；gitcode/github 两键恒在（兼容旧消费方）。
    """
    result = {"gitcode": "skipped", "github": "skipped", "message": ""}
    if issue_number in (None, "", 0):
        return result
    # D105 字符串 ident（gitee IK*）：保持平台原生形态透传（_native_issue_number），
    # 不做 int() 强转——此前 int('IKUFPS') 必炸走早退，主平台键（gitee）从未写入
    # result，VCS 侧永不自动关闭（V1 实测：confirm-done 后 gitee issue 仍 open）。
    from src.domains.runtime.vcs.api_client import _native_issue_number
    issue_number = _native_issue_number(issue_number)
    # Issue #3055：原生形态即判据——数字编号恒 int，平台 ident 恒 str
    _ident_is_numeric = isinstance(issue_number, int)

    # 旧业务语义（用户裁定 2026-11-10 保留原方向性）：镜像外关仅
    # 「非 github 主仓 → 其 github 孪生行」单向（#656 口径）。github 主仓
    # 永不外关（含同名仓）；我们分支 194eaf317 的双向全平台口径被用户否决。
    from src.kernel.repo_key import parse_repo_key
    _primary = parse_repo_key(repo_key)[0]
    result[_primary] = _try_close_issue_for_repo(repo_key, issue_number, user)
    if _primary != "github" and _ident_is_numeric:
        # 字符串 ident（gitee IK*）无数字语义，github 孪生镜像关闭无从对齐编号，
        # 保持 skipped（主平台关闭不受影响）。
        from src.api.issues_center import _mirror_repo_keys
        for _m_rk in _mirror_repo_keys(repo_key):
            _m_p = parse_repo_key(_m_rk)[0]
            if _m_p != "github":
                continue  # 只外关 github 孪生（旧口径，单向）
            result[_m_p] = _try_close_issue_for_repo(_m_rk, issue_number, user)
    # 无绑定 → github 键保持 skipped

    # Issue #345: bot_token_failed 时设置用户可见提示
    if "bot_token_failed" in [v for v in result.values() if isinstance(v, str)]:
        from src.domains.runtime.vcs.token_error import BOT_TOKEN_INVALID_MSG
        result["message"] = f"Issue 关闭失败：{BOT_TOKEN_INVALID_MSG}"

    return result

def _try_close_issue_for_repo(repo_key: str, issue_number: int, user: dict) -> str:
    """对单个 repo_key 执行幂等关闭 Issue，返回 success/failed/skipped。"""
    from src.kernel.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    if not owner or not repo:
        return "skipped"

    # 获取 token — Issue #343: 写操作走 bot token 优先，与建单路径对称
    # Issue #343 修复: bot token 失败时 fallback 到用户 token，用户 token 路径走 with_oauth_token
    token = ""
    _use_user_token_fallback = False
    try:
        # Issue #343: 写操作走仓库 bot token 优先（跳过 agent session token）。
        # 2026-11-13 if/else 收口：单一读链 get_repo_token_for——provider 基类
        # resolve_repo_token 派生（TokenResolver 策略链 + 卡能力位
        # owner_identity_token_fallback 的 owner-identity 回落，原 github 专属
        # get_github_token 尾段语义），所有平台含新平台同链零分支。
        from src.domains.mgmt.public import get_repo_token_for as _get_rt
        token = _get_rt(repo_key=repo_key) or ""
        if not token:
            # bot token 未配置，fallback 用户 token（走 with_oauth_token 保护）
            _use_user_token_fallback = True
    except Exception as e:
        log.warning(f"confirm-done close_issue: get token for {repo_key} failed: {e}")

    if not token and not _use_user_token_fallback:
        log.info(f"confirm-done close_issue: no token for {repo_key}, skipping")
        return "skipped"

    # Issue #343 修复: 用户 token fallback 路径走 with_oauth_token 保护（预刷新 + 401 重试）
    if _use_user_token_fallback and not token:
        # bot token 未配置，通过 with_oauth_token 获取用户 token 并执行关闭
        try:
            from src.api.oauth_token import with_oauth_token
            from src.domains.runtime.vcs.api_client import VcsApiClient
            _provider_name = provider
            def _do_close(token, _provider=provider, _owner=owner, _repo=repo, _num=issue_number):
                c = VcsApiClient(_provider, token, _owner, _repo)
                # 幂等检查
                state = c.get_issue_state(_num)
                if state == "closed":
                    return "skipped"
                resp = c.close_issue(_num)
                if isinstance(resp, dict) and "error" not in resp:
                    return "success"
                return "failed"
            result = with_oauth_token(user, _provider_name, _do_close)
            if result == "success":
                log.info(f"confirm-done: closed {repo_key}#{issue_number} (user token)")
            return result
        except Exception as e:
            log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} user token failed: {e}")
            return "failed"

    # bot token 路径：直接调用
    from src.domains.runtime.vcs.api_client import VcsApiClient
    client = VcsApiClient(provider, token, owner, repo)

    # 幂等检查：已 closed 则跳过
    try:
        state = client.get_issue_state(issue_number)
        if state == "closed":
            log.info(f"confirm-done: {repo_key}#{issue_number} already closed, skipping")
            return "skipped"
    except Exception as e:
        log.warning(f"confirm-done: get_issue_state {repo_key}#{issue_number} failed: {e}")
        # 查询失败不阻断关闭尝试

    # 执行关闭
    try:
        resp = client.close_issue(issue_number)
        if isinstance(resp, dict) and "error" not in resp:
            log.info(f"confirm-done: closed {repo_key}#{issue_number}")
            return "success"
        # Issue #345: 识别 401 返回 Bot Token 失效标记
        from src.domains.runtime.vcs.token_error import is_auth_error_result
        if is_auth_error_result(resp):
            log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} → Bot Token 可能失效")
            return "bot_token_failed"
        log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} → {resp}")
        return "failed"
    except Exception as e:
        # Issue #345: 异常路径也检查 401
        from src.domains.runtime.vcs.token_error import is_auth_error_exc
        if is_auth_error_exc(e):
            log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} → Bot Token 可能失效 (exc)")
            return "bot_token_failed"
        log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} exception: {e}")
        return "failed"

def _find_github_mirror_repo_key(repo_key: str) -> str:
    """查找 GitCode 仓库绑定的 GitHub 镜像 repo_key。

    managed_repos 表中同一 owner/repo 可能同时存在 gitcode: 和 github: 两条记录，
    通过 full_name 匹配。找不到返回空串。

    注：confirm-done 的实际路径已走 `_close_vcs_issues` → `issues_center._mirror_repo_keys`，
    本函数是同名逻辑的遗留副本（无调用点）；保留是为了不改变历史行为，
    跨域读已收口到 mgmt public 的 `repo_key_exists_any`，与 issues_center 的同名实现同口径。
    """
    try:
        from src.kernel.repo_key import split_owner_repo
        from src.domains.mgmt import public as _mgmt_public_mirror
        owner, repo = split_owner_repo(repo_key)
        if not owner or not repo:
            return ""
        gh_key = f"github:{owner}/{repo}"
        # managed_repos 归 mgmt：命中时返回库里的 repo_key 原值（列序/谓词同原语句），
        # 无行返回空串——原实现的两种拓扑（mgmt lib / 本地兜底 SQL）在这里收成一条 owner 原语。
        return _mgmt_public_mirror.repo_key_exists_any(gh_key)
    except Exception as e:
        log.warning(f"confirm-done: find github mirror for {repo_key} failed: {e}")
        return ""

@router.post("/{issue_id}/confirm-done", operation_id="issues_confirm_done", summary="Confirm issue done", description="Developer confirms the requirement is done: status -> completed, stage -> done (idempotent in terminal state).")
def confirm_issue_done(issue_id: int, body: _IssueConfirmBody, user: dict = Depends(require_no_session_token)):
    """开发者确认需求完成：status→completed、stage→done（任意阶段可确认，终态幂等）。"""
    from src.api.session_store import set_issue_stage
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number, status FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
                  (issue_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        _repo_key, _issue_num, _status = row[0], row[1], row[2]
        if _status == 'completed' or _status == 'done':
            # 幂等：已关闭的 Issue 再次 confirm-done 不重复调用关闭 API
            return {"ok": True, "already_done": True,
                    "issue_closed": {"gitcode": "skipped", "github": "skipped"}}
        if _status == 'cancelled':
            raise HTTPException(status_code=400, detail={"code": 400, "message": "已取消需求不可确认完成"})
        c.execute(
            "UPDATE issue SET status='completed', completed_reason=?, completed_at=datetime('now') WHERE id=?",
            (body.confirmation, issue_id))
        if _issue_num:
            set_issue_stage(_repo_key, f"#{_issue_num}", "done", "user")
        _rt_db.commit()
    log.info(f"Issue #{issue_id} confirmed done by {body.confirmation}")
    _issue_closed = _close_vcs_issues(_repo_key, _issue_num, user)
    return {"ok": True, "issue_id": issue_id, "status": "completed", "stage": "done",
            "issue_closed": _issue_closed}

def _sync_issue_pipeline_terminal(repo_key: str, issue_id_num) -> int:
    """Issue #393: admin 确认完成时，同步关联流水线到终态。

    - agent_tasks 中该需求未终态（queued/dispatched/running）的任务 → cancelled
    - events 中该需求 running 的事件 → completed（管理员终审视为正常收口，非失败）

    与 cancel_issue 不同：不做运行时 kill（管理员确认发生在流水线已收尾的场景，
    残留 running 属于脏状态，直接收敛，避免误杀仍在写回的引擎）。
    返回收敛的任务数。
    """
    from src.kernel.session_manager import db, db_lock
    _issue_id = f"#{issue_id_num}" if issue_id_num else ""
    if not _issue_id:
        return 0
    _synced = 0
    with db_lock:
        c = _rt_db.cursor()
        try:
            c.execute(
                "UPDATE agent_tasks SET status='cancelled', completed_at=COALESCE(completed_at, datetime('now')) "
                "WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running')",
                (repo_key, _issue_id))
            _synced += c.rowcount if c.rowcount and c.rowcount > 0 else 0
        except Exception as _e:
            log.warning(f"admin-confirm: sync agent_tasks failed: {_e}")
        try:
            c.execute(
                "UPDATE events SET status='completed' "
                "WHERE repo_key=? AND issue_id=? AND status='running'",
                (repo_key, _issue_id))
        except Exception as _e:
            log.warning(f"admin-confirm: sync events failed: {_e}")
        _rt_db.commit()
    # Issue #1667：admin 终审直接 SQL 把 events 置 completed，绕过了终态钩子；
    # 对账扫描兜底派发因此滞留的排队 A2A 事件（Spec 风险表要求）。
    try:
        from src.domains.runtime.pipeline.event_pipeline import reconcile_queued_a2a
        reconcile_queued_a2a(repo_key)
    except Exception as _qe:
        log.warning(f"admin-confirm: a2a queue reconcile failed: {_qe}")
    return _synced

@router.post("/{issue_id}/admin-confirm-done", operation_id="issues_admin_confirm_done",
             summary="Admin confirms issue done (final review)",
             description="Repository admin confirms the requirement is done: validates the caller's real repo role "
                         "(owner/admin via VCS permission API, cf. #377), then status -> completed, stage -> done, "
                         "syncs related pipeline tasks to terminal state, writes an audit log, and closes the VCS "
                         "issue (degraded, non-blocking). Idempotent in terminal state; non-admin -> 403.")
def admin_confirm_issue_done(issue_id: int, user: dict = Depends(require_no_session_token)):
    """Issue #393: 仓库 admin 成员确认需求完成（人工终审）。

    与开发者 confirm-done 的区别：
    - 不限归属（admin 可确认仓库内任何需求，含他人创建），但强制**仓库真实角色校验**；
    - 审计留痕（issue_admin_confirmed：操作人/时间/目标需求）；
    - 同步关联流水线任务到终态。

    幂等：completed → already_done:true（不重复关 VCS、不重复审计）；cancelled → 400。
    """
    from src.api.session_store import set_issue_stage
    from src.domains.mgmt.public import get_repo_admin_identity
    from src.kernel.session_manager import db, db_lock

    # 1) 定位需求（admin 可跨归属确认，仅要求仓库访问权）
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number, status FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    _repo_key, _issue_num, _status = row[0], row[1], row[2]

    # 2) 仓库访问校验（managed_repos 归属，与其他 repo 维度端点一致）
    from src.api.deps import check_repo_access
    check_repo_access(user, _repo_key)

    # 3) 仓库真实角色校验（fail-closed：未绑定/查询失败均视为非 admin）
    _admin_ident = get_repo_admin_identity(user, _repo_key)
    if not _admin_ident:
        raise HTTPException(status_code=403, detail={
            "code": 403,
            "message": "需要仓库管理员权限：仅仓库 Owner/Admin 成员可确认需求完成"})

    # 4) 状态机
    if _status == 'completed' or _status == 'done':
        return {"ok": True, "already_done": True, "confirmed_by": "admin",
                "issue_closed": {"gitcode": "skipped", "github": "skipped"}}
    if _status == 'cancelled':
        raise HTTPException(status_code=400, detail={"code": 400, "message": "已取消需求不可确认完成"})

    # 5) 终态落库 + 流水线收敛（同一 db_lock 串行，防并发双写）
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "UPDATE issue SET status='completed', completed_reason='admin_confirmed', "
            "completed_at=datetime('now') WHERE id=?", (issue_id,))
        if _issue_num:
            set_issue_stage(_repo_key, f"#{_issue_num}", "done", "admin")
        _rt_db.commit()
    _synced_tasks = _sync_issue_pipeline_terminal(_repo_key, _issue_num)

    # 6) 审计留痕（操作人/时间/目标需求；log_audit 自带 created_at + operator_id）
    try:
        from src.domains.mgmt.public import log_audit
        log_audit("issue_admin_confirmed", operator_id=user.get("id", 0) or 0,
                  detail={
                      "repo_key": _repo_key,
                      "issue_id": issue_id,
                      "issue_number": _issue_num,
                      "username": _admin_ident.get("username", ""),
                      "role_source": _admin_ident.get("source", ""),
                      "synced_tasks": _synced_tasks,
                  })
    except Exception as _e:
        log.warning(f"admin-confirm: audit log failed (issue={issue_id}): {_e}")

    log.info(f"Issue #{issue_id} admin-confirmed done by {_admin_ident.get('username', '')} "
             f"(repo={_repo_key}, synced_tasks={_synced_tasks})")

    # 7) VCS Issue 同步关闭（降级不阻断，与 confirm-done 同策略）
    _issue_closed = _close_vcs_issues(_repo_key, _issue_num, user)
    return {"ok": True, "issue_id": issue_id, "status": "completed", "stage": "done",
            "confirmed_by": "admin", "synced_tasks": _synced_tasks,
            "issue_closed": _issue_closed}

@router.post("/{issue_id}/cancel", operation_id="issues_cancel", summary="Cancel issue (stop pipeline)", description="Developer cancels the requirement from any stage: terminates agents and stops the pipeline.")
async def cancel_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """开发者取消需求：任何阶段可取消，Agent 终止 + 流水线停止。"""
    from src.domains.runtime.issue_cancel import cancel_issue_core

    return cancel_issue_core(issue_id, user_id=user["id"])

@router.post("/{issue_id}/resume", operation_id="issues_resume", summary="Resume cancelled issue", description="Resumes a cancelled requirement from its pre-cancel stage.")
async def resume_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """恢复已取消的需求：回到取消前 stage 继续流转。"""
    from src.api.session_store import get_issue_stage
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT status, repo_key, issue_number FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
                  (issue_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        status, repo_key, issue_number = row[0], row[1], row[2]
        if status != 'cancelled':
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仅已取消需求可恢复"})
        # Issue #1741：同一 repo_key+issue_number 多行全量幂等恢复（只改状态，不插入新卡，
        # 避免恢复后产生重复卡片）。
        if repo_key and issue_number:
            c.execute(
                "UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status='cancelled'",
                (repo_key, issue_number))
        else:
            c.execute(
                "UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL WHERE id=?",
                (issue_id,))
        _rt_db.commit()
    _issue_id = f"#{issue_number}" if issue_number else ""
    if _issue_id:
        from src.api.event_store import get_events
        _events = get_events(repo_key, issue_id=_issue_id, limit=50)
        _last_completed_agent = ""
        for _e in reversed(_events):
            if _e.get("status") == "completed" and _e.get("agent_type"):
                _last_completed_agent = _e["agent_type"]
                break
        from src.api.pipeline_config import get_pipeline_config
        from src.api.session_store import get_issue_scene
        _scene_tuple = get_issue_scene(repo_key, _issue_id)
        _scene_id = (_scene_tuple[0] if _scene_tuple and _scene_tuple[0] else "") or "default"
        _pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key)
        _nodes = (_pipe_cfg or {}).get("nodes", [])
        _resume_stage = ""
        if _last_completed_agent:
            for _n in _nodes:
                if _n.get("id") == _last_completed_agent:
                    _resume_stage = _n.get("next", "")
                    break
        if not _resume_stage:
            _resume_stage = _nodes[0].get("id", "vod") if _nodes else "vod"
        if _resume_stage and _resume_stage not in ("done", ""):
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, _issue_id, _resume_stage, "resume")
            try:
                import urllib.request, json as _json
                # Issue #1858：恢复续办走 A2A 控制面基址（含可选网关 /a2a 前缀）。
                from src.kernel.prompt_builder import resolve_a2a_base
                _a2a_base = resolve_a2a_base()
                if not _a2a_base:
                    raise RuntimeError("A2A base_url unresolved")
                _a2a_url = _a2a_base.rstrip("/") + "/api/a2a"
                _a2a_payload = _json.dumps({
                    "agent": _resume_stage,
                    "payload": {
                        "repository": repo_key.replace("gitcode:", ""),
                        "issue_number": issue_number,
                        "context": f"resume from cancelled, last_completed={_last_completed_agent}, next={_resume_stage}",
                    }
                }).encode()
                _req = urllib.request.Request(
                    _a2a_url,
                    data=_a2a_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST")
                # 同步 HTTP POST 不能在事件循环里裸跑（5s 超时会连带卡住整个进程）
                await asyncio.to_thread(urllib.request.urlopen, _req, timeout=5)
                log.info(f"Issue #{issue_id} resumed → re-dispatched to stage={_resume_stage}")
            except Exception as _e:
                from src.kernel.prompt_builder import format_base_url_candidates
                try:
                    _candidates = format_base_url_candidates("/api/a2a")
                except Exception:
                    _candidates = ""
                log.warning(f"Issue #{issue_id} resume re-dispatch failed: {_e}; {_candidates}")
    log.info(f"Issue #{issue_id} resumed by user")
    return {"ok": True, "issue_id": issue_id, "status": "processing"}

@router.post("/{issue_id}/reopen", operation_id="issues_reopen", summary="Reopen completed issue", description="Reverts a mistakenly completed requirement back to processing: clears completed_at/completed_reason and blanks stage so it stops reading as done. Does NOT auto redispatch (re-dispatch from the event page when you want the pipeline to continue). Owner-only, multi-row idempotent; the platform Issue itself is not reopened.")
def reopen_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """把误标完成的需求退回「处理中」（2026-11-20 主人口径 B）。

    与 `resume`（cancelled→processing，自动续派）的区别：
    - 只回状态 + 清完成信息 + **清空 stage**，不自动 A2A 派发，避免点一下就消耗引擎；
      续跑由用户在事件页手动「重派」决定从哪一步开始；
    - stage 必须清空：UI 的 `mapStatusL` 把 `stage='done'` 直接判成「已完成」，
      留着 done 即使 status 已是 processing 也仍然显示已完成（等于按钮没生效）。
    不碰平台 Issue（GitCode/GitHub issue 不重新打开）——#1408 撤销路径同口径。
    """
    from src.kernel.session_manager import db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute(
            "SELECT status, repo_key, issue_number, stage, completed_at "
            "FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
            (issue_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        cur_status, repo_key, issue_number, cur_stage, prev_completed_at = row[0], row[1], row[2], row[3], row[4]
        if cur_status != "completed":
            raise HTTPException(
                status_code=400,
                detail={"code": 400, "message": "仅已完成需求可回到处理中"})
        # Issue #1741 口径：同一 repo_key+issue_number 可能有多行，全量幂等回退（不插新卡）。
        if repo_key and issue_number:
            c.execute(
                "UPDATE issue SET status='processing', stage='', completed_at=NULL, "
                "completed_reason=NULL, updated_at=datetime('now') "
                "WHERE repo_key=? AND issue_number=? AND status='completed'",
                (repo_key, issue_number))
        else:
            c.execute(
                "UPDATE issue SET status='processing', stage='', completed_at=NULL, "
                "completed_reason=NULL, updated_at=datetime('now') "
                "WHERE id=? AND status='completed'",
                (issue_id,))
        if c.rowcount == 0:
            # 并发下已被别的进程改走 → 与 _write_issue_status 同口径按 409 让前端刷新
            _rt_db.rollback()
            raise HTTPException(
                status_code=409,
                detail={"code": 409, "message": "需求状态已变更，请刷新后重试"})
        _rt_db.commit()
    try:
        from src.domains.mgmt.public import log_audit
        log_audit("issue_reopened_from_completed", operator_id=user.get("id", 0) or 0,
                  detail={
                      "repo_key": repo_key,
                      "issue_id": issue_id,
                      "issue_number": issue_number,
                      "from_status": cur_status,
                      "to_status": "processing",
                      "from_stage": cur_stage or "",
                      "to_stage": "",
                      "prev_completed_at": prev_completed_at or "",
                      "auto_redispatch": False,
                  })
    except Exception as _e:
        log.warning(f"issue-reopen: audit log failed (issue={issue_id}): {_e}")
    log.info(f"Issue #{issue_id} reopened completed → processing by user {user.get('id', 0)} "
             f"(stage cleared, no auto redispatch)")
    return {"ok": True, "issue_id": issue_id, "status": "processing", "stage": ""}

@router.get("/{issue_id}/pipeline", operation_id="issues_pipeline_detail", summary="Issue pipeline roadmap (by issue.id)", description="Returns the issue pipeline info and event history for the detail page. Issue #539: adds optional business status fields (`issue_stage`/`issue_task_status`/`review_status`, pending_review = awaiting human confirmation) and `sre_result` (task_ref + result_ref with explicit enums) so the detail page distinguishes 'stage execution finished' from 'awaiting review'.")
def get_issue_pipeline_by_id(issue_id: int, user: dict = Depends(require_no_session_token)):
    """获取需求关联的流水线信息和事件历史（详情页展示）。"""
    from src.api.event_store import get_events
    from src.api.pipeline_config import get_pipeline_config
    from src.api.session_store import get_issue_stage, get_issue_scene
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        # 仓库全部视图下，用户可以看到其他人的 Issue
        # 按 id 查出 Issue，再校验用户是否有该仓库的访问权限
        c.execute("SELECT repo_key, issue_number, status FROM issue WHERE id=?",
                  (issue_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        repo_key, issue_number, _issue_task_status = row[0], row[1], (row[2] or "")
    # 校验仓库访问权限（与"仓库全部"列表接口一致）
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    issue_key = f"#{issue_number}" if issue_number else ""
    _scene_tuple = get_issue_scene(repo_key, issue_key)
    _scene_id = (_scene_tuple[0] if _scene_tuple and _scene_tuple[0] else "") or "default"
    _stage_info = get_issue_stage(repo_key, issue_key) if issue_key else None
    _current_stage = (_stage_info or {}).get("stage", "") if _stage_info else ""
    pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key)
    nodes = (pipe_cfg or {}).get("nodes", [])
    events = []
    if issue_key:
        events = get_events(repo_key, issue_id=issue_key, limit=30)
    # 统一计算节点状态（与菜单流水线页一致；compute_node_statuses 兼算 latest_event）
    from src.api.pipeline_status import compute_node_statuses
    nodes, _ = compute_node_statuses(
        nodes, events, _current_stage, repo_key=repo_key, issue_id=issue_key)
    # Issue #329: extract carried_context for cross-pipeline continuation source annotation
    from src.api.session_store import get_carried_context
    _carried_context = get_carried_context(repo_key, issue_number)
    _source_issue_number = _carried_context.get("source_issue_number", "") if _carried_context else ""

    # Check for cross_scene_handoff events (Issue #329)
    _has_cross_scene = False
    if issue_key:
        try:
            _has_cross_scene = any(e.get("event_type") == "cross_scene_handoff" for e in events)
        except Exception:
            pass

    # Issue #539：需求级业务状态（review_status，与「事件执行结束」的节点状态区分）
    # + SRE 阶段最新任务引用/验收结论（pending_review 时详情页直接可读，引用+摘要不内嵌全量）
    from src.api.pipeline_status import (derive_review_status, load_segment_task_refs,
                                         _UNAVAILABLE_TASK_REF, _UNAVAILABLE_RESULT_REF)
    _task_refs = load_segment_task_refs([repo_key], [e.get("seq") for e in events])
    _sre_pair = None
    for _e in reversed(events):
        if (_e.get("agent_type") or "") == "sre":
            _sre_pair = _task_refs.get(_e.get("seq"))
            break
    if _sre_pair:
        _sre_result = {"task_ref": _sre_pair[0], "result_ref": _sre_pair[1]}
    else:
        _sre_result = {"task_ref": dict(_UNAVAILABLE_TASK_REF),
                       "result_ref": dict(_UNAVAILABLE_RESULT_REF)}

    from src.api.pipeline_status import attach_stall_state, extract_failed_node
    # Issue #2432：失败节点（详细路线图展示失败状态 + 失败节点名，不输出失败原因）
    _failed_node, _failed_node_name = extract_failed_node(nodes)
    _out = {
        "scene_id": _scene_id,
        "failed_node": _failed_node,
        "failed_node_name": _failed_node_name,
        "scene_name": (pipe_cfg or {}).get("display_name") or (pipe_cfg or {}).get("name", _scene_id),
        "current_stage": _current_stage,
        "nodes": nodes,
        "events": events,
        # Issue #329: cross-pipeline continuation fields
        "carried_context": _carried_context,
        "source_issue_number": _source_issue_number,
        "has_cross_scene_handoff": _has_cross_scene,
        # Issue #539: 需求级业务状态（待人工确认等）+ SRE 验收结果引用
        "issue_stage": _current_stage,
        "issue_task_status": _issue_task_status,
        "review_status": derive_review_status(_current_stage, _issue_task_status),
        "sre_result": _sre_result,
    }
    attach_stall_state(_out, events, _current_stage, _issue_task_status)
    return _out

class IssueHandoffBody(BaseModel):
    target_scene_id: str = ""

def _load_issue_repo_scene(issue_id: int) -> tuple[str, int, str]:
    """按 issue.id 取 (repo_key, issue_number, current_scene_id)；不存在 → 404。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number, scene_id FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    _scene_id = (row[2] or "").strip() or "default"
    # D105 TEXT 域：issue_number 落 TEXT 列（字符串 ident 平台）。
    # 仅 ASCII 纯数字串还原 int（weekly-10 handoff 响应 int 契约）；其余形态
    # （IK* 字符串 ident / 全角 / 带空格 / None）一律原样透传——不用
    # _native_issue_number，因其 str.isdigit 对 None 会产出 'None'、
    # 对全角 '２００' 也会转 int（依赖隐式特性，窄化边界处拒绝）。
    _raw_num = row[1] if row[1] is not None else 0
    _num_s = str(_raw_num).strip()
    # Issue #3055：数字串→int（#2072 契约），其余原样保留（不得 int() 强转 ident）
    _norm_num = normalize_issue_ident(_raw_num)
    issue_number = int(_norm_num) if _norm_num and _norm_num.isdigit() else _raw_num
    return row[0], issue_number, _scene_id

@router.get("/{issue_id}/handoff/targets", operation_id="issues_handoff_targets",
            summary="List handoff target pipelines for an issue",
            description="Lists available cross-pipeline handoff targets for the issue-center detail view: repo-scoped scenes (permission-filtered), vod node required. Issue #450: the current pipeline is included (self-handoff allowed). Empty list is returned as-is (frontend shows explicit empty hint).")
async def targets_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """Issue #337: 需求详情「移交到其他流水线」目标流水线列表。"""
    from src.api.deps import check_repo_access
    from src.api.issues_center import get_handoff_targets
    repo_key, issue_number, current_scene_id = _load_issue_repo_scene(issue_id)
    check_repo_access(user, repo_key)
    out = get_handoff_targets(repo_key, issue_number, current_scene_id)
    return {"issue_id": issue_id, "issue_number": issue_number, "repo_key": repo_key,
            "current_scene_id": out["current_scene_id"], "targets": out["targets"]}

def _a2a_identity(repo_key: str, user: dict | None) -> tuple[str, str]:
    """从 repo_key / 用户派生 A2A payload 的 repository 与 account（handoff 与人工处置共用）。"""
    repository = repo_key.split(":", 1)[1] if ":" in repo_key else repo_key
    account = (user or {}).get("username") or ""
    if not account:
        account = (repository.split("/", 1)[0] if "/" in repository else repository)
    return repository, account


async def _dispatch_a2a(agent: str, payload: dict, user: dict | None,
                        force: bool = False) -> dict:
    """服务内直调标准 /api/a2a 链路（jwt 分支），并把 JSONResponse 解包为 dict。

    Issue #2816 复审：handoff 与 terminal-disposition 曾各写一份派生 + 解包逻辑，
    此处收敛为单入口，保持「拦截类 skipped」原样透传。
    """
    import json as _rjson
    from src.domains.runtime.routes.webhook import a2a_endpoint, A2AHandoffBody
    _dispatch_user = dict(user or {})
    _dispatch_user["auth_type"] = "jwt"
    _a2a_body = A2AHandoffBody(agent=agent, force=force, payload=payload)
    _res = await a2a_endpoint(None, _a2a_body, _dispatch_user)  # type: ignore[arg-type]  # jwt 分支不读 request
    if not isinstance(_res, dict):
        _raw = getattr(_res, "body", b"") or b""
        if isinstance(_raw, memoryview):
            _raw = _raw.tobytes()
        try:
            _res = _rjson.loads(bytes(_raw).decode("utf-8"))
        except Exception:
            _res = {"status": "error", "message": "A2A 返回解析失败"}
    return _res


@router.post("/{issue_id}/handoff", operation_id="issues_handoff",
             summary="Hand off issue to another pipeline",
             description="Hands the issue off to a target pipeline (Issue #337 list-view entry). Backend proxy: validates repo access + target membership (vod required; Issue #450: current pipeline allowed as target), 30s idempotency guard, then reuses the standard A2A cross-pipeline handoff path (a2a_endpoint -> _cross_pipeline_handoff). Result (including 'skipped' interceptions like duplicate_handoff) is passed through to the frontend.")
async def handoff_issue(issue_id: int, body: IssueHandoffBody,
                        user: dict = Depends(require_no_session_token)):
    """Issue #337: 移交提交——服务内直调 a2a_endpoint（与 a2a-redo 同模式），复用 #329 链路。"""
    import time as _time
    from src.api.deps import check_repo_access
    from src.api.issues_center import get_handoff_targets

    # 1) 定位需求 + 归属校验
    repo_key, issue_number, current_scene_id = _load_issue_repo_scene(issue_id)
    check_repo_access(user, repo_key)

    # 2) 目标校验：非空 / 在可移交列表内（含 vod 过滤，防绕过权限点名任意场景；Issue #450 允许目标=当前场景）
    target_scene_id = (body.target_scene_id or "").strip()
    if not target_scene_id:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请选择目标流水线"})
    _targets = get_handoff_targets(repo_key, issue_number, current_scene_id)
    _target_ids = {t["id"] for t in _targets["targets"]}
    if target_scene_id not in _target_ids:
        raise HTTPException(status_code=404, detail={
            "code": 404, "message": "目标流水线不存在或不可移交（需含 vod 节点且不归属当前仓库）"})

    # 3) 幂等防连点：30s 内同一需求重复提交拒绝（复用 a2a-redo 的 meta 键模式）
    from src.domains.mgmt.public import get_meta_value, set_meta_value  # 跨域走 owner public
    _mk = f"issue-handoff:{issue_id}"
    _last = get_meta_value(_mk) or ""
    try:
        _recent = _last and (_time.time() - float(_last)) < 30
    except ValueError:
        _recent = False
    if _recent:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "该需求刚发起过移交，请 30s 后再试"})

    # 4) 组装 payload：carried_context 读取历史接续信息并补来源场景
    from src.api.session_store import get_carried_context
    from src.api.pipeline_config import get_pipeline_config
    _carried = get_carried_context(repo_key, issue_number) or {}
    _carried.setdefault("source_issue_number", issue_number)
    _carried.setdefault("source_scene_id", current_scene_id)
    _src_cfg = get_pipeline_config(current_scene_id, repo_key=repo_key) or {}
    _carried.setdefault("source_scene_name", _src_cfg.get("display_name") or _src_cfg.get("name") or current_scene_id)

    _repository, _account = _a2a_identity(repo_key, user)
    _context = (f"用户在需求中心发起跨流水线移交（Issue #{issue_number}）："
                f"目标场景 {target_scene_id}，请快速复核后流转至 issue 确认 Spec。")

    # 5) 服务内直调标准 /api/a2a 链路（jwt 分支内部再校验 managed_repos，双保险）
    _res = await _dispatch_a2a("vod", {
        "repository": _repository,
        "account": _account,
        "issue_number": issue_number,
        "pr_number": "",
        "context": _context,
        "target_scene_id": target_scene_id,
        "target_node": "vod",
        "carried_context": _carried,
        "target_assignee": "",
        "engine_config": None,
    }, user)

    # 6) 记录幂等时间戳（真实入队/拦截都记，防连点）
    set_meta_value(_mk, str(_time.time()))
    log.info(f"[issue-handoff] issue_id={issue_id} #{issue_number} → scene={target_scene_id} "
             f"by={user.get('username', '')}: {_res}")
    return _res


class IssueTerminalDispositionBody(BaseModel):
    action: str = ""
    carrier_pr: int = 0
    target_agent: str = ""
    reason: str = ""


def _audit_terminal_disposition(user: dict, repo_key: str, issue_number, action: str,
                                by: str, carrier_pr: int, target_agent: str,
                                ok: bool, blocked: str = "", dispatch_seq: int = 0) -> None:
    """终态载体单人工处置留痕（Issue #2816 / 架构审查 C3）。

    写端点必须落 audit_logs，不只写 meta：处置动作会恢复 Issue 状态并强制派发。
    失败（审计链路异常）只告警，不影响处置结果。
    """
    try:
        from src.domains.mgmt.public import log_audit
        log_audit("issue_terminal_disposition",
                  operator_id=user.get("id", 0) or 0,
                  detail={"repo_key": repo_key, "issue_number": issue_number,
                          "action": action, "by": by, "carrier_pr": int(carrier_pr or 0),
                          "target_agent": target_agent, "ok": bool(ok),
                          "blocked": blocked, "dispatch_seq": int(dispatch_seq or 0)})
    except Exception as e:  # noqa: BLE001 - 审计失败不阻断人工处置
        log.warning(f"[terminal-disposition] audit failed #{issue_number}: {e}")


@router.post("/{issue_id}/terminal-disposition", operation_id="issues_terminal_disposition",
             summary="Dispose a folded issue whose carrier PR is terminal",
             description="Issue #2816: manual disposition for a folded issue whose carrier PR is marked needs-human-terminal (CONFLICT_SCOPE=intersect). Actions: new_carrier (carrier_pr required; resumes processing/coding and force-dispatches), reassign (target_agent required; force-dispatches to that agent), close (converges the issue via confirm-done). Bypasses the terminal dispatch guard only for this manual action.")
async def issue_terminal_disposition(issue_id: int, body: IssueTerminalDispositionBody,
                                     user: dict = Depends(require_no_session_token)):
    """人工处置入口：指定新载体 / 改派 / 关闭（Issue #2816）。

    终态载体单被 ``a2a_terminal_guard`` 短路后，自动循环不再推进；本入口提供
    显式人工出口，替代 B1/B11-fallback 空转。
    """
    from src.api.deps import check_repo_access
    from src.api.terminal_disposition import (
        clear_carrier_override, clear_manual_override, close_issue,
        record_disposition, resume_issue, set_carrier_override,
        set_manual_override, validate_disposition)
    from src.api.a2a_terminal_guard import clear_cache

    repo_key, issue_number, _scene = _load_issue_repo_scene(issue_id)
    check_repo_access(user, repo_key)

    action = (body.action or "").strip().lower()
    _err = validate_disposition(action, body.carrier_pr, body.target_agent)
    if _err:
        raise HTTPException(status_code=400, detail={"code": 400, "message": _err})

    _repository, _account = _a2a_identity(repo_key, user)
    _by = user.get("username") or ""
    # 调用方 reason 进 agent context 前清洗/截断，收敛提示注入面。
    _reason = re.sub(r"\s+", " ", (body.reason or "")).strip()[:200]

    if action == "close":
        affected = close_issue(repo_key, issue_number,
                               confirmed_by=f"terminal_disposition:{_by or 'manual'}")
        record_disposition(repo_key, issue_number, action,
                           {"by": _by, "ok": bool(affected), "affected": affected})
        _audit_terminal_disposition(user, repo_key, issue_number, action, _by, 0, "",
                                    bool(affected))
        clear_manual_override(repo_key, issue_number)
        # Issue #2816 复审：关闭时连载体 meta 一起清掉，避免单据重开后被旧载体判定假阴性。
        clear_carrier_override(repo_key, issue_number)
        clear_cache(repo_key, issue_number)
        return {"status": "ok" if affected else "blocked", "action": action,
                "issue_number": issue_number, "closed": bool(affected)}

    # Issue #2816 复审（硬违规）：处置入口只对可恢复状态开放。completed/cancelled
    # 等已收敛单据必须走 close，否则 resume 不命中却仍 force 派发 → status 不变 +
    # stage=coding + 复活 coding，与「正常单据行为不变」相悖。
    from src.api.pipeline_constants import RESUMABLE_ISSUE_STATUSES
    from src.domains.runtime.issues_center import get_issue_lifecycle_status
    _cur_status = get_issue_lifecycle_status(repo_key, issue_number)
    if _cur_status not in RESUMABLE_ISSUE_STATUSES:
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "message": (f"需求当前状态为 {_cur_status or '未知'}，已收敛单据不支持 {action}；"
                        "如需结束本单请使用 close")})

    # validate_disposition 已校验 reassign 的 target_agent，这里不再二次校验。
    _target_agent = ("coding" if action == "new_carrier"
                     else (body.target_agent or "").strip().lower())
    _carrier_pr = int(body.carrier_pr) if action == "new_carrier" else 0
    # 先恢复状态再落 meta/派发：恢复未命中（并发改写/状态已收敛）时不得写 stage，
    # 也不得留下「已人工越过」的持久标记后仍去 force 派发。
    _resumed = resume_issue(repo_key, issue_number,
                            stage=_target_agent, owner_agent=_target_agent)
    if not _resumed:
        record_disposition(repo_key, issue_number, action, {
            "by": _by, "carrier_pr": _carrier_pr, "blocked": "not_resumable"})
        _audit_terminal_disposition(user, repo_key, issue_number, action, _by,
                                    _carrier_pr, _target_agent, False,
                                    blocked="not_resumable")
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "message": "需求已收敛或状态已变更，未恢复派发；请刷新状态后改用 close"})
    if action == "new_carrier":
        set_carrier_override(repo_key, issue_number, _carrier_pr)
    # 记录人工越过：处置后旧 needs-human 标签不再在下一轮非 force 派发中反噬。
    set_manual_override(repo_key, issue_number, action, _carrier_pr, _by)
    clear_cache(repo_key, issue_number)

    _context = (f"人工处置终态载体单[compensated_by=manual:{_by}]：action={action}"
                + (f"，新载体 PR #{_carrier_pr}" if action == "new_carrier" else "")
                + "。请基于新载体/人工指定继续推进。"
                + (f" 处置说明：{_reason}" if _reason else ""))

    _res = await _dispatch_a2a(_target_agent, {
        "repository": _repository,
        "account": _account,
        "issue_number": issue_number,
        "pr_number": _carrier_pr if action == "new_carrier" else "",
        "carrier_pr": _carrier_pr,
        "context": _context,
    }, user, force=True)
    record_disposition(repo_key, issue_number, action, {
        "by": _by, "carrier_pr": _carrier_pr,
        "target_agent": _target_agent, "reason": _reason,
        "dispatch_seq": _res.get("seq") if isinstance(_res, dict) else 0,
    })
    _audit_terminal_disposition(user, repo_key, issue_number, action, _by, _carrier_pr,
                                _target_agent, True,
                                dispatch_seq=int((_res.get("seq") if isinstance(_res, dict) else 0) or 0))
    log.info(f"[terminal-disposition] issue_id={issue_id} #{issue_number} action={action} "
             f"by={_by}: {_res}")
    # 与 handoff 一致：直接返回 A2A 结果（不再包一层 result），附加处置元信息。
    _out = dict(_res) if isinstance(_res, dict) else {}
    _out.update({"action": action, "issue_number": issue_number,
                 "target_agent": _target_agent,
                 "disposition": {"by": _by, "carrier_pr": _carrier_pr,
                                 "reason": _reason}})
    return _out


@router.get("/{issue_id}/clarifications", operation_id="issues_clarifications", summary="Issue clarification history", description="Fetches the agent's clarification messages for the Issue.")
def get_issue_clarifications(issue_id: int, user: dict = Depends(require_no_session_token)):
    """拉取 agent 在该 Issue 上的澄清消息。"""
    from src.api.issues_center import _get_task_issue_info, _find_agent_task_ids
    from src.kernel.session_manager import db, db_lock
    info = _get_task_issue_info(issue_id)
    if not info:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number, conv_id = info
    agent_task_ids = _find_agent_task_ids(repo_key, issue_number)
    messages = []
    if agent_task_ids:
        import json as _json
        with db_lock:
            c = _rt_db.cursor()
            placeholders = ",".join("?" * len(agent_task_ids))
            c.execute(
                f"SELECT task_id, seq, type, tool, content, input_json, created_at "
                f"FROM task_messages WHERE task_id IN ({placeholders}) "
                f"AND type IN ('text','reasoning','error','tool_use') "
                f"ORDER BY created_at DESC LIMIT 50",
                agent_task_ids)
            rows = c.fetchall()
        for r in reversed(rows):
            msg = {"task_id": r[0], "seq": r[1], "type": r[2], "tool": r[3] or "",
                   "content": r[4] or "", "created_at": normalize_event_time(r[6])}
            if r[5]:
                try:
                    msg["input"] = _json.loads(r[5])
                except Exception:
                    msg["input"] = r[5]
            messages.append(msg)
    conv_messages = []
    if conv_id:
        from src.kernel.session_manager import get_messages
        conv_messages = get_messages(conv_id, limit=30)
    # host_for_repo_key 在本文件从未 import（出生即坏，base 5aa5d1bd0 同样）：
    # 这行每次调用都 NameError → 澄清/追问历史接口 500。平台卡按需导入，
    # 与 src/api/qa_gate.py 同一写法（避免模块顶层引入平台卡缓存）。
    from src.domains.runtime.vcs.platform_card import host_for_repo_key
    from src.kernel.repo_key import split_owner_repo
    _issue_host = host_for_repo_key(repo_key)  # 卡 host 派生（无平台名二分）
    # repo_key 带平台前缀（gitee:owner/repo），拼 URL 前必须拆掉，否则
    # https://gitee.com/gitee:owner/repo/issues/1（2026-11-13 同批修）
    _owner, _repo = split_owner_repo(repo_key)
    return {"messages": messages, "conversation_id": conv_id,
            "issue_url": f"https://{_issue_host}/{_owner}/{_repo}/issues/{issue_number}"}

@router.post("/{issue_id}/awaiting-input", operation_id="issues_awaiting-input",
             summary="Agent declares issue awaiting-input (待澄清)",
             description="vod/issue agent (or any agent_type in the issue's bound scene nodes, Issue #655) explicitly declares this issue needs user input: atomically moves active/processing -> awaiting-input, writes the clarification questions to the task conversation and sends a static Feishu card. Session-only (tk_), repo scope must match the issue. Colocated with issues clarify (user reply) / issues clarifications (history).")
async def issue_awaiting_input(issue_id: int, body: TaskAwaitingInputBody,
                               user: dict = Depends(get_current_user_or_api_key)):
    """vod/issue agent 显式声明「待澄清」（澄清三件套之 Agent 发起）。

    - 从 `/api/tasks/awaiting-input` 迁移而来（issue-explicit-awaiting-input-api-design.md）：awaiting-input
      **只改 issue 状态、与任务生命周期正交（C 单信号）**，故归属 /api/issues/{issue_id}。
    - 仅会话通道（tk_）：URL {issue_id} 定位需求 + 会话 `(repo_key, agent_type)` 作用域校验（与 notify 同源信任模型）。
    - 不终态化、不钉住 task；任务随引擎流结束由 finalize_by_stream_end 照常收尾。
    - **不做**：不触发 ChatAgent / 不执行 opencode / 不注入用户个人 token（§十）。
    """
    from src.api.issues_center import (_ensure_task_conversation, _get_feishu_open_id,
                                       _resolve_user_chat_bot, _run_awaiting_notify)
    from src.kernel.session_manager import db as _db, db_lock as _dl, get_chat_config

    questions = (body.questions or "").strip()
    if not questions:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "questions 必填（本次需澄清的问题）"})
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅会话 token（tk_）可调用（Agent 执行期声明待澄清）"})
    session_repo = user.get("repo_key") or ""
    session_agent = user.get("agent_type") or ""
    if not session_repo:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "会话 token 缺少仓库作用域"})
    if session_agent not in ("vod", "issue"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"agent_type={session_agent} 不在本接口范围（仅 vod/issue 可声明待澄清）"})

    with _dl:
        c = _rt_db.cursor()
        c.execute("SELECT id, repo_key, issue_number, assignee, user_id, title, status FROM issue WHERE id=?",
                  (issue_id,))
        irow = c.fetchone()
    if not irow:
        # Issue #667：卡片 id 无行 → 支持按 (会话仓库作用域, body.issue_number) 兜底解析：
        # 行已存在则复用；VCS 侧存在则 ensure_issue_task 自动补建（缺行同步缺口自动修复）。
        _fb_num = native_issue_number(getattr(body, "issue_number", 0) or 0)
        if is_actionable_issue_number(_fb_num):
            from src.kernel.repo_key import to_repo_key as _trk
            _fb_repo = _trk(session_repo)
            with _dl:
                c = _rt_db.cursor()
                c.execute("SELECT id, repo_key, issue_number, assignee, user_id, title, status, scene_id FROM issue "
                          "WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
                          (_fb_repo, _fb_num))
                irow = c.fetchone()
            if not irow:
                from src.domains.runtime.pipeline.event_pipeline import fetch_issue_via_vcs
                _st, _data = fetch_issue_via_vcs(_fb_repo, str(_fb_num))
                if _st == "exists":
                    from src.api.issues_center import ensure_issue_task
                    _fb_id = ensure_issue_task(_fb_repo, _fb_num,
                                               title=str((_data or {}).get("title", "") or ""),
                                               event_type="issue_opened")
                    with _dl:
                        c = _rt_db.cursor()
                        c.execute("SELECT id, repo_key, issue_number, assignee, user_id, title, status, scene_id FROM issue "
                                  "WHERE id=?", (_fb_id,))
                        irow = c.fetchone()
                elif _st == "missing":
                    raise HTTPException(status_code=404, detail={
                        "code": 404, "reason": "issue_not_found",
                        "message": f"Issue #{_fb_num} 不存在（GitCode 侧无此 Issue，可能已删除）",
                        "suggestion": "确认 Issue 编号；已删除的 Issue 属预期 404"})
        if not irow:
            raise HTTPException(status_code=404, detail={
                "code": 404, "reason": "internal_card_missing",
                "message": "需求不存在：内部需求库无该卡片行（同步未落或时序未到，且未能按 issue_number 兜底）",
                "hint": "awaiting-input 需传需求卡片 id（GRAPE_ISSUE_ID，issue 表主键），非 Issue number；"
                        "可先 grape issues detail --number $ISSUE_NUMBER --json-out 取 id；"
                        "卡片缺行时可在 body 附 issue_number（GitCode 侧编号）触发兜底补建（Issue #667）"})
    issue_card_id, repo_key, issue_number = irow[0], irow[1] or "", irow[2] or 0
    assignee, issue_user_id, title, cur_status = irow[3] or "", irow[4] or 0, irow[5] or "", irow[6] or ""
    from src.kernel.repo_key import to_repo_key
    if to_repo_key(repo_key) != to_repo_key(session_repo):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仓库作用域不匹配"})
    if cur_status in ("completed", "cancelled"):
        return {"ok": True, "issue_id": issue_card_id, "status": cur_status, "changed": False}

    with _dl:
        c = _rt_db.cursor()
        c.execute(
            "UPDATE issue SET status='awaiting_input' WHERE id=? AND status IN ('active','processing')",
            (issue_card_id,))
        changed = c.rowcount > 0
        _rt_db.commit()
    user_id = issue_user_id

    conv_id = ""
    if str(user_id or "") not in ("", "0") and changed:
        try:
            conv_id = _ensure_task_conversation(user_id, repo_key, issue_number, title, "",
                                                write_context_on_reuse=False)
            if conv_id:
                with _dl:
                    c = _rt_db.cursor()
                    c.execute("UPDATE issue SET conversation_id=? WHERE id=?", (conv_id, issue_card_id))
                    _rt_db.commit()
        except Exception as e:
            log.warning(f"Issue awaiting-input: conversation ensure failed: {e}")

    log.info(f"Issue awaiting-input: {repo_key}/#{issue_number} -> awaiting_input issue={issue_card_id} "
             f"changed={changed} (agent={session_agent}, questions={questions[:60]})")
    if changed:
        from src.api.issues_center import _get_feishu_open_id_by_user_id
        open_id = _get_feishu_open_id_by_user_id(user_id) or _get_feishu_open_id(assignee, repo_key)
        ctx = _resolve_user_chat_bot(None, user_id)
        api = _feishu_api_for_ctx(ctx)
        cfg = get_chat_config(user_id) if user_id else {"backend": "opencode"}
        asyncio.create_task(_run_awaiting_notify(ctx, api, user_id, conv_id, open_id,
                                                 questions, repo_key, issue_number, cfg))

    return {"ok": True, "issue_id": issue_card_id, "status": "awaiting_input", "changed": changed}

def _session_devops_issue(issue_id: int, user: dict) -> dict:
    """待审核系列端点通用鉴权：仅 tk_ 会话；agent_type∈(devops 白名单 ∪ issue 绑定场景节点集)（Issue #655）；
    issue.repo_key==token.repo_key；返回 issue 行。

    Issue #672：双路径解析——内部主键 miss（404）或 scope 不匹配（403，GitCode number 撞上
    其它仓库的内部 id）时，把 issue_id 视作 GitCode number 在会话仓库作用域内二次解析；
    本地亦无行但远端（number+owner/repo）存在时建最小行（同步修复，覆盖存量缺行）。
    """
    from src.kernel.session_manager import db as _db, db_lock as _dl
    from src.kernel.repo_key import to_repo_key, parse_repo_key
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅会话 token（tk_）可调用（Agent 执行期）"})
    _agent = user.get("agent_type") or ""
    if _agent not in _DEV_OPS_AGENTS:
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"agent_type={_agent} 不在本接口范围（devops agent）"})
    _repo = user.get("repo_key") or ""
    if not _repo:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "会话 token 缺少仓库作用域"})
    with _dl:
        c = _rt_db.cursor()
        c.execute("SELECT id, repo_key, issue_number, assignee, user_id, title, status FROM issue WHERE id=?", (issue_id,))
        _r = c.fetchone()
    if not _r:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    from src.kernel.repo_key import to_repo_key
    if _r[1] and to_repo_key(_r[1]) != to_repo_key(_repo):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仓库作用域不匹配"})
    return {"id": _r[0], "repo_key": _r[1], "issue_number": _r[2], "assignee": _r[3] or "",
            "user_id": _r[4] or 0, "title": _r[5] or "", "status": _r[6] or ""}

# ===== Issue #2695（#2695 移植）：pending-review 的 issue_id 前置校验（空/非法 → 400，不再误导性 405/422）=====

_INVALID_ISSUE_ID_MSG = "invalid issue_id"


def _invalid_issue_id() -> HTTPException:
    """统一错误体（与平台 `{code,message}` 约定一致）。"""
    return HTTPException(status_code=400,
                         detail={"code": 400, "message": _INVALID_ISSUE_ID_MSG})


def _require_numeric_issue_id(request: Request) -> None:
    """路由级前置校验（依赖）：原始路径段必须是纯数字。

    Issue #2695：调用方常把「内部需求卡片 id 之外的脏值」（空段/`abc`/`#pr_1`…）
    当成 issue_id 传入。此类请求此前要么被 FastAPI 的 int 注解拦成 422、要么空段
    根本不匹配任何路由而落到 SPA 兜底返回 405（无 detail），调用方无法自诊断。
    这里在路由入口前置成 400 + `invalid issue_id`，语义明确且不影响合法 id 链路。
    """
    _raw = str((request.path_params or {}).get("issue_id", "") or "")
    if not re.fullmatch(r"[0-9]+", _raw):
        raise _invalid_issue_id()


# 空 issue_id 形态：`/api/issues//pending-review`（prefix + "//pending-review"）——
# 路径段为空时 `/{issue_id}/pending-review` 模板不匹配，需独立条目才能给出 400 而非 SPA 兜底 405。
# 不进契约（include_in_schema=False）：它不是新增对外能力，只是同一端点的非法入参响应。
@router.post("//pending-review", include_in_schema=False,
             summary="issues pending-review with empty issue_id → 400 invalid issue_id")
async def issue_pending_review_empty_id():
    """Issue #2695：空 issue_id（`/api/issues//pending-review`）→ 400 invalid issue_id。"""
    raise _invalid_issue_id()


@router.post("/{issue_id}/pending-review", operation_id="issues_pending-review",
             summary="Agent declares issue pending_review (阶段审核点)",
             description="任意 devops agent（vod/issue/coding/qa/sre 或 issue 绑定场景节点角色，Issue #655）显式将 issue 置为待审核（阶段性审核点）：active/processing/awaiting_input -> pending_review，不改 stage，审核点写入任务会话。重复调用幂等（Issue #725）：已是 pending_review 时短路返回 200（status + idempotent:true，changed:false，不重复写会话/发通知）。Session-only (tk_)，repo 匹配。与 awaiting-input 对称（等用户双态）。issue_id 必须是纯数字内部 id（Issue #2695：空/非数字在入口前置返回 400 invalid issue_id，不是 GitCode issue number）。")
async def issue_pending_review(issue_id: int, body: TaskPendingReviewBody,
                               _issue_id_check: None = Depends(_require_numeric_issue_id),
                               user: dict = Depends(get_current_user_or_api_key)):
    """Agent 显式提交阶段审核点 → status=pending_review（不改 stage）。"""
    from src.api.issues_center import _ensure_task_conversation
    from src.kernel.session_manager import db as _db, db_lock as _dl, add_message as _add

    review_points = (body.review_points or "").strip()
    if not review_points:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "review_points 必填（本次需审核的内容）"})
    it = _session_devops_issue(issue_id, user)
    if it["status"] in ("completed", "cancelled"):
        return {"ok": True, "issue_id": it["id"], "status": it["status"], "changed": False}
    _changed = _set_issue_pending_review(it["repo_key"], it["issue_number"])
    conv_id = ""
    if str(it["user_id"] or "") not in ("", "0") and _changed:
        try:
            conv_id = _ensure_task_conversation(it["user_id"], it["repo_key"], it["issue_number"], it["title"], "",
                                                write_context_on_reuse=False)
            if conv_id:
                from src.kernel.repo_key import from_repo_key
                _refs = (body.references or "").strip()
                _msg = (f"🔎 需要你审核确认 · Issue #{it['issue_number']}\n\n"
                        f"仓库: {from_repo_key(it['repo_key'])}\n\n"
                        f"{review_points}" + (f"\n\n参考: {_refs}" if _refs else ""))
                _add(conv_id, "agent", _msg)
                with _dl:
                    c = _rt_db.cursor()
                    c.execute("UPDATE issue SET conversation_id=? WHERE id=?", (conv_id, it["id"]))
                    _rt_db.commit()
        except Exception as e:
            log.warning(f"Issue pending-review: conversation write failed: {e}")
    # 飞书静态卡片触达（同 awaiting）：等用户双态的“待审核”必须主动通知用户
    if _changed:
        try:
            from src.api.issues_center import (_get_feishu_open_id, _get_feishu_open_id_by_user_id,
                                               _resolve_user_chat_bot, _run_pending_review_notify)
            from src.kernel.session_manager import get_chat_config
            _open_id = _get_feishu_open_id_by_user_id(it["user_id"]) or _get_feishu_open_id(it["assignee"], it.get("repo_key", ""))
            _ctx = _resolve_user_chat_bot(None, it["user_id"])
            _api = _feishu_api_for_ctx(_ctx)
            _cfg = get_chat_config(it["user_id"]) if it["user_id"] else {"backend": "opencode"}
            asyncio.create_task(_run_pending_review_notify(
                _ctx, _api, it["user_id"], conv_id, _open_id, review_points,
                body.references or "", it["repo_key"], it["issue_number"], _cfg))
        except Exception as e:
            log.warning(f"Issue pending-review: notify schedule failed: {e}")
    return {"ok": True, "issue_id": it["id"], "status": "pending_review", "changed": _changed}

class _ReviewPassBody(BaseModel):
    target: str = "processing"   # processing | done
    evidence: str = ""
    pr_number: int = 0           # Issue #1211（#1211 移植）：QA 审查的 PR 号（透传待合入通知；缺省自动扫描关联 open PR）

class _ReviewRejectBody(BaseModel):
    target: str = "processing"   # processing | awaiting_input
    reason: str = ""

def _review_outcome(_out: str) -> tuple:
    """拆分内部审核结果码为 (result, reason)。

    Issue #1599：`result` 保留旧字符串语义（`noop` / `rejected_*` / `passed_*`）兼容现有消费方；
    `noop:<reason>` 拆出机器可读原因码（already_target/terminal/not_pending_review/error）。
    """
    if _out.startswith("noop"):
        _reason = _out.split(":", 1)[1] if ":" in _out else "not_pending_review"
        return "noop", _reason
    return _out, ""

@router.post("/{issue_id}/review-pass", operation_id="issues_review-pass",
             summary="Agent decides review passed",
             description="Agent 判定用户已通过审核，只能推进、不能终态：target=processing → pending_review→processing（继续），末节点归一 pending_review；target=done 一律 400（需求完成只能由人工 confirm-done）。响应回读 issue 表真实 status 并返回 changed(bool)；未命中附 reason（already_target/terminal/not_pending_review/agent_finalize_forbidden）。Session-only (tk_)，repo 匹配，devops agent。")
async def issue_review_pass(issue_id: int, body: _ReviewPassBody,
                            user: dict = Depends(get_current_user_or_api_key)):
    it = _session_devops_issue(issue_id, user)
    if str(body.target or "").strip().lower() == "done":
        # 人工闸：agent 没有终态通道，完成只能由人在平台上确认
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "agent 不能直接完成需求（target=done 已禁用）；请调用 review-pass target=processing 让需求进入 pending_review，由人工确认完成",
            "reason": "agent_finalize_forbidden"})
    _out = _review_pass(it["repo_key"], it["issue_number"],
                        target=body.target or "processing", evidence=body.evidence or "",
                        pr_number=body.pr_number or 0)
    _result, _reason = _review_outcome(_out)
    _st = _read_issue_status(it["repo_key"], it["issue_number"]) or it["status"]
    return {"ok": True, "issue_id": it["id"], "status": _st,
            "changed": _result != "noop", "result": _result, "reason": _reason}

@router.post("/{issue_id}/review-reject", operation_id="issues_review-reject",
             summary="Agent decides review rejected",
             description="Agent 判定用户未通过审核：仅 pending_review → processing（打回）或 awaiting_input（转澄清）。响应回读 issue 表真实 status 并返回 changed(bool)；未命中附 reason（already_target/terminal/not_pending_review）。noop 排查：`grape issues detail --json-out` 查看 issue_task_status。Session-only (tk_)，repo 匹配，devops agent。")
async def issue_review_reject(issue_id: int, body: _ReviewRejectBody,
                              user: dict = Depends(get_current_user_or_api_key)):
    it = _session_devops_issue(issue_id, user)
    _out = _review_reject(it["repo_key"], it["issue_number"],
                          target=body.target or "processing", reason=body.reason or "")
    _result, _reason = _review_outcome(_out)
    _st = _read_issue_status(it["repo_key"], it["issue_number"]) or it["status"]
    return {"ok": True, "issue_id": it["id"], "status": _st,
            "changed": _result != "noop", "result": _result, "reason": _reason}

class _IssueClarifyBody(BaseModel):
    message: str = ""

@router.post("/{issue_id}/clarify", operation_id="issues_clarify", summary="Reply issue clarification", description="Developer replies to an agent clarification: posts a GitCode issue comment and writes to the task conversation.")
async def clarify_issue(issue_id: int, body: _IssueClarifyBody, user: dict = Depends(require_no_session_token)):
    """开发者在需求中心回复 agent 澄清：同步发 GitCode Issue 评论 + 写入任务会话。"""
    from src.api.issues_center import _get_task_issue_info
    from src.kernel.session_manager import db, db_lock
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "消息不能为空"})
    info = _get_task_issue_info(issue_id)
    if not info:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number, conv_id = info
    _is_github = repo_key.startswith("github:")
    if _is_github:
        # Issue #343: GitHub 写操作走仓库 bot token
        from src.domains.mgmt.public import get_github_token  # 跨域走 owner public
        from src.kernel.repo_key import to_repo_key
        api_token = get_github_token(repo_key=to_repo_key(repo_key, provider="github"))
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_NOT_BOUND", "message": "仓库 GitHub Bot Token 未配置"})
    else:
        # Issue #343: GitCode 写操作走仓库 bot token（跳过 agent session token）
        from src.domains.mgmt.public import get_repo_bot_token
        from src.kernel.repo_key import to_repo_key
        api_token = get_repo_bot_token(repo_key=to_repo_key(repo_key))
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_NOT_BOUND", "message": "仓库 Bot Token 未配置"})
    from src.kernel.repo_key import split_owner_repo
    owner, rname = split_owner_repo(repo_key)
    comment_body = f"💬 **开发者澄清**\n\n{msg}"
    if _is_github:
        from src.domains.mgmt.public import _github_req
        try:
            _gh_resp = await asyncio.to_thread(
                _github_req, "POST", f"https://api.github.com/repos/{owner}/{rname}/issues/{issue_number}/comments",
                api_token, {"body": comment_body})
            # Issue #453 台账（S19-4）：GitHub 澄清评论入账——响应 dict 带 comment id
            try:
                from src.kernel.outbound_ledger import record_outbound, extract_content_id, content_hash_md5
                from src.kernel.repo_key import to_repo_key
                record_outbound("github", to_repo_key(f"{owner}/{rname}", "github"), "issue_comment",
                                extract_content_id(_gh_resp, "issue_comment") if isinstance(_gh_resp, dict) else "",
                                issue_number=str(issue_number), content_hash=content_hash_md5(comment_body))
            except Exception as _le:
                log.warning(f"outbound_ledger record (github clarify) failed: {_le}")
            log.info(f"Issue clarify: posted comment to {repo_key}/issues/{issue_number} by {user['username']}")
        except Exception as e:
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"GitHub 评论失败: {str(e)[:200]}"})
    else:
        # S75：else 分支改走 VcsApiClient 卡客户端（原硬编码 gitcode.com URL，
        # gitee 仓澄清评论必 404）；台账已由 post_issue_comment 内统一入账
        from src.kernel.repo_key import parse_repo_key
        from src.domains.runtime.vcs.api_client import VcsApiClient, _native_issue_number
        _provider = parse_repo_key(repo_key)[0]
        try:
            _client = VcsApiClient(_provider, api_token, owner, rname)
            # 同步 urllib 出站不得跑在事件循环上（GitCode 慢响应会冻住整个进程）
            _resp = await asyncio.to_thread(
                _client.post_issue_comment, _native_issue_number(issue_number), comment_body)
            if isinstance(_resp, dict) and _resp.get("error"):
                raise RuntimeError(str(_resp.get("error"))[:300])
            log.info(f"Issue clarify: posted comment to {repo_key}/issues/{issue_number} by {user['username']}")
        except HTTPException:
            raise
        except Exception as e:
            # Issue #345: 识别 401 返回 Bot Token 失效提示
            from src.domains.runtime.vcs.token_error import is_auth_error_exc, BOT_TOKEN_INVALID_MSG
            if is_auth_error_exc(e):
                raise HTTPException(status_code=502, detail={"code": 502,
                                                             "message": f"评论失败：{BOT_TOKEN_INVALID_MSG}"})
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"评论失败: {str(e)[:200]}"})
    if conv_id:
        try:
            from src.kernel.session_manager import add_message
            add_message(conv_id, "user", msg)
        except Exception as e:
            log.warning(f"Issue clarify: write conversation message failed: {e}")
    try:
        from src.api.issues_center import _resume_issue_waiting
        if _resume_issue_waiting(repo_key, issue_number):
            log.info(f"Issue clarify: awaiting_input → processing for {repo_key}/#{issue_number}")
    except Exception as e:
        log.warning(f"Issue clarify: restore processing failed: {e}")
    return {"ok": True, "message": "澄清已发送到 Issue 并同步到任务会话"}

@router.post("/{issue_id}/attachments", operation_id="issues_upload_attachment", summary="Upload issue attachment", description="Uploads an attachment into the issue attachment directory.")
async def upload_issue_attachment(issue_id: int, file: UploadFile = File(...),
                                  user: dict = Depends(require_no_session_token)):
    """上传附件到需求关联的附件目录。"""
    from src.api.issues_center import (_attach_save, _ATTACH_ALL_EXTS, _ATTACH_MAX_SIZE)
    from src.kernel.repo_key import split_owner_repo
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number = row[0], row[1]
    owner, rname = split_owner_repo(repo_key)
    original_name = file.filename or "unnamed"
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in _ATTACH_ALL_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的文件类型: {ext}"})
    content = await file.read()
    if len(content) > _ATTACH_MAX_SIZE:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"文件大小超过 {_ATTACH_MAX_SIZE // (1024 * 1024)}MB 限制"})
    import uuid
    safe_name = f"{uuid.uuid4().hex}{ext}"
    dir_name = str(issue_number) if issue_number else "pending"
    url = _attach_save(owner, rname, dir_name, safe_name, content)
    log.info(f"Issue attachment uploaded: issue={issue_id} file={original_name} -> {safe_name} ({len(content)} bytes)")
    return {"url": url, "filename": original_name, "size": len(content)}

@router.get("/{issue_id}/attachments", operation_id="issues_list_attachments", summary="List issue attachments", description="Lists attachments of an issue.")
def list_issue_attachments(issue_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """列出需求关联的附件文件列表。"""
    from src.api.issues_center import _attach_list
    from src.kernel.repo_key import split_owner_repo
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number = row[0], row[1]
    owner, rname = split_owner_repo(repo_key)
    attachments = []
    for dir_name in (["pending", str(issue_number)] if issue_number else ["pending"]):
        attachments.extend(_attach_list(owner, rname, dir_name))
    return {"attachments": attachments}

@router.delete("/{issue_id}/attachments/{filename}", operation_id="issues_delete_attachment", summary="Delete issue attachment", description="Deletes an attachment file by UUID filename.")
def delete_issue_attachment(issue_id: int, filename: str, user: dict = Depends(require_no_session_token)):
    """删除指定附件文件。filename 为 UUID 文件名。"""
    from src.api.issues_center import _attach_delete
    from src.kernel.repo_key import split_owner_repo
    from src.kernel.session_manager import db, db_lock
    import re
    if not re.match(r'^[0-9a-f]{32}\.\w+$', filename):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法文件名"})
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number = row[0], row[1]
    owner, rname = split_owner_repo(repo_key)
    dir_name = str(issue_number) if issue_number else "pending"
    if _attach_delete(owner, rname, dir_name, filename):
        log.info(f"Issue attachment deleted: issue={issue_id} file={filename}")
        return {"ok": True, "message": "已删除"}
    raise HTTPException(status_code=404, detail={"code": 404, "message": "附件不存在"})

@router.post("/attachments/pre-upload", include_in_schema=False)
async def pre_upload_attachment(file: UploadFile = File(...),
                                repo_key: str = "",
                                user: dict = Depends(require_no_session_token)):
    """创建 Issue 前的临时附件上传。文件存到 pending 目录，创建时按 safe_name 列表精确关联。"""
    from src.domains.runtime.routes.tasks import _attach_save, _ATTACH_ALL_EXTS, _ATTACH_MAX_SIZE
    from src.kernel.repo_key import to_repo_key, split_owner_repo
    rk = to_repo_key(repo_key)
    if not rk:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "repo_key 必填"})
    owner, rname = split_owner_repo(rk)
    original_name = file.filename or "unnamed"
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in _ATTACH_ALL_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的文件类型: {ext}"})
    content = await file.read()
    if len(content) > _ATTACH_MAX_SIZE:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"文件大小超过 {_ATTACH_MAX_SIZE // (1024 * 1024)}MB 限制"})
    import uuid
    safe_name = f"{uuid.uuid4().hex}{ext}"
    url = _attach_save(owner, rname, "pending", safe_name, content)
    log.info(f"Pre-upload attachment: file={original_name} -> {safe_name} ({len(content)} bytes)")
    return {"url": url, "filename": original_name, "safe_name": safe_name, "size": len(content)}

@router.delete("/attachments/pre-upload/{filename}", include_in_schema=False)
def pre_delete_attachment(filename: str, repo_key: str = "",
                          user: dict = Depends(require_no_session_token)):
    """删除 pending 里的临时附件。"""
    from src.domains.runtime.routes.tasks import _attach_delete
    from src.kernel.repo_key import to_repo_key, split_owner_repo
    import re
    if not re.match(r'^[0-9a-f]{32}\.\w+$', filename):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法文件名"})
    rk = to_repo_key(repo_key)
    owner, rname = split_owner_repo(rk)
    if _attach_delete(owner, rname, "pending", filename):
        return {"ok": True, "message": "已删除"}
    raise HTTPException(status_code=404, detail={"code": 404, "message": "附件不存在"})

@router.get("/mine", operation_id="issues_mine", summary="My issues (personal dimension)", description="My issues (issue-center personal dimension), enriched with scene/stage/is_running/agent_statuses. Issue #2082 paging (semantics unified with issues_list/gitrepo_milestones): page is 1-based; per_page defaults to 20 when omitted (env ISSUES_DEFAULT_PER_PAGE, set 0 to restore the legacy full-list default), is clamped to a max of 100 and 0 means explicit full list. With per_page>0 the response carries total/page/per_page/has_more (total counts the filtered set).")
def my_issues(status: str = Query("", pattern="^(active|processing|awaiting_input|pending_review|completed|cancelled|)?$"),
                    repo_key: str = Query("", description="按仓库过滤"),
                    sort_order: str = Query("desc", pattern="^(desc|asc)$", description="按创建时间排序方向"),
                    mode: str = Query("default", pattern="^(default|time)$", description="default=状态优先；time=纯时间序"),
                    created_by_me: str = Query("", description="仅我创建的任务"),
                    assignees: str = Query("", description="按创建人筛选(逗号分隔)"),
                    creators: str = Query("", description="按创建人过滤(逗号分隔, Issue #805；前端 mine 分支同款参数，此前被静默丢弃)"),
                    statuses: str = Query("", description="多状态过滤(逗号分隔；Issue #926 列表同款)。与单 status 二选一，同时给出时以 statuses 为准"),
                    q: str = Query("", max_length=200, description="Issue #926：标题/编号模糊搜索（服务端过滤；前端 mine 分支已下推 &q=，此前被静默丢弃）"),
                    page: int = Query(1, ge=1, description="页码（1-based，per_page>0 时生效）"),
                    per_page: int | None = Query(None, ge=0, description="每页条数；缺省=默认 20（ISSUES_DEFAULT_PER_PAGE 可调，置 0 回退 legacy 全量）；0=显式全量；上限 100，超限夹取"),
                    user: dict = Depends(require_org_member)):
    """我的需求列表（需求中心·个人维度）。

    返回当前用户的需求（Issue 卡片行，user_id 过滤）+ 富化
    （scene/stage/is_running/agent_statuses）。
    组织隔离：非个人组织只返回 org_id 匹配的 issue。

    Issue #2082（#2082 移植）：per_page>0（含缺省注入的默认 20）真分页，响应含
    total/page/per_page/has_more（total 与过滤后集合一致）；per_page=0
    显式全量（legacy 形态 {issues, sort_order, mode}，现状一致）。
    Issue #827: 过滤口径由单一 user_id 扩为 user_id OR created_by OR assignee（绑定集合兜底），
    移交后 user_id 被清零时创建人仍可见。
    """
    from src.api.issues_center import (_query_tasks, _build_assignee_filter, _build_creator_filter,
                                       resolve_issues_page_params, _get_gitcode_user_by_user_id)
    from src.domains.mgmt.public import is_org_personal
    _org_id = user.get("org_id", 0)
    _is_personal = is_org_personal(_org_id) if _org_id else True
    _filter_org_id = 0 if _is_personal else _org_id
    clauses = []
    params = []
    if repo_key:
        # 跨 provider 候选：前端传入 owner/repo 可能无 provider 信息。
        # P1-10：候选集由平台卡目录派生（repo_key_candidates），不再只查
        # gitcode/github 双前缀——gitee/gitlab 仓的 issue 也要命中。
        from src.kernel.repo_key import repo_key_candidates
        _rk_list = repo_key_candidates(repo_key)
        if _rk_list:
            _rk_ph = ",".join("?" * len(_rk_list))
            clauses.append(f"repo_key IN ({_rk_ph})")
            params.extend(_rk_list)
    # Issue #827: 绑定集合兜底——user_id 直接命中 OR created_by/assignee 命中当前用户平台用户名
    # （按 user_id 身份域反查登录名；查不到时退回原单一 user_id 口径，不放大可见范围）
    _login = _get_gitcode_user_by_user_id(user["id"])
    if _login:
        clauses.append("(user_id=? OR created_by=? OR assignee=?)")
        params.extend([user["id"], _login, _login])
    else:
        clauses.append("user_id=?")
        params.append(user["id"])
    # Issue #926 同款多状态过滤：statuses 优先于单 status（mine 列表分页后的服务端列过滤）。
    # N7 整改：互斥判定在构造期完成——statuses 合法则只压 IN 子句，否则只压单 status
    # 子句。不做后置 pop（clauses 与 params 下标不一一对应：'repo_key IN (?,?,?,?)'
    # 一条子句对应 4 个参数；按子句下标 pop 参数会删错位置的值 → 绑定整体错位）。
    _st_list = [x.strip() for x in (statuses or "").split(",") if x.strip()]
    if _st_list:
        _allowed = {"active", "processing", "awaiting_input",
                    "pending_review", "completed", "cancelled", "timeout_failed"}
        _st_ok = [x for x in _st_list if x in _allowed]
    else:
        _st_ok = []
    if _st_ok:
        clauses.append(f"status IN ({','.join('?' * len(_st_ok))})")
        params.extend(_st_ok)
    elif status:
        clauses.append("status=?")
        params.append(status)
    # Issue #926：q 服务端过滤（与 issues_list 同口径：标题/编号 LIKE，参数化）
    _q = (q or "").strip()
    if _q:
        clauses.append("(title LIKE ? OR issue_number LIKE ?)")
        params.extend([f"%{_q}%", f"%{_q}%"])
    # 组织隔离
    if _filter_org_id:
        clauses.append("org_id=?")
        params.append(_filter_org_id)
    _build_assignee_filter(assignees, clauses, params)
    # Issue #805：创建人过滤（前端 mine 分支发 creators=，此前被静默丢弃）
    _build_creator_filter(creators, clauses, params)
    where = " AND ".join(clauses)
    _order_dir = "DESC" if sort_order == "desc" else "ASC"
    _order_by = f"created_at {_order_dir}"
    if mode == "default":
        # Issue #926：与 issues_list / _merge_sort_items 同源（_status_order_sql），
        # 修复旧版仅 3 桶（active/processing/else）与 Python 4 级优先序口径不一致
        _order_by = f"{_status_order_sql()}, created_at {_order_dir}"
    # Issue #2082（#2082 移植）：缺省 per_page 注入灰度默认；>0 真分页（COUNT + LIMIT/OFFSET）
    page, per_page = resolve_issues_page_params(page, per_page)
    if per_page > 0:
        from src.kernel.session_manager import db, db_lock
        _total = 0
        try:
            with db_lock:
                c = db.cursor()
                c.execute(f"SELECT COUNT(*) FROM issue WHERE {where}", params)
                _total = c.fetchone()[0]
        except Exception as e:
            log.warning(f"issues mine: count failed: {e}")
        issues_out = _query_tasks(where, params, _order_by, include_user_id=False,
                                  limit=per_page, offset=(page - 1) * per_page)
        return {"issues": issues_out, "sort_order": sort_order, "mode": mode,
                "page": page, "per_page": per_page, "total": _total,
                "has_more": (page - 1) * per_page + len(issues_out) < _total}
    issues_out = _query_tasks(where, params, _order_by, include_user_id=False)
    return {"issues": issues_out, "sort_order": sort_order, "mode": mode}

_STATUS_PRIORITY = {"active": 0, "processing": 1, "awaiting_input": 2, "pending_review": 3}

def _status_order_sql() -> str:
    """Issue #926：default 模式状态优先序的 SQL CASE 片段（单一事实源）。

    与 Python 侧 _STATUS_PRIORITY 同源派生（终态兜底 4），消除 SQL CASE /
    _STATUS_PRIORITY / 前端映射三处口径复制导致的霰弹式同改风险。
    """
    _cases = " ".join(
        f"WHEN status='{_st}' THEN {_pr}"
        for _st, _pr in sorted(_STATUS_PRIORITY.items(), key=lambda kv: kv[1])
    )
    return f"CASE {_cases} ELSE 4 END"

def _merge_sort_items(items: list[dict], mode: str, sort_order: str) -> None:
    """Issue #926：与 SQL _order_by 对齐的就地稳定排序（issue 行 + PR 伪卡片统一合并口径）。

    两次稳定排序复刻 SQL「CASE 优先级 ASC, created_at DESC/ASC」：
    先按 created_at（方向取 sort_order），再按状态优先级（恒 ASC）稳定覆盖分组。
    mode=time 时仅按 created_at 排序。
    """
    items.sort(key=lambda it: str(it.get("created_at") or ""), reverse=(sort_order == "desc"))
    if mode != "time":
        items.sort(key=lambda it: _STATUS_PRIORITY.get(it.get("status") or "", 4))

def _filter_pr_items(pr_items: list[dict], q: str, statuses_set: set) -> list[dict]:
    """Issue #926：PR 伪卡片与 issue 行同口径的服务端过滤（q / statuses）。"""
    out = pr_items
    if statuses_set:
        out = [it for it in out if (it.get("status") or "pending") in statuses_set]
    if q:
        _needle = q.lower()
        out = [it for it in out
               if _needle in str(it.get("title") or "").lower()
               or _needle in str(it.get("issue_number") or "")]
    return out

def _cached_repo_list(ttl: float = 5.0):
    """需求中心列表的短 TTL 结果缓存。

    为什么必须加：列表接口单发 580ms，但 4 并发时每个请求涨到 1.8-2.5s —— 因为
    session_manager 是单连接 + 全局 db_lock，所有 DB 访问在 MySQL 下仍被串行化。
    而看板模式一次刷新会按状态分组并发打 6 个请求，串行叠加后直接顶穿前端 8s
    看门狗（症状：需求中心一直「加载中」→「加载失败: request aborted or timed out」）。
    彻底解法是把 DB 访问换成连接池（架构级改造），这里先用 5s 结果缓存把并发削成 1 次。

    key 含 user 标识：不同用户看到的需求集合不同，不能跨用户复用。
    （此装饰器曾在 clarify to_thread 提交中被误删，QA G1 判 BLOCKED；此处恢复。）
    """
    def deco(fn):
        _cache: dict = {}
        _lock = _th_cache.Lock()

        @_ft_cache.wraps(fn)
        def wrapper(*args, **kwargs):
            _u = kwargs.get("user") or {}
            _uid = (_u.get("id") if isinstance(_u, dict) else None) or ""
            _key = (
                _uid,
                tuple(str(a) for a in args),
                tuple(sorted((k, str(v)) for k, v in kwargs.items() if k != "user")),
            )
            _now = _time_cache.time()
            _hit = _cache.get(_key)
            if _hit and (_now - _hit[0]) < ttl:
                return _copy_cache.deepcopy(_hit[1])
            with _lock:
                _hit = _cache.get(_key)
                _now = _time_cache.time()
                if _hit and (_now - _hit[0]) < ttl:
                    return _copy_cache.deepcopy(_hit[1])
                _res = fn(*args, **kwargs)
                _cache[_key] = (_now, _copy_cache.deepcopy(_res))
                if len(_cache) > 64:
                    for _k in list(_cache)[:-64]:
                        _cache.pop(_k, None)
                return _res

        return wrapper
    return deco


@router.get("/{owner}/{repo}", operation_id="issues_list", summary="Repo issue list (stage/status enriched)", description="Repo issue list (unified all view) enriched with stage/status/events. Issue #2082 paging (semantics unified with issues_mine/gitrepo_milestones): page is 1-based; per_page defaults to 20 when omitted (env ISSUES_DEFAULT_PER_PAGE, set 0 to restore the legacy full-list default), clamped to max 100 (over-limit clamped, not rejected), 0 means explicit full list; `per_page>0` enables true server-side paging (LIMIT/OFFSET, response carries total/page/per_page/has_more); default `per_page=0` returns the full list with terminal statuses (completed/cancelled) capped to the `recent_terminal` (default 30) most recent (pass `recent_terminal=0` or an explicit `status` filter for the full set). Issue #926: PR pseudo-cards merge into the same LIMIT/OFFSET paging window (counted in total); `q` (title/number substring) and `statuses` (comma-separated multi-status) add server-side filtering; paged responses slim attachments to `attachments_count`.")
@_cached_repo_list(5.0)
def list_repo_issues(owner: str, repo: str,
                     status: str = Query("", pattern="^(active|processing|awaiting_input|pending_review|completed|cancelled|)$"),
                     sort_order: str = Query("desc", pattern="^(desc|asc)$", description="按创建时间排序方向"),
                     mode: str = Query("default", pattern="^(default|time)$", description="default=状态优先；time=纯时间序"),
                     assignees: str = Query("", description="按责任人筛选(逗号分隔)"),
                     creators: str = Query("", description="按创建人筛选(逗号分隔, Issue #805)"),
                     q: str = Query("", max_length=200, description="Issue #926：标题/编号模糊搜索（服务端过滤）"),
                     statuses: str = Query("", max_length=200, description="Issue #926：多状态筛选（逗号分隔 issue.status）"),
                     page: int = Query(1, ge=1, description="页码（1-based，per_page>0 时生效）"),
                     per_page: int | None = Query(None, ge=0, description="每页条数；缺省=默认 20（ISSUES_DEFAULT_PER_PAGE 可调，置 0 回退 legacy 全量）；0=显式全量（终态不再截断）；上限 100，超限夹取"),
                     recent_terminal: int | None = Query(None, ge=0, description="全量模式下终态(completed/cancelled)保留的最近条数；缺省=30（显式 per_page=0 的全量逃生口缺省=0 不截断）；0=全量"),
                     include_deleted: int = Query(0, ge=0, le=1, description="Issue #995：默认过滤 VCS 侧已确认删除的卡片（回写 vcs_exists=0 且已核验）；1=审计模式返回并标记。未核验卡片按存在处理（fail-open 不隐藏）。"),
                     user: dict = Depends(require_org_member)):
    """仓库 Issue 列表（归一：收编原 repo/{owner}/{repo}，复用 _query_tasks 富化 + 事件聚合）。

    - 按 owner/repo 定位仓库，返回全部卡片富化（scene/stage/is_running/agent_statuses）+ events_count/latest_event
    - 排序按 created_at + 状态优先级（mode 可切换纯时间序）
    - 行为对齐原 issues_list（仓库 public 读聚合，无显式仓库权限门禁）
    - Issue #995 VCS 存在性过滤：默认隐藏「已确认删除」（vcs_exists=0 且
      vcs_checked_at 已核验）的卡片，include_deleted=1 审计返回并标记
      （vcs_check/is_vcs_deleted 回显）；未核验（vcs_checked_at 空）fail-open
      不隐藏。过滤条件进同一 WHERE，分页 total/has_more 与过滤集合一致。
    - Issue #542 三种模式：
      * per_page>0：真分页（COUNT + LIMIT/OFFSET，响应含 total/page/per_page/has_more）
        Issue #926：PR 伪卡片并入同一分页窗口（issue 行窗口取 offset+per_page+PR 数，
        与全量 PR 卡按同排序键合并后切页；total 计入 PR 卡，滚动累计与 total 一致）
      * 默认（per_page=0，无 status 过滤，recent_terminal>0）：非终态全量 + 终态最近 N 条
        （分析文档第一步"数据截断"：前端不改也能先受益，翻页再拉走 per_page）
      * recent_terminal=0 或显式 status 过滤：legacy 全量返回
    - Issue #926：q（标题/编号 LIKE）与 statuses（逗号分隔多状态）为服务端过滤，
      PR 伪卡片同口径过滤（statuses 匹配映射后的 task_status）。
    """
    from src.api.issues_center import _query_tasks, _build_assignee_filter, _build_creator_filter, resolve_issues_page_params
    from src.domains.mgmt.public import is_org_personal
    _org_id = user.get("org_id", 0)
    _is_personal = is_org_personal(_org_id) if _org_id else True
    _filter_org_id = 0 if _is_personal else _org_id
    repo_keys = _repo_keys_of(owner, repo)
    # Issue #926 性能补丁（2026-11-04 压测）：repo 可能同时映射 gitcode/github 两个 key
    # （镜像仓），而 `repo_key IN (a,b)` 会让排序退化成"两个分支各自有序再归并"——
    # 表达式索引只能逐个分支用上，最终仍建临时 B-TREE（20 万行深页 131ms，单 key 为 1ms）。
    # 空 key 对结果零贡献，先剔掉只留有数据的 key；IN 退化为单值/少值时可走有序索引。
    try:
        from src.kernel.session_manager import db as _db, db_lock as _lock
        _live = []
        for _k in repo_keys:
            with _lock:
                _c = _rt_db.cursor()
                _c.execute("SELECT 1 FROM issue WHERE repo_key=? LIMIT 1", (_k,))
                if _c.fetchone():
                    _live.append(_k)
        if _live:
            repo_keys = _live
    except Exception:
        pass
    placeholders = ",".join("?" * len(repo_keys))
    clauses = [f"repo_key IN ({placeholders})"]
    params: list = list(repo_keys)
    # Issue #995 VCS 存在性过滤（spec 验收面，2026-11-13 补齐）：默认隐藏
    # 「已确认删除」（vcs_exists=0 且已核验）的卡片；include_deleted=1 审计
    # 旁路。未核验（vcs_checked_at 空）不进过滤——fail-open 不隐藏。
    if not include_deleted:
        clauses.append("NOT (vcs_exists=0 AND vcs_checked_at IS NOT NULL AND vcs_checked_at!='')")
    if status:
        clauses.append("status=?")
        params.append(status)
    # Issue #926：多状态服务端过滤（白名单校验，防注入）
    statuses_set = set()
    if statuses:
        _allowed = {"active", "processing", "awaiting_input", "pending_review", "completed", "cancelled"}
        statuses_set = {s.strip() for s in statuses.split(",") if s.strip()} & _allowed
        if not statuses_set:
            # Issue #926：全部为白名单外值时与 status 参数 pattern 校验（400）口径一致，
            # 显式拒绝而非静默退化为无过滤
            raise HTTPException(status_code=400, detail={
                "code": 400, "message": "statuses 参数不含任何合法状态值（active/processing/awaiting_input/pending_review/completed/cancelled）"})
        clauses.append(f"status IN ({','.join('?' * len(statuses_set))})")
        params.extend(sorted(statuses_set))
    # Issue #926：q 搜索（标题/编号子串，大小写不敏感）
    if q:
        clauses.append("(LOWER(title) LIKE ? OR CAST(issue_number AS TEXT) LIKE ?)")
        _like = f"%{q.lower()}%"
        params.extend([_like, _like])
    # 组织隔离
    if _filter_org_id:
        clauses.append("org_id=?")
        params.append(_filter_org_id)
    _build_assignee_filter(assignees, clauses, params)
    where = " AND ".join(clauses)
    _order_dir = "DESC" if sort_order == "desc" else "ASC"
    _order_by = f"created_at {_order_dir}"
    if mode == "default":
        _order_by = f"{_status_order_sql()}, created_at {_order_dir}"

    # Issue #2082（#2082 移植）：缺省 per_page 注入灰度默认（默认 20；开关置 0 → legacy per_page=0 路径）。

    # 显式 per_page=0 = 全量逃生口 → recent_terminal 缺省联动为 0（终态不截断，真正全量）；

    # 未显式传 per_page（含开关置 0 的 legacy 回退）→ recent_terminal 维持缺省 30（现状一致）。

    _per_page_explicit = per_page is not None

    page, per_page = resolve_issues_page_params(page, per_page)

    if recent_terminal is None:

        recent_terminal = 0 if (_per_page_explicit and per_page == 0) else 30


    _total = None
    _page_row_count = None
    # Issue #926：PR 伪卡片先取（全量聚合，含 q/statuses 同口径过滤），
    # 分页模式下并入统一 LIMIT/OFFSET 窗口
    pr_items = _filter_pr_items(_query_pr_subscription_events(repo_keys), q, statuses_set)
    for _pit in pr_items:
        _pit.setdefault("attachments", [])
        # Issue #805: PR 伪卡片无创建人概念（触发者语义保留 assignee），creator 置空防前端 undefined
        _pit.setdefault("creator", "")
    if per_page > 0:
        # ---- 真分页（Issue #926：PR 伪卡片并入统一窗口）----
        from src.kernel.session_manager import db, db_lock
        # Issue #926 性能补丁（2026-11-04 压测）：q 走 `LOWER(title) LIKE '%kw%'`，前后
        # 通配符下任何普通索引都用不上——5 万行 COUNT 74ms、20 万行约 300ms，占一次搜索
        # 请求的大头。q 模式不返精确 total（total=null），has_more 按"本页是否满页"推断
        # （最多多探一次空页，前端已按 has_more 终止）；无 q 时 COUNT 走索引，保持精确值。
        if q:
            _total = None
        else:
            try:
                with db_lock:
                    c = _rt_db.cursor()
                    c.execute(f"SELECT COUNT(*) FROM issue WHERE {where}", params)
                    _total = c.fetchone()[0]
            except Exception as e:
                log.warning(f"issues: count failed: {e}")
                _total = 0
            _total += len(pr_items)  # PR 卡计入 total（滚动累计口径一致）
        _offset = (page - 1) * per_page
        # 性能守卫（2026-11-04 压测）：default 模式第一排序键是状态优先级 CASE
        # （_status_order_sql），无法免排序；偏移越深单页越贵（issue 表 20 万行时
        # page=6666 达 350ms），且分页查询持 db_lock，会串行阻塞其它 DB 访问。
        # 超过 2 万条偏移直接拒绝并提示收窄，避免个别客户端把单核吃满。
        if _offset > 20000:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"深分页超过上限（偏移 {_offset} 条 > 20000），请用 q / statuses 收窄后再翻页"})
        # Issue #926：窗口化取数（替代深页 0..offset+per_page+pr 全前缀拉取，O(page) 而非 O(offset)）。
        # 合并序中 issue 行的全局位次 p 满足 seq-pr ≤ p ≤ seq（pr=PR 卡总数），故页窗口
        # [_offset, _offset+per_page) 内的 issue 行其 seq 必落在 [max(0,_offset-pr), _offset+per_page)；
        # 本地合并列表（窗口 issue 行 + 全部 PR 卡）排序后，前缀恰有 min(_offset, pr) 条
        # 全局位次 < _offset 的多余项，从该位置切片即得全局页窗口。
        _pr_count = len(pr_items)
        issues = _query_tasks(where, params, _order_by, include_user_id=True,
                              limit=per_page + _pr_count, offset=max(0, _offset - _pr_count))
        _merged = list(issues) + list(pr_items)
        _merge_sort_items(_merged, mode, sort_order)
        _page = _merged[min(_offset, _pr_count):][:per_page]
        issues = [it for it in _page if not it.get("is_pr_event")]
        _page_pr = [it for it in _page if it.get("is_pr_event")]
        _page_row_count = len(_page)
    elif status or statuses_set or recent_terminal <= 0:
        # ---- legacy 全量（显式 status 过滤 = 调用方明确要该状态全量）----
        issues = _query_tasks(where, params, _order_by, include_user_id=True)
    else:
        # ---- 默认截断：非终态全量 + 终态最近 recent_terminal 条 ----
        # mode=default 的 CASE 优先序（非终态 0-3 < 终态 4）下两段拼接即全局序；
        # mode=time 时合并后按 created_at 重排
        _nt_where = where + " AND status IN ('active','processing','awaiting_input','pending_review')"
        _t_where = where + " AND status NOT IN ('active','processing','awaiting_input','pending_review')"
        issues = _query_tasks(_nt_where, params, _order_by, include_user_id=True)
        _terminal = _query_tasks(_t_where, params, f"created_at {_order_dir}", include_user_id=True,
                                 limit=recent_terminal)
        issues = issues + _terminal
        if mode == "time" and issues:
            issues.sort(key=lambda it: it.get("created_at") or "", reverse=(sort_order == "desc"))

    # 事件聚合字段 + number/task_status/owner_agent 等别名（原 issues_list 字段兼容）
    # Issue #542：聚合按返回集合收窄（只格式化返回卡片的 events，不扫全仓事件）
    _numbers = [str(it.get("issue_number")) for it in issues if it.get("issue_number")]
    group_map = _group_events_by_issue(repo_keys, numbers=_numbers or None)
    stage_map = _issue_stage_map(repo_keys, numbers=_numbers or None)
    # Issue #926 + #648：仓库级批量附件映射（避免逐卡 OBS list）
    _attach_map = _issue_attachments_batch(repo_keys)
    for it in issues:
        _num = it.get("issue_number")
        it["number"] = _num or it.get("number")
        it["task_status"] = it["task_status"] if "task_status" in it else it.get("status", "")
        _st = stage_map.get(str(_num or "")) or {}
        it["owner_agent"] = it.get("owner_agent") or _st.get("owner_agent", "vod")
        it.setdefault("assignee", _st.get("assignee", ""))
        it.setdefault("parent_issue_id", _st.get("parent_issue_id", ""))
        it.setdefault("is_subtask_mode", _st.get("is_subtask_mode") or 0)
        evs = group_map.get(str(_num or "")) or []
        it["events_count"] = len(evs)
        # P1-E: Slim latest_event to essential fields (reduce response from ~713KB)
        _ev = evs[-1] if evs else None
        if _ev:
            it["latest_event"] = {
                "seq": _ev.get("seq"),
                "status": _ev.get("status"),
                "title": _ev.get("title"),
                "created_at": _ev.get("created_at"),
            }
        else:
            it["latest_event"] = None
        # P1-E: Remove heavy fields not needed in list view (detail page has /issues/{id}/pipeline)
        it.pop("agent_statuses", None)
        it.pop("task_description", None)
        # Issue #648：统一列表卡片带附件清单（无附件为 []，前端附件区直接消费）
        # PR 订阅伪卡片（is_pr_event）编号与 GitCode PR 对应，不是 issue 附件目录编号；
        # PR 号与同仓 issue 号重合时会命中错误附件目录，显式置 [] 防错配
        # Issue #926 + #648 合并口径：附件清单走仓库级批量映射（_attach_map，避免逐卡 OBS list）；
        # 分页模式瘦身——不回 attachments 清单（仅 attachments_count），计数直接取批量映射长度
        # （无需再走 _issue_attachments_count 的 URL 预签名/元数据构造成本），前端按 count
        # 判定 count=0 免请求直渲 / count>0 走 /issues/{id}/attachments 异步填充。
        if it.get("is_pr_event"):
            it["attachments"] = []
            it["attachments_count"] = 0
        else:
            _att = _attach_map.get(str(_num or ""), [])
            if per_page > 0:
                it.pop("attachments", None)
            else:
                it["attachments"] = _att
            it["attachments_count"] = len(_att)

    # PR 订阅事件伪卡片混入。Issue #926：分页模式已按合并序切页挑出本页 PR 卡；
    # Issue #1816：非分页（线上全量）路径不再整块追加到末尾，PR 伪卡片与真实 Issue
    # 同键合并重排，复用 _merge_sort_items 与 SQL/分页路径同源排序口径。
    if per_page > 0:
        issues.extend(_page_pr)
    else:
        issues.extend(pr_items)
        _merge_sort_items(issues, mode, sort_order)

    # Issue #542：分页模式返回服务端分页元数据；非分页模式 total 维持 len(issues)（兼容旧语义）
    if _total is not None:
        return {"issues": issues, "total": _total, "page": page, "per_page": per_page,
                "has_more": (page - 1) * per_page + (_page_row_count or 0) < _total}
    if per_page > 0:
        # q 模式（不精确 total）：满页即认为还有下一页，最多多探一次空页
        return {"issues": issues, "total": None, "page": page, "per_page": per_page,
                "has_more": (_page_row_count or 0) >= per_page}
    return {"issues": issues, "total": len(issues)}

@router.post("/{owner}/{repo}", operation_id="issues_create", summary="Create issue (unified entry)", description="Creates an Issue (unified external entry): creates the GitCode issue, records it and triggers the pipeline. Body: repo_key?, title, description?, assignee?, scene_id?, is_subtask_mode.")
async def create_issue(owner: str, repo: str, body: dict = None,
                       user: dict = Depends(require_org_member)):
    """创建 Issue（对外唯一创建入口）：建 GitCode Issue + 建档 + 触发 pipeline。

    body: {repo_key?, title, description?, assignee?, scene_id?, is_subtask_mode?}
    repo_key 可选（缺省按 {owner}/{repo} 拼）。返回 {ok, issue_number, task_id, created, ...}
    """
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    from src.api.org_context import validate_org_scene_entry
    _body = body or {}
    # D107/B2 补口（2026-11-11 实弹发现）：默认 repo_key 是裸名（{owner}/{repo}），
    # 建单链 _is_github 按前缀分流——裸名恒走 gitcode 链，github 仓（托管或 member）
    # 的用户 PAT 回落永不被命中（502 查重打错平台）。入口统一按 managed/user_repos
    # 解析真前缀（与知识面 _resolve_repo_key 同口径）；显式传 repo_key 不受影响。
    _rk_in = _body.get("repo_key") or ""
    if ":" not in _rk_in:
        from src.domains.mgmt.public import _resolve_repo_key as _resolve_rk
        _rk_in = _resolve_rk(f"{owner}/{repo}")
    # Issue #2118：组织场景入口校验（org-only 模板只能由组织上下文使用）
    _repo_key = _rk_in
    _scene_id = _body.get("scene_id", "") or ""
    validate_org_scene_entry(
        _repo_key,
        _scene_id,
        str(user.get("org_id") or ""),
    )
    b = TaskCreateBody(
        repo_key=_repo_key,
        title=_body.get("title", "") or "",
        description=_body.get("description", "") or "",
        assignee=_body.get("assignee", "") or "",
        scene_id=_scene_id,
        is_subtask_mode=int(_body.get("is_subtask_mode") or 0),
        attachments=_body.get("attachments") or [],
    )
    return await create_task_via_issue(b, user)

@router.get("/{owner}/{repo}/{number}", operation_id="issues_detail", summary="Issue detail (stage+events+pipeline)", description="Single issue detail: base fields + events (seq asc) + pipeline segments. D105 string-ident: `number` accepts numeric idents and platform string idents (gitee IK*, # prefix tolerated).")
def get_issue_detail(owner: str, repo: str, number: str,
                     user: dict = Depends(get_current_user_or_api_key)):
    repo_keys = _repo_keys_of(owner, repo)
    _enforce_session_repo_scope(user, repo_keys)
    # D105 字符串 ident（gitee IK*）：路径参数改 str——纯数字归一去前导零语义不变，
    # 非数字（IK*/#IK*）原样透传给 ledger 查找（number: int 会 422 拒掉 gitee ident）。
    num = number[1:] if number[:1] == "#" else number
    if num.isdigit():
        num = str(int(num))
    stage_map = _issue_stage_map(repo_keys)
    status_map = _task_status_map(repo_keys)
    group_map = _group_events_by_issue(repo_keys)
    title_map = _issue_title_map(repo_keys)
    if num not in stage_map and num not in status_map and num not in group_map:
        # Issue #672：本地聚合 miss 时按 number+owner/repo 走 git-repo 通道兜底，
        # 消除 issues detail 与 git-repo issues get 同编号 404/正常分裂（eduSwarm 实测）。
        _vcs = _vcs_issue_lookup(owner, repo, number, user)
        if _vcs:
            _vi = _vcs.get("issue") or {}
            _asg = _vi.get("assignee") or ""
            if isinstance(_asg, dict):
                _asg = _asg.get("login") or _asg.get("username") or ""
            # Issue #805: VCS 兜底详情透出创建人（GitCode issue 作者 login）
            _vcs_user = _vi.get("user") or {}
            _vcs_creator = (_vcs_user.get("login") or _vcs_user.get("username") or "") if isinstance(_vcs_user, dict) else ""
            from src.domains.runtime.vcs.api_client import _native_issue_number
            return {
                "number": _native_issue_number(number),
                "issue_id": None,
                "title": (_vi.get("title") or f"Issue #{number}"),
                "stage": "vod",
                "task_status": "",
                "owner_agent": "vod",
                "scene_id": "",
                "assignee": _asg,
                "creator": _vcs_creator,
                "is_subtask_mode": 0,
                "parent_issue_id": "",
                "events_count": 0,
                "latest_event": None,
                "events": [],
                "pipeline": {"issue": {"number": _native_issue_number(number)}, "summary": "", "status": "unknown", "segments": []},
                "source": "vcs",
                "vcs_provider": _vcs.get("provider", ""),
                "vcs_id": _vi.get("id"),
                "state": _vi.get("state", ""),
            }
        # Issue #667：三表未命中 → VCS 兜底核实并自动补建内部行（存在）/
        # 语义区分 404（不存在 vs 内部缺行且无法核实）
        _state = _sync_missing_issue_row(repo_keys, number)
        if _state != "exists":
            raise _detail_404("issue_not_found" if _state == "missing" else "internal_sync_missing", number)
        stage_map = _issue_stage_map(repo_keys)
        status_map = _task_status_map(repo_keys)
        group_map = _group_events_by_issue(repo_keys)
        title_map = _issue_title_map(repo_keys)
        _fallback_synced = True
    base = _assemble_issue(repo_keys[0], num, stage_map, status_map, group_map, title_map)
    events = group_map.get(num) or []
    pipeline = _pipeline_from_archive(repo_keys, num) or _pipeline_from_events(repo_keys[0], num, events)
    # Issue #539：详情内嵌 pipeline 同步叠加需求级业务状态与任务/结果引用
    from src.api.pipeline_status import (load_issue_business_status, load_segment_task_refs,
                                         enrich_pipeline_business_status, _pipeline_segment_seqs)
    _biz_stage, _biz_status = load_issue_business_status(repo_keys, num)
    enrich_pipeline_business_status(
        pipeline, _biz_stage, _biz_status,
        load_segment_task_refs(repo_keys, _pipeline_segment_seqs(pipeline)))
    return {**base, "events": events, "pipeline": pipeline}

@router.get("/{owner}/{repo}/{number}/events", operation_id="issues_events", summary="Issue events list (with structured output)", description="Event list of an issue (seq asc), each with structured output/logs when present. `number` accepts `5` or `#5`.")
def get_issue_events(owner: str, repo: str, number: str,
                     user: dict = Depends(get_current_user_or_api_key)):
    repo_keys = _repo_keys_of(owner, repo)
    _enforce_session_repo_scope(user, repo_keys)
    group_map = _group_events_by_issue(repo_keys)
    num = _num_from_issue_id(number) or number.replace("#", "")
    if num not in group_map:
        stage_map = _issue_stage_map(repo_keys)
        status_map = _task_status_map(repo_keys)
        if num not in stage_map and num not in status_map:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"Issue #{number} 不存在"})
        return {"events": [], "segments": [], "total": 0}
    events = group_map[num]
    return {"events": events, "segments": events, "total": len(events)}

@router.get("/{owner}/{repo}/{number}/pipeline", operation_id="issues_pipeline", summary="Issue pipeline roadmap", description="Pipeline roadmap of an issue: issue + summary + status + segments. Issue #539: top-level `status` means event execution finished (not requirement completion); business status is exposed via optional `issue_stage`/`issue_task_status`/`review_status`, and each segment output carries optional `task_ref`/`result_ref` (explicit enums: available/pending/unavailable; no restricted logs or credentials).")
def get_issue_pipeline(owner: str, repo: str, number: str,
                       user: dict = Depends(get_current_user_or_api_key)):
    repo_keys = _repo_keys_of(owner, repo)
    # D105 字符串 ident（gitee IK*）：与 issues_detail 同口径（number: int 对 IK* 422）。
    num = number[1:] if number[:1] == "#" else number
    if num.isdigit():
        num = str(int(num))
        number = num
    group_map = _group_events_by_issue(repo_keys)
    events = group_map.get(num) or []
    # weekly-05：优先 events 表（含结构化 output）；无事件时回退 event.json 档案
    if events:
        pipeline = _pipeline_from_events(repo_keys[0], num, events)
    else:
        pipeline = _pipeline_from_archive(repo_keys, num) or {
            "issue": {"number": number}, "summary": "", "status": "unknown", "segments": []}
    if not pipeline.get("segments") and num not in _issue_stage_map(repo_keys):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"Issue #{number} 不存在"})
    # Issue #539：叠加需求级业务状态 + 各段任务/结果引用（旧字段语义不变，新增字段可选）
    from src.api.pipeline_status import (load_issue_business_status, load_segment_task_refs,
                                         enrich_pipeline_business_status, _pipeline_segment_seqs)
    _biz_stage, _biz_status = load_issue_business_status(repo_keys, num)
    enrich_pipeline_business_status(
        pipeline, _biz_stage, _biz_status,
        load_segment_task_refs(repo_keys, _pipeline_segment_seqs(pipeline)))
    return pipeline

# ── Issue #627：已完成需求的生产续接（发布）子单 ──────────────────────────────
# 由 weekly-10 迁入（原实现 src/domains/runtime/routes/issues.py）；依赖 issues_center.create_release_subissue
# 与 session_store.list_release_subissues，均为现有模块。
from pydantic import Field as _Field

class _ProductionReleaseBody(BaseModel):
    candidate_sha: str = ""
    target_env: str = ""
    # PR #799 R3：与 target_env（≤64）宽严一致，验收证据引用限长（防超大 payload 直接落库）
    evidence_ref: str = _Field(default="", max_length=512)
    approved_at: str = ""

@router.post("/{issue_id}/production-release", operation_id="issues_production_release",
             summary="Create production release task for a completed issue (Issue #627)",
             description="User-authorized production continuation (release) entry for done/completed "
                         "issues: creates a unique release task bound to (issue, repo, full candidate "
                         "SHA, acceptance evidence, target env, approval source), then dispatches a "
                         "controlled sre a2a_event carrying the binding context. Validation chain: "
                         "auth -> terminal-state -> SRE acceptance -> candidate consistency -> target "
                         "env -> approval TTL -> uniqueness. Rejections preserve original acceptance "
                         "evidence (zero writes on reject paths). Result is backfilled separately "
                         "(deployed_success / deploy_failed / not_executed) — stage completed never "
                         "implies release success. Does NOT modify _TASK_STATUS_FLOWS terminal "
                         "semantics (bypass model). User-only: session token (tk_) -> 403.")
def create_production_release_endpoint(issue_id: int, body: _ProductionReleaseBody,
                                       user: dict = Depends(require_no_session_token)):
    """Issue #627: 已完成需求的生产续接（发布）子单入口（用户身份授权，方案 C）。

    创建「发布子单」（复用现有子任务机制：parent_issue_id + is_subtask_mode=1，绑定
    repo/候选SHA/目标环境/验收引用，parent=本需求），子单 stage=sre 直通标准流水线
    到 sre 执行部署，子单终态 release_result 即发布结果三分支；零状态机改动、零平行表。

    PR #799 R4 保留：本端点为同步 def（FastAPI 自动调度线程池执行）——派发链路
    （a2a_endpoint，60s 超时）不再阻塞事件循环，派发慢时全站 API 不受影响。
    """
    from src.api.issues_center import create_release_subissue, ProductionReleaseError
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number, user_id, status FROM issue WHERE id=? "
                  "AND (user_id=? OR user_id=0)", (issue_id, user["id"]))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    _repo_key, _issue_num, _owner_uid, _status = row[0], row[1], row[2], row[3]
    # 平台建单 token：优先仓库 bot token，其次用户 GitCode token（测试环境可为空，由建单失败兜底）
    _api_token = ""
    try:
        from src.domains.mgmt.public import get_repo_token_with_refresh  # 跨域走 owner public
        _api_token = get_repo_token_with_refresh(_repo_key) or ""
    except Exception:
        _api_token = ""
    if not _api_token:
        try:
            # D37/S57：repo 上下文必须按 repo_key 前缀派生 provider，禁止折叠 gitcode
            from src.api.deps import get_user_token_for_repo
            _api_token = get_user_token_for_repo(_repo_key, user.get("username") or "",
                                                 user_id=user.get("id") or "") or ""
        except Exception:
            _api_token = ""
    try:
        out = create_release_subissue(
            repo_key=_repo_key, issue_number=_issue_num,
            candidate_sha=body.candidate_sha or "",
            target_env=body.target_env or "",
            evidence_ref=body.evidence_ref or "",
            approved_by=str(user.get("id") or ""),  # D105：账号 id 已 UUID 字符串化（原 int() 崩溃）
            approved_at=body.approved_at or "",
            api_token=_api_token)
    except ProductionReleaseError as _e:
        raise HTTPException(status_code=_e.status, detail={"code": _e.status, "message": _e.message,
                                                           "error_code": _e.code})
    out["issue_id"] = issue_id
    return out

@router.get("/{issue_id}/production-release", operation_id="issues_production_release_list",
            summary="Query production release tasks of an issue (Issue #627)",
            description="Lists production release tasks for the issue with status, result branch "
                        "(deployed_success/deploy_failed/not_executed), failure reason and binding "
                        "elements (candidate SHA / target env / evidence / approval source).")
def list_production_releases(issue_id: int, user: dict = Depends(require_no_session_token)):
    """Issue #627: 查询发布子单列表（状态/结果三分支/失败原因/绑定要素随行返回）。"""
    from src.api.session_store import list_release_subissues
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        c.execute("SELECT repo_key, issue_number, user_id FROM issue WHERE id=? "
                  "AND (user_id=? OR user_id=0)", (issue_id, user["id"]))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    _repo_key, _issue_num = row[0], row[1]
    releases = list_release_subissues(_repo_key, _issue_num)
    return {"issue_id": issue_id, "issue_number": _issue_num, "repo_key": _repo_key,
            "releases": releases, "total": len(releases)}

@router.get("/{issue_id}/pipeline", operation_id="issues_pipeline_detail", summary="Issue pipeline roadmap (by issue.id)", description="Returns the issue pipeline info and event history for the detail page. Issue #539: adds optional business status fields (`issue_stage`/`issue_task_status`/`review_status`, pending_review = awaiting human confirmation) and `sre_result` (task_ref + result_ref with explicit enums) so the detail page distinguishes 'stage execution finished' from 'awaiting review'.")
def get_issue_pipeline_by_id(issue_id: int, user: dict = Depends(require_no_session_token)):
    """获取需求关联的流水线信息和事件历史（详情页展示）。"""
    from src.api.event_store import get_events
    from src.api.pipeline_config import get_pipeline_config
    from src.api.session_store import get_issue_stage, get_issue_scene
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = _rt_db.cursor()
        # 仓库全部视图下，用户可以看到其他人的 Issue
        # 按 id 查出 Issue，再校验用户是否有该仓库的访问权限
        c.execute("SELECT repo_key, issue_number, status FROM issue WHERE id=?",
                  (issue_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        repo_key, issue_number, _issue_task_status = row[0], row[1], (row[2] or "")
    # 校验仓库访问权限（与"仓库全部"列表接口一致）
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    issue_key = f"#{issue_number}" if issue_number else ""
    _scene_tuple = get_issue_scene(repo_key, issue_key)
    _scene_id = (_scene_tuple[0] if _scene_tuple and _scene_tuple[0] else "") or "default"
    _stage_info = get_issue_stage(repo_key, issue_key) if issue_key else None
    _current_stage = (_stage_info or {}).get("stage", "") if _stage_info else ""
    pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key)
    nodes = (pipe_cfg or {}).get("nodes", [])
    events = []
    if issue_key:
        events = get_events(repo_key, issue_id=issue_key, limit=30, include_payload=True)
    # 统一计算节点状态（与菜单流水线页一致；compute_node_statuses 兼算 latest_event）
    from src.api.pipeline_status import compute_node_statuses
    nodes, _ = compute_node_statuses(
        nodes, events, _current_stage, repo_key=repo_key, issue_id=issue_key)
    # Issue #329: extract carried_context for cross-pipeline continuation source annotation
    from src.api.session_store import get_carried_context

from pydantic import BaseModel, Field

from src.api.issues_center import (TaskAwaitingInputBody, TaskPendingReviewBody,
                                   _review_pass, _review_reject, _set_issue_pending_review,
                                   _read_issue_status, cancel_issue_rows)

def _detail_404(reason: str, number) -> HTTPException:
    """Issue #667：detail 404 语义区分——「Issue 不存在（VCS 侧亦无）」vs「需求不存在（内部缺行）」。"""
    if reason == "issue_not_found":
        return HTTPException(status_code=404, detail={
            "code": 404, "reason": "issue_not_found",
            "message": f"Issue #{number} 不存在（GitCode 侧无此 Issue，可能已删除或编号有误）",
            "suggestion": "确认 Issue 编号；已删除的 Issue 属预期 404"})
    return HTTPException(status_code=404, detail={
        "code": 404, "reason": "internal_sync_missing",
        "message": f"需求不存在：VCS 侧 Issue #{number} 存在性无法核实且内部需求库缺行（同步未落或时序未到）",
        "suggestion": "稍后重试（webhook 同步到位后自动恢复）；持续失败请检查仓库 token 与 webhook 同步链路"})

def _sync_missing_issue_row(repo_keys: list[str], number) -> str:
    """Issue #667 兜底解析：内部三表未命中时按 VCS 核实并自动补建 issue 行。

    返回：
    - "exists"：已补建（或已存在）内部行，调用方重读聚合后正常返回
    - "missing"：VCS 侧明确 404（Issue 不存在）
    - "unknown"：VCS 无法核实（token 缺失/网络异常），调用方按降级语义 404
    """
    num = str(number)
    from src.domains.runtime.pipeline.event_pipeline import fetch_issue_via_vcs
    for rk in repo_keys:
        try:
            state, data = fetch_issue_via_vcs(rk, num)
        except Exception:
            state, data = "unknown", {}
        if state == "exists":
            try:
                from src.api.issues_center import ensure_issue_task
                # Issue #805: VCS 兜底补建行时带上创建人（GitCode issue 作者）
                _fb_user = (data or {}).get("user") or {}
                _fb_creator = (_fb_user.get("login") or _fb_user.get("username") or "") \
                    if isinstance(_fb_user, dict) else ""
                # D105 字符串 ident（gitee IK*）：原生形态直接透传（ensure_issue_task
                # 内部按字符串入 SQL/文案；int() 会把 IK* 崩掉）
                ensure_issue_task(rk, num, title=str((data or {}).get("title", "") or ""),
                                  event_type="issue_opened", creator=_fb_creator)
                return "exists"
            except Exception as e:
                log.warning(f"issues: fallback ensure_issue_task failed repo={rk} #{num}: {e}")
                return "unknown"
        if state == "missing":
            return "missing"
    return "unknown"

def _attach_event_payloads(group_map: dict) -> None:
    """按需补 structured_output / agent_logs（Issue #859 性能修复，2026-11-03）。

    旧实现把 payload 两列塞进事件主查询：单仓 2 万事件 × 平均 8.3KB 日志 ≈ 166MB
    传输 + 2 万次 JSON 解析（实测 50ms → 386ms@5KB/条），且全程持 db_lock。
    现在主查询只取 12 列元数据，再按 seq 只补"每个 issue 最新 N 条"的 payload：
    单次最多 _EVENT_PAYLOAD_MAX_EVENTS 条、单条最多 _EVENT_PAYLOAD_MAX_CHARS 字符
    （超出即截断并打 *_truncated 标记，避免 MB 级大字段拖垮响应）。
    未命中的事件保留 output=None/logs=[]（与原响应形状一致）并标记 payload_omitted。
    """
    picks = []
    for evs in group_map.values():
        picks.extend(evs[-_EVENT_PAYLOAD_PER_ISSUE:])
    if len(picks) > _EVENT_PAYLOAD_MAX_EVENTS:
        picks = picks[-_EVENT_PAYLOAD_MAX_EVENTS:]
    if not picks:
        return
    cap = _EVENT_PAYLOAD_MAX_CHARS
    seqs = [int(p["seq"]) for p in picks]
    placeholders = ",".join("?" * len(seqs))
    from src.kernel.session_manager import db, db_lock
    import json as _json
    try:
        with db_lock:
            c = _rt_db.cursor()
            # SUBSTR 在 sqlite/MySQL 语义一致；多取 1 字符用于判断是否被截断
            # payload 不截断：Issue #996 复审指出先 SUBSTR 再 json.loads 会在
            # >32KB 时解析失败，导致 files/guard_impact 静默丢失；投影后仅保留
            # files/guard_impact 两个视图，内存占用随行释放。
            c.execute(
                f"SELECT seq, SUBSTR(structured_output, 1, {cap + 1}), "
                f"SUBSTR(agent_logs, 1, {cap + 1}), payload "
                f"FROM events WHERE seq IN ({placeholders})",
                seqs)
            rows = {int(r[0]): (r[1], r[2], r[3] if len(r) > 3 else None)
                    for r in c.fetchall()}
    except Exception:
        return
    picked = set()
    for p in picks:
        picked.add(id(p))
        raw_so, raw_logs, raw_payload = rows.get(int(p["seq"]), (None, None, None))
        if raw_payload:
            from src.api.event_payload_view import payload_view_from_json
            p.update(payload_view_from_json(raw_payload, seq=p["seq"]))
        if raw_so:
            if len(raw_so) > cap:
                p["output_truncated"] = True
            else:
                try:
                    p["output"] = _json.loads(raw_so)
                except Exception:
                    p["output"] = None
        if raw_logs:
            if len(raw_logs) > cap:
                p["logs_truncated"] = True
            else:
                try:
                    p["logs"] = _json.loads(raw_logs)
                except Exception:
                    p["logs"] = []
    for evs in group_map.values():
        for e in evs:
            if id(e) not in picked:
                e["payload_omitted"] = True

def _feishu_api_for_ctx(ctx):
    """按 ctx 的 app_id 取 FeishuAsync（旧 IM 下线 A2 M2：不再依赖 BotManager.api）。"""
    if not ctx:
        return None
    from src.domains.runtime.im.engine_wiring import feishu_api
    return feishu_api(getattr(ctx, "app_id", "") or "")

def _cancel_issue_agents(c, repo_key: str, issue_id: str) -> int:
    """Issue #1741：终止某需求卡片下仍在跑的 agent_tasks，返回终止数量。

    取消端点主路径与「主卡已取消」幂等收口分支共用，避免历史重复行残留 running agent 继续执行。
    """
    if not issue_id:
        return 0
    c.execute(
        "SELECT id, event_seq FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running')",
        (repo_key, issue_id))
    _agent_rows = c.fetchall()
    for _at_id, _at_seq in _agent_rows:
        try:
            from src.domains.runtime.engine.service import cancel_task as _cancel_agent
            _cancel_agent(_at_id)
        except Exception:
            pass
        try:
            if _at_seq:
                from src.domains.runtime.pipeline.event_pipeline import cancel_direct_task
                cancel_direct_task(_at_seq)
        except Exception:
            pass
        try:
            if _at_seq:
                from src.api.event_store import update_event_status
                update_event_status(_at_seq, "cancelled")
        except Exception:
            pass
    c.execute(
        "UPDATE agent_tasks SET status='cancelled' WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running')",
        (repo_key, issue_id))
    return len(_agent_rows)

def _scene_agent_allowlist(repo_key: str, scene_id: str) -> set:
    """Issue #655: 场景动态 agent 允许集 —— issue 绑定场景的 nodes[].id 集合。

    兜底（返回空集，仅原白名单生效）：scene_id 为空 / default（视为未绑定）、
    场景解析失败（异常 / get_pipeline_config 未命中且 fallback）。
    """
    sid = (scene_id or "").strip()
    if not sid or sid == "default":
        return set()
    try:
        from src.api.pipeline_config import get_pipeline_config
        cfg = get_pipeline_config(sid, repo_key=repo_key or "")
        if not cfg or cfg.get("fallback"):
            return set()
        return {str((n or {}).get("id") or "").strip()
                for n in cfg.get("nodes") or [] if (n or {}).get("id")}
    except Exception as e:
        log.warning(f"[issue-655] scene agent allowlist resolve failed scene={sid}: {e}")
        return set()

def _vcs_issue_lookup(owner: str, repo: str, number: "int | str", user: dict) -> dict | None:
    """Issue #672：按 GitCode number + owner/repo 走 git-repo 通道核对远端 issue 是否存在。

    双路径兜底之一（内部需求库 issue_id 解析失败时使用）：provider 双试 gitcode→github，
    token 优先仓库 bot token（devops 场景），会话用户 token 交叉回退。
    任何失败都视为未命中（兜底不放大错误），返回 None 或 {"provider", "issue"}。
    """
    from src.api.git_repo_api import _PROVIDER_MODULES, _vcs_req, _vcs_url
    from src.kernel.repo_key import to_repo_key
    number = str(number)  # int（内部主键）/ str（D105 字符串 ident）归一
    for _p in list(_PROVIDER_MODULES):
        try:
            _mod = _PROVIDER_MODULES.get(_p)
            if _mod is None:
                continue
            _rk = to_repo_key(f"{owner}/{repo}", provider=_p)
            _tok = ""
            try:
                _tok = _mod.bot_token(_rk) or ""
            except Exception:
                _tok = ""
            if not _tok and user.get("auth_type") == "session_token":
                try:
                    _tok = _mod.user_token(user.get("username") or "", user.get("id") or 0) or ""
                except Exception:
                    _tok = ""
            if not _tok:
                continue
            resp = _vcs_req(_p, "GET", _vcs_url(owner, repo, f"/issues/{number}", None, _p), _tok)
            if isinstance(resp, dict) and (resp.get("number") is not None or resp.get("id")):
                return {"provider": _p, "issue": resp}
        except HTTPException as e:
            if getattr(e, "status_code", 0) == 404:
                continue
            log.warning(f"Issue #672 VCS fallback: {_p} {owner}/{repo}#{number} lookup failed: {getattr(e, 'detail', '')}")
        except Exception as e:
            log.warning(f"Issue #672 VCS fallback: {_p} {owner}/{repo}#{number} error: {e}")
    return None

# Issue #859 性能（2026-11-03）：事件 payload 内联额度
# 每 issue 只内联最新 N 条的 output/logs；单次请求最多 M 条；单条超过 CHARS 就截断。
_EVENT_PAYLOAD_PER_ISSUE = 1

_EVENT_PAYLOAD_MAX_EVENTS = 200

_EVENT_PAYLOAD_MAX_CHARS = 32768
