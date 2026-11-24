"""Issue #3749：trace 中继可观测性（禁用态打点 + 启动自检 + 告警可用）。

背景：主控未注入 `OTLP_RELAY_TARGET` 时 `POST /api/runtime/traces` 恒 503
`trace relay disabled`，且该分支**完全不打点** → 「503 持续 5min 告警」无从生效；
部署清单也没有该变量，实例重启后仍是禁用态。

基线 `arch-weekly-11-ljt` 已修复「同名 Counter 重复注册被 except 吞成 None」并把
目标值改为按请求现读 env（见 `tests/runtime/test_trace_relay_env_and_metrics.py`）。
因此本文件只锁定 #3749 的增量契约：

1. 禁用态按 `status="503"` 计数（否则「没配」永远进不了指标/告警）；
2. 转发成功/上游不可达按 status 计数走统一入口，指标样本真实可见；
3. 启动自检输出一次 WARN/INFO、误配 `/v1/traces` 后缀时显式告警，且占住限频窗口
   使首个请求不再重复 WARN；文案不硬编码单一部署路径；
4. 两个承接中继端点的进程入口都实际调用自检（AST 断言 import + 调用 + try 包裹）。

不依赖外网 / LGTM：上游用假 AsyncClient，中继侧用 TestClient + 依赖覆盖。
"""
import ast
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

_LOG = "devops-bot"
_ROUTER = "src/domains/runtime/routes/runtime.py"


def _relay_app(monkeypatch, target: str = ""):
    from src.domains.runtime.routes import runtime as rt

    # 目标值按请求现读 env（基线契约，勿改回模块常量）
    monkeypatch.setenv("OTLP_RELAY_TARGET", target)
    monkeypatch.setattr(rt, "_relay_warn_ts", 0.0, raising=True)
    app = FastAPI()
    app.include_router(rt.router)
    app.dependency_overrides[rt._require_credential] = lambda: {"id": 1, "runtime_id": 1}
    return app


def _sample(status: str) -> float:
    from src.domains.insight.observability import registry as obs_registry

    val = obs_registry.get_sample_value("grape_trace_relay_requests_total",
                                       {"status": status})
    return float(val or 0.0)


# ===== 1. 指标可上报 =====

def test_relay_counter_is_registered():
    """指标必须真实存在（历史上重复注册把 Counter 置成 None，样本整体缺失）。"""
    from src.domains.runtime.routes import runtime as rt

    assert rt._relay_total is not None, (
        "grape_trace_relay_requests_total 未注册成功——检查是否存在重复注册"
        "（DuplicateTimeseries 被 except 吞掉）")


# ===== 2. 禁用态 503 打点 =====

def test_disabled_relay_counts_503(monkeypatch):
    """未配置 OTLP_RELAY_TARGET：每次请求都计 status="503"（配置缺失可被告警捕获）。"""
    before = _sample("503")
    client = TestClient(_relay_app(monkeypatch, target=""))

    first = client.post("/api/runtime/traces", content=bytes([0, 1]))
    second = client.post("/api/runtime/traces", content=bytes([0, 1]))

    assert first.status_code == 503 and second.status_code == 503
    assert first.headers.get("x-grape-trace-relay") == "disabled"
    total = _sample("503") - before
    # 同一进程内其它用例也可能落到 503 桶，这里只断言「每次请求都计」而非绝对值
    assert total >= 2


def test_forwarded_relay_counts_upstream_status(monkeypatch):
    """转发成功（上游 200）：计 status="200"，禁用标记头不出现，URL 不重复后缀。"""
    import httpx

    seen = {}

    class _Resp:
        status_code = 200
        content = b"ok"
        headers = {"content-type": "application/x-protobuf"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            seen["url"] = url
            seen["body"] = content
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    before = _sample("200")
    target = "http://relay-upstream.test:4318"
    client = TestClient(_relay_app(monkeypatch, target))
    r = client.post("/api/runtime/traces", content=bytes([0, 1]))

    assert r.status_code == 200
    assert r.headers.get("x-grape-trace-relay") is None
    assert seen["url"] == target + "/v1/traces", "中继应拼 /v1/traces 后缀且不重复"
    assert seen["body"] == bytes([0, 1])
    assert _sample("200") - before >= 1


def test_unreachable_relay_counts_and_returns_502(monkeypatch):
    """上游不可达：502 + status="relay_unreachable"（与配置缺失的 503 区分得开）。"""
    import httpx

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("connect refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    before = _sample("relay_unreachable")
    client = TestClient(_relay_app(monkeypatch, "http://relay-upstream.test:4318"))
    r = client.post("/api/runtime/traces", content=bytes([0, 1]))

    assert r.status_code == 502
    assert _sample("relay_unreachable") - before >= 1


# ===== 3. 启动自检 =====

def _reset_selfcheck(monkeypatch, target: str):
    from src.domains.runtime.routes import runtime as rt

    monkeypatch.setenv("OTLP_RELAY_TARGET", target)
    monkeypatch.setattr(rt, "_relay_selfcheck_done", False, raising=True)
    monkeypatch.setattr(rt, "_relay_warn_ts", 0.0, raising=True)
    return rt


def test_selfcheck_warns_once_when_unconfigured(monkeypatch, caplog):
    """空配置：启动期 WARN 一次（含变量名/503 语义/配置方式），重复调用不刷屏。"""
    rt = _reset_selfcheck(monkeypatch, "")

    with caplog.at_level(logging.INFO, logger=_LOG):
        info1 = rt.trace_relay_selfcheck()
        info2 = rt.trace_relay_selfcheck()

    assert info1 == {"enabled": False, "upstream": "", "endpoint": "",
                     "misconfigured": False}
    assert info2 == info1, "自检结果应幂等"

    warn = [r for r in caplog.records
            if r.levelno == logging.WARNING and "OTLP_RELAY_TARGET" in r.getMessage()]
    assert len(warn) == 1, [r.getMessage() for r in caplog.records]
    message = warn[0].getMessage()
    assert "503" in message
    assert "/etc/grape/grape.env" not in message, "文案不应硬编码单一部署路径"
    assert "/v1/traces" in message, "文案需说明变量不含 /v1/traces"


def test_selfcheck_shares_throttle_with_request_path(monkeypatch, caplog):
    """自检已提示过 → 首个禁用态请求不再重复 WARN（共用 10 分钟限频窗口）。"""
    rt = _reset_selfcheck(monkeypatch, "")

    app = FastAPI()
    app.include_router(rt.router)
    app.dependency_overrides[rt._require_credential] = lambda: {"id": 1, "runtime_id": 1}

    with caplog.at_level(logging.INFO, logger=_LOG):
        rt.trace_relay_selfcheck()
        first = TestClient(app).post("/api/runtime/traces", content=b"x")

    assert first.status_code == 503
    warn = [r for r in caplog.records
            if r.levelno == logging.WARNING and "OTLP_RELAY_TARGET" in r.getMessage()]
    assert len(warn) == 1, [r.getMessage() for r in caplog.records]


def test_selfcheck_logs_effective_endpoint_when_configured(monkeypatch, caplog):
    """已配置：INFO 打印生效端点（<target>/v1/traces，与请求路径同口径）。"""
    rt = _reset_selfcheck(monkeypatch, "http://lgtm.example:4318/")

    with caplog.at_level(logging.INFO, logger=_LOG):
        info = rt.trace_relay_selfcheck()

    assert info == {"enabled": True,
                    "upstream": "http://lgtm.example:4318/",
                    "endpoint": "http://lgtm.example:4318/v1/traces",
                    "misconfigured": False}
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("http://lgtm.example:4318/v1/traces" in m for m in msgs), msgs


def test_selfcheck_warns_on_suffix_misconfiguration(monkeypatch, caplog):
    """变量误带 `/v1/traces` → WARN（否则上游 404 静默绕过 503 告警）。"""
    rt = _reset_selfcheck(monkeypatch, "http://lgtm.example:4318/v1/traces")

    with caplog.at_level(logging.INFO, logger=_LOG):
        info = rt.trace_relay_selfcheck()

    assert info["misconfigured"] is True
    assert info["endpoint"] == "http://lgtm.example:4318/v1/traces/v1/traces"
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn) == 1, warn
    assert "/v1/traces" in warn[0] and "404" in warn[0], warn[0]


# ===== 4. 入口接线（承接中继端点的两个进程；结构化断言实际调用） =====

def _startup_calls(rel: str) -> list:
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))

    imported = {a.name for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)
                and (n.module or "").endswith("routes.runtime")
                for a in n.names}
    assert "trace_relay_selfcheck" in imported, f"{rel} 未导入 trace_relay_selfcheck"

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "trace_relay_selfcheck"]
    assert calls, f"{rel} 启动期未实际调用 trace_relay_selfcheck（仅注释提及不算）"

    guarded = [node for node in ast.walk(tree)
               if isinstance(node, ast.Try)
               for sub in ast.walk(node)
               if isinstance(sub, ast.Call)
               and isinstance(sub.func, ast.Name)
               and sub.func.id == "trace_relay_selfcheck"]
    assert guarded, "自检失败不应阻断启动（需 try/except 包裹）"
    return calls


@pytest.mark.parametrize("rel", [
    "src/api/main.py",                  # 单进程模式
    "src/api/grape_worker_server.py",   # 四进程：runtime 全量路由
])
def test_selfcheck_wired_into_startup(rel):
    assert _startup_calls(rel)


def test_router_module_holds_single_relay_counter_registration():
    """同名 Counter 只允许构造一次（重复注册会让指标整体失效）。"""
    root = Path(__file__).resolve().parents[2]
    text = (root / _ROUTER).read_text(encoding="utf-8")
    assert text.count('_PC("grape_trace_relay_requests_total"') == 1