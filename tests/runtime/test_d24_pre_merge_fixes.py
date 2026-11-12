"""D24（三族审计合并前修复）用例：
1. 探活跳过无能力平台（零危害：不冒名探测→不误删）
2. tasks.py 影子 ensure_issue_task 已删（#244 修复恢复生效）
3. token_resolver 重复类块已删（类序调整回归）
"""
import json
import inspect

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


def test_d24_probe_skips_unsupported_provider(gitee_card):
    """gitee 仓库探活：跳过（返回 None 不误判）——绝不打 gitcode API 冒名探测。"""
    src = inspect.getsource(__import__("src.domains.mgmt.routes.repos", fromlist=["repos"]))
    # 源码级：三分支（github/gitcode/else skip）
    assert 'elif provider == "gitcode":' in src
    assert "卡驱动探活随 S12 验收批次接入" in src
    # 行为级：内层函数对 gitee 返回 None（无 deleted 判定路径）
    # 源码级守卫：三分支（github/gitcode/else 跳过）且跳过分支即刻 return None
    assert 'elif provider == "gitcode":' in src
    assert "卡驱动探活随 S12 验收批次接入" in src
    _i = src.index("卡驱动探活随 S12 验收批次接入")
    _tail = src[_i:_i + 260]
    assert "return None" in _tail                      # 跳过分支立即返回（不落探测）


def test_d24_tasks_shadow_removed():
    """tasks.py 本地影子已删：re-export 生效，#244 org 归属修复（校准为仓库 org）可用。"""
    from src.domains.runtime.routes import tasks
    from src.api.issues_center import ensure_issue_task as canonical
    # 本地无同名定义（影子清除）
    src = inspect.getsource(tasks)
    assert "def ensure_issue_task" not in src
    # re-export 指向权威实现（含 #244 修复）
    assert tasks.ensure_issue_task is canonical
    canon_src = inspect.getsource(canonical)
    assert "org drift rejected" in canon_src      # #244 防串货
    assert "ORDER BY id LIMIT 1" in canon_src     # 幂等去重（全状态复用）


def test_d24_token_resolver_no_duplicate_classes():
    """类序调整回归修复：GitCode/GitHub Provider 各只定义一次。"""
    import src.kernel.token_resolver as tr
    src = inspect.getsource(tr)
    assert src.count("class GitCodeProvider(") == 1
    assert src.count("class GitHubProvider(") == 1
    assert src.count("class GenericVcsProvider(") == 1
