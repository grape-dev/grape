"""Issue #4445：节点 /tmp（tmpfs）水位与 janitor 清理量指标。

背景：worker 节点 ``/tmp`` 是 tmpfs（占用即钉住物理内存），长期被任务派生残留占满，
但 ``/metrics`` 上既没有 tmpfs 水位序列，也没有任何清理量序列——容量耗尽只能等 OOM
告警事后发现。本模块提供两类通路，都是拉取时采样（``on_metrics_scrape`` 钩子调用，
无新链路）：

1. 控制面进程本机（scrape 时 ``shutil.disk_usage`` / 进程内累计量）：
   - ``grape_tmpfs_used_ratio{mount}``             tmpfs 使用率（0~1）
   - ``grape_tmp_janitor_removed_items_total``     本进程累计清理条目数
   - ``grape_tmp_janitor_removed_bytes_total``     本进程累计释放字节数
2. worker 节点（心跳落 ``runtime_credentials`` 后由控制面按 host 采样，同 Issue #3773
   的 ``grape_acp_state_db_*`` 通道）：
   - ``grape_node_tmp_used_ratio{host}``                  节点 /tmp 使用率（0~1）
   - ``grape_node_tmp_janitor_removed_items_total{host}``  节点累计清理条目数
   - ``grape_node_tmp_janitor_removed_bytes_total{host}``  节点累计释放字节数

通路 2 是必要的：worker 最小依赖不含 ``prometheus_client``（Issue #2923），远端 agent
进程也**没有** ``/metrics`` 端点，只有心跳能把节点数据带回控制面。若只依赖通路 1，
远端节点的两条 /tmp 告警永远不会触发。

``install(registry)`` 与 kernel metrics / process_memory 同模式，由
observability.__init__ 注册；worker 包无 prometheus_client 时 Gauge 为 no-op 桩。
采样路径所有异常吞掉，埋点绝不拖垮 /metrics。
"""
import os
import shutil

try:
    from prometheus_client import Gauge
except Exception:  # pragma: no cover - worker 包路径
    from src.domains.insight.observability import Gauge  # noqa: F401  (no-op 桩)

_tmp_ratio_gauge = None
_janitor_items_gauge = None
_janitor_bytes_gauge = None
_node_ratio_gauge = None
_node_janitor_items_gauge = None
_node_janitor_bytes_gauge = None


def install(registry) -> None:
    """把 6 个 gauge 注册进指定 registry（observability.__init__ 调用，幂等）。"""
    global _tmp_ratio_gauge, _janitor_items_gauge, _janitor_bytes_gauge
    global _node_ratio_gauge, _node_janitor_items_gauge, _node_janitor_bytes_gauge
    if _tmp_ratio_gauge is not None:
        return
    _tmp_ratio_gauge = Gauge(
        "grape_tmpfs_used_ratio",
        "tmpfs usage ratio of the node temporary mount (0~1), sampled at scrape time.",
        ["mount"], registry=registry)
    _janitor_items_gauge = Gauge(
        "grape_tmp_janitor_removed_items_total",
        "Cumulative number of /tmp task-derived residual entries removed by the janitor.",
        registry=registry)
    _janitor_bytes_gauge = Gauge(
        "grape_tmp_janitor_removed_bytes_total",
        "Cumulative bytes freed by the /tmp task-derived residual janitor.",
        registry=registry)
    _node_ratio_gauge = Gauge(
        "grape_node_tmp_used_ratio",
        "tmp mount usage ratio (0~1) reported by a worker node heartbeat.",
        ["host"], registry=registry)
    _node_janitor_items_gauge = Gauge(
        "grape_node_tmp_janitor_removed_items_total",
        "Cumulative /tmp residual entries removed by the janitor on a worker node "
        "(reported by heartbeat).",
        ["host"], registry=registry)
    _node_janitor_bytes_gauge = Gauge(
        "grape_node_tmp_janitor_removed_bytes_total",
        "Cumulative bytes freed by the /tmp residual janitor on a worker node "
        "(reported by heartbeat).",
        ["host"], registry=registry)


def tmp_mount() -> str:
    """被观测的临时挂载点：GRAPE_TMP_MOUNT 优先，缺省系统临时目录。"""
    return (os.environ.get("GRAPE_TMP_MOUNT") or "/tmp").strip() or "/tmp"


def collect_tmp_usage() -> None:
    """拉取时采样入口：刷新 tmpfs 水位与 janitor 累计量。异常一律吞掉。"""
    if _tmp_ratio_gauge is None:
        return
    try:
        mount = tmp_mount()
        try:
            usage = shutil.disk_usage(mount)
            if usage.total > 0:
                _tmp_ratio_gauge.labels(mount=mount).set(usage.used / usage.total)
        except Exception:
            pass
        try:
            from src.domains.runtime.engine.cleanup import tmp_janitor_metrics_snapshot
            snap = tmp_janitor_metrics_snapshot()
            _janitor_items_gauge.set(int(snap.get("tmp_janitor_removed_items_total", 0)))
            _janitor_bytes_gauge.set(float(snap.get("tmp_janitor_removed_bytes_total", 0.0)))
        except Exception:
            pass
        _collect_node_tmp_metrics()
    except Exception:
        return


def _collect_node_tmp_metrics() -> None:
    """按 host 采样 worker 心跳上报的 /tmp 水位与 janitor 累计量。

    同一 host 可能有多行凭据（worker 上报真实值 + 控制面进程的 0 值行），无 ORDER BY
    的逐行 set 会被后一行覆盖 → 先按 host 取 ``tmp_percent`` 最大的一行（同 #3773）。
    """
    if _node_ratio_gauge is None or _node_janitor_items_gauge is None \
            or _node_janitor_bytes_gauge is None:
        return
    try:
        from src.domains.runtime import public as _rt_public
        rows = _rt_public.read_all_any("runtime_credentials_tmp_rows")
    except Exception:
        return
    best: dict[str, tuple] = {}
    for row in rows or []:
        values = list(row) + [None] * 5
        device_name, name, tmp_percent, items, freed = values[:5]
        host = str(device_name or name or "unknown")
        try:
            rank = float(tmp_percent or 0)
        except (TypeError, ValueError):
            rank = 0.0
        current = best.get(host)
        if current is None or rank > current[0]:
            best[host] = (rank, items, freed)
    for host, (_percent, items, freed) in best.items():
        ratio = min(max(float(_percent or 0) / 100.0, 0.0), 1.0)
        _node_ratio_gauge.labels(host=host).set(ratio)
        _node_janitor_items_gauge.labels(host=host).set(float(items or 0))
        _node_janitor_bytes_gauge.labels(host=host).set(float(freed or 0))