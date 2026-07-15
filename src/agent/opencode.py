"""Opencode 后端实现"""
import os
from src.config import SKILLS_DIR, GITCODE_API_TOKEN
from .base import AgentBackend


class OpencodeBackend(AgentBackend):
    name = "opencode"
    binary = "opencode"

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        args = ["run", "--format", "json", "--dangerously-skip-permissions"]
        if session_id:
            args.extend(["--session", session_id])
        args.append(prompt)
        return args

    def get_env(self) -> dict:
        env = os.environ.copy()
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        env["GITCODE_API_TOKEN"] = GITCODE_API_TOKEN
        return env
