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


_ATTACH_MAX_AGE_HOURS = 48   # 附件有效期 48 小时，不论任务成功与否


def _cleanup_stale_attachments():
    """清理超过 48h 的附件（不论任务成功与否）。

    判断依据：附件对象的 last_modified 时间，超过 48h 即删除。
    集群分布式锁互斥。
    """
    from datetime import datetime, timedelta, timezone
    from src.lock import acquire_lock, release_lock
    from src.config import OBS_PREFIX

    acquired = acquire_lock("attach_cleanup", ttl=_LOCK_TTL)
    if not acquired:
        log.info("Attachment cleanup: lock contention, skipped")
        return

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_ATTACH_MAX_AGE_HOURS)
        cleaned = 0

        from src.storage import get_storage
        storage = get_storage()

        if storage:
            # OBS 模式：遍历 OBS 对象，按 last_modified 判断
            prefix = (OBS_PREFIX or "").strip("/")
            _attach_prefix = f"{prefix}/issue_attachments/"
            cursor = ""
            while True:
                items, cursor = storage.list_objects_with_meta(_attach_prefix, cursor=cursor, limit=1000)
                for item in items:
                    key = item["key"]
                    last_mod = item.get("last_modified", "")
                    if not last_mod:
                        continue
                    try:
                        mod_dt = datetime.fromisoformat(last_mod.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if mod_dt < cutoff:
                        storage.delete_object(key)
                        cleaned += 1
                if not cursor:
                    break
        else:
            # 本地模式：扫描目录按文件修改时间判断
            _ATTACH_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "issue_attachments")
            _ATTACH_DIR = os.path.abspath(_ATTACH_DIR)
            if not os.path.isdir(_ATTACH_DIR):
                return
            import shutil
            for owner in os.listdir(_ATTACH_DIR):
                owner_dir = os.path.join(_ATTACH_DIR, owner)
                if not os.path.isdir(owner_dir):
                    continue
                for repo in os.listdir(owner_dir):
                    repo_dir = os.path.join(owner_dir, repo)
                    if not os.path.isdir(repo_dir):
                        continue
                    for issue_dir_name in os.listdir(repo_dir):
                        issue_dir = os.path.join(repo_dir, issue_dir_name)
                        if not os.path.isdir(issue_dir):
                            continue
                        # 检查目录下所有文件的修改时间，全部超过 48h 才删目录
                        all_stale = True
                        for fname in os.listdir(issue_dir):
                            fpath = os.path.join(issue_dir, fname)
                            if not os.path.isfile(fpath):
                                continue
                            try:
                                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
                                if mtime >= cutoff:
                                    all_stale = False
                                    break
                            except Exception:
                                pass
                        if all_stale:
                            shutil.rmtree(issue_dir, ignore_errors=True)
                            cleaned += 1

        if cleaned > 0:
            log.info(f"Attachment cleanup: removed {cleaned} stale attachment objects (age > {_ATTACH_MAX_AGE_HOURS}h)")
    finally:
        release_lock("attach_cleanup")


async def _upload_cleanup_worker(interval: float = _CLEANUP_INTERVAL):
    """定时清理 worker：启动后等 5 分钟首次执行（避免和启动同步抢资源），之后每 interval 秒。

    P1-D (Issue #357): Distributed lock ensures only one worker executes
    cleanup when uvicorn runs with --workers N.
    """
    from src.runtime.task_lock import run_with_distributed_lock
    await asyncio.sleep(300)  # 启动后 5 分钟首次执行
    while True:
        try:
            async with run_with_distributed_lock("upload_cleanup", ttl=600):
                _cleanup_temp_files()
                _cleanup_stale_attachments()
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
