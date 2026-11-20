"""worker 侧「本地库 schema 漂移」与探活诊断的回归测试。

背景（2026-11-20 Windows worker 实测）：
- worker 的 SQLite 是本地库，表/列比代码落后是常态；agent 不读主控表。
  旧代码把 `no such table` / `no such column` 记成 WARNING，sweeper 每 861s
  刷 5 条、任务级刷 2 条，日志被淹没。
- aishell-dsh 的 `acp --help` 因本机安装坏了要跑 65s 才 exit=1，失败原因
  （ERR_PNPM_EPERM）全被折叠成 `exit=1`，且每 10 分钟重跑一遍。
- hermes ACP 把 provider 404 包成 {"message":"Internal error",
  "data":{"details":"admitted_turn_failed"}}，平台只看到 Internal error。
"""
import os
import sys

import pytest


# ── 1. schema 漂移分类 ────────────────────────────────────────────
@pytest.mark.parametrize("msg,expected", [
    ("no such table: branch_guard_state", True),
    ("no such column: kind", True),
    ("(1146, \"Table 'x.y' doesn't exist\")", True),
    ("(1054, \"Unknown column 'kind' in 'field list'\")", True),
    ("(1051, \"Unknown table 'x'\")", True),
    ("database is locked", False),
    ("Permission denied", False),
])
def test_missing_schema_classification(msg, expected):
    from src.kernel.db_errors import missing_schema
    assert missing_schema(RuntimeError(msg)) is expected


def test_missing_schema_reads_numeric_errno():
    from src.kernel.db_errors import missing_schema

    class _Err(Exception):
        def __init__(self):
            super().__init__(1146, "x")

    assert missing_schema(_Err()) is True


# ── 2. sweeper 缺表降级为 debug（不再刷 WARNING）────────────────────
def test_branch_guard_prune_missing_table_is_debug(caplog, monkeypatch):
    import logging
    from src.domains.runtime.engine import branch_guard

    class _Cur:
        def execute(self, *a, **k):
            raise RuntimeError("no such table: branch_guard_state")

        rowcount = 0

    class _DB:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _DB(), raising=False)
    monkeypatch.setattr(sm, "db_lock", __import__("threading").RLock(), raising=False)

    with caplog.at_level(logging.DEBUG):
        n = branch_guard.prune_guard_state(older_than_days=30)
    assert n == 0
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, [w.message for w in warnings]
    assert any("no local table" in r.message for r in caplog.records)


# ── 3. aishell：失败原因带输出尾部 + 负面结果长缓存 ─────────────────
def _fake_cli(tmp_path, body: str):
    p = tmp_path / "aishell_fake.sh"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


def test_aishell_probe_reports_output_tail_and_caches_negative(tmp_path, caplog):
    import logging
    from src.domains.runtime.agents import aishell_acp

    cli = _fake_cli(tmp_path, (
        'echo "preparing..." >&2\n'
        'echo "[ERR_PNPM_EPERM] rename failed" >&2\n'
        'echo "aishell: preinstall guard failed" >&2\n'
        'exit 1\n'))
    aishell_acp.aishell_dsh_capable._cache.clear()
    aishell_acp.aishell_dsh_capable._warned.clear()

    with caplog.at_level(logging.WARNING):
        assert aishell_acp.aishell_dsh_capable(cli) is False
    logged = " ".join(r.message for r in caplog.records)
    assert "ERR_PNPM_EPERM" in logged, "失败原因必须带上 CLI 自己的输出尾部"

    ts, capable, ttl = aishell_acp.aishell_dsh_capable._cache[cli]
    assert capable is False
    assert ttl >= 3600, "负面结论应长缓存（默认 6h），别每 10 分钟重跑 65s 的 preinstall"

    # 第二次命中缓存，不再拉起子进程（cli 已被删除也能返回）
    os.remove(cli)
    assert aishell_acp.aishell_dsh_capable(cli) is False


# ── 4. hermes ACP 错误上报带 data.details ──────────────────────────
def test_acp_error_detail_includes_data_details():
    from src.domains.runtime.engine.agent import _acp_error_detail

    resp = {"error": {"code": -32603, "message": "Internal error",
                      "data": {"details": "admitted_turn_failed"}}}
    detail = _acp_error_detail(resp)
    assert "Internal error" in detail
    assert "admitted_turn_failed" in detail, "平台侧需要 details 才能定位"


# ── 5. overlay 可用性自检 / junction / 安全删除 ────────────────────
def test_overlay_looks_usable_detects_missing_runtime_dir(tmp_path):
    from src.domains.runtime.agents.hermes_acp import _overlay_looks_usable

    src = tmp_path / "src"
    iso = tmp_path / "iso"
    for root in (src, iso):
        root.mkdir()
    (src / "tools").mkdir()
    (src / "tools" / "python").mkdir()
    (src / "skills").mkdir()
    (src / "skills" / "a.md").write_text("x", encoding="utf-8")
    (iso / "skills").mkdir()
    (iso / "skills" / "a.md").write_text("x", encoding="utf-8")
    # iso 缺 tools → 不可用（引擎会重新 bootstrap）
    assert _overlay_looks_usable(str(iso), str(src)) is False
    (iso / "tools").mkdir()
    (iso / "tools" / "python").mkdir()
    assert _overlay_looks_usable(str(iso), str(src)) is True


def test_junction_dir_is_noop_off_windows(tmp_path):
    from src.domains.runtime.agents.hermes_acp import _junction_dir

    src = tmp_path / "s"
    src.mkdir()
    if os.name != "nt":
        assert _junction_dir(str(src), str(tmp_path / "l")) is False
        assert not (tmp_path / "l").exists()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink 语义在 Windows 需特权")
def test_remove_overlay_tree_does_not_follow_links(tmp_path):
    from src.domains.runtime.agents.hermes_acp import _remove_overlay_tree

    src = tmp_path / "shared"
    src.mkdir()
    keep = src / "precious.txt"
    keep.write_text("do not delete", encoding="utf-8")

    iso = tmp_path / "iso"
    iso.mkdir()
    (iso / "linked").symlink_to(src, target_is_directory=True)
    (iso / "mine.txt").write_text("x", encoding="utf-8")

    _remove_overlay_tree(str(iso))
    assert not iso.exists()
    assert keep.exists(), "删除 overlay 不能穿透链接删掉共享 home 的内容"


# ── 6. overlay 在所有平台都会尝试（Windows 走 junction 梯次）──────
def test_overlay_supported_is_true_everywhere():
    from src.domains.runtime.agents.hermes_acp import _overlay_supported
    assert _overlay_supported() is True