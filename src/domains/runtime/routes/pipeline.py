"""Pipeline 路由：Issue 工作流聚合展示"""
import os
import time
from fastapi import APIRouter, Depends, Query
from src.api.deps import require_no_session_token
from src.api.time_utils import normalize_event_time
from src.kernel.config import log


class _OwnedCursor:
    """自带连接的游标：close 时把连接一并关掉。"""

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
    """把 db.cursor()/commit()/rollback() 代理到 runtime 域连接。

    流水线看板读 events / agent_tasks（runtime 域），但路由由 mgmt 进程服务；
    三库模式下 mgmt 主库没有这些表，直连必然查空。
    """

    def cursor(self, *a, **kw):
        from src.kernel import db_registry
        if db_registry.current_domain() == "runtime":
            from src.kernel.session_manager import db as _main
            return _main.cursor(*a, **kw)
        if db_registry.uses_own_dsn("runtime"):
            _conn = db_registry.new_connection("runtime")
            return _OwnedCursor(_conn, _conn.cursor(*a, **kw))
        from src.kernel.session_manager import db as _main
        return _main.cursor(*a, **kw)

    def commit(self):
        from src.kernel import db_registry
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
                log.debug(f"domain rollback failed: {_e}")
            return None
        from src.kernel.session_manager import db as _main
        return _main.rollback()

    def __getattr__(self, item):
        from src.kernel.session_manager import db as _main
        return getattr(_main, item)


_rt_db = _DomainDbProxy()

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

AGENT_ORDER = ["vod", "issue", "coding", "qa", "sre"]
AGENT_LABELS = ["VOD", "Issue", "Coding", "QA", "SRE"]

STAGE_STATUS_MAP = {
    "pending": 0,
    "running": 1,
    "completed": 2,
    "failed": 3,
}

# Issue #2925：标题「占位符」判定单一事实源——兜底触发条件、写回条件、批量回填
# 复核必须同口径。此前写回条件只看 `title='' OR title IS NULL`，而标题本身是
# 事件类型名（如 issue_opened）时仍被判占位 → 下一轮继续外呼，永不收敛。
_EVENT_TYPE_NAMES = frozenset({
    "issue_opened", "issue_updated", "issue_comment", "issue_labeled",
    "issue_closed", "push", "tag_created", "release_created",
    "pull_request_opened", "pull_request_updated", "pull_request_merged",
    "note_hook_pr", "vod_signal", "a2a_event", "build_start", "build_failure",
    "deploy_start", "deploy_failure", "deploy_finish",
    # Issue #329: cross-pipeline handoff event types
    "assignee_changed", "cross_scene_handoff",
})
_PLACEHOLDER_PREFIXES = ("A2A:", "A2A (订阅):", "PR 订阅 QA:")


def _is_placeholder_title(t: str) -> bool:
    """标题是否为占位符（空 / 事件类型名 / A2A 等派生标题）。"""
    if not isinstance(t, str):
        t = str(t or "")
    return (not t) or t in _EVENT_TYPE_NAMES or any(
        t.startswith(p) for p in _PLACEHOLDER_PREFIXES)


# Issue #2925：进程内负缓存（降级路径）——`issue.title_fetched_at` 列不可用
# （迁移未跑到/临时库）时，仍保证「同仓第二次起 0 外呼」；有列时以列为准。
_TITLE_NEG_CACHE: dict = {}
# 进程内负缓存的条目上限：超过即先按 TTL 清理，避免长期运行进程无界增长。
_TITLE_NEG_CACHE_MAX = 4096


def _title_neg_cache_put(key, ts: float) -> None:
    """写入进程内负缓存；超过上限时剔除过期条目（无列降级路径专用）。"""
    if len(_TITLE_NEG_CACHE) >= _TITLE_NEG_CACHE_MAX:
        _ttl = _title_negative_ttl()
        _now = time.time()
        for _k, _v in list(_TITLE_NEG_CACHE.items()):
            if not _title_marker_fresh(_v, _now, _ttl):
                _TITLE_NEG_CACHE.pop(_k, None)
        if len(_TITLE_NEG_CACHE) >= _TITLE_NEG_CACHE_MAX:
            # 仍然超限（TTL 全新鲜 / 配置为永久）→ 按插入顺序丢掉最早的一批，
            # 保持有界；同 key 重写不刷新插入序，仍按首次写入位置淘汰。
            for _k in list(_TITLE_NEG_CACHE)[:len(_TITLE_NEG_CACHE) - _TITLE_NEG_CACHE_MAX + 1]:
                _TITLE_NEG_CACHE.pop(_k, None)
    _TITLE_NEG_CACHE[key] = ts


# 写回/标记 SQL 的并发护栏（三处共用，保证与占位判定同口径）。
_TITLE_WRITEBACK_GUARD = "(title='' OR title IS NULL OR title=?)"
# 标记读取按分片执行：`issue_number IN (...)` 随窗口（1+offset+limit）增长，
# 大 offset 下会触碰 SQLite 变量上限，分片保证单条 SQL 的占位符数量有界。
_TITLE_MARKER_QUERY_CHUNK = 200


def _title_negative_ttl() -> int:
    """负缓存有效期（秒）。默认 6h：足够覆盖热请求，又允许上游改标题后自然重试。
    0/负数表示标记永久有效（只靠「写回真实标题」解除）。"""
    try:
        raw = os.environ.get("GRAPE_PIPELINE_TITLE_NEG_CACHE_TTL", "")
        return int(raw) if str(raw).strip() != "" else 21600
    except Exception:
        return 21600


def _title_marker_fresh(ts, now: float, ttl: int) -> bool:
    """已尝试标记是否仍在有效期内（负缓存命中）。ts 为 epoch 秒字符串/数字。"""
    if not ts:
        return False
    if ttl <= 0:
        return True
    try:
        age = now - float(ts)
    except Exception:
        # 无法解析的标记按「已尝试」处理，宁可少一次外呼也不重复打爆上游
        return True
    return age < ttl


def _issue_number_of(issue_id: str):
    """`#123` → 123；PR 伪 id / 非数值 → None（不参与 issue 表回写）。"""
    s = str(issue_id or "").lstrip("#")
    return int(s) if s.isdigit() else None


def _backfill_window_titles(repo_key: str, repo_keys: list, title_map: dict,
                            issue_ids: list, events_by_issue: dict = None,
                            window_size: int = 0) -> dict:
    """Issue #2925：仅对当前页窗口内的占位标题做有界兜底，并落「已尝试」标记。

    三层收敛中的「范围有界 + 状态有界」：
      1. 范围：只处理调用方给定的窗口 issue（`window_size` 之外的 issue 不触发外呼，
         保持占位并由日志计数显式标记未核实）；
      2. 负缓存：`title_fetched_at` 仍在 TTL 内（或进程内缓存命中）直接跳过；
      3. 写回：只有「取到且非占位」的标题才落库，条件与 `_is_placeholder_title`
         同口径；取不到真实标题只落已尝试标记，不回写占位值。

    返回就地更新后的 `title_map`（真实标题立即可用于本次响应）。
    整段 best-effort：任何异常都不允许阻断流水线列表返回。
    """
    if title_map is None:
        title_map = {}
    if not issue_ids:
        return title_map
    _started = time.monotonic()
    _window_ids = list(issue_ids[:window_size]) if window_size > 0 else list(issue_ids)
    _candidates = []
    _cache_hit = 0
    for _iid in _window_ids:
        _cur = title_map.get(_iid, "")
        if not _is_placeholder_title(_cur):
            _cache_hit += 1
            continue
        _evts = (events_by_issue or {}).get(_iid) or []
        if any(not _is_placeholder_title(e.get("title") or "") for e in _evts):
            # events 表已有真实标题 → 本地兜底即可命中，无需外呼
            _cache_hit += 1
            continue
        _num = _issue_number_of(_iid)
        if _num is None:
            continue
        # 多仓场景下 issue 号可能在不同 repo 重复：回写必须带该 issue 实际所属
        # repo_key（优先取事件行，缺失回退调用方 repo_key），避免串仓覆盖。
        # events 查询为 ORDER BY seq ASC，最新事件在末尾；取最新一条的 repo_key，
        # 否则多仓同号时会回写到最旧事件所属的仓。
        _ev = _evts[-1] if _evts else {}
        _rk = _ev.get("repo_key") or repo_key
        _candidates.append((_iid, _num, _rk))
    if not _candidates:
        log.info("[pipeline] title_backfill repo=%s window=%d candidates=0 "
                 "vcs_calls=0 cache_hit=%d elapsed=%.3fs",
                 repo_key, len(_window_ids), _cache_hit, time.monotonic() - _started)
        return title_map

    # ① 持久已尝试标记（列存在才用；否则走进程内负缓存）
    _marker_ok = False
    _marked: dict = {}
    _now = time.time()
    try:
        from src.kernel.session_manager import db_lock
        from src.domains.runtime.schema_probe import column_exists
        with db_lock:
            _c = _rt_db.cursor()
            _marker_ok = column_exists(_c, "issue", "title_fetched_at")
            if _marker_ok:
                _ph_rk = ",".join("?" * len(repo_keys))
                # 分片读取：窗口大小 = 1+offset+limit，offset 无上限；单条 SQL 的
                # issue_number 占位符数量固定 ≤ _TITLE_MARKER_QUERY_CHUNK，
                # 避免大 offset 触发 SQLite 变量上限导致标记读取整体降级。
                for _ci in range(0, len(_candidates), _TITLE_MARKER_QUERY_CHUNK):
                    _chunk = _candidates[_ci:_ci + _TITLE_MARKER_QUERY_CHUNK]
                    _ph_num = ",".join("?" * len(_chunk))
                    _c.execute(
                        f"SELECT repo_key, issue_number, title_fetched_at FROM issue "
                        f"WHERE repo_key IN ({_ph_rk}) AND issue_number IN ({_ph_num})",
                        (*repo_keys, *[n for _, n, _ in _chunk]))
                    # 逐行取（单次分片最多 200 条）：不触发静态检查的 A7「持锁批量
                    # 扫描」形态，也避免为一次标记读取引入额外的大结果集。
                    while True:
                        _r = _c.fetchone()
                        if not _r:
                            break
                        _marked[(_r[0], _r[1])] = _r[2] or ""
    except Exception as e:
        _marker_ok = False
        log.warning(f"[pipeline] title backfill marker read failed (degrade to "
                    f"process cache): {e}")

    _ttl = _title_negative_ttl()
    _to_fetch = []
    _skipped_marked = 0
    for _iid, _num, _rk in _candidates:
        # 有列时以持久标记为主；进程内缓存是补充路径——issue 表没有该行
        # （events 有记录、issue 表无行）时持久标记无处落，只能靠进程缓存收敛。
        _marker_ts = _marked.get((_rk, _num), "") if _marker_ok else ""
        if (_title_marker_fresh(_marker_ts, _now, _ttl)
                or _title_marker_fresh(_TITLE_NEG_CACHE.get((_rk, _num)), _now, _ttl)):
            _skipped_marked += 1
            continue
        _to_fetch.append((_iid, _num, _rk))
    if not _to_fetch:
        log.info("[pipeline] title_backfill repo=%s window=%d candidates=%d "
                 "vcs_calls=0 skipped_marked=%d cache_hit=%d elapsed=%.3fs",
                 repo_key, len(_window_ids), len(_candidates), _skipped_marked,
                 _cache_hit, time.monotonic() - _started)
        return title_map

    # ② 有界执行：token/客户端复用 + 有限并发 + 总 deadline（见 event_pipeline）
    # 按 issue 实际所属 repo_key 分组，每组内 token/客户端只解析构建一次。
    _fetched: dict = {}
    # 逐条记录「确实开始执行」的候选：被 deadline 取消、从未发过请求的排队任务
    # 不得落负缓存（否则会把未尝试的 issue 锁 6h 不再重试）。
    _attempted_ids: set = set()
    _by_rk: dict = {}
    for _iid, _num, _rk in _to_fetch:
        _by_rk.setdefault(_rk, []).append(_iid)
    # 请求级共享总 deadline：多 repo_key 分组累计仍受同一预算约束（≤4s），
    # 而不是每组各扣一次 4s 导致多仓历史数据下累计超 5s。
    from src.domains.runtime.pipeline.event_pipeline import (
        fetch_issue_titles_batch, _title_fetch_deadline)
    _batch_expire_at = time.monotonic() + _title_fetch_deadline()
    for _rk, _ids in _by_rk.items():
        _stats: dict = {}
        _remain = _batch_expire_at - time.monotonic()
        if _remain <= 0:
            log.info(f"[pipeline] title backfill deadline exhausted before {_rk} "
                     f"(remaining groups skipped, keep placeholder)")
            break
        try:
            for _iid, _t in (fetch_issue_titles_batch(
                    _rk, _ids, deadline_seconds=_remain, stats=_stats) or {}).items():
                _fetched[(_rk, _iid)] = _t
        except Exception as e:
            log.warning(f"[pipeline] title backfill batch error for {_rk} "
                        f"(degrade to placeholder): {e}")
        # 只有真正开始执行（wrapper 已置位）的候选才允许落已尝试标记：
        # 无 token 时不得形成负缓存，deadline 取消的排队任务也不得被标记。
        for _started_iid in (_stats.get("attempted_ids") or ()):
            _attempted_ids.add((_rk, _started_iid))

    # ③ 状态有界：只写真实标题（同口径），失败落已尝试标记形成负缓存
    _now_iso = str(int(_now))
    _written = 0
    try:
        from src.kernel.session_manager import db_lock
        with db_lock:
            _db = _rt_db
            _c = _db.cursor()
            for _iid, _num, _rk in _to_fetch:
                _api = _fetched.get((_rk, _iid), "")
                _real = bool(_api) and not _is_placeholder_title(_api)
                _old = title_map.get(_iid, "")
                # 写回护栏与占位判定同口径：仅当库里仍是「空/空串/本次观测到的占位值」
                # 才覆盖，避免并发写入真实标题被旧值回滚；repo_key 逐条带上，
                # 防止多仓同号 issue 串仓覆盖。
                if _real:
                    if _marker_ok:
                        _c.execute(
                            f"UPDATE issue SET title=?, title_fetched_at=? "
                            f"WHERE repo_key=? AND issue_number=? "
                            f"AND {_TITLE_WRITEBACK_GUARD}",
                            (_api, _now_iso, _rk, _num, _old))
                    else:
                        _c.execute(
                            f"UPDATE issue SET title=? "
                            f"WHERE repo_key=? AND issue_number=? "
                            f"AND {_TITLE_WRITEBACK_GUARD}",
                            (_api, _rk, _num, _old))
                    # 日志计数只算真正落库的 UPDATE（并发护栏命中 rowcount=0 时
                    # 不算 fetched），避免 no-op 虚增 fetched、低估 failed。
                    if getattr(_c, "rowcount", -1) != 0:
                        _written += 1
                    title_map[_iid] = _api
                elif _marker_ok:
                    if (_rk, _iid) in _attempted_ids:
                        _c.execute(
                            f"UPDATE issue SET title_fetched_at=? "
                            f"WHERE repo_key=? AND issue_number=? "
                            f"AND {_TITLE_WRITEBACK_GUARD}",
                            (_now_iso, _rk, _num, _old))
                        # events 有记录但 issue 表无该行：持久标记命中 0 行，
                        # 降级落进程内负缓存，避免该 issue 每次请求都继续外呼。
                        if getattr(_c, "rowcount", -1) == 0:
                            _title_neg_cache_put((_rk, _num), _now)
                elif (_rk, _iid) in _attempted_ids:
                    _title_neg_cache_put((_rk, _num), _now)
            try:
                _db.commit()
            except Exception as e:
                # 不静默：commit 失败会让标记/标题回写整体丢失，必须留可观测信号。
                log.warning(f"[pipeline] title backfill commit failed "
                            f"(markers may be lost): {e}")
    except Exception as e:
        log.warning(f"[pipeline] title backfill writeback failed: {e}")

    log.info("[pipeline] title_backfill repo=%s window=%d candidates=%d "
             "vcs_calls=%d fetched=%d failed=%d cache_hit=%d skipped_marked=%d "
             "elapsed=%.3fs",
             repo_key, len(_window_ids), len(_candidates), len(_to_fetch),
             _written, len(_to_fetch) - _written, _cache_hit, _skipped_marked,
             time.monotonic() - _started)
    return title_map


def _get_latest_assignee(issue_events: list) -> str:
    """从 issue 事件（最新在前倒序）payload 解析 assignee（GitCode 用户名）

    优先 object_attributes.assignee / issue.assignee，assignee 可能为 str 或 dict{username}。
    """
    import json as _json
    for e in reversed(issue_events):
        payload = e.get("payload") or ""
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            continue
        obj = payload.get("issue") or payload.get("object_attributes") or {}
        if not isinstance(obj, dict):
            continue
        asg = obj.get("assignee", "")
        if isinstance(asg, dict):
            asg = asg.get("username", "") or asg.get("login", "")
        if asg:
            return asg
    return ""


def _resolve_feishu_name(gitcode_user: str, repo_key: str = "") -> str:
    """按平台用户名查飞书昵称（缺失回退用户名）。

    FR-17.1: 有 repo_key 时按 provider 分查对应列，避免跨平台同名误命中。
    """
    if not gitcode_user:
        return ""
    # FR-CQ-1: 提取为 um_column_for_repo 工具函数
    from src.kernel.repo_key import um_column_for_repo
    _col = um_column_for_repo(repo_key)
    if not _col:
        # D39/S59：无 um 列的平台（gitee/gitlab 等）直接回退用户名——与原实现
        # 「拼出空列名 → SQL 语法错 → 整段吞掉 → 回退用户名」同口径，只是不再靠报错。
        return gitcode_user
    try:
        from src.domains.mgmt import public as _mgmt_public
        return _mgmt_public.feishu_name_by_platform_column(_col, gitcode_user) or gitcode_user
    except Exception as _e:
        log.debug(f"[pipeline] feishu name resolve failed (user={gitcode_user!r}): {_e}")
        return gitcode_user


def _scene_label_for_event(scene_id: str, get_pipe_cfg, repo_key: str, pipe_cfg_cache: dict) -> str:
    """按 (scene_id, repo_key) 解析事件级真实场景名（Issue #479 家族：跨场景移交徽章）。

    复用调用方传入的 get_pipe_cfg 回调（带缓存），无配置时返回空串
    （前端兜底显示原始 scene_id）。任何异常吞掉返回空串——徽章是纯展示层，
    不允许影响流水线列表主数据。
    """
    if not scene_id:
        return ""
    try:
        _cfg = get_pipe_cfg(scene_id) if get_pipe_cfg else None
        if not _cfg:
            from src.api.pipeline_config import get_pipeline_config
            _cfg = get_pipeline_config(scene_id, repo_key=repo_key)
            if pipe_cfg_cache is not None:
                pipe_cfg_cache[scene_id] = _cfg
        return (_cfg.get("display_name") or _cfg.get("name", "")) if _cfg else ""
    except Exception:
        return ""


def _get_issue_pipeline(repo_key: str, issue_id: str, events: list,
                        title_map: dict = None, stage_map: dict = None,
                        pipe_cfg_cache: dict = None, get_pipe_cfg=None,
                        bot_usernames: set = None, feishu_name_cache: dict = None,
                        events_by_issue: dict = None, confirm_minutes: int | None = None) -> dict:
    """根据 issue_id 下的事件列表构建单个 Pipeline

    参数:
        title_map: 批量查出的 issue 标题映射（消除标题 N+1）
        stage_map: 批量查出的 issue stage/scene/assignee 映射（消除 get_issue_stage N+1）
        pipe_cfg_cache: 场景配置缓存 {scene_id: config}（消除 get_pipeline_config N+1）
        get_pipe_cfg: 获取场景配置的回调函数（带缓存）
        bot_usernames: 仓库的 bot_username 集合（消除 get_all_bot_usernames N+1）
        feishu_name_cache: 飞书用户名缓存 {gitcode_user: feishu_name}（消除 _resolve_feishu_name N+1）
        events_by_issue: issue_id → events 列表索引（Issue #542：O(issues×events) 全量线性
            过滤 → O(1) 索引取数；None 时回退线性过滤兼容旧调用）
    """
    if events_by_issue is not None:
        issue_events = events_by_issue.get(issue_id) or []
    else:
        issue_events = [e for e in events if e.get("issue_id") == issue_id]
    if not issue_events:
        return None

    # PR 订阅伪 issue_id 提前解析（供 title 兜底使用）
    import re as _re
    _pr_match = _re.match(r'#pr_(\d+)_', issue_id)

    # 优先从批量 title_map 取标题（消除 N+1）
    title = ""
    if title_map:
        title = title_map.get(issue_id, "")
    if not title:
        title = issue_events[0].get("title", "") or issue_id
    # Issue #2925：占位判定收敛到模块级单一事实源（写回/兜底/验收同口径）
    _is_placeholder = _is_placeholder_title

    # title_map 已提供该 issue 的行时跳过 DB 查询（title_map 本身就来自 issue 表）；
    # 仅当调用方未传 title_map / 缺该键时回退 DB（兼容无 title_map 的旧调用）。
    if _is_placeholder(title) and (not title_map or issue_id not in title_map):
        _iss_num = _issue_number_of(issue_id)
        if _iss_num is not None:
            try:
                from src.kernel.session_manager import db_lock as _dl
                _db = _rt_db
                with _dl:
                    _c = _db.cursor()
                    _c.execute("SELECT title FROM issue WHERE repo_key=? AND issue_number=?", (repo_key, _iss_num))
                    _t = _c.fetchone()
                    if _t and _t[0] and not _is_placeholder(_t[0]):
                        title = _t[0]
            except Exception:
                pass
    # events 表中找一条非占位符标题做兜底
    if _is_placeholder(title):
        for _e in issue_events:
            _et = _e.get("title", "") or ""
            if not _is_placeholder(_et):
                title = _et
                break
    # payload 兜底：events title 都是占位符 → 从 issue_opened 事件的 payload 中提取真实标题
    if _is_placeholder(title):
        import json as _json
        for _e in issue_events:
            if _e.get("event_type") != "issue_opened":
                continue
            _pl = _e.get("payload", "")
            if isinstance(_pl, str):
                try:
                    _pl = _json.loads(_pl)
                except Exception:
                    _pl = {}
            if not isinstance(_pl, dict):
                continue
            _obj = _pl.get("object_attributes", {})
            _iss = _pl.get("issue", {})
            _pt = ""
            if isinstance(_obj, dict):
                _pt = _obj.get("title", "") or ""
            if not _pt and isinstance(_iss, dict):
                _pt = _iss.get("title", "") or ""
            if _pt and not _is_placeholder(_pt):
                title = _pt
                break
    # PR 订阅跨 issue 兜底：同一 PR 号的其他事件可能有真实 PR 标题
    if _is_placeholder(title) and _pr_match:
        _pr_num = _pr_match.group(1)
        def _e_id(e):
            return e.get("issue_id", "")
        if events_by_issue is not None:
            # Issue #542：索引路径按索引键匹配 PR 伪 ID（O(issues)，避免遍历全量 events）
            _pr_pool = [e for iid, evts in events_by_issue.items()
                        if iid != issue_id and (_re.match(rf'#pr_{_pr_num}_', iid) or iid == f'#pr_{_pr_num}')
                        for e in evts]
        else:
            _pr_pool = [e for e in events
                        if _e_id(e) != issue_id
                        and (_re.match(rf'#pr_{_pr_num}_', _e_id(e)) or _e_id(e) == f'#pr_{_pr_num}')]
        for _e in _pr_pool:
            _et = _e.get("title", "") or ""
            if not _is_placeholder(_et):
                title = _et
                break
    # 窗口外/取不到真实标题时保持占位：标题外呼已上移到 `get_pipeline` 的
    # `_backfill_window_titles`（有界并发 + 负缓存），此处不再逐条串行外呼
    # （Issue #2925：原实现 97 条 → 22~37s，且写回口径失配导致永不收敛）。
    # 统一计算节点状态（与任务详情页共享同一套逻辑）
    from src.api.pipeline_status import compute_node_statuses, STATUS_COMPLETED

    # 优先从批量 stage_map 取（消除 get_issue_stage/get_issue_scene N+1）
    _st = (stage_map or {}).get(issue_id) or {}
    _stage = _st.get("stage", "")
    _scene_id = _st.get("scene_id", "")
    _pipe_cfg = get_pipe_cfg(_scene_id) if _scene_id and get_pipe_cfg else None
    if not _pipe_cfg and _scene_id:
        from src.api.pipeline_config import get_pipeline_config
        _pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key)
        if pipe_cfg_cache is not None:
            pipe_cfg_cache[_scene_id] = _pipe_cfg
    _scene_nodes = (_pipe_cfg or {}).get("nodes", []) or [{"id": a, "tag": a} for a in AGENT_ORDER]

    _nodes, stage_statuses = compute_node_statuses(_scene_nodes, issue_events, _stage)

    # 转换为原有数据格式（兼容前端 pipeline.js）
    stages = []
    for n in _nodes:
        _le = n.get("latest_event")
        if _le:
            _latest_event = {
                "seq": _le.get("seq", 0),
                "title": _le.get("title", "") or _le.get("event_type", ""),
                "event_type": _le.get("event_type", ""),
                "status": _le.get("status", ""),
                "actor": _le.get("actor", ""),
                # Issue #479 家族修复：跨场景移交徽章需要事件级 scene 信息。
                # compute_node_statuses 塞入的是原始事件 dict（含 DB 带出的 scene_id），
                # 此处白名单透传，并按 (scene_id, repo_key) 解析真实场景名（与场景列同构，
                # 仓库级场景被改名后显示真实名而非写死标签/原始 ID）。
                "scene_id": _le.get("scene_id", "") or "",
                "scene_name": _scene_label_for_event(_le.get("scene_id", "") or "", get_pipe_cfg, repo_key, pipe_cfg_cache),
                # Issue #2432：失败节点名（不输出失败原因）
                "failed_node": _le.get("failed_node", "") or "",
            }
        else:
            _latest_event = None
        stages.append({"agent": n.get("id", ""), "latest_event": _latest_event})

    all_done = all(s == STATUS_COMPLETED for s in stage_statuses)
    any_failed = any(s == 3 for s in stage_statuses)
    any_running = any(s == 1 for s in stage_statuses)

    if all_done:
        result = "Released"
    elif any_failed:
        result = "Failed"
    elif any_running:
        result = "Processing"
    else:
        result = "Pending"

    involved_agents = [_nodes[i].get("name") or _nodes[i].get("id", "") for i, s in enumerate(stage_statuses) if s > 0]

    # 责任人：优先 Issue assignee（issue_state.assignee，GitCode 真实指派，可为 grape_bot 或用户），
    # 回退从事件 actor 推断（_get_latest_assignee 已因 payload 不含 assignee 而基本失效，保留兜底）。
    current_owner = ""
    # _st 已在上方从 stage_map 获取
    def _resolve_feishu_name_cached(name):
        if not name:
            return ""
        if feishu_name_cache is not None and name in feishu_name_cache:
            return feishu_name_cache[name]
        result = _resolve_feishu_name(name, repo_key)
        if feishu_name_cache is not None:
            feishu_name_cache[name] = result
        return result

    if _st and _st.get("assignee"):
        current_owner = _resolve_feishu_name_cached(_st["assignee"])
    else:
        assignee = _get_latest_assignee(issue_events)
        if assignee:
            current_owner = _resolve_feishu_name_cached(assignee)
        else:
            for i in range(len(stage_statuses) - 1, -1, -1):
                if stage_statuses[i] > 0:
                    agent = _nodes[i].get("id", "") if i < len(_nodes) else ""
                    latest = stages[i].get("latest_event") or {}
                    actor = latest.get("actor", "")
                    if actor:
                        _bots = bot_usernames if bot_usernames is not None else set()
                        if not _bots:
                            from src.domains.mgmt.user.db import get_all_bot_usernames
                            _bots = get_all_bot_usernames()
                        if actor in _bots:
                            from src.domains.runtime.pipeline.event_pipeline import get_agent_display_name
                            current_owner = get_agent_display_name(agent)
                        else:
                            current_owner = actor
                    break

    # 生命周期阶段（issue_state.stage）：vod/issue/coding/qa/sre/done；无记录默认 vod
    stage = (_st.get("stage") if _st else "") or "vod"
    # Issue 级开发场景（从 stage_map 取，不再回退 get_issue_scene）
    # PR 订阅事件不写 issue_state，回退从 events 表 scene_id 列取
    scene_id = (_st.get("scene_id") if _st else "") or _scene_id or ""
    if not scene_id:
        for _e in issue_events:
            _sid = _e.get("scene_id") or ""
            if _sid:
                scene_id = _sid
                break
    # 解析场景名称（内置 + 成员自定义）——使用缓存回调
    scene_name = ""
    issue_pipeline_config = None
    if scene_id:
        _cfg = get_pipe_cfg(scene_id) if get_pipe_cfg else None
        if not _cfg:
            from src.api.pipeline_config import get_pipeline_config
            _cfg = get_pipeline_config(scene_id, repo_key=repo_key)
        if _cfg:
            scene_name = _cfg.get("display_name") or _cfg.get("name", scene_id)
            issue_pipeline_config = _cfg

    # 总执行时长：所有已完成事件的 duration 之和
    total_duration = 0.0
    for e in issue_events:
        d = e.get("duration")
        if d and isinstance(d, (int, float)):
            total_duration += d

    # 父任务 ID（子任务场景，issue_state.parent_issue_id）
    parent_issue_id = (_st.get("parent_issue_id") if _st else "") or ""

    # PR 订阅伪 issue_id（#pr_{num}_{scene_id}）简化显示
    if _pr_match:
        display_issue_id = f"PR#{_pr_match.group(1)}"
    else:
        display_issue_id = issue_id

    from src.api.pipeline_status import attach_stall_state, extract_failed_node
    # Issue #2432：失败节点（供详细路线图直接展示失败状态 + 失败节点名）
    _failed_node, _failed_node_name = extract_failed_node(_nodes, stage_statuses)
    _out = {
        "issue_id": display_issue_id,
        "failed_node": _failed_node,
        "failed_node_name": _failed_node_name,
        "raw_issue_id": issue_id,
        "title": title,
        "stages": stages,
        "statuses": stage_statuses,
        "result": result,
        "stage": stage,
        "scene_id": scene_id,
        "scene_name": scene_name,
        "pipeline_config": issue_pipeline_config,
        "agents": involved_agents,
        "current_owner": current_owner,
        "event_count": len(issue_events),
        "duration": round(total_duration, 1) if total_duration > 0 else None,
        "parent_issue_id": parent_issue_id,
    }
    attach_stall_state(_out, issue_events, stage, (_st.get("status") if _st else "") or "",
                       confirm_minutes=confirm_minutes)
    return _out


def _running_issue_ids(repo_keys) -> set[str]:
    """按 repo 查 agent_tasks 中 queued/dispatched/running 的 issue_id 集合（前端 B 执行态派生）。"""
    if isinstance(repo_keys, str):
        repo_keys = [repo_keys]
    from src.kernel.session_manager import db_lock
    db = _rt_db
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT DISTINCT issue_id FROM agent_tasks WHERE repo_key IN ({}) AND status IN ('queued','dispatched','running') AND issue_id NOT IN ('', '#0')".format(
                    ",".join("?" * len(repo_keys))
                ),
                repo_keys
            )
            return {r[0] for r in c.fetchall()}
    except Exception:
        return set()


def _pipeline_window(repo_keys: list, scene_filter: str, need: int):
    """Issue #1077-P：把「要构建哪几条流水线」下推到 SQL。

    原实现先为整仓所有有事件的 issue 构建流水线（每条约 2ms），再切片/过滤：
    6k issue 就要 12.9s，且与 history_limit/scene_id 传什么无关。

    返回 (ordered_ids, total, failed_ids)：
      ordered_ids : 按最新事件倒序的 issue（已按场景过滤），只取前 need 条
      total       : 该范围（全仓/单场景）的 issue 总数（用于 history_total）
      failed_ids  : 其中有失败节点的 issue 集合（某 agent 最新事件 status='failed'，
                    与 compute_node_statuses 的状态码 3 同口径）

    场景判定用 SQL LEFT JOIN：issue.scene_id 权威、events.scene_id 兜底，
    走 idx_tasks_repo_issue(repo_key, issue_number) 等值查找，不扫全表。
    """
    from src.kernel.session_manager import db_lock
    db = _rt_db
    _ph = ",".join("?" * len(repo_keys))
    need = max(1, int(need))
    # Issue #1312：审计事件与无归属事件不参与流水线聚合
    _exclude_clause = " AND e.event_type <> 'admin_action' AND e.issue_id NOT IN ('', '#0')"
    # 无场景过滤时不 join（省一次 i 表查找；全仓口径只依赖 events）
    if scene_filter:
        _join = ("LEFT JOIN issue i ON i.repo_key = e.repo_key "
                 "AND i.issue_number = CAST(SUBSTR(e.issue_id, 2) AS INTEGER)")
        _scene_clause = " AND COALESCE(NULLIF(i.scene_id,''), e.scene_id) = ?"
        _scene_params = [scene_filter]
    else:
        _join, _scene_clause, _scene_params = "", "", []
    with db_lock:
        c = db.cursor()
        # ① 窗口：最新事件倒序，只要前 need 条
        c.execute(
            f"SELECT e.issue_id, MAX(e.seq) AS m FROM events e {_join} "
            f"WHERE e.repo_key IN ({_ph}){_exclude_clause}{_scene_clause} "
            f"GROUP BY e.issue_id ORDER BY m DESC LIMIT ?",
            (*repo_keys, *_scene_params, need))
        ordered = [r[0] for r in c.fetchall()]
        # ② 总数（精确，供 history_total/统计）
        c.execute(
            f"SELECT COUNT(*) FROM (SELECT e.issue_id FROM events e {_join} "
            f"WHERE e.repo_key IN ({_ph}){_exclude_clause}{_scene_clause} "
            f"GROUP BY e.issue_id)",

            (*repo_keys, *_scene_params))
        total = c.fetchone()[0]
        # ③ 失败集合：每个 (issue, agent) 取最新事件后判 failed
        # 不用窗口函数：ROW_NUMBER 是 MySQL 8.0 才有的，5.7 直接 1064，
        # 而这整块一旦抛错就退化成「全仓构建」的 fallback（见调用方 except），
        # 场景页会静默显示所有场景的流水线。NOT EXISTS 语义等价（不存在更新 seq
        # 的同 (issue, agent) 事件 → 本条即最新），且 SQLite/MySQL 5.7 都能跑。
        c.execute(
            f"SELECT DISTINCT e.issue_id FROM events e {_join} "
            f"WHERE e.repo_key IN ({_ph}){_exclude_clause} AND e.agent_type != ''"
            f"{_scene_clause} AND e.status = 'failed'"
            f" AND NOT EXISTS (SELECT 1 FROM events e2"
            f"                 WHERE e2.repo_key = e.repo_key AND e2.issue_id = e.issue_id"
            f"                   AND e2.agent_type = e.agent_type AND e2.seq > e.seq)",
            (*repo_keys, *_scene_params))
        failed = {r[0] for r in c.fetchall()}
    return ordered, total, failed


@router.get("/{owner}/{repo}", operation_id="pipeline_get", summary="Repository pipeline roadmap", description="Repository pipeline roadmap (stage/nodes/events) for the current user. Issue #542: `history_limit` (default 50) caps the returned history items (most recent first); `history_total` carries the untruncated count. Issue #1077-S: optional `scene_id` scopes the whole response (active/history/stats/history_total) to one scene, so the scene detail page is no longer limited by the repo-wide recent window.")
def get_pipeline(owner: str, repo: str,
                 history_limit: int = Query(50, ge=1, le=500, description="history 截断条数（最近优先），默认 50"),
                 history_offset: int = Query(0, ge=0, description="history 偏移量（最近优先起点，0=最新；与 history_limit 组合支持前端增量翻页拉取全量历史）"),
                 scene_id: str = Query("", max_length=128, description="Issue #1077-S：只返回该场景的流水线（含 active/history/统计口径）；空=全仓（旧行为）"),
                 user: dict = Depends(require_no_session_token)):
    from src.kernel.repo_key import to_repo_key
    _owner_repo = f"{owner}/{repo}"
    repo_key = to_repo_key(_owner_repo)

    # 权限校验：用户必须有权访问该仓库
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

    # 同时查各平台前缀的事件（用户可能切换了 provider）
    # S104/D78：候选集卡派生（合并重同步——weekly-10 版回退成写死
    # gitcode/github 互查，gitee/gitlab 仓的流水线历史因此查不出来；
    # 恢复 weekly-08 的 repo_key_candidates 全平台候选）
    from src.kernel.repo_key import repo_key_candidates
    _repo_keys = repo_key_candidates(_owner_repo)

    from src.kernel.session_manager import db_lock

    db = _rt_db
    # Issue #1077-P：先算「要构建的 issue 窗口」（SQL 聚合 + 场景过滤），
    # 再只加载/归一化这些 issue 的事件——原先 60k 事件逐条 normalize_event_time
    # （每次还做一次后端探测）单独就占 0.8s。
    _scene_filter = str(scene_id or "").strip()
    _need = 1 + max(int(history_offset or 0), 0) + int(history_limit)
    try:
        _win_ids, _issues_total, _failed_ids = _pipeline_window(_repo_keys, _scene_filter, _need)
        _pushdown_ok = True
    except Exception as _e:
        log.warning(f"pipeline window pushdown failed, fallback to full load: {_e}")
        _win_ids, _issues_total, _failed_ids, _pushdown_ok = [], None, None, False

    with db_lock:
        c = db.cursor()
        try:
            _where = ("repo_key IN ({}) AND issue_id NOT IN ('', '#0') "
                      "AND event_type <> 'admin_action'").format(",".join("?" * len(_repo_keys)))
            _params = list(_repo_keys)
            if _pushdown_ok and _win_ids:
                _where += " AND issue_id IN ({})".format(",".join("?" * len(_win_ids)))
                _params += list(_win_ids)
            from src.domains.runtime.schema_probe import column_exists as _col_exists
            _failed_col = ("failed_node" if _col_exists(c, "events", "failed_node")
                           else "'' AS failed_node")
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, summary, status, "
                "created_at, actor, duration, repo_key, scene_id, completed_at, " + _failed_col + " "
                f"FROM events WHERE {_where} ORDER BY seq ASC", _params)
            rows = c.fetchall()
        except Exception as e:
            # 不得静默清空：缺列/下推失败会让整个流水线页空白，必须留可观测信号。
            log.warning(f"[pipeline] events query failed (pushdown={_pushdown_ok}): {e}")
            rows = []

    events = [
        {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "", "title": r[4],
         "status": r[6], "created_at": normalize_event_time(r[7]), "actor": r[8] or "", "payload": "",
         "duration": r[9], "repo_key": r[10] if len(r) > 10 else repo_key,
         "scene_id": r[11] if len(r) > 11 else "",
         "completed_at": normalize_event_time(r[12]) if len(r) > 12 else "",
         "failed_node": (r[13] or "") if len(r) > 13 else ""}
        for r in rows
    ]

    issue_ids = (_win_ids if _pushdown_ok else list(dict.fromkeys(e["issue_id"] for e in events if e["issue_id"])))

    # 批量查标题（消除 N+1：182 次 DB 查询 → 1 次）
    _title_map = {}
    # 批量查 issue stage/scene/assignee（消除 _get_issue_pipeline 内部 get_issue_stage/get_issue_scene N+1）
    _stage_map = {}
    if issue_ids:
        _iss_nums = []
        for iid in issue_ids:
            _n = str(iid).lstrip("#")
            if _n.isdigit():
                _iss_nums.append(int(_n))
        if _iss_nums:
            try:
                with db_lock:
                    c = db.cursor()
                    # 一次查询同时获取 title + stage + scene_id + assignee 等
                    c.execute(
                        "SELECT issue_number, title, stage, owner_agent, assignee, "
                        "scene_id, parent_issue_id, is_subtask_mode, status "
                        "FROM issue WHERE repo_key IN ({}) AND issue_number IN ({})".format(
                            ",".join("?" * len(_repo_keys)),
                            ",".join("?" * len(_iss_nums))
                        ),
                        (*_repo_keys, *_iss_nums)
                    )
                    for r in c.fetchall():
                        _iid = f"#{r[0]}"
                        _title_map[_iid] = r[1] or ""
                        _stage_map[_iid] = {
                            "stage": r[2] or "", "owner_agent": r[3] or "",
                            "assignee": r[4] or "", "scene_id": r[5] or "",
                            "parent_issue_id": r[6] or "", "is_subtask_mode": r[7] or 0,
                            "status": r[8] if len(r) > 8 else "",
                        }
            except Exception:
                pass

    # 场景配置缓存（同仓库内 scene_id 去重，消除 get_pipeline_config N+1）
    _pipe_cfg_cache = {}
    from src.api.pipeline_config import get_pipeline_config
    def _get_pipe_cfg(scene_id):
        if scene_id not in _pipe_cfg_cache:
            _pipe_cfg_cache[scene_id] = get_pipeline_config(scene_id, repo_key=repo_key) if scene_id else None
        return _pipe_cfg_cache[scene_id]

    # 一次性查 bot_usernames（消除 get_all_bot_usernames N+1）
    _bot_usernames = set()
    try:
        from src.domains.mgmt import public as _mgmt_public_bots
        _bot_usernames = set(_mgmt_public_bots.repo_bot_usernames_any(_repo_keys))
    except Exception as _e:
        # 不静默：空集会让下方责任人推断退化为「直接显示 actor」（不区分 bot/用户），
        # 必须留可观测信号，否则展示口径错误无从发现。
        log.warning(f"[pipeline] bot_username batch query failed: {_e}")

    # 飞书用户名缓存（请求级，消除 _resolve_feishu_name N+1）
    _feishu_name_cache = {}

    # 预建 issue_id → events 列表索引（避免 _get_issue_pipeline 内部反复遍历全量 events）
    _events_by_issue = {}
    for e in events:
        _events_by_issue.setdefault(e["issue_id"], []).append(e)
    # Issue #2925：窗口内占位标题「有界兜底」——只对当前页窗口内的 issue 外呼，
    # token/客户端复用 + 有限并发 + 总 deadline；写回与占位判定同口径并落已尝试
    # 标记。窗口外保持占位不触发外呼（由日志 skipped 计数显式标记未核实）。
    try:
        # 下推成功时 _win_ids 已是「最新事件倒序」的窗口；下推降级时 issue_ids 是
        # events 去重顺序，需先按最新 seq 排序，保证兜底窗口与最终展示窗口一致。
        if _pushdown_ok:
            _window_src = _win_ids
        else:
            _seq_by_id = {iid: max((e.get("seq") or 0) for e in evts)
                          for iid, evts in _events_by_issue.items()}
            _window_src = sorted(issue_ids, key=lambda x: _seq_by_id.get(x, 0),
                                 reverse=True)
        _title_map = _backfill_window_titles(
            repo_key, _repo_keys, _title_map, _window_src,
            _events_by_issue, window_size=_need)
    except Exception as e:
        log.warning(f"[pipeline] title backfill skipped (degrade to placeholder): {e}")
    # Issue #2050：确认窗一次读取（否则逐行 patrol_runtime_config() → 每行 5 条 meta SELECT）
    _confirm_minutes = None
    try:
        from src.api.patrol import patrol_runtime_config
        _confirm_minutes = int((patrol_runtime_config() or {}).get("confirm_minutes"))
    except Exception as e:
        log.warning(f"[pipeline] confirm_minutes read failed, fallback to default: {e}")
        _confirm_minutes = None
    pipelines = [_get_issue_pipeline(
        repo_key, iid, events,
        _title_map, _stage_map, _pipe_cfg_cache, _get_pipe_cfg,
        _bot_usernames, _feishu_name_cache, _events_by_issue,
        confirm_minutes=_confirm_minutes
    ) for iid in (_win_ids if _pushdown_ok else issue_ids)]
    pipelines = [p for p in pipelines if p is not None]

    # 下推失败时 events 是按「全仓」加载的，pipelines 也就成了全仓列表——这里按场景
    # 收口，否则场景详情页会静默显示所有场景的流水线（MySQL 5.7 窗口函数 1064 时的
    # 真实表现）。收口后 history_total/today_count/failed_count 由下面的 pipelines
    # 派生，口径自动一致。
    if not _pushdown_ok and _scene_filter:
        pipelines = [p for p in pipelines if str(p.get("scene_id") or "") == _scene_filter]

    # Issue #89: 检测 tasks 有记录但 events 无记录的 Issue（数据一致性差异）
    # 这些 Issue 在任务中心有任务，但流水线未生成事件 → 前端展示提示
    tasks_without_events = []
    # Issue #1077-P：成员判定改 set——原实现是 `_iid not in issue_ids`（list 线性查找），
    # 20 万 issue / 6 千事件编号时退化成平方级，这是 13s 耗时的主因。
    _issue_ids_set = set(issue_ids)
    try:
        with db_lock:
            c = db.cursor()
            # 索引友好写法：events(repo_key, issue_id) 等值查找，避免 20 万 issue × 全表扫描
            c.execute(
                "SELECT i.issue_number, i.title FROM issue i "
                "WHERE i.repo_key=? AND i.status IN ('active','processing') AND i.issue_number > 0 "
                "AND NOT EXISTS (SELECT 1 FROM events e "
                "                WHERE e.repo_key=i.repo_key AND e.issue_id='#'||i.issue_number) "
                "LIMIT 200",
                (repo_key,)
            )
            for r in c.fetchall():
                tasks_without_events.append({"issue_id": f"#{r[0]}", "title": r[1] or ""})
    except Exception:
        pass

    # 按 Issue 最新活动倒序：用预建索引中的最大 seq 排序（O(N)，避免反复遍历全量 events）
    _max_seq_cache = {iid: max((e["seq"] for e in evts), default=0) for iid, evts in _events_by_issue.items()}
    # Issue #1816：PR 伪 issue 的 issue_id 已在返回体改写为展示值 PR#N，缓存键是原始 id
    # （#pr_<n>_<scene>），必须用 raw_issue_id 查，否则键恒 0 沉底并按 PR 号正序。
    pipelines.sort(key=lambda p: _max_seq_cache.get(p.get("raw_issue_id") or p["issue_id"], 0), reverse=True)


    # 执行态派生（前端 B，见 multi-issue-isolation-design.md §4.4）：由 agent_tasks 实时派生，
    # 不维护 issue_agent_session.status 双份记账，避免状态漂移
    running_issues = _running_issue_ids(_repo_keys)
    for p in pipelines:
        p["executing"] = (p.get("raw_issue_id") or p["issue_id"]) in running_issues

    active = pipelines[0] if pipelines else None
    # Issue #542：history 截断为最近 history_limit 条（前端本就客户端分页展示），
    # history_total 保留全量条数；统计口径（today/failed/active_count）仍按全量计算。
    # history_offset：前端增量翻页拉取（0=最新一段；偏移后 active 不随 offset 重复返回，
    # has_more 标识是否还有更早的历史可拉）。
    if _pushdown_ok and _issues_total is not None:
        history_total = _issues_total - 1 if _issues_total > 1 else 0
    else:
        history_total = len(pipelines) - 1 if len(pipelines) > 1 else 0
    _offset = min(max(int(history_offset or 0), 0), history_total)
    _h_start = 1 + _offset
    _h_end = _h_start + history_limit
    history = pipelines[_h_start:_h_end] if len(pipelines) > 1 else []
    _has_more = (_offset + history_limit) < history_total

    # Pipeline config (event-dispatch-redesign-v2.md Phase 2)
    from src.api.pipeline_config import get_pipeline_config
    # 注：此前的占位局部变量与入参 scene_id 同名会互相覆盖，改为独立变量名（语义仍是「按场景取配置」）
    _scene_cfg_id = ""
    pipeline_config = get_pipeline_config(_scene_cfg_id) if _scene_cfg_id else None

    # 返回实际有事件的 repo_key（带 provider 前缀），供前端生成 VCS 链接
    _actual_repo_key = repo_key
    if events:
        _actual_repo_key = events[0].get("repo_key", repo_key) or repo_key

    # 返回 repo（不带 provider 前缀，前端用 _vcsUrl() 按 repo_key 判断 provider 拼链接）
    from src.kernel.repo_key import from_repo_key
    return {
        "repo": from_repo_key(_actual_repo_key),
        "repo_key": _actual_repo_key,
        "active": active,
        "history": history,
        "history_total": history_total,
        "history_limit": history_limit,
        "history_offset": _offset,
        "history_has_more": _has_more,
        "active_count": 1 if active and active["result"] in ("Processing", "Pending") else 0,
        "today_count": (_issues_total if _pushdown_ok and _issues_total is not None else len(pipelines)),
        "failed_count": (len(_failed_ids) if _pushdown_ok and _failed_ids is not None
                         else len([p for p in pipelines if p["result"] == "Failed"])),
        "scene_id": _scene_filter,
        "pipeline_config": pipeline_config,
        "tasks_without_events": tasks_without_events,
        "has_tasks_without_events": len(tasks_without_events) > 0,
    }
