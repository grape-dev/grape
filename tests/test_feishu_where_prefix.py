"""飞书导航命令卡片化测试 - /where 前缀归一化回归（feishu repo_key 前缀 bug, 2026-10-18）。

生产现象：群绑定 `gitcode:Jason7602/hello-calculator-11` 与当前会话仓库 `gitcode:Jason7602/hello-calculator-11`
（同一仓库）被 /where 误报「不一致」。

根因：handler_async 判定 `from_repo_key(bound) != conv["repo_key"]`
左侧 from_repo_key 剥了前缀、右侧 conv.repo_key 原封不动（带 gitcode:）→ 恒不等。

修复：两侧统一 from_repo_key => from_repo_key(bound) != from_repo_key(conv.repo_key)。
"""
import os
import sys
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.im.feishu_async import BotContext
from src.session_manager import (
    db, db_lock,
    create_conversation, get_conversation_by_im, join_feishu_chat,
)


@pytest.fixture(autouse=True)
def _clear_repo_bind_cache():
    from src.im.handler_async import _repo_bind_cache
    _repo_bind_cache.clear()
    yield
    _repo_bind_cache.clear()


def _ctx(app_id="cli_nav"):
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="chat-nav")


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("repo_im_group_binding", "chat_conversations", "chat_messages"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE '%navuser/%' OR repo_key LIKE '%repowhere/%'")
            except Exception:
                pass
        c.execute("DELETE FROM chat_agent_session WHERE id LIKE 'nav-%'")
        db.commit()


def _seed(bound_repo_key, conv_repo_key, title="repo 会话"):
    """seed 群绑定 repo_key + 会话 repo_key（两者可带/不带前缀自由组合）。"""
    _clean()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, im_app_id) VALUES ('nav-sess', 1, 'cli_nav')")
        c.execute(
            "INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) "
            "VALUES ('nav_chat', ?, 'gitcode', 'sender_1', 'cli_nav')", (bound_repo_key,))
        db.commit()
    cid = create_conversation(1, conv_repo_key, title=title)
    join_feishu_chat("nav_chat", cid, 1)


def _run_where():
    from src.im.handler_async import process_command_async
    api = AsyncMock()
    asyncio.run(process_command_async(
        _ctx(), api, "/where", "nav_chat", "sender_1", db,
        account="cli_nav", msg_id="msg_1", sender_open_id="ou_nav"))
    return api


def _card_text(api):
    if api.reply_card.called:
        return api.reply_card.call_args[0][2]
    return ""


class TestWherePrefixNormalization:
    """/where 的 repo_key 前缀归一化：同仓库（无论 DB 前缀形式）不误报不一致。"""

    def test_same_repo_both_prefixed_no_warning(self):
        """group bound 与 conversation 均带 gitcode: 前缀且同仓库 → 不报不一致（生产 bug 场景）。"""
        _seed(bound_repo_key="gitcode:navuser/repo-a",
              conv_repo_key="gitcode:navuser/repo-a")
        text = _card_text(_run_where())
        assert "不一致" not in text, f"同仓库不应提示不一致，got: {text}"
        assert "仓库：navuser/repo-a" in text, "展示应去前缀（无 gitcode:）"
        _clean()

    def test_same_repo_bound_prefixed_conv_naked(self):
        """bound 带前缀、conv 无前缀（历史遗留数据形态）→ 仍归一化为同仓库，不误报。"""
        _seed(bound_repo_key="gitcode:navuser/repo-b",
              conv_repo_key="navuser/repo-b")
        text = _card_text(_run_where())
        assert "不一致" not in text, f"同仓库应归一化，got: {text}"
        _clean()

    def test_same_repo_bound_naked_conv_prefixed(self):
        """bound 无前缀、conv 带前缀 → 仍归一化同仓库。"""
        _seed(bound_repo_key="navuser/repo-c",
              conv_repo_key="gitcode:navuser/repo-c")
        text = _card_text(_run_where())
        assert "不一致" not in text, f"同仓库应归一化，got: {text}"
        _clean()

    def test_different_repo_still_warns(self):
        """不同仓库（即使前缀不一致）→ 仍应提示不一致。"""
        _seed(bound_repo_key="gitcode:navuser/repo-a",
              conv_repo_key="gitcode:navuser/repo-b")
        text = _card_text(_run_where())
        assert "不一致" in text, f"不同仓库应提示不一致，got: {text}"
        _clean()

    def test_unbound_no_warning(self):
        """未绑定仓库 → 不因前缀逻辑报错，正常展示未绑定。"""
        _clean()
        cid = create_conversation(1, "gitcode:navuser/repo-a", title="repo 会话")
        join_feishu_chat("nav_chat", cid, 1)
        text = _card_text(_run_where())
        assert "仓库：未绑定" in text
        assert "不一致" not in text
        _clean()