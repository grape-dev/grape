"""unified_user_mapping app_id / update_unified_user_info 测试（feishu-app-scoped-openid-fix.md U01-U09）。"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, update_unified_user_info, get_feishu_open_id
from src.user import db as user_db

_USERS = ["uni_1", "uni_2", "uni_3", "uni_4"]


def _ensure_users():
    for uname in _USERS:
        if not user_db.get_user_account(uname):
            user_db.create_user(uname, "pw_test")


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user IN (%s)" % ",".join("?" * len(_USERS)), tuple(_USERS))
        db.commit()


def _row(gitcode_user):
    with db_lock:
        return db.cursor().execute(
            "SELECT gitcode_user, gitcode_nickname, feishu_open_id, feishu_name, app_id FROM unified_user_mapping WHERE gitcode_user=?",
            (gitcode_user,)).fetchone()


def test_u01_first_bind_records_app_id():
    """U01：首次绑定记录 app_id + open_id"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_1", "ou_a1", "飞书甲", gitcode_nickname="甲", app_id="cli_a")
    r = _row("uni_1")
    assert r[2] == "ou_a1" and r[4] == "cli_a" and r[3] == "飞书甲"
    _clean()


def test_u02_same_app_rebind_updates_open_id():
    """U02：同 app 重复绑定刷新 open_id，不清空"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_1", "ou_a1", "飞书甲", gitcode_nickname="甲", app_id="cli_a")
    update_unified_user_info("uni_1", "ou_a2", "飞书甲2", gitcode_nickname="甲", app_id="cli_a")
    r = _row("uni_1")
    assert r[2] == "ou_a2" and r[4] == "cli_a" and r[3] == "飞书甲2"
    _clean()


def test_u03_app_change_without_open_id_clears():
    """U03：换 app 且无新 open_id（手动配置）→ 刷新 app_id，清空 open_id/feishu_name"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_2", "ou_b1", "飞书乙", gitcode_nickname="乙", app_id="cli_b")
    update_unified_user_info("uni_2", open_id="", app_id="cli_c")   # 手动换 app
    r = _row("uni_2")
    assert r[4] == "cli_c" and r[2] == "" and r[3] == ""
    assert not get_feishu_open_id("uni_2")   # open_id 已清空（'' 或 None）
    _clean()


def test_u04_oauth_empty_open_id_preserves():
    """U04：OAuth（open_id 空、无 app_id）→ 保留已有 open_id/app_id"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_3", "ou_d1", "飞书丁", gitcode_nickname="丁", app_id="cli_d")
    update_unified_user_info("uni_3", open_id="", gitcode_nickname="丁2")   # OAuth 登录
    r = _row("uni_3")
    assert r[2] == "ou_d1" and r[4] == "cli_d" and r[1] == "丁2"
    _clean()


def test_u05_p2p_confirm_writes_app_id():
    """U05：p2p 确认（open_id + app_id）→ 写入 open_id + app_id + feishu_name"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_1", "ou_p2p", "吴扬", gitcode_nickname="吴扬", app_id="cli_p2p")
    r = _row("uni_1")
    assert r[2] == "ou_p2p" and r[4] == "cli_p2p" and r[3] == "吴扬"
    _clean()


def test_u06_sync_writes_open_id_with_app_id():
    """U06：sync（open_id + app_id）即使换 app 也直接写入（不因 app 变化清空）"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_2", "ou_old", "旧", gitcode_nickname="旧", app_id="cli_old")
    # 新 app 的 sync 带新 open_id → 直接写入
    update_unified_user_info("uni_2", "ou_new", "新", gitcode_nickname="新", app_id="cli_new")
    r = _row("uni_2")
    assert r[2] == "ou_new" and r[4] == "cli_new" and r[3] == "新"
    _clean()


def test_u07_feishu_name_refreshed_not_cleared():
    """U07：p2p 确认传 feishu_name → 写入非空，不再被清空"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_4", "ou_e1", "旧名", gitcode_nickname="戊", app_id="cli_e")
    update_unified_user_info("uni_4", "ou_e2", "新名", gitcode_nickname="戊", app_id="cli_e")
    r = _row("uni_4")
    assert r[3] == "新名" and r[2] == "ou_e2"
    _clean()


def test_u08_manual_config_change_reconciles():
    """U08：手动配置换 app → app_id 刷新 + open_id 清空（重确认），且后续 sync 带新 open_id 可重新写入"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_3", "ou_f1", "飞书己", gitcode_nickname="己", app_id="cli_f")
    update_unified_user_info("uni_3", open_id="", app_id="cli_g")   # 手动换 app
    r = _row("uni_3")
    assert r[4] == "cli_g" and r[2] == ""
    # 重确认后写入新 app open_id
    update_unified_user_info("uni_3", "ou_f2", "飞书己2", gitcode_nickname="己", app_id="cli_g")
    assert _row("uni_3")[2] == "ou_f2"
    _clean()


def test_u09_oauth_does_not_change_app_id():
    """U09：OAuth 登录不改 app_id/open_id"""
    _ensure_users(); _clean()
    update_unified_user_info("uni_4", "ou_g1", "名", gitcode_nickname="庚", app_id="cli_h")
    update_unified_user_info("uni_4", open_id="", gitcode_nickname="庚2")
    r = _row("uni_4")
    assert r[4] == "cli_h" and r[2] == "ou_g1"
    _clean()
