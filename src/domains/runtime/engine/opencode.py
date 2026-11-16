"""Opencode Runtime 实现"""

import os

from src.kernel.config import SKILLS_DIR

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

    # ===== [E-02 云沙箱 PTY 修复 2026-11-13] PTYAware 四方法 =====
    # 根因：CubeSandbox 的 commands.run 是同步无 stdin 通道的（ExecHandle.send_stdin
    # 为 pass 空实现）——prompt_via_stdin 引擎走非 PTY 路径 prompt 必丢（exit=1
    # "You must provide a message or a command"，实证 t_8fad23936b99）。
    # opencode_wrapper.py 本就按 PTY 路径设计（prompt 从 GRAPE_TASK_PROMPT env
    # 读、依赖 _ndjson_common、session 从 GRAPE_TASK_SESSION 读）——声明 PTY
    # 能力后云沙箱链即走 PTY+wrapper，本地链（Popen stdin）行为不变。
    def pty_wrapper_name(self) -> str | None:
        return "opencode_wrapper.py"

    def pty_use_ndjson_common(self) -> bool:
        return True  # wrapper import _ndjson_common（emit/emit_done/错误分流）

    def pty_needs_stdin_payload(self) -> bool:
        return False  # prompt 经 GRAPE_TASK_PROMPT env（executor PTY 分支注入）

    def pty_prepare_env(self, env: dict, session_id: str) -> None:
        if session_id:
            env["GRAPE_TASK_SESSION"] = session_id  # wrapper session 续聊（not found 重试同款）


    def get_env(self, account: str = "", user_id: str = "") -> dict:
        env = os.environ.copy()
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        import logging

        from src.domains.runtime.engine.os_adapter import bun_cache_dir, bun_cache_fallback_dir
        cache_dir = bun_cache_dir()
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as exc:
            # Issue #857: 缓存只影响性能，目录建不出来就降级到临时目录，别拖垮任务启动
            fallback = bun_cache_fallback_dir()
            logging.getLogger(__name__).warning(
                "bun cache dir %s unusable (%s), falling back to %s",
                cache_dir, exc, fallback,
            )
            cache_dir = fallback
            try:
                os.makedirs(cache_dir, exist_ok=True)
            except OSError:
                logging.getLogger(__name__).warning(
                    "bun cache fallback dir %s unusable, bun cache disabled", cache_dir
                )
                env.pop("BUN_RUNTIME_TRANSPILER_CACHE_PATH", None)
                env.pop("JSC_diskCachePath", None)
                return env
        env["BUN_RUNTIME_TRANSPILER_CACHE_PATH"] = cache_dir
        env["JSC_diskCachePath"] = cache_dir
        return env
