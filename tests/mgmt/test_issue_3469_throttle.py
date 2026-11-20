"""Issue #3469 B：登录锁定 lock_until 时区错位复现 + 回归。

背景：login_throttle 的 created_at 由 _now_str() 用 time.gmtime() 写成 **UTC** 字符串，
而 _get_lock_until() 原用 time.mktime(time.strptime(...)) 按 **本地时区** 解析。
东八区下 lock_until = 失败时刻 - 8h + duration，恒在过去 → time.time() < lock_until
永远为 False，账号维度/IP 维度锁定永不生效（现网 15 分钟窗口 12097 次失败从未锁定）。

这里在 TZ=Asia/Shanghai 下用真实 SQLite 测试库（conftest 已把 DEVOPS_DB_PATH 指向
data/test_sessions.db，db_setup 建表由 conftest 的 session fixture 完成）实跑，
并用 calendar.timegm 从库里原始字符串复算 lock_until。
"""
import calendar
import os
import time
import uuid

import pytest

from src.domains.mgmt.auth import login_throttle

_TS_FMT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture(autouse=True)
def _tz_asia_shanghai():
    """整用例跑在东八区；结束后恢复原时区。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.fixture(autouse=True)
def _clean_rows():
    """清理本模块写入的失败记录，避免污染其它用例。"""
    yield
    db = login_throttle.db
    with login_throttle.db_lock:
        c = db.cursor()
        c.execute("DELETE FROM login_failures WHERE identifier LIKE 'issue3469%'")
        db.commit()


def _rows(identifier):
    with login_throttle.db_lock:
        c = login_throttle.db.cursor()
        c.execute(
            "SELECT created_at FROM login_failures WHERE identifier=? ORDER BY created_at ASC",
            (identifier,),
        )
        return [r[0] for r in c.fetchall()]


def _utc_ts(s):
    return calendar.timegm(time.strptime(s, _TS_FMT))


def test_account_lock_effective_in_cst():
    """账号维度：5 次连续失败 → check_throttle 必须拒绝，且 lock_until 在未来。"""
    assert time.timezone == -8 * 3600 or time.altzone == -8 * 3600, "TZ 未切换到东八区"
    ident = "issue3469_acct_" + uuid.uuid4().hex[:8]
    ip = "203.0.113.11"

    for _ in range(login_throttle.ACCOUNT_LOCK_THRESHOLD):
        login_throttle.record_failure(ident, ip)

    res = login_throttle.check_throttle(ident, ip)
    assert res["allowed"] is False, f"东八区下账号锁定未生效（Issue #3469 B 回归）：{res}"
    assert res.get("locked") is True
    assert res["error_code"] == "ACCOUNT_LOCKED"
    assert res["retry_after"] > 0

    # 库内落的是 UTC 字符串：用 calendar.timegm 复算应与当前时间同量级
    rows = _rows(ident)
    assert len(rows) == login_throttle.ACCOUNT_LOCK_THRESHOLD
    stored_ts = _utc_ts(rows[login_throttle.ACCOUNT_LOCK_THRESHOLD - 1])
    now = time.time()
    assert abs(stored_ts - now) < 120, f"created_at 不是 UTC 字符串：{rows[-1]}"

    # 锁定截止 = 第 N 条失败时间(UTC) + 时长，必须在未来
    lock_until = _utc_ts(rows[login_throttle.ACCOUNT_LOCK_THRESHOLD - 1]) \
        + login_throttle.ACCOUNT_LOCK_DURATION
    assert lock_until > now, (
        f"lock_until {lock_until} <= now {now}（按本地时区解析 UTC 串的错位未修）"
    )
    assert res["retry_after"] == int(lock_until - now)
    assert login_throttle.is_account_locked(ident) is True


def test_ip_lock_effective_in_cst():
    """IP 维度：同 IP 不同 identifier 连续 20 次失败 → check 拒绝且 ip_locked=True。"""
    ip = "203.0.113." + str(200 + int(uuid.uuid4().hex[:2], 16) % 40)
    idents = [f"issue3469_ip_{uuid.uuid4().hex[:8]}_{i}"
              for i in range(login_throttle.IP_LOCK_THRESHOLD)]
    for ident in idents:
        login_throttle.record_failure(ident, ip)

    # 用一条未失败过的 identifier 触发，确保命中的是 IP 维度
    fresh = "issue3469_ip_fresh_" + uuid.uuid4().hex[:8]
    res = login_throttle.check_throttle(fresh, ip)
    assert res["allowed"] is False, f"东八区下 IP 锁定未生效（Issue #3469 B 回归）：{res}"
    assert res.get("ip_locked") is True
    assert res["retry_after"] > 0

    rows = _rows(idents[0])
    lock_until = _utc_ts(rows[-1]) + login_throttle.IP_LOCK_DURATION
    assert lock_until > time.time()


def _get_lock_until_legacy(identifier, is_ip=False):
    """旧实现（mktime 按本地时区解析 UTC 串）——仅用于反证，不进产品代码。"""
    threshold = login_throttle.IP_LOCK_THRESHOLD if is_ip else login_throttle.ACCOUNT_LOCK_THRESHOLD
    duration = login_throttle.IP_LOCK_DURATION if is_ip else login_throttle.ACCOUNT_LOCK_DURATION
    window = login_throttle.IP_WINDOW if is_ip else login_throttle.ACCOUNT_WINDOW
    column = "ip" if is_ip else "identifier"
    with login_throttle.db_lock:
        c = login_throttle.db.cursor()
        c.execute(
            f"SELECT created_at FROM login_failures "
            f"WHERE {column}=? AND created_at>=? ORDER BY created_at ASC "
            f"LIMIT 1 OFFSET {threshold - 1}",
            (identifier, login_throttle._since_str(window)),
        )
        row = c.fetchone()
        if not row:
            return None
        return time.mktime(time.strptime(row[0], _TS_FMT)) + duration


def test_legacy_mktime_reproduces_the_bug(monkeypatch):
    """反证：把 _get_lock_until 还原成 mktime 版本，同一用例应「锁不住」。

    即证明下面的 allowed=True 是旧实现的失败态，而修复后的 refused 才是正确行为。
    """
    ident = "issue3469_neg_" + uuid.uuid4().hex[:8]
    ip = "203.0.113.12"
    for _ in range(login_throttle.ACCOUNT_LOCK_THRESHOLD):
        login_throttle.record_failure(ident, ip)

    # 修复态：锁得住
    assert login_throttle.check_throttle(ident, ip)["allowed"] is False

    # 旧实现：lock_until 落在过去 8 小时 → 同一份数据下 check 放行（bug 复现）
    monkeypatch.setattr(login_throttle, "_get_lock_until", _get_lock_until_legacy)
    legacy_until = _get_lock_until_legacy(ident)
    assert legacy_until is not None
    assert legacy_until < time.time(), "旧实现应算出已过期的 lock_until"
    assert login_throttle.check_throttle(ident, ip)["allowed"] is True