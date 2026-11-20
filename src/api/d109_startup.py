"""D109 平台对象模型启动装配（A 进程 / 单进程共用）。

放在独立模块而不是留在 `src/api/main.py`：多进程部署的入口是
`src/api/grape_server.py`（A 进程），单进程兼容入口是 `src/api/main.py`；
启动导入必须两个入口都跑，而 grape_server 不能 import main（会连带构建
单进程 app）。设计分片 d109 §12 风险 1：导入放 lifespan 同步段，
失败 fail-open（读路径回落文件 + meta）。
"""
from __future__ import annotations


def reconcile_missing_columns(conn, log, only_tables: "set[str] | None" = None) -> "list[str]":
    """P2-2：把"历史半建表"补齐——表在但缺列。

    原实现只靠 `CREATE TABLE IF NOT EXISTS` + "already exists" 字符串忽略，
    半建表（表在、列缺）不会报错也不会补列 → 读路径静默缺字段。
    这里按 SQLAlchemy 模型逐列对比实际 schema，缺列用 ALTER TABLE ADD COLUMN 补。
    返回补上的 "表.列" 列表；失败逐列告警（fail-open，不阻断启动）。
    """
    from sqlalchemy.schema import CreateColumn

    from src.kernel.persistence.models_platform_object import (
        ChannelRepoLinkModel,
        OAuthChannelModel,
        RepoPlatformModel,
        UserRepoPlatformLinkModel,
    )

    models = [OAuthChannelModel, RepoPlatformModel, ChannelRepoLinkModel,
              UserRepoPlatformLinkModel]
    is_mysql = getattr(getattr(conn, "dialect", None), "name", "") in ("mysql", "mariadb")
    added: list[str] = []
    for model in models:
        tbl = model.__tablename__
        if only_tables and tbl not in only_tables:
            continue
        try:
            if is_mysql:
                have = {r[0] for r in conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns"
                    f" WHERE table_schema = DATABASE() AND table_name = '{tbl}'")}
            else:
                have = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({tbl})")}
        except Exception as e:
            log.warning("P2-2 schema probe failed %s: %s", tbl, e)
            continue
        if not have:
            continue  # 整表不存在：CREATE 循环负责（失败已单独告警）
        for col in model.__table__.columns:
            if col.name in have:
                continue
            try:
                col_ddl = str(CreateColumn(col).compile(dialect=conn.dialect))
                conn.exec_driver_sql(f"ALTER TABLE {tbl} ADD COLUMN {col_ddl}")
                added.append(f"{tbl}.{col.name}")
                log.info("P2-2 补齐半建表缺列: %s.%s", tbl, col.name)
            except Exception as e:
                log.warning("P2-2 add column failed %s.%s: %s", tbl, col.name, e)
    return added


def _ensure_platform_tables(log) -> None:
    """补齐建表欠账：MySQL 等非 sqlite 后端在导入前建 D109 六表（幂等）。

    为什么需要：SQLite 的建表由迁移 `202611150007` 负责；`db_setup.py` 的 MySQL 分支
    直接 `return`（不跑 sqlite 迁移），所以 MySQL 三域部署下 D109 六表不存在 →
    启动导入抛 `1146 Table ... doesn't exist`，整条读路径回落文件，写接口全失败。
    这里在导入前按当前方言建表，把这条路径补齐。

    幂等：`CREATE TABLE IF NOT EXISTS` + 已存在错误忽略；部分唯一索引换成
    MySQL 的生成列 + 复合唯一键（`platform_object_ddl` 的等价实现）。
    fail-open：任何失败只告警，仍回落旧存储（不阻断启动）。
    """
    try:
        from src.kernel.persistence.engine import get_engine

        eng = get_engine()
        if eng.dialect.name == "sqlite":
            return  # 迁移负责，不重复建

        from src.api.platform_object_ddl import iter_statements
        from src.kernel.mysql_compat import translate_sql

        ok = 0
        skipped = 0
        with eng.begin() as conn:
            for stmt in iter_statements():
                try:
                    conn.exec_driver_sql(translate_sql(stmt))
                    ok += 1
                except Exception as e:  # 已存在 / 方言差异：不致命
                    msg = str(e).lower()
                    if "already exists" in msg or "duplicate" in msg:
                        skipped += 1
                        continue
                    log.warning("D109 DDL statement failed (non-fatal): %s | %s",
                                stmt.split("\n")[0][:90], e)
        log.info("D109 platform-object tables ensured (dialect=%s, applied=%d, skipped=%d)",
                 eng.dialect.name, ok, skipped)
        # P2-2：CREATE 只保证"表存在"，历史半建表（表在列缺）要按模型逐列补齐
        try:
            tbls = {"grape_t_oauth_channels", "grape_t_repo_platforms", "grape_t_channel_repo_link",
                    "grape_t_user_repo_platform_link"}
            with eng.begin() as _c2:
                fixed = reconcile_missing_columns(_c2, log, only_tables=tbls)
            if fixed:
                log.warning("D109 半建表已补齐缺列: %s", ", ".join(fixed))
        except Exception as _rc:  # 补列失败不阻断启动
            log.warning("D109 半建表补列失败 (non-fatal): %s", _rc)
    except Exception as e:  # 建表失败也必须放行（fail-open）
        log.warning("D109 platform-object table ensure failed (non-fatal): %s", e)


def d109_platform_object_startup(log) -> None:
    """D109 平台对象模型：启动扫描导入 + 一次性凭据迁移/关系回填。

    幂等、fail-open：失败只告警，读路径回落「文件 + meta」（d109 §16 影响 #1 的缓解）。
    """
    from sqlalchemy.orm import Session
    from src.kernel.persistence.engine import get_engine
    from src.domains.platform import importer, migration

    _ensure_platform_tables(log)

    with Session(get_engine()) as s:
        report = importer.run_startup_import(s, owner=f"startup-{__import__('os').getpid()}")
    log.info("D109 platform-object import: %s", report.summary())

    st = importer._read_state()
    if not st.get("credentials_migrated"):
        with Session(get_engine()) as s:
            out = migration.run_all(s)
            s.commit()
        log.info("D109 credential migration: %s | reconcile ok=%s",
                 out.get("stats"), out.get("reconcile", {}).get("ok"))
        st["credentials_migrated"] = True
        importer._write_state(st)

    # 导入/迁移后必须主动失效缓存：启动早期可能有模块已按旧文件卡缓存过
    # （TTL 300s 内不会自刷新）→ 不失效会出现「改配置重启后页面还是旧值」。
    try:
        from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
        from src.domains.mgmt.auth.providers import invalidate_provider_cache

        invalidate_platform_card_cache()
        invalidate_provider_cache()
        log.info("D109: caches invalidated after import/migration")
    except Exception as _inv_err:
        log.warning(f"D109 cache invalidate failed (non-fatal): {_inv_err}")