"""Opencode 后端实现"""
import os
from src.config import SKILLS_DIR
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

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        from src.config import get_account
        acc = get_account(account)
        env = os.environ.copy()
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        if acc.get("gitcode_api_token"):
            env["GITCODE_API_TOKEN"] = acc["gitcode_api_token"]
        return env
