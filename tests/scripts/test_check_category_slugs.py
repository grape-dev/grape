"""Issue #3796：scripts/check_category_slugs.py 门禁单测（CI/本地校验 seam）。

覆盖：
- scenes/ 目录缺失 → 打印降级说明、退出 0（部署根 scenes/ 由 grape-skills 下发）；
- 含「字典无此 slug 且无别名」的 scene.json → 退出 1 且明细含 slug 与场景名；
- 仅携带可别名解析的旧 slug（skill-dev 等）→ 退出 0（过渡期合法）；
- --dir 指定非默认 scenes 目录。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_category_slugs.py"

spec = importlib.util.spec_from_file_location("check_category_slugs", _SCRIPT)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
sys.modules["check_category_slugs"] = gate
spec.loader.exec_module(gate)


def _write_scene(scenes_dir: str, name: str, category: str) -> None:
    d = os.path.join(scenes_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "scene.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "category": category}, f, ensure_ascii=False)


def test_missing_scenes_dir_warns_on_stderr(tmp_path, capsys):
    """缺 scenes/ → 输出 WARNING（不静默通过），仍以 0 退出（本仓基线无该目录）。"""
    rc = gate.main(["--dir", str(tmp_path / "nope")])
    cap = capsys.readouterr()
    assert rc == 0
    assert "WARNING" in cap.err
    assert "not found" in cap.err.lower()


def test_missing_scenes_dir_allow_missing_is_explicit(tmp_path, capsys):
    """显式 --allow-missing：缺目录时降级为 INFO（CI/本地已在别处校验时使用）。"""
    rc = gate.main(["--dir", str(tmp_path / "nope"), "--allow-missing"])
    cap = capsys.readouterr()
    assert rc == 0
    assert "WARNING" not in cap.err
    assert "--allow-missing" in cap.out


def test_unknown_slug_fails_nonzero_with_detail(tmp_path, capsys):
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    _write_scene(scenes, "legacy-bad", "no-such-slug")
    _write_scene(scenes, "legacy-ok", "skill-dev")
    rc = gate.main(["--dir", scenes])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no-such-slug" in out
    assert "legacy-bad" in out
    assert "legacy-ok" not in out


def test_alias_only_legacy_slugs_pass(tmp_path, capsys):
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    for name, slug in (("a", "skill-dev"), ("b", "iac"), ("c", "general"),
                       ("d", "content"), ("e", "ai"), ("f", "swarm-dev"),
                       ("g", "it-rd-grape-platform")):
        _write_scene(scenes, name, slug)
    assert gate.main(["--dir", scenes]) == 0


def test_default_dir_is_repo_root_scenes(monkeypatch, tmp_path, capsys):
    """--dir 缺省用仓库根 scenes/（本仓基线无该目录 → 降级退出 0）。"""
    rc = gate.main([])
    assert rc == 0  # 本仓基线无 scenes/，降级跳过


def test_cli_exit_code_via_subprocess(tmp_path):
    """门禁实际以子进程运行：退出码必须可被 CI 感知（未知 slug 非 0）。"""
    import subprocess
    scenes = str(tmp_path / "scenes")
    os.makedirs(scenes)
    _write_scene(scenes, "bad", "no-such-slug")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--dir", scenes],
        capture_output=True, text=True, timeout=120, cwd=str(_REPO))
    assert proc.returncode == 1
    assert "no-such-slug" in (proc.stdout + proc.stderr)
