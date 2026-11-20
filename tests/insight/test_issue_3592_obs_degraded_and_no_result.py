"""Issue #3592：把「未知」和「空」区分开，别把失败画成 0。

三处观测缺陷，共同的病是同一个值同时表示「没有数据」和「数据是 0」：

1. 前端 worker 卡片：overview 拿不到就退回 fallbackOps，而 fallback 的
   workers 是硬编码 0 —— 页面于是显示「0 台在线」，看起来像平台上没有 worker。
2. 后端 overview：各段是 ``except Exception: pass``，查询失败时计数器留 0，
   一条日志都不写。事后无法区分「这台机器真没有 worker」与「这次查询失败」。
3. worker 侧 ``_result = None`` 同时表达「真超时」和「执行函数返回 None」，
   后者被上报成 ``task timed out after 7200.0s``（实测 2 秒完成的任务）。

这里锁住修复后的契约：degraded 段名、超时哨兵、非超时的独立错误消息。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _user():
    return {"id": 1, "username": "admin", "org_id": 0, "role": "admin"}


def test_overview_reports_degraded_workers(monkeypatch):
    """workers 查询失败 → total=0 但 degraded 里必须点名，且不抛。"""
    from src.domains.insight.routes import dashboard
    from src.domains.runtime import public as rt_public

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated worker query failure")

    monkeypatch.setattr(rt_public, "dashboard_worker_rows", _boom, raising=False)

    resp = dashboard.get_ops_overview(repo="", user=_user())

    assert "workers" in resp["degraded"], resp["degraded"]
    # 远端 worker 行必须为空（只剩本机直连那条 127.0.0.1 兜底行）
    remote = [w for w in resp["workers"]["list"] if w.get("last_ip") != "127.0.0.1"]
    assert remote == [], remote


def test_overview_degraded_is_empty_on_happy_path():
    """正常路径不能乱标 degraded，否则前端的「未知」提示会一直挂着。"""
    from src.domains.insight.routes import dashboard

    resp = dashboard.get_ops_overview(repo="", user=_user())
    assert resp["degraded"] == [], resp["degraded"]
    assert resp["workers"]["total"] >= 0


def test_overview_reports_degraded_runtimes(monkeypatch):
    """runtimes 段失败同样要点名，不能只报 workers。"""
    from src.domains.insight.routes import dashboard
    from src.domains.runtime import public as rt_public

    orig = rt_public.dashboard_worker_rows
    monkeypatch.setattr(rt_public, "dashboard_worker_rows", orig, raising=False)

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated runtime query failure")

    # runtime 列表走 dashboard.py 里的 _rt_dash.dashboard_runtime_rows
    monkeypatch.setattr(rt_public, "dashboard_runtime_rows", _boom, raising=False)
    resp = dashboard.get_ops_overview(repo="", user=_user())
    assert "runtimes" in resp["degraded"], resp["degraded"]
    assert resp["runtimes"]["total"] == 0


def test_timeout_sentinel_is_distinct_from_none():
    """agent.py 必须用哨兵表达超时，不能再用 None。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "..", "src", "domains", "runtime",
                            "engine", "agent.py"), encoding="utf-8").read()

    assert "_TIMED_OUT = object()" in src
    assert "elif _result is _TIMED_OUT:" in src
    # 超时消息仍然保留 M3 观测依赖的签名
    assert 'f"task timed out after {task_timeout}s"' in src
    # 执行函数返回 None 时必须走独立消息，不能再冒充超时
    assert "task produced no result" in src
    # 旧的「None 即超时」写法不能回潮
    assert "elif _result is None:\n                            # 超时" not in src


def test_frontend_fallback_marks_workers_unknown():
    """前端 fallback 不能再给 workers 硬编码 0。"""
    here = os.path.dirname(os.path.abspath(__file__))
    vue = open(os.path.join(here, "..", "..", "src", "api", "frontend", "app",
                            "src", "views", "ObservabilityView.vue"),
               encoding="utf-8").read()
    assert "workers: { total: 0, online: 0, offline: 0, list: [] }" not in vue
    assert "workers: null" in vue
    assert "kpiVal(" in vue
    assert "opsFailedAt" in vue


@pytest.mark.parametrize("key", ["obs_no_data", "obs_stale_since", "obs_partial"])
def test_locale_keys_exist(key):
    """新增的提示文案必须在 zh/en 两份字典里都有，否则界面显示原始 key。"""
    import json

    here = os.path.dirname(os.path.abspath(__file__))
    for loc in ("zh", "en"):
        p = os.path.join(here, "..", "..", "src", "api", "frontend", "shell",
                         "locales", loc, "observability.json")
        data = json.load(open(p, encoding="utf-8"))
        assert key in data, f"{loc} missing {key}"