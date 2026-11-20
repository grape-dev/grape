"""Issue #3055 A/C 项回归：ident 判定/强转统一 + 仓库解析与越仓反查作用域。

覆盖（对应交付验收）：
- 内核唯一入口语义（纯数字 = int 契约；平台 ident = str；垃圾形态拒绝）；
- A2A 入口（POST /api/a2a、GET /api/a2a/status）字符串 ident 不再 400，垃圾仍 400；
- 判定/强转收口后的写/读路径：stage/status 落库、awaiting_input 恢复、
  /done /cancel /resume 对应函数、关单关任务、cancelled 读取；
- event_store 兜底 issue_id=`#<ident>`；
- C 项：裸 owner/repo 按全 provider 候选解析为 gitee；越仓 mismatch 不跨 provider；
- 守卫：AST 扫描本次收口点，任何 `int(issue...)` / `issue*.isdigit()` 回潮即红。
"""
import ast
import os

import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from src.kernel.issue_ident import (  # noqa: E402
    is_native_issue_ident, is_actionable_issue_number, is_valid_issue_number_input,
    native_issue_number, normalize_issue_ident,
)
from src.kernel.session_manager import db, db_lock  # noqa: E402

REPO_KEY = "gitee:acme/ident3055"
OTHER_GITEE = "gitee:other/ident3055"
OTHER_GITCODE = "gitcode:other/ident3055"
IDENT = "IKW28R"


@pytest.fixture(autouse=True)
def _init_db():
    from src.api.db_setup import init_database
    init_database()
    _cleanup()
    yield
    _cleanup()


def _cleanup():
    keys = (REPO_KEY, OTHER_GITEE, OTHER_GITCODE, "acme/ident3055")
    with db_lock:
        c = db.cursor()
        for table in ("issue", "agent_tasks", "events", "managed_repos"):
            try:
                c.execute(f"DELETE FROM {table} WHERE repo_key IN (?,?,?,?)", keys)
            except Exception:
                pass
        db.commit()


def _seed_issue(repo_key=REPO_KEY, number=IDENT, status="active", stage="issue"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
            "VALUES ('u1','',?,?,?,'active')", (repo_key, number, f"card-{number}"))
        row_id = c.lastrowid
        c.execute("UPDATE issue SET status=?, stage=?, owner_agent=? WHERE id=?",
                  (status, stage, stage, row_id))
        db.commit()
        return row_id


def _issue_row(repo_key=REPO_KEY, number=IDENT):
    with db_lock:
        return db.cursor().execute(
            "SELECT id, issue_number, status, stage, owner_agent FROM issue "
            "WHERE repo_key=? AND issue_number=?", (repo_key, number)).fetchone()


# ------------------------------------------------------------------ 内核谓词

def test_kernel_ident_predicate_and_coercion():
    assert is_native_issue_ident(30) and is_native_issue_ident("30")
    assert is_native_issue_ident(IDENT) and is_native_issue_ident(f"#{IDENT}")
    # Issue #3055 QA 回归：gitee 真实单号是 I + 5 位 base36，第二位常为数字；
    # 旧文法 [A-Z]{2,}[A-Z0-9]* 会把它们判非法（入口比基线还窄）。
    assert is_native_issue_ident("I1J8U4")
    assert is_native_issue_ident("#I1J8U4")
    assert is_native_issue_ident("I5")
    assert not is_native_issue_ident("")
    assert not is_native_issue_ident(None)
    assert not is_native_issue_ident("pr_123")     # PR 订阅伪 id
    assert not is_native_issue_ident("abc123")     # 小写随机文本非平台 ident
    assert not is_native_issue_ident("x" * 64)     # 超长
    assert not is_native_issue_ident("issue-12")

    assert native_issue_number("30") == 30 and isinstance(native_issue_number("30"), int)
    assert native_issue_number(IDENT) == IDENT and isinstance(native_issue_number(IDENT), str)
    assert native_issue_number("#0007") == 7

    assert normalize_issue_ident(" #IKW28R ") == IDENT
    assert is_actionable_issue_number(30) and not is_actionable_issue_number(0)
    assert is_actionable_issue_number(IDENT)


def test_a2a_input_shape_rejects_garbage():
    assert is_valid_issue_number_input("30") and is_valid_issue_number_input(30)
    assert is_valid_issue_number_input(IDENT)
    for bad in ("", "   ", "#1", f"#{IDENT}", "IK W28R", "fix the bug", None, "x" * 64, "issue-1"):
        assert not is_valid_issue_number_input(bad), bad


# ------------------------------------------------------------------ A2A 入口

def test_a2a_entry_accepts_ident_rejects_garbage():
    from fastapi.testclient import TestClient
    from src.api.main import app
    # 托管记录种子：入口通过形态校验后才校验仓库真实性
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                  "VALUES (?, 'acme', 'ident3055', 'acme/ident3055')", (REPO_KEY,))
        db.commit()
    client = TestClient(app)

    ident_resp = client.post("/api/a2a", json={
        "agent": "issue",
        "payload": {"repository": "acme/ident3055", "issue_number": IDENT}})
    assert ident_resp.status_code != 400, ident_resp.text
    assert "invalid issue_number" not in ident_resp.text

    for bad in ("#1", "fix the login bug", "x" * 64):
        resp = client.post("/api/a2a", json={
            "agent": "issue",
            "payload": {"repository": "acme/ident3055", "issue_number": bad}})
        assert resp.status_code == 400, (bad, resp.text)
        assert "invalid issue_number" in resp.text

    status_resp = client.get("/api/a2a/status", params={
        "repository": "acme/ident3055", "issue_number": "#1", "agent": "issue"})
    assert status_resp.status_code == 400


# ------------------------------------------------------------------ 写/读路径

def test_session_store_accepts_ident():
    from src.api.session_store import (
        set_issue_stage, get_issue_stage, get_issue_status, set_issue_scene,
    )
    _seed_issue()
    set_issue_stage(REPO_KEY, IDENT, "done", "qa")
    set_issue_scene(REPO_KEY, IDENT, "scene-3055")
    row = _issue_row()
    assert row and row[3] == "done", row
    assert get_issue_stage(REPO_KEY, IDENT)["stage"] == "done"
    assert get_issue_status(REPO_KEY, IDENT) in ("done", "completed", "processing")
    # ident 原样落库（SQLite 亲和保留文本，未被截断成 0）
    assert str(row[1]) == IDENT


def test_awaiting_input_resume_with_ident():
    from src.domains.runtime.issues_center import _resume_issue_waiting
    _seed_issue(status="awaiting_input", stage="issue")
    assert _resume_issue_waiting(REPO_KEY, native_issue_number(IDENT)) is True
    assert _issue_row()[2] == "processing"


def test_done_cancel_resume_with_ident(monkeypatch):
    import src.domains.runtime.issues_center as ic
    monkeypatch.setattr(ic, "_notify_done", lambda *a, **k: None)
    monkeypatch.setattr(ic, "_cleanup_auto_transferred_attachments", lambda *a, **k: None)

    _seed_issue()
    assert ic.cancel_issue_task(REPO_KEY, native_issue_number(IDENT)) == 1
    assert _issue_row()[2] == "cancelled"
    assert ic.resume_issue_task(REPO_KEY, native_issue_number(IDENT)) == 1
    assert _issue_row()[2] == "active"

    assert ic.confirm_issue_done(REPO_KEY, native_issue_number(IDENT)) == 1
    assert _issue_row()[2] == "completed"


def test_close_issue_closes_tasks_with_ident():
    from src.domains.runtime.issues_center import close_issue_tasks
    _seed_issue()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, issue_id, repo_key, "
            "agent_type, prompt, status) VALUES ('t3055', 'sess', 0, ?, ?, 'issue', 'x', 'completed')",
            (f"#{IDENT}", REPO_KEY))
        db.commit()
    affected = close_issue_tasks(REPO_KEY, native_issue_number(IDENT))
    # 关单链按 ident 命中 issue 行（原 isdigit 门禁下 ident 永不进入此链）
    assert affected >= 1
    assert _issue_row()[2] == "pending_review"


def test_cancelled_check_reads_ident_row():
    """event_pipeline/webhook 的 cancelled 门禁 = is_native_issue_ident + native 参数。"""
    from src.domains.runtime.issues_center import cancel_issue_task
    _seed_issue()
    assert cancel_issue_task(REPO_KEY, native_issue_number(IDENT)) == 1
    with db_lock:
        row = db.cursor().execute(
            "SELECT status FROM issue WHERE repo_key=? AND issue_number=? AND status='cancelled' LIMIT 1",
            (REPO_KEY, native_issue_number(IDENT))).fetchone()
    assert row and row[0] == "cancelled"


def test_event_store_fallback_issue_id_keeps_ident():
    from src.domains.runtime.event_store import append_event
    seq = append_event(REPO_KEY, "a2a_event", title="t", source="a2a",
                       payload={"issue_number": IDENT}, status="completed")
    assert seq > 0
    with db_lock:
        row = db.cursor().execute(
            "SELECT issue_id FROM events WHERE seq=?", (seq,)).fetchone()
    assert row and row[0] == f"#{IDENT}"


# ------------------------------------------------------------------ C 项

def test_bare_repo_key_resolves_to_gitee():
    from src.domains.runtime.routes.webhook import _resolve_a2a_repo_key
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                  "VALUES ('gitee:acme/ident3055', 'acme', 'ident3055', 'acme/ident3055')")
        db.commit()
    assert _resolve_a2a_repo_key("acme/ident3055") == REPO_KEY
    assert _resolve_a2a_repo_key(f"gitcode:acme/ident3055") == "gitcode:acme/ident3055"


def test_repo_fallback_warning():
    from src.domains.runtime.a2a_repo_scope import RepoKeyResolution
    res = RepoKeyResolution.resolve("nowhere/unmanaged3055")
    assert res.resolved is False
    assert res.repo_key == "gitcode:nowhere/unmanaged3055"
    assert "fallback" in res.fallback_warning()


def test_mismatch_scoped_by_provider():
    from src.domains.runtime.a2a_repo_scope import IssueRepoScope
    from src.domains.runtime.routes.webhook import _issue_repo_mismatch
    _seed_issue(repo_key=OTHER_GITCODE, number="77")
    assert IssueRepoScope(REPO_KEY).other_repo_with_issue("77") == ""
    assert _issue_repo_mismatch(REPO_KEY, "77") == ""
    _seed_issue(repo_key=OTHER_GITEE, number="77")
    assert _issue_repo_mismatch(REPO_KEY, "77") == OTHER_GITEE
    assert _issue_repo_mismatch(REPO_KEY, IDENT) == ""


# ------------------------------------------------------------------ 守卫

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_CLOSURE_MODULES = [
    "src/kernel/session_manager.py",
    "src/kernel/prompt_builder.py",
    "src/api/session_store.py",
    "src/api/patrol.py",
    "src/api/patrol_compensate.py",
    "src/api/issue_vcs_index.py",
    "src/api/qa_gate.py",
    "src/domains/runtime/event_store.py",
    "src/domains/runtime/pipeline/event_pipeline.py",
    "src/domains/runtime/routes/webhook.py",
    "src/domains/runtime/routes/issues.py",
    "src/domains/runtime/routes/events.py",
    "src/domains/runtime/routes/pipeline.py",
    "src/domains/runtime/engine/service.py",
    "src/domains/runtime/engine/takeover.py",
    "src/domains/runtime/im/notify.py",
    "src/domains/runtime/im/outbox.py",
    "src/domains/runtime/issues_center.py",
    "src/domains/contracts/local/runtime_local.py",
    "src/domains/mgmt/public/__init__.py",
]

# 允许的既有例外：PR 号 / 路径参数归一（设计口径明确「不改」）
_ALLOWED_LINES = {
    ("src/domains/runtime/pipeline/event_pipeline.py", 1016),   # PR 号 _numeric_value
    ("src/domains/runtime/pipeline/event_pipeline.py", 5358),   # PR 号 exclude
    ("src/domains/runtime/routes/issues.py", 174),              # PR 号解析
    ("src/api/qa_gate.py", 656),                                # PR 号
}


def _dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return _dotted(node.value)
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _scan_coercions(path: str):
    """返回 int(issue*) / issue*.isdigit() 形态的行号列表（Issue #3055 收口点）。"""
    src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "int" and node.args:
            arg = _dotted(node.args[0])
            if "issue" in arg.lower():
                hits.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "isdigit":
            recv = _dotted(node.func.value)
            if "issue" in recv.lower():
                hits.append(node.lineno)
    return hits


def test_guard_no_issue_int_or_isdigit_left():
    offenders = {}
    for path in _CLOSURE_MODULES:
        bad = [ln for ln in _scan_coercions(path) if (path, ln) not in _ALLOWED_LINES]
        if bad:
            offenders[path] = bad
    assert not offenders, f"issue 号 int()/isdigit() 回潮：{offenders}"


def test_guard_single_predicate_source():
    """判据唯一来源：内核以外不得再有 ident 正则/第二判据实现。"""
    import re as _re
    for path in _CLOSURE_MODULES:
        src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
        assert not _re.search(r"fullmatch\(\s*r?[\"']\[A-Z\]\{2,\}", src), \
            f"{path} 复制了 ident 正则（应转发内核 is_native_issue_ident）"