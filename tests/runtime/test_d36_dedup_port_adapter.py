"""D36（S56）用例：查重策略下沉适配器——调用方零平台分支（先测后改）。

结构守卫（RED）：issues_center._find_open_issue_by_title 不得含平台分支
（_github_req 直调 / provider == "github" 分派）——策略归 VcsApiClient
端口方法内部。行为锁：S55 用例继续全绿（三平台端点/语义不变）。
"""
import inspect


def test_s56_find_open_issue_branchless_structure():
    """结构守卫：查重 wrapper 无平台分支、无直调 _github_req。"""
    from src.api import issues_center as ic
    src = inspect.getsource(ic._find_open_issue_by_title)
    # 剥注释后断言（防 docstring 提及误命中）
    stripped = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    for forbidden in ("_github_req", 'provider == "github"', '== "github"'):
        assert forbidden not in stripped, (
            f"_find_open_issue_by_title 仍含平台分支/直调: {forbidden!r} "
            f"——策略应下沉 VcsApiClient.find_open_issue_by_title")


def test_s56_client_port_exists():
    """端口方法存在且契约清晰：返回 number 或 None；传输错误抛 RuntimeError。"""
    from src.domains.runtime.vcs.api_client import VcsApiClient
    assert hasattr(VcsApiClient, "find_open_issue_by_title")


def test_s56_find_impl_no_provider_literal():
    """结构守卫：适配器内部策略选择不得比较 provider 名——"有无搜索端点"
    是平台能力，走平台卡字段（issue_search），第 5 平台配卡即得。"""
    import inspect
    from src.domains.runtime.vcs.api_client import VcsApiClient
    src = inspect.getsource(VcsApiClient._find_open_issue_impl)
    stripped = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    for forbidden in ('== "github"', "== 'github'", '!= "github"'):
        assert forbidden not in stripped, (
            f"_find_open_issue_impl 仍写死 provider 名: {forbidden!r} "
            f"——应改为卡字段 issue_search 能力分派")


def test_s56_card_issue_search_field():
    """卡字段就位：github 内置卡 issue_search=True（搜索端点能力）；
    默认（gitcode/gitee）为 False（分页策略）。"""
    from src.domains.runtime.vcs.platform_card import get_platform_card
    gh = get_platform_card("github")
    gc = get_platform_card("gitcode")
    assert bool(getattr(gh, "issue_search", False)) is True
    assert bool(getattr(gc, "issue_search", False)) is False


def test_s56_client_error_form_classifiable():
    """契约锁：client 查重失败的 RuntimeError 消息携带可被
    token_error.extract_http_status_from_exc 识别的 (NNN) 形态——
    github bot token 401 须命中「Bot Token 失效」文案分支（D36 回归防线）。"""
    from unittest.mock import patch
    from src.domains.runtime.vcs.api_client import VcsApiClient
    from src.domains.runtime.vcs.token_error import is_auth_error_exc

    def _fake(url, method, data=None, headers=None, **kw):
        return {"error": "HTTP 401"}
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=_fake):
        c = VcsApiClient("github", "tok", "o", "r")
        try:
            c.find_open_issue_by_title("T")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert is_auth_error_exc(e), f"401 未被识别: {e}"
