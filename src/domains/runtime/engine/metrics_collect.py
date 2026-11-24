"""worker 指标兜底采集（平台差异集中在这里，别散进 agent.py）。

为什么单独一个模块（架构守护 A8）：`agent.py` 已经 5k+ 行，采集口径有 wmic / psutil /
ctypes-Win32 / PowerShell-CIM 四条路径，放在同一个文件里后续调整要改多处。这里收成一个
入口 `windows_metrics_fallback()` / `unix_metrics_psutil_fill()`，`os_adapter` 只保留平台判定。

数值口径（每个数带来源，避免魔鬼数字）：
- `psutil.cpu_percent(interval=None)`：psutil 契约「距上次调用以来的平均占用」，不阻塞调用方；
  worker 每 15s 心跳一次，天然就是这个周期的平均值。旧实现固定 `interval=0.2` 会在心跳线程里
  凭空睡 200ms（无来源）。
- PowerShell 只发**一次**调用（一段脚本同时打印 cpu/mem/核数/内存总量），预算取
  `RUNTIME_HEARTBEAT_INTERVAL`（默认 15s）的一半：最坏 7.5s < 一个心跳周期，不会把心跳推迟
  到下一轮（`runtime.py` docstring 记录的「心跳排队 → 引擎失联」就是同类故障）。
- CIM 失败后按 `20 × 心跳周期`（默认 5min）退避：PowerShell 被策略禁用时不要每轮都去撞，
  更不能每轮串 4 次超时。
"""
import os

from src.kernel.config import log

_HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("RUNTIME_HEARTBEAT_INTERVAL", "15"))
# 单次 CIM 调用的时间预算：半个心跳周期（见模块 docstring 的推导）
_CIM_TIMEOUT_SECONDS = max(1.0, min(8.0, _HEARTBEAT_INTERVAL_SECONDS / 2))
# CIM 失败退避：20 个心跳周期（默认 5min），避免策略禁用时每轮重试
_CIM_RETRY_COOLDOWN_SECONDS = max(60.0, _HEARTBEAT_INTERVAL_SECONDS * 20)
_cim_cooldown_until = 0.0


def _empty_metrics() -> dict:
    return {"cpu_percent": 0.0, "memory_percent": 0.0, "disk_percent": 0.0,
            "tmp_percent": 0.0, "bandwidth_percent": 0.0, "bandwidth_mbps": 0.0,
            "load_avg": "", "spec": ""}

def _unix_metrics_psutil_fill(metrics: dict) -> None:
    """Unix 上 /proc 读失败时用 psutil 补空缺字段（只填空值，不覆盖已有采样）。"""
    try:
        import psutil as _psu
    except Exception:
        return
    if not metrics.get("memory_percent"):
        try:
            metrics["memory_percent"] = round(float(_psu.virtual_memory().percent), 1)
        except Exception:
            pass
    if not metrics.get("disk_percent"):
        try:
            _du = _psu.disk_usage("/")
            if _du.total > 0:
                metrics["disk_percent"] = round(float(_du.percent), 1)
        except Exception:
            pass
    if not metrics.get("spec"):
        try:
            _cores = os.cpu_count() or 0
            _total_b = int(_psu.virtual_memory().total or 0)
            if _cores and _total_b:
                _gb = _total_b // (1024 ** 3)
                metrics["spec"] = f"{_cores}C{_gb}G" if _gb >= 1 else \
                    f"{_cores}C{_total_b // (1024 ** 2)}MB"
        except Exception:
            pass


def _windows_ctypes_fallback(metrics: dict) -> None:
    """Windows 兜底之二：ctypes 调 Win32 API（不依赖 wmic / psutil / PowerShell）。

    为什么还要这一级：wmic 在新版 Windows 已移除，psutil 在部分 worker 安装里缺失，
    PowerShell 也可能被策略禁用（-NoProfile 仍受 ExecutionPolicy/安全软件影响）。而
    `GlobalMemoryStatusEx` / `GetSystemTimes` / `shutil.disk_usage` 是进程内调用，只要
    Python 能跑就一定能拿到数。只填空字段，已有采样不覆盖。
    """
    from src.domains.runtime.engine.os_adapter import is_windows as _is_win
    if not _is_win():
        return
    import ctypes as _ct
    from ctypes import wintypes as _wt

    # 内存占用 + 总量（GlobalMemoryStatusEx.dwMemoryLoad 就是百分比）
    if not metrics.get("memory_percent") or not metrics.get("spec"):
        try:
            class _MEMSTATUS( _ct.Structure):
                _fields_ = [("dwLength", _wt.DWORD), ("dwMemoryLoad", _wt.DWORD),
                            ("ullTotalPhys", _ct.c_ulonglong), ("ullAvailPhys", _ct.c_ulonglong),
                            ("ullTotalPageFile", _ct.c_ulonglong), ("ullAvailPageFile", _ct.c_ulonglong),
                            ("ullTotalVirtual", _ct.c_ulonglong), ("ullAvailVirtual", _ct.c_ulonglong),
                            ("ullAvailExtendedVirtual", _ct.c_ulonglong)]
            _st = _MEMSTATUS()
            _st.dwLength = _ct.sizeof(_MEMSTATUS)
            if _ct.windll.kernel32.GlobalMemoryStatusEx(_ct.byref(_st)):
                if not metrics.get("memory_percent"):
                    metrics["memory_percent"] = round(float(_st.dwMemoryLoad), 1)
                if not metrics.get("spec") and _st.ullTotalPhys:
                    _cores = os.cpu_count() or 0
                    _gb = int(_st.ullTotalPhys) // (1024 ** 3)
                    if _cores and _gb:
                        metrics["spec"] = f"{_cores}C{_gb}G"
        except Exception:
            pass

    # CPU：GetSystemTimes 两次采样差分（首次无基准 → 跳过，下次心跳就有值）
    if not metrics.get("cpu_percent"):
        try:
            class _FILETIME(_ct.Structure):
                _fields_ = [("dwLowDateTime", _wt.DWORD), ("dwHighDateTime", _wt.DWORD)]

            def _ticks(ft):
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            _idle, _kernel, _user = _FILETIME(), _FILETIME(), _FILETIME()
            _ok = _ct.windll.kernel32.GetSystemTimes(
                _ct.byref(_idle), _ct.byref(_kernel), _ct.byref(_user))
            if _ok:
                _cur = (_ticks(_idle), _ticks(_kernel) + _ticks(_user))
                _prev = getattr(_windows_ctypes_fallback, "_prev", None)
                _windows_ctypes_fallback._prev = _cur
                if _prev:
                    _d_idle = _cur[0] - _prev[0]
                    _d_total = _cur[1] - _prev[1]
                    if _d_total > 0:
                        metrics["cpu_percent"] = round(
                            max(0.0, min(100.0, (1 - _d_idle / _d_total) * 100)), 1)
        except Exception:
            pass

    # 磁盘 / 临时目录：shutil 不需要 wmic，前面的采集若失败这里再试一次
    if not metrics.get("disk_percent"):
        try:
            _du = _shutil.disk_usage(os.path.abspath(os.sep))
            if _du.total > 0:
                metrics["disk_percent"] = round(_du.used / _du.total * 100, 1)
        except Exception:
            pass
    if not metrics.get("tmp_percent"):
        try:
            from src.domains.runtime.engine.os_adapter import temp_dir as _os_temp
            _tu = _shutil.disk_usage(_os_temp())
            if _tu.total > 0:
                metrics["tmp_percent"] = round(_tu.used / _tu.total * 100, 1)
        except Exception:
            pass


def _windows_metrics_fallback(metrics: dict) -> None:
    """Windows 指标兜底：psutil → Win32(ctypes) → PowerShell CIM，只补仍然为 0 的字段。

    为什么要这一步：`_collect_worker_metrics` 在 Windows 上用 `wmic` 采集，而 wmic 从
    Win11 24H2 / 新版 Windows Server 起已被移除。命令不存在时采集块按设计静默跳过，
    返回的 cpu/memory/disk/tmp 全是 0.0、spec 为空 —— 心跳正常但看板这台机器永远是
    「全 0」（实测：桌面 worker 心跳 0 分钟前、四项全 0、spec 空）。这里不再依赖 wmic：
    先试 psutil（requirements 已声明依赖），没有再用 PowerShell 的 CIM 接口。
    """
    want = [k for k in ("cpu_percent", "memory_percent", "disk_percent", "tmp_percent")
            if not metrics.get(k)]
    if not want and metrics.get("spec"):
        return

    # ① psutil：一次拿到 cpu/mem/disk/tmp 与机型串
    try:
        import psutil as _psu
    except Exception:
        _psu = None
    if _psu is not None:
        try:
            if "cpu_percent" in want:
                metrics["cpu_percent"] = round(float(_psu.cpu_percent(interval=None)), 1)
        except Exception:
            pass
        try:
            if "memory_percent" in want:
                metrics["memory_percent"] = round(float(_psu.virtual_memory().percent), 1)
        except Exception:
            pass
        try:
            if "disk_percent" in want:
                _du = _psu.disk_usage(os.path.abspath(os.sep))
                if _du.total > 0:
                    metrics["disk_percent"] = round(float(_du.percent), 1)
        except Exception:
            pass
        try:
            if "tmp_percent" in want:
                from src.domains.runtime.engine.os_adapter import temp_dir as _os_temp
                _tu = _psu.disk_usage(_os_temp())
                if _tu.total > 0:
                    metrics["tmp_percent"] = round(float(_tu.percent), 1)
        except Exception:
            pass
        if not metrics.get("spec"):
            try:
                _cores = _psu.cpu_count(logical=True) or os.cpu_count() or 0
                _total_b = int(_psu.virtual_memory().total or 0)
                if _cores and _total_b:
                    _gb = _total_b // (1024 ** 3)
                    metrics["spec"] = f"{_cores}C{_gb}G" if _gb >= 1 else \
                        f"{_cores}C{_total_b // (1024 ** 2)}MB"
            except Exception:
                pass
    if not [k for k in ("cpu_percent", "memory_percent", "disk_percent", "tmp_percent")
            if not metrics.get(k)] and metrics.get("spec"):
        return

    # ② ctypes + Win32 API：不依赖 psutil / PowerShell / wmic，进程内直接问内核
    _windows_ctypes_fallback(metrics)
    if not [k for k in ("cpu_percent", "memory_percent", "disk_percent", "tmp_percent")
            if not metrics.get(k)] and metrics.get("spec"):
        return

    # ③ PowerShell + CIM（psutil 与 Win32 都拿不到时才走）：**一次**调用取全部值，
    # 失败后按 _CIM_RETRY_COOLDOWN_SECONDS 退避，避免每轮心跳都串几次子进程超时。
    global _cim_cooldown_until
    import time as _time
    if _time.time() < _cim_cooldown_until:
        return
    import subprocess as _wsp

    def _ps_once(script: str) -> str:
        r = _wsp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                     capture_output=True, text=True, timeout=_CIM_TIMEOUT_SECONDS)
        return (r.stdout or "").strip()

    # 一段脚本输出四行：cpu% / mem% / 逻辑核数 / 物理内存字节；取不到的项留空
    _script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$cpu=(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average;"
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "if($os.TotalVisibleMemorySize -gt 0){"
        "$mem=round((1-($os.FreePhysicalMemory/$os.TotalVisibleMemorySize))*100,1)};"
        "$cores=(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum;"
        "$memb=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory;"
        "Write-Output \"$cpu`n$mem`n$cores`n$memb\"")
    try:
        _out = _ps_once(_script).splitlines()
    except Exception as e:  # noqa: BLE001 - 兜底失败不能让心跳挂
        log.debug(f"windows CIM metrics fallback failed (cooldown {int(_CIM_RETRY_COOLDOWN_SECONDS)}s): {e}")
        _cim_cooldown_until = _time.time() + _CIM_RETRY_COOLDOWN_SECONDS
        return
    _cim_cooldown_until = 0.0
    _vals = [(x or "").strip() for x in _out] + ["", "", "", ""]

    def _num(v):
        try:
            return float(str(v).strip())
        except Exception:
            return None

    if not metrics.get("cpu_percent"):
        _v = _num(_vals[0])
        if _v is not None:
            metrics["cpu_percent"] = round(_v, 1)
    if not metrics.get("memory_percent"):
        _v = _num(_vals[1])
        if _v is not None:
            metrics["memory_percent"] = round(_v, 1)
    if not metrics.get("spec"):
        _cores = _num(_vals[2])
        _mem_b = _num(_vals[3])
        if _cores and _mem_b:
            _gb = int(_mem_b) // (1024 ** 3)
            metrics["spec"] = f"{int(_cores)}C{_gb}G"
