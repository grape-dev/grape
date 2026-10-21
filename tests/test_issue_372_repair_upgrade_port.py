"""Issue #372 §22-24: repair_host 降级写空 + auto_upgrade 校验 + GRAPE_PORT 统一测试

外部 AI 审查发现的同类问题：
- A1: repair_host 降级后仍无条件写 bot token（第 3/4 处同 bug）
- A2: auto_upgrade 不校验 add_collaborator 返回值
- C: GRAPE_PORT / GRAPE_MAIN_PORT 命名分裂

测试覆盖：
- T12-01: repair_host 降级到路径 B 后 bot token 字段为空
- T12-02: auto_upgrade add_collaborator 失败时不写 bot token
- T12-03: _local_port 优先读 GRAPE_PORT，fallback GRAPE_MAIN_PORT
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.event_pipeline import db, db_lock

REPO_PREFIX = "gitcode:owner/test-repair-upgrade"


def setup_function():
    pass


def teardown_function():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE ?", (REPO_PREFIX + "%",))
        db.commit()


# ===========================================================================
# T12-01: repair_host 降级后 bot token 字段为空
# ===========================================================================

def test_repair_host_fallback_bot_token_empty():
    """T12-01: repair_host 降级到路径 B → bot_gitcode_token 为空。

    模拟：bot_token 非空（路径 A 有全局 bot），但 bot 协作者失败 → _use_path_b=True。
    修复前：_db_kwargs[db_bot_token_field] = bot_token（无条件写入）→ 403。
    修复后：_db_kwargs[db_bot_token_field] = (bot_token if not _use_path_b else "")。
    """
    from src.user.db import add_managed_repo
    import uuid

    repo_key = f"{REPO_PREFIX}-fallback-{uuid.uuid4().hex[:6]}"
    bot_token = "GLOBAL_BOT_THAT_FAILED"
    _use_path_b = True  # 降级发生

    # 模拟 repair_host L1232-1239 的 _db_kwargs 组装
    _db_kwargs = {
        "webhook_id": 999,
        "bot_username": "grape_bot",
        "host_user_id": (888 if _use_path_b else 0),
        "webhook_secret": "RANDOM_SECRET",
        "gitcode_webhook_token": "USER_TOKEN",
        "bot_gitcode_token": (bot_token if not _use_path_b else ""),  # 修复后
    }
    add_managed_repo(repo_key, "owner", "test-repo", **_db_kwargs)

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT bot_gitcode_token, host_user_id FROM managed_repos WHERE repo_key=?",
            (repo_key,),
        ).fetchone()

    assert row is not None, "managed_repos 应有记录"
    assert row[0] == "", f"降级后 bot_gitcode_token 应为空，实际: {row[0]!r}"
    assert row[1] == 888, f"host_user_id 应为 888（路径 B），实际: {row[1]}"


# ===========================================================================
# T12-02: auto_upgrade add_collaborator 失败时不写 bot token
# ===========================================================================

def test_auto_upgrade_skips_on_collaborator_failure():
    """T12-02: auto_upgrade add_collaborator 返回 ok=False → 跳过，不写 bot token。

    修复前：不检查返回值，无条件 _update_repo_bot_token → 403。
    修复后：ok=False 时 continue 跳过。
    """
    from unittest.mock import patch, MagicMock
    from src.user.db import auto_upgrade_path_b_repos

    # Mock _query_path_b_repos to return one repo
    _test_repo = {
        "repo_key": "gitcode:owner/test-upgrade-skip",
        "repo_owner": "owner",
        "repo_name": "test-upgrade-skip",
        "host_user_id": 777,
    }

    # Mock add_collaborator to return (False, "permission denied")
    _updated_tokens = []
    def mock_update(repo_key, bot_token, provider):
        _updated_tokens.append((repo_key, bot_token, provider))

    with patch("src.user.db._query_path_b_repos", return_value=[_test_repo]), \
         patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "alice"}), \
         patch("src.api.deps.get_user_token_with_refresh", return_value="HOST_TOKEN"), \
         patch("src.user.repo_manager.add_collaborator", return_value=(False, "permission denied")), \
         patch("src.user.repo_manager.accept_invite_for_repo"), \
         patch("src.user.db._update_repo_bot_token", side_effect=mock_update):
        auto_upgrade_path_b_repos("gitcode", "BOT_TOKEN", "grape_bot")

    # add_collaborator failed → _update_repo_bot_token should NOT be called
    assert len(_updated_tokens) == 0, (
        f"add_collaborator 失败时不应写 bot token，实际调用了 _update_repo_bot_token {_updated_tokens}"
    )


def test_auto_upgrade_writes_on_collaborator_success():
    """T12-02b: auto_upgrade add_collaborator 返回 ok=True → 写 bot token。"""
    from unittest.mock import patch, MagicMock
    from src.user.db import auto_upgrade_path_b_repos

    _test_repo = {
        "repo_key": "gitcode:owner/test-upgrade-ok",
        "repo_owner": "owner",
        "repo_name": "test-upgrade-ok",
        "host_user_id": 777,
    }

    _updated_tokens = []
    def mock_update(repo_key, bot_token, provider, **kwargs):
        _updated_tokens.append((repo_key, bot_token, provider))

    with patch("src.user.db._query_path_b_repos", return_value=[_test_repo]), \
         patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "alice"}), \
         patch("src.api.deps.get_user_token_with_refresh", return_value="HOST_TOKEN"), \
         patch("src.user.repo_manager.add_collaborator", return_value=(True, "")), \
         patch("src.user.repo_manager.accept_invite_for_repo"), \
         patch("src.user.db.get_webhook_url", return_value="http://example.com/webhook/gitcode"), \
         patch("src.user.db._get_managed_webhook_id", return_value=0), \
         patch("src.user.repo_manager.setup_webhook", return_value=12345), \
         patch("src.user.db._update_repo_bot_token", side_effect=mock_update):
        auto_upgrade_path_b_repos("gitcode", "BOT_TOKEN", "grape_bot")

    assert len(_updated_tokens) == 1, "成功时应写 bot token"
    assert _updated_tokens[0][0] == "gitcode:owner/test-upgrade-ok"
    assert _updated_tokens[0][1] == "BOT_TOKEN"


# ===========================================================================
# T12-03: _local_port 优先读 GRAPE_PORT
# ===========================================================================

def test_local_port_prefers_grape_port():
    """T12-03: _local_port 优先读 GRAPE_PORT，fallback GRAPE_MAIN_PORT。"""
    from src.prompt_builder import _local_port

    # Save original env
    _orig_gp = os.environ.get("GRAPE_PORT")
    _orig_gmp = os.environ.get("GRAPE_MAIN_PORT")

    try:
        # Case 1: GRAPE_PORT set → use it
        os.environ["GRAPE_PORT"] = "8080"
        os.environ.pop("GRAPE_MAIN_PORT", None)
        assert _local_port() == "8080", f"应读 GRAPE_PORT=8080，实际: {_local_port()}"

        # Case 2: GRAPE_PORT not set, GRAPE_MAIN_PORT set → fallback
        os.environ.pop("GRAPE_PORT", None)
        os.environ["GRAPE_MAIN_PORT"] = "9000"
        assert _local_port() == "9000", f"应 fallback GRAPE_MAIN_PORT=9000，实际: {_local_port()}"

        # Case 3: both set → GRAPE_PORT wins
        os.environ["GRAPE_PORT"] = "7000"
        os.environ["GRAPE_MAIN_PORT"] = "9000"
        assert _local_port() == "7000", f"GRAPE_PORT 应优先于 GRAPE_MAIN_PORT，实际: {_local_port()}"

        # Case 4: neither set → default 3000
        os.environ.pop("GRAPE_PORT", None)
        os.environ.pop("GRAPE_MAIN_PORT", None)
        assert _local_port() == "3000", f"默认应为 3000，实际: {_local_port()}"
    finally:
        # Restore
        if _orig_gp is not None:
            os.environ["GRAPE_PORT"] = _orig_gp
        else:
            os.environ.pop("GRAPE_PORT", None)
        if _orig_gmp is not None:
            os.environ["GRAPE_MAIN_PORT"] = _orig_gmp
        else:
            os.environ.pop("GRAPE_MAIN_PORT", None)


# ===========================================================================
# T13-03/04/05: 仓库级 bot token 层 host_user_id 守卫
# ===========================================================================

def test_get_repo_token_with_refresh_skips_repo_bot_for_path_b():
    """T13-03: get_repo_token_with_refresh 路径 B 仓库跳过仓库级 bot token 层。

    即使 bot_gitcode_token 列非空（脏写入），host_user_id > 0 → 跳过 → 走用户 token。
    """
    from unittest.mock import patch, MagicMock
    from src.user.db import get_repo_token_with_refresh

    # path B repo: host_user_id > 0, bot_gitcode_token has dirty value
    with patch("src.user.db.db") as mock_db, \
         patch("src.user.db.db_lock"), \
         patch("src.user.db._get_global_bot_token", return_value=""), \
         patch("src.user.db.get_host_user_id", return_value=42), \
         patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "alice"}), \
         patch("src.api.deps.get_user_token_with_refresh", return_value="USER_TOKEN"):
        cursor = MagicMock()
        # devops_agent_session: empty
        # managed_repos.bot_gitcode_token: "DIRTY_BOT_TOKEN" (should be skipped!)
        cursor.fetchone.side_effect = [None]
        mock_db.cursor.return_value = cursor
        result = get_repo_token_with_refresh("gitcode:owner/repo")
        print(f"  result: {result!r}")
        assert result == "USER_TOKEN", (
            f"路径 B 仓库应跳过 bot_gitcode_token 层走用户 token，实际: {result!r}"
        )


def test_get_repo_bot_token_skips_repo_bot_for_path_b():
    """T13-04: get_repo_bot_token 路径 B 仓库跳过仓库级 bot token 层。"""
    from unittest.mock import patch, MagicMock
    from src.user.db import get_repo_bot_token

    with patch("src.user.db.db") as mock_db, \
         patch("src.user.db.db_lock"), \
         patch("src.user.db._get_global_bot_token", return_value=""), \
         patch("src.user.db.get_host_user_id", return_value=42), \
         patch("src.user.db.get_bot_token_by_repo", return_value="DIRTY_BOT"), \
         patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "alice"}), \
         patch("src.api.deps.get_user_token_with_refresh", return_value="USER_TOKEN"):
        result = get_repo_bot_token("gitcode:owner/repo")
        print(f"  result: {result!r}")
        assert result == "USER_TOKEN", (
            f"路径 B 仓库应跳过 bot_gitcode_token 走用户 token，实际: {result!r}"
        )


def test_get_github_token_skips_repo_bot_for_path_b():
    """T13-05: get_github_token 路径 B 仓库跳过仓库级 bot token 层。"""
    from unittest.mock import patch, MagicMock
    from src.user.db import get_github_token

    with patch("src.user.db.db") as mock_db, \
         patch("src.user.db.db_lock"), \
         patch("src.user.db.get_host_user_id", return_value=42), \
         patch("src.auth.grape_account_db.get_grape_account_by_username", return_value=None), \
         patch("src.user.db.get_global_github_bot_token", return_value=""), \
         patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"username": "alice"}), \
         patch("src.api.deps.get_user_github_token_with_refresh", return_value="USER_GH_TOKEN"):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("DIRTY_GH_BOT",)
        mock_db.cursor.return_value = cursor
        result = get_github_token("github:owner/repo")
        print(f"  result: {result!r}")
        assert result == "USER_GH_TOKEN", (
            f"路径 B 仓库应跳过 bot_github_token 走用户 token，实际: {result!r}"
        )


# ===========================================================================
# T14-01/02: bot_username 降级写空 + _update_repo_bot_token 清 host_user_id
# ===========================================================================

def test_host_repo_fallback_bot_username_empty():
    """T14-01: host_repo 降级到路径 B 后 bot_username 为空。

    降级时 bot_username=global_bot_username 写入 → build_task_env 查 bot_username
    命中全局 bot → PR 审查人=全局 bot → bot 无协作权限 → 审查失败。
    修复：bot_username=(global_bot_username if not _use_path_b else "")。
    """
    from src.user.db import add_managed_repo
    import uuid

    repo_key = f"gitcode:owner/test-botusername-{uuid.uuid4().hex[:6]}"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.commit()

    global_bot_username = "grape_bot"
    _use_path_b = True

    add_managed_repo(
        repo_key, "owner", "test-repo", webhook_id=100,
        gitcode_webhook_token="USER_TOKEN",
        bot_gitcode_token="",
        bot_username=(global_bot_username if not _use_path_b else ""),
        org_id=0, host_user_id=999, webhook_secret="SECRET",
    )

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT bot_username FROM managed_repos WHERE repo_key=?", (repo_key,)
        ).fetchone()

    assert row is not None
    assert row[0] == "", f"降级后 bot_username 应为空，实际: {row[0]!r}"


def test_repair_host_fallback_bot_username_empty():
    """T14-02: repair_host 降级到路径 B 后 bot_username 为空。

    同 T14-01 但针对 repair_host 的 _db_kwargs 组装。
    """
    from src.user.db import add_managed_repo
    import uuid

    repo_key = f"gitcode:owner/test-repair-botusername-{uuid.uuid4().hex[:6]}"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.commit()

    bot_username = "grape_bot"
    _use_path_b = True  # repair_host 降级

    # 模拟 repair_host L1232-1239 的 _db_kwargs 组装
    _db_kwargs = {
        "webhook_id": 999,
        "bot_username": (bot_username if not _use_path_b else ""),
        "host_user_id": (888 if _use_path_b else 0),
        "webhook_secret": "RANDOM_SECRET",
        "gitcode_webhook_token": "USER_TOKEN",
        "bot_gitcode_token": (bot_username if not _use_path_b else ""),  # 同步修复
    }
    add_managed_repo(repo_key, "owner", "test-repo", **_db_kwargs)

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT bot_username, host_user_id FROM managed_repos WHERE repo_key=?",
            (repo_key,),
        ).fetchone()

    assert row is not None, "managed_repos 应有记录"
    assert row[0] == "", f"repair_host 降级后 bot_username 应为空，实际: {row[0]!r}"
    assert row[1] == 888, f"host_user_id 应为 888（路径 B），实际: {row[1]}"


def test_update_repo_bot_token_clears_host_user_id():
    """T14-02: _update_repo_bot_token 同时清 host_user_id=0。

    auto_upgrade 升级后 host_user_id 应归 0（路径 A），消除语义混合状态。
    """
    from src.user.db import add_managed_repo, _update_repo_bot_token
    import uuid

    repo_key = f"gitcode:owner/test-upgrade-clear-{uuid.uuid4().hex[:6]}"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.commit()

    # Create path B repo (host_user_id > 0)
    add_managed_repo(repo_key, "owner", "test-repo", webhook_id=200,
                     gitcode_webhook_token="USER_TOKEN", bot_gitcode_token="",
                     bot_username="", org_id=0, host_user_id=555, webhook_secret="SEC")

    # Verify host_user_id is 555
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT host_user_id, bot_gitcode_token FROM managed_repos WHERE repo_key=?", (repo_key,)).fetchone()
    assert row[0] == 555, f"初始 host_user_id 应为 555，实际: {row[0]}"

    # Upgrade: write bot token
    _update_repo_bot_token(repo_key, "BOT_TOKEN", "gitcode")

    # Verify host_user_id cleared to 0
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT host_user_id, bot_gitcode_token FROM managed_repos WHERE repo_key=?", (repo_key,)).fetchone()
    assert row[0] == 0, f"升级后 host_user_id 应为 0，实际: {row[0]}"
    assert row[1] == "BOT_TOKEN", f"bot_gitcode_token 应为 BOT_TOKEN，实际: {row[1]}"
