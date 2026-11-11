"""持久化层（S78/D53）：身份/凭据/仓库/渠道四域 ORM 化骨架。

边界（需求文档 S78 行）：仅覆盖领域建模过的低频结构化域；高频事件流
（issue/events/task/pipeline/observability）保持 raw SQL。

架构约定（D53 定稿）：
- 桥接模式（JDBC 等价）：SQLAlchemy Dialect 承接 sqlite/mysql 方言差异，
  Repository 单实现双方言；mysql_compat 保留给存量裸 SQL 路径；
- UoW：用例级 session，Service 层唯一提交点；Repository 只 flush 不 commit；
- Repository 返回 DTO（dataclass），ORM 实体不出口（守卫固化）；
- 模型 = 权威 schema 声明；DDL 唯一出口 = 盖章迁移系统（禁 create_all、
  禁 Alembic autogenerate 绕过门禁——S64 教训）；漂移由 compare 守卫抓；
- 敏感模型 __repr__ 脱敏（token/secret 永不进日志/异常栈）；
- S91 收口：GRAPE_PERSISTENCE_ORM 开关已退役，Repository 层是唯一持久化路径。
"""

