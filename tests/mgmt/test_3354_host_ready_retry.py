"""Issue #3354：host_after_create 对「刚建仓 VCS 未就绪」的 404 做有限退避重试。

现象：建仓返回成功，托管半失败（host_status=partial / webhook_id=0），
手工再执行一次 host 立刻成功 → 时序竞态。这里锁重试边界：
只对 404/repo_not_found 重试，权限/限流/5xx 立即抛出。
"""
import pytest

from src.domains.mgmt.routes import repos


def _err(code: int) -> RuntimeError:
    return RuntimeError(f"GitCode API POST https://gitcode.com/api/v5/repos/o/r/hooks failed ({code})")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # 退避时长不参与断言：置零避免用例真的等待 2+4+8s
    monkeypatch.setattr(repos, "_HOST_REPO_READY_DELAYS", (0, 0, 0))


def test_retry_until_repo_ready():
    calls = []

    def _flaky(*a, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise _err(404)
        return 87693

    assert repos._host_call_with_ready_retry(_flaky, "t", "o", "r", "u") == 87693
    assert len(calls) == 3, "两次 404 后应继续重试并成功"


def test_non_404_error_not_retried():
    calls = []

    def _denied(*a, **kw):
        calls.append(1)
        raise _err(403)

    with pytest.raises(RuntimeError):
        repos._host_call_with_ready_retry(_denied)
    assert len(calls) == 1, "403 重试无意义，必须立即抛出"


def test_gives_up_after_bounded_attempts():
    calls = []

    def _never_ready(*a, **kw):
        calls.append(1)
        raise _err(404)

    with pytest.raises(RuntimeError):
        repos._host_call_with_ready_retry(_never_ready)
    assert len(calls) == 1 + len(repos._HOST_REPO_READY_DELAYS), \
        "重试次数必须有界（1 次首发 + 固定退避次数）"