"""Issue #3847：网关发布窗口 502 风暴加固——配置/脚本/告警回归。

现象：10:12–10:14 发布窗口内网关对健康 upstream 回 1,173 次 502（5xx 率 1.63%，
基线 0.077%），同时段上游 `/api/health`、`/static/openapi.json` 仍 200。根因是
reload/摘流切断在途与 keepalive 连接，且网关无重试、无 keepalive 复用、无优雅退出。

seam（静态配置契约）：
- 四份网关模板均 include 公共重试片段与 upstream keepalive 片段；
- upstream keepalive_timeout 必须大于客户端侧；
- systemd 单元与 nginx `worker_shutdown_timeout` 对齐优雅退出；
- 发布脚本「摘流→排空→退出」+ 502 率门禁，阈值与告警一致（0.005）。
"""
import os
import re
import shutil
import subprocess

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

TEMPLATES = [
    "deploy/nginx-grape.conf",
    "deploy/nginx-grape-cluster.conf",
    "deploy/nginx-grape-four-process.conf",
    "deploy/nginx-grape-compose.conf",
]
RETRY_INC = "deploy/nginx-grape-upstream.inc"
KEEPALIVE_INC = "deploy/nginx-grape-upstream-keepalive.inc"
SHUTDOWN_INC = "deploy/nginx-worker-shutdown.inc"
DRAIN_SCRIPT = "scripts/deploy-drain.sh"
SYSTEMD_UNITS = [
    "deploy/systemd/grape-server.service",
    "deploy/systemd/grape-event-server.service",
    "deploy/systemd/grape-worker-server.service",
    "deploy/systemd/grape-insight-server.service",
]

THRESHOLD = "0.005"   # 0.5%：发布门禁与告警 warning 共用
CRITICAL = "0.02"     # 2%：告警 critical


def _read(rel):
    with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _keepalive_timeout(conf_text):
    m = re.search(r"^\s*keepalive_timeout\s+(\d+)s;", conf_text, re.M)
    assert m, "missing keepalive_timeout directive"
    return int(m.group(1))


# ---------------------------------------------------------------------------
# 公共片段
# ---------------------------------------------------------------------------

def test_retry_inc_has_next_upstream_and_keepalive_client_side():
    inc = _read(RETRY_INC)
    assert "proxy_next_upstream error timeout http_502 http_503;" in inc
    assert "proxy_next_upstream_tries 2;" in inc
    assert "proxy_next_upstream_timeout" in inc
    # upstream keepalive 只有 HTTP/1.1 + Connection "" 才生效
    assert "proxy_http_version 1.1;" in inc
    assert 'proxy_set_header Connection "";' in inc
    assert "keepalive_timeout 65s;" in inc


def test_upstream_keepalive_inc_and_window_ordering():
    keep = _read(KEEPALIVE_INC)
    assert "keepalive 32;" in keep
    assert "keepalive_requests 1000;" in keep
    upstream_timeout = _keepalive_timeout(keep)
    client_timeout = _keepalive_timeout(_read(RETRY_INC))
    assert upstream_timeout > client_timeout, (
        "upstream keepalive_timeout 必须大于客户端侧，避免复用对端已关闭连接"
    )


def test_worker_shutdown_inc_documents_main_context():
    inc = _read(SHUTDOWN_INC)
    assert "worker_shutdown_timeout 30s;" in inc
    # 只允许 main 上下文，需提示不能放进 conf.d/*.conf
    assert "main 上下文" in inc


# ---------------------------------------------------------------------------
# 四份模板接入
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tpl", TEMPLATES)
def test_template_includes_retry_and_keepalive(tpl):
    conf = _read(tpl)
    assert "include conf.d/nginx-grape-upstream.inc;" in conf
    assert "include conf.d/nginx-grape-upstream-keepalive.inc;" in conf


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_keepalive_included_inside_upstream_block(tpl):
    conf = _read(tpl)
    blocks = re.findall(r"upstream\s+\w+\s*\{[^}]*\}", conf, re.S)
    assert blocks, f"{tpl}: 未见 upstream 块"
    for block in blocks:
        assert "include conf.d/nginx-grape-upstream-keepalive.inc;" in block, (
            f"{tpl}: upstream 块缺少 keepalive include"
        )


# ---------------------------------------------------------------------------
# systemd 优雅退出
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("unit", SYSTEMD_UNITS)
def test_systemd_graceful_shutdown(unit):
    content = _read(unit)
    assert "KillSignal=SIGTERM" in content
    assert "TimeoutStopSec=45" in content
    # systemd 等待窗口必须大于 nginx worker_shutdown_timeout(30s)
    shutdown = _read(SHUTDOWN_INC)
    nginx_timeout = _keepalive_timeout(shutdown.replace("worker_shutdown_timeout", "keepalive_timeout"))
    stop_timeout = int(content.split("TimeoutStopSec=", 1)[1].split("\n", 1)[0].strip())
    assert stop_timeout > nginx_timeout


# ---------------------------------------------------------------------------
# 发布脚本：摘流→排空→退出 + 502 门禁 + dry-run
# ---------------------------------------------------------------------------

def test_drain_script_phases_and_gate():
    script = _read(DRAIN_SCRIPT)
    assert "摘流" in script and "排空" in script and "退出" in script
    assert 'GATEWAY_502_THRESHOLD="${GATEWAY_502_THRESHOLD:-' + THRESHOLD + '}"' in script
    assert "GATEWAY_MIN_SAMPLES" in script  # 小样本保护
    assert "GATEWAY_ROLLBACK_CMD" in script
    assert "GATEWAY_DRAIN_CMD" in script
    assert "GATEWAY_INFLIGHT_CMD" in script
    assert "--dry-run" in script
    assert "nginx -t" in script


def test_drain_script_is_bash_syntax_valid():
    path = os.path.join(_REPO_ROOT, DRAIN_SCRIPT)
    proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# 告警分级与阈值一致性
# ---------------------------------------------------------------------------

def test_alerts_tiered_5xx_and_threshold_consistency():
    alerts = _read("deploy/alerts.yml")
    assert "GrapeHTTPErrorRateWarning" in alerts
    assert "GrapeHTTPErrorRateCritical" in alerts
    assert "GrapeGateway502RateWarning" in alerts
    assert "GrapeGateway502RateCritical" in alerts
    # 分级阈值 0.5% / 2%，且旧单阈值 10% 不再作为 5xx 规则
    assert "> " + THRESHOLD in alerts
    assert "> " + CRITICAL in alerts
    assert "GrapeHTTPErrorRateHigh" not in alerts
    # 与发布脚本共用同一 502 阈值常量
    assert THRESHOLD in _read(DRAIN_SCRIPT)


def test_alerts_yml_is_valid_yaml():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(_REPO_ROOT, "deploy/alerts.yml"), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    names = [r["alert"] for g in data["groups"] for r in g["rules"]]
    for name in ("GrapeHTTPErrorRateWarning", "GrapeHTTPErrorRateCritical",
                 "GrapeGateway502RateWarning", "GrapeGateway502RateCritical"):
        assert name in names


# ---------------------------------------------------------------------------
# nginx -t 结构校验（有 nginx 且能隔离 prefix 时执行）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tpl", ["deploy/nginx-grape.conf", "deploy/nginx-grape-cluster.conf"])
def test_nginx_config_syntax(tmp_path, tpl):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("系统未安装 nginx，跳过 nginx -t")
    prefix = tmp_path / "nginx"
    confd = prefix / "conf.d"
    confd.mkdir(parents=True)
    (prefix / "logs").mkdir()
    confd.joinpath("grape.conf").write_text(_read(tpl), encoding="utf-8")
    for inc in (RETRY_INC, KEEPALIVE_INC):
        confd.joinpath(os.path.basename(inc)).write_text(_read(inc), encoding="utf-8")
    (prefix / "nginx.conf").write_text(
        "worker_processes 1;\n"
        f"pid {prefix}/nginx.pid;\n"
        "error_log stderr;\n"
        "events { worker_connections 64; }\n"
        "http {\n"
        "    include conf.d/grape.conf;\n"
        "}\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [nginx, "-t", "-p", str(prefix), "-c", "nginx.conf"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr