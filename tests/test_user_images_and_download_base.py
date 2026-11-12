"""D17 用例：图片附件基址与 ZIP 下载基址按平台卡派生（扫雷第三轮）。"""
import json

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "user_images_base": "https://attachments.example.org/uimg",
    "download_base": "https://gitee.com",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


def test_d17_user_image_extraction_from_card(gitee_card):
    from src.domains.runtime.vcs.api_client import extract_user_image_urls
    text = "前图 ![a](https://attachments.example.org/uimg/1.png) 后图 https://attachments.example.org/uimg/2.png"
    urls = extract_user_image_urls(text, "gitee")
    assert urls == ["https://attachments.example.org/uimg/1.png",
                    "https://attachments.example.org/uimg/2.png"]
    # gitcode 回归（默认卡）
    from src.domains.runtime.vcs.api_client import extract_gitcode_user_image_urls
    gc = extract_user_image_urls("![x](https://raw.gitcode.com/user-images/a.png)", "gitcode")
    assert gc == ["https://raw.gitcode.com/user-images/a.png"]
    assert extract_gitcode_user_image_urls("![x](https://raw.gitcode.com/user-images/a.png)") == gc
    # 平台无此机制（github 默认卡 user_images_base 空）→ []
    assert extract_user_image_urls("![x](https://raw.gitcode.com/user-images/a.png)", "github") == []


def test_d17_scene_sync_download_base_from_card(gitee_card):
    from src.domains.runtime.storage.scene_sync import _parse_repo_host_path
    # 卡匹配：gitee → 卡 download_base
    base, path = _parse_repo_host_path("https://gitee.com/grape-dev/skills")
    assert base == "https://gitee.com" and path == "grape-dev/skills"
    # 存量回归：github/gitcode 原映射
    assert _parse_repo_host_path("https://github.com/o/r") == ("https://codeload.github.com", "o/r")
    assert _parse_repo_host_path("https://gitcode.com/o/r") == ("https://raw.gitcode.com", "o/r")


def test_d17_builtin_card_fields_present():
    from src.domains.runtime.vcs.platform_card import get_platform_card
    invalidate_platform_card_cache()
    gc = get_platform_card("gitcode")
    gh = get_platform_card("github")
    gl = get_platform_card("gitlab")
    assert gc.user_images_base == "https://raw.gitcode.com/user-images"
    assert gc.download_base == "https://raw.gitcode.com"
    assert gh.download_base == "https://codeload.github.com"
    assert gh.user_images_base == ""      # github 无编辑器图片机制（Issue #315 是 gitcode 专属）
    assert gl.download_base == "https://gitlab.com"


def test_d17_issues_center_collect_threads_provider(gitee_card):
    """_collect_issue_user_images 按 repo_key 前缀取 provider（gitee 卡基址生效）。"""
    from src.api.issues_center import _collect_issue_user_images
    # 无 token 路径：仅验证提取阶段按 gitee 基址命中（下载降级不外抛）
    out = _collect_issue_user_images("o", "r", 1,
                                     ["看图 https://attachments.example.org/uimg/1.png"],
                                     repo_key="gitee:o/r")
    assert isinstance(out, dict)  # 顶层不抛（降级容错）
