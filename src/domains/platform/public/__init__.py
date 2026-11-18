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
