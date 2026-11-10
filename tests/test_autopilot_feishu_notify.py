"""Autopilot Feishu subscription notification tests (pure logic, no network IO).

Covers the terminal-state notify pipeline introduced by the
autopilot-feishu-subscription design:
- parse_findings: `result` field passthrough + trailing-garbage stripping
- _autopilot_result_level / build_exec_card: level mapping + card v2 shape
- _notify_subscribers: idempotency guard + subscriber fanout (mocked outbound)

The scheduler module is loaded lazily inside a session-scoped fixture so the
conftest `_ensure_test_db` fixture can initialize the real (SQLite) test DB
first; the notify tests then swap in an isolated in-memory DB.
"""
import asyncio
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import types
import threading

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SRC_DIR)

_log = logging.getLogger("test_autopilot_notify")

_SCHED = None


@pytest.fixture(scope="module")
def sched():
    """Load scheduler.py standalone with stubbed config/session_manager.

    Real src.session_manager stays intact for other tests (conftest already
    initialized it); we install our stubs only around the standalone load.
    """
    global _SCHED
    if _SCHED is not None:
        yield _SCHED
        return

    saved = {k: sys.modules.get(k) for k in ("src.config", "src.session_manager")}
    cfg = types.ModuleType("src.config")
    cfg.log = _log
    src_pkg = sys.modules.get("src") or types.ModuleType("src")
    if not hasattr(src_pkg, "__path__"):
        src_pkg.__path__ = [os.path.join(SRC_DIR, "src")]
    sys.modules["src"] = src_pkg
    sys.modules["src.config"] = cfg

    sm = types.ModuleType("src.session_manager")
    sm.db = None
    sm.db_lock = threading.Lock()
    sys.modules["src.session_manager"] = sm

    try:
        spec = importlib.util.spec_from_file_location(
            "sched_notify_test", os.path.join(SRC_DIR, "src", "runtime", "scheduler.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SCHED = mod
        yield mod
    finally:
        # restore real modules for the rest of the test session
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v


# ===== parse_findings =====

def test_parse_findings_result_field(sched):
    out = ('<!-- autopilot-findings\n'
           '{"result":"warning","summary":"s1","findings":[{"id":"f1","title":"t","description":"d",'
           '"severity":"high","category":"quality"}]}\n-->')
    r = sched.parse_findings(out)
    assert r["result"] == "warning"
    assert r["findings"][0]["id"] == "f1"


def test_parse_findings_result_backcompat(sched):
    out = '<!-- autopilot-findings\n{"summary":"s2","findings":[]}\n-->'
    r = sched.parse_findings(out)
    assert r["result"] == ""


def test_parse_findings_trailing_garbage(sched):
    out = ('<!-- autopilot-findings\n'
           '{"result":"ok","summary":"s3","findings":[]}--\n-->')
    r = sched.parse_findings(out)
    assert r["result"] == "ok"
    assert r["summary"] == "s3"


def test_strip_trailing_garbage(sched):
    assert sched._strip_trailing_garbage('{"a":1} --') == '{"a":1}'
    assert sched._strip_trailing_garbage('{"a":1}\n\t ') == '{"a":1}'
    assert sched._strip_trailing_garbage('{"a":1}') == '{"a":1}'


# ===== result level + card =====

def test_result_level(sched):
    assert sched._autopilot_result_level("failed", {}) == "error"
    assert sched._autopilot_result_level("cancelled", {}) == "error"
    assert sched._autopilot_result_level("success", {"result": "ok"}) == "ok"
    assert sched._autopilot_result_level("success", {"result": "bogus"}) == ""
    assert sched._autopilot_result_level("success", {}) == ""


def test_build_exec_card_v2(sched):
    findings = {"result": "warning",
                "findings": [{"id": "f1", "title": "t1", "severity": "high", "category": "sec"}]}
    card = sched.build_exec_card("T1", "repo/x", 1, "success", "conclusion", findings, "http://w")
    assert card["card_v2"]["schema"] == "2.0"
    assert card["template"] == "yellow"
    buttons = [e for e in card["card_v2"]["body"]["elements"] if e.get("tag") == "button"]
    assert buttons and buttons[0]["behaviors"][0]["value"]["cmd"] == "/finding-list"
    assert buttons[0]["behaviors"][0]["value"]["exec_id"] == 1
    json.dumps(card)  # serializable


def test_build_exec_card_empty_findings_no_button(sched):
    card = sched.build_exec_card("T", "r", 2, "success", "s", {"result": "ok", "findings": []}, "")
    buttons = [e for e in card["card_v2"]["body"]["elements"] if e.get("tag") == "button"]
    assert not buttons


def test_build_exec_card_failed_is_error(sched):
    card = sched.build_exec_card("T", "r", 3, "failed", "", {}, "")
    assert card["template"] == "red"


# ===== _notify_subscribers (mocked outbound) =====

@pytest.fixture()
def notify_env(sched):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE autopilot_executions (id INTEGER PRIMARY KEY, task_id INT, "
                 "status TEXT, notified INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE autopilot_subscriptions (id INTEGER PRIMARY KEY, task_id INT, user_id INT)")
    conn.execute("CREATE TABLE autopilot_tasks (id INTEGER PRIMARY KEY, name TEXT, repo_key TEXT)")
    conn.execute("INSERT INTO autopilot_executions (id, task_id) VALUES (7, 42)")
    conn.execute("INSERT INTO autopilot_subscriptions (task_id, user_id) VALUES (42, 8)")
    conn.execute("INSERT INTO autopilot_subscriptions (task_id, user_id) VALUES (42, 9)")
    conn.execute("INSERT INTO autopilot_tasks (id, name, repo_key) VALUES (42, 'T42', 'gitcode:demo/repo')")
    conn.commit()

    sm = sys.modules["src.session_manager"]
    old_db, old_lock = sm.db, sm.db_lock
    sm.db = conn
    sm.db_lock = threading.Lock()

    sent = []

    async def fake_send(ctx, api, user_id, open_id, card, task_id, exec_id):
        sent.append(user_id)

    old_send = sched._run_autopilot_notify
    sched._run_autopilot_notify = fake_send

    class _BM:
        api = "apiX"
    # 用 sys.modules 打桩三个依赖模块 —— 必须保存并在 teardown 原样还原，
    # 否则后续测试文件会拿到缺属性的假模块（曾导致 test_table* 大面积 ImportError）。
    _stub_names = ("src.api.main", "src.api.issues_center", "src.prompt_builder")
    _saved_mods = {n: sys.modules.get(n) for n in _stub_names}
    fm = types.ModuleType("src.api.main")
    fm.bot_manager = _BM()
    sys.modules["src.api.main"] = fm
    ic = types.ModuleType("src.api.issues_center")
    ic._get_feishu_open_id_by_user_id = lambda uid: "ou-X"
    # A2 M2 后的契约：返回 BotContext（有 app_id），api 由调用方按 app_id 取共享客户端
    ic._resolve_user_chat_bot = lambda bm, uid: types.SimpleNamespace(app_id="appX")
    sys.modules["src.api.issues_center"] = ic
    pb = types.ModuleType("src.prompt_builder")
    pb._resolve_server_url = lambda rid=0: "http://web"
    sys.modules["src.prompt_builder"] = pb

    # scheduler module captured db/db_lock at import time — patch them too
    old_sdb, old_slock = sched.db, sched.db_lock
    sched.db = conn
    sched.db_lock = sm.db_lock
    try:
        yield {"sent": sent}
    finally:
        sched.db, sched.db_lock = old_sdb, old_slock
        sched._run_autopilot_notify = old_send
        sm.db, sm.db_lock = old_db, old_lock
        for _n in _stub_names:
            if _saved_mods[_n] is None:
                sys.modules.pop(_n, None)
            else:
                sys.modules[_n] = _saved_mods[_n]
        conn.close()


def test_notify_subscribers_fanout_and_idempotency(sched, notify_env):
    sent = notify_env["sent"]
    # 保存并还原"当前事件循环"：本用例自建 loop 后若不还原，后续用
    # asyncio.get_event_loop() 的测试会抛 RuntimeError: no current event loop。
    try:
        _prev_loop = asyncio.get_event_loop()
    except RuntimeError:
        _prev_loop = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        sched._notify_subscribers(42, 7, "success", "done", {"result": "ok", "findings": []})
        loop.run_until_complete(asyncio.sleep(0.1))
        assert sorted(sent) == [8, 9]

        # second terminal hook must not double-send (notified=1 guard)
        sched._notify_subscribers(42, 7, "success", "again", {})
        loop.run_until_complete(asyncio.sleep(0.1))
        assert sorted(sent) == [8, 9]
    finally:
        loop.close()
        asyncio.set_event_loop(_prev_loop)


def test_exec_task_id_lookup(sched, notify_env):
    assert sched._exec_task_id(7) == 42
    assert sched._exec_task_id(999) == 0
