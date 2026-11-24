"""Issue #4158：同名顶层定义覆盖导致 A4 全渠道通知扇出被"飞书专用旧版"顶掉。

事故形态：模块里整段复制粘贴出第二份 `_run_*_notify`（只走飞书卡片，`ctx is None` 即
`reason=no_ctx` 丢弃）。Python 后定义覆盖前定义，pending_review / awaiting_input / 任务分配
通知静默退回旧链路。

本用例锁定三件事：
1. 5 个文件顶层同名 def/class 计数为 0；
2. `ctx=None` 且仅非飞书渠道可达时，三族通知真的走 `notify_user` 扇出（sent_ok，绝不 skipped_no_ctx）；
3. UUID 账号 id（D105）不被 `int()` 短路，`_resolve_issue_notify_targets` 照常解析通知目标。
"""
from __future__ import annotations

import ast
import asyncio
import collections
import contextlib
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("DEVOPS_DB_PATH", str(ROOT / "data" / "test_sessions.db"))

UUID_UID = "2ec8bad0-8ea1-4f71-9416-e508dc36f97e"

_DEDUP_TARGET_FILES = [
    ROOT / "src/domains/runtime/issues_center.py",
    ROOT / "src/domains/runtime/public/__init__.py",
    ROOT / "src/domains/runtime/routes/issues.py",
    ROOT / "src/domains/mgmt/public/__init__.py",
    ROOT / "src/api/db_setup.py",
]


def _top_level_duplicates(path: Path) -> dict[str, list[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    seen: dict[str, list[int]] = collections.defaultdict(list)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen[node.name].append(node.lineno)
    return {name: lines for name, lines in seen.items() if len(lines) > 1}


@pytest.mark.parametrize("path", _DEDUP_TARGET_FILES, ids=lambda p: p.name)
def test_no_duplicate_top_level_defs(path):
    """Issue #4158：每符号只保留唯一实现（重名 = 后定义静默覆盖前定义）。"""
    dups = _top_level_duplicates(path)
    assert dups == {}, f"{path} 仍有顶层同名定义: {dups}"


def _run(coro):
    return asyncio.run(coro)


def _install_notify_user(monkeypatch, result):
    """把 `notify_user`（函数内 import）替换为可控返回值，并记录调用参数。"""
    calls: list[tuple] = []
    from src.domains.runtime.im import notify as im_notify

    async def _fake_notify_user(user_id, kind, text, **kwargs):
        calls.append((user_id, kind, text, kwargs))
        return result

    monkeypatch.setattr(im_notify, "notify_user", _fake_notify_user)
    return calls


@pytest.fixture()
def fresh_counters():
    import src.domains.runtime.issues_center as ic

    ic._IM_NOTIFY_COUNTERS.clear()
    yield ic
    ic._IM_NOTIFY_COUNTERS.clear()


def test_pending_review_fans_out_without_ctx(fresh_counters, monkeypatch):
    """ctx=None + 仅非飞书渠道 → 必须送达（sent_ok），不得退化成 skipped_no_ctx。"""
    ic = fresh_counters
    sent_ok = {"sent": ["wecom"], "failed": [], "skipped": {}}
    calls = _install_notify_user(monkeypatch, sent_ok)

    _run(ic._run_pending_review_notify(
        None, None, UUID_UID, "", "", "请审核", "PR#1",
        "gitcode:org/test", 101, {}))

    counters = ic.get_im_notify_counters()
    assert len(calls) == 1 and calls[0][1] == "pending_review"
    assert calls[0][0] == UUID_UID
    assert counters.get("sent_ok") == 1
    assert "skipped_no_ctx" not in counters
    assert not any(k.startswith("skipped_") for k in counters), counters


def test_awaiting_fans_out_without_ctx(fresh_counters, monkeypatch):
    ic = fresh_counters
    sent_ok = {"sent": ["wecom"], "failed": [], "skipped": {}}
    calls = _install_notify_user(monkeypatch, sent_ok)

    _run(ic._run_awaiting_notify(
        None, None, UUID_UID, "", "", "补充信息",
        "gitcode:org/test", 102, {}))

    counters = ic.get_im_notify_counters()
    assert len(calls) == 1 and calls[0][1] == "awaiting"
    assert calls[0][0] == UUID_UID
    assert counters.get("sent_ok") == 1
    assert "skipped_no_ctx" not in counters
    assert not any(k.startswith("skipped_") for k in counters), counters


def test_task_notify_fans_out_without_ctx(fresh_counters, monkeypatch):
    ic = fresh_counters
    sent_ok = {"sent": ["wecom"], "failed": [], "skipped": {}}
    calls = _install_notify_user(monkeypatch, sent_ok)

    _run(ic._run_notify(
        None, None, UUID_UID, "", "", "任务描述",
        "gitcode:org/test", 103, {}))

    counters = ic.get_im_notify_counters()
    assert len(calls) == 1 and calls[0][1] == "task"
    assert counters.get("sent_ok") == 1
    assert "skipped_no_ctx" not in counters


def test_pending_review_reports_skip_when_no_channel(fresh_counters, monkeypatch):
    """对照：真无渠道（sent/failed/skipped 全空）仍计 skipped_no_ctx，语义未被改坏。"""
    ic = fresh_counters
    _install_notify_user(monkeypatch, {"sent": [], "failed": [], "skipped": {}})

    _run(ic._run_pending_review_notify(
        None, None, UUID_UID, "", "", "请审核", "",
        "gitcode:org/test", 104, {}))

    counters = ic.get_im_notify_counters()
    assert counters.get("skipped_no_ctx") == 1


def test_resolve_issue_notify_targets_keeps_uuid(fresh_counters, monkeypatch):
    """UUID 账号 id 不得被判成 0/无归属而短路（保留新版 `_uid_ok` 判定）。"""
    ic = fresh_counters

    class _FakeCursor:
        def execute(self, *args, **kwargs):
            return self

        def fetchone(self):
            return (UUID_UID, "alice", "需求标题", "")

    class _FakeDB:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(ic, "db", _FakeDB())
    monkeypatch.setattr(ic, "db_lock", contextlib.nullcontext())
    monkeypatch.setattr(ic, "_ensure_task_conversation", lambda *a, **k: "", raising=False)

    open_id_calls: list[str] = []

    def _fake_open_id_by_uid(user_id):
        open_id_calls.append(user_id)
        return "ou_uuid"

    monkeypatch.setattr(ic, "_get_feishu_open_id_by_user_id", _fake_open_id_by_uid, raising=False)
    monkeypatch.setattr(ic, "_get_feishu_open_id", lambda *a, **k: "", raising=False)
    monkeypatch.setattr(ic, "get_chat_config", lambda uid: {"uid": uid}, raising=False)

    out = ic._resolve_issue_notify_targets("gitcode:org/test", 105)

    assert out["user_id"] == UUID_UID
    assert open_id_calls == [UUID_UID], "UUID 账号必须查询 open_id，不得按 0 短路"
    assert out["open_id"] == "ou_uuid"
    assert out["cfg"] == {"uid": UUID_UID}