"""S90 用例（D66，先测后改）：模板 push 目标 host 卡派生 + tarball 源平台分派表。

1. push 目标 host 派生：github 走存量分支（行为不变）；**非 github 目标
   不再硬编码 gitcode.com**（gitee 建模板仓此前会推错 host——真二分缺陷）；
2. tarball 源平台分派表（_TEMPLATE_SOURCE_DOWNLOADERS）：github 源走 gh
   tarball、gitcode/未知源走 gitcode shallow-clone 兜底（含 github 源无
   clone_url 的误配置修正：不再误用 gitcode 下载器）。
"""
import pytest


def _push(template, provider="gitcode", monkeypatch=None, calls=None):
    from src.domains.mgmt.user import repo_manager as rm
    monkeypatch.setattr(rm, "_run_git", lambda *a, **k: calls.append(a[0]) or "")
    monkeypatch.setattr(rm, "_run_git_has_origin", lambda w: False)
    monkeypatch.setattr(rm, "_git_has_changes", lambda w: True)
    monkeypatch.setattr(rm, "_download_gitcode_tarball", lambda *a, **k: None)
    monkeypatch.setattr(rm, "_download_and_extract_tarball", lambda *a, **k: None)
    tpl = dict(template)
    tpl.setdefault("source", "git")
    tpl.setdefault("source_repo", "o/src")
    tpl.setdefault("clone_url", "https://example.com/o/src.git")
    return rm.push_template_to_repo(tpl, "me", "tgt", "user_tok",
                                    bot_token="", provider=provider)


def test_s90_push_target_host_from_card_gitee(monkeypatch):
    """gitee 目标：remote add URL host 来自平台卡（不再 gitcode.com）。"""
    calls = []
    assert _push({"id": "t"}, provider="gitee",
                 monkeypatch=monkeypatch, calls=calls) is True
    remote = [c for c in calls if c[:2] == ["remote", "add"]]
    assert remote and "gitee.com/me/tgt.git" in remote[0][3]
    assert "gitcode.com" not in remote[0][3]


def test_s90_push_target_host_gitcode_parity(monkeypatch):
    """gitcode 目标：URL 逐字节语义不变（oauth2:user_tok@gitcode.com）。"""
    calls = []
    assert _push({"id": "t"}, provider="gitcode",
                 monkeypatch=monkeypatch, calls=calls) is True
    remote = [c for c in calls if c[:2] == ["remote", "add"]]
    assert remote and remote[0][3] == "https://oauth2:user_tok@gitcode.com/me/tgt.git"


def test_s90_push_target_github_legacy_branch(monkeypatch):
    """github 目标：存量分支行为不变（***@github.com）。"""
    calls = []
    assert _push({"id": "t"}, provider="github",
                 monkeypatch=monkeypatch, calls=calls) is True
    remote = [c for c in calls if c[:2] == ["remote", "add"]]
    assert remote and "github.com/me/tgt.git" in remote[0][3]


def test_s90_tarball_dispatch_table():
    """源平台分派表：github 源走 gh tarball（含 GITHUB_TOKEN 头）；未知源回落 gitcode。"""
    from src.domains.mgmt.user import repo_manager as rm
    assert "github" in rm._TEMPLATE_SOURCE_DOWNLOADERS
    assert "gitcode" in rm._TEMPLATE_SOURCE_DOWNLOADERS
    assert rm._TEMPLATE_SOURCE_DOWNLOADERS.get("nosuch") is None


def test_s90_tarball_dispatch_github_source(monkeypatch):
    """github 源模板（clone_url 存在）：走 gh tarball 下载器（不再落入 gitcode 兜底）。"""
    from src.domains.mgmt.user import repo_manager as rm
    dl = []
    monkeypatch.setattr(rm, "_download_and_extract_tarball",
                        lambda url, w, t, headers=None: dl.append(url))
    monkeypatch.setattr(rm, "_run_git", lambda *a, **k: "")
    monkeypatch.setattr(rm, "_run_git_has_origin", lambda w: False)
    monkeypatch.setattr(rm, "_git_has_changes", lambda w: True)
    ok = rm.push_template_to_repo(
        {"id": "t", "source": "git", "source_platform": "github",
         "clone_url": "https://github.com/o/r.git", "source_repo": "o/r"},
        "me", "tgt", "user_tok", bot_token="", provider="gitcode")
    assert ok is True
    assert dl and dl[0] == "https://api.github.com/repos/o/r/tarball"
