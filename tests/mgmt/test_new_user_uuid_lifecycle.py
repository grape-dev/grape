"""D105-7 机制固化：uuid7 新用户注册 → binding_ticket 全链 → 凭据审计 e2e。

模拟 D105 第二批后的「新用户最小闭环」（UC61）：全部走真实 DB 写入路径
（grape_account_db / credential_usage_log 直调，不经 mock 网络——本闭环
不含 OAuth HTTP 握手与路由层，属数据库层 e2e；路由层由 test_transfer_bind
族覆盖，OAuth 握手需真实第三方端点，不可行处不构造，见文件尾注释）。
验收口径：零异常、零 410（票据校验一次通过、不误判 mismatch）。

建库手法同 test_d105_uuid_pk_migration：临时库跑 init_database() 全链。
"""
import os
import sqlite3

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _restore_global_db_state():
    """同 test_d105_uuid_pk_migration：恢复 env + 重载受影响模块，防全局污染。"""
    import importlib
    saved = {k: os.environ.get(k) for k in ("DEVOPS_DB_PATH",)}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    for mod_name in ("src.domains.mgmt.user.db", "src.domains.mgmt.auth.grape_account_db", "src.domains.mgmt.admin.db",
                     "src.domains.mgmt.user.org_db", "src.kernel.persistence.repositories",
                     "src.kernel.persistence.credential_usage_log", "src.api.deps"):
        try:
            importlib.reload(importlib.import_module(mod_name))
        except Exception:
            pass


@pytest.fixture()
def fresh_db(tmp_path):
    import importlib
    dbp = str(tmp_path / "lifecycle.db")
    os.environ["DEVOPS_DB_PATH"] = dbp
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    # `from src.kernel.session_manager import db` 持旧绑定的消费模块全家族重挂，
    # 否则读写落在上一用例的临时库（同 test_d105_uuid_pk_migration 家族口径）
    for mod_name in ("src.domains.mgmt.user.db", "src.domains.mgmt.auth.grape_account_db", "src.domains.mgmt.admin.db",
                     "src.domains.mgmt.user.org_db", "src.kernel.persistence.repositories",
                     "src.kernel.persistence.credential_usage_log", "src.api.deps"):
        try:
            importlib.reload(importlib.import_module(mod_name))
        except Exception:
            pass
    db_setup.init_database()
    return dbp


def _is_uuid36(v) -> bool:
    return (isinstance(v, str) and len(v) == 36 and v.count("-") == 4
            and "-" not in v.replace("-", ""))


def test_new_user_uuid7_full_lifecycle(fresh_db):
    """uuid7 新用户：注册 → save/validate/consume binding_ticket → 身份绑定
    → 凭据审计落库。全链零异常、票据校验一次通过（410 根因回归锁）。"""
    from src.domains.mgmt.auth import grape_account_db as gdb

    # 1) 注册：uuid7 主键（36 字符 v7 形态），读回一致
    acct = gdb.create_grape_account("uc61_user", "uc61@example.com", "pw-uc61")
    assert acct, "create_grape_account 返回 None（注册路径异常）"
    uid = acct["id"]
    assert _is_uuid36(uid), f"新账号 id 应为 uuid36: {uid!r}"
    assert gdb.get_grape_account_by_id(uid)["username"] == "uc61_user"

    # 2) binding_ticket 全链：save → validate(不消费) → validate(消费) → consume
    ticket = gdb.save_binding_ticket(
        uid, "gitee", "ext-uc61", external_login="uc61@gitee",
        external_name="uc61", original_user_id=uid)
    assert ticket, "save_binding_ticket 失败"
    # 先校验不消费（Issue #376 口径：业务事务前只校验）——本人票据不得 410
    info = gdb.validate_binding_ticket(ticket, uid, consume=False)
    assert info is not None, "validate_binding_ticket(consume=False) 判 mismatch=410 根因复发"
    assert info["provider"] == "gitee" and info["external_id"] == "ext-uc61"
    # 二次校验（幂等不消费）仍通过
    assert gdb.validate_binding_ticket(ticket, uid, consume=False) is not None
    # 业务事务成功后消费：一次性语义
    info2 = gdb.validate_binding_ticket(ticket, uid, consume=True)
    assert info2 is not None, "validate_binding_ticket(consume=True) 误判 mismatch"
    assert not gdb.consume_binding_ticket(ticket), "票据应已随 validate 消费"
    assert gdb.validate_binding_ticket(ticket, uid) is None, "已消费票据重放必须拒（410 语义保留）"

    # 3) 他人票据隔离：别的用户拿此票必须拒（只读校验，防误消费）
    other = gdb.create_grape_account("uc61_other", "uc61o@example.com", "pw-x")
    t2 = gdb.save_binding_ticket(other["id"], "github", "ext-o")
    assert gdb.validate_binding_ticket(t2, uid, consume=False) is None, "跨用户票据必须拒"
    assert gdb.validate_binding_ticket(t2, other["id"], consume=False) is not None

    # 4) 身份绑定落库：external_identities（TEXT PK uuid4 + grape_user_id TEXT）
    assert gdb.create_external_identity(uid, "gitee", "ext-uc61",
                                        external_login="uc61@gitee")
    idents = gdb.get_user_identities(uid)
    assert any(i["provider"] == "gitee" for i in idents)

    # 5) 凭据审计：credential_usage_log（user_id / initiating_actor_id TEXT 通道）
    from src.kernel.persistence.credential_usage_log import log_usage
    row_id = log_usage("cred-uc61", uid, initiating_actor_id=uid,
                       operation="test", repo_key="o/r", flush=True)
    assert _is_uuid36(row_id), f"审计行 id 应为 uuid36: {row_id!r}"

    # 6) 存储面终检：本用户触达的所有账号域外键列 typeof 恒 text 无混型
    # （binding_tickets 票据已被消费删除，补存一张在途票供终检）
    assert gdb.save_binding_ticket(uid, "gitcode", "ext-final",
                                   original_user_id=uid)
    con = sqlite3.connect(fresh_db)
    for table, col in (("binding_tickets", "initiating_user_id"),
                       ("binding_tickets", "original_user_id"),
                       ("external_identities", "grape_user_id"),
                       ("grape_t_credential_usage_log", "user_id"),
                       ("grape_t_credential_usage_log", "initiating_actor_id")):
        types = [r[0] for r in con.execute(
            f"SELECT DISTINCT typeof({col}) FROM {table} WHERE {col}=?",
            (uid,))]
        assert types == ["text"], f"{table}.{col} 存出混型: {types}"
    con.close()


def test_new_user_no_410_after_mixed_legacy_rows(fresh_db):
    """混型共存底线：同库先有存量 int 形态用户（字符串化 '24'）再注册 uuid
    新用户——两者票据互不串扰、各自校验通过（等值查询互查不中的根因锁）。"""
    from src.domains.mgmt.auth import grape_account_db as gdb
    # 存量用户：直接插 int 时代遗留的字符串化 id（迁移保值形态）
    con = sqlite3.connect(fresh_db)
    con.execute(
        "INSERT INTO grape_accounts(id, username, password_hash, status) "
        "VALUES ('24', 'legacy24', 'x', 1)")
    con.commit()
    con.close()
    new = gdb.create_grape_account("uc61b", "uc61b@example.com", "pw")
    assert _is_uuid36(new["id"])
    t_old = gdb.save_binding_ticket("24", "gitee", "e1")
    t_new = gdb.save_binding_ticket(new["id"], "gitee", "e2")
    assert gdb.validate_binding_ticket(t_old, "24", consume=False) is not None
    assert gdb.validate_binding_ticket(t_new, new["id"], consume=False) is not None
    assert gdb.validate_binding_ticket(t_old, new["id"], consume=False) is None, "存量/新用户票据不得互认"
    assert gdb.validate_binding_ticket(t_new, "24", consume=False) is None
    # 各自消费（一次性语义在新老形态 id 上都成立）
    assert gdb.validate_binding_ticket(t_old, "24") is not None
    assert gdb.validate_binding_ticket(t_new, new["id"]) is not None


# 不可行处说明（skip 说明，非运行时 skip——本闭环无需跳过任何用例）：
# - OAuth 回调握手（第三方 authorize/token 端点）：需真实外部 IdP 与回调域名，
#   不 mock 网络则不可行；账号建号路径以 create_grape_account_oauth 的 DB
#   直调等价覆盖（本文件走 create_grape_account，同库同主键生成链）。
# - HTTP 路由层（/auth/transfer-bind 等）：由 test_transfer_bind /
#   test_issue_376_mysql_transfer_bind 族覆盖，本文件不重复。
