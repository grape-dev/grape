"""Issue #995：provider 感知的 VCS Issue 编号索引（存在性校验 + TTL 缓存 + 台账回写）。

背景：`issues list` 只查本地 `issue` 台账，GitCode/GitHub 侧已删除的卡片仍会返回，
下游按 `issue_number` 查重时与 `git-repo issues get` 的 404 口径不一致，导致误判。

设计（见 Issue #995 Spec）：
- 按 repo_key（自带 provider 前缀）分别分页拉取 VCS issue 编号集合，gitcode/github 不混用；
- 进程内 TTL 缓存（默认 300s），避免列表请求逐条 get 的 N+1；
- 拉取失败/无 token/超时 → 返回 ``None``（未知），调用方 fail-open，不隐藏任何卡片；
- 校验结果批量回写 `issue.vcs_exists` / `issue.vcs_checked_at`，默认过滤可走 SQL，
  分页 COUNT 与返回集合口径天然一致。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from src.kernel.config import log
from src.kernel.issue_ident import native_issue_number  # Issue #3055
from src.kernel.repo_key import parse_repo_key

# 索引 TTL（秒）：GitCode/GitHub 侧删除/新建卡片在该窗口内可容忍短暂不一致。
VCS_INDEX_TTL_SECONDS = 300
# 失败结果短缓存，避免 VCS 抖动时每个列表请求都重试。
VCS_INDEX_FAILURE_TTL_SECONDS = 30
# 单次分页拉取上限（100 条/页 × 200 页 = 2 万条），超限视为 unknown（fail-open，防误杀）。
_MAX_PAGES = 200
_PER_PAGE = 100
# 索引未命中时允许逐条复核的最大候选数；超过即整体 unknown（fail-open），绝不批量确认删除。
_CONFIRM_LIMIT = 100
# 单条复核的并发度：串行 100 条 × 100ms 上游 ≈ 10s，并发 8 路降到 ~1.3s（Issue #995 复审）。
_CONFIRM_WORKERS = 8
# 存量回填每仓之间的限速（秒），避免启动期触发 VCS 限流。
_BACKFILL_SLEEP_SECONDS = 0.2

_cache: dict[str, tuple[float, Optional[frozenset]]] = {}
# 刷新结果缓存（status/unresolved），使列表请求只在缓存 miss 时回写台账。
_refresh_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """清空进程内索引缓存（测试/运维手动刷新用）。"""
    with _cache_lock:
        _cache.clear()
        _refresh_cache.clear()


def _cached(repo_key: str) -> tuple[bool, Optional[frozenset]]:
    with _cache_lock:
        entry = _cache.get(repo_key)
    if not entry:
        return False, None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        with _cache_lock:
            _cache.pop(repo_key, None)
        return False, None
    return True, value


def _store(repo_key: str, value: Optional[frozenset]) -> None:
    ttl = VCS_INDEX_TTL_SECONDS if value is not None else VCS_INDEX_FAILURE_TTL_SECONDS
    with _cache_lock:
        _cache[repo_key] = (time.monotonic() + ttl, value)


def _store_refresh(repo_key: str, info: dict) -> None:
    ttl = VCS_INDEX_TTL_SECONDS if info.get("status") == "ok" else VCS_INDEX_FAILURE_TTL_SECONDS
    with _cache_lock:
        _refresh_cache[repo_key] = (time.monotonic() + ttl, info)


def _fetch_gitcode_numbers(owner: str, repo: str, token: str) -> Optional[set[int]]:
    """分页拉取 GitCode issue 编号；仅在空页终止，短页不代表末页（防索引截断误删）。"""
    from src.domains.mgmt.user.repo_manager import _req

    numbers: set[int] = set()
    page = 1
    while True:
        if page > _MAX_PAGES:
            return None
        url = (f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues"
               f"?state=all&page={page}&per_page={_PER_PAGE}")
        data = _req("GET", url, token=token)
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                numbers.add(int(item.get("number")))
            except (TypeError, ValueError):
                continue
        if not data:
            break
        page += 1
    return numbers or None


def _fetch_github_numbers(owner: str, repo: str, token: str) -> Optional[set[int]]:
    """分页拉取 GitHub issue 编号；仅在空页终止，短页不代表末页（防索引截断误删）。"""
    from src.domains.mgmt.user.repo_manager import _github_req

    numbers: set[int] = set()
    page = 1
    while True:
        if page > _MAX_PAGES:
            return None
        url = (f"https://api.github.com/repos/{owner}/{repo}/issues"
               f"?state=all&page={page}&per_page={_PER_PAGE}")
        data = _github_req("GET", url, token=token)
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict):
                continue
            # GitHub issues 接口会把 PR 也当成 issue 返回，编号空间重叠但非需求卡片，排除。
            if item.get("pull_request"):
                continue
            try:
                numbers.add(int(item.get("number")))
            except (TypeError, ValueError):
                continue
        if not data:
            break
        page += 1
    return numbers or None


def _fetch_numbers_via_port(provider: str, owner: str, repo: str,
                            token: str) -> Optional[set[int]]:
    """卡/端口驱动的 issue 编号分页拉取（provider 无关；gitee/gitlab 等非二分平台）。

    资源段与鉴权全部来自 providers 端口（``module_for``：模块或平台卡垫片）：
    API_BASE + ``repo_segment``（gitlab=/projects/{urlencoded}，其余=/repos/{o}/{r}）
    + ``request``（按卡 api_auth 取 query_token/private_token/Bearer）。分页到空页
    为止（短页不代表末页，防索引截断误删）。端口不可用 → ValueError（调用方
    fail-open + warning，绝不静默返回 unknown）。
    """
    from src.domains.runtime.vcs.providers import module_for

    mod = module_for(provider)
    if mod is None:
        raise ValueError(f"provider {provider!r} 无资源 API 端口/平台卡，无法建立 issue 索引")
    seg_fn = getattr(mod, "repo_segment", None)
    if callable(seg_fn):
        repo_path = seg_fn(owner, repo)
    else:
        repo_path = f"repos/{owner}/{repo}"
    api_base = str(getattr(mod, "API_BASE", "") or "").rstrip("/")
    if not api_base:
        raise ValueError(f"provider {provider!r} 端口未声明 API_BASE，无法建立 issue 索引")
    numbers: set[int] = set()
    page = 1
    while True:
        if page > _MAX_PAGES:
            return None
        url = (f"{api_base}/{repo_path}/issues"
               f"?state=all&page={page}&per_page={_PER_PAGE}")
        data = mod.request("GET", url, token)
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict):
                continue
            # github 方言（github/gitcode/gitee）issues 接口把 PR 混入编号空间 → 排除。
            if item.get("pull_request"):
                continue
            try:
                numbers.add(int(item.get("number")))
            except (TypeError, ValueError):
                continue
        if not data:
            break
        page += 1
    return numbers or None


def vcs_issue_numbers(repo_key: str) -> Optional[frozenset[int]]:
    """返回 repo_key 对应 VCS 的 issue 编号集合；无法核实时返回 ``None``。

    ``None`` 表示 unknown（无 token / 网络异常 / 端口不可用 / 空集合防护），
    调用方必须 fail-open，不得据此隐藏卡片；任何 unknown 均落 warning（不静默）。
    """
    hit, cached = _cached(repo_key)
    if hit:
        return cached

    provider, owner, repo = parse_repo_key(repo_key)
    result: Optional[frozenset[int]] = None
    try:
        # 2026-11-13 if/else 收口：github/gitcode 专属 fetch 分叉退役——
        # _fetch_numbers_via_port 已是全平台通用实现（API_BASE/repo_segment/
        # 鉴权全走 providers 端口；PR 排除、空页终止语义与专属版一致，四平台
        # 实测端口齐备）；token 统一走 get_repo_token_for（策略链+能力位回落）。
        # 原专属 _fetch_github_numbers/_fetch_gitcode_numbers 保留为测试锚。
        from src.domains.mgmt.user.db import get_repo_token_for
        token = get_repo_token_for(repo_key) or ""
        if token:
            raw = _fetch_numbers_via_port(provider, owner, repo, token)
            result = frozenset(raw) if raw else None
        else:
            log.warning(
                f"issue_vcs_index: {repo_key} 无可用 token，issue 索引 fail-open（unknown）")
    except Exception as exc:
        log.warning(f"issue_vcs_index: {repo_key} fetch failed: {exc}, fail-open")
        result = None

    _store(repo_key, result)
    return result


def _confirm_missing(repo_key: str, numbers: list[int]) -> tuple[set[int], set[int]]:
    """对索引未命中的编号做单条复核，返回 ``(confirmed_missing, unresolved)``。

    索引有 TTL 缓存：新建卡片可能在缓存刷新前不在集合里。单条 `get` 复核可避免
    把刚创建的卡片误标为已删除；无 token / 网络异常（unknown）一律按“存在”处理
    （fail-open）。候选过多（> ``_CONFIRM_LIMIT``）时整体视为 unresolved（fail-open），
    绝不自动确认删除，避免索引不完整时批量误标。
    """
    if len(numbers) > _CONFIRM_LIMIT:
        log.warning(
            f"issue_vcs_index: {repo_key} {len(numbers)} unresolved candidates "
            f"> {_CONFIRM_LIMIT}, fail-open (no bulk confirm)")
        return set(), set(numbers)
    confirmed: set[int] = set()
    unresolved: set[int] = set()
    try:
        from src.domains.runtime.pipeline.event_pipeline import fetch_issue_via_vcs
    except Exception as exc:
        log.warning(f"issue_vcs_index: import fetch_issue_via_vcs failed: {exc}")
        return set(), set(numbers)

    def _one(num: int):
        try:
            state, _ = fetch_issue_via_vcs(repo_key, num)
        except Exception:
            state = "unknown"
        return num, state

    # Issue #995 复审：串行复核 100 条 × 上游 100ms ≈ 10s，会直接顶满首个列表请求；
    # 并发 8 路压到 ~1.3s。ex.map 保序，返回口径与串行完全一致。
    workers = min(_CONFIRM_WORKERS, len(numbers)) or 1
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vcs-confirm") as pool:
        for num, state in pool.map(_one, numbers):
            if state == "missing":
                confirmed.add(num)
            elif state != "exists":
                unresolved.add(num)
    return confirmed, unresolved


def _apply_existence(repo_key: str, numbers: frozenset[int]) -> Optional[frozenset[int]]:
    """把仓库内台账卡片的 vcs_exists 与当前 VCS 编号集合对齐（幂等）。

    返回 ``None`` 表示读/写失败（调用方必须映射为 unavailable/fail-open）；
    返回编号集合表示回写成功，集合内为“无法确认存在性”的编号（卡片级 unknown）。
    """
    from src.domains.runtime import public as runtime_public

    # 读台账（任意拓扑原语内部持 db_lock；网络复核必须在锁外，避免阻塞其它 DB 访问）。
    try:
        rows = runtime_public.issue_vcs_index_rows_any(repo_key)
    except Exception as exc:
        log.warning(f"issue_vcs_index: {repo_key} read issue rows failed: {exc}")
        return None

    candidates: list[int] = []
    for row in rows:
        _id, _num, _exists, _checked = row
        try:
            # Issue #3055：原生形态比较（数字→int 与 platform 数字集同型；ident→str 走单条复核）
            _n_native = native_issue_number(_num)
            if _exists != 0 and _n_native not in numbers:
                candidates.append(_n_native)
        except (TypeError, ValueError):
            continue
    # 索引未命中的“当前仍标记为存在”的卡片，逐条复核后才判定删除（防 TTL 内新建卡片误杀）。
    confirmed_missing, unresolved = _confirm_missing(repo_key, candidates)

    to_deleted: list[int] = []
    to_restored: list[int] = []
    to_stamp: list[int] = []
    for row in rows:
        _id, _num, _exists, _checked = row
        try:
            _num_int = native_issue_number(_num)
            exists = _num_int in numbers
        except (TypeError, ValueError):
            continue
        if not exists:
            # 已标记删除的保持；新发现的仅在单条复核确认 404 后才落 0。
            if _exists != 0 and _num_int in confirmed_missing:
                to_deleted.append(_id)
        elif _exists == 0:
            to_restored.append(_id)
        elif not _checked:
            to_stamp.append(_id)

    if not (to_deleted or to_restored or to_stamp):
        return frozenset(unresolved)
    # 三组 UPDATE 一次原子回写（owner 侧 `_any_tx`）：任一条失败整批回滚 → unavailable，
    # 与原实现的 `db.commit()` / rollback 口径一致；分块（500/批）在 owner 侧完成。
    try:
        runtime_public.apply_issue_vcs_existence_any(to_deleted, to_restored, to_stamp)
    except Exception as exc:
        log.warning(f"issue_vcs_index: {repo_key} write back failed: {exc}")
        return None
    return frozenset(unresolved)


def _refresh_entry(repo_key: str) -> tuple[bool, dict | None]:
    """读取刷新结果缓存，不删除过期项：返回 (是否仍在 TTL 内, 缓存值)。

    过期但存在的值要留给 ``refresh_vcs_existence`` 做 stale-while-revalidate：
    先把旧结果立刻返回，再在后台刷新（Issue #995 复审的冷刷新尖峰修复）。
    """
    with _cache_lock:
        entry = _refresh_cache.get(repo_key)
    if not entry:
        return False, None
    expires_at, info = entry
    return (time.monotonic() < expires_at), info


# 后台刷新去重：同一个 repo 同时只允许一个刷新线程，避免列表高峰把上游打爆。
_refresh_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _refresh_repo(repo_key: str) -> dict:
    """同步执行一次「拉编号 → 对齐台账 → 存缓存」，返回状态字典。"""
    numbers = vcs_issue_numbers(repo_key)
    if numbers is None:
        info = {"status": "unavailable", "unresolved": frozenset()}
    else:
        unresolved = _apply_existence(repo_key, numbers)
        if unresolved is None:
            info = {"status": "unavailable", "unresolved": frozenset()}
        else:
            info = {"status": "ok", "unresolved": frozenset(unresolved)}
    _store_refresh(repo_key, info)
    return info


def _schedule_refresh(repo_key: str) -> bool:
    """把一次刷新丢到后台线程（去重）。返回是否真的启动了新线程。"""
    with _inflight_lock:
        if repo_key in _refresh_inflight:
            return False
        _refresh_inflight.add(repo_key)

    def _run() -> None:
        try:
            _refresh_repo(repo_key)
        except Exception as exc:
            log.warning(f"issue_vcs_index: background refresh failed {repo_key}: {exc}")
        finally:
            with _inflight_lock:
                _refresh_inflight.discard(repo_key)

    threading.Thread(target=_run, name=f"vcs-refresh-{repo_key}"[:64], daemon=True).start()
    return True


def refresh_vcs_existence(repo_keys: list[str]) -> dict[str, dict]:
    """按 repo_key 刷新存在性并回写台账（仅刷新缓存 miss 时回写，避免每请求全表扫描）。

    返回 ``{repo_key: {"status": "ok" | "unavailable", "unresolved": frozenset[int]}}``：
    - ``ok``：已拿到编号集合并完成回写；``unresolved`` 为无法确认存在性的卡片编号；
    - ``unavailable``：无法核实或回写失败（调用方按 fail-open 不过滤该 provider 的卡片）。

    缓存过期时走 stale-while-revalidate：旧结果立即返回（列表请求不等上游），
    后台线程按 repo 去重刷新；只有从来没有缓存过的 repo 才同步刷新。
    """
    status: dict[str, dict] = {}
    for repo_key in repo_keys:
        if not repo_key:
            continue
        fresh, info = _refresh_entry(repo_key)
        if fresh and info is not None:
            status[repo_key] = info
            continue
        if info is not None:
            # 过期但可用：先给旧值，后台补新值（旧值最多多存活一个刷新周期）。
            _schedule_refresh(repo_key)
            status[repo_key] = info
            continue
        status[repo_key] = _refresh_repo(repo_key)
    return status


def backfill_vcs_exists(repo_keys: Optional[list[str]] = None) -> dict:
    """存量回填（幂等可重跑）：对台账涉及的仓库刷新 VCS 存在性并落列。

    - 默认覆盖 `issue` 表中所有尚待校验（vcs_checked_at 为空）的仓库；
    - 分批（默认 200 卡/批）限速，避免启动期/VCS 限流；
    - 仅做软标记，不删除台账行，保留审计与事件链。
    """
    from src.domains.runtime import public as runtime_public

    if repo_keys is None:
        try:
            repo_keys = runtime_public.issue_vcs_backfill_repo_keys_any()
        except Exception as exc:
            log.warning(f"issue_vcs_index: backfill repo scan failed: {exc}")
            return {"repos": 0, "ok": 0, "unavailable": 0, "cards": 0}

    summary = {"repos": 0, "ok": 0, "unavailable": 0, "cards": 0}
    for repo_key in repo_keys:
        status = refresh_vcs_existence([repo_key])
        summary["repos"] += 1
        if (status.get(repo_key) or {}).get("status") == "ok":
            summary["ok"] += 1
        else:
            summary["unavailable"] += 1
        try:
            summary["cards"] += runtime_public.issue_vcs_checked_count_any(repo_key)
        except Exception as exc:  # 计数失败不影响回填主流程（仅少报 cards 数）
            log.debug(f"issue_vcs_index: card count skipped {repo_key}: {exc}")
        time.sleep(_BACKFILL_SLEEP_SECONDS)
    log.info(f"issue_vcs_index: backfill done {summary}")
    return summary
