"""Issue #4262 / PR #2157 门禁复审 BLOCKED：迁移 202611240011 三态判据（假绿修复）。

事故（复审 Required）
--------------------
`ensure_state_column_defaults` 原判据 ``str(current.get(col) or "") == ""`` 把
information_schema 的 ``COLUMN_DEFAULT`` 的 **NULL**（= 列无默认值，待修）与 **''**
（= 已修）压成同一态：MySQL 存量库（#3733 首版 TEXT 落地成 ``NOT NULL DEFAULT NULL``）
与新装库（CREATE TABLE 走 mysql_compat 的表达式默认值）上一条 MODIFY 都不发，
迁移 202611240011 因约定「fn 正常返回 = 盖章」被盖死（假绿），strict 模式下不显式
给值的 INSERT 依旧 1364；post_check 用的是同一个糊掉的判据，也跟着放行。

覆盖（三态分别驱动 ensure 与 post_check）
------------------------------------------
1. ``''``  → ensure 零写（唯一幂等态）、post_check 通过；
2. ``NULL`` → ensure 必发 MODIFY、post_check 必失败（假绿回归锁）；
3. 缺列 → ensure raise 且一条 DDL 都不发（本迁移改不了缺列，交 202611240007 重试）、
   post_check 必失败；
4. 表达式默认值（MySQL ``TEXT DEFAULT ('')`` 的落地形态）→ 同样视同未收敛；
5. 混合态只修未收敛列（不许把 NULL 当已修）。
"""
import sqlite3

import pytest

from src.domains.mgmt.migrations import autopilot_dispatch_state as mig

MIG_VERSION = "202611240011"
MIG_NAME = "autopilot-state-column-defaults"
COLS = tuple(col for col, _ddl in mig.STATE_COLUMNS)
DDL_BY_COL = dict(mig.STATE_COLUMNS)


class _ModifyRecorder:
    """``_destructive_ddl`` 替身：记录发出的 MODIFY（SQLite 没有 MODIFY COLUMN）。

    ``accepted=False`` 模拟 min_code_version 门槛拦截（返回 False = 跳过、不执行）。
    """

    def __init__(self, accepted: bool = True):
        self.accepted = accepted
        self.ddls = []

    def __call__(self, cursor, ddl, *, kind_hint=""):
        self.ddls.append((ddl, kind_hint))
        return self.accepted


@pytest.fixture
def mysql_info(monkeypatch):
    """假 MySQL 后端 + 可控的 information_schema 读数 + MODIFY 替身。"""

    def _setup(states: dict, *, accepted: bool = True) -> _ModifyRecorder:
        _patch_mysql_backend(monkeypatch, True)
        monkeypatch.setattr(mig, "_state_column_defaults", lambda _conn: dict(states),
                           raising=False)
        rec = _ModifyRecorder(accepted)
        monkeypatch.setattr(_db_setup(), "_destructive_ddl", rec, raising=False)
        return rec

    return _setup


def _conn():
    return sqlite3.connect(":memory:")


def _module(name: str):
    """按 sys.modules 现存对象取模块。

    不用 `monkeypatch.setattr("src.kernel.session_manager.X", ...)` 的字符串形态：
    个别用例（tests/mgmt/test_autopilot_feishu_notify.py 的独立加载）会替换
    `sys.modules["src.kernel.session_manager"]`，其后 `src.kernel` 上可能不再挂着
    该属性，字符串解析直接 AttributeError；被测函数走的是 `from ... import`，即
    sys.modules 里的那个对象，用 importlib 取到的才是同一份。
    """
    import importlib

    return importlib.import_module(name)


def _patch_mysql_backend(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(_module("src.kernel.session_manager"),
                        "_is_mysql_backend", lambda: value)


def _db_setup():
    return _module("src.api.db_setup")


# ===== 0. 判据本身 =====


def test_classify_column_default_is_three_state():
    """假绿回归锁：NULL（待修）与 ''（已修）必须是两个不同态。"""
    assert mig.classify_column_default({}, "dispatch_state") == mig.DEFAULT_MISSING
    assert mig.classify_column_default(
        {"dispatch_state": None}, "dispatch_state") == mig.DEFAULT_NULL
    assert mig.classify_column_default(
        {"dispatch_state": ""}, "dispatch_state") == mig.DEFAULT_OK
    assert mig.classify_column_default(
        {"dispatch_state": "_utf8mb4''"}, "dispatch_state") == mig.DEFAULT_OTHER
    assert mig.DEFAULT_NULL != mig.DEFAULT_OK
    assert mig.DEFAULT_MISSING != mig.DEFAULT_OK


# ===== 1. 已收敛（''）：零写、post_check 通过 =====


def test_ensure_is_zero_write_when_defaults_are_empty_string(mysql_info):
    rec = mysql_info({col: "" for col in COLS})
    assert mig.ensure_state_column_defaults(_conn()) == 0
    assert rec.ddls == [], "已收敛为空串不得再发 MODIFY（幂等）"


def test_post_check_passes_when_defaults_are_empty_string(mysql_info):
    mysql_info({col: "" for col in COLS})
    mig.state_column_defaults_converged(_conn())  # 不抛 = 通过


# ===== 2. NULL（待修）：ensure 必发 MODIFY、post_check 必失败 =====


def test_ensure_modifies_every_column_without_default(mysql_info):
    rec = mysql_info({col: None for col in COLS})
    fixed = mig.ensure_state_column_defaults(_conn())

    assert fixed == 3, "COLUMN_DEFAULT IS NULL 是待修态，三列都要 MODIFY"
    assert [ddl for ddl, _kind in rec.ddls] == [
        f"ALTER TABLE {mig.TABLE} MODIFY COLUMN {col} {DDL_BY_COL[col]}" for col in COLS]
    assert {kind for _ddl, kind in rec.ddls} == {"cleanup"}, "危险 DDL 必须走 cleanup 语义"


def test_post_check_fails_for_null_default(mysql_info):
    mysql_info({col: None for col in COLS})
    with pytest.raises(RuntimeError) as ei:
        mig.state_column_defaults_converged(_conn())
    assert "dispatch_state" in str(ei.value)
    assert "not stamped" in str(ei.value), "post_check 不收敛必须拦住盖章"


# ===== 3. 缺列：ensure raise 且零 DDL、post_check 失败 =====


def test_ensure_raises_and_writes_nothing_when_column_missing(mysql_info):
    rec = mysql_info({col: "" for col in COLS if col != "dispatch_state_at"})
    with pytest.raises(RuntimeError) as ei:
        mig.ensure_state_column_defaults(_conn())

    message = str(ei.value)
    assert "missing" in message and "dispatch_state_at" in message
    assert "202611240007" in message, "缺列只能由补列迁移修，需在报错里指明来源"
    assert rec.ddls == [], "缺列时不得对现存列半途改定义（会留下未收敛的半态）"


def test_post_check_fails_for_missing_column(mysql_info):
    mysql_info({col: "" for col in COLS if col != "dispatch_state_at"})
    with pytest.raises(RuntimeError) as ei:
        mig.state_column_defaults_converged(_conn())
    assert "missing" in str(ei.value)
    assert "dispatch_state_at" in str(ei.value)


# ===== 4. 表达式默认值 / 混合态 =====


def test_ensure_repairs_expression_default(mysql_info):
    """MySQL `TEXT NOT NULL DEFAULT ('')` 落地后 COLUMN_DEFAULT 读到表达式文本。"""
    rec = mysql_info({col: "_utf8mb4''" for col in COLS})
    assert mig.ensure_state_column_defaults(_conn()) == 3
    assert len(rec.ddls) == 3


def test_post_check_fails_for_expression_default(mysql_info):
    mysql_info({col: "_utf8mb4''" for col in COLS})
    with pytest.raises(RuntimeError):
        mig.state_column_defaults_converged(_conn())


def test_ensure_only_repairs_unconverged_columns(mysql_info):
    rec = mysql_info({"dispatch_state": None, "dispatch_state_reason": "",
                      "dispatch_state_at": ""})
    fixed = mig.ensure_state_column_defaults(_conn())

    assert fixed == 1, "混合态只修 NULL 那一列（把 NULL 当已修就是假绿）"
    assert [ddl for ddl, _kind in rec.ddls] == [
        f"ALTER TABLE {mig.TABLE} MODIFY COLUMN dispatch_state {DDL_BY_COL['dispatch_state']}"]


# ===== 5. 门槛拦截 / SQLite / 注册 =====


def test_ensure_raises_when_cleanup_gate_blocks(mysql_info):
    mysql_info({col: None for col in COLS}, accepted=False)
    with pytest.raises(RuntimeError) as ei:
        mig.ensure_state_column_defaults(_conn())
    assert "cleanup gate" in str(ei.value)


def test_sqlite_backend_is_noop(monkeypatch):
    _patch_mysql_backend(monkeypatch, False)
    assert mig.ensure_state_column_defaults(_conn()) == 0
    mig.state_column_defaults_converged(_conn())  # SQLite 列定义自带 DEFAULT ''


def test_migration_registered_as_mgmt_cleanup_with_post_check():
    ds = _db_setup()

    mers = [m for m in ds._MIGRATIONS if m.version == MIG_VERSION]
    assert len(mers) == 1, f"{MIG_VERSION} 应唯一注册，实际 {[m.name for m in mers]}"
    mer = mers[0]
    assert mer.name == MIG_NAME
    assert mer.kind == "cleanup", "MODIFY COLUMN 属危险 DDL，必须 cleanup + 门槛"
    assert tuple(mer.domains) == ("mgmt",), "autopilot_tasks 归 mgmt 域"
    assert callable(mer.fn)
    assert callable(mer.post_check), "默认值可判定，必须带 post_check（Issue #3648）"