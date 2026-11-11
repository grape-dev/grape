"""AtomCode headless JSONL runtime (schema v1)."""
import json
import os

from .base import AgentRuntime


class AtomCodeRuntime(AgentRuntime):
    name = "atomcode"
    binary = "atomcode"
    prompt_via_stdin = True
    text_separator = ""
    requires_terminal_event = True
    allow_auto_retry = False
    supports_session = False

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        return self.build_args_for_env(prompt, session_id, os.environ)

    def build_args_for_env(self, prompt: str, session_id: str, env: dict) -> list[str]:
        # /dev/stdin is verified on the initial macOS target. Do not put the
        # prompt in argv, where process lists and executor logs can expose it.
        if os.name == "nt":
            raise RuntimeError("AtomCode stdin prompt transport is not supported on Windows")
        args = ["--ephemeral", "--dev", "--no-telemetry", "--output-format", "jsonl"]
        provider = str(env.get("GRAPE_ATOMCODE_PROVIDER") or "").strip()
        model = str(env.get("GRAPE_ATOMCODE_MODEL") or "").strip()
        if bool(provider) != bool(model):
            raise ValueError("GRAPE_ATOMCODE_PROVIDER and GRAPE_ATOMCODE_MODEL must be set together")
        if provider:
            args += ["--provider", provider, "--model", model]
        args += ["--prompt-file", "/dev/stdin"]
        return args

    def parse_output(self, line: str) -> dict | None:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get("type", "")
        if kind == "run.started":
            if type(event.get("schema_version")) is not int or event.get("schema_version") != 1:
                return {"type": "error", "message": "Unsupported AtomCode JSONL schema version"}
            return {"type": "step_start", "sessionID": ""}
        if kind in ("message.delta", "reasoning.delta"):
            text = event.get("text")
            return {"type": "text" if kind == "message.delta" else "reasoning", "text": text} if isinstance(text, str) and text else None
        if kind == "tool.started":
            args = event.get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    args = {"raw": args}
            if not isinstance(args, dict):
                args = {"raw": args}
            return {"type": "tool_use", "tool": str(event.get("name") or ""), "input": args}
        if kind == "tool.completed":
            content = str(event.get("content") or "")
            if event.get("is_error"):
                content = "[tool error] " + content
            return {"type": "tool_result", "text": content}
        if kind == "usage":
            return {"type": "usage", "usage": {
                "input": _nonnegative_int(event.get("prompt_tokens")),
                "output": _nonnegative_int(event.get("completion_tokens")),
                "cache_read": _nonnegative_int(event.get("cached_tokens")),
            }}
        if kind == "turn.completed":
            reason = event.get("stop_reason")
            exit_code = event.get("exit_code")
            ok = reason == "Stopped" and type(exit_code) is int and exit_code == 0
            return {"type": "terminal", "ok": ok,
                    "message": "" if ok else f"AtomCode did not complete: {reason or 'unknown'} (exit={exit_code})"}
        if kind == "run.failed":
            return {"type": "terminal", "ok": False,
                    "message": "AtomCode failed: " + str(event.get("message") or "unknown error")}
        if kind == "error":
            # A retryable error can be followed by a successful turn; preserve it
            # as a diagnostic, and let the terminal determine the result.
            message = str(event.get("message") or "AtomCode error")
            return {"type": "warning" if event.get("retryable") is True else "error", "message": message}
        if kind in ("warning", "retry", "rate_limit"):
            return {"type": "warning", "message": str(event.get("message") or event.get("reason") or kind)}
        return {"type": "other", "raw": str(kind)}

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        return os.environ.copy()


def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
