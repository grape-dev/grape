"""结构化产出解析测试（见 acceptance-plan M1-S1~S4 / test_event_store.py）"""
import pytest

from src.api.event_store import parse_agent_output_json, evaluate_agent_output


class TestParseAgentOutputJson:
    def test_parses_valid_block(self):
        text = "处理完成。\n```json\n{\"actions\": [\"labeled:bug\"], \"conclusion\": \"分类为 bug\", \"artifacts\": [], \"next_step\": \"\", \"issue_summary\": \"s\"}\n```"
        out = parse_agent_output_json(text)
        assert out and out["actions"] == ["labeled:bug"]
        assert out["conclusion"] == "分类为 bug"

    def test_rejects_no_block(self):
        assert parse_agent_output_json("无 JSON 块") is None

    def test_rejects_wrong_shape(self):
        assert parse_agent_output_json('{"foo": "bar"}') is None

    def test_rejects_invalid_json(self):
        assert parse_agent_output_json('{"actions": [' + chr(0x1F600) + '}') is None


class TestEvaluatePrefersJson:
    def test_json_block_wins(self):
        text = "分析完毕。{\"actions\": [], \"conclusion\": \"确定是内存泄漏\", \"artifacts\": [], \"next_step\": \"\", \"issue_summary\": \"\"}"
        has, result = evaluate_agent_output("issue", [], text)
        assert has and result == "确定是内存泄漏"

    def test_fallback_to_regex(self):
        # 无 JSON 块 → 走现有正则（issue 关键词）
        has, result = evaluate_agent_output("issue", [], "已回复该 Issue 并标记完成")
        assert has and "已回复" in result


class TestBuildPromptContract:
    def test_prompt_includes_output_requirement(self):
        # issue-219 方向 D+E：输出契约首段为 Task Completion（英文）
        from src.prompt_builder import build_devops_agent_prompt
        evt = {"type": "issue_opened", "repository": "owner/repo", "payload": {"issue": {"number": 5, "title": "t", "body": "b"}}, "source": "gitcode", "issue_id": "#5"}
        prompt = build_devops_agent_prompt(evt, [], "", ["issue"])
        assert "## Task Completion" in prompt
        assert "issue_summary" in prompt
