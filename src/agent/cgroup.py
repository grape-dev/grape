"""CgroupSlice — cgroup v1 子组管理（Issue #139）。

设计参考：deepagents-sandbox/cgroup.py

特性：
- cgroup v1（memory + cpu + pids 子系统独立挂载）
- 降级策略：不可用时 warn + 跳过，不拒绝启动
- context manager 生命周期（__enter__/__exit__）
- 内存限额默认 0 = 自动取宿主机 50%（宽松硬上限）
- OOM 检测：exit_code == 137
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger("devops-bot")


class CgroupError(Exception):
    pass


class CgroupSlice:
    """cgroup v1 子组管理（memory + cpu + pids）。

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
        mem_limit_mb: int = 0,
        cpu_quota: int = 0,
        max_pids: int = 0,
    ):
        self._name = f"grape-sandbox-{task_id[:12]}"
        self._mem_limit = mem_limit_mb
        self._cpu_quota = cpu_quota
        self._max_pids = max_pids
        self._created = False
        self._has_sudo = shutil.which("sudo") is not None

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
        try:
            # 内存限额：0 = 自动取宿主机 50%
            mem_limit = self._mem_limit
            if mem_limit == 0:
                mem_limit = self._auto_mem_limit()
                if mem_limit > 0:
                    log.info(
                        f"cgroup: auto memory limit = {mem_limit}MB (50% of host)"
                    )

            # memory 子组
            self._mkdir("/sys/fs/cgroup/memory/" + self._name)
            if mem_limit > 0:
                self._write(
                    "/sys/fs/cgroup/memory/" + self._name + "/memory.limit_in_bytes",
                    str(mem_limit * 1024 * 1024),
                )

            # cpu 子组
            self._mkdir("/sys/fs/cgroup/cpu,cpuacct/" + self._name)
            if self._cpu_quota > 0:
                self._write(
                    "/sys/fs/cgroup/cpu,cpuacct/" + self._name + "/cpu.cfs_quota_us",
                    str(self._cpu_quota * 1000),
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
            return True
        except Exception as e:
            log.warning(f"cgroup: create failed, resource limits disabled: {e}")
            self.destroy(force=True)
            return False

    def attach_pid(self, pid: int) -> None:
        """将 Agent 子进程 PID 写入 cgroup。"""
        if not self._created:
            return
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
                    log.warning(f"cgroup: attach pid {pid} to {subsys} failed: {e}")

    def destroy(self, *, force: bool = False) -> None:
        """销毁 cgroup 子组。force=True 时忽略进程残留。"""
        if not self._created and not force:
            return
        for subsys in ("memory", "cpu,cpuacct", "pids"):
            path = f"/sys/fs/cgroup/{subsys}/{self._name}"
            if os.path.isdir(path):
                try:
                    self._sudo("rmdir", path)
                except Exception:
                    pass
        self._created = False

    def __enter__(self) -> "CgroupSlice":
        self.create()
        return self

    def __exit__(self, *args: object) -> None:
        self.destroy()

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

    def _sudo(self, *args: str) -> None:
        subprocess.run(
            ["sudo", *args], check=True, capture_output=True, timeout=10
        )

    def _mkdir(self, path: str) -> None:
        self._sudo("mkdir", "-p", path)

    def _write(self, path: str, value: str) -> None:
        self._sudo("bash", "-c", f"echo {value} > {path}")
