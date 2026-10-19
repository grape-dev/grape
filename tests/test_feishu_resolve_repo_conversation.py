"""方案：飞书任务会话已进入时不再被「未绑定仓库」拦截（docs/weekly-07/feishu-task-session-unbound-chat-fix-design.md）。

背景：IM 渠道配好飞书但群未绑定仓库时，用户点击任务通知“已进入会话”，会话本身带
repo_key；但 process_chat_async 入口 resolve_repo 只查「绑定意图/正文仓库串/群绑定表」，
不看当前会话 → 返回空 → 弹“未绑定仓库”拦截，用户明明已处会话却被拒。

修复：resolve_repo 追加第④层——群绑定为空时查 get_conversation_by_im(chat_id, user_id)，
会话有 repo_key 即返回会话 repo（get_conversation_by_im 已按 user_id 过滤，天然归属校验）。

UT-01 群未绑定 + 已入任务会话 → 返回会话 repo（不拦截）
UT-02 群未绑定 + 会话属于他人 → 仍空（归属安全）
UT-03 群未绑定 + 会话无 repo → 仍空
UT-04 群已绑定（回归） → 群绑定 repo 生效
UT-05 群未绑定 + 无会话 → 仍空
"""
import os

import pytest

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(SRC_DIR, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, create_conversation, join_feishu_chat


@pytest.fixture(autouse=True)
def _clear_repo_bind_cache():
    """每个测试前清 _repo_bind_cache，防跨测试缓存污染（群未绑定↔已绑定切换）。"""
    from src.im.handler_async import _repo_bind_cache
    _repo_bind_cache.clear()
    yield
    _repo_bind_cache.clear()


def _seed_user(binding_repo=None, chat_id="oc_test_conv", uid=1, account="cli_test", conv_repo=None,
               conv_uid=1, no_conv=False):
    """seed 群绑定（可选）+ 会话（可选）。返回清场函数。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id=?", (chat_id,))
        c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (chat_id,))
        if binding_repo:
            c.execute(
                "INSERT INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) "
                "VALUES (?,?,?,?,?)", (chat_id, binding_repo, "gitcode", "sender", account))
        if not no_conv and conv_repo:
            cid = create_conversation(conv_uid, conv_repo, title="任务会话")
            join_feishu_chat(chat_id, cid, conv_uid)
        db.commit()

    def _cleanup():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id=?", (chat_id,))
            c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (chat_id,))
            db.commit()

    return _cleanup


def _resolve(text, chat_id="oc_test_conv", user_id=1, account="cli_test"):
    from src.im.handler_async import resolve_repo
    return resolve_repo(text, chat_id, "sender_1", db, account=account, user_id=user_id)


class TestResolveRepoConversationFallback:
    def test_unbound_group_but_in_task_conversation_returns_conv_repo(self):
        """UT-01：群未绑定，已进入有 repo 的任务会话 → 返回会话 repo（不再弹未绑定）。"""
        cleanup = _seed_user(binding_repo=None, conv_repo="gitcode:test/conv-repo", uid=1, conv_uid=1)
        try:
            got = _resolve("请解答这个需求")
            assert got == "gitcode:test/conv-repo", f"应返回会话 repo，got {got}"
        finally:
            cleanup()

    def test_unbound_no_conv_still_empty(self):
        """UT-05：群未绑定且无任何会话 → 仍返回空（触发未绑定卡片）。"""
        cleanup = _seed_user(binding_repo=None, no_conv=True)
        try:
            assert _resolve("请问怎么做") == ""
        finally:
            cleanup()

    def test_unbound_conv_owned_by_other_returns_empty(self):
        """UT-02：群未绑定，会话属于他人（user_id 不匹配）→ 返回空（归属安全，get_conversation_by_im 按 uid 过滤）。"""
        cleanup = _seed_user(binding_repo=None, conv_repo="gitcode:test/other-repo", uid=1, conv_uid=999)
        try:
            # 当前用户 uid=1，会话 owner=999 → get_conversation_by_im(chat_id, 1) 查不到
            assert _resolve("请解答") == ""
        finally:
            cleanup()

    def test_unbound_conv_without_repo_returns_empty(self):
        """UT-03：群未绑定，会话存在但 repo_key 为空 → 返回空（无仓库上下文不放行）。"""
        from src.session_manager import create_conversation, join_feishu_chat, db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id='oc_test_conv'")
            c.execute("DELETE FROM chat_conversations WHERE im_channel_id='oc_test_conv'")
            db.commit()
        cid = create_conversation(1, repo_key="", title="无仓库会话")
        join_feishu_chat("oc_test_conv", cid, 1)
        try:
            assert _resolve("请问怎么做") == ""
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id='oc_test_conv'")
                c.execute("DELETE FROM chat_conversations WHERE im_channel_id='oc_test_conv'")
                db.commit()

    def test_bound_group_regression(self):
        """UT-04：群已绑定 → 群绑定 repo 生效（回归，不因会话 fallback 干扰）。"""
        cleanup = _seed_user(binding_repo="gitcode:test/bound-repo",
                             conv_repo="gitcode:test/conv-repo2", uid=1, conv_uid=1)
        try:
            got = _resolve("请问")
            assert got == "gitcode:test/bound-repo", f"群绑定优先，got {got}"
        finally:
            cleanup()

    def test_text_repo_match_still_wins(self):
        """正文含 owner/repo 仍命中（原路径 ② 不回归）。"""
        cleanup = _seed_user(binding_repo=None, no_conv=True)
        try:
            got = _resolve("请看看 test/text-repo 这个仓库")
            assert got == "test/text-repo", f"正文仓库串优先，got {got}"
        finally:
            cleanup()