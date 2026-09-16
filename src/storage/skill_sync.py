"""官方技能同步模块。

职责：
1. 远程同步：从 grape-skills 仓库下载 zip → 合并 skill-description.json → 同步 skill 文件目录 → DB 同步
2. OBS 同步：扫描本地 skills/<agent_type>/<skill>/ 目录，计算 content_hash → 打包 zip → 上传 OBS
3. DB 同步：从 skill-description.json INSERT/UPDATE 技能元信息（含 category_id）

触发方式：
- asyncio 定时任务（每 30 分钟，启动即执行）
- 任何模式都启动（非 OBS 模式跳过上传步骤，DB 同步正常执行）

不处理删除——保持和原有 DB 同步逻辑一致（只加不删，以 skill-description.json 为权威）。
"""
import asyncio
import hashlib
import json
import logging
import os
import shutil
import zipfile

from src.config import SKILLS_DIR, CONFIG_DIR, log, resolve_official_skill_path

logger = logging.getLogger(__name__)
_bg_tasks: set[asyncio.Task] = set()


def _compute_skill_hash(skill_dir: str) -> str:
    """计算技能目录的内容 hash（遍历所有文件，按文件名排序，取完整 SHA-256）。

    返回 64 字符十六进制字符串。目录不存在返回空串。
    """
    if not os.path.isdir(skill_dir):
        return ""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "__MACOSX", ".git"))
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(root, f), skill_dir)
            h.update(rel.encode())
            try:
                with open(os.path.join(root, f), "rb") as fp:
                    h.update(fp.read())
            except Exception:
                pass
    return h.hexdigest()  # 完整 64 字符十六进制


def _pack_skill_zip(skill_dir: str) -> bytes:
    """把技能目录打包成 zip 字节流。"""
    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_dir):
            dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git", "__pycache__")]
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, os.path.dirname(skill_dir)).replace(os.sep, "/")
                zf.write(full, rel)
    buf.seek(0)
    return buf.getvalue()


def _sync_skills_to_db(data: dict) -> int:
    """从 skill-description.json 数据同步官方技能元信息到 DB（INSERT/UPDATE，含 category_id）。

    唯一调用方：sync_official_skills()（worker 每 30 分钟调一次）。

    仅在实际数据变更时才 UPDATE + 更新 updated_at，避免 blanket UPDATE 架空 sort=latest。
    返回处理的 skill 数量。
    """
    from src.session_manager import db as _db, db_lock as _db_lock

    count = 0
    with _db_lock:
        c = _db.cursor()
        # 构建 slug → category_id 缓存
        cat_map = {}
        try:
            for row in c.execute("SELECT slug, id FROM skill_category WHERE is_active=1").fetchall():
                cat_map[row[0]] = row[1]
        except Exception:
            pass
        for skill_name, info in data.items():
            try:
                display_name = info.get("name", skill_name)
                description = info.get("description", "")
                agent_type = info.get("agent_type", "")
                skill_path_val = info.get("path", skill_name)
                sub_scene = info.get("sub_scene", "")
                category_id = cat_map.get(sub_scene, 0)
                if sub_scene and category_id == 0:
                    logger.warning(f"skill '{skill_name}' sub_scene='{sub_scene}' has no matching skill_category slug, category_id=0")
                row = c.execute(
                    "SELECT display_name, description, agent_type, skill_path, file_path, sub_scene, category_id FROM skill WHERE skill_name=? AND scope='official'",
                    (skill_name,)
                ).fetchone()
                if row:
                    old_display, old_desc, old_agent, old_path, old_fp, old_sub, old_cat = row
                    # 只在数据实际变更时才 UPDATE（避免 blanket UPDATE 刷新 updated_at 架空 sort=latest）
                    fp_nonempty = old_fp and old_fp != ""
                    changed = (old_display != display_name or old_desc != description
                               or old_agent != agent_type or old_sub != sub_scene
                               or old_cat != category_id
                               or (not fp_nonempty and old_path != skill_path_val))
                    if changed:
                        if fp_nonempty:
                            c.execute(
                                "UPDATE skill SET display_name=?, description=?, agent_type=?, sub_scene=?, category_id=?, updated_at=datetime('now') WHERE skill_name=? AND scope='official'",
                                (display_name, description, agent_type, sub_scene, category_id, skill_name)
                            )
                        else:
                            c.execute(
                                "UPDATE skill SET display_name=?, description=?, agent_type=?, skill_path=?, sub_scene=?, category_id=?, updated_at=datetime('now') WHERE skill_name=? AND scope='official'",
                                (display_name, description, agent_type, skill_path_val, sub_scene, category_id, skill_name)
                            )
                else:
                    c.execute(
                        "INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, content_hash, sub_scene, category_id) VALUES (?,?,?,?,?,'official','','',?,?)",
                        (skill_name, display_name, description, agent_type, skill_path_val, sub_scene, category_id)
                    )
                count += 1
            except Exception:
                logger.exception(f"sync skill '{skill_name}' to DB failed, skipping")
        _db.commit()
    return count


async def sync_official_skills_to_obs() -> dict:
    """OBS 同步：扫描本地 skill 目录，算 hash → 打包 → 上传 OBS（增量）。

    DB 同步由调用方（_skill_sync_worker）负责，此函数只做 hash + OBS 上传。
    返回 {"added": N, "updated": N, "skipped": N}。
    """
    from src.storage import get_storage, skill_key
    from src.user.db import get_skill_content_hash, update_skill_content_hash

    # 读 skill-description.json 获取官方技能清单
    json_path = os.path.join(CONFIG_DIR, "skill-description.json")
    if not os.path.exists(json_path):
        logger.warning("skill-description.json not found, skip obs sync")
        return {"added": 0, "updated": 0, "skipped": 0}

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    storage = get_storage()
    result = {"added": 0, "updated": 0, "skipped": 0}

    for skill_name, info in data.items():
        skill_path = info.get("path", skill_name)

        # 解析本地目录路径
        skill_dir = os.path.join(SKILLS_DIR, skill_path)
        if not os.path.isdir(skill_dir):
            # 尝试 resolve_official_skill_path 的路径
            resolved = resolve_official_skill_path(skill_name)
            if resolved:
                skill_dir = os.path.dirname(resolved)
            if not os.path.isdir(skill_dir):
                logger.debug(f"skill dir not found: {skill_name}, skip")
                continue

        # 计算当前 content_hash（放线程池，不阻塞事件循环）
        loop = asyncio.get_running_loop()
        current_hash = await loop.run_in_executor(None, _compute_skill_hash, skill_dir)
        if not current_hash:
            continue

        # 查 DB 的 content_hash 和 file_path（按 skill_name + scope 查主键 id + hash + file_path）
        skill_id, db_hash, db_file_path = get_skill_content_hash(skill_name)

        # skip 条件：hash 相同且（非OBS模式 或 file_path非空）
        # 非OBS模式：hash匹配就skip（不需要file_path）
        # OBS模式：hash匹配但file_path为空时不skip（需要上传OBS补file_path）
        if db_hash == current_hash:
            if storage is None or db_file_path:
                result["skipped"] += 1
                continue

        # hash 不同或技能不存在 → 需要同步
        object_key = ""
        if storage is not None:
            # OBS 模式：打包 zip → 上传 OBS（都放线程池，不阻塞事件循环）
            object_key = skill_key(scope="official", owner="", skill_name=skill_name)
            try:
                zip_data = await loop.run_in_executor(None, _pack_skill_zip, skill_dir)
                await loop.run_in_executor(None, storage.save_bytes, object_key, zip_data)
                logger.info(f"uploaded skill to OBS: {skill_name} -> {object_key}")
            except Exception as e:
                logger.error(f"upload skill {skill_name} to OBS failed: {e}")
                continue

        # 按主键 id 更新 DB
        updated = update_skill_content_hash(skill_id, current_hash, object_key)
        if updated:
            if db_hash and db_hash != current_hash:
                result["updated"] += 1
                logger.info(f"skill updated: {skill_name} (hash: {db_hash[:8]}... -> {current_hash[:8]}...)")
            elif db_hash == current_hash and not db_file_path and object_key:
                result["updated"] += 1
                logger.info(f"skill file_path synced: {skill_name} (hash unchanged, file_path was empty)")
            else:
                result["added"] += 1
                logger.info(f"skill added: {skill_name} (hash: {current_hash[:8]}...)")

    logger.info(f"obs sync done: +{result['added']} ~{result['updated']} skipped={result['skipped']}")
    return result


async def sync_official_skills() -> dict:
    """完整同步：远程下载 → DB 同步 → OBS 同步。

    每步独立 try/except，失败不阻塞后续步骤。
    Issue #179: 补齐 skill 远程同步能力，参考 scene_sync 的 _download_repo_zip 模式。
    """
    import httpx
    from src.storage.scene_sync import _download_repo_zip, _get_sync_token

    result = {"json_merged": 0, "dirs_synced": 0, "dirs_skipped": 0, "db_synced": 0, "obs_added": 0, "obs_updated": 0, "obs_skipped": 0}

    # 1. 远程同步：下载 zip → 合并 json → 拷贝 skill 目录
    tmp_dir = None
    try:
        token = _get_sync_token()
        if not token:
            logger.warning("skill sync: no GitCode token configured, skip remote sync")
        else:
            async with httpx.AsyncClient(timeout=60.0) as client:
                try:
                    repo_root = await _download_repo_zip(client, token)
                except Exception as e:
                    logger.warning(f"skill sync: download repo zip failed: {e}")
                    repo_root = None

            if repo_root:
                tmp_dir = repo_root

                # 读远程 skill-description.json
                remote_json_path = os.path.join(repo_root, "skill-description.json")
                if os.path.exists(remote_json_path):
                    with open(remote_json_path, "r", encoding="utf-8") as f:
                        remote_data = json.load(f)

                    # 合并到本地 json（远程覆盖本地，本地自定义保留）
                    local_json_path = os.path.join(CONFIG_DIR, "skill-description.json")
                    local_data = {}
                    if os.path.exists(local_json_path):
                        with open(local_json_path, "r", encoding="utf-8") as f:
                            local_data = json.load(f)

                    merged = dict(local_data)
                    for skill_name, info in remote_data.items():
                        if skill_name not in merged or merged[skill_name] != info:
                            merged[skill_name] = info
                            result["json_merged"] += 1

                    if result["json_merged"] > 0:
                        tmp_path = local_json_path + ".tmp"
                        with open(tmp_path, "w", encoding="utf-8") as f:
                            json.dump(merged, f, ensure_ascii=False, indent=2)
                        os.replace(tmp_path, local_json_path)
                        logger.info(f"skill sync: merged {result['json_merged']} skills into local json")
                else:
                    remote_data = {}
                    logger.warning("skill sync: skill-description.json not found in zip")

                # 同步 skill 文件目录（增量 hash 比对，分布式锁保护）
                from src.lock import acquire_lock, release_lock, renew_lock
                _LOCK_KEY = "skill_remote_sync"
                _LOCK_TTL = 300
                if not acquire_lock(_LOCK_KEY, ttl=_LOCK_TTL):
                    logger.info("skill sync: lock contention, skip directory sync")
                else:
                    try:
                        remote_skills_dir = os.path.join(repo_root, "skills")
                        if os.path.isdir(remote_skills_dir) and remote_data:
                            for skill_name, info in remote_data.items():
                                if not renew_lock(_LOCK_KEY, ttl=_LOCK_TTL):
                                    logger.warning("skill sync: lock lost during directory sync, aborting")
                                    break
                                skill_path = info.get("path", skill_name)
                                if ".." in skill_path or skill_path.startswith("/"):
                                    logger.warning(f"skill sync: skip unsafe path '{skill_path}' for {skill_name}")
                                    continue
                                remote_skill_dir = os.path.join(remote_skills_dir, skill_path)
                                if not os.path.isdir(remote_skill_dir):
                                    continue
                                local_skill_dir = os.path.join(SKILLS_DIR, skill_path)
                                remote_hash = _compute_skill_hash(remote_skill_dir)
                                if not remote_hash:
                                    continue
                                local_hash = _compute_skill_hash(local_skill_dir)
                                if remote_hash == local_hash:
                                    result["dirs_skipped"] += 1
                                    continue
                                if os.path.isdir(local_skill_dir):
                                    shutil.rmtree(local_skill_dir, ignore_errors=True)
                                os.makedirs(os.path.dirname(local_skill_dir), exist_ok=True)
                                shutil.copytree(remote_skill_dir, local_skill_dir, dirs_exist_ok=True)
                                result["dirs_synced"] += 1
                                logger.info(f"skill sync: synced directory {skill_name}")
                    finally:
                        release_lock(_LOCK_KEY)

                # 清理过期锁
                from src.lock import cleanup_expired_locks
                cleanup_expired_locks()

                logger.info(
                    f"skill sync: remote done json_merged={result['json_merged']}, "
                    f"dirs_synced={result['dirs_synced']}, dirs_skipped={result['dirs_skipped']}"
                )
    except Exception as e:
        logger.exception(f"skill sync: remote sync failed: {e}")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # 2. DB 同步：读本地 json → _sync_skills_to_db
    try:
        json_path = os.path.join(CONFIG_DIR, "skill-description.json")
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                result["db_synced"] = _sync_skills_to_db(json.load(f))
    except Exception as e:
        logger.exception(f"skill sync: DB sync failed: {e}")

    # 3. OBS 同步：算 hash → 打包 → 上传
    try:
        obs_result = await sync_official_skills_to_obs()
        result["obs_added"] = obs_result.get("added", 0)
        result["obs_updated"] = obs_result.get("updated", 0)
        result["obs_skipped"] = obs_result.get("skipped", 0)
    except Exception as e:
        logger.exception(f"skill sync: OBS sync failed: {e}")

    return result


async def _skill_sync_worker(interval: float = 1800.0):
    """定时同步 worker：启动即执行，之后每 interval 秒。

    调用 sync_official_skills() 完成完整同步：
    远程下载 → DB 同步 → OBS 同步（每步独立 try/except）。
    """
    while True:
        try:
            await sync_official_skills()
        except asyncio.CancelledError:
            logger.info("skill sync worker cancelled")
            raise
        except Exception:
            logger.exception("skill sync run failed")
        await asyncio.sleep(interval)


def start_skill_sync_scheduler():
    """启动定时同步调度（任何模式都启动）。"""
    interval = max(float(os.environ.get("SKILL_SYNC_INTERVAL", "1800")), 60)
    t = asyncio.create_task(_skill_sync_worker(interval), name="skill-sync")
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    log.info(f"skill sync scheduler started (interval={interval}s)")


async def stop_skill_sync_scheduler():
    """优雅关闭：cancel 所有定时任务并等待退出。"""
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    log.info("skill sync scheduler stopped")
