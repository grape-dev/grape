"""Issue #2925：流水线标题兜底（窗口内批量、有界、可收敛）。

分层说明（架构守护 A8 整改）：缓存三件套、窗口收敛编排与写回护栏原先落在
`src/domains/runtime/routes/pipeline.py`，现下沉到 domains/runtime/pipeline；
routes 只负责注入 DB 与调用，不再持有编排逻辑。

三层收敛：
  1. 范围有界：只处理调用方给定的窗口 issue，窗口外不触发外呼；
  2. 执行有界：token/客户端复用 + 有限并发 + 总 deadline（见 event_pipeline）；
  3. 状态有界：占位判定单一事实源 + 同口径写回护栏 + 已尝试标记负缓存。
"""
import os
import time

from src.kernel.config import log

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
# 进程内「真实标题」缓存（降级路径）：`events` 有记录但 `issue` 表无该行时，
# 取到的真实标题无处回写。若只更新本次响应，该 issue 后续每次请求都会重复外呼。
# 键 (repo_key, issue_number) → (title, ts)，TTL 与负缓存同口径。
_TITLE_REAL_CACHE: dict = {}
# 进程内缓存的条目上限：超过即先按 TTL 清理，避免长期运行进程无界增长。
# 取值来源：单条约 200B（(repo_key, number) 二元组键 + float/str 值），
# 4096 条 ≈ 0.8MB，相对进程常驻内存可忽略；该值只承担「防止无界增长」，
# 不是容量目标，超出后按 TTL → 插入序淘汰，语义不受影响。
_TITLE_NEG_CACHE_MAX = 4096


def _title_cache_put(cache: dict, key, value) -> None:
    """写入进程内缓存；超过上限时剔除过期条目，保证有界。"""
    if len(cache) >= _TITLE_NEG_CACHE_MAX:
        _ttl = _title_negative_ttl()
        _now = time.time()
        for _k, _v in list(cache.items()):
            # 负缓存值为 ts；真实缓存值为 (title, ts)。
            _ts = _v[1] if isinstance(_v, tuple) else _v
            if not _title_marker_fresh(_ts, _now, _ttl):
                cache.pop(_k, None)
        if len(cache) >= _TITLE_NEG_CACHE_MAX:
            # 仍然超限（TTL 全新鲜 / 配置为永久）→ 按插入顺序丢掉最早的一批，
            # 保持有界；同 key 重写不刷新插入序，仍按首次写入位置淘汰。
            for _k in list(cache)[:len(cache) - _TITLE_NEG_CACHE_MAX + 1]:
                cache.pop(_k, None)
    cache[key] = value


def _title_neg_cache_put(key, ts: float) -> None:
    """写入进程内负缓存；超过上限时剔除过期条目（无列降级路径专用）。"""
    _title_cache_put(_TITLE_NEG_CACHE, key, ts)


def _title_real_cache_put(key, title: str, ts: float) -> None:
    """写入进程内真实标题缓存（issue 表无行时避免重复外呼）。"""
    _title_cache_put(_TITLE_REAL_CACHE, key, (title, ts))


# 写回/标记 SQL 的并发护栏（三处共用，保证与占位判定同口径）。
_TITLE_WRITEBACK_GUARD = "(title='' OR title IS NULL OR title=?)"
# 标记读取按分片执行：`issue_number IN (...)` 随窗口（1+offset+limit）增长。
# 取值来源：SQLite 单条 SQL 变量上限 999，此处同时占 len(repo_keys) + len(chunk)
# 个占位符；200 给 repo_key 侧（平台前缀候选通常 ≤6）留足余量，且分片后单次
# 结果集有界，不触发「持锁批量扫描」形态。
_TITLE_MARKER_QUERY_CHUNK = 200

_TITLE_NEG_CACHE_TTL_DEFAULT = 21600
"""负缓存默认有效期（秒）= 6h。

取值口径：按「一个工作日」量级取整 —— 热请求（同仓第二次起）在开启的会话内
必然命中；上游事后改了标题，当天内也会自然重试一次。**该值未经命中率与陈旧容忍度
实测**，属量级默认值而非测量结论；需要更严/更松时用 env 覆盖（见 .env.example），
0 表示永久（只靠写回真实标题解除）。
"""


def _title_negative_ttl() -> int:
    """负缓存有效期（秒）。默认 6h：足够覆盖热请求，又允许上游改标题后自然重试。
    0/负数表示标记永久有效（只靠「写回真实标题」解除）。"""
    try:
        raw = os.environ.get("GRAPE_PIPELINE_TITLE_NEG_CACHE_TTL", "")
        return int(raw) if str(raw).strip() != "" else _TITLE_NEG_CACHE_TTL_DEFAULT
    except Exception:
        return _TITLE_NEG_CACHE_TTL_DEFAULT


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
    """`#123` → 123；平台 ident（`IKW28R`/`I1J8U4`）→ 原样字符串；PR 伪 id / 空 → None。

    Issue #3055：基线实现用 `isdigit()` 判定，会把字符串 ident 判成 None →
    pipeline 的标题回填对 gitee/ident 单完全不生效（与入口收口同源问题）。
    判据统一走 kernel.issue_ident（数字恒 int、ident 恒 str、pr_/空恒 None）。
    """
    from src.kernel.issue_ident import native_issue_number, normalize_issue_ident

    if not normalize_issue_ident(issue_id):
        return None
    return native_issue_number(issue_id)


def _default_db():
    """延迟取 runtime 域 DB 代理（避免与 event_pipeline 的顶层 import 成环）。"""
    from src.domains.runtime.pipeline.event_pipeline import _rt_db
    return _rt_db


def backfill_window_titles(repo_key: str, repo_keys: list, title_map: dict,
                           issue_ids: list, events_by_issue: dict = None,
                           window_size: int = 0, db=None) -> dict:
    """Issue #2925：仅对当前页窗口内的占位标题做有界兜底，并落「已尝试」标记。

    三层收敛中的「范围有界 + 状态有界」：
      1. 范围：只处理调用方给定的窗口 issue（`window_size` 之外的 issue 不触发外呼，
         保持占位并由日志计数显式标记未核实）；
      2. 负缓存：`title_fetched_at` 仍在 TTL 内（或进程内缓存命中）直接跳过；
      3. 写回：只有「取到且非占位」的标题才落库，条件与 `_is_placeholder_title`
         同口径；取不到真实标题只落已尝试标记，不回写占位值。

    `db`：runtime 域 DB 代理；缺省取 `event_pipeline._rt_db`（生产路径）。
    调用方（routes 层）显式传入自己的代理，测试可注入内存库。

    返回就地更新后的 `title_map`（真实标题立即可用于本次响应）。
    整段 best-effort：任何异常都不允许阻断流水线列表返回。
    """
    if db is None:
        db = _default_db()
    if title_map is None:
        title_map = {}
    if not issue_ids:
        return title_map
    _started = time.monotonic()
    _now = time.time()
    _ttl = _title_negative_ttl()
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
        # issue 表无该行时上一轮取到的真实标题无处回写，命中进程内真实标题缓存
        # 直接复用，避免该 issue 每次请求都重复外呼。
        _real_cached = _TITLE_REAL_CACHE.get((_rk, _num))
        if _real_cached and _title_marker_fresh(_real_cached[1], _now, _ttl):
            title_map[_iid] = _real_cached[0]
            _cache_hit += 1
            continue
        _candidates.append((_iid, _num, _rk))
    if not _candidates:
        log.info("[pipeline] title_backfill repo=%s window=%d candidates=0 "
                 "vcs_calls=0 cache_hit=%d elapsed=%.3fs",
                 repo_key, len(_window_ids), _cache_hit, time.monotonic() - _started)
        return title_map

    # ① 持久已尝试标记（列存在才用；否则走进程内负缓存）
    _marker_ok = False
    _marked: dict = {}
    try:
        from src.kernel.session_manager import db_lock
        from src.domains.runtime.schema_probe import column_exists
        with db_lock:
            _c = db.cursor()
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
    # 不得落负缓存（否则会把未尝试的 issue 锁住不再重试）。
    _attempted_ids: set = set()
    _submitted = 0
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
        _submitted += int(_stats.get("submitted") or 0)
        for _started_iid in (_stats.get("attempted_ids") or ()):
            _attempted_ids.add((_rk, _started_iid))

    # ③ 状态有界：只写真实标题（同口径），失败落已尝试标记形成负缓存
    _now_iso = str(int(_now))
    _written = 0
    try:
        from src.kernel.session_manager import db_lock
        with db_lock:
            _db = db
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
                    else:
                        # 取到真实标题但 issue 表无该行（events 有记录、issue 表
                        # 缺行）：无处回写，落进程内真实标题缓存，后续请求直接
                        # 复用该标题且 0 外呼（与失败分支的负缓存对称）。
                        _title_real_cache_put((_rk, _num), _api, _now)
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
             "submitted=%d vcs_calls=%d cancelled=%d fetched=%d failed=%d "
             "cache_hit=%d skipped_marked=%d elapsed=%.3fs",
             repo_key, len(_window_ids), len(_candidates), _submitted,
             len(_attempted_ids), max(0, _submitted - len(_attempted_ids)),
             _written, max(0, len(_attempted_ids) - _written),
             _cache_hit, _skipped_marked, time.monotonic() - _started)
    return title_map