"""策略异常定义（零依赖）。"""


class TaskBlocked(Exception):
    """任务级拒绝——提示注入检测高危。不调 sandbox_manager。"""
    pass


class CommandBlocked(Exception):
    """命令级拒绝——危险命令检测。不调 sandbox.wrap。"""
    pass
