"""硬化补充场景 S14–S15：测试隔离纪律。

旧实现：DDL 场景手工把 _cleanup_gate_passed 恢复成另一个函数的原值（hasattr 恒真），
同进程后续 table_ownership s10 读到假 gate 变红。
口径：测试一律用 monkeypatch 恢复；同进程多文件必须互不影响。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_s14_ddl_and_ownership_scenarios_share_a_process():
    """回归：DDL 场景污染 _cleanup_gate_passed 时，本用例会红。"""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/api/test_destructive_ddl_stamp_scenarios.py",
         "tests/kernel/test_table_ownership_copy_audit.py"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def test_s15_ddl_scenarios_restore_globals_through_the_fixture():
    src = (ROOT / "tests/api/test_destructive_ddl_stamp_scenarios.py").read_text(encoding="utf-8")
    assert "monkeypatch.setattr(d, \"_cleanup_gate_passed\"" in src
    assert "d._cleanup_gate_passed = " not in src, "不得手工恢复模块全局（会污染同进程其它用例）"
