"""收件箱消费期语义（对齐 PR #1854）：托管复验 / 租约续期 / 完成行清理。

三条不变量（都出自 PR #1854 的实现，我们在通用 inbox 上做）：
1. 投递入队后、消费前仓库若被解除托管 → 不再执行（托管=授权，撤销要生效）；
2. 流水线时长可能远超租约（300s）→ 消费期间必须续租，否则第二个 worker 会重复认领；
3. 已完成（consumed）行 24h 后清理、dead 行 7 天后清理；pending/failed 永不清。
"""
import asyncio
import json
import time

import pytest

from src.domains.runtime import webhook_inbox as wi


def _item(**kw):
    base = {"id": 7, "repo_key": "gitlab:yangaiwu/demo", "headers": json.dumps({"x-gitlab-event": "Push Hook"}),
            "payload": "{}", "attempts": 0}
    base.update(kw)
    return base


def test_unhosted_delivery_is_skipped_without_running(monkeypatch):
    calls = {"pipeline": 0, "finish": []}
    monkeypatch.setattr(wi, "is_still_hosted", lambda repo_key: False)
    monkeypatch.setattr(wi, "finish_claim",
                        lambda iid, owner, ok, error="": calls["finish"].append((iid, ok, error)) or {"status": "consumed"})

    def _pipeline(*a, **kw):
        calls["pipeline"] += 1

    wi._consume_one(_item(), "owner-1", pipeline=_pipeline)
    assert calls["pipeline"] == 0
    assert calls["finish"] == [(7, True, "unhosted: skipped")]


def test_hosted_delivery_runs_and_is_consumed(monkeypatch):
    seen = {}
    monkeypatch.setattr(wi, "is_still_hosted", lambda repo_key: True)
    monkeypatch.setattr(wi, "finish_claim",
                        lambda iid, owner, ok, error="": seen.update(ok=ok, error=error) or {"status": "consumed"})

    def _pipeline(body, headers, account="", repo_key_override=""):
        seen.update(account=account, repo=repo_key_override, headers=headers)

    wi._consume_one(_item(), "owner-1", pipeline=_pipeline)
    assert seen["ok"] is True and seen["error"] == ""
    assert seen["account"] == "yangaiwu/demo" and seen["repo"] == "gitlab:yangaiwu/demo"
    assert seen["headers"] == {"x-gitlab-event": "Push Hook"}


def test_pipeline_failure_marks_retry(monkeypatch):
    seen = {}
    monkeypatch.setattr(wi, "is_still_hosted", lambda repo_key: True)
    monkeypatch.setattr(wi, "finish_claim",
                        lambda iid, owner, ok, error="": seen.update(ok=ok, error=error) or {"status": "failed"})

    def _boom(*a, **kw):
        raise RuntimeError("pipeline exploded")

    wi._consume_one(_item(), "owner-1", pipeline=_boom)
    assert seen["ok"] is False and "pipeline exploded" in seen["error"]


def test_hosting_recheck_fails_open_when_contract_unavailable(monkeypatch):
    """GSB 不可用时不能把整批投递卡死：按托管处理（fail-open）。"""
    import src.kernel.gsb.provider as gsb

    def _boom(_name):
        raise RuntimeError("gsb down")

    monkeypatch.setattr(gsb, "get_service", _boom)
    assert wi.is_still_hosted("gitlab:yangaiwu/demo") is True


def test_hosting_recheck_reports_unmanaged(monkeypatch):
    import src.kernel.gsb.provider as gsb

    class _Svc:
        def get_repo_webhook_config(self, repo_key):
            return {}

    monkeypatch.setattr(gsb, "get_service", lambda _name: _Svc())
    assert wi.is_still_hosted("gitlab:yangaiwu/demo") is False


def test_lease_is_renewed_during_long_pipeline(monkeypatch):
    renewals = []
    monkeypatch.setattr(wi, "renew_claim", lambda iid, owner: renewals.append(iid) or True)
    monkeypatch.setattr(wi, "is_still_hosted", lambda repo_key: True)
    monkeypatch.setattr(wi, "finish_claim",
                        lambda iid, owner, ok, error="": {"status": "consumed"})

    def _slow_pipeline(*a, **kw):
        time.sleep(0.35)

    result = asyncio.run(wi._run_with_lease(_item(), "owner-1", pipeline=_slow_pipeline,
                                            renew_interval=0.05))
    assert result["status"] == "consumed"
    assert len(renewals) >= 2, f"租约未在长任务期间续期: {renewals}"


def test_lease_lost_stops_renewing(monkeypatch):
    state = {"n": 0}

    def _renew(iid, owner):
        state["n"] += 1
        return False

    monkeypatch.setattr(wi, "renew_claim", _renew)
    monkeypatch.setattr(wi, "is_still_hosted", lambda repo_key: True)
    monkeypatch.setattr(wi, "finish_claim",
                        lambda iid, owner, ok, error="": {"status": "lost"})

    asyncio.run(wi._run_with_lease(_item(), "owner-1",
                                   pipeline=lambda *a, **kw: time.sleep(0.2),
                                   renew_interval=0.05))
    assert state["n"] == 1, "租约丢失后应停止续租（避免覆盖别人的执行）"


class _FakeCursor:
    def __init__(self, log):
        self.log = log
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.log.append((" ".join(str(sql).split()), params))
        self.rowcount = 3 if "status='consumed'" in sql else 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDb:
    def __init__(self):
        self.log = []
        self.committed = 0

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        self.committed += 1


def test_purge_only_touches_consumed_and_dead(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr(wi, "db", fake)
    monkeypatch.setattr(wi, "db_lock", __import__("threading").Lock())
    out = wi.purge_finished()
    sql = " | ".join(s for s, _ in fake.log)
    assert out == {"consumed": 3, "dead": 1}
    assert "status='consumed'" in sql and "status='dead'" in sql
    assert "pending" not in sql and "'failed'" not in sql, "pending/failed 不得被清理"
    assert fake.committed == 1


def test_finish_claim_success_keeps_note():
    """成功路径也要把 note 落 last_error（如 unhosted: skipped），否则无从追溯。"""
    import inspect

    src = inspect.getsource(wi.finish_claim)
    assert 'status=\'consumed\'' in src
    assert "last_error=?" in src, "consumed 分支必须写 note，而不是恒清空"