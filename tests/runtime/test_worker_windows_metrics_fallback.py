"""Windows 指标兜底：wmic 在新版 Windows 已移除时的 psutil / CIM 兜底。"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _zeros():
    return {"cpu_percent": 0.0, "memory_percent": 0.0, "disk_percent": 0.0,
            "tmp_percent": 0.0, "bandwidth_percent": 0.0, "bandwidth_mbps": 0.0,
            "load_avg": "", "spec": ""}


def test_psutil_fallback_fills_all_zero_metrics(monkeypatch):
    from src.domains.runtime.engine import agent

    class _VM:
        percent = 37.5
        total = 16 * 1024 ** 3

    class _DU:
        total = 100

        def __init__(self, pct):
            self.percent = pct

    fake = types.SimpleNamespace(
        cpu_percent=lambda interval=None: 21.0,
        virtual_memory=lambda: _VM(),
        disk_usage=lambda path: _DU(63.0),
        cpu_count=lambda logical=True: 8,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    m = _zeros()
    agent._windows_metrics_fallback(m)
    assert m["cpu_percent"] == 21.0
    assert m["memory_percent"] == 37.5
    assert m["disk_percent"] == 63.0
    assert m["spec"] == "8C16G"


def test_psutil_does_not_overwrite_good_values(monkeypatch):
    from src.domains.runtime.engine import agent

    class _VM:
        percent = 99.0
        total = 8 * 1024 ** 3

    fake = types.SimpleNamespace(
        cpu_percent=lambda interval=None: 99.0,
        virtual_memory=lambda: _VM(),
        disk_usage=lambda path: types.SimpleNamespace(total=1, percent=99.0),
        cpu_count=lambda logical=True: 4,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    m = _zeros()
    m.update({"cpu_percent": 5.0, "spec": "4C8G", "tmp_percent": 12.0})
    agent._windows_metrics_fallback(m)
    assert m["cpu_percent"] == 5.0, "已有采样值不能被兜底覆盖"
    assert m["spec"] == "4C8G"
    assert m["memory_percent"] == 99.0, "空缺字段仍要补上"


def test_cim_fallback_when_psutil_missing(monkeypatch):
    from src.domains.runtime.engine import agent

    monkeypatch.setitem(sys.modules, "psutil", None)  # import 失败路径

    def _fake_run(cmd, **kw):
        joined = " ".join(cmd)
        if "LoadPercentage" in joined:
            return types.SimpleNamespace(stdout="18.5\n", returncode=0)
        if "FreePhysicalMemory" in joined:
            return types.SimpleNamespace(stdout="44.0\n", returncode=0)
        if "NumberOfLogicalProcessors" in joined:
            return types.SimpleNamespace(stdout="4\n", returncode=0)
        if "TotalPhysicalMemory" in joined:
            return types.SimpleNamespace(stdout=str(8 * 1024 ** 3) + "\n", returncode=0)
        return types.SimpleNamespace(stdout="", returncode=0)

    import subprocess
    monkeypatch.setattr(subprocess, "run", _fake_run, raising=False)
    m = _zeros()
    agent._windows_metrics_fallback(m)
    assert m["cpu_percent"] == 18.5
    assert m["memory_percent"] == 44.0
    assert m["spec"] == "4C8G"


def test_unix_psutil_fill_only_when_proc_read_failed(monkeypatch):
    """Linux 上 /proc 读失败 → memory/disk 用 psutil 补；已有值不动。"""
    from src.domains.runtime.engine import agent

    class _VM:
        percent = 41.5
        total = 8 * 1024 ** 3

    fake = types.SimpleNamespace(
        virtual_memory=lambda: _VM(),
        disk_usage=lambda path: types.SimpleNamespace(total=1, percent=55.0),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    m = _zeros()
    agent._unix_metrics_psutil_fill(m)
    assert m["memory_percent"] == 41.5
    assert m["disk_percent"] == 55.0
    assert m["spec"] == f"{os.cpu_count()}C8G"

    m2 = _zeros()
    m2.update({"memory_percent": 12.0, "disk_percent": 33.0, "spec": "2C4G"})
    agent._unix_metrics_psutil_fill(m2)
    assert (m2["memory_percent"], m2["disk_percent"], m2["spec"]) == (12.0, 33.0, "2C4G")
