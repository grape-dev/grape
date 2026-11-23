"""A2A 上下文键的类型归一（Issue: 交接 500 —— 'dict' object has no attribute 'strip'）。

现场：agent 端把 `context` 传成对象后，`POST /api/a2a` 在 a2a_context_key 里直接
`.strip()` → AttributeError → 500，事件不建、下一节点永不派发（节点「执行完成就卡住」）。
这里锁三件事：
1. 对象/数组 context 不再抛，且同内容（键序无关）得到同一个键；
2. 纯字符串 context 的键值与旧实现完全一致（不破坏既有幂等）；
3. 从落库 payload 复算（a2a_event_context_key）与创建时算键结果一致。
"""

import hashlib

from src.kernel.a2a_context import a2a_context_key, a2a_event_context_key


def _sha(text: str) -> str:
    return "ctx:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def test_dict_context_does_not_raise_and_is_stable():
    obj = {"pr": 11, "note": "T3 补测"}
    key1 = a2a_context_key("", obj)
    key2 = a2a_context_key("", {"note": "T3 补测", "pr": 11})  # 键序不同
    assert key1.startswith("ctx:")
    assert key1 == key2


def test_list_context_does_not_raise():
    assert a2a_context_key("", ["a", {"b": 1}]).startswith("ctx:")


def test_string_context_key_matches_legacy_formula():
    """字符串路径的键必须与旧实现逐字一致（既有事件行的幂等不能变）。"""
    assert a2a_context_key("", "  T3 补测  ") == _sha("T3 补测")
    assert a2a_context_key("", "") == ""
    assert a2a_context_key("", None) == ""


def test_idempotency_key_wins_over_context():
    assert a2a_context_key("task-42:qa", {"x": 1}) == "idem:task-42:qa"


def test_event_context_key_matches_creation_for_object_context():
    """落库 payload 里 context 是对象 → 复算键必须与创建时相同（否则去重失效）。"""
    obj = {"pr": 11, "note": "补测"}
    created = a2a_context_key("", obj)
    raw = '{"context": {"pr": 11, "note": "补测"}}'
    assert a2a_event_context_key("", raw) == created


def test_event_context_key_matches_creation_for_string_context():
    created = a2a_context_key("", "同一段上下文")
    assert a2a_event_context_key("", '{"context": "同一段上下文"}') == created


def test_non_serializable_object_degrades_instead_of_raising():
    class Weird:
        def __str__(self):
            return "weird-value"

    assert a2a_context_key("", Weird()) == _sha("weird-value")