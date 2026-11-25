"""tests/runtime 共享 fixture。

Issue #4347：runtime 鉴权失败预算是进程级单例，TestClient 默认来源 IP 均为
"testclient"——整套件同 IP 失败会跨用例累计、误触发 429/封禁殃及无关用例。
每条用例前后重置计数器，保证既有 401 语义用例不受影响。
"""
import pytest

from src.domains.runtime.routes.auth_failure_budget import auth_failure_gate


@pytest.fixture(autouse=True)
def _reset_auth_failure_budget():
    auth_failure_gate.reset()
    yield
    auth_failure_gate.reset()
