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
        # 重定向 bun JIT 编译缓存（libopentui .so）到磁盘，避免 /tmp tmpfs 被大量 .so 撑满。
        # bun 按 cwd 生成不同 hash 的 .so（每个 4.4MB），grape 每个 task cwd 不同 → 不清理会积累数 G。
        # BUN_RUNTIME_TRANSPILER_CACHE_PATH 控制 bun transpiler .pile 缓存；
        # JSC_diskCachePath 控制 JavaScriptCore 的 native .so JIT 缓存。
        # 两者组合实测 /tmp 零 .so 写入。
        cache_dir = "/var/cache/bun-compile"
        os.makedirs(cache_dir, exist_ok=True)
        env["BUN_RUNTIME_TRANSPILER_CACHE_PATH"] = cache_dir
        env["JSC_diskCachePath"] = cache_dir
        return env
