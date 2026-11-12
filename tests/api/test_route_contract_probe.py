"""路由契约探针的合成用例（QA #1272 I3：补自动化测试）。

覆盖两类历史缺陷：
  1) 四进程全不可达时曾输出 PASS（门禁空转）→ 必须 fail-loud；
  2) conf upstream 端口与 GRAPE_PROBE_PORTS 不同源时曾刷满伪 drift → 必须单条 config-mismatch。

两个用例都不依赖真实运行中的进程（用不可达端口 / 合成 conf），可在 CI 裸跑。
"""
import os
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[2] / "scripts" / "probe" / "route_contract_probe.py"


def _run(args, env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run([sys.executable, str(PROBE), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def test_probe_fails_loud_when_all_processes_unreachable(tmp_path):
    """四端口都不通 → 退出码必须非 0，且报出 liveness（此前是 PASS）。"""
    conf = tmp_path / "nginx.conf"
    conf.write_text(
        "upstream grape_server {\n    server 127.0.0.1:39991;\n}\n"
        "server {\n    listen 443;\n    location / {\n        proxy_pass http://grape_server;\n    }\n}\n",
        encoding="utf-8",
    )
    r = _run(["--conf", str(conf)],
             {"GRAPE_PROBE_PORTS": "mgmt=39981,event=39982,worker=39983,insight=39984"})
    assert r.returncode != 0, r.stdout
    assert "liveness" in r.stdout
    assert "RESULT: PASS" not in r.stdout


def test_probe_reports_single_config_mismatch_when_ports_disagree(tmp_path):
    """upstream 名与端口角色表冲突 → 单条 config-mismatch，而不是逐路径噪音。"""
    conf = tmp_path / "nginx.conf"
    conf.write_text(
        "upstream grape_server {\n    server 127.0.0.1:39982;\n}\n"
        "upstream grape_event_server {\n    server 127.0.0.1:39982;\n}\n"
        "server {\n    listen 443;\n    location / {\n        proxy_pass http://grape_server;\n    }\n}\n",
        encoding="utf-8",
    )
    r = _run(["--conf", str(conf)],
             {"GRAPE_PROBE_PORTS": "mgmt=39981,event=39982,worker=39983,insight=39984"})
    assert r.returncode != 0, r.stdout
    assert "config-mismatch" in r.stdout
    assert r.stdout.count("config-mismatch") <= 2  # 单条结论，不刷屏