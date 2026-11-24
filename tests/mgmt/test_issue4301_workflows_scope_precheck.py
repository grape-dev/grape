"""Issue #4301：GitHub 凭据 workflow 权限预检与早期诊断。

PR#2135 复审后口径：
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
    """verify_token_scopes 形态的假 provider（真实 provider 经 TokenCapability
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
    """可配置 scopes 的假 provider；返回 provider 实例供测试断言调用次数。"""
    holder = {"provider": _FakeProvider("repo, admin:repo_hook, workflow")}
    monkeypatch.setattr(
        "src.domains.mgmt.vcs.create_provider",
        lambda p: holder["provider"])
    return holder


def _save(platform, value, user_id=f"{NS}u1"):
    body = UpdateCredentials(platform_tokens={platform: value})
    return update_credentials(body, user={"id": user_id, "username": user_id})


class _FakeResp:
    """requests.Response 替身（legacy github_token 入口内联 requests.get 用）。"""

    def __init__(self, headers=None, payload=None, status=200):
        self.headers = headers if headers is not None else {}
        self._payload = payload if payload is not None else {"id": f"{NS}ghid", "login": "gh"}
        self.status_code = status

    def json(self):
        return self._payload


def _save_legacy(resp, user_id=f"{NS}u2", monkeypatch=None):
    """legacy 专用 github_token 入口：mock requests.get（/user + FR-4 探活）。"""

    def _fake_get(url, **kwargs):
        if url == "https://api.github.com/user":
            return resp
        return _FakeResp(payload=[{"id": 1}])  # FR-4 /user/repos 探活：非空列表

    monkeypatch.setattr("requests.get", _fake_get)
    body = UpdateCredentials(github_token="ghp_legacy_tok")
    return update_credentials(body, user={"id": user_id, "username": user_id})


# ---------- 纯函数：scope 解析三态（两路共用唯一实现） ----------

def test_scope_parse_three_states():
    import src.domains.mgmt.user.repo_manager as rm
    assert rm.workflows_write_from_scopes("repo, admin:repo_hook, workflow") is True
    assert rm.workflows_write_from_scopes("repo, admin:repo_hook") is False
    assert rm.workflows_write_from_scopes(None) is None, "无响应头（细粒度 PAT）= 未知"
    # 空 scopes（响应头存在但为空）→ 确认无权限，而非未知
    assert rm.workflows_write_from_scopes("") is False


def test_scope_platforms_only_github():
    import src.domains.mgmt.user.repo_manager as rm
    assert rm.workflows_probe_supported("github") is True
    assert rm.workflows_probe_supported("gitcode") is False


# ---------- 泛型入口 platform_tokens：绑定期自动探测 ----------

def test_binding_classic_with_workflow_records_true(fake_verify):
    fake_verify["provider"]._scopes = "repo, admin:repo_hook, workflow"
    resp = _save("github", "ghp_tok")
    assert credential_workflows_write(f"{NS}u1", "github") is True
    assert not resp.get("scope_warnings")


def test_binding_classic_without_workflow_records_false_and_warns(fake_verify):
    fake_verify["provider"]._scopes = "repo, admin:repo_hook"
    resp = _save("github", "ghp_tok")
    assert credential_workflows_write(f"{NS}u1", "github") is False
    warnings = resp.get("scope_warnings") or []
    assert any("workflow" in w for w in warnings), warnings


def test_binding_finegrained_unknown_does_not_persist_and_warns(fake_verify):
    fake_verify["provider"]._scopes = None
    resp = _save("github", "github_pat_xxx")
    cfg = get_credential(f"{NS}u1", "github_pat") or {}
    assert "workflows_write" not in cfg, "未知态不应落键（不得伪装成受限）"
    warnings = resp.get("scope_warnings") or []
    assert any("Read and write" in w for w in warnings), warnings


def test_binding_single_network_round_trip(fake_verify, monkeypatch):
    """R-2 回归：绑定预检复用 verify_token 的 /user 响应头，不得再发
    第二次网络请求（串行叠加会超出前端 PUT 绑定预算）。"""
    import src.domains.mgmt.user.repo_manager as rm

    def _boom(tok):
        raise AssertionError("绑定路径不得再发独立 scope 网络探测")

    monkeypatch.setattr(rm, "_github_token_scopes", _boom)
    fake_verify["provider"]._scopes = "repo, workflow"
    _save("github", "ghp_tok")
    assert fake_verify["provider"].verify_calls == 1, "verify_token 应只调用一次"
    assert credential_workflows_write(f"{NS}u1", "github") is True


def test_binding_explicit_declaration_wins_over_probe(fake_verify, monkeypatch):
    import src.domains.mgmt.user.repo_manager as rm

    def _boom(scopes):
        raise AssertionError("显式声明命中时不应再解析探测结果")

    monkeypatch.setattr(rm, "workflows_write_from_scopes", _boom)
    fake_verify["provider"]._scopes = "repo, workflow"
    _save("github", {"token": "ghp_tok", "workflows_write": True})
    assert credential_workflows_write(f"{NS}u1", "github") is True


def test_binding_non_workflow_platform_no_warning(fake_verify):
    resp = _save("gitcode", "gc_tok")
    assert not resp.get("scope_warnings")


# ---------- legacy 专用 github_token 入口（与泛型入口语义等价，D102-P） ----------

def test_legacy_classic_with_workflow_records_true(monkeypatch):
    resp = _save_legacy(_FakeResp(headers={"X-OAuth-Scopes": "repo, workflow"}),
                        monkeypatch=monkeypatch)
    assert credential_workflows_write(f"{NS}u2", "github") is True
    assert not resp.get("scope_warnings")


def test_legacy_classic_without_workflow_records_false_and_warns(monkeypatch):
    resp = _save_legacy(_FakeResp(headers={"X-OAuth-Scopes": "repo"}),
                        monkeypatch=monkeypatch)
    assert credential_workflows_write(f"{NS}u2", "github") is False
    warnings = resp.get("scope_warnings") or []
    assert any("workflow" in w for w in warnings), warnings


def test_legacy_finegrained_unknown_does_not_persist_and_warns(monkeypatch):
    """R-1 回归：细粒度 PAT（无 scope 头）在专用入口同样不落键并回传
    细粒度权限清单提示——此前专用入口静默无告警，与泛型入口分叉。"""
    resp = _save_legacy(_FakeResp(headers={}), monkeypatch=monkeypatch)
    cfg = get_credential(f"{NS}u2", "github_pat") or {}
    assert "workflows_write" not in cfg, "未知态不应落键"
    warnings = resp.get("scope_warnings") or []
    assert any("Read and write" in w for w in warnings), warnings


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


# ---------- Spec AC5：阻断提示接入 spec/方案汇编（PR#2135 复审 Required） ----------

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
    """把 repo_key 的绑定用户指到测试凭据域（真实链路 = managed_repos.host_user_id）。"""
    monkeypatch.setattr("src.kernel.ports.tokens.get_host_user_id",
                        lambda rk: user_id)
    return user_id


def test_spec_prompt_injects_blocker_for_workflow_task(monkeypatch):
    """凭据明确无 workflow 权限 + 任务涉及 .github/workflows → spec 阶段提示进 prompt。"""
    from src.kernel import prompt_builder as pb
    uid = _bind_repo_user(monkeypatch)
    set_credential(uid, "github_pat", {"token": "t", "login": "l",
                                       "workflows_write": False})

    prompt = pb.build_devops_agent_prompt(
        _spec_evt("需要新增 .github/workflows/deploy.yml"), [], "", ["issue"],
        agent_type="issue")

    assert "## Spec Stage Capability Blocker (workflows)" in prompt
    assert "缺少 workflow 权限" in prompt
    # 提示须落在 Role Definition 之前（spec/方案阶段的早期诊断，不混进角色段）
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
    """未知态（细粒度 PAT 不落键）/ 仓库未绑定用户 → 不阻断。"""
    from src.kernel import prompt_builder as pb
    uid = _bind_repo_user(monkeypatch)
    set_credential(uid, "github_pat", {"token": "t", "login": "l"})
    assert not pb._build_workflows_blocker_section(
        "github:owner/repo", "issue", ".github/workflows/x.yml")
    # 未绑定（0 哨兵）
    monkeypatch.setattr("src.kernel.ports.tokens.get_host_user_id", lambda rk: 0)
    set_credential(f"{NS}repo2", "github_pat", {"token": "t", "login": "l",
                                                "workflows_write": False})
    assert not pb._build_workflows_blocker_section(
        "github:owner/repo", "issue", ".github/workflows/x.yml")


def test_blocker_section_scoped_to_spec_stage_agents(monkeypatch):
    """只有产出 Spec/方案 的 agent 注入；无前缀 repo_key 不猜平台。"""
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


# ---------- 真实 provider 响应头捕获链路（复审 Optional：FakeProvider 未覆盖） ----------

class _HeaderCaptureCard:
    provider = "github"
    api_base = "https://api.github.invalid"
    api_auth = "bearer"
    dialect = "github"
    name = "GitHub"
    ca_bundle = ""


class _FakeUpstreamResp:
    def __init__(self, headers, payload):
        import json as _json
        self.headers = headers
        self.status = 200
        self._raw = _json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_generic_provider_captures_scopes_header(monkeypatch):
    """GenericVcsProvider._http → last_response_headers → verify_token_scopes 单次往返。"""
    import src.kernel.upstream_http as uh
    from src.domains.mgmt.vcs.base import GenericVcsProvider

    seen = {"calls": 0}

    def _fake_open(req, **kwargs):
        seen["calls"] += 1
        return _FakeUpstreamResp({"x-oauth-scopes": "repo, admin:repo_hook, workflow"},
                                 {"id": 1, "login": "lg1"})

    monkeypatch.setattr(uh, "open_with_policy", _fake_open)
    provider = GenericVcsProvider(_HeaderCaptureCard())
    assert provider.last_response_headers is None
    info, scopes = provider.verify_token_scopes("ghp_tok")
    assert info.get("login") == "lg1"
    assert scopes == "repo, admin:repo_hook, workflow"
    assert seen["calls"] == 1, "verify_token_scopes 必须复用 /user 的同一次往返"


def test_generic_provider_without_scopes_header_yields_none(monkeypatch):
    """细粒度 PAT：响应头无 x-oauth-scopes → None（未知态，不阻断绑定）。"""
    import src.kernel.upstream_http as uh
    from src.domains.mgmt.vcs.base import GenericVcsProvider

    monkeypatch.setattr(uh, "open_with_policy",
                        lambda req, **kw: _FakeUpstreamResp({}, {"id": 2, "login": "lg2"}))
    provider = GenericVcsProvider(_HeaderCaptureCard())
    info, scopes = provider.verify_token_scopes("github_pat_x")
    assert info.get("login") == "lg2"
    assert scopes is None
