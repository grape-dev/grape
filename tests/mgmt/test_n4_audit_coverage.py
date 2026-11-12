"""N4 实弹探针（pytest 环境）：rc UUID 回填 + 命中审计行落库 + 幂等。"""
import os
import uuid

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from src.kernel.persistence.credential_usage_log import list_by_credential
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user import db as udb
from src.domains.mgmt.user.credentials_backfill_n1 import backfill_repo_credentials_uuid


def _clean(rk):
    with db_lock:
        db.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
        db.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
        db.commit()


def test_n4_1_rc_uuid_backfill_and_hit_audit():
    """rc 加 id → 回填 UUID → 命中审计行（credential_id=UUID + repo_key）。"""
    rk = "gitcode:n4a" + uuid.uuid4().hex[:6] + "/repo"
    try:
        # 写入（add_managed_repo 走 rc executemany——此时 id 为空串）
        assert udb.add_managed_repo(rk, "n4a", "repo",
                                    bot_gitcode_token="tok_n4") is True
        # 回填
        backfill_repo_credentials_uuid()
        row = db.execute(
            "SELECT id, value FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type='bot'",
            (rk,)).fetchone()
        assert row and row[0], "id 未回填"
        rid = row[0]
        assert len(rid) == 36 and rid.count("-") == 4
        # TokenResolver 命中（审计行落库）
        from src.kernel.token_resolver import TokenResolver, create_provider_from_repo_key
        resolver = TokenResolver(create_provider_from_repo_key(rk))
        resolver.strategies = [s for s in resolver.strategies if s.level == "repo_bot"]
        tok = resolver.get_token(rk)
        assert tok == "tok_n4", f"命中失败: {tok!r}"
        # 异步落库=最终一致：轮询等待后台批量刷
        import time
        rows = []
        deadline = time.time() + 5
        while time.time() < deadline:
            rows = list_by_credential(rid)
            if rows:
                break
            time.sleep(0.1)
        assert rows, "审计行未落库（5s 内）"
        assert rows[0]["credential_kind"] == "repo_credentials"
        assert rows[0]["repo_key"] == rk
        assert rows[0]["operation"] == "repo_bot_hit"
    finally:
        _clean(rk)


def test_n4_2_historical_hit_audit_with_repo_key():
    """历史列回落命中：审计行 credential_id 空 + repo_key 冗余定位。"""
    rk = "gitcode:n4b" + uuid.uuid4().hex[:6] + "/repo"
    try:
        with db_lock:
            db.execute(
                "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, bot_gitcode_token) "
                "VALUES (?,?,?,?,?)", (rk, "n4b", "repo", "n4b/repo", "hist_tok_n4"))
            db.commit()
        from src.kernel.token_resolver import TokenResolver, create_provider_from_repo_key
        resolver = TokenResolver(create_provider_from_repo_key(rk))
        resolver.strategies = [s for s in resolver.strategies if s.level == "repo_bot"]
        tok = resolver.get_token(rk)
        assert tok == "hist_tok_n4"
        # 历史列审计行（repo_key 定位）——异步最终一致，轮询等待
        import time
        row = None
        deadline = time.time() + 5
        while time.time() < deadline:
            with db_lock:
                row = db.execute(
                    "SELECT credential_id, repo_key, operation FROM grape_t_credential_usage_log "
                    "WHERE repo_key=? AND operation='repo_bot_hit_legacy_column' "
                    "ORDER BY created_at DESC LIMIT 1", (rk,)).fetchone()
            if row:
                break
            time.sleep(0.1)
        assert row, "历史列审计行未落库（5s 内）"
        assert row[0] == "" and row[1] == rk
    finally:
        _clean(rk)


def test_n4_3_backfill_idempotent():
    """幂等：二次回填零新增。"""
    rk = "gitcode:n4c" + uuid.uuid4().hex[:6] + "/repo"
    try:
        udb.add_managed_repo(rk, "n4c", "repo", bot_gitcode_token="tok_n4c")
        backfill_repo_credentials_uuid()
        id1 = db.execute(
            "SELECT id FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type='bot'",
            (rk,)).fetchone()[0]
        backfill_repo_credentials_uuid()
        id2 = db.execute(
            "SELECT id FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type='bot'",
            (rk,)).fetchone()[0]
        assert id1 == id2, "幂等失败：二次回填改 id"
    finally:
        _clean(rk)


class TestN4Perf:
    """N4-p1/p2/p3：索引、保留期、异步写入。"""

    def test_p1_indexes_exist(self):
        """审计查询索引在场（防全表扫）。"""
        idx = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='grape_t_credential_usage_log'").fetchall()
        names = {r[0] for r in idx}
        assert "idx_cul_credential_id" in names
        assert "idx_cul_repo_key" in names

    def test_p2_retention_cleanup(self):
        """保留期清理：超期行删、新行留（force 跳过节流）。"""
        import datetime
        from src.kernel.persistence.credential_usage_log import (
            RETENTION_DAYS, cleanup_expired_retention)
        rk = "n4p2" + uuid.uuid4().hex[:6]
        old_ts = (datetime.datetime.now()
                  - datetime.timedelta(days=RETENTION_DAYS + 1)).strftime("%Y-%m-%d %H:%M:%S")
        with db_lock:
            db.execute(
                "INSERT INTO grape_t_credential_usage_log (id, credential_id, credential_kind, user_id, created_at) "
                "VALUES (?, ?, 'repo_credentials', 1, ?)",
                (str(uuid.uuid4()), rk, old_ts))
            db.commit()
        n = cleanup_expired_retention(force=True)
        assert n >= 1
        with db_lock:
            left = db.execute(
                "SELECT COUNT(*) FROM grape_t_credential_usage_log WHERE credential_id=?", (rk,)).fetchone()[0]
        assert left == 0

    def test_p2b_cleanup_throttle_30d(self, monkeypatch):
        """清理节流：30 天窗口内第二次调用零执行（monotonic 进程内节流）。"""
        from src.kernel.persistence import credential_usage_log as cul
        cul._cleanup_last_mono = 0.0  # 重置进程内状态（测试隔离）
        import time
        # 模拟"刚清理过"：_last = now → 窗口内调用直接跳过
        cul._cleanup_last_mono = time.monotonic()
        n = cul.cleanup_expired_retention()
        assert n == 0, "节流窗口内不应执行"
        # force 绕过节流
        n2 = cul.cleanup_expired_retention(force=True)
        assert n2 >= 0  # force 执行（返回清理数）

    def test_p2c_batch_delete(self):
        """分批 DELETE：批量造 2500 超期行（>单批 1000）→ 全清 + 幂等。"""
        import datetime
        from src.kernel.persistence.credential_usage_log import cleanup_expired_retention
        old_ts = (datetime.datetime.now()
                  - datetime.timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
        tag = "n4p2c" + uuid.uuid4().hex[:6]
        with db_lock:
            for i in range(2500):
                db.execute(
                    "INSERT INTO grape_t_credential_usage_log (id, credential_id, credential_kind, user_id, created_at) "
                    "VALUES (?, ?, 'repo_credentials', 1, ?)",
                    (str(uuid.uuid4()), tag, old_ts))
            db.commit()
        n = cleanup_expired_retention(force=True)
        assert n >= 2500
        with db_lock:
            left = db.execute(
                "SELECT COUNT(*) FROM grape_t_credential_usage_log WHERE credential_id=?", (tag,)).fetchone()[0]
        assert left == 0

    def test_p3_async_enqueue_flushes(self):
        """异步入队 → 后台批量落库（flush=True 直写对照）。"""
        from src.kernel.persistence.credential_usage_log import log_usage
        import time
        rid = log_usage("n4p3_async", 1, operation="async_test")
        assert rid
        # 后台线程批量刷（攒批窗口内轮询等待）
        deadline = time.time() + 5
        while time.time() < deadline:
            if list_by_credential("n4p3_async"):
                break
            time.sleep(0.1)
        rows = list_by_credential("n4p3_async")
        assert rows and rows[0]["operation"] == "async_test"
        # flush=True 同步直写
        rid2 = log_usage("n4p3_sync", 1, operation="sync_test", flush=True)
        rows2 = list_by_credential("n4p3_sync")
        assert rows2 and rows2[0]["id"] == rid2
