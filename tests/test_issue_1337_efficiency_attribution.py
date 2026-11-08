"""Issue #1337: 能效看板归属链兜底 + bot 规则化 + INSERT 补 created_by 测试。

钉死四条约定：
1. **归属链 user_id 兜底** —— created_by/assignee 双空时经 issue.user_id →
   unified_user_mapping（gitcode_user 优先，github_login 兜底）回收归属。
2. **未归属单列** —— 双空且 user_id 无映射的行计入「(未归属)」档：
   计入 + 未归属 = 去 bot 后原始行数，不静默丢数。
3. **bot 规则化** —— `*bot` / `*-bot`（大小写不敏感）规则匹配，名单外 bot
   （grace_bot / grape_dev_bot / foo-bot）不再混入人形行。
4. **源头补 created_by** —— issues_center._ensure_assignee_task_card、
   routes/tasks.ensure_issue_task、session_store._sync_issue_fields 三处
   INSERT 补写 created_by（取操作人/责任人账号）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = "gitcode:org/repo"
UNASSIGNED = "(未归属)"

_UID_BASE = 133700


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
    _exec("DELETE FROM issue WHERE repo_key=?", (REPO,))
    _exec("DELETE FROM obs_llm_usage WHERE repo_key=?", (REPO,))
    _exec("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
    _exec("DELETE FROM unified_user_mapping WHERE user_id>? AND user_id<=?",
          (_UID_BASE, _UID_BASE + 50))


@pytest.fixture(autouse=True)
def _clean_tables():
    _cleanup()
    _grant_repo_access()
    yield
    _cleanup()


def _seed_issue(repo_key, number, creator="", assignee="", user_id=0,
                status="active", is_subtask=0, user_id_null=False):
    if user_id_null:
        _exec(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title,"
            " status, is_subtask_mode, created_by) VALUES (NULL,?,?,?,'t',?,?,?)",
            (assignee, repo_key, number, status, is_subtask, creator))
        return
    _exec(
        "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title,"
        " status, is_subtask_mode, created_by) VALUES (?,?,?,?,'t',?,?,?)",
        (user_id, assignee, repo_key, number, status, is_subtask, creator))


def _seed_mapping(user_id, gitcode_user="", github_login=""):
    _exec(
        "INSERT INTO unified_user_mapping (user_id, gitcode_user, github_login)"
        " VALUES (?,?,?)", (user_id, gitcode_user, github_login))


def _grant_repo_access(user_id=133701):
    from src.user import db as user_db
    base = ("org", "repo")
    _exec("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
    user_db.add_managed_repo(REPO, base[0], base[1])
    _exec("DELETE FROM user_repos WHERE user_id=? AND repo_key=?", (user_id, REPO))
    user_db.mark_user_repo_join(user_id, REPO)


def _client():
    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app

    user = {"id": 133701, "username": "board-user", "org_id": 0,
            "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user
    return TestClient(app), app, require_org_member


# --------------------------------------------- A：归属链 user_id 兜底

def test_user_id_fallback_via_gitcode_mapping():
    _seed_mapping(_UID_BASE + 1, gitcode_user="carol")
    _seed_issue(REPO, 601, user_id=_UID_BASE + 1)
    _seed_issue(REPO, 602, user_id=_UID_BASE + 1, status="completed")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        by = {c["user"]: c for c in body["contributors"]}
        assert by["carol"]["issues"] == 2 and by["carol"]["completed"] == 1
        assert body["total_issues"] == 2
    finally:
        app.dependency_overrides.pop(_dep, None)


def test_user_id_fallback_github_login_when_no_gitcode():
    _seed_mapping(_UID_BASE + 2, github_login="carol-gh")
    _seed_issue(REPO, 603, user_id=_UID_BASE + 2)
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        by = {c["user"]: c for c in body["contributors"]}
        assert by["carol-gh"]["issues"] == 1
    finally:
        app.dependency_overrides.pop(_dep, None)


# --------------------------------------------- B：未归属单列

def test_unassigned_bucket_counts_double_empty_rows():
    _seed_issue(REPO, 611, creator="alice")
    _seed_issue(REPO, 612)                      # 双空 + user_id=0
    _seed_issue(REPO, 613, user_id=_UID_BASE + 3)  # user_id 无映射 → 未归属
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        by = {c["user"]: c for c in body["contributors"]}
        assert by[UNASSIGNED]["issues"] == 2
        # 计入 + 未归属 = 去 bot 后原始行数
        assert body["total_issues"] == 3
    finally:
        app.dependency_overrides.pop(_dep, None)


def test_unassigned_tokens_attributed_to_bucket():
    _seed_issue(REPO, 614)                       # 双空 issue 行（未归属）
    _exec(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt,"
        " repo_key, agent_type, issue_id, status) VALUES"
        " ('t_1337_u','sess',7,'p',?,'coding','#614','completed')", (REPO,))
    lu = __import__(
        "src.observability.llm_usage", fromlist=["record_usage"]).record_usage
    lu("t_1337_u", [{"model": "glm-5.3", "input": 30, "output": 3, "turn": 0}],
       source="hermes")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        by = {c["user"]: c for c in body["contributors"]}
        assert by[UNASSIGNED]["total_tokens"] == 33
        assert body["total_tokens"] == 33
    finally:
        app.dependency_overrides.pop(_dep, None)


# --------------------------------------------- C：bot 规则化

def test_bot_rule_covers_list_missing_bots():
    _seed_issue(REPO, 621, creator="grace_bot")
    _seed_issue(REPO, 622, creator="grape_dev_bot")
    _seed_issue(REPO, 623, creator="Foo-Bot")
    _seed_issue(REPO, 624, creator="alice")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        users = [c["user"] for c in body["contributors"]]
        assert users == ["alice"], users
        assert body["total_issues"] == 1
    finally:
        app.dependency_overrides.pop(_dep, None)


def test_bot_rule_in_token_chain():
    _seed_issue(REPO, 625, creator="grace_bot")
    _exec(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt,"
        " repo_key, agent_type, issue_id, status) VALUES"
        " ('t_1337_bot','sess',7,'p',?,'coding','#625','completed')", (REPO,))
    lu = __import__(
        "src.observability.llm_usage", fromlist=["record_usage"]).record_usage
    lu("t_1337_bot",
       [{"model": "glm-5.3", "input": 80, "output": 8, "turn": 0}],
       source="hermes")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        assert body["total_tokens"] == 0
        assert body["contributors"] == []
    finally:
        app.dependency_overrides.pop(_dep, None)


@pytest.fixture
def _issue_user_id_nullable():
    """模拟存量/异常库形态：issue.user_id 允许 NULL（新库 DDL 为 NOT NULL）。

    QA Optional 形态②要求覆盖「issue 行命中但 user_id 为 NULL」。用建表 SQL 的
    等价可空副本整体替换 issue（唯一放宽 user_id 的 NOT NULL，其余列/索引/数据
    原样保留），用例结束后按原 DDL 还原并把 NULL user_id 归零，不影响其他用例。
    """
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(issue)").fetchall()]
        _ddl_row = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='issue'"
        ).fetchone()
        _ix_rows = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='index'"
            " AND tbl_name='issue' AND sql IS NOT NULL").fetchall()
        assert _ddl_row, "issue 表必须已初始化"
        orig_ddl, orig_indexes = _ddl_row[0], [r[0] for r in _ix_rows]
        nullable_ddl = orig_ddl.replace(
            "user_id          INTEGER NOT NULL", "user_id          INTEGER")
        assert nullable_ddl != orig_ddl, "user_id NOT NULL 放宽未生效（DDL 文案漂移？）"
        c.execute("DROP TABLE IF EXISTS issue_nullable_1337")
        c.execute(nullable_ddl.replace("issue (", "issue_nullable_1337 (", 1))
        c.execute(f"INSERT INTO issue_nullable_1337 ({','.join(cols)})"
                  f" SELECT {','.join(cols)} FROM issue")
        c.execute("DROP TABLE issue")
        c.execute("ALTER TABLE issue_nullable_1337 RENAME TO issue")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        sel = ",".join("COALESCE(user_id,0)" if x == "user_id" else x
                       for x in cols)
        c.execute("DROP TABLE IF EXISTS issue")
        c.execute(orig_ddl)
        for ix in orig_indexes:
            c.execute(ix)
        c.execute(f"INSERT INTO issue ({','.join(cols)}) SELECT {sel}"
                  " FROM issue")
        db.commit()


def test_issue_row_with_null_user_id_tokens_fall_into_unassigned(_issue_user_id_nullable):
    """形态②（QA Optional）：issue 行命中但 user_id 为 NULL 且 created_by/assignee
    全空 → token 用量入「(未归属)」档，不随孤儿分支静默丢弃。

    对账恒等式（#1337-B）：计入 + 未归属 = 去 bot 原始行数；
    与 issue 链 COALESCE(user_id,0) 口径一致。
    对照组：#616 无 issue 行（真孤儿）→ 用量丢弃。
    """
    _seed_issue(REPO, 615, user_id_null=True)    # 命中行但归属全空（user_id NULL）
    _seed_issue(REPO, 616, creator="alice")      # 正常归属，作对照
    for tid in ("t_1337_null", "t_1337_orphan"):
        _exec(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt,"
            " repo_key, agent_type, issue_id, status) VALUES"
            f" ('{tid}','sess',7,'p',?,'coding','#{615 if tid == 't_1337_null' else 999}','completed')",
            (REPO,))
    lu = __import__(
        "src.observability.llm_usage", fromlist=["record_usage"]).record_usage
    lu("t_1337_null",
       [{"model": "glm-5.3", "input": 40, "output": 4, "turn": 0}],
       source="hermes")
    lu("t_1337_orphan",
       [{"model": "glm-5.3", "input": 60, "output": 6, "turn": 0}],
       source="hermes")
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        by = {c["user"]: c for c in body["contributors"]}
        # 命中行 NULL user_id → 未归属档 44；真孤儿 66 丢弃 → total=44
        assert by[UNASSIGNED]["total_tokens"] == 44, by.get(UNASSIGNED)
        assert by["alice"]["total_tokens"] == 0
        assert body["total_tokens"] == 44, body["total_tokens"]
    finally:
        app.dependency_overrides.pop(_dep, None)


# --------------------------------------------- D：INSERT 补 created_by

def test_ensure_assignee_task_card_writes_created_by():
    from src.api.issues_center import _ensure_assignee_task_card
    _seed_mapping(_UID_BASE + 4, gitcode_user="dave")
    task_id = _ensure_assignee_task_card(_UID_BASE + 4, "", REPO, 631,
                                         "t", "d")
    assert task_id
    row = _exec(
        "SELECT created_by FROM issue WHERE repo_key=? AND issue_number=?",
        (REPO, 631)).fetchone()
    assert row and row[0] == "dave", row


def test_ensure_issue_task_writes_created_by_from_sender():
    from src.api.routes.tasks import ensure_issue_task
    _seed_mapping(_UID_BASE + 5, gitcode_user="erin")
    task_id = ensure_issue_task(REPO, 632, title="t", sender="erin")
    assert task_id
    row = _exec(
        "SELECT created_by FROM issue WHERE repo_key=? AND issue_number=?",
        (REPO, 632)).fetchone()
    assert row and row[0] == "erin", row


def test_sync_issue_fields_creates_row_with_created_by():
    from src.api.session_store import _sync_issue_fields
    _sync_issue_fields(REPO, "#633", {"stage": "coding"},
                       ensure_row=True, assignee="frank")
    row = _exec(
        "SELECT created_by, assignee FROM issue WHERE repo_key=?"
        " AND issue_number=?", (REPO, 633)).fetchone()
    assert row and row[0] == "frank" and row[1] == "frank", row


def test_bot_rule_keeps_human_logins_ending_with_bot():
    """Issue #1337 返工：`abbot` / `robot` / `talbot` 这类真人 login 以 bot 结尾，
    不能被 bot 规则误杀（原实现裸 endswith('bot') 会把它们从看板静默丢掉，
    与 issue 抱怨的「数据不全」同一性质）。
    """
    for num, creator in ((651, "abbot"), (652, "robot"), (653, "talbot")):
        _seed_issue(REPO, num, creator=creator)
    cli, app, _dep = _client()
    try:
        body = cli.get("/api/dashboard/efficiency",
                       params={"repo": "org/repo"}).json()
        users = sorted(c["user"] for c in body["contributors"])
        assert users == ["abbot", "robot", "talbot"], users
        assert body["total_issues"] == 3, body["total_issues"]
    finally:
        app.dependency_overrides.pop(_dep, None)
