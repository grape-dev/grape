"""资源限制组件——cgroup v2/v1 + ulimit 兜底。

从 cgroup.py 迁移，封装成 CgroupLimiter 实现 ResourceLimiter Protocol。
4 个 bug 修复：路径硬编码→/proc/mounts / CPU-PIDS 默认 0→1核+128 / memory 50%→按引擎 / parse_memory 统一。

装饰器模式：wrap_self_attach 在命令内部自 attach cgroup + ulimit 兜底。
不依赖外部 pid——cgroup attach 在命令内部通过 echo $$ > cgroup.procs 完成。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Protocol

log = logging.getLogger("devops-bot")


# ===== ResourceLimiter Protocol =====

class ResourceLimiter(Protocol):
    """资源限制组件——通过 wrap 融入命令，不用 apply(pid)。

    装饰器模式：wrap_self_attach 在命令内部自 attach cgroup + ulimit 兜底。
    """

    def prepare(self) -> None:
        """创建 cgroup 子组目录。在 __enter__ 时调用。"""
        ...

    def wrap_self_attach(self, cmd: list[str]) -> list[str]:
        """装饰命令：sh -c 'echo $$ > cgroup.procs; ulimit ...; exec "$@"' -- <cmd>"""
        ...

    def release(self) -> None:
        """销毁 cgroup 子组。幂等。"""
        ...

# K8s 资源量后缀
_MEM_SUFFIX = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
               "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}

# 按引擎区分的默认资源限制（Bug #3 修复：不再统一 50%）
_ENGINE_LIMITS = {
    "hermes": {"mem_mb": 2048, "cpu_quota": 1000, "max_pids": 128},
    "opencode": {"mem_mb": 1024, "cpu_quota": 1000, "max_pids": 128},
    "codearts": {"mem_mb": 1024, "cpu_quota": 1000, "max_pids": 128},
    "codex": {"mem_mb": 1024, "cpu_quota": 1000, "max_pids": 128},
}
_DEFAULT_LIMITS = {"mem_mb": 1024, "cpu_quota": 1000, "max_pids": 128}


def parse_memory(s: str) -> int:
    """解析内存量字符串为 MB。支持 K8s 格式：256Mi / 1Gi / 512M / 1048576。"""
    s = s.strip()
    if s.isdigit():
        return int(s) // (1024 * 1024)
    for suffix, mult in _MEM_SUFFIX.items():
        if s.endswith(suffix):
            num = s[:-len(suffix)].strip()
            if num.isdigit():
                return int(num) * mult // (1024 * 1024)
    if s.isdigit():
        return int(s)
    log.warning(f"cgroup: invalid memory format '{s}'")
    return 0


def parse_cpu(s: str) -> int:
    """解析 CPU 限额为毫核数（1000 = 1 核）。"""
    s = s.strip()
    if s.endswith("m"):
        num = s[:-1].strip()
        if num.isdigit():
            return int(num)
    try:
        return int(float(s) * 1000)
    except ValueError:
        return 0


def _detect_cgroup_version() -> int:
    """检测 cgroup 版本。Bug #1 修复：读 /proc/mounts 而非硬编码路径。"""
    try:
        with open("/proc/mounts") as f:
            mounts = f.read()
        if "cgroup2" in mounts:
            return 2
        if "cgroup" in mounts:
            return 1
    except Exception:
        pass
    # 降级：检查路径
    if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return 2
    if os.path.isdir("/sys/fs/cgroup/memory"):
        return 1
    return 0


class CgroupLimiter:
    """资源限制组件——cgroup v2/v1 + ulimit 兜底。

    实现 ResourceLimiter Protocol。
    prepare() 在 __enter__ 时创建 cgroup 目录。
    wrap_self_attach() 在命令内部自 attach + ulimit。
    release() 在 cleanup() 时销毁。
    """

    def __init__(self, exec_backend: str = ""):
        limits = _ENGINE_LIMITS.get(exec_backend, _DEFAULT_LIMITS)
        self._mem_limit = limits["mem_mb"]
        self._cpu_quota = limits["cpu_quota"]  # Bug #2 修复：默认 1 核而非 0
        self._max_pids = limits["max_pids"]    # Bug #2 修复：默认 128 而非 0
        self._exec_backend = exec_backend

        # 标识符
        import hashlib
        _safe = re.sub(r"[^a-zA-Z0-9_-]", "", exec_backend)[:16]
        _hash = hashlib.sha256(exec_backend.encode()).hexdigest()[:8]
        self._name = f"grape-sandbox-{_safe}-{_hash}"

        self._created = False
        self._has_sudo = shutil.which("sudo") is not None
        self._cgroup_version = _detect_cgroup_version()

    def prepare(self) -> None:
        """创建 cgroup 子组。失败不抛异常（降级为 ulimit 兜底）。"""
        if not self._has_sudo:
            log.warning("cgroup: sudo not available, resource limits disabled (ulimit only)")
            return
        if self._cgroup_version == 2:
            self._create_v2()
        elif self._cgroup_version == 1:
            self._create_v1()
        else:
            log.warning("cgroup: neither v2 nor v1 detected, resource limits disabled")

    def wrap_self_attach(self, cmd: list[str]) -> list[str]:
        """cgroup 自 attach + ulimit 兜底。

        sh -c 'echo $$ > cgroup.procs; ulimit ...; exec "$@"' -- <cmd>
        两个 -- 语义不同：第一个是 bwrap 分隔符（BwrapSandbox 拼接），
        第二个是 sh 的 $0 占位符。
        """
        ulimit_str = 'ulimit -u 128 -v 1048576 -f 102400 -n 256 2>/dev/null'
        if self._created and self._cgroup_version == 2:
            cgroup_procs = f"/sys/fs/cgroup/{self._name}/cgroup.procs"
            return ["sh", "-c",
                    f'echo $$ > {cgroup_procs} 2>/dev/null; {ulimit_str}; exec "$@"',
                    "--"] + cmd
        elif self._created and self._cgroup_version == 1:
            # v1 仅兼容保留：只写 memory 子系统 tasks。
            # 未写 cpu/pids 子系统的 tasks + cgroup.procs——多线程进程子线程可能不在限制内。
            # 如需完整 v1 支持：补写 /sys/fs/cgroup/cpu/{name}/tasks + /sys/fs/cgroup/pids/{name}/tasks
            # + cgroup.procs（进程级）确保整个进程组迁入。
            # 当前环境为 cgroup v2，v1 路径实际不触发。
            mem_procs = f"/sys/fs/cgroup/memory/{self._name}/tasks"
            return ["sh", "-c",
                    f'echo $$ > {mem_procs} 2>/dev/null; {ulimit_str}; exec "$@"',
                    "--"] + cmd
        else:
            # cgroup 不可用——纯 ulimit 兜底
            return ["sh", "-c",
                    f'{ulimit_str}; exec "$@"',
                    "--"] + cmd

    def release(self) -> None:
        """销毁 cgroup 子组。幂等。"""
        if not self._created:
            return
        self._created = False
        if self._cgroup_version == 2:
            self._destroy_v2()
        elif self._cgroup_version == 1:
            self._destroy_v1()

    # ===== cgroup v2 =====

    def _create_v2(self):
        try:
            base = f"/sys/fs/cgroup/{self._name}"
            controllers = []
            if self._mem_limit > 0:
                controllers.append("+memory")
            if self._cpu_quota > 0:
                controllers.append("+cpu")
            if self._max_pids > 0:
                controllers.append("+pids")
            if controllers:
                try:
                    self._write("/sys/fs/cgroup/cgroup.subtree_control", " ".join(controllers))
                except Exception as e:
                    log.warning(f"cgroup v2: enable controllers failed: {e}")
            self._mkdir(base)
            if self._mem_limit > 0:
                self._write(f"{base}/memory.max", str(self._mem_limit * 1024 * 1024))
                high = self._mem_limit * 1024 * 1024 * 90 // 100
                try:
                    self._write(f"{base}/memory.high", str(high))
                except Exception:
                    pass
            if self._cpu_quota > 0:
                quota_us = self._cpu_quota * 100
                self._write(f"{base}/cpu.max", f"{quota_us} 100000")
            if self._max_pids > 0:
                self._write(f"{base}/pids.max", str(self._max_pids))
            self._created = True
            log.info(f"cgroup v2: slice created at {base}")
        except Exception as e:
            log.warning(f"cgroup v2: create failed: {e}")
            self._destroy_v2()

    def _destroy_v2(self):
        path = f"/sys/fs/cgroup/{self._name}"
        if os.path.isdir(path):
            try:
                self._sudo("rmdir", path)
            except Exception:
                pass

    # ===== cgroup v1 =====

    def _create_v1(self):
        try:
            self._mkdir(f"/sys/fs/cgroup/memory/{self._name}")
            if self._mem_limit > 0:
                self._write(
                    f"/sys/fs/cgroup/memory/{self._name}/memory.limit_in_bytes",
                    str(self._mem_limit * 1024 * 1024))
            self._mkdir(f"/sys/fs/cgroup/cpu/{self._name}")
            if self._cpu_quota > 0:
                quota_us = self._cpu_quota * 100
                self._write(f"/sys/fs/cgroup/cpu/{self._name}/cpu.cfs_quota_us", str(quota_us))
                self._write(f"/sys/fs/cgroup/cpu/{self._name}/cpu.cfs_period_us", "100000")
            self._mkdir(f"/sys/fs/cgroup/pids/{self._name}")
            if self._max_pids > 0:
                self._write(f"/sys/fs/cgroup/pids/{self._name}/pids.max", str(self._max_pids))
            self._created = True
            log.info(f"cgroup v1: slice created for {self._name}")
        except Exception as e:
            log.warning(f"cgroup v1: create failed: {e}")
            self._destroy_v1()

    def _destroy_v1(self):
        for sub in ("memory", "cpu", "pids"):
            path = f"/sys/fs/cgroup/{sub}/{self._name}"
            if os.path.isdir(path):
                try:
                    self._sudo("rmdir", path)
                except Exception:
                    pass

    # ===== 辅助 =====

    def _write(self, path: str, value: str):
        self._sudo("sh", "-c", f'echo "{value}" > {path}')

    def _mkdir(self, path: str):
        self._sudo("mkdir", path)

    def _sudo(self, *args: str):
        if not self._has_sudo:
            raise RuntimeError("sudo not available")
        import subprocess
        subprocess.run(["sudo"] + list(args), check=True, capture_output=True, timeout=5)


# ===== NullResourceLimiter =====

class NullResourceLimiter:
    """空资源限制——完全实现 Protocol。"""

    def prepare(self) -> None: pass
    def wrap_self_attach(self, cmd: list[str]) -> list[str]: return cmd
    def release(self) -> None: pass
