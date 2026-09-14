"""Per-task resource lifecycle manager (Cordis effect/dispose pattern adapted for Grape).

All temporary resources (overlay dirs, tmp files, tracked procs) register here.
On task completion (success/failure/exception), call dispose() to release
everything in LIFO order - guaranteed cleanup on any path.
"""
import contextlib
import glob
import logging
import os
import shutil
import signal

log = logging.getLogger("devops-bot")


class TaskResourceStack:
    """Per-task resource container. Registers overlays, procs, tmp patterns.

    Usage:
        res = TaskResourceStack(task_id, cwd)
        res.track_overlay("/tmp/hermes-acp-xxx")
        res.track_proc(proc)
        res.track_tmp_pattern("/tmp/tf-deploy-*")
        # ... execute task ...
        res.dispose()  # cleanup everything (idempotent)
    """

    def __init__(self, task_id: str = "", cwd: str = ""):
        self.task_id = task_id
        self.cwd = cwd
        self._stack = contextlib.ExitStack()
        self._overlays: list[str] = []
        self._procs: list = []
        self._tmp_patterns: list[str] = []
        self._disposed = False

    def track_overlay(self, path: str):
        """Register a per-task HERMES_HOME overlay dir for cleanup."""
        self._overlays.append(path)

    def track_proc(self, proc):
        """Register a subprocess for tracking + termination on dispose."""
        self._procs.append(proc)
        try:
            from src.agent.executor import _track_proc
            _track_proc(proc)
        except Exception:
            pass

    def untrack_proc(self, proc):
        """Manually untrack a proc (after normal exit, before dispose)."""
        if proc in self._procs:
            self._procs.remove(proc)
        try:
            from src.agent.executor import _untrack_proc
            _untrack_proc(proc)
        except Exception:
            pass

    def track_tmp_pattern(self, pattern: str):
        """Register a glob pattern for /tmp cleanup."""
        self._tmp_patterns.append(pattern)

    def dispose(self, kill_procs: bool = True, keep_overlays: bool = False):
        """Release all tracked resources. Idempotent.

        keep_overlays: True 时保留 overlay 目录（用于 hermes ACP 稳定 iso_home，
        state.db 需持久化供下次 session/resume，chat-ctx-fix §方向一）。
        """
        if self._disposed:
            return
        self._disposed = True

        # 1. Terminate tracked procs still alive
        if kill_procs:
            for proc in self._procs:
                self._kill_proc(proc)
                try:
                    from src.agent.executor import _untrack_proc
                    _untrack_proc(proc)
                except Exception:
                    pass

        # 2. Remove overlay dirs (unless keep_overlays)
        if not keep_overlays:
            for path in self._overlays:
                try:
                    shutil.rmtree(path, ignore_errors=True)
                except Exception:
                    pass

        # 3. Clean tmp patterns
        for pattern in self._tmp_patterns:
            for path in glob.glob(pattern):
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass

        # 4. Close ExitStack
        try:
            self._stack.close()
        except Exception:
            pass

        log.info(
            f"TaskResourceStack disposed: task={self.task_id} "
            f"procs={len(self._procs)} overlays={len(self._overlays)} "
            f"tmp_patterns={len(self._tmp_patterns)}"
        )

    def _kill_proc(self, proc):
        """SIGTERM -> 3s grace -> SIGKILL."""
        try:
            if hasattr(proc, "poll") and proc.poll() is not None:
                return  # already exited
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    # ===== Issue #134: 健康度查询接口（增强层，不改派发语义）=====

    def get_engine_health(self, runtime_id: int) -> dict:
        """查询单个引擎的健康度数据（委托 health_scorer 模块）。

        返回 {health_score, total_tasks, failed_tasks, consecutive_failures, ...}
        """
        try:
            from src.runtime.health_scorer import get_health_score
            return get_health_score(runtime_id)
        except Exception:
            return {"health_score": 100, "total_tasks": 0, "failed_tasks": 0,
                    "consecutive_failures": 0, "failure_rate": 0.0}

    def is_engine_healthy(self, runtime_id: int) -> bool:
        """判断引擎是否健康（健康分 >= 阈值）。"""
        try:
            from src.runtime.health_scorer import is_healthy
            return is_healthy(runtime_id)
        except Exception:
            return True  # 模块异常时不阻塞派发

    def filter_healthy_engines(self, runtime_ids: list[int]) -> list[int]:
        """从候选引擎列表中过滤健康的（增强层入口）。

        全部不健康时降级兜底取最优机器，始终返回非空列表（只要输入非空）。
        """
        try:
            from src.runtime.health_scorer import filter_or_fallback
            return filter_or_fallback(runtime_ids)
        except Exception:
            return list(runtime_ids)  # 模块异常时不过滤（不阻塞派发）
