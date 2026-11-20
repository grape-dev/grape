"""纯函数环境探测——只出能力事实，不选 provider。

只允许被 manager.py import。其它模块禁止直接 import detect。
detect 不 import backends/manager.py——纯函数，零内部依赖。

detect_capabilities() 返回环境能力 dict。
manager 基于能力 dict 选 provider（用 registry dict 映射，不 if name ==）。
"""
from __future__ import annotations

import os
import shutil

from src.kernel.config import log

# bwrap 候选路径——不依赖 PATH（Worker 可能 PATH 不完整）
_BWRAP_CANDIDATES = [
    "bwrap",               # PATH 查找
    "/usr/bin/bwrap",      # yum/dnf 默认
    "/usr/local/bin/bwrap",  # 源码安装
    "/opt/bubblewrap/bin/bwrap",  # 自定义安装
]


def detect_capabilities(env: dict | None = None) -> dict:
    """探测环境能力。返回 dict。

    返回值由 manager 用于选 provider，不直接创建 provider 实例。
    bwrap 不可用时 log.error（运维可见）。
    """
    bwrap_ok = has_bwrap_binary()
    if not bwrap_ok:
        log.error(
            "bwrap not found in PATH or candidate paths "
            f"({_BWRAP_CANDIDATES})——sandbox will degrade to NullSandbox. "
            "Install: yum install bubblewrap")
    return {
        "bwrap_available": bwrap_ok,
        "landlock_available": has_landlock(),
        "landlock_abi_version": _landlock_abi_version(),
        "cgroup_version": _detect_cgroup_version(),
        "sudo_available": shutil.which("sudo") is not None,
    }


def has_bwrap_binary() -> bool:
    """检测 bwrap 二进制是否存在（候选路径 + PATH）。"""
    for candidate in _BWRAP_CANDIDATES:
        if os.path.basename(candidate) == candidate:
            # 短名——用 PATH 查找
            if shutil.which(candidate):
                return True
        else:
            # 绝对路径——直接检查
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return True
    return False


def has_landlock() -> bool:
    """检测 Landlock 可用性（内核 ABI ≥1 即可用）。"""
    return _landlock_abi_version() >= 1


def _landlock_abi_version() -> int:
    """返回 Landlock ABI 版本号（≥1 可用，≥3 支持 IPC，≥4 支持网络）。0 = 不可用。"""
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        # landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)
        abi = libc.landlock_create_ruleset(None, 0, 1)
        return max(abi, 0)
    except Exception:
        return 0


def _detect_cgroup_version() -> int:
    """检测 cgroup 版本。返回 2（v2）/ 1（v1）/ 0（不可用）。"""
    try:
        with open("/proc/mounts") as f:
            mounts = f.read()
        if "cgroup2" in mounts:
            return 2
        if "cgroup" in mounts:
            return 1
    except Exception:
        pass
    if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return 2
    if os.path.isdir("/sys/fs/cgroup/memory"):
        return 1
    return 0
