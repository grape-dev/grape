"""跨域 clone 派生走契约 S1–S6（评审 4：mgmt 自拼地址 / token 进 URL）。

口径：clone 地址、用户名、鉴权头只有 runtime.vcs.clone_endpoint 一个真源，
mgmt 通过 runtime.public 契约取；不允许 import runtime.vcs.* 内部模块。
"""
from __future__ import annotations

import base64
import pathlib

import pytest

from src.domains.mgmt import org_context_repo as ocr
from src.domains.mgmt.user import repo_manager as rm
from src.domains.runtime import public as rt_pub


def test_s1_org_context_repo_clone_url_comes_from_the_contract(monkeypatch):
    from src.domains.runtime import public as pub

    monkeypatch.setattr(pub, "clone_url", lambda rk: "https://example.test/a/b.git")
    assert ocr._clone_url("gitee:a/b") == "https://example.test/a/b.git"


def test_s2_repo_manager_uses_header_and_keeps_token_out_of_the_url(monkeypatch):
    seen = {"calls": []}

    def _run(args, cwd, timeout=120):
        seen["calls"].append(list(args))
        return ""

    monkeypatch.setattr(rm, "_run_git", _run)
    monkeypatch.setattr(rt_pub, "clone_url", lambda rk: "https://gitee.test/o/r.git")
    monkeypatch.setattr(rt_pub, "clone_auth_header",
                        lambda rk, tok: "Basic " + base64.b64encode(f"o:{tok}".encode()).decode())
    rm._download_gitcode_tarball("gitee:o/r", "/tmp/w", "/tmp", "tok-123")
    clone_call = next(c for c in seen["calls"] if "clone" in c)
    joined = " ".join(clone_call)
    assert "https://gitee.test/o/r.git" in joined
    assert "http.extraheader=Authorization: Basic " in joined
    assert "tok-123" not in joined, "token 不得出现在 URL/命令行明文里"
    assert "@gitcode.com" not in joined


@pytest.mark.parametrize("repo_key,expect_user", [
    ("gitee:acme/r", "acme"),
    ("github:acme/r", "x-access-token"),
])
def test_s3_clone_auth_header_follows_the_card(monkeypatch, repo_key, expect_user):
    monkeypatch.setattr(
        rt_pub, "clone_params",
        lambda rk: {"clone_auth": "basic_password" if rk.startswith("gitee") else "x_access_token",
                    "username": "" if rk.startswith("gitee") else "x-access-token",
                    "owner": "acme"},
    )
    header = rt_pub.clone_auth_header(repo_key, "tok")
    user = base64.b64decode(header.split(" ", 1)[1]).decode().split(":", 1)[0]
    assert user == expect_user


def test_s4_clone_identity_accepts_username_and_name(monkeypatch):
    from src.domains.runtime.vcs import clone_identity as ci
    import src.kernel.token_resolver as tr

    class _P:
        def __init__(self, info):
            self._info = info

        def verify_token(self, token):
            return self._info

    monkeypatch.setattr(tr, "create_provider", lambda p: _P({"username": "gl-user"}))
    assert ci._fetch_login("gitlab", "tok") == "gl-user"

    monkeypatch.setattr(tr, "create_provider", lambda p: _P({"name": "named"}))
    assert ci._fetch_login("gitlab", "tok") == "named"


def test_s5_mgmt_does_not_import_runtime_internals_for_clone():
    root = pathlib.Path(__file__).resolve().parents[2] / "src/domains/mgmt"
    for rel in ("routes/repos.py", "org_context_repo.py", "user/repo_manager.py"):
        text = (root / rel).read_text(encoding="utf-8")
        bad = [line for line in text.splitlines()
               if line.strip().startswith("from src.domains.runtime.")
               and "runtime.public" not in line and "runtime import public" not in line]
        assert not bad, f"{rel} 仍有直连 runtime 内部：{bad}"


def test_s6_existing_direct_imports_are_ratcheted():
    """基线已有的直连点登记在案，不允许新增。"""
    allowed = {
        "admin/repo_platform_routes.py", "routes/plugins.py", "routes/plugins_agent.py",
    }
    root = pathlib.Path(__file__).resolve().parents[2] / "src/domains/mgmt"
    offenders = set()
    for f in root.rglob("*.py"):
        rel = str(f.relative_to(root))
        for line in f.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t.startswith("from src.domains.runtime.") and "runtime.public" not in t:
                offenders.add(rel)
    assert offenders <= allowed, f"新增跨域直连：{sorted(offenders - allowed)}"