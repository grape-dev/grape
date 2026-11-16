"""Agent Runtime 抽象基类"""
import json
from contextlib import contextmanager


class AgentRuntime:
    """Agent Runtime 基类，所有执行引擎实现需继承此类。"""
    name = ""
    binary = ""
    text_separator = "\n"
    prompt_via_stdin = True  # issue #351: 默认 stdin 传 prompt，规避 argv 经 cmd 壳截断多行提示词
    _serve_port = 0  # codearts 方案E2: serve 端口（pre_exec 设置，build_args 读取）

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        raise NotImplementedError

    @contextmanager
    def prepare_prompt(self, prompt: str, cwd: str):
        """Own invocation-local prompt resources for the lifetime of execution."""
        yield prompt

    def parse_output(self, line: str) -> dict | None:
        """NDJSON 逐行解析，返回统一事件 dict 或 None"""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        et = event.get("type", "unknown")
        if et == "step_start":
            sid = event.get("sessionID", "")
            return {"type": "step_start", "sessionID": sid}
        elif et == "text":
            part = event.get("part", {})
            if isinstance(part, dict):
                text = part.get("text", "")
                ptype = part.get("type", "")
                if text and ptype in ("text", "reasoning"):
                    return {"type": ptype, "text": text, "sessionID": event.get("sessionID", "")}
        elif et == "reasoning":
            part = event.get("part", {})
            if isinstance(part, dict):
                text = part.get("text", "")
                if text:
                    return {"type": "reasoning", "text": text}
        elif et == "tool_use":
            part = event.get("part", {})
            if isinstance(part, dict):
                tool = part.get("tool", "")
                inp = part.get("state", {}).get("input", {})
                return {"type": "tool_use", "tool": tool, "input": inp}
        elif et == "step_finish":
            tokens = event.get("part", {}).get("tokens", {})
            return {"type": "step_finish", "tokens": tokens.get("total", 0)}
        elif et == "error":
            err = event.get("error", {}).get("data", {}).get("message", str(event))
            return {"type": "error", "message": err}
        return {"type": "other", "raw": et}

    def get_env(self, account: str = "", user_id: str = "") -> dict:
        return {}

    def pre_exec(self, cwd: str, env: dict) -> dict:
        """_run_once 启动子进程前的钩子（默认无操作）。
        codearts 方案E2 用此钩子启动 per-task codearts serve，返回 {port, proc, env}。
        """
        return {"port": 0, "proc": None, "env": env}

    # ===== [沙箱迁移 i7 漏移植恢复 2026-11-11] Protocol 默认实现 =====
    # （backend_protocol.py，子类按需 override）。此前只移植了 executor 消费端
    # （isinstance(backend, CloudBackendProvider) → backend.cloud_backend() 分叉），
    # 引擎侧供给端整段丢失——HermesRuntime 不满足 CloudBackendProvider，云沙箱
    # hermes 永远走 CLI fallback（chat -q → exit 127，VOD 沙箱任务实测）。
    # 与 i7 src/runtime/base.py 同款；hermes 的 override 见 engine/hermes.py。

    @property
    def protocol(self) -> str:
        """执行协议：'acp'（JSON-RPC 握手）或 'ndjson'（CLI --format json）。默认 NDJSON。"""
        return "ndjson"

    def cloud_backend(self) -> "object":
        """返回云沙箱路径用的 backend（满足 AgentBackend 协议）。默认返回 self。

        hermes 等云路径协议不同的引擎 override 返回 ACP backend
        （HermesACPBackend——满足 AgentBackend 协议但非 AgentRuntime 子类，
        Pyright 协议多态按运行时 isinstance 判定，返回类型放宽为协议形态）。
        """
        return self

    def pty_wrapper_name(self) -> str | None:
        """PTY wrapper 脚本文件名。None = 不走 PTY wrapper。默认 None。"""
        return None
