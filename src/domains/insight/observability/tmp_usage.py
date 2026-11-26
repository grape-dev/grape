"""Issue #4445：节点 /tmp（tmpfs）水位与 janitor 清理量指标。

背景：worker 节点 ``/tmp`` 是 tmpfs（占用即钉住物理内存），长期被任务派生残留占满，
但 ``/metrics`` 上既没有 tmpfs 水位序列，也没有任何清理量序列——容量耗尽只能等 OOM
告警事后发现。本模块提供拉取时采样（``on_metrics_scrape`` 钩子调用，无新链路）：

- ``grape_tmpfs_used_ratio{mount}``             tmpfs 使用率（0~1）
- ``grape_tmp_janitor_removed_items_total``     本进程累计清理条目数
- ``grape_tmp_janitor_removed_bytes_total``     本进程累计释放字节数

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


def install(registry) -> None:
    """把 3 个 gauge 注册进指定 registry（observability.__init__ 调用，幂等）。"""
    global _tmp_ratio_gauge, _janitor_items_gauge, _janitor_bytes_gauge
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
    except Exception:
        return