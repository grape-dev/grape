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
    # 启用条件 = 该域配置了自有 DSN（三库独立）。历史开关（GRAPE_<D>_READ_VIA_LIB）
    # 不再作为唯一闸门：开关打开但没 DSN 时无法独立连接，仍返回 None；反之配了 DSN
    # 就说明已进入独立拓扑，读路径必须走 owner 域（否则命中"无视图 → 1146"）。
    try:
        from src.kernel import db_registry

        if not db_registry.uses_own_dsn(domain):
            if os.environ.get(env_flag) == "1":
                import logging

                logging.getLogger(__name__).warning(
                    f"{env_flag}=1 但 {domain} 未配置自有 DSN，读路径仍走本地 SQL")
            return None
        mod = __import__(f"src.domains.{domain}.public", fromlist=["*"])
        avail = getattr(mod, "available", None)
        if callable(avail) and not avail():
            return None
        return mod
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(f"{domain} lib unavailable: {exc}")
        return None


def mgmt_lib(foreign_only: bool = False) -> ModuleType | None:
    """mgmt 域 lib。`foreign_only=True` 时，**当前进程本身就是 mgmt 域则返回 None**。

    为什么写路径要传 foreign_only：mgmt 进程也挂载了 runtime 路由
    （`grape_server.py` 里 include 了 events/chat/pipeline/runtime），同一段代码
    可能在两个进程里执行。若在 mgmt 进程里也走 lib，写会落到**独立连接**（autocommit），
    失去调用方事务的回滚能力；同域写必须留在本地事务里。
    """
    lib = _lib("mgmt", "GRAPE_MGMT_READ_VIA_LIB")
    if lib is not None and foreign_only:
        from src.kernel import db_registry

        if db_registry.current_domain() == "mgmt":
            return None
    return lib


def runtime_lib(foreign_only: bool = False) -> ModuleType | None:
    lib = _lib("runtime", "GRAPE_RUNTIME_READ_VIA_LIB")
    if lib is not None and foreign_only:
        from src.kernel import db_registry

        if db_registry.current_domain() == "runtime":
            return None
    return lib


def insight_lib() -> ModuleType | None:
    return _lib("insight", "GRAPE_INSIGHT_READ_VIA_LIB")

def mgmt_write_lib(foreign_only: bool = True) -> ModuleType | None:
    """W2 写通道句柄：只要求 mgmt 自有 DSN（`GRAPE_DSN_MGMT` + `GRAPE_DOMAIN_LIB=1`）。

    与读句柄的区别：**不受按域读开关**（`GRAPE_MGMT_READ_VIA_LIB`）影响——读开关管
    灰度读路径，写通道按方案 §3.5 独立启用；`foreign_only=True` 时 mgmt 进程内返回 None
    （同域写必须留在本地事务，不走独立连接）。
    """
    try:
        from src.kernel import db_registry

        if not db_registry.uses_own_dsn("mgmt"):
            return None
        if foreign_only and db_registry.current_domain() == "mgmt":
            return None
        mod = __import__("src.domains.mgmt.public", fromlist=["*"])
        avail = getattr(mod, "available", None)
        if callable(avail) and not avail():
            return None
        return mod
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(f"mgmt write lib unavailable: {exc}")
        return None


def owner_lib(domain: str, foreign_only: bool = True) -> ModuleType | None:
    """跨域**读写通道**句柄：只要求该域自有 DSN（`GRAPE_DSN_<D>` + `GRAPE_DOMAIN_LIB=1`）。

    与 `*_lib()` 读句柄的区别：**不受按域读开关影响**——读开关
    （`GRAPE_<D>_READ_VIA_LIB`）管灰度读路径；W2 写通道与 P3.5 接口化读按方案独立启用。
    `foreign_only=True` 时当前进程本身就是该域则返回 None（同域读写留本地事务）。
    """
    if domain not in ("mgmt", "runtime", "insight"):
        raise ValueError(f"unknown domain: {domain}")
    try:
        from src.kernel import db_registry

        if not db_registry.uses_own_dsn(domain):
            return None
        if foreign_only and db_registry.current_domain() == domain:
            return None
        mod = __import__(f"src.domains.{domain}.public", fromlist=["*"])
        avail = getattr(mod, "available", None)
        if callable(avail) and not avail():
            return None
        return mod
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(f"{domain} owner lib unavailable: {exc}")
        return None


def switch_on(env_flag: str, domain: str) -> bool:
    """历史灰度开关的统一判定：显式 `=1`，**或** 该域已配自有 DSN（独立拓扑）。

    三库独立后不存在跨域视图，继续用"未开开关"当默认会让读路径 1146；
    因此 DSN 存在即视为开启。单库/视图期（无 DSN）保持原行为。
    """
    if os.environ.get(env_flag) == "1":
        return True
    try:
        from src.kernel import db_registry

        return db_registry.uses_own_dsn(domain)
    except Exception:  # noqa: BLE001
        return False
