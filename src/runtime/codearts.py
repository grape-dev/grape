"""CodeArts Runtime 实现（华为云 CodeArts CLI 执行引擎）。

方案 G: per-task 独立数据目录隔离权限
  通过 KERNEL_DATA_DIR 指向 per-task 临时目录，预写 global.json (write/edit=allow)
  + sandbox/config.json (network_policy=allow_all)，实现 per-process 权限隔离。
  临时目录在 _execute_task (agent.py) 中创建，通过 env_extra 覆盖 get_env 的默认值。

调用形态：
    codearts run --format json --auto [--model <model>] [--session <sid>] <prompt>

--auto: 设置 bash_mode=always_allow，解决 bash 权限
global.json write=allow: 不触发 permission.asked，解决 write/edit 权限
sandbox/config.json allow_all: bash 命令中的网络请求不被拦截

输出协议：NDJSON 事件流，与 opencode 完全兼容（parse_output 复用基类解析）。
"""
import os
from .base import AgentRuntime

# 预写的权限配置：write/edit/external_directory_write=allow
_ALLOW_PERMS = [
    {"permission": "browser", "pattern": "*", "action": "ask"},
    {"permission": "edit", "pattern": "*", "action": "allow"},
    {"permission": "write", "pattern": "*", "action": "allow"},
    {"permission": "webfetch", "pattern": "*", "action": "ask"},
    {"permission": "websearch", "pattern": "*", "action": "allow"},
    {"permission": "external_directory_write", "pattern": "*", "action": "allow"},
    {"permission": "external_directory_read", "pattern": "*", "action": "allow"},
    {"permission": "dotfile", "pattern": "*", "action": "ask"},
]


class CodeArtsRuntime(AgentRuntime):
    name = "codearts"
    binary = "codearts"
    prompt_via_stdin = False        # 命令行参数传 prompt（stdin 触发沙箱网络封锁）。
                                    # issue #351: Windows .cmd 壳截断风险由 exe 优先解析
                                    # + executor 启动前 WARN 兜底，不改 stdin（避免网络回归）。

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        """构建 codearts run 参数。

        --auto: 设置 bash_mode=always_allow，解决 bash 工具权限。
        write/edit 权限通过 global.json (action=allow) 解决，不触发 permission.asked。
        session_id 当前未启用——codearts 有独立 KERNEL_DATA_DIR（per-task 临时目录），
        session 跨 task 不可见；pipeline 上下文传递靠 prompt_builder 注入前序结论。
        """
        args = ["run", "--format", "json", "--auto"]
        # 模型名以 worker 本机 codearts_cli.json 的 model 为准（与 opencode/hermes
        # 引擎同逻辑：模型由本机配置决定）；仅当显式设置 CODEARTS_MODEL 环境变量
        # 时才传 --model 覆盖。不设默认硬编码前缀——
        # 曾写死 modelarts/deepseek-v4-flash 导致 Model not found（2026-08-20 seq#1390）。
        model = os.environ.get("CODEARTS_MODEL")
        if model:
            args.extend(["--model", model])
        # prompt 作为 positional argument（不走 stdin，避免沙箱网络封锁）
        if prompt:
            args.append(prompt)
        return args

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        from src.runtime.os_adapter import home_dir
        home = home_dir()
        env = os.environ.copy()
        # codearts 启动时检查 AK/SK（华为云认证），不给就拒。
        # 但我们走自定义 provider（opengw），给 dummy 值绕过检查即可。
        env["CODEARTS_CLI_AK"] = env.get("CODEARTS_CLI_AK", "dummy")
        env["CODEARTS_CLI_SK"] = env.get("CODEARTS_CLI_SK", "dummy")
        # 数据目录：默认指向宿主机 ~/.codeartsdoer/cli-data。
        # 方案 G: _execute_task 中通过 env_extra 覆盖为 per-task 临时目录，
        # 预写 global.json + sandbox/config.json 实现权限隔离。
        # 用 setdefault 让 env_extra 中的 KERNEL_DATA_DIR 能覆盖此值。
        env.setdefault("SCENARIO", "codeartsdoer")
        env.setdefault("KERNEL_DATA_DIR", os.path.join(home, ".codeartsdoer", "cli-data"))
        env.setdefault("KERNEL_CONFIG_DIR", os.path.join(home, ".codeartsdoer"))
        # 指向自定义 provider 配置文件（含 opengw 的 baseURL/apiKey/models）
        env.setdefault("OPENCODE_CONFIG",
                       os.path.join(home, ".codeartsdoer", "codearts_cli.json"))
        env.setdefault("OPENCODE_CONFIG_FILE", "codearts_cli.json,codearts_cli.jsonc")
        # 启动脚本环境变量：OPENCODE_MODE=tui 影响 network-interceptor 策略，
        # 缺此变量会导致外部 API 请求被拦截。
        env.setdefault("OPENCODE_MODE", "tui")
        env.setdefault("PLUGIN_ENV", "hc")
        env.setdefault("OPENCODE_CHANNEL", "latest")
        # 禁止 codearts 拉华为云 xCenter 模型列表（我们用本地缓存）
        env.setdefault("OPENCODE_DISABLE_MODELS_FETCH", "1")
        # 禁用自动更新
        env.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "true")
        env.setdefault("OPENCODE_ALWAYS_NOTIFY_UPDATE", "false")
        # 信任自签名证书（华为云内网场景需要）
        env.setdefault("NODE_TLS_REJECT_UNAUTHORIZED", "0")
        from src.config import SKILLS_DIR
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        # self-check: 关键变量缺失时打 warning，便于 worker journalctl 定位
        _required = ["CODEARTS_CLI_AK", "CODEARTS_CLI_SK", "KERNEL_DATA_DIR",
                     "OPENCODE_CONFIG", "OPENCODE_MODE", "SCENARIO"]
        _missing = [k for k in _required if not env.get(k)]
        if _missing:
            from src.config import log as _log
            _log.warning(f"codearts env missing: {_missing}")
        return env
