"""obs_runtime_samples 多主单写租约测试（Issue #702）。

钉死四件事（Spec 验收对应的可测子集）：
1. 互斥：他主持有未过期租约时本主 acquire 失败（→ 循环跳过写入）；
2. 失联接管：持锁者 lease 过期（回拨 expires_at）后其余主控可原子抢占；
3. 续期：本主每轮重复 acquire 即续期（lease TTL 刷新、holder 不变）；
4. 清理绑定：过期样本的 DELETE 只在持锁路径的 cleanup_expired_samples() 执行，
   snapshot_runtimes() 不再顺带清理（消除多主各自清理放大）；
5. 降级：锁表异常 → acquire 返回 False（采样暂停、不放大写入），不抛出。

多主模拟：不发多进程，直接改 distributed_locks 行伪造"他主持有 / 失联过期"
（expires_at 是服务端唯一事实源，语义等价）。
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import sample_lease  # noqa: E402
from src.observability import rollup  # noqa: E402

PREFIX = "obslease-test-"
OTHER_HOLDER = "other-master-01"          # 模拟的另一台主控


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM distributed_locks WHERE lock_key=?", (sample_lease.LEASE_KEY,))
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE ?", (PREFIX + "%",))
            c.execute("DELETE FROM obs_runtime_samples WHERE device_name LIKE ?", (PREFIX + "%",))
        except Exception:
            pass
        db.commit()


def _mk_cred(name, status="active"):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name,"
                  " status, cpu_percent, memory_percent, disk_percent, bandwidth_percent)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  ("h-" + name, PREFIX + name, PREFIX + name, "global", "", status,
                   12.5, 34.5, 56.5, 1.5))
        db.commit()
        c.execute("SELECT id FROM runtime_credentials WHERE name=?", (PREFIX + name,))
        return int(c.fetchone()[0])


def _mk_sample(runtime_id, device, minutes_ago, cpu=10.0):
    from src.session_manager import db, db_lock
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO obs_runtime_samples (runtime_id, device_name, sampled_at,"
                  " cpu_percent, memory_percent, disk_percent, health_score, status)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (int(runtime_id), device, ts, cpu, 20.0, 50.0, 90, "online"))
        db.commit()


def _seed_other_holder(expires_at: int):
    """伪造另一台主控持有租约（直写锁行，等价于那台主控 acquire 成功后的状态）。"""
    from src.session_manager import db, db_lock
    now = int(time.time())
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM distributed_locks WHERE lock_key=?", (sample_lease.LEASE_KEY,))
        c.execute("INSERT INTO distributed_locks (lock_key, holder, locked_at, expires_at,"
                  " update_at, created_at) VALUES (?,?,?,?,?,?)",
                  (sample_lease.LEASE_KEY, OTHER_HOLDER, now, expires_at, now, now))
        db.commit()


def _count_samples(runtime_id) -> int:
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM obs_runtime_samples WHERE runtime_id=?", (int(runtime_id),))
        return int(c.fetchone()[0])


def _count_prefix_samples() -> int:
    """数本测试 PREFIX 的样本行（对其他用例/环境残留行免疫，隔离既有约定同 test_obs_rollup）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM obs_runtime_samples WHERE device_name LIKE ?", (PREFIX + "%",))
        return int(c.fetchone()[0])


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    yield
    _clean()


# ---------------------------------------------------------------- 租约语义
class TestSampleLease:
    def test_acquire_first_time_then_renew_extends(self):
        assert sample_lease.acquire_lease() is True
        st = sample_lease.lease_status()
        assert st is not None
        assert st["expires_at"] > int(time.time())
        # 持有者续期：expires_at 刷新、holder 不变
        time.sleep(1.1)                            # 保证 expires_at 可观测前进
        assert sample_lease.renew_lease() is True
        st2 = sample_lease.lease_status()
        assert st2 is not None
        assert st2["holder"] == st["holder"]
        assert st2["expires_at"] > st["expires_at"]

    def test_renew_fails_when_other_master_holds(self):
        now = int(time.time())
        _seed_other_holder(expires_at=now + 120)
        assert sample_lease.renew_lease() is False  # 只续自己的锁

    def test_acquire_fails_while_other_master_holds(self):
        now = int(time.time())
        _seed_other_holder(expires_at=now + 120)     # 他主持有、未过期
        assert sample_lease.acquire_lease() is False  # → 本轮跳过写入
        st = sample_lease.lease_status()
        assert st is not None
        assert st["holder"] == OTHER_HOLDER

    def test_takeover_after_lease_expired(self):
        now = int(time.time())
        _seed_other_holder(expires_at=now - 1)      # 他主失联：lease 已过期
        assert sample_lease.acquire_lease() is True  # 其余主控原子抢占接管
        st = sample_lease.lease_status()
        assert st is not None
        assert st["holder"] != OTHER_HOLDER

    def test_release_then_other_can_acquire(self):
        assert sample_lease.acquire_lease() is True
        sample_lease.release_lease()
        assert sample_lease.lease_status() is None
        now = int(time.time())
        _seed_other_holder(expires_at=now + 120)
        assert sample_lease.acquire_lease() is False  # 释放后他人可正常持有

    def test_acquire_degrades_to_false_when_lock_table_broken(self):
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute("DROP TABLE IF EXISTS distributed_locks")
            db.commit()
        # 锁表损坏 → 降级 False（采样暂停、不放大写入），不抛异常
        assert sample_lease.acquire_lease() is False
        assert sample_lease.lease_status() is None
        # 恢复锁表供后续用例（init_database 幂等但不重建已盖章迁移，直接补建表）
        from src.api.db_setup import _create_tables
        _create_tables()


# ---------------------------------------------------------------- 单写编排
class TestSingleWriterTick:
    def test_other_master_holding_skips_write_and_cleanup(self):
        """多主模拟核心验收：他主持有时本主一轮 0 INSERT、0 DELETE。"""
        rid = _mk_cred("a")
        _mk_sample(rid, PREFIX + "a", minutes_ago=8 * 24 * 60, cpu=99)  # 8 天前超龄样本
        now = int(time.time())
        _seed_other_holder(expires_at=now + 120)
        before = _count_samples(rid)

        r = rollup.sample_once_under_lease()

        assert r["leased"] is False and r["skipped"] is True
        assert r["inserted"] == 0, "未持锁不得写入采样"
        assert r["deleted"] == 0, "未持锁不得执行清理"
        assert _count_samples(rid) == before, "超龄样本未被未持锁者清理"

    def test_leased_tick_samples_and_cleans(self):
        rid = _mk_cred("b")
        _mk_sample(rid, PREFIX + "b", minutes_ago=8 * 24 * 60, cpu=99)  # 超龄
        r = rollup.sample_once_under_lease()
        assert r["leased"] is True and r["skipped"] is False
        assert r["inserted"] >= 1
        assert r["deleted"] >= 1, "持锁者应清理超龄样本"
        # 同一分钟同 runtime 至多 1 条新写入（单写者语义下不再叠加）
        assert _count_samples(rid) == 1

    def test_expired_lease_takeover_then_samples_resume(self):
        """失联接管：他主 lease 过期 → 本主接管并恢复采样（曲线无断点）。"""
        rid = _mk_cred("c")
        now = int(time.time())
        _seed_other_holder(expires_at=now - 1)      # 他主失联
        r1 = rollup.sample_once_under_lease()
        assert r1["leased"] is True and r1["inserted"] >= 1
        # 接管者持续持锁续期
        r2 = rollup.sample_once_under_lease()
        assert r2["leased"] is True and r2["skipped"] is False
        assert _count_samples(rid) == 2  # 两次采样各一条，无重复叠加


# ---------------------------------------------------------------- 清理职责拆分
class TestCleanupSplit:
    def test_snapshot_no_longer_prunes_expired(self):
        """snapshot_runtimes 只采样不清理（清理放大消除的契约）。"""
        rid = _mk_cred("d")
        _mk_sample(rid, PREFIX + "d", minutes_ago=8 * 24 * 60, cpu=99)
        before = _count_prefix_samples()
        r = rollup.snapshot_runtimes()
        assert r["inserted"] >= 1
        # 超龄 PREFIX 样本原样保留 + 本次新采样一条（不被 snapshot 清理）；
        # 按 PREFIX 计数对环境残留（主控 hostname runtime 等）免疫。
        assert _count_prefix_samples() == before + 1, "超龄样本应原样保留（不被 snapshot 清理）"

    def test_cleanup_expired_samples_prunes_only_overage(self):
        rid = _mk_cred("e")
        _mk_sample(rid, PREFIX + "e", minutes_ago=8 * 24 * 60, cpu=99)   # 超龄 → 清
        _mk_sample(rid, PREFIX + "e", minutes_ago=5, cpu=11)             # 7 天内 → 留
        r = rollup.cleanup_expired_samples()
        assert r["deleted"] == 1, "只清超龄的一条"
        assert _count_samples(rid) == 1, "7 天内样本保留"

    def test_cleanup_idempotent(self):
        rid = _mk_cred("f")
        _mk_sample(rid, PREFIX + "f", minutes_ago=9 * 24 * 60, cpu=99)
        r1 = rollup.cleanup_expired_samples()
        r2 = rollup.cleanup_expired_samples()
        assert r1["deleted"] == 1 and r2["deleted"] == 0
