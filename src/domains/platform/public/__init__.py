"""platform 域对外契约（进程内 import）。

与 mgmt/runtime/insight public 的约定一致：
- 只暴露**函数**（`check_lib_query_names` 只按 `def` 登记，常量再导出会被判未登记）；
- 参数/返回值 JSON 友好，不传游标、不返回行对象；
- 转发体一律**函数内 import**：`platform.importer` 在函数体里反向取 `mgmt.public`
  （`set_meta_value`），顶层 import 会让 `platform.public -> platform.importer ->
  mgmt.public` 成为模块级边，加载顺序敏感处易成环。

当前承载导入状态判据（`is_table_backed`）与导入纪元推进（`bump_epoch`）两个原语：
mgmt/runtime 侧原先直接 import `src.domains.platform.importer`（平台对象模型的内部模块），
收敛到本模块后调用点只改模块路径，函数名与语义不变。
"""
from __future__ import annotations


def is_table_backed() -> bool:
    """导入已完成、平台对象可纯表读取时为 True；否则调用方必须回落旧存储。

    内部实现对 `platform.importer.is_table_backed` 做调用期解析，
    因此测试对 `importer.is_table_backed` 的 monkeypatch 依然生效。
    """
    from src.domains.platform.importer import is_table_backed as _impl

    return _impl()


def bump_epoch() -> int:
    """平台对象表写入后推进导入纪元，返回新纪元号。"""
    from src.domains.platform.importer import bump_epoch as _impl

    return _impl()


# --- 能力视图（platform.capabilities）--------------------------------------
# mgmt 侧（admin/repos/repo_manager）原先直接 import capabilities 内部模块，
# 现收敛到本模块。转发体保持函数内 import，调用点函数名不变。


def hosting_providers() -> frozenset:
    """可作为托管方的平台集合。"""
    from src.domains.platform.capabilities import hosting_providers as _impl

    return _impl()


def known_providers() -> set:
    """所有已知平台 provider 集合。"""
    from src.domains.platform.capabilities import known_providers as _impl

    return _impl()


def platform_capabilities(provider: str) -> dict:
    """单个平台的能力描述字典。"""
    from src.domains.platform.capabilities import platform_capabilities as _impl

    return _impl(provider)


def all_capabilities(providers=None) -> dict:
    """全平台能力汇总；providers 为空时取默认集合。"""
    from src.domains.platform.capabilities import all_capabilities as _impl

    return _impl(providers)


def user_platform_view(user_id: str) -> dict:
    """按用户维度汇总的平台视图。"""
    from src.domains.platform.capabilities import user_platform_view as _impl

    return _impl(user_id)


# --- 关系写入钩子（platform.hooks）-----------------------------------------
# mgmt 侧（routes/auth、routes/grape_auth、routes/settings、user/db、
# admin/user_repo_platform_link_routes）原先直接 import 内部模块 `platform.hooks`，
# 现收敛到本模块。转发体保持函数内 import：调用期才解析 `hooks.<name>`，
# 于是测试对 `platform.hooks.<name>` 的 monkeypatch 依然生效。
# 全部钩子本身 fail-safe（内部 try/except 只告警），这里不再加壳，语义不变。


def on_oauth_login(user_id: str, oauth_provider: str) -> None:
    """OAuth 登录/绑定成功 → 建立用户↔平台仓库关系。"""
    from src.domains.platform.hooks import on_oauth_login as _impl

    return _impl(user_id, oauth_provider)


def on_oauth_unbound(user_id: str, oauth_provider: str) -> None:
    """解绑渠道 → 无其它来源时撤销用户↔平台仓库关系。"""
    from src.domains.platform.hooks import on_oauth_unbound as _impl

    return _impl(user_id, oauth_provider)


def on_pat_saved(user_id: str, repo_provider: str) -> None:
    """PAT 保存 → 建立用户↔平台仓库关系。"""
    from src.domains.platform.hooks import on_pat_saved as _impl

    return _impl(user_id, repo_provider)


def on_pat_deleted(user_id: str, repo_provider: str) -> None:
    """PAT 删除 → 无其它来源时撤销用户↔平台仓库关系。"""
    from src.domains.platform.hooks import on_pat_deleted as _impl

    return _impl(user_id, repo_provider)


def on_platform_disabled(user_id: str, repo_provider: str) -> None:
    """用户禁用该平台 → 写 disabled_by_user 关系档位。"""
    from src.domains.platform.hooks import on_platform_disabled as _impl

    return _impl(user_id, repo_provider)


def is_platform_visible(provider: str) -> bool:
    """该平台是否对**用户面**可见（真源 = 平台行 enabled，fail-open）。"""
    from src.domains.platform.visibility import is_platform_visible as _impl

    return _impl(provider)


# --- 平台目录（platform.registry）-----------------------------------------
# mgmt 侧（routes/auth、routes/settings）原先直接 import registry 内部模块；
# 两者都只读、返回 JSON 友好结构，转发即可，调用点函数名不变。


def platform_descriptors() -> "list[dict]":
    """系统支持的仓库平台描述（表优先，卡兜底；空列表也是答案）。"""
    from src.domains.platform.registry import platform_descriptors as _impl

    return _impl()


def supported_platforms() -> "list[str]":
    """系统支持的仓库平台 id 列表。"""
    from src.domains.platform.registry import supported_platforms as _impl

    return _impl()


# --- 管理面 → 平台对象表同步（platform.admin_sync）------------------------
# mgmt/admin/config_store 原先直接 import admin_sync 内部模块；幂等、失败不阻断。


def sync_from_admin_payload(payload: dict) -> None:
    """把管理面扁平 payload 同步进平台对象表（幂等；失败不阻断）。"""
    from src.domains.platform.admin_sync import sync_from_admin_payload as _impl

    return _impl(payload)
