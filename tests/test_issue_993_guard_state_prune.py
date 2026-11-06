"""Issue #993 R9/R10：branch_guard_state TTL 清理 + preflight 并发查询。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import src.runtime.branch_guard as bg
from src.session_manager import db, db_lock

RK = "gitcode:prune993/repo"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM branch_guard_state WHERE repo_key=?", (RK,))
        db.commit()


def _insert(branch, updated_at, pending=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO branch_guard_state "
            "(repo_key, head_branch, expected_remote_sha, pr_number, base_branch, updated_at, "
            " pending_expected_sha, pending_prev_sha) VALUES (?,?,?,?,?,?,?,?)",
            (RK, branch, "a" * 40, 1, "weekly-mys", updated_at, pending, ""))
        db.commit()


@pytest.fixture(autouse=True)
def _reset():
    _clean()
    yield
    _clean()


def test_prune_removes_only_old_rows_without_pending():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=120)).isoformat()
    recent = (now - timedelta(days=10)).isoformat()
    _insert("feat/old", old)
    _insert("feat/recent", recent)
    _insert("feat/old-pending", old, pending="b" * 40)
    n = bg.prune_guard_state(90)
    with db_lock:
        left = {r[0] for r in db.cursor().execute(
            "SELECT head_branch FROM branch_guard_state WHERE repo_key=?", (RK,)).fetchall()}
    assert n == 1
    assert left == {"feat/recent", "feat/old-pending"}, left


def test_prune_is_idempotent_and_returns_zero_second_time():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    _insert("feat/gone", old)
    assert bg.prune_guard_state(90) == 1
    assert bg.prune_guard_state(90) == 0


def test_prune_disabled_by_non_positive_days():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    _insert("feat/keep", old)
    assert bg.prune_guard_state(0) == 0
    with db_lock:
        cnt = db.cursor().execute(
            "SELECT COUNT(*) FROM branch_guard_state WHERE repo_key=?", (RK,)).fetchone()[0]
    assert cnt == 1


def test_sweeper_helper_calls_prune(monkeypatch):
    import src.runtime.cleanup as cleanup
    monkeypatch.setattr(bg, "prune_guard_state", lambda days=90: 7)
    assert cleanup._sweep_branch_guard_state() == 7


# ===================== R10: preflight 并发 =====================
def test_preflight_runs_two_queries_concurrently(monkeypatch):
    bg.save_lease_state(RK, "feat/pf", expected_remote_sha="sha_x")
    seen = []

    def slow_remote(rk, br, token=""):
        seen.append(("remote_start", time.perf_counter()))
        time.sleep(0.1)
        seen.append(("remote_end", time.perf_counter()))
        return "sha_x"

    def slow_pr(rk, br, token=""):
        seen.append(("pr_start", time.perf_counter()))
        time.sleep(0.2)
        seen.append(("pr_end", time.perf_counter()))
        return {"state": "open", "number": 1, "base": "weekly-mys"}

    monkeypatch.setattr(bg, "resolve_remote_head", slow_remote)
    monkeypatch.setattr(bg, "fetch_pr_state", slow_pr)

    t0 = time.perf_counter()
    verdict = bg.preflight_push(RK, "feat/pf", token="tk")
    elapsed = time.perf_counter() - t0

    assert verdict["allowed"] is True
    # 并发 ⇒ 总耗时 ≈ max(0.1, 0.2) 而不是 0.3
    assert elapsed < 0.28, elapsed
    starts = [t for name, t in seen if name.endswith("_start")]
    assert max(starts) - min(starts) < 0.1, "两次查询应几乎同时发起"


def test_preflight_fail_open_when_one_query_raises(monkeypatch):
    bg.save_lease_state(RK, "feat/pf2", expected_remote_sha="")

    def boom(rk, br, token=""):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(bg, "resolve_remote_head", boom)
    monkeypatch.setattr(bg, "fetch_pr_state", lambda rk, br, token="": {"state": "open", "number": 2})
    verdict = bg.preflight_push(RK, "feat/pf2", token="tk")
    assert verdict["allowed"] is True and verdict["remote_sha"] == "" and verdict["pr"]["number"] == 2


def test_preflight_still_blocks_merged_with_concurrency(monkeypatch):
    bg.save_lease_state(RK, "feat/pf3", expected_remote_sha="sha_x")
    monkeypatch.setattr(bg, "resolve_remote_head", lambda rk, br, token="": "sha_x")
    monkeypatch.setattr(bg, "fetch_pr_state",
                        lambda rk, br, token="": {"state": "merged", "number": 993, "base": "weekly-mys"})
    verdict = bg.preflight_push(RK, "feat/pf3", token="tk")
    assert verdict["allowed"] is False and verdict["reason"] == "pr_merged"