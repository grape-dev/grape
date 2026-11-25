"""Issue #3750：webhook 验签拒绝事实台账（`obs_webhook_rejects`）。

钉死四件容易回归的事：
1. **只存指纹不存明文**：`token_fp`/`expected_fp` 恒为 `sha256:<12>` 或 `absent`，
   任何长度的 token 前缀/子串都不得落库（#860 脱敏成果）；
2. **写入不阻塞**：有界队列，队满丢弃并计数，不抛、不重试轰炸；
3. **窗口计数按仓库**：`count_recent` 只数窗口内、按 repo_key 分组（跨仓库不聚合）；
4. **建表与归属**：DDL 登记在 `db_setup._NEW_TABLES`，归属清单登记为 insight 域。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.insight.observability import webhook_rejects as wr  # noqa: E402

REPO = "gitcode:grape-dev/grape"
OTHER = "gitcode:grape-dev/other"
EXPECTED = "expected-secret-3750"
WRONG = "stale-secret-3750"


def _clean():
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM obs_webhook_rejects")
        db.commit()


def _rows(repo_key=REPO):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, provider, event, ip, token_fp, expected_fp, reason,"
                  " created_at FROM obs_webhook_rejects WHERE repo_key=?", (repo_key,))
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]


def _insert(repo_key, created_at, token_fp="sha256:aaaaaaaaaaaa"):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute(
            "INSERT INTO obs_webhook_rejects (repo_key, provider, event, ip, token_fp,"
            " expected_fp, reason, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (repo_key, "gitcode", "Push Hook", "203.0.113.9", token_fp,
             "sha256:bbbbbbbbbbbb", "mismatch", created_at))
        db.commit()


class TestFingerprint:
    def test_format_is_stable_and_irreversible(self):
        fp = wr.token_fingerprint(EXPECTED)
        assert fp.startswith("sha256:")
        assert len(fp) == len("sha256:") + 12
        assert fp == wr.token_fingerprint(EXPECTED)
        assert fp != wr.token_fingerprint(WRONG)
        assert EXPECTED not in fp
        assert EXPECTED[:6] not in fp

    def test_absent_token(self):
        assert wr.token_fingerprint("") == "absent"
        assert wr.token_fingerprint(None) == "absent"

    def test_reason(self):
        assert wr.reject_reason("") == "absent"
        assert wr.reject_reason(None) == "absent"
        assert wr.reject_reason(WRONG) == "mismatch"


class TestRecord:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        wr.flush(timeout=2.0)
        _clean()

    def test_record_writes_fingerprints_only(self):
        assert wr.record_reject(REPO, "gitcode", "Push Hook", "203.0.113.9",
                                token=WRONG, expected=EXPECTED) is True
        wr.flush(timeout=3.0)
        rows = _rows()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["provider"] == "gitcode"
        assert row["event"] == "Push Hook"
        assert row["ip"] == "203.0.113.9"
        assert row["reason"] == "mismatch"
        assert row["token_fp"] == wr.token_fingerprint(WRONG)
        assert row["expected_fp"] == wr.token_fingerprint(EXPECTED)
        assert row["created_at"]
        # 明文/前缀一律不得落库
        flat = "|".join(str(v) for v in row.values())
        assert WRONG not in flat and EXPECTED not in flat
        assert WRONG[:6] not in flat and EXPECTED[:6] not in flat

    def test_absent_token_records_absent_reason(self):
        wr.record_reject(REPO, "gitcode", "Note Hook", "203.0.113.9",
                         token="", expected=EXPECTED)
        wr.flush(timeout=3.0)
        row = _rows()[0]
        assert row["reason"] == "absent"
        assert row["token_fp"] == "absent"

    def test_queue_full_drops_without_raising(self):
        rec = wr._RejectRecorder(maxsize=1)
        assert rec.record(REPO) is True
        assert rec.record(REPO) is False
        assert rec.dropped == 1
        assert rec.stats()["dropped"] == 1


class TestAsyncRecorder:
    """生产路径（有界队列 + 后台线程）也要有用例：sync 模式下测试不覆盖这条链路。"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_background_writer_persists_batch(self):
        rec = wr._RejectRecorder(maxsize=16)
        rec.start()
        try:
            for _ in range(3):
                assert rec.record(REPO, "gitcode", "Push Hook", "203.0.113.9",
                                  "sha256:aaaaaaaaaaaa", "sha256:bbbbbbbbbbbb", "mismatch")
            rec.flush(timeout=5.0)
            assert rec.stats()["written"] == 3, rec.stats()
            assert len(_rows()) == 3
        finally:
            rec._stop.set()


class TestCountRecent:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_window_and_grouping(self):
        now = datetime.now(timezone.utc)
        _insert(REPO, now.isoformat())
        _insert(REPO, (now - timedelta(seconds=10)).isoformat())
        # 窗口外（2 分钟前）不计
        _insert(REPO, (now - timedelta(minutes=2)).isoformat())
        _insert(OTHER, now.isoformat())

        from src.kernel.session_manager import db, db_lock
        with db_lock:
            counts = wr.count_recent(db.cursor(), window_sec=60)
        assert counts.get(REPO) == 2, counts
        assert counts.get(OTHER) == 1, "跨仓库必须各自计数，不能聚合"

    def test_empty_table_is_zero(self):
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            assert wr.count_recent(db.cursor(), window_sec=60) == {}


class TestCountRecentSplit:
    """一条条件聚合同时给出「存在期内的仓库」与「窗口内计数」（告警求值单次查询）。"""

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_presence_dimension_with_zero_window_count(self):
        now = datetime.now(timezone.utc)
        _insert(REPO, now.isoformat())
        # 窗口外（2 分钟前）、存在期（15 分钟）内 → 维度保留但计数为 0（走 resolved 分支）
        _insert(OTHER, (now - timedelta(minutes=2)).isoformat())
        # 存在期外（20 分钟前）→ 维度整体消失
        _insert("gitcode:grape-dev/old", (now - timedelta(minutes=20)).isoformat())

        from src.kernel.session_manager import db, db_lock
        with db_lock:
            counts = wr.count_recent_split(db.cursor(), window_sec=60, presence_sec=900)
        assert counts == {REPO: 1, OTHER: 0}, counts

    def test_single_query_matches_count_recent_when_window_is_presence(self):
        now = datetime.now(timezone.utc)
        _insert(REPO, now.isoformat())
        _insert(REPO, (now - timedelta(seconds=30)).isoformat())
        _insert(OTHER, (now - timedelta(minutes=5)).isoformat())

        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            assert wr.count_recent_split(c, window_sec=60, presence_sec=60) == \
                wr.count_recent(c, window_sec=60) == {REPO: 2}


class TestRetention:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_cleanup_removes_expired_rows(self):
        now = datetime.now(timezone.utc)
        _insert(REPO, (now - timedelta(days=30)).isoformat())
        _insert(REPO, now.isoformat())
        wr._last_cleanup = 0.0
        removed = wr.maybe_cleanup(interval=0, days=14)
        assert removed == 1
        assert len(_rows()) == 1

    def test_interval_throttles_second_call(self):
        wr._last_cleanup = 0.0
        wr.maybe_cleanup(interval=3600, days=14)
        assert wr.maybe_cleanup(interval=3600, days=14) == 0


class TestSchemaRegistration:
    def test_ddl_registered_for_creation(self):
        from src.api import db_setup
        names = {n for n, _ddl in db_setup._NEW_TABLES}
        assert "obs_webhook_rejects" in names

    def test_table_exists_after_init(self):
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='obs_webhook_rejects'").fetchone()
        assert row is not None

    def test_owner_is_insight(self):
        from src.kernel import table_ownership as own
        assert own.owner_of("obs_webhook_rejects") == "insight"

    def test_index_covers_time_range_then_repo(self):
        """窗口计数与留存清理都是 `WHERE created_at >= / <`（不带 repo_key 等值），
        故索引首列必须是 created_at；(repo_key, created_at) 反序对这类查询用不上索引。"""
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            rows = db.cursor().execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index'"
                " AND name='idx_obs_webhook_rejects_time_repo'").fetchall()
        assert rows, "缺 (created_at, repo_key) 索引：窗口计数会全表扫"
        sql = (rows[0][1] or "").replace(" ", "").lower()
        assert "(created_at,repo_key)" in sql, sql