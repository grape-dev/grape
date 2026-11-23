"""PluginRunner 公开错误类型。"""


class PluginRunnerError(Exception):
    """Runner 公开错误基类。"""


class ManifestError(PluginRunnerError):
    """plugin.json 缺失或不可读。"""


class UnknownCommand(PluginRunnerError):
    """command 不在 manifest.commands 中。"""


class EngineNotSupported(PluginRunnerError):
    """引擎与目标 adapter 的 compat.engines 不兼容（D7 强声明，不做动态回落）。"""


class MissingCredential(PluginRunnerError):
    """adapter 声明的 required 凭据在调用时未提供。"""


class VenvError(PluginRunnerError):
    """插件 venv 创建/依赖装配失败。"""


class TransportError(PluginRunnerError):
    """传输层失败（连接中断/协议非法等）；Timeout 归一为 CallTimeout，其余归一本类。"""


class CallTimeout(PluginRunnerError):
    """单次调用超出墙钟超时（子进程/请求已终止）。"""
