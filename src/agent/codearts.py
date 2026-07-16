"""CodeArts 后端实现"""
import os
from src.config import GITCODE_API_TOKEN
from .base import AgentBackend


class CodeArtsBackend(AgentBackend):
    name = "codearts"
    binary = "codearts"

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        args = ["run", "--format", "json"]
        if session_id:
            args.extend(["--sessionID", session_id])
        args.append(prompt)
        return args

    def get_env(self) -> dict:
        env = os.environ.copy()
        env["GITCODE_API_TOKEN"] = GITCODE_API_TOKEN
        return env
