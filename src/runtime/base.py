"""Agent Runtime 抽象基类"""
import json


class AgentRuntime:
    """Agent Runtime 基类，所有执行引擎实现需继承此类。"""
    name = ""
    binary = ""
    prompt_via_stdin = True  # issue #351: 默认 stdin 传 prompt，规避 argv 经 cmd 壳截断多行提示词
    _serve_port = 0  # codearts 方案E2: serve 端口（pre_exec 设置，build_args 读取）

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        raise NotImplementedError

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

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        return {}

    def pre_exec(self, cwd: str, env: dict) -> dict:
        """_run_once 启动子进程前的钩子（默认无操作）。
        codearts 方案E2 用此钩子启动 per-task codearts serve，返回 {port, proc, env}。
        """
        return {"port": 0, "proc": None, "env": env}
