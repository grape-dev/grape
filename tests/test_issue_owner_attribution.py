"""需求卡归属（user_id）回归测试（docs/weekly-08/issue-card-owner-attribution-fix-design.md）。

覆盖：ensure_issue_task 显式 user_id 优先、复用分支归属校准（0/默认账号→正确值）、
webhook/sender 解析兜底、_sync_issue_fields ensure_row 空 assignee 不再命中任意空登录用户。
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")

REPO = "gitcode:owner_attrib/demo"
_uid_creator = None  # 创建人（有映射）
_uid_default = None  # 默认/承载账号


def _clean():
    """只清业务行，保留测试账号与映射（setup_module 创建）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()


def setup_module():
    from src.api import db_setup
    try:
        db_setup.init_database()
    except Exception:
        pass
    global _uid_creator, _uid_default
    from src.session_manager import db, db_lock
    from src.auth.password import hash_password
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username LIKE 'attr_%'")
        db.commit()
    # 创建人（有映射）
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                  "VALUES ('attr_creator', NULL, ?, 'attr_creator', 1, 0)", (hash_password("x"),))
        _uid_creator = c.lastrowid
        c.execute("INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                  "VALUES ('attr_default', NULL, ?, 'attr_default', 1, 0)", (hash_password("x"),))
        _uid_default = c.lastrowid
        # 创建人 gitcode/github 登录映射（与 _get_user_id_by_gitcode 一致：user_id>0 + grape_accounts 命中）
        c.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, github_login, updated_at) "
                  "VALUES (?, 'attr_creator', 'attr_creator', datetime('now'))", (_uid_creator,))
        db.commit()


def teardown_module():
    _clean()


def _mk_card(issue_num=1, user_id=0, assignee=""):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
                  "VALUES (?, ?, ?, ?, 't', 'active')", (user_id, assignee, REPO, issue_num))
        db.commit()
        return c.lastrowid


def _card_user(issue_num=1):
    from src.session_manager import db, db_lock
    with db_lock:
        r = db.cursor().execute(
            "SELECT user_id, assignee FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
            (REPO, issue_num)).fetchone()
    return (r[0], r[1]) if r else None


def test_QA01_create_passes_creator_user_id():
    """create_task_via_issue 以登录 user.id 建档 → 卡 user_id = 登录 id（不依赖字符串映射）。"""
    from src.api.issues_center import create_task_via_issue, TaskCreateBody, ensure_issue_task
    with patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
         patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 1}), \
         patch("src.api.deps.get_user_token_with_refresh", return_value="tok"), \
         patch("src.api.issues_center.ensure_issue_task", wraps=ensure_issue_task), \
         patch("src.api.issues_center._get_user_id_by_gitcode", return_value=None):  # 字符串映射失效
        import asyncio
        asyncio.run(create_task_via_issue(
            TaskCreateBody(repo_key="owner_attrib/demo", title="QA01", description="d"),
            {"id": _uid_creator, "username": "attr_creator", "auth_type": "jwt"}))
    card = _card_user(1)
    assert card and card[0] == _uid_creator, "显式创建身份优先，映射失效也不应丢归属"


def test_QA02_reuse_calibrates_default_account():
    """已有卡 user_id=默认账号，解析=创建人 → 复用分支校准为创建人。"""
    _clean()
    _mk_card(2, user_id=1, assignee="")
    from src.api.issues_center import ensure_issue_task
    with patch("src.api.issues_center._get_user_id_by_gitcode", return_value=_uid_creator):
        ensure_issue_task(REPO, 2, title="t", assignee="creator")
    assert _card_user(2) == (_uid_creator, "creator")


def test_QA03_reuse_calibrates_zero():
    """已有卡 user_id=0，再次 ensure（解析出创建人）→ 校准为创建人（原逻辑保留）。"""
    _clean()
    _mk_card(3, user_id=0, assignee="")
    from src.api.issues_center import ensure_issue_task
    with patch("src.api.issues_center._get_user_id_by_gitcode", return_value=_uid_creator):
        ensure_issue_task(REPO, 3, title="t", assignee="creator")
    assert _card_user(3)[0] == _uid_creator


def test_QA04_webhook_sender_maps_to_creator():
    """webhook issue_opened：sender 可映射 → 卡 user_id=映射 id。"""
    _clean()
    from src.api.issues_center import ensure_issue_task
    with patch("src.api.issues_center._get_user_id_by_gitcode", return_value=_uid_creator):
        ensure_issue_task(REPO, 4, title="t", sender="attr_creator")
    assert _card_user(4)[0] == _uid_creator


def test_QA05_webhook_sender_unmapped_zero():
    """webhook sender 映射不到 → 卡 user_id=0（不落默认账号）。"""
    _clean()
    from src.api.issues_center import ensure_issue_task
    with patch("src.api.issues_center._get_user_id_by_gitcode", return_value=None):
        ensure_issue_task(REPO, 5, title="t", sender="ghost_user")
    assert _card_user(5)[0] == 0


def test_QA06_ensure_row_empty_assignee_zero():
    """_sync_issue_fields ensure_row：assignee 空 → 补卡 user_id=0，不得命中任意空登录用户。"""
    _clean()
    # 造一个「空 github_login」的干扰行（历史上正是这类行被 LIMIT 1 命中）
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE user_id=? OR user_id=?",
                  (_uid_creator, _uid_default))
        c.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, github_login, updated_at) "
                  "VALUES (?, 'interference', '', datetime('now'))", (_uid_default,))
        c.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, github_login, updated_at) "
                  "VALUES (?, 'attr_creator', 'attr_creator', datetime('now'))", (_uid_creator,))
        db.commit()
    from src.api.session_store import _sync_issue_fields
    _sync_issue_fields(REPO, "#6", {"stage": "vod", "owner_agent": "vod", "assignee": ""}, ensure_row=True)
    assert _card_user(6)[0] == 0, "空 assignee 补卡不得命中空登录用户"


def test_QA07_ensure_row_nonempty_maps():
    """_sync_issue_fields ensure_row：assignee=登录名（有映射+账号守卫）→ 补卡 user_id=映射 id。"""
    _clean()
    from src.api.session_store import _sync_issue_fields
    _sync_issue_fields(REPO, "#7", {"stage": "vod", "owner_agent": "vod", "assignee": "attr_creator"}, ensure_row=True)
    assert _card_user(7)[0] == _uid_creator