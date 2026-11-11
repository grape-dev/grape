"""S89 用例（D65，先测后改）：树 API 家族卡位 + OAuth email 补抓卡位。

1. git_tree_family 能力位：github=github_api（default_branch/recursive/
   直连 ref）；gitcode/gitee/gitlab=v5（branches[0]/commits?sha 预校验/
   recursive 拒绝——存量语义）；无卡回退 v5；
2. 结构守卫：git_repo_api 树家族 p == "github" 清零（gitcode 专属
   milestone 族登记保留）；
3. providers OAuth email 补抓：渠道卡 emails_endpoint 能力位（github
   内置 /user/emails），providers.py 零平台比较。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock


def test_s89_git_tree_family_capability():
    from src.domains.runtime.vcs.platform_card import get_platform_card
    assert get_platform_card("github").git_tree_family == "github_api"
    assert get_platform_card("gitcode").git_tree_family == "v5"
    assert get_platform_card("gitee").git_tree_family == "v5"
    assert get_platform_card("gitlab").git_tree_family == "v5"


def test_s89_tree_family_no_card_fallback_v5():
    """无卡/异常回退 v5（存量 else 分支语义；github 恒有内置卡）。"""
    from src.api.git_repo_api import _tree_family
    assert _tree_family("nosuchplatform") == "v5"


def test_s89_no_platform_comparison_git_repo_api():
    """结构守卫：git_repo_api 零 p == "github"（树家族已卡化；
    gitcode 专属 milestone 族 p == "gitcode" 登记 D63 保留）。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src/api/git_repo_api.py").read_text()
    assert 'p == "github"' not in src and 'p != "github"' not in src, (
        "git_repo_api 残留 provider 变量比较（D65/S89 树家族卡位；"
        'card.dialect 判断属方言域合法保留）')


def test_s89_no_platform_comparison_providers():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src/domains/mgmt/auth/providers.py").read_text()
    assert '== "github"' not in src, "providers 残留 github 特判（email 补抓改渠道卡位）"


def test_s89_channel_emails_endpoint_builtin():
    """渠道卡 emails_endpoint：github 内置 /user/emails；gitcode 空=无补抓。"""
    from src.domains.mgmt.auth.providers import _channel_emails_endpoint
    assert _channel_emails_endpoint("github") == "https://api.github.com/user/emails"
    assert _channel_emails_endpoint("gitcode") == ""
