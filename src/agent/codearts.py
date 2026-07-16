"""CodeArts 后端实现"""
import os
from .base import AgentBackend


class CodeArtsBackend(AgentBackend):
    name = "codearts"
    binary = "codearts"
    prompt_via_stdin = True

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        args = ["run", "--format", "json"]
        if session_id:
            args.extend(["--session", session_id])
        return args

    def get_env(self, account: str = "default") -> dict:
        from src.config import get_account
        acc = get_account(account)
        env = os.environ.copy()
        if acc.get("gitcode_api_token"):
            env["GITCODE_API_TOKEN"] = acc["gitcode_api_token"]
        if acc.get("CODEARTS_CLI_AK"):
            env["CODEARTS_CLI_AK"] = acc["CODEARTS_CLI_AK"]
        if acc.get("CODEARTS_CLI_SK"):
            env["CODEARTS_CLI_SK"] = acc["CODEARTS_CLI_SK"]
        return env
