"""Issue #3469：验证码服务（verification.py）加固用例。

覆盖条目：
- A  UTC 时间串按 UTC 解析（东八区下不再秒过期）
- H  check_rate_limit 落影子记录（未注册邮箱也能推高邮箱/IP 限流计数）
- I  哈希落库带 kid 前缀，轮换期旧 kid 仍可校验
- J  校验匹配全部未使用码（重发不废旧码），失败对所有有效行累计 attempts
- M  邮件已发但落库失败 → 返回 ok=False / CODE_STORE_FAILED
- P  verify_code 返回结构化 error_code（NOT_FOUND/EXPIRED/USED/INVALID）
- Q  target 纳入哈希盐（发往 A 的码不能确认 B）
- count_sends 公开计数接口

全部用例走真实 HMAC 比对路径（不 mock 哈希、不用 dev 固定码 888888）。
"""
import os
import re
import sys
import time

import pytest

# 测试库隔离：必须在 import src.kernel.session_manager 之前设置
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
os.environ["DEVOPS_DB_PATH"] = "/tmp/test_issue_3469_verification.db"
os.environ["OBS_LOG_DB"] = "0"
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(os.environ["DEVOPS_DB_PATH"] + _suffix)
    except OSError:
        pass

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.api.db_setup import _create_tables  # noqa: E402
from src.domains.mgmt.auth import verification as v  # noqa: E402

_create_tables()

CODE = "135790"   # 非 dev 固定码
TARGET_A = "target-a@example.com"
TARGET_B = "target-b@example.com"


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

class _FakeEmailService:
    def send_verification_code(self, to, code, scene):
        return True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """清库 + 关闭 dev 验证码旁路，保证每条用例都走真实 HMAC 路径。"""
    monkeypatch.setattr(v, "is_dev_verification_code", lambda code: False)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM verification_codes WHERE email LIKE 't3469\\_%' ESCAPE '\\'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM verification_codes WHERE email LIKE 't3469\\_%' ESCAPE '\\'")
        db.commit()


def _send(monkeypatch, email, scene="register", code=CODE, target="",
          ip="10.11.12.13", rl_key=""):
    """发码：生成器固定返回 code，邮件服务始终成功。"""
    monkeypatch.setattr(v, "generate_code", lambda: code)
    monkeypatch.setattr(v, "get_email_service", lambda: _FakeEmailService())
    return v.send_code(email, scene, ip, rate_limit_key=rl_key, target=target)


def _insert(email, scene, code, target="", expires_in=600, used=0, attempts=0,
            ip="10.11.12.13", created_at=None):
    """直接往 verification_codes 插一行（绕开 60s 限流，便于构造场景）。"""
    now = created_at or v._utc_str(time.time())
    expires = v._utc_str(time.time() + expires_in)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO verification_codes "
            "(email, code_hash, scene, ip, attempts, used, expires_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (email, v._stored_hash(code, target), scene, ip, attempts, used, expires, now),
        )
        db.commit()
        return c.lastrowid


def _rows(email, scene):
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, code_hash, attempts, used, expires_at FROM verification_codes "
            "WHERE email=? AND scene=? ORDER BY id",
            (email, scene),
        )
        return c.fetchall()


# ---------------------------------------------------------------------------
# A：UTC 解析 + Q：target 绑定
# ---------------------------------------------------------------------------

def test_utc_expiry_not_instant_in_utc8(monkeypatch):
    """TZ=Asia/Shanghai 下发码后可立即校验；过期行返回 CODE_EXPIRED。"""
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        email = "t3469_tz@example.com"
        assert _send(monkeypatch, email, code="246801")["ok"] is True
        # 若用 time.mktime 解析（本地时区），东八区下 expires_at 提前 8 小时 → 这里会失败
        assert v.verify_code(email, "register", "246801") == {"ok": True}

        # 另起场景构造「已过期」行
        assert _send(monkeypatch, email, scene="reset_password", code="975310")["ok"] is True
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE verification_codes SET expires_at=? "
                "WHERE email=? AND scene='reset_password'",
                (v._utc_str(time.time() - 60), email),
            )
            db.commit()
        r = v.verify_code(email, "reset_password", "975310")
        assert r["ok"] is False
        assert r["error_code"] == "CODE_EXPIRED"
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_target_binding(monkeypatch):
    """Q：发往 A 的码不能确认 B；同一 target 才能命中。"""
    email_a = "t3469_q_a@example.com"
    assert _send(monkeypatch, email_a, code="112233", target=TARGET_A)["ok"] is True
    r = v.verify_code(email_a, "register", "112233", target=TARGET_B)
    assert r["ok"] is False and r["error_code"] == "CODE_INVALID"

    email_b = "t3469_q_b@example.com"
    assert _send(monkeypatch, email_b, code="112233", target=TARGET_A)["ok"] is True
    assert v.verify_code(email_b, "register", "112233", target=TARGET_A)["ok"] is True


# ---------------------------------------------------------------------------
# J：重发不废旧码 / 错码累计
# ---------------------------------------------------------------------------

def test_resend_keeps_old_code_valid(monkeypatch):
    """J：同 key+scene 两条未使用码，用第一封的码仍能通过，且两行都被消费。"""
    email = "t3469_j_resend@example.com"
    now = v._utc_str(time.time())
    _insert(email, "register", "111111", created_at=now)
    _insert(email, "register", "222222", created_at=v._utc_str(time.time() + 1))

    assert v.verify_code(email, "register", "111111") == {"ok": True}
    rows = _rows(email, "register")
    assert len(rows) == 2
    assert all(r[3] == 1 for r in rows), "命中后应把该 key+scene 全部未使用行置 used=1"


def test_wrong_code_increments_attempts_then_burns(monkeypatch):
    """J：错码 attempts+1 且返回 CODE_INVALID；连错 5 次该行置 used=1。"""
    email = "t3469_j_attempts@example.com"
    row_id = _insert(email, "register", "333333")

    for i in range(1, v.MAX_ATTEMPTS + 1):
        r = v.verify_code(email, "register", "999999")
        assert r["ok"] is False and r["error_code"] == "CODE_INVALID"
        rows = _rows(email, "register")
        assert rows[0][2] == i, f"第 {i} 次错码后 attempts 应为 {i}"

    assert _rows(email, "register")[0][3] == 1, "达到 MAX_ATTEMPTS 应置 used=1"
    assert row_id


# ---------------------------------------------------------------------------
# I：kid 前缀
# ---------------------------------------------------------------------------

def test_hash_carries_kid_and_old_kid_still_verifies(monkeypatch):
    """I：落库哈希形如 kid:64hex；密钥池里旧 kid 的哈希仍可校验。"""
    from src.kernel.auth_sdk import jwt_utils
    old_kid = "kid-3469-old"
    old_secret = "old-secret-for-issue-3469-with-enough-length"
    monkeypatch.setitem(jwt_utils._SECRET_KEYS, old_kid, old_secret)

    email = "t3469_i_kid@example.com"
    digest = v._hmac_digest(old_secret, CODE, "")
    stored = f"{old_kid}:{digest}"
    assert re.fullmatch(rf"{re.escape(old_kid)}:[0-9a-f]{{64}}", stored)

    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO verification_codes "
            "(email, code_hash, scene, ip, attempts, used, expires_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (email, stored, "register", "10.11.12.13", 0, 0,
             v._utc_str(time.time() + 600), v._utc_str(time.time())),
        )
        db.commit()

    assert v.verify_code(email, "register", CODE) == {"ok": True}


# ---------------------------------------------------------------------------
# H：影子记录
# ---------------------------------------------------------------------------

def test_check_rate_limit_writes_shadow_record(monkeypatch):
    """H：未注册邮箱连续两次 check_rate_limit，第二次被 60s 间隔拦住，且落了影子行。"""
    email = "t3469_h_unregistered@example.com"
    first = v.check_rate_limit(email, "register", "10.99.0.1")
    assert first["ok"] is True

    rows = _rows(email, "register")
    assert len(rows) == 1
    assert rows[0][1] == ""      # code_hash 为空
    assert rows[0][3] == 1       # used=1

    second = v.check_rate_limit(email, "register", "10.99.0.1")
    assert second["ok"] is False, "影子记录必须参与 60s 间隔限流"

    # 计数接口可见（E 的换绑上限用）
    assert v.count_sends(email, "register") == 1

    # 影子记录不能被过期清理连带删掉（否则限流计数会提前失效）
    import random as _random
    monkeypatch.setattr(_random, "random", lambda: 0.0)
    v._cleanup_expired()
    assert len(_rows(email, "register")) == 1


def test_count_sends_fail_closed(monkeypatch):
    """count_sends 异常时 fail-closed 返回大值。"""
    class _BrokenCursor:
        def execute(self, *a, **kw):
            raise RuntimeError("boom")

    class _BrokenDB:
        def cursor(self):
            return _BrokenCursor()

    monkeypatch.setattr(v, "db", _BrokenDB())
    assert v.count_sends("t3469_none@example.com", "register") >= 1000000


# ---------------------------------------------------------------------------
# M：落库失败
# ---------------------------------------------------------------------------

def test_store_failure_returns_false(monkeypatch):
    """M：邮件已发但 INSERT 失败 → ok=False + CODE_STORE_FAILED。"""
    real_db = v.db

    class _Cursor:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if "INSERT INTO verification_codes" in sql:
                raise RuntimeError("insert-boom")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _DB:
        def cursor(self):
            return _Cursor(real_db.cursor())

        def commit(self):
            return real_db.commit()

    email = "t3469_m@example.com"
    monkeypatch.setattr(v, "generate_code", lambda: "555666")
    monkeypatch.setattr(v, "get_email_service", lambda: _FakeEmailService())
    monkeypatch.setattr(v, "db", _DB())

    result = v.send_code(email, "register", "10.1.2.3")
    assert result["ok"] is False
    assert result["error_code"] == "CODE_STORE_FAILED"
    assert result["message"] == "验证码发送失败，请重试"


# ---------------------------------------------------------------------------
# P：结构化失败原因
# ---------------------------------------------------------------------------

def test_failure_reasons_are_structured(monkeypatch):
    """P：NOT_FOUND / EXPIRED / USED / INVALID 四种 error_code 齐全。"""
    # 查无记录
    r = v.verify_code("t3469_p_none@example.com", "register", "123123")
    assert r["ok"] is False and r["error_code"] == "CODE_NOT_FOUND"

    # 已过期
    _insert("t3469_p_expired@example.com", "register", "123123", expires_in=-60)
    r = v.verify_code("t3469_p_expired@example.com", "register", "123123")
    assert r["ok"] is False and r["error_code"] == "CODE_EXPIRED"

    # 已使用
    _insert("t3469_p_used@example.com", "register", "123123", used=1)
    r = v.verify_code("t3469_p_used@example.com", "register", "123123")
    assert r["ok"] is False and r["error_code"] == "CODE_USED"

    # 错码
    _insert("t3469_p_invalid@example.com", "register", "123123")
    r = v.verify_code("t3469_p_invalid@example.com", "register", "000000")
    assert r["ok"] is False and r["error_code"] == "CODE_INVALID"

    # message 均为中文人话
    r = v.verify_code("t3469_p_nope@example.com", "register", "1")
    assert isinstance(r["message"], str) and r["message"]

# ---------------------------------------------------------------------------
# 复审修复：consume=False 只判不消费 / 消费幂等（防密码错一次烧码）
# ---------------------------------------------------------------------------

def test_consume_false_does_not_burn_code(monkeypatch):
    """先只判定（consume=False）多次都不消耗；正式消费后同样的码返回 CODE_USED。"""
    email = "t3469_consume@example.com"
    assert _send(monkeypatch, email, code="445566")["ok"] is True

    for _ in range(2):
        assert v.verify_code(email, "register", "445566", consume=False)["ok"] is True
    rows = _rows(email, "register")
    assert all(r[3] == 0 for r in rows), "consume=False 不得把码置 used"

    assert v.verify_code(email, "register", "445566", consume=True)["ok"] is True
    again = v.verify_code(email, "register", "445566")
    assert again["ok"] is False and again["error_code"] == "CODE_USED"


def test_mark_all_used_second_call_reports_zero(monkeypatch):
    """并发消费：同 key+scene 第二次 mark_all_used 必须返回 False（无行可消费）。"""
    email = "t3469_markall@example.com"
    _insert(email, "register", "556677")

    first = v._store.mark_all_used(email, "register")
    second = v._store.mark_all_used(email, "register")
    assert first is True and second is False
