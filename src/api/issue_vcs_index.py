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
from typing import Optional

from src.config import log
from src.repo_key import parse_repo_key

# 索引 TTL（秒）：GitCode/GitHub 侧删除/新建卡片在该窗口内可容忍短暂不一致。
VCS_INDEX_TTL_SECONDS = 300
# 失败结果短缓存，避免 VCS 抖动时每个列表请求都重试。
VCS_INDEX_FAILURE_TTL_SECONDS = 30
# 单次分页拉取上限（100 条/页 × 200 页 = 2 万条），超限视为 unknown（fail-open，防误杀）。
_MAX_PAGES = 200
_PER_PAGE = 100
# 索引未命中时允许逐条复核的最大候选数；超过即整体 unknown（fail-open），绝不批量确认删除。
_CONFIRM_LIMIT = 100
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


def _cached_refresh(repo_key: str) -> tuple[bool, dict]:
    with _cache_lock:
        entry = _refresh_cache.get(repo_key)
    if not entry:
        return False, {}
    expires_at, info = entry
    if time.monotonic() >= expires_at:
        with _cache_lock:
            _refresh_cache.pop(repo_key, None)
        return False, {}
    return True, info


def _store_refresh(repo_key: str, info: dict) -> None:
    ttl = VCS_INDEX_TTL_SECONDS if info.get("status") == "ok" else VCS_INDEX_FAILURE_TTL_SECONDS
    with _cache_lock:
        _refresh_cache[repo_key] = (time.monotonic() + ttl, info)


def _fetch_gitcode_numbers(owner: str, repo: str, token: str) -> Optional[set[int]]:
    """分页拉取 GitCode issue 编号；仅在空页终止，短页不代表末页（防索引截断误删）。"""
    from src.user.repo_manager import _req

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
    from src.user.repo_manager import _github_req

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


def vcs_issue_numbers(repo_key: str) -> Optional[frozenset[int]]:
    """返回 repo_key 对应 VCS 的 issue 编号集合；无法核实时返回 ``None``。

    ``None`` 表示 unknown（无 token / 网络异常 / 非支持 provider / 空集合防护），
    调用方必须 fail-open，不得据此隐藏卡片。
    """
    hit, cached = _cached(repo_key)
    if hit:
        return cached

    provider, owner, repo = parse_repo_key(repo_key)
    result: Optional[frozenset[int]] = None
    try:
        if provider == "github":
            from src.user.db import get_github_token
            token = get_github_token(repo_key=repo_key) or ""
            if token:
                raw = _fetch_github_numbers(owner, repo, token)
                result = frozenset(raw) if raw else None
        elif provider == "gitcode":
            from src.user.db import get_repo_token_with_refresh
            token = get_repo_token_with_refresh(repo_key) or ""
            if token:
                raw = _fetch_gitcode_numbers(owner, repo, token)
                result = frozenset(raw) if raw else None
        else:
            result = None
    except Exception as exc:
        log.warning(f"issue_vcs_index: {repo_key} fetch failed: {exc}, fail-open")
        result = None

    _store(repo_key, result)
    return result


def _chunks(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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
        from src.api.event_pipeline import fetch_issue_via_vcs
    except Exception as exc:
        log.warning(f"issue_vcs_index: import fetch_issue_via_vcs failed: {exc}")
        return set(), set(numbers)
    for num in numbers:
        try:
            state, _ = fetch_issue_via_vcs(repo_key, num)
        except Exception:
            state = "unknown"
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
    from src.session_manager import db, db_lock

    # 读台账（短暂持有 db_lock；网络复核必须在锁外，避免阻塞其它 DB 访问）。
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, issue_number, vcs_exists, vcs_checked_at FROM issue WHERE repo_key=?",
                (repo_key,))
            rows = c.fetchall()
        except Exception as exc:
            log.warning(f"issue_vcs_index: {repo_key} read issue rows failed: {exc}")
            return None

    candidates: list[int] = []
    for row in rows:
        _id, _num, _exists, _checked = row
        try:
            if _exists != 0 and int(_num) not in numbers:
                candidates.append(int(_num))
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
            _num_int = int(_num)
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
    with db_lock:
        c = db.cursor()
        try:
            for chunk in _chunks(to_deleted):
                c.execute(
                    f"UPDATE issue SET vcs_exists=0, vcs_checked_at=datetime('now') "
                    f"WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            for chunk in _chunks(to_restored):
                c.execute(
                    f"UPDATE issue SET vcs_exists=1, vcs_checked_at=datetime('now') "
                    f"WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            for chunk in _chunks(to_stamp):
                c.execute(
                    f"UPDATE issue SET vcs_checked_at=datetime('now') "
                    f"WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            db.commit()
        except Exception as exc:
            log.warning(f"issue_vcs_index: {repo_key} write back failed: {exc}")
            try:
                db.rollback()
            except Exception:
                pass
            return None
    return frozenset(unresolved)


def refresh_vcs_existence(repo_keys: list[str]) -> dict[str, dict]:
    """按 repo_key 刷新存在性并回写台账（仅刷新缓存 miss 时回写，避免每请求全表扫描）。

    返回 ``{repo_key: {"status": "ok" | "unavailable", "unresolved": frozenset[int]}}``：
    - ``ok``：已拿到编号集合并完成回写；``unresolved`` 为无法确认存在性的卡片编号；
    - ``unavailable``：无法核实或回写失败（调用方按 fail-open 不过滤该 provider 的卡片）。
    """
    status: dict[str, dict] = {}
    for repo_key in repo_keys:
        if not repo_key:
            continue
        hit, info = _cached_refresh(repo_key)
        if hit:
            status[repo_key] = info
            continue
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
        status[repo_key] = info
    return status


def backfill_vcs_exists(repo_keys: Optional[list[str]] = None) -> dict:
    """存量回填（幂等可重跑）：对台账涉及的仓库刷新 VCS 存在性并落列。

    - 默认覆盖 `issue` 表中所有尚待校验（vcs_checked_at 为空）的仓库；
    - 分批（默认 200 卡/批）限速，避免启动期/VCS 限流；
    - 仅做软标记，不删除台账行，保留审计与事件链。
    """
    from src.session_manager import db, db_lock

    if repo_keys is None:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT DISTINCT repo_key FROM issue "
                    "WHERE vcs_checked_at IS NULL OR vcs_checked_at=''")
                repo_keys = [r[0] for r in c.fetchall() if r[0]]
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
            with db_lock:
                c = db.cursor()
                c.execute("SELECT COUNT(*) FROM issue WHERE repo_key=? AND vcs_checked_at IS NOT NULL",
                          (repo_key,))
                summary["cards"] += int(c.fetchone()[0] or 0)
        except Exception:
            pass
        time.sleep(_BACKFILL_SLEEP_SECONDS)
    log.info(f"issue_vcs_index: backfill done {summary}")
    return summary
