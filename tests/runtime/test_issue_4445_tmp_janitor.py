"""Issue #4445：/tmp 任务派生残留 janitor 行为测试。

覆盖验收：
1. 无标记、超 TTL 的 qa*/pr*/verify*/base*/rev*/deploy-ws/venv*/pkg-*/*_head 被清；
2. 活跃 task_id 目录、当前 TASK_TMPDIR、符号链接、白名单外第三方文件不被删；
3. dry-run 只统计不删除；
4. 累计清理计数可从 tmp_janitor_metrics_snapshot() 读取。
"""
import os

import pytest

from src.domains.runtime.engine import cleanup


@pytest.fixture(autouse=True)
def _janitor_env(monkeypatch):
    monkeypatch.setattr(cleanup, "_TMP_JANITOR_MODE", "on")
    monkeypatch.setattr(cleanup, "_TMP_JANITOR_TTL_SECONDS", 7200)
    monkeypatch.setattr(
        cleanup, "_TMP_JANITOR_PATTERNS",
        ("qa*", "pr*", "verify*", "base*", "rev*", "deploy-ws", "venv*",
         "pkg-*", "head*", "wt-*"))
    monkeypatch.setattr(cleanup, "_active_session_task_ids", lambda: {"t_active"})
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
        "qa-1839", "pr2031-head", "verify4356", "base4350", "rev3582",
        "deploy-ws", "venv4241", "pkg-202611242342", "head4350", "wt-abc")]
    active = _mk(tmp_path / "qa-active")
    os.rename(active, tmp_path / "t_active")
    fresh = _mk(tmp_path / "qa-fresh", age=False)
    third_party = _mk(tmp_path / "notes-backup")
    link_target = _mk(tmp_path / "real-target")
    link = tmp_path / "qa-link"
    os.symlink(link_target, link)

    removed = cleanup._sweep_tmp_task_residuals(now=1_700_000_000, roots=[str(tmp_path)])

    assert removed == len(stale)
    for p in stale:
        assert not os.path.exists(p), f"超 TTL 任务派生残留未清理: {p}"
    assert os.path.isdir(fresh), "TTL 内对象被误删"
    assert os.path.isdir(third_party), "白名单外第三方文件被误删"
    assert os.path.isdir(tmp_path / "t_active"), "活跃 task_id 目录被误删"
    assert os.path.islink(link), "符号链接被删除"
    assert os.path.isdir(link_target), "符号链接目标被跟随删除"


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