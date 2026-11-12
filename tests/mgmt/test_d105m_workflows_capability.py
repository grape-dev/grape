"""D105-18：凭据级 workflows 写能力（三态）行为锁。

判定维度从平台移到凭据（GitHub 权限模型 requested ∩ granted；gh-aw ADR-0002
显式声明制，不静默推断）。五态断言（评审裁定口径）：
  true            → push 不删 .github/workflows
  显式 false      → 删（新能力：明确受限的凭据）
  未声明+github   → 删（旧逻辑回退，基线冻结）
  未声明+gitcode  → 不删（零回归的真正断言）
  OAuth 路径      → 保守删（保守默认，非平台事实——OAuth scope 不可内省，
                    预留未来 oauth_scopes 声明入口）
"""
import os

import pytest

from src.kernel.session_manager import db, db_lock
from src.kernel.token_resolver import credential_workflows_write
from src.domains.mgmt.user.db import set_credential

NS = "d105m"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_credentials WHERE CAST(user_id AS TEXT) LIKE ?", (f"{NS}%",))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_credentials WHERE CAST(user_id AS TEXT) LIKE ?", (f"{NS}%",))
        db.commit()


def _declare(user_id, provider, value):
    """声明能力：value=True/False 显式三态；None=清除声明（未声明）。"""
    cfg = {"token": "t", "login": "lg"}
    if value is not None:
        cfg["workflows_write"] = value
    set_credential(user_id, f"{provider}_pat", cfg)


def test_unit_three_state():
    _declare(f"{NS}u1", "zzp", True)
    assert credential_workflows_write(f"{NS}u1", "zzp") is True
    _declare(f"{NS}u1", "zzp", False)
    assert credential_workflows_write(f"{NS}u1", "zzp") is False
    _declare(f"{NS}u1", "zzp", None)  # 未声明
    assert credential_workflows_write(f"{NS}u1", "zzp") is None
    assert credential_workflows_write(f"{NS}nobody", "zzp") is None  # 无凭据行


@pytest.fixture
def push_env(tmp_path, monkeypatch):
    """local 模板（含 .github/workflows）+ 捕获首次 git 调用时 workflows 是否保留。"""
    import src.domains.mgmt.user.repo_manager as rm
    tpl_id = f"{NS}tpl"
    tpl_dir = tmp_path / tpl_id
    (tpl_dir / ".github" / "workflows").mkdir(parents=True)
    (tpl_dir / "README.md").write_text("x")
    (tpl_dir / ".github" / "workflows" / "ci.yml").write_text("on: push")
    monkeypatch.setattr("src.kernel.config.EXAMPLE_DIR", str(tpl_dir.parent))
    seen = {}

    def fake_git(args, cwd, timeout=120):
        # 捕获点=push 调用（此前可能有 git init 等前置调用，早捕会误判保留）
        if seen.get("workflows_kept") is None and args and args[0] == "push":
            seen["workflows_kept"] = os.path.isdir(os.path.join(cwd, ".github", "workflows"))
        return ""

    monkeypatch.setattr(rm, "_run_git", fake_git)
    monkeypatch.setattr(rm, "_run_git_has_origin", lambda wd: False)
    return seen


def _push(provider, user_id, monkeypatch):
    import src.domains.mgmt.user.repo_manager as rm
    return rm.push_template_to_repo(
        {"source": "local", "id": f"{NS}tpl"}, "owner-1", f"repo-{provider}-{user_id}",
        user_token="tok", provider=provider, user_id=user_id)


def test_true_declared_keeps_workflows(push_env, monkeypatch):
    """显式声明 true → 不删（比现状更正确的修复）。"""
    import src.domains.mgmt.user.repo_manager as rm
    uid = f"{NS}w1"
    _declare(uid, "github", True)
    monkeypatch.setattr(rm, "credential_workflows_write", lambda u, p: True)
    _push("github", uid, monkeypatch)
    assert push_env["workflows_kept"] is True, "声明有权限的凭据仍被删 workflows"


def test_false_declared_removes_workflows(push_env, monkeypatch):
    """显式声明受限（false）→ 删（新能力：明确受限的凭据）。"""
    import src.domains.mgmt.user.repo_manager as rm
    uid = f"{NS}w2"
    _declare(uid, "gitcode", False)
    monkeypatch.setattr(rm, "credential_workflows_write", lambda u, p: False)
    _push("gitcode", uid, monkeypatch)
    assert push_env["workflows_kept"] is False, "声明受限的凭据未被删 workflows"


def test_undeclared_github_falls_back_to_remove(push_env, monkeypatch):
    """未声明 + github → 旧逻辑回退：删（基线冻结，行为不变）。"""
    import src.domains.mgmt.user.repo_manager as rm
    uid = f"{NS}w3"
    monkeypatch.setattr(rm, "credential_workflows_write", lambda u, p: None)
    _push("github", uid, monkeypatch)
    assert push_env["workflows_kept"] is False


def test_undeclared_gitcode_falls_back_to_keep(push_env, monkeypatch):
    """未声明 + gitcode → 旧逻辑回退：不删（零回归的真正断言）。"""
    import src.domains.mgmt.user.repo_manager as rm
    uid = f"{NS}w4"
    monkeypatch.setattr(rm, "credential_workflows_write", lambda u, p: None)
    _push("gitcode", uid, monkeypatch)
    assert push_env["workflows_kept"] is True, "gitcode 未声明被删 workflows=行为翻转回归"


def test_oauth_path_conservative_remove(push_env, monkeypatch):
    """OAuth 路径（无 <p>_pat 声明）→ None → 旧逻辑回退：保守删。
    注意：保守默认，非平台事实——OAuth scope 不可内省，预留 oauth_scopes 声明入口。"""
    import src.domains.mgmt.user.repo_manager as rm
    uid = f"{NS}w5"
    monkeypatch.setattr(rm, "credential_workflows_write", lambda u, p: None)
    _push("github", uid, monkeypatch)
    assert push_env["workflows_kept"] is False
