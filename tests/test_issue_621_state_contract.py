"""Issue #621：issues update `state` 字段契约对齐后端语义（reopen/close）。

- schema 层：enum ["reopen","close"] + description（对齐 GitHub state_event）+ examples ["reopen"]。
- 请求示例：模型级 examples {"state": "reopen"}。
- 运行时行为不变：`state` 仍为自由 str（透传后端，非法值由后端 400 拒绝）。
"""
import importlib

from src.api.main import app
from src.api.openapi import custom_openapi


def _schema():
    schema = custom_openapi(app)
    return schema["components"]["schemas"]["IssueUpdateBody"]


def test_state_contract_enum_description_example():
    schema = _schema()
    state = schema["properties"]["state"]
    assert state.get("enum") == ["reopen", "close"]
    assert "reopen" in state.get("description", "") and "close" in state.get("description", "")
    assert state.get("examples") == ["reopen"]
    # 请求示例：{"state": "reopen"}
    assert schema.get("examples") == [{"state": "reopen"}]


def test_state_runtime_unchanged_still_free_string():
    """运行时行为不动：模型字段仍是 str（非 Literal 校验），空默认值可实例化。"""
    gra = importlib.import_module("src.api.git_repo_api")
    body = gra.IssueUpdateBody()
    assert body.state == ""
    body = gra.IssueUpdateBody(state="close")
    assert body.state == "close"
