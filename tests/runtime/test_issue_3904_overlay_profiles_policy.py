"""Issue #3904: hermes ACP overlay 构建跳过清单与可用性自检必需清单同源。

历史缺陷：`_build_overlay_home` 把 ``profiles`` 列入跳过清单（overlay 不产出），
`_overlay_looks_usable` 却把 ``profiles`` 列为必需 —— 共享 HERMES_HOME 一旦出现
``profiles/``（Hermes 多 profile 目录），每条任务必然「构建不产出 → 自检不可用 →
整体回退共享 home」：任务串行（``_SHARED_HOME_LOCK``）、共享 state.db 无限膨胀、
``usage_collect`` 读不到 overlay state.db。

覆盖：
- 两张清单同源（无交集），新增条目任一侧都不会再现互斥
- 含 profiles 的共享 home → overlay 可用，不再回退
- 回退原因可诊断（`_overlay_unusable_reasons`）+ 回退计数
- fail-closed 不退化：缺运行时仍判不可用
- usage_collect 回退感知：overlay 缺失时读共享 HERMES_HOME 账本，缺失时无 WARNING
"""
import inspect
import logging
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine.agent import _backend_for_engine  # noqa: F401,E402  先入 runtime 打破循环导入
from src.domains.runtime.agents import hermes_acp  # noqa: E402
from src.domains.runtime.engine import usage_collect  # noqa: E402


def _mk_src_home(tmp_path, entries):
    """构造共享 HERMES_HOME：entries = {name: 子项数}（0 → 空目录，>0 → 放子项）。"""
    src = tmp_path / "src_home"
    src.mkdir()
    (src / "config.yaml").write_text("tool_progress: old\n", encoding="utf-8")
    (src / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    for name, children in entries.items():
        p = src / name
        p.mkdir(parents=True, exist_ok=True)
        for i in range(children):
            child = p / f"item{i}"
            if name == "tools":
                child.mkdir()
            else:
                child.write_text("x", encoding="utf-8")
    return str(src)


# ── 1. 同源清单漂移守卫 ───────────────────────────────────────────
def test_required_and_not_mirrored_are_disjoint():
    overlap = set(hermes_acp._OVERLAY_REQUIRED_ENTRIES) & set(hermes_acp._OVERLAY_NOT_MIRRORED)
    assert not overlap, (
        f"必需清单与构建跳过清单出现交集 {sorted(overlap)} —— "
        "这正是 #3904 的互斥缺陷：构建不产出、自检却必需 → 100% 回退共享 home")


# ── 2. 核心复现：共享 home 有 profiles 时 overlay 必须可用 ─────────────
@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink 语义在 Windows 需特权")
def test_profiles_present_in_src_builds_usable_overlay(tmp_path):
    src = _mk_src_home(tmp_path, {"tools": 1, "installs": 1, "skills": 1,
                                 "plugins": 1, "profiles": 2})
    (tmp_path / "src_home" / "auth.json").write_text("{}", encoding="utf-8")
    iso = str(tmp_path / "iso")
    os.makedirs(iso)

    hermes_acp._build_overlay_home(iso, src)

    profiles = os.path.join(iso, "profiles")
    assert os.path.exists(profiles), "profiles 必须进 overlay（跳过即互斥）"
    assert os.path.islink(profiles), "profiles 按只读共享资产 symlink"
    assert hermes_acp._overlay_unusable_reasons(iso, src) == []
    assert hermes_acp._overlay_looks_usable(iso, src) is True


def test_legacy_empty_profiles_dir_replaced_by_symlink(tmp_path):
    """存量 overlay 里若有真实空 profiles/（旧兜底产物），构建期必须换成 symlink。

    否则自检会判 `profiles:empty` → 仍然回退共享 home（换汤不换药的二次互斥）。
    """
    if sys.platform.startswith("win"):
        pytest.skip("symlink 语义在 Windows 需特权")
    src = _mk_src_home(tmp_path, {"profiles": 1})
    iso = str(tmp_path / "iso")
    os.makedirs(os.path.join(iso, "profiles"))       # 遗留真实空目录
    hermes_acp._build_overlay_home(iso, src)
    assert os.path.islink(os.path.join(iso, "profiles"))
    assert hermes_acp._overlay_looks_usable(iso, src) is True


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink 语义在 Windows 需特权")
def test_profiles_only_src_home_is_usable(tmp_path):
    """最小复现：只有 profiles 的共享 home（其余条目不存在）也必须可用。"""
    src = _mk_src_home(tmp_path, {"profiles": 1})
    iso = str(tmp_path / "iso")
    os.makedirs(iso)
    hermes_acp._build_overlay_home(iso, src)
    assert hermes_acp._overlay_looks_usable(iso, src) is True


# ── 3. 回退原因可诊断 ─────────────────────────────────────────────
def test_missing_required_entry_reports_reason(tmp_path):
    src = _mk_src_home(tmp_path, {"tools": 1, "profiles": 1})
    iso = str(tmp_path / "iso")
    os.makedirs(iso)
    os.makedirs(os.path.join(iso, "profiles"))       # 空目录（模拟构建漏产出）
    reasons = hermes_acp._overlay_unusable_reasons(iso, src)
    assert any(r.startswith("tools:") for r in reasons), reasons
    assert hermes_acp._overlay_looks_usable(iso, src) is False
    # 构建跳过清单内的条目永不出现在原因里（不同源即再互斥）
    for entry in hermes_acp._OVERLAY_NOT_MIRRORED:
        assert not any(r.startswith(f"{entry}:") for r in reasons), (entry, reasons)


# ── 4. fail-closed 不退化 ─────────────────────────────────────────
def test_runtime_and_install_missing_still_unusable(tmp_path):
    src = _mk_src_home(tmp_path, {"tools": 1, "installs": 1, "skills": 1})
    iso = str(tmp_path / "iso")
    os.makedirs(iso)
    os.makedirs(os.path.join(iso, "skills"))         # 空 → 引擎会重新 bootstrap
    assert hermes_acp._overlay_looks_usable(iso, src) is False
    reasons = hermes_acp._overlay_unusable_reasons(iso, src)
    assert {r.split(":", 1)[0] for r in reasons} == {"tools", "installs", "skills"}


# ── 5. 回退计数 + 结构化日志 ──────────────────────────────────────
def test_fallback_counter_increments():
    before = hermes_acp.overlay_fallback_total()
    assert hermes_acp._record_overlay_fallback() == before + 1
    assert hermes_acp.overlay_fallback_total() == before + 1


def test_stream_execute_logs_structured_fallback():
    """回退日志必须带 total/tier/reasons，且自检走同源函数（源码级守卫）。"""
    src = inspect.getsource(hermes_acp.HermesACPBackend.stream_execute)
    assert "ACP_OVERLAY_FALLBACK" in src
    assert "total=" in src and "tier=" in src and "reasons=" in src
    assert "_overlay_unusable_reasons(" in src
    assert "_record_overlay_fallback(" in src


# ── 6. usage_collect 回退感知 ────────────────────────────────────
def _mk_state_db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT,"
        " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
        " cache_write_tokens INTEGER, reasoning_tokens INTEGER);")
    conn.executemany(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_usage_collect_falls_back_to_shared_home_state_db(tmp_path, monkeypatch):
    shared = tmp_path / "shared-hermes"
    shared.mkdir()
    _mk_state_db(str(shared / "state.db"),
                 [("s1", "claude-x", "", 100, 20, 5, 0, 3)])
    monkeypatch.setenv("HERMES_HOME", str(shared))
    cwd = str(tmp_path / "workspace")               # overlay home 未建 → 不存在
    os.makedirs(cwd)

    # 未回退的 cwd 不读共享账本（否则会把共享库里别的历史累计当成本次用量）
    assert usage_collect._hermes_state_db_paths(cwd) == [
        os.path.join(usage_collect._hermes_iso_home(cwd), "state.db")]

    hermes_acp._mark_shared_home_fallback(cwd)      # 真正回退时由 stream_execute 打标
    paths = usage_collect._hermes_state_db_paths(cwd)
    assert len(paths) == 2 and paths[1] == str(shared / "state.db"), paths

    col = usage_collect.UsageCollector("hermes", cwd=cwd, session_id="s1")
    col._baseline = {"hermes": {}}
    recs = col.finish()
    assert len(recs) == 1 and recs[0]["input"] == 100, recs


def test_unmarked_cwd_does_not_read_shared_ledger(tmp_path, monkeypatch):
    """没有回退标记的 cwd：即便共享 home 有账本也不得计入（防重复计量）。"""
    shared = tmp_path / "shared-hermes"
    shared.mkdir()
    _mk_state_db(str(shared / "state.db"),
                 [("s1", "m", "", 999, 0, 0, 0, 0)])
    monkeypatch.setenv("HERMES_HOME", str(shared))
    cwd = str(tmp_path / "untouched-workspace")
    os.makedirs(cwd)
    col = usage_collect.UsageCollector("hermes", cwd=cwd, session_id="s1")
    col._baseline = {"hermes": {}}
    assert col.finish() == []


def test_usage_collect_missing_ledger_is_silent(tmp_path, monkeypatch, caplog):
    """overlay 被回退删除、共享 home 也没账本 → 不得逐任务 WARNING（#3904 噪音源）。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    cwd = str(tmp_path / "workspace")
    os.makedirs(cwd)
    col = usage_collect.UsageCollector("hermes", cwd=cwd, session_id="s1")
    with caplog.at_level(logging.WARNING, logger=usage_collect.log.name):
        col._baseline = {"hermes": {}}
        assert col.finish() == []
    noise = [r for r in caplog.records
             if r.levelno >= logging.WARNING and "sqlite" in r.getMessage()]
    assert not noise, [r.getMessage() for r in noise]