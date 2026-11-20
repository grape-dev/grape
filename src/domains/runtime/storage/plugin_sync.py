"""官方插件同步模块（Issue #1084 插件市场 B1，复用 skill_sync 同步模式）。

职责（对齐 src/storage/skill_sync.py 的同步模式）：
1. DB 同步：读 plugin-description.json（权威清单）→ upsert 插件元信息（scope='official'，含分类映射）
2. OBS 同步：扫描本地 plugins/<plugin>/ 目录，计算 content_hash → 打包 zip → 上传 OBS 固定 key

触发方式：
- asyncio 定时任务（每 30 分钟，启动即执行；无描述文件时安全空转）
- B1 不做远程仓库下载半边（官方插件包暂由本地源目录提供，远程下载留后续里程碑）

不处理删除——与 skill_sync 铁律一致（只加不删，以 plugin-description.json 为权威，
下架走 status=delisted 由管理端手工执行）。
"""
import asyncio
import json
import logging
import os
import uuid

from src.kernel.config import CONFIG_DIR, SKILLS_DIR, log

logger = logging.getLogger(__name__)
_bg_tasks: set[asyncio.Task] = set()

# 描述清单（权威）与本地插件包源目录；env 可覆盖（测试/部署定制）
_DEFAULT_JSON = os.path.join(CONFIG_DIR, "plugin-description.json")
_DEFAULT_SOURCE_DIR = os.path.join(os.path.dirname(SKILLS_DIR), "plugins")


def _description_json_path() -> str:
    return os.environ.get("PLUGIN_DESCRIPTION_JSON", _DEFAULT_JSON)


def _source_root() -> str:
    return os.environ.get("PLUGIN_SYNC_SOURCE_DIR", _DEFAULT_SOURCE_DIR)


def _pack_plugin_zip(plugin_dir: str) -> bytes:
    """打包插件目录为 zip（zip 根 = {plugin_name}/），直接复用 skill_sync 同款实现。"""
    from src.domains.runtime.storage.skill_sync import _pack_skill_zip
    return _pack_skill_zip(plugin_dir)


def datetime_utc_iso() -> str:
    """当前 UTC 时间 ISO 串（对齐 routes/plugins._now 的 Python 侧时间戳口径）。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _category_id_by_name(name: str) -> int:
    """分类名 → grape_t_plugin_category.id（未命中/未填返回 0=未分类）。"""
    if not name:
        return 0
    from src.kernel.session_manager import db as _db, db_lock as _dl
    try:
        with _dl:
            c = _db.cursor()
            row = c.execute(
                "SELECT id FROM grape_t_plugin_category WHERE name=? AND is_active=1",
                (name,)).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def sync_official_plugins_to_db() -> int:
    """从 plugin-description.json 同步官方插件元信息到 DB（INSERT/UPDATE，scope='official'）。

    仅在实际数据变更时才 UPDATE（避免 blanket UPDATE 架空列表 sort=latest，对齐 skill_sync）。
    返回处理的插件数量。唯一调用方：sync_official_plugins()。
    """
    json_path = _description_json_path()
    if not os.path.exists(json_path):
        logger.warning(f"plugin-description.json not found ({json_path}), skip db sync")
        return 0
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        logger.warning("plugin-description.json is not an object, skip db sync")
        return 0

    from src.kernel.session_manager import db as _db, db_lock as _dl
    count = 0
    now = datetime_utc_iso()
    with _dl:
        c = _db.cursor()
        for plugin_name, info in data.items():
            try:
                display_name = str(info.get("display_name") or info.get("name") or plugin_name)
                description = str(info.get("description", "") or "")
                version = str(info.get("version", "1.0.0") or "1.0.0")
                category_id = _category_id_by_name(str(info.get("category", "") or ""))
                row = c.execute(
                    "SELECT display_name, description, version, category_id "
                    "FROM grape_t_plugin WHERE plugin_name=? AND scope='official'",
                    (plugin_name,)).fetchone()
                if row:
                    changed = (row[0] != display_name or row[1] != description
                               or row[2] != version or row[3] != category_id)
                    if changed:
                        c.execute(
                            "UPDATE grape_t_plugin SET display_name=?, description=?, version=?, "
                            "category_id=?, updated_by='plugin_sync', updated_at=? "
                            "WHERE plugin_name=? AND scope='official'",
                            (display_name, description, version, category_id, now, plugin_name))
                else:
                    c.execute(
                        "INSERT INTO grape_t_plugin (id, plugin_name, display_name, description, "
                        "version, category_id, author, scope, owner_id, status, is_active, "
                        "created_by, updated_by, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?, 'official', '', 'active', 1, 'plugin_sync', "
                        "'plugin_sync', ?, ?)",
                        (uuid.uuid4().hex, plugin_name, display_name, description,
                         version, category_id, "grape", now, now))
                count += 1
            except Exception:
                logger.exception(f"sync plugin '{plugin_name}' to DB failed, skipping")
        _db.commit()
    return count


async def sync_official_plugins_to_obs() -> dict:
    """OBS 同步：扫描本地插件目录 → 打包 zip → 上传 OBS 固定 key（增量，指纹相同跳过）。

    DB 指纹口径对齐 upload 链路：content_hash = 规范化 zip 字节 sha256
    （routes/plugins.py 同款），保证"存储对象字节 ↔ DB 指纹"可互相校验。
    返回 {"added": N, "updated": N, "skipped": N}（对齐 skill_sync 计数口径）。
    """
    import hashlib

    json_path = _description_json_path()
    if not os.path.exists(json_path):
        logger.warning(f"plugin-description.json not found ({json_path}), skip obs sync")
        return {"added": 0, "updated": 0, "skipped": 0}
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"added": 0, "updated": 0, "skipped": 0}

    from src.kernel.session_manager import db as _db, db_lock as _dl
    from src.domains.runtime.storage import get_skill_storage, plugin_key

    storage = get_skill_storage()
    result = {"added": 0, "updated": 0, "skipped": 0}
    source_root = _source_root()

    for plugin_name, info in data.items():
        plugin_path = info.get("path", plugin_name)
        if ".." in plugin_path or os.path.isabs(plugin_path):
            logger.warning(f"plugin sync: skip unsafe path '{plugin_path}' for {plugin_name}")
            continue
        plugin_dir = os.path.join(source_root, plugin_path)
        if not os.path.isdir(plugin_dir):
            logger.debug(f"plugin dir not found: {plugin_name} ({plugin_dir}), skip")
            continue

        with _dl:
            c = _db.cursor()
            row = c.execute(
                "SELECT id, content_hash, object_key FROM grape_t_plugin "
                "WHERE plugin_name=? AND scope='official'", (plugin_name,)).fetchone()
        if not row:
            # DB 行未就绪（db 同步未跑/清单缺条目）→ 本轮跳过，等下一轮 DB 同步补齐
            logger.warning(f"plugin sync: DB row missing for '{plugin_name}', skip obs upload")
            continue
        plugin_id, db_hash, _db_object_key = row

        if storage is None:
            logger.debug(f"plugin sync: storage disabled, skip obs upload for {plugin_name}")
            continue

        loop = asyncio.get_running_loop()
        try:
            zip_data = await loop.run_in_executor(None, _pack_plugin_zip, plugin_dir)
            zip_hash = hashlib.sha256(zip_data).hexdigest()
        except Exception as e:
            logger.error(f"pack plugin {plugin_name} failed: {e}")
            continue

        # skip 条件：指纹相同且对象键已回填（增量幂等）
        object_key = plugin_key("official", "", plugin_name)
        if db_hash == zip_hash and _db_object_key:
            result["skipped"] += 1
            continue

        try:
            await loop.run_in_executor(None, storage.save_bytes, object_key, zip_data)
            logger.info(f"uploaded plugin to OBS: {plugin_name} -> {object_key}")
        except Exception as e:
            logger.error(f"upload plugin {plugin_name} to OBS failed: {e}")
            continue

        with _dl:
            c = _db.cursor()
            c.execute(
                "UPDATE grape_t_plugin SET content_hash=?, object_key=?, "
                "updated_by='plugin_sync', updated_at=? WHERE id=?",
                (zip_hash, object_key, datetime_utc_iso(), plugin_id))
            _db.commit()
        if db_hash and db_hash != zip_hash:
            result["updated"] += 1
        else:
            result["added"] += 1

    logger.info(f"plugin obs sync done: +{result['added']} ~{result['updated']} "
                f"skipped={result['skipped']}")
    return result


async def sync_official_plugins() -> dict:
    """完整同步：DB 同步（json 权威）→ OBS 同步。每步独立 try/except，失败不阻塞后续步骤。"""
    result = {"db_synced": 0, "obs_added": 0, "obs_updated": 0, "obs_skipped": 0}
    try:
        result["db_synced"] = sync_official_plugins_to_db()
    except Exception:
        logger.exception("plugin sync: DB sync failed")
    try:
        obs_result = await sync_official_plugins_to_obs()
        result["obs_added"] = obs_result.get("added", 0)
        result["obs_updated"] = obs_result.get("updated", 0)
        result["obs_skipped"] = obs_result.get("skipped", 0)
    except Exception:
        logger.exception("plugin sync: OBS sync failed")
    return result


async def _plugin_sync_worker(interval: float = 1800.0):
    """定时同步 worker：启动即执行，之后每 interval 秒（对齐 _skill_sync_worker 模式）。"""
    from src.domains.runtime.engine.task_lock import run_with_distributed_lock
    while True:
        try:
            async with run_with_distributed_lock("plugin_sync", ttl=1800):
                await sync_official_plugins()
        except asyncio.CancelledError:
            logger.info("plugin sync worker cancelled")
            raise
        except Exception:
            logger.exception("plugin sync run failed")
        await asyncio.sleep(interval)


def start_plugin_sync_scheduler():
    """启动官方插件同步调度（任何模式都启动；无描述文件时空转）。"""
    interval = max(float(os.environ.get("PLUGIN_SYNC_INTERVAL", "1800")), 60)
    t = asyncio.create_task(_plugin_sync_worker(interval), name="plugin-sync")
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    log.info(f"plugin sync scheduler started (interval={interval}s)")


async def stop_plugin_sync_scheduler():
    """优雅关闭：cancel 所有定时任务并等待退出。"""
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    log.info("plugin sync scheduler stopped")
