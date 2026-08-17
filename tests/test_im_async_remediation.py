"""整改验收测试 — 目标态接口（整改后运行）。

配套 docs/new-version/feishu-dual-impl-remediation.md 整改方案。

覆盖：
  B 组：FeishuAsync.sync_repo_feishu_mapping（异步群成员 ↔ 协作者映射）

A 组（get_chat_members 分页/token）见 tests/test_feishu_handler.py；
D 组（纯逻辑）见 tests/test_feishu_pure_logic.py；
命令处理（/repo /chat /task 等导航命令卡片化）见 tests/test_feishu_navigation_commands.py。
（旧 /bind /unbind /status /autodev /notify 命令已于 feishu-repo-navigation-commands-design.md 删除，
  其验收用例同步移除。）
"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


def _make_ctx(app_id="cli_remed"):
    from src.im.feishu_async import BotContext
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="remed-test")


def _make_api_with_session():
    from src.im.feishu_async import FeishuAsync
    api = FeishuAsync()
    session = MagicMock()
    api._get_session = AsyncMock(return_value=session)
    return api, session


def _http_response(payload):
    resp = MagicMock()
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


# ===== B. FeishuAsync.sync_repo_feishu_mapping =====


class TestSyncRepoFeishuMapping:
    async def _run(self, members, collabs, bot_token="bot_tok"):
        """执行 sync_repo_feishu_mapping：mock 群成员 + 协作者拉取。"""
        from src.im.feishu_async import FeishuAsync
        api = FeishuAsync()
        ctx = _make_ctx()
        api.get_chat_members = AsyncMock(return_value=members)
        from src.user import db as user_db
        with patch.object(user_db, "get_bot_token_by_repo", return_value=bot_token):
            with patch("src.im.feishu_async._fetch_gitcode_collaborators", return_value=collabs):
                matched = await api.sync_repo_feishu_mapping(ctx, "gitcode:owner/repo", "oc_b")
        return matched, api

    def test_b1_nickname_match_writes_mapping(self):
        """B1: 群成员昵称匹配 GitCode 协作者，写入 unified_user_mapping。"""
        from src.session_manager import db, db_lock
        from src.user import db as user_db
        # 预置 user_accounts 用户（set_feishu_open_id 需要真实 user_id 才写入）
        user_db.get_user_by_username('zhangsan') or user_db.create_user('zhangsan', 'pw')
        with db_lock:
            db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user='zhangsan'")
            db.commit()
        matched, _ = asyncio.run(self._run(
            members=[{"name": "张三", "open_id": "ou_b1"}],
            collabs=[{"nick_name": "张三", "username": "zhangsan"}],
        ))
        assert matched == 1
        with db_lock:
            row = db.cursor().execute(
                "SELECT gitcode_user, gitcode_nickname, feishu_open_id FROM unified_user_mapping WHERE gitcode_user='zhangsan'"
            ).fetchone()
        assert row and row[1] == "张三" and row[2] == "ou_b1"
        with db_lock:
            db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user='zhangsan'")
            db.commit()

    def test_b2_no_members_returns_zero(self):
        """B2: 无群成员时返回 0，不写库。"""
        matched, _ = asyncio.run(self._run(members=[], collabs=[]))
        assert matched == 0

    def test_b3_no_bot_token_returns_zero(self):
        """B3: 无 bot token（含全局兜底失败）时返回 0。"""
        from src.im.feishu_async import FeishuAsync
        api = FeishuAsync()
        ctx = _make_ctx()
        api.get_chat_members = AsyncMock(return_value=[{"name": "张三", "open_id": "ou_b3"}])
        from src.user import db as user_db
        with patch.object(user_db, "get_bot_token_by_repo", return_value=""):
            with patch("builtins.open") as mock_open:
                mock_open.side_effect = FileNotFoundError  # 全局兜底文件不存在
                with patch("src.im.feishu_async._fetch_gitcode_collaborators") as mock_fetch:
                    matched = asyncio.run(api.sync_repo_feishu_mapping(ctx, "gitcode:owner/repo", "oc_b3"))
        assert matched == 0
        mock_fetch.assert_not_called()

    def test_b4_empty_collaborators_no_exception(self):
        """B4: 协作者为空时匹配 0，不抛异常。"""
        matched, _ = asyncio.run(self._run(
            members=[{"name": "张三", "open_id": "ou_b4"}], collabs=[]))
        assert matched == 0

    def test_b5_writes_gitcode_nickname(self):
        """B5: 匹配时写入 gitcode_nickname（覆盖 B1，此处单独断言昵称）。"""
        from src.session_manager import db, db_lock
        from src.user import db as user_db
        user_db.get_user_by_username('lisi') or user_db.create_user('lisi', 'pw')
        with db_lock:
            db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user='lisi'")
            db.commit()
        matched, _ = asyncio.run(self._run(
            members=[{"name": "李四", "open_id": "ou_b5"}],
            collabs=[{"nick_name": "李四", "username": "lisi"}],
        ))
        assert matched == 1
        with db_lock:
            row = db.cursor().execute(
                "SELECT gitcode_nickname FROM unified_user_mapping WHERE gitcode_user='lisi'"
            ).fetchone()
        assert row[0] == "李四"
        with db_lock:
            db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user='lisi'")
            db.commit()
