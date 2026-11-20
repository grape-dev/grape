# -*- coding: utf-8 -*-
"""whoami 必须走 runtime 域游标（分库 1054 守护）。

runtime_credentials 归 runtime 域；三库拓扑下 mgmt 进程库里只有旧视图/旧副本，
缺 cred_type/credential_uid（#646 后）。/api/runtime/whoami 用进程库读表会
1054 → 安装脚本第一步全部 500。
"""
import inspect


def test_whoami_uses_runtime_domain_cursor():
    from src.domains.runtime.routes import runtime as rt
    src = inspect.getsource(rt.runtime_whoami)
    assert '_domain_cursor("runtime")' in src, (
        "whoami 查 runtime_credentials 必须走 _domain_cursor(\"runtime\")"
        "（mgmt 进程库的同名视图无 cred_type/credential_uid → 1054）")
    assert "session_manager import db" not in src, (
        "whoami 不得用进程库连接读 runtime_credentials")
