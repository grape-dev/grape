"""find_cli Linux 发现兜底单测（aishell-dsh 一期改动 #9）。

背景（2026-11-06 远端 Worker E2E 实证）：pip --user 安装的 CLI 落在 ~/.local/bin，
systemd 服务进程 PATH 不含该目录 → worker 发现链扫不到。本测锁死：
- engine_search_paths 在 Linux 返回 ~/.local/bin/<cli> 通用候选
- PATH 查不到时 find_cli 经兜底命中该文件
- 空引擎名不产生候选
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.runtime.os_adapter import engine_search_paths, find_cli


def test_linux_search_path_candidate(monkeypatch):
    if sys.platform == "win32":
        return
    home = "/tmp/fakehome-findcli"
    monkeypatch.setenv("HOME", home)
    paths = engine_search_paths("aishell-dsh")
    assert paths == [os.path.join(home, ".local", "bin", "aishell-dsh")]
    assert engine_search_paths("") == []


def test_find_cli_fallback_hits_local_bin(monkeypatch, tmp_path):
    if sys.platform == "win32":
        return
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    cli = bindir / "some-engine-cli"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    # 名字唯一，PATH 上不可能存在 → which 落空，走 ~/.local/bin 兜底
    assert find_cli("some-engine-cli") == str(cli)


def test_find_cli_untouched_name_still_empty(monkeypatch):
    if sys.platform == "win32":
        return
    monkeypatch.setenv("HOME", "/tmp/definitely-no-such-home-findcli")
    assert find_cli("no-such-engine-xyz") == ""
