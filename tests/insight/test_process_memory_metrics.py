"""Issue #4203：进程级内存指标采样（v2/v1/无 cgroup 限额三分支 + 引擎子进程聚合）。

seam（公开接口，src/domains/insight/observability/process_memory.py）：
- install(registry) / collect_process_memory(proc_root)：5 个 gauge 拉取时采样；
- read_vm_rss_bytes / read_cgroup_memory_limit(proc_root, sys_fs)：注入 fake /proc、
  /sys/fs/cgroup 验证解析与无限额分支；
- grape_process_memory_limit_unbounded：无 cgroup 限额时置 1（分支在指标上可辨）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.insight.observability import process_memory as pm  # noqa: E402


@pytest.fixture()
def fake_fs(tmp_path):
    """构造 /proc + /sys/fs/cgroup 假根，返回 (proc_root, sys_fs)。"""
    proc = tmp_path / "proc"
    sysfs = tmp_path / "sys" / "fs" / "cgroup"
    proc.mkdir(parents=True)
    sysfs.mkdir(parents=True)
    return str(proc), str(sysfs)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _install_once():
    """install 幂等 + 独立 registry，返回 registry.get_sample_value 辅助。"""
    from prometheus_client import CollectorRegistry
    reg = CollectorRegistry()
    saved = (pm._rss_gauge, pm._limit_gauge, pm._ratio_gauge,
             pm._unbounded_gauge, pm._engine_child_gauge)
    try:
        pm._rss_gauge = pm._limit_gauge = pm._ratio_gauge = None
        pm._unbounded_gauge = pm._engine_child_gauge = None
        pm._known_engines.clear()
        pm.install(reg)
        yield lambda name, **labels: reg.get_sample_value(name, labels)
    finally:
        (pm._rss_gauge, pm._limit_gauge, pm._ratio_gauge,
         pm._unbounded_gauge, pm._engine_child_gauge) = saved
        pm._known_engines.clear()


@pytest.fixture()
def gauge():
    yield from _install_once()


def _make_self_status(proc_root, vm_rss_kb=123456):
    _write(os.path.join(proc_root, "self", "status"),
           f"Name:\tgrape\nVmSize:\t 999999 kB\nVmRSS:\t {vm_rss_kb} kB\nThreads:\t8\n")


class TestReadVmRss:
    def test_parses_vmrss_kb_to_bytes(self, fake_fs):
        proc_root, _ = fake_fs
        _make_self_status(proc_root, vm_rss_kb=2048)
        assert pm.read_vm_rss_bytes(os.path.join(proc_root, "self", "status")) == 2048 * 1024

    def test_missing_file_returns_zero(self, fake_fs):
        proc_root, _ = fake_fs
        assert pm.read_vm_rss_bytes(os.path.join(proc_root, "self", "status")) == 0

    def test_no_vmrss_line_returns_zero(self, fake_fs):
        proc_root, _ = fake_fs
        _write(os.path.join(proc_root, "self", "status"), "Name:\tgrape\n")
        assert pm.read_vm_rss_bytes(os.path.join(proc_root, "self", "status")) == 0


class TestCgroupLimitV2:
    def test_unified_limit(self, fake_fs):
        proc_root, sys_fs = fake_fs
        _write(os.path.join(proc_root, "self", "cgroup"),
               "0::/system.slice/grape-worker-server.service\n")
        _write(os.path.join(sys_fs, "system.slice", "grape-worker-server.service",
                            "memory.max"), "8589934592\n")
        assert pm.read_cgroup_memory_limit(proc_root=proc_root, sys_fs=sys_fs) == 8589934592

    def test_root_path_limit(self, fake_fs):
        proc_root, sys_fs = fake_fs
        _write(os.path.join(proc_root, "self", "cgroup"), "0::/\n")
        _write(os.path.join(sys_fs, "memory.max"), "4294967296\n")
        assert pm.read_cgroup_memory_limit(proc_root=proc_root, sys_fs=sys_fs) == 4294967296

    def test_max_placeholder_means_unbounded(self, fake_fs):
        proc_root, sys_fs = fake_fs
        _write(os.path.join(proc_root, "self", "cgroup"), "0::/\n")
        _write(os.path.join(sys_fs, "memory.max"), "max\n")
        assert pm.read_cgroup_memory_limit(proc_root=proc_root, sys_fs=sys_fs) == 0

    def test_missing_cgroup_file_means_unbounded(self, fake_fs):
        proc_root, sys_fs = fake_fs
        assert pm.read_cgroup_memory_limit(proc_root=proc_root, sys_fs=sys_fs) == 0


class TestCgroupLimitV1:
    def test_v1_memory_subsystem(self, fake_fs):
        proc_root, sys_fs = fake_fs
        _write(os.path.join(proc_root, "self", "cgroup"),
               "11:memory:/grape-worker\n")
        _write(os.path.join(sys_fs, "memory", "grape-worker",
                            "memory.limit_in_bytes"), "4294967296\n")
        assert pm.read_cgroup_memory_limit(proc_root=proc_root, sys_fs=sys_fs) == 4294967296

    def test_v1_placeholder_limit_is_unbounded(self, fake_fs):
        proc_root, sys_fs = fake_fs
        _write(os.path.join(proc_root, "self", "cgroup"), "11:memory:/grape\n")
        _write(os.path.join(sys_fs, "memory", "grape",
                            "memory.limit_in_bytes"), str(1 << 44))
        assert pm.read_cgroup_memory_limit(proc_root=proc_root, sys_fs=sys_fs) == 0


class TestCollectProcessMemory:
    def test_bounded_cgroup_branch(self, fake_fs, gauge, monkeypatch):
        proc_root, sys_fs = fake_fs
        monkeypatch.setenv("GRAPE_PROCESS_ROLE", "worker")
        _make_self_status(proc_root, vm_rss_kb=1024)  # 1 MiB
        _write(os.path.join(proc_root, "self", "cgroup"), "0::/\n")
        _write(os.path.join(sys_fs, "memory.max"), str(4 * 1024 * 1024))  # 4 MiB
        pm.collect_process_memory(proc_root=proc_root, sys_fs=sys_fs)
        assert gauge("grape_process_memory_rss_bytes", process="worker") == 1024 * 1024
        assert gauge("grape_process_memory_limit_bytes", process="worker") == 4 * 1024 * 1024
        assert gauge("grape_process_memory_ratio", process="worker") == pytest.approx(0.25)
        assert gauge("grape_process_memory_limit_unbounded", process="worker") == 0
    def test_unbounded_branch_marks_series(self, fake_fs, gauge, monkeypatch):
        """无 cgroup 限额分支：limit/ratio=0 且 unbounded=1（不把没有限额伪装成 0B）。"""
        proc_root, _ = fake_fs
        monkeypatch.setenv("GRAPE_PROCESS_ROLE", "worker")
        _make_self_status(proc_root, vm_rss_kb=2048)
        # 不写任何 cgroup 文件 → 无限额
        pm.collect_process_memory(proc_root=proc_root)
        assert gauge("grape_process_memory_rss_bytes", process="worker") == 2048 * 1024
        assert gauge("grape_process_memory_limit_bytes", process="worker") == 0
        assert gauge("grape_process_memory_ratio", process="worker") == 0
        assert gauge("grape_process_memory_limit_unbounded", process="worker") == 1

    def test_engine_children_aggregate_and_reset(self, fake_fs, gauge, monkeypatch):
        """活跃引擎子进程按 backend 聚合；消失后显式归零（stale series 教训）。"""
        proc_root, _ = fake_fs
        monkeypatch.setenv("GRAPE_PROCESS_ROLE", "worker")
        _make_self_status(proc_root)

        class _FakeProc:
            def __init__(self, pid, backend):
                self.pid = pid
                self._grape_backend = backend

        _write(os.path.join(proc_root, "101", "status"), "VmRSS:\t 1024 kB\n")
        _write(os.path.join(proc_root, "102", "status"), "VmRSS:\t 2048 kB\n")

        class _FakeExec:
            _active_procs = {_FakeProc(101, "codex"), _FakeProc(102, "codex")}

        fake_mod = _FakeExec
        import types
        monkeypatch.setitem(sys.modules, "src.domains.runtime.agents.executor", fake_mod)

        def _sample(pid_status_dir="/proc"):
            per = {}
            for proc in (101, 102):
                rss = pm.read_vm_rss_bytes(os.path.join(pid_status_dir, str(proc), "status"))
                per["codex"] = per.get("codex", 0) + rss
            return per
        monkeypatch.setattr(pm, "_sample_engine_children", _sample)
        pm.collect_process_memory(proc_root=proc_root)
        assert gauge("grape_engine_child_rss_bytes", process="worker",
                    engine="codex") == 3 * 1024 * 1024

        # 子进程消失 → 序列归零而非保留旧值
        monkeypatch.setattr(pm, "_sample_engine_children", lambda pid_status_dir="/proc": {})
        pm.collect_process_memory(proc_root=proc_root)
        assert gauge("grape_engine_child_rss_bytes", process="worker",
                    engine="codex") == 0

    def test_not_installed_is_noop(self, fake_fs):
        # 未 install（_rss_gauge=None）时 collect 直接返回，不抛
        pm.collect_process_memory(proc_root=fake_fs[0])


class TestStartupSelfcheck:
    def test_report_contains_limit(self, fake_fs, monkeypatch):
        proc_root, sys_fs = fake_fs
        monkeypatch.setenv("GRAPE_PROCESS_ROLE", "worker")
        _write(os.path.join(proc_root, "self", "cgroup"), "0::/\n")
        _write(os.path.join(sys_fs, "memory.max"), str(6 * 1024 ** 3))
        report = pm.effective_memory_limit_report(proc_root=proc_root, sys_fs=sys_fs)
        assert "process=worker" in report and "cgroup limit=" in report
        assert "unbounded" not in report

    def test_report_unbounded(self, fake_fs, monkeypatch):
        proc_root, _ = fake_fs
        monkeypatch.setenv("GRAPE_PROCESS_ROLE", "worker")
        report = pm.effective_memory_limit_report(proc_root=proc_root)
        assert "unbounded" in report

    def test_registry_exported_via_scrape_hook(self, monkeypatch):
        """on_metrics_scrape 挂了采样钩子：指标经 observability registry 可导出。"""
        from prometheus_client import generate_latest
        import src.domains.insight.observability as obs
        body = generate_latest(obs.registry).decode()
        assert "grape_process_memory_rss_bytes" in body
        assert "grape_engine_child_rss_bytes" in body
