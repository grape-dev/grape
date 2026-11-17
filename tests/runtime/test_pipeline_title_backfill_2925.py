"""Issue #2925：`/api/pipeline` 标题兜底的有界收敛回归。

覆盖三层收敛：
  - 口径：占位判定单一事实源（兜底触发 == 写回判定）；
  - 范围：只兜底当前页窗口，窗口外不触发外呼；
  - 状态：只写真实标题，失败落已尝试标记（持久列 / 进程内降级），热请求 0 外呼；
  - 执行：token/client 单次复用 + 有限并发 + 总 deadline 可收敛。
"""
import sys
import os
import re
import time

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db"))

from unittest.mock import patch, MagicMock

import pytest

import src.domains.runtime.routes.pipeline as pipeline_mod
import src.domains.runtime.pipeline.event_pipeline as ep_mod


class _FakeCursor:
    """最小可用的 issue 表假游标：解析 WHERE，支持标记读取 + title/title_fetched_at 写回。

    Issue #2925 review：此前忽略 SQL WHERE，导致「跨仓同号不串仓」「并发 title=? 护栏」
    「窗口外不写回」等断言形同虚设。现在按 SQL 里的 repo_key/issue_number 等值条件
    与 `(title='' OR title IS NULL OR title=?)` 护栏语义执行，并维护 rowcount。

    issues 支持两种键：
      - `{num: {...}}`：全部归属默认 repo_key（旧用例）；
      - `{(repo_key, num): {...}}`：多仓同号场景。
    """

    def __init__(self, issues, repo_key="gitcode:o/r"):
        self.issues = issues
        self.repo_key = repo_key
        self.rows = []
        self.updates = []
        self.rowcount = -1

    def _get(self, rk, num):
        if (rk, num) in self.issues:
            return self.issues[(rk, num)]
        if rk == self.repo_key and num in self.issues:
            return self.issues[num]
        return None

    def _all_rows(self):
        for k, v in self.issues.items():
            if isinstance(k, tuple):
                yield k[0], k[1], v
            else:
                yield self.repo_key, k, v

    def execute(self, sql, params=()):
        s = " ".join(str(sql).split())
        self.rowcount = -1
        if s.startswith("SELECT repo_key, issue_number, title_fetched_at"):
            m = re.search(r"repo_key IN \(([^)]*)\) AND issue_number IN \(([^)]*)\)", s)
            rk_n = m.group(1).count("?")
            num_n = m.group(2).count("?")
            repo_keys = set(params[:rk_n])
            nums = set(params[rk_n:rk_n + num_n])
            self.rows = [(rk, num, v.get("marker", ""))
                         for rk, num, v in self._all_rows()
                         if rk in repo_keys and num in nums]
            return self
        if s.startswith("UPDATE issue SET"):
            self.updates.append((s, params))
            _set, _where = s.split("WHERE", 1)
            _rk = params[-3]
            _num = params[-2]
            row = None
            if "repo_key=?" in _where and "issue_number=?" in _where:
                row = self._get(_rk, _num)
            if row is None:
                self.rowcount = 0
                return self
            # 并发护栏：仅当库里仍是空 / 本次观测到的占位值时才覆盖。
            if "(title='' OR title IS NULL OR title=?)" in _where:
                _old = params[-1]
                _cur = row.get("title", "")
                if not (_cur in ("", None) or _cur == _old):
                    self.rowcount = 0
                    return self
            if "SET title=?" in _set and "title_fetched_at=?" in _set:
                row["title"] = params[0]
                row["marker"] = params[1]
            elif "SET title=?" in _set:
                row["title"] = params[0]
            elif "SET title_fetched_at=?" in _set:
                row["marker"] = params[0]
            self.rowcount = 1
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
    """假 batch 需同步 stats.attempted / attempted_ids，模拟「确实开始执行」。"""
    _st = kw.get("stats")
    if isinstance(_st, dict):
        _st["attempted"] = len(ids)
        _st["attempted_ids"] = set(ids)
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
    """列表聚合路径不再逐条外呼（API 兜底已移到窗口批量层）。

    review：旧实现用 `except Exception: pass` 包裹逐条外呼，抛 AssertionError 会被
    吞掉、基线也通过。这里改为记录调用列表并断言为空，锁住「调用被删除」。
    """
    calls = []

    def _record(*a, **kw):
        calls.append((a, kw))
        return ""

    monkeypatch.setattr(ep_mod, "_fetch_issue_title_via_api", _record)
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
    assert calls == []


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

# ===== QA review round 2：空 title_map / 跨仓同号 / 并发护栏 / 共享 deadline / 窗口接线 =====


def test_empty_title_map_still_backfills(monkeypatch):
    """issue 表批量读取失败（title_map 为空）时仍按 issue_ids 构造候选并兜底。

    review [中]：原实现空 map 直接 return，而列表路径已删除逐条 API 兜底 →
    标题永久占位。空 map 时候选 _cur 视为 '' 继续走窗口批量。
    """
    issues = {1: {"title": "", "marker": ""}}
    _setup_db(monkeypatch, issues)
    calls = []

    def _fake_batch(repo_key, ids, **kw):
        calls.append(list(ids))
        _mark_attempted(kw, ids)
        return {"#1": "真实标题"}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {}, ["#1"], {}, window_size=1)

    assert calls == [["#1"]]
    assert out == {"#1": "真实标题"}
    assert issues[1]["title"] == "真实标题"

    # 第二次请求仍拿不到 issue 表批量结果（title_map 为空）→ 命中已尝试标记，0 外呼
    out2 = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {}, ["#1"], {}, window_size=1)
    assert calls == [["#1"]]
    assert out2.get("#1", "") == ""


def test_cross_repo_same_number_only_hits_matching_repo(monkeypatch):
    """多仓同号：候选以事件行 repo_key 为准，只回写命中的仓库。"""
    issues = {
        ("gitcode:o/r", 7): {"title": "issue_opened", "marker": ""},
        ("gitcode:o/r2", 7): {"title": "issue_opened", "marker": ""},
    }
    _setup_db(monkeypatch, issues)
    calls = []

    def _fake_batch(repo_key, ids, **kw):
        calls.append((repo_key, list(ids)))
        _mark_attempted(kw, ids)
        return {"#7": "r2 真实标题"}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    events_by_issue = {"#7": [{"repo_key": "gitcode:o/r2", "title": "issue_opened"}]}
    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r", "gitcode:o/r2"],
        {"#7": "issue_opened"}, ["#7"], events_by_issue, window_size=1)

    assert calls == [("gitcode:o/r2", ["#7"])]
    assert out["#7"] == "r2 真实标题"
    assert issues[("gitcode:o/r2", 7)]["title"] == "r2 真实标题"
    assert issues[("gitcode:o/r", 7)]["title"] == "issue_opened"
    assert issues[("gitcode:o/r", 7)]["marker"] == ""


def test_concurrent_real_title_guard(monkeypatch):
    """并发护栏：写回前库里已被并发写入真实标题 → title=? 护栏拒绝覆盖。"""
    issues = {1: {"title": "别人写入的真实标题", "marker": ""}}
    _setup_db(monkeypatch, issues)

    def _fake_batch(repo_key, ids, **kw):
        _mark_attempted(kw, ids)
        return {"#1": "API 标题"}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": "issue_opened"}, ["#1"], {}, 1)

    assert issues[1]["title"] == "别人写入的真实标题"
    assert issues[1]["marker"] == ""
    # 本次响应仍可用 API 拿到的标题（只影响展示，不改库）
    assert out["#1"] == "API 标题"


def test_shared_deadline_across_repo_groups(monkeypatch):
    """请求级共享总 deadline：第二个 repo 组拿到的是剩余预算，而非再扣一份。"""
    issues = {
        ("gitcode:a/x", 1): {"title": "", "marker": ""},
        ("gitcode:b/y", 2): {"title": "", "marker": ""},
    }
    _setup_db(monkeypatch, issues)
    monkeypatch.setattr(ep_mod, "_title_fetch_deadline", lambda: 0.5)
    deadlines = []

    def _fake_batch(repo_key, ids, **kw):
        deadlines.append(kw.get("deadline_seconds"))
        time.sleep(0.2)
        _mark_attempted(kw, ids)
        return {iid: f"T{iid}" for iid in ids}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    events_by_issue = {
        "#1": [{"repo_key": "gitcode:a/x", "title": ""}],
        "#2": [{"repo_key": "gitcode:b/y", "title": ""}],
    }
    pipeline_mod._backfill_window_titles(
        "gitcode:a/x", ["gitcode:a/x", "gitcode:b/y"], {"#2": "", "#1": ""},
        ["#1", "#2"], events_by_issue, window_size=2)

    assert len(deadlines) == 2
    assert deadlines[0] <= 0.5
    assert deadlines[1] < deadlines[0]


def test_get_pipeline_window_size_wiring(monkeypatch):
    """get_pipeline 把 `1 + history_offset + history_limit` 接到窗口兜底。"""
    captured = {}

    def _cap(repo_key, repo_keys, title_map, issue_ids,
             events_by_issue=None, window_size=0):
        captured["window_size"] = window_size
        captured["issue_ids"] = list(issue_ids)
        return title_map

    monkeypatch.setattr(pipeline_mod, "_backfill_window_titles", _cap)
    monkeypatch.setattr(pipeline_mod, "_pipeline_window",
                        lambda repo_keys, scene, need: (["#1"], 1, set()))
    import src.api.deps as deps
    monkeypatch.setattr(deps, "check_repo_access", lambda user, repo_key: None)

    resp = pipeline_mod.get_pipeline(
        owner="o", repo="r", history_limit=3, history_offset=2,
        scene_id="", user={"id": 1, "username": "tester"})

    assert captured["window_size"] == 1 + 2 + 3
    assert captured["issue_ids"] == ["#1"]
    assert isinstance(resp, dict)


# ===== QA review round 3：无行降级 / 取消任务不标记 / token 计入 deadline / 最新仓 / 分片 =====


def test_events_without_issue_row_falls_back_to_process_cache(monkeypatch):
    """events 有记录但 issue 表无行：持久标记写 0 行 → 降级进程负缓存，第二次 0 外呼。"""
    db = _setup_db(monkeypatch, {}, marker_ok=True)
    calls = []

    def _fake_batch(repo_key, ids, **kw):
        calls.append(list(ids))
        _mark_attempted(kw, ids)
        return {}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    events_by_issue = {"#7": [{"repo_key": "gitcode:o/r", "title": "issue_opened"}]}

    pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#7": ""}, ["#7"], events_by_issue, 1)
    assert calls == [["#7"]]
    assert db.cur.updates and all(u[0].startswith("UPDATE issue") for u in db.cur.updates)
    # 持久标记命中 0 行 → 必须落进程内负缓存兜底
    assert ("gitcode:o/r", 7) in pipeline_mod._TITLE_NEG_CACHE

    out2 = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#7": ""}, ["#7"], events_by_issue, 1)
    assert calls == [["#7"]]
    assert out2["#7"] == ""


def test_no_token_and_no_marker_column_does_not_poison(monkeypatch):
    """无标记列 + 无 token 组合：不得落进程负缓存，每个请求都应可重试。"""
    _setup_db(monkeypatch, {1: {"title": "", "marker": ""}}, marker_ok=False)
    tok_calls = []

    def _no_token(rk):
        tok_calls.append(rk)
        return ""

    monkeypatch.setattr(ep_mod, "_resolve_issue_read_token", _no_token)
    pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": ""}, ["#1"], {}, 1)
    pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": ""}, ["#1"], {}, 1)

    assert tok_calls == ["gitcode:o/r", "gitcode:o/r"]
    assert pipeline_mod._TITLE_NEG_CACHE == {}


def test_latest_event_repo_key_wins_for_duplicate_numbers(monkeypatch):
    """多仓同号：events 为 seq ASC，应取最新事件（末尾）的 repo_key 回写。"""
    issues = {
        ("gitcode:o/r", 7): {"title": "issue_opened", "marker": ""},
        ("gitcode:o/r2", 7): {"title": "issue_opened", "marker": ""},
    }
    _setup_db(monkeypatch, issues)
    calls = []

    def _fake_batch(repo_key, ids, **kw):
        calls.append((repo_key, list(ids)))
        _mark_attempted(kw, ids)
        return {"#7": "r2 最新标题"}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    events_by_issue = {"#7": [
        {"repo_key": "gitcode:o/r", "title": "issue_opened"},
        {"repo_key": "gitcode:o/r2", "title": "issue_opened"},
    ]}
    out = pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r", "gitcode:o/r2"],
        {"#7": "issue_opened"}, ["#7"], events_by_issue, 1)

    assert calls == [("gitcode:o/r2", ["#7"])]
    assert out["#7"] == "r2 最新标题"
    assert issues[("gitcode:o/r2", 7)]["title"] == "r2 最新标题"
    assert issues[("gitcode:o/r", 7)]["title"] == "issue_opened"


def test_cancelled_futures_not_marked_as_attempted(monkeypatch):
    """排队后被取消（从未发请求）的候选不得落已尝试标记。"""
    issues = {n: {"title": "", "marker": ""} for n in (1, 2, 3)}
    _setup_db(monkeypatch, issues)

    def _fake_batch(repo_key, ids, **kw):
        st = kw.get("stats")
        if isinstance(st, dict):
            st["attempted"] = 1
            st["attempted_ids"] = {ids[0]}
        return {}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], {"#1": "", "#2": "", "#3": ""},
        ["#1", "#2", "#3"], {}, 3)

    assert issues[1]["marker"] != ""
    assert issues[2]["marker"] == ""
    assert issues[3]["marker"] == ""


def test_batch_attempted_counts_started_only(monkeypatch):
    """attempted 只统计真正开始执行的任务，排队被取消的不算。"""
    monkeypatch.setattr(ep_mod, "_resolve_issue_read_token", lambda rk: "tok")
    monkeypatch.setattr(ep_mod, "_build_issue_read_client",
                        lambda *a: _fake_client(delay=0.6))
    stats = {}
    ep_mod.fetch_issue_titles_batch(
        "gitcode:o/r", ["#1", "#2", "#3"], max_workers=1,
        deadline_seconds=0.3, timeout=0.3, stats=stats)
    assert stats["attempted"] == 1
    assert len(stats["attempted_ids"]) == 1


def test_batch_deadline_includes_token_resolution(monkeypatch):
    """token 解析耗时计入总 deadline：解析超预算后不再构建客户端/发起请求。"""
    def _slow_token(rk):
        time.sleep(0.2)
        return "tok"

    monkeypatch.setattr(ep_mod, "_resolve_issue_read_token", _slow_token)
    client = _fake_client(titles={1: "T1"})
    built = []
    monkeypatch.setattr(ep_mod, "_build_issue_read_client",
                        lambda *a: built.append(1) or client)
    stats = {}
    started = time.monotonic()
    out = ep_mod.fetch_issue_titles_batch(
        "gitcode:o/r", ["#1"], max_workers=1,
        deadline_seconds=0.1, timeout=0.1, stats=stats)
    elapsed = time.monotonic() - started

    assert out == {}
    assert stats["attempted"] == 0
    assert built == []
    assert client.calls == []
    assert elapsed < 0.5, elapsed


def test_marker_read_chunks_large_window(monkeypatch):
    """标记读取按分片执行：大窗口不把所有 issue_number 塞进一条 IN。"""
    n = 450
    issues = {i: {"title": "", "marker": ""} for i in range(1, n + 1)}
    db = _setup_db(monkeypatch, issues)
    select_sizes = []
    _orig_execute = db.cur.execute

    def _exec(sql, params=()):
        s = " ".join(str(sql).split())
        if s.startswith("SELECT repo_key, issue_number, title_fetched_at"):
            m = re.search(r"issue_number IN \(([^)]*)\)", s)
            select_sizes.append(m.group(1).count("?"))
        return _orig_execute(sql, params)

    monkeypatch.setattr(db.cur, "execute", _exec)

    def _fake_batch(repo_key, ids, **kw):
        _mark_attempted(kw, ids)
        return {}

    monkeypatch.setattr(ep_mod, "fetch_issue_titles_batch", _fake_batch)
    ids = [f"#{i}" for i in range(1, n + 1)]
    title_map = {iid: "" for iid in ids}
    events_by_issue = {iid: [{"repo_key": "gitcode:o/r", "title": "issue_opened"}]
                       for iid in ids}
    pipeline_mod._backfill_window_titles(
        "gitcode:o/r", ["gitcode:o/r"], title_map, ids, events_by_issue, n)

    assert select_sizes == [200, 200, 50]
