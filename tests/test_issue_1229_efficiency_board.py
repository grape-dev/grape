"""Issue #1229: 能效看板聚合端点测试。

钉死五条容易回归的约定：
1. **空数据空态** —— 无 issue/无用量时 contributors 为空数组，不 500、不造数据。
2. **提出/完成口径** —— issue 单表（C3 权威）created_by 空回退 assignee；
   完成 = status='completed'；子任务行（is_subtask_mode=1）不计入。
3. **bot 排除** —— bot 用户名不是协作者（bot 是执行方不是需求方）。
4. **token 归属链** —— obs_llm_usage(run_kind='task') → agent_tasks.issue_id
   → issue 创建人；total = input+output+cache_read+cache_write（不含 reasoning，
   与 src/observability/tokens.py 同口径）；孤儿任务（无 issue 行/bot 的 issue）丢弃。
5. **仓库隔离 + 鉴权** —— 只统计 repo 参数指定仓库；匿名请求 401。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = "gitcode:org/repo"
REPO_OTHER = "gitcode:other/repo2"
BOT = "grape_bot"


def _db():
    from src.session_manager import db
    return db


def _exec(sql, params=()):
    from src.session_manager import db_lock
    with db_lock:
        c = _db().cursor()
        c.execute(sql, tuple(params))
        _db().commit()
        return c


def _cleanup():
    _exec("DELETE FROM issue WHERE repo_key IN (?,?)", (REPO, REPO_OTHER))
    _exec(
        "DELETE FROM obs_llm_usage WHERE repo_key IN (?,?)", (REPO, REPO_OTHER))
    _exec("DELETE FROM agent_tasks WHERE repo_key IN (?,?)", (REPO, REPO_OTHER))


@pytest.fixture(autouse=True)
def _clean_board_tables():
    _cleanup()
    _grant_repo_access()
    yield
    _cleanup()


def _seed_issue(repo_key, number, creator="", assignee="", status="active",
                is_subtask=0):
    _exec(
        "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title,"
        " status, is_subtask_mode, created_by) VALUES (0,?,?,?,'t',?,?,?)",
        (assignee, repo_key, number, status, is_subtask, creator))


def _grant_repo_access(user_id=122901):
    """给测试用户注册仓库可见性（check_repo_access 需要 managed_repos + user_repos）。"""
    from src.user import db as user_db
    for rk, base in (("gitcode:org/repo", ("org", "repo")),
                     ("gitcode:other/repo2", ("other", "repo2"))):
        _exec("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
        user_db.add_managed_repo(rk, base[0], base[1])
        _exec("DELETE FROM user_repos WHERE user_id=? AND repo_key=?",
              (user_id, rk))
        user_db.mark_user_repo_join(user_id, rk)


def _seed_task_task_usage(task_id, repo_key, issue_number, usage,
                          run_kind="task"):
    """种 agent_tasks 行 + obs_llm_usage 行（归属反查链的输入）。"""
    _exec(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt,"
        " repo_key, agent_type, issue_id, status) VALUES"
        " (?,'sess',7,'p',?,'coding',?,'completed')",
        (task_id, repo_key, f"#{issue_number}"))
    lu = __import__(
        "src.observability.llm_usage", fromlist=["record_usage"]).record_usage
    lu(task_id, [usage], source="hermes")


def _client():
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    user = {"id": 122901, "username": "board-user", "org_id": 0,
            "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    return TestClient(app), app, require_org_member


# ------------------------------------------------------------- 切片 A：契约

def test_t1_endpoint_registered_not_404():
    """T1: 路由已注册（端点存在性契约，实现前应 404 变红）。"""
    cli, app, _dep = _client()
    try:
        r = cli.get("/api/dashboard/efficiency", params={"repo": "org/repo"})
        assert r.status_code != 404, \
            f"endpoint not registered: {r.status_code} {r.text[:200]}"
    finally:
        app.dependency_overrides.pop(_dep, None)


def test_t2_empty_data_returns_empty_contributors():
    """T2: 空库 → 空态数据形态（contributors=[]，计数 0），不 500。"""
    cli, app, _dep = _client()
    try:
        r = cli.get("/api/dashboard/efficiency", params={"repo": "org/repo"})
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body.get("contributors") == []
        assert body.get("total_issues") == 0
        assert body.get("completed_issues") == 0
        assert body.get("total_tokens") == 0
        assert body.get("repo") == REPO
    finally:
        app.dependency_overrides.pop(_dep, None)


# ------------------------------------------------------- 切片 B：聚合正确性

def test_t3_issue_counts_per_creator_with_assignee_fallback():
    """T3: 提出数按创建人聚合；created_by 空回退 assignee；完成数只数 completed。"""
    _seed_issue(REPO, 101, creator="alice")
    _seed_issue(REPO, 102, creator="alice", status="completed")
    _seed_issue(REPO, 103, creator="", assignee="bob")          # 回退 assignee
    _seed_issue(REPO, 104, creator="bob", status="completed")
    _seed_issue(REPO, 105, creator="bob", status="completed")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        by = {c["user"]: c for c in body["contributors"]}
        assert by["alice"]["issues"] == 2 and by["alice"]["completed"] == 1
        assert by["bob"]["issues"] == 3 and by["bob"]["completed"] == 2
        assert body["total_issues"] == 5 and body["completed_issues"] == 3
    finally:
        app.dependency_overrides.pop(_dep, None)


def test_t4_sorted_by_issues_desc_and_limit():
    """T4: 按 issues 降序排序；limit 截断生效。"""
    _seed_issue(REPO, 201, creator="alice")
    for i in range(3):
        _seed_issue(REPO, 202 + i, creator="bob")
    _seed_issue(REPO, 205, creator="carol")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo", "limit": 2}).json()
        users = [c["user"] for c in body["contributors"]]
        assert users == ["bob", "alice"], users    # 3 > 1 > 1，alice 先于 carol
    finally:
        app.dependency_overrides.pop(_dep, None)


# ---------------------------------------------------- 切片 C：token 归属链

def test_t5_tokens_attributed_via_task_issue_creator_chain():
    """T5: 用量经 task→issue 创建人归属求和；total 不含 reasoning。"""
    _seed_issue(REPO, 301, creator="alice")
    # alice 的 issue 301：两个任务，input+output+cache_read+cache_write=100+20+10+5=135
    _seed_task_task_usage("t_1229_a1", REPO, 301,
                          {"model": "glm-5.3", "input": 60, "output": 10,
                           "turn": 0})
    _seed_task_task_usage("t_1229_a2", REPO, 301,
                          {"model": "glm-5.3", "input": 40, "output": 10,
                           "cache_read": 10, "cache_write": 5, "reasoning": 999,
                           "turn": 1})
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        by = {c["user"]: c for c in body["contributors"]}
        # total = input+output+cache_read+cache_write（不含 reasoning，同 tokens.py）
        assert by["alice"]["total_tokens"] == 135, by.get("alice")
        assert by["alice"]["input_tokens"] == 100
        # output_tokens 列只含纯 output（cache 计入 total 但不混入 output 列）
        assert by["alice"]["output_tokens"] == 20
        assert body["total_tokens"] == 135
    finally:
        app.dependency_overrides.pop(_dep, None)


def test_t5b_orphan_task_usage_dropped():
    """T5b: 无 issue 行的孤儿任务用量不归属任何人（丢弃，不挂到别的协作者）。"""
    _seed_issue(REPO, 302, creator="alice")
    _seed_task_task_usage("t_1229_orphan", REPO, 999,
                          {"model": "glm-5.3", "input": 50, "output": 5,
                           "turn": 0})
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        by = {c["user"]: c for c in body["contributors"]}
        assert by["alice"]["total_tokens"] == 0
        assert body["total_tokens"] == 0
    finally:
        app.dependency_overrides.pop(_dep, None)


# ---------------------------------------------------- 切片 D：边界与安全

def test_t6_bot_and_subtasks_excluded():
    """T6: bot 用户名与子任务行不计入协作者统计。

    patch get_all_bot_usernames 保证确定性（bot 列表由环境配置决定，
    与 tests/test_bot_opened_recommend.py 同先例）。
    """
    from unittest.mock import patch
    _seed_issue(REPO, 401, creator=BOT)                    # bot 创建 → 排除
    _seed_issue(REPO, 402, creator="alice", is_subtask=1)  # 子任务 → 排除
    _seed_issue(REPO, 403, creator="alice")
    with patch("src.user.db.get_all_bot_usernames", return_value=[BOT]):
        cli, app, _dep = _client()
        try:
            body = cli.get("/api/dashboard/efficiency",
                           params={"repo": "org/repo"}).json()
        finally:
            app.dependency_overrides.pop(_dep, None)
    users = [c["user"] for c in body["contributors"]]
    assert users == ["alice"], users
    assert body["total_issues"] == 1


def test_t6b_bot_issue_tokens_dropped():
    """T6b: bot 创建的 issue，其任务用量不归属任何人。"""
    from unittest.mock import patch
    _seed_issue(REPO, 404, creator=BOT)
    _seed_task_task_usage("t_1229_bot", REPO, 404,
                          {"model": "glm-5.3", "input": 80, "output": 8,
                           "turn": 0})
    with patch("src.user.db.get_all_bot_usernames", return_value=[BOT]):
        cli, app, _dep = _client()
        try:
            body = cli.get("/api/dashboard/efficiency",
                           params={"repo": "org/repo"}).json()
        finally:
            app.dependency_overrides.pop(_dep, None)
    assert body["contributors"] == []
    assert body["total_tokens"] == 0


def test_t7_missing_repo_param_returns_empty():
    """T7: 未指定 repo → 空态（看板是仓库维度视图，不做全库聚合）。"""
    _seed_issue(REPO, 405, creator="alice")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency").json()
        assert body["contributors"] == [] and body["repo"] == ""
    finally:
        app.dependency_overrides.pop(_dep, None)


def test_t8_anonymous_request_401():
    """T8: 匿名（无 Authorization）请求返回 401，不泄露业务数据。"""
    from fastapi.testclient import TestClient
    from src.api.main import app
    with TestClient(app) as anon_cli:
        r = anon_cli.get("/api/dashboard/efficiency",
                         params={"repo": "org/repo"})
        assert r.status_code == 401, r.text[:200]


def test_t9_repo_isolation():
    """T9: 只统计 repo 参数指定的仓库，不串其它仓库数据。"""
    _seed_issue(REPO, 501, creator="alice")
    _seed_issue(REPO_OTHER, 502, creator="mallory")
    _seed_task_task_usage("t_1229_iso", REPO_OTHER, 502,
                          {"model": "glm-5.3", "input": 70, "output": 7,
                           "turn": 0})
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        users = [c["user"] for c in body["contributors"]]
        assert "mallory" not in users
        assert body["total_issues"] == 1 and body["total_tokens"] == 0
    finally:
        app.dependency_overrides.pop(_dep, None)


def test_t9b_no_platform_prefix_accepted():
    """T9b: repo 传 owner/repo（无平台前缀）与 gitcode: 前缀同口径。"""
    _seed_issue(REPO, 503, creator="alice")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        assert body["total_issues"] == 1
        assert body["repo"] == REPO
    finally:
        app.dependency_overrides.pop(_dep, None)
