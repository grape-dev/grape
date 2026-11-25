"""Issue #3759：上游永久失败熔断（仓库级存在性台账 + 退避 + 短路 + 自愈）。

验收口径：
1. 404/403/401（429 除外）判定为永久失败；
2. 首次永久失败只触发**一次**仓库级 `/repos/{owner}/{repo}` 探测，后续同仓库 issue
   不再逐条探测；探测确认 missing → `vcs_missing`；
3. 熔断期内逐 issue 读短路为 missing（0 上游请求），退避放行时间 = 5m→15m→60m 封顶；
4. 上游恢复（探测 exists / 正常读成功）→ 清除 `vcs_missing`，订阅自动恢复；
5. 台账读不到 → fail-open（不短路）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domains.runtime.vcs import issue_poll_breaker as br  # noqa: E402
from src.domains.runtime.pipeline import event_pipeline as ep  # noqa: E402

RK = "gitcode:huaweicloud-mate/repository-requests"
T0 = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc)


class _FakeStore:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.writes: list[tuple[str, dict]] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(br, "_read_state_db", self.read)
        monkeypatch.setattr(br, "_write_state_db", self.write)

    def read(self, repo_key):
        return dict(self.rows.get(repo_key, {}))

    def write(self, repo_key, fields):
        self.writes.append((repo_key, dict(fields)))
        self.rows[repo_key] = {**self.rows.get(repo_key, {}), **fields}
        return True


@pytest.fixture(autouse=True)
def _clean_cache():
    br.clear_cache()
    yield
    br.clear_cache()


@pytest.fixture
def store(monkeypatch):
    s = _FakeStore()
    s.install(monkeypatch)
    return s


@pytest.fixture
def probe_calls(monkeypatch):
    calls = []

    def _probe(repo_key, *, client=None):
        calls.append(repo_key)
        return "missing"

    monkeypatch.setattr(br, "probe_repo", _probe)
    return calls


class TestClassification:
    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 451])
    def test_permanent_4xx(self, code):
        assert br.is_permanent_status(code) is True

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504, 200, 302, None, "", "abc"])
    def test_retryable_or_invalid(self, code):
        assert br.is_permanent_status(code) is False

    def test_backoff_ladder_caps_at_one_hour(self):
        assert br.backoff_seconds(1) == 300
        assert br.backoff_seconds(2) == 900
        assert br.backoff_seconds(3) == 3600
        assert br.backoff_seconds(99) == 3600
        assert br.backoff_seconds(0) == 300
        assert br.backoff_seconds(None) == 300


class TestProbeAndFuse:
    def test_first_404_probes_once_and_fuses(self, store, probe_calls):
        st = br.note_permanent_failure(RK, 404, operation="issues/62", now=T0)
        assert probe_calls == [RK]
        assert st["vcs_status"] == "vcs_missing"
        assert st["vcs_probe_result"] == "missing"
        assert st["vcs_fail_count"] == 1
        assert st["vcs_missing_since"]
        assert store.rows[RK]["vcs_last_error_code"] == "404"
        # 首次失败 → 退避档位 1（5 分钟）
        next_probe = datetime.fromisoformat(st["vcs_next_probe_at"])
        assert next_probe == T0 + timedelta(seconds=300)

    def test_second_issue_404_does_not_reprobe_within_backoff(self, store, probe_calls):
        br.note_permanent_failure(RK, 404, operation="issues/62", now=T0)
        br.note_permanent_failure(RK, 404, operation="issues/63", now=T0 + timedelta(seconds=30))
        # 仓库在熔断中且未到探测点 → 不再探测（告警/请求量不随 issue 数增长）
        assert probe_calls == [RK]

    def test_blocked_during_backoff_and_released_at_due(self, store, probe_calls):
        br.note_permanent_failure(RK, 404, operation="issues/62", now=T0)
        blocked, st = br.blocked(RK, now=T0 + timedelta(seconds=60))
        assert blocked is True and st["vcs_status"] == "vcs_missing"
        blocked, _ = br.blocked(RK, now=T0 + timedelta(seconds=299))
        assert blocked is True
        # 到点放行一次探测（放行后由读路径重新探测并自愈/再熔断）
        blocked, _ = br.blocked(RK, now=T0 + timedelta(seconds=300))
        assert blocked is False

    def test_probe_exists_does_not_fuse_repo(self, store, monkeypatch):
        monkeypatch.setattr(br, "probe_repo", lambda repo_key, *, client=None: "exists")
        st = br.note_permanent_failure(RK, 404, operation="issues/62", now=T0)
        assert st["vcs_status"] == "ok"
        assert br.blocked(RK, now=T0)[0] is False

    def test_probe_unknown_does_not_fuse_repo(self, store, monkeypatch):
        monkeypatch.setattr(br, "probe_repo", lambda repo_key, *, client=None: "unknown")
        st = br.note_permanent_failure(RK, 404, operation="issues/62", now=T0)
        assert st["vcs_status"] == "ok"
        assert br.blocked(RK, now=T0)[0] is False

    def test_429_is_not_permanent_and_never_probes(self, store, probe_calls):
        st = br.note_permanent_failure(RK, 429, operation="issues/62", now=T0)
        assert probe_calls == []
        assert st.get("vcs_status") != "vcs_missing"
        assert br.blocked(RK, now=T0)[0] is False

    def test_probe_backoff_ladder_grows_with_failures(self, store, probe_calls):
        br.note_permanent_failure(RK, 404, now=T0)
        # 第 1 次探测后 5 分钟到点 → 第 2 次探测，退避升到 15 分钟
        br.note_permanent_failure(RK, 404, now=T0 + timedelta(seconds=300))
        assert len(probe_calls) == 2
        st = store.rows[RK]
        assert datetime.fromisoformat(st["vcs_next_probe_at"]) == \
            T0 + timedelta(seconds=300) + timedelta(seconds=900)
        # 第 3 次 → 60 分钟封顶
        br.note_permanent_failure(RK, 404, now=T0 + timedelta(seconds=1200))
        assert len(probe_calls) == 3
        st = store.rows[RK]
        assert datetime.fromisoformat(st["vcs_next_probe_at"]) == \
            T0 + timedelta(seconds=1200) + timedelta(seconds=3600)

    def test_repeated_failures_are_throttled_for_db_writes(self, store, probe_calls):
        """批量复核 8 并发 × N 条：同一仓库短时间内的重复 404 不得逐条写台账。"""
        br.note_permanent_failure(RK, 404, operation="issues/62", now=T0)
        writes_after_first = len(store.writes)
        for _ in range(5):
            br.note_permanent_failure(RK, 404, operation="issues/63",
                                      now=T0 + timedelta(seconds=1))
        assert len(store.writes) == writes_after_first
        assert len(probe_calls) == 1
        # 超过最小间隔后恢复记账
        br.note_permanent_failure(RK, 404, now=T0 + timedelta(seconds=60))
        assert len(store.writes) > writes_after_first


class TestRecovery:
    def test_recovery_clears_fuse(self, store, probe_calls):
        br.note_permanent_failure(RK, 404, now=T0)
        assert br.blocked(RK, now=T0)[0] is True
        assert br.note_recovery(RK, operation="issues/62", now=T0 + timedelta(seconds=10)) is True
        assert br.blocked(RK, now=T0 + timedelta(seconds=10))[0] is False
        st = br.state(RK, fresh=True)
        assert st["vcs_status"] == "ok"
        assert st["vcs_missing_since"] == ""
        assert int(st["vcs_fail_count"]) == 0

    def test_healthy_repo_recovery_is_zero_write(self, store):
        store.rows[RK] = {"vcs_status": "ok", "vcs_fail_count": 0,
                          "vcs_last_error_code": "", "vcs_next_probe_at": ""}
        before = len(store.writes)
        assert br.note_recovery(RK) is False
        assert len(store.writes) == before  # 健康仓库每次读不得产生跨域写

    def test_reset_clears_all_fields(self, store, probe_calls):
        br.note_permanent_failure(RK, 404, now=T0)
        after = br.reset(RK, reason="ops_rebind", now=T0 + timedelta(seconds=5))
        assert after["vcs_status"] == "ok"
        assert after["vcs_missing_since"] == ""
        assert after["vcs_probe_result"] == ""
        assert int(after["vcs_fail_count"]) == 0
        assert br.blocked(RK, now=T0 + timedelta(seconds=5))[0] is False


class TestFailOpen:
    def test_unreadable_ledger_never_blocks(self, monkeypatch):
        monkeypatch.setattr(br, "_read_state_db", lambda repo_key: None)
        br.clear_cache()
        assert br.blocked(RK)[0] is False
        # 写失败也不得抛异常（读路径不得因台账故障改变语义）
        assert br.note_permanent_failure(RK, 404) is not None

    def test_missing_repo_card_does_not_block(self, store):
        # 无台账行（未托管/无卡）→ 不熔断
        assert br.blocked("gitcode:nobody/nothing")[0] is False


class TestReadPathShortCircuit:
    def test_vcs_get_issue_short_circuits_when_blocked(self, store, probe_calls, monkeypatch):
        # 熔断窗口必须覆盖「真实 now」（blocked() 不注入时钟）：用当前时间熔断。
        br.note_permanent_failure(RK, 404, now=datetime.now(timezone.utc))

        class _BoomClient:
            def get_issue(self, *a, **kw):  # pragma: no cover - 不应被调用
                raise AssertionError("blocked repo must not hit upstream")

        monkeypatch.setattr(ep, "_build_issue_read_client", lambda rk, tok: _BoomClient())
        monkeypatch.setattr(ep, "_resolve_issue_read_token", lambda rk: "tk_fake")
        assert ep._vcs_get_issue(RK, "62") == ("missing", {})
        assert ep._vcs_get_issue(RK, "63") == ("missing", {})

    def test_vcs_get_issue_404_marks_ledger(self, store, probe_calls, monkeypatch):
        class _Client404:
            def get_issue(self, *a, **kw):
                return {"error": "HTTP 404", "status_code": 404, "detail": "Project not found"}

        monkeypatch.setattr(ep, "_build_issue_read_client", lambda rk, tok: _Client404())
        monkeypatch.setattr(ep, "_resolve_issue_read_token", lambda rk: "tk_fake")
        assert ep._vcs_get_issue(RK, "62") == ("missing", {})
        assert probe_calls == [RK]
        assert store.rows[RK]["vcs_status"] == "vcs_missing"

    def test_vcs_get_issue_success_clears_fuse(self, store, probe_calls, monkeypatch):
        br.note_permanent_failure(RK, 404, now=T0)

        class _ClientOK:
            def get_issue(self, *a, **kw):
                return {"number": 62, "title": "back"}

        monkeypatch.setattr(ep, "_build_issue_read_client", lambda rk, tok: _ClientOK())
        monkeypatch.setattr(ep, "_resolve_issue_read_token", lambda rk: "tk_fake")
        # 熔断中仍短路，不会打上游；放行到点后成功读 → 清除熔断
        store.rows[RK]["vcs_next_probe_at"] = T0.isoformat()
        br.clear_cache()
        state, data = ep._vcs_get_issue(RK, "62")
        assert state == "exists" and data["title"] == "back"
        assert store.rows[RK]["vcs_status"] == "ok"


class TestUpstreamReqDedupe:
    def test_repeated_404_logs_once_per_window(self, monkeypatch, caplog):
        from src.api import git_repo_api as api
        from src.domains.mgmt.user.repo_manager import UpstreamAPIError

        def _raise(*a, **kw):
            raise UpstreamAPIError("GET", "https://gitcode.com/api/v5/repos/o/r/issues/62",
                                   404, "Project not found", provider="GitCode")

        monkeypatch.setattr(api, "_provider_module", lambda p: type("M", (), {"request": staticmethod(_raise)})())
        api._UPSTREAM_LOG_DEDUPER = None
        url = "https://gitcode.com/api/v5/repos/o/r/issues/{}"

        with caplog.at_level("WARNING"):
            for n in ("62", "63", "62", "63"):
                with pytest.raises(Exception):
                    api._vcs_req("gitcode", "GET", url.format(n), "tk")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"
                    and "upstream error" in r.getMessage()]
        # issues/62 与 issues/63 归一为同一 key → 窗口内只留 1 条完整 WARNING
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "issues/62" in msg and "permanent=True" in msg
        assert "hint=disable subscription" in msg

    def test_status_code_passthrough_preserved(self, monkeypatch):
        from src.api import git_repo_api as api
        from src.domains.mgmt.user.repo_manager import UpstreamAPIError
        from fastapi import HTTPException

        def _raise(*a, **kw):
            raise UpstreamAPIError("GET", "https://gitcode.com/api/v5/repos/o/r", 403, "nope")

        monkeypatch.setattr(api, "_provider_module", lambda p: type("M", (), {"request": staticmethod(_raise)})())
        with pytest.raises(HTTPException) as exc:
            api._vcs_req("gitcode", "GET", "https://gitcode.com/api/v5/repos/o/r", "tk")
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == 403

    def test_429_is_not_flagged_permanent(self, monkeypatch, caplog):
        from src.api import git_repo_api as api
        from src.domains.mgmt.user.repo_manager import UpstreamAPIError
        import re

        def _raise(*a, **kw):
            raise UpstreamAPIError("GET", "https://gitcode.com/api/v5/repos/o/r/issues/1", 429, "rate")

        monkeypatch.setattr(api, "_provider_module", lambda p: type("M", (), {"request": staticmethod(_raise)})())
        api._UPSTREAM_LOG_DEDUPER = None
        with caplog.at_level("WARNING"):
            with pytest.raises(Exception):
                api._vcs_req("gitcode", "GET", "https://gitcode.com/api/v5/repos/o/r/issues/1", "tk")
        msgs = [r.getMessage() for r in caplog.records if "upstream error" in r.getMessage()]
        assert msgs and re.search(r"permanent=False", msgs[0])