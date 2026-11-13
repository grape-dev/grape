"""对账三件套（D53 验收）：schema 漂移 / 字段值 / 返回形态。

- schema：reflect 实际表 vs 模型 metadata（列名/可空/主键集合 + 类型
  归一化比对——sqlite 宽松形态 MEDIUMTEXT≈TEXT）；
- 字段值：裸 SQL 写入 → ORM 读出同值（防映射漂移）；
- 返回形态：静态扫描 Repository 公开方法返回注解不含 ORM 实体。
"""
from __future__ import annotations

import re

from sqlalchemy import MetaData, Table, inspect

from src.kernel.persistence.base import Base
from src.kernel.persistence import models as _models  # noqa: F401 —— 注册模型到 metadata

_MAPPED_TABLES = ("grape_accounts", "external_identities", "user_credentials",
                  "managed_repos", "grape_t_repo_credentials")


def _norm_type(t: str) -> str:
    t = (t or "").upper().strip()
    t = {"MEDIUMTEXT": "TEXT", "LONGTEXT": "TEXT", "VARCHAR": "TEXT"}.get(t, t)
    t = re.sub(r"VARCHAR\(\d+\)", "TEXT", t)
    t = re.sub(r"\(.*\)", "", t) if t.startswith("TEXT") else t
    return t


def schema_drift(engine, dynamic_columns: "set[str] | list[str] | None" = None) -> "list[str]":
    """模型 metadata vs 实际表结构：列名集合/主键/可空性/归一类型漂移清单。

    ``dynamic_columns``：managed_repos 上「卡派生、设计上只存在于 DB」的列
    （如 bot_gitee_token），由调用方从平台卡注册表派生后注入；kernel 层不感知域模型。
    """
    assert Base.metadata.tables, "模型 metadata 为空——models 模块未注册（静默空集反模式）"
    drift = []
    insp = inspect(engine)
    actual = MetaData()
    actual.reflect(bind=engine, only=[t for t in _MAPPED_TABLES
                                      if insp.has_table(t)])
    # 卡派生动态列由调用方注入（reconcile 属 kernel 层，不得反向 import src.domains —— R1）
    dynamic = set(dynamic_columns or ())
    for tname in _MAPPED_TABLES:
        model_t = Base.metadata.tables.get(tname)
        actual_t = actual.tables.get(tname)
        if model_t is None or actual_t is None:
            drift.append(f"{tname}: model or table missing")
            continue
        mcols = {c.name: c for c in model_t.columns}
        acols = {c.name: c for c in actual_t.columns}
        for name in mcols.keys() - acols.keys():
            drift.append(f"{tname}.{name}: in model, missing in DB")
        for name in acols.keys() - mcols.keys():
            if tname == "managed_repos" and name in dynamic:
                continue  # 卡派生动态列：设计上在 DB 不在模型（F4/D106）
            drift.append(f"{tname}.{name}: in DB, missing in model "
                         f"(需盖章迁移同步模型或补列口子补表)")
        for name in mcols.keys() & acols.keys():
            mc, ac = mcols[name], acols[name]
            if bool(mc.nullable) != bool(ac.nullable) and name not in ("repo_key",) \
               and not mc.primary_key:  # sqlite 自增主键方言性报 nullable=True
                drift.append(f"{tname}.{name}: nullable mismatch "
                             f"model={mc.nullable} db={ac.nullable}")
            if _norm_type(str(mc.type)) != _norm_type(str(ac.type)) and                not (_norm_type(str(mc.type)) == "INTEGER" and _norm_type(str(ac.type)) in ("INTEGER", "BIGINT")):
                drift.append(f"{tname}.{name}: type mismatch "
                             f"model={_norm_type(str(mc.type))} db={_norm_type(str(ac.type))}")
    return drift


def return_shape_guard() -> "list[str]":
    """Repository 公开方法返回注解不得含 ORM 实体（lotus-core 先例）。"""
    import src.kernel.persistence.repositories as repos
    offenders = []
    for name in dir(repos):
        obj = getattr(repos, name)
        if not isinstance(obj, type) or name.startswith("_"):
            continue
        for meth in dir(obj):
            if meth.startswith("_"):
                continue
            fn = getattr(obj, meth, None)
            if callable(fn) and getattr(fn, "__annotations__", {}).get("return"):
                ret = str(fn.__annotations__["return"])
                if "Model" in ret:
                    offenders.append(f"{name}.{meth} -> {ret}")
    return offenders
