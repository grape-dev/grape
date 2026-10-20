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

幂等:(task_id, seq) 联合唯一。seq 由 stable_seq() 按 (source, model, turn) 确定性生成,
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
    " provider_raw, model_raw, model_key,"
    " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
    " cost_usd, cost_status, updated_at"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))"
    " ON CONFLICT(dedup_key) DO UPDATE SET "
    "task_id=excluded.task_id, seq=excluded.seq, turn=excluded.turn, source=excluded.source,"
    " provider_raw=excluded.provider_raw, model_raw=excluded.model_raw, model_key=excluded.model_key,"
    " input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,"
    " cache_read_tokens=excluded.cache_read_tokens, cache_write_tokens=excluded.cache_write_tokens,"
    " reasoning_tokens=excluded.reasoning_tokens,"
    " cost_usd=excluded.cost_usd, cost_status=excluded.cost_status,"
    " updated_at=datetime('now')"
)


def record_usage(task_id: str, usages: Iterable[Dict[str, Any]], source: str = "report") -> int:
    """写台账。usages 为记录列表(每条可含 provider/model/cost_status + 任意别名 token 字段)。

    返回实际写入/更新的行数。任何异常都不向上抛——用量采集绝不能拖垮任务主链路。
    """
    items: List[Dict[str, Any]] = [u for u in (usages or []) if isinstance(u, dict)]
    if not task_id or not items:
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
            attr = _attribution(task_id, c)
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
                    f"{task_id}:{seq}", task_id, seq, turn, source,
                    attr.get("repo_key", ""), attr.get("scene_id", ""),
                    attr.get("agent_type", ""), attr.get("engine_id", 0), attr.get("user_id", 0),
                    normalize_provider(u.get("provider") or u.get("provider_raw") or ""),
                    model_raw, model_key,
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
    log.info(f"llm_usage: task={task_id} source={source} rows={written}")
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


def task_usage(task_id: str) -> List[Dict[str, Any]]:
    """单个任务的用量行(P1 任务详情用)。"""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT model_key, model_raw, source, turn,"
                " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
                " reasoning_tokens, cost_usd, cost_status FROM obs_llm_usage"
                " WHERE task_id=? ORDER BY model_key, turn", (task_id,))
            return _dict_rows(c)
    except Exception as e:
        log.warning(f"llm_usage: read failed task={task_id}: {e}")
        return []


def summarize(repo_key: Optional[str] = None, since: Optional[str] = None,
              limit: int = 50) -> List[Dict[str, Any]]:
    """按模型聚合(P1 总览 ⑨ 图用)。"""
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