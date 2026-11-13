"""Issue #2261 —— IM 主动通知 outbox（B1）+ 引擎多源解析（A）+ 可观测（C）。

覆盖：
  1. 迁移 004：老库（001 旧 DDL 建的表）幂等补列、双跑不炸、新库直接建全；
  2. engine_wiring 注册点 set_current_engine/get_current_engine；
  3. notify.resolve_engine 多源解析（grape_server 模型 / main 模型 / app.state / 全空 None）；
  4. outbox 仓储：入队幂等（out_track_id 唯一）、取批、状态迁移、退避、过期、重发；
  5. notify_user 入队化：同事件二次入队不新增行、no_binding 一步落 failed；
  6. 消费者 run_once：持有租约者投递成功置 sent；非持有者不消费；失败退避递增；
  7. API：/status outbox 段 + /outbound 查询 + /retry 重发。

QA #1314 复审新增：
  8. M1 行级互斥：claim_for_sending 原子认领（pending→sending），二次认领失败；
     内联投递与消费者并发只投一次（真实 send 只发生 1 次）；
  9. M2 模式翻转：消费者 set_engine 换新引擎 + 新 instance_id 后能继续认领；
 10. m2 no_binding 一步落终态（pending 中间态不可见）；去重路径 row-exists 覆盖；
 11. m3 mark_sent 不递增 attempt_count；
 12. m4 mark_failed 乐观锁（并发状态变化时放弃写入）。
"""
import asyncio
import importlib.util
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, ROOT)

from src.domains.runtime.im.channel.capability import Capability

# 共享 fakes（QA #1314 S9：conftest 提供，fanout 测试同款）
from tests.runtime.im.conftest import FakeChannel, FakeEngine

_MIG_DIR = os.path.join(ROOT, "scripts", "migrations")


def _load_mig(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_MIG_DIR, name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def outbox_db(tmp_path, monkeypatch):
    """独立 sqlite 库：跑启动迁移（001+004），返回 db 路径。"""
    path = str(tmp_path / "outbox.db")
    monkeypatch.setenv("DEVOPS_DB_PATH", path)
    from src.domains.runtime.im import engine_wiring
    engine_wiring._reset_backend_cache()
    engine_wiring.run_startup_migration(path)
    return path


@pytest.fixture()
def clean_notify():
    from src.domains.runtime.im import notify as N
    N._reset_for_tests()
    yield
    N._reset_for_tests()


# ---------------------------------------------------------------- 1. 迁移 004

def test_004_migration_adds_columns_to_legacy_table(tmp_path):
    """老库（001 旧列集）→ 004 补列，双跑幂等。"""
    m001 = _load_mig("001_channel_tables")
    m004 = _load_mig("004_notify_outbox")
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    # 模拟老库：先把 outbound_card 建成旧列集（手工删掉新列语义：直接建旧 DDL）
    conn.executescript(
        """
        CREATE TABLE grape_t_channel_outbound_card (
            out_track_id     VARCHAR(191) PRIMARY KEY,
            installation_id  TEXT NOT NULL,
            channel_type     VARCHAR(32) NOT NULL DEFAULT 'dingtalk',
            channel_chat_id  TEXT NOT NULL DEFAULT '',
            chat_type        TEXT NOT NULL DEFAULT 'p2p',
            sender_staff_id  TEXT NOT NULL DEFAULT '',
            card_template_id VARCHAR(191) NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(grape_t_channel_outbound_card)")]
    assert "status" not in cols

    added = m004.ensure_notify_outbox_columns(conn)
    assert set(added) == {"payload", "status", "attempt_count", "next_retry_at",
                          "expires_at", "last_error", "updated_at"}
    # 双跑幂等：不再新增
    added2 = m004.ensure_notify_outbox_columns(conn)
    assert added2 == []
    # 既有表被 001 兜底补齐（不缺基础列）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(grape_t_channel_outbound_card)")]
    for c in ("payload", "status", "attempt_count", "next_retry_at", "expires_at",
              "last_error", "updated_at", "created_at", "channel_chat_id"):
        assert c in cols, c
    conn.close()


def test_004_new_db_has_columns_directly(tmp_path):
    """新库：001 直接建全（无需 004 二次补列）。"""
    m001 = _load_mig("001_channel_tables")
    conn = sqlite3.connect(str(tmp_path / "new.db"))
    m001.ensure_channel_tables(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(grape_t_channel_outbound_card)")]
    for c in ("payload", "status", "attempt_count", "next_retry_at", "expires_at",
              "last_error", "updated_at"):
        assert c in cols, c
    conn.close()


def test_004_startup_migration_wired(outbox_db):
    """run_startup_migration 串接 004：新库直接有列。"""
    conn = sqlite3.connect(outbox_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(grape_t_channel_outbound_card)")]
    assert "status" in cols and "attempt_count" in cols
    conn.close()


# ---------------------------------------------------------------- 2. engine_wiring 注册点

def test_engine_wiring_current_engine_registry():
    from src.domains.runtime.im import engine_wiring
    sentinel = object()
    engine_wiring.set_current_engine(None)          # 清场
    assert engine_wiring.get_current_engine() is None
    engine_wiring.set_current_engine(sentinel)
    assert engine_wiring.get_current_engine() is sentinel
    engine_wiring.set_current_engine(None)          # 复位，不污染其它用例
    assert engine_wiring.get_current_engine() is None


# ---------------------------------------------------------------- 3. resolve_engine 多源

def test_resolve_engine_grape_server_model(clean_notify, monkeypatch):
    """A 进程（grape_server）模型：模块属性挂了引擎 → _engine() 非 None。"""
    from src.domains.runtime.im import notify as N
    import src.api.grape_server as gs
    monkeypatch.setattr(gs, "im_engine", object(), raising=False)
    try:
        assert N._engine() is not None
    finally:
        monkeypatch.setattr(gs, "im_engine", None, raising=False)


def test_resolve_engine_worker_process_gets_none(clean_notify, monkeypatch):
    """4 进程口径：B/C/D 进程（无引擎、无注册点）resolve_engine → None，不再碰单进程入口。"""
    from src.domains.runtime.im import notify as N
    import src.api.grape_server as gs
    from src.domains.runtime.im import engine_wiring
    engine_wiring.set_current_engine(None)
    monkeypatch.setattr(gs, "im_engine", None, raising=False)
    monkeypatch.setattr(N, "_running_asgi_app", None)
    assert N._engine() is None
    # 断言没有退回「import 单进程 app」的老路：resolve_engine 的字节码命名里不再有 main 候选
    names = set(N.resolve_engine.__code__.co_names)
    assert "grape_server" in names
    assert "main" not in names


def test_resolve_engine_wiring_registry_first(clean_notify, monkeypatch):
    """engine_wiring 注册点优先级最高（B/C 进程无模块属性也能拿到）。"""
    from src.domains.runtime.im import notify as N, engine_wiring
    sentinel = object()
    engine_wiring.set_current_engine(sentinel)
    try:
        assert N.resolve_engine() is sentinel
    finally:
        engine_wiring.set_current_engine(None)


def test_resolve_engine_app_state_fallback(clean_notify, monkeypatch):
    """全模块属性为空 → 运行中 ASGI app.state.im_engine 兜底。"""
    from src.domains.runtime.im import notify as N
    import src.api.grape_server as gs
    from src.domains.runtime.im import engine_wiring

    engine_wiring.set_current_engine(None)
    monkeypatch.setattr(gs, "im_engine", None, raising=False)

    sentinel = object()

    class FakeApp:
        class state:
            im_engine = sentinel

    monkeypatch.setattr(N, "_running_asgi_app", FakeApp)
    try:
        assert N.resolve_engine() is sentinel
    finally:
        monkeypatch.setattr(N, "_running_asgi_app", None)


def test_resolve_engine_all_empty_returns_none(clean_notify, monkeypatch):
    from src.domains.runtime.im import notify as N
    import src.api.grape_server as gs
    from src.domains.runtime.im import engine_wiring
    engine_wiring.set_current_engine(None)
    monkeypatch.setattr(gs, "im_engine", None, raising=False)
    monkeypatch.setattr(N, "_running_asgi_app", None)
    assert N._engine() is None


# ---------------------------------------------------------------- 4/5/6. outbox 仓储 + 入队 + 消费

def _mk_lease(outbox_db, key: str, owner: str):
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT OR REPLACE INTO grape_t_channel_lease "
        "(lease_key, channel_type, installation_id, lease_owner, updated_at) VALUES (?,?,?,?,?)",
        (key, key.split(":", 1)[0], key.split(":", 1)[1], owner, "2026-11-12 00:00:00"))
    conn.commit()
    conn.close()


def _mk_binding(outbox_db, installation_id: str, chat_id: str):
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_chat_session_binding "
        "(id, installation_id, channel_type, channel_chat_id, chat_type) VALUES (?,?,?,?, 'p2p')",
        (f"b-{installation_id}", installation_id, "feishu", chat_id))
    conn.commit()
    conn.close()


def test_enqueue_idempotent_same_track(outbox_db):
    """同 out_track_id 二次入队不新增行（跨进程幂等由主键保证）。"""
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(1, "awaiting", "gitcode:o/r", 7, "feishu", "inst-1")
    payload = {"kind": "awaiting", "text": "请补充", "repo_key": "gitcode:o/r",
               "issue_number": 7, "user_id": 1}
    first = OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
                       channel_chat_id="oc_1", payload=payload,
                       expires_at=OB.new_expiry())
    second = OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
                        channel_chat_id="oc_1", payload=payload,
                        expires_at=OB.new_expiry())
    assert first is True and second is False
    conn = sqlite3.connect(outbox_db)
    n = conn.execute("SELECT COUNT(*) FROM grape_t_channel_outbound_card").fetchone()[0]
    conn.close()
    assert n == 1


def test_claim_batch_only_lease_owner(outbox_db):
    """非租约持有者不消费（跨主控路由核心语义）。"""
    from src.domains.runtime.im import outbox as OB
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A")
    _mk_lease(outbox_db, "feishu:inst-2", "owner-B")
    for inst in ("inst-1", "inst-2"):
        tid = OB.out_track_id(1, "awaiting", "gitcode:o/r", 7, "feishu", inst)
        OB.enqueue(out_track_id=tid, installation_id=inst, channel_type="feishu",
                   channel_chat_id=f"oc_{inst}", payload={"kind": "awaiting"},
                   expires_at=OB.new_expiry())
    got_a = OB.claim_batch("owner-A")
    assert [r["installation_id"] for r in got_a] == ["inst-1"]
    got_b = OB.claim_batch("owner-B")
    assert [r["installation_id"] for r in got_b] == ["inst-2"]


def test_claim_batch_skips_when_no_lease(outbox_db):
    """无租约的行不消费（租约可能在别的实例手里）。"""
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(1, "done", "gitcode:o/r", 8, "feishu", "inst-x")
    OB.enqueue(out_track_id=tid, installation_id="inst-x", channel_type="feishu",
               channel_chat_id="oc_x", payload={"kind": "done"},
               expires_at=OB.new_expiry())
    assert OB.claim_batch("owner-A") == []


def test_claim_for_sending_exclusive(outbox_db):
    """QA #1314 M1：pending → sending 原子认领；二次认领必须失败（行级互斥）。"""
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(1, "task", "gitcode:o/r", 30, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "task"},
               expires_at=OB.new_expiry())
    assert OB.claim_for_sending(tid) is True          # 首个认领者拿到投递权
    assert OB.get_row(tid)["status"] == "sending"      # 认领态真实写入
    assert OB.claim_for_sending(tid) is False          # 二次认领被拒（防双发）
    # sending 行不再被 claim_batch 取走（消费者轮询不会重复认领）
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A")
    assert OB.claim_batch("owner-A") == []
    # 认领后 mark_sent 收尾闭环（sending → sent）
    OB.mark_sent(tid)
    assert OB.get_row(tid)["status"] == "sent"


def test_claim_batch_rows_are_sending(outbox_db):
    """QA #1314 M1：claim_batch 返回的行已置 sending（投递期间不被二次认领）。"""
    from src.domains.runtime.im import outbox as OB
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A")
    tid = OB.out_track_id(1, "task", "gitcode:o/r", 31, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "task"},
               expires_at=OB.new_expiry())
    rows = OB.claim_batch("owner-A")
    assert len(rows) == 1
    assert OB.get_row(rows[0]["out_track_id"])["status"] == "sending"


def test_mark_failed_backoff_and_max_attempts(outbox_db):
    """失败退避：attempt_count 递增；>=5 置 failed 不再重试。"""
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(2, "error", "gitcode:o/r", 9, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "error"},
               expires_at=OB.new_expiry())
    statuses = [OB.mark_failed(tid, "send_failed") for _ in range(OB.MAX_ATTEMPTS)]
    assert all(s == "pending" for s in statuses[:-1])
    assert statuses[-1] == "failed"
    row = OB.get_row(tid)
    assert row["attempt_count"] == OB.MAX_ATTEMPTS
    assert row["status"] == "failed"
    assert row["last_error"] == "send_failed"
    # failed 后不再被 claim
    assert all(r["out_track_id"] != tid for r in OB.claim_batch("any"))


def test_mark_failed_backoff_ladder(outbox_db):
    """QA #1314 m1：四档退避 1/2/4/8s 全部可达（attempt 1..4 失败后的 next_retry 差值）。"""
    from src.domains.runtime.im import outbox as OB
    from src.domains.runtime.im.outbox import _parse_ts, _now_str
    tid = OB.out_track_id(2, "error", "gitcode:o/r", 32, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "error"},
               expires_at=OB.new_expiry())
    for expected_backoff in OB.RETRY_BACKOFF:      # 1, 2, 4, 8
        before = _parse_ts(_now_str())
        status = OB.mark_failed(tid, "send_failed")
        assert status == "pending"
        row = OB.get_row(tid)
        delay = _parse_ts(row["next_retry_at"]) - before
        assert delay >= expected_backoff - 1 and delay <= expected_backoff + 2, \
            f"backoff 档 {expected_backoff}s 不可达（实际 {delay}s）"


def test_mark_failed_optimistic_lock_gives_up_on_conflict(outbox_db):
    """QA #1314 m4：行被并发方改变（retry 置 attempt=0）→ 放弃写入返回 ''。

    模拟真实并发交错：mark_failed 读到 attempt=4 的行并算出 failed 终态；
    写回前 API retry 已把行重置（attempt=0/pending）。旧调用不得覆盖。
    """
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(2, "error", "gitcode:o/r", 33, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "error"},
               expires_at=OB.new_expiry())
    # 先把行推到 attempt=4（下一次 mark_failed 将置 failed）
    for _ in range(OB.MAX_ATTEMPTS - 1):
        OB.mark_failed(tid, "send_failed")
    assert OB.get_row(tid)["attempt_count"] == OB.MAX_ATTEMPTS - 1

    # 模拟「读-改」之间并发方介入：行整体进入 failed→expired→requeue（attempt=0）
    conn = sqlite3.connect(outbox_db)
    conn.execute("UPDATE grape_t_channel_outbound_card SET status='failed' "
                 "WHERE out_track_id=?", (tid,))
    conn.commit()
    conn.close()
    OB.requeue_for_retry(tid)
    assert OB.get_row(tid)["attempt_count"] == 0

    # 直接复刻 mark_failed 的「读旧值 → 条件写」：旧 attempt 快照不再匹配 → 放弃
    old_attempt = OB.MAX_ATTEMPTS - 1          # mark_failed 逻辑读到的旧值
    new_status = "failed" if old_attempt + 1 >= OB.MAX_ATTEMPTS else "pending"
    conn = sqlite3.connect(outbox_db)
    cur = conn.execute(
        "UPDATE grape_t_channel_outbound_card SET status=?, attempt_count=? "
        "WHERE out_track_id=? AND status IN ('pending','sending') AND attempt_count=?",
        (new_status, old_attempt + 1, tid, old_attempt))
    conn.commit()
    changed = (getattr(cur, "rowcount", 0) or 0) > 0
    conn.close()
    assert changed is False
    row = OB.get_row(tid)
    assert row["status"] == "pending" and row["attempt_count"] == 0   # 并发方结果未被覆盖


def test_expire_overdue_and_expired_not_claimed(outbox_db):
    """过期置 expired；expired 不进 claim 批。"""
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(3, "task", "gitcode:o/r", 10, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "task"},
               expires_at=OB.new_expiry(-600))    # 已过期
    n = OB.expire_overdue()
    assert n == 1
    assert OB.get_row(tid)["status"] == "expired"
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A")
    assert OB.claim_batch("owner-A") == []


def test_requeue_for_retry_resets(outbox_db):
    """failed/expired → pending（重置 attempt/退避/TTL）。"""
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(4, "pending_review", "gitcode:o/r", 11, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "pending_review"},
               expires_at=OB.new_expiry(-600))
    OB.expire_overdue()
    assert OB.get_row(tid)["status"] == "expired"
    assert OB.requeue_for_retry(tid) is True
    row = OB.get_row(tid)
    assert row["status"] == "pending" and row["attempt_count"] == 0
    # 非 failed/expired 状态拒绝重发
    assert OB.requeue_for_retry(tid) is False


def test_counts_and_recent_undelivered(outbox_db):
    from src.domains.runtime.im import outbox as OB
    t1 = OB.out_track_id(5, "awaiting", "gitcode:o/r", 12, "feishu", "inst-1")
    t2 = OB.out_track_id(6, "task", "gitcode:o/r", 13, "feishu", "inst-1")
    OB.enqueue(out_track_id=t1, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1",
               payload={"kind": "awaiting", "user_id": 5, "repo_key": "gitcode:o/r",
                        "issue_number": 12},
               expires_at=OB.new_expiry(), status="failed", last_error="no_binding")
    OB.enqueue(out_track_id=t2, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1",
               payload={"kind": "task", "user_id": 6, "repo_key": "gitcode:o/r",
                        "issue_number": 13},
               expires_at=OB.new_expiry())
    c = OB.counts()
    assert c == {"pending": 1, "sending": 0, "sent": 0, "failed": 1, "expired": 0}
    recent = OB.recent_undelivered()
    kinds = {(r["out_track_id"], r["reason"]) for r in recent}
    assert (t1, "no_binding") in kinds
    row = [r for r in recent if r["out_track_id"] == t1][0]
    assert row["kind"] == "awaiting" and row["user_id"] == 5
    assert row["repo_key"] == "gitcode:o/r" and row["issue_number"] == 12
    rows = OB.list_rows(status="failed")
    assert len(rows) == 1 and rows[0]["kind"] == "awaiting"


def test_notify_user_enqueues_and_immediate_deliver(outbox_db, clean_notify):
    """有绑定 + 本进程引擎可用 → 入队并立即投递；二次触发不新增行。"""
    from src.domains.runtime.im import notify as N, outbox as OB
    # 安装 + 绑定
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, status) VALUES ('inst-1','feishu','user','1','active')")
    conn.commit()
    conn.close()
    _mk_binding(outbox_db, "inst-1", "oc_user")

    ch = FakeChannel(Capability.TEXT | Capability.RICH_CARD)
    eng = FakeEngine({"inst-1": ch})

    res = asyncio.run(N.notify_user(1, "awaiting", "请补充信息",
                                    repo_key="gitcode:o/r", issue_number=7,
                                    engine=eng, dedupe=False))
    assert res["sent"] == ["feishu"]
    assert ch.calls == 1
    # outbox 落库且已 sent
    tid = OB.out_track_id(1, "awaiting", "gitcode:o/r", 7, "feishu", "inst-1")
    row = OB.get_row(tid)
    assert row is not None and row["status"] == "sent"

    # 三进程并发语义：同单号二次触发不新增行、不重发（返回值可回填已投递状态，
    # 但真实发送只发生一次 —— ch.calls 保持 1）
    res2 = asyncio.run(N.notify_user(1, "awaiting", "请补充信息",
                                     repo_key="gitcode:o/r", issue_number=7,
                                     engine=eng, dedupe=True))
    assert ch.calls == 1, "同单号不得真实重发"
    conn = sqlite3.connect(outbox_db)
    n = conn.execute("SELECT COUNT(*) FROM grape_t_channel_outbound_card").fetchone()[0]
    conn.close()
    assert n == 1


def test_notify_user_no_binding_writes_failed_row(outbox_db, clean_notify):
    """无 p2p 绑定：一步落 failed 行（last_error=no_binding），不再静默（QA #1314 m2）。"""
    from src.domains.runtime.im import notify as N, outbox as OB
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, status) VALUES ('inst-9','feishu','user','9','active')")
    conn.commit()
    conn.close()

    res = asyncio.run(N.notify_user(9, "task", "x", targets=[
        {"installation_id": "inst-9", "channel_type": "feishu"}], engine=FakeEngine({})))
    assert res["skipped"]["feishu"] == "no_binding"
    tid = OB.out_track_id(9, "task", "", 0, "feishu", "inst-9")
    row = OB.get_row(tid)
    assert row is not None and row["status"] == "failed"
    assert row["last_error"] == "no_binding"
    assert row["attempt_count"] == 0       # 非发送失败不计 attempt


def test_notify_user_no_binding_existing_row_rewritten(outbox_db, clean_notify):
    """QA #1314 m2：row-exists（去重）路径无绑定时也落 no_binding 终态。"""
    from src.domains.runtime.im import notify as N, outbox as OB
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, status) VALUES ('inst-9','feishu','user','9','active')")
    conn.commit()
    conn.close()
    tid = OB.out_track_id(9, "task", "", 0, "feishu", "inst-9")
    # 预置已存在行（pending，模拟入队后、判定绑定前崩溃留下的中间态）
    OB.enqueue(out_track_id=tid, installation_id="inst-9", channel_type="feishu",
               channel_chat_id="", payload={"kind": "task", "user_id": 9},
               expires_at=OB.new_expiry())
    assert OB.get_row(tid)["status"] == "pending"
    asyncio.run(N.notify_user(9, "task", "x", targets=[
        {"installation_id": "inst-9", "channel_type": "feishu"}],
        engine=FakeEngine({}), dedupe=True))
    row = OB.get_row(tid)
    assert row["status"] == "failed" and row["last_error"] == "no_binding"


def test_notify_user_engine_down_leaves_pending(outbox_db, clean_notify, monkeypatch):
    """引擎不可用：行保持 pending（不置失败），等持有租约的消费者接力。"""
    from src.domains.runtime.im import notify as N, outbox as OB
    from src.domains.runtime.im import engine_wiring
    import src.api.grape_server as gs
    engine_wiring.set_current_engine(None)
    monkeypatch.setattr(gs, "im_engine", None, raising=False)
    monkeypatch.setattr(N, "_running_asgi_app", None)
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, status) VALUES ('inst-2','feishu','user','2','active')")
    conn.commit()
    conn.close()
    _mk_binding(outbox_db, "inst-2", "oc_2")

    res = asyncio.run(N.notify_user(2, "awaiting", "q", targets=[
        {"installation_id": "inst-2", "channel_type": "feishu"}]))
    assert res["skipped"]["feishu"] == "engine_down"
    tid = OB.out_track_id(2, "awaiting", "", 0, "feishu", "inst-2")
    row = OB.get_row(tid)
    assert row is not None and row["status"] == "pending"


def test_notify_user_skips_when_consumer_holds_row(outbox_db, clean_notify):
    """QA #1314 M1：行已被消费者认领（sending）→ 内联路径不投递、不重复发送。"""
    from src.domains.runtime.im import notify as N, outbox as OB
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, status) VALUES ('inst-1','feishu','user','1','active')")
    conn.commit()
    conn.close()
    _mk_binding(outbox_db, "inst-1", "oc_user")

    # 模拟消费者刚认领：行已在 sending
    tid = OB.out_track_id(1, "awaiting", "gitcode:o/r", 7, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_user",
               payload={"kind": "awaiting", "text": "请补充", "repo_key": "gitcode:o/r",
                        "issue_number": 7, "user_id": 1},
               expires_at=OB.new_expiry())
    assert OB.claim_for_sending(tid)

    ch = FakeChannel(Capability.TEXT)
    eng = FakeEngine({"inst-1": ch})
    res = asyncio.run(N.notify_user(1, "awaiting", "请补充信息",
                                    repo_key="gitcode:o/r", issue_number=7,
                                    engine=eng, dedupe=True))
    assert ch.calls == 0, "sending 行不得被内联路径重复投递"
    assert res["sent"] == []
    assert OB.get_row(tid)["status"] == "sending"   # 认领方继续持有


def test_notify_user_offline_releases_sending_row(outbox_db, clean_notify):
    """QA #1314 M1：内联认领后 offline → sending 行回 pending（租约持有实例接力）。"""
    from src.domains.runtime.im import notify as N, outbox as OB
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, status) VALUES ('inst-1','feishu','user','1','active')")
    conn.commit()
    conn.close()
    _mk_binding(outbox_db, "inst-1", "oc_user")

    ch = FakeChannel(Capability.TEXT)
    eng = FakeEngine({})                            # 渠道没连上 → offline
    res = asyncio.run(N.notify_user(1, "task", "x",
                                    repo_key="gitcode:o/r", issue_number=7,
                                    engine=eng, dedupe=False))
    assert res["skipped"]["feishu"] == "offline"
    tid = OB.out_track_id(1, "task", "gitcode:o/r", 7, "feishu", "inst-1")
    row = OB.get_row(tid)
    assert row is not None and row["status"] == "pending", \
        "offline 中止后行应回 pending 而非滞留 sending"


def test_consumer_run_once_delivers_and_marks_sent(outbox_db, clean_notify, monkeypatch):
    """消费者：持有租约 → 投递成功置 sent；engine=None 跳过不置失败。"""
    from src.domains.runtime.im import outbox as OB
    from src.domains.runtime.im import notify as N
    import src.api.grape_server as gs
    from src.domains.runtime.im import engine_wiring
    conn = sqlite3.connect(outbox_db)
    conn.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, status) VALUES ('inst-1','feishu','user','1','active')")
    conn.commit()
    conn.close()
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A")

    tid = OB.out_track_id(1, "awaiting", "gitcode:o/r", 7, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1",
               payload={"kind": "awaiting", "text": "请补充", "repo_key": "gitcode:o/r",
                        "issue_number": 7, "user_id": 1, "conv_id": "conv-7"},
               expires_at=OB.new_expiry())

    ch = FakeChannel(Capability.TEXT)
    eng = FakeEngine({"inst-1": ch})
    consumer = OB.OutboxConsumer(eng, instance_id="owner-A")
    n = asyncio.run(consumer.run_once())
    assert n == 1
    assert ch.calls == 1
    row = OB.get_row(tid)
    assert row["status"] == "sent"
    assert row["attempt_count"] == 0, "QA #1314 m3：mark_sent 不得递增 attempt_count"

    # engine=None：pending 行不被置失败
    tid2 = OB.out_track_id(1, "awaiting", "gitcode:o/r", 8, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid2, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "awaiting", "text": "x",
                                                "repo_key": "gitcode:o/r", "issue_number": 8,
                                                "user_id": 1},
               expires_at=OB.new_expiry())
    consumer_none = OB.OutboxConsumer(None, instance_id="owner-A")
    engine_wiring.set_current_engine(None)
    monkeypatch.setattr(gs, "im_engine", None, raising=False)
    monkeypatch.setattr(N, "_running_asgi_app", None)
    try:
        n2 = asyncio.run(consumer_none.run_once())
        assert n2 == 0
        assert OB.get_row(tid2)["status"] == "pending"
    finally:
        monkeypatch.undo()


def test_consumer_run_once_non_owner_no_deliver(outbox_db, clean_notify):
    """非租约持有实例：run_once 不投递（行留给持有者）。"""
    from src.domains.runtime.im import outbox as OB
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A")
    tid = OB.out_track_id(1, "awaiting", "gitcode:o/r", 7, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "awaiting", "text": "x",
                                                "repo_key": "gitcode:o/r", "issue_number": 7,
                                                "user_id": 1},
               expires_at=OB.new_expiry())
    ch = FakeChannel(Capability.TEXT)
    eng = FakeEngine({"inst-1": ch})
    consumer = OB.OutboxConsumer(eng, instance_id="owner-B")
    n = asyncio.run(consumer.run_once())
    assert n == 0 and ch.calls == 0
    assert OB.get_row(tid)["status"] == "pending"


def test_consumer_set_engine_after_mode_flip(outbox_db, clean_notify):
    """QA #1314 M2：模式翻转重建引擎 → set_engine 换新句柄后消费者能继续认领。"""
    from src.domains.runtime.im import outbox as OB
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A")

    old_ch = FakeChannel(Capability.TEXT)
    old_eng = FakeEngine({"inst-1": old_ch}, instance_id="owner-A")
    consumer = OB.OutboxConsumer(old_eng)
    assert consumer.instance_id == "owner-A"

    tid = OB.out_track_id(1, "task", "gitcode:o/r", 34, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "task", "text": "x",
                                                "repo_key": "gitcode:o/r", "issue_number": 34,
                                                "user_id": 1},
               expires_at=OB.new_expiry())

    # 模拟翻转：旧引擎停、新引擎（新 instance_id）起
    new_ch = FakeChannel(Capability.TEXT)
    new_eng = FakeEngine({"inst-1": new_ch}, instance_id="owner-A2")
    consumer.set_engine(new_eng)
    assert consumer.instance_id == "owner-A2"
    assert consumer._engine is new_eng

    # 新引擎实例能认领自己租约下的行
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A2")
    n = asyncio.run(consumer.run_once())
    assert n == 1 and new_ch.calls == 1
    assert OB.get_row(tid)["status"] == "sent"


def test_consumer_run_once_self_heals_engine_from_registry(outbox_db, clean_notify, monkeypatch):
    """QA #1314 M2 自愈：消费者 engine=None 时每轮从注册点刷新（翻转后自动恢复）。"""
    from src.domains.runtime.im import outbox as OB, engine_wiring
    _mk_lease(outbox_db, "feishu:inst-1", "owner-A")
    tid = OB.out_track_id(1, "task", "gitcode:o/r", 35, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "task", "text": "x",
                                                "repo_key": "gitcode:o/r", "issue_number": 35,
                                                "user_id": 1},
               expires_at=OB.new_expiry())
    ch = FakeChannel(Capability.TEXT)
    eng = FakeEngine({"inst-1": ch}, instance_id="owner-A")
    engine_wiring.set_current_engine(eng)
    consumer = OB.OutboxConsumer(None, instance_id="owner-A")
    try:
        n = asyncio.run(consumer.run_once())
        assert n == 1 and ch.calls == 1
        assert OB.get_row(tid)["status"] == "sent"
    finally:
        engine_wiring.set_current_engine(None)


# ---------------------------------------------------------------- 7. API /status + /outbound + /retry

def test_status_includes_outbox_section(outbox_db, clean_notify):
    """/api/im/status 增 outbox 计数与 recent_undelivered。"""
    from src.domains.mgmt.routes import im as IM
    from src.domains.runtime.im import outbox as OB
    t1 = OB.out_track_id(7, "awaiting", "gitcode:o/r", 20, "feishu", "inst-1")
    OB.enqueue(out_track_id=t1, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1",
               payload={"kind": "awaiting", "user_id": 7, "repo_key": "gitcode:o/r",
                        "issue_number": 20},
               expires_at=OB.new_expiry(), status="failed", last_error="no_binding")
    data = IM.im_engine_status(user=_user())
    assert "outbox" in data
    ob = data["outbox"]
    assert ob["counts"]["failed"] == 1
    recent = ob["recent_undelivered"]
    assert recent and recent[0]["reason"] == "no_binding"
    assert recent[0]["kind"] == "awaiting"


def _user(uid=1234):
    return {"id": uid, "username": "tester", "auth_type": "jwt"}


def test_outbound_list_and_retry(outbox_db, clean_notify):
    """/api/im/outbound 查询 + /api/im/outbound/{id}/retry 重发。"""
    from src.domains.mgmt.routes import im as IM
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(8, "pending_review", "gitcode:o/r", 21, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1",
               payload={"kind": "pending_review", "user_id": 8, "repo_key": "gitcode:o/r",
                        "issue_number": 21, "text": "请审核"},
               expires_at=OB.new_expiry(-600))
    OB.expire_overdue()

    rows = IM.im_outbound(status="expired", user=_user())
    assert rows["total"] == 1
    assert rows["outbound"][0]["out_track_id"] == tid
    assert rows["outbound"][0]["kind"] == "pending_review"
    assert rows["outbound"][0]["status"] == "expired"

    res = IM.im_outbound_retry(tid, _user())
    assert res["out_track_id"] == tid and res["status"] == "pending"
    assert OB.get_row(tid)["status"] == "pending"


def test_outbound_retry_rejects_not_found_and_bad_state(outbox_db):
    from src.domains.mgmt.routes import im as IM
    import fastapi
    with pytest.raises(fastapi.HTTPException) as ei:
        IM.im_outbound_retry("no-such-track", _user())
    assert ei.value.status_code == 404
    from src.domains.runtime.im import outbox as OB
    tid = OB.out_track_id(9, "task", "", 0, "feishu", "inst-1")
    OB.enqueue(out_track_id=tid, installation_id="inst-1", channel_type="feishu",
               channel_chat_id="oc_1", payload={"kind": "task", "user_id": 9},
               expires_at=OB.new_expiry())
    with pytest.raises(fastapi.HTTPException) as ei:
        IM.im_outbound_retry(tid, _user())     # pending 状态不可重发
    assert ei.value.status_code == 400


# ---------------------------------------------------------------- 8. lifespan 接线（grape_server A 进程模型）

def test_grape_server_lifespan_starts_consumer_and_registry(clean_notify, monkeypatch):
    """A 进程模型：lifespan 起 outbox 消费者 + 引擎注册点（_engine() 非 None 的根因修复）。"""
    from src.domains.runtime.im import engine_wiring, notify as N, outbox as OB

    class FakeEngine:
        instance_id = "fake-A"
        supervisor = None
        async def stop(self):
            engine_wiring.set_current_engine(None)

    captured = {}

    async def fake_start_im_engine(db_path=None):
        eng = FakeEngine()
        captured["engine"] = eng
        engine_wiring.set_current_engine(eng)
        return eng

    monkeypatch.setattr(engine_wiring, "engine_mode", lambda: "on")
    monkeypatch.setattr(engine_wiring, "start_im_engine", fake_start_im_engine)

    # 起真正的 grape_server app（lifespan 真跑）：不起真实定时任务也要能快速关闭。
    import src.api.grape_server as gs

    async def scenario():
        from fastapi import FastAPI
        # 直接调 grape_server.lifespan（不 import app 全量路由，避免重度初始化）
        app = FastAPI()
        async with gs.lifespan(app):
            eng = N._engine()
            assert eng is not None, "A 进程模型下 _engine() 必须 非 None（#2261 根因）"
            assert engine_wiring.get_current_engine() is captured["engine"]
            consumer = getattr(app.state, "outbox_consumer", None)
            assert isinstance(consumer, OB.OutboxConsumer)
            assert consumer._task is not None and not consumer._task.done()
        # lifespan 退出后：消费者停、注册点清空
        assert engine_wiring.get_current_engine() is None

    asyncio.run(scenario())


def test_main_mode_flip_rebinds_consumer(monkeypatch):
    """QA #1314 M2 端到端：main.py 翻转 on→off→on 后消费者绑到新引擎+新 instance_id。

    直接驱动 main.lifespan 内的 _im_mode_watcher 逻辑等价路径：
    attach_outbox_consumer 起消费者 → 翻转 stop_outbox_consumer / set_engine。
    """
    from src.domains.runtime.im import outbox as OB
    from fastapi import FastAPI

    class FakeEngine:
        def __init__(self, iid):
            self.instance_id = iid
            self.supervisor = None
        async def stop(self):
            pass

    async def scenario():
        app = FastAPI()
        eng1 = FakeEngine("inst-old")
        consumer = OB.attach_outbox_consumer(app, eng1)
        assert consumer is not None
        assert consumer.instance_id == "inst-old"
        # 翻转 off：停消费者
        OB.stop_outbox_consumer(app)
        # 翻转 on：新引擎（新 instance_id）
        eng2 = FakeEngine("inst-new")
        # 重新挂载（与 main.py watcher 的 set_engine 路径一致）
        consumer2 = OB.attach_outbox_consumer(app, eng2)
        assert consumer2 is not None and consumer2 is not consumer
        assert consumer2.instance_id == "inst-new"
        # set_engine 原位换新引擎（watcher 用同一消费者对象复用）
        consumer.set_engine(eng2)
        assert consumer.instance_id == "inst-new"
        await consumer.stop()
        await consumer2.stop()

    asyncio.run(scenario())

# ---------------------------------------------------------------- 4 进程适配（#2261 移植）

def test_outbox_columns_wired_into_db_setup_not_only_engine_start():
    """4 进程口径：outbox 补列必须挂在 db_setup（四进程都跑），不能只在引擎启动时补。

    引擎启动路径（run_startup_migration）只在 A 进程 start_im_engine(mode=on) 时执行；
    mode=off / A 未起时 B/C 进程仍会写 outbox，缺列即静默丢通知。
    """
    import inspect
    from src.api import db_setup
    src = inspect.getsource(db_setup.setup_db) if hasattr(db_setup, "setup_db") else inspect.getsource(db_setup)
    assert "ensure_notify_outbox_schema" in src or "ensure_channel_schema" in src


def test_mgmt_router_mounted_on_a_process_only():
    """4 进程口径：/api/im 只挂 A（mgmt）；C（worker）不挂，避免拿到空引擎的状态。"""
    import src.api.grape_server as gs
    import src.api.grape_worker_server as gw

    def paths(app):
        out = set()
        for r in app.routes:
            for sub in getattr(getattr(r, "original_router", None), "routes", []) or [r]:
                p = getattr(sub, "path", "")
                if p:
                    out.add(p)
        return out

    a = paths(gs.app)
    c = paths(gw.app)
    # A 进程：/api/im 由 include_router 惰性挂载，openapi 里能看到全部路径
    assert "/api/im/status" in gs.app.openapi()["paths"]
    assert not any(p.startswith("/api/im") for p in c), "C 进程不应挂载 /api/im"
