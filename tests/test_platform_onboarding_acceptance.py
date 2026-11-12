"""「配卡即接入」最小验收用例集（扫雷⑦机制落地，UC65）。

目标：把"新增一个仓库托管平台"的最小面固化为一组进程内断言——
临时写一张 foo 平台卡（tmp 目录 + monkeypatch _CONFIGS_DIR + 缓存失效，
不连外网），逐面验收卡驱动的接入链路：

  ① 卡注册面：build_platform_card / list_vcs_platforms 含 foo
  ② webhook 面：_generic_backend_for 非 None / list_webhook_providers 含 foo
  ③ 资源 API 面：卡 resources 段驱动——translate_inbound('foo',...) 正向映射
     （台阶3：声明化规则 + github 形态字段级缺省合并，纯配卡零模块）；
     负向：非法资源/非法 op 仍被拒（显式声明原则）
  ④ 工厂面：create_provider('foo') 返回 GenericVcsProvider（provider_name
     =='foo'），不落 GitCodeProvider 兜底
  ⑤ 建仓面：create_repo 走卡驱动 _http（进程内 mock，断言非 error dict）
  ⑥ 凭据面：credential_registry 能解析 foo_pat（动态 PAT 规则命中平台卡）
  ⑦ bot 身份面：resolve_bot_username('foo') 有明确行为（卡 bot_username）

机制说明见 docs/weekly-09/multi-platform-decouple/d105e-platform-onboarding-acceptance-design.md。
"""
from __future__ import annotations

import json

import pytest

import src.domains.runtime.vcs.platform_card as platform_card
from src.domains.runtime.vcs.platform_card import (
    PlatformCard, build_platform_card, get_platform_card,
    invalidate_platform_card_cache, list_vcs_platforms,
)

# foo 卡：github 方言族 + 显式 bot_username（⑦ 断言载体）
# 台阶3：卡带 resources 段 → 资源 API 面纯配卡接入（③ 由预期失败翻正）。
# rules 演示声明化映射（body→description 覆盖 github 缺省字段规则，逐字段合并）。
FOO_CARD_RAW = {
    "name": "Foo", "dialect": "github", "host": "foo.example.com",
    "webhook_path": "/webhook/foo", "event_header": "X-Foo-Event",
    "api_base": "https://foo.example.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "hmac", "clone_auth": "x_access_token",
    "bot_username": "foo_bot",
    "resources": {
        "repo_segment": "repos/{owner}/{repo}",
        "query_state": {"open": "opened"},
        "rules": {
            "issues.create": {"body": "description"},
        },
    },
}


@pytest.fixture()
def foo_platform(monkeypatch, tmp_path):
    """临时 foo 平台卡注册：tmp configs 目录 + 卡缓存失效（进程内，零外网）。

    平台卡注册 = _load_cards() 扫 _CONFIGS_DIR 目录（import 后惰性加载），
    故 monkeypatch 模块级 _CONFIGS_DIR 指向 tmp 目录 + invalidate 即完成注册；
    用例结束 invalidate 恢复内置默认，不污染同进程其它用例。
    """
    (tmp_path / "foo.json").write_text(json.dumps(FOO_CARD_RAW), encoding="utf-8")
    monkeypatch.setattr(platform_card, "_CONFIGS_DIR", str(tmp_path))
    invalidate_platform_card_cache()
    yield get_platform_card("foo")
    invalidate_platform_card_cache()


def _assert_foo_card(card):
    assert isinstance(card, PlatformCard)
    assert card.provider == "foo" and card.host == "foo.example.com"


# ---------------------------------------------------------------------------
# ① 卡注册面
# ---------------------------------------------------------------------------
def test_a1_card_build_and_registry(foo_platform):
    """配卡即接入第①面：卡可构建且进入平台清单。"""
    _assert_foo_card(foo_platform)
    assert "foo" in list_vcs_platforms()


# ---------------------------------------------------------------------------
# ② webhook 面
# ---------------------------------------------------------------------------
def test_a2_webhook_surface(foo_platform):
    """第②面：webhook 链路（GenericBackend + 动态路由清单）无需任何代码。"""
    from src.domains.runtime.vcs.adapter import _generic_backend_for, list_webhook_providers
    gb = _generic_backend_for("foo")
    assert gb is not None
    providers = dict(list_webhook_providers())
    assert providers.get("foo") == "/webhook/foo"


# ---------------------------------------------------------------------------
# ③ 资源 API 面（台阶3 翻正：卡 resources 段驱动，配卡即接入）
# ---------------------------------------------------------------------------
def test_a3_resource_api_card_driven(foo_platform):
    """第③面正向：translate_inbound 走卡 resources 声明化规则。

    断言返回映射的**具体结构**（防"静默空映射"假绿）：
    - 卡规则 body→description 覆盖 github 缺省（逐字段合并语义）；
    - title 无卡规则 → github 缺省同名透传（缺省仅覆盖 github 已定义资源类型）。
    """
    from src.domains.runtime.vcs.providers import translate_inbound
    out = translate_inbound("foo", "issues", "create",
                            {"title": "t", "body": "b", "assignee": ""},
                            {"owner": "o", "repo": "r"})
    assert out == {"title": "t", "description": "b"}


def test_a3_resource_api_query_state_card_driven(foo_platform):
    """第③面正向（query state）：卡 query_state 逐键覆盖 github 恒等缺省。"""
    from src.domains.runtime.vcs.providers import translate_query_state
    assert translate_query_state("foo", "open") == "opened"   # 卡覆盖
    assert translate_query_state("foo", "closed") == "closed"  # github 缺省


def test_a3_resource_api_undeclared_resource_rejected(foo_platform):
    """第③面负向：非法资源类型仍被拒——显式声明原则（github 缺省只覆盖
    github 已定义资源类型，卡未声明又不属 github 内置 → ValueError）。"""
    from src.domains.runtime.vcs.providers import translate_inbound
    with pytest.raises(ValueError, match="snippets"):
        translate_inbound("foo", "snippets", "create",
                          {"title": "t"}, {"owner": "o", "repo": "r"})


def test_a3_resource_api_undeclared_op_rejected(foo_platform):
    """第③面负向：非法 op 同样被拒（卡与 github 缺省都未定义该 resource.op）。"""
    from src.domains.runtime.vcs.providers import translate_inbound
    with pytest.raises(ValueError, match="frobnicate"):
        translate_inbound("foo", "issues", "frobnicate",
                          {"title": "t"}, {"owner": "o", "repo": "r"})


# ---------------------------------------------------------------------------
# ④ 工厂面
# ---------------------------------------------------------------------------
def test_a4_create_provider_card_driven(foo_platform):
    """第④面：create_provider 卡驱动，不落 GitCodeProvider 存量兜底。"""
    from src.kernel.token_resolver import GitCodeProvider, create_provider
    p = create_provider("foo")
    assert p is not None
    assert p.provider_name == "foo"
    assert not isinstance(p, GitCodeProvider)
    from src.kernel.token_resolver import GenericVcsProvider
    assert isinstance(p, GenericVcsProvider)


# ---------------------------------------------------------------------------
# ⑤ 建仓面（进程内 mock _http，不连外网）
# ---------------------------------------------------------------------------
def test_a5_create_repo_card_path(foo_platform, monkeypatch):
    """第⑤面：卡驱动建仓走 GenericVcsProvider.create_repo（POST /user/repos，
    卡 api_base/api_auth 决定形态）。HTTP 层 mock 为平台返回 dict——
    断言返回非 error dict（卡驱动路径形态），离线可重复。
    """
    from src.kernel.token_resolver import GenericVcsProvider, create_provider

    captured = {}

    def _fake_http(self, method, path, token, data=None, form=False):
        captured.update(method=method, path=path, form=form, data=dict(data or {}))
        return {"name": "demo", "full_name": "someone/demo", "id": 123}

    monkeypatch.setattr(GenericVcsProvider, "_http", _fake_http)
    p = create_provider("foo")
    out = p.create_repo("tok", "demo", description="d", private=True)
    assert isinstance(out, dict) and "error" not in out
    assert out.get("full_name") == "someone/demo"
    # 形态由卡决定：github 方言默认 query_token → form 编码（Gitee 系 v5 语义）
    assert captured["method"] == "POST" and captured["path"] == "/user/repos"
    assert captured["form"] is True
    assert captured["data"]["name"] == "demo"


# ---------------------------------------------------------------------------
# ⑥ 凭据面
# ---------------------------------------------------------------------------
def test_a6_credential_registry_resolves_pat(foo_platform):
    """第⑥面：foo_pat 经动态 PAT 规则（键形态 + 平台卡在册）合法化，
    注册表无需为每个新平台改代码。"""
    from src.domains.mgmt.user.credential_registry import lookup, validate_fields
    spec = lookup("foo_pat")
    assert spec is not None
    assert spec.key == "foo_pat" and not spec.deprecated
    ok, missing = validate_fields("foo_pat", {"token": "x"})
    assert ok is True and missing == ()


# ---------------------------------------------------------------------------
# ⑦ bot 身份面
# ---------------------------------------------------------------------------
def test_a7_bot_username_resolves(foo_platform):
    """第⑦面：bot 身份有明确行为——卡 bot_username 即返回值（foo 卡显式
    配置 foo_bot；不配卡/卡空串=无 bot 协作能力，认领流降级）。"""
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    assert resolve_bot_username("foo") == "foo_bot"


# ---------------------------------------------------------------------------
# 反向锁：无卡平台不接入（防"处处兜底成 gitcode"的假接入）
# ---------------------------------------------------------------------------
def test_a8_unknown_platform_not_onboarded():
    """未配卡平台不在平台清单/路由中——「配卡即接入」的另一面是「无卡即不接入」。"""
    from src.domains.runtime.vcs.adapter import _generic_backend_for
    assert get_platform_card("nosuchplatform_xyz") is None
    assert _generic_backend_for("nosuchplatform_xyz") is None
