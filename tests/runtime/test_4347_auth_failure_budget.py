"""Issue #4347：runtime 鉴权失败 IP 失败预算（>10/min → 429；>60/h → 短期封禁）
+ 拒绝日志按 IP 聚合节流（同一 IP 每分钟最多 1 条摘要）单测。

覆盖验收标准：
- 失败预算计数（分钟/小时滑动窗口）；
- 429 返回路径（Retry-After 头；封禁期内入口直接 429；只计鉴权失败，成功不受限）；
- 聚合日志节流（同一 IP 每分钟最多 1 条摘要，含累计次数 + 首末时间）。
"""
import hashlib
import secrets

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.domains.runtime.routes.auth_failure_budget import (AuthFailureBudget,
                                                            auth_failure_gate)
from src.domains.runtime.routes.runtime import _auth_failure_key
from src.kernel.session_manager import db, db_lock

T0 = 1_700_000_000.0  # 固定时间基点（滑动窗口单测可复现）


def _make_token(name="test_4347_cred"):
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        db.cursor().execute(
            "INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), name, "global"))
        db.commit()
    return token


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'test_4347%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'test_4347%'")
        db.commit()


@pytest.fixture
def client():
    return TestClient(__import__("src.api.main", fromlist=["app"]).app)


class TestGateMinuteBudget:
    def test_first_10_reject_then_429_threshold(self):
        gate = AuthFailureBudget()
        for i in range(10):
            out = gate.record_failure("1.1.1.1", "rta_AAAA", "/api/runtime/register", now=T0 + i)
            assert out.action == "reject"
        # 第 11 次（>10/min）→ 429
        out = gate.record_failure("1.1.1.1", "rta_AAAA", "/api/runtime/register", now=T0 + 10)
        assert out.action == "throttle"
        assert 0 < out.retry_after <= 61
        # 分钟窗口滑出后恢复 401（未达小时阈值不封禁）
        out = gate.record_failure("1.1.1.1", "rta_AAAA", "/api/runtime/register", now=T0 + 61)
        assert out.action == "reject"
        assert gate.check_banned("1.1.1.1", now=T0 + 62) == 0

    def test_minute_threshold_env_overridable(self, monkeypatch):
        gate = AuthFailureBudget()
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "2")
        for i in range(2):
            out = gate.record_failure("9.9.9.9", "rta_X", "/e", now=T0 + i)
            assert out.action == "reject"
        out = gate.record_failure("9.9.9.9", "rta_X", "/e", now=T0 + 2)
        assert out.action == "throttle"


class TestGateHourBan:
    def test_hour_cumulative_bans_and_expires(self):
        gate = AuthFailureBudget()
        # 60 次分散在 10 个分钟窗口（每窗口 6 次 < 10，不触发分钟 429）
        for m in range(10):
            for i in range(6):
                out = gate.record_failure("2.2.2.2", "rta_BBBB", "/api/runtime/heartbeat",
                                          now=T0 + m * 60 + i)
                assert out.action == "reject"
        # 1 小时内第 61 次 → 短期封禁（默认 600s）
        out = gate.record_failure("2.2.2.2", "rta_BBBB", "/api/runtime/heartbeat", now=T0 + 620)
        assert out.action == "ban"
        assert out.retry_after == 600
        assert gate.check_banned("2.2.2.2", now=T0 + 621) > 0
        # 封禁期内的失败不计数（防封禁被无限续期）
        out = gate.record_failure("2.2.2.2", "rta_BBBB", "/api/runtime/heartbeat", now=T0 + 622)
        assert out.action == "ban"
        # 封禁过期后解除
        assert gate.check_banned("2.2.2.2", now=T0 + 622 + 601) == 0

    def test_hour_threshold_env_overridable(self, monkeypatch):
        gate = AuthFailureBudget()
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "3")
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_HOUR_LIMIT", "4")
        monkeypatch.setenv("RUNTIME_AUTH_BAN_SECONDS", "120")
        # 跨分钟分布：每个分钟窗口 ≤2 次，不触发分钟限流，只累计小时窗口
        for ts in (T0, T0 + 1, T0 + 61, T0 + 62):
            out = gate.record_failure("8.8.8.8", "rta_Y", "/e", now=ts)
            assert out.action == "reject"
        # 1 小时内第 5 次（>4）→ 封禁
        out = gate.record_failure("8.8.8.8", "rta_Y", "/e", now=T0 + 121)
        assert out.action == "ban" and out.retry_after == 120
        assert gate.check_banned("8.8.8.8", now=T0 + 122) > 0
        assert gate.check_banned("8.8.8.8", now=T0 + 250) == 0


class TestGateLogAggregation:
    def test_one_summary_per_window_with_cumulative_count(self):
        gate = AuthFailureBudget()
        lines = []
        for i in range(5):
            out = gate.record_failure("3.3.3.3", "rta_C", "/api/runtime/register", now=T0 + i)
            if out.log_line:
                lines.append(out.log_line)
        # 同一 60s 窗口内最多 1 条摘要
        assert len(lines) == 1
        assert "runtime credential rejected" in lines[0]
        # 下一窗口首条摘要携带被抑制的累计（4 条被抑制 + 当前 1 条）
        out = gate.record_failure("3.3.3.3", "rta_C", "/api/runtime/claim", now=T0 + 61)
        assert out.log_line
        assert "window_count=5" in out.log_line
        assert "first=" in out.log_line and "last=" in out.log_line
        assert "/api/runtime/claim" in out.log_line and "/api/runtime/register" in out.log_line
        # 本窗口后续失败继续被抑制
        out = gate.record_failure("3.3.3.3", "rta_C", "/api/runtime/claim", now=T0 + 62)
        assert out.log_line == ""

    def test_ban_event_forces_visible_summary(self, monkeypatch):
        gate = AuthFailureBudget()
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "100")
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_HOUR_LIMIT", "5")
        # 同窗口首条失败照常发摘要，后续被抑制并累计
        out = gate.record_failure("5.5.5.5", "rta_D", "/e", now=T0)
        assert out.log_line != ""
        for i in range(4):
            out = gate.record_failure("5.5.5.5", "rta_D", "/e", now=T0 + 1 + i)
            assert out.log_line == ""
        # 第 6 次（小时 >5）触发封禁：即使本窗口已发过摘要，状态迁移必须强制可见，
        # 且摘要带上被抑制的累计（4 条抑制 + 当前 1 条 = window_count=5）
        out = gate.record_failure("5.5.5.5", "rta_D", "/e", now=T0 + 6)
        assert out.action == "ban"
        assert out.log_line != "" and "action=ban" in out.log_line
        assert "window_count=5" in out.log_line

    def test_per_ip_isolation(self):
        gate = AuthFailureBudget()
        for i in range(12):
            gate.record_failure("6.6.6.6", "rta_E", "/e", now=T0 + i)
        out = gate.record_failure("7.7.7.7", "rta_E", "/e", now=T0 + 12)
        assert out.action == "reject"
        assert out.log_line != ""  # 独立窗口账本，首次失败照常留痕


class TestRegister429Path:
    """鉴权失败预算在 /api/runtime/register 上的行为（HTTP 层）。"""

    def test_register_429_after_minute_budget(self, client, monkeypatch):
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "3")
        bad = {"Authorization": "Bearer rta_" + "x" * 40}
        for _ in range(3):
            r = client.post("/api/runtime/register", headers=bad, json={"runtimes": []})
            assert r.status_code == 401
        # 第 4 次（>3/min）→ 429 + Retry-After
        r = client.post("/api/runtime/register", headers=bad, json={"runtimes": []})
        assert r.status_code == 429
        assert r.headers.get("Retry-After", "").isdigit()
        assert r.json().get("detail", {}).get("code") == 429

    def test_valid_credential_unaffected_by_budget(self, client, monkeypatch):
        """只计鉴权失败：超预算 IP 的成功注册不受影响（多 worker 共享出口 NAT 不误伤）。"""
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "3")
        bad = {"Authorization": "Bearer rta_" + "x" * 40}
        for _ in range(4):
            client.post("/api/runtime/register", headers=bad, json={"runtimes": []})
        token = _make_token()
        r = client.post("/api/runtime/register",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"device_name": "ci-4347", "runtimes": [{"name": "opencode"}]})
        assert r.status_code == 200

    def test_ban_blocks_all_then_expires(self, client, monkeypatch):
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "3")
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_HOUR_LIMIT", "3")
        monkeypatch.setenv("RUNTIME_AUTH_BAN_SECONDS", "2")
        token = _make_token()
        bad = {"Authorization": "Bearer rta_" + "x" * 40}
        for _ in range(4):
            r = client.post("/api/runtime/register", headers=bad, json={"runtimes": []})
        assert r.status_code == 429  # 第 4 次：小时累计 >3 → 封禁
        # 封禁期内：有效凭据也被入口 429 拦截（该 IP 已进入短期封禁）
        r = client.post("/api/runtime/register",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"runtimes": []})
        assert r.status_code == 429
        assert r.headers.get("Retry-After", "").isdigit()
        # 封禁过期后恢复
        import time as _time
        _time.sleep(2.5)
        r = client.post("/api/runtime/register",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"runtimes": [{"name": "opencode"}]})
        assert r.status_code == 200


class TestGateReset:
    def test_singleton_reset_clears_state(self):
        auth_failure_gate.record_failure("1.2.3.4", "rta_Z", "/e", now=T0)
        auth_failure_gate.reset()
        assert auth_failure_gate.check_banned("1.2.3.4", now=T0 + 1) == 0
        out = auth_failure_gate.record_failure("1.2.3.4", "rta_Z", "/e", now=T0 + 2)
        assert out.action == "reject"

def _fake_request(client_host: str, xff: str | None = None) -> Request:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/api/runtime/register",
        "query_string": b"", "headers": headers, "client": (client_host, 12345),
        "server": ("testserver", 80), "scheme": "http",
    })


class TestSecureBudgetKey:
    """C1：预算/封禁键不能被客户端伪造/轮换的 X-Forwarded-For 操控。"""

    def test_untrusted_peer_ignores_forged_xff(self):
        assert _auth_failure_key(_fake_request("203.0.113.7", "9.9.9.9")) == "203.0.113.7"
        assert _auth_failure_key(_fake_request("203.0.113.7", "1.1.1.1")) == "203.0.113.7"

    def test_trusted_proxy_accepts_xff(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.1")
        assert _auth_failure_key(_fake_request("10.0.0.1", "198.51.100.4")) == "198.51.100.4"
        # 非白名单对端即使带 XFF 也不采信
        assert _auth_failure_key(_fake_request("10.0.0.2", "198.51.100.4")) == "10.0.0.2"

    def test_loopback_default_trusted(self):
        # 缺省信任本机回环（nginx 与 worker 同机部署的安全默认）
        assert _auth_failure_key(_fake_request("127.0.0.1", "198.51.100.8")) == "198.51.100.8"


class TestRetryAfterAlignment:
    def test_retry_after_matches_sliding_window_expiry(self):
        """R1：按返回的 Retry-After 等待后，下一次失败不再被 429。"""
        gate = AuthFailureBudget()
        for i in range(10):
            assert gate.record_failure("4.4.4.4", "rta_R", "/e", now=T0 + i).action == "reject"
        out = gate.record_failure("4.4.4.4", "rta_R", "/e", now=T0 + 10)
        assert out.action == "throttle"
        # 第 1 条失败在 T0+60 滑出，now=T0+10 → 50s 后 +1s 保护
        assert out.retry_after == 51
        after = gate.record_failure("4.4.4.4", "rta_R", "/e", now=T0 + 10 + out.retry_after)
        assert after.action == "reject"


class TestUnknownIpLogging:
    def test_empty_ip_logs_fixed_key_without_counting(self):
        """R2：空 IP 仍留痕（ip=unknown），但不计数、不封禁。"""
        gate = AuthFailureBudget()
        lines = []
        for i in range(5):
            out = gate.record_failure("", "rta_U", "/e", now=T0 + i)
            assert out.action == "reject" and out.retry_after == 0
            if out.log_line:
                lines.append(out.log_line)
        assert len(lines) == 1
        assert "runtime credential rejected" in lines[0]
        assert "ip=unknown" in lines[0]
        # 下一窗口继续留痕（聚合节流不吞掉整类事件）
        out = gate.record_failure("", "rta_U", "/e", now=T0 + 61)
        assert out.log_line != "" and "ip=unknown" in out.log_line


class TestForgedXffHttpPath:
    def test_rotating_xff_cannot_bypass_minute_budget(self, client, monkeypatch):
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "3")
        bad_token = "rta_" + "x" * 40
        for i in range(3):
            r = client.post("/api/runtime/register",
                            headers={"Authorization": f"Bearer {bad_token}",
                                     "X-Forwarded-For": f"198.51.100.{i + 10}"},
                            json={"runtimes": []})
            assert r.status_code == 401
        r = client.post("/api/runtime/register",
                        headers={"Authorization": f"Bearer {bad_token}",
                                 "X-Forwarded-For": "198.51.100.99"},
                        json={"runtimes": []})
        assert r.status_code == 429

    def test_forged_victim_ip_not_banned(self, client, monkeypatch):
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "100")
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_HOUR_LIMIT", "3")
        monkeypatch.setenv("RUNTIME_AUTH_BAN_SECONDS", "600")
        bad = {"Authorization": "Bearer rta_" + "x" * 40,
               "X-Forwarded-For": "203.0.113.9"}
        for _ in range(3):
            assert client.post("/api/runtime/register", headers=bad,
                               json={"runtimes": []}).status_code == 401
        # 伪造受害方 IP 不进入封禁；同一 key(TestClient 对端) 的有效凭据仍可访问
        token = _make_token()
        r = client.post("/api/runtime/register",
                        headers={"Authorization": f"Bearer {token}",
                                 "X-Forwarded-For": "203.0.113.9"},
                        json={"device_name": "ci-4347-xff", "runtimes": [{"name": "opencode"}]})
        assert r.status_code == 200


class TestWhoamiCtx429Path:
    def test_whoami_invalid_token_uses_budget(self, client, monkeypatch):
        """_require_credential_ctx（whoami）失败分支同样接入预算与 429。"""
        monkeypatch.setenv("RUNTIME_AUTH_FAIL_MINUTE_LIMIT", "2")
        bad = {"Authorization": "Bearer rta_" + "y" * 40}
        for _ in range(2):
            assert client.get("/api/runtime/whoami", headers=bad).status_code == 401
        r = client.get("/api/runtime/whoami", headers=bad)
        assert r.status_code == 429
        assert r.headers.get("Retry-After", "").isdigit()
