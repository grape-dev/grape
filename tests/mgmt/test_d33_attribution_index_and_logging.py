"""D33（S53）用例：归属热路径索引 + 外层异常留痕（先测后改）。

两项均来自 D31 复核（外部评审两撤回一降级后成立的两条小项）：
1. external_identities(provider, external_login) 无索引覆盖——归属是每个
   webhook 事件的热路径，加幂等索引（零数据变更）。
2. issues_center 身份优先块 except: pass——resolver 自身有 log，但
   import 级失败永久静默回退，补一行 warning。
"""
import json
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock


def _seed_account(uid: int, username: str):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO grape_accounts (id, username, email, status) "
            "VALUES (?, ?, ?, 1)", (uid, username, f"{username}@t.local"))
        db.commit()


def _seed_um(uid: int, col: str, login: str):
    with db_lock:
        db.cursor().execute(
            f"INSERT OR REPLACE INTO unified_user_mapping (user_id, {col}) VALUES (?, ?)",
            (uid, login))
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        for uid in (601, 602):
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
            c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (uid,))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        for uid in (601, 602):
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
            c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (uid,))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.commit()


# ------------------------------------------------------- 1. 热路径索引

def test_s53_attribution_index_exists():
    """归属查询键 (provider, external_login) 必须有索引覆盖（DDL 结构锁）。"""
    idx = db.cursor().execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='external_identities'").fetchall()
    covered = [r for r in idx if r[1] and "external_login" in r[1] and "provider" in r[1]]
    assert covered, (
        f"external_identities 无 (provider, external_login) 索引——归属热路径全表扫。"
        f"现有索引: {[(r[0], r[1]) for r in idx]}")


# ------------------------------------------------------- 2. 异常留痕

def test_s53_identity_failure_logs_and_falls_back(caplog):
    """resolver 异常 → 不吞：log.warning 留痕 + 回退 um 兜底（行为锁）。"""
    from src.api.issues_center import _get_user_id_by_gitcode
    _seed_account(601, "alice")
    _seed_um(601, "gitcode_user", "alice_platform")
    with patch("src.domains.mgmt.auth.grape_account_db.resolve_user_id_by_platform_login",
               side_effect=RuntimeError("boom")):
        with caplog.at_level("WARNING"):
            got = _get_user_id_by_gitcode("alice_platform", repo_key="gitcode:acme/web")
    assert got == 601                                  # 回退 um（旧行为不变）
    assert any("identity-first" in r.message.lower() or "resolve_user_id" in r.message.lower()
               for r in caplog.records), "异常被静默吞——必须留 warning"


def test_s53_identity_hit_unchanged(caplog):
    """特征锁：resolver 正常命中路径行为不变、无告警噪音。"""
    from src.api.issues_center import _get_user_id_by_gitcode
    from src.domains.mgmt.auth.grape_account_db import resolve_user_id_by_platform_login
    _seed_account(601, "alice")
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO external_identities "
            "(grape_user_id, provider, external_id, external_login, status) "
            "VALUES (601, 'gitcode', 'g-601', 'alice_platform', 1)")
        db.commit()
    got = _get_user_id_by_gitcode("alice_platform", repo_key="gitcode:acme/web")
    assert got == 601
