"""Issue #3580/P3：webhook 收件箱在 **真 MySQL** 上的契约用例（opt-in）。

为什么需要这份用例：`tests/runtime/test_issue_3580_gitlab_multi_instance.py` 里的
收件箱用例跑在 conftest 强制的 SQLite 上，而线上事故全部只在 MySQL 侧出现：

- `last_error` 是 TEXT/MEDIUMTEXT 列，MySQL 不允许 `DEFAULT ''`；插入时省略该列在
  严格模式下直接 1364（SQLite 永远测不出来，当初就是这个 P0 绿测上线后 500）；
- 索引列/前缀长度、`INSERT ... ON DUPLICATE` 的方言差异；
- 默认 REPEATABLE READ：不结束事务就轮询，控制台读到的是旧快照，
  worker 明明写完了也看不见（所以下面的读取统一 `commit()` 后再读）。

设计纪律：默认 **skip**，绝不连应用库。只有显式给出 `GRAPE_TEST_MYSQL_DSN`
（形如 `mysql://user:pass@host:3306/db`，库必须是可随意清空的**专用测试库**）才跑，
CI 里由 MySQL service 提供该 DSN。用例只 CREATE TABLE / DELETE 行，不 DROP 库。
"""
from __future__ import annotations

import os
import threading
from urllib.parse import unquote, urlparse

import pytest

_DSN = os.environ.get("GRAPE_TEST_MYSQL_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="GRAPE_TEST_MYSQL_DSN 未设置：本用例需要隔离的 MySQL（CI 用 service 注入）",
)


def _dsn_parts(dsn: str) -> dict:
    u = urlparse(dsn)
    if u.scheme != "mysql" or not u.hostname or not u.path.strip("/"):
        raise pytest.skip(f"GRAPE_TEST_MYSQL_DSN 非法: {dsn!r}")
    return {
        "host": u.hostname,
        "port": u.port or 3306,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "database": u.path.strip("/"),
    }


@pytest.fixture()
def compat_db(monkeypatch):
    """把 webhook_inbox 的模块级 db/db_lock 换成隔离 MySQL 连接，用完清行。"""
    from src.kernel.mysql_compat import MySQLCompatDB
    import src.domains.runtime.webhook_inbox as wi

    db = MySQLCompatDB(charset="utf8mb4", **_dsn_parts(_DSN))
    lock = threading.RLock()
    # 每个用例都从空表开始：DDL 用 CREATE TABLE IF NOT EXISTS，残留旧形态会让
    # 「建表形态守卫」永远绿（实测：不 DROP 时把 DDL 退回 NOT NULL 也测不出）。
    c = db.cursor()
    try:
        c.execute("DROP TABLE IF EXISTS grape_t_webhook_inbox")
        db.commit()
    except Exception:  # noqa: BLE001 - 空库首跑属正常
        db.rollback()

    monkeypatch.setattr(wi, "db", db, raising=False)
    monkeypatch.setattr(wi, "db_lock", lock, raising=False)

    yield db

    try:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_webhook_inbox")
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    db.close()


def _read(db, sql, params=None):
    """读一行：MySQL 默认 REPEATABLE READ，读完必须 commit 才看得到别人写入。"""
    c = db.cursor()
    c.execute(sql, params or ())
    row = c.fetchone()
    db.commit()
    return row


def test_inbox_table_ddl_is_mysql_safe(compat_db):
    """建表 DDL 在 MySQL 上跑得通，且 last_error 可空（TEXT 不能带 DEFAULT ''）。"""
    from src.domains.runtime.webhook_inbox import ensure_webhook_inbox_table

    assert ensure_webhook_inbox_table() is True

    row = _read(
        compat_db,
        "SELECT IS_NULLABLE, DATA_TYPE FROM information_schema.columns "
        "WHERE TABLE_SCHEMA=? AND TABLE_NAME='grape_t_webhook_inbox' AND COLUMN_NAME='last_error'",
        (_dsn_parts(_DSN)["database"],),
    )
    assert row is not None, "last_error 列不存在"
    assert str(row[0]).upper() == "YES"
    assert str(row[1]).lower() in ("text", "mediumtext", "longtext")


def test_enqueue_is_idempotent_and_redacts_headers(compat_db):
    """入队落库：同投递第二次幂等复用同一行，敏感验签头不落库、正文保留。"""
    from src.domains.runtime.webhook_inbox import enqueue_webhook, ensure_webhook_inbox_table

    ensure_webhook_inbox_table()
    first = enqueue_webhook(
        repo_key="gitlab-corp:grp/proj", provider="gitlab-corp",
        event_header="Merge Request Hook", body=b'{"object_kind":"merge_request"}',
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s3cret"},
        delivery_id="delivery-1364",
    )
    assert first["duplicate"] is False
    assert first["status"] == "pending"

    dup = enqueue_webhook(
        repo_key="gitlab-corp:grp/proj", provider="gitlab-corp",
        event_header="Merge Request Hook", body=b'{"object_kind":"merge_request"}',
        headers={}, delivery_id="delivery-1364",
    )
    assert dup["duplicate"] is True and dup["id"] == first["id"]

    # 敏感验签头不落库，正文/事件类型保留
    headers = _read(
        compat_db, "SELECT headers FROM grape_t_webhook_inbox WHERE id=?", (first["id"],)
    )[0]
    assert "s3cret" not in str(headers)
    payload = _read(
        compat_db, "SELECT payload FROM grape_t_webhook_inbox WHERE id=?", (first["id"],)
    )[0]
    assert "merge_request" in str(payload)


def test_strict_mode_reproduces_legacy_1364(compat_db):
    """反证：本用例跑在真正严格的 MySQL 上——把列退回旧的 NOT NULL 形态后，
    任何省略 last_error 的 INSERT 必然 1364。所以上面几条绿不是“库太宽松”。"""
    import pymysql

    from src.domains.runtime.schema_ddl import _WEBHOOK_INBOX_DDL

    c = compat_db.cursor()
    c.execute("DROP TABLE IF EXISTS grape_t_webhook_inbox")
    c.execute(_WEBHOOK_INBOX_DDL)
    compat_db.commit()
    # 模拟兼容层剥掉 TEXT 字面默认值后的存量库形态
    c.execute("ALTER TABLE grape_t_webhook_inbox MODIFY last_error MEDIUMTEXT NOT NULL")
    compat_db.commit()

    legacy_insert = (
        "INSERT INTO grape_t_webhook_inbox "
        "(repo_key, provider, delivery_id, event_header, payload, headers, status, "
        " attempts, max_attempts, next_attempt_at, created_at, updated_at) "
        "VALUES ('gitlab-corp:g/p','gitlab-corp','d-legacy','Push Hook','{}','{}',"
        "'pending',0,5,'','','')"
    )
    with pytest.raises(pymysql.err.OperationalError) as err:
        c.execute(legacy_insert)
        compat_db.commit()
    assert err.value.args[0] == 1364, f"期望 1364，实际 {err.value.args[0]}"
    compat_db.rollback()

    # 迁移修法（MODIFY ... NULL）之后同样的 INSERT 必须成功
    c.execute("ALTER TABLE grape_t_webhook_inbox MODIFY last_error MEDIUMTEXT NULL")
    compat_db.commit()
    c.execute(legacy_insert)
    compat_db.commit()
    row = _read(
        compat_db,
        "SELECT IS_NULLABLE FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='grape_t_webhook_inbox' AND COLUMN_NAME='last_error'",
    )
    assert str(row[0]).upper() == "YES"


def test_claim_and_finish_visible_with_repeatable_read(compat_db):
    """claim → finish(ok) 后，另一条连接（新事务）能读到 consumed，不会卡在旧快照。"""
    from src.domains.runtime.webhook_inbox import (
        claim_next, enqueue_webhook, ensure_webhook_inbox_table, finish_claim,
    )

    ensure_webhook_inbox_table()
    item = enqueue_webhook(
        repo_key="gitlab-corp:grp/proj2", provider="gitlab-corp",
        event_header="Push Hook", body=b'{"ref":"main"}', headers={}, delivery_id="d-ok",
    )
    claimed = claim_next("owner-ci", lease_seconds=60)
    assert claimed and claimed["id"] == item["id"]

    assert finish_claim(claimed["id"], "owner-ci", ok=True)["status"] == "consumed"
    row = _read(
        compat_db,
        "SELECT status, claim_owner FROM grape_t_webhook_inbox WHERE id=?",
        (item["id"],),
    )
    assert row[0] == "consumed" and row[1] == ""


def test_failure_backoff_and_replay(compat_db):
    """失败按指数退避写 next_attempt_at，退避窗内不再被抢，到期后可重放。"""
    from src.domains.runtime.webhook_inbox import (
        claim_next, enqueue_webhook, ensure_webhook_inbox_table, finish_claim,
    )

    ensure_webhook_inbox_table()
    item = enqueue_webhook(
        repo_key="gitlab-corp:grp/proj3", provider="gitlab-corp",
        event_header="Pipeline Hook", body=b'{"status":"failed"}', headers={},
        delivery_id="d-fail",
    )
    claimed = claim_next("owner-ci")
    assert claimed and claimed["id"] == item["id"]

    out = finish_claim(claimed["id"], "owner-ci", ok=False, error="upstream 502")
    assert out["status"] == "failed" and out["attempts"] == 1

    row = _read(
        compat_db,
        "SELECT status, attempts, last_error, next_attempt_at FROM grape_t_webhook_inbox WHERE id=?",
        (item["id"],),
    )
    assert row[0] == "failed" and int(row[1]) == 1
    assert row[2] == "upstream 502"          # MySQL 下 TEXT 列能存非空错误
    assert row[3], "退避时间必须写进 next_attempt_at，否则立即重放打爆上游"

    # 退避窗内不可被抢；把 next_attempt_at 拨到过去后立即可重放
    assert claim_next("owner-ci") is None
    c = compat_db.cursor()
    c.execute(
        "UPDATE grape_t_webhook_inbox SET next_attempt_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", item["id"]),
    )
    compat_db.commit()
    again = claim_next("owner-ci")
    assert again and again["id"] == item["id"]
    assert int(again["attempts"]) == 1


def test_last_error_accepts_null(compat_db):
    """列声明 DEFAULT NULL：显式写 NULL 也要能读回（迁移把 NOT NULL 放开后的契约）。"""
    from src.domains.runtime.webhook_inbox import enqueue_webhook, ensure_webhook_inbox_table

    ensure_webhook_inbox_table()
    item = enqueue_webhook(
        repo_key="gitlab-corp:grp/proj4", provider="gitlab-corp",
        event_header="Note Hook", body=b'{"object_attributes":{}}', headers={},
        delivery_id="d-null",
    )
    c = compat_db.cursor()
    c.execute("UPDATE grape_t_webhook_inbox SET last_error=NULL WHERE id=?", (item["id"],))
    compat_db.commit()
    row = _read(
        compat_db, "SELECT last_error FROM grape_t_webhook_inbox WHERE id=?", (item["id"],)
    )
    assert row[0] is None