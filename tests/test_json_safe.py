"""Issue #684：统一 JSON 序列化出口（SafeJSONResponse / dumps_safe）覆盖测试。

验收：含原始控制字符 / 孤立代理项的字符串字段，序列化输出可 json.loads
无预处理直接解析；单一统一出口（default_response_class），非逐端点补丁。
"""
import json

import pytest

from src.api.json_safe import SafeJSONResponse, dumps_safe, render_safe


def test_control_chars_round_trip():
    """\x01-\x1f 全量控制字符 + 原始换行/制表：输出 json.loads 直接解析且值不变。"""
    raw = "".join(chr(c) for c in range(0x01, 0x1F)) + "\x00\x7f\n\r\t"
    content = {"body": raw, "nested": [{"text": "a\x02b"}], "num": 1}
    out = dumps_safe(content)
    assert b"\x01" not in out  # 原始控制字符不进入字节流
    parsed = json.loads(out)
    assert parsed == content


def test_lone_surrogate_fallback():
    """孤立代理项：ensure_ascii=False 会 UnicodeEncodeError → 降级转义后仍可解析。"""
    content = {"body": "bad \ud800 surrogate \udfff"}
    out = dumps_safe(content)
    parsed = json.loads(out)
    assert parsed["body"] == "bad \ud800 surrogate \udfff"


def test_unicode_preserved_when_safe():
    """普通中文/emoji 走紧凑 UTF-8 主路径（不被全量 ASCII 转义）。"""
    out = dumps_safe({"t": "中文🎉"})
    assert "中文🎉".encode("utf-8") in out


def test_non_dict_content():
    """list/str/None 内容同样安全序列化。"""
    assert json.loads(dumps_safe([{"a": "x\x01"}])) == [{"a": "x\x01"}]
    assert json.loads(dumps_safe("plain")) == "plain"
    assert json.loads(dumps_safe(None)) is None


def test_render_safe_alias():
    assert render_safe({"k": "v\x1f"}) == dumps_safe({"k": "v\x1f"})


def test_response_class_render():
    """SafeJSONResponse.render：控制字符转义进合法 JSON 转义序列。"""
    resp = SafeJSONResponse(content={"body": "line1\nline2\x01"})
    body = resp.body
    parsed = json.loads(body)
    assert parsed["body"] == "line1\nline2\x01"
    assert b"line1\\nline2\\u0001" in body  # 控制字符以转义序列出现在字节流
