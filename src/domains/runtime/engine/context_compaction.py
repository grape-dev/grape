"""Context compaction for Agent sessions (Cordis context compaction pattern adapted for Grape).

When accumulated task messages exceed a threshold, generate a summary of older messages
and replace them, preventing LLM context window overflow and timeout.

This runs on the server side (in _execute_task's message reporting loop) and on the
hermes ACP side (checking state.db size before session/resume).
"""
import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("devops-bot")

# Threshold: trigger compaction when message count exceeds this
COMPACTION_THRESHOLD = int(os.environ.get("GRAPE_COMPACTION_THRESHOLD", "200"))
# Keep this many recent messages after compaction
COMPACTION_KEEP_RECENT = int(os.environ.get("GRAPE_COMPACTION_KEEP_RECENT", "50"))
# Summary placeholder inserted before recent messages
_SUMMARY_MARKER = "[compacted-history]"


def should_compact(messages: list[dict]) -> bool:
    """Check if message count exceeds compaction threshold."""
    return len(messages) > COMPACTION_THRESHOLD


def compact_messages(messages: list[dict]) -> list[dict]:
    """Compact older messages into a summary, keeping recent ones intact.

    Instead of calling an LLM (which would add latency and cost), we generate
    a structured summary of the old messages: count by type, extract key
    tool_use names, and truncate long text outputs. This is sufficient to
    reduce context size while preserving execution trace.

    Args:
        messages: Full list of task_messages dicts with 'type' and 'data'

    Returns:
        Compacted list: [summary_message, ...recent_messages]
    """
    if len(messages) <= COMPACTION_THRESHOLD:
        return messages

    old_messages = messages[:-COMPACTION_KEEP_RECENT]
    recent_messages = messages[-COMPACTION_KEEP_RECENT:]

    # Build structured summary
    type_counts: dict[str, int] = {}
    tool_names: list[str] = []
    text_snippets: list[str] = []

    for msg in old_messages:
        msg_type = msg.get("type", "unknown")
        type_counts[msg_type] = type_counts.get(msg_type, 0) + 1

        data = msg.get("data", "")
        if msg_type == "tool_use" and isinstance(data, dict):
            tool_names.append(data.get("tool", ""))
        elif msg_type == "text" and isinstance(data, str):
            # Keep first 80 chars of each text message as snippet
            if len(data.strip()) > 0:
                text_snippets.append(data.strip()[:80])

    summary_parts = [
        f"{_SUMMARY_MARKER} ({len(old_messages)} messages compacted)",
        f"Type counts: {json.dumps(type_counts, ensure_ascii=False)}",
    ]
    if tool_names:
        # Deduplicate tool names, keep order
        seen = set()
        unique_tools = [t for t in tool_names if t and not (t in seen or seen.add(t))]
        summary_parts.append(f"Tools used: {', '.join(unique_tools[:20])}")
    if text_snippets:
        summary_parts.append(f"Key text: {' | '.join(text_snippets[:10])}")

    summary_msg = {
        "type": "text",
        "data": "\n".join(summary_parts),
    }

    log.info(
        f"Context compaction: {len(old_messages)} old messages -> 1 summary, "
        f"keeping {len(recent_messages)} recent (total {len(old_messages) + len(recent_messages)} -> {len(recent_messages) + 1})"
    )

    return [summary_msg] + recent_messages


def check_state_db_size(hermes_home: str, threshold_mb: float = 50.0) -> bool:
    """Check if hermes state.db is too large (context bloat indicator).

    Args:
        hermes_home: HERMES_HOME directory path
        threshold_mb: Size threshold in MB

    Returns:
        True if state.db exceeds threshold (should compact or reset session)
    """
    state_db = os.path.join(hermes_home, "state.db")
    try:
        if os.path.exists(state_db):
            size_mb = os.path.getsize(state_db) / (1024 * 1024)
            if size_mb > threshold_mb:
                log.warning(
                    f"state.db size {size_mb:.1f}MB exceeds threshold {threshold_mb}MB, "
                    f"considering session reset to prevent context overflow"
                )
                return True
    except Exception:
        pass
    return False
