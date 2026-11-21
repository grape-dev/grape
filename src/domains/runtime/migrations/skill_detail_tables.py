"""Issue #3168 技能详情页的 runtime 域迁移（202611181301/1302/1303）。

skill / grape_t_skill_version / grape_t_skill_file / grape_t_skill_comment 全部归 runtime
（configs/table_ownership.json），因此这三条迁移连同 SQL 一起住在本域
migrations 下——src/api 层直写 runtime 表会被
tests/arch/test_foreign_table_refs.py（P0-2 棘轮）计为新增跨域引用。
注册仍由 src/api/db_setup.py 引导器做（register_migration），本模块只导出内容，
模式同 ensure_runtime_type_info_columns / drop_same_domain_links。

domains=("runtime",)：三库模式由 runtime 进程执行
（run_domain_migrations 按域过滤，mgmt 进程不跑、不盖章）；单库 / 测试环境
不筛域、全量执行，行为与迁移写在 db_setup 时完全一致。

版本号沿革（rebase 到 arch-weekly-11 的改号记录）：
- 1301 原用 202611180001，基线已被 ensure-cloud-sandboxes-in-runtime 占用（撞号守卫）；
- 1302 原用 202611180002，基线已被 ensure-runtime-type-info-columns 占用；
- 1301/1302 曾顺延到 202611181201/1202，因基线守卫
  tests/api/test_category_columns_repair.py 钉死「20261118120x 段必须为空」
  再顺延到 202611181301/1302；1303 原用 202611180003（基线
  issue2761-skill-classification-columns 占用；该条已按 Issue #3648 改号到
  202611210001，classifier 列后置校验也住本模块）。
"""
from __future__ import annotations


def _column_exists(cur, table: str, column: str) -> bool:
    """PRAGMA 探测（sqlite 原生；MySQL 由 kernel mysql_compat 翻译，同 db_setup._ensure_column）。"""
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _ensure_column(cur, table: str, column: str, ddl: str) -> None:
    """幂等补列（本域表本域执行，无跨库视图分支；列名反引号兼容 MySQL 保留字）。"""
    if _column_exists(cur, table, column):
        return
    cur.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")


def verify_skill_classification_columns(conn=None) -> list:
    """后置校验（Issue #3648）：skill 必须真的具备 cat_l1_id / cat_l2_id。

    执行器在盖章前调用；缺列 raise → 不盖章、下次启动重试。
    可注入连接供测试使用（sqlite / MySQLCompat 均走 PRAGMA 翻译）。
    """
    if conn is None:
        from src.kernel.session_manager import db as conn
    cur = conn.cursor()
    rows = cur.execute("PRAGMA table_info(skill)").fetchall()
    if not rows:
        raise RuntimeError(
            "skill 表不存在（分类列后置校验失败；不盖章，下次启动重试）")
    cols = {r[1] for r in rows}
    missing = [col for col in ("cat_l1_id", "cat_l2_id") if col not in cols]
    if missing:
        raise RuntimeError(
            f"skill 缺分类列 {missing}（列迁移未真正到位；不盖章，下次启动重试）")
    return ["cat_l1_id", "cat_l2_id"]


def ensure_skill_detail_schema() -> None:
    """skill 补 summary/download_count 列，建 grape_t_skill_version/grape_t_skill_file/grape_t_skill_comment。

    幂等：PRAGMA 先探测；CREATE TABLE IF NOT EXISTS 可重复执行。
    失败即 raise（执行器约定：抛错 = 不盖章，下次启动重试）。
    """
    from src.domains.runtime.schema_ddl import (
        _SKILL_VERSION_DDL, _SKILL_FILE_DDL, _SKILL_COMMENT_DDL,
    )
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        _ensure_column(c, "skill", "summary", "TEXT DEFAULT ''")
        _ensure_column(c, "skill", "download_count", "INTEGER DEFAULT 0")
        for _name, _ddl in (("grape_t_skill_version", _SKILL_VERSION_DDL),
                            ("grape_t_skill_file", _SKILL_FILE_DDL),
                            ("grape_t_skill_comment", _SKILL_COMMENT_DDL)):
            c.execute(_ddl)
        db.commit()


def backfill_official_skill_versions() -> None:
    """73 个官方技能各补一行 1.0.0（时间取 skill.created_at，不伪造中间版本）。

    kind=backfill：单行失败不阻断（日志留痕），表级失败跳过等 schema 迁移建表。
    """
    from src.kernel.id_gen import uuid7_str
    from src.kernel.session_manager import db, db_lock
    from src.kernel.config import log

    with db_lock:
        c = db.cursor()
        try:
            rows = c.execute(
                "SELECT s.id, s.created_at FROM skill s "
                "WHERE s.scope='official' AND s.is_active=1 "
                "AND NOT EXISTS (SELECT 1 FROM grape_t_skill_version v WHERE v.skill_id=s.id)"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001 - 表不存在时由 1301 schema 迁移负责
            log.warning(f"202611181302 official version backfill skipped: {exc}")
            return
        for sid, created_at in rows or []:
            try:
                c.execute(
                    "INSERT INTO grape_t_skill_version "
                    "(id, skill_id, version, change_note, uploaded_at, uploaded_by) "
                    "VALUES (?,?,?,?,?,?)",
                    (uuid7_str(), int(sid), "1.0.0", "存量官方技能版本补录",
                     created_at or "", "system"))
            except Exception as exc:  # noqa: BLE001 - 单行失败不阻断
                log.warning(f"202611181302 version row failed for skill {sid}: {exc}")
        db.commit()


def add_skill_intro_override() -> None:
    """skill 补 intro_override 列（TEXT，存 JSON：{scope/steps/products/constraints/review}）。"""
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        _ensure_column(c, "skill", "intro_override", "TEXT DEFAULT ''")
        db.commit()


MIGRATION_SCHEMA = {
    "version": "202611181301",
    "name": "issue3168-skill-detail-schema",
    "kind": "safe",
    "fn": ensure_skill_detail_schema,
    "domains": ("runtime",),
}

MIGRATION_BACKFILL = {
    "version": "202611181302",
    "name": "issue3168-official-skill-version-backfill",
    "kind": "backfill",
    "fn": backfill_official_skill_versions,
    "domains": ("runtime",),
}

MIGRATION_INTRO_OVERRIDE = {
    "version": "202611181303",
    "name": "skill-intro-override-schema",
    "kind": "safe",
    "fn": add_skill_intro_override,
    "domains": ("runtime",),
}
