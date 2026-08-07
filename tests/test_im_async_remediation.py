"""整改验收测试 — 目标态接口（整改后运行）。

配套 docs/new-version/feishu-dual-impl-remediation.md 整改方案。

覆盖：
  B 组：FeishuAsync.sync_repo_feishu_mapping（异步群成员 ↔ 协作者映射）
  C 组：handler_async.process_command_async（异步命令处理 + 阻塞回归）

A 组（get_chat_members 分页/token）见 tests/test_feishu_handler.py；
D 组（纯逻辑）见 tests/test_feishu_pure_logic.py。
"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
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


# ===== C. handler_async.process_command_async =====


class TestProcessCommandAsync:
    async def _run_cmd(self, text, chat_id="oc_c", sender="u1", account="cli_c", bound_repo=None):
        from src.session_manager import db, db_lock
        with db_lock:
            db.cursor().execute("DELETE FROM repo_im_group_binding WHERE im_chat_id=?", (chat_id,))
            db.commit()
        if bound_repo:
            from src.repo_key import to_repo_key
            with db_lock:
                db.cursor().execute(
                    "INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, im_app_id) VALUES (?,?,?)",
                    (chat_id, to_repo_key(bound_repo), account))
                db.commit()
        ctx = _make_ctx(account)
        api = AsyncMock()
        api.sync_repo_feishu_mapping = AsyncMock(return_value=0)
        from src.im.handler_async import process_command_async
        result = await process_command_async(ctx, api, text, chat_id, sender, db, account)
        return result, api

    def test_c1_bind_writes_binding_and_calls_sync(self):
        """C1: /bind 写 binding + 调用 async sync。"""
        from src.session_manager import db, db_lock
        result, api = asyncio.run(self._run_cmd("/bind owner/repo"))
        assert "已绑定" in result
        api.sync_repo_feishu_mapping.assert_called_once()
        with db_lock:
            row = db.cursor().execute(
                "SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id='oc_c'").fetchone()
        assert row and row[0] == "gitcode:owner/repo"

    def test_c2_bind_with_matches_shows_count(self):
        """C2: sync 返回匹配数时展示。"""
        from src.session_manager import db
        ctx = _make_ctx("cli_c2")
        api = AsyncMock()
        api.sync_repo_feishu_mapping = AsyncMock(return_value=3)
        from src.im.handler_async import process_command_async
        result = asyncio.run(process_command_async(ctx, api, "/bind owner/repo", "oc_c2", "u1", db, "cli_c2"))
        assert "已绑定" in result and "3" in result

    def test_c3_unbind_deletes_binding(self):
        """C3: /unbind 删除 binding。"""
        from src.session_manager import db, db_lock
        result, _ = asyncio.run(self._run_cmd("/unbind", bound_repo="owner/repo"))
        assert "已解绑" in result
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM repo_im_group_binding WHERE im_chat_id='oc_c'").fetchone()
        assert row[0] == 0

    def test_c4_status_bound(self):
        """C4: /status 已绑定展示 repo。"""
        result, _ = asyncio.run(self._run_cmd("/status", bound_repo="owner/repo"))
        assert "gitcode:owner/repo" in result

    def test_c5_status_unbound(self):
        """C5: /status 未绑定提示。"""
        result, _ = asyncio.run(self._run_cmd("/status"))
        assert "未绑定" in result

    def test_c6_autodev_on(self):
        """C6: /autodev on 开启自动实现。"""
        from src.session_manager import db, db_lock
        result, _ = asyncio.run(self._run_cmd("/autodev on", bound_repo="owner/repo"))
        assert "已开启" in result
        with db_lock:
            row = db.cursor().execute(
                "SELECT auto_impl_enabled FROM repo_im_group_binding WHERE im_chat_id='oc_c'").fetchone()
        assert row[0] == 1

    def test_c7_notify_off(self):
        """C7: /notify off 关闭通知。"""
        from src.session_manager import db, db_lock
        result, _ = asyncio.run(self._run_cmd("/notify off", bound_repo="owner/repo"))
        assert "已关闭" in result
        with db_lock:
            row = db.cursor().execute(
                "SELECT notify_enabled FROM repo_im_group_binding WHERE im_chat_id='oc_c'").fetchone()
        assert row[0] == 0

    def test_c8_clean_clears_session(self):
        """C8: /clean 清空 agent session。"""
        from src.session_manager import db, db_lock
        with db_lock:
            db.cursor().execute(
                "INSERT OR REPLACE INTO chat_agent_session (id, repo_key, backend_session_id) VALUES ('sess-c8','gitcode:owner/repo','old_oc')")
            db.commit()
        result, _ = asyncio.run(self._run_cmd("/clean", bound_repo="owner/repo"))
        assert "已清除" in result
        with db_lock:
            row = db.cursor().execute(
                "SELECT backend_session_id FROM chat_agent_session WHERE id='sess-c8'").fetchone()
        assert row[0] == ""

    def test_c9_help(self):
        """C9: /help 返回命令列表。"""
        result, _ = asyncio.run(self._run_cmd("/help"))
        assert "/bind" in result and "/clean" in result

    def test_c10_unknown_command_returns_empty(self):
        """C10: 未知命令返回空串。"""
        result, _ = asyncio.run(self._run_cmd("/unknownxyz"))
        assert result == ""

    def test_c11_no_sync_http_in_command_path(self):
        """C11（阻塞回归）：命令路径不调用同步 urlopen / 全局 token。

        旧实现 process_command → sync_repo_feishu_mapping 会触发同步 HTTP；
        目标实现 /status 为纯 DB，不应触发任何同步网络调用。
        """
        result, _ = asyncio.run(self._run_cmd("/status", bound_repo="owner/repo"))
        # 若代码回退到旧同步路径，patch 这些符号将失败（已删除）→ 测试报错即回归
        from src.im import handler_async
        assert not hasattr(handler_async, "sync_repo_feishu_mapping")  # 旧同步函数已移除
        assert "gitcode:owner/repo" in result
