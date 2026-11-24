"""Issue #3773: hermes ACP state.db 可自愈体积治理。"""
import importlib.util
import json
import os
import sqlite3
import sys
import time

import pytest

from src.domains.runtime.engine import state_db_maintenance as m
from src.domains.runtime.engine.context_compaction import check_state_db_size

NOW = 1_800_000_000.0
DAY = 86400.0


def _make_db(path, sessions, payload: int = 10_000, wal: bool = False):
    conn = sqlite3.connect(str(path))
    if wal:
        # 生产 hermes state.db 为 WAL 模式：VACUUM 的新镜像先落 WAL，主文件仍是旧尺寸
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at REAL,
            ended_at REAL,
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE TABLE session_model_usage (
            session_id TEXT,
            model TEXT,
            first_seen REAL,
            last_seen REAL
        );
        """
    )
    for item in sessions:
        sid, last_ts = item[0], item[1]
        ended_ts = item[2] if len(item) > 2 else None
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at, message_count) VALUES (?,?,?,?)",
            (sid, last_ts, ended_ts, 3),
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                (sid, "user", f"{sid}-{i}-" + ("x" * payload), last_ts + i),
            )
        conn.execute(
            "INSERT INTO session_model_usage (session_id, model, first_seen, last_seen)"
            " VALUES (?,?,?,?)",
            (sid, "m", last_ts, last_ts),
        )
    conn.commit()
    conn.close()


def _sessions(path):
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT id FROM sessions").fetchall()}
    finally:
        conn.close()


def _messages(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def _six_sessions():
    return [
        ("s_old40", NOW - 40 * DAY),
        ("s_old30", NOW - 30 * DAY),
        ("s_old20", NOW - 20 * DAY),
        ("s_rec3", NOW - 3 * DAY),
        ("s_rec1", NOW - 1 * DAY),
        ("s_new", NOW - 3600),
    ]


class TestPrune:
    def test_retention_window_prunes_only_old_sessions(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        before = m.db_size_bytes(str(db))
        res = m.prune_and_vacuum(
            str(db), retention_days=7, min_sessions=2,
            target_bytes=100 * 1024 * 1024, now=NOW,
            backup_dir=str(tmp_path / "backups"),
        )
        assert res.result == m._RESULT_OK
        assert res.pruned_sessions == 3
        assert res.pruned_messages == 9
        assert res.after_bytes <= before
        assert res.backup_path and os.path.isfile(res.backup_path)
        assert res.integrity_after == "ok"
        assert _sessions(str(db)) == {"s_rec3", "s_rec1", "s_new"}

    def test_min_sessions_protects_recent_sessions(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        res = m.prune_and_vacuum(
            str(db), retention_days=0, min_sessions=2,
            target_bytes=100 * 1024 * 1024, now=NOW,
            backup_dir=str(tmp_path / "backups"),
        )
        assert res.result == m._RESULT_OK
        assert res.pruned_sessions == 4
        assert _sessions(str(db)) == {"s_rec1", "s_new"}

    def test_dry_run_does_not_modify_or_backup(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        before = m.db_size_bytes(str(db))
        backups = tmp_path / "backups"
        res = m.prune_and_vacuum(
            str(db), retention_days=7, min_sessions=2, target_bytes=1,
            now=NOW, dry_run=True, backup_dir=str(backups),
        )
        assert res.result == m._RESULT_DRY_RUN
        assert res.pruned_sessions >= 3
        assert m.db_size_bytes(str(db)) == before
        assert not backups.exists()

    def test_failure_rolls_back_from_backup(self, tmp_path, monkeypatch):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        before_sessions = _sessions(str(db))
        before_messages = _messages(str(db))

        def _boom(_conn):
            raise RuntimeError("vacuum failed")

        monkeypatch.setattr(m, "_vacuum", _boom)
        res = m.prune_and_vacuum(
            str(db), retention_days=0, min_sessions=0, target_bytes=1,
            now=NOW, backup_dir=str(tmp_path / "backups"),
        )
        assert res.result == m._RESULT_FAILED
        assert res.reason == "restored_from_backup"
        assert res.backup_path and os.path.isfile(res.backup_path)
        assert _sessions(str(db)) == before_sessions
        assert _messages(str(db)) == before_messages


class TestCheckStateDbSize:
    def test_cleanup_success_allows_resume(self, tmp_path):
        db = tmp_path / "state.db"
        sessions = [
            ("s_old1", NOW - 30 * DAY),
            ("s_old2", NOW - 20 * DAY),
            ("s_new", NOW - 3600),
        ]
        _make_db(db, sessions, payload=40_000)
        res = check_state_db_size(
            str(tmp_path), threshold_mb=0.25, low_water_mb=0.1,
            retention_days=7, min_sessions=1, backup_dir=str(tmp_path / "b"),
            now=NOW, force=True,
        )
        assert res.exceeded is True
        assert res.cleanup.result == m._RESULT_OK
        assert res.skip_resume is False
        assert res.after_bytes <= res.before_bytes
        assert bool(res) is False

    def test_archive_when_protected_sessions_still_exceed(self, tmp_path):
        db = tmp_path / "state.db"
        sessions = [(f"s{i}", NOW - i * 60) for i in range(10)]
        _make_db(db, sessions, payload=20_000)
        res = check_state_db_size(
            str(tmp_path), threshold_mb=0.15, low_water_mb=0.05,
            retention_days=0, min_sessions=10, backup_dir=str(tmp_path / "b"),
            now=NOW, force=True,
        )
        assert res.exceeded is True
        assert res.skip_resume is True
        assert res.reason == "archived"
        assert res.archive and res.archive.result == m._RESULT_ARCHIVED
        assert res.archive.backup_path and os.path.isfile(res.archive.backup_path)
        assert os.path.getsize(db) < res.before_bytes

    def test_cleanup_failure_skips_resume(self, tmp_path, monkeypatch):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        monkeypatch.setattr(m, "prune_and_vacuum", lambda *a, **k: m.MaintenanceResult(
            db_path=str(db), result=m._RESULT_FAILED, error="locked"))
        res = check_state_db_size(
            str(tmp_path), threshold_mb=0.05, retention_days=7, min_sessions=2,
            now=NOW, force=True,
        )
        assert res.skip_resume is True
        assert res.reason == "cleanup_failed"

    def test_throttle_skips_second_cleanup(self, tmp_path, monkeypatch):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions(), payload=40_000)
        monkeypatch.setattr(m, "archive_and_recreate", lambda *a, **k: m.MaintenanceResult(
            db_path=str(db), result=m._RESULT_FAILED, error="keep-big-for-test"))
        first = check_state_db_size(
            str(tmp_path), threshold_mb=0.05, low_water_mb=0.01,
            retention_days=0, min_sessions=1, min_interval_s=3600,
            backup_dir=str(tmp_path / "b"), now=NOW, force=True,
        )
        assert first.cleanup.result == m._RESULT_OK
        assert first.skip_resume is True
        assert first.reason == "archive_failed"
        second = check_state_db_size(
            str(tmp_path), threshold_mb=0.000001, low_water_mb=0,
            retention_days=0, min_sessions=1, min_interval_s=3600,
            backup_dir=str(tmp_path / "b"), now=NOW + 1, force=False,
        )
        assert second.exceeded is True
        assert second.skip_resume is True
        assert second.reason == "throttled"

    def test_below_threshold_is_noop(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        res = check_state_db_size(str(tmp_path), threshold_mb=10, now=NOW)
        assert res.exceeded is False
        assert res.skip_resume is False
        assert res.cleanup is None


class TestDiscoveryAndMetrics:
    def test_discover_shared_and_isolated(self, tmp_path):
        root = tmp_path / "homes"
        (root).mkdir()
        _make_db(root / "state.db", [("shared", NOW)])
        child = root / "abc123"
        child.mkdir()
        _make_db(child / "state.db", [("iso", NOW)])
        other = root / "no_db"
        other.mkdir()
        paths = m.discover_state_dbs(str(root))
        names = {os.path.basename(os.path.dirname(p)) for p in paths}
        assert len(paths) == 2
        assert "homes" in names and "abc123" in names

    def test_worker_metrics_snapshot(self, tmp_path, monkeypatch):
        m.reset_metrics()
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        m.prune_and_vacuum(
            str(db), retention_days=0, min_sessions=1,
            target_bytes=100 * 1024 * 1024, now=NOW,
            backup_dir=str(tmp_path / "b"),
        )
        m.record_resume_skipped("test")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        snap = m.worker_metrics_snapshot()
        counts = json.loads(snap["acp_state_db_cleanup_counts"])
        assert counts.get("ok", 0) >= 1
        assert snap["acp_resume_skipped_total"] == 1
        assert snap["acp_state_db_bytes"] > 0
        m.reset_metrics()


class TestOpsScript:
    def _load(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "ops",
                            "hermes_acp_state_db_cleanup.py")
        spec = importlib.util.spec_from_file_location("hermes_acp_state_db_cleanup", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hermes_acp_state_db_cleanup"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_dry_run_then_cleanup_then_idempotent(self, tmp_path):
        mod = self._load()
        db = tmp_path / "state.db"
        real_now = time.time()
        sessions = [
            ("s_old40", real_now - 40 * DAY, real_now - 40 * DAY),
            ("s_old30", real_now - 30 * DAY, real_now - 30 * DAY),
            ("s_old20", real_now - 20 * DAY, real_now - 20 * DAY),
            ("s_rec3", real_now - 3 * DAY, real_now - 3 * DAY),
            ("s_rec1", real_now - 1 * DAY, real_now - 1 * DAY),
            ("s_new", real_now - 3600, None),
        ]
        _make_db(db, sessions, payload=40_000)
        before = m.db_size_bytes(str(db))
        backups = tmp_path / "b"
        rc = mod.main([
            "--db-path", str(db), "--dry-run", "--max-mb", "0.05",
            "--backup-dir", str(backups), "--retention-days", "7",
            "--min-sessions", "1", "--no-archive",
        ])
        assert rc == 0
        assert m.db_size_bytes(str(db)) == before
        assert not backups.exists()

        rc = mod.main([
            "--db-path", str(db), "--max-mb", "0.05", "--low-water-mb", "0.02",
            "--backup-dir", str(backups), "--retention-days", "7",
            "--min-sessions", "1", "--no-archive",
        ])
        assert rc == 0
        assert m.db_size_bytes(str(db)) < before
        assert list(backups.glob("*.bak"))

        rc = mod.main([
            "--db-path", str(db), "--max-mb", "0.05",
            "--backup-dir", str(backups), "--no-archive",
        ])
        assert rc == 0

class TestSizeAndLock:
    def test_db_size_includes_wal_and_shm(self, tmp_path):
        db = tmp_path / "state.db"
        db.write_bytes(b"x" * 100)
        (tmp_path / "state.db-wal").write_bytes(b"w" * 200)
        (tmp_path / "state.db-shm").write_bytes(b"s" * 50)
        assert m.db_size_bytes(str(db)) == 350

    def test_thread_lock_fallback_releases_each_time(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "fcntl", None)
        db = tmp_path / "state.db"
        db.write_bytes(b"x")
        first = m._DbLock(str(db))
        assert first.__enter__() is True
        assert m._thread_lock.locked() is True
        first.__exit__(None, None, None)
        assert m._thread_lock.locked() is False, "无 fcntl 回退路径泄漏 _thread_lock"
        second = m._DbLock(str(db))
        assert second.__enter__() is True
        second.__exit__(None, None, None)
        assert m._thread_lock.locked() is False

    def test_backup_dir_permissions_tightened_when_existing(self, tmp_path):
        import stat as _stat

        backups = tmp_path / "backups"
        backups.mkdir()
        os.chmod(backups, 0o755)  # 已存在的宽松目录
        db = tmp_path / "state.db"
        _make_db(db, [("s", NOW)])
        res = m.prune_and_vacuum(
            str(db), retention_days=7, min_sessions=1, target_bytes=100 * 1024 * 1024,
            now=NOW, backup_dir=str(backups),
        )
        assert res.result == m._RESULT_OK
        assert _stat.S_IMODE(os.stat(backups).st_mode) == 0o700
        assert _stat.S_IMODE(os.stat(res.backup_path).st_mode) == 0o600


class TestProtectAndForcedRound:
    def test_protect_session_ids_keeps_resume_target(self, tmp_path):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        res = m.prune_and_vacuum(
            str(db), retention_days=0, min_sessions=0,
            target_bytes=100 * 1024 * 1024, now=NOW,
            backup_dir=str(tmp_path / "backups"),
            protect_session_ids=["s_rec3"], force_min_idle_days=0,
        )
        assert res.result == m._RESULT_OK
        assert "s_rec3" in _sessions(str(db)), "即将 resume 的 session 被清理"
        assert _sessions(str(db)) == {"s_rec3"}

    def test_forced_round_only_prunes_closed_idle_sessions(self, tmp_path):
        m.reset_metrics()
        db = tmp_path / "state.db"
        sessions = [
            ("closed_old", NOW - 40 * DAY, NOW - 40 * DAY),
            ("closed_recent", NOW - 3600, NOW - 3600),
            ("active_old", NOW - 60 * DAY, None),
        ]
        _make_db(db, sessions)
        res = m.prune_and_vacuum(
            str(db), retention_days=365, min_sessions=0, target_bytes=1,
            now=NOW, backup_dir=str(tmp_path / "backups"),
            force_min_idle_days=1,
        )
        assert res.result == m._RESULT_OK
        assert res.forced_pruned_sessions == 1
        assert _sessions(str(db)) == {"closed_recent", "active_old"}, \
            "forced 轮不得删除未结束会话或刚结束的会话"
        assert m._cleanup_counts.get(m._RESULT_FORCED, 0) == 1
        m.reset_metrics()


class TestVacuumCheckpointInWalMode:
    """VACUUM 在 WAL 模式下把新镜像写进 WAL：不 checkpoint 则测量虚高。

    后果一：``after_bytes``/``freed_bytes`` 失真，指标恒 0；
    后果二：低水位停轮条件恒不成立，forced 轮必执行并多删已结束但仍在保留窗口内的
    会话（实测 19 个「已结束 2 天」会话被删）。
    """

    def test_retention_round_reports_real_disk_size(self, tmp_path):
        db = tmp_path / "state.db"
        # 40 个超保留窗口、每个 ~250KB 的会话：单跑 retention 轮就足以压到目标以下
        sessions = [(f"old{i}", NOW - 30 * DAY, NOW - 30 * DAY) for i in range(40)]
        sessions += [(f"recent{i}", NOW - 3600 * i, None) for i in range(10)]
        _make_db(db, sessions, payload=50_000, wal=True)
        before = m.db_size_bytes(str(db))

        res = m.prune_and_vacuum(
            str(db), retention_days=7, min_sessions=10, target_bytes=0,
            now=NOW, backup_dir=str(tmp_path / "backups"), force_min_idle_days=1,
        )

        assert res.result == m._RESULT_OK
        assert res.pruned_sessions == 40
        # freed_bytes 必须反映真实释放量，而不是「旧主库 + WAL 新镜像」的虚高值
        assert res.freed_bytes > 0
        assert res.after_bytes < before
        # 测量口径 = 磁盘真实占用（会话关闭后 SHM 会被清理，故留 64KB 容差）
        assert abs(res.after_bytes - m.db_size_bytes(str(db))) <= 64 * 1024
        assert res.after_bytes <= 6 * 1024 * 1024

    def test_round1_hitting_target_skips_forced_round(self, tmp_path):
        m.reset_metrics()
        db = tmp_path / "state.db"
        sessions = [(f"old{i}", NOW - 30 * DAY, NOW - 30 * DAY) for i in range(40)]
        # 已结束 2 天：在 7 天保留窗口内，仅 forced 轮会删；轮次判定正确时不该被删
        sessions += [(f"closed2d{i}", NOW - 2 * DAY, NOW - 2 * DAY) for i in range(19)]
        sessions += [(f"recent{i}", NOW - 3600 * i, None) for i in range(10)]
        _make_db(db, sessions, payload=50_000, wal=True)

        res = m.prune_and_vacuum(
            str(db), retention_days=7, min_sessions=10, target_bytes=5 * 1024 * 1024,
            now=NOW, backup_dir=str(tmp_path / "backups"), force_min_idle_days=1,
        )

        assert res.result == m._RESULT_OK
        assert res.pruned_sessions == 40
        assert res.forced_pruned_sessions == 0, "retention 轮已达标，forced 轮不得再删会话"
        assert res.freed_bytes > 0
        kept = _sessions(str(db))
        assert {f"closed2d{i}" for i in range(19)} <= kept
        assert m._cleanup_counts.get(m._RESULT_FORCED, 0) == 0
        m.reset_metrics()


class TestBackupFailure:
    def test_backup_failure_returns_failed_without_raising(self, tmp_path, monkeypatch):
        db = tmp_path / "state.db"
        _make_db(db, _six_sessions())
        before_sessions = _sessions(str(db))

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(m, "_backup_db", _boom)
        res = m.prune_and_vacuum(
            str(db), retention_days=0, min_sessions=0, target_bytes=1,
            now=NOW, backup_dir=str(tmp_path / "backups"),
        )
        assert res.result == m._RESULT_FAILED
        assert res.reason == "backup_failed"
        assert res.backup_path == ""
        assert _sessions(str(db)) == before_sessions

        health = check_state_db_size(
            str(tmp_path), threshold_mb=0.0001, low_water_mb=0,
            retention_days=0, min_sessions=0, backup_dir=str(tmp_path / "backups"),
            now=NOW, force=True,
        )
        assert health.exceeded is True
        assert health.skip_resume is True
        assert health.cleanup.reason == "backup_failed"


class TestAcpResumeDecision:
    def test_check_passes_resume_session_as_protected(self, monkeypatch, tmp_path):
        from src.domains.runtime.agents import hermes_acp as ha

        captured = {}

        class _Health:
            skip_resume = False
            reason = ""
            db_path = str(tmp_path / "state.db")
            after_bytes = 1

        def fake_check(home, **kwargs):
            captured["home"] = home
            captured.update(kwargs)
            return _Health()

        monkeypatch.setattr(
            "src.domains.runtime.engine.context_compaction.check_state_db_size",
            fake_check)
        _health, skip = ha._check_state_db_before_resume(
            str(tmp_path), "sess-resume", label="t ")
        assert captured["protect_session_ids"] == ["sess-resume"]
        assert skip is False

    def test_check_records_resume_skipped(self, monkeypatch):
        from src.domains.runtime.agents import hermes_acp as ha

        recorded = []

        class _Health:
            skip_resume = True
            reason = "archived"
            db_path = "/tmp/state.db"
            after_bytes = 10

        monkeypatch.setattr(
            "src.domains.runtime.engine.context_compaction.check_state_db_size",
            lambda home, **kwargs: _Health())
        monkeypatch.setattr(m, "record_resume_skipped",
                            lambda reason: recorded.append(reason))
        _health, skip = ha._check_state_db_before_resume("/tmp", "sess")
        assert skip is True
        assert recorded == ["archived"]
