"""Issue #2792 切片 3：外部执行结论上报的幂等键与事件字段（纯函数 seam）。

Spec（评论 id 201190348）实现路径 4：外部执行者（用户态、无 task_id）携带
issue + 节点 + 结论上报时，写为**对应节点事件**（`source=external`、
`status=completed`、结构化结论），并按 `(issue, node, session)` 幂等去重。

幂等键必须落在 events 的 `(repo_key, idempotency_key)` 唯一索引格式约束内
（`[A-Za-z0-9_:.\\-]{1,128}`，见 a2a 端点同款校验），否则落库直接失败；
这里把键派生与事件字段组装单独测，端点只做连线。
"""
from __future__ import annotations

import re

from src.api.external_node_report import (
    EXTERNAL_SOURCE,
    NODE_EVENT_TYPE,
    build_external_node_event,
    external_report_idempotency_key,
)

_KEY_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,128}$")


def test_same_issue_node_session_yields_same_key():
    """同一 (issue, node, session) 重复上报必须命中同一个键（幂等的前提）。"""
    a = external_report_idempotency_key(2792, "issue", "sess-1")
    b = external_report_idempotency_key("2792", "issue", "sess-1")
    assert a == b


def test_key_distinguishes_issue_node_and_session():
    """三个维度任一不同即不同键：不同节点/不同会话是各自独立的结论。"""
    base = external_report_idempotency_key(2792, "issue", "sess-1")
    assert base != external_report_idempotency_key(2793, "issue", "sess-1")
    assert base != external_report_idempotency_key(2792, "coding", "sess-1")
    assert base != external_report_idempotency_key(2792, "issue", "sess-2")


def test_key_is_safe_for_event_store_constraint():
    """键必须满足 events 幂等列格式约束（含中文节点名/异常会话名时也不得越界）。"""
    for node, session in (("issue", ""), ("编码节点", "sess/1 with spaces"),
                          ("coding", "s" * 300)):
        key = external_report_idempotency_key(2792, node, session)
        assert _KEY_RE.match(key), key
        assert len(key) <= 128


def test_event_fields_mark_source_and_node():
    """事件必须自称「外部执行、某节点、已完成」——这是 Issue 页面能看见结论的前提。"""
    ev = build_external_node_event(
        2792, "issue", '{"conclusion": "Spec 已确认"}',
        task_id=7, session="sess-1", actor="alice")
    assert ev["event_type"] == NODE_EVENT_TYPE == "a2a_event"
    assert ev["source"] == EXTERNAL_SOURCE == "external"
    assert ev["agent_type"] == "issue"
    assert ev["issue_id"] == "#2792"
    assert ev["status"] == "completed"
    assert ev["payload"]["external"] is True
    assert ev["payload"]["node"] == "issue"
    assert ev["payload"]["issue_number"] == 2792
    assert ev["payload"]["task_id"] == 7
    assert ev["payload"]["session"] == "sess-1"
    assert ev["actor"] == "alice"
    assert ev["result"] == '{"conclusion": "Spec 已确认"}'


def test_event_idempotency_key_matches_helper():
    """事件里的键必须与键派生函数同源，否则去重与落库会各算各的。"""
    ev = build_external_node_event(2792, "qa", "结论", session="sess-9")
    assert ev["idempotency_key"] == external_report_idempotency_key(2792, "qa", "sess-9")


def test_event_title_names_node_and_is_not_a_bare_event_type():
    """标题要能上 Issue 事件链（`_get_issue_title` 会把裸 event_type 当占位丢弃）。"""
    ev = build_external_node_event(2792, "coding", "结论")
    assert "coding" in ev["title"]
    assert ev["title"] not in ("a2a_event", "issue_opened")