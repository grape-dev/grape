"""主控机内运行的引擎用量采集（chat / devops / im / wiki）。

**为什么需要它**（审计 2026-10-17）：`src/runtime/usage_collect.py` 的采集器原本只在 **worker 侧**
（`src/runtime/agent.py`）被调用 → 主控机内直接跑的引擎（chat agent、devops agent、IM bot、视频理解）
的 token 用量**一条都没进台账**。引擎原生账本（hermes `state.db`/hwcloud 日志/opencode `message.data`）
在主控侧同样存在，采集代码也完全可复用，缺的只是"谁来调"。

**归属口径（2026-10-17 决定 B）**：这类运行没有 `agent_tasks` 行，因此
`obs_llm_usage.task_id` 落空串，主体放 `run_id`（如 `chat:<chat_task_id>`），
幂等键用 `{run_kind}:{run_id}:{seq}`，归属（repo/scene/agent_type/user/engine）由调用方通过
`attr=` 传入 —— **只允许内部代码调用**，不走 HTTP 上报口（否则归属可被外部伪造，成本口径不可信）。

用法（生成式调用点的标准三行，绝不能影响主链路）：

    _mu = MasterRun("chat", chat_task_id, engine=backend_name, cwd=cwd,
                    attr={"repo_key": repo_key, "scene_id": conv_id,
                          "agent_type": "chat", "user_id": user["id"]})
    _mu.start()
    try:
        for evt_type, data in stream_...():
            _mu.feed(evt_type, data)     # 流式引擎（codex）靠这个拿每轮 usage
            ...
    finally:
        _mu.finish(session_id=_new_sid)  # 收尾写台账；任何异常只打日志
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class MasterRun:
    """一次主控机内引擎运行的用量采集器（幂等、绝不上抛）。"""

    def __init__(self, run_kind: str, run_id: str, engine: str, cwd: str = "",
                 session_id: str = "", attr: Optional[Dict[str, Any]] = None,
                 source: str = ""):
        self.run_kind = (run_kind or "").strip().lower()
        self.run_id = (run_id or "").strip()
        self.engine = (engine or "").strip().lower()
        self.cwd = cwd or ""
        self.session_id = session_id or ""
        self.attr = dict(attr or {})
        self.source = (source or "").strip()
        self._collector = None
        self._started = False
        self._done = False

    # ---- 内部：构造采集器（引擎账本读取复用 worker 侧同一份实现）----
    def _new_collector(self):
        from src.runtime.usage_collect import UsageCollector
        return UsageCollector(self.engine, cwd=self.cwd, session_id=self.session_id)

    def start(self) -> None:
        """运行开始前调用：记累计型账本（hermes/hwcloud）的基线。失败静默降级。"""
        if not self.run_id or not self.engine:
            return
        try:
            self._collector = self._new_collector()
            self._collector.start()
            self._started = True
        except Exception as e:      # pragma: no cover - 采集失败不影响运行
            log.warning(f"master usage: start failed run={self.run_kind}:{self.run_id}: {e}")
            self._collector = None

    def feed(self, evt_type: str, data: Any) -> None:
        """把引擎事件喂给采集器（流式引擎如 codex 用）。失败静默。"""
        if not self._collector:
            return
        try:
            self._collector.feed(evt_type, data)
        except Exception:
            pass

    def finish(self, session_id: str = "") -> int:
        """运行结束调用：采集并**直接写台账**（不走 HTTP 上报口）。返回写入行数。

        - `session_id`：引擎本次实际使用的会话号（opencode 新会话只有跑完才知道，
          由调用方从 done 事件的 `oc_session_id` 取）。
        - 累计型账本（hermes/hwcloud）由后台线程异步合批落盘，读数可能滞后 → 与 worker 侧
          同样留一次短重试（1.0s），避免稳定少计。
        - 幂等：重复调用直接返回（`_done` 守卫）；写库失败只打日志。
        """
        if self._done or not self._started or not self._collector:
            return 0
        self._done = True
        sid = session_id or self.session_id or ""
        try:
            usages = self._collector.finish(session_id=sid)
            if not usages and self.engine in ("hermes", "hwcloud", "aishell"):
                time.sleep(1.0)
                usages = self._collector.finish(session_id=sid)
            if not usages:
                return 0
            return self._write(usages)
        except Exception as e:
            log.warning(f"master usage: finish failed run={self.run_kind}:{self.run_id}: {e}")
            return 0

    def _write(self, usages: List[Dict[str, Any]]) -> int:
        """逐条落台账（保留 per-record 的 source，例如 `hermes:title_generation`）。"""
        from src.observability import llm_usage as lu
        from src.runtime.agent import _usage_payload     # 与 worker 侧同一份字段归一

        n = 0
        for u in usages:
            payload = _usage_payload(u, self.engine)
            src = self.source or str(payload.get("source") or "") or self.engine or "master"
            # run_kind='task'：主体就是 task_id（record_usage 会据此反查 agent_tasks 补归属，
            # 且幂等键与 worker 采集一致 → 同一任务不会因为跨本机/worker 重复计）
            _tid = self.run_id if self.run_kind == "task" else ""
            n += lu.record_usage(_tid, [payload], source=src, run_kind=self.run_kind,
                                 run_id=self.run_id, attr=self.attr)
        if n:
            log.info(f"master usage: run_kind={self.run_kind} run={self.run_id} "
                     f"engine={self.engine} rows={n}")
        return n


def for_task(task_id: str, engine: str, cwd: str = "", session_id: str = "", *,
             seq: Any = "", repo_key: str = "", agent_type: str = "", user_id: int = 0,
             engine_id: int = 0, kind_fallback: str = "devops", source: str = "") -> "MasterRun":
    """`for_task`：给**事件流水线 / autopilot 这类"通常有 agent_tasks 行"**的运行用。

    这类运行的 `task_id` 由 pipeline 自己创建（`_create_direct_task` / event_pipeline 的 INSERT），
    所以：
    - `task_id` 有值 → `run_kind='task'`：归属由 `record_usage` 从 agent_tasks **自动反查**
      （repo/user/agent_type 现成，不需要调用方传 attr），幂等键沿用 `{task_id}:{seq}`，
      与 worker 侧的采集**完全一致** —— 同一任务先本机跑、后重跑到 worker 也不会重复计。
    - `task_id` 为空（少数 `d_<seq>` 兜底路径）→ `run_kind=kind_fallback` + attr 兜底。
    """
    attr = {"repo_key": repo_key, "agent_type": agent_type,
            "user_id": int(user_id or 0), "engine_id": int(engine_id or 0)}
    if task_id:
        # task 口径：归属以 agent_tasks 为准，attr 只做补空（如 task 行 runtime_id 为空时补引擎 id）
        return MasterRun("task", str(task_id), engine, cwd=cwd, session_id=session_id,
                         source=source, attr=attr)
    _rid = f"{kind_fallback}:{seq or new_run_id(kind_fallback)}"
    return MasterRun(kind_fallback, _rid, engine, cwd=cwd, session_id=session_id, source=source,
                     attr=attr)


def new_run_id(prefix: str) -> str:
    """给"一次运行"造主体标识（调用点拿不到现成 id 时用；会话维度仍留在 `scene_id`）。"""
    import uuid
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


def wrap_generator(run_kind: str, run_id: str, engine: str, gen, *, cwd: str = "",
                   session_id: str = "", attr: Optional[Dict[str, Any]] = None,
                   source: str = ""):
    """把"生成器 + 收尾"包成一个生成器：边转发边 feed，结束时自动 finish。

    适合调用点不方便插三行的地方（例如 `async for`/桥接函数内部）：

        for evt_type, data in wrap_generator("im", msg_id, engine, stream_agent(...), cwd=cwd):
            ...
    """
    mu = MasterRun(run_kind, run_id, engine, cwd=cwd, session_id=session_id,
                   attr=attr, source=source)
    mu.start()
    try:
        for item in gen:
            try:
                evt_type, data = item
            except Exception:
                yield item
                continue
            mu.feed(evt_type, data)
            yield item
    finally:
        mu.finish()