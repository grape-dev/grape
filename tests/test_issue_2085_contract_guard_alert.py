"""Issue #2085：worker contract_guard 失败信号外发（接外部告警）。

Spec 切片：guard 检出契约退化（degraded）或缓存重建失败（rebuild_failed）时，
在 say() 日志之外外发结构化告警信号（可注入 alert_sink，默认行为保持不变），
经 worker→主控上报落 src/domains/insight/observability 指标出口（grape_contract_guard_failures_total），
deploy/alerts.yml 新增 GrapeContractGuardFailed 规则可消费。
warn-only 启动语义与 GRAPE_CONTRACT_GUARD_DISABLED 逃生口不破坏。

seam（公开接口）：
- run_startup_check(..., alert_sink=...) 注入 sink 的启动检查入口；
- http_alert_sink(...) worker→主控信号上报（死地址落本地兜底文件）；
- POST /api/runtime/contract-guard/signal 主控承接端点；
- record_contract_guard_failure(...) src/domains/insight/observability 指标出口；
- deploy/alerts.yml GrapeContractGuardFailed 规则与指标名一致性。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.domains.runtime.engine import contract_guard as guard  # noqa: E402


def _full_schema(version: str = "2.2.0") -> dict:
    """构造健康全量契约（六组业务前缀 + 端点数达基线；常量 import 权威定义）。"""
    paths = {}
    for g in guard.REQUIRED_PATH_PREFIXES:
        paths[f"{g}ping"] = {"get": {}}
    for i in range(max(0, guard.DEFAULT_MIN_ENDPOINTS - len(paths))):
        paths[f"/api/x{i}"] = {"get": {}}
    return {"info": {"title": "Agentic DevOps API", "version": version}, "paths": paths}


def _runtime_only_schema() -> dict:
    """构造退化 runtime-only 契约（54 endpoints，仅 /api/runtime/*）。"""
    return {
        "info": {"title": "Agentic DevOps API", "version": "2.2.0"},
        "paths": {f"/api/runtime/ep{i}": {"post": {}} for i in range(54)},
    }


# ---------- 切片①：run_startup_check 注入 alert_sink 外发失败信号 ----------

def test_guard_emits_degraded_signal_to_injected_sink(tmp_path):
    """检出退化即外发 outcome=degraded 信号（重建成功也发：两类事件分开计数）。"""
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_runtime_only_schema()), encoding="utf-8")
    signals = []
    out = guard.run_startup_check(config_dir=d, base_url="http://s",
                                  fetch=lambda url: _full_schema(), log=lambda m: None,
                                  alert_sink=signals.append)
    assert out["status"] == "rebuilt"
    assert [s["outcome"] for s in signals] == ["degraded"]
    assert "runtime-only" in signals[0]["detail"]


def test_guard_emits_rebuild_failed_signal_separately(tmp_path):
    """重建失败在退化信号之外单独外发 outcome=rebuild_failed（分开标识）。"""
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_runtime_only_schema()), encoding="utf-8")
    signals = []

    def _boom(url):
        raise OSError("connection refused")

    out = guard.run_startup_check(config_dir=d, base_url="http://s", fetch=_boom,
                                  log=lambda m: None, alert_sink=signals.append)
    assert out["status"] == "failed"
    assert [s["outcome"] for s in signals] == ["degraded", "rebuild_failed"]
    assert "connection refused" in signals[1]["detail"]


def test_guard_no_signal_when_cache_healthy(tmp_path):
    """健康态不外发任何信号（默认行为保持不变）。"""
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_full_schema()), encoding="utf-8")
    signals = []
    out = guard.run_startup_check(config_dir=d, fetch=lambda url: _full_schema(),
                                  log=lambda m: None, alert_sink=signals.append)
    assert out["status"] == "ok"
    assert signals == []


def test_guard_default_no_sink_keeps_behavior(tmp_path):
    """不注入 alert_sink（默认 None）时行为与 #1616 完全一致：无外发、返回口径不变。"""
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_runtime_only_schema()), encoding="utf-8")
    out = guard.run_startup_check(config_dir=d, base_url="http://s",
                                  fetch=lambda url: _full_schema(), log=lambda m: None)
    assert out["status"] == "rebuilt"
    assert out["action"] == "rebuild"


def test_sink_error_never_breaks_startup(tmp_path):
    """sink 自身故障按 warn-only 处理：不抛异常、不改变返回口径（逃生语义不破坏）。"""
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_runtime_only_schema()), encoding="utf-8")

    def _bad_sink(signal):
        raise RuntimeError("alert sink exploded")

    warnings = []
    out = guard.run_startup_check(config_dir=d, base_url="http://s",
                                  fetch=lambda url: _full_schema(),
                                  log=warnings.append, alert_sink=_bad_sink)
    assert out["status"] == "rebuilt"  # 启动语义不受告警链路影响
    assert any("alert" in w for w in warnings)  # sink 故障可诊断（留日志）


def test_guard_alert_outcomes_cover_both_failure_kinds():
    """退化检出/重建失败两类口径都在 ALERT_OUTCOMES 权威常量内（供漂移看护）。"""
    assert guard.ALERT_OUTCOMES == ("degraded", "rebuild_failed")


# ---------- 切片②：src/observability 指标出口承接 guard 失败信号 ----------

def test_record_contract_guard_failure_exposes_prometheus_metric():
    """承接点 record_contract_guard_failure → /metrics 出口出现
    grape_contract_guard_failures_total{outcome=...}（两类 outcome 分开计数）。"""
    from prometheus_client import generate_latest
    from src.domains.insight.observability import (CONTRACT_GUARD_FAILURES_METRIC,
                                   CONTRACT_GUARD_OUTCOMES, record_contract_guard_failure,
                                   registry)
    assert CONTRACT_GUARD_FAILURES_METRIC == "grape_contract_guard_failures_total"
    record_contract_guard_failure("degraded", "endpoint count 54 < full baseline 261")
    record_contract_guard_failure("rebuild_failed", "fetch failed: connection refused")
    text = generate_latest(registry).decode("utf-8")
    assert f"{CONTRACT_GUARD_FAILURES_METRIC}{{outcome=\"degraded\"}}" in text
    assert f"{CONTRACT_GUARD_FAILURES_METRIC}{{outcome=\"rebuild_failed\"}}" in text


def test_guard_outcomes_match_observability_vocabulary():
    """worker 侧 ALERT_OUTCOMES 与服务端 CONTRACT_GUARD_OUTCOMES 同口径
    （自包含双实现漂移看护，同 #1616 缓存 key 双实现看护模式）。"""
    from src.domains.insight.observability import CONTRACT_GUARD_OUTCOMES
    assert guard.ALERT_OUTCOMES == CONTRACT_GUARD_OUTCOMES


# ---------- 切片③：http_alert_sink 传输契约（经 run_startup_check 生产路径） ----------

class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _PostProbe:
    """替换 urllib.request.urlopen：记录请求并模拟主控响应（或持续死地址）。"""

    def __init__(self, fail=False):
        self.requests = []
        self.fail = fail

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, req.headers, req.data, timeout))
        if self.fail:
            raise OSError("dead address: connection refused")
        return _Resp()


def test_sink_ships_signal_to_master(monkeypatch, tmp_path):
    """上报成功：POST {base_url}/api/runtime/contract-guard/signal 携带 Bearer 与 JSON。"""
    probe = _PostProbe()
    monkeypatch.setattr(guard.urllib.request, "urlopen", probe)
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_runtime_only_schema()), encoding="utf-8")
    out = guard.run_startup_check(config_dir=d, base_url="http://s",
                                  fetch=lambda url: _full_schema(), log=lambda m: None,
                                  alert_sink=guard.http_alert_sink(base_url="http://s",
                                                                   token="rta_probe", config_dir=d))
    assert out["status"] == "rebuilt"
    assert len(probe.requests) == 1  # 重建成功只发 degraded 信号
    url, headers, data, _timeout = probe.requests[0]
    assert url == f"http://s{guard.ALERT_SIGNAL_PATH}"
    assert headers.get("Authorization") == "Bearer rta_probe"
    payload = json.loads(data.decode("utf-8"))
    assert payload["outcome"] == "degraded"
    assert not (d / guard.ALERT_FALLBACK_FILE).exists()  # 送达不落兜底


def test_sink_falls_back_to_local_file_on_dead_address(monkeypatch, tmp_path):
    """死地址兜底：上报不可达 → 信号追加落 cache_dir/contract-guard-alerts.jsonl，
    不抛异常、不阻断启动（死地址场景恰是要告警的场景，落点必须可诊断）。"""
    probe = _PostProbe(fail=True)
    monkeypatch.setattr(guard.urllib.request, "urlopen", probe)
    monkeypatch.setenv(guard.FETCH_TIMEOUT_ENV, "0.1")
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_runtime_only_schema()), encoding="utf-8")

    def _boom(url):
        raise OSError("connection refused")

    warnings = []
    out = guard.run_startup_check(config_dir=d, base_url="http://s", fetch=_boom,
                                  log=warnings.append,
                                  alert_sink=guard.http_alert_sink(base_url="http://s",
                                                                   token="rta_probe", config_dir=d))
    assert out["status"] == "failed"  # 兜底不改变 warn-only 启动语义
    lines = [json.loads(x) for x in
             (d / guard.ALERT_FALLBACK_FILE).read_text(encoding="utf-8").splitlines() if x]
    assert [s["outcome"] for s in lines] == ["degraded", "rebuild_failed"]  # 两类都兜底
    assert all(s.get("fallback") == "alert-sink-unreachable" for s in lines)
    assert any("fell back" in w for w in warnings)  # 兜底留日志可诊断


def test_sink_without_token_falls_back_without_network(monkeypatch, tmp_path):
    """无 RUNTIME_TOKEN（无法鉴权）→ 不发网络请求，直接落本地兜底。"""
    probe = _PostProbe()
    monkeypatch.setattr(guard.urllib.request, "urlopen", probe)
    monkeypatch.delenv("RUNTIME_TOKEN", raising=False)
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_runtime_only_schema()), encoding="utf-8")
    out = guard.run_startup_check(config_dir=d, base_url="http://s",
                                  fetch=lambda url: _full_schema(), log=lambda m: None,
                                  alert_sink=guard.http_alert_sink(base_url="http://s",
                                                                   token="", config_dir=d))
    assert out["status"] == "rebuilt"
    assert probe.requests == []  # 无 token 不尝试外发
    assert (d / guard.ALERT_FALLBACK_FILE).exists()


# ---------- 切片④：主控承接端点 + worker 生产接线 ----------

_GUARD_TOKEN = "rta_test_2085_guard_signal"


def _seed_guard_credential():
    import hashlib
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name='guard-signal-test-2085'")
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status)"
            " VALUES (?,?,?, 'global', 'tester', 'active')",
            (hashlib.sha256(_GUARD_TOKEN.encode()).hexdigest(),
             "guard-signal-test-2085", "guard-signal-dev"))
        db.commit()


def test_contract_guard_signal_endpoint_records_metric():
    """POST {ALERT_SIGNAL_PATH}（rta_ 鉴权）→ 服务端计数落指标出口；
    非法 outcome 拒收 400，无凭据 401（alerts.yml 规则消费的就是这个指标）。"""
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.domains.insight.observability import CONTRACT_GUARD_FAILURES
    _seed_guard_credential()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_GUARD_TOKEN}"}

    before = CONTRACT_GUARD_FAILURES.labels(outcome="degraded")._value.get()
    r = client.post(guard.ALERT_SIGNAL_PATH,
                    json={"outcome": "degraded", "detail": "endpoint count 54 < baseline",
                          "host": "dev-1", "pid": 42, "ts": 1.0},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"
    after = CONTRACT_GUARD_FAILURES.labels(outcome="degraded")._value.get()
    assert after == before + 1  # 端点承接 → 指标出口计数 +1

    r_bad = client.post(guard.ALERT_SIGNAL_PATH, json={"outcome": "bogus"}, headers=headers)
    assert r_bad.status_code == 400
    r_noauth = client.post(guard.ALERT_SIGNAL_PATH, json={"outcome": "degraded"})
    assert r_noauth.status_code == 401


def test_preflight_wires_alert_sink(monkeypatch, tmp_path):
    """preflight（start-worker.sh 调用）生产接线：main() 内置 http_alert_sink 外发。"""
    sink_payloads = []

    def _fake_factory(**kw):
        def _sink(payload):
            sink_payloads.append(payload)
            return True
        return _sink

    monkeypatch.setattr(guard, "http_alert_sink", _fake_factory)
    monkeypatch.delenv(guard.GUARD_DISABLE_ENV, raising=False)
    monkeypatch.delenv("GRAPE_SERVER_URL", raising=False)
    monkeypatch.delenv("GRAPE_BASE_URL", raising=False)
    d = tmp_path / "grape"
    d.mkdir()
    (d / "openapi-2.2.0.json").write_text(json.dumps(_runtime_only_schema()), encoding="utf-8")
    monkeypatch.setenv("GRAPE_CONFIG_DIR", str(d))
    rc = guard.main([])
    assert rc == 3  # warn-only：退化+重建失败退出码 3，不阻断
    assert [p["outcome"] for p in sink_payloads] == ["degraded", "rebuild_failed"]


def test_escape_hatch_disables_check_and_signal(monkeypatch, tmp_path):
    """GRAPE_CONTRACT_GUARD_DISABLED=true：整个 guard（含信号外发）短路，逃生口不破坏。"""
    factory_calls = []

    def _fake_factory(**kw):
        factory_calls.append(kw)

        def _sink(payload):
            return True
        return _sink

    monkeypatch.setattr(guard, "http_alert_sink", _fake_factory)
    monkeypatch.setenv(guard.GUARD_DISABLE_ENV, "true")
    monkeypatch.setenv("GRAPE_CONFIG_DIR", str(tmp_path))
    rc = guard.main([])
    assert rc == 0
    assert factory_calls == []  # 逃生口连 sink 构造都不发生


def test_agent_main_wires_alert_sink():
    """agent.py 生产接线看护（镜像 #1616 test_agent_main_wires_in_process_guard）。"""
    body = open(os.path.join(os.path.dirname(__file__), "..", "src", "domains", "runtime", "engine", "agent.py"),
                encoding="utf-8").read()
    assert "http_alert_sink" in body, "agent.py 应将 http_alert_sink 注入 run_startup_check"
    assert "alert_sink=" in body


# ---------- 切片⑤：deploy/alerts.yml 规则与指标出口一致性 ----------

def _alerts_rules():
    import yaml
    path = os.path.join(os.path.dirname(__file__), "..", "deploy", "alerts.yml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["groups"][0]["rules"]


def test_alerts_yml_has_grape_contract_guard_failed_rule():
    """alerts.yml 含 GrapeContractGuardFailed 规则，表达式引用的指标名与
    src/domains/insight/observability 实际暴露的指标常量一致（表达式/指标名漂移看护）。"""
    from src.domains.insight.observability import CONTRACT_GUARD_FAILURES_METRIC
    rule = next((r for r in _alerts_rules() if r.get("alert") == "GrapeContractGuardFailed"), None)
    assert rule is not None, "deploy/alerts.yml 缺少 GrapeContractGuardFailed 规则"
    assert CONTRACT_GUARD_FAILURES_METRIC in rule["expr"], \
        f"规则表达式未消费实际指标 {CONTRACT_GUARD_FAILURES_METRIC}: {rule['expr']}"
    assert "increase(" in rule["expr"]  # 事件型 counter 用 increase 口径
    assert rule["labels"]["severity"] == "warning"
    assert rule.get("for") == "5m"
    annotations = " ".join(str(v) for v in (rule.get("annotations") or {}).values())
    assert "contract" in annotations.lower()
    assert "排查" in annotations or "Check" in annotations  # 注文明契约退化语义与排查指引


def test_alerts_yml_rule_annotation_covers_both_outcomes_semantics():
    """规则注释明示两类 outcome 语义与业务影响（退化=命令组缺失风险，重建失败=带退化契约跑）。"""
    rule = next(r for r in _alerts_rules() if r.get("alert") == "GrapeContractGuardFailed")
    annotations = " ".join(str(v) for v in (rule.get("annotations") or {}).values())
    for kw in ("degraded", "rebuild_failed"):
        assert kw in annotations, f"注缺 {kw} 语义"
