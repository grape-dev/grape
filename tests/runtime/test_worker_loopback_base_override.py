"""远端 worker：回环业务基址必须被本 worker 可达地址覆盖（实测 GitLab #9/#10）。

沙箱里的 127.0.0.1 是沙箱自己，pipeline 若把 GRAPE_SERVER_URL 写成回环，CLI 的
每个 API 调用都会打到本机 nginx → 301 → https 证书主机名不匹配。回连基址
（RUNTIME_SERVER_URL）与业务基址是两个键，不能因为前者相等就跳过修正。
"""


def test_loopback_business_base_is_overridden():
    from src.domains.runtime.engine.agent import _apply_local_worker_base_override

    env = {"GRAPE_SERVER_URL": "http://127.0.0.1",
           "RUNTIME_SERVER_URL": "http://192.168.0.227"}
    task = {"server_url": "http://192.168.0.227"}
    _apply_local_worker_base_override(env, task, "http://192.168.0.227")
    assert env["GRAPE_SERVER_URL"] == "http://192.168.0.227"
    assert env["RUNTIME_SERVER_URL"] == "http://192.168.0.227"


def test_explicit_business_base_is_preserved():
    """双地址部署（业务基址与回连基址不同主机）必须原样保留。"""
    from src.domains.runtime.engine.agent import _apply_local_worker_base_override

    env = {"GRAPE_SERVER_URL": "https://grape.example.com",
           "RUNTIME_SERVER_URL": "http://192.168.0.227"}
    task = {"server_url": "http://192.168.0.227"}
    _apply_local_worker_base_override(env, task, "http://192.168.0.227")
    assert env["GRAPE_SERVER_URL"] == "https://grape.example.com"