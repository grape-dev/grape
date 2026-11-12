"""平台卡验收矩阵（台阶1：CI 常驻化）。

把「配卡即接入」验收（tests/test_platform_onboarding_acceptance.py 的①②④⑥③手法）
收敛为参数化矩阵：configs/platforms/ 每张在册卡一个用例，逐格断言——

  ① 卡构建面：get_platform_card 非 None（Builder invariant 全过）
  ② webhook 面：_generic_backend_for 非 None / list_webhook_providers 含该平台
  ④ 工厂面：create_provider 返回实例且 provider_name 正确，非 gitcode 平台
     不落 GitCodeProvider 存量兜底
  ⑥ 凭据面：credential_registry 能解析 <p>_pat（动态 PAT 规则命中平台卡）
  ③ 资源 API 面：无模块平台看卡——卡带 resources 段走声明化翻译（正向），
     卡也无则 translate_inbound 必抛 ValueError（门槛锁常驻）

任何一张卡任何格红 → 该平台用例红；新增平台配卡即自动进矩阵（零用例改动）。
"""
from __future__ import annotations

import pytest

from src.domains.runtime.vcs.platform_card import get_platform_card, list_vcs_platforms

_ALL_PLATFORMS = list_vcs_platforms()


@pytest.mark.parametrize("provider", _ALL_PLATFORMS)
def test_matrix_card_build(provider):
    """① 卡构建面：卡可构建（D14 invariant 全过）且在册。"""
    card = get_platform_card(provider)
    assert card is not None
    assert card.provider == provider
    assert card.host


@pytest.mark.parametrize("provider", _ALL_PLATFORMS)
def test_matrix_webhook_surface(provider):
    """② webhook 面：GenericBackend 可用 + 动态路由清单含该平台。"""
    from src.domains.runtime.vcs.adapter import _generic_backend_for, list_webhook_providers
    assert _generic_backend_for(provider) is not None
    assert provider in dict(list_webhook_providers())


@pytest.mark.parametrize("provider", _ALL_PLATFORMS)
def test_matrix_factory(provider):
    """④ 工厂面：create_provider 返回正确实例，不误落 GitCode 兜底。

    gitcode 本体返回 GitCodeProvider 是原生类（非兜底语义）；其余平台
    不得落 GitCodeProvider（配卡未生效误兜底的信号）。
    """
    from src.kernel.token_resolver import GitCodeProvider, create_provider
    p = create_provider(provider)
    assert p is not None
    assert p.provider_name == provider
    if provider != "gitcode":
        assert not isinstance(p, GitCodeProvider)


@pytest.mark.parametrize("provider", _ALL_PLATFORMS)
def test_matrix_credential_pat(provider):
    """⑥ 凭据面：<p>_pat 经动态 PAT 规则合法化（注册表零代码扩展）。"""
    from src.domains.mgmt.user.credential_registry import lookup, validate_fields
    key = f"{provider}_pat"
    spec = lookup(key)
    assert spec is not None
    assert spec.key == key and not spec.deprecated
    ok, missing = validate_fields(key, {"token": "x"})
    assert ok is True and missing == ()


@pytest.mark.parametrize("provider", _ALL_PLATFORMS)
def test_matrix_resource_api_module_gate(provider):
    """③ 资源 API 面门槛（台阶3 卡驱动化后）：

    - 有模块（存量四平台）→ 翻译入口可用（模块回退路径）；
    - 无模块 + 卡带 resources 段 → 卡驱动正向翻译可用（纯配卡接入）；
    - 无模块 + 无 resources 卡 → translate_inbound 必抛 ValueError。
    """
    from src.domains.runtime.vcs.providers import _MODULES, translate_inbound
    if provider in _MODULES:
        assert translate_inbound is not None  # 门槛已跨越（模块在册）
        return
    card = get_platform_card(provider)
    if card is not None and card.resources:
        out = translate_inbound(provider, "issues", "create",
                                {"title": "t", "body": "b"}, {"owner": "o", "repo": "r"})
        assert isinstance(out, dict)  # 卡驱动正向路径
    else:
        with pytest.raises(ValueError, match=provider):
            translate_inbound(provider, "issues", "create",
                              {"title": "t", "body": "b"}, {"owner": "o", "repo": "r"})


def test_matrix_no_module_platform_raises():
    """③ 补充：无模块且无卡平台 translate_inbound 必抛 ValueError。

    以合成平台名锁死门槛本身（不依赖任何在册卡恰好缺模块）——
    资源 API 面"无卡不接入"的架构约束常驻 CI。
    """
    from src.domains.runtime.vcs.providers import translate_inbound
    with pytest.raises(ValueError, match="nosuchplat"):
        translate_inbound("nosuchplat", "issues", "create",
                          {"title": "t", "body": "b"}, {"owner": "o", "repo": "r"})


# ---------------------------------------------------------------------------
# 台阶3：带 resources 的卡资源 API 全绿（合成卡，进程内零外网）
# ---------------------------------------------------------------------------
_CD_RAW = {
    "name": "CardDriven", "dialect": "github", "host": "cd.example.com",
    "webhook_path": "/webhook/cd", "event_header": "X-CardDriven-Event",
    "api_base": "https://cd.example.com/api/v5", "api_auth": "query_token",
    "resources": {
        "repo_segment": "repos/{owner}/{repo}",
        "path_map": {"/pulls": "/merge_requests"},
        "query_state": {"open": "opened"},
        "rules": {"issues.update": {"body": "description",
                                    "assignee": {"drop": True},
                                    "state": {"native": "state_event",
                                              "values": {"open": "reopen", "closed": "close"}}}},
    },
}


@pytest.fixture()
def card_driven_platform(monkeypatch, tmp_path):
    """临时注册带 resources 段的合成卡（同 acceptance ①③ 手法）。"""
    import json
    import src.domains.runtime.vcs.platform_card as platform_card
    (tmp_path / "cd.json").write_text(json.dumps(_CD_RAW), encoding="utf-8")
    monkeypatch.setattr(platform_card, "_CONFIGS_DIR", str(tmp_path))
    platform_card.invalidate_platform_card_cache()
    yield get_platform_card("cd")
    platform_card.invalidate_platform_card_cache()


def test_matrix_card_resources_translation_full_green(card_driven_platform):
    """③ 全绿：带 resources 的卡，资源 API 翻译面（create/update/query/query_state）
    全部按声明生效——新平台零 providers/<name>.py 模块。"""
    from src.domains.runtime.vcs.providers import translate_inbound, translate_query_state
    assert card_driven_platform is not None
    # update：卡规则生效（body→description、assignee drop、state→state_event+值映射）
    out = translate_inbound("cd", "issues", "update",
                            {"body": "b", "assignee": "u", "state": "open",
                             "labels": ["x"]},
                            {"owner": "o", "repo": "r"})
    assert out == {"description": "b", "state_event": "reopen", "labels": ["x"]}
    # create：卡未声明该 op → github 形态缺省（字段级合并）同名透传
    assert translate_inbound("cd", "issues", "create",
                             {"title": "t", "body": "b"}, {}) == {"title": "t", "body": "b"}
    # query：github 缺省 state 规则 + 卡 query_state 覆盖
    assert translate_query_state("cd", "open") == "opened"
    assert translate_query_state("cd", "closed") == "closed"


def test_matrix_card_resources_url_and_shim(card_driven_platform):
    """③ 全绿（URL/request 面）：module_for 为纯配卡平台生成通用垫片——
    repo_segment 模板逐段编码、path_map 生效、API_BASE 取卡。"""
    from src.domains.runtime.vcs.providers import module_for, _MODULES
    assert "cd" not in _MODULES  # 前提：纯配卡，零模块
    mod = module_for("cd")
    assert mod is not None and mod.API_BASE == "https://cd.example.com/api/v5"
    assert mod.repo_segment("o", "r") == "repos/o/r"
    assert mod.repo_segment("a b", "r+x") == "repos/a%20b/r%2Bx"
    assert mod.PATH_MAP == {"/pulls": "/merge_requests"}
    from src.api.git_repo_api import _vcs_url
    url = _vcs_url("a b", "r", "/pulls", None, "cd")
    assert url == "https://cd.example.com/api/v5/repos/a%20b/r/merge_requests"
    assert _vcs_url("o", "r", "/issues/42", None, "cd").endswith("/repos/o/r/issues/42")


def test_matrix_card_resources_gate_constants(card_driven_platform):
    """③ 全绿（接入面）：git_repo_api 支持判定对纯配卡平台放行。"""
    from src.api.git_repo_api import _is_supported_provider
    assert _is_supported_provider("cd") is True
    assert _is_supported_provider("nosuchplat_xyz") is False


@pytest.mark.parametrize("provider", _ALL_PLATFORMS)
def test_matrix_brand_color(provider):
    """台阶2 格：卡 brand_color 为合法 #RRGGBB 或空（品牌资产进卡后的 schema 锁）。"""
    card = get_platform_card(provider)
    c = card.brand_color
    assert c == "" or (len(c) == 7 and c[0] == "#"
                       and all(ch in "0123456789abcdefABCDEF" for ch in c[1:]))
