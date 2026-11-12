"""声明式 Base——只声明映射，绝不 create_all（DDL 唯一出口 = 盖章迁移）。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """四域 ORM 模型基类。

    纪律（D53）：本 metadata 是 schema 的权威声明，但禁止调用
    Base.metadata.create_all——建表/建列唯一出口 = 盖章迁移系统
    （含 S64/S77 每启动补列口子）。漂移由对账守卫用例抓取。
    """
