"""Issue #4203：进程级内存指标（worker 主进程 + 引擎子进程 RSS + cgroup 限额）。

背景：master-004 24h 内 4 次 OOM kill，杀进程前节点 MemAvailable 39.5% —— 进程/cgroup 级
内存失控，但 /metrics 上没有任何进程级 RSS 序列，「看不见」也就无法设限与告警。

本模块提供拉取时采样（on_metrics_scrape 钩子调用，无新链路）：
- grape_process_memory_rss_bytes{process}      /proc/self/status VmRSS
- grape_process_memory_limit_bytes{process}     cgroup v2 memory.max / v1 memory.limit_in_bytes
- grape_process_memory_ratio{process}           rss/limit（无限额时 0）
- grape_process_memory_limit_unbounded{process} 无 cgroup 限额 = 1（采集分支可见）
- grape_engine_child_rss_bytes{process,engine}  活跃引擎子进程按 backend 聚合 RSS
  （来源 agents/executor._active_procs —— 与停机 terminate_active_agents 同一注册表）

无 cgroup 限额（裸进程/开发机）时 limit=0、ratio=0、unbounded=1，分支在指标上可辨，
而不是把「没有限额」伪装成 0 字节。

install(registry) 与 kernel metrics 同模式，由 observability.__init__ 注册；
worker 包无 prometheus_client 时 Gauge 为 no-op 桩，模块仍可导入。
"""
import os
import threading

try:
    from prometheus_client import Gauge
except Exception:  # pragma: no cover - worker 包路径
    from src.domains.insight.observability import Gauge  # noqa: F401  (no-op 桩)

# 采样中所有异常吞掉（埋点绝不拖垮 /metrics）；文件读取上限防病态 status 行
_STATUS_MAX_BYTES = 8192
# v1 的占位上限（2^40+ 级）视为无限额
_V1_PLACEHOLDER_LIMIT = 1 << 40

_rss_gauge = None
_limit_gauge = None
_ratio_gauge = None
_unbounded_gauge = None
_engine_child_gauge = None

_sampling_lock = threading.Lock()
# 本轮采样出现过的 engine 标签：消失后必须显式归零（对齐 _apply_source_gauges 的教训——
# 只写「本轮查到的」会让 gauge 永远保留上一次的值）
_known_engines: set[str] = set()


def process_role() -> str:
    """process 标签值：GRAPE_PROCESS_ROLE（worker/event/insight…），缺省 server。"""
    return (os.environ.get("GRAPE_PROCESS_ROLE") or "server").strip().lower() or "server"


def install(registry) -> None:
    """把 5 个 gauge 注册进指定 registry（observability.__init__ 调用，幂等）。"""
    global _rss_gauge, _limit_gauge, _ratio_gauge, _unbounded_gauge, _engine_child_gauge
    if _rss_gauge is not None:
        return
    _rss_gauge = Gauge(
        "grape_process_memory_rss_bytes",
        "Resident set size of this process, sampled from /proc/<pid>/status VmRSS.",
        ["process"], registry=registry)
    _limit_gauge = Gauge(
        "grape_process_memory_limit_bytes",
        "Effective cgroup memory limit for this process (v2 memory.max / v1 "
        "memory.limit_in_bytes); 0 when no limit is set.",
        ["process"], registry=registry)
    _ratio_gauge = Gauge(
        "grape_process_memory_ratio",
        "RSS / cgroup limit; 0 when no limit is set.",
        ["process"], registry=registry)
    _unbounded_gauge = Gauge(
        "grape_process_memory_limit_unbounded",
        "1 when this process has no cgroup memory limit (bare/dev host), else 0.",
        ["process"], registry=registry)
    _engine_child_gauge = Gauge(
        "grape_engine_child_rss_bytes",
        "Aggregated RSS of live engine child processes spawned by this process, "
        "grouped by backend (from the executor process registry).",
        ["process", "engine"], registry=registry)


def read_vm_rss_bytes(status_path: str) -> int:
    """解析 /proc/<pid>/status 的 VmRSS 行，返回字节；不可读/缺失返回 0。"""
    try:
        with open(status_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # 形如 "VmRSS:\t 123456 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
                    return 0
    except (OSError, ValueError):
        return 0
    return 0


def _split_cgroup_entry(entry: str):
    """解析 /proc/self/cgroup 一行 → (hierarchy, controllers, path)；坏行返回 None。"""
    parts = entry.split(":", 2)
    return tuple(parts) if len(parts) == 3 else None


def read_cgroup_memory_limit(proc_root: str = "/proc",
                             sys_fs: str = "/sys/fs/cgroup") -> int:
    """读取本进程生效的 cgroup 内存限额（字节）；无 cgroup / 无限额返回 0。

    cgroup v2：/proc/self/cgroup 的 `0::<path>` → /sys/fs/cgroup/<path>/memory.max
    （根路径直接 /sys/fs/cgroup/memory.max）；`max` 占位 = 无限额。
    cgroup v1：`<n>:memory:<path>` → /sys/fs/cgroup/memory/<path>/memory.limit_in_bytes；
    v1 占位上限（≥ 2^40）视为无限额。

    Issue #4203 复审：混合层级下 `0::` 条目存在但 v2 的 memory.max 缺失/为 `max`
    （限额实际挂在 v1 控制器上）时，v2 解析为 0 **不等于**无限额——必须继续回退
    v1 再判定 unbounded，否则启动自检误报 unbounded、ratio 告警永不触发。
    """
    try:
        with open(os.path.join(proc_root, "self", "cgroup"), "r",
                  encoding="utf-8", errors="replace") as f:
            entries = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return 0
    for entry in entries:
        parts = _split_cgroup_entry(entry)
        if parts is None or parts[0] != "0":
            continue
        path = parts[2].strip("/")
        limit_path = os.path.join(sys_fs, path, "memory.max") if path \
            else os.path.join(sys_fs, "memory.max")
        value = _parse_limit_file(limit_path)
        if value > 0:
            return value
        # v2 未给出限额（文件缺失 / `max` 占位）→ 继续回退 v1 控制器
        break
    # v1 回退（memory 子系统）
    for entry in entries:
        parts = _split_cgroup_entry(entry)
        if parts is None or "memory" not in parts[1].split(","):
            continue
        path = parts[2].strip("/")
        limit_path = os.path.join(sys_fs, "memory", path, "memory.limit_in_bytes") \
            if path else os.path.join(sys_fs, "memory", "memory.limit_in_bytes")
        value = _parse_limit_file(limit_path)
        if 0 < value < _V1_PLACEHOLDER_LIMIT:
            return value
        return 0
    return 0


def _parse_limit_file(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(_STATUS_MAX_BYTES).strip()
        if not raw or raw == "max":
            return 0
        return int(raw)
    except Exception:
        return 0


def _sample_engine_children(pid_status_dir: str = "/proc") -> dict[str, int]:
    """按 backend 聚合活跃引擎子进程 RSS；注册表不可用/为空返回 {}。"""
    try:
        from src.domains.runtime.agents.executor import _active_procs, _active_procs_lock
    except Exception:
        return {}
    per_engine: dict[str, int] = {}
    with _active_procs_lock:
        procs = list(_active_procs)
    for proc in procs:
        # 注册表里是 Popen 对象；用 getattr 取值（不 try/except），坏条目直接跳过
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            continue
        rss = read_vm_rss_bytes(os.path.join(pid_status_dir, str(pid), "status"))
        if rss <= 0:
            continue
        backend = (getattr(proc, "_grape_backend", "") or "unknown").strip().lower() or "unknown"
        per_engine[backend] = per_engine.get(backend, 0) + rss
    return per_engine


def collect_process_memory(proc_root: str = "/proc",
                           sys_fs: str = "/sys/fs/cgroup") -> None:
    """拉取时采样入口：刷新 5 个 gauge。任何异常吞掉，绝不拖垮 /metrics。"""
    if _rss_gauge is None:
        return
    try:
        with _sampling_lock:
            role = process_role()
            rss = read_vm_rss_bytes(os.path.join(proc_root, "self", "status"))
            limit = read_cgroup_memory_limit(proc_root=proc_root, sys_fs=sys_fs)
            _rss_gauge.labels(process=role).set(rss)
            _limit_gauge.labels(process=role).set(limit)
            if limit > 0:
                _ratio_gauge.labels(process=role).set(rss / limit)
                _unbounded_gauge.labels(process=role).set(0)
            else:
                # 无 cgroup 限额（裸进程/开发机/容器未配 memory.max）：
                # limit/ratio 上报 0 并显式标注 unbounded，分支可辨
                _ratio_gauge.labels(process=role).set(0)
                _unbounded_gauge.labels(process=role).set(1)
            per_engine = _sample_engine_children(pid_status_dir=proc_root)
            _known_engines.update(set(per_engine))
            for engine in _known_engines:
                _engine_child_gauge.labels(process=role, engine=engine).set(
                    per_engine.get(engine, 0))
    except Exception:
        return


def effective_memory_limit_report(proc_root: str = "/proc",
                                  sys_fs: str = "/sys/fs/cgroup") -> str:
    """启动自检用的可读报告行（限额字节 + cgroup 路径 + 进程角色）。"""
    role = process_role()
    limit = read_cgroup_memory_limit(proc_root=proc_root, sys_fs=sys_fs)
    if limit > 0:
        return (f"[memory-limit] process={role} cgroup limit={limit} bytes "
                f"({limit / (1024 ** 3):.2f} GiB)")
    return f"[memory-limit] process={role} cgroup limit=unbounded (no memory.max set)"


def log_effective_memory_limit() -> None:
    """worker/服务 lifespan 首段调用：打印生效 cgroup 内存限额，无限额打 WARNING。

    Issue #4203：杜绝「限额来源不可见」—— 004 上 OOM 后只能靠 dmesg 反查受害者
    归属哪个 unit。启动时把生效值打进日志，值班一眼可查。
    """
    from src.kernel.config import log
    try:
        report = effective_memory_limit_report()
    except Exception as e:
        log.warning(f"[memory-limit] selfcheck failed: {e}")
        return
    if "unbounded" in report:
        log.warning(f"{report} — set systemd MemoryMax/MemoryHigh (deploy/systemd/"
                    f"grape-worker-server.service) to bound RSS growth")
    else:
        log.info(report)
