"""Issue #4182：观测页不再轮询内网专用的 /health/realtime。

/public 用户访问 /health/realtime 会被 nginx 白名单直接 403。该调用对观测页是冗余的：
页面主数据来自受鉴权的 /api/dashboard/ops/overview。本测试锁住三件事：

1. 前端源码里不再出现 /health/realtime（否则又会每小时打 81 次 403）；
2. fallbackOps() 在拿不到 overview 时返回 null（未知），不再把失败画成 0；
3. 内网诊断能力保留：health_endpoints 的 loopback 门禁未被削弱。
"""
import os

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND_SRC = os.path.join(REPO_ROOT, "src", "api", "frontend")
OBS_VUE = os.path.join(
    FRONTEND_SRC, "app", "src", "views", "ObservabilityView.vue"
)


def _obs_source() -> str:
    with open(OBS_VUE, encoding="utf-8") as fh:
        return fh.read()


def test_observability_no_health_realtime_call():
    """观测页不能再请求 /health/realtime，也不应保留 rtData 状态。"""
    src = _obs_source()
    assert "/health/realtime" not in src
    assert "rtData" not in src


def test_no_frontend_source_calls_health_realtime():
    """全前端排查：任何 .vue/.js/.ts 都不能再直调内网专用 health 端点。"""
    hits = []
    for base, _dirs, files in os.walk(FRONTEND_SRC):
        for name in files:
            if not name.endswith((".vue", ".js", ".ts")):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                if "/health/realtime" in fh.read():
                    hits.append(os.path.relpath(path, REPO_ROOT))
    assert hits == [], hits


def test_refresh_obs_only_reads_ops_overview():
    """refreshObs 只保留受鉴权的 ops/overview 请求。"""
    src = _obs_source()
    assert "API()\n      .get('/dashboard/ops/overview' + repoParam)" in src or (
        ".get('/dashboard/ops/overview' + repoParam)" in src
    )
    assert "Promise.all" not in src.split("function refreshObs", 1)[1].split(
        "const realOps", 1
    )[0]


def test_fallback_ops_marks_all_sections_unknown():
    """fallbackOps 无入参，tasks/runtimes/workers 全为 null（未知不是 0）。"""
    src = _obs_source()
    body = src.split("function fallbackOps", 1)[1].split(
        "const opsDegraded", 1
    )[0]
    assert "function fallbackOps()" in src
    assert "tasks: null" in body
    assert "runtimes: null" in body
    assert "workers: null" in body
    assert "|| 0" not in body


def test_ops_failed_at_set_even_without_previous_snapshot():
    """overview 失败必须可见（置 opsFailedAt），不能仅在已有快照时才提示。"""
    src = _obs_source()
    body = src.split("function refreshObs", 1)[1].split("const realOps", 1)[0]
    assert "opsFailedAt.value = new Date().toLocaleTimeString('zh-CN')" in body
    assert "else if (opsData.value)" not in body


def test_task_kpis_use_kpi_val_for_unknown():
    """任务 KPI 用 kpiVal 表达未知，不能以 || 0 冒充 0。"""
    src = _obs_source()
    assert "kpiVal(taskStats.total)" in src
    assert "kpiVal(taskStats.completed)" in src
    assert "kpiVal(taskStats.in_progress)" in src
    assert "kpiVal(taskStats.failed)" in src
    assert "taskStats.total || 0" not in src
    assert "taskStats.completed || 0" not in src


def test_internal_health_realtime_loopback_gate_preserved():
    """Out of scope：后端 /health/realtime 不改，内网 loopback 仍可访问。"""
    path = os.path.join(REPO_ROOT, "src", "api", "health_endpoints.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert '@app.get("/health/realtime")' in src
    assert "_is_loopback" in src