"""Event Store — 事件记录与查询（shared events table）"""
import json, re, threading, asyncio
from datetime import datetime
from src.kernel.owner_db import db_pair

db, db_lock = db_pair("runtime")
from src.kernel.config import log
from src.kernel.issue_ident import is_actionable_issue_number, normalize_issue_ident  # Issue #3055
from src.kernel.time_utils import normalize_event_time
from src.kernel.event_payload_view import payload_view_from_json
from src.kernel.a2a_context import a2a_event_context_key
from src.kernel.context_digest import build_digest, render_compact

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
    cols: set = set()
    for sql in (
        "SELECT COLUMN_NAME FROM information_schema.columns "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='events'",
        "PRAGMA table_info(events)",
    ):
        try:
            rows = c.execute(sql).fetchall() or []
            if rows:
                # information_schema: COLUMN_NAME 在 0；PRAGMA: 列名在 1
                cols = {(r[0] if isinstance(r[0], str) else r[1]) for r in rows}
                break
        except Exception as e:  # noqa: BLE001 - 后端不支持该方言时换下一种
            log.debug(f"_events_columns: {sql.split()[0]} 不可用: {e}")
            continue
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


def purge_stale_event_guards(max_age_seconds: int = 1800) -> int:
    """TTL 兜底清理守卫行（进程崩溃来不及终态释放时的自愈；默认 30min）。

    守卫作用域仅为"活跃/近窗"，超时删除后允许同 (issue,agent) 重新建事件。
    Issue #372 §12.7: 从 6h 缩短到 30min——活跃 guard 不应存活这么久，
    异常退出后 30min 内自动恢复，不影响用户重试。
    Issue #372 §33: 跳过有运行中 task 的 guard——长任务不应被 TTL 误清理导致重复派发。
    """
    try:
        _cutoff = _utc_now_str(-max_age_seconds)
        with db_lock:
            c = db.cursor()
            # §33: 只清理没有运行中 task 的 guard（防长任务被误清理后重复派发）
            c.execute(
                "DELETE FROM event_active_guard WHERE created_at < ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM agent_tasks t "
                "  WHERE t.status IN ('dispatched','running') "
                "  AND t.event_seq = event_active_guard.seq"
                ")",
                (_cutoff,)
            )
            db.commit()
            return c.rowcount
    except Exception as e:
        log.warning(f"purge_stale_event_guards failed: {e}")
        return 0


def _utc_now_str(offset_seconds: int = 0) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS'（与 datetime('now') 同形；避免引入 DB 方言差异）。"""
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


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
# - digest length: 16 hex chars = 64 bits, same width as the request
#   fingerprint; collisions are negligible for the (repo, issue, node) scope.
CONTENT_CONTEXT_MAX_CHARS = 400
CONTENT_KEY_DIGEST_CHARS = 16


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


def update_event_status(seq: int, status: str, result: str = "", exit_code: int = None):
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
    """
    _TERMINAL = ("completed", "failed", "cancelled")
    _FALLBACK_REASON = "[E-FAIL-NODIAG] 事件置 failed 但未携带诊断信息（update_event_status 裸调用，见 #476）"
    from datetime import datetime, timezone as _tz
    _notify_repo = _notify_issue = _notify_agent = ""
    with db_lock:
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
                c.execute("SELECT result, exit_code, completed_at, agent_type, repo_key, issue_id "
                          "FROM events WHERE seq=?", (seq,))
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
                # Issue #2432：失败终态落库失败节点名（执行失败/超时/重试耗尽统一走此入口）
                if _events_column_exists(c, "failed_node"):
                    c.execute(
                        "UPDATE events SET status=?, result=?, exit_code=?, completed_at=?, "
                        "failed_node=CASE WHEN ?<>'' THEN ? ELSE failed_node END WHERE seq=?",
                        (status, _reason, _exit, _completed, _agent_type, _agent_type, seq))
                else:
                    c.execute(
                        "UPDATE events SET status=?, result=?, exit_code=?, completed_at=? WHERE seq=?",
                        (status, _reason, _exit, _completed, seq))
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
    """
    with db_lock:
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


def update_event_complete(seq: int, status: str, exit_code: int = None,
                          duration: float = None, agent_logs: list = None,
                          completed_at: str = "", result: str = "",
                          structured_output: dict = None,
                          revive_from_timeout_failed: bool = False,
                          allow_from_takeover: bool = False):
    """写入执行结果（status, exit_code, duration, agent_logs, completed_at, result, structured_output）

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
    """
    if not completed_at:
        from datetime import datetime, timezone as _tz
        completed_at = datetime.now(_tz.utc).isoformat()
    _is_terminal = status in ("completed", "failed", "cancelled")
    _agent_type = _repo_key = _issue_id = ""
    _terminal_written = False
    with db_lock:
        try:
            c = db.cursor()
            # 检查当前状态：已终态的事件不允许被其他终态覆盖
            c.execute("SELECT status, agent_type, repo_key, issue_id FROM events WHERE seq=?", (seq,))
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
                if not _revive_ok:
                    log.info(f"Event {seq} already {row[0]}, skip {status} overwrite (result={result[:60]})")
                    return
                log.info(f"Event {seq}: failed -> completed via timeout revive "
                         f"(Issue #1071 narrow gate)")
            # Issue #1503：终态写入 result 兜底（与 completed_at 兜底同层）。
            # 放在终态保护之后：被 skip 的覆盖写入不产生兜底。
            if _is_terminal and not (result or "").strip():
                _ec = exit_code if exit_code is not None else "?"
                result = f"{status} (exit_code={_ec})"
                log.info(f"Event {seq}: terminal write with empty result, "
                         f"backfilled default summary (#1503)")
            _structured = json.dumps(structured_output, ensure_ascii=False) if structured_output is not None else None
            cols = _events_columns(c)
            # Issue #2432: the node that failed is the event's own agent_type,
            # read from the row selected above (arch layout keeps it in `row`).
            if row:
                _agent_type = str(row[1] or "")
                _repo_key = str(row[2] or "")
                _issue_id = str(row[3] or "")
            # Issue #2432：失败终态落库失败节点名（执行失败/超时/重试耗尽统一在终态收口）
            _set_failed_node = status == "failed" and "failed_node" in cols and _agent_type
            _failed_sql = "" if not _set_failed_node else ", failed_node=?"
            _failed_param = () if not _set_failed_node else (_agent_type,)
            if "structured_output" in cols and _structured is not None:
                c.execute(
                    "UPDATE events SET status=?, exit_code=?, duration=?, agent_logs=?, completed_at=?, "
                    "result=?, structured_output=?" + _failed_sql + " WHERE seq=?",
                    (status, exit_code, duration, json.dumps(agent_logs or []),
                     completed_at, result, _structured) + _failed_param + (seq,))
            else:
                c.execute(
                    "UPDATE events SET status=?, exit_code=?, duration=?, agent_logs=?, completed_at=?, "
                    "result=?" + _failed_sql + " WHERE seq=?",
                    (status, exit_code, duration, json.dumps(agent_logs or []),
                     completed_at, result) + _failed_param + (seq,))
            db.commit()
            _terminal_written = True
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


def get_events_grouped_by_issue(repo_key: str) -> dict[str, list[dict]]:
    """按 issue_id 聚类事件，返回 {issue_id: [events]}"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at FROM events WHERE repo_key=? AND issue_id != '' ORDER BY seq ASC",
                (repo_key,)
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


def get_issue_summary(repo_key: str, max_issues: int = 10, exclude_issue: str = "") -> str:
    """按 Issue 聚类生成事件摘要文本，用于 Prompt 注入。

    组织级仓库（managed_repos.org_id 非空）使用结构化任务记忆；
    普通仓库保持既有 events.result 摘要逻辑，不受组织上下文改动影响。
    """
    if _is_org_scoped_repo(repo_key):
        return _get_org_issue_summary(repo_key, max_issues, exclude_issue)
    return _get_legacy_issue_summary(repo_key, max_issues, exclude_issue)


def _get_legacy_issue_summary(repo_key: str, max_issues: int = 10,
                              exclude_issue: str = "") -> str:
    """原有仓库级摘要：事件流 + events.result 最新摘要（保持原行为）。"""
    grouped = get_events_grouped_by_issue(repo_key)
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
        last_result = _get_latest_result(repo_key, iid)
        if last_result:
            snippet = last_result[:300].replace("\n", " ").strip()
            if len(last_result) > 300:
                snippet += "…"
            lines.append(f"    最新结论: {snippet}")

    return "Issue Pipeline:\n" + "\n".join(lines)


def _get_org_issue_summary(repo_key: str, max_issues: int = 10, exclude_issue: str = "") -> str:
    """组织级：按 Issue 聚类，优先消费结构化任务记忆（仅组织关联仓库调用）。

    每条 Issue 包含：事件流（agent:type✓）+ 最新已完成事件的 result 摘要（截断 300 字符），
    使 Chat Agent 能感知「部署已完成 / PR 已合并」等关键结论，避免重复追问用户已提供的信息。

    exclude_issue：排除当前 Issue（issue-219 方向 A——当前 Issue 不应出现在自己的 pipeline 摘要中）。
    比较时两侧均忽略 '#' 前缀，兼容 events.issue_id 存 "#5" 或 "5" 两种格式。
    """
    grouped = get_events_grouped_by_issue(repo_key)
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
                last_result = _get_latest_result(repo_key, iid)
                if last_result:
                    lines.append(f"    最新结论: {last_result[:300].strip()}…")
        else:
            last_result = _get_latest_result(repo_key, iid)
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


def _get_latest_result(repo_key: str, issue_id: str) -> str:
    """获取某 Issue 下最新已完成事件的 result 字段（截断 500 字符）"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT result FROM events WHERE repo_key=? AND issue_id=? AND status='completed' AND result IS NOT NULL AND result != '' ORDER BY seq DESC LIMIT 1",
                (repo_key, issue_id)
            )
            row = c.fetchone()
            return (row[0] or "")[:500] if row else ""
        except Exception:
            return ""
