"""S88 用例（D64，先测后改）：bot 身份来源链数据驱动 + git_repo_api 方言位收口。

三块：
1. resolve_bot_username 语义锁（数据驱动重构前后逐条保持）：
   github=独立单值源 / gitcode=pool 首位 / gitlab=仅卡 / config-only=仅卡 /
   未知平台=gitcode pool（存量兜底语义）；
2. get_global_bot_usernames_for 语义回归（S87 helper，self-loop 链共用）；
3. git_repo_api 方言位：labels 拆分请求 / PR 评论端点路径由卡 dialect 派生
   （_card_dialect helper：gitee→github 方言 / gitlab→gitlab / 无卡→github 存量默认）。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _env():
    with db_lock:
        db.cursor().execute("INSERT OR REPLACE INTO meta (key,value) VALUES "
                            "('global_bot_github_username','gh-bot-x')")
        db.cursor().execute("INSERT OR REPLACE INTO meta (key,value) VALUES "
                            "('global_bot_usernames','[\"gc-bot-a\",\"gc-bot-b\"]')")
        db.commit()
    # 60s TTL 缓存可能持有此前用例的文件 fallback 值（configs/global-bot-token.json
    # 在开发机存在时毒化断言）——与 test_global_bot_token_db.py 同款清缓存手法
    from src.domains.mgmt.user.db import _invalidate_global_bot_cache
    _invalidate_global_bot_cache()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key IN "
                            "('global_bot_github_username','global_bot_usernames')")
        db.commit()
    _invalidate_global_bot_cache()


# ---------- 1. resolve_bot_username 语义锁 ----------

def test_s88_bot_username_github_single_value_source():
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    assert resolve_bot_username("github") == "gh-bot-x"


def test_s88_bot_username_gitcode_pool_first():
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    assert resolve_bot_username("gitcode") == "gc-bot-a"


def test_s88_bot_username_gitlab_card_only():
    """gitlab 无全局 bot 源（S87 语义陷阱）：只走卡，绝不误挂 gitcode pool。"""
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    assert resolve_bot_username("gitlab") == ""


def test_s88_bot_username_config_only_card_only():
    """config-only 平台（gitee）：仅卡 bot_identity。"""
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    assert resolve_bot_username("gitee") == ""


def test_s88_bot_username_unknown_provider_fail_closed():
    """未知平台（无卡）：fail-closed ValueError（D105-17，用户裁定废除 pool 兜底）。"""
    import pytest as _pytest
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    with _pytest.raises(ValueError) as ei:
        resolve_bot_username("nosuchplatform")
    assert "nosuchplatform" in str(ei.value)


# ---------- 2. S87 helper 语义回归 ----------

def test_s88_global_bot_usernames_for_regression():
    from src.domains.mgmt.user.db import get_global_bot_usernames_for
    assert get_global_bot_usernames_for("github") == ["gh-bot-x"]
    assert get_global_bot_usernames_for("gitcode") == ["gc-bot-a", "gc-bot-b"]
    assert get_global_bot_usernames_for("gitee") == ["gc-bot-a", "gc-bot-b"]


# ---------- 3. git_repo_api 方言位 ----------

def test_s88_pr_comment_endpoint_capability():
    """评论端点能力位：github=issues（inline-review 怪癖）；gitcode/gitee=pulls（同方言但 v5 端点正常）。"""
    from src.domains.runtime.vcs.platform_card import get_platform_card
    assert get_platform_card("github").pr_comment_endpoint == "issues"
    assert get_platform_card("gitcode").pr_comment_endpoint == "pulls"
    assert get_platform_card("gitee").pr_comment_endpoint == "pulls"  # 同方言≠同怪癖
    assert get_platform_card("gitlab").pr_comment_endpoint == "pulls"


def test_s88_pr_labels_via_issues_capability():
    from src.domains.runtime.vcs.platform_card import get_platform_card
    assert get_platform_card("github").pr_labels_via_issues is True
    assert get_platform_card("gitcode").pr_labels_via_issues is False
    assert get_platform_card("gitee").pr_labels_via_issues is False


def test_s88_no_platform_comparison_in_labels_and_comment_paths():
    """结构守卫：labels 拆分 / PR 评论路径不再内联 p == "github"。"""
    import pathlib, re
    src = (pathlib.Path(__file__).resolve().parent.parent / "src/api/git_repo_api.py").read_text()
    # 提取 update_pull_request 与 PR 评论端点两个函数体（按标记行就近断言）
    assert '_gh_labels = _labels if p == "github"' not in src, (
        "labels 拆分请求仍内联平台比较——改 _card_dialect 派生（D64/S88）")
    assert '_comments_path = f"/issues/{number}/comments" if p == "github"' not in src, (
        "PR 评论端点路径仍内联平台比较——改 _card_dialect 派生（D64/S88）")
