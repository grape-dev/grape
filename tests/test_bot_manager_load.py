"""Tests for BotManager.load_from_db — chat_bot 按用户级 chat_agent_session 加载，多用户多 bot"""
import pytest, asyncio, os, sys
from unittest.mock import AsyncMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SRC_DIR)
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


@pytest.fixture
def manager():
    from src.im.feishu_async import BotManager
    m = BotManager(max_concurrent_per_bot=2)
    return m


def _seed_users_and_config():
    """在测试库写入两个用户的 chat 配置 + 一个 vod 配置"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_agent_session WHERE (repo_key='' OR repo_key IS NULL) AND im_app_id!=''")
        c.execute("DELETE FROM chat_agent_session WHERE user_id IN (20001, 20002, 20003)")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'iso/%'")
        c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id LIKE 'iso%' OR bot_app_id='test'")
        # 用户 A chat
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret, im_provider) VALUES ('sess-iso-1',20001,'','iso_chat_a','secret_a','feishu')"
        )
        # 用户 B chat
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret, im_provider) VALUES ('sess-iso-2',20002,'','iso_chat_b','secret_b','feishu')"
        )
        # VOD repo 配置
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, im_app_id, im_app_secret, im_provider) VALUES ('sess-vod-1','iso/repo-1','vod','iso_vod_1','secret_v','feishu')"
        )
        # global 登记表：chat 两个 + vod 一个
        for aid, btype in [("iso_chat_a","chat_bot"),("iso_chat_b","chat_bot"),("iso_vod_1","vod_bot")]:
            c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?,?)", (aid, btype))
        db.commit()


def test_load_from_db_loads_all_users():
    """多用户 chat 配置 + vod 都应被加载（按 app_id 去重）"""
    _seed_users_and_config()
    from src.im.feishu_async import BotManager
    m = BotManager(max_concurrent_per_bot=2)

    async def run():
        with patch.object(m, "add_bot", new=AsyncMock()) as mock_add:
            await m.load_from_db()
            return mock_add.call_args_list

    calls = asyncio.run(run())
    app_ids = {c.args[0] for c in calls if c.args}
    # 应包含 iso_chat_a、iso_chat_b、iso_vod_1
    assert "iso_chat_a" in app_ids, f"missing iso_chat_a, got {app_ids}"
    assert "iso_chat_b" in app_ids, f"missing iso_chat_b, got {app_ids}"
    assert "iso_vod_1" in app_ids, f"missing iso_vod_1, got {app_ids}"
    assert len(app_ids) == 3, f"expected 3 unique app_ids, got {app_ids}"


def test_load_from_db_dedup_same_app_id_two_users():
    """同一 app_id 被两个用户配置时只启动一个 bot"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_agent_session WHERE (repo_key='' OR repo_key IS NULL) AND im_app_id!=''")
        c.execute("DELETE FROM chat_agent_session WHERE user_id IN (20001, 20002)")
        c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id LIKE 'iso%' OR bot_app_id='test'")
        # 两个用户配置同一个 app_id（异常场景，测试去重）
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret, im_provider) VALUES ('sess-sh-1',20001,'','iso_shared','secret_1','feishu')")
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret, im_provider) VALUES ('sess-sh-2',20002,'','iso_shared','secret_2','feishu')")
        c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES ('iso_shared','chat_bot')")
        db.commit()

    from src.im.feishu_async import BotManager
    m = BotManager(max_concurrent_per_bot=2)

    async def run():
        with patch.object(m, "add_bot", new=AsyncMock()) as mock_add:
            await m.load_from_db()
            return mock_add.call_args_list

    calls = asyncio.run(run())
    app_ids = [c.args[0] for c in calls]
    assert app_ids.count("iso_shared") == 1, f"shared app_id should start once, got {app_ids}"


def test_load_from_db_empty():
    """无配置时不应加载任何 bot"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # 清空所有用户级 chat 配置 + iso 前缀的 vod 配置 + iso 登记
        c.execute("DELETE FROM chat_agent_session WHERE (repo_key='' OR repo_key IS NULL) AND im_app_id!=''")
        c.execute("DELETE FROM chat_agent_session WHERE user_id IN (20001, 20002, 20003)")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'iso/%'")
        c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id LIKE 'iso%' OR bot_app_id='test'")
        db.commit()
    from src.im.feishu_async import BotManager
    m = BotManager(max_concurrent_per_bot=2)

    async def run():
        with patch.object(m, "add_bot", new=AsyncMock()) as mock_add:
            await m.load_from_db()
            return mock_add.call_args_list

    calls = asyncio.run(run())
    assert calls == [], f"expected no bots, got {calls}"
