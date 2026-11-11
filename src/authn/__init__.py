"""凭证 / 会话子包（Issue #2010）：token 解析与签发、会话连接管理。

命名使用 ``authn`` 而非 ``auth``：``src.auth`` 是 Issue #699 已删除的 legacy
兼容目录，``tests/arch/test_domain_boundaries.py`` 断言其必须不可导入。
"""
