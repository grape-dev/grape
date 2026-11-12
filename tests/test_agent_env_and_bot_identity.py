"""S36/S37（D25）用例：agent 运行时注入卡派生 + 流水线 bot 身份来源链。"""
import json

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
    "bot_username": "grape_bot_gitee",
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


def test_s36_agent_env_from_card(gitee_card):
    from src.kernel.prompt_builder import _vcs_api_base, _vcs_token_env, _vcs_bot_token_env, _vcs_name
    # gitee：全卡派生（agent 不再拿 gitcode 的 base/空变量名）
    assert _vcs_api_base("gitee:o/r") == "https://gitee.com/api/v5"
    assert _vcs_token_env("gitee:o/r") == "USER_GITEE_TOKEN"
    assert _vcs_bot_token_env("gitee:o/r") == "BOT_GITEE_TOKEN"
    assert _vcs_name("gitee:o/r") == "Gitee"
    # 存量回归：两平台原值
    assert _vcs_api_base("github:o/r") == "https://api.github.com"
    assert _vcs_api_base("gitcode:o/r") == "https://gitcode.com/api/v5"
    assert _vcs_api_base("o/r") == "https://gitcode.com/api/v5"
    assert _vcs_token_env("github:o/r") == "USER_GITHUB_TOKEN"
    assert _vcs_bot_token_env("gitcode:o/r") == "BOT_GITCODE_TOKEN"
    assert _vcs_name("github:o/r") == "GitHub"
    assert _vcs_name("gitcode:o/r") == "GitCode"


def test_s36_token_env_consistent_with_askpass(gitee_card):
    """agent 注入的 token 变量名与 repogit askpass 链同源（同卡派生）。"""
    from src.kernel.prompt_builder import _vcs_bot_token_env
    from src.domains.runtime.engine.repogit import _write_askpass
    import tempfile
    assert _vcs_bot_token_env("gitee:o/r") == "BOT_GITEE_TOKEN"
    with tempfile.TemporaryDirectory() as td:
        p = _write_askpass(td, "gitee:o/r")
        assert "BOT_GITEE_TOKEN" in open(p).read()


def test_s37_pipeline_bot_identity(gitee_card):
    """event_pipeline 提交身份走 resolve_bot_username 来源链（源码级 + 行为级）。"""
    import inspect
    from src.api import event_pipeline
    src = inspect.getsource(event_pipeline)
    assert "resolve_bot_username(_prov)" in src
    assert 'from src.domains.mgmt.user.db import get_global_github_bot_username' not in \
        src[src.index("def _resolve_bot_name"):] if "def _resolve_bot_name" in src else True
    # 行为级：来源链对 gitee 返回卡 bot；存量平台走全局
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    assert resolve_bot_username("gitee") == "grape_bot_gitee"
