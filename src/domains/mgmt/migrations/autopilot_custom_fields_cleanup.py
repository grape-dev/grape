"""Autopilot / 场景 custom_fields 存量清理（迁移 202611240001）。

Issue #4066：写侧此前不校验 custom_fields，非法条目（空名 / 非大写 / 保留前缀）
可长期存在于 `autopilot_tasks.custom_fields` 与场景 `advanced_options.custom_fields`，
每 tick 被执行侧丢弃并刷 `custom env ... rejected` WARNING（生产 1 小时 7 条），
配置意图与运行实际不一致。

本迁移按**当前**校验规则（`scene_custom_env.custom_env_error` 同口径）一次性剔除
非法条目，消除小时级噪声；合法条目（含 Issue #4066 白名单 `GRAPE_ANALYSIS_MODE` /
`GRAPE_ORG_ID` / `GRAPE_MANAGED_BY`）保留，场景其它 advanced_options 键不动。

归属：`autopilot_tasks` / `devops_pipeline_scene` 均归 mgmt（configs/table_ownership.json），
SQL 住本域 migrations 下（跨域守卫 P0-2）；注册由 `src/api/db_setup.py` 引导器做。

约定：
- **幂等**：只按当前规则过滤，无非法项时不做 UPDATE（存量库会重跑）；
- **失败抛出**：SQL 异常不吞（executor 约定「抛错=不盖章，下次启动重试」）；
- `post_check`：残留非法条目为 0 才盖章；表不存在视为收敛（新装库由 DDL 保证为空）；
- 无法解析的 custom_fields 原文不动（不破坏未知形态数据），仅记录告警。
"""
from __future__ import annotations

import json

TABLE_TASK = "autopilot_tasks"
TABLE_SCENE = "devops_pipeline_scene"


def _table_exists(c, table: str) -> bool:
    """表是否存在（sqlite / mysql 双后端；表名来自本模块常量，无注入面）。"""
    from src.kernel.mysql_compat import get_db_backend

    if get_db_backend() == "mysql":
        row = c.execute(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema=DATABASE() AND table_name='{table}'").fetchone()
    else:
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def is_invalid_custom_field(f) -> bool:
    """单条 custom_field 是否非法（与写侧 `custom_env_error` 同口径，含空名）。"""
    from src.kernel.scene_custom_env import custom_env_error

    if not isinstance(f, dict):
        return True
    return bool(custom_env_error(str(f.get("name") or "")))


def _clean_fields(raw_fields):
    """返回 (清理后列表, 剔除条数)；非 list 输入原样返回 (None, 0)。"""
    if not isinstance(raw_fields, list):
        return None, 0
    kept = [f for f in raw_fields if not is_invalid_custom_field(f)]
    return kept, len(raw_fields) - len(kept)


def _clean_autopilot_tasks(c, log) -> int:
    if not _table_exists(c, TABLE_TASK):
        log.info(f"202611240001 {TABLE_TASK} 不存在，跳过")
        return 0
    rows = c.execute(
        f"SELECT id, custom_fields FROM {TABLE_TASK} "
        "WHERE custom_fields IS NOT NULL AND custom_fields != ''").fetchall()
    changed = 0
    for task_id, raw in rows:
        try:
            fields = json.loads(raw or "[]")
        except Exception as e:  # noqa: BLE001 - 未知形态不破坏，留痕即可
            log.warning(f"202611240001 task {task_id} custom_fields 无法解析，跳过: {e}")
            continue
        kept, dropped = _clean_fields(fields)
        if kept is None or not dropped:
            continue
        c.execute(
            f"UPDATE {TABLE_TASK} SET custom_fields=? WHERE id=?",
            (json.dumps(kept, ensure_ascii=False) if kept else "", task_id))
        changed += 1
    return changed


def _clean_scene_options(c, log) -> int:
    if not _table_exists(c, TABLE_SCENE):
        log.info(f"202611240001 {TABLE_SCENE} 不存在，跳过")
        return 0
    rows = c.execute(
        f"SELECT scene_id, repo_key, advanced_options FROM {TABLE_SCENE} "
        "WHERE advanced_options IS NOT NULL AND advanced_options != ''").fetchall()
    changed = 0
    for scene_id, repo_key, raw in rows:
        try:
            opts = json.loads(raw or "{}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"202611240001 scene {scene_id} advanced_options 无法解析，跳过: {e}")
            continue
        if not isinstance(opts, dict):
            continue
        kept, dropped = _clean_fields(opts.get("custom_fields"))
        if kept is None or not dropped:
            continue
        opts["custom_fields"] = kept
        c.execute(
            f"UPDATE {TABLE_SCENE} SET advanced_options=? WHERE scene_id=? AND repo_key=?",
            (json.dumps(opts, ensure_ascii=False), scene_id, repo_key))
        changed += 1
    return changed


def cleanup_invalid_custom_fields(conn=None) -> None:
    """一次性清理两处存量 custom_fields 的非法条目（幂等）。"""
    from contextlib import nullcontext

    from src.kernel.config import log

    if conn is None:
        from src.kernel.session_manager import db, db_lock

        conn, lock = db, db_lock
    else:
        lock = nullcontext()
    with lock:
        c = conn.cursor()
        _tasks = _clean_autopilot_tasks(c, log)
        _scenes = _clean_scene_options(c, log)
        conn.commit()
    log.info(
        f"202611240001 存量 custom_fields 清理完成：autopilot_tasks {_tasks} 行、"
        f"devops_pipeline_scene {_scenes} 行")


def verify_custom_fields_clean(conn=None) -> None:
    """后置校验：两处均无非法条目才盖章（Issue #3648：结果可判定才 post_check）。"""
    from src.kernel.config import log

    if conn is None:
        from src.kernel.session_manager import db

        conn = db
    c = conn.cursor()
    residues = []
    if _table_exists(c, TABLE_TASK):
        for task_id, raw in c.execute(
                f"SELECT id, custom_fields FROM {TABLE_TASK} "
                "WHERE custom_fields IS NOT NULL AND custom_fields != ''").fetchall():
            try:
                fields = json.loads(raw or "[]")
            except Exception:  # noqa: BLE001 - fn 已对不可解析项留痕，不算残留
                continue
            if isinstance(fields, list):
                bad = [f for f in fields if is_invalid_custom_field(f)]
                if bad:
                    residues.append(f"task#{task_id}")
    if _table_exists(c, TABLE_SCENE):
        for scene_id, raw in c.execute(
                f"SELECT scene_id, advanced_options FROM {TABLE_SCENE} "
                "WHERE advanced_options IS NOT NULL AND advanced_options != ''").fetchall():
            try:
                opts = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(opts, dict):
                continue
            bad = [f for f in (opts.get("custom_fields") or []) if is_invalid_custom_field(f)]
            if bad:
                residues.append(f"scene#{scene_id}")
    if residues:
        raise RuntimeError(
            "202611240001 post_check 未收敛：残留非法 custom_fields → "
            + ", ".join(residues) + "（不盖章，下次启动重试）")
    log.info("202611240001 post_check 通过：存量 custom_fields 无非法条目")


MIGRATION = {
    "version": "202611240001",
    "name": "autopilot-custom-fields-cleanup",
    "kind": "cleanup",
    "fn": cleanup_invalid_custom_fields,
    "domains": ("mgmt",),
    "post_check": verify_custom_fields_clean,
}