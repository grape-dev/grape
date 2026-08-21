"""Hermes Runtime 实现（Nous Research Hermes Agent 执行引擎）。

调用形态（实测 hermes CLI）：
    hermes chat -q <prompt> --yolo -v [--continue] [--skills SKILLS]

v2 — 切换到 `chat -q -v` verbose 模式（替代旧 `-z` oneshot）：
- `-z` oneshot 只在全部执行完成后一次性输出最终文本，中间无任何 stdout 输出；
- `chat -q -v` 实时逐行输出完整过程（思考、API 请求/响应、工具调用/结果），
  配合 Grape 的 report_messages + fanout SSE 通道，前端实时日志面板可准实时展示。

输出协议（verbose 模式）：
  stdout/stderr 混合输出，每行一类事件，parse_output 按行模式匹配解析为结构化事件：
  - `[thinking] xxx`           → reasoning 事件
  - `Tool call: X with args: Y` → tool_use 事件
  - `Tool X completed in Ns`    → tool_finish 标记（tool_use 结果在下一行 Result:）
  - `Result: {...}`             → tool_result 事件
  - `API Request - Model: X`    → api_call 事件
  - `API Response received`     → api_response 事件
  - `Conversation completed`    → step_finish 事件
  - `session_id: xxx`           → session 标记
  - 其他非日志纯文本行           → text 事件（最终回复片段）

兼容旧版本（无 chat 子命令或 -v 不支持时回退 -z 模式）。
"""
import os
import re
import subprocess
import threading
import json as _json
from .base import AgentRuntime


class HermesRuntime(AgentRuntime):
    name = "hermes"
    binary = "hermes"

    def __init__(self):
        super().__init__()
        self._cli_ok: bool | None = None
        self._cli_probe_lock = threading.Lock()
        self._verbose_ok: bool | None = None
        self._verbose_probe_lock = threading.Lock()

    def _probe_cli_flag(self) -> bool:
        """探测当前 hermes 是否支持 --cli（一次性，带锁缓存）。

        老版本（如 lvjuntao 机器 v0.11.0，2026.4）无 --cli 参数，硬带会
        'unrecognized arguments: --cli' 直接 exit=2（远程 hermes 跑失败根因）。
        以 `hermes --help` 输出是否含 --cli 判断，比版本号阈值更直接。
        """
        with self._cli_probe_lock:
            if self._cli_ok is not None:
                return self._cli_ok
            ok = False
            try:
                res = subprocess.run([self.binary, "--help"],
                                     capture_output=True, text=True, timeout=10)
                ok = "--cli" in (res.stdout or "")
            except Exception:
                ok = False
            self._cli_ok = ok
            return ok

    def _probe_verbose_mode(self) -> bool:
        """探测 hermes chat -q 是否支持 -v（verbose 实时输出）。

        新版本 hermes chat -q -v 逐行输出思考/工具/API 过程；
        老版本可能不支持 -v 或无 chat 子命令 → 回退 -z oneshot 模式。
        """
        with self._verbose_probe_lock:
            if self._verbose_ok is not None:
                return self._verbose_ok
            ok = False
            try:
                res = subprocess.run([self.binary, "chat", "--help"],
                                     capture_output=True, text=True, timeout=10)
                ok = "-v" in (res.stdout or "") and "--verbose" in (res.stdout or "")
            except Exception:
                ok = False
            self._verbose_ok = ok
            return ok

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        verbose = self._probe_verbose_mode()
        if verbose:
            # v2: chat -q -v 模式，实时输出思考/工具/API 过程
            # verbose 级别由 HERMES_TOOL_PROGRESS_MODE env 控制（off→new→all→verbose）
            # 默认 new：只展示新消息和工具结果，不展示内部思考/API 细节，减少日志量
            args = ["chat", "-q", prompt, "--yolo", "-v"]
        else:
            # 旧版回退：-z oneshot 模式（只在完成时输出最终文本）
            args = ["-z", prompt, "--yolo"]
            if self._probe_cli_flag():
                args.append("--cli")
        if session_id:
            args.append("--continue")
        return args

    def parse_output(self, line: str) -> dict | None:
        """解析 hermes verbose 输出行为结构化事件。

        verbose 模式每行输出一类事件，按行模式匹配：
        - [thinking] xxx           → reasoning
        - Tool call: X with args   → tool_use
        - Tool X completed         → tool_finish
        - Result: {...}            → tool_result
        - API Request              → api_call
        - API Response received    → api_response
        - Conversation completed   → step_finish
        - session_id: xxx          → session 标记（不转发）
        - 最终回复纯文本行          → text

        旧版 -z 模式：每行 stdout 为最终回复文本，逐行转 text 事件。
        """
        line = line.rstrip("\n\r")
        if not line.strip():
            return None

        # ── verbose 模式行解析 ──

        # [thinking] xxx → reasoning
        m = re.search(r'\[thinking\]\s*(.+)', line)
        if m:
            return {"type": "reasoning", "text": m.group(1).strip(), "sessionID": ""}

        # Tool call: terminal with args: {"command": "..."} → tool_use
        m = re.search(r'Tool call:\s*(\w+)\s+with args:\s*(.+)', line)
        if m:
            tool_name = m.group(1).strip()
            args_str = m.group(2).strip().rstrip("...")
            try:
                args_dict = _json.loads(args_str)
            except Exception:
                args_dict = {"raw": args_str}
            return {"type": "tool_use", "tool": tool_name, "input": args_dict, "sessionID": ""}

        # Tool X completed in Ns → tool_finish
        m = re.search(r'Tool\s+(\w+)\s+completed in\s+([\d.]+)s', line)
        if m:
            return {"type": "tool_finish", "text": f"Tool {m.group(1)} completed ({m.group(2)}s)", "sessionID": ""}

        # Result: {...} → tool_result
        m = re.match(r'\s*Result:\s*(.+)', line)
        if m:
            return {"type": "tool_result", "text": m.group(1).strip(), "sessionID": ""}

        # API Request - Model: xxx → api_call
        if "API Request - Model:" in line:
            m = re.search(r'API Request - Model:\s*([\w\-./]+)', line)
            model = m.group(1) if m else ""
            return {"type": "api_call", "text": f"API call → {model}", "sessionID": ""}

        # API Response received → api_response
        if "API Response received" in line:
            return {"type": "api_response", "text": "API response received", "sessionID": ""}

        # Conversation completed → step_finish (终态信号)
        if "Conversation completed" in line:
            return {"type": "step_finish", "text": line.strip(), "sessionID": ""}

        # session_id: xxx → 提取 session，不转发为事件
        m = re.match(r'session_id:\s*(\S+)', line)
        if m:
            return {"type": "session", "text": "", "sessionID": m.group(1)}

        # ── 过滤日志框架行（带时间戳的 DEBUG/INFO/WARNING 行）──
        # 格式: HH:MM:SS - module.level - MESSAGE
        if re.match(r'\d{2}:\d{2}:\d{2}\s+-\s+\S+\s+-\s+(DEBUG|INFO|WARNING|ERROR)', line):
            # 提取 INFO 级别中的有用信息作为 step_start / text
            m = re.search(r'- INFO\s+-\s+(.+)', line)
            if m:
                msg = m.group(1).strip()
                # conversation turn → step_start
                if "conversation turn:" in msg:
                    return {"type": "step_start", "text": msg, "sessionID": ""}
                # 其他 INFO 消息转成 text（如 "Conversation completed after..." 等）
                # 但不转发纯 DEBUG 噪音
                return None  # INFO 日志行不转发，避免噪音
            return None  # DEBUG/WARNING 行不转发

        # 过滤 "📞 Tool N: xxx" 行（Rich 工具预览，与日志 Tool call 重复）
        if line.strip().startswith("📞 Tool"):
            return None
        # 过滤 "Args: {" 行（Rich 工具参数预览）
        if line.strip().startswith("Args:"):
            return None
        # 过滤 "┊" 开头的 Rich 装饰行（终端预览框）
        if "┊" in line:
            return None
        # 过滤 JSON 片段行（Rich 工具参数预览的续行，如 "command": "..." 或 } ）
        stripped_line = line.strip()
        if stripped_line.startswith('"') and '":' in stripped_line:
            return None
        if stripped_line == "}":
            return None
        # 过滤 Rich box 边框行
        if re.match(r'^[╭╰╰─│║┌└├┤┐┘┬┴┼]', line):
            return None
        # Rich tool preview lines (💻, ✅, 🔧, 💬, 🎉, 🤖, 🔗, 🔑, 🔄, 📊, ⚠️, 🛠️)
        # 这些是装饰性的，但部分含有用信息——提取关键几类
        if "✅ Tool" in line and "completed" in line:
            # "✅ Tool 1 completed in 0.02s" — 与日志行重复，转 tool_finish
            return {"type": "tool_finish", "text": line.strip(), "sessionID": ""}
        if "💬 Starting conversation:" in line:
            m = re.search(r"Starting conversation:\s*'(.+)'", line)
            return {"type": "step_start", "text": f"Conversation started: {m.group(1)[:80]}" if m else line.strip(), "sessionID": ""}
        if "🎉 Conversation completed" in line:
            return {"type": "step_finish", "text": "Conversation completed", "sessionID": ""}

        # 过滤纯装饰/emoji行（初始化横幅）
        if re.match(r'^[🤖🔗🔑🔄📊⚠️🛠️✅]', line) and not line.strip().startswith("✅ Tool"):
            return None
        # 过滤 "Enabled toolset" 行
        if "Enabled toolset" in line:
            return None
        # 过滤 "Loaded N tools" 行
        if re.match(r'^🛠️.*Loaded \d+ tools', line):
            return None
        # 过滤 "Final tool selection" 行
        if "Final tool selection" in line:
            return None
        # 过滤 "Context limit" 行
        if "Context limit:" in line:
            return None
        # 过滤 "Enabled toolsets" 行
        if "Enabled toolsets:" in line:
            return None
        # 过滤 "Some tools may not work" 行
        if "Some tools may not work" in line:
            return None
        # 过滤 "Initializing agent" 行
        if "Initializing agent" in line:
            return None
        # 过滤 "Query:" 行（开头）
        if line.startswith("Query:"):
            return None
        # 过滤 "Resume this session" 行
        if "Resume this session" in line:
            return None
        # 过滤 "hermes --resume" 行
        if line.strip().startswith("hermes --resume"):
            return None
        # 过滤 Session/Duration/Messages 统计行
        if re.match(r'^(Session|Duration|Messages):', line):
            return None
        # 过滤 "AI Agent initialized" 行
        if "AI Agent initialized" in line:
            return None
        # 过滤 "Using" 行 (base URL / API key / fallback)
        if re.match(r'^[🔗🔑🔄]', line) or line.startswith("Using "):
            return None
        # 过滤 ──── 分隔线
        if re.match(r'^─+$', line):
            return None
        # 过滤 "fallback_google" 行
        if "fallback_google" in line:
            return None

        # ── 剩余非日志纯文本行 → text 事件（最终回复片段）──
        # 旧版 -z 模式：每行都是最终回复文本
        # verbose 模式：最终回复在 Conversation completed 之后，以缩进纯文本行出现
        stripped = line.strip()
        if stripped:
            return {"type": "text", "text": stripped, "sessionID": ""}

        return None

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        env = os.environ.copy()
        from src.config import SKILLS_DIR
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        # verbose 模式下禁用 Rich 彩色输出（ANSI 转义序列干扰 parse_output）
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        # 工具进度级别：new = 只展示新消息/工具结果，不展示内部思考/API 细节
        # 减少子进程输出量 → 减少 state.db 写入 → 降低并发争锁概率
        # 级别：off(无) → new(新消息) → all(含思考) → verbose(全部含API)
        env["HERMES_TOOL_PROGRESS_MODE"] = os.environ.get("HERMES_TOOL_PROGRESS_MODE", "new")
        return env
