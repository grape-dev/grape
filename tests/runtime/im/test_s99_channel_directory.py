"""S99 用例（D74，先测后改）：um 退役批次 3——IM 通道目录（channel_directory）收敛。

用户拍板方案 B：feishu/gitcode_nickname 通道数据收敛单一模块 API，um 列
降级为模块私有存储。行为约束（护栏 5 固化的六类行为）：
①绑定（bind，app 级成对）；②open_id→user 反查；③agent 回写（FR-18.4
USER_ID>0）；④解析（S54 身份优先 + um 兜底顺序内置）；⑤app 级清空 /
user 级精确清空（FR-21.10）；⑥昵称更新。
"""
import os
import sys
import sqlite3
import threading
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.im import channel_directory as cd


@pytest.fixture()
def um_db(monkeypatch, tmp_path):
    """独立 sqlite um/grape_accounts 库。"""
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute("""CREATE TABLE unified_user_mapping (
        user_id INTEGER PRIMARY KEY, gitcode_user TEXT DEFAULT '', gitcode_nickname TEXT DEFAULT '',
        feishu_open_id TEXT DEFAULT '', feishu_name TEXT DEFAULT '', app_id TEXT DEFAULT '',
        github_login TEXT DEFAULT '', dingtalk_user_id TEXT DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE grape_accounts (
        id INTEGER PRIMARY KEY, username TEXT, nickname TEXT DEFAULT '', status INTEGER DEFAULT 1)""")
    monkeypatch.setattr("src.kernel.session_manager.db", conn)
    monkeypatch.setattr("src.kernel.session_manager.db_lock", threading.RLock())
    yield conn
    conn.close()


def test_s99_bind_and_reverse_lookup(um_db):
    """①绑定（app 级成对语义：app_id 必填关键字）+ ②open_id 反查。"""
    assert cd.bind(7, "ou_x1", feishu_name="张三", app_id="app_a")
    row = cd.reverse_lookup_by_open_id("ou_x1")
    assert row["user_id"] == 7 and row["feishu_name"] == "张三"
    assert row["gitcode_user"] == ""  # FR-18.7：不碰平台列（schema 默认空串）
    # app_id 成对护栏：不传 app_id 必须 TypeError
    with pytest.raises(TypeError):
        cd.bind(8, "ou_x2", feishu_name="李四")
    # 未知 open_id → 空骨架
    assert cd.reverse_lookup_by_open_id("ou_none")["user_id"] is None


def test_s99_write_back_fr18_4(um_db):
    """③agent 回写：USER_ID>0 约束；app_id 成对必填。"""
    cd.write_back(0, "ou_bad", "n", app_id="a")  # 0 → no-op
    row = cd.reverse_lookup_by_open_id("ou_bad")
    assert row["user_id"] is None
    with pytest.raises(TypeError):
        cd.write_back(9, "ou_ok", "n")
    cd.write_back(9, "ou_ok", "n", app_id="app_b")
    row = cd.reverse_lookup_by_open_id("ou_ok")
    assert row["user_id"] == 9 and row["feishu_name"] == "n"


def test_s99_resolve_s54_order_identity_first(um_db):
    """④解析：S54 身份优先命中 → user_id 直查，不走 um 名字域。"""
    um_db.execute("INSERT INTO unified_user_mapping (user_id, feishu_open_id) VALUES (5, 'ou_u5')")
    um_db.commit()
    with patch("src.domains.mgmt.auth.grape_account_db.resolve_user_id_by_platform_login", return_value=5) as m:
        assert cd.resolve_open_id_for_login("someone", "gitcode:o/r") == "ou_u5"
        m.assert_called_once()


def test_s99_resolve_s54_fallback_um_then_skip(um_db):
    """④解析：身份未命中 → um 名字域兜底；无 um 列平台（github）跳过兜底返回空。"""
    um_db.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id) "
                  "VALUES (6, 'gc_login', 'ou_u6')")
    um_db.commit()
    with patch("src.domains.mgmt.auth.grape_account_db.resolve_user_id_by_platform_login", return_value=None):
        assert cd.resolve_open_id_for_login("gc_login", "gitcode:o/r") == "ou_u6"
        # D39/S59：github 无 um 列 → 兜底跳过
        assert cd.resolve_open_id_for_login("gc_login", "github:o/r") == ""


def test_s99_clear_app_and_user(um_db):
    """⑤app 级清空（全部该 app 行）与 user 级精确清空（FR-21.10）。"""
    for uid, oid, app in ((1, "ou_a", "app1"), (2, "ou_b", "app1"), (3, "ou_c", "app2")):
        um_db.execute("INSERT INTO unified_user_mapping (user_id, feishu_open_id, app_id) VALUES (?,?,?)",
                      (uid, oid, app))
    um_db.commit()
    cd.clear_for_app("app1")
    assert cd.resolve_open_id_by_user(1) == "" and cd.resolve_open_id_by_user(2) == ""
    assert cd.resolve_open_id_by_user(3) == "ou_c"  # 其他 app 不受影响
    cd.clear_for_user(3)
    assert cd.resolve_open_id_by_user(3) == ""
    cd.clear_for_app("")  # 空串防御：原语义 no-op 不抛错


def test_s99_set_gitcode_nickname(um_db):
    """⑥昵称更新：upsert（无行 INSERT OR IGNORE 后 UPDATE）。"""
    cd.set_gitcode_nickname(11, "nickA")
    row = um_db.execute("SELECT gitcode_nickname FROM unified_user_mapping WHERE user_id=11").fetchone()
    assert row[0] == "nickA"
    cd.set_gitcode_nickname(11, "nickB")
    row = um_db.execute("SELECT gitcode_nickname FROM unified_user_mapping WHERE user_id=11").fetchone()
    assert row[0] == "nickB"


def test_s99_legacy_wrappers_delegate(um_db):
    """存量薄包装（issues_center._get_feishu_open_id / _by_user_id）委托 directory，行为不变。"""
    um_db.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id) "
                  "VALUES (12, 'gc12', 'ou_12')")
    um_db.commit()
    from src.api.issues_center import _get_feishu_open_id, _get_feishu_open_id_by_user_id
    with patch("src.domains.mgmt.auth.grape_account_db.resolve_user_id_by_platform_login", return_value=None):
        assert _get_feishu_open_id("gc12", "gitcode:o/r") == "ou_12"
    assert _get_feishu_open_id_by_user_id(12) == "ou_12"


def test_s99_dependency_direction_guard():
    """护栏 3：身份域/存储层不得反向依赖 channel_directory（单向依赖）。"""
    from pathlib import Path
    base = Path(__file__).parent.parent.parent / "src"
    for rel in ("domains/mgmt/auth/grape_account_db.py", "session_manager.py", "domains/mgmt/user/db.py"):
        assert "channel_directory" not in (base / rel).read_text(), f"{rel} 禁止反向依赖"
