"""Issue #453 最小根治版：代发内容台账（outbound_content_ledger）。

纲领："问谁发起的，别问用谁的钥匙。"
- 写侧：所有经我们发出的内容（评论/建 Issue/编辑正文），发送成功后把 VCS 返回的
  ID 记进本地表；先记进程内 TTL 集合再落盘，消灭"INSERT 未落盘 webhook 先到"的毫秒竞态。
- 读侧：webhook 事件回来时按同一 ID（评论族）或内容指纹（编辑回声）查表——
  命中 = 我们发的 → 拦（reason=outbound_ledger_hit）；无记录 = 用户的 → 放行。
- 查询异常一律放行（宁漏拦不误杀，兜底判断 bot 名单作为第二道网接手）。
- 保留 30 天（webhook 不会晚到一个月，防膨胀）。

场景对应：docs/issue-453/spec.md S11-S16。
"""
import hashlib
import threading
import time

from src.config import log

# 进程内 TTL 集合：发送响应返回后、INSERT 落盘前 webhook 先到的竞态窗口（S12-3）
_TTL_SECONDS = 300
_TTL_SET: dict = {}  # key -> expire_ts
_TTL_LOCK = threading.Lock()

_RETENTION_DAYS = 30
_last_cleanup_day = ""
# S30-G5：issue_close / issue_meta 对账时效窗口（秒）——共享键动作的回声必然紧邻
# 发送动作到达；窗口外旧记录不参与对账，用户手动操作不误拦
_META_WINDOW_SECONDS = 1800
# 建表一次性标记（审查 D6 性能优化）：记录已建表的 db 连接对象——同一连接跳过 DDL，
# 连接更换（reload/后端切换）自动失效重跑
_TABLE_READY_CONN = None


# 系统回帖标记（与 token_resolver/event_pipeline 各写入点一致）——指纹计算前剥离：
# GitCode 内容净化会剥掉 HTML 注释，webhook 回流的正文无 marker，两侧指纹必须同构
_MARKER = "<!-- grape-auto-comment -->"


def strip_marker(text: str) -> str:
    """剥离 grape-auto-comment 标记（含残留的换行），返回对账用正文。"""
    if not text:
        return ""
    return text.replace(_MARKER, "").strip()


def content_hash_md5(text: str) -> str:
    """内容指纹（S11-5 辅键：provider 不返回 ID / issue 编辑回声 S14 用）。

    一律对剥离 marker 后的正文计算：GitCode 回流正文无 marker，发送侧带 marker，
    不剥离则两侧指纹永远对不上。
    """
    _t = strip_marker(text)
    if not _t:
        return ""
    return hashlib.md5(_t.encode("utf-8", "ignore")).hexdigest()


def _ttl_key(provider: str, repo_key: str, content_type: str, content_id: str) -> str:
    return f"{provider}|{repo_key}|{content_type}|{content_id}"


def _ensure_table():
    """幂等建表（跟随 session_manager 现有 db/db_lock 连接）。

    进程内一次性（性能优化，审查 D6）：首次调用后记录已就绪的 db 连接对象，
    同一连接不再重跑 DDL——消除热路径上每条 webhook 事件 3-5 条带 db_lock 的
    CREATE/information_schema 往返（高并发时的全局锁串行化点）。
    绑定连接对象而非布尔标记：测试隔离层会 reload(session_manager) 换新连接，
    新库（新连接）上须重新建表，标记随连接失效自动重跑，语义不变。
    """
    global _TABLE_READY_CONN
    from src.session_manager import db, db_lock
    if _TABLE_READY_CONN is db:
        return
    with db_lock:
        db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_content_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT DEFAULT '',
                repo_key TEXT DEFAULT '',
                content_type TEXT DEFAULT '',
                content_id TEXT DEFAULT '',
                issue_number TEXT DEFAULT '',
                actor_identity TEXT DEFAULT '',
                origin_channel TEXT DEFAULT 'webhook',
                origin_task TEXT DEFAULT '',
                content_hash TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            )
        """)
        try:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_ledger_cid "
                       "ON outbound_content_ledger (provider, repo_key, content_type, content_id)")
            db.commit()
        except Exception:
            pass
        # 索引迁移（幂等）：旧版索引不含 repo_key（3 列），同 ID 跨仓会被
        # INSERT OR IGNORE 误判重复——检测列数不对则重建
        try:
            c = db.cursor()
            c.execute("SELECT COUNT(DISTINCT COLUMN_NAME) FROM information_schema.statistics "
                      "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='outbound_content_ledger' "
                      "AND INDEX_NAME='idx_outbound_ledger_cid'")
            _row = c.fetchone()
            _ncols = int(_row[0]) if _row else 0
            if _ncols and _ncols < 4:
                db.execute("DROP INDEX idx_outbound_ledger_cid ON outbound_content_ledger")
                db.execute("CREATE UNIQUE INDEX idx_outbound_ledger_cid "
                           "ON outbound_content_ledger (provider, repo_key, content_type, content_id)")
                db.commit()
        except Exception:
            pass  # SQLite 或无旧索引——CREATE IF NOT EXISTS 已覆盖
        try:
            db.execute("CREATE INDEX IF NOT EXISTS idx_outbound_ledger_hash "
                       "ON outbound_content_ledger (provider, content_hash)")
        except Exception:
            pass
        db.commit()
        _TABLE_READY_CONN = db


def _cleanup_expired():
    """保留 30 天（S11-6）；每天最多清一次，失败不影响主流程。

    截止时间在 Python 侧计算（MySQL 兼容层禁止 datetime('now', '-N days') 带修饰符方言）。
    """
    global _last_cleanup_day
    _today = time.strftime("%Y-%m-%d")
    if _today == _last_cleanup_day:
        return
    _last_cleanup_day = _today
    try:
        from datetime import datetime, timedelta
        _cutoff = (datetime.now() - timedelta(days=_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute("DELETE FROM outbound_content_ledger WHERE created_at < ?",
                       (_cutoff,))
            db.commit()
    except Exception as e:
        log.warning(f"outbound_ledger cleanup failed: {e}")


def record_outbound(provider: str, repo_key: str, content_type: str, content_id: str,
                    issue_number: str = "", actor_identity: str = "",
                    origin_channel: str = "webhook", origin_task: str = "",
                    content_hash: str = "") -> bool:
    """写侧记账（S12）：发送成功后调用。best-effort——入账失败不阻断发送、不抛异常。

    content_type: issue_comment / issue / issue_edit
    content_id:   VCS 返回的内容 ID（评论 ID / issue 编号）；编辑回声无 ID 时传 issue_number 兼值
    """
    if not provider or not repo_key or not content_type:
        return False
    _cid = str(content_id or issue_number or "")
    if not _cid:
        return False
    # 先记 TTL 集合（毫秒竞态窗口内可查，S12-3）
    with _TTL_LOCK:
        _TTL_SET[_ttl_key(provider, repo_key, content_type, _cid)] = time.time() + _TTL_SECONDS
    try:
        _ensure_table()
        from src.session_manager import db, db_lock
        with db_lock:
            db.execute(
                "INSERT OR IGNORE INTO outbound_content_ledger "
                "(provider, repo_key, content_type, content_id, issue_number, actor_identity, "
                " origin_channel, origin_task, content_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (provider, repo_key, content_type, _cid, str(issue_number or ""),
                 actor_identity or "", origin_channel or "webhook", origin_task or "",
                 content_hash or "", time.strftime("%Y-%m-%d %H:%M:%S")))
            db.commit()
        _cleanup_expired()
        return True
    except Exception as e:
        log.warning(f"outbound_ledger record failed (non-blocking, TTL set still guards): {e}")
        return False


def is_our_outbound(provider: str, repo_key: str, content_type: str, content_id: str,
                    content_hash: str = "", require_hash_for_edit: bool = True) -> bool:
    """读侧对账（S13）：命中 = 我们发的。查询异常一律返回 False（放行，零误杀）。

    **只有精确 ID 命中才返回 True**。content_hash 仅用于辅助核对：有 ID 但表内该 ID
    记录的指纹与事件侧不一致时**只告警不拦**（指纹可能因平台净化/格式化产生差异，
    不能让指纹误差拦截正常事件）。

    issue_edit 特例（S14）：content_id 是 issue 编号而非全局唯一 ID——用户编辑同一 issue
    的正常事件会与我们的编辑记账同键。因此 issue_edit **必须指纹同时一致**才算命中
    （ID 同键 + 指纹不同 → 只告警放行），否则用户编辑被误拦。

    issue_close / issue_meta 特例（S30，G5）：与 issue_edit 同病——编号是共享键，
    "我们关过一次 → 30 天内谁关都算我们的"会误拦用户手动操作。解法与 S11-4 的
    竞态假设同构：webhook 回流必然紧邻发送动作（毫秒到秒级）。这两类对账要求
    **编号命中 + 台账记录在时效窗口内**（默认 30 分钟）才算命中；窗口外的旧记录
    不参与对账（宁可漏拦一条迟到回声，不误拦用户 30 天内的手动操作——零误杀
    优先级高于零漏拦）。
    """
    _cid = str(content_id or "")
    if not provider or not repo_key or not _cid:
        return False
    _is_edit = content_type == "issue_edit"
    _is_meta_like = content_type in ("issue_close", "issue_meta")
    now = time.time()
    # 1) TTL 集合（INSERT 未落盘的竞态窗口）——issue_edit 不走 TTL（编号非唯一键，
    #    TTL 命中无法区分"我们的编辑回声"与"用户编辑同号 issue"）
    if not _is_edit:
        with _TTL_LOCK:
            for k in [k for k, exp in _TTL_SET.items() if exp < now]:
                _TTL_SET.pop(k, None)
            if _TTL_SET.get(_ttl_key(provider, repo_key, content_type, _cid), 0) > now:
                return True
    # 2) 持久表
    try:
        _ensure_table()
        from src.session_manager import db, db_lock
        if _is_edit:
            # 编辑回声：ID（issue 编号）+ 指纹双命中才拦
            if not content_hash:
                return False
            with db_lock:
                c = db.cursor()
                c.execute("SELECT 1 FROM outbound_content_ledger WHERE provider=? AND repo_key=? "
                          "AND content_type=? AND content_id=? AND content_hash=? LIMIT 1",
                          (provider, repo_key, content_type, _cid, content_hash))
                return bool(c.fetchone())
        if _is_meta_like:
            # close/meta 回声（S30-G5）：编号命中 + 时效窗口双确认——
            # created_at 解析失败/为空按"不在窗口"处理（放行，零误杀）
            _cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - _META_WINDOW_SECONDS))
            with db_lock:
                c = db.cursor()
                c.execute("SELECT 1 FROM outbound_content_ledger WHERE provider=? AND repo_key=? "
                          "AND content_type=? AND content_id=? AND created_at>=? LIMIT 1",
                          (provider, repo_key, content_type, _cid, _cutoff))
                return bool(c.fetchone())
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1 FROM outbound_content_ledger WHERE provider=? AND repo_key=? "
                      "AND content_type=? AND content_id=? LIMIT 1",
                      (provider, repo_key, content_type, _cid))
            if c.fetchone():
                return True
        # ID 未命中时指纹仅作旁路核对信号（不参与拦截判定）
        if content_hash:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT content_id FROM outbound_content_ledger WHERE provider=? "
                          "AND content_hash=? AND repo_key=? LIMIT 1", (provider, content_hash, repo_key))
                _row = c.fetchone()
            if _row:
                log.warning(f"outbound_ledger: content hash matches (provider={provider}, "
                            f"repo={repo_key}, ledger_id={_row[0]}) but event ID {_cid} not in ledger "
                            f"— possible provider ID drift or replay; ALLOW-THROUGH (no block)")
        return False
    except Exception as e:
        log.warning(f"outbound_ledger lookup failed (allow-through): {e}")
        return False


def extract_content_id(resp, content_type: str = "issue_comment") -> str:
    """从 VCS 创建响应提取内容 ID——**分平台字段归一**（写侧）。

    - issue_comment：GitCode/GitCode 风格响应为 {"id": ...}；GitHub 同为 {"id": ...}
      （评论 ID，与 webhook object_attributes.id / comment.id 同源）
    - issue：issue 编号（number/iid 优先）——`id` 是 VCS 内部主键，与事件侧不同源，跳过
    """
    if not isinstance(resp, dict):
        return ""
    if content_type == "issue":
        for key in ("number", "iid", "id"):
            v = resp.get(key)
            if v is not None and str(v) != "":
                return str(v)
        return ""
    for key in ("id", "comment_id", "number", "iid"):
        v = resp.get(key)
        if v is not None and str(v) != "":
            return str(v)
    return ""


def extract_event_content_id(provider: str, payload: dict) -> str:
    """从 webhook payload 提取内容 ID——**分平台提取**（读侧，S13/S16）。

    - GitCode（Note Hook）：object_attributes.id（issue/PR 评论同源）
    - GitHub（issue-comment / pull_request_review_comment）：comment.id
    - issue_opened（按编号对账，豁免之后用）：object_attributes.iid/number 或 issue.number
    """
    if not isinstance(payload, dict):
        return ""
    _oa = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
    _c = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
    if provider == "gitcode":
        return str(_oa.get("id") or "")
    if provider == "github":
        return str(_c.get("id") or _oa.get("id") or "")
    return str(_oa.get("id") or _c.get("id") or "")
