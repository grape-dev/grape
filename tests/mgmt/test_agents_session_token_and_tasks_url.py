"""S34/S35（D23）用例：agents 跨平台候选集 + 会话 token 列派生 + tasks 链接。"""
import json
import uuid

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
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


def test_s34_agent_repo_key_candidates(gitee_card):
    """agents 会话聚合的候选集含 gitee 前缀（源码级：函数体走 repo_key_candidates）。"""
    import inspect
    from src.domains.mgmt.routes import agents
    src = inspect.getsource(agents)
    assert 'repo_key_candidates(' in src  # 调用形态不强绑变量名（repo_key/_owner_repo 均可）
    # 旧 if/elif 双前缀对不再存在
    assert 'if repo_key.startswith("gitcode:")' not in src
    assert 'elif repo_key.startswith("github:")' not in src


def test_s35_session_token_column_from_card(gitee_card):
    """会话 token 列按 provider 从卡派生：gitee → repo_gitee_token（列不存在于 DDL，
    但列名派生正确——批次 1 列族落地后即可用）；存量回归。"""
    from src.domains.mgmt.vcs import create_provider
    assert create_provider("github").session_token_field() == "repo_github_token"
    assert create_provider("gitcode").session_token_field() == "repo_gitcode_token"
    assert create_provider("gitee").session_token_field() == "repo_gitee_token"
    # agents 源码级：token 列走 session_token_field
    import inspect
    from src.domains.mgmt.routes import agents
    assert 'create_provider(_provider).session_token_field()' in inspect.getsource(agents)


def test_s34_tasks_issue_url_card_driven(gitee_card):
    """tasks 的 issue 链接走 issue_web_url（卡派生），源码级 + 行为级。"""
    import inspect
    from src.domains.runtime.routes import tasks
    src = inspect.getsource(tasks)
    assert '"github.com" if _repo.startswith' not in src
    assert 'issue_web_url(_repo' in src
    from src.kernel.repo_key import issue_web_url
    assert issue_web_url("gitee:o/r", 5) == "https://gitee.com/o/r/issues/5"
    assert issue_web_url("github:o/r", 5) == "https://github.com/o/r/issues/5"
    assert issue_web_url("gitcode:o/r", 5) == "https://gitcode.com/o/r/issues/5"


def test_d23_guard_grape_auth_readback_is_deliberate():
    """grape_auth 的 github 读回校验保留（Issue #455 刻意锚点，文档 D23 记录）。"""
    import inspect
    from src.domains.mgmt.routes import grape_auth
    src = inspect.getsource(grape_auth)
    assert 'READBACK FAILED' in src  # 锚点仍在（刻意设计，不清扫）
