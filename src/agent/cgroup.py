"""CgroupSlice — cgroup v2 优先 + v1 降级（Issue #139）。

设计参考：deepagents-sandbox/cgroup.py + Linux cgroup v2 docs

特性：
- cgroup v2 优先（unified 层级 /sys/fs/cgroup/），v2 不可用时降级到 v1
- v2 路径：/sys/fs/cgroup/grape-sandbox-xxx/memory.max / cpu.max / pids.max
- v1 路径：/sys/fs/cgroup/memory/grape-sandbox-xxx/memory.limit_in_bytes 等
- 降级策略：不可用时 warn + 跳过，不拒绝启动
- context manager 生命周期（__enter__/__exit__）
- 内存限额默认 0 = 自动取宿主机 50%（宽松硬上限）
- OOM 检测：exit_code == 137
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("devops-bot")

# K8s 资源量后缀（借鉴 isolate 的 ParseMemory/ParseCPU）
_MEM_SUFFIX = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
               "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


def parse_memory(s: str) -> int:
    """解析内存量字符串为字节数。支持 K8s 格式：256Mi / 1Gi / 512M / 1048576。

    返回 MB（向下取整）。无效格式返回 0。
    """
    s = s.strip()
    if s.isdigit():
        # 纯数字 — 按字节解析
        return int(s) // (1024 * 1024)
    for suffix, mult in _MEM_SUFFIX.items():
        if s.endswith(suffix):
            num = s[:-len(suffix)].strip()
            if num.isdigit():
                return int(num) * mult // (1024 * 1024)
    # 无后缀的纯数字 — 可能是 MB
    if s.isdigit():
        return int(s)
    log.warning(f"cgroup: invalid memory format '{s}', expected e.g. '256Mi' or '2048'")
    return 0


def parse_cpu(s: str) -> int:
    """解析 CPU 限额字符串为毫核数（1000 = 1 核）。支持：1 / 0.5 / 500m。

    返回毫核数。无效格式返回 0。
    """
    s = s.strip()
    if s.endswith("m"):
        num = s[:-1].strip()
        if num.isdigit():
            return int(num)
    try:
        cores = float(s)
        return int(cores * 1000)
    except ValueError:
        log.warning(f"cgroup: invalid cpu format '{s}', expected e.g. '1' or '500m'")
        return 0


class CgroupError(Exception):
    pass


def _detect_cgroup_version() -> int:
    """检测 cgroup 版本。返回 2（v2 unified）或 1（v1 legacy）。"""
    # v2: /sys/fs/cgroup/cgroup.controllers 存在
    if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return 2
    # v1: /sys/fs/cgroup/memory/ 存在
    if os.path.isdir("/sys/fs/cgroup/memory"):
        return 1
    return 0  # 不可用


class CgroupSlice:
    """cgroup v2/v1 子组管理（memory + cpu + pids）。

    用法：
        with CgroupSlice(task_id, mem_limit_mb=0) as cg:
            cg.attach_pid(proc.pid)
            # ... Agent 运行 ...
        # __exit__ 自动销毁 cgroup 子组

    降级：sudo 不可用 / cgroup 不可写 → create() 返回 False，不限制资源。
    """

    def __init__(
        self,
        task_id: str,
        mem_limit_mb: int | str = 0,
        cpu_quota: int | str = 0,
        max_pids: int = 0,
    ):
        # 用 md5 hash 保证并发唯一性（sanitize[:12] 会碰撞）
        import hashlib as _hashlib
        unique_id = _hashlib.md5(task_id.encode()).hexdigest()[:16]
        self._name = f"grape-sandbox-{unique_id}"
        # 支持 K8s 格式（"2Gi" / "500m"）或纯数字（MB / 毫核）
        self._mem_limit = parse_memory(str(mem_limit_mb)) if isinstance(mem_limit_mb, str) else mem_limit_mb
        self._cpu_quota = parse_cpu(str(cpu_quota)) if isinstance(cpu_quota, str) else cpu_quota
        self._max_pids = max_pids
        self._created = False
        self._has_sudo = shutil.which("sudo") is not None
        self._cgroup_version = _detect_cgroup_version()

    @property
    def name(self) -> str:
        return self._name

    @property
    def created(self) -> bool:
        return self._created

    def create(self) -> bool:
        """创建 cgroup 子组。返回 False = 降级（不可用但已告警）。"""
        if not self._has_sudo:
            log.warning(
                "cgroup: sudo not available, resource limits disabled (degraded)"
            )
            return False

        if self._cgroup_version == 2:
            return self._create_v2()
        elif self._cgroup_version == 1:
            return self._create_v1()
        else:
            log.warning(
                "cgroup: neither v2 nor v1 detected, resource limits disabled"
            )
            return False

    # ===== cgroup v2 =====

    def _create_v2(self) -> bool:
        """cgroup v2 unified 层级创建子组。"""
        try:
            base = f"/sys/fs/cgroup/{self._name}"

            # 内存限额：0 = 自动取宿主机 50%
            mem_limit = self._mem_limit
            if mem_limit == 0:
                mem_limit = self._auto_mem_limit()
                if mem_limit > 0:
                    log.info(
                        f"cgroup v2: auto memory limit = {mem_limit}MB (50% of host)"
                    )

            # 1. 先启用 controller（v2 "no internal process" 约束：
            #    根 cgroup 已有进程时不能在其 subtree_control 启用新 controller，
            #    必须在 mkdir 子组之前写 subtree_control）
            controllers = []
            if mem_limit > 0:
                controllers.append("+memory")
            if self._cpu_quota > 0:
                controllers.append("+cpu")
            if self._max_pids > 0:
                controllers.append("+pids")
            if controllers:
                try:
                    self._write(
                        "/sys/fs/cgroup/cgroup.subtree_control",
                        " ".join(controllers),
                    )
                except Exception as e:
                    log.warning(f"cgroup v2: enable controllers failed: {e}")

            # 2. 创建子组
            self._mkdir(base)

            # 3. memory.max（硬上限 — OOM kill）
            if mem_limit > 0:
                self._write(
                    f"{base}/memory.max",
                    str(mem_limit * 1024 * 1024),
                )
                # memory.high（90% 软上限 — 内核回收内存但不杀进程，借鉴 m9m）
                high_bytes = mem_limit * 1024 * 1024 * 90 // 100
                try:
                    self._write(f"{base}/memory.high", str(high_bytes))
                except Exception as e:
                    log.warning(f"cgroup v2: memory.high failed: {e}")

            # 4. cpu.max（格式："$MAX $PERIOD"，如 "50000 100000" = 50%）
            # cpu_quota 是毫核数（1000 = 1 核），转换为微秒：quota_us = 毫核 * 100
            if self._cpu_quota > 0:
                quota_us = self._cpu_quota * 100
                self._write(
                    f"{base}/cpu.max",
                    f"{quota_us} 100000",
                )

            # 5. pids.max
            if self._max_pids > 0:
                self._write(f"{base}/pids.max", str(self._max_pids))

            self._created = True
            log.info(f"cgroup v2: slice created at {base}")
            return True
        except Exception as e:
            log.warning(f"cgroup v2: create failed, resource limits disabled: {e}")
            self.destroy(force=True)
            return False

    def _attach_pid_v2(self, pid: int) -> None:
        """v2: 写 PID 到 cgroup.procs。"""
        path = f"/sys/fs/cgroup/{self._name}/cgroup.procs"
        if os.path.exists(path):
            self._write(path, str(pid))

    def _destroy_v2(self) -> None:
        """v2: 删除子组目录。"""
        path = f"/sys/fs/cgroup/{self._name}"
        if os.path.isdir(path):
            try:
                self._sudo("rmdir", path)
            except Exception:
                pass

    # ===== cgroup v1（降级）=====

    def _create_v1(self) -> bool:
        """cgroup v1 各子系统独立创建子组（降级路径）。"""
        try:
            mem_limit = self._mem_limit
            if mem_limit == 0:
                mem_limit = self._auto_mem_limit()
                if mem_limit > 0:
                    log.info(
                        f"cgroup v1: auto memory limit = {mem_limit}MB (50% of host)"
                    )

            # memory 子组
            self._mkdir("/sys/fs/cgroup/memory/" + self._name)
            if mem_limit > 0:
                self._write(
                    "/sys/fs/cgroup/memory/" + self._name + "/memory.limit_in_bytes",
                    str(mem_limit * 1024 * 1024),
                )
                # memory.soft_limit_in_bytes（90% 软上限 — 内核回收内存但不杀进程）
                soft_bytes = mem_limit * 1024 * 1024 * 90 // 100
                try:
                    self._write(
                        "/sys/fs/cgroup/memory/" + self._name + "/memory.soft_limit_in_bytes",
                        str(soft_bytes),
                    )
                except Exception as e:
                    log.warning(f"cgroup v1: memory.soft_limit failed: {e}")

            # cpu 子组
            self._mkdir("/sys/fs/cgroup/cpu,cpuacct/" + self._name)
            if self._cpu_quota > 0:
                # cpu_quota 是毫核数（1000 = 1 核），v1: cfs_quota_us = 毫核 * 100
                quota_us = self._cpu_quota * 100
                self._write(
                    "/sys/fs/cgroup/cpu,cpuacct/" + self._name + "/cpu.cfs_quota_us",
                    str(quota_us),
                )
                self._write(
                    "/sys/fs/cgroup/cpu,cpuacct/" + self._name + "/cpu.cfs_period_us",
                    "100000",
                )

            # pids 子组（可选）
            if self._max_pids > 0:
                self._mkdir("/sys/fs/cgroup/pids/" + self._name)
                self._write(
                    "/sys/fs/cgroup/pids/" + self._name + "/pids.max",
                    str(self._max_pids),
                )

            self._created = True
            log.info(f"cgroup v1: slice created (memory+cpu{'+pids' if self._max_pids > 0 else ''})")
            return True
        except Exception as e:
            log.warning(f"cgroup v1: create failed, resource limits disabled: {e}")
            self.destroy(force=True)
            return False

    def _attach_pid_v1(self, pid: int) -> None:
        """v1: 写 PID 到各子系统的 tasks 文件。"""
        for subsys, has_dir in [
            ("memory", True),
            ("cpu,cpuacct", True),
            ("pids", self._max_pids > 0),
        ]:
            if not has_dir:
                continue
            path = f"/sys/fs/cgroup/{subsys}/{self._name}/tasks"
            if os.path.exists(path):
                try:
                    self._write(path, str(pid))
                except Exception as e:
                    log.warning(f"cgroup v1: attach pid {pid} to {subsys} failed: {e}")

    def _destroy_v1(self) -> None:
        """v1: 删除各子系统子组目录。"""
        for subsys in ("memory", "cpu,cpuacct", "pids"):
            path = f"/sys/fs/cgroup/{subsys}/{self._name}"
            if os.path.isdir(path):
                try:
                    self._sudo("rmdir", path)
                except Exception:
                    pass

    # ===== 公共方法 =====

    def attach_pid(self, pid: int) -> None:
        """将 Agent 子进程 PID 写入 cgroup。"""
        if not self._created:
            return
        if self._cgroup_version == 2:
            self._attach_pid_v2(pid)
        else:
            self._attach_pid_v1(pid)

    def destroy(self, *, force: bool = False) -> None:
        """销毁 cgroup 子组。force=True 时忽略进程残留。"""
        if not self._created and not force:
            return
        if self._cgroup_version == 2:
            self._destroy_v2()
        else:
            self._destroy_v1()
        self._created = False

    def __enter__(self) -> "CgroupSlice":
        self.create()
        return self

    def __exit__(self, *args: object) -> None:
        self.destroy()

    # ===== 资源统计（借鉴 m9m GetStats）=====

    def get_stats(self) -> dict:
        """读取 cgroup 资源使用统计。任务结束后调用。

        v2: cpu.stat / memory.peak
        v1: cpuacct.usage / memory.max_usage_in_bytes
        """
        if not self._created:
            return {}
        stats: dict = {}
        try:
            if self._cgroup_version == 2:
                base = f"/sys/fs/cgroup/{self._name}"
                # CPU 使用时间（微秒）
                try:
                    with open(f"{base}/cpu.stat") as f:
                        for line in f:
                            if line.startswith("usage_usec"):
                                stats["cpu_usec"] = int(line.split()[1])
                except Exception:
                    pass
                # 峰值内存（字节）
                try:
                    with open(f"{base}/memory.peak") as f:
                        stats["mem_peak_bytes"] = int(f.read().strip())
                except Exception:
                    pass
            elif self._cgroup_version == 1:
                cg_mem = f"/sys/fs/cgroup/memory/{self._name}"
                cg_cpu = f"/sys/fs/cgroup/cpu,cpuacct/{self._name}"
                # CPU 使用时间（纳秒）
                try:
                    with open(f"{cg_cpu}/cpuacct.usage") as f:
                        stats["cpu_nsec"] = int(f.read().strip())
                except Exception:
                    pass
                # 峰值内存（字节）
                try:
                    with open(f"{cg_mem}/memory.max_usage_in_bytes") as f:
                        stats["mem_peak_bytes"] = int(f.read().strip())
                except Exception:
                    pass
        except Exception:
            pass
        return stats

    # ===== 内部方法 =====

    @staticmethod
    def _auto_mem_limit() -> int:
        """读取宿主机总内存，返回 50%（MB）。读取失败返回 0（不限）。

        最低 512MB，防止极小内存机器设过低导致 Agent 无法启动。
        """
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                        total_mb = total_kb // 1024
                        return max(512, total_mb // 2)
        except Exception:
            pass
        return 0

    def _sudo(self, *args: str, input: bytes | None = None) -> None:
        subprocess.run(
            ["sudo", *args], check=True, capture_output=True, timeout=10,
            input=input,
        )

    def _mkdir(self, path: str) -> None:
        self._sudo("mkdir", "-p", path)

    def _write(self, path: str, value: str) -> None:
        """安全写入 cgroup 文件 — 用 tee 代替 bash -c echo，避免 shell 注入。"""
        self._sudo("tee", path, input=value.encode())
