"""Opencode Runtime 实现"""
import os
from src.config import SKILLS_DIR
from .base import AgentRuntime


class OpencodeRuntime(AgentRuntime):
    name = "opencode"
    binary = "opencode"
    # issue #351: prompt 经 stdin 传入（opencode run 无消息参数时从 stdin 读取），
    # 规避 Windows .cmd 壳把 argv 中的换行符当作命令分隔符截断多行提示词。
    prompt_via_stdin = True

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        args = ["run", "--format", "json", "--dangerously-skip-permissions"]
        if session_id:
            args.extend(["--session", session_id])
        # prompt 不作为位置参数传递（stdin 传输，见 prompt_via_stdin）
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
