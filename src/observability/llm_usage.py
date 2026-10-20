"""可观测性:LLM token 用量台账(obs_llm_usage)。

设计文档:docs/weekly-07/observability-llm-usage-design.md

三条铁律(改动前先读设计文档 §4):
1. **追加不覆盖**——一个任务可能多轮、多模型(主模型 + title_generation 等辅助模型),
   一律 INSERT 多行。
2. **绝不写 agent_tasks.usage_json**——该列是"会话续接指针"(session_id/work_dir/engine),
   被 routes/events.py、im/handler_async.py、routes/chat.py 消费,归 complete_task 独占。
   历史上 /tasks/{id}/usage 端点用 UPDATE ... SET usage_json=? 覆盖它,一旦真有引擎调用
   就会冲掉指针、破坏多轮续接与 B2 引擎守卫。
3. **归属由主控反查**——repo_key/scene_id/agent_type/user_id/engine_id 取自 agent_tasks,
   不接受上报方传值,避免用量归属被写错或伪造。

4. **运行主体分两类(2026-10-17 决定)**:
   - `run_kind='task'`（默认,worker 任务）:`task_id` 是 agent_tasks.id,`run_id` 同值;
     幂等键沿用**老格式** `{task_id}:{seq}` —— 升级后 worker 重报仍命中同一行,不会翻倍。
   - 其它 `run_kind`（`chat`/`devops`/`im`/`wiki` 等**主控机内直接跑的引擎**）:
     **没有 agent_tasks 行** → `task_id` 写空串,主体放 `run_id`(如 `chat:<conv_id>:<msg_id>`),
     幂等键用 `{run_kind}:{run_id}:{seq}`;归属(repo/scene/agent_type/user/engine)由调用方
     通过 `attr=` 传入——这类调用只允许**内部代码**发起,不走 HTTP 上报口(归属不接受外部传值)。

幂等:(运行主体, seq) 唯一。seq 由 stable_seq() 按 (source, model, turn) 确定性生成,
重报命中同一行走 UPSERT 覆盖——worker 重试不会把用量翻倍。

语义差异(实测结论,见设计文档 §3.1):
- codex / opencode / hwcloud 账本是**增量**,上报前已 SUM;
- hermes session_model_usage 是**累计值**,适配器侧取差值后才上报。
本模块只负责落库,不关心增量/累计。
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger("observability.llm_usage")

# ---------------------------------------------------------------- 字段归一化

# 各引擎对同一语义的字段叫法不同,统一映射到台账列
_TOKEN_FIELDS: Dict[str, Sequence[str]] = {
    "input_tokens": ("input_tokens", "input", "prompt_tokens", "prompt"),
    "output_tokens": ("output_tokens", "output", "completion_tokens", "completion"),
    "cache_read_tokens": ("cache_read_tokens", "cache_read", "cache_read_input_tokens"),
    "cache_write_tokens": ("cache_write_tokens", "cache_write", "cache_creation_input_tokens"),
    "reasoning_tokens": ("reasoning_tokens", "reasoning", "reasoning_output_tokens"),
}

_MODEL_SEP_RE = re.compile(r"[\s_]+")


def _to_int(v: Any) -> int:
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else 0
    if isinstance(v, str):
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return 0
        return n if n > 0 else 0
    return 0


def normalize_tokens(raw: Dict[str, Any]) -> Dict[str, int]:
    """把任意引擎的 usage 字典拍平成台账的 token 列(缺失=0)。"""
    out: Dict[str, int] = {}
    for col, aliases in _TOKEN_FIELDS.items():
        for a in aliases:
            got = _to_int(raw.get(a))
            if got:
                out[col] = got
                break
        out.setdefault(col, 0)
    return out


def normalize_model(name: str) -> str:
    """P0 轻量归一化:去空白、转小写、去掉 provider 前缀、空白/下划线→连字符。

    只解决"大小写/空白/前缀"这类纯噪声。`claude-sonnet-4.5` 与 `claude-sonnet-4-5`
    这类真正的别名合并交给 P1 的 obs_model_map 表(设计文档 §5.1),这里不过度归一化——
    归错了比不归更糟。
    """
    s = (name or "").strip().lower()
    if "/" in s:
        s = s.split("/")[-1]           # anthropic/claude-x → claude-x
    s = _MODEL_SEP_RE.sub("-", s)
    return s.strip("-")


def normalize_provider(name: str) -> str:
    return (name or "").strip().lower()


# ---------------------------------------------------------------- 幂等键

def stable_seq(source: str, model_key: str, turn: int = 0) -> int:
    """确定性 seq:同一 (source, model, turn) 永远得到同一个值。

    重复上报命中 dedup_key 唯一约束 → UPSERT 覆盖,token 不会翻倍。
    掩到 31 位,避开 MySQL 有符号 INT 上限(2147483647)。
    """
    raw = f"{source}|{model_key}|{turn}".encode("utf-8")
    return int(hashlib.sha1(raw).hexdigest()[:8], 16) & 0x7FFFFFFF


# ---------------------------------------------------------------- 归属反查

def _attribution(task_id: str, cursor) -> Dict[str, Any]:
    """从 agent_tasks 反查归属维度。查不到就返回空(不阻塞落库)。"""
    try:
        cursor.execute(
            "SELECT repo_key, agent_type, runtime_id, user_id, issue_id "
            "FROM agent_tasks WHERE id=?", (task_id,))
        row = cursor.fetchone()
    except Exception as e:  # pragma: no cover - 表缺失/连接异常
        log.warning(f"llm_usage attribution lookup failed task={task_id}: {e}")
        return {}
    if not row:
        return {}
    if isinstance(row, dict):
        repo_key, agent_type, runtime_id, user_id, issue_id = (
            row.get("repo_key"), row.get("agent_type"), row.get("runtime_id"),
            row.get("user_id"), row.get("issue_id"))
    else:
        repo_key, agent_type, runtime_id, user_id, issue_id = row
    return {
        "repo_key": repo_key or "",
        "agent_type": agent_type or "",
        "engine_id": int(runtime_id or 0),
        "user_id": int(user_id or 0),
        "scene_id": _scene_of(repo_key or "", issue_id or ""),
    }


def _scene_of(repo_key: str, issue_id: str) -> str:
    """场景归属(P0 尽力而为):查不到就空串,不编造。"""
    if not repo_key or not issue_id:
        return ""
    try:
        from src.api.session_store import get_issue_scene
        got = get_issue_scene(repo_key, issue_id)
        if got and got[0]:
            return got[0]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 落库

_INSERT_SQL = (
    "INSERT INTO obs_llm_usage ("
    "dedup_key, task_id, seq, turn, source, repo_key, scene_id, agent_type, engine_id, user_id,"
    " provider_raw, model_raw, model_key, run_id, run_kind,"
    " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
    " cost_usd, cost_status, updated_at"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))"
    " ON CONFLICT(dedup_key) DO UPDATE SET "
    "task_id=excluded.task_id, seq=excluded.seq, turn=excluded.turn, source=excluded.source,"
    " provider_raw=excluded.provider_raw, model_raw=excluded.model_raw, model_key=excluded.model_key,"
    " input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,"
    " cache_read_tokens=excluded.cache_read_tokens, cache_write_tokens=excluded.cache_write_tokens,"
    " reasoning_tokens=excluded.reasoning_tokens,"
    " cost_usd=excluded.cost_usd, cost_status=excluded.cost_status,"
    " updated_at=datetime('now')"
)

# run_kind 取值（其它值也接受，只是排不进 KNOWN 分类）
RUN_KINDS = ("task", "chat", "devops", "im", "wiki")


def dedup_key_for(run_kind: str, task_id: str, run_id: str, seq: int) -> str:
    """幂等键：**worker 任务沿用老格式** `{task_id}:{seq}`（升级后重报仍命中老行，不翻倍）；
    其它运行类型用 `{run_kind}:{run_id}:{seq}`。
    """
    if (run_kind or "task") == "task":
        return f"{task_id}:{seq}"
    return f"{run_kind}:{run_id}:{seq}"


# ---------------------------------------------------------------- 年度归档

ARCHIVE_KEEP_DAYS = 365     # 台账保留近一年；更早的行搬到 obs_llm_usage_archive


def archive_old_usage(keep_days: int = ARCHIVE_KEEP_DAYS, batch: int = 2000) -> Dict[str, int]:
    """把超过保留期的台账行**搬**到 `obs_llm_usage_archive`（审计决定 2026-10-17：年度归档）。

    为什么不是简单 DELETE：台账是计费/报表依据，删掉就再也算不出来了；归档表结构镜像台账，
    报表需要跨年对账时可 UNION 两张表（或只查归档表）。

    实现要点：
    - `created_at` 是 **数据库本地时间串**（`datetime('now')` 默认值，不是 ISO！审计踩过）——
      因此粗筛同时给「本地格式」与「ISO 格式」两个边界（OR，两条都能用上 created_at 索引），
      再用 `normalize_event_time` 精确判定，两种存法都不会漏/误搬。
    - 列集合用 `PRAGMA table_info` 取两张表的**交集**（不含 id）→ 以后给台账加列，归档自动跟上，
      不会因为列数不一致而失败。
    - 分批（默认 2000 行/次），单次失败不影响主循环；返回 {archived, checked}。
    """
    from src.session_manager import db, db_lock
    out = {"archived": 0, "checked": 0}
    try:
        now = datetime.now(timezone.utc)
        cut = now - timedelta(days=keep_days)
        cut_iso = cut.isoformat()
        # 本地格式边界：库侧 datetime('now') 写的是数据库本地时间（生产为 CST）
        try:
            from src.api.time_utils import normalize_event_time as _nzt
        except Exception:
            _nzt = None
        cut_local = (cut + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        with db_lock:
            c = db.cursor()
            live_cols = [r[1] for r in c.execute("PRAGMA table_info(obs_llm_usage)").fetchall()]
            arc_cols = [r[1] for r in c.execute("PRAGMA table_info(obs_llm_usage_archive)").fetchall()]
            cols = [x for x in live_cols if x in arc_cols and x != "id"]
            if not cols:
                log.warning("llm_usage archive: no shared columns, skip")
                return out
            c.execute("SELECT id, created_at FROM obs_llm_usage"
                      " WHERE created_at IS NOT NULL AND created_at != ''"
                      "   AND (created_at < ? OR created_at < ?)"
                      " ORDER BY created_at ASC LIMIT ?",
                      (cut_iso, cut_local, int(batch)))
            rows = c.fetchall()
            out["checked"] = len(rows)
            ids: List[int] = []
            for rid, ts in rows:
                _t = str(ts)
                if _nzt is not None:
                    try:
                        _n = _nzt(_t)
                        if isinstance(_n, str) and _n:
                            _t = _n
                    except Exception:
                        pass
                if _t < cut_iso:                  # 归一后统一 ISO 比较
                    ids.append(int(rid))
            if not ids:
                return out
            ph = ",".join("?" * len(ids))
            collist = ", ".join(cols)
            c.execute(f"INSERT INTO obs_llm_usage_archive ({collist})"
                      f" SELECT {collist} FROM obs_llm_usage WHERE id IN ({ph})", tuple(ids))
            moved = int(c.rowcount or 0)
            c.execute(f"DELETE FROM obs_llm_usage WHERE id IN ({ph})", tuple(ids))
            db.commit()
            out["archived"] = moved
            if moved:
                log.info(f"llm_usage archive: moved {moved} rows older than {keep_days}d")
    except Exception as e:
        log.warning(f"llm_usage archive failed: {e}")
    return out


def record_usage(task_id: str, usages: Iterable[Dict[str, Any]], source: str = "report",
                 run_kind: str = "task", run_id: str = "",
                 attr: Optional[Dict[str, Any]] = None) -> int:
    """写台账。usages 为记录列表(每条可含 provider/model/cost_status + 任意别名 token 字段)。

    - `run_kind='task'`（默认）:worker 任务的用量上报,`task_id` 必填,归属从 agent_tasks 反查。
    - 其它 `run_kind`（chat/devops/im/wiki）:主控机内直接跑的引擎,**没有 agent_tasks 行** →
      `task_id` 落空串、主体用 `run_id`（如 `chat:<conv_id>:<msg_id>`），归属由 `attr=` 传入
      （只允许内部代码调用；HTTP 上报口不接受外部传值,避免归属被伪造成本口径)。
      `attr` 支持 repo_key / scene_id / agent_type / engine_id / user_id。

    返回实际写入/更新的行数。任何异常都不向上抛——用量采集绝不能拖垮任务主链路。
    """
    kind = (run_kind or "task").strip() or "task"
    subject = task_id if kind == "task" else (run_id or task_id)
    items: List[Dict[str, Any]] = [u for u in (usages or []) if isinstance(u, dict)]
    if not subject or not items:
        return 0
    if kind == "task" and not task_id:
        return 0
    try:
        from src.session_manager import db, db_lock
    except Exception as e:  # pragma: no cover
        log.warning(f"llm_usage: session_manager unavailable: {e}")
        return 0

    written = 0
    try:
        with db_lock:
            c = db.cursor()
            _given = dict(attr or {})       # 调用方（内部代码）提供的归属，仅作兜底
            if kind == "task":
                # 归属以 agent_tasks 反查为准；调用方给的只做**补空**——
                # 例如 task 行的 runtime_id 为空时，用调用方知道的引擎 id 兜住。
                attr = _attribution(task_id, c)
                for _k, _v in _given.items():
                    if not attr.get(_k) and _v:
                        attr[_k] = _v
            else:
                attr = _given
            for u in items:
                model_raw = str(u.get("model") or u.get("model_raw") or "")
                model_key = normalize_model(model_raw) or "unknown"
                turn = _to_int(u.get("turn"))
                seq = stable_seq(source, model_key, turn)
                tok = normalize_tokens(u)
                if not any(tok.values()):
                    continue          # 全 0 不落库(0 与"未知"是两件事,别污染台账)
                cost = u.get("cost_usd", u.get("cost"))
                try:
                    cost = float(cost) if cost is not None else None
                except (TypeError, ValueError):
                    cost = None
                c.execute(_INSERT_SQL, (
                    dedup_key_for(kind, task_id, subject, seq),
                    task_id if kind == "task" else "",
                    seq, turn, source,
                    attr.get("repo_key", ""), attr.get("scene_id", ""),
                    attr.get("agent_type", ""), attr.get("engine_id", 0), attr.get("user_id", 0),
                    normalize_provider(u.get("provider") or u.get("provider_raw") or ""),
                    model_raw, model_key, subject, kind,
                    tok["input_tokens"], tok["output_tokens"],
                    tok["cache_read_tokens"], tok["cache_write_tokens"],
                    tok["reasoning_tokens"],
                    cost, str(u.get("cost_status") or ""),
                ))
                written += 1
            db.commit()
    except Exception as e:
        log.warning(f"llm_usage: record failed task={task_id}: {e}")
        try:
            with db_lock:
                db.rollback()
        except Exception:
            pass
        return 0
    log.info(f"llm_usage: run_kind={kind} subject={subject} source={source} rows={written}")
    return written


# ---------------------------------------------------------------- 汇总查询

def _plain(v: Any) -> Any:
    """pymysql 会把 SUM/REAL 列返回成 Decimal——转成 JSON 可序列化的原始类型。"""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _dict_rows(cursor) -> List[Dict[str, Any]]:
    cols = [d[0] for d in cursor.description]
    return [{k: _plain(v) for k, v in zip(cols, r)} for r in cursor.fetchall()]


_USAGE_COLS = ("model_key, model_raw, source, turn,"
               " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
               " reasoning_tokens, cost_usd, cost_status")


def _query_usage(where: str = "", params: Sequence[Any] = (), order: str = " ORDER BY model_key, turn",
                 limit: int = 0) -> List[Dict[str, Any]]:
    """用量明细的共用读法（L1/L2/详情页都用同一份列投影）。"""
    try:
        from src.session_manager import db, db_lock
        sql = f"SELECT {_USAGE_COLS} FROM obs_llm_usage" + where + order
        ps: List[Any] = list(params)
        if limit:
            sql += " LIMIT ?"
            ps.append(int(limit))
        with db_lock:
            c = db.cursor()
            c.execute(sql, tuple(ps))
            return _dict_rows(c)
    except Exception as e:
        log.warning(f"llm_usage: read failed ({where.strip()[:40]}): {e}")
        return []


def task_usage(task_id: str) -> List[Dict[str, Any]]:
    """单个**任务**的用量行（P1 任务详情用）。只出 run_kind='task' 的行。"""
    return _query_usage(" WHERE task_id=? AND run_kind='task'", (str(task_id),))


def run_usage(run_kind: str, run_id: str) -> List[Dict[str, Any]]:
    """某个**非任务运行**（chat/devops/im/wiki）的用量明细。"""
    return _query_usage(" WHERE run_kind=? AND run_id=?", (str(run_kind), str(run_id)))


def usage_by_kind(since: Optional[str] = None) -> List[Dict[str, Any]]:
    """按运行类型聚合（报表/对账用）：每类多少行、多少 token。"""
    sql = ("SELECT run_kind, COUNT(*) AS rows_cnt, SUM(input_tokens) AS input_tokens,"
           " SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,"
           " SUM(reasoning_tokens) AS reasoning_tokens FROM obs_llm_usage")
    params: List[Any] = []
    if since:
        sql += " WHERE created_at >= ?"
        params.append(since)
    sql += " GROUP BY run_kind ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC"
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(sql, tuple(params))
            return _dict_rows(c)
    except Exception as e:
        log.warning(f"llm_usage: usage_by_kind failed: {e}")
        return []


def summarize(repo_key: Optional[str] = None, since: Optional[str] = None,
              limit: int = 50, run_kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """按模型聚合(P1 总览 ⑨ 图用)。`run_kind` 非空时只算该类型（task/chat/...）。"""
    sql = (
        "SELECT model_key, COUNT(*) AS calls,"
        " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,"
        " SUM(cache_read_tokens) AS cache_read_tokens,"
        " SUM(cache_write_tokens) AS cache_write_tokens,"
        " SUM(reasoning_tokens) AS reasoning_tokens, SUM(cost_usd) AS cost_usd"
        " FROM obs_llm_usage WHERE 1=1")
    params: List[Any] = []
    if repo_key:
        sql += " AND repo_key=?"
        params.append(repo_key)
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if run_kind:
        sql += " AND run_kind=?"
        params.append(str(run_kind))
    sql += " GROUP BY model_key ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC LIMIT ?"
    params.append(int(limit))
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(sql, tuple(params))
            cols = [d[0] for d in c.description]
            return [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception as e:
        log.warning(f"llm_usage: summarize failed: {e}")
        return []