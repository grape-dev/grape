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

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from src.config import log
from src.repo_key import parse_repo_key, split_owner_repo

# 索引 TTL（秒）：GitCode/GitHub 侧删除/新建卡片在该窗口内可容忍短暂不一致。
VCS_INDEX_TTL_SECONDS = 300
# 失败结果短缓存，避免 VCS 抖动时每个列表请求都重试。
VCS_INDEX_FAILURE_TTL_SECONDS = 30
# 单次分页拉取上限（100 条/页 × 200 页 = 2 万条），超限视为 unknown（fail-open，防误杀）。
_MAX_PAGES = 200
_PER_PAGE = 100

_cache: dict[str, tuple[float, Optional[frozenset]]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """清空进程内索引缓存（测试/运维手动刷新用）。"""
    with _cache_lock:
        _cache.clear()


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


def _fetch_gitcode_numbers(owner: str, repo: str, token: str) -> Optional[set[int]]:
    numbers: set[int] = set()
    page = 1
    while page <= _MAX_PAGES:
        url = (f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues"
               f"?state=all&page={page}&per_page={_PER_PAGE}")
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + token, "User-Agent": "Mozilla/5.0"})
        data = json.load(urllib.request.urlopen(req, timeout=15))
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                numbers.add(int(item.get("number")))
            except (TypeError, ValueError):
                continue
        if len(data) < _PER_PAGE:
            break
        page += 1
    else:
        return None
    return numbers or None


def _fetch_github_numbers(owner: str, repo: str, token: str) -> Optional[set[int]]:
    numbers: set[int] = set()
    page = 1
    while page <= _MAX_PAGES:
        url = (f"https://api.github.com/repos/{owner}/{repo}/issues"
               f"?state=all&page={page}&per_page={_PER_PAGE}")
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + token,
                          "Accept": "application/vnd.github+json",
                          "User-Agent": "Mozilla/5.0"})
        data = json.load(urllib.request.urlopen(req, timeout=15))
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
        if len(data) < _PER_PAGE:
            break
        page += 1
    else:
        return None
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
    except urllib.error.HTTPError as exc:
        log.warning(f"issue_vcs_index: {repo_key} HTTP {exc.code}, fail-open")
        result = None
    except Exception as exc:
        log.warning(f"issue_vcs_index: {repo_key} fetch failed: {exc}, fail-open")
        result = None

    _store(repo_key, result)
    return result


def _chunks(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _confirm_missing(repo_key: str, numbers: list[int]) -> set[int]:
    """对索引未命中的编号做单条复核，返回确认 404 的编号集合。

    索引有 TTL 缓存：新建卡片可能在缓存刷新前不在集合里。单条 `get` 复核可避免
    把刚创建的卡片误标为已删除；无 token / 网络异常（unknown）一律按“存在”处理
    （fail-open）。候选过多（>100）时不再逐条复核，按索引结果处理，避免拖垮请求。
    """
    if len(numbers) > 100:
        return set(numbers)
    confirmed: set[int] = set()
    try:
        from src.api.event_pipeline import fetch_issue_via_vcs
    except Exception as exc:
        log.warning(f"issue_vcs_index: import fetch_issue_via_vcs failed: {exc}")
        return set()
    for num in numbers:
        try:
            state, _ = fetch_issue_via_vcs(repo_key, num)
        except Exception:
            state = "unknown"
        if state == "missing":
            confirmed.add(num)
    return confirmed


def _apply_existence(repo_key: str, numbers: frozenset[int]) -> None:
    """把仓库内台账卡片的 vcs_exists 与当前 VCS 编号集合对齐（幂等）。"""
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
            return

    candidates: list[int] = []
    for row in rows:
        _id, _num, _exists, _checked = row
        try:
            if _exists != 0 and int(_num) not in numbers:
                candidates.append(int(_num))
        except (TypeError, ValueError):
            continue
    # 索引未命中的“当前仍标记为存在”的卡片，逐条复核后才判定删除（防 TTL 内新建卡片误杀）。
    confirmed_missing = _confirm_missing(repo_key, candidates)

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
        return
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

def refresh_vcs_existence(repo_keys: list[str]) -> dict[str, str]:
    """按 repo_key 刷新存在性并回写台账。

    返回 ``{repo_key: "ok" | "unavailable"}``：``ok`` 表示已拿到编号集合并完成回写，
    ``unavailable`` 表示无法核实（调用方按 fail-open 不过滤该 provider 的卡片）。
    """
    status: dict[str, str] = {}
    for repo_key in repo_keys:
        if not repo_key:
            continue
        numbers = vcs_issue_numbers(repo_key)
        if numbers is None:
            status[repo_key] = "unavailable"
            continue
        try:
            _apply_existence(repo_key, numbers)
            status[repo_key] = "ok"
        except Exception as exc:
            log.warning(f"issue_vcs_index: {repo_key} refresh failed: {exc}")
            status[repo_key] = "unavailable"
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
        if status.get(repo_key) == "ok":
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
        time.sleep(0.2)
    log.info(f"issue_vcs_index: backfill done {summary}")
    return summary