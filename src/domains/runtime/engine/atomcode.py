"""Rust-based AtomCode CLI: prompt files and schema-v1 headless JSONL."""
import functools
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager

from .base import AgentRuntime


@functools.lru_cache(maxsize=4)
def cli_help_text(binary: str = "atomcode") -> str:
    """Return `atomcode --help` output, or "" when the CLI cannot be probed.

    Issue #3511: CLI 版本间参数集不同（5.0.4 没有 `--output-format`，多了会
    `error: unexpected argument` → exit=2，任务全灭）。可选参数一律按 help
    能力探测决定，探测失败（CLI 缺失/超时）返回空串表示「不知道」——调用方
    此时保持既有参数不变，避免把老环境改坏。
    """
    try:
        proc = subprocess.run(
            [binary, "--help"], capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception:  # noqa: BLE001 - 探测失败只是没有能力信息
        return ""


class AtomCodeRuntime(AgentRuntime):
    name = "atomcode"
    binary = "atomcode"
    prompt_via_stdin = False
    text_separator = ""  # message.delta contains fragments, not complete messages

    @contextmanager
    def prepare_prompt(self, prompt: str, cwd: str):
        # Keep prompts outside the checkout so an agent's git add cannot commit
        # them. The executor mounts this one file read-only when using bwrap.
        # Close before launch for Windows readers; mkstemp creates a private file.
        fd, path = tempfile.mkstemp(prefix="grape-atomcode-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(prompt)
            yield path
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        """prompt is the file path yielded by prepare_prompt, never prompt text."""
        args = ["--prompt-file", prompt]
        _help = cli_help_text(self.binary)
        # 能力探测：help 里没有的开关绝不传（AtomCode 5.0.4 收到
        # --output-format 直接 `error: unexpected argument` → exit=2，任务全灭）。
        # _help 为空 = 探测不到，保持历史参数不变（既不放宽也不收紧）。
        if not _help or "--output-format" in _help:
            args.extend(["--output-format", "jsonl"])
        if not _help or "--dangerously-skip-permissions" in _help:
            args.append("--dangerously-skip-permissions")
        if session_id:
            args.extend(["--resume", session_id])
        return args

    def parse_output(self, line: str) -> dict | None:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get("type", "unknown")
        if kind == "run.started":
            if event.get("schema_version") != 1:
                return {"type": "error", "message": "Unsupported AtomCode JSONL schema version"}
            return {"type": "step_start", "sessionID": event.get("session_id") or ""}
        if kind in ("message.delta", "reasoning.delta"):
            text = event.get("text")
            if not isinstance(text, str) or not text:
                return None
            return {"type": "text" if kind == "message.delta" else "reasoning", "text": text}
        if kind == "tool.started":
            arguments = event.get("arguments", "{}")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {"raw": arguments}
            if not isinstance(arguments, dict):
                arguments = {"raw": arguments}
            return {"type": "tool_use", "tool": event.get("name", ""), "input": arguments}
        if kind == "tool.completed":
            content = event.get("content") or ""
            return {"type": "tool_result", "text": ("Error: " if event.get("is_error") else "") + str(content)}
        if kind == "usage":
            usage = self._split_usage(event)
            return {"type": "step_finish", "tokens": usage["input"] + usage["output"] + usage["cache_read"],
                    "usage": usage}
        if kind in ("error", "run.failed"):
            return {"type": "error", "message": str(event.get("message") or kind),
                    "terminal": kind == "run.failed"}
        if kind == "turn.completed":
            if event.get("exit_code") != 0 or event.get("snapshot_error"):
                return {"type": "error", "terminal": True, "message": str(event.get("snapshot_error") or
                        f"AtomCode turn failed: {event.get('stop_reason')} (exit={event.get('exit_code')})"),
                        "usage_total": self._split_usage(event)}
            # Totals repeat usage events; do not count them twice.
            # usage_total 仅作兜底：每轮 usage 事件缺失时才计入（如进程被提前终止）。
            return {"type": "other", "raw": kind, "terminal": True,
                    "usage_total": self._split_usage(event)}
        return {"type": "other", "raw": kind}

    @staticmethod
    def _split_usage(event: dict) -> dict:
        """把 atomcode 的 token 字段拆成看板口径（input/output/cache_read）。

        kernel 契约：prompt 是总输入，cached 是其中的缓存命中子集——两者不可相加。
        """
        def number(key):
            try:
                return max(0, int(event.get(key) or 0))
            except (ValueError, TypeError):
                return 0
        prompt = number("prompt_tokens")
        cached = min(prompt, number("cached_tokens"))
        return {"input": prompt - cached, "output": number("completion_tokens"),
                "cache_read": cached}

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        from src.kernel.config import SKILLS_DIR
        return {**os.environ, "DEVOPS_SKILLS_DIR": SKILLS_DIR}
