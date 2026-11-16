"""沙箱异常定义（零依赖）。"""


class SandboxUnavailableError(Exception):
    """沙箱不可用且 sandbox_mode 要求拒绝执行（fail-closed）。"""
    pass


class NetworkStartupError(Exception):
    """网络隔离组件启动失败。"""
    pass
