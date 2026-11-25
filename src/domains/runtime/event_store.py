"""Event Store — 事件记录与查询（shared events table）"""
import json, re, threading, asyncio, random, time, contextlib, os
from contextlib import nullcontext as _nullcontext, contextmanager as _contextmanager
from datetime import datetime
from src.kernel.owner_db import db_pair

db, db_lock = db_pair("runtime")
from src.kernel.lock_instrument import task_lock, lock_owner, event_lock
from src.kernel.config import log
from src.kernel.issue_ident import is_actionable_issue_number, normalize_issue_ident  # Issue #3055
from src.kernel.time_utils import normalize_event_time
from src.kernel.event_payload_view import payload_view_from_json
from src.kernel.a2a_context import a2a_event_context_key
from src.kernel.context_digest import build_digest, render_compact
from src.domains.runtime.agents.failure_info import (
    finalize_failure_info,
    failure_info_json,
    parse_failure_info,
)

_EVENTS_COLUMNS_CACHE = None


def _events_columns(c) -> set:
    """events 表的列清单（SQLite 走 PRAGMA，MySQL 走 information_schema）。

    历史写法直接 `PRAGMA table_info(events)`：三库 MySQL 下这句原样发给服务端 → 1064，
    整条终态 UPDATE 被 except 吞掉 → 事件永远停在 running（2026-11-16 #9820 实证：
    派发失败后连「标记 failed」都写不进去，UI 一直显示 running/无引擎）。
    """
    global _EVENTS_COLUMNS_CACHE
    if _EVENTS_COLUMNS_CACHE is not None:
        return _EVENTS_COLUMNS_CACHE
    # 探列单一入口（kernel.ddl_safety）：PRAGMA（compat 翻译）+ 裸 MySQL
    # information_schema 回退。原实现自己拼两种方言，PR #2009 I2 收敛。
    from src.kernel.ddl_safety import table_columns

    cols = set(table_columns(c, "events"))
    _EVENTS_COLUMNS_CACHE = cols
    return cols


# ====== Per-repo 锁（从旧 event_processor 迁移）======
# 2026-08-16 事故修复：由 threading.Lock 改为 asyncio.Lock。
# 旧实现在异步事件循环里同步 acquire，同仓库并发消息会死锁整机（feishu-chat-event-loop-deadlock-fix.md）。
_repo_locks = {}
_repo_locks_lock = threading.Lock()


def acquire_repo_lock(repo_key: str) -> asyncio.Lock:
    with _repo_locks_lock:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = asyncio.Lock()
        return _repo_locks[repo_key]


def _extract_issue_id_from_text(text: str) -> str:
    """从文本中提取 Issue/PR 编号（支持 #44、/issues/42、/pulls/26、/merge_requests/3）"""
    if not text:
        return ""
    import re
    # 优先匹配 GitCode 链接格式
    m = re.search(r'/(?:issues|pulls|merge_requests)/(\d+)', text)
    if m:
        return f"#{m.group(1)}"
    # 其次匹配 #数字（但不能是 0）
    m = re.search(r'#(\d+)', text)
    if m and m.group(1) != "0":
        return f"#{m.group(1)}"
    return ""


def _extract_issue_id_from_link(text: str) -> str:
    """仅从 Issue/PR 链接中提取编号（Issue #1312：裸 #N 不作为归属来源）。"""
    if not text:
        return ""
    import re
    m = re.search(r'/(?:issues|pulls|merge_requests)/(\d+)', text)
    if m and m.group(1) != "0":
        return f"#{m.group(1)}"
    return ""


def _extract_entity_type_from_text(text: str) -> str:
    """从文本中提取实体类型（issue vs pull_request）。

    根据 URL 路径区分：/issues/N → "issue"，/pulls/N 或 /merge_requests/N → "pull_request"。
    仅 #N 格式（无 URL 路径）返回 ""（未知，默认按 issue 处理）。
    """
    if not text:
        return ""
    import re
    m = re.search(r'/issues/(\d+)', text)
    if m:
        return "issue"
    m = re.search(r'/(?:pulls|merge_requests)/(\d+)', text)
    if m:
        return "pull_request"
    return ""


def _insert_event_row(c, repo_key: str, event_type: str, title: str, issue_id: str,
                      agent_type: str, source: str, actor: str, summary: str,
                      payload: dict, status: str, result: str, scene_id: str,
                      entity_type: str, idempotency_key: str,
                      completed_at: str) -> int:
    """在调用方已持 db_lock/事务内 INSERT 一条事件，返回 seq（失败返回 0）。

    不 commit：由调用方统一提交，便于排队幂等「查重 + 插入」在同一事务内原子完成
    （Issue #1667 审查修复②）。
    """
    from src.domains.mgmt.public import get_org_id_by_repo_key
    _org_id = get_org_id_by_repo_key(repo_key)
    _payload_json = json.dumps(payload or {})
    if idempotency_key:
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status, result, scene_id, entity_type, org_id, idempotency_key, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (repo_key, issue_id, event_type, source, agent_type, title, actor, summary,
             _payload_json, status, result, scene_id, entity_type, _org_id,
             idempotency_key, completed_at))
    else:
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status, result, scene_id, entity_type, org_id, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (repo_key, issue_id, event_type, source, agent_type, title, actor, summary,
             _payload_json, status, result, scene_id, entity_type, _org_id,
             completed_at))
    _seq = c.lastrowid
    # 验证行确实写入（防止 WAL 并发写入冲突时 lastrowid 返回旧值）
    c.execute("SELECT seq FROM events WHERE seq=?", (_seq,))
    if not c.fetchone():
        log.warning(f"Event store FAILED: INSERT returned lastrowid={_seq} but row not found "
                    f"(WAL conflict?), repo={repo_key}, type={event_type}")
        return 0
    return _seq


def _existing_seq_for_idempotency(repo_key: str, idempotency_key: str) -> int:
    """幂等键撞唯一索引 → 返回已存在事件 seq 的负值；否则 0。"""
    if not idempotency_key:
        return 0
    _existing = find_event_by_idempotency_key(repo_key, idempotency_key)
    if _existing:
        return -int(_existing["seq"])
    return 0


def append_event(repo_key: str, event_type: str, title: str = "",
                 issue_id: str = "", agent_type: str = "", source: str = "gitcode",
                 actor: str = "", summary: str = "",
                 payload: dict = None, status: str = "completed",
                 result: str = "", scene_id: str = "",
                 entity_type: str = "", idempotency_key: str = "",
                 completed_at: str = "") -> int:
    """写入事件到 events 表，返回 seq。result 为 Agent 答复结论（纯文本）。

    Issue #1497：终态 status（completed/failed/cancelled/timeout）INSERT 时
    同事务写 completed_at（终态⇔completed_at 非空不变量）——本函数是 a2a 派发、
    跨流水线交接、订阅、admin 审计、技能解析失败等直写终态入口的统一收口点；
    调用方显式传入 completed_at 时优先使用。非终态 status 不写。

    issue_id：传入为空时仅信任结构化来源（payload 的 issue_number / issue 对象 /
    链接），裸 #N 不再解析；无法解析时写空串表示"无关联"（Issue #1312）。
    scene_id: PR 订阅等场景级事件持久化场景ID，空字符串表示无关联场景。
    entity_type: 实体类型（"issue" / "pull_request"），区分编号相同的 issue 与 PR。
    idempotency_key: A2A 幂等键（Issue #544 L2）。非空时写入列并受
    (repo_key, idempotency_key) 唯一索引约束；撞键时返回已存在事件 seq 的负值
    （调用方据此走"返回已有事件"分支），异常仍返回 0。
    """
    # 兜底解析 issue_id
    if not issue_id:
        if isinstance(payload, dict):
            # A2A 用 issue_number 字段，或 issue/pull_request 对象
            num = payload.get("issue_number", "")
            if not num:
                for key in ("issue", "pull_request", "merge_request"):
                    obj = payload.get(key)
                    if isinstance(obj, dict):
                        num = obj.get("number", obj.get("iid", ""))
                        if num:
                            break
            if num:
                _num = str(num).lstrip("#")
                # Issue #3055：ident（gitee IK*）同样兜底为 #<ident>；0 占位仍不兜底
                if is_actionable_issue_number(_num):
                    issue_id = f"#{normalize_issue_ident(_num)}"
        if not issue_id:
            issue_id = _extract_issue_id_from_link(result) or _extract_issue_id_from_link(summary) \
                or _extract_issue_id_from_link(title)
    _seq = 0
    # Issue #1497：终态 INSERT 同事务写 completed_at（已有非空值由调用方显式传入保留）
    if status in ("completed", "failed", "cancelled", "timeout") and not completed_at:
        from datetime import datetime, timezone as _tz
        completed_at = datetime.now(_tz.utc).isoformat()
    with db_lock:
        c = db.cursor()
        try:
            _seq = _insert_event_row(
                c, repo_key, event_type, title, issue_id, agent_type, source, actor,
                summary, payload, status, result, scene_id, entity_type,
                idempotency_key, completed_at)
            if not _seq:

                db.rollback()
                _seq = _existing_seq_for_idempotency(repo_key, idempotency_key)
                return _seq
            db.commit()
            payload_keys = list((payload or {}).keys())
            log.info(f"Event stored: seq={_seq}, repo={repo_key}, type={event_type}, "
                     f"agent={agent_type}, issue={issue_id}, actor={actor}, source={source}, "
                     f"status={status}, result={result[:80]}, payload_keys={payload_keys}")
        except Exception as e:
            log.warning(f"Failed to store event: {e}")
            # Issue #544 L2：幂等键撞唯一索引 → 返回已存在事件 seq 的负值（调用方转"返回已有事件"）
            _existing = _existing_seq_for_idempotency(repo_key, idempotency_key)
            if _existing:
                return _existing
            return 0
    # WAL checkpoint 在 db_lock 释放后执行（避免阻塞其他需要 db_lock 的操作）
    try:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    return _seq


def append_queued_a2a_event(repo_key: str, *, issue_id: str, agent_type: str,
                            title: str, summary: str, payload: dict,
                            context_key: str, idempotency_key: str = "") -> int:
    """原子写入排队 A2A 事件（Issue #1667 审查修复②：queue 幂等原子化）。

    在**同一 db_lock/事务**内完成「同上下文已排队 → 复用」与「插入新排队行」：
    - 返回 >0：新插入的排队事件 seq；
    - 返回 <0：同上下文已有排队事件，值为 `-existing_seq`（调用方复用，不重复派发）；
    - 返回 0：写入失败。

    并发同一 context_key 的 queue 请求只会插入一行（先前查后写在锁内串行化）；
    显式 idempotency_key 撞唯一索引时同样回落为已存在 seq 的负值。
    """
    _payload = dict(payload or {})
    _qmeta = _payload.get("_a2a_queue")
    if not isinstance(_qmeta, dict):
        _qmeta = {}
    _qmeta["context_key"] = context_key
    _payload["_a2a_queue"] = _qmeta
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, idempotency_key, payload FROM events "
                "WHERE repo_key=? AND issue_id=? AND agent_type=? "
                "AND event_type='a2a_event' AND status='queued' ORDER BY seq ASC",
                (repo_key, issue_id, agent_type))
            for _seq, _ik, _raw in (c.fetchall() or []):
                _row_key = ""
                try:
                    _p = json.loads(_raw or "{}")
                    if isinstance(_p, dict):
                        _qm = _p.get("_a2a_queue") or {}
                        if isinstance(_qm, dict):
                            _row_key = str(_qm.get("context_key") or "")
                except Exception:
                    _row_key = ""
                if not _row_key:
                    _row_key = a2a_event_context_key(_ik or "", _raw or "")
                if not context_key or _row_key == context_key:
                    return -int(_seq)
            _seq = _insert_event_row(
                c, repo_key, "a2a_event", title, issue_id, agent_type, "a2a", "",
                summary, _payload, "queued", "", "", "", idempotency_key, "")
            if not _seq:
                db.rollback()
                return 0
            db.commit()
            return _seq
        except Exception as e:
            log.warning(f"append_queued_a2a_event failed repo={repo_key} issue={issue_id} "
                        f"agent={agent_type}: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            _existing = _existing_seq_for_idempotency(repo_key, idempotency_key)
            if _existing:
                return _existing
            return 0


def find_event_by_idempotency_key(repo_key: str, idempotency_key: str) -> dict | None:
    """Issue #544 L2：按 (repo_key, idempotency_key) 查已有 A2A 事件（核对/去重共用）。"""
    if not repo_key or not idempotency_key:
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, status, created_at,"
                " completed_at, payload FROM events"
                " WHERE repo_key=? AND idempotency_key=? ORDER BY seq DESC LIMIT 1",
                (repo_key, idempotency_key))
            row = c.fetchone()
        if not row:
            return None
        return {"seq": row[0], "issue_id": row[1] or "", "event_type": row[2] or "",
                "agent_type": row[3] or "", "title": row[4] or "", "status": row[5] or "",
                "created_at": row[6] or "", "completed_at": row[7] or "",
                "payload": row[8] or ""}
    except Exception as e:
        log.warning(f"find_event_by_idempotency_key failed: {e}")
        return None


def find_recent_same_event(repo_key: str, issue_id: str, agent_type: str,
                           event_type: str, window_seconds: int = 60) -> int:
    """同 (repo,issue,agent,event_type) 在近窗内是否已有事件，返回其 seq（无则 0）。

    时间口径：cutoff 由 db_now_str 按存储层口径生成（MySQL=CST 墙钟 / SQLite=UTC），
    与 events.created_at 的写入口径一致。**不可**改用 _utc_now_str()——那是 Python
    UTC 字符串，与 MySQL 下的 created_at 差 8 小时，会把窗口放大成 8 小时（Issue #1487
    衍生：PR 评论 note_hook_pr 被静默判重吞掉）。
    """
    from src.api.time_utils import db_now_str
    cutoff = db_now_str(-window_seconds)
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT seq FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? "
            "AND event_type=? AND created_at >= ? ORDER BY seq DESC LIMIT 1",
            (repo_key, issue_id or "#0", agent_type, event_type, cutoff))
        row = c.fetchone()
        return row[0] if row else 0


def claim_event_guard(repo_key: str, issue_id: str, agent_type: str) -> bool:
    """原子抢占事件活跃守卫（duplicate-issue-opened-event-fix-design.md 方案 A）。

    (repo, issue, agent) 唯一约束：INSERT OR IGNORE 成功（rowcount=1）= 抢占成功；
    失败 = 已有活跃事件（含近窗），跨进程互斥由数据库唯一索引保证。
    """
    try:
        _now = _utc_now_str()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO event_active_guard (repo_key, issue_id, agent_type, seq, created_at) "
                "VALUES (?,?,?,0,?)",
                (repo_key, issue_id, agent_type, _now))
            db.commit()
            return c.rowcount == 1
    except Exception as e:
        log.warning(f"claim_event_guard failed ({repo_key}, {issue_id}, {agent_type}): {e}")
        return False


def update_event_guard_seq(repo_key: str, issue_id: str, agent_type: str, seq: int) -> None:
    """把守卫行的 seq 记录为对应当前事件（抢占成功后回填）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE event_active_guard SET seq=? WHERE repo_key=? AND issue_id=? AND agent_type=? AND seq=0",
                (seq, repo_key, issue_id, agent_type))
            db.commit()
    except Exception as e:
        log.warning(f"update_event_guard_seq failed seq={seq}: {e}")


def release_event_guard(repo_key: str, issue_id: str, agent_type: str, seq: int) -> None:
    """释放守卫（事件进入终态）。仅当守卫被当前 seq 持有才删除，避免误删新事件守卫。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM event_active_guard WHERE repo_key=? AND issue_id=? AND agent_type=? AND seq=?",
                (repo_key, issue_id, agent_type, seq))
            db.commit()
    except Exception as e:
        log.warning(f"release_event_guard failed seq={seq}: {e}")


# ===== Issue #3794: TTL 清理分批契约 =====
# 旧实现单大事务 DELETE + 相关子查询（agent_tasks 缺 (event_seq,status) 组合索引），
# 与 claim/heartbeat/状态回写互相等锁 → InnoDB 1205 锁等待超时，主控 100% 失败。
# 取数依据（审查 C-1「魔法数五源」；实测脚本见 MR 评论，宿主机 MySQL 8.0 上
# 用 12.1 万行 agent_tasks + 2000 行候选守卫跑出）：
#   单批 DELETE(含 NOT EXISTS 复核、索引在位)：50 行 141ms / 100 行 138ms /
#   200 行 138ms / 500 行 138ms —— 批大小在 50~500 之间对单批持锁时长不敏感
#   （差 3ms，开销主要是语句与索引查找的固定成本），所以 200 取的是「够用且不浪费
#   IN 列表」的中值，不是性能拐点。
#   _PURGE_BATCH=200  ← 仓内既有先例：runtime/pipeline/title_backfill.py:88 的
#                       _TITLE_MARKER_QUERY_CHUNK=200（同域、同「热表分批」形态）。
#                       线上实测 event_active_guard 常态仅 7 行（2026-11-24，runtime 库），
#                       一批即可清空；200 是上限而非期望值。
#   _PURGE_MAX_ROUNDS=50 → 10000 行/次 ← 实测常态 7 行，上限留 3 个数量级余量；
#                       有界是为了「清理永不空转」（后台 30s 循环里的同步调用，不能无界）。
#   _LOCK_RETRY_BACKOFF=0.2 ← 仓内既有先例：runtime/im/notify.py:46 的
#                       _RETRY_BACKOFF=0.4（单次亚秒级重试）。取一半，因为这里只重试一次；
#                       +U(0,0.1) 抖动是为了四台主控不同步撞同一行锁。
_PURGE_BATCH = 200            # 单批最大行数（两段短事务，避免长持锁）
_PURGE_MAX_ROUNDS = 50        # 单次调用最多轮数（10000 行/次上限，防无界循环/空转）
_LOCK_RETRY_BACKOFF_SECONDS = 0.2  # 1205 短退避基线（加随机抖动，避免多主控同步重试）
# 可重试的 InnoDB 锁冲突：1205 锁等待超时 / 1213 死锁（后者由引擎自动回滚事务，
# 语义同样是「稍后重试即可」）。失败一次就放弃会让 TTL 自愈继续卡住（Issue #3794）。
_RETRYABLE_LOCK_ERROR_CODES = (1205, 1213)
# 单批 DELETE 的行锁等待上限（秒）：MySQL 默认 50s，在进程级 db_lock 内等满会冻全站
# 语句级行锁上限：3s。实测依据（Issue #3794 事故，2026-11-24）——
#   旧实现持全局 db_lock 等 InnoDB 行锁，四台主控 6h 内各冻 15~35 次、单次 49~92s；
#   同期客户端 499 在 30.0s 就 abort（浏览器 "signal is aborted without reason"）。
# 30s 是用户可感知的硬预算，取 10%（3s）为单批持锁上限：撞锁时最多放弃一批、下一轮
# 5min 后自愈，不再把 /api/ping 与静态资源一起拖到 30s。
# 实测（同上脚本）：另一会话持 guard 行锁 6s 时，默认 50s → DELETE 等 6149ms 才成功；
# 上限 3s → 3001ms 抛 1205 并放弃本批 —— 这就是「6s 停顿」变「3s 放弃」的量化差别。仓内既有同类取值：
# runtime/public/__init__.py:1341 的引擎 upsert 对同一张 agent_tasks 热表用 5。
_PURGE_LOCK_WAIT_SECONDS = 3

# rowcount 指标（挂 observability 独立 registry；prometheus 不可用时静默跳过）
_PURGE_METRIC = None


def _is_retryable_lock_error(e: BaseException) -> bool:
    """判定可重试的 InnoDB 锁冲突（pymysql: e.args[0] 为错误码；兜底匹配 message）。"""
    args = getattr(e, "args", None)
    if args and isinstance(args[0], int) and args[0] in _RETRYABLE_LOCK_ERROR_CODES:
        return True
    text = str(e).lower()
    return "lock wait timeout" in text or "deadlock" in text


def _purge_metric_inc(result: str, amount: int = 1) -> None:
    """grape_event_guard_purged_total{result=ok|failed}：ok 按删除行数计，failed 计 1。"""
    global _PURGE_METRIC
    try:
        if _PURGE_METRIC is None:
            from prometheus_client import Counter
            from src.domains.insight.observability import registry
            _PURGE_METRIC = Counter(
                "grape_event_guard_purged_total",
                "event_active_guard TTL purge rows (Issue #3794)",
                ["result"], registry=registry)
        _PURGE_METRIC.labels(result=result).inc(amount)
    except Exception as e:  # noqa: BLE001 - prometheus 不可用不影响清理本身
        log.debug(f"event guard purge metric skipped: {e}")


@_contextmanager
def _bounded_lock_wait(c, seconds: int = _PURGE_LOCK_WAIT_SECONDS):
    """把本连接的 InnoDB 行锁等待上限临时压到 `seconds`，退出时还原。

    为什么必须做：`db_lock` 是进程级大锁，DELETE 在锁内等行锁时整个进程（含
    /api/ping 与静态文件）都会排队；MySQL 默认 innodb_lock_wait_timeout=50，
    线上实测单次冻结 49~92s（Issue #3794 事故日志）。取值依据见
    `_PURGE_LOCK_WAIT_SECONDS` 注释。

    降级约定（审查 O3）：SET 被拒（权限/方言/连接异常）时**不抛、不吞掉整批清理**，
    退化成「无上限但仍分批」的旧行为 —— 否则偶发 1205 会变成「每次清理都 0 删除」，
    自愈路径反而彻底失效。还原失败（审查 O2）升为 WARNING 并回滚一次重试，因为这个
    连接随后会被同一线程的 claim/heartbeat 复用，残留 3s 会让那些语句更早抛 1205。
    """
    from src.kernel.session_manager import _is_mysql_backend

    if not _is_mysql_backend():
        yield
        return
    prev = None
    try:
        prev = c.execute("SELECT @@SESSION.innodb_lock_wait_timeout").fetchone()
        prev = int(prev[0]) if prev and prev[0] is not None else None
    except Exception as e:  # noqa: BLE001 - 读不到旧值按 MySQL 默认 50 还原
        log.debug(f"read innodb_lock_wait_timeout failed: {e}")
    try:
        c.execute("SET SESSION innodb_lock_wait_timeout=%d" % int(seconds))
    except Exception as e:  # noqa: BLE001 - 设不上就退化为无上限，不能因此整批失败
        log.warning(f"bound innodb_lock_wait_timeout skipped (degrade to unbounded): {e}")
        yield
        return
    try:
        yield
    finally:
        restore_to = int(prev if prev else 50)
        for attempt in (1, 2):
            try:
                c.execute("SET SESSION innodb_lock_wait_timeout=%d" % restore_to)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    try:
                        db.rollback()  # 先把可能残留的事务清掉，再试一次还原
                    except Exception as rb:  # noqa: BLE001 - 清理语句本身的错误更重要
                        log.debug(f"rollback before restore retry failed: {rb}")
                    continue
                log.warning(
                    f"restore innodb_lock_wait_timeout={restore_to} failed, this connection "
                    f"keeps the bounded wait until reconnect: {e}")


def _delete_guard_batch(cutoff: str, seqs: list) -> int:
    """短事务删除一批守卫行；DELETE 内重新 NOT EXISTS 复核（防 TOCTOU，§33 不回归）。

    select 与 delete 之间 task 可能转 running——只有 DELETE 内复核才不误删长任务守卫。
    独立 db_lock + commit，批间释放锁给 claim/heartbeat，避免 1205。
    """
    placeholders = ",".join("?" * len(seqs))
    with db_lock:
        c = db.cursor()
        try:
            deleted = 0
            with _bounded_lock_wait(c, _PURGE_LOCK_WAIT_SECONDS):
                c.execute(
                    "DELETE FROM event_active_guard WHERE created_at < ? "
                    "AND seq IN (" + placeholders + ") "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM agent_tasks t "
                    "  WHERE t.status IN ('dispatched','running') "
                    "  AND t.event_seq = event_active_guard.seq"
                    ")",
                    (cutoff, *seqs))
                # rowcount 必须在退出 _bounded_lock_wait 之前取：退出时 finally 会再执行一条
                # `SET SESSION innodb_lock_wait_timeout=…` 还原语句，MySQLCompatCursor 的
                # rowcount 会跟着那条 SET 变成 0。真库实测（MySQL 8.0.43）：DELETE 删 3 行
                # 立刻读 rowcount=3，再跑一条 SET 后读为 0 —— 在 with 外读会让批次返回值恒为 0，
                # ok 指标与 purged 日志永不出现（QA 原子回归 D1）。sqlite 分支无 SET，测不出来。
                deleted = int(c.rowcount or 0)
            db.commit()
            return deleted
        except Exception:
            # 失败语句所在事务及其已持行锁必须立刻释放：退避后在同一事务里重试会再撞锁，
            # 并且延长对 claim/heartbeat 的持锁时间（与本次优化目标相反）。
            db.rollback()
            raise


def purge_stale_event_guards(max_age_seconds: int = 1800) -> int:
    """TTL 兜底清理守卫行（进程崩溃来不及终态释放时的自愈；默认 30min）。

    守卫作用域仅为"活跃/近窗"，超时删除后允许同 (issue,agent) 重新建事件。
    Issue #372 §12.7: 从 6h 缩短到 30min——活跃 guard 不应存活这么久，
    异常退出后 30min 内自动恢复，不影响用户重试。
    Issue #372 §33: 跳过有运行中 task 的 guard——长任务不应被 TTL 误清理导致重复派发。
    Issue #3794: 重写为两段短事务按 seq 分批（BATCH=200 × 最多 50 轮）：
    先只读 SELECT 候选 seq（不含相关子查询），再逐批短事务 DELETE（内含复核），
    1205/1213 短退避重试一次；失败 WARNING 带 seq 集合、返回累计已删数，不中断后台循环。
    QA #1981 R2：候选窗口按 seq keyset 严格后移 —— 旧实现候选批全被 running/dispatched
    保护时 `deleted == 0 → break` 会整轮结束，剩余可删过期守卫永远清不掉（头阻塞，
    与「守卫自愈失效」同一症状）。现在无论本批删掉多少，窗口都推进到 max(seq) 之后，
    空转只受 _PURGE_MAX_ROUNDS 上限约束。
    """
    total = 0
    cutoff = _utc_now_str(-max_age_seconds)
    last_seq = -1  # keyset 游标：event_active_guard.seq 为 INTEGER NOT NULL DEFAULT 0
    for round_no in range(1, _PURGE_MAX_ROUNDS + 1):
        # 第一段：短只读事务选候选（无相关子查询，不与 agent_tasks 热表互锁）
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT seq FROM event_active_guard WHERE created_at < ? AND seq > ? "
                    f"ORDER BY seq LIMIT {_PURGE_BATCH}",
                    (cutoff, last_seq))
                rows = c.fetchall()
                # 读完立刻结束只读事务（审查 O4）：单库 MySQL 的默认 REPEATABLE READ 会把
                # 快照留到下一次提交，让后面的 DELETE 复查 NOT EXISTS 看到旧版本（§33 保护变弱）。
                # 这里不持有任何写锁，提交空事务只是把快照丢掉；三库拓扑本来就是每语句 autocommit。
                try:
                    db.commit()
                except Exception as e:  # noqa: BLE001 - 快照清理失败不影响本轮清理
                    log.debug(f"purge candidate snapshot commit failed: {e}")
        except Exception as e:
            log.warning(f"purge_stale_event_guards select failed (round {round_no}): {e}")
            _purge_metric_inc("failed")
            return total
        if not rows:
            break
        seqs = [r[0] for r in rows]
        last_seq = max(seqs)  # 窗口严格后移：受保护批也不会再被下一轮选中
        # 第二段：短删除事务；1205/1213 短退避重试一次
        deleted = None
        for attempt in (1, 2):
            try:
                deleted = _delete_guard_batch(cutoff, seqs)
                break
            except Exception as e:
                if attempt == 1 and _is_retryable_lock_error(e):
                    time.sleep(_LOCK_RETRY_BACKOFF_SECONDS + random.uniform(0, 0.1))
                    continue
                log.warning(
                    f"purge_stale_event_guards failed (round {round_no}, "
                    f"seqs_n={len(seqs)} seqs_range={min(seqs) if seqs else -1}..{max(seqs) if seqs else -1}): {e}")
                _purge_metric_inc("failed")
                return total
        total += deleted or 0
        if deleted:
            # 逐批计入 ok：后续轮次失败提前返回时，前面已删行数也不会从指标里丢失。
            _purge_metric_inc("ok", deleted)
        if len(seqs) < _PURGE_BATCH:
            break  # 不足一批 = 已到尾部
    if total:
        log.info(f"purge_stale_event_guards purged={total}")
    return total


def _utc_now_str(offset_seconds: int = 0) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS'（与 datetime('now') 同形；避免引入 DB 方言差异）。"""
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def append_terminal_suppressed_event(repo_key: str, *, event_type: str, title: str,
                                     issue_id: str, source: str, actor: str, summary: str,
                                     payload: dict, scene_id: str = "",
                                     entity_type: str = "", comment_id: str = "") -> int:
    """Issue #4170：交付终态后 ``issue_comment`` 的「仅记录、不派发」落库。

    终态（承载 PR merged + Issue 仓库态 closed）后的新评论不再派发下游 Agent，
    但仍写一行 ``events``（status='skipped'，payload 带 ``_terminal_suppressed``）
    保留审计轨迹，事件列表/详情可查；同时补 event_skipped 审计。
    保留评论 id 幂等键，重复投递不落重复行。

    返回 seq：>0 新写入；<0 幂等命中已存在（abs 为已存在 seq）；0 写入失败。
    """
    _payload = dict(payload or {})
    _payload["_terminal_suppressed"] = {
        "suppressed": True,
        "reason": "terminal_delivery_suppressed",
        "comment_id": str(comment_id or ""),
        "issue_id": str(issue_id or ""),
    }
    _summary = summary or f"@{actor or 'someone'} 评论了 Issue {issue_id or '?'}（交付终态，仅记录不派发）"
    _seq = append_event(
        repo_key, event_type, title=title, issue_id=issue_id, agent_type="",
        source=source, actor=actor, summary=_summary, payload=_payload,
        status="skipped", result="terminal_delivery_suppressed",
        scene_id=scene_id, entity_type=entity_type,
        idempotency_key=(f"comment:{comment_id}" if comment_id else ""))
    if _seq > 0:
        append_skipped_event(
            repo_key, event_type, "terminal_delivery_suppressed",
            detail=(f"comment_id={comment_id or '?'} issue={issue_id or '?'}"),
            issue_id=issue_id or "")
    elif _seq == 0:
        # events 行写入失败：至少落 event_skipped 审计，避免静默丢失终态评论迹
        log.warning(f"append_terminal_suppressed_event: events row failed, "
                    f"fallback to event_skipped (repo={repo_key}, issue={issue_id}, "
                    f"comment={comment_id or '?'})")
        append_skipped_event(
            repo_key, event_type, "terminal_delivery_suppressed",
            detail=(f"events_insert_failed comment_id={comment_id or '?'} "
                    f"issue={issue_id or '?'}"),
            issue_id=issue_id or "")
    return _seq


def append_skipped_event(repo_key: str, event_type: str, reason: str, detail: str = "",
                         issue_id: str = ""):
    """写入被跳过/无产出的事件审计记录到 event_skipped 表

    issue_id（Issue #89）：关联 Issue 编号，便于流水线页面展示「部分任务尚未生成流水线事件」。
    """
    with db_lock:
        try:
            c = db.cursor()
            c.execute("INSERT INTO event_skipped (repo_key, issue_id, event_type, reason, detail) VALUES (?,?,?,?,?)",
                      (repo_key, issue_id or "", event_type, reason, detail))
            db.commit()
            log.info(f"Event skipped: repo={repo_key}, issue={issue_id or ''}, type={event_type}, reason={reason}, detail={detail[:80]}")
        except Exception as e:
            log.warning(f"Event skipped FAILED: {e}")


# ====== Issue #3444：评论→Issue 事件内容去重 ======
# 背景：同一评论重复落库时评论 id 不同（两个评论 id，已脱敏），既有
# `comment:<id>` 幂等键无法判重，两个 issue 处理事件并行派发。
# 现按「repo + issue + event_type + 评论内容哈希 + 邻近时间窗」判重：
# 首个事件照常派发，后到事件落 status='skipped' 留痕（summary 带源评论
# id / 原事件 seq），并写 event_skipped(duplicate_comment_content)。
COMMENT_DEDUP_DEFAULT_WINDOW_SECONDS = 1.0
def _env_int(name: str, default: int) -> int:
    """可选整数环境变量：缺省/非法返回默认值并显式告警（不静默吞）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(f"invalid {name} {raw!r}, fallback to {default}")
        return default


# 判重扫描上限：量级估算（近 1 秒窗口内的评论事件远小于该值），无 spec 数值来源，
# 按 L2 登记为待测量项，可用 GRAPE_COMMENT_DEDUP_SCAN_LIMIT 覆盖。
COMMENT_DEDUP_SCAN_LIMIT = _env_int("GRAPE_COMMENT_DEDUP_SCAN_LIMIT", 200)
DUPLICATE_COMMENT_CONTENT_REASON = "duplicate_comment_content"
# Issue #2508 review: named bounds for the content fingerprint, each with a
# reason, instead of bare numbers inside the hash expression.
# digest length: 16 hex chars = 64 bits, same width as the request fingerprint;
# collisions are negligible for the (repo, issue, node) scope.
# 定义上移（Issue #3444 review）：comment_content_fingerprint 在本文件更早处使用，
# 避免跨 130 行的前置引用。
CONTENT_KEY_DIGEST_CHARS = 16


def comment_dedup_window_seconds() -> float:
    """判重时间窗（秒）。默认 1 秒（同秒重复）；<=0 关闭内容去重。

    `GRAPE_COMMENT_DEDUP_WINDOW_SECONDS` 可参数化窗口，非法值回落默认。
    """
    import os as _os
    raw = _os.environ.get("GRAPE_COMMENT_DEDUP_WINDOW_SECONDS", "")
    if raw not in (None, ""):
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError) as e:
            log.debug(f"comment dedup window parse failed ({raw!r}): {e}")
    return COMMENT_DEDUP_DEFAULT_WINDOW_SECONDS


def comment_dedup_now() -> float:
    """判重时钟（epoch 秒）。独立函数便于测试替换时间源。"""
    import time as _time
    return _time.time()


def comment_content_fingerprint(note: str) -> str:
    """评论内容指纹：空白归一后 SHA1 摘要（16 hex），内容相同即同指纹。

    空/纯空白评论归一为空串后同样产生稳定指纹，使 `_dedup` 无条件写入 payload，
    后续同内容空评论也能按内容判重（Spec「新建事件 payload 写入 `_dedup`」）。
    """
    import hashlib as _hashlib
    text = str(note or "").strip()
    return _hashlib.sha1(text.encode("utf-8")).hexdigest()[:CONTENT_KEY_DIGEST_CHARS]


def _comment_dedup_meta(payload) -> dict:
    """从事件 payload 读取 `_dedup` 指纹字典（缺失/非法返回空 dict）。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except Exception:
            return {}
    if not isinstance(payload, dict):
        return {}
    meta = payload.get("_dedup")
    return meta if isinstance(meta, dict) else {}


def find_recent_duplicate_comment(repo_key: str, issue_id: str, event_type: str,
                                  content_hash: str, window_seconds: float | None = None,
                                  comment_id: str = "") -> dict:
    """查找同 Issue 同内容且落在时间窗内的评论事件，返回命中依据（无则空 dict）。

    返回 `{seq, comment_id, ts}`：`seq` 为原事件 seq，`comment_id` 为源评论 id。
    仅比较非 `skipped`/`deduped` 的真实处理事件，避免把留痕行当作原事件。

    `comment_id` 非空时按「评论 id 优先」返回：若窗口内已有同评论 id 的事件
    （无论内容），返回带 `same_id=True` 的命中，供调用方走既有
    `duplicate_comment_delivery` 语义；否则才按内容哈希返回原事件，避免同 id
    重投但内容恰与另一评论 id 的原事件相同时被误判为内容去重。
    """
    if not repo_key or not issue_id or not content_hash:
        return {}
    window = comment_dedup_window_seconds() if window_seconds is None else float(window_seconds)
    if window <= 0:
        return {}
    now = comment_dedup_now()
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, payload FROM events WHERE repo_key=? AND issue_id=? "
                "AND event_type=? AND status NOT IN ('skipped','deduped') "
                "ORDER BY seq DESC LIMIT ?",
                (repo_key, issue_id, event_type, COMMENT_DEDUP_SCAN_LIMIT))
            rows = c.fetchall() or []
    except Exception as e:
        log.warning(f"find_recent_duplicate_comment failed: {e}")
        return {}
    first_hit = None
    for seq, raw in rows:
        meta = _comment_dedup_meta(raw)
        hit_id = str(meta.get("comment_id") or "")
        if comment_id and hit_id == str(comment_id):
            # 同评论 id 已存在（无论内容）：优先返回，交由 id 幂等语义处理。
            return {"seq": int(seq), "comment_id": hit_id, "ts": 0.0, "same_id": True}
        if str(meta.get("content_hash") or "") != content_hash:
            continue
        try:
            ts = float(meta.get("ts"))
        except (TypeError, ValueError) as e:
            log.debug(f"comment dedup timestamp parse failed: {e}")
            continue
        if abs(now - ts) > window:
            continue
        if first_hit is None:
            first_hit = {"seq": int(seq), "comment_id": hit_id, "ts": ts}
    return first_hit or {}


def append_comment_dedup_event(repo_key: str, *, event_type: str, title: str, issue_id: str,
                               source: str, agent_type: str, actor: str, dedup: dict,
                               scene_id: str = "", entity_type: str = "") -> int:
    """写入被内容判重短路的后到评论事件（status='skipped'）并留痕。

    `dedup` 为去重依据（`dedup_of_seq` / `source_comment_id` / `comment_id` /
    `content_hash` / `window_seconds` / `ts`）：summary 显式携带源评论 id 与原事件
    seq 供事件详情直接审计，payload 仅保存 `_dedup` 依据，不复制完整 webhook 体。
    """
    source_comment_id = str(dedup.get("source_comment_id") or "")
    duplicate_comment_id = str(dedup.get("comment_id") or "")
    content_hash = str(dedup.get("content_hash") or "")
    dedup_of_seq = int(dedup.get("dedup_of_seq") or 0)
    evidence = (f"重复评论已去重（源评论 id={source_comment_id or '?'}, "
                f"原事件 seq={dedup_of_seq}）")
    payload = {
        "_dedup": {
            "deduped": True,
            "reason": DUPLICATE_COMMENT_CONTENT_REASON,
            "content_hash": content_hash,
            "comment_id": duplicate_comment_id,
            "source_comment_id": source_comment_id,
            "dedup_of_seq": dedup_of_seq,
            "window_seconds": float(dedup.get("window_seconds") or 0),
            "ts": float(dedup.get("ts") or comment_dedup_now()),
        }
    }
    _seq = 0
    with db_lock:
        try:
            c = db.cursor()
            _seq = _insert_event_row(
                c, repo_key, event_type, title, issue_id, agent_type, source, actor,
                evidence, payload, "skipped", DUPLICATE_COMMENT_CONTENT_REASON, scene_id,
                entity_type, "", "")
            db.commit()
        except Exception as e:
            log.warning(f"append_comment_dedup_event failed: {e}")
            with contextlib.suppress(Exception):
                db.rollback()
            return 0
    append_skipped_event(
        repo_key, event_type, DUPLICATE_COMMENT_CONTENT_REASON,
        detail=(f"comment_id={duplicate_comment_id or '?'} "
                f"dedup_of_seq={dedup_of_seq} "
                f"source_comment_id={source_comment_id or '?'} "
                f"content_hash={content_hash}"),
        issue_id=issue_id)
    return _seq


# ====== Issue #2508：终态 a2a 事件投递去重 ======
def _non_replayable_task_statuses() -> tuple:
    """不可重放的任务终态：以 ``service.TASK_TERMINAL_STATUSES`` 为单源，
    显式排除 failed/timeout_failed（保持既有失败重试语义）。"""
    from src.domains.runtime.engine.service import TASK_TERMINAL_STATUSES
    return tuple(s for s in TASK_TERMINAL_STATUSES if s not in ("failed", "timeout_failed"))


# Issue #2508 review: named bounds for the content fingerprint, each with a
# reason, instead of bare numbers inside the hash expression.
# - context prefix: the A2A payload caps context at 500 chars; 400 keeps the
#   leading, differentiating part while leaving room for pr_number.
# - digest length: CONTENT_KEY_DIGEST_CHARS, defined near the #3444 dedup
#   section above (comment_content_fingerprint uses it earlier in this file).
CONTENT_CONTEXT_MAX_CHARS = 400


def _content_key(payload: dict) -> str:
    """请求内容指纹（Issue #2508 review）：context + pr_number 的摘要。

    fingerprint 只按 repo|issue|agent 派生，同一节点同一天的所有交办都相同，
    单靠它 + 时间窗会把「打回后几分钟内的正常再交办」误判成重投。content_key
    把本轮请求的实际内容带进来：内容不同 = 新的一轮交办，不拦。
    """
    import hashlib as _hashlib
    if not isinstance(payload, dict):
        return ""
    raw = (f"{str(payload.get('context') or '')[:CONTENT_CONTEXT_MAX_CHARS]}"
           f"|{str(payload.get('pr_number') or '')}")
    return _hashlib.sha1(raw.encode()).hexdigest()[:CONTENT_KEY_DIGEST_CHARS]


def _completed_within(completed_at: str, window_s: int) -> bool:
    """completed_at 是否落在最近 window_s 秒内（Issue #2508 review）。"""
    if window_s <= 0:
        return False
    raw = str(completed_at or "").strip()
    if not raw:
        return False
    from datetime import datetime, timezone
    dt = None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() <= window_s


def find_terminal_event_replay(repo_key: str, event_seq: int = 0,
                               idempotency_key: str = "",
                               fingerprint: str = "",
                               exclude_seq: int = 0,
                               agent_type: str = "", issue_id: str = "",
                               content_key: str = "",
                               fingerprint_window_s: int | None = None) -> dict:
    """投递前「事件终态 + 关联任务终态」双检（Issue #2508）。

    以 ``(repo_key, event_seq)`` 为主键，``idempotency_key`` 作为无 seq 时的补充
    查询维度（复用既有 ``find_event_by_idempotency_key``）；``exclude_seq`` 用于
    排队 drain 排除排队事件自身、只匹配更早的同幂等键事件。命中 completed/
    cancelled 的任一终态即返回结构化命中信息，调用方据此静默跳过投递并写
    ``event_skipped(reason=terminal_event_replay)`` 审计。

    failed/timeout 的重试语义约束：本函数仅把 completed/cancelled 视为「不可
    重放终态」（任务口径由 ``service.TASK_TERMINAL_STATUSES`` 显式排除
    failed/timeout_failed）；failed/timeout 返回空 dict，保持现有失败重试链路不变。
    ``fingerprint`` 默认仅用于审计明细，不作为唯一命中键（它是 repo+issue+agent 维度
    的请求指纹，跨轮次稳定，单靠它会误杀合法的下一轮交办）。

    Issue #2508 review：调度层重投时可能生成**新的 seq** 且 payload 里
    ``idempotency_key`` 为空（线上事故即此形态），(repo, seq) 主键与幂等键两个维度
    都失配 → 闸门漏。补一条内容维度：同 repo + 同 issue + 同节点 + 同 fingerprint
    且前一条事件在 ``fingerprint_window_s`` 秒内已 completed/cancelled，视为同一
    次投递的重放。窗口默认 180 秒，``GRAPE_REPLAY_FP_WINDOW=0`` 可关闭；事故间隔约 176 秒，
    窗口取 180 才能覆盖该形态，误杀面已由 content_key 收窄。
    """
    from src.api.pipeline_constants import REPLAY_BLOCKING_EVENT_STATUSES
    if not repo_key:
        return {}
    try:
        _seq = int(event_seq or 0)
        _exclude = int(exclude_seq or 0)
    except (TypeError, ValueError):
        _seq, _exclude = 0, 0

    row = None
    _fp_hit = False
    with db_lock:
        try:
            c = db.cursor()
            if _seq > 0 and _seq != _exclude:
                c.execute(
                    "SELECT seq, status, payload FROM events WHERE repo_key=? AND seq=? LIMIT 1",
                    (repo_key, _seq))
                row = c.fetchone()
            if not row and idempotency_key:
                if _exclude:
                    # 排队事件自身即最新同键行，需显式排除后匹配更早的行。
                    c.execute(
                        "SELECT seq, status, payload FROM events WHERE repo_key=? "
                        "AND idempotency_key=? AND seq<>? ORDER BY seq DESC LIMIT 1",
                        (repo_key, idempotency_key, _exclude))
                    row = c.fetchone()
                else:
                    _hit = find_event_by_idempotency_key(repo_key, idempotency_key)
                    if _hit:
                        row = (_hit["seq"], _hit["status"], _hit.get("payload") or "")
            if not row:
                return {}
            _hit_seq, _status, _payload_raw = row[0], str(row[1] or ""), row[2]
            c.execute(
                "SELECT status FROM agent_tasks WHERE event_seq=? "
                "ORDER BY created_at DESC LIMIT 1", (_hit_seq,))
            _task_row = c.fetchone()
            # Issue #2508 review: the target event itself is usually the one being
            # delivered (still running), so the content dimension only applies when
            # neither the event nor its task is a non-replayable terminal.
            _task_now = str((_task_row[0] if _task_row else "") or "")
            _replayable_now = (
                str(_status) not in REPLAY_BLOCKING_EVENT_STATUSES
                and (not _task_now or _task_now not in _non_replayable_task_statuses()))
            # content_key 为空表示调用方没有内容维度可比较：此时不按 fingerprint 拦，
            # 宁可漏一次重投，也不能误杀正常交办（fingerprint 不含轮次）。
            if (_replayable_now and fingerprint and agent_type and issue_id and content_key):
                import os as _os
                _win = fingerprint_window_s
                if _win is None:
                    try:
                        _win = int(_os.environ.get("GRAPE_REPLAY_FP_WINDOW", "180"))
                    except (TypeError, ValueError):
                        _win = 180
                if _win > 0:
                    c.execute(
                        "SELECT seq, status, payload, completed_at FROM events "
                        "WHERE repo_key=? AND event_type='a2a_event' AND agent_type=? "
                        "AND issue_id=? AND seq<>? AND status IN ('completed','cancelled') "
                        "AND payload LIKE ? ORDER BY seq DESC LIMIT 1",
                        (repo_key, agent_type, issue_id, _hit_seq, f"%{fingerprint}%"))
                    _fp_row = c.fetchone()
                    _fp_payload = {}
                    if _fp_row:
                        try:
                            _fp_payload = json.loads(_fp_row[2] or "{}")
                        except Exception:
                            _fp_payload = {}
                    if (_fp_row
                            and _completed_within(_fp_row[3], _win)
                            and _content_key(_fp_payload) == content_key):
                        _fp_hit = True
                        _hit_seq, _status, _payload_raw = (
                            _fp_row[0], str(_fp_row[1] or ""), _fp_row[2])
                        c.execute(
                            "SELECT status FROM agent_tasks WHERE event_seq=? "
                            "ORDER BY created_at DESC LIMIT 1", (_hit_seq,))
                        _task_row = c.fetchone()
        except Exception as e:
            log.warning(f"find_terminal_event_replay query failed seq={event_seq}: {e}")
            return {}

    _task_status = str((_task_row[0] if _task_row else "") or "")
    _event_terminal = _status in REPLAY_BLOCKING_EVENT_STATUSES
    _task_terminal = bool(_task_status) and _task_status in _non_replayable_task_statuses()
    if not (_event_terminal or _task_terminal):
        return {}

    _payload = {}
    try:
        _payload = json.loads(_payload_raw or "{}")
    except Exception:
        _payload = {}
    if not isinstance(_payload, dict):
        _payload = {}
    return {
        "seq": int(_hit_seq or 0),
        "status": _status,
        "reason": "fingerprint_window" if _fp_hit else "terminal_event_replay",
        "task_status": _task_status,
        "event_terminal": _event_terminal,
        "task_terminal": _task_terminal,
        "fingerprint": str(_payload.get("fingerprint") or fingerprint or ""),
        "idempotency_key": str(_payload.get("idempotency_key") or idempotency_key or ""),
    }


def mark_event_reissue(seq: int, reissue: bool = True) -> bool:
    """给事件 payload 打人工重派 reissue 标记（Issue #2508）。

    仅由 redispatch 调用；用于让重派链路显式声明「本次为人工重放」，避免被
    终态投递去重误拦。不改变 ``reset_event_terminal`` 仅限人工重派的语义。
    """
    try:
        _seq = int(seq or 0)
    except (TypeError, ValueError):
        return False
    if _seq <= 0:
        return False
    with db_lock:
        try:
            c = db.cursor()
            c.execute("SELECT payload FROM events WHERE seq=? LIMIT 1", (_seq,))
            row = c.fetchone()
            if not row:
                return False
            try:
                _payload = json.loads(row[0] or "{}")
            except Exception:
                _payload = {}
            if not isinstance(_payload, dict):
                _payload = {}
            from src.api.pipeline_constants import REISSUE_EVENT_PAYLOAD_KEY
            _payload[REISSUE_EVENT_PAYLOAD_KEY] = bool(reissue)
            c.execute("UPDATE events SET payload=? WHERE seq=?",
                      (json.dumps(_payload, ensure_ascii=False), _seq))
            db.commit()
            return True
        except Exception as e:
            log.warning(f"mark_event_reissue failed seq={seq}: {e}")
            return False


def mark_event_redispatch_of(seq: int, original_seq: int = 0) -> bool:
    """在事件 payload 打上人工重派痕迹（Issue #2432 review）。

    与 ``_reissue`` 分开：``_reissue`` 是投递闸门的放行标记（会让该事件永久可重放），
    ``redispatch_of`` 只表示「这一轮失败之后有人重派过」，供失败通知的周期判定使用，
    不影响任何投递判定。
    """
    try:
        _seq = int(seq or 0)
    except (TypeError, ValueError):
        return False
    if _seq <= 0:
        return False
    with db_lock:
        try:
            c = db.cursor()
            c.execute("SELECT payload FROM events WHERE seq=?", (_seq,))
            row = c.fetchone()
            if not row:
                return False
            try:
                _p = json.loads(row[0] or "{}")
            except Exception:
                _p = {}
            if not isinstance(_p, dict):
                _p = {}
            _p["redispatch_of"] = int(original_seq or _seq)
            c.execute("UPDATE events SET payload=? WHERE seq=?",
                      (json.dumps(_p, ensure_ascii=False), _seq))
            db.commit()
            return True
        except Exception as e:
            log.warning(f"mark_event_redispatch_of failed seq={seq}: {e}")
            return False


def is_event_reissue(seq: int) -> bool:
    """事件是否携带人工重派 reissue 标记（Issue #2508）。"""
    try:
        _seq = int(seq or 0)
    except (TypeError, ValueError):
        return False
    if _seq <= 0:
        return False
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT payload FROM events WHERE seq=? LIMIT 1", (_seq,))
            row = c.fetchone()
        if not row:
            return False
        _payload = json.loads(row[0] or "{}")
        from src.api.pipeline_constants import REISSUE_EVENT_PAYLOAD_KEY
        return bool(isinstance(_payload, dict) and _payload.get(REISSUE_EVENT_PAYLOAD_KEY))
    except Exception:
        return False


def parse_agent_output_json(final_text: str) -> dict | None:
    """从 final_text 提取结构化 JSON 产出块（鲁棒解析，兼容 ACP chunk 损坏）。

    返回含 actions/conclusion/artifacts/next_step/issue_summary 任一字段的 dict，否则 None。
    """
    if not final_text:
        return None
    import re as _re, json as _json

    # Step 0: 去 markdown code fence
    _clean = final_text.replace("```json", "").replace("```", "")

    # Step 1: 定位 JSON 块 — 优先用 {"actions" 模式，降级用正则
    m = _re.search(r'\{\s*"actions"', _clean)
    if m:
        raw = _clean[m.start():]
        # 去掉尾部非 JSON 标记
        for marker in ('[PREMATURE_END_TURN]', '\n---', '\n\n['):
            idx = raw.rfind(marker)
            if idx > 0:
                raw = raw[:idx]
    else:
        # 降级：旧正则
        m2 = _re.search(r"\{.*\}", _clean, _re.S)
        if not m2:
            return None
        raw = m2.group(0)

    # Step 2: 修复断裂字段名（\n 在字段名中间，如 "con\nclusion"）
    for field in _KNOWN_OUTPUT_FIELDS:
        for i in range(1, len(field)):
            broken = field[:i] + '\n' + field[i:]
            if broken in raw:
                raw = raw.replace(broken, field)

    # Step 3: 修复换行（字符串外去 \n，字符串内裸 \n 转义为 \n）
    raw = _fix_json_newlines(raw)

    # Step 4: 补闭合 } 如果缺失
    raw = _ensure_json_closed(raw)

    # Step 5: 直接解析
    try:
        obj = _json.loads(raw)
        if _is_valid_output(obj):
            return obj
    except Exception:
        pass

    # Step 6: compact 兜底（去所有非字符串内空白后解析）
    compact = _strip_non_string_ws(raw)
    try:
        obj = _json.loads(compact)
        if _is_valid_output(obj):
            return obj
    except Exception:
        pass

    return None


_KNOWN_OUTPUT_FIELDS = ("actions", "conclusion", "artifacts", "next_step", "issue_summary")


def _is_valid_output(obj) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in _KNOWN_OUTPUT_FIELDS)


def _fix_json_newlines(json_str: str) -> str:
    """去掉 JSON 中非字符串值内的换行符，字符串内的裸 \\n 转义为 \\\\n。"""
    result = []
    in_string = False
    escape = False
    for ch in json_str:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\':
            result.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if ch == '\n':
            if in_string:
                result.append('\\n')
            continue
        result.append(ch)
    return "".join(result)


def _ensure_json_closed(json_str: str) -> str:
    """如果 JSON 缺少闭合 }，按括号深度补上（忽略字符串内的 {{ }}）。"""
    s = json_str.rstrip()
    if s.endswith('}'):
        return s
    depth = 0
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
    if depth > 0:
        s += '}' * depth
    return s


def _strip_non_string_ws(json_str: str) -> str:
    """去掉 JSON 中所有非字符串内的空白（最终兜底）。"""
    result = []
    in_string = False
    escape = False
    for ch in json_str:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\':
            result.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if ch in ' \t\n\r' and not in_string:
            continue
        result.append(ch)
    return "".join(result)


def _extract_tool_command(t) -> str:
    """从 tool_use 项中安全提取 command 字符串。

    input 可能是 dict（标准）、str（hermes JSON 解析结果）、或其他类型。
    hermes.py:132 json.loads 成功时可能返回 str 而非 dict，需兼容。
    """
    if not isinstance(t, dict):
        return ""
    inp = t.get("input", {})
    if isinstance(inp, dict):
        return str(inp.get("command", ""))
    if isinstance(inp, str):
        return inp
    return str(inp)


def evaluate_agent_output(agent_type: str, tool_uses: list, final_text: str) -> tuple:
    """判定 Agent 是否有明确答复产出，返回 (has_output: bool, result: str)

    tool_uses: stream_runtime 产生的 tool_use 事件输入列表（含 bash 命令）
    final_text: Agent 最终答复文本
    优先解析末尾 JSON 块（结构化产出契约）；失败走现有正则降级。
    """
    # 结构化产出优先（M1）：final_text 末尾 JSON 块
    out = parse_agent_output_json(final_text)
    if out:
        conclusion = out.get("conclusion", "")
        if conclusion:
            return True, conclusion

    text = final_text or ""
    has_output = False
    result = ""

    # 通用：final_text 含 GitCode 产出链接或 PR 引用
    import re
    if re.search(r'(merge_requests|pull/[^/]+/merge_requests|/pulls?)/?\d+', text):
        m = re.search(r'(?:merge_requests|/pulls?)/?(\d+)', text)
        pr_num = m.group(1) if m else "?"
        result = f"创建/涉及了 PR #{pr_num}"
        has_output = True
        return has_output, result
    if re.search(r'/issues/\d+', text):
        m = re.search(r'/issues/(\d+)', text)
        issue_num = m.group(1) if m else "?"
        result = f"涉及/创建了 Issue #{issue_num}"
        has_output = True
        return has_output, result
    # PR #N 文本引用（如"PR #5 已创建"、"PR #4 已修复"），不依赖 URL 格式
    m = re.search(r'PR\s*#\s*(\d+)', text)
    if m:
        result = f"创建/涉及了 PR #{m.group(1)}"
        has_output = True
        return has_output, result

    # 按 agent_type 判定
    if agent_type == "coding":
        # 检测创建 PR 的写操作
        for t in tool_uses:
            cmd = _extract_tool_command(t)
            if "POST" in cmd.upper() and ("pulls" in cmd or "merge_requests" in cmd):
                result = "提交了代码变更（PR）"
                has_output = True
                break
    elif agent_type == "issue":
        # issue skill 的最终步骤是"必须回复评论"——final_text 非空即视为已处理（有答复产出）
        for t in tool_uses:
            cmd = _extract_tool_command(t)
            upper = cmd.upper()
            if "POST" in upper and ("/comments" in cmd or "labels" in cmd):
                result = "对 Issue 执行了写操作（评论/标签）"
                has_output = True
                break
        if not has_output:
            # 处理结论关键词（宽松匹配"需重大修改"等变体；排除纯思考过程）
            if re.search(r'(评论已|已回复|已处理|invalid|标记|关闭|打标签|已创建|审查结论|分类为|已发布|已完成)', text):
                result = text.strip()[:200]
                has_output = True
            elif text.strip() and len(text.strip()) > 50 and re.search(r'(#\d+|issue|Issue|标题)', text):
                result = text.strip()[:200]
                has_output = True
    elif agent_type in ("qa", "pr"):
        # pr skill 强制"发送评论是最终步骤"——final_text 非空即视为审查完成（有答复产出）
        # tool_use 检测发布审查评论的写操作作为补充
        for t in tool_uses:
            cmd = _extract_tool_command(t)
            if "POST" in cmd.upper() and ("comments" in cmd or "pulls" in cmd or "issues" in cmd):
                result = "发布了审查评论"
                has_output = True
                break
        if not has_output:
            # 审查结论关键词（宽松匹配"需重大修改"等变体）
            if re.search(r'(审查通过|审查结论|审查完成|需.+修改|发现问题|代码审查|评论已发布|检查项|审查报告)', text):
                result = "输出了审查结论"
                has_output = True
            elif text.strip() and len(text.strip()) > 50:
                # 兜底：pr skill 最终步骤是发送评论，有实质答复即视为产出
                result = "完成了 PR 审查"
                has_output = True
    elif agent_type == "sre":
        for t in tool_uses:
            cmd = _extract_tool_command(t)
            if "DEPLOY" in cmd.upper() or ("POST" in cmd.upper() and "deploy" in cmd.lower()):
                result = "执行了部署/构建操作"
                has_output = True
                break
    elif agent_type == "vod":
        if re.search(r'/issues/\d+', text):
            m = re.search(r'/issues/(\d+)', text)
            result = f"创建了 Issue #{m.group(1) if m else '?'}"
            has_output = True
        if not has_output:
            for t in tool_uses:
                cmd = _extract_tool_command(t)
                if "POST" in cmd.upper() and ("/comments" in cmd or "labels" in cmd):
                    result = "对 Issue 执行了写操作（评论/标签）"
                    has_output = True
                    break

    return has_output, result



def _status_label(status: str) -> str:
    """终态状态的人类可读标签（#1503 后到原因合并用）。"""
    return {"completed": "已完成", "failed": "失败", "cancelled": "已终止"}.get(status, status)


def _notify_pipeline_failure(repo_key: str, issue_id: str, agent_type: str, seq: int) -> None:
    """Issue #2432：失败终态触发飞书群通知（异步、异常不阻断主流程）。

    去重按「需求+节点」并绑定节点状态周期：同一节点上一终态仍为 failed 时
    视为同一失败周期，不重复通知；节点恢复（completed/cancelled）后再次失败会重新通知。
    通知失败只记日志，绝不影响终态写入与流水线主流程。
    """
    try:
        from src.domains.runtime.pipeline_failure_notify import notify_node_failure
        notify_node_failure(repo_key, issue_id, agent_type, seq)
    except Exception as e:
        log.warning(f"Pipeline failure notify failed seq={seq}: {e}")


def update_event_status(seq: int, status: str, result: str = "", exit_code: int = None,
                        failure_info: dict = None):
    """更新事件状态（含终态保护）。

    幂等保护：已终态（completed/failed/cancelled）的事件不允许被其他终态覆盖。
    - 已 completed 的事件不允许被 failed 覆盖（防止任务成功后延迟的 failed 信号覆盖结果，
      Issue #130 seq#5304 场景：任务实际成功但事件被错误标记失败）。
    - 已 failed 的事件不允许被 completed 覆盖（防止用户终止后残留进程跑完调 complete 把
      failed 覆盖成 completed，2026-08-24 seq#2488 实例）。
    - 非终态（running/dispatched）→终态或非终态→非终态的更新不受限制。

    Issue #476：终态 failed 非空 result 防御。裸调用（只传 status="failed"）历史上产生
    "无头失败"（result 空 / exit_code NULL / 无 completed_at），事件列表出现无法解释的红。
    防御规则：
    - 目标 status=failed 且当前 result 为空时，回填 fallback reason + exit_code + completed_at；
    - 调用方显式传 result 时优先使用（不覆盖已存在的非空 result/exit_code/completed_at）。
    - failed 之外的状态（completed/cancelled/running/dispatched）不受影响，保持原语义。
    Issue #3433：failed 终态写结构化 failure_info（异常类型/失败阶段/报错摘要/runtime/agent）；
    调用方未提供时按 result/exit_code 自动兜底，历史裸调用同样获得可读归因。
    """
    _TERMINAL = ("completed", "failed", "cancelled")
    _FALLBACK_REASON = "[E-FAIL-NODIAG] 事件置 failed 但未携带诊断信息（update_event_status 裸调用，见 #476）"
    from datetime import datetime, timezone as _tz
    _notify_repo = _notify_issue = _notify_agent = ""
    # Issue #3927 审查 R1 复审：终态保护与 update_event_complete 共用 event_lock(seq)，
    # 锁序固定 db_lock → event_lock（与 complete 的 _write_guard → event_lock 一致）。
    with db_lock, event_lock(seq):
        try:
            c = db.cursor()
            if status in _TERMINAL:
                c.execute("SELECT status, result FROM events WHERE seq=?", (seq,))
                row = c.fetchone()
                if row and row[0] in _TERMINAL and row[0] != status:
                    if (result or "").strip():
                        # Issue #1503：后到写入携带非空原因 → 合并进 result，状态保持不变。
                        # 终态保护只挡「状态翻转」，不吞诊断信息：原因（如 worker 后到的
                        # 失败报告）对排障有价值，静默丢弃会造成 result 空的不可解释终态。
                        _cur = (row[1] or "").strip()
                        _late = result.strip()
                        if _late in _cur:
                            _merged = _cur
                        elif _cur:
                            _merged = f"{_cur} | 后到补充({_status_label(status)}): {_late}"
                        else:
                            _merged = _late
                        c.execute("UPDATE events SET result=? WHERE seq=?", (_merged[:200], seq))
                        db.commit()
                        log.info(f"Event {seq}: late {status} write with reason merged into "
                                 f"result, status stays {row[0]} (#1503)")
                    else:
                        log.info(f"Event {seq} already {row[0]}, skip {status} overwrite "
                                 f"(terminal state protection, bare flip)")
                    return
            if status == "failed":
                _has_runtime = _events_column_exists(c, "runtime")
                _runtime_expr = "runtime" if _has_runtime else "''"
                c.execute("SELECT result, exit_code, completed_at, agent_type, repo_key, issue_id, "
                          + _runtime_expr + " FROM events WHERE seq=?", (seq,))
                cur = c.fetchone()
                _need_fallback = not (cur and cur[0])
                _reason = result or (
                    _FALLBACK_REASON if _need_fallback else (cur[0] if cur else _FALLBACK_REASON))
                _exit = exit_code if exit_code is not None else (cur[1] if cur and cur[1] is not None else 1)
                _completed = (cur[2] if cur and cur[2] else
                              datetime.now(_tz.utc).isoformat())
                _agent_type = (cur[3] or "") if cur else ""
                _repo_key = (cur[4] or "") if cur else ""
                _issue_id = (cur[5] or "") if cur else ""
                _runtime = (cur[6] or "") if cur else ""
                # Issue #3433：failed 终态写结构化失败原因（未提供时按 result/exit_code 兜底）
                _failure_sql = ""
                _failure_param = ()
                if _events_column_exists(c, "failure_info"):
                    _failure_value = finalize_failure_info(
                        failure_info, result=_reason, exit_code=_exit,
                        agent=_agent_type, runtime=_runtime)
                    _failure_sql = ", failure_info=?"
                    _failure_param = (failure_info_json(_failure_value),)
                # Issue #2432：失败终态落库失败节点名（执行失败/超时/重试耗尽统一走此入口）
                if _events_column_exists(c, "failed_node"):
                    c.execute(
                        "UPDATE events SET status=?, result=?, exit_code=?, completed_at=?, "
                        "failed_node=CASE WHEN ?<>'' THEN ? ELSE failed_node END"
                        + _failure_sql + " WHERE seq=?",
                        (status, _reason, _exit, _completed, _agent_type, _agent_type)
                        + _failure_param + (seq,))
                else:
                    c.execute(
                        "UPDATE events SET status=?, result=?, exit_code=?, completed_at=?"
                        + _failure_sql + " WHERE seq=?",
                        (status, _reason, _exit, _completed) + _failure_param + (seq,))
                if _need_fallback:
                    log.warning(f"Event {seq}: bare failed update, backfilled diagnostic result (#476)")
                _notify_repo, _notify_issue, _notify_agent = _repo_key, _issue_id, _agent_type
            else:
                if status in _TERMINAL:
                    # Issue #1105：completed/cancelled 终态同样强制落 completed_at
                    #（终态⇔completed_at 非空不变量）；已有非空值不覆盖。
                    _now = datetime.now(_tz.utc).isoformat()
                    c.execute(
                        "UPDATE events SET status=?, "
                        "completed_at=CASE WHEN completed_at IS NULL OR completed_at='' "
                        "THEN ? ELSE completed_at END WHERE seq=?",
                        (status, _now, seq))
                else:
                    c.execute("UPDATE events SET status=? WHERE seq=?", (status, seq))
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event {seq}: {e}")
    # Issue #2432：失败终态通知（锁外执行，通知内部会再次读库，避免嵌套加锁）
    if status == "failed" and _notify_repo:
        _notify_pipeline_failure(_notify_repo, _notify_issue, _notify_agent, seq)
    if status in _TERMINAL:
        # 释放活跃守卫（独立 db_lock；避免在临界区内嵌套获取非可重入锁）
        _release_guard_by_seq(seq)
        # Issue #1667：终态 → 自动派发排队在其后的 A2A handoff
        _drain_a2a_queue_after_terminal(seq)




def _drain_a2a_queue_after_terminal(seq: int) -> None:
    """Issue #1667：事件终态后触发 A2A 排队派发（失败不影响终态写入）。

    审查修复④（阻塞/递归）：
    - 有运行中的事件循环（FastAPI 异步路径）→ 卸载到线程池执行，终态写入不再
      被网络派发阻塞，也不在异步上下文里新建事件循环；
    - 无事件循环（脚本/同步 worker/测试）→ 同步执行，保持确定性；
    - 派发本身带重入锁（event_pipeline._A2A_DRAIN_LOCK），由 _fail_event 触发的
      二次终态钩子在 drain 期间直接短路，不形成递归链。
    """
    try:
        from src.domains.runtime.pipeline.event_pipeline import drain_queued_a2a
        try:
            _loop = asyncio.get_running_loop()
        except RuntimeError:
            _loop = None
        if _loop is not None:
            _loop.create_task(asyncio.to_thread(drain_queued_a2a, seq))
        else:
            drain_queued_a2a(seq)
    except Exception as e:
        log.warning(f"A2A queue drain hook failed seq={seq}: {e}")

def _release_guard_by_seq(seq: int) -> None:
    """事件终态 → 按事件的 (repo,issue,agent) 释放对应守卫（仅当前 seq）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key, issue_id, agent_type FROM events WHERE seq=? LIMIT 1", (seq,))
            row = c.fetchone()
        if row:
            release_event_guard(row[0], row[1] or "#0", row[2] or "", seq)
    except Exception as e:
        log.warning(f"_release_guard_by_seq failed seq={seq}: {e}")


def reset_event_terminal(seq: int):
    """redispatch 专用：无视终态保护重置事件为 running 并清结构化产出。

    语义：事件被人工重派/重试 → 视为重新处理中，允许重跑成功后 complete_task 写入
    structured_output/result（避免「事件首次 failed 后 redispatch 成功却无法覆盖」）。
    仅 redispatch 调用；普通 complete 仍走 update_event_complete 终态保护。
    Issue #3927 审查 R1：终态解除同样纳入事件维度锁 t_ev:<seq>（db_lock →
    event_lock 锁序与 update_event_complete 的 _write_guard → event_lock 一致），
    防止「重置 running」与「迟到终态写」互不可见地交错。
    """
    with db_lock:
        with event_lock(seq):
            try:
                c = db.cursor()
                c.execute(
                    "UPDATE events SET status='running', exit_code=NULL, completed_at='', "
                    "structured_output=NULL, result='' WHERE seq=?",
                    (seq,))
                db.commit()
            except Exception as e:
                log.warning(f"reset_event_terminal failed seq={seq}: {e}")
    # redispatch：重新抢占守卫（终态时已释放；此处幂等重抢，防重跑期间并发事件滑入）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key, issue_id, agent_type FROM events WHERE seq=? LIMIT 1", (seq,))
            row = c.fetchone()
        if row:
            claim_event_guard(row[0], row[1] or "#0", row[2] or "")
            update_event_guard_seq(row[0], row[1] or "#0", row[2] or "", seq)
    except Exception as e:
        log.warning(f"reset_event_terminal re-claim guard failed seq={seq}: {e}")


def update_event_issue_id(seq: int, issue_id: str):
    """回写事件归属 Issue（用于引导主事件 issue_id 预分配后的实际号校准）"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("UPDATE events SET issue_id=? WHERE seq=?", (issue_id, seq))
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event issue_id {seq}: {e}")


def update_event_config(seq: int, runtime: str, skill: str, started_at: str):
    """写入执行配置（runtime, skill, started_at）"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("UPDATE events SET runtime=?, skill=?, started_at=? WHERE seq=?",
                      (runtime, skill, started_at, seq))
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event config {seq}: {e}")


def _mysql_backend() -> bool:
    """是否 MySQL（云 RDS）后端：池化/每线程连接，单语句事务不共享连接。

    探测失败按 SQLite/单库保守处理（保留进程级短临界区）。
    """
    try:
        from src.kernel.session_manager import _is_mysql_backend
        return _is_mysql_backend()
    except Exception:
        return False


def _write_guard():
    """Issue #3927 审查 R4：终态/payload 短写的按后端锁选择。

    - MySQL：`nullcontext()`——每线程/池化连接，commit 只作用于本线程语句，无锁写安全；
    - SQLite/单库：返回进程级 `db_lock` 短临界区——单条共享连接的 `commit()` 是
      **连接级**语义，会把其他线程未提交的语句一并提交（跨线程事务交错），
      终态与 payload 写必须与其他 event_store 写者互斥（与基线行为一致）。
    """
    if _mysql_backend():
        from contextlib import nullcontext
        return nullcontext()
    return db_lock


# Issue #3927 审查 R2：payload 写失败的可重试标记（同 seq 后续终态短路时自愈补写）。
_pending_payload_guard = threading.Lock()
_pending_payload: set[int] = set()


def _payload_mark_pending(seq: int) -> None:
    with _pending_payload_guard:
        _pending_payload.add(int(seq))


def _payload_is_pending(seq: int) -> bool:
    with _pending_payload_guard:
        return int(seq) in _pending_payload


def _payload_clear_pending(seq: int) -> None:
    with _pending_payload_guard:
        _pending_payload.discard(int(seq))


def pending_payload_count() -> int:
    """待补写 payload 的事件数（观测/测试/巡检用）。"""
    with _pending_payload_guard:
        return len(_pending_payload)


def _write_event_payload_fields(seq: int, agent_logs, structured_output) -> bool:
    """Issue #3927：agent_logs / structured_output 大字段独立短写。

    与终态字段拆事务（终态先行、大字段补齐，读侧已兼容）。序列化（可能是几十 MB
    的 dumps）与 UPDATE 都在 event_lock 内完成（审查 R2：大字段写不再游离在保护
    锁外），但仍**不持进程级全局锁**——MySQL 下无锁并行，SQLite 由外层
    `_write_guard` 短临界区覆盖（审查 R4）。

    返回是否写成功；失败由调用方登记 pending 标记（重复 complete 短路时自愈补写），
    不回滚已落库的终态字段。
    """
    try:
        _logs_json = json.dumps(agent_logs or [])
    except Exception as e:  # noqa: BLE001 - 大字段脏数据不阻断终态
        log.warning(f"Event {seq}: serialize agent_logs failed: {e}")
        return False
    _structured_json = None
    if structured_output is not None:
        try:
            _structured_json = json.dumps(structured_output, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Event {seq}: serialize structured_output failed: {e}")
            return False
    try:
        c = db.cursor()
        cols = _events_columns(c)
        if "structured_output" in cols and _structured_json is not None:
            c.execute("UPDATE events SET agent_logs=?, structured_output=? WHERE seq=?",
                      (_logs_json, _structured_json, seq))
        else:
            c.execute("UPDATE events SET agent_logs=? WHERE seq=?", (_logs_json, seq))
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning(f"Event {seq}: write payload fields failed: {e}")
        return False


def update_event_complete(seq: int, status: str, exit_code: int = None,
                          duration: float = None, agent_logs: list = None,
                          completed_at: str = "", result: str = "",
                          structured_output: dict = None,
                          failure_info: dict = None,
                          revive_from_timeout_failed: bool = False,
                          allow_from_takeover: bool = False,
                          task_id: str = ""):
    """写入执行结果（status, exit_code, duration, agent_logs, completed_at, result, structured_output）

    Issue #3927（审查修订）：终态保护锁按**事件维度统一**为 `t_ev:<seq>`（R1）——
    传/不传 task_id 的全部终态写者共用一把锁，`SELECT status → UPDATE` 恢复原子；
    `task_id` 仅作日志/指标归因与同任务附加串行（嵌套在内，锁序无交叉）。
    终态字段 UPDATE 与 agent_logs/structured_output 大字段仍拆两个事务/两次短写，
    但都在 event_lock 内完成（R2：大字段写不再游离在保护锁外；失败登记 pending
    标记，重复 complete 短路时自愈补写）。全程不持 session 全局锁；SQLite/单库
    拓扑由 `_write_guard` 保留进程级短临界区（R4：commit 是连接级语义）。

    幂等保护：终态事件不允许被其他终态覆盖。
    - 已 completed 的事件不允许被 failed 覆盖（防止 complete→fail 竞态）。
    - 已 failed 的事件不允许被 completed 覆盖（防止用户终止后残留进程跑完
      调 complete 把 failed 覆盖成 completed，2026-08-24 seq#2488 实例）。
    - structured_output：完整结构化产出（actions/conclusion/artifacts/next_step/issue_summary），
      weekly-05 起落库替代 event.json seg.output；None 不覆盖旧值。
    - revive_from_timeout_failed（Issue #1071）：**唯一的窄口子**——只允许「兜底超时判死的
      failed 事件」被迟到结果改判 completed。调用方必须是 service.revive_timeout_task，
      它已校验：任务态 timeout_failed、未复活过、在复活窗口内、errors 带兜底超时来源标记、
      结果含完整结构化产出。不传此参数时终态保护与 seq#2488 之后的行为完全一致。
    Issue #1105：completed_at 为空时兜底赋当前时间（终态⇔completed_at 非空不变量）；
    调用方显式传入非空值仍优先使用。
    Issue #1503：终态写入 result 为空时兜底赋「状态默认摘要（含 exit_code）」
    （终态⇔result 非空不变量，消除 seq=55067 类 exit_code=1 但 result 空的静默失败）；
    调用方显式传入非空 result 仍优先使用。与 completed_at 兜底同层收口。
    Issue #3433：status=failed 时落结构化 failure_info（异常类型/失败阶段/报错摘要/
    runtime/agent）；调用方未提供时按 result/exit_code 自动兜底。成功/取消不写该字段。
    """
    if not completed_at:
        from datetime import datetime, timezone as _tz
        completed_at = datetime.now(_tz.utc).isoformat()
    _is_terminal = status in ("completed", "failed", "cancelled")
    _agent_type = _repo_key = _issue_id = _runtime = ""
    _terminal_written = False
    # Issue #3927 审查修订：
    # - R1：终态保护统一按事件维度 t_ev:<seq>（所有终态写者共用一把锁）；
    # - R2：payload 写入锁内（不持全局锁）；
    # - R4：SQLite/单库由 _write_guard 保留短临界区（锁序固定：db_lock → event_lock
    #   → task_lock，与 reset_event_terminal 一致，无交叉死锁面）。
    _owner_label = f"complete:{task_id or seq}"
    with lock_owner(_owner_label):
        with _write_guard():
            with event_lock(seq):
                with (task_lock(task_id) if task_id else _nullcontext()):
                    try:
                        c = db.cursor()
                        # 检查当前状态：已终态的事件不允许被其他终态覆盖
                        if status == "failed":
                            # Issue #3433：失败原因需带 runtime；老库缺列时降级为空串
                            _runtime_expr = ("runtime" if _events_column_exists(c, "runtime")
                                             else "''")
                            c.execute("SELECT status, agent_type, repo_key, issue_id, "
                                      + _runtime_expr + " FROM events WHERE seq=?", (seq,))
                        else:
                            c.execute("SELECT status, agent_type, repo_key, issue_id "
                                      "FROM events WHERE seq=?", (seq,))
                        row = c.fetchone()
                        # 接管态保护：事件一旦被标记 takeover（接手的是人，不是 worker），
                        # worker 侧的迟到 complete/fail 一律不得改写；只有接管服务自己
                        # （finish/abort，allow_from_takeover=True）能落终态。
                        if row and row[0] == "takeover" and status in ("completed", "failed", "cancelled"):
                            if not allow_from_takeover:
                                log.info(f"Event {seq} is taken over by a human, skip {status} overwrite")
                                return
                            log.info(f"Event {seq}: takeover -> {status} (by takeover service)")
                        if row and row[0] in ("completed", "failed", "cancelled") and status in ("completed", "failed", "cancelled"):
                            _revive_ok = (revive_from_timeout_failed
                                          and row[0] == "failed" and status == "completed")
                            # Issue #3834：接管 finish 是用户授权的终态写入。若让渡时 cancel_task
                            # 已把事件级联收敛成 cancelled（cascade 合成补偿），而 mark_task_takeover
                            # 未把状态拉回 takeover（老实例/写入竞态），旧实现会在这里静默跳过 →
                            # 事件仍是补偿文案、无 structured_output，下一节点永远不派发。
                            # 窄口子：仅接管服务（allow_from_takeover=True）且 cancelled→completed/failed 放行。
                            # #3834 复审③：failed 也对称放行——接管失败时事件同样停在让渡期 cancelled，
                            # 不放行会留下「接口返回 failed、库里却还是 cancelled+合成补偿文案」的口径分裂。
                            _takeover_ok = (allow_from_takeover
                                            and row[0] == "cancelled" and status in ("completed", "failed"))
                            if not (_revive_ok or _takeover_ok):
                                log.info(f"Event {seq} already {row[0]}, skip {status} overwrite (result={result[:60]})")
                                # R2 自愈：首写 payload 失败登记过 pending → 重复 complete
                                # 终态短路时补写大字段（清标记；仍不覆盖终态字段）。
                                if _payload_is_pending(seq) and (agent_logs or structured_output is not None):
                                    if _write_event_payload_fields(seq, agent_logs, structured_output):
                                        _payload_clear_pending(seq)
                                        log.info(f"Event {seq}: backfilled pending payload on duplicate complete")
                                return
                            if _takeover_ok:
                                log.info(f"Event {seq}: cancelled -> {status} via takeover finish "
                                         f"(Issue #3834 authorized revive)")
                            else:
                                log.info(f"Event {seq}: failed -> completed via timeout revive "
                                         f"(Issue #1071 narrow gate)")
                        # Issue #3433：显式结构化原因里的 message 优先于 #1503 机器摘要（仅空 result 时）
                        _result_was_empty = not (result or "").strip()
                        _raw_failure_info = parse_failure_info(failure_info)
                        # 放在终态保护之后：被 skip 的覆盖写入不产生兜底。
                        if _is_terminal and not (result or "").strip():
                            _ec = exit_code if exit_code is not None else "?"
                            result = f"{status} (exit_code={_ec})"
                            log.info(f"Event {seq}: terminal write with empty result, "
                                     f"backfilled default summary (#1503)")
                        cols = _events_columns(c)
                        # Issue #2432: the node that failed is the event's own agent_type,
                        # read from the row selected above (arch layout keeps it in `row`).
                        if row:
                            _agent_type = str(row[1] or "")
                            _repo_key = str(row[2] or "")
                            _issue_id = str(row[3] or "")
                            _runtime = str(row[4] or "") if len(row) > 4 else ""
                        # Issue #3433：失败终态结构化原因（成功/取消不写，保持既有归档结构）
                        _failure_sql = ""
                        _failure_param = ()
                        if status == "failed":
                            _failure_value = finalize_failure_info(
                                failure_info, result=result, exit_code=exit_code,
                                agent=_agent_type, runtime=_runtime)
                            if _result_was_empty and _raw_failure_info.get("message"):
                                result = (f"[{_failure_value['error_type']}/"
                                          f"{_failure_value['phase_label']}] "
                                          f"{_failure_value['message']}")[:500]
                            if _events_column_exists(c, "failure_info"):
                                _failure_sql = ", failure_info=?"
                                _failure_param = (failure_info_json(_failure_value),)
                        # Issue #2432：失败终态落库失败节点名（执行失败/超时/重试耗尽统一在终态收口）
                        _set_failed_node = status == "failed" and "failed_node" in cols and _agent_type
                        _failed_sql = "" if not _set_failed_node else ", failed_node=?"
                        _failed_param = () if not _set_failed_node else (_agent_type,)
                        # 终态 UPDATE：锁内短写
                        c.execute(
                            "UPDATE events SET status=?, exit_code=?, duration=?, completed_at=?, "
                            "result=?" + _failed_sql + _failure_sql + " WHERE seq=?",
                            (status, exit_code, duration, completed_at, result)
                            + _failed_param + _failure_param + (seq,))
                        db.commit()
                        _terminal_written = True
                        # Issue #3927 审查 R2：agent_logs/structured_output 在 event_lock
                        # 内补写（终态先行、大字段补齐拆事务；MySQL 不持全局锁，
                        # SQLite 由外层 _write_guard 覆盖）。失败登记 pending，
                        # 重复 complete 短路时自愈补写，不再有「completed 而
                        # structured_output 永久为空」的不可自愈路径。
                        _write_payload = "structured_output" in cols
                        try:
                            _payload_ok = _write_event_payload_fields(
                                seq, agent_logs, structured_output if _write_payload else None)
                        except Exception as _pe:  # noqa: BLE001 - payload 失败不回滚终态
                            _payload_ok = False
                            log.warning(f"Event {seq}: write payload fields raised: {_pe}")
                        if _payload_ok:
                            _payload_clear_pending(seq)
                        elif agent_logs or structured_output is not None:
                            _payload_mark_pending(seq)
                    except Exception as e:
                        log.warning(f"Failed to update event complete {seq}: {e}")
    # Issue #2432：失败终态（执行失败/超时/重试耗尽）→ 异步通知飞书群，异常不阻断
    if _terminal_written and status == "failed" and _repo_key:
        _notify_pipeline_failure(_repo_key, _issue_id, _agent_type, seq)
    # 事件进入终态 → 释放活跃守卫（允许同 (issue,agent) 开启新事件）
    if status in ("completed", "failed", "cancelled"):
        _release_guard_by_seq(seq)
        # Issue #1667：前序 A2A 事件终态 → 自动派发排队在其后的 handoff（仅一次）
        _drain_a2a_queue_after_terminal(seq)
    # WAL checkpoint 在 db_lock 释放后执行
    try:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass


def get_events(repo_key: str, agent_type: str = "", issue_id: str = "",
               status: str = "", limit: int = 50, offset: int = 0,
               include_payload: bool = False) -> list[dict]:
    """查询事件列表，支持按 agent/issue/status 过滤。

    include_payload=True 时附带解析后的 payload（仅取 files/guard_impact 视图，
    避免大 payload 全量返回；Issue #996 展示需求）。
    """
    clauses = ["repo_key=?"]
    params = [repo_key]

    if agent_type:
        clauses.append("agent_type=?")
        params.append(agent_type)
    if issue_id:
        clauses.append("issue_id=?")
        params.append(issue_id)
    if status:
        clauses.append("status=?")
        params.append(status)

    where = " AND ".join(clauses)
    with db_lock:
        c = db.cursor()
        try:
            _payload_col = ", payload" if include_payload else ""
            # Issue #2432：failed_node 为后加列（老库可能尚未迁移），缺失时降级为 NULL。
            _has_failed_node = _events_column_exists(c, "failed_node")
            _failed_col = ", failed_node" if _has_failed_node else ", NULL AS failed_node"
            c.execute(
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, runtime, skill, exit_code, duration, started_at, completed_at{_payload_col}{_failed_col} FROM events WHERE {where} ORDER BY seq DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            )
            out = []
            for r in c.fetchall():
                item = {
                    "seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "",
                    "title": r[4] or r[2], "actor": r[5] or "", "summary": r[6] or r[4] or r[2],
                    "status": r[7], "created_at": normalize_event_time(r[8]),
                    "runtime": r[9] or "", "skill": r[10] or "",
                    "exit_code": r[11], "duration": r[12],
                    "started_at": normalize_event_time(r[13] or ""),
                    "completed_at": normalize_event_time(r[14] or ""),
                    "failed_node": (r[16] or "") if include_payload and len(r) > 16 else (
                        (r[15] or "") if not include_payload and len(r) > 15 else ""),
                }
                if include_payload:
                    item.update(payload_view_from_json(r[15], seq=r[0]))
                out.append(item)
            return out
        except Exception:
            return []


def _events_column_exists(c, col: str) -> bool:
    """events 表是否已有指定列（Issue #2432 后加列兼容老库）。

    Issue #2432 review: PRAGMA is sqlite-only; go through the shared two-backend
    probe so the check also works on MySQL (three-database topology).
    """
    from src.domains.runtime.schema_probe import column_exists
    return column_exists(c, "events", col)


def get_event_count(repo_key: str, status: str = "") -> int:
    """统计事件数量"""
    clauses = ["repo_key=?"]
    params = [repo_key]
    if status:
        clauses.append("status=?")
        params.append(status)
    with db_lock:
        try:
            c = db.cursor()
            c.execute(f"SELECT COUNT(*) FROM events WHERE {' AND '.join(clauses)}", params)
            return c.fetchone()[0]
        except Exception:
            return 0


def _entity_type_where(entity_type: str, params: list) -> str:
    """实体维度 WHERE 片段（Issue #3375），空串 = 不过滤（既有行为）。

    历史行 entity_type 为空（列 DEFAULT ''）时按 event_type 反推实体：
    PR/MR 事件视为 pull_request，其余视为 issue——避免历史 PR 评审行被
    `entity_type=issue` 过滤放行、混入 Issue prompt 摘要。
    """
    if not entity_type:
        return ""
    if entity_type in ("issue", "pull_request"):
        from src.domains.runtime.vcs.event_types import ALL_PR_EVENTS
        _pr_types = sorted(ALL_PR_EVENTS)
        _ph = ",".join("?" for _ in _pr_types)
        params.extend(_pr_types)
        if entity_type == "issue":
            return (" AND (entity_type='issue' OR (COALESCE(entity_type,'')=''"
                    f" AND event_type NOT IN ({_ph})))")
        return (" AND (entity_type='pull_request' OR (COALESCE(entity_type,'')=''"
                f" AND event_type IN ({_ph})))")
    params.append(entity_type)
    return " AND entity_type=?"


def get_events_grouped_by_issue(repo_key: str, entity_type: str = "") -> dict[str, list[dict]]:
    """按 issue_id 聚类事件，返回 {issue_id: [events]}。

    entity_type（Issue #3375）：可选实体维度过滤——
      - ""（默认）：不过滤，兼容既有调用方（行为不变）；
      - "issue"：只取 issue 实体事件（历史空 entity_type 行按 event_type 反推，
        PR/MR 事件不算 issue）；
      - "pull_request"：只取 pull_request 实体事件（含历史空 entity_type 的 PR 行）。
    用于 PR 类事件的上下文注入隔离，避免其他 PR 的评审结论混入。
    """
    _where = "repo_key=? AND issue_id != ''"
    _params: list = [repo_key]
    _where += _entity_type_where(entity_type, _params)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at FROM events WHERE {_where} ORDER BY seq ASC",
                _params
            )
            rows = c.fetchall()
        except Exception:
            return {}

    grouped = {}
    for r in rows:
        iid = r[1]
        if iid not in grouped:
            grouped[iid] = []
        grouped[iid].append({
            "seq": r[0], "event_type": r[2], "agent_type": r[3] or "",
            "title": r[4] or r[2], "actor": r[5] or "", "summary": r[6] or r[4] or r[2],
            "status": r[7], "created_at": normalize_event_time(r[8]),
        })
    return grouped


def _is_org_scoped_repo(repo_key: str) -> bool:
    """仅团队组织关联仓库启用富任务记忆；普通/个人仓库保持原有摘要逻辑。"""
    try:
        from src.domains.mgmt import public as _mgmt_public

        org_id = _mgmt_public.get_repo_org_id(repo_key)
        return bool(org_id) and not _mgmt_public.get_org_is_personal(str(org_id))
    except Exception as exc:
        # G-2：端口异常不能静默降级成「非组织仓库」，否则结构化任务记忆在单库
        # 拓扑下会永久失效且不可观测。端口已在 mgmt public 内支持单库回退；这里
        # 仅保留 fail-open 兜底并显式留痕（异常=配置/拓扑问题，需告警排查）。
        log.warning(f"org-scoped repo check failed repo={repo_key}: {exc}")
        return False


def get_issue_summary(repo_key: str, max_issues: int = 10, exclude_issue: str = "",
                      entity_type: str = "") -> str:
    """按 Issue 聚类生成事件摘要文本，用于 Prompt 注入。

    组织级仓库（managed_repos.org_id 非空）使用结构化任务记忆；
    普通仓库保持既有 events.result 摘要逻辑，不受组织上下文改动影响。

    exclude_issue：排除当前 Issue（issue-219 方向 A——当前 Issue 不应出现在自己的 pipeline 摘要中）。
    比较时两侧均忽略 '#' 前缀，兼容 events.issue_id 存 "#5" 或 "5" 两种格式。

    entity_type（Issue #3375）：实体维度过滤，透传 get_events_grouped_by_issue/
    _get_latest_result。默认空串 = 既有行为不变（Issue 事件调用无需改动）。
    """
    if _is_org_scoped_repo(repo_key):
        return _get_org_issue_summary(repo_key, max_issues, exclude_issue,
                                      entity_type=entity_type)
    return _get_legacy_issue_summary(repo_key, max_issues, exclude_issue,
                                     entity_type=entity_type)


def _get_legacy_issue_summary(repo_key: str, max_issues: int = 10,
                              exclude_issue: str = "", entity_type: str = "") -> str:
    """原有仓库级摘要：事件流 + events.result 最新摘要（保持原行为）。"""
    grouped = get_events_grouped_by_issue(repo_key, entity_type=entity_type)
    if not grouped:
        return ""

    _ex = str(exclude_issue or "").lstrip("#")
    lines = []
    sorted_issues = sorted(grouped.keys(), key=lambda iid: grouped[iid][-1]["created_at"], reverse=True)[:max_issues]
    if _ex:
        sorted_issues = [iid for iid in sorted_issues if str(iid).lstrip("#") != _ex]

    for iid in sorted_issues:
        events = grouped[iid]
        agent_flow = []
        for ev in events:
            agent = ev["agent_type"] or "?"
            action = ev["event_type"]
            status_icon = {"completed": "✓", "running": "▶", "failed": "✗", "pending": "○"}.get(ev["status"], "?")
            agent_flow.append(f"{agent}:{action}{status_icon}")

        title = events[0]["title"] or iid
        lines.append(f"  {iid} {title} — {' → '.join(agent_flow)}")

        # 注入最新已完成事件的 result 摘要（关键结论，如"部署完成 EIP=xxx"）
        last_result = _get_latest_result(repo_key, iid, entity_type=entity_type)
        if last_result:
            snippet = last_result[:300].replace("\n", " ").strip()
            if len(last_result) > 300:
                snippet += "…"
            lines.append(f"    最新结论: {snippet}")

    return "Issue Pipeline:\n" + "\n".join(lines)


def _get_org_issue_summary(repo_key: str, max_issues: int = 10, exclude_issue: str = "",
                           entity_type: str = "") -> str:
    """组织级：按 Issue 聚类，优先消费结构化任务记忆（仅组织关联仓库调用）。

    每条 Issue 包含：事件流（agent:type✓）+ 最新已完成事件的 result 摘要（截断 300 字符），
    使 Chat Agent 能感知「部署已完成 / PR 已合并」等关键结论，避免重复追问用户已提供的信息。

    exclude_issue：排除当前 Issue（issue-219 方向 A——当前 Issue 不应出现在自己的 pipeline 摘要中）。
    比较时两侧均忽略 '#' 前缀，兼容 events.issue_id 存 "#5" 或 "5" 两种格式。

    entity_type（Issue #3375）：实体维度过滤，透传 get_events_grouped_by_issue/
    _get_latest_result。默认空串 = 既有行为不变（Issue 事件调用无需改动）。
    """
    grouped = get_events_grouped_by_issue(repo_key, entity_type=entity_type)
    if not grouped:
        return ""

    _ex = str(exclude_issue or "").lstrip("#")
    lines = []
    sorted_issues = sorted(grouped.keys(), key=lambda iid: grouped[iid][-1]["created_at"], reverse=True)[:max_issues]
    if _ex:
        sorted_issues = [iid for iid in sorted_issues if str(iid).lstrip("#") != _ex]

    for iid in sorted_issues:
        events = grouped[iid]
        agent_flow = []
        for ev in events:
            agent = ev["agent_type"] or "?"
            action = ev["event_type"]
            status_icon = {"completed": "✓", "running": "▶", "failed": "✗", "pending": "○"}.get(ev["status"], "?")
            agent_flow.append(f"{agent}:{action}{status_icon}")

        title = events[0]["title"] or iid
        lines.append(f"  {iid} {title} — {' → '.join(agent_flow)}")

        # 优先消费 agent_tasks.result_text 的结构化结论（actions/conclusion/
        # artifacts）；events.result 在流结束兜底时可能只是「任务已完成」占位，
        # 不能代表任务真正产出了什么。缺失时再回退旧 result 摘要。
        memories = get_issue_task_digests(repo_key, iid, limit=3)
        digests = [
            build_digest(
                m["output"], agent_type=m.get("agent_type", ""),
                status=m.get("status", ""), task_id=m.get("task_id", ""),
                errors=m.get("errors", ""))
            for m in memories if m.get("output")
        ]
        if digests:
            compact = render_compact(digests[0])
            if compact:
                lines.append(f"    最新结论: {compact}")
            # 不同角色回答不同问题：最新一条作为「最新结论」，其余角色按标签
            # 补充实现/审查/部署结论，避免最新是 coding 时丢掉更早的 QA 结论。
            seen_compact = {compact}
            for role, label in (("coding", "实现"), ("qa", "审查"), ("sre", "部署")):
                item = next(
                    (d for d in digests if d.get("agent_type") == role), None)
                role_compact = render_compact(item) if item else ""
                if role_compact and role_compact not in seen_compact:
                    lines.append(f"    {label}: {role_compact}")
                    seen_compact.add(role_compact)
            verification: list[str] = []
            artifacts: list[str] = []
            for digest in digests:
                verification.extend(digest.get("verification") or [])
                artifacts.extend(digest.get("artifacts") or [])
            if verification:
                lines.append(f"    验证: {'；'.join(verification)[:240]}")
            if artifacts:
                lines.append(f"    产物: {'；'.join(artifacts)[:240]}")
            if not compact and not verification and not artifacts:
                last_result = _get_latest_result(repo_key, iid, entity_type=entity_type)
                if last_result:
                    lines.append(f"    最新结论: {last_result[:300].strip()}…")
        else:
            last_result = _get_latest_result(repo_key, iid, entity_type=entity_type)
            if last_result:
                snippet = last_result[:300].replace("\n", " ").strip()
                if len(last_result) > 300:
                    snippet += "…"
                lines.append(f"    最新结论: {snippet}")

    return "Issue Pipeline:\n" + "\n".join(lines)


_TASK_DIGEST_COLS = ("id, agent_type, status, skill, result_text, errors, "
                     "created_at, completed_at")
_TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled", "timeout_failed")

# 任务记忆选择：不能只按「最新」取——PR 打开/标签回声/无变更复核等后续任务
# 会把真正的实现卡片挤掉。这里按信息量打分，优先保留实现/修复/带产物的任务。
_TASK_NOOP_RE = re.compile(
    r"(?i)(本次无新增|无新增改动|无需新改动|无待处理动作|无新变更|"
    r"仅核对|自身回声|未重复|不重复|无需重复|已交付成果|确认无需|"
    r"任务已完成：级联收敛)")
_TASK_IMPL_RE = re.compile(
    r"(?i)(新增|实现|重构|修复|改造|补充测试|补齐|提交|推送|"
    r"创建 PR|PR\s*#|分支 feat|迁移|升级|删除|替换|落地)")
_TASK_ARTIFACT_RE = re.compile(
    r"(?i)(PR\s*#|merge_requests?/|pull/|分支|commit|提交|comment id|"
    r"评论 id|审核点|review)")


def _task_memory_score(memory: dict) -> float:
    """任务记忆信息量评分：实现/修复/验证/产物加分，无变更回声降权。"""
    output = memory.get("output") or {}
    if not isinstance(output, dict):
        output = {"conclusion": str(output or "")}
    text = " ".join([
        str(output.get("conclusion") or ""),
        str(output.get("issue_summary") or ""),
        " ".join(str(x) for x in output.get("actions") or []),
        " ".join(str(x) for x in output.get("artifacts") or []),
    ])
    actions = output.get("actions") or []
    artifacts = " ".join(str(x) for x in output.get("artifacts") or [])
    score = 0.0
    if _TASK_NOOP_RE.search(text):
        score -= 6.0
    if _TASK_IMPL_RE.search(text):
        score += 3.0
    if _TASK_ARTIFACT_RE.search(artifacts):
        score += 2.0
    if re.search(r"(?i)(pytest|测试|passed|复跑|验证|pre-pr-check)", text):
        score += 1.0
    score += min(len(actions), 6) * 0.2
    return score


def _issue_id_variants(issue_id: str) -> list[str]:
    base = str(issue_id or "").strip()
    if not base:
        return []
    num = base.lstrip("#")
    out: list[str] = []
    for value in (base, num, f"#{num}"):
        if value and value not in out:
            out.append(value)
    return out


def _task_row_to_memory(row) -> dict:
    raw = row[4] or ""
    output = parse_agent_output_json(raw)
    if output is None:
        text = raw.strip()
        output = {"conclusion": text} if text else None
    return {
        "task_id": row[0] or "",
        "agent_type": row[1] or "",
        "status": row[2] or "",
        "skill": row[3] or "",
        "output": output,
        "raw": raw,
        "errors": row[5] or "",
        "created_at": row[6] or "",
        "completed_at": row[7] or "",
    }


def get_issue_task_digests(repo_key: str, issue_id: str = "",
                           agent_types: tuple[str, ...] = (
                               "coding", "qa", "sre", "issue", "vod"),
                           limit: int = 3) -> list[dict]:
    """按角色取该 Issue 最新的终态任务摘要（每角色 1 条，按完成时间倒序）。

    一个 Issue 往往经历 coding → qa → sre：不同角色的结论分别回答「改了什么」
    「验证了什么」「部署了什么」。只取最新一条会丢掉实现细节，因此按角色保留。
    """
    variants = _issue_id_variants(issue_id)
    if not repo_key or not variants:
        return []
    wanted = {str(x) for x in agent_types}
    marks = ",".join("?" * len(variants))
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT {_TASK_DIGEST_COLS} FROM agent_tasks WHERE repo_key=? "
                f"AND issue_id IN ({marks}) AND status IN "
                f"({','.join('?' * len(_TERMINAL_TASK_STATUSES))}) "
                "ORDER BY COALESCE(completed_at, created_at) DESC LIMIT 100",
                (repo_key, *variants, *_TERMINAL_TASK_STATUSES),
            )
            rows = c.fetchall()
        except Exception as exc:
            log.warning(f"read task digests failed repo={repo_key} "
                        f"issue={issue_id}: {exc}")
            return []
    by_role: dict[str, list[dict]] = {}
    for row in rows:
        agent_type = row[1] or ""
        if agent_type not in wanted:
            continue
        by_role.setdefault(agent_type, []).append(_task_row_to_memory(row))
    selected: list[dict] = []
    for agent_type, memories in by_role.items():
        # 同角色取信息量最高的一条；无变更/回声任务只在前者不存在时保留。
        best = max(
            memories,
            key=lambda m: (_task_memory_score(m),
                           str(m.get("completed_at") or "")),
        )
        selected.append(best)
    selected.sort(key=lambda m: str(m.get("completed_at") or ""), reverse=True)
    return selected[:max(1, int(limit or 1))]


def get_latest_task_digest(repo_key: str, issue_id: str = "",
                           task_id: str = "") -> dict:
    """读取任务的结构化产出摘要（优先指定 task_id，否则取该 Issue 最新终态任务）。

    返回 ``{}`` 表示没有可用任务；否则含 task 元信息与解析后的 ``output``。
    结构化字段由 agent 通过 ``grape tasks complete --json`` 写入 result_text。
    """
    if not repo_key:
        return {}
    variants = _issue_id_variants(issue_id)
    with db_lock:
        c = db.cursor()
        try:
            row = None
            if task_id:
                c.execute(
                    f"SELECT {_TASK_DIGEST_COLS} FROM agent_tasks "
                    "WHERE id=? AND repo_key=? LIMIT 1",
                    (str(task_id), repo_key),
                )
                row = c.fetchone()
            if row is None and variants:
                # target.child_task_id 可能是历史 issue/seq 值而非 agent_tasks.id；
                # 指定 id 查不到时回退到该 Issue 的最新终态任务，避免丢执行记忆。
                marks = ",".join("?" * len(variants))
                c.execute(
                    f"SELECT {_TASK_DIGEST_COLS} FROM agent_tasks WHERE repo_key=? "
                    f"AND issue_id IN ({marks}) AND status IN "
                    f"({','.join('?' * len(_TERMINAL_TASK_STATUSES))}) "
                    "ORDER BY COALESCE(completed_at, created_at) DESC LIMIT 1",
                    (repo_key, *variants, *_TERMINAL_TASK_STATUSES),
                )
                row = c.fetchone()
        except Exception as exc:
            log.warning(f"read latest task digest failed repo={repo_key} "
                        f"issue={issue_id}: {exc}")
            return {}
    return _task_row_to_memory(row) if row else {}


def _get_latest_result(repo_key: str, issue_id: str, entity_type: str = "") -> str:
    """获取某 Issue 下最新已完成事件的 result 字段（截断 500 字符）。

    entity_type（Issue #3375）：与 get_events_grouped_by_issue 同口径过滤
    （历史空 entity_type 行按 event_type 反推实体）。
    """
    _where = "repo_key=? AND issue_id=? AND status='completed' AND result IS NOT NULL AND result != ''"
    _params: list = [repo_key, issue_id]
    _where += _entity_type_where(entity_type, _params)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT result FROM events WHERE {_where} ORDER BY seq DESC LIMIT 1",
                _params
            )
            row = c.fetchone()
            return (row[0] or "")[:500] if row else ""
        except Exception:
            return ""




# ====== Issue #4088：评论派发层幂等守卫（event_seq + comment_id）======

# 幂等命中原因（result/skipped 出参 + event_skipped 留痕共用同一措辞）
COMMENT_DISPATCH_IDEMPOTENT_REASON = "duplicate_comment_dispatch"
# 重试窗口默认 3600s：分钟级抖动重试/重放集中窗口内同一评论对同一 Agent 只
# 送达一次；env GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS 可参数化，<=0 关闭
COMMENT_DISPATCH_GUARD_DEFAULT_WINDOW_SECONDS = 3600.0


def comment_dispatch_guard_window_seconds() -> float:
    """派发守卫重试窗口（秒）。默认 3600；<=0 关闭守卫（旁路放行）。"""
    import os as _os
    raw = _os.environ.get("GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS", "")
    if raw not in (None, ""):
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            log.warning("grape_t_comment_dispatch_guard: invalid GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS=%r, "
                        "falling back to %ss", raw, COMMENT_DISPATCH_GUARD_DEFAULT_WINDOW_SECONDS)
    return COMMENT_DISPATCH_GUARD_DEFAULT_WINDOW_SECONDS


def claim_comment_dispatch_guard(repo_key: str, agent_type: str, comment_id: str,
                                  event_seq: int = 0) -> bool:
    """派发前原子抢占 (repo, agent, comment_id) 幂等守卫；True=抢占成功可派发。

    同一 db_lock 临界区内先清理本键的过期行（窗口外旧守卫不阻断新派发），再
    INSERT OR IGNORE 抢占——跨线程/跨进程并发同键仅一次成功（PK 唯一约束）。
    fail-open：窗口关闭（<=0）或键不全（无 cid/agent）时放行，保持既有行为。
    """
    window = comment_dispatch_guard_window_seconds()
    if window <= 0 or not (repo_key and agent_type and comment_id):
        return True
    try:
        _cutoff = _utc_now_str(-int(window))
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM grape_t_comment_dispatch_guard WHERE repo_key=? AND agent_type=? "
                "AND comment_id=? AND created_at < ?",
                (repo_key, agent_type, comment_id, _cutoff))
            c.execute(
                "INSERT OR IGNORE INTO grape_t_comment_dispatch_guard "
                "(repo_key, agent_type, comment_id, event_seq, task_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (repo_key, agent_type, comment_id, int(event_seq or 0), "", _utc_now_str()))
            ok = c.rowcount == 1
            db.commit()
        return ok
    except Exception as e:
        # 守卫故障不阻断派发主链路（fail-open），异常留日志供排障
        log.warning(f"claim_comment_dispatch_guard failed ({repo_key}/{agent_type}/"
                    f"{comment_id}): {e}")
        return True


def find_comment_dispatch_guard_hit(repo_key: str, agent_type: str,
                                    comment_id: str) -> dict:
    """窗口内命中已有派发守卫 → 返回 {event_seq, task_id, created_at}；否则 {}。"""
    window = comment_dispatch_guard_window_seconds()
    if window <= 0 or not (repo_key and agent_type and comment_id):
        return {}
    try:
        _cutoff = _utc_now_str(-int(window))
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT event_seq, task_id, created_at FROM grape_t_comment_dispatch_guard "
                "WHERE repo_key=? AND agent_type=? AND comment_id=?",
                (repo_key, agent_type, comment_id))
            row = c.fetchone()
        if not row or (row[2] or "") < _cutoff:
            return {}
        return {"event_seq": int(row[0] or 0), "task_id": row[1] or "",
                "created_at": row[2] or ""}
    except Exception as e:
        log.warning(f"find_comment_dispatch_guard_hit failed ({repo_key}/{agent_type}/"
                    f"{comment_id}): {e}")
        return {}


def comment_dispatch_guard_allows_redispatch(repo_key: str, agent_type: str,
                                             comment_id: str, seq: int) -> dict:
    """重派侧判定：同 seq（本事件自身恢复）放行；跨 seq 命中拒绝并回显命中 seq。

    返回 {"allowed": bool, "hit_seq": int}——跨 seq 命中意味着同一评论已有
    首派事件（hit_seq），重派当前事件会重复送达同一 Agent，应改派 hit_seq。
    """
    hit = find_comment_dispatch_guard_hit(repo_key, agent_type, comment_id)
    if not hit:
        return {"allowed": True, "hit_seq": 0}
    hit_seq = int(hit.get("event_seq") or 0)
    if hit_seq == int(seq or 0):
        return {"allowed": True, "hit_seq": hit_seq}
    return {"allowed": False, "hit_seq": hit_seq}


def mark_comment_dispatch_skipped(seq: int, note: str) -> bool:
    """派发幂等命中短路：事件行标 skipped + 落 result 依据（终态不覆盖）。

    与 ``update_event_status`` 的差异：skipped 非终态走裸 UPDATE 会丢 result，
    这里 status/result 一次写入，且对已终态/已 skipped 的行幂等跳过。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE events SET status='skipped', result=? WHERE seq=? "
                "AND status NOT IN ('completed','failed','cancelled','skipped','takeover')",
                (note, seq))
            ok = c.rowcount == 1
            db.commit()
        return ok
    except Exception as e:
        log.warning(f"mark_comment_dispatch_skipped failed seq={seq}: {e}")
        return False


def release_comment_dispatch_guard_by_seq(seq: int) -> int:
    """接管跳过等「未真正派发」路径：按首派事件 seq 释放派发守卫。

    与终态释放不同：成功派发/终态后守卫必须保留（窗口内去重），因此本 helper
    只供「事件未派发」的接管跳过路径调用，避免残留守卫在窗口内吞掉重投。
    """
    if int(seq or 0) <= 0:
        return 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_t_comment_dispatch_guard WHERE event_seq=?",
                      (int(seq),))
            _n = c.rowcount
            db.commit()
        return _n
    except Exception as e:
        log.warning(f"release_comment_dispatch_guard_by_seq failed seq={seq}: {e}")
        return 0


def purge_stale_comment_dispatch_guards() -> int:
    """TTL 兜底清理派发守卫过期行（窗口外允许重新派发）。

    与 ``purge_stale_event_guards`` 同口径：跳过有运行中 task 的守卫——
    窗口 3600s 可能短于长任务时长，误清理后重投会再建任务段，与「命中不新建
    任务段」目标冲突。
    """
    window = comment_dispatch_guard_window_seconds()
    if window <= 0:
        return 0
    try:
        _cutoff = _utc_now_str(-int(window))
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM grape_t_comment_dispatch_guard WHERE created_at < ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM agent_tasks t "
                "  WHERE t.status IN ('dispatched','running') "
                "  AND t.event_seq = grape_t_comment_dispatch_guard.event_seq"
                ")",
                (_cutoff,))
            _n = c.rowcount
            db.commit()
        return _n
    except Exception as e:
        log.warning(f"purge_stale_comment_dispatch_guards failed: {e}")
        return 0


