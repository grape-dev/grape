"""Issue #4231：仓库名称校验抽公共工具，check-name 与 create 同口径。

验收覆盖：
- 含空格/中文/超长名称在 check-name 入口直接 400，且不触达 token/上游（0 次调用）；
- create 复用同一工具（旧内联 `^[\\w\\-\\.]+$` 因 Python 的 Unicode 语义会放行中文，
  本用例锁死该回归）；
- 后端只回裸原因，不再拼「检查失败:」，避免与前端模板叠加成双前缀。
"""

import pytest
from fastapi import HTTPException

from src.domains.mgmt.routes import repos as repos_mod
from src.domains.mgmt.user.repo_naming import REPO_NAME_MAX_LENGTH, validate_repo_name

_USER = {"id": 1, "username": "u-4231", "login_provider": "gitcode"}


@pytest.fixture()
def upstream_calls(monkeypatch):
    calls = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr(repos_mod, "get_user_token_for", _record)
    monkeypatch.setattr(repos_mod, "_get_user_provider_and_token", _record)
    return calls


@pytest.mark.parametrize("name", ["repo", "my-repo_1.2", "a" * REPO_NAME_MAX_LENGTH])
def test_validate_accepts_legal_names(name):
    assert validate_repo_name(name) == (True, "")


@pytest.mark.parametrize("name,reason_part", [
    ("", "不能为空"),
    ("   ", "不能为空"),
    ("a" * (REPO_NAME_MAX_LENGTH + 1), "不能超过"),
    ("my repo", "只能包含"),
    ("项目名称", "只能包含"),
    ("repo/name", "只能包含"),
    (".", "不能为"),
    ("..", "不能为"),
])
def test_validate_rejects_bad_names(name, reason_part):
    ok, reason = validate_repo_name(name)
    assert ok is False
    assert reason_part in reason
    assert "检查失败" not in reason


@pytest.mark.parametrize("name", ["my repo", "项目名称", "a" * (REPO_NAME_MAX_LENGTH + 1), ".", ".."])
def test_check_name_rejects_invalid_before_upstream(upstream_calls, name):
    with pytest.raises(HTTPException) as exc:
        repos_mod.check_repo_name(name=name, provider="", owner="", user=_USER)
    assert exc.value.status_code == 400
    msg = exc.value.detail["message"]
    assert msg and "检查失败" not in msg
    assert upstream_calls == [], "非法名称不得触达 token/上游调用"


def test_check_name_valid_name_still_resolves_token(upstream_calls):
    with pytest.raises(HTTPException) as exc:
        repos_mod.check_repo_name(name="my-repo_1.2", provider="gitcode", owner="", user=_USER)
    assert exc.value.status_code == 400
    assert exc.value.detail["message"] == "API Token 未配置"
    assert upstream_calls, "合法名称应继续走 token 解析，不被本地校验拦截"


def test_create_blocks_chinese_name_like_check_name(upstream_calls):
    body = repos_mod.CreateRepoBody(name="项目名称")
    with pytest.raises(HTTPException) as exc:
        repos_mod.create_new_repo(body, user=_USER)
    assert exc.value.status_code == 400
    assert "只能包含" in exc.value.detail["message"]
    assert upstream_calls == []


def test_create_path_uses_same_charset_rule(upstream_calls):
    body = repos_mod.CreateRepoBody(name="ok-name", path="路径")
    with pytest.raises(HTTPException) as exc:
        repos_mod.create_new_repo(body, user=_USER)
    assert exc.value.status_code == 400
    assert exc.value.detail["message"].startswith("仓库路径")


@pytest.mark.parametrize("name", ["my repo", "项目名称", "a" * (REPO_NAME_MAX_LENGTH + 1)])
def test_create_and_check_name_report_same_reason(upstream_calls, name):
    def _outcome(fn):
        try:
            fn()
        except HTTPException as exc:
            return exc.status_code, exc.detail.get("message")
        return 200, None

    def _check():
        repos_mod.check_repo_name(name=name, provider="", owner="", user=_USER)

    def _create():
        repos_mod.create_new_repo(repos_mod.CreateRepoBody(name=name), user=_USER)

    assert _outcome(_check) == _outcome(_create)
