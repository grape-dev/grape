"""CodeArts 后端实现"""
import os
from src.config import GITCODE_API_TOKEN, CODEARTS_CLI_AK, CODEARTS_CLI_SK
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
        if CODEARTS_CLI_AK:
            env["CODEARTS_CLI_AK"] = CODEARTS_CLI_AK
        if CODEARTS_CLI_SK:
            env["CODEARTS_CLI_SK"] = CODEARTS_CLI_SK
        return env
