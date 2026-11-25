"""Issue #3510 —— IM/站内信通知对 UUID 账号 id 的端到端回归。

缺陷：`grape_accounts.id` 自 D105 起是 UUIDv7 字符串，但通知链多处裸 `int(user_id)`，
UUID 抛 ValueError 后被各层 except 吞掉，表现为「通知没发但调用方以为成功」。

本用例只打桩 `active_targets` / `p2p_chat_id` / 引擎解析，**保留真实 out_track_id
与 outbox 入队**（#3469 的用例把 out_track_id 打桩了，测不出这个缺陷）。
"""
import asyncio
import hashlib
import os
import pathlib
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, ROOT)

UUID_UID = "2ec8bad0-8ea1-4f71-9416-e508dc36f97e"


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


@pytest.fixture()
def uuid_target(monkeypatch):
    """目标解析打桩：一条 active 的飞书安装 + 一个 p2p 会话（其余走真实实现）。"""
    from src.domains.runtime.im import notify as N
    monkeypatch.setattr(N, "active_targets",
                        lambda uid: [{"installation_id": "inst-uuid",
                                      "channel_type": "feishu"}])
    monkeypatch.setattr(N, "p2p_chat_id", lambda inst: "oc_uuid")
    monkeypatch.setattr(N, "_engine", lambda: None)      # 本进程无引擎 → 行保持 pending
    return {"installation_id": "inst-uuid", "channel_type": "feishu"}


def test_norm_uid_keeps_numeric_int_and_uuid_string():
    """归一口径：纯数字转 int（旧单号不漂移），UUID 原样保留字符串。"""
    from src.kernel.user_ids import norm_uid

    assert norm_uid("24") == 24 and isinstance(norm_uid("24"), int)
    assert norm_uid(24) == 24
    assert norm_uid(UUID_UID) == UUID_UID
    assert norm_uid(UUID_UID) != 0


def test_out_track_id_numeric_unchanged_and_uuid_supported():
    """数字 id 的 sha1 口径与现网一致；UUID 不再抛 ValueError。"""
    from src.domains.runtime.im import outbox as OB

    expected = hashlib.sha1(b"24|email_change||0|feishu|inst-1").hexdigest()[:40]
    assert OB.out_track_id(24, "email_change", "", 0, "feishu", "inst-1") == expected
    assert OB.out_track_id("24", "email_change", "", 0, "feishu", "inst-1") == expected

    uuid_track = OB.out_track_id(UUID_UID, "email_change", "", 0, "feishu", "inst-1")
    assert len(uuid_track) == 40 and uuid_track != expected


def test_notify_user_uuid_enqueues_real_track(outbox_db, clean_notify, uuid_target):
    """UUID 账号走真实 out_track_id 入队，不再静默失败。"""
    from src.domains.runtime.im import notify as N
    from src.domains.runtime.im import outbox as OB

    res = asyncio.run(N.notify_user(UUID_UID, "email_change", "x", dedupe=False))
    assert res["skipped"]["feishu"] == "engine_down"

    track = OB.out_track_id(UUID_UID, "email_change", "", 0, "feishu", "inst-uuid")
    row = OB.get_row(track)
    assert row is not None, "UUID 账号必须落 outbox 行"
    assert row["status"] == "pending"
    assert row["payload"]["user_id"] == UUID_UID
    assert row["payload"]["kind"] == "email_change"


def test_notify_user_numeric_payload_stays_int(outbox_db, clean_notify, monkeypatch):
    """数字账号行为不变：同单号入队，payload user_id 仍是 int（兼容既有消费侧）。"""
    from src.domains.runtime.im import notify as N
    from src.domains.runtime.im import outbox as OB

    monkeypatch.setattr(N, "active_targets",
                        lambda uid: [{"installation_id": "inst-24",
                                      "channel_type": "feishu"}])
    monkeypatch.setattr(N, "p2p_chat_id", lambda inst: "oc_24")
    monkeypatch.setattr(N, "_engine", lambda: None)

    res = asyncio.run(N.notify_user(24, "email_change", "x", dedupe=False))
    assert res["skipped"]["feishu"] == "engine_down"

    track = OB.out_track_id(24, "email_change", "", 0, "feishu", "inst-24")
    row = OB.get_row(track)
    assert row is not None and row["status"] == "pending"
    assert row["payload"]["user_id"] == 24


def test_schedule_notify_uuid_returns_true_and_enqueues(outbox_db, clean_notify, uuid_target):
    """异步直投路径 schedule_notify：UUID 返回 True（AC）且真实入队。"""
    from src.domains.runtime.im import notify as N
    from src.domains.runtime.im import outbox as OB

    async def _run():
        assert N.schedule_notify(UUID_UID, "email_change", "x") is True
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert pending, "schedule_notify 应创建投递任务"
        await asyncio.gather(*pending)

    asyncio.run(_run())
    track = OB.out_track_id(UUID_UID, "email_change", "", 0, "feishu", "inst-uuid")
    assert OB.get_row(track) is not None


def _fake_issue_db(monkeypatch, row):
    """把 _issue_owner() 内 ``from session_manager import db`` 换成固定行。"""
    from src.kernel import session_manager as SM

    class _Cursor:
        def execute(self, *_a, **_kw):
            return self

        def fetchone(self):
            return row

    class _DB:
        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(SM, "db", _DB())


def test_issue_owner_keeps_uuid_and_numeric(monkeypatch):
    """Issue 归属 user_id 为 TEXT：UUID 保留 str，数字仍归一为 int。"""
    from src.domains.runtime.im import notify as N

    _fake_issue_db(monkeypatch, (UUID_UID, "conv-uuid", "Title"))
    uid, conv, title = N._issue_owner("repo", 1)
    assert uid == UUID_UID and isinstance(uid, str)
    assert (conv, title) == ("conv-uuid", "Title")

    _fake_issue_db(monkeypatch, ("24", "", ""))
    uid, _conv, _title = N._issue_owner("repo", 1)
    assert uid == 24 and isinstance(uid, int)


def test_notify_issue_event_uuid_owner_not_dropped(monkeypatch):
    """UUID 归属 Issue 走 notify_issue_event 不再被 uid=0 吞掉。"""
    from src.domains.runtime.im import notify as N

    _fake_issue_db(monkeypatch, (UUID_UID, "", ""))
    calls = []
    monkeypatch.setattr(N, "schedule_notify",
                        lambda uid, *a, **kw: calls.append((uid, a, kw)) or True)

    N.notify_issue_event("repo", 7, "email_change", "x")

    assert calls and calls[0][0] == UUID_UID
    assert calls[0][2].get("repo_key") == "repo"
    assert calls[0][2].get("issue_number") == 7


def test_handlers_usage_user_id_behavior():
    """两处 handler 用量采集归一（行为/类型断言）：UUID 保持 str，数字保持 int。"""
    from src.domains.runtime.im import handler_async as chat
    from src.domains.runtime.im import vod_handler_async as vod

    for mod in (chat, vod):
        uuid_uid = mod._usage_user_id(UUID_UID)
        assert uuid_uid == UUID_UID and isinstance(uuid_uid, str)
        numeric_uid = mod._usage_user_id("24")
        assert numeric_uid == 24 and isinstance(numeric_uid, int)
        assert mod._usage_user_id(None) == 0


def test_handlers_use_norm_uid_not_bare_int():
    """两处用量采集 payload 构造：UUID 账号不得再被裸 int() 强转。"""
    src = pathlib.Path(ROOT) / "src" / "domains" / "runtime" / "im"
    vod = (src / "vod_handler_async.py").read_text(encoding="utf-8")
    chat = (src / "handler_async.py").read_text(encoding="utf-8")

    assert '_usage_user_id(agent_session.get("user_id"))' in vod
    assert 'int(agent_session.get("user_id") or 0)' not in vod
    assert '_usage_user_id(user_id)' in chat
    assert '"user_id": int(user_id or 0)' not in chat
