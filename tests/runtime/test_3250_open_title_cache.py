"""Issue #3250：建单幂等查重的 open 标题索引缓存。

原实现每次都把 `state=open` 全部翻完（330 open = 7 页 4.5s，2000 open ≈ 30s）。
修复后按仓库缓存「标题 → number」索引（TTL 60s，可 env 调）：
- 完整索引里没有该标题 → 直接判「不存在」，不再翻页；
- 新建成单写回索引（remember_open_issue_title）；
- 索引里的标题直接命中。
"""
import pytest

from src.domains.runtime.vcs import api_client as ac


def _issue(i, title=None):
    return {"title": title or f"issue-{i}", "number": i, "state": "open"}


@pytest.fixture(autouse=True)
def _clear_cache():
    ac._open_title_cache.clear()
    yield
    ac._open_title_cache.clear()


def _client(pages, calls):
    c = ac.VcsApiClient("gitcode", "tok", "o", "r")
    c._platform_card = lambda: None   # 强制走分页遍历分支（非 search 平台）

    def _req(_method, path, *a, **kw):
        calls.append(path)
        page = int(path.rsplit("page=", 1)[1])
        return pages.get(page, [])

    c._request = _req
    return c


def test_first_lookup_walks_all_pages_then_cache_answers():
    pages = {1: [_issue(i) for i in range(1, 51)],
             2: [_issue(i) for i in range(51, 81)]}   # 第 2 页不足 50 → 索引完整
    calls = []
    c = _client(pages, calls)

    assert c._find_open_issue_impl("absent", 1000) is None
    assert len(calls) == 2, "首次必须翻到末页才能建立完整索引"

    # 第二次同仓库不同标题：完整索引里没有 → 不翻页，直接判无
    assert c._find_open_issue_impl("absent-2", 1000) is None
    assert len(calls) == 2, "完整索引命中判无后不得再打 VCS"


def test_cached_hit_returns_number_without_calls():
    pages = {1: [_issue(i) for i in range(1, 51)], 2: [_issue(51)]}
    calls = []
    c = _client(pages, calls)
    c._find_open_issue_impl("warm", 1000)
    n_calls = len(calls)

    assert c._find_open_issue_impl("issue-7", 1000) == 7
    assert len(calls) == n_calls, "命中索引不应再翻页"


def test_remember_after_create_makes_next_check_free():
    pages = {1: [_issue(1)]}
    calls = []
    c = _client(pages, calls)
    c._find_open_issue_impl("warm", 1000)     # 建立索引（1 次请求）
    assert len(calls) == 1

    ac.remember_open_issue_title("gitcode", "o", "r", "just-created", 4321)
    assert c._find_open_issue_impl("just-created", 1000) == 4321
    assert len(calls) == 1, "建单写回后，同标题查重必须零请求"


def test_expired_index_refetches(monkeypatch):
    pages = {1: [_issue(1)]}
    calls = []
    c = _client(pages, calls)
    c._find_open_issue_impl("a", 1000)
    assert len(calls) == 1

    monkeypatch.setattr(ac, "_OPEN_TITLE_TTL_SECONDS", 0)
    c._find_open_issue_impl("b", 1000)
    assert len(calls) == 2, "TTL 过期必须重新取 VCS 事实"


def test_early_hit_marks_index_incomplete():
    pages = {1: [_issue(i) for i in range(1, 51)], 2: [_issue(i) for i in range(51, 81)]}
    calls = []
    c = _client(pages, calls)
    assert c._find_open_issue_impl("issue-3", 1000) == 3
    assert len(calls) == 1, "第 1 页就命中，允许早退"

    # 早退留下的索引不完整：另一个不存在的标题必须继续翻页核验，不能直接判无
    assert c._find_open_issue_impl("issue-77", 1000) == 77
    assert len(calls) == 3