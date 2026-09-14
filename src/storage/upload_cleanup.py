"""临时上传文件定时清理模块（Issue-54）。

职责：每小时清理 uploaded_files 表中 scope='temp' 且超过 24 小时的孤儿文件，
同时删除对应的 OBS/本地物理文件。

集群并发安全：通过 distributed_locks 表分布式锁互斥（src/lock.py），
同一时间只有一个实例执行清理。

触发方式：
- asyncio 定时任务（启动后每 1 小时执行一次）
- 失败不阻塞服务，打 WARNING 日志
"""
import asyncio
import logging
import os

from src.config import log
from src.session_manager import db, db_lock

logger = logging.getLogger(__name__)
_bg_tasks: set[asyncio.Task] = set()

_CLEANUP_INTERVAL = 3600.0  # 每 1 小时执行一次
_MAX_TEMP_AGE_HOURS = 24    # 超过 24 小时的 temp 文件清理
_LOCK_KEY = "upload_cleanup"
_LOCK_TTL = 300             # 锁 TTL 5 分钟（清理任务不会超过 5 分钟）


def _cleanup_temp_files():
    """清理超过 24 小时的临时上传文件（scope='temp'）。

    集群分布式锁互斥，抢不到锁直接跳过。
    """
    from src.lock import acquire_lock, release_lock
    from src.user.db import list_temp_uploaded_files_before, hard_delete_uploaded_file

    acquired = acquire_lock(_LOCK_KEY, ttl=_LOCK_TTL)
    if not acquired:
        log.info("Upload cleanup: lock contention, skipped (another instance is running)")
        return

    try:
        temp_files = list_temp_uploaded_files_before(_MAX_TEMP_AGE_HOURS)
        if not temp_files:
            return

        cleaned = 0
        for f in temp_files:
            file_key = f.get("file_key", "")
            file_id = f.get("id", 0)
            if not file_key:
                hard_delete_uploaded_file(file_id)
                cleaned += 1
                continue

            # 删物理文件
            try:
                from src.storage import get_storage
                storage = get_storage()
                if storage:
                    storage.delete_object(file_key)
                else:
                    # 非 OBS 模式：删本地文件
                    static_dir = os.path.join(os.path.dirname(__file__), "..", "api", "static", "uploads", "scene-icons")
                    local_path = os.path.join(static_dir, os.path.basename(file_key))
                    if os.path.exists(local_path):
                        os.remove(local_path)
            except Exception as e:
                logger.warning(f"Upload cleanup: delete file '{file_key}' failed: {e}")

            # 硬删除 DB 记录
            hard_delete_uploaded_file(file_id)
            cleaned += 1

        if cleaned > 0:
            log.info(f"Upload cleanup: removed {cleaned} temp files (age > {_MAX_TEMP_AGE_HOURS}h)")
    finally:
        release_lock(_LOCK_KEY)


async def _upload_cleanup_worker(interval: float = _CLEANUP_INTERVAL):
    """定时清理 worker：启动后等 5 分钟首次执行（避免和启动同步抢资源），之后每 interval 秒。"""
    await asyncio.sleep(300)  # 启动后 5 分钟首次执行
    while True:
        try:
            _cleanup_temp_files()
        except asyncio.CancelledError:
            logger.info("Upload cleanup worker cancelled")
            raise
        except Exception:
            logger.exception("Upload cleanup run failed")
        await asyncio.sleep(interval)


def start_upload_cleanup_scheduler():
    """启动定时清理调度。"""
    t = asyncio.create_task(_upload_cleanup_worker(_CLEANUP_INTERVAL), name="upload-cleanup")
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    log.info(f"Upload cleanup scheduler started (interval={_CLEANUP_INTERVAL}s, max_age={_MAX_TEMP_AGE_HOURS}h)")


async def stop_upload_cleanup_scheduler():
    """优雅关闭：cancel 所有定时任务并等待退出。"""
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    log.info("Upload cleanup scheduler stopped")
