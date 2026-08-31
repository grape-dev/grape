#!/usr/bin/env python3
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.log_parser import parse, RawMetrics


def _make_session(log_text, result_json=None):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "output.log"), "w") as f:
        f.write(log_text)
    if result_json:
        with open(os.path.join(d, "result.json"), "w") as f:
            import json
            json.dump(result_json, f)
    return d


def test_parse_status_from_result_json():
    d = _make_session("", {"status": "success", "duration_seconds": 10, "exit_code": 0})
    m = parse(d)
    assert m.status == "success"


def test_error_count():
    d = _make_session("level=ERROR something failed\nlevel=INFO ok\n")
    m = parse(d)
    assert m.tool_fail_count == 1


def test_cli_calls_from_arrow_and_dollar():
    d = _make_session("\x1b[0m→ \x1b[0mRead SKILL.md\n$ hcloud version\n$ echo hello\n")
    m = parse(d)
    assert m.cli_call_count >= 2


def test_sdk_calls_from_hcloud():
    d = _make_session("$ hcloud SWR ListNamespaces --cli-region=cn-north-4\n")
    m = parse(d)
    assert m.sdk_call_count >= 1


def test_prompt_match_workflow_step():
    d = _make_session("Now executing Workflow Step 2: Verify prerequisites\n[✓] Verify prerequisites\n")
    m = parse(d)
    assert m.prompt_match_count >= 1


def test_prompt_match_capital_step():
    d = _make_session("Now I'll execute the skill workflow. Step 3: run the read-only SWR queries.\n")
    m = parse(d)
    assert m.prompt_match_count >= 1


def test_prompt_match_step_range():
    d = _make_session("Step 1-2: query scope is cn-north-4. Step 3: execute query.\n")
    m = parse(d)
    assert m.prompt_match_count >= 1


def test_no_bug_by_default():
    d = _make_session("everything is fine\n")
    m = parse(d)
    assert m.has_bug is False


def test_bug_detected():
    d = _make_session(
        "bash scripts/test-cli-commands.sh . cli 2>&1\n"
        "=== Test Suite ===\n"
        "Traceback (most recent call last):\n"
        "  ...\n"
        "BUG: something\n"
    )
    m = parse(d)
    assert m.has_bug is True


def test_llm_call_count_uses_process():
    d = _make_session("message=stream session.id=ses_xxx\nmessage=process session.id=ses_xxx\nmessage=stream session.id=ses_xxx\nmessage=process session.id=ses_xxx\n")
    m = parse(d)
    assert m.llm_call_count == 2


def test_no_prompt_match_fake():
    d = _make_session("", {"status": "success", "duration_seconds": 10, "exit_code": 0})
    m = parse(d)
    assert m.prompt_match_count == 0


def test_bug_outside_test_script_not_counted():
    d = _make_session(
        "python3 -c \"import pymysql\"\n"
        "Traceback (most recent call last):\n"
        "  File \"<string>\", line 7, in <module>\n"
        "pymysql.err.OperationalError: (1054, \"Unknown column 'skill_name' in 'field list'\")\n"
        "\n"
        "bash scripts/test-cli-commands.sh . cli 2>&1\n"
        "=== Test Suite ===\n"
        "[TC-01] PASS\n"
        "=== Test Summary ===\n"
        "PASS: 1\n"
    )
    m = parse(d)
    assert m.has_bug is False


def test_bug_inside_test_script_counted():
    d = _make_session(
        "bash scripts/test-cli-commands.sh . cli 2>&1\n"
        "=== Test Suite ===\n"
        "[TC-01] Command: hcloud SWR List\n"
        "Traceback (most recent call last):\n"
        "  File \"test.sh\", line 5, in <module>\n"
        "RuntimeError: BUG detected\n"
        "=== Test Summary ===\n"
        "PASS: 0\n"
    )
    m = parse(d)
    assert m.has_bug is True


def test_bug_no_test_script_defaults_false():
    d = _make_session("hcloud CES ListAlarmRules --cli-region=cn-north-4\n")
    m = parse(d)
    assert m.has_bug is False
