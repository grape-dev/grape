"""Opencode Runtime 实现"""
import os
from src.config import SKILLS_DIR
from .base import AgentRuntime


class OpencodeRuntime(AgentRuntime):
    name = "opencode"
    binary = "opencode"

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        args = ["run", "--format", "json", "--dangerously-skip-permissions"]
        if session_id:
            args.extend(["--session", session_id])
        args.append(prompt)
        return args

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        env = os.environ.copy()
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        from src.runtime.os_adapter import bun_cache_dir
        cache_dir = bun_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        env["BUN_RUNTIME_TRANSPILER_CACHE_PATH"] = cache_dir
        env["JSC_diskCachePath"] = cache_dir
        return env
