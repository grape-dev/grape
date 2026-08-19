"""CodeArts Runtime 实现（华为云 CodeArts CLI 执行引擎）。

基于 opencode 的华为云定制版本。

调用形态：
    codearts run --format json [--model <model>] [--session <sid>] <prompt>

注意：prompt 通过命令行参数传入（非 stdin），因为 codearts 沙箱模式
对 stdin 输入的网络策略为 deny_all，阻塞 LLM API 调用。
测试证实命令行参数方式正常。
输出协议：NDJSON 事件流，与 opencode 完全兼容（parse_output 复用基类解析）。
"""
import os
from .base import AgentRuntime


class CodeArtsRuntime(AgentRuntime):
    name = "codearts"
    binary = "codearts"
    prompt_via_stdin = False        # 命令行参数传 prompt（stdin 触发沙箱网络封锁）

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        """构建 codearts run 参数

        session_id 被忽略——codearts 有独立 KERNEL_DATA_DIR，其他引擎
        创建的 session 不可见；且 VOD/Issue 任务每次应从头开始，续会话无意义。
        """
        args = ["run", "--format", "json"]
        # 从环境变量读取模型，兜底默认
        model = os.environ.get("CODEARTS_MODEL", "modelarts/deepseek-v4-flash")
        if model:
            args.extend(["--model", model])
        # prompt 作为 positional argument（不走 stdin，避免沙箱网络封锁）
        if prompt:
            args.append(prompt)
        return args

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        home = os.path.expanduser("~")
        env = os.environ.copy()
        # codearts 启动时检查 AK/SK（华为云认证），不给就拒。
        # 但我们走自定义 provider（modelarts），给 dummy 值绕过检查即可。
        env["CODEARTS_CLI_AK"] = env.get("CODEARTS_CLI_AK", "dummy")
        env["CODEARTS_CLI_SK"] = env.get("CODEARTS_CLI_SK", "dummy")
        # 数据目录隔离：必须指向 codearts 自己的目录，否则和 opencode 的
        # SQLite DB 冲突（CREATE TABLE `workspace` already exists）。
        env.setdefault("SCENARIO", "codeartsdoer")
        env.setdefault("KERNEL_DATA_DIR", os.path.join(home, ".codeartsdoer", "cli-data"))
        env.setdefault("KERNEL_CONFIG_DIR", os.path.join(home, ".codeartsdoer"))
        # 指向自定义 provider 配置文件（含 modelarts 的 baseURL/apiKey/models）
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
        return env