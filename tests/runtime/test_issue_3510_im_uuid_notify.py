"""Issue #3510：IM 通知对 UUID 账号 id 失效（裸 int() 强转异常被吞 → 站内信静默丢失）。

关键点：**保留真实 `out_box.out_track_id`**（#3469 的用例把它打桩了，恰好掩盖了本缺陷），
只打桩 `active_targets` / `p2p_chat_id` / `enqueue` 这三个外部依赖。
"""
import hashlib

import pytest


UUID_UID = "2ec8bad0-8ea1-4f71-9416-e508dc36f97e"


def test_out_track_id_accepts_uuid_account_id():
    """UUID 账号 id 必须能出单号（原实现 `int(user_id)` → ValueError）。"""
    from src.domains.runtime.im.outbox import out_track_id

    tid = out_track_id(UUID_UID, "email_change", "", 0, "feishu", "inst-1")
    assert isinstance(tid, str) and len(tid) == 40

    other = out_track_id("11111111-2222-3333-4444-555555555555", "email_change",
                         "", 0, "feishu", "inst-1")
    assert tid != other          # 不同账号不得撞单号

    # 同一输入幂等（跨进程只发一次的前提）
    assert tid == out_track_id(UUID_UID, "email_change", "", 0, "feishu", "inst-1")


def test_out_track_id_numeric_identity_unchanged():
    """数字 id 的 sha1 输入不得漂移，否则历史 pending 行会被当成新事件重发。"""
    from src.domains.runtime.im.outbox import out_track_id

    expected = hashlib.sha1(b"24|email_change||0|feishu|inst-1").hexdigest()[:40]
    assert out_track_id(24, "email_change", "", 0, "feishu", "inst-1") == expected
    # 数字以字符串形式传入，口径必须与 int 完全一致
    assert out_track_id("24", "email_change", "", 0, "feishu", "inst-1") == expected


def test_norm_uid_keeps_uuid_and_coerces_digits():
    from src.kernel.user_ids import norm_uid

    assert norm_uid(UUID_UID) == UUID_UID
    assert norm_uid(" 24 ") == 24
    assert norm_uid(24) == 24
    assert norm_uid(None) == ""
    # 历史上 `int(user_id or 0)` 对 UUID 必炸——这里反向锁住"别再回来"
    with pytest.raises(ValueError):
        int(UUID_UID)


def test_schedule_notify_enqueues_for_uuid_account(monkeypatch):
    """复现 #3510 的原始路径：UUID 账号 + 真实 out_track_id + 无事件循环。"""
    from src.domains.runtime.im import notify as im_notify
    from src.domains.runtime.im import outbox as OB

    monkeypatch.setattr(im_notify, "active_targets",
                        lambda uid: [{"installation_id": "inst-1", "channel_type": "feishu"}],
                        raising=False)
    monkeypatch.setattr(im_notify, "p2p_chat_id", lambda inst: "chat-1", raising=False)
    monkeypatch.setattr(OB, "new_expiry", lambda: "2099-01-01 00:00:00", raising=False)

    queued = []

    def _fake_enqueue(**kw):
        queued.append(kw)
        return True

    monkeypatch.setattr(OB, "enqueue", _fake_enqueue, raising=False)

    assert im_notify.schedule_notify(UUID_UID, "email_change", "改绑提醒") is True
    assert len(queued) == 1, "UUID 账号必须真的入队（原实现这里因 int() 失败被吞）"
    row = queued[0]
    assert row["installation_id"] == "inst-1"
    assert isinstance(row["payload"]["user_id"], str)      # UUID 原样进 payload
    assert row["payload"]["user_id"] == UUID_UID
    assert len(row["out_track_id"]) == 40 and row["status"] if "status" in row else True


def test_enqueue_identity_uses_uuid_not_zero(monkeypatch):
    """曾出现过的退化形态：int() 失败后 uid=0，所有 UUID 账号撞成同一个单号。"""
    from src.domains.runtime.im.outbox import out_track_id

    a = out_track_id(UUID_UID, "email_change", "", 0, "feishu", "inst-1")
    b = out_track_id("ffffffff-0000-1111-2222-333333333333", "email_change",
                     "", 0, "feishu", "inst-1")
    zero = out_track_id(0, "email_change", "", 0, "feishu", "inst-1")
    assert a != zero and b != zero