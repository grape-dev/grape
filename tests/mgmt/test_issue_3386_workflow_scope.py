"""Issue #3386：GitHub workflow scope 探测与推送前预检。

覆盖 Spec 验收中可单测的两段：
1. ``get_github_token_scopes`` 对 X-OAuth-Scopes 的三态解析（有/无/空 header）；
2. 推送预检判定分支（非 GitHub、未触及 workflows、显式能力位、scope 有/无/未知）；
3. 绑定 PAT 时缺 workflow → 响应回传 ``missing_scopes`` 并落下能力位。
"""
import pytest

from src.domains.mgmt.user import repo_manager
from src.domains.runtime.engine import branch_guard

NS = "i3386wf"


@pytest.fixture(autouse=True)
def _clean():
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_credentials WHERE CAST(user_id AS TEXT) LIKE ?",
                  (f"{NS}%",))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_credentials WHERE CAST(user_id AS TEXT) LIKE ?",
                  (f"{NS}%",))
        db.commit()


def _scopes_for(monkeypatch, headers):
    monkeypatch.setattr(repo_manager, "_github_req_with_headers",
                        lambda *a, **k: ({}, headers))
    return repo_manager.get_github_token_scopes("tok")


def test_scope_header_present_is_parsed(monkeypatch):
    scopes = _scopes_for(monkeypatch, {"X-OAuth-Scopes": "repo, workflow,read:org"})
    assert scopes == {"repo", "workflow", "read:org"}


def test_scope_header_absent_is_unknown(monkeypatch):
    assert _scopes_for(monkeypatch, {}) is None


def test_scope_header_empty_is_empty_set(monkeypatch):
    assert _scopes_for(monkeypatch, {"X-OAuth-Scopes": ""}) == set()


def test_scope_header_lookup_is_case_insensitive(monkeypatch):
    scopes = _scopes_for(monkeypatch, {"x-oauth-scopes": "repo"})
    assert scopes == {"repo"}


def test_scope_probe_failure_is_unknown(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(repo_manager, "_github_req_with_headers", _boom)
    assert repo_manager.get_github_token_scopes("tok") is None


def test_scope_empty_token_is_unknown_without_request(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("空 token 不应发起请求")

    monkeypatch.setattr(repo_manager, "_github_req_with_headers", _fail)
    assert repo_manager.get_github_token_scopes("") is None


@pytest.mark.parametrize("provider,files,capability,scopes,allowed,reason", [
    ("gitcode", [".github/workflows/ci.yml"], None, {"repo"}, True, "ok"),
    ("github", ["src/app.py"], None, {"repo"}, True, "ok"),
    ("github", [".github/workflows/ci.yml"], None, {"repo", "workflow"}, True, "ok"),
    ("github", [".github/workflows/ci.yml"], None, {"repo"}, False, "workflow_scope_missing"),
    ("github", [".github/workflows/ci.yml"], True, None, True, "ok"),
    ("github", [".github/workflows/ci.yml"], False, {"repo", "workflow"}, False,
     "workflow_scope_missing"),
    ("github", [".github/workflows/ci.yml"], None, None, True, "workflow_scope_unknown"),
    ("github", [".github/ISSUE_TEMPLATE/bug.md"], None, {"repo"}, True, "ok"),
])
def test_decide_workflow_scope_push(provider, files, capability, scopes, allowed, reason):
    verdict = branch_guard.decide_workflow_scope_push(provider, files, capability, scopes)
    assert verdict["allowed"] is allowed
    assert verdict["reason"] == reason
    if not allowed:
        assert "workflow" in verdict["message"]
    if reason == "workflow_scope_unknown":
        assert "workflow" in verdict["message"]


def test_workflow_files_changed_filters_prefix(monkeypatch):
    class _R:
        returncode = 0
        stdout = ("src/a.py\n.github/workflows/ci.yml\n"
                  ".github/workflows/release.yaml\n.github/ISSUE_TEMPLATE/bug.md\n")

    monkeypatch.setattr("src.domains.runtime.engine.repogit._run_git",
                        lambda *a, **k: _R())
    files = branch_guard._workflow_files_changed("/w", "abc", "def")
    assert files == [".github/workflows/ci.yml", ".github/workflows/release.yaml"]


def test_workflow_files_changed_diff_failure_is_empty(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr("src.domains.runtime.engine.repogit._run_git",
                        lambda *a, **k: _R())
    assert branch_guard._workflow_files_changed("/w", "abc", "def") == []


def test_check_workflow_scope_skips_non_github(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("non-github 不应触发 GitHub API/scope 探测")

    monkeypatch.setattr(branch_guard, "_workflow_files_changed", _fail)
    verdict = branch_guard.check_workflow_scope_push("gitcode:o/r", "/w", "abc")
    assert verdict["allowed"] is True


def test_check_workflow_scope_skips_when_no_workflow_files(monkeypatch):
    monkeypatch.setattr(branch_guard, "_workflow_files_changed", lambda *a, **k: [])
    verdict = branch_guard.check_workflow_scope_push("github:o/r", "/w", "abc")
    assert verdict["allowed"] is True and verdict["reason"] == "ok"


def test_check_workflow_scope_uses_explicit_capability(monkeypatch):
    monkeypatch.setattr(branch_guard, "_workflow_files_changed",
                        lambda *a, **k: [".github/workflows/ci.yml"])
    monkeypatch.setattr(
        "src.domains.mgmt.user.repo_manager.credential_workflows_write",
        lambda uid, p: True)
    verdict = branch_guard.check_workflow_scope_push(
        "github:o/r", "/w", "abc", user_id="u1")
    assert verdict["allowed"] is True


def _bind_github_pat(monkeypatch, scopes):
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials

    class _P:
        # Issue #3386 复审：github 校验路径走 verify_token_with_headers，
        # 响应头直接带 X-OAuth-Scopes（None → 头缺失=未知）
        def verify_token(self, tok):
            return self.verify_token_with_headers(tok)[0]

        def verify_token_with_headers(self, tok):
            headers = {} if scopes is None else {"X-OAuth-Scopes": ", ".join(sorted(scopes))}
            return {"id": f"{NS}ext1", "login": "octo"}, headers

    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider", lambda p: _P())

    def _no_reprobe(tok):
        raise AssertionError("scope 探测应复用校验响应头，不应再 GET /user")

    monkeypatch.setattr(repo_manager, "get_github_token_scopes", _no_reprobe)
    return update_credentials(
        UpdateCredentials(platform_tokens={"github": "ghp_tok"}),
        user={"id": f"{NS}u1"})


def test_bind_missing_workflow_returns_missing_scopes(monkeypatch):
    from src.domains.mgmt.user.db import get_credential
    resp = _bind_github_pat(monkeypatch, {"repo", "read:org"})
    assert resp["missing_scopes"]["github"] == ["workflow"]
    assert any("workflow" in w for w in resp["scope_warnings"])
    cfg = get_credential(f"{NS}u1", "github_pat") or {}
    assert cfg.get("workflows_write") is False


def test_bind_with_workflow_sets_capability_true(monkeypatch):
    from src.domains.mgmt.user.db import get_credential
    resp = _bind_github_pat(monkeypatch, {"repo", "workflow"})
    assert "missing_scopes" not in resp
    cfg = get_credential(f"{NS}u1", "github_pat") or {}
    assert cfg.get("workflows_write") is True


def test_bind_unknown_scope_does_not_block_or_declare(monkeypatch):
    from src.domains.mgmt.user.db import get_credential
    resp = _bind_github_pat(monkeypatch, None)
    assert "missing_scopes" not in resp
    assert any("X-OAuth-Scopes" in w for w in resp.get("scope_warnings", []))
    cfg = get_credential(f"{NS}u1", "github_pat") or {}
    assert "workflows_write" not in cfg


def test_bind_explicit_declaration_kept_when_scope_unknown(monkeypatch):
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
    from src.domains.mgmt.user.db import get_credential

    class _P:
        def verify_token(self, tok):
            return self.verify_token_with_headers(tok)[0]

        def verify_token_with_headers(self, tok):
            # 显式声明 workflows_write=True，但响应头缺 X-OAuth-Scopes（未知）
            return {"id": f"{NS}ext2", "login": "octo"}, {}

    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider", lambda p: _P())
    monkeypatch.setattr(repo_manager, "get_github_token_scopes",
                        lambda tok: (_ for _ in ()).throw(AssertionError(
                            "未知时应保留显式声明，不应再探测")))
    update_credentials(
        UpdateCredentials(platform_tokens={"github": {
            "token": "ghp_tok", "workflows_write": True}}),
        user={"id": f"{NS}u2"})
    cfg = get_credential(f"{NS}u2", "github_pat") or {}
    assert cfg.get("workflows_write") is True


def test_parse_x_oauth_scopes_headers_directly():
    assert repo_manager.parse_x_oauth_scopes(
        {"x-oauth-scopes": "repo, workflow"}) == {"repo", "workflow"}
    assert repo_manager.parse_x_oauth_scopes({}) is None
    assert repo_manager.parse_x_oauth_scopes(None) is None
    assert repo_manager.parse_x_oauth_scopes({"X-OAuth-Scopes": ""}) == set()


def test_workflow_files_changed_enumerates_all_unpushed_commits(monkeypatch):
    """新分支无 remote_sha 且 origin/HEAD 不可用：枚举全部未推送提交（复审项 3）。

    多提交新分支只看最后一提交（local_sha^）会漏判早先提交的 workflow 改动。
    """
    diff_tree_out = {
        "sha2": "src/a.py\n",            # 早先提交改了 workflow
        "sha1": ".github/workflows/ci.yml\n",
    }

    def fake_run_git(args, **k):
        a = list(args)

        class _R:
            def __init__(self, code, out):
                self.returncode = code
                self.stdout = out

        if "symbolic-ref" in a:
            return _R(1, "")
        if "rev-list" in a:
            return _R(0, "sha2\nsha1\n")
        if "diff-tree" in a:
            return _R(0, diff_tree_out.get(a[-1], ""))
        raise AssertionError(f"unexpected git call: {a}")

    monkeypatch.setattr("src.domains.runtime.engine.repogit._run_git", fake_run_git)
    files = branch_guard._workflow_files_changed("/w", "", "tip")
    assert files == [".github/workflows/ci.yml"]


def test_workflow_files_changed_unpushed_enum_failure_is_empty(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr("src.domains.runtime.engine.repogit._run_git",
                        lambda *a, **k: _R())
    assert branch_guard._workflow_files_changed("/w", "", "tip") == []


def test_unknown_scope_advisory_is_nonblocking_stderr_warning(capsys):
    """复审项 2：未知 scope fail-open 放行时必须输出非阻断 stderr 告警。"""
    verdict = branch_guard.decide_workflow_scope_push(
        "github", [".github/workflows/ci.yml"], None, None)
    assert verdict["allowed"] is True
    branch_guard._advise_workflow_scope_unknown(verdict, "github:o/r", "feat/x")
    err = capsys.readouterr().err
    assert "[branch-guard] WARNING" in err
    assert "workflow" in err


def test_unknown_scope_advisory_silent_for_other_verdicts(capsys):
    branch_guard._advise_workflow_scope_unknown(
        {"allowed": True, "reason": "ok", "message": ""}, "github:o/r", "feat/x")
    branch_guard._advise_workflow_scope_unknown(
        {"allowed": False, "reason": "workflow_scope_missing", "message": ""}, "github:o/r", "feat/x")
    branch_guard._advise_workflow_scope_unknown(None, "github:o/r", "feat/x")
    assert capsys.readouterr().err == ""
