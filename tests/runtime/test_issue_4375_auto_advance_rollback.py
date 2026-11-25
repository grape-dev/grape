"""Issue #4375：auto-advance 回滚读 events 落到 mgmt 库恒 1146 —— 改走 runtime 域只读口。

背景（生产实测）：`grape-mgmt` 每小时约 5 条
`Pipeline: auto-advance stage rollback failed ... Table 'grape_prod_mgmt.events' doesn't
exist`，异常被 `except` 吞成 WARNING，回滚永不执行 → 自动推进的下游 A2A 事件终态 failed
后 `issue.stage` 仍停在从未运行的节点。同源 `resolve_skill_display_name` 读 `skill` 表
同样落 mgmt 库（约 8 条/小时）并降级展示名。

根因：`runtime.public._any_cursor` 自带第二套判定（`uses_own_dsn("runtime") ? 域连接 :
进程主库`），三库拓扑缺 `GRAPE_DSN_RUNTIME` 时静默回落 mgmt 主库；回滚函数又直接用
`session_manager.db` 直查 `events`（归 runtime 域）。

本用例锁五件事：
1. 双库拓扑（进程库 = mgmt，无 events/skill 表）下回滚读走 runtime 域游标，stage 由
   `guard.to` 回退 `guard.from`，全程不触达进程主库；
2. 下游事件终态 failed 的端到端用例：`_fail_event` 之后 `issue.stage` 真的回退；
3. 四类短路（无 stage_guard / stage 已变 / payload 非法 / seq 非法）不回退、不抛异常；
4. 展示名解析在双库拓扑取到 display_name 且不降级（无 WARNING）；
5. 缺 `GRAPE_DSN_RUNTIME` 时显式 `DomainUnavailable`，回滚失败升 ERROR 并带
   `route/target_db/pid/host` 事实串，绝不回落 mgmt 库（源码棘轮 + 行为级哨兵）。
"""
import inspect
import json
import re
from pathlib import Path

import pytest

from src.domains.runtime import public as rt
from src.domains.runtime.pipeline import event_pipeline as ep
from src.kernel import db_registry
from src.kernel.session_manager import db, db_lock

_REPO = "gitcode:org/i4375"
_ISSUE_ID = "4375001"
_RUNTIME_DSN = "mysql://rollback_user:***@127.0.0.1:3306/grape_prod_runtime"
_PROCESS_DB = "grape_prod_mgmt"
_GUARD_PAYLOAD = json.dumps({"stage_guard": {"from": "coding", "to": "qa"}})


class _BombMain:
    """进程主库哨兵：任何触达即证明「回滚读落到了 mgmt 库」（本 Issue 根因）。"""

    def cursor(self, *a, **kw):  # pragma: no cover - 触达即失败
        raise AssertionError("回滚读触达进程主库（mgmt）—— Issue #4375 回归")

    def commit(self, *a, **kw):  # pragma: no cover
        raise AssertionError("回滚写触达进程主库（mgmt）—— Issue #4375 回归")

    def rollback(self, *a, **kw):  # pragma: no cover
        raise AssertionError("回滚触达进程主库（mgmt）—— Issue #4375 回归")


class _LookupCursor:
    """按 SQL 子串回答的域游标桩：记录执行过的语句，可按需注入异常。"""

    def __init__(self, lookup=None, exc=None):
        self.lookup = dict(lookup or {})
        self.exc = exc
        self.executed = []
        self.closed = False
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        if self.exc is not None:
            raise self.exc
        return self

    def _answer(self):
        sql = self.executed[-1][0]
        for key, value in self.lookup.items():
            if key in sql:
                return value
        return None

    def fetchone(self):
        return self._answer()

    def fetchall(self):
        answer = self._answer()
        return list(answer or [])

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, *a, **kw):
        return self._cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def three_db(monkeypatch):
    """真实三库拓扑：进程域 = mgmt（主库哨兵），runtime 域 DSN 可配/可不配。"""
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    monkeypatch.setenv("DEVOPS_DB_NAME", _PROCESS_DB)
    monkeypatch.delenv("GRAPE_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("GRAPE_DSN_MGMT", raising=False)
    monkeypatch.delenv("GRAPE_DSN_INSIGHT", raising=False)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _BombMain(), raising=False)
    return monkeypatch


def _install_runtime_domain(monkeypatch, cur):
    """三库拓扑：runtime 域有自有 DSN，域连接拿到注入的假游标。"""
    monkeypatch.setenv("GRAPE_DSN_RUNTIME", _RUNTIME_DSN)
    monkeypatch.setattr(db_registry, "domain_db",
                        lambda d: _FakeConn(cur) if d == "runtime" else None)
    return cur


@pytest.fixture
def stage_recorder(monkeypatch):
    """把 issue.stage 读写换成记录器（回滚语义验证，不触达任何库）。"""
    calls = {"get": [], "set": []}
    state = {"stage": "qa"}

    def _get(repo_key, issue_id):
        calls["get"].append((repo_key, issue_id))
        return {"stage": state["stage"]}

    def _set(repo_key, issue_id, stage, owner_agent, *a, **kw):
        calls["set"].append((repo_key, issue_id, stage, owner_agent))
        state["stage"] = stage

    monkeypatch.setattr("src.api.session_store.get_issue_stage", _get)
    monkeypatch.setattr("src.api.session_store.set_issue_stage", _set)
    return calls, state


# ── ① 双库拓扑：读走 runtime 域，stage 回退，不碰 mgmt ─────────────────────
def test_dual_db_rollback_reads_runtime_domain_and_restores_stage(three_db, stage_recorder):
    cur = _install_runtime_domain(three_db, _LookupCursor(
        {"FROM events WHERE seq=?": (_REPO, _ISSUE_ID, _GUARD_PAYLOAD)}))
    calls, state = stage_recorder

    ep._rollback_auto_advance_stage(52)

    # 读通道：runtime 域具名查询（列序照抄，LIMIT 1 按主键唯一）
    assert cur.executed, "回滚没有走 runtime 域游标"
    sql, args = cur.executed[0]
    assert sql == "SELECT repo_key, issue_id, payload FROM events WHERE seq=? LIMIT 1"
    assert args == (52,)
    # 回退动作：guard.to(qa) → guard.from(coding)
    assert calls["get"] == [(_REPO, _ISSUE_ID)]
    assert calls["set"] == [(_REPO, _ISSUE_ID, "coding", "coding")]
    assert state["stage"] == "coding"


def test_dual_db_rollback_does_not_touch_process_db(three_db, stage_recorder, caplog):
    """进程主库是哨兵（触达即抛）：回滚路径若仍回落 mgmt，set 不会被调用且日志有失败。"""
    _install_runtime_domain(three_db, _LookupCursor(
        {"FROM events WHERE seq=?": (_REPO, _ISSUE_ID, _GUARD_PAYLOAD)}))
    calls, _ = stage_recorder

    with caplog.at_level("ERROR"):
        ep._rollback_auto_advance_stage(53)

    assert calls["set"], "守卫未执行（疑似回落到 mgmt 主库）"
    assert "auto-advance stage rollback failed" not in caplog.text


# ── ② 下游事件终态 failed（端到端，真实行）：issue.stage 已回退 ─────────────
def test_failed_downstream_event_rolls_back_issue_stage():
    """单库/测试拓扑（真实 sqlite 行）：`_fail_event` 收尾必须把 stage 退回 guard.from。"""
    from src.api.session_store import get_issue_stage

    num = int(_ISSUE_ID)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, payload) "
            "VALUES (?,?,?,?,?,?)",
            (_REPO, _ISSUE_ID, "a2a_event", "qa", "running", _GUARD_PAYLOAD))
        seq = int(c.lastrowid)
        c.execute(
            "INSERT INTO issue (repo_key, issue_number, status, stage, owner_agent, "
            "user_id, assignee) VALUES (?,?,?,?,?,?,?)",
            (_REPO, num, "processing", "qa", "qa", 1, "tester"))
        db.commit()
    try:
        ep._fail_event(seq, "downstream dispatch failed")

        with db_lock:
            status = db.cursor().execute(
                "SELECT status FROM events WHERE seq=?", (seq,)).fetchone()[0]
            stage = db.cursor().execute(
                "SELECT stage FROM issue WHERE repo_key=? AND issue_number=?",
                (_REPO, num)).fetchone()[0]
        assert status == "failed"
        assert stage == "coding", "下游 failed 后 issue.stage 未从 guard.to 退回 guard.from"
        assert (get_issue_stage(_REPO, _ISSUE_ID) or {}).get("stage") == "coding"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM events WHERE seq=?", (seq,))
            db.cursor().execute(
                "DELETE FROM issue WHERE repo_key=? AND issue_number=?", (_REPO, num))
            db.commit()


# ── ③ 四类短路：不回退、不抛异常 ────────────────────────────────────────────
@pytest.mark.parametrize("seq", [0, None, "", "not-a-seq"])
def test_invalid_seq_short_circuits(three_db, stage_recorder, seq):
    cur = _install_runtime_domain(three_db, _LookupCursor())
    calls, _ = stage_recorder

    ep._rollback_auto_advance_stage(seq)

    assert cur.executed == [], "非法 seq 不应发起 events 读"
    assert calls["set"] == []


def test_missing_event_row_short_circuits(three_db, stage_recorder):
    cur = _install_runtime_domain(three_db, _LookupCursor())  # fetchone → None
    calls, _ = stage_recorder

    ep._rollback_auto_advance_stage(61)

    assert cur.executed and calls["set"] == []


@pytest.mark.parametrize("payload, label", [
    (json.dumps({"agent": "qa"}), "无 stage_guard"),
    ("not-json{", "payload 非法 JSON"),
    (json.dumps(["coding", "qa"]), "payload 非 dict"),
    ("", "payload 空"),
])
def test_bad_payload_short_circuits(three_db, stage_recorder, payload, label):
    cur = _install_runtime_domain(three_db, _LookupCursor(
        {"FROM events WHERE seq=?": (_REPO, _ISSUE_ID, payload)}))
    calls, _ = stage_recorder

    ep._rollback_auto_advance_stage(62)

    assert cur.executed, f"{label}: 应读到行"
    assert calls["set"] == [], f"{label}: 不应回退 stage"


def test_stage_already_moved_short_circuits(three_db, stage_recorder):
    _install_runtime_domain(three_db, _LookupCursor(
        {"FROM events WHERE seq=?": (_REPO, _ISSUE_ID, _GUARD_PAYLOAD)}))
    calls, state = stage_recorder
    state["stage"] = "sre"  # 已被其它路径收敛

    ep._rollback_auto_advance_stage(63)

    assert calls["get"], "应读取当前 stage 以做幂等判定"
    assert calls["set"] == [], "stage 已变化时不得覆盖"


def test_equal_from_to_short_circuits(three_db, stage_recorder):
    payload = json.dumps({"stage_guard": {"from": "qa", "to": "qa"}})
    _install_runtime_domain(three_db, _LookupCursor(
        {"FROM events WHERE seq=?": (_REPO, _ISSUE_ID, payload)}))
    calls, _ = stage_recorder

    ep._rollback_auto_advance_stage(64)

    assert calls["get"] == [] and calls["set"] == []


# ── ④ 展示名解析：双库返回 display_name，不降级 ─────────────────────────────
def test_resolve_skill_display_name_reads_runtime_domain_in_dual_db(three_db, caplog):
    from src.api.skill_display import resolve_skill_display_name

    cur = _install_runtime_domain(three_db, _LookupCursor({
        "FROM skill WHERE id=? AND is_active=1": ("VOD 助手",),
        "scope='official'": ("VOD 助手（官方）",),
    }))

    with caplog.at_level("WARNING"):
        by_id = resolve_skill_display_name("vod-cli", skill_id=7)
        official = resolve_skill_display_name("vod-cli")

    assert by_id == "VOD 助手", "按 id 解析未走 runtime 域（疑似回落 mgmt → 1146 降级）"
    assert official == "VOD 助手（官方）"
    assert all("FROM skill" in sql for sql, _ in cur.executed)
    assert "resolve_skill_display_name failed" not in caplog.text, (
        "展示名解析发生了降级（WARNING 兜底）")


# ── ⑤ 缺 DSN：显式 DomainUnavailable，ERROR 带事实串，不回落 mgmt ─────────────
def test_missing_runtime_dsn_raises_domain_unavailable(three_db):
    """三库拓扑缺 GRAPE_DSN_RUNTIME：公开读口显式失败，不静默打 mgmt 库。"""
    three_db.delenv("GRAPE_DSN_RUNTIME", raising=False)

    with pytest.raises(db_registry.DomainUnavailable) as ei:
        rt.event_routing_payload(1)

    assert ei.value.domain == "runtime"
    assert "GRAPE_DSN_RUNTIME" in str(ei.value)


def test_rollback_failure_logs_error_with_conn_facts(three_db, caplog):
    three_db.delenv("GRAPE_DSN_RUNTIME", raising=False)

    with caplog.at_level("ERROR"):
        ep._rollback_auto_advance_stage(77)  # 不抛

    text = caplog.text
    assert "auto-advance stage rollback failed seq=77" in text
    assert "route=unavailable" in text, f"连接事实缺失（无法与「旧进程未重启」分流）: {text}"
    assert "pid=" in text and "host=" in text
    # 脱敏硬约束：日志会被集中采集/转贴，不得出现账号或完整 DSN
    assert "rollback_user" not in text and "mysql://" not in text


# ── ⑥ 源码棘轮：SQL / 路由判定只留一份 ──────────────────────────────────────
def _code_only(fn) -> str:
    """只取函数**代码**（剥掉 docstring），避免「注释写了根因就红」。"""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    body = getattr(node, "body", [])
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        node.body = body[1:]
    return ast.unparse(tree)


def test_rollback_no_longer_reads_events_from_process_db():
    src = _code_only(ep._rollback_auto_advance_stage)
    assert "event_routing_payload" in src, "回滚必须走 runtime 域 public 只读口"
    assert "session_manager" not in src and "db_lock" not in src, (
        "回滚仍在用进程主库直查（Issue #4375 根因）")
    assert "FROM events" not in src, "events SQL 只应留在 runtime 域 public 一份"


def test_any_cursor_uses_kernel_route_of_only():
    """棘轮：`_any_cursor` 只认内核 `route_of`（经 cursor_for），不得再有回落判定。"""
    src = _code_only(rt._any_cursor)
    assert "cursor_for" in src
    assert "uses_own_dsn" not in src and "session_manager" not in src


_SRC = Path(ep.__file__).resolve().parents[3]  # …/src/domains/runtime/pipeline/event_pipeline.py → …/src
_ROUTING_READ_RE = re.compile(r"SELECT\s+repo_key,\s*issue_id,\s*payload\s+FROM\s+events", re.IGNORECASE)


def test_event_routing_read_has_single_definition_in_src():
    found = {}
    for path in _SRC.rglob("*.py"):
        n = len(_ROUTING_READ_RE.findall(path.read_text(encoding="utf-8")))
        if n:
            found[str(path.relative_to(_SRC))] = n
    assert found == {"domains/runtime/public/__init__.py": 1}, (
        "events 路由字段读取发生漂移（应收口到 runtime 域 public 唯一具名查询），"
        f"实际={found}")