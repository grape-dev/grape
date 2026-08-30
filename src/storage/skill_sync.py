"""官方技能 OBS 自动同步模块（official-skill-obs-sync-design.md）。

职责：
1. 扫描本地 skills/<agent_type>/<skill>/ 目录，计算 content_hash
2. hash 变了 → 打包 zip → 上传 OBS → 更新 DB file_path + content_hash

触发方式：
- asyncio 定时任务（OBS 模式，每 30 分钟）
- 手动触发（admin 页面"同步官方技能"按钮 → sync_official_skills API）

非 OBS 模式（storage=None）：定时调度不启动，手动触发时只更新 DB content_hash。

不处理删除——保持和原有 DB 同步逻辑一致（只加不删，以 skill-description.json 为权威）。
"""
import asyncio
import hashlib
import json
import logging
import os
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
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "__MACOSX", ".git")]
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


async def sync_official_skills_to_obs() -> dict:
    """全量同步官方技能到 OBS（增量：hash 不变的跳过）。

    返回 {"added": N, "updated": N, "skipped": N}。
    """
    from src.storage import get_storage, skill_key
    from src.user.db import get_skill_content_hash, update_skill_content_hash

    # 读 skill-description.json 获取官方技能清单
    json_path = os.path.join(CONFIG_DIR, "skill-description.json")
    if not os.path.exists(json_path):
        logger.warning("skill-description.json not found, skip sync")
        return {"added": 0, "updated": 0, "skipped": 0}

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    storage = get_storage()
    result = {"added": 0, "updated": 0, "skipped": 0}

    for skill_name, info in data.items():
        agent_type = info.get("agent_type", "")
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

        # 计算当前 content_hash
        current_hash = _compute_skill_hash(skill_dir)
        if not current_hash:
            continue

        # 查 DB 的 content_hash
        db_hash = get_skill_content_hash(skill_name)

        if db_hash == current_hash:
            result["skipped"] += 1
            continue

        # hash 不同或技能不存在 → 需要同步
        object_key = ""
        if storage is not None:
            # OBS 模式：打包 zip → 上传 OBS
            object_key = skill_key(scope="official", owner="", skill_name=skill_name)
            try:
                zip_data = _pack_skill_zip(skill_dir)
                # 上传是同步 IO 操作，用 run_in_executor 避免阻塞事件循环
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, storage.save_bytes, object_key, zip_data)
                logger.info(f"uploaded skill to OBS: {skill_name} -> {object_key}")
            except Exception as e:
                logger.error(f"upload skill {skill_name} to OBS failed: {e}")
                continue

        # 更新 DB
        updated = update_skill_content_hash(skill_name, "official", current_hash, object_key)
        if updated:
            if db_hash:
                result["updated"] += 1
                logger.info(f"skill updated: {skill_name} (hash: {db_hash[:8]}... -> {current_hash[:8]}...)")
            else:
                result["added"] += 1
                logger.info(f"skill added: {skill_name} (hash: {current_hash[:8]}...)")
        else:
            # DB 里没有这个技能记录，db_setup.py 的 _sync_official_skills 会负责 INSERT
            result["skipped"] += 1

    logger.info(f"sync done: +{result['added']} ~{result['updated']} skipped={result['skipped']}")
    return result


async def _skill_sync_worker(interval: float = 1800.0):
    """定时同步 worker：启动即执行，之后每 interval 秒增量扫描。"""
    while True:
        try:
            await sync_official_skills_to_obs()
        except asyncio.CancelledError:
            logger.info("skill sync worker cancelled")
            raise  # 必须重新 raise，不能吞掉取消异常
        except Exception:
            logger.exception("skill sync run failed")
        # 无论成功失败都 sleep，避免雪崩式死循环
        await asyncio.sleep(interval)


def start_skill_sync_scheduler():
    """启动定时同步调度（仅 OBS 模式）。"""
    from src.storage import get_storage
    if get_storage() is None:
        log.info("skill sync scheduler: OBS not enabled, skip")
        return
    interval = float(os.environ.get("SKILL_SYNC_INTERVAL", "1800"))
    t = asyncio.create_task(_skill_sync_worker(interval), name="skill-sync")
    _bg_tasks.add(t)  # 持有强引用，防止 GC 回收导致任务静默消失
    t.add_done_callback(_bg_tasks.discard)
    log.info(f"skill sync scheduler started (interval={interval}s)")


async def stop_skill_sync_scheduler():
    """优雅关闭：cancel 所有定时任务并等待退出。"""
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    log.info("skill sync scheduler stopped")
