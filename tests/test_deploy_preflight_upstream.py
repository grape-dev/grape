"""Issue #3764：部署预检的 nginx 上游端口归属断言。

背景：master-001 网关把 ``/api/runtime*`` 残留转发到业务 API 端口 127.0.0.1:8181，
1 小时 3,326 次请求仅 173 次成功（claim 405 / install 502）。本用例锁定
``deploy_preflight.check_upstream_role``：``/api/runtime*`` 上游必须是 worker
端口（基线 38083），错配必失败；无配置探测时 skip；``--nginx-conf`` 与
``--nginx-dump`` 两路输入等价。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_preflight.py"

sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("deploy_preflight", SCRIPT)
assert _spec is not None and _spec.loader is not None
dp = importlib.util.module_from_spec(_spec)
sys.modules["deploy_preflight"] = dp
_spec.loader.exec_module(dp)

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL  # noqa: E402

LEDGER_DDL = (
    "CREATE TABLE IF NOT EXISTS grape_t_schema_migrations ("
    " version TEXT PRIMARY KEY, name TEXT, kind TEXT, applied_at TEXT,"
    " duration_ms INTEGER, checksum TEXT)"
)

CORRECT_CONF = """
upstream grape_mgmt { server 127.0.0.1:38081; }
upstream grape_event { server 127.0.0.1:38082; }
upstream grape_worker { server 127.0.0.1:38083; }
upstream grape_insight { server 127.0.0.1:38084; }

server {
    listen 38080;
    location = /openapi.json { proxy_pass http://grape_mgmt; }
    location /api/runtime/ { proxy_pass http://grape_worker; }
    location /api/runtime/install { proxy_pass http://grape_worker; }
    location /api/tasks/ { proxy_pass http://grape_worker; }
    location /api/webhook/ { proxy_pass http://grape_event; }
    location /api/a2a { proxy_pass http://grape_event; }
    location /api/dashboard/ { proxy_pass http://grape_insight; }
    location / { proxy_pass http://grape_mgmt; }
}
"""


def _seed_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(PLATFORM_OBJECT_DDL)
    conn.execute("CREATE TABLE IF NOT EXISTS grape_t_repo_credentials ("
                 " id TEXT, repo_key TEXT, cred_type TEXT, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT, value TEXT)")
    conn.execute(LEDGER_DDL)
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at, duration_ms, checksum)"
        " VALUES ('202611170030','create-platform-object-tables','safe','2026-11-16 18:47:01',2,'')")
    conn.commit()
    conn.close()
    return path


def _run_cli(path: Path, flag: str, conf_path: Path, *, json_out: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GRAPE_PROCESS_ROLE", "DEVOPS_DB_NAME", "GRAPE_NGINX_CONF")
           and not k.startswith("GRAPE_DSN_") and k != "GRAPE_DOMAIN_LIB"}
    env["GRAPE_PROCESS_ROLE"] = "mgmt"
    env["DEVOPS_DB_BACKEND"] = "sqlite"
    cmd = [sys.executable, str(SCRIPT), "--db-path", str(path), flag, str(conf_path)]
    if json_out:
        cmd.append("--json")
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)


def _upstream_check(stdout: str) -> dict:
    data = json.loads(stdout)
    checks = [c for c in data["checks"] if c["check"] == "nginx_upstream_role"]
    assert len(checks) == 1, data
    return checks[0]


def test_runtime_upstream_on_worker_port_passes():
    """基线配置：/api/runtime* → worker(38083) 必须通过。"""
    c = dp.check_upstream_role(CORRECT_CONF)
    assert c.ok, c.detail
    assert "38083" in c.detail


def test_runtime_upstream_8181_residue_fails():
    """复刻 001 残留：/api/runtime* 指到业务 API 端口 8181 → 失败且可定位。"""
    conf = """
    upstream grape_worker { server 127.0.0.1:8181; }
    server { location /api/runtime/ { proxy_pass http://grape_worker; } }
    """
    c = dp.check_upstream_role(conf)
    assert not c.ok
    assert "8181" in c.detail
    assert "38083" in c.detail
    assert "/api/runtime" in c.detail


def test_business_port_8181_elsewhere_still_passes():
    """业务 API 端口 8181 用在别的 location 不算错，只要 /api/runtime 正确。"""
    conf = """
    upstream grape_worker { server 127.0.0.1:38083; }
    upstream grape_business { server 127.0.0.1:8181; }
    server {
        location /api/runtime/ { proxy_pass http://grape_worker; }
        location = /api/health { proxy_pass http://grape_business; }
        location / { proxy_pass http://grape_business; }
    }
    """
    c = dp.check_upstream_role(conf)
    assert c.ok, c.detail


def test_unresolvable_upstream_fails():
    """upstream 未定义/无法解析必须失败并提示，不能静默放过。"""
    conf = """
    server { location /api/runtime/ { proxy_pass http://grape_worker; } }
    """
    c = dp.check_upstream_role(conf)
    assert not c.ok
    assert "无法解析" in c.detail
    assert "grape_worker" in c.detail


def test_no_config_is_skip_not_fail():
    """探测不到配置 → skip（ok=True），避免误伤无网关节点。"""
    c = dp.check_upstream_role("")
    assert c.ok
    assert "skip" in c.detail


def test_ports_override_supports_legacy_8083():
    """001 过渡口径：--ports/GRAPE_PROCESS_PORTS 覆盖 worker=8083 后应通过。"""
    conf = """
    upstream grape_worker { server 127.0.0.1:8083; }
    server { location /api/runtime/ { proxy_pass http://grape_worker; } }
    """
    assert not dp.check_upstream_role(conf).ok
    ports = dp.parse_ports("worker=8083")
    c = dp.check_upstream_role(conf, ports=ports)
    assert c.ok, c.detail
    assert "8083" in c.detail


def test_nginx_conf_and_nginx_dump_are_equivalent(tmp_path):
    """--nginx-conf 与 --nginx-dump 两路输入语义等价。"""
    db = _seed_db(tmp_path / "ok.db")
    conf = tmp_path / "grape.conf"
    conf.write_text(CORRECT_CONF, encoding="utf-8")
    dump = tmp_path / "nginx-T.txt"
    dump.write_text(
        "# configuration file /etc/nginx/conf.d/grape.conf:\n"
        + CORRECT_CONF
        + "\n# configuration file /etc/nginx/nginx.conf:\n"
        "nginx: configuration file /etc/nginx/nginx.conf test is successful\n",
        encoding="utf-8",
    )

    r_conf = _run_cli(db, "--nginx-conf", conf)
    r_dump = _run_cli(db, "--nginx-dump", dump)
    assert r_conf.returncode == 0, r_conf.stdout + r_conf.stderr
    assert r_dump.returncode == 0, r_dump.stdout + r_dump.stderr
    c_conf = _upstream_check(r_conf.stdout)
    c_dump = _upstream_check(r_dump.stdout)
    assert c_conf["ok"] is True and c_dump["ok"] is True
    assert (c_conf["detail"].replace(f"conf:{conf}", "<src>")
            == c_dump["detail"].replace(f"dump:{dump}", "<src>"))


def test_repo_gateway_configs_are_aligned_with_fact_source():
    """仓库内 cluster / four-process / compose 三套网关配置都必须通过上游归属校验。

    cluster 与 four-process 为 127.0.0.1 四进程拓扑，按绝对端口事实源校验；
    compose 为容器服务名拓扑（worker=38082），只校验指向 worker 角色。
    """
    for name in (
        "nginx-grape-cluster.conf",
        "nginx-grape-four-process.conf",
        "nginx-grape-compose.conf",
    ):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        c = dp.check_upstream_role(text)
        assert c.ok, f"{name}: {c.detail}"

    compose = (ROOT / "deploy" / "nginx-grape-compose.conf").read_text(encoding="utf-8")
    c = dp.check_upstream_role(compose)
    assert "worker" in c.detail
    assert dp.check_upstream_role(compose, ports={"worker": 9999}).ok


def test_deprecated_three_process_conf_passes_only_with_legacy_ports():
    """废弃三进程布局（38080-38082）按自身端口口径可校验，防止 runtime 漂到 8181。

    默认四进程口径下它必然失败（worker=38082 ≠ 38083），因此 CI 用 --ports 显式声明。
    """
    text = (ROOT / "deploy" / "nginx-grape.conf").read_text(encoding="utf-8")
    assert not dp.check_upstream_role(text).ok
    c = dp.check_upstream_role(
        text, ports=dp.parse_ports("mgmt=38080,event=38081,worker=38082,insight=38083"))
    assert c.ok, c.detail


def test_auto_detection_scans_all_conf_d_files(monkeypatch, tmp_path):
    """conf.d 同时存在 grape-gzip.conf 与 grape.conf 时必须检到后者（Issue #3764 R1）。

    旧实现只取 sorted(glob()) 第一个命中，grape-gzip.conf ASCII 序在前，
    含 /api/runtime* 的网关文件从未被读取 → check 静默 skip。
    """
    gzip_conf = tmp_path / "grape-gzip.conf"
    gzip_conf.write_text("gzip_vary on;\ngzip_types application/json;\n", encoding="utf-8")
    gateway = tmp_path / "grape.conf"
    gateway.write_text(
        "upstream grape_worker { server 127.0.0.1:8181; }\n"
        "server { location /api/runtime/ { proxy_pass http://grape_worker; } }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dp.glob, "glob", lambda pattern: [str(gzip_conf), str(gateway)])

    sources = dp.load_nginx_sources()
    assert [s.label for s in sources] == [f"conf:{gzip_conf}", f"conf:{gateway}"]
    assert [s.gateway for s in sources] == [False, True]

    text, label = dp.load_nginx_text()
    assert "8181" in (text or "")
    assert "grape-gzip.conf" in label and "grape.conf" in label

    c = dp.check_upstream_role_sources(sources)
    assert not c.ok
    assert str(gateway) in c.detail
    assert "8181" in c.detail and "38083" in c.detail
    assert "grape-gzip.conf" in c.detail  # 非网关文件被记录而非丢弃


def test_gateway_file_without_runtime_location_warns_not_silent_pass():
    """网关文件没有 /api/runtime* 时必须 warn 点名文件，不允许静默 PASS。"""
    conf = "server { location / { proxy_pass http://127.0.0.1:38080; } }"
    c = dp.check_upstream_role(conf, source="conf:/etc/nginx/conf.d/grape.conf")
    assert c.ok
    assert "未发现 /api/runtime*" in c.detail
    assert "grape.conf" in c.detail


def test_runtime_location_without_proxy_pass_warns_not_silent_pass():
    """runtime location 没有 proxy_pass 时无法断言，必须 warn 而非静默 PASS。"""
    conf = """
    upstream grape_worker { server 127.0.0.1:38083; }
    server { location /api/runtime/ { return 404; } }
    """
    c = dp.check_upstream_role(conf, source="conf:/etc/nginx/conf.d/grape.conf")
    assert c.ok
    assert "WARN" in c.detail
    assert "无 proxy_pass" in c.detail
    assert "grape.conf" in c.detail


def test_runtime_upstream_unix_socket_warns_not_silent_pass():
    """upstream 只含 unix socket（无 host:port）时给 warn，不静默 PASS。"""
    conf = """
    upstream grape_worker { server unix:/run/grape-worker.sock; }
    server { location /api/runtime/ { proxy_pass http://grape_worker; } }
    """
    c = dp.check_upstream_role(conf)
    assert c.ok
    assert "WARN" in c.detail
    assert "无可解析 server" in c.detail


def test_runtime_upstream_server_without_port_warns_not_silent_pass():
    """upstream server 缺端口（无法归属到 worker 端口）时给 warn，不静默 PASS。"""
    conf = """
    upstream grape_worker { server 127.0.0.1 max_fails=3; }
    server { location /api/runtime/ { proxy_pass http://grape_worker; } }
    """
    c = dp.check_upstream_role(conf)
    assert c.ok
    assert "WARN" in c.detail
    assert "无可解析 server" in c.detail


def test_parse_ports_rejects_invalid_values():
    """--ports 非法值（非整数/未知角色/越界端口）必须 ValueError，CLI 退出码 2。"""
    for spec in ("worker=abc", "bogus=1", "worker=70000"):
        with pytest.raises(ValueError):
            dp.parse_ports(spec)


def test_cli_invalid_ports_exits_2():
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--ports", "worker=abc"],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "端口非整数" in r.stderr or "端口非整数" in r.stdout