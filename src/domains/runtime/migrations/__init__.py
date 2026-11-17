"""runtime 域迁移实现（注册接线仍在 src/api/db_setup.py，见 ``register_migration``）。

Issue #3055 的 B 项属 runtime 域（issue / chat_conversations 归 runtime），实现放在
本域名下，db_setup.py 只保留一行 ``register_migration(...)`` 与薄适配——避免把一个
9000+ 行的启动模块继续堆大。
"""