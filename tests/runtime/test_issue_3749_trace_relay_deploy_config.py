"""Issue #3749：部署清单中继配置契约（配置面回归测试）。

Issue 的根因是「变量只存在于代码与测试，部署清单没有」——代码改对了清单没跟上会复现。
这里把清单当契约断言：五个配置面必须出现 `OTLP_RELAY_TARGET`，告警规则必须存在。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", [
    ".env.example",                                   # 主文档入口
    "deploy/docker-compose.yml",                      # 四容器：worker-server 承接中继
    "deploy/systemd/grape-worker-server.service",     # 生产 systemd 单元（说明来源）
    "scripts/grape-systemd-run.sh",                   # systemd 运行期 env 注入
    "start-worker.sh",                                # worker 启动脚本（opt-in 透传）
])
def test_deploy_surface_declares_relay_target(rel):
    assert "OTLP_RELAY_TARGET" in _read(rel), f"{rel} 未声明 OTLP_RELAY_TARGET"


def test_compose_sets_relay_target_for_worker_server():
    """worker-server 容器显式透传该变量（四进程拓扑下中继端点由它承接）。"""
    text = _read("deploy/docker-compose.yml")
    block = text.split("  grape-worker-server:", 1)[1].split("  grape-insight-server:", 1)[0]
    assert "OTLP_RELAY_TARGET" in block


def test_alerts_rule_for_relay_503():
    """503 持续 5min 告警：选中 grape_trace_relay_requests_total{status="503"}。"""
    text = _read("deploy/alerts.yml")
    assert "GrapeTraceRelayRejected" in text
    compact = "".join(text.split())
    assert 'grape_trace_relay_requests_total{status="503"}' in compact.replace("'", '"')
    rule = text.split("GrapeTraceRelayRejected", 1)[1][:600]
    assert "for: 5m" in rule
    assert "OTLP_RELAY_TARGET" in rule, "告警文案需指明 OTLP_RELAY_TARGET 未配置"