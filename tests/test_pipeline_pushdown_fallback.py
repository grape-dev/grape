"""架构审查 M-1：SQL 下推异常时必须回落完整加载，而不是返回空流水线。

下推（_pipeline_window）失败后 `_win_ids` 为空；旧实现无条件 `for iid in _win_ids`，
于是「有数据」直接变「暂无 Issue」（静默清空）。用内存 sqlite 走真实 get_pipeline
代码路径（打桩窗口函数抛错 / 正常返回），断言两条分支的产出。
"""
import contextlib
import os
import sqlite3
import sys

os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from src.api.routes import pipeline as pipeline_route
import src.session_manager as sm

REPO = "gitcode:fallback/repo"

_SCHEMA = [
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, issue_id TEXT, event_type TEXT,
        agent_type TEXT, title TEXT, summary TEXT, status TEXT, created_at TEXT,
        actor TEXT, duration INTEGER, repo_key TEXT, scene_id TEXT)""",
    """CREATE TABLE issue (
        repo_key TEXT, issue_number INTEGER, title TEXT, status TEXT, stage TEXT,
        owner_agent TEXT, assignee TEXT, scene_id TEXT, parent_issue_id TEXT,
        is_subtask_mode INTEGER, user_id TEXT)""",
    """CREATE TABLE tasks (repo_key TEXT, issue_id TEXT)""",
]


def _seed():
    conn = sqlite3.connect(":memory:")
    for ddl in _SCHEMA:
        conn.execute(ddl)
    conn.execute(
        "INSERT INTO issue VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (REPO, 101, "fallback issue", "active", "vod", "vod", "u1", "", None, 0, "u1"))
    conn.execute(
        "INSERT INTO events (issue_id, event_type, agent_type, title, summary, status, "
        "created_at, actor, duration, repo_key, scene_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("#101", "issue_opened", "vod", "fallback issue", "", "done",
         "2026-11-05 10:00:00", "tester", 1, REPO, ""))
    conn.commit()
    return conn


def _patch_common(monkeypatch, conn):
    monkeypatch.setattr(sm, "db", conn)
    monkeypatch.setattr(sm, "db_lock", contextlib.nullcontext())
    monkeypatch.setattr("src.api.deps.check_repo_access", lambda *a, **k: None)


def _ids(out):
    out_ids = []
    active = out.get("active")
    if isinstance(active, dict) and active:
        out_ids.append(active.get("issue_id"))
    for h in out.get("history") or []:
        out_ids.append(h.get("issue_id"))
    return out_ids


def test_pushdown_failure_falls_back_to_full_load(monkeypatch):
    _patch_common(monkeypatch, _seed())

    def _boom(*a, **k):
        raise RuntimeError("pushdown blew up")

    monkeypatch.setattr(pipeline_route, "_pipeline_window", _boom)

    out = pipeline_route.get_pipeline("fallback", "repo", history_limit=50,
                                        history_offset=0, scene_id="", user={"id": "u1"})

    assert "#101" in _ids(out), f"下推失败后不应清空流水线，实际返回: {out!r}"
    assert out.get("active"), "下推失败后应仍有 active 流水线"


def test_pushdown_success_still_uses_window(monkeypatch):
    _patch_common(monkeypatch, _seed())
    seen = {}
    real_window = pipeline_route._pipeline_window

    def _spy(repo_keys, scene_filter, need):
        seen["called"] = True
        return real_window(repo_keys, scene_filter, need)

    monkeypatch.setattr(pipeline_route, "_pipeline_window", _spy)

    out = pipeline_route.get_pipeline("fallback", "repo", history_limit=50,
                                        history_offset=0, scene_id="", user={"id": "u1"})

    assert seen.get("called"), "正常情况下应走下推路径"
    assert _ids(out) == ["#101"]


def _seed_two_scenes():
    """两个场景各一条 issue/事件，用于验证 fallback 的场景收口。"""
    conn = sqlite3.connect(":memory:")
    for ddl in _SCHEMA:
        conn.execute(ddl)
    for num, scene in ((101, "scene-A"), (102, "scene-B")):
        conn.execute(
            "INSERT INTO issue VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (REPO, num, f"issue {num}", "active", "vod", "vod", "u1", scene, None, 0, "u1"))
        conn.execute(
            "INSERT INTO events (issue_id, event_type, agent_type, title, summary, status, "
            "created_at, actor, duration, repo_key, scene_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"#{num}", "issue_opened", "vod", f"issue {num}", "", "done",
             "2026-11-05 10:00:00", "tester", 1, REPO, scene))
    conn.commit()
    return conn


def test_pushdown_failure_still_scopes_to_scene(monkeypatch):
    """下推失败（如 MySQL 5.7 语法错）回落全仓时，场景页不得串出别的场景。"""
    _patch_common(monkeypatch, _seed_two_scenes())
    # 场景配置查询会摸真实 db_lock（本测试用 nullcontext 替换），这里直接打桩
    monkeypatch.setattr("src.api.pipeline_config.get_pipeline_config", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("(1064, 'You have an error in your SQL syntax')")

    monkeypatch.setattr(pipeline_route, "_pipeline_window", _boom)

    out = pipeline_route.get_pipeline("fallback", "repo", history_limit=10,
                                      history_offset=0, scene_id="scene-A", user={"id": "u1"})

    assert _ids(out) == ["#101"], f"fallback 应按场景收口，实际返回: {out!r}"
    assert out.get("history_total") == 0
    for row in ([out["active"]] if out.get("active") else []) + (out.get("history") or []):
        assert row.get("scene_id") == "scene-A"