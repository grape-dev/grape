"""Issue #3738：set_meta_value_any 必须真正幂等（不依赖 UPDATE rowcount 判断行存在）。

根因：MySQL 默认连接（未开 CLIENT_FOUND_ROWS）下同值 UPDATE 的 rowcount=0，
旧实现误判「行不存在」→ INSERT 撞 meta.PRIMARY 1062，被调用方吞成
`set_meta_value failed` WARNING，写入静默失败（生产 12 条/小时）。

冲突合并口径（2026-11-24，arch-weekly-11-ljt 已独立修复同一缺陷）：
目标分支 ac92dc382 已把该写路径收敛为**单语句** upsert
（`meta_upsert` = INSERT ... ON CONFLICT(`key`) DO UPDATE，MySQL 侧由
mysql_compat 翻译成 ON DUPLICATE KEY UPDATE），与本 PR 原「insert-absent →
update」两段式等价但更短、更原子。合并时保留目标分支实现，回归用例改锁
**行为契约**（不再锁 SQL 条数形状），避免把已合入的更优实现改回去。

覆盖（测试 seam = 公开接口 set_meta_value_any / set_meta_values_any /
get_meta_value_any）：
1. MySQL rowcount 语义模拟（假连接，db_registry seam）：行已存在 + 同值写入
   不抛 1062，且值保持不变（对旧两段式实现变红）。
2. 护栏：写路径恒为一条 upsert 语句，绝不出现「先 UPDATE 再按 rowcount 决定
   INSERT」的两段式序列（那条不带冲突处理的 INSERT 正是撞 1062 的语句）。
3. sqlite 实跑（进程库回退路径）：同值幂等连写、值变更、并发首写、批量版
   set_meta_values_any 既有行为不变。
"""
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.kernel import db_registry
from src.kernel.session_manager import db, db_lock
from src.domains.runtime import public as rtp

KEY_SAME = "test_3738_same_value_key"
KEY_CONC = "test_3738_concurrent_key"
BATCH_KEYS = ("test_3738_batch_a", "test_3738_batch_b")


def _cleanup(keys):
    with db_lock:
        c = db.cursor()
        for k in keys:
            c.execute("DELETE FROM meta WHERE key=?", (k,))
        db.commit()


class _MySQLSemanticsCursor:
    """模拟 MySQL 默认 rowcount 语义的假游标（内存 meta 表）。

    关键差异（正是 #3738 根因）：UPDATE 只在值**真正变化**时 rowcount=1，
    同值 UPDATE rowcount=0——与「行不存在」不可区分；不带冲突处理（无
    ON CONFLICT / ON DUPLICATE KEY / IGNORE）的 INSERT 撞主键抛 1062
    （此处用 sqlite3.IntegrityError 等价模拟）。
    """

    def __init__(self, state, log):
        self._state = state
        self._log = log
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._log.append(sql)
        vals = list(params or ())
        s = " ".join(sql.split()).upper()
        if s.startswith("INSERT") and ("ON CONFLICT" in s or "ON DUPLICATE KEY" in s
                                       or "IGNORE" in s):
            # upsert 语义：行存在则原地改值，不存在则补行；任何情况都不抛 1062
            key, value = vals[0], vals[1]
            exists = key in self._state
            if exists and self._state[key] != value:
                self.rowcount = 2  # MySQL：更新计 2 行
            elif exists:
                self.rowcount = 0  # MySQL：值未变计 0 行（调用方不得据此判存在性）
            else:
                self.rowcount = 1
            self._state[key] = value
        elif s.startswith("INSERT"):
            key, value = vals[0], vals[1]
            if key in self._state:
                raise sqlite3.IntegrityError(
                    f"(1062) Duplicate entry '{key}' for key 'meta.PRIMARY'")
            self._state[key] = value
            self.rowcount = 1
        elif s.startswith("UPDATE"):
            value, key = vals[0], vals[1]
            if key in self._state and self._state[key] != value:
                self._state[key] = value
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:
            self.rowcount = 0
        return self

    def fetchone(self):
        return None

    def close(self):
        return None


@pytest.fixture
def mysql_semantics(monkeypatch):
    """三库拓扑模拟：runtime 域走自有 DSN（假连接），SQL 记录在案。

    同时替换 `cursor`（单语句写路径 `_any_cursor`）与 `transaction`（多语句写路径
    `_any_tx`）两个 seam：这样无论实现回到哪种写法，都被同一套 MySQL rowcount
    语义与语句序列护栏覆盖，护栏不会因写法切换而静默失效。
    """
    state = {}
    log = []

    @contextmanager
    def fake_cursor(domain):
        assert domain == "runtime"
        yield _MySQLSemanticsCursor(state, log)

    @contextmanager
    def fake_tx(domain):
        assert domain == "runtime"
        yield _MySQLSemanticsCursor(state, log)

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: d == "runtime")
    monkeypatch.setattr(db_registry, "cursor", fake_cursor)
    monkeypatch.setattr(db_registry, "transaction", fake_tx)
    return state, log


def test_same_value_rewrite_no_1062_under_mysql_rowcount(mysql_semantics):
    """验收 1：行已存在 + 同值重写，在 MySQL rowcount 语义下不抛 1062。"""
    state, _ = mysql_semantics
    state[KEY_SAME] = "v"
    # 旧两段式实现：UPDATE rowcount=0 → 误判行不存在 → INSERT 撞主键（本用例对其变红）
    rtp.set_meta_value_any(KEY_SAME, "v")
    assert state[KEY_SAME] == "v"


def test_value_change_and_absent_insert(mysql_semantics):
    """顺带覆盖：行存在改值 / 行不存在首写，两条路径都落在同一 upsert 原语上。"""
    state, _ = mysql_semantics
    state[KEY_SAME] = "old"
    rtp.set_meta_value_any(KEY_SAME, "new")
    assert state[KEY_SAME] == "new"
    rtp.set_meta_value_any("test_3738_absent_key", "first")
    assert state["test_3738_absent_key"] == "first"


def test_write_path_is_single_statement_upsert(mysql_semantics):
    """验收 3（护栏）：一次写入 = 一条 upsert 语句，与 rowcount 彻底解耦。

    旧实现「UPDATE → rowcount=0 → INSERT」在本用例下必然暴露：执行序列里会出现
    UPDATE 或一条不带冲突处理的 INSERT（那条正是撞 1062 的语句）。
    """
    state, log = mysql_semantics
    writes = ((KEY_SAME, "v"), (KEY_SAME, "v"), (KEY_SAME, "v2"),
              ("test_3738_seq_new", "n1"), ("test_3738_seq_new", "n1"))
    for key, value in writes:
        rtp.set_meta_value_any(key, value)

    assert state[KEY_SAME] == "v2"
    assert state["test_3738_seq_new"] == "n1"
    assert len(log) == len(writes), f"每次写入应为一条语句，实际 {log!r}"
    for sql in log:
        up = " ".join(sql.split()).upper()
        assert up.startswith("INSERT"), f"写路径应只剩 upsert INSERT: {sql!r}"
        assert any(tok in up for tok in ("ON CONFLICT", "ON DUPLICATE KEY", "IGNORE")), (
            f"出现了不带冲突处理的 INSERT（撞 1062 的语句）: {sql!r}")
        assert "ROWCOUNT" not in up
    # 不再出现「先 UPDATE 再决定是否 INSERT」的两段式序列
    assert not any(s.strip().upper().startswith("UPDATE") for s in log)


def test_sqlite_same_value_idempotent():
    """验收 1（sqlite 实跑）：同 key 同值连写两次均成功且终值正确。"""
    _cleanup([KEY_SAME])
    try:
        rtp.set_meta_value_any(KEY_SAME, "v")
        rtp.set_meta_value_any(KEY_SAME, "v")  # 旧行为在 MySQL 下即此步 1062
        assert rtp.get_meta_value_any(KEY_SAME) == "v"
        rtp.set_meta_value_any(KEY_SAME, "v2")
        assert rtp.get_meta_value_any(KEY_SAME) == "v2"
    finally:
        _cleanup([KEY_SAME])


def test_sqlite_concurrent_first_write():
    """验收 2（sqlite 实跑）：两线程并发首写同一 key，均成功、无 1062、终值合法。"""
    _cleanup([KEY_CONC])
    errors = []
    barrier = threading.Barrier(2)

    def _write(v):
        try:
            barrier.wait(timeout=10)
            rtp.set_meta_value_any(KEY_CONC, v)
        except Exception as e:  # noqa: BLE001 - 收集并发写异常供断言
            errors.append(e)

    t1 = threading.Thread(target=_write, args=("vA",))
    t2 = threading.Thread(target=_write, args=("vB",))
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    try:
        assert errors == [], f"并发首写出现异常: {errors}"
        final = rtp.get_meta_value_any(KEY_CONC)
        assert final in ("vA", "vB"), f"终值丢失: {final!r}"
        # 后写者语义：并发收敛后再写一次，终值必须等于后写者
        rtp.set_meta_value_any(KEY_CONC, "final")
        assert rtp.get_meta_value_any(KEY_CONC) == "final"
    finally:
        _cleanup([KEY_CONC])


def test_batch_version_behavior_unchanged():
    """验收 4：批量版 set_meta_values_any 既有行为保持不变。"""
    _cleanup(BATCH_KEYS)
    try:
        n = rtp.set_meta_values_any([(BATCH_KEYS[0], "a"), (BATCH_KEYS[1], "b")])
        assert n == 2
        assert rtp.get_meta_value_any(BATCH_KEYS[0]) == "a"
        # 同值重写（批量版既有幂等语义，参照实现）
        rtp.set_meta_values_any([(BATCH_KEYS[0], "a"), (BATCH_KEYS[1], "c")])
        assert rtp.get_meta_value_any(BATCH_KEYS[0]) == "a"
        assert rtp.get_meta_value_any(BATCH_KEYS[1]) == "c"
    finally:
        _cleanup(BATCH_KEYS)
