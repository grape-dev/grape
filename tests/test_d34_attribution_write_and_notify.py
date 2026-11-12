"""D34（S54）用例：归属链收口——建档写入路径 + 飞书通知链接入身份优先。

先测后改纪律：RED 断言（新能力）+ 特征锁（旧行为兜底不变）。
背景：D31 只接了两个读点（issues_center/_get_user_id_by_gitcode、
engine_select/_repo_user_id）；session_store 建档 INSERT 的 COALESCE um
join（写入路径）与 _get_feishu_open_id（通知链）仍是纯名字域——错位数据
的生产与消费两端各漏一处。
"""
import pytest

from src.kernel.session_manager import db, db_lock


def _seed_account(uid: int, username: str):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO grape_accounts (id, username, email, status) "
            "VALUES (?, ?, ?, 1)", (uid, username, f"{username}@t.local"))
        db.commit()


def _seed_identity(uid: int, provider: str, ext_id: str, login: str):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO external_identities "
            "(grape_user_id, provider, external_id, external_login, status) "
            "VALUES (?, ?, ?, ?, 1)", (uid, provider, ext_id, login))
        db.commit()


def _seed_um(uid: int, col: str, login: str, open_id: str = ""):
    with db_lock:
        db.cursor().execute(
            f"INSERT OR REPLACE INTO unified_user_mapping (user_id, {col}, feishu_open_id) "
            f"VALUES (?, ?, ?)", (uid, login, open_id))
        db.commit()


def _cleanup(uids):
    with db_lock:
        c = db.cursor()
        for uid in uids:
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
            c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (uid,))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        c.execute("DELETE FROM issue WHERE repo_key LIKE 'd34test/%'")
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup((701, 702, 703))
    with db_lock:
        db.cursor().execute("DELETE FROM issue WHERE repo_key LIKE 'd34test/%'")
        db.commit()
    yield
    _cleanup((701, 702, 703))


def _issue_row(repo_key: str, num: str):
    with db_lock:
        row = db.cursor().execute(
            "SELECT user_id, assignee FROM issue WHERE repo_key=? AND issue_number=?",
            (repo_key, num)).fetchone()
    return row


# ------------------------------------------- 1. 建档写入路径（session_store）

def test_s54_issue_row_insert_identity_first():
    """身份行存在、um 无行 → 建档 user_id 取身份域（原为 0 静默算错）。"""
    from src.api.session_store import _sync_issue_fields
    _seed_account(701, "alice_reg")
    _seed_identity(701, "gitcode", "g-701", "alice_platform")   # um 故意不种
    _sync_issue_fields("d34test/web", "5", {"stage": "open"},
                       ensure_row=True, assignee="alice_platform")
    row = _issue_row("d34test/web", "5")
    assert row is not None
    assert str(row[0]) == "701"   # D105: 读回 str（G2 身份优先修复后归属真实 id）
    assert row[1] == "alice_platform"


def test_s54_issue_row_insert_um_fallback_unchanged():
    """特征锁：身份未命中、um 有行 → um 兜底建档（旧行为不变）。"""
    from src.api.session_store import _sync_issue_fields
    _seed_account(702, "bob_reg")
    _seed_um(702, "gitcode_user", "bob_platform")               # identity 故意不种
    _sync_issue_fields("d34test/web", "6", {"stage": "open"},
                       ensure_row=True, assignee="bob_platform")
    row = _issue_row("d34test/web", "6")
    assert row is not None and str(row[0]) == "702"  # D105: user_id VARCHAR(36) 域读回 str（G2 d34 值回归已修）


def test_s54_issue_row_insert_no_source_zero():
    """特征锁：assignee 无任何映射 → user_id=0（原语义保持）。"""
    from src.api.session_store import _sync_issue_fields
    _sync_issue_fields("d34test/web", "7", {"stage": "open"},
                       ensure_row=True, assignee="nobody_here")
    row = _issue_row("d34test/web", "7")
    assert row is not None and str(row[0]) == "0"  # D105: 读回 str，无映射语义不变


# ------------------------------------------- 2. 飞书通知链（_get_feishu_open_id）

def test_s54_feishu_open_id_identity_first():
    """身份命中 → 经 user_id 查 open_id（um 平台列空也能通知到人）。"""
    from src.api.issues_center import _get_feishu_open_id
    _seed_account(703, "carol_reg")
    _seed_identity(703, "gitcode", "g-703", "carol_platform")
    _seed_um(703, "gitcode_user", "", open_id="ou-carol")       # 平台列空，open_id 在
    got = _get_feishu_open_id("carol_platform", repo_key="gitcode:d34test/web")
    assert got == "ou-carol"      # RED：现状按 um.gitcode_user 查 → 空串


def test_s54_feishu_open_id_um_fallback_unchanged():
    """特征锁：身份未命中、um 平台列有行 → 旧名字域路径不变。"""
    from src.api.issues_center import _get_feishu_open_id
    _seed_um(703, "gitcode_user", "dave_platform", open_id="ou-dave")
    got = _get_feishu_open_id("dave_platform", repo_key="gitcode:d34test/web")
    assert got == "ou-dave"


def test_s54_feishu_open_id_miss_empty():
    """特征锁：两级都未命中 → 空串（原语义保持）。"""
    from src.api.issues_center import _get_feishu_open_id
    assert _get_feishu_open_id("ghost_login", repo_key="gitcode:d34test/web") == ""
