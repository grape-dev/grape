"""Issue #2925：`/api/pipeline` 标题兜底的有界收敛回归。

覆盖三层收敛：
  - 口径：占位判定单一事实源（兜底触发 == 写回判定）；
  - 范围：只兜底当前页窗口，窗口外不触发外呼；
  - 状态：只写真实标题，失败落已尝试标记（持久列 / 进程内降级），热请求 0 外呼；
  - 执行：token/client 单次复用 + 有限并发 + 总 deadline 可收敛。
"""
import sys
import os
import time

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db"))

from unittest.mock import patch, MagicMock

import pytest

import src.domains.runtime.routes.pipeline as pipeline_mod
import src.domains.runtime.pipeline.event_pipeline as ep_mod


class _FakeCursor:
    """最小可用的 issue 表假游标：支持标记读取 + title/title_fetched_at 写回。"""

    def __init__(self, issues, repo_key="gitcode:o/r"):
        self.issues = issues          # {num: {"title": str, "marker": str}}
        self.repo_key = repo_key
        self.rows = []
        self.updates = []

    def execute(self, sql, params=()):
        s = " ".join(str(sql).split())
        if s.startswith("SELECT repo_key, issue_number, title_fetched_at"):
            self.rows = [(self.repo_key, n, v.get("marker", ""))
                         for n, v in self.issues.items()]
            return self
        if s.startswith("UPDATE issue SET"):
            self.updates.append((s, params))
            _set = s.split("WHERE")[0]
            _num = params[-2]
            if _num not in self.issues:
                return self
            if "SET title=?" in _set and "title_fetched_at=?" in _set:
                self.issues[_num]["title"] = params[0]
                self.issues[_num]["marker"] = params[1]
            elif "SET title=?" in _set:
                self.issues[_num]["title"] = params[0]
            elif "SET title_fetched_at=?" in _set:
                self.issues[_num]["marker"] = params[0]
            return self
        return self

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass


class _FakeDb:
    def __init__(self, issues):
        self.cur = _FakeCursor(issues)
        self.commits = 0

    def cursor(self, *a, **kw):
        return self.cur

    def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _reset_process_cache():
    pipeline_mod._TITLE_NEG_CACHE.clear()
    yield
    pipeline_mod._TITLE_NEG_CACHE.clear()


def _mark_attempted(kw, ids):
    """假 batch 需同步 stats.attempted，模拟「确实提交过上游请求」。"""
    _st = kw.get("stats")
    if isinstance(_st, dict):
        _st["attempted"] = len(ids)
    return _st


def _setup_db(monkeypatch, issues, marker_ok=True):
    db = _FakeDb(issues)
    monkeypatch.setattr(pipeline_mod, "_rt_db", db)
    monkeypatch.setattr(
        "src.domains.runtime.schema_probe.column_exists",
        lambda c, t, col: marker_ok)
    return db


def test_placeholder_single_source():
    """占位判定：空 / 事件类型名 / A2A 前缀为占位；真实标题不是。"""
    assert pipeline_mod._is_placeholder_title("")
    assert pipeline_mod._is_placeholder_title("issue_opened")
    assert pipeline_mod._is_placeholder_title("A2A: coding")
    assert pipeline_mod._is_placeholder_title("A2A (订阅): coding")
    assert pipeline_mod._is_placeholder_title("PR 订阅 QA: 1")
    assert not pipeline_mod._is_placeholder_title("修复流水线标题 N+1")


def test_window_only_backfill_and_writeback(monkeypatch):
    """只对窗口内 issue 外呼，且只写真实标题。"""
    issues = {n: {"title": "issue_opened", "marker": ""} for n in (1, 2, 3, 4, 5)}
    db = _setup_db(monkeypatch, issues)
    title_map = {"#1": "issue_opened", "#2": "issue_opened", "#3": "issue_opened",
                 "#4": "issue_opened", "#5": "issue_opened"}
    calls = []

    def _fake_batch(repo_key, ids, **kw):
        calls.append(list(ids))
        _mark_attempted(kw, ids)
        return {iid: f"真实标题 {iid}" for iid in ids}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], title_map, ["#1", "#2", "#3", "#4", "#5"],
        events_by_issue={}, window_size=3)

    assert calls == [["#1", "#2", "#3"]]
    assert out["#1"] == "真实标题 #1" and out["#3"] == "真实标题 #3"
    # 窗口外保持占位，且未被外呼/写回
    assert out["#4"] == "issue_opened" and out["#5"] == "issue_opened"
    assert set(db.cur.issues) == {1, 2, 3, 4, 5}
    assert db.cur.issues[1]["title"] == "真实标题 #1"
    assert db.cur.issues[4]["title"] == "issue_opened"
    assert db.cur.issues[4]["marker"] == ""
    assert db.commits == 1


def test_placeholder_result_not_written_but_marked(monkeypatch):
    """上游返回仍是占位值时不写 title，只落已尝试标记（同口径护栏）。"""
    issues = {1: {"title": "", "marker": ""}}
    db = _setup_db(monkeypatch, issues)
    title_map = {"#1": ""}
    def _fake_batch(repo_key, ids, **kw):
        _mark_attempted(kw, ids)
        return {"#1": "issue_opened"}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)

    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], title_map, ["#1"], {}, window_size=1)

    assert out["#1"] == ""
    assert issues[1]["title"] == ""
    assert issues[1]["marker"] != ""


def test_real_title_not_overwritten(monkeypatch):
    """库里已是真实标题时不回写、不外呼（口径一致：非占位即命中）。"""
    issues = {1: {"title": "真实标题", "marker": ""}}
    _setup_db(monkeypatch, issues)
    called = []
    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch",
                        lambda *a, **kw: called.append(1) or {})
    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": "真实标题"}, ["#1"], {}, 1)
    assert out["#1"] == "真实标题"
    assert called == []
    assert issues[1]["title"] == "真实标题"


def test_fresh_marker_skips_vcs(monkeypatch):
    """title_fetched_at 在 TTL 内 → 热请求 0 次外呼。"""
    issues = {1: {"title": "", "marker": str(int(time.time()))}}
    _setup_db(monkeypatch, issues)
    called = []
    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch",
                        lambda *a, **kw: called.append(1) or {})
    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": ""}, ["#1"], {}, 1)
    assert out["#1"] == ""
    assert called == []


def test_expired_marker_retries(monkeypatch):
    """标记超过 TTL → 允许重试（避免上游改标题后永久占位）。"""
    _setup_db(monkeypatch, {1: {"title": "", "marker": "1"}})
    called = []
    def _fake_batch(repo_key, ids, **kw):
        called.append(ids)
        _mark_attempted(kw, ids)
        return {"#1": "新标题"}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": ""}, ["#1"], {}, 1)
    assert called == [["#1"]]
    assert out["#1"] == "新标题"


def test_no_marker_column_uses_process_cache(monkeypatch):
    """无 title_fetched_at 列时降级为进程内负缓存：第二次请求 0 外呼。"""
    _setup_db(monkeypatch, {1: {"title": "", "marker": ""}}, marker_ok=False)
    calls = []
    def _fake_batch(repo_key, ids, **kw):
        calls.append(ids)
        _mark_attempted(kw, ids)
        return {}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)

    pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": ""}, ["#1"], {}, 1)
    assert calls == [["#1"]]

    # 第二次同仓同号：负缓存命中，不再外呼
    pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": ""}, ["#1"], {}, 1)
    assert calls == [["#1"]]


def test_no_token_does_not_poison_negative_cache(monkeypatch):
    """无 token 时不得落已尝试标记：临时凭据问题不能导致 6h 内不再重试。"""
    issues = {1: {"title": "", "marker": ""}}
    _setup_db(monkeypatch, issues)
    monkeypatch.setattr(ep_mod, "_resolve_issue_read_token", lambda rk: "")
    pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": ""}, ["#1"], {}, 1)
    assert issues[1]["marker"] == ""
    assert pipeline_mod._TITLE_NEG_CACHE == {}


def test_get_issue_pipeline_no_serial_vcs_call(monkeypatch):
    """列表聚合路径不再逐条外呼（API 兜底已移到窗口批量层）。"""
    def _boom(*a, **kw):
        raise AssertionError("逐条 VCS 调用不应出现在 _get_issue_pipeline")

    monkeypatch.setattr(ep_mod, "_fetch_issue_title_via_api", _boom)
    with patch("src.domains.mgmt.user.db.get_all_bot_usernames", return_value=[]), \
         patch("src.domains.runtime.pipeline.event_pipeline.get_agent_display_name",
               side_effect=lambda a: a):
        events = [{"seq": 1, "issue_id": "#1", "event_type": "issue_opened",
                   "agent_type": "issue", "title": "issue_opened",
                   "status": "completed", "actor": "u",
                   "created_at": "2026-08-04 10:00:00", "payload": ""}]
        p = pipeline_mod._get_issue_pipeline(
            "gitcode:o/r", "#1", events, {"#1": "issue_opened"})
    assert p is not None
    assert p["title"] == "issue_opened"


def _fake_client(delay=0.0, titles=None):
    class _C:
        def __init__(self):
            self.calls = []

        def get_issue(self, num, timeout=30):
            self.calls.append(num)
            if delay:
                time.sleep(delay)
            title = (titles or {}).get(num)
            return {"title": title} if title else {"error": "x"}

    return _C()


def test_batch_resolves_token_and_client_once(monkeypatch):
    """token/client 单次请求内只解析/构建一次，并发拉取全部标题。"""
    tok_calls = []
    built = []
    client = _fake_client(titles={1: "T1", 2: "T2", 3: "T3"})

    monkeypatch.setattr(ep_mod, "_resolve_issue_read_token",
                        lambda rk: tok_calls.append(rk) or "tok")
    monkeypatch.setattr(ep_mod, "_build_issue_read_client",
                        lambda rk, tok: built.append((rk, tok)) or client)

    stats = {}
    out = ep_mod.fetch_issue_titles_batch(
        "gitcode:o/r", ["#1", "#2", "#3", "#pr_9_x"],
        max_workers=3, deadline_seconds=2, timeout=1, stats=stats)
    assert out == {"#1": "T1", "#2": "T2", "#3": "T3"}
    assert stats["attempted"] == 3
    assert tok_calls == ["gitcode:o/r"]
    assert built == [("gitcode:o/r", "tok")]
    assert sorted(client.calls) == [1, 2, 3]


def test_batch_no_token_returns_empty(monkeypatch):
    monkeypatch.setattr(ep_mod, "_resolve_issue_read_token", lambda rk: "")
    built = []
    monkeypatch.setattr(ep_mod, "_build_issue_read_client",
                        lambda *a: built.append(1) or None)
    assert ep_mod.fetch_issue_titles_batch("gitcode:o/r", ["#1"]) == {}
    assert built == []


def test_batch_deadline_bounds_elapsed(monkeypatch):
    """总 deadline 到点返回，慢请求不阻塞（部分结果可用）。"""
    monkeypatch.setattr(ep_mod, "_resolve_issue_read_token", lambda rk: "tok")
    monkeypatch.setattr(ep_mod, "_build_issue_read_client",
                        lambda *a: _fake_client(delay=0.2))

    ids = [f"#{n}" for n in range(1, 21)]
    started = time.monotonic()
    out = ep_mod.fetch_issue_titles_batch(
        "gitcode:o/r", ids, max_workers=2, deadline_seconds=0.25, timeout=0.25)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, elapsed
    assert isinstance(out, dict)