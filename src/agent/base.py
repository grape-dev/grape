"""Agent 后端抽象基类"""
import json


class AgentBackend:
    """Agent 后端基类，所有后端实现需继承此类。"""
    name = ""
    binary = ""
    prompt_via_stdin = False

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

    def get_env(self) -> dict:
        return {}
