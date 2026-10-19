"""飞书 task 会话执行仓库对齐测试（feishu-task-session-repo-alignment-design.md 方案 1-A）。

背景：`/task-open` 仅把群粘到 task 会话（chat_conversations.im_channel_id），不改群绑定；
普通消息 resolve_repo 仍取群绑定仓库（如 devops-bot），导致 chat 在错误仓库空 cwd 执行 opencode
→ 静默无输出、消息无答复。方案：task 会话已激活且有 repo_key 时，执行仓库以会话仓库为准。

覆盖：
- UT-01 task 会话激活 → 执行仓库=会话仓库（加锁/wsd 用会话仓库）
- UT-02 general 会话 → 沿用群绑定
- UT-03 task 会话无 repo_key → 沿用群绑定（fail-safe）
- UT-04 无会话 → 沿用群绑定
- UT-05 _resolve_task_conversation 按 conv_type/repo_key 判定
- UT-09 未绑定仓库消息 → /repo 卡片带「未绑定仓库」提示（red 头）
"""
import pytest
import os
import sys
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import sqlite3

from src.session_manager import db, db_lock
from src.api import event_store

GROUP_CHAT = "oc_test_repo_align"
GROUP_BOUND = "gitcode:Jason7602/devops-bot"
TASK_CONV_ID = "conv_task_align"
TASK_REPO = "gitcode:Jason7602/hello-calculator-11"
USER = 2


@pytest.fixture(autouse=True)
def _cleanup():
    with db_lock:
        c = db.cursor()
        for t in ("chat_conversations", "repo_im_group_binding"):
            try:
                chk = c.execute("PRAGMA table_info(%s)" % t).fetchall()
                has_im = any(r[1] == "im_channel_id" for r in chk)
                has_bind = any(r[1] == "im_chat_id" for r in chk)
                if t == "chat_conversations" and has_im:
                    c.execute(f"DELETE FROM {t} WHERE im_channel_id=? OR id LIKE 'conv_task%'", (GROUP_CHAT,))
                elif t == "repo_im_group_binding" and has_bind:
                    c.execute(f"DELETE FROM {t} WHERE im_chat_id=?", (GROUP_CHAT,))
            except Exception:
                pass
        db.commit()
    try:
        from src.im.handler_async import _repo_bind_cache
        _repo_bind_cache.clear()
    except Exception:
        pass
    yield
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM chat_conversations WHERE id LIKE 'conv_task%' OR im_channel_id=?", (GROUP_CHAT,))
            c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id=?", (GROUP_CHAT,))
        except Exception:
            pass
        db.commit()


def _clean_all():
    """清空该群全部会话与绑定，保证用例起点干净。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (GROUP_CHAT,))
            c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id=?", (GROUP_CHAT,))
        except Exception:
            pass
        db.commit()


def _seed_group_binding(repo_key=None):
    """群绑定（repo_im_group_binding）：默认绑 devops-bot，或显式传仓库。"""
    rk = repo_key or GROUP_BOUND
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id=?", (GROUP_CHAT,))
        c.execute(
            "INSERT INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id, im_provider) "
            "VALUES (?, ?, 'gitcode', '2', 'cli_test', 'feishu')",
            (GROUP_CHAT, rk))
        db.commit()


def _seed_conversation(conv_type="task", repo_key=TASK_REPO):
    """种子会话：task/general。"""
    cid = TASK_CONV_ID if conv_type == "task" else "conv_general_1"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_conversations WHERE id IN (?, 'conv_general_1')", (cid,))
        c.execute(
            "INSERT INTO chat_conversations (id, user_id, repo_key, im_channel_id, conv_type, issue_number, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (cid, USER, repo_key or "", GROUP_CHAT, conv_type, 5 if repo_key else 0))
        db.commit()
        return cid


def _resolve_exec_repo():
    """调用实现函数：返回最终执行仓库 repo_key（带前缀）。"""
    from src.im.handler_async import _resolve_feishu_exec_repo
    return _resolve_feishu_exec_repo(GROUP_BOUND, GROUP_CHAT, USER)


class TestTaskSessionRepoAlignment:
    def test_ut01_task_session_aligns_repo(self):
        """UT-01：task 会话已 join + 有 repo_key → 执行仓库=会话仓库（不是群绑定）。"""
        _seed_group_binding()          # 群绑定 = devops-bot
        _seed_conversation("task", TASK_REPO)  # 会话 = hello-calculator-11
        got = _resolve_exec_repo()
        assert got == TASK_REPO, f"task 会话应覆盖群绑定, got {got}"

    def test_ut02_general_keeps_group_bound(self):
        """UT-02：general 会话 → 沿用群绑定。"""
        _clean_all()
        _seed_group_binding()
        _seed_conversation("general", GROUP_BOUND)
        got = _resolve_exec_repo()
        assert got == GROUP_BOUND, f"general 会话不应覆盖, got {got}"

    def test_ut03_task_without_repo_keeps_group_bound(self):
        """UT-03：task 会话但 repo_key 空 → 沿用群绑定（fail-safe）。"""
        _seed_group_binding()
        _seed_conversation("task", "")
        got = _resolve_exec_repo()
        assert got == GROUP_BOUND, f"空 repo_key 不应覆盖, got {got}"

    def test_ut04_no_conversation_keeps_group_bound(self):
        """UT-04：无会话 → 沿用群绑定。"""
        _clean_all()
        _seed_group_binding()
        got = _resolve_exec_repo()
        assert got == GROUP_BOUND

    def test_ut05_task_conv_detector(self):
        """UT-05：task 会话检测（_resolve_task_conversation）。"""
        from src.im.handler_async import _resolve_task_conversation
        _seed_conversation("task", TASK_REPO)
        conv = _resolve_task_conversation(GROUP_CHAT, USER)
        assert conv is not None
        assert conv["conv_type"] == "task"
        assert conv["repo_key"] == TASK_REPO

    def test_ut05b_general_not_detected(self):
        """UT-05b：general 会话不应被当作 task 对齐源。"""
        from src.im.handler_async import _resolve_task_conversation
        _clean_all()
        _seed_conversation("general", GROUP_BOUND)
        assert _resolve_task_conversation(GROUP_CHAT, USER) is None

    def test_ut05c_no_conv_not_detected(self):
        """UT-05c：无会话返回 None。"""
        from src.im.handler_async import _resolve_task_conversation
        _clean_all()
        assert _resolve_task_conversation(GROUP_CHAT, USER) is None


class TestRepoCardUnboundHint:
    """未绑定仓库时 `/repo` 卡片应带明确提示（feishu-task-session-repo-alignment-design.md 补充）。"""

    @staticmethod
    def _call(unbound_hint):
        from src.im.handler_async import _send_repo_card, _repo_bind_cache
        from src.im.feishu_async import BotContext
        _repo_bind_cache.clear()
        api = AsyncMock()
        ctx = BotContext(app_id="cli_test", app_secret="s", bot_type="chat_bot", name="test")
        got = []

        async def _fake_send_card(ctx_, api_, msg_id_, card):
            got.append(card)
            return True
        with patch("src.user.db.get_managed_repos", return_value=[
            {"repo_key": "gitcode:owner/a", "is_admin": 1, "is_join": 0},
            {"repo_key": "gitcode:owner/b", "is_admin": 0, "is_join": 1},
        ]), patch("src.im.handler_async._current_bound_repo", return_value=""), \
             patch("src.im.handler_async._send_card_reply", side_effect=_fake_send_card):
            asyncio.run(_send_repo_card(ctx, api, "oc_t", "msg1", 2, "cli_test", 1,
                                        unbound_hint=unbound_hint))
        return got[0]

    def test_ut09_unbound_hint_shown(self):
        """"你好"未绑定仓库触发卡片时，含「未绑定仓库」提示 + red 头。"""
        card = self._call(unbound_hint=True)
        assert card["header"]["template"] == "red"
        assert "未绑定仓库" in card["header"]["title"]["content"]
        body = json.dumps(card["elements"], ensure_ascii=False)
        assert "未绑定仓库" in body
        assert "已托管仓库列表" in body
        # 提示行不得占用编号：第一个编号 1. 应为第一个仓库，而非提示
        assert "1. ⚠️" not in body, "提示行不应被编号"
        assert "1. owner/a（owner）" in body, "仓库编号应从 1 开始"
        assert "2. owner/b（member）" in body, "第二个仓库编号应为 2"

    def test_ut09b_explicit_repo_no_hint(self):
        """/repo 显式命令仍为普通列表（blue，无未绑定提示）。"""
        card = self._call(unbound_hint=False)
        assert card["header"]["template"] == "blue"
        assert "未绑定仓库" not in card["header"]["title"]["content"]

    def test_ut09c_msg_calls_repo_card_with_hint(self):
        """未绑定仓库消息路径调 _send_repo_card 且传 unbound_hint=True。"""
        from src.im.handler_async import process_chat_async, _repo_bind_cache
        from src.im.feishu_async import BotContext
        _repo_bind_cache.clear()
        api = AsyncMock()
        got = []

        async def _fake_sc(ctx_, api_, msg_id_, card):
            got.append(card)
            return True
        # 关键：mock get_user_id_by_app_id→USER，使未绑定分支进入 _send_repo_card
        with patch("src.user.db.get_managed_repos", return_value=[
            {"repo_key": "gitcode:owner/a", "is_admin": 1, "is_join": 0}]):
            with patch("src.im.handler_async._send_card_reply", side_effect=_fake_sc):
                with patch("src.im.handler_async.get_user_id_by_app_id", return_value=USER):
                    with patch("src.im.handler_async._current_bound_repo", return_value=""):
                        ctx = BotContext(app_id="cli_test2", app_secret="s", bot_type="chat_bot", name="t")
                        parsed = {"type": "message", "text": "你好", "chat_id": "oc_x", "message_id": "msg_2",
                                  "sender_id": "u1", "sender_open_id": "ou_1", "chat_type": "group"}
                        asyncio.run(process_chat_async(ctx, api, parsed))
        assert got, "应发出 /repo 卡片"
        card = got[0]
        assert card["header"]["template"] == "red"
        assert "未绑定仓库" in card["header"]["title"]["content"]