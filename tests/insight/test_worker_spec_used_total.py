"""看板 worker 卡片：把百分比还原成「已用 / 总量」（内存），CPU 仍显示百分比。

worker 心跳只上报百分比（cpu_percent / memory_percent），库里没有绝对值；机型 spec
（如 "8C14G"）是心跳一起上报的，解析后即可给出分母。本文件锁住三层契约：

1. `_spec_cpu_mem` 的解析口径（含解析不出时返回 None，不编造分母）；
2. `/ops/overview` 的 worker 行新增 `memory_total_mb` / `memory_used_mb` / `cpu_cores`；
3. 14 列旧 provider 行仍然可用（灰度期远端实现可能还没带上 spec）。
"""
import os
import sys

from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock

RK = "gitcode:selftest/worker-spec"
CRED_ID = 9731


def _user():
    return {"id": 1, "username": "admin", "org_id": 0, "role": "admin"}


def _clean():
    with db_lock:
        try:
            db.cursor().execute("DELETE FROM runtime_credentials WHERE id=?", (CRED_ID,))
            db.commit()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


def _seed_worker(spec, cpu=12.5, mem=41.2):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (id, token_hash, name, device_name, scope, status, "
            "last_seen_at, last_ip, cpu_percent, memory_percent, disk_percent, tmp_percent, "
            "bandwidth_percent, bandwidth_mbps, load_avg, owner_name, spec) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (CRED_ID, "hash-spec-test", "spec-test-worker", "spec-test-host", "user", "active",
             datetime.now(timezone.utc).isoformat(), "10.0.0.9",
             cpu, mem, 56.0, 30.4, 1.0, 2.5, "0.1,0.2", "admin", spec))
        db.commit()


class TestSpecParsing:

    @pytest.mark.parametrize("spec,cores,mem_mb", [
        ("8C14G", 8, 14 * 1024),
        ("4C7G", 4, 7 * 1024),
        ("4C15G", 4, 15 * 1024),
        ("2C3.5G", 2, 3584),
        ("", None, None),
        ("unknown", None, None),
        ("Windows", None, None),
    ])
    def test_spec_parse(self, spec, cores, mem_mb):
        from src.domains.insight.routes.dashboard import _spec_cpu_mem
        assert _spec_cpu_mem(spec) == (cores, mem_mb)


class TestWorkerRowsCarryTotals:

    def test_overview_exposes_used_and_total(self):
        from src.domains.insight.routes.dashboard import get_ops_overview
        _seed_worker("8C14G", cpu=12.5, mem=41.2)
        resp = get_ops_overview(repo="", user=_user())
        rows = [w for w in resp["workers"]["list"] if w.get("id") == CRED_ID]
        assert rows, "未找到刚插入的 worker 行"
        w = rows[0]
        assert w["cpu_cores"] == 8
        assert w["memory_total_mb"] == 14 * 1024
        assert w["memory_used_mb"] == int(round(14 * 1024 * 41.2 / 100.0))
        assert w["cpu_percent"] == 12.5

    def test_overview_without_spec_falls_back_to_percent_only(self):
        from src.domains.insight.routes.dashboard import get_ops_overview
        _seed_worker("", cpu=5.0, mem=30.0)
        resp = get_ops_overview(repo="", user=_user())
        w = [x for x in resp["workers"]["list"] if x.get("id") == CRED_ID][0]
        assert w["memory_total_mb"] is None
        assert w["memory_used_mb"] is None
        assert w["memory_percent"] == 30.0, "没有分母时仍要保留百分比，前端据此回落"

    def test_legacy_14_column_rows_still_work(self, monkeypatch):
        """灰度期远端 provider 仍可能只回 14 列（无 spec）——不得整段降级。"""
        from src.domains.insight.routes import dashboard
        from src.domains.runtime import public as rt_public

        def _rows(*_a, **_kw):
            return [(CRED_ID, "legacy-worker", "legacy-host", "active",
                     datetime.now(timezone.utc).isoformat(), "10.0.0.8",
                     11.0, 22.0, 33.0, 44.0, 0.5, 1.5, "0.0", "admin")]

        monkeypatch.setattr(rt_public, "dashboard_worker_rows", _rows, raising=False)
        resp = dashboard.get_ops_overview(repo="", user=_user())
        assert "workers" not in resp["degraded"], resp["degraded"]
        w = [x for x in resp["workers"]["list"] if x.get("id") == CRED_ID][0]
        assert w["memory_percent"] == 22.0
        assert w["memory_total_mb"] is None
        assert w["spec"] == ""

class TestMetricsStaleness:
    """心跳断了以后卡片上的 cpu/mem 是最后一次采样值（多数是 0）——必须标成「无采样」。"""

    def test_stale_worker_is_flagged(self):
        from src.domains.insight.routes.dashboard import get_ops_overview
        from datetime import timedelta
        _seed_worker("4C7G", cpu=0.0, mem=0.0)
        with db_lock:
            db.cursor().execute(
                "UPDATE runtime_credentials SET last_seen_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat(), CRED_ID))
            db.commit()
        w = [x for x in get_ops_overview(repo="", user=_user())["workers"]["list"]
             if x.get("id") == CRED_ID][0]
        assert w["metrics_stale"] is True
        # 阈值与健康态派生同源（RUNTIME_STALE_SECONDS 默认 100s）
        assert w["metrics_age_seconds"] and w["metrics_age_seconds"] > 100

    def test_fresh_worker_is_not_flagged(self):
        from src.domains.insight.routes.dashboard import get_ops_overview
        _seed_worker("4C7G", cpu=12.5, mem=41.2)
        w = [x for x in get_ops_overview(repo="", user=_user())["workers"]["list"]
             if x.get("id") == CRED_ID][0]
        assert w["metrics_stale"] is False
        assert w["metrics_age_seconds"] is not None and w["metrics_age_seconds"] < 100


class TestAllZeroMetrics:
    """四项全 0 但心跳新鲜 → 按真实采样展示（不再改判成「无采样」）。"""

    def test_all_zero_metrics_kept_as_sample(self):
        from src.domains.insight.routes.dashboard import get_ops_overview
        _seed_worker("4C7G", cpu=0.0, mem=0.0)
        with db_lock:
            db.cursor().execute(
                "UPDATE runtime_credentials SET cpu_percent=0, memory_percent=0, "
                "disk_percent=0, tmp_percent=0 WHERE id=?", (CRED_ID,))
            db.commit()
        w = [x for x in get_ops_overview(repo="", user=_user())["workers"]["list"]
             if x.get("id") == CRED_ID][0]
        assert w["metrics_stale"] is False, "心跳新鲜就该按采样展示，不能因为全 0 改判"
        assert w["cpu_percent"] == 0 and w["memory_percent"] == 0

    def test_partial_metrics_not_stale(self):
        from src.domains.insight.routes.dashboard import get_ops_overview
        _seed_worker("4C7G", cpu=0.0, mem=41.2)
        w = [x for x in get_ops_overview(repo="", user=_user())["workers"]["list"]
             if x.get("id") == CRED_ID][0]
        assert w["metrics_stale"] is False
