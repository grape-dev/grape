"""跨域读的统一入口（业务代码用）：按开关返回对应域的 lib，不可用返回 None。

用法：
    from src.kernel.domain_read import mgmt_lib
    lib = mgmt_lib()
    if lib is not None:
        val = lib.get_repo_field(repo_key, "bot_username")
    else:
        <原 SQL 路径>

开关：GRAPE_MGMT_READ_VIA_LIB=1（另有 runtime_lib / insight_lib，开关同理）。
"""
from __future__ import annotations

import os
from types import ModuleType


def _lib(domain: str, env_flag: str) -> ModuleType | None:
    if os.environ.get(env_flag) != "1":
        return None
    try:
        mod = __import__(f"src.domains.{domain}.public", fromlist=["*"])
        avail = getattr(mod, "available", None)
        if callable(avail) and not avail():
            return None
        return mod
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(f"{domain} lib unavailable: {exc}")
        return None


def mgmt_lib() -> ModuleType | None:
    return _lib("mgmt", "GRAPE_MGMT_READ_VIA_LIB")


def runtime_lib() -> ModuleType | None:
    return _lib("runtime", "GRAPE_RUNTIME_READ_VIA_LIB")


def insight_lib() -> ModuleType | None:
    return _lib("insight", "GRAPE_INSIGHT_READ_VIA_LIB")