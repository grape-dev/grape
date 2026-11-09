# -*- coding: utf-8 -*-
"""任务 overlay home 不得镜像/保留 hermes 网关控制文件（生产 RCA 回归测试）。

背景（内部 worker 事故，2026-10-23）：
_build_overlay_home 会把共享 HERMES_HOME 的内容 symlink 进 per-task 隔离 home；
若把 gateway.lock / gateway.sock / gateway.pid / gateway_state.json /
gateway-starts.log 也镜像进去，hermes 的网关归属判定 missing_owner_state(home)
（lstat，不跟随符号链接）会判 unsafe_control_path → inaccessible → ACP
session/new 被拒（-32603）→ 新建工作区任务 100% 失败。

本测试覆盖两点：
1) 共享 home 里的网关控制文件不得被镜像进隔离 home；
2) 修复部署前遗留的镜像符号链接必须被清理（老 home 也要能恢复）。
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

# src/agent/__init__.py 会连带导入 runtime 层（与 test 收集期存在循环导入），
# 这里用轻量包占位 + 按文件路径加载，只取 hermes_acp 模块本身。
_pkg = types.ModuleType("src.agent")
_pkg.__path__ = [os.path.join(ROOT, "src", "agent")]
sys.modules.setdefault("src.agent", _pkg)
_spec = importlib.util.spec_from_file_location(
    "src.agent.hermes_acp", os.path.join(ROOT, "src", "agent", "hermes_acp.py"))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["src.agent.hermes_acp"] = _mod
_spec.loader.exec_module(_mod)
_build_overlay_home = _mod._build_overlay_home

GW_ENTRIES = ("gateway.sock", "gateway.sock.path", "gateway.lock",
              "gateway.pid", "gateway_state.json", "gateway-starts.log")


def _mk_shared_home(root):
    src = os.path.join(root, "shared_home")
    os.makedirs(os.path.join(src, "skills"), exist_ok=True)
    # 网关控制文件（真实文件 + 符号链接两种形态）
    with open(os.path.join(src, "gateway.lock"), "w", encoding="utf-8") as f:
        f.write("lock")
    os.symlink(os.path.join(src, "gateway.lock"), os.path.join(src, "gateway.sock"))
    for name in ("gateway.pid", "gateway_state.json", "gateway-starts.log"):
        with open(os.path.join(src, name), "w", encoding="utf-8") as f:
            f.write("x")
    # 一个普通内容（用于确认镜像功能本身没被破坏）
    with open(os.path.join(src, "skills", "a.md"), "w", encoding="utf-8") as f:
        f.write("skill")
    return src


def test_gateway_control_files_not_mirrored(tmp_path):
    src = _mk_shared_home(str(tmp_path))
    iso = os.path.join(str(tmp_path), "iso_home")
    os.makedirs(iso, exist_ok=True)

    _build_overlay_home(iso, src)

    for name in GW_ENTRIES:
        assert not os.path.lexists(os.path.join(iso, name)), (
            f"{name} 不应被镜像进任务隔离 home（会导致 hermes ACP session/new 被拒 -32603）")


def test_legacy_gateway_links_cleaned(tmp_path):
    """修复部署前建成的 home 里已镜像了控制文件符号链接 → 必须被清理。"""
    import shutil

    src = _mk_shared_home(str(tmp_path))
    iso = os.path.join(str(tmp_path), "iso_home")
    os.makedirs(iso, exist_ok=True)
    # 模拟历史遗留：老 home 里已有镜像出来的符号链接
    for name in ("gateway.lock", "gateway.pid", "gateway_state.json"):
        os.symlink(os.path.join(src, name), os.path.join(iso, name))

    _build_overlay_home(iso, src)

    for name in ("gateway.lock", "gateway.pid", "gateway_state.json"):
        assert not os.path.islink(os.path.join(iso, name)), f"{name} 遗留符号链接未被清理"
        assert not os.path.lexists(os.path.join(iso, name)), f"{name} 仍存在于隔离 home"
    # 普通内容仍应可见（功能未被整体破坏）
    assert os.path.lexists(os.path.join(iso, "skills"))