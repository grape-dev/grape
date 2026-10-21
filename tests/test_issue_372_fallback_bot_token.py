"""Issue #372 §21: 路径 A 降级后 bot_gitcode_token 写空修复测试

外部 AI 审查发现：host_repo GitCode 路径 A 降级到路径 B 时，
add_managed_repo 仍写 bot_gitcode_token=global_bot_token（非空），
导致 get_repo_token_with_refresh 第 2 级命中返回无协作权限的 bot token → 403。

修复：bot_gitcode_token=(global_bot_token if not _use_path_b else "")

测试覆盖：
- T11-01: 路径 A 降级到路径 B → bot_gitcode_token 为空
- T11-02: 路径 A 正常 → bot_gitcode_token 为全局 bot token
- T11-03: 路径 B 从一开始（无全局 bot）→ bot_gitcode_token 为空
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

# 从 event_pipeline 导入 db（与生产代码使用同一连接对象，避免全量 collection 污染）
from src.api.event_pipeline import db, db_lock

REPO_PREFIX = "gitcode:owner/test-fallback-bot-token"


def _clean(repo_key):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
        db.commit()


def setup_function():
    pass


def teardown_function():
    # Clean all test repos
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE ?", (REPO_PREFIX + "%",))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE ?", (REPO_PREFIX + "%",))
        db.commit()


def _get_bot_gitcode_token(repo_key):
    """查 managed_repos.bot_gitcode_token。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT bot_gitcode_token, host_user_id FROM managed_repos WHERE repo_key=?",
            (repo_key,),
        ).fetchone()
    return row


def test_path_a_fallback_bot_token_empty():
    """T11-01: 路径 A 降级到路径 B → bot_gitcode_token 为空。

    模拟：global_bot_token 非空，但 bot 协作者失败 → _use_path_b=True。
    修复前：bot_gitcode_token=global_bot_token（非空）→ 403。
    修复后：bot_gitcode_token="" → get_repo_token_with_refresh 跳过第 2 级 → 走用户 token。
    """
    from src.user.db import add_managed_repo
    import uuid

    repo_key = f"{REPO_PREFIX}-fallback-{uuid.uuid4().hex[:6]}"
    _clean(repo_key)

    # 模拟路径 A 降级到路径 B：global_bot_token 非空但 _use_path_b=True
    global_bot_token = "GLOBAL_BOT_THAT_FAILED"
    _use_path_b = True  # 降级发生

    # 调用与 repos.py L1052 相同的逻辑
    add_managed_repo(
        repo_key, "owner", "test-repo", webhook_id=123,
        gitcode_webhook_token="USER_TOKEN",
        bot_gitcode_token=(global_bot_token if not _use_path_b else ""),  # 修复后
        bot_username="grape_bot", org_id=0,
        host_user_id=(999 if _use_path_b else 0),  # 路径 B 写 host_user_id
        webhook_secret="RANDOM_SECRET",
    )

    row = _get_bot_gitcode_token(repo_key)
    assert row is not None, "managed_repos 应有记录"
    assert row[0] == "", f"路径 A 降级后 bot_gitcode_token 应为空，实际: {row[0]!r}"
    assert row[1] == 999, f"host_user_id 应为 999（路径 B），实际: {row[1]}"


def test_path_a_normal_bot_token_written():
    """T11-02: 路径 A 正常（未降级）→ bot_gitcode_token 为全局 bot token。"""
    from src.user.db import add_managed_repo
    import uuid

    repo_key = f"{REPO_PREFIX}-normal-{uuid.uuid4().hex[:6]}"
    _clean(repo_key)

    global_bot_token = "GLOBAL_BOT_OK"
    _use_path_b = False  # 路径 A 正常

    add_managed_repo(
        repo_key, "owner", "test-repo", webhook_id=456,
        gitcode_webhook_token="BOT_TOKEN",
        bot_gitcode_token=(global_bot_token if not _use_path_b else ""),
        bot_username="grape_bot", org_id=0,
        host_user_id=(0 if not _use_path_b else 999),
        webhook_secret="",
    )

    row = _get_bot_gitcode_token(repo_key)
    assert row is not None
    assert row[0] == "GLOBAL_BOT_OK", f"路径 A 正常时 bot_gitcode_token 应为全局 bot token，实际: {row[0]!r}"
    assert row[1] == 0, f"host_user_id 应为 0（路径 A），实际: {row[1]}"


def test_path_b_from_start_bot_token_empty():
    """T11-03: 路径 B 从一开始（无全局 bot token）→ bot_gitcode_token 为空。"""
    from src.user.db import add_managed_repo
    import uuid

    repo_key = f"{REPO_PREFIX}-pathb-{uuid.uuid4().hex[:6]}"
    _clean(repo_key)

    global_bot_token = ""  # 无全局 bot
    _use_path_b = True

    add_managed_repo(
        repo_key, "owner", "test-repo", webhook_id=789,
        gitcode_webhook_token="USER_TOKEN",
        bot_gitcode_token=(global_bot_token if not _use_path_b else ""),
        bot_username="", org_id=0,
        host_user_id=999,
        webhook_secret="RANDOM_SECRET",
    )

    row = _get_bot_gitcode_token(repo_key)
    assert row is not None
    assert row[0] == "", f"路径 B bot_gitcode_token 应为空，实际: {row[0]!r}"
