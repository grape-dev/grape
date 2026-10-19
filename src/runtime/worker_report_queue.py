"""worker 侧补报队列（graceful-shutdown-requeue-design.md Step 4）。

背景：worker 完成/失败任务后经 HTTP 上报 master（RuntimeClient _request 仅 4 次退避≈60s），
master 停机窗口内上报失败即丢；worker 重连后也不补报 → 「已完成未上报」任务滞留 running，
只能被 recover_orphans/zombie 回炉或 force_fail（间接误标/重跑）。

本模块提供**本地持久化补报队列**：任务完成/失败先入队（内存 + jsonl 落盘，重启不丢），
master 恢复后由 worker 重放（带 run_token，master 幂等+守卫）。上报 ack 后出队。

用法（agent 侧）：
    q = WorkerReportQueue()
    q.add(entry)                      # 先入队
    ok = await client.complete(..., run_token=entry["run_token"])
    if ok is not None: q.remove(task_id)      # ack 后出队
    await replay_pending_reports(client, q)   # 启动/重连/周期调
"""
import json
import os
import threading
import time

from src.config import log

_TTL_SECONDS = int(os.environ.get("GRAPE_WORKER_REPORT_TTL", str(24 * 3600)))
_CAP = int(os.environ.get("GRAPE_WORKER_REPORT_CAP", "10000"))


def _default_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".grape", "worker-report-queue.jsonl")


class WorkerReportQueue:
    """本地持久化补报队列（线程安全）。条目:
    {task_id, status: completed|failed, result_text, exit_code, session_id, work_dir,
     engine, runtime_id, run_token(=认领 started_at), created_at}
    """

    def __init__(self, path: str = "", ttl: int = _TTL_SECONDS, cap: int = _CAP):
        self.path = path or _default_path()
        self.ttl = ttl
        self.cap = cap
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(e, dict) and e.get("task_id"):
                        self._pending[e["task_id"]] = e
        except Exception as e:
            log.warning(f"worker_report_queue: load failed: {e}")

    def _flush(self):
        try:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                for e in self._pending.values():
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning(f"worker_report_queue: flush failed: {e}")

    def add(self, entry: dict):
        entry = dict(entry)
        entry.setdefault("created_at", time.time())
        with self._lock:
            self._pending[entry.get("task_id") or entry["task_id"]] = entry
            # 容量上限：超限删除最旧（优先 TTL 之外的，其次最早入队）
            if len(self._pending) > self.cap:
                _now = time.time()
                keys = list(self._pending.keys())
                keys.sort(key=lambda k: self._pending[k].get("created_at", 0))
                for k in keys:
                    if len(self._pending) <= self.cap:
                        break
                    if (_now - (self._pending[k].get("created_at") or 0)) > self.ttl or len(self._pending) > self.cap:
                        self._pending.pop(k, None)
            self._flush()

    def remove(self, task_id: str):
        with self._lock:
            if self._pending.pop(task_id, None) is not None:
                self._flush()

    def prune_expired(self) -> int:
        _now = time.time()
        with self._lock:
            old = [k for k, v in self._pending.items() if _now - (v.get("created_at") or 0) > self.ttl]
            for k in old:
                self._pending.pop(k, None)
            if old:
                self._flush()
            return len(old)

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._pending.values()]

    def size(self) -> int:
        with self._lock:
            return len(self._pending)


async def replay_pending_reports(client, queue: WorkerReportQueue):
    """重放补报队列：对每条 pending 调 master complete/fail（带 run_token），ack 后出队。"""
    pruned = queue.prune_expired()
    if pruned:
        log.info(f"worker_report_queue: pruned {pruned} expired report(s)")
    for entry in queue.pending():
        task_id = entry.get("task_id") or ""
        run_token = entry.get("run_token") or ""
        try:
            if entry.get("status") == "failed":
                resp = await client.fail(task_id, entry.get("result_text") or "", entry.get("exit_code") or -1,
                                         run_token=run_token)
            else:
                resp = await client.complete(task_id, entry.get("result_text") or "", entry.get("exit_code") or 0,
                                             session_id=entry.get("session_id") or "",
                                             work_dir=entry.get("work_dir") or "",
                                             engine=entry.get("engine") or "",
                                             run_token=run_token)
            if resp is not None:
                queue.remove(task_id)
                log.info(f"worker_report_queue: replayed {task_id} ({entry.get('status')})")
        except Exception as e:
            log.warning(f"worker_report_queue: replay {task_id} failed: {e}")