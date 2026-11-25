"""Issue #3580 复审（对齐 PR #1854）：API 侧私有 CA + 不跟随重定向。

RED→GREEN：
1. 平台卡 `ca_bundle` 此前只进 git 传输层（repogit），API 侧全走默认 SSL 上下文 →
   私有 CA 自建实例的列仓/托管/建钩/Issue/MR 全部 TLS 失败；
2. 带 `PRIVATE-TOKEN` 的 3xx 会被 urllib 把同一份 header 带到重定向目标（异域泄漏），
   GitLab 方言族上游请求必须不跟随重定向；
3. 非 GitLab 平台保持存量语义（跟随、系统 CA）——零回归；
4. 四个上游调用点（provider 模块 / token_resolver / api_client / 托管钩子）都经统一入口。
"""
from __future__ import annotations

import inspect
import os

import pytest

from src.kernel import upstream_http as uh


class _FakeCard:
    def __init__(self, provider, api_base, dialect, ca_bundle=""):
        self.provider = provider
        self.api_base = api_base
        self.dialect = dialect
        self.ca_bundle = ca_bundle


@pytest.fixture()
def cards(monkeypatch):
    cards = {
        "gitlab-corp": _FakeCard("gitlab-corp", "https://gl.corp/api/v4", "gitlab", "/tmp/gl-ca.pem"),
        "gitcode": _FakeCard("gitcode", "https://gitcode.com/api/v5", "gitcode"),
        "gitlab": _FakeCard("gitlab", "https://gitlab.com/api/v4", "gitlab"),
    }
    monkeypatch.setattr("src.domains.runtime.vcs.platform_card.get_platform_card",
                        lambda p: cards.get(p))
    # 端口面两个符号都要打桩：`policy_for` 走的是 `kernel.ports.vcs_cards`，
    # 不是 domains 模块。只打 list_vcs_platforms 时，get_platform_card 仍会落回
    # 端口绑定——端口在本文件之前被别的用例解绑/替换过就抛 PortNotBound，被
    # `policy_for` 吞成 ("", True)，于是全量跑时本文件的断言假红、单文件跑全绿
    # （P2 测试隔离：`tests/kernel/test_upstream_policy_cache_ttl.py` 之后即可复现）。
    monkeypatch.setattr("src.kernel.ports.vcs_cards.list_vcs_platforms",
                        lambda: list(cards.keys()))
    monkeypatch.setattr("src.kernel.ports.vcs_cards.get_platform_card",
                        lambda p: cards.get(p))
    uh.reset_cache()
    yield cards
    uh.reset_cache()


def test_policy_takes_card_ca_and_disables_redirects_for_gitlab(cards):
    ca, follow = uh.policy_for("https://gl.corp/api/v4/projects/a%2Fb/issues")
    assert ca == "/tmp/gl-ca.pem"
    assert follow is False


def test_policy_keeps_legacy_semantics_for_other_platforms(cards):
    assert uh.policy_for("https://gitcode.com/api/v5/repos/a/b/issues") == ("", True)
    assert uh.policy_for("https://example.com/whatever") == ("", True)


def test_no_redirect_handler_refuses_to_follow():
    # pyright: ignore[reportArgumentType] —— 处理函数只关心返回 None
    assert uh._NoRedirect().redirect_request(
        None, None, 302, "", {}, "https://evil") is None  # type: ignore[arg-type]


def test_ssl_context_plain_and_missing_ca():
    assert uh.ssl_context("") is None
    assert uh.ssl_context("/definitely/missing/ca.pem") is None       # 回落系统 CA，不抛错


def test_ssl_context_with_system_bundle():
    for cand in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(cand):
            assert uh.ssl_context(cand) is not None
            return
    pytest.skip("no system CA bundle on this host")


def test_all_upstream_call_sites_use_the_policy_entry():
    from src.domains.mgmt.vcs import base as vcs_base
    from src.domains.runtime.vcs import api_client
    from src.kernel import vcs_gitlab_hooks

    # Issue #3386 复审：_http 拆出 _http_with_headers（响应头复用），
    # 统一入口落在 _http_with_headers；_http 是其零逻辑委托包装。
    for fn in (vcs_base.GenericVcsProvider._http_with_headers,
               api_client.VcsApiClient._do_request,
               vcs_gitlab_hooks._request):
        assert "open_with_policy" in inspect.getsource(fn), f"{fn} 未走上游统一入口"
