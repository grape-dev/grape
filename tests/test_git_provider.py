"""Git provider 中性翻译层测试（git-repo-provider-neutral-interface-design.md §6.1 D1-D7）。

覆盖：pulls/issues 的 inbound 双 provider 翻译、旧字段别名归一、query state、未知 provider。
"""
import pytest

from src.vcs import providers
from src.vcs.providers import translate_inbound, translate_query_state

OWNER_REPO = "o/r"


def test_t1_pulls_create_gitcode_load():
    neutral = {"title": "feat: #5", "head": "feat/x", "base": "main", "body": "",
               "related_issue_nums": [5], "auto_close_issues": True}
    out = translate_inbound("gitcode", "pulls", "create", neutral, {"owner_repo": OWNER_REPO})
    assert out["title"] == "feat: #5"
    assert out["inner_issue_nums"] == "o/r#5"
    assert out["close_related_issue"] is True
    assert "related_issue_nums" not in out
    assert "auto_close_issues" not in out


def test_t2_pulls_create_github_load():
    neutral = {"title": "feat: #5", "head": "feat/x", "base": "main", "body": "",
               "related_issue_nums": [5], "auto_close_issues": True}
    out = translate_inbound("github", "pulls", "create", neutral, {"owner_repo": OWNER_REPO})
    assert out["head"] == "feat/x" and out["base"] == "main"
    assert "Closes #5" in out["body"]
    assert "inner_issue_nums" not in out
    assert "close_related_issue" not in out
    assert "related_issue_nums" not in out


def test_t4_issues_create_gitcode_no_repo_field():
    neutral = {"title": "t", "body": "d", "assignee": "bob"}
    out = translate_inbound("gitcode", "issues", "create", neutral, {"owner_repo": OWNER_REPO})
    assert out["title"] == "t" and out["assignee"] == "bob"
    assert "repo" not in out


def test_t5_issues_update_state_normalized():
    github = translate_inbound("github", "issues", "update", {"state": "close"}, {"owner_repo": OWNER_REPO})
    assert github["state"] == "closed"
    gitcode = translate_inbound("gitcode", "issues", "update", {"state": "closed"}, {"owner_repo": OWNER_REPO})
    assert gitcode["state"] == "close"
    empty = translate_inbound("github", "issues", "update", {"state": ""}, {"owner_repo": OWNER_REPO})
    assert "state" not in empty


def test_t6_query_state_normalized():
    assert translate_query_state("gitcode", "open") == "opened"
    assert translate_query_state("github", "open") == "open"
    assert translate_query_state("gitcode", "all") == "all"


def test_t7_assignees_converted():
    gitcode = translate_inbound("gitcode", "issues", "update", {"assignee": "bob"}, {"owner_repo": OWNER_REPO})
    assert gitcode["assignee"] == "bob"
    github = translate_inbound("github", "issues", "create", {"assignee": "bob"}, {"owner_repo": OWNER_REPO})
    assert github["assignees"] == ["bob"]


def test_t10_unknown_provider_raises():
    with pytest.raises(ValueError):
        translate_inbound("gitlab", "issues", "create", {}, {})


def test_module_import_and_registry():
    assert set(providers._MODULES) == {"gitcode", "github"}


def test_provider_required_fail_fast_no_gitcode_default():
    """看护：git_repo_api 不得再硬编码 provider 默认 gitcode；漏传 provider 的调用必须 fail-fast。"""
    import inspect
    from src.api import git_repo_api as g

    src = inspect.getsource(g)
    assert 'provider="gitcode"' not in src
    assert 'provider: str = ""' in src
    assert '(owner, repo, "", None)' not in src  # 旧漏传形式已消除

    with pytest.raises(ValueError):
        g._vcs_url("o", "r", "", None, "gitlab")
    with pytest.raises(ValueError):
        g._vcs_url("o", "r", "", None, "")
    with pytest.raises(ValueError):
        g._repo_token("o", "r", {}, "gitlab")


def test_all_route_calls_pass_provider():
    """看护：文件中每次 _vcs_url/_repo_token 调用都显式传 provider（p 或 'gitcode'/'github'）。"""
    import re
    from pathlib import Path

    src = Path("src/api/git_repo_api.py").read_text(encoding="utf-8")
    pattern = re.compile(r"(_vcs_url|_repo_token)\((.*?)\)", re.S)
    for m in pattern.finditer(src):
        call = m.group(0)
        if call.startswith("def ") or "provider: str" in call:
            continue
        inner = m.group(2)
        assert re.search(r",\s*(p|\"(?:gitcode|github)\")\s*$", inner), f"missing provider: {call[:90]}"