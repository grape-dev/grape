"""S103：D105-18 凭据级 workflows 写能力——路由场景用例。

场景（requirements S103 行）：凭据 tab 配 PAT 时三态声明 → 落库 →
查询接口回显 → push 行为分流。行为锁细节见 test_d105m_workflows_capability，
本文件锁路由场景（对象形态保存 / 旧前端字符串形态兼容 / 查询回显）。
"""
import pytest

from src.domains.mgmt.routes.settings import get_credentials, update_credentials
from src.domains.mgmt.routes.settings import UpdateCredentials
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user.db import get_credential, set_credential

NS = "s103wf"


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
    """platform_tokens 保存链的 token 验证打点（卡驱动 create_provider）。"""
    class _P:
        def verify_token(self, tok):
            return {"id": f"{NS}ext1", "login": "lg1", "name": "LG"}

    monkeypatch.setattr("src.kernel.token_resolver.create_provider", lambda p: _P())


def _save(platform, value):
    """value: str（旧前端形态）或 dict（D105-18 对象形态）。"""
    body = UpdateCredentials(platform_tokens={platform: value})
    update_credentials(body, user={"id": f"{NS}u1"})


def test_s103a_declared_true_persists_and_surfaces(fake_verify):
    """S103-a：凭据 tab 显式声明 workflows_write=true → 落库 + get_credentials 回显。"""
    _save("gitcode", {"token": "tok-ok", "workflows_write": True})
    cfg = get_credential(f"{NS}u1", "gitcode_pat") or {}
    assert cfg.get("token") == "tok-ok"
    assert cfg.get("workflows_write") is True
    resp = get_credentials(user={"id": f"{NS}u1", "username": f"{NS}u1"})
    git = (resp or {}).get("gitcode_pat") or {}
    assert git.get("exists") is True
    assert git.get("workflows_write") is True, "凭据 tab 清除/能力判定数据源未回显声明"


def test_s103b_declared_false_persists(fake_verify):
    """S103-b：显式声明 false（受限凭据）→ False 落库（三态，不与未声明混淆）。"""
    _save("gitcode", {"token": "tok-ok", "workflows_write": False})
    cfg = get_credential(f"{NS}u1", "gitcode_pat") or {}
    assert cfg.get("workflows_write") is False


def test_s103c_legacy_string_form_undeclared(fake_verify):
    """S103-c：旧前端字符串形态保存 → 无 workflows_write 键（未声明=回退平台旧逻辑）。"""
    _save("gitcode", "tok-ok")
    cfg = get_credential(f"{NS}u1", "gitcode_pat") or {}
    assert "workflows_write" not in cfg, "字符串形态不得隐式声明能力"
    assert credential_state(f"{NS}u1", "gitcode") is None


def credential_state(user_id, provider):
    from src.kernel.token_resolver import credential_workflows_write
    return credential_workflows_write(user_id, provider)


def test_s103d_e2e_declare_changes_push_behavior(push_env_local, monkeypatch):
    """S103-d：端到端分流——声明 true 的 gitcode 凭据 push 含 workflows 的模板不删
    （旧逻辑 gitcode 本就不删；关键=github 声明 true 也不删，平台名不再是判定维度）。"""
    import src.domains.mgmt.user.repo_manager as rm
    uid = f"{NS}u2"
    set_credential(uid, "github_pat", {"token": "t", "login": "lg",
                                       "workflows_write": True})
    seen = push_env_local
    monkeypatch.setattr(rm, "_run_git_has_origin", lambda wd: False)
    rm.push_template_to_repo({"source": "local", "id": f"{NS}tpl"},
                             "owner-1", f"repo-{uid}", user_token="tok",
                             provider="github", user_id=uid)
    assert seen.get("workflows_kept") is True, "github 凭据声明 true 仍被删 workflows"


@pytest.fixture
def push_env_local(tmp_path, monkeypatch):
    """与 test_d105m 同款：local 模板 + push 调用捕获 workflows 是否保留。"""
    import src.domains.mgmt.user.repo_manager as rm
    tpl_dir = tmp_path / f"{NS}tpl"
    (tpl_dir / ".github" / "workflows").mkdir(parents=True)
    (tpl_dir / "README.md").write_text("x")
    (tpl_dir / ".github" / "workflows" / "ci.yml").write_text("on: push")
    monkeypatch.setattr("src.kernel.config.EXAMPLE_DIR", str(tpl_dir.parent))
    seen = {}

    def fake_git(args, cwd, timeout=120):
        if seen.get("workflows_kept") is None and args and args[0] == "push":
            seen["workflows_kept"] = (cwd and __import__("os").path.isdir(
                __import__("os").path.join(cwd, ".github", "workflows")))
        return ""

    monkeypatch.setattr(rm, "_run_git", fake_git)
    return seen
