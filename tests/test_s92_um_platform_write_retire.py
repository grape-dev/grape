"""S92 用例（D68，先测后改）：um 平台归属列运行时停写（feishu/昵称列保留）。

背景（D68 台账）：um 平台登录名列（gitcode_user/github_login）已由
external_identities 身份域喂养——session_manager 启动反向回填（只填空）
+ 读者全部身份优先（S51/S54）。运行时写平台列=冗余，停写后 um 平台列
退化为"启动时快照"（fallback 只服务存量读者，正是退役路径）。
feishu_open_id/feishu_name/gitcode_nickname 列保留运行时写（身份域无
对应物，IM 映射域建模=批次 3）。
"""
import json

import re

import pytest

from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _env():
    with db_lock:
        db.cursor().execute("DELETE FROM unified_user_mapping WHERE user_id IN (9101,9102)")
        db.cursor().execute("DELETE FROM external_identities WHERE grape_user_id IN (9101,9102)")
        db.cursor().execute("DELETE FROM grape_accounts WHERE id IN (9101,9102)")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM unified_user_mapping WHERE user_id IN (9101,9102)")
        db.cursor().execute("DELETE FROM external_identities WHERE grape_user_id IN (9101,9102)")
        db.cursor().execute("DELETE FROM grape_accounts WHERE id IN (9101,9102)")
        db.commit()


def _um_row(user_id):
    with db_lock:
        return db.cursor().execute(
            "SELECT gitcode_user, github_login, feishu_open_id, gitcode_nickname "
            "FROM unified_user_mapping WHERE user_id=?", (user_id,)).fetchone()


def test_s92_bind_no_runtime_platform_col_write():
    """绑定后运行时不再写 um 平台归属列（身份域是唯一写源；feishu 列保留）。"""
    from src.domains.mgmt.auth.grape_account_db import create_external_identity
    with db_lock:
        db.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (9101)")
        db.commit()
    assert create_external_identity(
        grape_user_id=9101, provider="gitcode", external_id="s92-1",
        external_login="s92login", access_token="at", refresh_token="rt",
        expires_in=3600) is True
    row = _um_row(9101)
    assert row is not None
    assert row[0] in ("", None), f"um.gitcode_user 被运行时写入: {row!r}（应停写）"


def test_s92_unbind_clear_kept():
    """解绑清空保留（存量读者一致性；冻结数据清了也无害）。"""
    from src.domains.mgmt.auth.grape_account_db import _sync_um_platform_col
    with db_lock:
        db.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id, gitcode_user) "
                   "VALUES (9102, 'legacy')")
        db.commit()
    _sync_um_platform_col(9102, "gitcode", "whatever")
    # 停写后：不清（解绑清空走 unbind 路由，非本函数）
    row = _um_row(9102)
    assert row[0] == "legacy"


def test_s92_no_runtime_platform_col_writes_in_src():
    """结构守卫：src 中 SET gitcode_user=/SET github_login= 运行时写清零，
    仅允许 session_manager 启动反向回填（identity→um 桥）保留。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for pat in ("SET gitcode_user=", "SET github_login=?",
                    "gitcode_user=CASE WHEN"):
            if pat in text:
                for i, line in enumerate(text.splitlines(), 1):
                    if pat in line:
                        # 解绑清空语句（写 ''）保留：冻结数据清了无害，
                        # 且为存量读者保持 unbind 一致性语义（D68）
                        if re.search(r"SET (gitcode_user|github_login)=''", line):
                            continue
                        offenders.append(f"{py.name}:{i}")
    # 唯一合法保留：session_manager 启动反向回填（2 处 UPDATE + 1 处 CASE）
    allowed = {"session_manager.py"}
    bad = [o for o in offenders if o.split(":")[0] not in allowed]
    assert not bad, f"um 平台列运行时写残留: {bad}"
    # 回填桥必须还在（读者兜底的数据源）
    assert any("session_manager.py" in o for o in offenders), \
        "session_manager 启动回填桥丢失——读者兜底将断粮"
