"""Issue #4301: GitHub credential workflow precheck and early diagnosis.

Semantics agreed after the PR#2135 review:
  - 两路入口（legacy ``github_token`` / 泛型 ``platform_tokens``）共用同一
    scope 解析器 ``workflows_write_from_scopes``，语义等价（D102-P）：
    含 workflow=True / 不含=False / 无响应头=None（细粒度 PAT，不落键，
    回传权限勾选清单提示）。
  - 绑定期预检复用 verify_token 的同一次 /user 响应头（单次网络往返），
    不再串行第二次 GET /user（R-2：叠加超时超出前端 PUT 绑定预算）。
  - 显式声明优先于自动探测；非 workflow 平台不产生告警。
  - ``workflows_capability_blocker`` 早期诊断（False 阻断 / 其余不阻断）。
"""
import pytest

from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
from src.domains.mgmt.user.db import get_credential, set_credential
from src.kernel.session_manager import db, db_lock
from src.kernel.token_resolver import credential_workflows_write

NS = "i4301"


@pytest.fixture(autouse=True)
def _clean():
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


class _FakeProvider:
    """Fake provider shaped like verify_token_with_headers (the real one gets the same
    默认实现拿到同一语义）：scopes 由测试注入，verify 调用次数可断言。"""

    def __init__(self, scopes):
        self._scopes = scopes
        self.verify_calls = 0

    def verify_token(self, tok):
        self.verify_calls += 1
        return {"id": f"{NS}ext1", "login": "lg1", "name": "LG"}

    def verify_token_scopes(self, tok):
        info = self.verify_token(tok)
        return info, self._scopes

    def token_expiry(self, tok):
        return {}


@pytest.fixture
def fake_verify(monkeypatch):
    """Fake provider with configurable scopes; returned so call counts can be asserted."""
    holder = {"provider": _FakeProvider("repo, admin:repo_hook, workflow")}
    monkeypatch.setattr(
        "src.domains.mgmt.vcs.create_provider",
        lambda p: holder["provider"])
    return holder


def _save(platform, value, user_id=f"{NS}u1"):
    body = UpdateCredentials(platform_tokens={platform: value})
    return update_credentials(body, user={"id": user_id, "username": user_id})


class _FakeResp:
    """requests.Response stand-in (the legacy github_token entry calls requests.get inline)."""

    def __init__(self, headers=None, payload=None, status=200):
        self.headers = headers if headers is not None else {}
        self._payload = payload if payload is not None else {"id": f"{NS}ghid", "login": "gh"}
        self.status_code = status

    def json(self):
        return self._payload


def _save_legacy(resp, user_id=f"{NS}u2", monkeypatch=None):
    """Legacy github_token entry: mock requests.get (/user plus the FR-4 probe)."""

    def _fake_get(url, **kwargs):
        if url == "https://api.github.com/user":
            return resp
        return _FakeResp(payload=[{"id": 1}])  # FR-4 /user/repos 探活：非空列表

    monkeypatch.setattr("requests.get", _fake_get)
    body = UpdateCredentials(github_token="ghp_legacy_tok")
    return update_credentials(body, user={"id": user_id, "username": user_id})


# ---------- pure function: three-state scope parsing (one shared implementation) ----------

def test_scope_parse_three_states():
    import src.domains.mgmt.user.repo_manager as rm
    assert rm.workflows_write_from_scopes("repo, admin:repo_hook, workflow") is True
    assert rm.workflows_write_from_scopes("repo, admin:repo_hook") is False
    assert rm.workflows_write_from_scopes(None) is None, "无响应头（细粒度 PAT）= 未知"
    # empty scopes (header present but blank) -> confirmed absent permission, not unknown
    assert rm.workflows_write_from_scopes("") is False


def test_scope_platforms_only_github():
    import src.domains.mgmt.user.repo_manager as rm
    assert rm.workflows_probe_supported("github") is True
    assert rm.workflows_probe_supported("gitcode") is False


# ---------- agent spec-stage diagnosis ----------

def test_blocker_blocks_only_when_explicitly_forbidden():
    import src.domains.mgmt.user.repo_manager as rm
    set_credential(f"{NS}diag", "github_pat", {"token": "t", "login": "l",
                                               "workflows_write": False})
    assert rm.workflows_capability_blocker(f"{NS}diag", "github", True)
    assert not rm.workflows_capability_blocker(f"{NS}diag", "github", False)

    set_credential(f"{NS}ok", "github_pat", {"token": "t", "login": "l",
                                             "workflows_write": True})
    assert not rm.workflows_capability_blocker(f"{NS}ok", "github", True)

    set_credential(f"{NS}unk", "github_pat", {"token": "t", "login": "l"})
    assert not rm.workflows_capability_blocker(f"{NS}unk", "github", True)


# ---------- Spec AC5: blocker wired into the spec/plan assembly (PR#2135 Required) ----------

def _spec_evt(body: str) -> dict:
    return {
        "type": "issue_opened",
        "repository": "owner/repo",
        "source": "github",
        "repo_key": "github:owner/repo",
        "issue_id": "4301",
        "payload": {"issue": {"number": 4301,
                              "title": "Bug: create GitHub Actions pipeline",
                              "body": body}},
    }


def _bind_repo_user(monkeypatch, user_id=f"{NS}repo"):
    """Point the repo_key at the test credential domain (real chain: managed_repos.host_user_id)."""
    monkeypatch.setattr("src.kernel.ports.tokens.get_host_user_id",
                        lambda rk: user_id)
    return user_id


def test_spec_prompt_injects_blocker_for_workflow_task(monkeypatch):
    """Credential explicitly lacks workflow and the task touches .github/workflows -> blocked in prompt."""
    from src.kernel import prompt_builder as pb
    uid = _bind_repo_user(monkeypatch)
    set_credential(uid, "github_pat", {"token": "t", "login": "l",
                                       "workflows_write": False})

    prompt = pb.build_devops_agent_prompt(
        _spec_evt("需要新增 .github/workflows/deploy.yml"), [], "", ["issue"],
        agent_type="issue")

    assert "## Spec Stage Capability Blocker (workflows)" in prompt
    assert "缺少 workflow 权限" in prompt
    # the blocker must precede Role Definition (early spec-stage signal, not part of the role block)
    assert prompt.index("Spec Stage Capability Blocker") < prompt.index("## Role Definition")


def test_spec_prompt_no_blocker_when_credential_has_permission(monkeypatch):
    from src.kernel import prompt_builder as pb
    uid = _bind_repo_user(monkeypatch)
    set_credential(uid, "github_pat", {"token": "t", "login": "l",
                                       "workflows_write": True})
    prompt = pb.build_devops_agent_prompt(
        _spec_evt("需要新增 .github/workflows/deploy.yml"), [], "", ["issue"],
        agent_type="issue")
    assert "Spec Stage Capability Blocker" not in prompt


def test_spec_prompt_no_blocker_when_task_unrelated(monkeypatch):
    from src.kernel import prompt_builder as pb
    uid = _bind_repo_user(monkeypatch)
    set_credential(uid, "github_pat", {"token": "t", "login": "l",
                                       "workflows_write": False})
    prompt = pb.build_devops_agent_prompt(
        _spec_evt("修复 README 排版"), [], "", ["issue"], agent_type="issue")
    assert "Spec Stage Capability Blocker" not in prompt


def test_spec_prompt_no_blocker_for_unknown_or_unbound(monkeypatch):
    """Unknown state (fine-grained PAT persists nothing) or unbound repo -> no blocking."""
    from src.kernel import prompt_builder as pb
    uid = _bind_repo_user(monkeypatch)
    set_credential(uid, "github_pat", {"token": "t", "login": "l"})
    assert not pb._build_workflows_blocker_section(
        "github:owner/repo", "issue", ".github/workflows/x.yml")
    # unbound (0 sentinel)
    monkeypatch.setattr("src.kernel.ports.tokens.get_host_user_id", lambda rk: 0)
    set_credential(f"{NS}repo2", "github_pat", {"token": "t", "login": "l",
                                                "workflows_write": False})
    assert not pb._build_workflows_blocker_section(
        "github:owner/repo", "issue", ".github/workflows/x.yml")


def test_blocker_section_scoped_to_spec_stage_agents(monkeypatch):
    """Only agents that emit the Spec/plan get it injected; an unprefixed repo_key never guesses a platform."""
    from src.kernel import prompt_builder as pb
    uid = _bind_repo_user(monkeypatch)
    set_credential(uid, "github_pat", {"token": "t", "login": "l",
                                       "workflows_write": False})
    assert not pb._build_workflows_blocker_section(
        "github:owner/repo", "qa", ".github/workflows/x.yml")
    assert not pb._build_workflows_blocker_section(
        "owner/repo", "issue", ".github/workflows/x.yml")
    assert not pb._build_workflows_blocker_section(
        "github:owner/repo", "issue", "")
    assert pb._build_workflows_blocker_section(
        "github:owner/repo", "issue", "path: .github\\workflows\\ci.yml")
