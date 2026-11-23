"""Issue #3169 — Chat 插件多选注入（P1-1 可红反馈环）

Seam（用户可见行为）：
1. /chat/submit 请求体的 plugins 字段不被丢弃（ChatMessage 显式声明字段）。
2. 选中插件后会话 Prompt 含 `## Plugin Capabilities` 段，且列出所选插件
   （其中插件能力明细来自 B5/C1 契约，读取失败时退化为仅列插件名，不阻塞）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault(
    "DEVOPS_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "test_sessions.db"),
)

import pytest  # noqa: E402


def test_chat_message_keeps_plugins_field():
    from src.domains.runtime.routes.chat import ChatMessage

    msg = ChatMessage(message="hi", repo_key="gitcode:o/r", plugins=["demo", "other"])
    assert msg.plugins == ["demo", "other"]


def test_general_chat_prompt_injects_selected_plugins(monkeypatch):
    from src.domains.runtime.engine import plugin_state
    from src.kernel.prompt_builder import build_general_chat_prompt

    monkeypatch.setattr(
        plugin_state,
        "collect_plugin_capabilities",
        lambda *a, **k: [
            {
                "name": "demo",
                "version": "1.2.3",
                "capabilities": "示例能力",
                "hooks": ["scene_node"],
                "status": "ready",
                "health": "ok",
            }
        ],
    )

    prompt = build_general_chat_prompt(
        "use it", repo_key="gitcode:o/r", plugins=["demo"]
    )
    assert "## Plugin Capabilities" in prompt
    assert "demo" in prompt
    assert "1.2.3" in prompt


def test_general_chat_prompt_degrades_to_names_when_state_unavailable(monkeypatch):
    from src.domains.runtime.engine import plugin_state
    from src.kernel.prompt_builder import build_general_chat_prompt

    def _boom(*a, **k):
        raise RuntimeError("state unavailable")

    monkeypatch.setattr(plugin_state, "collect_plugin_capabilities", _boom)

    prompt = build_general_chat_prompt(
        "use it", repo_key="gitcode:o/r", plugins=["demo"]
    )
    assert "## Plugin Capabilities" in prompt
    assert "demo" in prompt


def test_issue_chat_prompt_injects_selected_plugins(monkeypatch):
    from src.domains.runtime.engine import plugin_state
    from src.kernel.prompt_builder import build_issue_chat_prompt

    monkeypatch.setattr(plugin_state, "collect_plugin_capabilities", lambda *a, **k: [])

    prompt = build_issue_chat_prompt(
        "use it",
        "gitcode:o/r",
        [],
        {"conv_type": "task", "issue_number": 1, "title": "t"},
        plugins=["demo"],
    )
    assert "## Plugin Capabilities" in prompt
    assert "demo" in prompt


def test_runtime_detail_exposes_desired_plugins(monkeypatch):
    """P2：runtime 详情下发 desired_plugins（角色标签种子 ∪ 控制台远装期望）。"""
    from src.domains.mgmt.plugins import resolver
    from src.domains.mgmt.routes import settings

    monkeypatch.setattr(resolver, "desired_plugins_for_tags", lambda tags: ["seed-a"])
    out = settings._runtime_desired_plugins({"id": 999999999, "agent_tags": ["coding"]})
    assert "seed-a" in out


def test_chat_prompt_forwards_plugins():
    """统一 prompt 构建入口必须把 plugins 透传到 prompt（防改一处漏三处）。"""
    from src.domains.runtime.routes.chat import _build_chat_prompt_for

    prompt, _ = _build_chat_prompt_for(
        {"runtime_id": 0},
        {"id": "1", "username": "u"},
        {},
        [],
        "gitcode:o/r",
        {"conv_type": "chat"},
        "hi",
        "chat",
        0,
        plugins=["demo"],
    )
    assert "## Plugin Capabilities" in prompt