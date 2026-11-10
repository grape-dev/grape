"""Issue #869 + A2 M3：租约可靠性 + 时钟口径 + 多实例分片接线测试。

A2 M3：租约已迁到独立表 grape_t_channel_lease，键 = "<channel_type>:<installation_id>"。

覆盖验收标准：
1. acquire → renew 后 lease_expires_at 前移且可被抢占行数=0（SQLite 真后端，
   写入与比较同钟——不再依赖 SQL datetime('now')/MySQL NOW() 的服务器本地时间）；
2. 两实例环分配不重叠、各方只连归属安装；单实例 channels 仍为全量归属；
3. 心跳过期/未来时间（CST 脏行）实例不进环；
4. Supervisor 每轮扫描 batch_renew 已持租约；续约失败走优雅让渡。
"""
import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.im.bot_lease import BotLeaseStore
from src.im.channel import (
    CancelContext, Channel, ChannelConfig, OutboundMessage, SendResult, Type,
)
from src.im.channel.registry import ChannelRegistry as Registry
from src.im.engine.lease import lease_key
from src.im.engine.supervisor import Supervisor

_NOW_FMT = "%Y-%m-%d %H:%M:%S"


def _utc_str(dt: datetime) -> str:
    return dt.strftime(_NOW_FMT)


@pytest.fixture
def sqlite_db(monkeypatch, tmp_path):
    """真 SQLite 后端 + 替换 session_manager 的 db/db_lock（函数内 import 生效）。

    A2 M3 起租约在独立表 grape_t_channel_lease，且抢锁前要校验归属安装仍 active。
    """
    import src.session_manager as sm
    conn = sqlite3.connect(str(tmp_path / "lease.db"))
    conn.execute(
        "CREATE TABLE grape_t_channel_installation ("
        "id TEXT PRIMARY KEY, channel_type TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE grape_t_channel_lease ("
        "lease_key TEXT PRIMARY KEY, channel_type TEXT, installation_id TEXT, "
        "lease_token TEXT, lease_owner TEXT, lease_expires_at TEXT, "
        "lease_acquired_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE grape_im_instance ("
        "instance_id TEXT PRIMARY KEY, hostname TEXT, ip TEXT, "
        "im_enabled INTEGER, last_heartbeat TEXT)"
    )
    monkeypatch.setattr(sm, "db", conn, raising=True)
    monkeypatch.setattr(sm, "db_lock", __import__("threading").RLock(), raising=True)
    yield conn
    conn.close()


def _seed_install(conn, installation_id="inst-1", channel_type="feishu", status="active"):
    conn.execute(
        "INSERT OR REPLACE INTO grape_t_channel_installation (id, channel_type, status) VALUES (?,?,?)",
        (installation_id, channel_type, status),
    )
    conn.commit()
    return f"{channel_type}:{installation_id}"


# ---------------------------------------------------------------- 时钟口径 / 续约契约

def test_acquire_then_renew_not_preemptable(sqlite_db):
    """acquire → renew：lease_expires_at 前移、他实例抢占失败、不出现在可抢列表。"""
    key = _seed_install(sqlite_db)

    r1 = BotLeaseStore("r1", ttl=60)
    token = r1.try_acquire(key)
    assert token

    row = sqlite_db.execute(
        "SELECT lease_expires_at, lease_acquired_at FROM grape_t_channel_lease WHERE lease_key=?",
        (key,),
    ).fetchone()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    exp = datetime.strptime(row[0], _NOW_FMT)
    acq = datetime.strptime(row[1], _NOW_FMT)
    # 写入是 Python UTC 口径：expiry ≈ now+60，acquired_at ≈ now（与比较同钟）
    assert timedelta(seconds=55) < exp - now <= timedelta(seconds=60)
    assert abs(acq - now) < timedelta(seconds=5)

    r2 = BotLeaseStore("r2", ttl=60)
    assert r2.try_acquire(key) is None          # 未过期不可抢（可被抢占行数=0）
    assert r1.renew(key, token) is True
    exp2 = sqlite_db.execute(
        "SELECT lease_expires_at FROM grape_t_channel_lease WHERE lease_key=?", (key,)
    ).fetchone()[0]
    assert exp2 == row[0] or datetime.strptime(exp2, _NOW_FMT) >= exp  # 前移（同秒内亦不回退）
    assert r2.try_acquire(key) is None          # 续约后仍不可抢
    assert r1.try_acquire(key) is None          # 未释放时自己重抢也失败（CAS 语义）
    expired = r2.list_expired_or_unheld()
    assert all(e["installation_id"] != "inst-1" for e in expired)


def test_expired_lease_is_preemptable(sqlite_db):
    """过期（同钟 UTC 口径写入旧值）→ 他实例可抢占。"""
    key = _seed_install(sqlite_db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sqlite_db.execute(
        "INSERT INTO grape_t_channel_lease "
        "(lease_key, channel_type, installation_id, lease_token, lease_expires_at, lease_owner) "
        "VALUES (?, 'feishu', 'inst-1', 'old-tok', ?, 'r1')",
        (key, _utc_str(now - timedelta(seconds=120))),
    )
    sqlite_db.commit()
    r2 = BotLeaseStore("r2", ttl=60)
    assert r2.try_acquire(key) is not None


def test_revoked_installation_cannot_be_leased(sqlite_db):
    """A2 M3 新增：安装被 revoke 后，即使租约空着也不得再抢锁建连。"""
    key = _seed_install(sqlite_db, status="revoked")
    assert BotLeaseStore("r1", ttl=60).try_acquire(key) is None


def test_lease_sql_uses_python_clock_not_sql_now(sqlite_db):
    """执行 SQL 不含 datetime('now')（Python 侧统一时钟，MySQL NOW() CST 口径差异被规避）。"""
    import types

    import src.session_manager as sm
    key = _seed_install(sqlite_db)
    seen: list = []

    class SpyCursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            seen.append(sql)
            return self._inner.execute(sql, params)

        def __getattr__(self, item):
            return getattr(self._inner, item)

    spy_conn = types.SimpleNamespace(
        cursor=lambda: SpyCursor(sqlite_db.cursor()),
        commit=sqlite_db.commit,
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sm, "db", spy_conn, raising=True)
    try:
        store = BotLeaseStore("r1", ttl=60)
        store.try_acquire(key)
        store.list_expired_or_unheld()
    finally:
        monkey.undo()
    lease_sql = [s for s in seen if "grape_t_channel_lease" in s and "UPDATE" in s.upper()]
    assert lease_sql, "no lease SQL captured"
    assert all("datetime('now')" not in s for s in lease_sql)


def test_list_active_instances_heartbeat_window(sqlite_db):
    """过期心跳不进环；未来时间（CST 脏行）不进环；im_enabled=0 不进环。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        ("alive", 1, _utc_str(now - timedelta(seconds=10))),
        ("stale-10min", 1, _utc_str(now - timedelta(minutes=10))),
        ("future-cst", 1, _utc_str(now + timedelta(hours=8))),
        ("disabled", 0, _utc_str(now - timedelta(seconds=5))),
    ]
    sqlite_db.executemany(
        "INSERT INTO grape_im_instance (instance_id, im_enabled, last_heartbeat) "
        "VALUES (?,?,?)", rows)
    sqlite_db.commit()
    store = BotLeaseStore("r1")
    assert store.list_active_instances(timeout=45.0) == ["alive"]


def test_lease_sql_translates_for_mysql_backend():
    """MySQL 方言翻译后不引入 NOW()（时钟已 Python 参数化，双方言等价）。"""
    from src.mysql_compat import translate_sql
    acquire_sql = (
        "UPDATE grape_t_channel_lease "
        "SET lease_token = ?, lease_expires_at = ?, "
        "    lease_owner = ?, lease_acquired_at = ? "
        "WHERE lease_key = ? "
        "  AND (lease_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at < ?)"
    )
    out = translate_sql(acquire_sql)
    assert "NOW()" not in out
    assert "UTC_TIMESTAMP" not in out          # 时间全部走参数，无 DB 时钟依赖
    assert "%s" in out                         # 占位符已翻译为 pymysql 风格


# ---------------------------------------------------------------- Supervisor 分片接线

class OkChannel(Channel):
    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg

    def type(self) -> Type:
        return Type.FEISHU

    async def connect(self, ctx: CancelContext) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, out: OutboundMessage) -> SendResult:
        return SendResult(ok=True)


class SharedLeaseStore:
    """两实例共享的租约替身：先到先得 + 记录 batch_renew 调用。"""

    def __init__(self, active):
        self.active = active
        self.held: dict = {}
        self.renew_calls: list = []

    def list_active_instances(self, timeout=45.0, now_dt=None):
        return list(self.active)

    def try_acquire(self, key, steal_after=60, instance_timeout=45.0):
        if key in self.held:
            return None
        self.held[key] = f"tok:{key}"
        return self.held[key]

    def renew(self, key, token):
        return self.held.get(key) == token

    def release(self, key, token):
        if self.held.get(key) == token:
            self.held.pop(key)

    def batch_renew(self, held):
        self.renew_calls.append(dict(held))
        return {k: (self.held.get(k) == v) for k, v in (held or {}).items()}


APPS = [f"app-{i}" for i in range(1, 9)]
KEYS = [lease_key(app, "feishu") for app in APPS]


async def _provider():
    return [{
        "installation_id": app,
        "channel_type": "feishu",
        "scope": "user",
        "config": {"app_id": app, "app_secret": "x"},
    } for app in APPS]


def _build_sup(instance_id, leases):
    registry = Registry()
    registry.register(Type.FEISHU, OkChannel)
    return Supervisor(
        registry=registry, instance_id=instance_id, lease_store=leases,
        installation_provider=_provider,
    )


def test_two_instances_disjoint_assignment():
    """两实例共享环：归属不重叠、并集覆盖全部安装、各自只连归属。"""
    leases = SharedLeaseStore(["r1", "r2"])
    sup1 = _build_sup("r1", leases)
    sup2 = _build_sup("r2", leases)

    async def scenario():
        sup1._running = True
        sup2._running = True
        await sup1._scan_once()
        await sup2._scan_once()

    asyncio.run(scenario())
    c1, c2 = set(sup1._channels), set(sup2._channels)
    assert sup1.instances == ["r1", "r2"]
    assert sup2.instances == ["r1", "r2"]
    assert not (c1 & c2)                      # 不重叠 → 不重复连接
    assert c1 | c2 == set(APPS)               # 覆盖全部
    assert set(leases.held) == set(KEYS)      # 每把租约唯一持有


def test_single_instance_keeps_all_channels():
    leases = SharedLeaseStore(["r1"])
    sup = _build_sup("r1", leases)
    asyncio.run(_scan(sup))
    assert set(sup._channels) == set(APPS)
    assert sup.instances == ["r1"]


async def _scan(sup):
    sup._running = True
    await sup._scan_once()


def test_scan_renews_held_leases_each_round():
    """每轮扫描对已持租约 batch_renew（TTL 60s / 扫描 30s 的 2 倍余量）。"""
    leases = SharedLeaseStore(["r1"])
    sup = _build_sup("r1", leases)

    async def scenario():
        sup._running = True
        await sup._scan_once()
        assert leases.renew_calls == []       # 第一轮只有 acquire
        await sup._scan_once()                # 第二轮先续约

    asyncio.run(scenario())
    # A2 M3：续约按 lease_key（"feishu:<inst_id>"）提交，不再用裸 installation_id
    expected = {lease_key(inst_id, "feishu"): tok
                for inst_id, tok in sup._lease_tokens.items()}
    assert leases.renew_calls == [expected]


def test_renew_failure_yields_gracefully():
    """batch_renew 返回 False（被抢/失主）→ 拆连接 + 释放租约（优雅让渡）。"""
    leases = SharedLeaseStore(["r1"])
    sup = _build_sup("r1", leases)

    async def scenario():
        sup._running = True
        await sup._scan_once()
        # 模拟租约被抢：token 易主（他实例已抢走，try_acquire 不再成功）
        for k in list(leases.held):
            leases.held[k] = f"stolen:{k}"
        leases.batch_renew = lambda held: {k: False for k in held}
        await sup._scan_once()

    asyncio.run(scenario())
    assert sup._channels == {}
    assert sup._lease_tokens == {}
    # 我方 token 已按让渡路径释放（表里只剩易主 token，不为我方持有）
    assert all(v.startswith("stolen:") for v in leases.held.values())


def test_stale_instance_not_in_ring():
    """实例掉出 list_active_instances（心跳过期）→ 不再在环中、其安装被让渡。"""
    leases = SharedLeaseStore(["r1", "r2"])
    sup1 = _build_sup("r1", leases)
    sup2 = _build_sup("r2", leases)

    async def scenario():
        sup1._running = True
        sup2._running = True
        await sup1._scan_once()
        await sup2._scan_once()
        # r2 掉线：心跳过期掉环（不再扫描上报），其租约随 TTL 过期释放
        leases.active = ["r1"]
        leases.held.clear()
        await sup1._scan_once()               # r1 接管全部

    asyncio.run(scenario())
    assert sup1.instances == ["r1"]           # r2 不再在环（心跳过期不进环）
    assert set(sup1._channels) == set(APPS)
    assert set(leases.held) == set(KEYS)


# ---------------------------------------------------------------- #869 复核补丁（心跳上报 / 结果键翻译）

def test_scan_reports_heartbeat_so_ring_sees_instance(sqlite_db):
    """扫描轮上报自身心跳：grape_im_instance 出现本实例 → list_active_instances 不再为空。

    修前：新引擎从不 heartbeat，`list_active_instances()` 恒为 []，环永远只有自己，
    多实例部署仍然不分片（实测部署后返回空列表）。
    """
    store = BotLeaseStore("inst-hb", ttl=60)
    registry = Registry()
    registry.register(Type.FEISHU, OkChannel)

    async def _empty_provider():
        return []

    sup = Supervisor(registry=registry, instance_id="inst-hb", lease_store=store,
                     installation_provider=_empty_provider)

    assert store.list_active_instances() == []          # 修前口径
    asyncio.run(_scan(sup))
    assert "inst-hb" in store.list_active_instances()   # 心跳已落库
    assert sup.instances == ["inst-hb"]
    assert sqlite_db.execute(
        "SELECT COUNT(*) FROM grape_im_instance WHERE instance_id='inst-hb'"
    ).fetchone()[0] == 1


def test_batch_renew_returns_caller_keys(sqlite_db):
    """续约结果键保持调用方键（Supervisor 按该键查 False → 走优雅让渡）。

    A2 M3：不再有"键翻译"层，所以这条不变量要由 store 本身保证。
    """
    key = _seed_install(sqlite_db)
    store = BotLeaseStore("r1", ttl=60)
    token = store.try_acquire(key)
    assert token
    assert store.batch_renew({key: token}) == {key: True}
    # 锁被别人抢走（token 失配）→ False，键同样是调用方键
    assert store.batch_renew({key: "stale-token"}) == {key: False}
    # 心跳仍写 grape_im_instance（实例环，与租约表无关）
    store.heartbeat()
    assert sqlite_db.execute(
        "SELECT COUNT(*) FROM grape_im_instance WHERE instance_id='r1'"
    ).fetchone()[0] == 1


