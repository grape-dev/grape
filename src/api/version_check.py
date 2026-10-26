"""启动版本检查（Issue #455）。

主服务要求 Python >= 3.10（新式类型注解/语法依赖）。低于下限时在 import 任何业务模块
之前显式提示并退出，避免 3.9 环境下导入阶段即报语法错误、排查成本高。

run.py / main.py / start_server.py 均调用 ensure_python_version()。
"""

import os
import sys

_MIN_PYTHON = (3, 10)


def ensure_python_version(version_info=None, min_version=_MIN_PYTHON) -> None:
    """版本检查：低于下限输出显式指引并退出（不抛异常，直接 exit）。

    Args:
        version_info: 可注入的版本元组（测试用）；默认取 sys.version_info。
        min_version: 最低版本元组，默认 (3, 10)。
    """
    vi = version_info if version_info is not None else sys.version_info
    if vi >= min_version:
        return
    cur = f"{vi[0]}.{vi[1]}.{vi[2]}" if len(vi) >= 3 else f"{vi[0]}.{vi[1]}"
    req = ".".join(str(x) for x in min_version)
    sys.stderr.write(
        f"[ERROR] Grape 主服务要求 Python >= {req}，当前为 {cur}\n"
        "请升级 Python 后重试（推荐 3.12）。详见 docs/0-to-1-checklist.md 环境阶段。\n"
    )
    sys.exit(1)