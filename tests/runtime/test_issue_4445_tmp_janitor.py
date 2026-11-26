"""Issue #4445：/tmp 任务派生残留 janitor 行为测试。

覆盖验收：
1. 无标记、超 TTL 的 qa*/pr*/verify*/base*/rev*/deploy-ws/venv*/pkg-*/*head*/wt-* 残留被清；
2. 活跃任务目录（含真实形态 `qa-<task_id>` / `pr<task_id>-head`）、当前 TASK_TMPDIR、
   符号链接、白名单外第三方文件不被删；
3. 活跃任务探测失败 → 本轮整体跳过（fail-closed）；
4. dry-run 只统计不删除，且**不累加**累计指标；
5. 累计清理计数可从 tmp_janitor_metrics_snapshot() 读取；
6. env 误配（空串/非数字/未知模式）不崩、保守回退。
"""
import os

import pytest

from src.domains.runtime.engine import cleanup

_ACTIVE_ID = "1839"
_PATTERNS = (
    "qa[0-9]*", "qa-*", "pr[0-9]*", "pr-*", "verify[0-9]*", "verify-*",
    "base[0-9]*", "rev[0-9]*", "head[0-9]*", "deploy-ws", "venv[0-9]*",
    "venv-*", "pkg-*", "wt-*",
)


@pytest.fixture(autouse=True)
def _janitor_env(monkeypatch):
    monkeypatch.setattr(cleanup, "_TMP_JANITOR_MODE", "on")
    monkeypatch.setattr(cleanup, "_TMP_JANITOR_TTL_SECONDS", 7200)
    monkeypatch.setattr(cleanup, "_TMP_JANITOR_PATTERNS", _PATTERNS)
    monkeypatch.setattr(cleanup, "_query_active_task_ids",
                        lambda: (True, {_ACTIVE_ID}))
    monkeypatch.setattr(cleanup, "_tmp_janitor_protected_paths", lambda: set())
    monkeypatch.setattr(cleanup, "_tmp_janitor_items_total", 0)
    monkeypatch.setattr(cleanup, "_tmp_janitor_bytes_total", 0)
    yield


def _age(path, seconds=10800):
    old = 1_600_000_000
    os.utime(path, (old, old))
    return old


def _mk(path, content=b"x" * 16, age=True):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "payload.bin"), "wb") as f:
        f.write(content)
    if age:
        _age(path)
    return path


def test_stale_task_residuals_removed_and_safe_paths_kept(tmp_path):
    stale = [_mk(tmp_path / n) for n in (
        "qa-9999", "pr2031-head", "verify4356", "base4350", "rev3582",
        "deploy-ws", "venv4241", "pkg-202611242342", "head4350", "wt-abc")]
    # QA Required：真实残留目录名是 `qa-<task_id>` / `pr<task_id>-head` 形态，目录名
    # 并不等于 task_id —— 这三类都必须被活跃任务归属识别豁免。
    active = [_mk(tmp_path / n) for n in (
        "qa-1839", "pr1839-head", "qa-active-1839")]
    fresh = _mk(tmp_path / "qa-fresh", age=False)
    third_party = _mk(tmp_path / "notes-backup")
    # QA Optional：收敛前缀后，baseline/review/headless 等第三方目录不再命中白名单。
    narrower_third_party = [_mk(tmp_path / n) for n in (
        "baseline-notes", "review-notes", "headless-chrome")]
    link_target = _mk(tmp_path / "real-target")
    link = tmp_path / "qa-link"
    os.symlink(link_target, link)

    removed = cleanup._sweep_tmp_task_residuals(now=1_700_000_000, roots=[str(tmp_path)])

    assert removed == len(stale)
    for p in stale:
        assert not os.path.exists(p), f"超 TTL 任务派生残留未清理: {p}"
    for p in active:
        assert os.path.isdir(p), f"活跃任务目录被误删: {p}"
    assert os.path.isdir(fresh), "TTL 内对象被误删"
    assert os.path.isdir(third_party), "白名单外第三方文件被误删"
    for p in narrower_third_party:
        assert os.path.isdir(p), f"收敛前缀后仍误删第三方目录: {p}"
    assert os.path.islink(link), "符号链接被删除"
    assert os.path.isdir(link_target), "符号链接目标被跟随删除"


def test_active_lookup_failure_skips_round_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "_query_active_task_ids", lambda: (False, set()))
    p = _mk(tmp_path / "qa-1839")
    assert cleanup._sweep_tmp_task_residuals(now=1_700_000_000, roots=[str(tmp_path)]) == 0
    assert os.path.isdir(p), "活跃任务探测失败时必须 fail-closed（本轮不删）"
    snap = cleanup.tmp_janitor_metrics_snapshot()
    assert snap["tmp_janitor_removed_items_total"] == 0


def test_protected_task_tmpdir_skipped(tmp_path, monkeypatch):
    task_tmp = _mk(tmp_path / "qa-protected")
    monkeypatch.setattr(cleanup, "_tmp_janitor_protected_paths", lambda: {str(task_tmp)})
    removed = cleanup._sweep_tmp_task_residuals(now=1_700_000_000, roots=[str(tmp_path)])
    assert removed == 0
    assert os.path.isdir(task_tmp), "当前 TASK_TMPDIR 被误删"


def test_dry_run_reports_without_deleting(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "_TMP_JANITOR_MODE", "dry")
    p = _mk(tmp_path / "qa-dry")
    removed = cleanup._sweep_tmp_task_residuals(now=1_700_000_000, roots=[str(tmp_path)])
    assert removed == 1
    assert os.path.isdir(p), "dry-run 不应删除任何对象"


def test_dry_run_does_not_accumulate_metrics(tmp_path, monkeypatch):
    """QA Required：dry-run 只报告不删，累计清理量必须保持不变（否则看板/告警假阳性）。"""
    monkeypatch.setattr(cleanup, "_TMP_JANITOR_MODE", "dry")
    _mk(tmp_path / "qa-dry-a", content=b"y" * 64)
    _mk(tmp_path / "qa-dry-b", content=b"z" * 32)
    before = cleanup.tmp_janitor_metrics_snapshot()
    removed = cleanup._sweep_tmp_task_residuals(now=1_700_000_000, roots=[str(tmp_path)])
    after = cleanup.tmp_janitor_metrics_snapshot()
    assert removed == 2
    assert after == before, "dry-run 不得累加 tmp_janitor_*_total"


def test_metrics_snapshot_accumulates(tmp_path):
    _mk(tmp_path / "qa-a", content=b"y" * 32)
    _mk(tmp_path / "qa-b", content=b"z" * 64)
    cleanup._sweep_tmp_task_residuals(now=1_700_000_000, roots=[str(tmp_path)])
    snap = cleanup.tmp_janitor_metrics_snapshot()
    assert snap["tmp_janitor_removed_items_total"] == 2
    assert snap["tmp_janitor_removed_bytes_total"] > 0


def test_off_disables_janitor(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "_TMP_JANITOR_MODE", "off")
    p = _mk(tmp_path / "qa-off")
    assert cleanup._sweep_tmp_task_residuals(now=1_700_000_000, roots=[str(tmp_path)]) == 0
    assert os.path.isdir(p)


def test_local_tmp_sweep_runs_without_cluster_lock(tmp_path, monkeypatch):
    """QA Optional：未拿到集群锁的节点也要能跑本机 /tmp 清理。"""
    p = _mk(tmp_path / "qa-local")
    monkeypatch.setattr(cleanup, "_opencode_staging_roots", lambda: set())
    cleaned = cleanup._sweep_local_tmp_once(now=1_700_000_000, roots=[str(tmp_path)])
    assert cleaned >= 1
    assert not os.path.exists(p)


def test_env_parse_tolerates_bad_values(monkeypatch):
    """QA Optional：导入期 int() 崩溃 → 失败回落默认值；未知模式保守落 dry。"""
    monkeypatch.setenv("GRAPE_TEST_BAD_INT", "not-a-number")
    assert cleanup._env_int("GRAPE_TEST_BAD_INT", 7200) == 7200
    monkeypatch.setenv("GRAPE_TEST_BAD_INT", "")
    assert cleanup._env_int("GRAPE_TEST_BAD_INT", 7200) == 7200
    monkeypatch.setenv("GRAPE_TEST_BAD_INT", "-5")
    assert cleanup._env_int("GRAPE_TEST_BAD_INT", 7200) == 7200
    monkeypatch.setenv("GRAPE_TEST_BAD_INT", "3600")
    assert cleanup._env_int("GRAPE_TEST_BAD_INT", 7200) == 3600

    monkeypatch.setenv("SWEEPER_TMP_JANITOR", " Off ")
    assert cleanup._tmp_janitor_mode() == "off"
    monkeypatch.setenv("SWEEPER_TMP_JANITOR", "ture")
    assert cleanup._tmp_janitor_mode() == "dry", "未知拼写必须保守落 dry（不删）"


def test_default_patterns_reject_loose_prefixes():
    """QA Optional：默认白名单不得再命中 baseline/review/headless 这类第三方目录。"""
    patterns = cleanup._TMP_JANITOR_DEFAULT_PATTERNS.split(",")
    for name in ("baseline-notes", "review-notes", "headless-chrome", "browser-cache"):
        assert not any(cleanup.fnmatch.fnmatchcase(name, p) for p in patterns), name
    for name in ("qa-1839", "pr2031-head", "verify4356", "base4350",
                 "rev3582", "head4350", "deploy-ws", "venv4241", "wt-abc"):
        assert any(cleanup.fnmatch.fnmatchcase(name, p) for p in patterns), name


def test_empty_active_set_degrades_ttl_to_six_hours(tmp_path, monkeypatch):
    """OPS-1（PR #2245 复审）：活跃集为空时不能当「没有活跃任务」。

    worker 侧本进程库可能「表在但无 controller 侧任务行」，此时 2h TTL 会把仍在运行、
    但久未写盘的 qa*/pr* 目录当残留删掉。空集时 TTL 抬到 6h 下限：
    · 3h 前 → 保留（原 TTL 2h 会删，这是回归点）
    · 7h 前 → 删除
    """
    import time

    monkeypatch.setattr(cleanup, "_query_active_task_ids", lambda: (True, set()))
    now = time.time()
    recent = tmp_path / "qa-9-recent"
    old = tmp_path / "qa-9-old"
    recent.mkdir(); old.mkdir()
    os.utime(recent, (now - 3 * 3600, now - 3 * 3600))
    os.utime(old, (now - 7 * 3600, now - 7 * 3600))

    removed = cleanup._sweep_tmp_task_residuals(now=now, roots=[str(tmp_path)])

    assert recent.is_dir(), "3h 的目录在降级 TTL(6h) 内，不得删除"
    assert not old.exists(), "7h 的目录已超降级 TTL，应被清除"
    assert removed == 1, f"只应删掉超 6h 的那一个，实际 {removed}"
