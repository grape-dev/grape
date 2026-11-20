"""Issue #2225：发布前 Issue/PR 号一致性断言。"""
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.domains.runtime.vcs import release_guard
import src.api.git_repo_api as git_repo_api


def _user(**kw):
    base = {"id": 1, "username": "bot", "auth_type": "session_token",
            "agent_type": "coding", "session_key": "t_2225", "repo_key": "gitcode:o/hello"}
    base.update(kw)
    return base


def test_first_issue_anchor_only_subject_not_later_refs():
    body = "## 概述\n\n实现 Issue #2225：隔离暂存目录。\n\n参考 Issue #2080 与 #2085 的实测。"
    assert release_guard.first_issue_anchor(body) == 2225
    assert release_guard.first_issue_ref("feat: #2225 修复") == 2225


def test_first_issue_anchor_matches_chinese_adjacent_text():
    assert release_guard.first_issue_anchor("实现Issue #2225的隔离") == 2225
    assert release_guard.first_issue_anchor("Spec: Issue #2085—正文") == 2085


def test_check_allows_matching_issue():
    ok, msg = release_guard.check_issue_consistency(
        task_issue=2225, target_issue=2225,
        body="实现 Issue #2225，关联参考 #2080", operation="issues update")
    assert ok, msg


def test_check_blocks_foreign_body_anchor():
    ok, msg = release_guard.check_issue_consistency(
        task_issue=2225, target_issue=2225,
        body="# Spec: Issue #2085 — 另一个会话的正文", operation="issues update")
    assert not ok
    assert "#2085" in msg and "2225" in msg


def test_check_blocks_target_issue_mismatch_on_body_write():
    ok, _ = release_guard.check_issue_consistency(
        task_issue=2225, target_issue=2085, body="普通正文", operation="issues update")
    assert not ok


def test_check_allows_metadata_only_update_on_other_issue():
    ok, _ = release_guard.check_issue_consistency(
        task_issue=2225, target_issue=2085, body="", operation="issues update")
    assert ok


def test_check_blocks_pr_title_and_related_mismatch():
    ok, _ = release_guard.check_issue_consistency(
        task_issue=2225, title="feat: #2085 wrong", body="", operation="pulls create")
    assert not ok
    ok, _ = release_guard.check_issue_consistency(
        task_issue=2225, title="feat: #2225 ok", body="",
        related_issue_nums=[2085], operation="pulls create")
    assert not ok


def test_check_explicit_override_allows_cross_issue():
    ok, _ = release_guard.check_issue_consistency(
        task_issue=2225, target_issue=2085,
        body="Issue #2085 explicitly allowed", allow_cross_issue=True,
        operation="issues update")
    assert ok


def test_resolve_skips_non_session_and_chat():
    assert release_guard.resolve_session_task_issue(_user(auth_type="api_key")) is None
    assert release_guard.resolve_session_task_issue(_user(agent_type="chat")) is None


def test_resolve_treats_placeholder_issue_zero_as_none(monkeypatch):
    # QA 复审 R2：缺省 issue_id "#0"/"0" 是「无 Issue 上下文」占位——必须是 None，
    # 否则该会话全部正文写入会被一致性断言按 expected=0 拦成 409。
    for raw in ("#0", "0", "  #0  ", ""):
        monkeypatch.setattr(release_guard, "_read_task_issue_id", lambda key, _r=raw: _r)
        assert release_guard.resolve_session_task_issue(_user()) is None
    monkeypatch.setattr(release_guard, "_read_task_issue_id", lambda key: "#2225")
    assert release_guard.resolve_session_task_issue(_user()) == 2225


def test_check_fail_open_on_placeholder_task_issue():
    # 占位 0 与无法解析同义：放行，不产生 409 风暴（目标 Issue 也不参与比较）。
    for task_issue in (0, "#0", "0", None, ""):
        ok, msg = release_guard.check_issue_consistency(
            task_issue=task_issue, target_issue=2085,
            body="# Spec: Issue #2085", title="feat: #2085 x", operation="issues update")
        assert ok, (task_issue, msg)


def test_resolve_read_avoids_global_db_lock(monkeypatch):
    """QA 复审 Nice-to-have：读 issue_id 走只读连接，不长时间持有全局 db_lock。"""
    import sqlite3
    import tempfile as _tempfile
    from src.kernel import session_manager as sm

    tmp = _tempfile.mkdtemp(prefix="grape-2225-db-")
    db_path = f"{tmp}/sessions.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE agent_tasks (id TEXT PRIMARY KEY, issue_id TEXT)")
    conn.execute("INSERT INTO agent_tasks (id, issue_id) VALUES ('t_2225', '#2225')")
    conn.commit()
    conn.close()

    lock_used = []
    monkeypatch.setattr(sm, "_db_path", db_path)
    monkeypatch.setattr(sm, "db_lock", type("L", (), {
        "__enter__": lambda s: lock_used.append(1),
        "__exit__": lambda s, *a: None})())
    assert release_guard._read_task_issue_id("t_2225") == "#2225"
    assert lock_used == []


def test_update_issue_guard_blocks_before_upstream_write(monkeypatch):
    calls = []
    monkeypatch.setattr(git_repo_api, "_vcs_req",
                        lambda *a, **k: calls.append((a, k)) or {"title": "t"})
    monkeypatch.setattr(git_repo_api, "_repo_token", lambda *a, **k: "tok")
    monkeypatch.setattr(release_guard, "resolve_session_task_issue", lambda user: 2225)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.patch(
            "/api/git-repo/o/hello/issues/2225",
            json={"body": "# Spec: Issue #2085\n其他会话的正文"})
        assert resp.status_code == 409
        detail = resp.json().get("detail") or {}
        assert detail.get("error_code") == "ISSUE_ASSERT_MISMATCH"
        assert calls == []  # 断言失败 → 绝不调用上游写接口
    finally:
        app.dependency_overrides.clear()


def test_update_issue_guard_allows_matching_body(monkeypatch):
    calls = []
    monkeypatch.setattr(git_repo_api, "_vcs_req",
                        lambda *a, **k: calls.append((a, k)) or {"title": "t"})
    monkeypatch.setattr(git_repo_api, "_repo_token", lambda *a, **k: "tok")
    monkeypatch.setattr(release_guard, "resolve_session_task_issue", lambda user: 2225)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.patch(
            "/api/git-repo/o/hello/issues/2225",
            json={"body": "实现 Issue #2225，参考 #2080"})
        assert resp.status_code == 200
        assert calls  # 一致时正常写入上游
    finally:
        app.dependency_overrides.clear()


def _wire(monkeypatch, task_issue=2225):
    """P1/P2 端点用例公共接线：记录上游调用，session 任务 Issue 固定可解析。"""
    calls = []
    monkeypatch.setattr(git_repo_api, "_vcs_req",
                        lambda *a, **k: calls.append((a, k)) or {"id": 999, "body": "ok"})
    monkeypatch.setattr(git_repo_api, "_repo_token", lambda *a, **k: "tok")
    if task_issue is not None:
        monkeypatch.setattr(release_guard, "resolve_session_task_issue", lambda user: task_issue)
    return calls


# ---------- P2（QA 裁定 11-16）：issues create 接正文主语锚点校验 ----------

def test_issue_create_guard_blocks_forged_cross_issue_body(monkeypatch):
    # 无目标号可比时比对 body 首个 `Issue #N` 锚点：伪造跨 Issue body → 409 且无线上写入。
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/issues",
            json={"title": "bug: 串写", "body": "# Spec: Issue #2085\n其他会话的正文"})
        assert resp.status_code == 409
        detail = resp.json().get("detail") or {}
        assert detail.get("error_code") == "ISSUE_ASSERT_MISMATCH"
        assert calls == []  # 断言失败 → 绝不调用上游写接口
    finally:
        app.dependency_overrides.clear()


def test_issue_create_guard_allows_body_without_anchor(monkeypatch):
    # 无锚点放行，不阻断正常发布（P2 裁定口径）。
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/issues",
            json={"title": "bug: 列表为空", "body": "复现步骤：……"})
        assert resp.status_code == 200
        assert calls
    finally:
        app.dependency_overrides.clear()


def test_issue_create_guard_allows_matching_anchor(monkeypatch):
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/issues",
            json={"title": "feat: 新增", "body": "实现 Issue #2225 的隔离，参考 #2080"})
        assert resp.status_code == 200
        assert calls
    finally:
        app.dependency_overrides.clear()


# ---------- P1(a)（QA 裁定 11-16）：issues_comments_create 补 guard 接线 ----------

def test_issue_comment_guard_blocks_forged_cross_issue_body(monkeypatch):
    # 伪造跨 Issue body 写入目标 Issue 评论 → 409 中止且无线上写入。
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/issues/2225/comments",
            json={"body": "# Spec: Issue #2085 — 另一个会话的正文"})
        assert resp.status_code == 409
        detail = resp.json().get("detail") or {}
        assert detail.get("error_code") == "ISSUE_ASSERT_MISMATCH"
        assert calls == []
    finally:
        app.dependency_overrides.clear()


def test_issue_comment_guard_blocks_foreign_target_issue(monkeypatch):
    # target_issue=number 比对：评论目标 Issue 与当前任务不一致 → 409 且无线上写入。
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/issues/2085/comments",
            json={"body": "普通回复内容"})
        assert resp.status_code == 409
        assert calls == []
    finally:
        app.dependency_overrides.clear()


def test_issue_comment_guard_allows_own_issue_comment(monkeypatch):
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/issues/2225/comments",
            json={"body": "[Coding Agent] 实现完成，关联 Issue #2225，参考 #2080"})
        assert resp.status_code == 200
        assert calls
    finally:
        app.dependency_overrides.clear()


def test_issue_comment_guard_allows_chat_session_relay(monkeypatch):
    # 讨论区 chat 会话代发用户真实回复不经断言（resolve 对 chat fail-open，真实函数生效）。
    calls = _wire(monkeypatch, task_issue=None)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user(agent_type="chat")
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/issues/2085/comments",
            json={"body": "用户真实回复"})
        assert resp.status_code == 200
        assert calls
    finally:
        app.dependency_overrides.clear()


# ---------- P1(b)（QA 裁定 11-16）：pulls_comments_create 补 guard 接线 ----------

def test_pull_comment_guard_blocks_forged_cross_issue_body(monkeypatch):
    # 伪造跨 Issue body 写入 PR 评论 → 409 中止且无线上写入。
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/pulls/1295/comments",
            json={"body": "# Spec: Issue #2085 — 另一个会话的正文"})
        assert resp.status_code == 409
        detail = resp.json().get("detail") or {}
        assert detail.get("error_code") == "ISSUE_ASSERT_MISMATCH"
        assert calls == []
    finally:
        app.dependency_overrides.clear()


def test_pull_comment_guard_allows_normal_review_comment(monkeypatch):
    # 正常 PR 评论（PR 号与任务 Issue 号不比对——不同号段空间，比对会误杀全部正常评论）。
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/pulls/1295/comments",
            json={"body": "[Coding Agent] 已按审查意见修改，请重新审查（PR #1295，任务 Issue #2225）"})
        assert resp.status_code == 200
        assert calls
    finally:
        app.dependency_overrides.clear()


def test_pull_comment_guard_allows_chat_session_relay(monkeypatch):
    calls = _wire(monkeypatch, task_issue=None)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user(agent_type="chat")
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/pulls/1295/comments",
            json={"body": "用户对 PR 的真实回复"})
        assert resp.status_code == 200
        assert calls
    finally:
        app.dependency_overrides.clear()


def test_check_allows_body_opening_with_foreign_quote_reference():
    """F4：以他号引用行开头的合法报告正文不得误杀。"""
    body = ("> Issue #2403 规则包：A3 50tps 为业务要求。\n\n"
            "PR #1501 审查结论见下。")
    ok, msg = release_guard.check_issue_consistency(
        task_issue=2225, target_issue=2225, body=body, operation="issues comments create")
    assert ok, msg


def test_first_issue_anchor_skips_quoted_lines():
    body = "> Issue #2403 引用他号原文\n>> 嵌套引用 Issue #2080\n正文主语 Issue #2225"
    assert release_guard.first_issue_anchor(body) == 2225
    # 全文只剩引用行 → 无主语锚点，放行由调用方按 target 校验兜底
    assert release_guard.first_issue_anchor("> Issue #2403 only quote") is None


def test_check_still_blocks_all_quote_or_foreign_subject():
    """非引用正文的他号主语仍必须拦截（防回归）。"""
    ok, msg = release_guard.check_issue_consistency(
        task_issue=2225, target_issue=2225,
        body="Issue #2085 — 整篇正文来自另一个会话", operation="issues update")
    assert not ok


def test_read_task_issue_id_skips_sqlite_fastpath_on_mysql_backend(tmp_path, monkeypatch):
    """QA round-2 F5：MySQL 后端下不得打开 SQLite 只读快路径（残留旧库会读错数据源）。"""
    import sqlite3 as _sq
    from src.kernel import session_manager as sm

    stale = tmp_path / "sessions.db"
    conn = _sq.connect(str(stale))
    conn.execute("CREATE TABLE agent_tasks (id TEXT PRIMARY KEY, issue_id TEXT)")
    conn.execute("INSERT INTO agent_tasks VALUES ('t_mysql', '#9999')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sm, "_db_path", str(stale))
    monkeypatch.setattr(sm, "_is_mysql_backend", lambda: True, raising=False)

    opened = []
    real_connect = release_guard.sqlite3.connect

    def spy_connect(*a, **kw):
        opened.append((a, kw))
        raise AssertionError("sqlite fast-path must not be used on mysql backend")

    monkeypatch.setattr(release_guard.sqlite3, "connect", spy_connect)
    # sm.db 兜底也走不通时按 fail-open（返回 None）——但关键断言是快路径没被打开
    class _BrokenDb:
        class _C:
            def execute(self, *a, **kw):
                raise RuntimeError("db unavailable")
        db = _C()
    monkeypatch.setattr(sm, "db", _BrokenDb.db, raising=False)
    monkeypatch.setattr(sm, "db_lock", __import__("threading").RLock(), raising=False)
    result = release_guard._read_task_issue_id("t_mysql")
    assert opened == [], "sqlite3.connect must not be called on mysql backend"
    assert result is None


def test_read_task_issue_id_uses_sqlite_fastpath_on_sqlite_backend(tmp_path, monkeypatch):
    """对照：SQLite 后端仍走只读快路径（保持 Nice-to-have 优化语义）。"""
    import sqlite3 as _sq
    from src.kernel import session_manager as sm

    dbf = tmp_path / "sessions.db"
    conn = _sq.connect(str(dbf))
    conn.execute("CREATE TABLE agent_tasks (id TEXT PRIMARY KEY, issue_id TEXT)")
    conn.execute("INSERT INTO agent_tasks VALUES ('t_sql', '#123')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sm, "_db_path", str(dbf))
    monkeypatch.setattr(sm, "_is_mysql_backend", lambda: False, raising=False)
    assert release_guard._read_task_issue_id("t_sql") == "#123"
