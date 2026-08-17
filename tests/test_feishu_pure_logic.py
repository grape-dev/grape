"""feishu.py 纯逻辑函数回归测试（当前即可运行）。

配套 docs/new-version/feishu-dual-impl-remediation.md 整改方案：
无论这些函数在整改后「保留在 feishu.py」还是「迁入 handler_async.py」，
行为都必须保持不变——本文件锁定该不变性。

注意：detect_bind_intent / detect_clean_intent / resolve_repo 均为纯逻辑（无网络 IO），
resolve_repo 仅做 SQLite 快查询，不触发同步 HTTP，因此可在整改前安全运行。
"""
import os
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


def _bind_repo(chat_id, repo_key, account):
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id=?", (chat_id,))
        c.execute(
            "INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, im_app_id) VALUES (?,?,?)",
            (chat_id, to_repo_key(repo_key), account))
        db.commit()


def test_d1_detect_bind_intent():
    from src.im.handler_async import detect_bind_intent
    assert detect_bind_intent("绑定到 owner/repo") == "owner/repo"
    assert detect_bind_intent("帮我绑定到 org/devops-toolkit") == "org/devops-toolkit"
    assert detect_bind_intent("使用 a/b 仓库") == "a/b"


def test_d2_detect_bind_intent_none():
    from src.im.handler_async import detect_bind_intent
    assert detect_bind_intent("查一下状态") == ""
    assert detect_bind_intent("你好呀") == ""


def test_d3_detect_clean_intent_true():
    from src.im.handler_async import detect_clean_intent
    assert detect_clean_intent("清除会话缓存") is True
    assert detect_clean_intent("重置Agent上下文") is True
    assert detect_clean_intent("重来一下") is True
    assert detect_clean_intent("清理缓存") is True


def test_d4_detect_clean_intent_false():
    from src.im.handler_async import detect_clean_intent
    assert detect_clean_intent("你好") is False
    assert detect_clean_intent("仓库状态如何") is False


def test_d5_resolve_repo_from_binding():
    from src.im.handler_async import resolve_repo
    from src.session_manager import db
    _bind_repo("oc_d5", "owner/repo", "cli_d5")
    assert resolve_repo("现在如何", "oc_d5", "u1", db, "cli_d5") == "gitcode:owner/repo"


def test_d6_resolve_repo_explicit():
    from src.im.handler_async import resolve_repo
    # 显式 owner/repo 命中正则，DB 无关
    assert resolve_repo("owner/repo 怎么样", "oc_d6", "u1", db=None, account="") == "owner/repo"


def test_d6b_resolve_repo_bind_intent_wins():
    from src.im.handler_async import resolve_repo
    from src.session_manager import db
    _bind_repo("oc_d6b", "already/bound", "cli_d6b")
    # 自然语言绑定意图优先于已绑定仓库
    assert resolve_repo("绑定到 new/repo", "oc_d6b", "u1", db, "cli_d6b") == "new/repo"
