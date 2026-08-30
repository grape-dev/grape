#!/usr/bin/env python3
import json
import re
import os
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional

from .config import LOG_PATTERNS

OPENCODE_DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")


@dataclass
class CallChainItem:
    chain_type: str
    description: str
    status: str
    exec_time: str
    sort_order: int
    timestamp: str = ""


@dataclass
class RawMetrics:
    status: str = "unknown"
    duration_seconds: float = 0.0
    exit_code: int = -1
    has_bug: bool = False
    prompt_match_count: int = 0
    total_tokens: int = 0
    tool_fail_count: int = 0
    retry_count: int = 0
    cli_call_count: int = 0
    sdk_call_count: int = 0
    api_call_count: int = 0
    llm_call_count: int = 0
    context_compressed: bool = False
    call_chain: List[CallChainItem] = field(default_factory=list)
    security_risk_level: str = "medium"


def _count_pattern(text, pattern):
    return len(re.findall(pattern, text))


_TEST_SCRIPT_RE = re.compile(
    r"(?:\$\s+[\w\-./=\"]*\s*)?(?:bash\s+)?[\w./-]*test-cli-commands\.sh[^\n]*",
    re.IGNORECASE,
)


def _has_bug_in_test_segment(log_text):
    """
    判定 skill 是否存在 BUG：只在 test-cli-commands.sh 执行段内匹配 bug 模式。

    背景：output.log 是整个 opencode 会话日志，混入了 agent 评测时的调试输出
    （如 python3 -c 查 DB、环境探测等）。这些调试产生的 Traceback 不应被判定为
    skill 的 bug。方案 A：只统计 skill 测试脚本执行段内的 bug 模式。

    若无测试脚本执行段，返回 False（不误判）。
    """
    lines = log_text.split("\n")
    segments = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _TEST_SCRIPT_RE.search(line):
            segment = [line]
            i += 1
            while i < len(lines):
                # 遇到下一个工具命令标记或 opencode 日志行，结束当前测试段
                if (
                    re.search(r"message=(?:evaluated|loop|process|stream)\b", lines[i])
                    or re.match(r"^\x1b\[0m→\s", lines[i])
                ):
                    break
                segment.append(lines[i])
                i += 1
            segments.append("\n".join(segment))
        else:
            i += 1
    if not segments:
        return False
    combined = "\n".join(segments)
    return _count_pattern(combined, LOG_PATTERNS["bug"]) > 0


def _parse_result_json(session_dir):
    path = os.path.join(session_dir, "result.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def _parse_output_log(session_dir):
    path = os.path.join(session_dir, "output.log")
    if os.path.exists(path):
        with open(path, "r", errors="replace") as f:
            return f.read()
    return ""


def _extract_call_chain(log_text):
    chain = []
    order = 0
    chain_pattern = re.compile(
        r"\[(?P<type>LLM|CLI|SDK|API)\]\s*(?P<desc>.+?)\s*\|\s*(?P<status>\w+)\s*\|\s*(?P<time>[\dms]+)"
    )
    for m in chain_pattern.finditer(log_text):
        order += 1
        chain.append(CallChainItem(
            chain_type=m.group("type"),
            description=m.group("desc").strip(),
            status=m.group("status").strip(),
            exec_time=m.group("time").strip(),
            sort_order=order,
        ))
    return chain


def _parse_opencode_tokens(log_text):
    session_id = _extract_session_id(log_text)
    if session_id and os.path.exists(OPENCODE_DB_PATH):
        try:
            conn = sqlite3.connect(OPENCODE_DB_PATH)
            row = conn.execute(
                "SELECT tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write "
                "FROM session WHERE id=?", (session_id,)
            ).fetchone()
            conn.close()
            if row and any(v > 0 for v in row):
                return sum(row)
        except Exception:
            pass
    total = 0
    for key in ("token_input", "token_output", "token_reasoning", "token_cache_read", "token_cache_write"):
        matches = re.findall(LOG_PATTERNS[key], log_text)
        if matches:
            total += sum(int(t) for t in matches)
    return total


def _extract_session_id(log_text):
    m = re.search(r"session\.id=(ses_\w+)", log_text)
    return m.group(1) if m else None


def _parse_opencode_steps(log_text):
    steps = re.findall(LOG_PATTERNS["loop_step"], log_text)
    return max(int(s) for s in steps) if steps else 0


def _parse_opencode_tool_invokes(log_text):
    results = []
    for m in re.finditer(LOG_PATTERNS["tool_invoke"], log_text):
        tool = m.group(1) or m.group(2)
        results.append(tool)
    return results


def _build_call_chain_from_opencode(log_text, metrics):
    import datetime as dt

    chain = []
    order = 0
    clean = re.sub(r"\x1b\[[0-9;]*m", "", log_text)

    events = []

    ts_pattern = re.compile(r"^timestamp=(\S+)")
    tool_arrow = re.compile(r"^→\s+(Read|Write|Edit|Glob|Grep|WebFetch|Task|Skill)\s+(.*)", re.MULTILINE)
    tool_bash = re.compile(r"^\$\s+(.*)", re.MULTILINE)
    llm_stream = re.compile(r"message=stream\s+providerID=\S+\s+modelID=(\S+)", re.MULTILINE)
    llm_process = re.compile(r"message=process\s+session\.id=\S+\s+messageID=(\S+)")
    permission_bash = re.compile(r'message="evaluated permission"\s+permission=bash\s+pattern="([^"]+)"')
    step_loop = re.compile(r"message=loop\s+session\.id=\S+\s+step=(\d+)")

    lines = clean.split("\n")
    prev_ts = None
    for line in lines:
        ts_m = ts_pattern.match(line)
        if ts_m:
            try:
                prev_ts = dt.datetime.fromisoformat(ts_m.group(1).replace("Z", "+00:00"))
            except Exception:
                prev_ts = None

        if not prev_ts:
            continue

        m = tool_arrow.match(line.strip())
        if m:
            tool_name = m.group(1)
            tool_arg = m.group(2).strip()[:80]
            ctype = "CLI"
            if tool_name in ("WebFetch", "Task"):
                ctype = "API"
            elif tool_name == "Skill":
                ctype = "SDK"
            events.append((prev_ts, ctype, f"{tool_name} {tool_arg}", "ok"))
            continue

        m = tool_bash.match(line.strip())
        if m:
            cmd = m.group(1).strip()[:80]
            ctype = "CLI"
            if cmd.startswith("hcloud "):
                ctype = "SDK"
            elif cmd.startswith("curl ") or cmd.startswith("python3 ") and "http" in cmd:
                ctype = "API"
            events.append((prev_ts, ctype, f"bash: {cmd}", "ok"))
            continue

        m = llm_process.search(line)
        if m:
            events.append((prev_ts, "LLM", "LLM inference", "ok"))
            continue

    events.sort(key=lambda e: e[0])

    for i, (ts, ctype, desc, status) in enumerate(events):
        order += 1
        if i > 0:
            delta = (ts - events[i - 1][0]).total_seconds()
            exec_time = f"{int(delta * 1000)}ms" if delta < 1 else f"{delta:.1f}s"
        else:
            exec_time = "0ms"
        chain.append(CallChainItem(
            chain_type=ctype,
            description=desc,
            status=status,
            exec_time=exec_time,
            sort_order=order,
            timestamp=ts.strftime("%H:%M:%S"),
        ))

    return chain


def parse(session_dir, agent="opencode"):
    metrics = RawMetrics()
    result = _parse_result_json(session_dir)
    metrics.status = result.get("status", "unknown")
    metrics.duration_seconds = result.get("duration_seconds", 0.0)
    metrics.exit_code = result.get("exit_code", -1)

    log_text = _parse_output_log(session_dir)
    metrics.has_bug = _has_bug_in_test_segment(log_text)
    metrics.prompt_match_count = _count_pattern(log_text, LOG_PATTERNS["prompt_match"])

    metrics.total_tokens = _parse_opencode_tokens(log_text)

    error_count = _count_pattern(log_text, LOG_PATTERNS["error"])
    metrics.tool_fail_count = error_count
    metrics.retry_count = _count_pattern(log_text, LOG_PATTERNS["retry"])

    process_count = _count_pattern(log_text, r"message=process\s")
    metrics.llm_call_count = process_count

    tool_invokes = _parse_opencode_tool_invokes(log_text)
    arrow_tools = [t for t in tool_invokes if t]
    bash_count = _count_pattern(log_text, r"\$\s+(?:\x1b\[[0-9;]*m)?\w+")
    metrics.cli_call_count = len(arrow_tools) + bash_count

    metrics.sdk_call_count = _count_pattern(log_text, LOG_PATTERNS["sdk_call"])
    metrics.api_call_count = _count_pattern(log_text, LOG_PATTERNS["api_call"])
    metrics.context_compressed = _count_pattern(log_text, LOG_PATTERNS["context_compress"]) > 0

    metrics.call_chain = _extract_call_chain(log_text)
    if not metrics.call_chain:
        metrics.call_chain = _build_call_chain_from_opencode(log_text, metrics)

    return metrics
