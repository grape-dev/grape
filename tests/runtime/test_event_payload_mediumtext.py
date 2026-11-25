"""Issue #4339：`events.payload` 升 MEDIUMTEXT + 写入口字节守卫。

背景：payload 列曾是 TEXT(65,535)，同表 `agent_logs`/`result`/`structured_output`
都是 MEDIUMTEXT。超 64KB 的 issue/PR 事件 payload 在 MySQL 非严格 `sql_mode` 下被
静默截断成半截 JSON（生产 seq 107279/107291 均恰好 65,535 字节且 `JSON_VALID=0`），
`payload_view_from_json` 解析失败后整份 payload 视图降级为空 —— 静默数据丢失。

本文件锁定四件事：

1. 建表 DDL（events / 卡片表）与 ALTER 常量的文本口径，尤其 **ALTER 必须带
   `MODIFY COLUMN`**——迁移执行器只把含该关键字的语句路由到 `_destructive_ddl`
   （`min_code_version` 门槛 + 失败抛出），缺 `COLUMN` 会绕过门槛且失败被吞；
2. 两条注册迁移在位（版本/名字/域/post_check），且版本号大于已发布基线 max（R3）；
3. 迁移的五条分支：已目标态零 DDL、`text` → ALTER + 复检、门槛拦截抛错、
   ALTER 后仍为 `text` 抛错、非 owner（缺表 / 跨库视图）抛 `_NonOwnerObject`；
4. 写入口守卫：>64KB 落库可解析、>1MB 留 WARNING、>16MB ERROR + 拒绝写入且库中无行。
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import sqlite3

import pytest

from src.api import db_setup as ds
from src.domains.mgmt import migrations_ddl as mgmt_mig
from src.domains.mgmt.migrations import channel_outbound_card_payload_mediumtext as card_mig
from src.domains.mgmt.schema_ddl import _CHANNEL_OUTBOUND_CARD_DDL
from src.domains.runtime import event_store as es
from src.domains.runtime import migrations_ddl as runtime_mig
from src.domains.runtime.migrations import events_payload_mediumtext as payload_mig
from src.domains.runtime.schema_ddl import _EVENTS_TABLE_DDL
from src.kernel.event_payload_view import payload_view_from_json
from src.kernel.session_manager import db, db_lock

REPO = "gitcode:test/issue4339-payload"

# ===== 1. DDL / ALTER 常量文本口径 =====


def test_events_ddl_payload_is_mediumtext():
    assert re.search(r"payload\s+MEDIUMTEXT\s+DEFAULT '\{\}'", _EVENTS_TABLE_DDL), \
        "events.payload 建表 DDL 未升 MEDIUMTEXT（新装库仍会 64KB 截断）"


def test_channel_outbound_card_ddl_payload_is_mediumtext():
    assert re.search(
        r"payload\s+MEDIUMTEXT\s+NOT\s+NULL\s+DEFAULT '\{\}'",
        _CHANNEL_OUTBOUND_CARD_DDL), \
        "grape_t_channel_outbound_card.payload 建表 DDL 未升 MEDIUMTEXT"


@pytest.mark.parametrize("ddl", [
    runtime_mig.ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT,
    mgmt_mig.ALTER_GRAPE_T_CHANNEL_OUTBOUND_CARD__PAYLOAD_MEDIUMTEXT,
])
def test_alter_constants_carry_modify_column(ddl):
    """必须含 `MODIFY COLUMN`：缺 COLUMN 的语句走不到 _destructive_ddl 收口。"""
    assert "MODIFY COLUMN" in ddl, f"ALTER 缺 COLUMN 关键字（会绕过门槛）: {ddl}"
    assert "MEDIUMTEXT" in ddl
    assert not re.search(r"MODIFY\s+(?!COLUMN)", ddl), ddl


# ===== 2. 注册与版本号 =====


def _registered(version: str):
    return next((m for m in ds._MIGRATIONS if m.version == version), None)


def test_events_payload_migration_registered_on_runtime_domain():
    mig = _registered(payload_mig.MIGRATION_VERSION)
    assert mig is not None, "202611250001 未注册（存量库永远停在 TEXT）"
    assert mig.name == payload_mig.MIGRATION_NAME
    assert "runtime" in mig.domains
    assert mig.post_check is payload_mig.verify_events_payload_mediumtext


def test_card_payload_migration_registered_on_mgmt_domain():
    mig = _registered(card_mig.MIGRATION_VERSION)
    assert mig is not None, "202611250002 未注册（存量卡片表永远停在 TEXT）"
    assert mig.name == card_mig.MIGRATION_NAME
    assert "mgmt" in mig.domains
    assert mig.post_check is card_mig.verify_channel_outbound_card_payload_mediumtext


def test_new_versions_are_beyond_released_baseline():
    """R3：新迁移号必须大于基线 max_version，否则往历史区间塞号被判红。"""
    baseline = json.loads(
        (pathlib.Path(__file__).resolve().parents[2]
         / "tests" / "arch" / "migration_versions_baseline.json").read_text(encoding="utf-8"))
    released_max = baseline["max_version"]
    assert payload_mig.MIGRATION_VERSION > released_max
    assert card_mig.MIGRATION_VERSION > released_max


# ===== 3. 迁移分支（假 cursor，无需 MySQL）=====


class _Conn:
    """最小连接替身：迁移只用到 cursor()/commit()。"""

    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


class _ProbeCursor:
    """按序返回 information_schema 探测结果，并记录被执行的 DDL。"""

    def __init__(self, data_types, *, missing=False):
        self._types = list(data_types)
        self._missing = missing
        self.executed = []
        self._row = None

    def execute(self, sql, params=None):
        if "information_schema" in sql:
            if self._missing:
                self._row = None
            else:
                self._row = (self._types.pop(0),) if self._types else None
        else:
            self.executed.append(sql)
            self._row = None
        return self

    def fetchone(self):
        return self._row


def _patch_mysql(monkeypatch, *, present=True, base=True):
    monkeypatch.setattr(ds, "_mysql_backend", lambda: True)
    monkeypatch.setattr(ds, "_table_present", lambda c, t: present)
    monkeypatch.setattr(ds, "_is_base_table", lambda c, t: base)


def test_events_migration_noop_when_already_mediumtext(monkeypatch):
    """幂等核心：已是目标态 → 零 ALTER（COPY 算法不能每次启动重放）。"""
    _patch_mysql(monkeypatch)
    cursor = _ProbeCursor(["mediumtext"])
    calls = []
    monkeypatch.setattr(ds, "_destructive_ddl",
                        lambda c, ddl, **kw: calls.append(ddl) or True)
    conn = _Conn(cursor)

    stats = payload_mig.migrate_events_payload_mediumtext(conn=conn)

    assert stats == {"backend": "mysql", "before": "mediumtext",
                     "after": "mediumtext", "altered": False}
    assert calls == [], "已是 mediumtext 仍发了 ALTER"
    assert cursor.executed == []
    assert conn.commits == 0


def test_events_migration_alters_text_then_rechecks(monkeypatch):
    _patch_mysql(monkeypatch)
    cursor = _ProbeCursor(["text", "mediumtext"])
    calls = []
    monkeypatch.setattr(
        ds, "_destructive_ddl",
        lambda c, ddl, **kw: calls.append((ddl, kw)) or True)
    conn = _Conn(cursor)

    stats = payload_mig.migrate_events_payload_mediumtext(conn=conn)

    assert stats["altered"] is True
    assert stats["before"] == "text" and stats["after"] == "mediumtext"
    ddl, kwargs = calls[0]
    assert ddl is runtime_mig.ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT
    assert kwargs["kind_hint"] == "cleanup"
    assert kwargs["version"] == payload_mig.MIGRATION_VERSION
    assert conn.commits == 1


def test_events_migration_raises_when_gate_blocks_alter(monkeypatch):
    """门槛拦截 → 抛错不盖章（执行器以「正常返回 = 盖章」为约定）。"""
    _patch_mysql(monkeypatch)
    monkeypatch.setattr(ds, "_destructive_ddl", lambda c, ddl, **kw: False)
    with pytest.raises(RuntimeError, match="门槛"):
        payload_mig.migrate_events_payload_mediumtext(conn=_Conn(_ProbeCursor(["text"])))


def test_events_migration_raises_when_alter_did_not_take_effect(monkeypatch):
    """_destructive_ddl 对非 MDL 异常 fail-open → 必须复检 DATA_TYPE。"""
    _patch_mysql(monkeypatch)
    monkeypatch.setattr(ds, "_destructive_ddl", lambda c, ddl, **kw: True)
    with pytest.raises(RuntimeError, match="ALTER 后仍为 text"):
        payload_mig.migrate_events_payload_mediumtext(
            conn=_Conn(_ProbeCursor(["text", "text"])))


@pytest.mark.parametrize("present,base", [(False, True), (True, False)])
def test_events_migration_non_owner_raises_non_owner_object(monkeypatch, present, base):
    """缺表 / 跨库只读视图 → 跳过且不盖章，交 owner 域收敛。"""
    _patch_mysql(monkeypatch, present=present, base=base)
    with pytest.raises(ds._NonOwnerObject):
        payload_mig.migrate_events_payload_mediumtext(
            conn=_Conn(_ProbeCursor(["text"])))


def test_events_migration_sqlite_is_noop(monkeypatch):
    """SQLite 无列长语义：不发 DDL，新装库口径由 schema_ddl 保证。"""
    monkeypatch.setattr(ds, "_mysql_backend", lambda: False)
    conn = sqlite3.connect(":memory:")
    try:
        stats = payload_mig.migrate_events_payload_mediumtext(conn=conn)
    finally:
        conn.close()
    assert stats == {"backend": "sqlite", "before": "", "after": "", "altered": False}


def test_events_post_check_raises_when_still_text(monkeypatch):
    _patch_mysql(monkeypatch)
    with pytest.raises(RuntimeError, match="post_check 未收敛"):
        payload_mig.verify_events_payload_mediumtext(conn=_Conn(_ProbeCursor(["text"])))


def test_events_post_check_passes_at_target_state(monkeypatch):
    _patch_mysql(monkeypatch)
    payload_mig.verify_events_payload_mediumtext(conn=_Conn(_ProbeCursor(["mediumtext"])))


def test_card_migration_uses_mgmt_alter_and_is_idempotent(monkeypatch):
    _patch_mysql(monkeypatch)
    calls = []
    monkeypatch.setattr(
        ds, "_destructive_ddl",
        lambda c, ddl, **kw: calls.append(ddl) or True)

    noop = card_mig.migrate_channel_outbound_card_payload_mediumtext(
        conn=_Conn(_ProbeCursor(["mediumtext"])))
    assert noop["altered"] is False and calls == []

    altered = card_mig.migrate_channel_outbound_card_payload_mediumtext(
        conn=_Conn(_ProbeCursor(["text", "mediumtext"])))
    assert altered["altered"] is True
    assert calls == [mgmt_mig.ALTER_GRAPE_T_CHANNEL_OUTBOUND_CARD__PAYLOAD_MEDIUMTEXT]


def test_card_post_check_raises_when_still_text(monkeypatch):
    _patch_mysql(monkeypatch)
    with pytest.raises(RuntimeError, match="post_check 未收敛"):
        card_mig.verify_channel_outbound_card_payload_mediumtext(
            conn=_Conn(_ProbeCursor(["text"])))


# ===== 4. 写入口字节守卫（真 SQLite 测试库）=====


@pytest.fixture()
def repo_key():
    def _cleanup():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
            db.commit()

    _cleanup()
    yield REPO
    _cleanup()


def _row_payload(seq: int):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT LENGTH(payload), payload FROM events WHERE seq=?", (seq,))
        return c.fetchone()


def _count_events() -> int:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key=?", (REPO,))
        return int(c.fetchone()[0])


def test_payload_over_64kb_roundtrips_and_parses(repo_key):
    """验收核心：>64KB 的 issue_opened payload 完整落库且可解析（旧列会截断）。"""
    body = "x" * 70_000
    payload = {
        "issue": {"number": 4339, "title": "payload", "body": body},
        "files": [{"path": "src/domains/runtime/event_store.py", "additions": 30}],
        "guard_impact": {"changed": ["src/domains/runtime/schema_ddl.py"]},
    }
    seq = es.append_event(REPO, "issue_opened", "long issue body", issue_id="#4339",
                          agent_type="vod", payload=payload)
    assert seq > 0, "超 64KB 的事件应正常落库（列已升 MEDIUMTEXT）"

    length, raw = _row_payload(seq)
    assert int(length) > 65_535, f"payload 长度 {length}，未超过旧 TEXT 上限"
    decoded = json.loads(raw)          # 半截 JSON 会在这里抛错
    assert len(decoded["issue"]["body"]) == len(body)
    view = payload_view_from_json(raw, seq=seq)
    assert view, "payload_view_from_json 解析失败（视图降级为空）"
    assert view["guard_impact"]["changed"] == ["src/domains/runtime/schema_ddl.py"]


def test_payload_over_1mb_warns_but_is_stored(repo_key, caplog):
    payload = {"files": [{"path": "big.diff", "patch": "y" * 1_200_000}]}
    with caplog.at_level(logging.WARNING):
        seq = es.append_event(REPO, "push", "big payload", issue_id="#4339",
                              agent_type="coding", payload=payload)
    assert seq > 0
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "event payload large" in r.getMessage()]
    assert warnings, ">1MB 的 payload 未留 WARNING（无法观察 payload 增长趋势）"
    assert "bytes=" in warnings[0].getMessage()


def test_payload_over_16mb_is_rejected_with_error(repo_key, caplog):
    """>16MB：ERROR + 拒绝写入，绝不写截断值（库中不新增该事件行）。"""
    payload = {"blob": "z" * es._MEDIUMTEXT_MAX_BYTES}
    before = _count_events()
    with caplog.at_level(logging.ERROR):
        seq = es.append_event(REPO, "issue_opened", "huge payload", issue_id="#4339",
                              agent_type="vod", payload=payload)
    assert seq == 0, "超 MEDIUMTEXT 上限的 payload 必须拒绝写入"
    assert _count_events() == before, "被拒绝的事件不应落库（含截断行）"

    errors = [r for r in caplog.records
              if r.levelno == logging.ERROR and "event payload too large" in r.getMessage()]
    assert errors, "超限拒绝未留 ERROR"
    message = errors[0].getMessage()
    assert "bytes=" in message and str(es._MEDIUMTEXT_MAX_BYTES) in message
    assert "event_type=issue_opened" in message and REPO in message
    assert "#4339" in message


def test_payload_guard_thresholds_are_mediumtext_limits():
    assert es._MEDIUMTEXT_MAX_BYTES == 16_777_215
    assert es._PAYLOAD_WARN_BYTES == 1_048_576
    assert es._PAYLOAD_WARN_BYTES < es._MEDIUMTEXT_MAX_BYTES