"""Issue #4158（PR #2077 合入后 DevOps 复核）：`scripts/check_f811.py` 缺工具假绿回归。

病灶：`python -m ruff` 缺模块时 `rc=1`、`stdout` 为空、`stderr=No module named ruff`；
旧实现对 `rc in (0, 1)` 放行，再用 `json.loads(proc.stdout or "[]")` 把空输出解析成
「0 处命中」，打印 `当前 0 / 基线 72` 并 `exit 0`——本地 `make lint` 因此可被绕过。
本套用例锁住三条口径：

1. 缺 ruff → 显式 FAIL（rc=1 + 可执行修复指引），不得伪装成 0 处命中；
2. `rc in (0, 1)` 但 stdout 为空 / 非 JSON / 非数组 → 显式 FAIL；
3. ruff 正常时棘轮判定不变（未超基线 rc=0，超出 rc=1 并指出新增项）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SCRIPT = os.path.join(_REPO, "scripts", "check_f811.py")

_spec = importlib.util.spec_from_file_location("check_f811_gate", _SCRIPT)
assert _spec is not None and _spec.loader is not None
f811 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f811)


def _fake_run(*, rc: int, stdout: str = "", stderr: str = ""):
    """替身 subprocess.run：只回放 ruff 的 (rc, stdout, stderr) 组合。"""

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    return _run


def _hit(file: str, line: int, message: str = "Redefinition of unused `x`") -> dict:
    return {"file": file, "line": line, "code": "F811", "message": message}


class TestRuffAvailability:
    def test_availability_matches_interpreter_probe(self):
        """判定必须与 `sys.executable -m ruff` 的调用解释器同源（同一 find_spec）。"""
        assert f811._ruff_available() == (importlib.util.find_spec("ruff") is not None)

    def test_missing_ruff_fails_loud(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: False)
        with pytest.raises(RuntimeError, match="ruff 不可用"):
            f811.collect("src")

    def test_missing_ruff_failure_keeps_repair_hint(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: False)
        with pytest.raises(RuntimeError, match=r"pip install -e '\.\[dev\]'"):
            f811.collect("src")


class TestCollectHardening:
    def test_rc1_empty_stdout_is_not_zero_hits(self, monkeypatch):
        """事故原形：缺模块 → rc=1 + 空 stdout，旧实现解析成 0 处命中并 exit 0。"""
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        monkeypatch.setattr(
            f811.subprocess, "run",
            _fake_run(rc=1, stdout="", stderr="No module named ruff"),
        )
        with pytest.raises(RuntimeError, match="无 JSON 输出"):
            f811.collect("src")

    def test_rc1_empty_stdout_error_quotes_stderr(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        monkeypatch.setattr(
            f811.subprocess, "run",
            _fake_run(rc=1, stdout="  \n", stderr="No module named ruff"),
        )
        with pytest.raises(RuntimeError, match="No module named ruff"):
            f811.collect("src")

    def test_rc0_empty_stdout_fails(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        monkeypatch.setattr(f811.subprocess, "run", _fake_run(rc=0, stdout=""))
        with pytest.raises(RuntimeError, match="无 JSON 输出"):
            f811.collect("src")

    def test_unexpected_returncode_fails(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        monkeypatch.setattr(
            f811.subprocess, "run",
            _fake_run(rc=2, stdout="", stderr="error: unrecognized arguments"),
        )
        with pytest.raises(RuntimeError, match="rc=2"):
            f811.collect("src")

    def test_non_json_stdout_fails(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        monkeypatch.setattr(
            f811.subprocess, "run",
            _fake_run(rc=1, stdout="error: invalid value for '--select'"),
        )
        with pytest.raises(RuntimeError, match="不是 JSON"):
            f811.collect("src")

    def test_non_array_json_fails(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        monkeypatch.setattr(
            f811.subprocess, "run",
            _fake_run(rc=0, stdout=json.dumps({"hits": []})),
        )
        with pytest.raises(RuntimeError, match="JSON 数组"):
            f811.collect("src")

    def test_zero_hits_parsed_as_empty_list(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        monkeypatch.setattr(f811.subprocess, "run", _fake_run(rc=0, stdout="[]"))
        assert f811.collect("src") == []

    def test_flat_location_rows_parsed(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        payload = [{
            "filename": os.path.join(_REPO, "src", "api", "main.py"),
            "code": "F811",
            "message": "Redefinition of unused `x` from line 1",
            "location": {"row": 758, "column": 1},
        }]
        monkeypatch.setattr(f811.subprocess, "run",
                            _fake_run(rc=1, stdout=json.dumps(payload)))
        hits = f811.collect("src")
        assert hits == [_hit("src/api/main.py", 758,
                             "Redefinition of unused `x` from line 1")]

    def test_nested_diagnostics_rows_parsed(self, monkeypatch):
        monkeypatch.setattr(f811, "_ruff_available", lambda: True)
        payload = [{
            "filename": os.path.join(_REPO, "src", "api", "main.py"),
            "diagnostics": [{
                "code": "F811",
                "message": "Redefinition of unused `y`",
                "location": {"row": 42, "column": 5},
            }],
        }]
        monkeypatch.setattr(f811.subprocess, "run",
                            _fake_run(rc=1, stdout=json.dumps(payload)))
        assert f811.collect("src") == [_hit("src/api/main.py", 42,
                                            "Redefinition of unused `y`")]


class TestMainVerdict:
    def _run_main(self, monkeypatch, capsys, *, hits, baseline):
        monkeypatch.setattr(sys, "argv", ["check_f811.py", "src"])
        monkeypatch.setattr(f811, "collect", lambda target: hits)
        monkeypatch.setattr(f811, "load_baseline", lambda: baseline)
        rc = f811.main()
        return rc, capsys.readouterr()

    def test_missing_ruff_exits_nonzero(self, monkeypatch, capsys):
        def _boom(target):
            raise RuntimeError("ruff 不可用（当前解释器未安装）→ 门禁显式失败，不跳过")

        monkeypatch.setattr(sys, "argv", ["check_f811.py", "src"])
        monkeypatch.setattr(f811, "collect", _boom)
        rc = f811.main()
        err = capsys.readouterr().err
        assert rc == 1
        assert "ruff 不可用" in err

    def test_not_above_baseline_passes(self, monkeypatch, capsys):
        rc, out = self._run_main(monkeypatch, capsys, hits=[],
                                 baseline={"total": 0, "items": []})
        assert rc == 0
        assert "OK: F811 未新增" in out.out

    def test_decrease_suggests_baseline_update(self, monkeypatch, capsys):
        rc, out = self._run_main(monkeypatch, capsys, hits=[],
                                 baseline={"total": 2, "items": ["src/a.py:1", "src/a.py:9"]})
        assert rc == 0
        assert "可 --update-baseline 收紧" in out.out

    def test_new_hit_fails_and_names_it(self, monkeypatch, capsys):
        rc, out = self._run_main(
            monkeypatch, capsys,
            hits=[_hit("src/api/main.py", 758)],
            baseline={"total": 0, "items": []},
        )
        assert rc == 1
        assert "src/api/main.py:758" in out.err


class TestRealInvocation:
    def test_empty_target_dir_gate_verdict_matches_tool_state(self, tmp_path):
        """真实子进程跑一遍：有 ruff → rc=0；缺 ruff → rc=1 且提示不可用（不得假绿）。"""
        proc = subprocess.run(
            [sys.executable, _SCRIPT, str(tmp_path)],
            cwd=_REPO, capture_output=True, text=True, timeout=300,
        )
        if importlib.util.find_spec("ruff") is None:
            assert proc.returncode == 1, proc.stdout
            assert "ruff 不可用" in proc.stderr
        else:
            assert proc.returncode == 0, proc.stderr or proc.stdout
            assert "OK: F811 未新增" in proc.stdout