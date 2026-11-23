"""Issue #3969：网关 worker 上游多实例 + 故障切换 + keepalive 配置契约。

漂移形态（生产 2026-11-23）：`upstream grape_worker_server` 只有单台
`127.0.0.1:8083`，worker 重启先关监听 57s + 冷启动 36s 期间新连接被内核拒绝，
nginx 无可切换上游 → 91s 内 1,178 次 502。

seam（配置契约）：
- 两份反代配置的 worker upstream 必须含对等主控 include（缺省仅本机）与 keepalive；
- `/api/runtime/`、`/api/tasks/` 必须声明 proxy_next_upstream（故障切换）与 HTTP/1.1 keepalive；
- 幂等端点（heartbeat / register / state）允许 `non_idempotent` 重试；
- 生成的等价最小配置必须通过 `nginx -t`（语法可核验）。
"""
import os
import shutil
import subprocess
import tempfile

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_FOUR = os.path.join(_REPO_ROOT, "deploy/nginx-grape-four-process.conf")
_CLUSTER = os.path.join(_REPO_ROOT, "deploy/nginx-grape-cluster.conf")

_NEXT_UPSTREAM = "proxy_next_upstream error timeout http_502"


def _conf(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _block(conf: str, header: str) -> str:
    """按大括号配对截取以 header 起始的配置块。"""
    i = conf.index(header)
    j = conf.index("{", i)
    depth = 0
    for k in range(j, len(conf)):
        if conf[k] == "{":
            depth += 1
        elif conf[k] == "}":
            depth -= 1
            if depth == 0:
                return conf[i:k + 1]
    raise AssertionError(f"unbalanced block: {header}")


@pytest.mark.parametrize("path,upstream,peers", [
    (_FOUR, "grape_worker_server", "grape-worker-peers.conf"),
    (_CLUSTER, "grape_worker", "grape-cluster-worker-peers.conf"),
])
class TestWorkerUpstream:
    def test_peer_include_present(self, path, upstream, peers):
        block = _block(_conf(path), f"upstream {upstream}")
        assert f"include /etc/nginx/conf.d/{peers}*;" in block, \
            "对等主控 worker 端点需经 include 注入（缺省文件不存在 = 仅本机）"

    def test_keepalive_enabled(self, path, upstream, peers):
        block = _block(_conf(path), f"upstream {upstream}")
        assert "keepalive 32;" in block

    def test_local_server_has_failure_marking(self, path, upstream, peers):
        block = _block(_conf(path), f"upstream {upstream}")
        assert "max_fails=2" in block and "fail_timeout=10s" in block


@pytest.mark.parametrize("path", [_FOUR, _CLUSTER])
def test_runtime_location_has_next_upstream_and_keepalive(path):
    conf = _conf(path)
    block = _block(conf, "location /api/runtime/")
    assert _NEXT_UPSTREAM in block
    assert "proxy_next_upstream_tries 2;" in block
    assert "proxy_next_upstream_timeout 5s;" in block
    assert "proxy_http_version 1.1;" in block
    assert 'proxy_set_header Connection "";' in block


def test_four_process_tasks_location_has_next_upstream():
    conf = _conf(_FOUR)
    block = _block(conf, "location ~ ^/api/tasks/(notify|complete|current|awaiting-input)($|/)")
    assert _NEXT_UPSTREAM in block
    assert "proxy_http_version 1.1;" in block


def test_cluster_tasks_location_has_next_upstream():
    block = _block(_conf(_CLUSTER), "location /api/tasks/")
    assert _NEXT_UPSTREAM in block
    assert 'proxy_set_header Connection "";' in block


def test_four_process_idempotent_runtime_posts_allow_non_idempotent():
    """幂等端点（heartbeat/register/state）允许网关级 non_idempotent 重试。"""
    conf = _conf(_FOUR)
    block = _block(conf, "location ~ ^/api/runtime/(heartbeat")
    assert "non_idempotent" in block
    assert "proxy_next_upstream_tries 2;" in block


def _nginx_test(conf_text: str) -> subprocess.CompletedProcess:
    if not shutil.which("nginx"):
        pytest.skip("nginx not installed")
    tmp = tempfile.mkdtemp(prefix="grape-nginx-3969-")
    conf_path = os.path.join(tmp, "nginx.conf")
    wrapper = f"""
worker_processes 1;
pid {tmp}/nginx.pid;
error_log {tmp}/error.log;
events {{ worker_connections 64; }}
http {{
  access_log off;
  {conf_text}
}}
"""
    with open(conf_path, "w", encoding="utf-8") as f:
        f.write(wrapper)
    return subprocess.run(["nginx", "-t", "-c", conf_path],
                          capture_output=True, text=True)


def test_worker_upstream_snippet_passes_nginx_t():
    """把真实 worker upstream 块 + 关键 location 组装为最小配置，交给 nginx -t 校验语法。"""
    conf = _conf(_FOUR)
    upstream = _block(conf, "upstream grape_worker_server")
    runtime = _block(conf, "location /api/runtime/")
    idem = _block(conf, 'location ~ ^/api/runtime/(heartbeat')
    snippet = f"""
{upstream}
server {{
  listen 127.0.0.1:18098;
  {runtime}
  {idem}
}}
"""
    proc = _nginx_test(snippet)
    assert proc.returncode == 0, proc.stderr
    assert "successful" in proc.stderr