"""引擎侧 LLM 用量账本采集(worker 侧)。

设计文档:docs/weekly-07/observability-llm-usage-design.md §3.1

**为什么不是"解析 ACP 事件流"**:实测两个 ACP 引擎的 `usage_update` 都不带计费 token——
hermes 只给 {size, used}(上下文压力),hwcloud 给 {cost, used, size}(有成本、无 input/output 拆分)。
所以只能读各引擎自己的账本:

| 引擎 | 账本 | 语义 | 归属键 |
|---|---|---|---|
| codex | 事件流 `turn.completed.usage` | 每轮**增量** | 进程内 |
| opencode | SQLite `message.data.tokens` | 每消息**增量** | session_id |
| hermes | SQLite `session_model_usage` | **累计值** | cwd-hash 隔离目录 |
| hwcloud | `<iso>/logs/hwcloud.log` | 每 run**增量** | cwd-hash 隔离目录 |

**增量 vs 累计**:增量源直接 SUM;累计源(hermes)必须在任务开始时记基线、结束时取差值,
否则续接会话会把历史任务的用量重复计入。hwcloud 日志同理——按字节偏移只读本次新增部分。

**读取纪律**:引擎 SQLite 是 WAL 模式(现场有 `-wal`/`-shm` 三件套),进程退出后未 checkpoint
时只读连接可能读不到最新数据 ⇒ 一律**先拷三件套到临时目录再读**,不对现场库开连接。

**失败纪律**:任何异常都降级为"本次无用量",绝不抛给调用方——用量采集不能拖垮任务主链路。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 路径解析

def _cwd_hash(cwd: str) -> str:
    """与 src/agent/{hermes,hwcloud}_acp.py 的隔离目录算法保持一致。"""
    return hashlib.md5((cwd or ".").encode()).hexdigest()[:12]


def _hermes_iso_home(cwd: str) -> str:
    root = os.path.join(os.path.expanduser("~"), ".cache", "hermes-acp-homes")
    return os.path.join(root, _cwd_hash(cwd))


def _hwcloud_iso_home(cwd: str) -> str:
    root = os.path.join(os.path.expanduser("~"), ".cache", "hwcloud-acp-homes")
    return os.path.join(root, _cwd_hash(cwd))


def _opencode_db_path() -> str:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(data_home, "opencode", "opencode.db")


def _open_sqlite_readonly(path: str, workdir: str, copy_max_mb: int = 64) -> Optional[sqlite3.Connection]:
    """打开引擎 SQLite 账本(只读)。

    小库(≤ copy_max_mb)拷贝 db + -wal + -shm 三件套到临时目录再读:进程已退出且 WAL 未
    checkpoint 时,直接对现场库开只读连接可能读不到最新数据(设计文档 §3.1「WAL 陷阱」)。
    大库(opencode 生产库实测 750MB+)整库拷贝的代价不可接受 ⇒ 直接只读连接,失败再退化为拷贝。
    """
    if not os.path.exists(path):
        return None
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return None

    if size_mb > copy_max_mb:
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except Exception as e:
            log.warning(f"usage_collect: direct readonly open failed {path}: {e}")

    base = os.path.join(workdir, os.path.basename(path))
    try:
        shutil.copy2(path, base)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(path + suffix):
                shutil.copy2(path + suffix, base + suffix)
        return sqlite3.connect(f"file:{base}?mode=ro", uri=True)
    except Exception as e:
        log.warning(f"usage_collect: copy/open sqlite failed {path}: {e}")
        return None


def _sum_tokens(row: Dict[str, Any]) -> int:
    return sum(int(row.get(k) or 0) for k in
               ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens"))


# ---------------------------------------------------------------- 采集器

class UsageCollector:
    """任务级用量采集器:start() 记基线 → 执行 → finish() 取本次增量。

    codex 无落盘账本,由事件流喂入(`feed`);其余三个引擎读本地账本。
    """

    def __init__(self, engine: str, cwd: str = "", session_id: str = "",
                 opencode_db: str = ""):
        self.engine = (engine or "").strip().lower()
        self.cwd = cwd or ""
        self.session_id = session_id or ""
        self.opencode_db = opencode_db or _opencode_db_path()

        self._started = False
        self._baseline: Dict[str, Any] = {}
        self._fed: List[Dict[str, Any]] = []          # codex 事件流累积
        self._turn = 0
        self._tmpdir: Optional[str] = None

    # -------------------------------------------------- 生命周期

    def start(self) -> None:
        """任务开始前调用:记累计型账本的基线。失败静默降级。"""
        self._started = True
        try:
            if self.engine == "hermes":
                self._baseline = {"hermes": self._hermes_snapshot()}
            elif self.engine in ("hwcloud", "aishell"):
                # aishell 是 hwcloud 的换名,采集路径一致
                self._baseline = {"hwcloud": self._hwcloud_offset()}
            elif self.engine == "opencode":
                self._baseline = {"opencode": self._opencode_watermark()}
        except Exception as e:
            log.warning(f"usage_collect: baseline failed engine={self.engine}: {e}")
            self._baseline = {}

    def feed(self, evt_type: str, data: Any) -> None:
        """流式引擎(codex)在事件循环里喂事件。"""
        if evt_type != "step_finish" or not isinstance(data, dict):
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        self._turn += 1
        rec = dict(usage)
        rec["turn"] = self._turn
        self._fed.append(rec)

    def finish(self, session_id: str = "") -> List[Dict[str, Any]]:
        """任务结束后调用:返回本次任务的用量记录列表(可能为空)。

        session_id:引擎在本次运行中实际使用的会话号(opencode 新会话只有跑完才知道,
        由调用方从 done 事件取 oc_session_id 传入),覆盖构造时的值。
        """
        if session_id:
            self.session_id = session_id
        try:
            if self.engine == "codex":
                return self._fed
            if self.engine == "opencode":
                return self._opencode_usage()
            if self.engine == "hermes":
                return self._hermes_usage()
            if self.engine in ("hwcloud", "aishell"):
                return self._hwcloud_usage()
            return []
        except Exception as e:
            log.warning(f"usage_collect: finish failed engine={self.engine}: {e}")
            return []
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _workdir(self) -> str:
        if not self._tmpdir:
            self._tmpdir = tempfile.mkdtemp(prefix="grape-usage-")
        return self._tmpdir

    # -------------------------------------------------- opencode

    def _opencode_watermark(self) -> int:
        """基线 = 全库当前最大 time_created(毫秒)。

        不按 session 取:新会话的 session_id 要跑完才知道,start() 时拿不到。
        用全库水位即可精确圈出"本任务新写入的消息"(opencode 的 time_created 单调递增)。
        """
        conn = _open_sqlite_readonly(self.opencode_db, self._workdir())
        if conn is None:
            return 0
        try:
            row = conn.execute("SELECT MAX(time_created) FROM message").fetchone()
            return int(row[0] or 0) if row else 0
        except Exception as e:
            log.warning(f"usage_collect: opencode watermark failed: {e}")
            return 0
        finally:
            conn.close()

    def _opencode_usage(self) -> List[Dict[str, Any]]:
        """按 session 聚合 assistant 消息的 tokens(只取基线之后的新消息)。"""
        if not self.session_id:
            return []
        watermark = int((self._baseline or {}).get("opencode") or 0)
        conn = _open_sqlite_readonly(self.opencode_db, self._workdir())
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT data, time_created FROM message"
                " WHERE session_id=? AND time_created > ? ORDER BY time_created",
                (self.session_id, watermark))
            agg: Dict[str, Dict[str, Any]] = {}
            for data, _ts in cur.fetchall():
                try:
                    obj = json.loads(data)
                except (TypeError, ValueError):
                    continue
                tk = obj.get("tokens") or {}
                if not tk:
                    continue
                model = str(obj.get("modelID") or "")
                key = model or "unknown"
                a = agg.setdefault(key, {
                    "model": model, "provider": str(obj.get("providerID") or ""),
                    "input": 0, "output": 0, "reasoning": 0,
                    "cache_read": 0, "cache_write": 0, "turn": 0,
                })
                a["input"] += int(tk.get("input") or 0)
                a["output"] += int(tk.get("output") or 0)
                a["reasoning"] += int(tk.get("reasoning") or 0)
                cache = tk.get("cache") or {}
                a["cache_read"] += int(cache.get("read") or 0)
                a["cache_write"] += int(cache.get("write") or 0)
                a["turn"] += 1
                cost = obj.get("cost")
                if isinstance(cost, (int, float)) and cost:
                    a["cost_usd"] = float(cost)
            return [v for v in agg.values() if any(
                v.get(k) for k in ("input", "output", "reasoning", "cache_read", "cache_write"))]
        finally:
            conn.close()

    # -------------------------------------------------- hermes

    def _hermes_rows(self) -> Dict[str, Dict[str, int]]:
        """{key: 计数} —— key = f"{session_id}|{model}|{task}"。"""
        iso = _hermes_iso_home(self.cwd)
        db_path = os.path.join(iso, "state.db")
        conn = _open_sqlite_readonly(db_path, self._workdir())
        if conn is None:
            return {}
        try:
            cur = conn.execute(
                "SELECT session_id, model, COALESCE(task,''), input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, reasoning_tokens"
                " FROM session_model_usage")
            out: Dict[str, Dict[str, int]] = {}
            for sid, model, task, i, o, cr, cw, rz in cur.fetchall():
                out[f"{sid}|{model}|{task}"] = {
                    "input": int(i or 0), "output": int(o or 0),
                    "cache_read": int(cr or 0), "cache_write": int(cw or 0),
                    "reasoning": int(rz or 0),
                }
            return out
        except Exception as e:
            log.warning(f"usage_collect: hermes read failed: {e}")
            return {}
        finally:
            conn.close()

    def _hermes_snapshot(self) -> Dict[str, Dict[str, int]]:
        return self._hermes_rows()

    def _hermes_usage(self) -> List[Dict[str, Any]]:
        """累计值 → 取差值(本任务新增部分)。续接会话时这是唯一正确做法。"""
        before: Dict[str, Dict[str, int]] = (self._baseline or {}).get("hermes") or {}
        after = self._hermes_rows()
        recs: List[Dict[str, Any]] = []
        for key, cur in after.items():
            base = before.get(key) or {}
            delta = {k: max(int(cur.get(k, 0)) - int(base.get(k, 0)), 0)
                     for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
            if not any(delta.values()):
                continue
            _sid, model, task = (key.split("|", 2) + ["", ""])[:3]
            recs.append({
                "model": model,
                "provider": "",
                "input": delta["input"], "output": delta["output"],
                "cache_read": delta["cache_read"], "cache_write": delta["cache_write"],
                "reasoning": delta["reasoning"],
                # 辅助调用(title_generation 等)按模型归并,但在 source 上留痕可区分
                "_source_suffix": task,
            })
        return recs

    # -------------------------------------------------- hwcloud / aishell

    def _hwcloud_log(self) -> str:
        return os.path.join(_hwcloud_iso_home(self.cwd), "logs", "hwcloud.log")

    def _hwcloud_offset(self) -> int:
        """基线 = 日志当前字节数(日志是 append-only,只看本次新增)。"""
        try:
            return os.path.getsize(self._hwcloud_log())
        except OSError:
            return 0

    def _hwcloud_usage(self) -> List[Dict[str, Any]]:
        path = self._hwcloud_log()
        offset = int((self._baseline or {}).get("hwcloud") or 0)
        if not os.path.exists(path):
            return []
        # 日志被轮转/重建(当前长度 < 基线)→ 退化为整文件解析,避免漏采
        if os.path.getsize(path) < offset:
            offset = 0
        agg: Dict[str, Dict[str, Any]] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if not line or "prompt_tokens" not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if o.get("msg") != "agent end":
                        continue
                    model = str(o.get("model") or "")
                    key = model or "unknown"
                    a = agg.setdefault(key, {"model": model, "provider": "",
                                             "input": 0, "output": 0, "turn": 0})
                    a["input"] += int(o.get("prompt_tokens") or 0)
                    a["output"] += int(o.get("completion_tokens") or 0)
                    a["turn"] += 1
        except Exception as e:
            log.warning(f"usage_collect: hwcloud log read failed: {e}")
            return []
        return [v for v in agg.values() if v.get("input") or v.get("output")]


def collect_usage(engine: str, cwd: str = "", session_id: str = "",
                  opencode_db: str = "") -> List[Dict[str, Any]]:
    """一次性采集(无法记基线时的兜底:仅适用于独立会话的引擎)。

    优先用 UsageCollector 的 start/finish 两段式——累计型账本(hermes/hwcloud 日志)
    没有基线会重复计入历史。本函数只做静态读取,不做差值。
    """
    uc = UsageCollector(engine, cwd=cwd, session_id=session_id, opencode_db=opencode_db)
    uc._started = True
    return uc.finish()