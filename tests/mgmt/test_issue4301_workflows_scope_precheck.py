"""Issue #4301：GitHub 凭据 workflow 权限预检与早期诊断。

覆盖：
  - 经典 PAT scope 响应头三态解析（含 workflow=True / 不含=False / 无头=None）
  - PUT /settings/credentials 绑定期自动落 workflows_write（true/false/未知）
  - 显式声明优先于自动探测
  - 细粒度 PAT（未知）不落键、不阻断、回传权限勾选清单提示
  - 非 workflow 平台不产生告警
  - workflows_capability_blocker 早期诊断（False 阻断 / 其余不阻断）
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


@pytest.fixture
def fake_verify(monkeypatch):
    class _P:
        def verify_token(self, tok):
            return {"id": f"{NS}ext1", "login": "lg1", "name": "LG"}

        def token_expiry(self, tok):
            return {}

    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider", lambda p: _P())


def _save(platform, value, user_id=f"{NS}u1"):
    body = UpdateCredentials(platform_tokens={platform: value})
    return update_credentials(body, user={"id": user_id, "username": user_id})


# ---------- 纯函数：scope 解析三态 ----------

def test_probe_scope_header_three_states(monkeypatch):
    import src.domains.mgmt.user.repo_manager as rm
    monkeypatch.setattr(rm, "_github_token_scopes", lambda tok: "repo, admin:repo_hook, workflow")
    assert rm.probe_github_workflows_write("t") is True
    monkeypatch.setattr(rm, "_github_token_scopes", lambda tok: "repo, admin:repo_hook")
    assert rm.probe_github_workflows_write("t") is False
    monkeypatch.setattr(rm, "_github_token_scopes", lambda tok: None)
    assert rm.probe_github_workflows_write("t") is None
    # 空 scopes（响应头存在但为空）→ 确认无权限
    monkeypatch.setattr(rm, "_github_token_scopes", lambda tok: "")
    assert rm.probe_github_workflows_write("t") is False


def test_probe_registry_only_github():
    import src.domains.mgmt.user.repo_manager as rm
    assert rm.workflows_probe_supported("github") is True
    assert rm.workflows_probe_supported("gitcode") is False
    assert rm.probe_workflows_write("gitcode", "t") is None


# ---------- 绑定期自动探测 ----------

def test_binding_classic_with_workflow_records_true(fake_verify, monkeypatch):
    import src.domains.mgmt.user.repo_manager as rm
    monkeypatch.setattr(rm, "probe_workflows_write", lambda p, t: True)
    resp = _save("github", "ghp_tok")
    assert credential_workflows_write(f"{NS}u1", "github") is True
    assert not resp.get("scope_warnings")


def test_binding_classic_without_workflow_records_false_and_warns(fake_verify, monkeypatch):
    import src.domains.mgmt.user.repo_manager as rm
    monkeypatch.setattr(rm, "probe_workflows_write", lambda p, t: False)
    resp = _save("github", "ghp_tok")
    assert credential_workflows_write(f"{NS}u1", "github") is False
    warnings = resp.get("scope_warnings") or []
    assert any("workflow" in w for w in warnings), warnings


def test_binding_finegrained_unknown_does_not_persist_and_warns(fake_verify, monkeypatch):
    import src.domains.mgmt.user.repo_manager as rm
    monkeypatch.setattr(rm, "probe_workflows_write", lambda p, t: None)
    resp = _save("github", "github_pat_xxx")
    cfg = get_credential(f"{NS}u1", "github_pat") or {}
    assert "workflows_write" not in cfg, "未知态不应落键（不得伪装成受限）"
    warnings = resp.get("scope_warnings") or []
    assert any("Read and write" in w for w in warnings), warnings


def test_binding_explicit_declaration_wins_over_probe(fake_verify, monkeypatch):
    import src.domains.mgmt.user.repo_manager as rm

    def _boom(p, t):
        raise AssertionError("显式声明命中时不应再探测")

    monkeypatch.setattr(rm, "probe_workflows_write", _boom)
    _save("github", {"token": "ghp_tok", "workflows_write": True})
    assert credential_workflows_write(f"{NS}u1", "github") is True


def test_binding_non_workflow_platform_no_warning(fake_verify):
    resp = _save("gitcode", "gc_tok")
    assert not resp.get("scope_warnings")


# ---------- Agent spec 阶段诊断 ----------

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
