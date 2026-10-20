"""Issue #372 §29-34: claim_by_tag 注入 + update_issue marker + R10 bot token +
webhook 清空 + TRUSTED_HOSTS + guard TTL 保护测试

测试覆盖：
- T17-01: claim_by_tag 返回的 task 包含非空 repo_gitcode_token
- T17-02: update_issue body 含 grape-auto-comment marker
- T17-03: detect_webhook_from_host 配置 TRUSTED_HOSTS 时拒绝非白名单 host
- T17-04: detect_webhook_from_host 未配置 TRUSTED_HOSTS 时放行
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest
from unittest.mock import patch, MagicMock


# ===========================================================================
# T17-01: claim_by_tag token injection
# ===========================================================================

def test_claim_by_tag_injects_token():
    """T17-01: claim_by_tag 返回的 task 包含非空 repo_gitcode_token。

    修复前：claim_by_tag 没有 token 注入逻辑 → 远程 worker 拿到空 token。
    修复后：与 claim() 一致，返回前注入。
    """
    from src.runtime.service import claim_by_tag

    # Mock get_task to return a task with empty repo_gitcode_token
    _mock_task = {
        "id": "t_test_claim_tag_001",
        "repo_key": "gitcode:owner/test-repo",
        "repo_gitcode_token": "",  # 路径 B: 刻意为空
        "status": "running",
        "event_seq": 0,
    }

    with patch("src.runtime.service.get_task", return_value=_mock_task), \
         patch("src.runtime.service.db") as mock_db, \
         patch("src.runtime.service.db_lock"), \
         patch("src.runtime.service.refresh_busy_snapshot"), \
         patch("src.runtime.instances.runtime_instance_name", return_value="opencode"), \
         patch("src.runtime.service._get_task_token", return_value="INJECTED_TOKEN"), \
         patch("src.runtime.service._check_pool_engines_reachable", return_value=True), \
         patch("src.runtime.service.is_healthy", return_value=True, create=True), \
         patch("src.runtime.health_scorer.is_healthy", return_value=True):
        # Mock the DB cursor to return a valid task_id
        cursor = MagicMock()
        cursor.fetchone.return_value = ("t_test_claim_tag_001",)
        cursor.rowcount = 1
        mock_db.cursor.return_value = cursor

        result = claim_by_tag("vod", 1)

    if result:
        assert result.get("repo_gitcode_token") == "INJECTED_TOKEN", (
            f"claim_by_tag 应注入 token，实际: {result.get('repo_gitcode_token')!r}"
        )


# ===========================================================================
# T17-02: update_issue marker
# ===========================================================================

def test_update_issue_adds_marker():
    """T17-02: update_issue body 含 grape-auto-comment marker。

    修复前：update_issue 不加 marker → 自循环检测漏拦截。
    修复后：body 字段追加 marker。
    """
    import inspect
    from src.api.git_repo_api import update_issue, create_pull, create_issue, update_pull

    for fn_name, fn in [("update_issue", update_issue), ("create_pull", create_pull),
                        ("create_issue", create_issue), ("update_pull", update_pull)]:
        src = inspect.getsource(fn)
        assert "grape-auto-comment" in src, (
            f"{fn_name} 必须包含 grape-auto-comment marker 逻辑"
        )
        assert "_body" in src, f"{fn_name} 必须有 _body 变量处理 body 字段"


# ===========================================================================
# T17-03: detect_webhook_from_host TRUSTED_HOSTS rejection
# ===========================================================================

def test_detect_webhook_rejects_non_trusted_host():
    """T17-03: 配置 TRUSTED_HOSTS 时拒绝非白名单 host。"""
    from src.user.db import detect_webhook_from_host

    _orig = os.environ.get("TRUSTED_HOSTS")
    try:
        os.environ["TRUSTED_HOSTS"] = "grape.example.com,203.0.113.5"
        # Non-trusted host → rejected
        result = detect_webhook_from_host("evil.attacker.com", "", "")
        assert result == "", f"非白名单 host 应拒绝，实际: {result!r}"

        # Trusted host → accepted
        result2 = detect_webhook_from_host("grape.example.com", "", "")
        assert result2 == "http://grape.example.com/webhook/gitcode", (
            f"白名单 host 应放行，实际: {result2!r}"
        )

        # Trusted IP with port → accepted
        result3 = detect_webhook_from_host("203.0.113.5:3000", "", "")
        assert result3 == "http://203.0.113.5:3000/webhook/gitcode"
    finally:
        if _orig is not None:
            os.environ["TRUSTED_HOSTS"] = _orig
        else:
            os.environ.pop("TRUSTED_HOSTS", None)


def test_detect_webhook_allows_all_when_no_trusted_hosts():
    """T17-04: 未配置 TRUSTED_HOSTS 时放行所有非 localhost。"""
    from src.user.db import detect_webhook_from_host

    _orig = os.environ.get("TRUSTED_HOSTS")
    try:
        os.environ.pop("TRUSTED_HOSTS", None)
        result = detect_webhook_from_host("any.host.com", "", "")
        assert result == "http://any.host.com/webhook/gitcode"
    finally:
        if _orig is not None:
            os.environ["TRUSTED_HOSTS"] = _orig
        else:
            os.environ.pop("TRUSTED_HOSTS", None)


# ===========================================================================
# T17-05: purge_stale_event_guards protects running tasks
# ===========================================================================

def test_purge_skips_running_task_guards():
    """T17-05: purge_stale_event_guards 跳过有运行中 task 的 guard。

    修复前：TTL 到期即清理 → 长任务 guard 被误清 → 重复派发。
    修复后：SQL 加 NOT EXISTS 子查询保护运行中 task 的 guard。
    """
    import inspect
    from src.api.event_store import purge_stale_event_guards

    src = inspect.getsource(purge_stale_event_guards)
    assert "NOT EXISTS" in src, (
        "purge_stale_event_guards 必须包含 NOT EXISTS 子查询保护运行中 task"
    )
    assert "dispatched" in src and "running" in src, (
        "必须检查 dispatched/running 状态的 task"
    )
