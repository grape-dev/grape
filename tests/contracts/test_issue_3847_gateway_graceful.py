"""Issue #3847：网关发布窗口 502 风暴加固——配置/脚本/告警回归。

现象：10:12–10:14 发布窗口内网关对健康 upstream 回 1,173 次 502（5xx 率 1.63%，
基线 0.077%），同时段上游 `/api/health`、`/static/openapi.json` 仍 200。根因是
reload/摘流切断在途与 keepalive 连接，且网关无重试、无 keepalive 复用、无优雅退出。

seam（Issue #3847 审查整改后）：
本文件不再只做「字符串存在性」断言，改为三条可失效即红的契约：
1) **语义解析**：upstream peer 数量（<2 则 proxy_next_upstream 空转）、每个反代
   location 的 Connection 头重设（location 级 proxy_set_header 会整体覆盖 server 级，
   这是 keepalive 在主流量路径失效的成因）、compose 卷挂载覆盖 template 的全部 include
   （否则容器启动即 [emerg] open() failed）；
2) **真实执行**：`scripts/deploy-drain.sh` 用假 curl 驱动，断言「网关整体不可用不得
   放行」「无摘流命令不得重启（fail-closed）」「摘流失败必须中止」等失败语义；
3) **真实 nginx**：用系统 nginx（缺失则 skip）加载模板 + 桩上游，实测 keepalive 复用、
   Upgrade 映射切换、502 重试是否真的发生。
"""
import http.server
import os
import re
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

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
INSTALL_SCRIPT = "deploy/install-nginx-grape-gateway.sh"
COMPOSE = "deploy/docker-compose.yml"
ALERTS = "deploy/alerts.yml"
LOKI_RULES = "deploy/loki-rules.yml"
ALERTS_PY = "src/domains/insight/observability/alerts.py"
GRAFANA_README = "deploy/grafana/README.md"
GRAFANA_SRE_JSON = "deploy/grafana/alerts/alerts-grape-sre.json"
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


def _brace_blocks(text, pattern):
    """按花括号配平切块，返回 [(header, body)]；用于语义解析而非字符串包含。"""
    out = []
    for m in re.finditer(pattern, text, re.M):
        i = m.end() - 1
        depth = 0
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((m.group(0).strip(), text[m.end(): i]))
    return out


def _locations(conf):
    return _brace_blocks(conf, r"^\s*location\s+[^{]+\{")


def _upstreams(conf):
    return _brace_blocks(conf, r"^upstream\s+\w+\s*\{")


def _proxy_locations(conf):
    return [(h, b) for h, b in _locations(conf) if "proxy_pass" in b]


def _keepalive_timeout(conf_text):
    m = re.search(r"^\s*keepalive_timeout\s+(\d+)s;", conf_text, re.M)
    assert m, "missing keepalive_timeout directive"
    return int(m.group(1))


# ---------------------------------------------------------------------------
# 公共片段：重试 + keepalive 对齐
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


def test_retry_inc_documents_single_peer_caveat():
    """单 peer 下 tries 2 是空转——片段必须写明前提，避免模板回到单 peer 而不自知。"""
    inc = _read(RETRY_INC)
    assert re.search(r"≥2\s*个?\s*peer|>=2\s*peer", inc), "缺少「重试需 ≥2 peer」的前提说明"
    assert "location" in inc and "整体覆盖" in inc


def test_upstream_keepalive_inc_and_window_ordering():
    keep = _read(KEEPALIVE_INC)
    assert "keepalive 32;" in keep
    assert "keepalive_requests 1000;" in keep
    upstream_timeout = _keepalive_timeout(keep)
    client_timeout = _keepalive_timeout(_read(RETRY_INC))
    assert upstream_timeout > client_timeout, (
        "upstream keepalive_timeout 必须大于客户端侧，避免复用对端已关闭连接"
    )


def test_keepalive_inc_does_not_overstate_idle_connection_capacity():
    """nginx keepalive 池按对端 sockaddr 复用，同址双 peer 共用同一池。

    片段旧注释称 2 peer → 64 条空闲连接，与实际容量（仍为 keepalive 32）不符，
    会让「连接数是否足够」的判断偏一倍（审查 Nit）。
    """
    keep = _read(KEEPALIVE_INC)
    assert "64 条空闲连接" not in keep, "keepalive 片段仍声称同址双 peer 提供 64 条空闲连接"
    assert re.search(r"同一?池|共用", keep), "缺少「同址双 peer 共用同一 keepalive 池」的说明"


def test_worker_shutdown_inc_documents_main_context():
    inc = _read(SHUTDOWN_INC)
    assert "worker_shutdown_timeout 30s;" in inc
    # 只允许 main 上下文，需提示不能放进 conf.d/*.conf
    assert "main 上下文" in inc


# ---------------------------------------------------------------------------
# 四份模板接入（语义）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tpl", TEMPLATES)
def test_template_includes_retry_and_keepalive(tpl):
    conf = _read(tpl)
    assert "include conf.d/nginx-grape-upstream.inc;" in conf
    assert "include conf.d/nginx-grape-upstream-keepalive.inc;" in conf


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_keepalive_included_inside_upstream_block(tpl):
    conf = _read(tpl)
    blocks = _upstreams(conf)
    assert blocks, f"{tpl}: 未见 upstream 块"
    for header, body in blocks:
        assert "include conf.d/nginx-grape-upstream-keepalive.inc;" in body, (
            f"{tpl}: {header} 缺少 keepalive include"
        )


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_every_upstream_has_at_least_two_peers(tpl):
    """proxy_next_upstream 只在 ≥2 peer 时才会把请求投给下一个 peer。

    单 peer upstream 下 `tries 2` 实测只有 1 次连接尝试（审查问题 2），模板必须声明
    第二个 peer（同机部署用同址双 peer），否则重试配置是死配置。
    """
    conf = _read(tpl)
    ups = _upstreams(conf)
    assert ups, f"{tpl}: 未见 upstream 块"
    for header, body in ups:
        peers = re.findall(r"^\s*server\s+\S+;", body, re.M)
        assert len(peers) >= 2, f"{tpl}: {header} 只有 {len(peers)} 个 peer，proxy_next_upstream 不会生效"


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_every_proxy_location_resets_connection_header(tpl):
    """location 内任何 proxy_set_header 都会整体覆盖 server 级 proxy_set_header。

    upstream.inc 在 server 级设的 `Connection ""` 因此进不到反代 location 里，实测
    upstream 收到 `Connection: close`、keepalive 失效（审查问题 3）。每个反代 location
    必须自己重设：普通 location 为 ""，WS/SSE 升级 location 用 map 变量条件切换。
    """
    conf = _read(tpl)
    locs = _proxy_locations(conf)
    assert len(locs) >= 8, f"{tpl}: 反代 location 数量异常（{len(locs)}）"
    for header, body in locs:
        m = re.search(r"^\s*proxy_set_header\s+Connection\s+(\S+);", body, re.M)
        assert m, f"{tpl}: {header} 未重设 Connection（keepalive 会被 server 级覆盖丢失）"
        value = m.group(1)
        has_upgrade = re.search(r"^\s*proxy_set_header\s+Upgrade\b", body, re.M) is not None
        if has_upgrade:
            assert value == "$grape_connection_upgrade", (
                f"{tpl}: {header} 含 Upgrade，Connection 必须用 map 条件变量，实际 {value}"
            )
        else:
            assert value == '""', f"{tpl}: {header} 普通反代 location 的 Connection 必须为空，实际 {value}"


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_upgrade_map_declared_in_http_context(tpl):
    """map 只能位于 http 上下文；模板本身被 include 进 http{}，故须在 server 之前声明。"""
    conf = _read(tpl)
    m = re.search(
        r"^map\s+\$http_upgrade\s+\$grape_connection_upgrade\s*\{(?P<body>[^}]*)\}",
        conf, re.M,
    )
    assert m, f"{tpl}: 缺少 $http_upgrade → $grape_connection_upgrade 的 map"
    body = m.group("body")
    assert re.search(r"default\s+upgrade;", body)
    assert re.search(r"''\s+\"\";", body)
    if "location" in conf:
        first_server = re.search(r"^\s*server\s*\{", conf, re.M)
        assert first_server and m.start() < first_server.start(), (
            f"{tpl}: map 必须声明在 server 块之前（http 上下文）"
        )


# ---------------------------------------------------------------------------
# compose：容器内 include 的片段必须真的挂载（否则启动即 emerg）
# ---------------------------------------------------------------------------

def _compose_nginx_mounts():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_read(COMPOSE))
    vols = data["services"]["nginx"].get("volumes") or []
    parsed = []
    for v in vols:
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) >= 2:
                parsed.append((parts[0], parts[1]))
    return parsed


def _resolve_mount(mounts, target):
    """把容器内绝对路径解析到宿主源路径（支持目录挂载）；未覆盖返回 None。"""
    for src, dst in mounts:
        if dst.rstrip("/") == target.rstrip("/"):
            return src
        if dst.endswith("/") and target.startswith(dst):
            return os.path.join(src, target[len(dst):])
    return None


def test_compose_nginx_mounts_cover_every_include():
    """grape.conf 里 include 的片段在容器内解析到 /etc/nginx/conf.d/*.inc。

    原先只挂载了 grape.conf，新增的 upstream*.inc 未挂载 → 容器启动 [emerg]
    open() failed（审查问题 1）。这里逐个 include 解析挂载并比对内容。
    """
    conf = _read("deploy/nginx-grape-compose.conf")
    includes = re.findall(r"^\s*include\s+conf\.d/([^;]+);", conf, re.M)
    assert includes, "compose 模板未见 include"
    mounts = _compose_nginx_mounts()
    nginx_prefix = "/etc/nginx"
    for name in includes:
        target = f"{nginx_prefix}/conf.d/{name}"
        src = _resolve_mount(mounts, target)
        assert src, f"{COMPOSE}: nginx 服务未挂载 {target}（容器内 include 会 open() failed）"
        host_path = os.path.normpath(os.path.join(_REPO_ROOT, "deploy", src))
        assert os.path.isfile(host_path), f"{COMPOSE}: 挂载源 {src} 不存在"
        assert _read(f"deploy/{name}").strip() == open(host_path, encoding="utf-8").read().strip(), (
            f"{COMPOSE}: 挂到 {target} 的文件与 deploy/{name} 不一致"
        )


def test_compose_does_not_mount_whole_conf_dir():
    """整目录挂载会把 nginx-grape*.conf 全部纳入 conf.d/*.conf 自动 include，
    造成重复 upstream / 重复 listen 冲突——必须逐个挂载 .conf 与 .inc。"""
    mounts = _compose_nginx_mounts()
    for src, dst in mounts:
        if dst.rstrip("/") == "/etc/nginx/conf.d" and src.rstrip("/") in (".", "./", "/app/deploy", "../deploy"):
            pytest.fail(f"{COMPOSE}: 不应整目录挂载 {src} → {dst}（会重复加载所有 nginx-grape*.conf）")


def test_compose_services_have_graceful_stop_window():
    """compose 路径的优雅退出：给 SIGTERM 排空留窗口（与 systemd TimeoutStopSec=45 对齐）。

    容器里 worker_shutdown_timeout 属 main 上下文、不能放 conf.d；nginx 官方镜像默认
    STOPSIGNAL SIGQUIT（优雅退出），后端进程则依赖 stop_grace_period 不被过早 SIGKILL。
    """
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_read(COMPOSE))
    for name in ("grape-server", "grape-event-server", "grape-worker-server",
                 "grape-insight-server", "nginx"):
        svc = data["services"][name]
        assert svc.get("stop_grace_period") == "45s", f"{COMPOSE}: {name} 未设置 45s 排空窗口"


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
# 发布脚本：静态契约
# ---------------------------------------------------------------------------

def test_drain_script_phases_and_gate():
    script = _read(DRAIN_SCRIPT)
    assert "摘流" in script and "排空" in script and "退出" in script
    assert 'GATEWAY_502_THRESHOLD="${GATEWAY_502_THRESHOLD:-' + THRESHOLD + '}"' in script
    assert "GATEWAY_MIN_SAMPLES" in script            # 小样本保护
    assert "GATEWAY_MIN_FAILURES" in script           # 单次偶发失败不回滚
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
# 发布脚本：真实执行（假 curl 驱动门禁与失败语义）
# ---------------------------------------------------------------------------

_FAKE_CURL = r"""#!/usr/bin/env bash
# 假 curl：模拟 `curl -s -o /dev/null -m 5 -w '%{http_code}' <url>`
[ -n "${FAKE_LOG:-}" ] && date +%s%N >> "$FAKE_LOG"
case "${FAKE_MODE:-ok}" in
  ok)    printf '200'; exit 0 ;;
  down)  exit 7 ;;                 # 连接失败（真 curl 此时也非 0 退出）
  000)   printf '000'; exit 7 ;;   # 连接失败 + 000 双写
  502)   printf '502'; exit 0 ;;
  503)   printf '503'; exit 0 ;;
  504)   printf '504'; exit 0 ;;
  slow)  sleep 1; printf '200'; exit 0 ;;   # 慢响应：验证窗口按墙钟而非迭代次数计时
  mixed503)                       # 503,200,503,503,200：3/5 失败（非全失败）
    n=$(cat "${FAKE_COUNTER}" 2>/dev/null || echo 0)
    n=$((n+1)); echo "$n" > "${FAKE_COUNTER}"
    case "$n" in 1|3|4) printf '503' ;; *) printf '200' ;; esac; exit 0 ;;
  one502)
    n=$(cat "${FAKE_COUNTER}" 2>/dev/null || echo 0)
    n=$((n+1)); echo "$n" > "${FAKE_COUNTER}"
    if [ "$n" = "1" ]; then printf '502'; exit 0; else printf '200'; exit 0; fi ;;
  *)     printf '200'; exit 0 ;;
esac
"""


def _run_drain(tmp_path, mode="ok", samples=2, extra_env=None, args=()):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(_FAKE_CURL, encoding="utf-8")
    curl.chmod(0o755)
    rollback_marker = tmp_path / "rollback_ran"
    stop_marker = tmp_path / "stop_ran"
    env = dict(os.environ)
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "FAKE_MODE": mode,
        "FAKE_COUNTER": str(tmp_path / "counter"),
        "GATEWAY_BASE_URL": "http://127.0.0.1:1",
        "GATEWAY_HEALTH_PATHS": "/api/health",
        "GATEWAY_SAMPLE_INTERVAL": "1",
        "GATEWAY_GATE_WINDOW_SECONDS": str(samples),
        "GATEWAY_MIN_SAMPLES": str(samples),
        "GATEWAY_DRAIN_TIMEOUT": "0",
        "GATEWAY_DRAIN_CMD": "true",
        "GATEWAY_STOP_CMD": f"touch {stop_marker}",
        "GATEWAY_ROLLBACK_CMD": f"touch {rollback_marker}",
    })
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", os.path.join(_REPO_ROOT, DRAIN_SCRIPT), *args],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    return proc, stop_marker, rollback_marker


def test_drain_refuses_restart_without_drain_command(tmp_path):
    """未摘流直接重启 = 在途请求被切成 502。必须 fail-closed，而不是 WARN 后继续。"""
    env = {"GATEWAY_DRAIN_CMD": ""}
    proc, stop_marker, _ = _run_drain(tmp_path, extra_env=env)
    assert proc.returncode != 0, f"未配置摘流命令仍以 0 退出（fail-open）：\n{proc.stdout}\n{proc.stderr}"
    assert not stop_marker.exists(), "未摘流却执行了重启命令"
    assert "GATEWAY_ALLOW_UNDRAINED" in proc.stderr


def test_drain_allows_undrained_only_with_explicit_opt_in(tmp_path):
    env = {"GATEWAY_DRAIN_CMD": "", "GATEWAY_ALLOW_UNDRAINED": "1"}
    proc, stop_marker, _ = _run_drain(tmp_path, extra_env=env)
    assert proc.returncode == 0, proc.stderr
    assert stop_marker.exists(), "显式豁免后应继续执行重启"


def test_drain_aborts_when_drain_command_fails(tmp_path):
    env = {"GATEWAY_DRAIN_CMD": "false"}
    proc, stop_marker, _ = _run_drain(tmp_path, extra_env=env)
    assert proc.returncode != 0
    assert not stop_marker.exists(), "摘流失败后仍然重启了进程"


def test_gate_fails_when_gateway_unreachable(tmp_path):
    """网关整体不可用 → 采样全是连接失败，绝不能算成「502 率 0」放行。"""
    proc, _, rollback_marker = _run_drain(tmp_path, mode="down")
    assert proc.returncode != 0, f"网关不可用仍放行：\n{proc.stdout}\n{proc.stderr}"
    assert "连接失败" in proc.stdout
    assert rollback_marker.exists(), "网关不可用未触发回滚命令"


def test_gate_fails_when_connection_failure_reports_000(tmp_path):
    """curl 失败同时打印 000（真 curl 行为）也必须计为失败，不能被当成状态码。"""
    proc, _, rollback_marker = _run_drain(tmp_path, mode="000")
    assert proc.returncode != 0, f"000 连接失败仍放行：\n{proc.stdout}"
    assert rollback_marker.exists()


def test_gate_rolls_back_on_502_storm(tmp_path):
    proc, _, rollback_marker = _run_drain(tmp_path, mode="502", samples=4)
    assert proc.returncode != 0
    assert "触发自动回滚" in proc.stderr
    assert rollback_marker.exists()


@pytest.mark.parametrize("code", ["503", "504"])
def test_gate_rolls_back_on_non_502_5xx_storm(tmp_path, code):
    """验收目标是「窗口内 5xx ≤0.5%」，503/504 与 502 同为 5xx。

    修复前失败率只累加 n502+nconn，全返 503 时 `502=0` → 放行 exit 0，
    应用侧 503/504 风暴（上游过载、超时）会被门禁当成健康。
    """
    proc, _, rollback_marker = _run_drain(tmp_path, mode=code, samples=4)
    assert proc.returncode != 0, f"全部 {code} 仍放行：\n{proc.stdout}\n{proc.stderr}"
    assert "触发自动回滚" in proc.stderr
    assert rollback_marker.exists()


def test_gate_failure_rate_counts_all_5xx_samples(tmp_path):
    """失败率口径 = 5xx（含 502）+ 连接失败，而非仅 502+连接失败。"""
    proc, _, rollback_marker = _run_drain(tmp_path, mode="mixed503", samples=5)
    m = re.search(r"失败率 ([0-9.]+) > 阈值", proc.stderr)
    assert m, f"未见失败率判定输出：\n{proc.stdout}\n{proc.stderr}"
    assert float(m.group(1)) > 0.5, f"503 未计入失败率：{m.group(1)}"
    assert rollback_marker.exists()


def test_gate_all_5xx_below_min_samples_still_rolls_back(tmp_path):
    """全样本失败（此处全 503）即网关判死，不能被「最小样本不足」放行。

    小窗口/少路径（或缩窗排障）下 total < GATEWAY_MIN_SAMPLES 时，旧实现先判
    样本不足再判失败率 → 全失败也 exit 0（fail-open）。
    """
    env = {"GATEWAY_MIN_SAMPLES": "30"}
    proc, _, rollback_marker = _run_drain(tmp_path, mode="503", samples=3, extra_env=env)
    assert proc.returncode != 0, f"全样本 5xx 且样本不足仍放行：\n{proc.stdout}\n{proc.stderr}"
    assert rollback_marker.exists()


def test_gate_all_connections_failed_below_min_samples_rolls_back(tmp_path):
    env = {"GATEWAY_MIN_SAMPLES": "30"}
    proc, _, rollback_marker = _run_drain(tmp_path, mode="down", samples=3, extra_env=env)
    assert proc.returncode != 0, f"网关全不可用仍以样本不足放行：\n{proc.stdout}\n{proc.stderr}"
    assert rollback_marker.exists()


def test_gate_ignores_single_isolated_502(tmp_path):
    """180 样本粒度下 1 次 502 = 0.56% > 阈值；基线 0.077% 时这会误回滚发布，
    故要求失败样本数 ≥ GATEWAY_MIN_FAILURES（默认 3）。"""
    proc, _, rollback_marker = _run_drain(tmp_path, mode="one502", samples=3)
    assert proc.returncode == 0, f"单次偶发 502 触发了回滚：\n{proc.stdout}\n{proc.stderr}"
    assert not rollback_marker.exists()


def test_gate_passes_on_healthy_gateway(tmp_path):
    proc, _, rollback_marker = _run_drain(tmp_path)
    assert proc.returncode == 0
    assert "[OK]" in proc.stdout
    assert not rollback_marker.exists()


def test_gate_window_is_wall_clock_not_loop_iterations(tmp_path):
    """采样窗口必须按墙钟计时，而不是「迭代次数 × 间隔」。

    修复前每轮只把 elapsed 加一个 GATEWAY_SAMPLE_INTERVAL，与 curl 实际耗时无关：
    每样本 1s × 3 路径、配置窗口 3s 时，实际要跑 3 轮 ×(3+1)s = 12s；默认 60s 窗口
    若 curl 全部超时（-m 5）则约 60×(15+1)=960s 才判定，回滚被延迟到 16 分钟。
    """
    env = {"GATEWAY_HEALTH_PATHS": "/a /b /c"}
    started = time.time()
    proc, _, _ = _run_drain(tmp_path, mode="slow", samples=3, extra_env=env)
    elapsed = time.time() - started
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 8, f"窗口 3s 实际耗时 {elapsed:.1f}s（按迭代次数计时的旧行为约 12s）"


def test_gate_short_circuits_early_on_failure(tmp_path):
    """失败率与最小失败数已达标时应提前判定，不必跑满窗口（否则回滚延迟一整个窗口）。"""
    env = {"GATEWAY_MIN_SAMPLES": "3", "GATEWAY_MIN_FAILURES": "3"}
    started = time.time()
    proc, _, rollback_marker = _run_drain(tmp_path, mode="503", samples=60, extra_env=env)
    elapsed = time.time() - started
    assert proc.returncode != 0
    assert rollback_marker.exists()
    assert elapsed < 10, f"已确认失败仍跑满 60s 窗口（{elapsed:.1f}s）"


def test_sampling_starts_before_restart_covers_cutover_window(tmp_path):
    """门禁采样必须覆盖重启/切流瞬间（在 stop_unit 之前开始），否则切流 502 采不到。"""
    sample_log = tmp_path / "samples.log"
    stop_stamp = tmp_path / "stop.stamp"
    env = {
        "FAKE_LOG": str(sample_log),
        "GATEWAY_STOP_CMD": f"date +%s%N > {stop_stamp}; sleep 2",
        "GATEWAY_GATE_WINDOW_SECONDS": "4",
        "GATEWAY_MIN_SAMPLES": "2",
    }
    proc, _, _ = _run_drain(tmp_path, samples=4, extra_env=env)
    assert proc.returncode == 0, proc.stderr
    assert sample_log.exists() and stop_stamp.exists()
    first_sample = int(sample_log.read_text().split()[0])
    stop_at = int(stop_stamp.read_text().split()[0])
    assert first_sample - stop_at < 200_000_000, (
        "首个采样晚于重启——切流瞬间的失败没有被覆盖"
    )


def test_drain_dry_run_executes_nothing(tmp_path):
    proc, stop_marker, rollback_marker = _run_drain(tmp_path, args=("--dry-run",))
    assert proc.returncode == 0
    assert not stop_marker.exists() and not rollback_marker.exists()
    assert "[dry-run]" in proc.stdout


# ---------------------------------------------------------------------------
# 告警：网关 502 必须来自 nginx 日志侧（Loki），不是应用指标
# ---------------------------------------------------------------------------

def test_alerts_tiered_5xx_and_threshold_consistency():
    alerts = _read(ALERTS)
    assert "GrapeHTTPErrorRateWarning" in alerts
    assert "GrapeHTTPErrorRateCritical" in alerts
    # 分级阈值 0.5% / 2%，且旧单阈值 10% 不再作为 5xx 规则
    assert "> " + THRESHOLD in alerts
    assert "> " + CRITICAL in alerts
    assert "GrapeHTTPErrorRateHigh" not in alerts
    # 与发布脚本共用同一 502 阈值常量
    assert THRESHOLD in _read(DRAIN_SCRIPT)


def test_gateway_502_alerts_are_log_side_not_app_metric():
    """nginx 产生的 502 只进 Loki（access.log），不进 grape_http_requests_total；
    挂在应用指标上的网关 502 规则在事故时段恒为 0，结构上不可能触发。"""
    alerts = _read(ALERTS)
    # 应用指标侧不得再定义网关 502 规则（注释里指明去向不算定义）
    assert not re.search(r"-\s*alert:\s*GrapeGateway502Rate", alerts)
    assert "grape_http_requests_total{status=\"502\"}" not in alerts
    loki = _read(LOKI_RULES)
    assert "GrapeGateway502RateWarning" in loki
    assert "GrapeGateway502RateCritical" in loki
    assert 'job="nginx-access"' in loki
    assert "count_over_time" in loki, "Loki 的 rate() 返回字节数，分母必须用 count_over_time"
    assert THRESHOLD in loki and CRITICAL in loki


def test_alerts_and_loki_rules_yml_are_valid_yaml():
    yaml = pytest.importorskip("yaml")
    for rel in (ALERTS, LOKI_RULES):
        data = yaml.safe_load(_read(rel))
        names = [r["alert"] for g in data["groups"] for r in g["rules"]]
        assert names, f"{rel}: 未见告警规则"
    alerts = yaml.safe_load(_read(ALERTS))
    names = [r["alert"] for g in alerts["groups"] for r in g["rules"]]
    for name in ("GrapeHTTPErrorRateWarning", "GrapeHTTPErrorRateCritical"):
        assert name in names
    loki = yaml.safe_load(_read(LOKI_RULES))
    names = [r["alert"] for g in loki["groups"] for r in g["rules"]]
    for name in ("GrapeGateway502RateWarning", "GrapeGateway502RateCritical"):
        assert name in names


def _seed_rules():
    """用 ast 解析 SEED_RULES（不 import 告警模块，避免拉 DB/runtime 依赖）。"""
    import ast
    tree = ast.parse(_read(ALERTS_PY))
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "SEED_RULES":
                return ast.literal_eval(node.value)
    raise AssertionError(f"{ALERTS_PY}: 未见 SEED_RULES 定义")


def test_insight_seed_http_5xx_rules_match_alerts_yml_tiers():
    """Insight 种子规则必须与 deploy/alerts.yml 同为 5xx 分级 0.5%/2%。

    修复前种子是单条 threshold=0.1、seed_source=GrapeHTTPErrorRateHigh；Insight 重启
    后仍按 10% 求值，1.63% 的事故时段两侧都不触发（审查 Required R3）。
    """
    rules = [r for r in _seed_rules() if r.get("metric") == "http_5xx_rate"]
    assert len(rules) == 2, f"5xx 种子规则应拆为 warning/critical 两条，实际 {len(rules)}"
    assert {float(r["threshold"]) for r in rules} == {float(THRESHOLD), float(CRITICAL)}, (
        f"种子阈值 {[r['threshold'] for r in rules]} 与 alerts.yml 的 0.5%/2% 不一致"
    )
    assert {r["severity"] for r in rules} == {"warning", "critical"}
    assert {r["seed_source"] for r in rules} == {
        "GrapeHTTPErrorRateWarning", "GrapeHTTPErrorRateCritical",
    }
    assert not [r for r in _seed_rules() if r.get("seed_source") == "GrapeHTTPErrorRateHigh"], (
        "alerts.yml 已删除 GrapeHTTPErrorRateHigh，种子规则不得再引用该名字"
    )


def test_grafana_5xx_rule_aligned_with_tiered_thresholds():
    """Grafana SRE 规则（排除 /faro/collect）不得再停在 10% 旧口径。"""
    readme = _read(GRAFANA_README)
    sre = _read(GRAFANA_SRE_JSON)
    assert "GrapeHTTPErrorRateHigh" not in readme, "README 仍引用已删除的 GrapeHTTPErrorRateHigh"
    assert '"$A > 10"' not in sre, "Grafana 数学表达式仍是 $A > 10（10% 旧阈值）"
    assert '"$A > 0.5"' in sre, "Grafana 规则应改用 0.5%（与 alerts.yml Warning 对齐）"
    assert "GrapeHTTPErrorRateHigh" not in sre, "Grafana summary 仍引用已删除的规则名"
    assert "0.5%" in readme and "10%" not in readme.split("## 数据源 uid")[0].split("## 告警规则")[1], (
        "Grafana README 告警表仍在描述 10% 阈值"
    )


def test_no_dangling_grape_http_error_rate_high_anywhere():
    """全仓不得再出现已删除规则名的悬空引用（含 Insight 种子与 Grafana 侧）。"""
    for rel in (ALERTS_PY, GRAFANA_README, GRAFANA_SRE_JSON, ALERTS, LOKI_RULES):
        assert "GrapeHTTPErrorRateHigh" not in _read(rel), f"{rel} 仍引用已删除的 GrapeHTTPErrorRateHigh"


# ---------------------------------------------------------------------------
# 原生下发脚本：幂等 + nginx -t 失败回滚
# ---------------------------------------------------------------------------

def _install_prefix(tmp_path, backend_port=None, listen_port=None):
    prefix = tmp_path / "nginx"
    confd = prefix / "conf.d"
    confd.mkdir(parents=True)
    (prefix / "logs").mkdir()
    backend_port = backend_port or _free_port()
    listen_port = listen_port or _free_port()
    conf = _rewrite_for_test(_read("deploy/nginx-grape-compose.conf"), backend_port, listen_port)
    confd.joinpath("grape.conf").write_text(conf, encoding="utf-8")
    (prefix / "nginx.conf").write_text(
        "worker_processes 1;\n"
        f"pid {prefix}/nginx.pid;\n"
        f"error_log {prefix}/error.log warn;\n"
        "events { worker_connections 64; }\n"
        "http {\n"
        "    access_log off;\n"
        "    include conf.d/*.conf;\n"
        "}\n",
        encoding="utf-8",
    )
    return prefix


def _run_install(prefix, extra_env=None, args=()):
    env = dict(os.environ)
    env.update({
        "NGINX_CONF_DIR": str(prefix / "conf.d"),
        "NGINX_MAIN_CONF": str(prefix / "nginx.conf"),
        "NGINX_TEST_ARGS": f"-p {prefix} -c nginx.conf",
        "NGINX_SKIP_RELOAD": "1",
    })
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", os.path.join(_REPO_ROOT, INSTALL_SCRIPT), *args],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def test_install_script_syntax_and_dry_run(tmp_path):
    path = os.path.join(_REPO_ROOT, INSTALL_SCRIPT)
    assert os.path.isfile(path), f"{INSTALL_SCRIPT}: 缺少原生下发脚本（审查 Optional O3）"
    assert subprocess.run(["bash", "-n", path], capture_output=True).returncode == 0
    prefix = _install_prefix(tmp_path)
    proc = _run_install(prefix, args=("--dry-run",))
    assert proc.returncode == 0, proc.stderr
    assert "[dry-run]" in proc.stdout
    assert not (prefix / "conf.d" / "nginx-grape-upstream.inc").exists(), "dry-run 不应真的落盘"


def test_install_script_copies_includes_and_wires_shutdown(tmp_path):
    prefix = _install_prefix(tmp_path)
    proc = _run_install(prefix)
    assert proc.returncode == 0, proc.stderr
    confd = prefix / "conf.d"
    for inc in (RETRY_INC, KEEPALIVE_INC):
        dst = confd / os.path.basename(inc)
        assert dst.is_file() and dst.read_text() == _read(inc)
    shutdown = prefix / os.path.basename(SHUTDOWN_INC)
    assert shutdown.is_file() and shutdown.read_text() == _read(SHUTDOWN_INC)
    main_conf = (prefix / "nginx.conf").read_text()
    assert "include nginx-worker-shutdown.inc;" in main_conf
    assert main_conf.index("include nginx-worker-shutdown.inc;") < main_conf.index("events {"), (
        "worker_shutdown_timeout 必须在 main 上下文（events 之前）"
    )


def test_install_script_is_idempotent(tmp_path):
    prefix = _install_prefix(tmp_path)
    first = _run_install(prefix)
    assert first.returncode == 0, first.stderr
    before = {p.name: p.read_text() for p in prefix.rglob("*") if p.is_file()}
    second = _run_install(prefix)
    assert second.returncode == 0, second.stderr
    after = {p.name: p.read_text() for p in prefix.rglob("*") if p.is_file()}
    assert before == after, "重复下发改变了文件内容（非幂等）"


def test_install_script_rolls_back_when_nginx_test_fails(tmp_path):
    prefix = _install_prefix(tmp_path)
    (prefix / "conf.d" / "broken.conf").write_text("definitely not nginx syntax;\n", encoding="utf-8")
    main_before = (prefix / "nginx.conf").read_text()
    proc = _run_install(prefix)
    assert proc.returncode != 0, f"nginx -t 失败仍以 0 退出：\n{proc.stdout}\n{proc.stderr}"
    assert not (prefix / "conf.d" / "nginx-grape-upstream.inc").exists(), "失败后未清理新下发的片段"
    assert "include nginx-worker-shutdown.inc;" not in (prefix / "nginx.conf").read_text(), (
        "失败后未回滚主 nginx.conf"
    )
    assert (prefix / "nginx.conf").read_text() == main_before


# ---------------------------------------------------------------------------
# 真实 nginx：语法 + keepalive 复用 + Upgrade 映射 + 502 重试
# ---------------------------------------------------------------------------

def _nginx_bin():
    return shutil.which("nginx")


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _rewrite_for_test(conf, backend_port, listen_port):
    """把模板改写成可在测试环境启动的等价配置（仅地址/端口/TLS 差异）。"""
    conf = re.sub(r"^(\s*)listen\s+\d+(\s+ssl)?;", rf"\1listen {listen_port};", conf, flags=re.M)
    conf = re.sub(r"^(\s*server\s+)\S+;", rf"\g<1>127.0.0.1:{backend_port};", conf, flags=re.M)
    return conf


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        srv: "_UpstreamServer" = self.server  # type: ignore[assignment]
        srv.conns += 1
        super().setup()

    def do_GET(self):
        srv: "_UpstreamServer" = self.server  # type: ignore[assignment]
        srv.requests.append(dict(self.headers.items()))
        if srv.mode == "502-once" and len(srv.requests) == 1:
            body = b'{"error":"cut"}'
            self.send_response(502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - 与 BaseHTTPRequestHandler 签名一致
        pass


class _UpstreamServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.conns = 0
        self.mode = "ok"


@pytest.fixture()
def upstream():
    srv = _UpstreamServer(("127.0.0.1", _free_port()), _UpstreamHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def nginx_runtime(tmp_path, upstream):
    """按模板（默认单机版）拉起真实 nginx，指向桩上游。"""
    nginx = _nginx_bin()
    if not nginx:
        pytest.skip("系统未安装 nginx，跳过真实 nginx 行为用例")
    prefix = tmp_path / "nginx"
    confd = prefix / "conf.d"
    confd.mkdir(parents=True)
    (prefix / "logs").mkdir()
    listen_port = _free_port()
    conf = _rewrite_for_test(_read("deploy/nginx-grape.conf"), upstream.server_address[1], listen_port)
    confd.joinpath("grape.conf").write_text(conf, encoding="utf-8")
    for inc in (RETRY_INC, KEEPALIVE_INC):
        confd.joinpath(os.path.basename(inc)).write_text(_read(inc), encoding="utf-8")
    nginx_conf = (
        "worker_processes 1;\n"
        f"pid {prefix}/nginx.pid;\n"
        f"error_log {prefix}/error.log warn;\n"
        "events { worker_connections 64; }\n"
        "http {\n"
        "    access_log off;\n"
        "    include conf.d/grape.conf;\n"
        "}\n"
    )
    (prefix / "nginx.conf").write_text(nginx_conf, encoding="utf-8")
    proc = subprocess.run([nginx, "-p", str(prefix), "-c", "nginx.conf"], capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"nginx 启动失败：{proc.stderr}")
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", listen_port), 0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        subprocess.run([nginx, "-p", str(prefix), "-c", "nginx.conf", "-s", "stop"], capture_output=True)
        pytest.fail("nginx 未在 5s 内监听测试端口")
    yield listen_port
    subprocess.run([nginx, "-p", str(prefix), "-c", "nginx.conf", "-s", "stop"], capture_output=True, text=True)


def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_nginx_keepalive_reuses_upstream_connection(nginx_runtime, upstream):
    """主流量路径两个请求必须复用一条 upstream 连接，且不得下发 Connection: close。"""
    for _ in range(2):
        assert _http_get(f"http://127.0.0.1:{nginx_runtime}/api/self-check") == 200
        time.sleep(0.2)
    deadline = time.time() + 3
    while len(upstream.requests) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(upstream.requests) == 2, f"上游只收到 {len(upstream.requests)} 个请求"
    assert upstream.conns == 1, f"upstream 连接未复用：{upstream.conns} 条 TCP 连接 / 2 个请求"
    for headers in upstream.requests:
        assert headers.get("Connection") is None, (
            f"upstream 收到 Connection: {headers.get('Connection')}，keepalive 失效"
        )


def test_nginx_plain_proxy_location_also_keepalives(nginx_runtime, upstream):
    """非兜底 location（/webhook/）此前被 location 级 proxy_set_header 覆盖成
    Connection: close，每请求新建连接——整改后同样必须复用。"""
    for _ in range(2):
        assert _http_get(f"http://127.0.0.1:{nginx_runtime}/webhook/x") == 200
        time.sleep(0.2)
    assert len(upstream.requests) == 2
    assert upstream.conns == 1, f"/webhook/ 未复用 upstream 连接：{upstream.conns} 条"
    assert all(h.get("Connection") is None for h in upstream.requests)


def test_nginx_upgrade_map_switches_connection_only_for_ws(nginx_runtime, upstream):
    assert _http_get(
        f"http://127.0.0.1:{nginx_runtime}/ws",
        headers={"Upgrade": "websocket", "Connection": "Upgrade"},
    ) == 200
    assert upstream.requests[-1].get("Connection") == "upgrade", (
        "WS 升级请求未透传 Connection: upgrade"
    )


def test_nginx_retries_502_on_second_peer(tmp_path, upstream):
    """同址双 peer 下，首个 502 必须触发第二次尝试并返回 200（单 peer 时实测不重试）。"""
    nginx = _nginx_bin()
    if not nginx:
        pytest.skip("系统未安装 nginx，跳过真实 nginx 行为用例")
    upstream.mode = "502-once"
    prefix = tmp_path / "nginx"
    confd = prefix / "conf.d"
    confd.mkdir(parents=True)
    (prefix / "logs").mkdir()
    listen_port = _free_port()
    conf = _rewrite_for_test(_read("deploy/nginx-grape.conf"), upstream.server_address[1], listen_port)
    confd.joinpath("grape.conf").write_text(conf, encoding="utf-8")
    for inc in (RETRY_INC, KEEPALIVE_INC):
        confd.joinpath(os.path.basename(inc)).write_text(_read(inc), encoding="utf-8")
    (prefix / "nginx.conf").write_text(
        "worker_processes 1;\n"
        f"pid {prefix}/nginx.pid;\n"
        f"error_log {prefix}/error.log warn;\n"
        "events { worker_connections 64; }\n"
        'http { access_log off; include conf.d/grape.conf; }\n',
        encoding="utf-8",
    )
    proc = subprocess.run([nginx, "-p", str(prefix), "-c", "nginx.conf"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", listen_port), 0.2):
                    break
            except OSError:
                time.sleep(0.05)
        code = _http_get(f"http://127.0.0.1:{listen_port}/api/self-check")
        deadline = time.time() + 3
        while len(upstream.requests) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert code == 200, f"客户端仍收到 {code}：502 未重试"
        assert len(upstream.requests) == 2, f"上游只被尝试 {len(upstream.requests)} 次"
    finally:
        subprocess.run([nginx, "-p", str(prefix), "-c", "nginx.conf", "-s", "stop"], capture_output=True)


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_nginx_config_syntax(tmp_path, tpl):
    """nginx -t 校验全部四份模板（含 map 上下文、双 peer、compose 服务名改写）。"""
    nginx = _nginx_bin()
    if not nginx:
        pytest.skip("系统未安装 nginx，跳过 nginx -t")
    prefix = tmp_path / "nginx"
    confd = prefix / "conf.d"
    confd.mkdir(parents=True)
    (prefix / "logs").mkdir()
    conf = _rewrite_for_test(_read(tpl), _free_port(), _free_port())
    confd.joinpath("grape.conf").write_text(conf, encoding="utf-8")
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