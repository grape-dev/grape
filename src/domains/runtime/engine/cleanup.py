"""sweeper：Issue `done` 后清理 workdir（见 multi-issue-isolation-design.md §3.4.2）。

- 仅清理 `issue_state.stage='done'`（终态）的 Issue workdir，进行中绝不清理；
- 保留 event.json 档案（独立路径）与 issue_agent_session DB 行（审计）；
- 跳过有 dispatched/running 任务的会话；
- 对超 TTL 未活跃的僵尸 workdir 兜底清理；
- 对 chat / feishu / vod / history 目录做 TTL 清理（无活跃会话时删除）。
"""
import asyncio, contextlib, os, shutil, tempfile, time

from src.kernel import config, db_registry
from src.kernel.config import log, CHAT_CWD_BASE

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import repogit

_SWEEP_INTERVAL = int(os.environ.get("SWEEPER_INTERVAL_SECONDS", "900"))  # 15min

_ZOMBIE_TTL_DAYS = int(os.environ.get("SWEEPER_ZOMBIE_TTL_DAYS", "3"))  # 非 done issue workdir 3 天不活跃清理

# Chat / feishu / vod 目录 TTL（默认 3 天未活跃清理，比 issue 的 7 天更短——这些是临时会话目录）
_SESSION_DIR_TTL_DAYS = int(os.environ.get("SWEEPER_SESSION_DIR_TTL_DAYS", "3"))

# 悬空 agent_tasks 回收 TTL（sweep_stuck_agent_tasks_once，单位分钟）：
# queued 超时无人认领 / running·dispatched 所属 runtime 已离线或不存在且 started_at 超时 /
# running·dispatched 超长未收敛（worker 侧任务超时 RUNTIME_TASK_TIMEOUT=1h，6h 仍运行必为孤儿）
_QUEUED_STALE_MINUTES = int(os.environ.get("SWEEPER_QUEUED_STALE_MINUTES", "180"))
# Issue #2225：会话暂存目录 / 存量平铺文件 TTL（默认 2h；worker 单任务超时 1h）。
_TASK_TMPDIR_TTL_SECONDS = int(os.environ.get("SWEEPER_TASK_TMPDIR_TTL_SECONDS", "7200"))
# QA round-2 F6：历史固定名平铺残留白名单——sweeper 只清理这些约定文件名，
# 非约定文件名（第三方工具可能写入共享暂存根）不触碰。
_OPCODE_FLAT_RESIDUAL_NAMES = frozenset({
    "body.md", "req.json", "spec.md",
})
# 会话子目录归属标记（与 session_tmpdir._MARKER_NAME 同步）。
_SESSION_DIR_MARKER = ".grape-session-tmpdir"

_DEAD_RUNTIME_MINUTES = int(os.environ.get("SWEEPER_DEAD_RUNTIME_MINUTES", "30"))
# Issue #3352：有会话但长时间没有任何执行消息的 running 任务——worker 侧任务超时（1h）
# 早该收尾，仍挂着说明 complete/fail 回报丢了（网关 504/断网）。这类僵尸按 6h 长超时回收
# 太慢，期间一直占 `_real_active_count` 并发槽；这里给一个独立的空闲阈值。
_RUNNING_IDLE_MINUTES = int(os.environ.get("SWEEPER_RUNNING_IDLE_MINUTES", "120"))

# Issue #2281：sweeper 清理判据的对比基线。worktree 的未推送判定应以场景开发基线
# （GRAPE_WORK_BRANCH）为准，而不是恒为 origin/main——否则基线分支上的正常提交会被
# 误计为「未推送」，反复归档/拒绝删除。取值与 ensure_issue_worktree 同源：
# 进程 env（API 注入） → 本地 DB 场景配置（API 直连 MySQL 时有效）。
def _sweep_base_branch(repo_key: str) -> str:
    _wb = (os.environ.get("GRAPE_WORK_BRANCH") or "").strip()
    if _wb:
        return _wb
    try:
        return repogit._read_scene_work_branch(repo_key) or ""
    except Exception:
        return ""

def _long_running_minutes() -> int:
    """running·dispatched 孤儿回收阈值（分钟）。

    Issue #470：默认值随统一超时配置派生（timeout 分钟 × 6——worker 超时 1h 时
    6h 仍运行必为孤儿），不再写死 360；SWEEPER_LONG_RUNNING_MINUTES env 兜底
    优先级保留（运维显式调参 > 配置派生）。
    """
    _env = os.environ.get("SWEEPER_LONG_RUNNING_MINUTES", "").strip()
    if _env:
        try:
            v = int(float(_env))
            if v > 0:
                return v
        except Exception:
            pass
    from src.kernel.config import get_orphan_threshold_minutes
    return get_orphan_threshold_minutes()

_QUEUED_STALE_TXT = "中断：queued 超时无 worker 认领（sweeper 兜底回收）"

_DEAD_RUNTIME_TXT = "中断：running/dispatched 悬空，所属 runtime 已离线或不存在（sweeper 兜底回收）"

_LONG_RUNNING_TXT = "中断：running/dispatched 超长未收敛（sweeper 兜底回收，worker 任务超时阈值 1h）"
_IDLE_RUNNING_TXT = ("中断：running 任务长时间无执行消息（complete/fail 回报丢失，"
                     "sweeper 兜底回收并归还并发槽）")

_DUAL_SIGNAL_TXT = "中断：双信号已齐（stream_ended+self_reported）但终态未收敛（sweeper 兜底回收）"

# pending_self_report 任务的执行流活跃窗：主控重启会把「worker 还在跑」的 REMOTE 任务转成
# pending_self_report（等 worker 自报），此后任务不再属于 queued/dispatched/running。
# 事件层回收若只认这三个状态（见 sweep_stuck_events_once），就会把仍在产执行流的事件判
# failed —— 实测 2026-11-24：coding 事件 18:12 被判失败，同一任务 18:15 仍在写消息。
# 这里按 task_messages 活跃度补判。窗口值与任务侧共用（liveness 模块单一来源），避免
# 「任务侧 600 / 事件侧被 env 改成 900」这类判据分叉。
from src.domains.runtime.engine.liveness import (  # noqa: E402
    PENDING_STREAM_ACTIVE_WINDOW_SECONDS as _PENDING_STREAM_ACTIVE_WINDOW_SECONDS,
)

# 探针分片大小：单条 SQL 的 IN 绑定参数随僵尸事件规模线性增长，整批一次绑定会撞驱动上限
# （MySQL 65535 / SQLite 999），分成 ≤200 一批并在异常时只影响该批。
_PROBE_BATCH = 200

# 同一判定的「成功」方向：agent 已经自报 exit_code=0，只是主控没来得及写终态
# （tasks/complete 504、主控重启等）。这不是中断，必须收敛成 completed。
_DUAL_SIGNAL_OK_TXT = "已完成：agent 自报成功（stream_ended+self_reported，sweeper 补偿收敛）"

_GIT_AGENTS = ("vod", "issue", "coding", "qa", "sre")


# SWEEPER_LOCK_MODE:
#   legacy（默认，行为与改造前一致）= 进程主连接 + 进程级 db_lock；
#   own = 每次扫一轮用**自己的 runtime 域连接**（autocommit），完全不占 db_lock，
#         业务侧（心跳/claim/tags）不再和批量扫描抢同一把锁。
# 判据里要求 uses_own_dsn("runtime")：单库/SQLite/测试拓扑没有分域 DSN 时必须回落 legacy，
# 否则 sqlite 单写者会被并发写打爆（database is locked）。
_SWEEPER_LOCK_MODE = (os.environ.get("SWEEPER_LOCK_MODE", "legacy") or "legacy").strip().lower()


_OWN_MODE_LOGGED = False


def _use_own_sweeper_conn() -> bool:
    global _OWN_MODE_LOGGED
    if _SWEEPER_LOCK_MODE not in ("own", "1", "true", "yes"):
        return False
    try:
        ok = bool(db_registry.uses_own_dsn("runtime"))
    except Exception:
        return False
    if ok and not _OWN_MODE_LOGGED:
        _OWN_MODE_LOGGED = True
        log.info("sweeper: SWEEPER_LOCK_MODE=own —— 使用 runtime 域独立连接，不占用进程 db_lock")
    return ok


def _log_round(scope: str, cleaned: int, elapsed: float) -> None:
    """每轮结束的总耗时（>=2s 记 warning：扫描慢到影响业务时能直接从日志看出来）。"""
    if elapsed >= 2.0:
        log.warning(f"sweeper round '{scope}' took {elapsed:.1f}s, cleaned={cleaned}")
    else:
        log.info(f"sweeper round '{scope}' done in {elapsed:.2f}s, cleaned={cleaned}")


def _commit() -> None:
    """legacy 模式下显式提交；own 模式 autocommit 已在语句结束提交（避免误提交主连接）。"""
    if _use_own_sweeper_conn():
        return
    try:
        _commit()
    except Exception as e:  # noqa: BLE001
        log.debug(f"sweeper commit failed: {e}")


class _ReplicaLock:
    """跨副本互斥句柄：acquire/release 各自在线程池里跑，持有期间连接保持打开。

    MySQL GET_LOCK(name, 0) 非阻塞：抢不到说明另一个副本正在扫，本轮直接跳过。
    非 own 模式 / 锁机制不可用时一律放行 —— 退化成改造前的行为，不把清扫停掉。
    """

    def __init__(self, key: str):
        self.key = key
        self._conn = None
        self._held = False

    def acquire(self) -> bool:
        if not _use_own_sweeper_conn():
            return True
        conn = None
        try:
            conn = db_registry.new_connection("runtime", autocommit=True)
            cur = conn.cursor()
            cur.execute("SELECT GET_LOCK(?, 0)", (self.key,))
            row = cur.fetchone()
            if row and row[0] == 1:
                self._conn, self._held = conn, True
                return True
            try:
                conn.close()
            except Exception:
                log.debug("sweeper: 关闭独立连接失败（忽略）")
            return False
        except Exception as e:  # noqa: BLE001 - 锁不可用不阻断清扫
            log.debug(f"sweeper lock unavailable ({self.key}): {e}")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    log.debug("sweeper: 关闭独立连接失败（忽略）")
            return True

    def release(self) -> None:
        if self._conn is None:
            return
        try:
            if self._held:
                self._conn.cursor().execute("SELECT RELEASE_LOCK(?)", (self.key,))
        except Exception as e:  # noqa: BLE001
            log.debug(f"sweeper release failed ({self.key}): {e}")
        finally:
            try:
                self._conn.close()
            except Exception:
                log.debug("sweeper: 关闭独立连接失败（忽略）")
            self._conn, self._held = None, False


_CHAT_STATUS_IDX_READY = False


def _ensure_chat_status_index(conn) -> None:
    """确保 chat_conversations(status) 索引存在（own 模式下用 sweeper 自己的 runtime 连接建）。

    为什么不在注册式迁移里做：三库模式（GRAPE_DOMAIN_LIB=1）进程**跳过 init_database**，
    注册迁移只在单库模式下执行；而 mgmt 进程的库是 mgmt 域，对 runtime 表只能跳过。
    索引的唯一真实消费者就是 sweeper 的 `WHERE status='active'`，所以在首次用到时
    用归属域连接幂等建一次最稳（MySQL 的 TEXT 列必须带前缀长度，这里给 191）。
    """
    global _CHAT_STATUS_IDX_READY
    if _CHAT_STATUS_IDX_READY:
        return
    _CHAT_STATUS_IDX_READY = True
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema=DATABASE() AND table_name='chat_conversations' "
            "AND index_name='idx_chat_conv_status'")
        row = cur.fetchone()
        if row and row[0]:
            return
        cur.execute("CREATE INDEX idx_chat_conv_status ON chat_conversations(status(191))")
        log.info("sweeper: created index idx_chat_conv_status on chat_conversations(status)")
    except Exception as e:  # noqa: BLE001 - 建索引失败不影响清扫
        log.debug(f"sweeper: chat_conversations(status) index ensure skipped: {e}")


@contextlib.contextmanager
def _sweep_mgmt_cursor():
    """IM 三张表归 mgmt 域：own 模式必须换 mgmt 域连接，用 runtime 连接会 1146。"""
    if _use_own_sweeper_conn() and db_registry.uses_own_dsn("mgmt"):
        conn = db_registry.new_connection("mgmt", autocommit=True)
        try:
            yield conn.cursor()
        finally:
            try:
                conn.close()
            except Exception as _ce:
                log.debug(f"sweeper mgmt 连接关闭失败: {_ce}")
        return
    with db_lock:
        yield db.cursor()


@contextlib.contextmanager
def _sweep_cursor():
    """sweeper 专用游标：三库拓扑走 runtime 域独立连接，单库/测试拓扑回落主库 + db_lock。

    sweeper 是批量扫描（整张 agent_tasks / events），若和请求路径共用进程级 db_lock，
    扫描期间心跳、claim、/tags、tasks/complete、tasks/current 全部排队，nginx 直接
    504（2026-11-15 seq=9762 实测：qa 任务跑完 742 条消息，`grape tasks complete`
    连续 7 次 504，最后只能被 sweeper 判失败）。独立连接把两条路径彻底解耦。
    """
    if db_registry.uses_own_dsn("runtime"):
        conn = db_registry.new_connection("runtime", autocommit=True)
        try:
            _ensure_chat_status_index(conn)
            yield conn.cursor()
        finally:
            try:
                conn.close()
            except Exception as _ce:
                log.debug(f"sweeper 独立连接关闭失败: {_ce}")
        return
    with db_lock:
        yield db.cursor()

def _done_issues() -> list[tuple[str, str]]:
    """返回到达终态（stage='done'）的 (repo_key, issue_id) 列表。Phase C3：读 issue 单表。"""
    with _sweep_cursor() as c:
        rows = c.execute("SELECT repo_key, issue_number FROM issue WHERE stage='done'").fetchall()
        return [(r[0], f"#{r[1]}") for r in rows]

def _active_issue_sessions() -> set[tuple[str, str, str]]:
    """有 dispatched/running 任务的 (repo_key, agent_type, issue_num) 集合（不清理）。"""
    with _sweep_cursor() as c:
        c.execute("SELECT DISTINCT issue_session_id FROM agent_tasks WHERE status IN ('dispatched','running') AND issue_session_id!=''")
        ids = [r[0] for r in c.fetchall()]
    if not ids:
        return set()
    ph = ",".join("?" for _ in ids)
    active = set()
    with _sweep_cursor() as c:
        c.execute(f"SELECT repo_key, agent_type, issue_id FROM issue_agent_session WHERE id IN ({ph})", ids)
        for r in c.fetchall():
            active.add((r[0], r[1], str(r[2]).lstrip("#")))
    return active


def _resolve_repo_git_token(repo_key: str) -> str:
    """sweeper 侧的仓库执行 token（Issue #2041）。

    未推送判定的远端事实复核（worktree_guard._remote_branch_head → `ls-remote`）
    需要鉴权；sweeper 不在任务上下文内，只能自行解析。解析失败/无 token → 空串，
    判定自动回退本地 ref 链（保守，不改变「宁可残留，不可丢改动」口径）。
    """
    if not repo_key:
        return ""
    try:
        from src.kernel.repo import Repo
        return Repo(repo_key).bot_token or ""
    except Exception as e:
        log.warning(f"sweeper git token resolve failed {repo_key}: {e}")
        return ""


def _zombie_issue_dirs(ttl: float) -> list[str]:
    """超 TTL 未活跃的 issue workdir。

    主结构：{owner}/{repo}/issues/{n}/{agent}/（按 Issue 统一隔离，见 workdir-issue-centric-design.md）。
    过渡期兼容旧结构 {owner}/{repo}/{agent}/issues/{n}/，新代码不再写入、仅扫描兜底清理。
    """
    out = []
    now = time.time()

    def _check(p: str):
        if not os.path.isdir(p):
            return
        try:
            mtime = os.path.getmtime(p)
        except Exception:
            return
        if now - mtime > ttl:
            out.append(p)

    def _scan_repo_dir(repo_dir: str):
        """扫描单个 repo 目录下的 issue workdir（兼容新旧结构）。"""
        if not os.path.isdir(repo_dir):
            return
        # 新结构：{repo}/issues/{n}/{agent}/
        issues_root = os.path.join(repo_dir, "issues")
        if os.path.isdir(issues_root):
            for num in os.listdir(issues_root):
                issue_dir = os.path.join(issues_root, num)
                if not os.path.isdir(issue_dir):
                    continue
                for agent in _GIT_AGENTS:
                    _check(os.path.join(issue_dir, agent))
        # 过渡期旧结构：{repo}/{agent}/issues/{n}/（仅兜底，新代码不写入）
        for agent in _GIT_AGENTS:
            issues = os.path.join(repo_dir, agent, "issues")
            if not os.path.isdir(issues):
                continue
            for num in os.listdir(issues):
                _check(os.path.join(issues, num))

    try:
        # 遍历 WORKSPACE_DIR 下所有目录，找到含 issues/ 的 repo 目录
        # 新结构：{WORKSPACE_DIR}/{provider}/{owner}/{repo}/issues/
        # 旧结构：{WORKSPACE_DIR}/{owner}/{repo}/issues/
        _ws = config.WORKSPACE_DIR
        if not os.path.isdir(_ws):
            return out
        for entry1 in os.listdir(_ws):
            d1 = os.path.join(_ws, entry1)
            if not os.path.isdir(d1):
                continue
            # 检查 d1 是否为 provider 目录（新结构）或 owner 目录（旧结构）
            # provider 目录下是 owner 层；owner 目录下是 repo 层
            for entry2 in os.listdir(d1):
                d2 = os.path.join(d1, entry2)
                if not os.path.isdir(d2):
                    continue
                # 检查 d2 是否直接含 issues（旧结构：d2=repo_dir）
                if os.path.isdir(os.path.join(d2, "issues")):
                    _scan_repo_dir(d2)
                # 新结构：d2 是 owner 目录，下面还有 repo 层
                for entry3 in os.listdir(d2):
                    d3 = os.path.join(d2, entry3)
                    if os.path.isdir(d3) and os.path.isdir(os.path.join(d3, "issues")):
                        _scan_repo_dir(d3)
    except Exception as e:
        log.warning(f"sweeper zombie scan failed: {e}")
    return out

def _active_chat_conv_ids() -> set[str]:
    """有活跃任务的 chat conversation ID 集合（不清理）。"""
    try:
        with _sweep_cursor() as c:
            c.execute("SELECT id FROM chat_conversations WHERE status='active'")
            return {r[0] for r in c.fetchall()}
    except Exception:
        return set()

def _stale_session_dirs(ttl: float) -> list[str]:
    """超 TTL 未活跃的 chat / feishu / vod / history 目录。

    chat: {CHAT_CWD_BASE}/chat/{user_id}/{conv_id}/  — 跳过 DB 中 status='active' 的会话
    feishu: {WORKSPACE_DIR}/feishu/{repo}/{sess}/
    vod: {WORKSPACE_DIR}/vod/{repo}/
    history: {WORKSPACE_DIR}/history/{account}/{repo}/
    """
    out = []
    now = time.time()

    def _check_dir(p: str):
        if not os.path.isdir(p):
            return
        try:
            mtime = os.path.getmtime(p)
        except Exception:
            return
        if now - mtime > ttl:
            out.append(p)

    # --- chat 目录 --- 
    try:
        active_convs = _active_chat_conv_ids()
        chat_base = os.path.join(CHAT_CWD_BASE, "chat")
        if os.path.isdir(chat_base):
            for user_dir in os.listdir(chat_base):
                user_path = os.path.join(chat_base, user_dir)
                if not os.path.isdir(user_path):
                    continue
                for conv_dir in os.listdir(user_path):
                    conv_path = os.path.join(user_path, conv_dir)
                    if not os.path.isdir(conv_path):
                        continue
                    # 跳过活跃会话
                    if conv_dir in active_convs:
                        continue
                    _check_dir(conv_path)
    except Exception as e:
        log.warning(f"sweeper chat dir scan failed: {e}")

    # --- chat 旧残留目录（WORKSPACE_DIR/chat/）---
    # 2026-08-27 修复前 chat.py/tasks.py 曾误用 WORKSPACE_DIR 而非 CHAT_CWD_BASE，
    # 导致部分 chat 目录创建在 {WORKSPACE_DIR}/chat/ 下。新代码不再写入此路径，
    # 但 sweeper 需扫描兜底清理，否则孤儿目录永久堆积。
    try:
        legacy_chat_base = os.path.join(config.WORKSPACE_DIR, "chat")
        if os.path.isdir(legacy_chat_base):
            for user_dir in os.listdir(legacy_chat_base):
                user_path = os.path.join(legacy_chat_base, user_dir)
                if not os.path.isdir(user_path):
                    continue
                for conv_dir in os.listdir(user_path):
                    conv_path = os.path.join(user_path, conv_dir)
                    if not os.path.isdir(conv_path):
                        continue
                    # 旧残留目录无需跳过 active——正确位置在 CHAT_CWD_BASE 下
                    _check_dir(conv_path)
            # 如果 legacy_chat_base 本身已空，也清理掉
            try:
                if not os.listdir(legacy_chat_base):
                    os.rmdir(legacy_chat_base)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"sweeper legacy chat dir scan failed: {e}")

    # --- feishu 目录 ---
    try:
        feishu_base = os.path.join(config.WORKSPACE_DIR, "feishu")
        if os.path.isdir(feishu_base):
            for repo_dir in os.listdir(feishu_base):
                repo_path = os.path.join(feishu_base, repo_dir)
                if not os.path.isdir(repo_path):
                    continue
                for sess_dir in os.listdir(repo_path):
                    _check_dir(os.path.join(repo_path, sess_dir))
    except Exception as e:
        log.warning(f"sweeper feishu dir scan failed: {e}")

    # --- vod 目录 ---
    # 结构：vod/{provider}/{owner_repo}/ 或旧 vod/{owner_repo}/
    try:
        vod_base = os.path.join(config.WORKSPACE_DIR, "vod")
        if os.path.isdir(vod_base):
            for entry in os.listdir(vod_base):
                entry_path = os.path.join(vod_base, entry)
                if not os.path.isdir(entry_path):
                    continue
                # 新结构：provider 目录下有子目录
                has_subdirs = any(os.path.isdir(os.path.join(entry_path, d))
                                  for d in os.listdir(entry_path))
                if has_subdirs:
                    for repo_dir in os.listdir(entry_path):
                        _check_dir(os.path.join(entry_path, repo_dir))
                else:
                    # 旧结构：直接是 repo 目录
                    _check_dir(entry_path)
    except Exception as e:
        log.warning(f"sweeper vod dir scan failed: {e}")

    # --- history 目录 ---
    # 结构：history/{account}/{provider}/{owner_repo}/ 或旧 history/{account}/{owner_repo}/
    try:
        hist_base = os.path.join(config.WORKSPACE_DIR, "history")
        if os.path.isdir(hist_base):
            for account_dir in os.listdir(hist_base):
                account_path = os.path.join(hist_base, account_dir)
                if not os.path.isdir(account_path):
                    continue
                for entry in os.listdir(account_path):
                    entry_path = os.path.join(account_path, entry)
                    if not os.path.isdir(entry_path):
                        continue
                    # 新结构：provider 目录下有子目录
                    has_subdirs = any(os.path.isdir(os.path.join(entry_path, d))
                                      for d in os.listdir(entry_path))
                    if has_subdirs:
                        for repo_dir in os.listdir(entry_path):
                            _check_dir(os.path.join(entry_path, repo_dir))
                    else:
                        # 旧结构：直接是 repo 目录
                        _check_dir(entry_path)
    except Exception as e:
        log.warning(f"sweeper history dir scan failed: {e}")

    return out

def _pending_self_report_live_seqs(cursor, seqs, batch: int | None = None) -> set:
    """pending_self_report 事件里「worker 仍在产执行流」的 seq 集合（活跃窗内）。

    判据与任务侧活跃流豁免同源（`liveness.PENDING_STREAM_ACTIVE_WINDOW_SECONDS`）：
    task_messages 最新一条 created_at 距 now < 窗口。数据缺失（无消息/解析失败）一律
    不豁免：宁可照旧回收，也不留一个永远判不死的僵尸事件。

    健壮性：IN 查询按 `batch`（默认 200）分片，避免僵尸规模大时一次绑定撞驱动参数上限；
    某一批探测失败只让该批不豁免，并在日志里带数量（log.error），不让整轮静默失效
    —— 这个探针唯一的价值就是在故障态下生效，恰在故障态最可能抛错。
    """
    if not seqs:
        return set()
    _batch = int(batch or _PROBE_BATCH)
    from datetime import datetime, timezone

    from src.domains.runtime.engine.health import parse_dt
    now_ts = datetime.now(timezone.utc).timestamp()
    out = set()
    pend = []
    # ① 事件 → pending 任务
    for i in range(0, len(seqs), _batch):
        chunk = list(seqs)[i:i + _batch]
        ph = ",".join("?" * len(chunk))
        try:
            cursor.execute(
                f"SELECT event_seq, id FROM agent_tasks WHERE event_seq IN ({ph}) "
                "AND status='pending_self_report'", chunk)
            pend.extend((r[0], r[1]) for r in cursor.fetchall() if r[0] is not None)
        except Exception as e:
            log.error(f"sweeper: pending task probe failed for {len(chunk)} event seq(s), "
                      f"treat as not exempt: {e}")
    if not pend:
        return out
    # ② 任务 → 最新消息时刻（只在候选任务集上查）
    ids = list(dict.fromkeys(r[1] for r in pend))
    latest = {}
    for i in range(0, len(ids), _batch):
        chunk = ids[i:i + _batch]
        ph_ids = ",".join("?" * len(chunk))
        try:
            cursor.execute(
                f"SELECT task_id, created_at FROM task_messages WHERE task_id IN ({ph_ids}) "
                "ORDER BY id DESC", chunk)
            for tid, created_at in cursor.fetchall():
                if tid in latest:
                    continue  # 已取到该任务最新一条（ORDER BY id DESC）
                latest[tid] = created_at
        except Exception as e:
            log.error(f"sweeper: message probe failed for {len(chunk)} pending task(s), "
                      f"treat as not exempt: {e}")
    for seq, tid in pend:
        raw = (latest.get(tid) or "").strip()
        if not raw:
            continue
        try:
            ts = parse_dt(raw)
        except Exception:
            ts = None
        if ts is None:
            continue
        if now_ts - float(ts) < _PENDING_STREAM_ACTIVE_WINDOW_SECONDS:
            out.add(seq)
    return out


def sweep_stuck_events_once(no_start_minutes: int = 10, stale_minutes: int = 30) -> int:
    """兜底回收：running 事件超时且无 agent_tasks 活跃任务支撑 → 置 failed（防卡 running 僵尸）。

    事件在 append_event 时即 running，若入队/启动失败或执行中断且未更新终态，
    事件永远 running（Agent 详情页显示"进行中"，但无任务/日志/结果）。两类僵尸：
    - started_at 为空（从未开始）且 created_at 超过 no_start_minutes → 入队/启动从未成功
    - started_at 非空但超过 stale_minutes 且无活跃任务 → 执行中断（任务记录已消失）
    有 queued/dispatched/running 任务支撑的 running 事件一律跳过（真在执行）。
    created_at 为 DB 本地时间（MySQL），started_at 为 UTC ISO 字符串，分开比较。
    """
    from datetime import datetime, timezone, timedelta
    fixed = 0
    try:
        now_local = datetime.now()
        no_start_cut = (now_local - timedelta(minutes=no_start_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        now_utc = datetime.now(timezone.utc)
        stale_cut = (now_utc - timedelta(minutes=stale_minutes)).isoformat()
        with _sweep_cursor() as c:
            # 方案 E：入队为 pending，功能上等同旧的"未开始 running"——超时无活跃任务同样回收
            c.execute(
                "SELECT seq FROM events WHERE status IN ('pending','running') AND "
                "(started_at IS NULL OR started_at='') AND created_at < ?",
                (no_start_cut,))
            seqs = [r[0] for r in c.fetchall()]
            c.execute(
                "SELECT seq FROM events WHERE status IN ('pending','running') AND started_at != '' "
                "AND started_at IS NOT NULL AND started_at < ?",
                (stale_cut,))
            seqs += [r[0] for r in c.fetchall()]
        seqs = list(dict.fromkeys(seqs))
        if not seqs:
            return 0
        ph = ",".join("?" * len(seqs))
        with _sweep_cursor() as c:
            c.execute(
                f"SELECT DISTINCT event_seq FROM agent_tasks WHERE event_seq IN ({ph}) "
                "AND status IN ('queued','dispatched','running')",
                seqs)
            live = {r[0] for r in c.fetchall()}
            # pending_self_report 不是活跃状态，但 worker 可能在继续跑（主控重启把执行中的
            # REMOTE 任务转成 pending_self_report，等 worker 自报）。用 task_messages 最新
            # 写入距 now < 活跃窗补判，避免「任务在写消息、事件先被判 failed」的次序颠倒。
            live |= _pending_self_report_live_seqs(c, seqs)
        dead = [s for s in seqs if s not in live]
        if dead:
            ph2 = ",".join("?" * len(dead))
            now_iso = now_utc.isoformat()
            # 区分「创建任务失败」与「running 超时」：
            # - 该事件在 agent_tasks 连一条记录都没有 → 极可能是 _create_direct_task/pre 入队失败（INSERT 绑定数错误等），
            #   结果写明真实根因，避免一律误标"running 超时"（见 agent-tasks-direct-create-binding-fix-design.md §5.4）。
            # - 有任务记录但非活跃（终态/缺失）→ 才是真正的 running 超时/执行中断。
            _no_task_txt = "中断：running 超时无任务执行记录，且 agent_tasks 无任何关联行——疑似创建任务失败（create_direct_task/enqueue 未落行，请查服务端日志）"
            _timeout_txt = "中断：running 超时无活跃任务执行记录（sweeper 兜底回收）"
            _with_task_rows = set()
            # Issue #587：超时探活收敛——最近任务已终态时按任务终态映射收敛
            # （cancelled 任务 → 事件 cancelled，不再是 failed；保留业务结论语义），
            # 而非一律标 failed。
            _terminal_by_seq = {}
            try:
                with _sweep_cursor() as _tc:
                    _tc.execute(
                        f"SELECT event_seq, status, exit_code, id FROM agent_tasks "
                        f"WHERE event_seq IN ({ph2}) AND status IN ('completed','failed','cancelled') "
                        f"ORDER BY created_at ASC", dead)
                    for _s, _st, _ec, _tid in _tc.fetchall():
                        # 同一 event_seq 多条重试任务时以最新任务为准（结论回填须限定任务 id）
                        _terminal_by_seq[_s] = (_st, _ec, _tid)
            except Exception as _te:
                log.warning(f"sweeper: terminal task lookup failed: {_te}")
            try:
                # Nit 修复：import 移出 db_lock 临界区（首次导入不应持锁执行）
                from src.domains.runtime.engine.service import _cascade_event_terminal
                with _sweep_cursor() as _c:
                    _c.execute(f"SELECT DISTINCT event_seq FROM agent_tasks WHERE event_seq IN ({ph2})", dead)
                    _with_task_rows = {r[0] for r in _c.fetchall()}
                    # 同一连接上的第二个游标（own 模式不能再用进程主连接 db.cursor()）
                    _c2 = _c.connection.cursor()
                    for s in dead:
                        if s in _terminal_by_seq:
                            # 任务已终态 → 级联到任务终态（探活收敛，幂等）
                            _st, _ec, _tid = _terminal_by_seq[s]
                            if _cascade_event_terminal(_c2, s, _st, exit_code=_ec):
                                fixed += 1
                            continue
                        _txt = _timeout_txt if s in _with_task_rows else _no_task_txt
                        _c2.execute(
                            "UPDATE events SET status='failed', exit_code=1, completed_at=?, result=? "
                            "WHERE seq=? AND status IN ('pending','running')",
                            (now_iso, _txt, s))
                        fixed += _c2.rowcount
                    _commit()
            except Exception as e:
                log.warning(f"sweeper: per-seq recovery failed, fallback bulk: {e}")
                with _sweep_cursor() as _c:
                    _c.execute(
                        f"UPDATE events SET status='failed', exit_code=1, completed_at=?, "
                        f"result='{_timeout_txt}' WHERE seq IN ({ph2}) "
                        f"AND status IN ('pending','running')",
                        (now_iso, *dead))
                    fixed = _c.rowcount
                    _commit()
            if fixed:
                log.info(f"sweeper: recovered {fixed} stuck running event(s): {dead}")
            # 取消对应的线程池排队任务（防止 event 已 failed 但子进程还在跑）
            try:
                from src.domains.runtime.pipeline.event_pipeline import cancel_direct_task
                for _seq in dead:
                    cancel_direct_task(_seq)
            except Exception as e:
                log.warning(f"sweeper: cancel_direct_task failed: {e}")
    except Exception as e:
        log.warning(f"sweeper event recovery failed: {e}")
    return fixed

def _dt_age_minutes(s: str | None, now: float | None = None) -> float | None:
    """解析 created_at/started_at（ISO 或本地 'YYYY-MM-DD HH:MM:SS'）距今分钟数；解析失败返回 None。"""
    if not s:
        return None
    from datetime import datetime
    txt = str(s).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        pass
    if dt is None:
        for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(txt, _fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    from datetime import timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    _now = now if now is not None else time.time()
    return max(0.0, (_now - dt.timestamp()) / 60.0)

def _task_idle_minutes(c, task_id, now: float) -> float | None:
    """running 任务最近一次执行消息距今分钟数（无消息返回 None）。"""
    try:
        row = c.execute("SELECT MAX(created_at) FROM task_messages WHERE task_id=?",
                        (task_id,)).fetchone()
    except Exception as _e:  # noqa: BLE001
        log.debug(f"sweeper: task {task_id} message idle probe failed: {_e}")
        return None
    ts = row[0] if row else None
    return _dt_age_minutes(ts, now) if ts else None


def sweep_stuck_agent_tasks_once(queued_stale_minutes: int | None = None,
                                 dead_runtime_minutes: int | None = None,
                                 long_running_minutes: int | None = None,
                                 idle_running_minutes: int | None = None) -> int:
    """兜底回收悬空 agent_tasks（需求中心「运行中/雷达扫描」亮灯的直接依据）。

    现状：sweeper 只回收 events（sweep_stuck_events_once），完全不碰 agent_tasks。
    广播任务无 worker 认领（runtime_id=0）、或 runtime 被删/离线后，queued/dispatched/running
    永远悬空 → `_batch_running_agent_tasks` 恒命中 → issue 卡片 is_running=true 雷达动画永久亮。
    实例：#210 孤儿广播任务 t_84b3622a7c2d（2026-09-29 07:18 入队，广播回落本机修复 17:36 才部署）。

    回收规则（保守，防误伤活跃任务）：
    - `queued` 且 created_at 超 queued_stale_minutes（默认 180min）→ 判死。正常集群入队→认领仅数秒。
    - 任意非终态且 stream_ended=1 ∧ self_reported=1 → 判死。双信号齐意味着收敛必须发生且应瞬时完成，
      仍停留非终态即收敛失败（finalize 路径中断）。实例：hello-calculator-14 #10 QA 任务
      t_1a1a9c440f9a（事件 2219 已 completed、structured_output 齐全，任务却恒 running 亮雷达）。
    - `running`/`dispatched` 且 started_at 超 long_running_minutes（默认 360min，worker 侧任务超时 1h，
      6h 仍运行必为孤儿，与 runtime 是否在线无关）→ 判死。
    - `running`/`dispatched` 且 runtime_id!=0 且 runtime 已离线/不存在，且 started_at 超
      dead_runtime_minutes（默认 30min）→ 判死。
    started_at 为空的 running（本机 direct 自执行）与 runtime_id=0 均按上述年龄规则保守判定，不盲杀；
    但双信号规则无年龄要求（信号齐即完成态）。
    关联事件若仍 running 一并置 failed（与 sweep_stuck_events_once 语义一致）。
    """
    from datetime import datetime, timezone, timedelta
    now = time.time()
    q_stale = _QUEUED_STALE_MINUTES if queued_stale_minutes is None else int(queued_stale_minutes)
    d_stale = _DEAD_RUNTIME_MINUTES if dead_runtime_minutes is None else int(dead_runtime_minutes)
    l_stale = _long_running_minutes() if long_running_minutes is None else int(long_running_minutes)
    i_stale = _RUNNING_IDLE_MINUTES if idle_running_minutes is None else int(idle_running_minutes)
    if l_stale <= d_stale:
        l_stale = d_stale  # 长超时阈值不得小于 runtime 死亡阈值（避免重叠误读）

    online: set = set()
    try:
        with _sweep_cursor() as c:
            c.execute("SELECT id FROM agent_runtimes WHERE status='online'")
            online = {r[0] for r in c.fetchall()}
    except Exception as e:
        log.warning(f"sweeper agent_tasks runtime scan failed: {e}")
        return 0

    dead: list = []  # (task_id, event_seq, result_text, runtime_id)
    try:
        with _sweep_cursor() as c:
            c.execute("SELECT id, event_seq, runtime_id, created_at, started_at, status, "
                      "stream_ended, self_reported, exit_code, runtime_session_id "
                      "FROM agent_tasks WHERE status IN ('queued','dispatched','running')")
            for tid, event_seq, rid, created_at, started_at, status, stream_ended, self_reported, exit_code, rt_session in c.fetchall():
                if stream_ended == 1 and self_reported == 1:
                    # 双信号齐 ≠ 失败：agent 已经跑完并自报终态，sweeper 只是补写主控漏掉的那一次
                    # 收敛。按它自报的 exit_code 决定方向，否则「跑完且成功」会被打成失败
                    # （2026-11-15 seq=9762：qa 任务 742 条消息、结论齐全，仍被判中断）。
                    if int(exit_code or 0) == 0:
                        dead.append((tid, event_seq, _DUAL_SIGNAL_OK_TXT, rid, True))
                    else:
                        dead.append((tid, event_seq, _DUAL_SIGNAL_TXT, rid, False))
                elif status == "queued":
                    age = _dt_age_minutes(created_at, now)
                    if age is not None and age >= q_stale:
                        dead.append((tid, event_seq, _QUEUED_STALE_TXT, rid, False))
                elif status in ("dispatched", "running"):
                    age = _dt_age_minutes(started_at, now)
                    if age is not None and age >= l_stale:
                        dead.append((tid, event_seq, _LONG_RUNNING_TXT, rid, False))
                    elif (rid and rid != 0 and rid not in online
                          and age is not None and age >= d_stale):
                        dead.append((tid, event_seq, _DEAD_RUNTIME_TXT, rid, False))
                    elif str(rt_session or "").strip() and age is not None:
                        # Issue #3352：有会话但长时间没有执行消息 —— 引擎在跑却不再产出，
                        # 典型是 complete/fail 回报丢了（网关 504/断网），必须回收槽位。
                        _idle = _task_idle_minutes(c, tid, now)
                        if _idle is None:
                            _idle = age   # 建会话后一条消息都没有 → 用任务年龄保守兜底
                        if _idle >= i_stale:
                            dead.append((tid, event_seq, _IDLE_RUNNING_TXT, rid, False))
    except Exception as e:
        log.warning(f"sweeper agent_tasks scan failed: {e}")
        return 0

    if not dead:
        return 0
    fixed = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with _sweep_cursor() as c:
            for tid, event_seq, txt, rid, _ok in dead:
                try:
                    _final = "completed" if _ok else "failed"
                    _code = 0 if _ok else 1
                    if event_seq:
                        c.execute("UPDATE events SET status=?, exit_code=?, completed_at=?, result=? "
                                  "WHERE seq=? AND status IN ('pending','running')",
                                  (_final, _code, now_iso, txt, event_seq))
                    c.execute("UPDATE agent_tasks SET status=?, exit_code=?, completed_at=?, "
                              "errors=?, result_text=? WHERE id=? "
                              "AND status IN ('queued','dispatched','running')",
                              (_final, _code, now_iso, txt, txt, tid))
                    # L2（2026-10-17）：回收时若任务原占用引擎槽（running/dispatched 且有 runtime_id），
                    # 同步释放 busy_slots，防僵尸计数（local-engine-busy-slots-zombie-fix-design.md §九）。
                    # queued（runtime_id=0）任务未占槽，不释放。
                    if c.rowcount > 0 and rid and rid != 0:
                        try:
                            from src.domains.runtime.engine.service import release_busy_slot
                            release_busy_slot(rid, cursor=c)
                        except Exception as _le:
                            log.warning(f"sweeper agent_tasks release busy slot {tid} failed: {_le}")
                    fixed += c.rowcount
                except Exception as e:
                    log.warning(f"sweeper agent_tasks recycle {tid} failed: {e}")
            _commit()
    except Exception as e:
        log.warning(f"sweeper agent_tasks recycle commit failed: {e}")
    if fixed:
        log.info(f"sweeper: recycled {fixed} stuck agent_task(s): "
                 f"{[(t, 'completed' if ok else 'failed') for t, _, _, _, ok in dead]}")
    return fixed

def sweep_once() -> int:
    cleaned = 0
    try:
        # worker 端本地 SQLite 可能无 events/issue_state 表，
        # 统一检测一次，跳过所有 DB 依赖的清理（事件回收/issue worktree 清理）
        _has_db_tables = True
        try:
            with _sweep_cursor() as c:
                # 不能用 sqlite_master：MySQL 后端下它必然抛异常，于是 _has_db_tables
                # 被判 False，**下面所有事件/任务回收整段被跳过**，卡在 running 的事件
                # 和 failed 的任务再没人收敛（2026-11-15 实测：5 个 issue 永久「处理中」，
                # 其中 3 个的 agent_tasks 早已 failed）。改成后端无关的存在性探测。
                c.execute("SELECT 1 FROM issue_state LIMIT 1")
                c.fetchall()
                _has_db_tables = True
        except Exception:
            _has_db_tables = False

        # 兜底回收卡 running 僵尸事件（入队/执行失败未更新终态的历史遗留）
        if _has_db_tables:
            try:
                cleaned += sweep_stuck_events_once()
            except Exception as e:
                log.warning(f"sweeper stuck-event sweep failed: {e}")
            # 兜底回收悬空 agent_tasks（queued 无 worker 认领 / runtime 已死的 running·dispatched），
            # 否则需求中心 is_running=true 雷达动画永久亮（sweep_stuck_agent_tasks_once 见 #210 佐证）
            # 「认领了但引擎会话从未建立」的任务：原先只由 agent 主动调 recover-orphans 处理，
            # agent 一旦卡住任务就永远 running（#87/#88 永久「处理中」的直接原因）。
            try:
                from src.domains.runtime.engine.sweep_no_session import sweep_claim_without_session_once
                cleaned += sweep_claim_without_session_once()
            except Exception as e:
                log.warning(f"sweeper claim-without-session sweep failed: {e}")
            try:
                cleaned += sweep_stuck_agent_tasks_once()
            except Exception as e:
                log.warning(f"sweeper stuck-task sweep failed: {e}")
            pass  # （占位：保持上面的收尾结构）
        # IM 实例心跳表只增不减（每次重启留一行）→ 周期清理 24h 前的行；
        # 实例靠心跳 UPSERT 自愈（≤10s 重建），见 bot_lease.purge_stale_instances。
        # 注意：**不能**放在上面 `if _has_db_tables:` 块里 —— 那个判断查的是 SQLite 的
        # sqlite_master，MySQL 后端恒为 False，钩子会被整块跳过（实测踩过）。
        try:
            from src.domains.runtime.im.bot_lease import purge_stale_instances
            cleaned += purge_stale_instances(older_than_hours=24)
        except Exception as e:
            log.debug(f"sweeper im-instance purge skipped: {e}")

        # Issue #993 R9：分支守卫状态表只增不减（每分支一行），按 updated_at 做 TTL 清理。
        cleaned += _sweep_branch_guard_state()
        # A7/F4：IM 只增表（去重/入站流水/卡片台账）TTL 清理
        cleaned += _sweep_im_tables()

        from src.kernel.repo_key import split_owner_repo
        from src.domains.runtime.engine import repogit
        # Issue #2041：未推送判定需远端事实复核（ls-remote 鉴权）；单次 sweep 内
        # 每仓只解析一次 token，失败降级空串（判定回退本地 ref 链）。
        _tok_cache: dict = {}

        def _tok(key: str) -> str:
            if key not in _tok_cache:
                _tok_cache[key] = _resolve_repo_git_token(key)
            return _tok_cache[key]

        if _has_db_tables:
            from src.domains.runtime.engine import repogit
            active = _active_issue_sessions()
            for repo_key, issue_id in _done_issues():
                num = str(issue_id or "").lstrip("#")
                if not num or num == "0":
                    continue
                for agent_type in _GIT_AGENTS:
                    if (repo_key, agent_type, num) in active:
                        continue
                    issue_dir = repogit.issue_dir_for(repo_key, agent_type, num)
                    if not os.path.isdir(issue_dir):
                        continue
                    try:
                        # Issue #2041：删除前过保全守卫（未推送/脏 → 先归档），
                        # 守卫拒绝（归档失败）→ 返回 False，不计数（目录保留）。
                        if repogit.cleanup_issue_worktree(repo_key, agent_type, num,
                                                          token=_tok(repo_key),
                                                          base_branch=_sweep_base_branch(repo_key),
                                                          reason="sweep_done_issue"):
                            cleaned += 1
                    except Exception as e:
                        log.warning(f"sweeper cleanup failed {issue_dir}: {e}")
        # 僵尸兜底
        ttl = _ZOMBIE_TTL_DAYS * 86400
        for p in _zombie_issue_dirs(ttl):
            try:
                # Issue #2041：workdir 删除前过保全闸（未推送/脏 → 先归档，
                # 归档失败拒绝删除，目录保留待下轮重试）；非 worktree 残留照旧清理。
                from src.domains.runtime.engine.worktree_guard import (
                    guard_issue_workdir_removal, parse_issue_workdir)
                _zrk, _znum, _zag = parse_issue_workdir(p)
                if not guard_issue_workdir_removal(p, reason="zombie_ttl",
                                                   token=_tok(_zrk)):
                    continue
                shutil.rmtree(p, ignore_errors=True)
                cleaned += 1
            except Exception as e:
                log.warning(f"sweeper zombie cleanup failed {p}: {e}")
        # chat / feishu / vod / history 目录 TTL 清理
        session_ttl = _SESSION_DIR_TTL_DAYS * 86400
        session_cleaned = 0
        for p in _stale_session_dirs(session_ttl):
            try:
                shutil.rmtree(p, ignore_errors=True)
                session_cleaned += 1
            except Exception as e:
                log.warning(f"sweeper session dir cleanup failed {p}: {e}")
        if session_cleaned:
            log.info(f"sweeper cleaned {session_cleaned} stale session dir(s)")
            cleaned += session_cleaned
        # /tmp 全局兜底清理（2026-08-29 修复）：
        # worker /tmp 是 tmpfs（3.7G），被 hermes-acp-*/hermes-results/hermes-iso-*/
        # pytest-of-root/pip-unpack-* 等残留堆积撑满 → curl ENOSPC → 静默安装失败。
        # issue #238: hermes 隔离目录已迁移到 ~/.cache，此处同步扫描新路径。
        # _cleanup_task_tmp 在任务完成时清理已知的 agent 工作产物，但异常退出/进程被杀
        # 时残留不被清理。这里在 sweeper 周期（15min）兜底扫描清理过期文件。
        tmp_cleaned = _sweep_tmp_residuals()
        if tmp_cleaned:
            log.info(f"sweeper cleaned {tmp_cleaned} /tmp residual dir(s)/file(s)")
            cleaned += tmp_cleaned
        # workspace repo 级别清理：project-info/codebase 目录在不活跃 repo 上堆积严重
        # （单个 project-info 可达 496M，多个 repo 合计 GB 级）
        ws_cleaned = _sweep_stale_workspace_repos()
        if ws_cleaned:
            log.info(f"sweeper cleaned {ws_cleaned} stale workspace repo dir(s)")
            cleaned += ws_cleaned
        if cleaned:
            log.info(f"sweeper cleaned {cleaned} issue workdir(s)")
        return cleaned
    except Exception as e:
        log.warning(f"sweep_once failed: {e}")
        return 0


def _opencode_staging_roots() -> set:
    """Issue #2225：会话暂存根集合（worker 临时目录 + 兼容 `/tmp`）。"""
    bases = set()
    try:
        from src.domains.runtime.engine.session_tmpdir import staging_base
        bases.add(staging_base())
    except Exception as e:
        log.warning(f"sweeper opencode staging root resolve failed: {e}")
    bases.add(os.path.join(tempfile.gettempdir(), "opencode"))
    bases.add("/tmp/opencode")
    return bases


def _active_session_task_ids() -> set:
    """Issue #2225：仍在 queued/dispatched/running 的任务 id（其会话子目录跳过）。"""
    ids = set()
    try:
        # G-01/A7 棘轮正解：读侧走免锁通道 bulk_read.reader()，不持 db_lock
        # （持锁 fetchall 会让心跳/claim 路径排队，check_async_blocking 拦截）。
        from src.kernel.bulk_read import reader
        with reader() as c:
            c.execute("SELECT id FROM agent_tasks "
                      "WHERE status IN ('queued','dispatched','running')")
            ids = {str(r[0]) for r in c.fetchall() if r and r[0]}
    except Exception as e:
        from src.kernel.db_errors import missing_schema
        if missing_schema(e):
            log.debug(f"sweeper opencode active-task lookup skipped (no local table): {e}")
        else:
            log.warning(f"sweeper opencode active-task lookup failed: {e}")
    return ids


def _sweep_opencode_tmpdirs(now: float, roots=None) -> int:
    """Issue #2225：清理 `/tmp/opencode` 存量平铺固定名文件与超 TTL 会话子目录。

    - 平铺文件仅清理约定固定名残留（body.md/req.json/spec.md/a2a_*.json 等，
      ``_OPCODE_FLAT_RESIDUAL_NAMES`` 白名单），超过 ``_TASK_TMPDIR_TTL_SECONDS``
      即删除，避免误导后续会话。非约定文件名不触碰——暂存根是与 /tmp 同级的
      共享目录，无权清理第三方工具的文件（QA round-2 F6）。
    - 会话子目录必须携带 ``.grape-session-tmpdir`` 标记才视为 grape 会话目录；
      超同一 TTL 兜底清理，活跃会话持续写入会刷新 mtime，不会被误删。无标记
      的目录（非 grape 创建）跳过。
    - QA 复审 R3：``roots`` 显式传入时只扫描这些目录——共享 worker 上跑 pytest 必须
      收敛扫描根到 tmp_path，否则会删掉其他会话的真实暂存目录。
    - QA 复审 Nice-to-have：``{task_id}`` 目录即便超 TTL，只要任务仍处
      queued/dispatched/running 就跳过，避免「长时间不写暂存」的活跃会话被误清。
    """
    import shutil as _shutil
    cleaned = 0
    bases = list(roots) if roots is not None else sorted(_opencode_staging_roots())
    active = _active_session_task_ids()
    for base in bases:
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                p = os.path.join(base, name)
                try:
                    if now - os.path.getmtime(p) < _TASK_TMPDIR_TTL_SECONDS:
                        continue
                    if os.path.isdir(p) and not os.path.islink(p):
                        if name in active:
                            continue
                        if not os.path.isfile(os.path.join(p, _SESSION_DIR_MARKER)):
                            continue
                        _shutil.rmtree(p, ignore_errors=True)
                        cleaned += 1
                    elif os.path.isfile(p) or os.path.islink(p):
                        if name not in _OPCODE_FLAT_RESIDUAL_NAMES:
                            continue
                        os.remove(p)
                        cleaned += 1
                except Exception as e:
                    log.warning(f"sweeper opencode residual cleanup failed {p}: {e}")
        except Exception as e:
            log.warning(f"sweeper opencode residual scan failed {base}: {e}")
    return cleaned


def _sweep_tmp_residuals() -> int:
    """兜底清理 /tmp 及 ~/.cache 下 agent 残留（sweeper 每 15min 调一次）。

    清理目标：
    - hermes-iso-* / hermes-sandbox-* / hermes-snap-*：hermes 引擎隔离目录残留
      （issue #238: 已从 /tmp 迁移到 ~/.cache；hermes-acp-homes 是持久化目录不清理）
    - hermes-results：agent call 结果缓存（只增不删）
    - pytest-of-root：测试残留
    - pip-unpack-* / pip-metadata-*：pip 安装临时目录
    - tf-deploy-* / blog-* / ptmpl_*：SRE/VOD agent 工作产物

    安全策略：
    - 只清理已知前缀的文件/目录，不碰其他进程
    - 目录按 mtime 判断：超过 1 小时未修改才清理（避免清到正在执行的任务）
    - 单文件（如 hermes-results 内的文件）同样按 mtime 判断
    """
    import glob as _glob
    import time as _time

    _TMP_TTL_SECONDS = 3600  # 1 小时
    _now = _time.time()
    cleaned = 0

    # 目录类清理（按前缀 glob + mtime 判断）
    # hermes 隔离目录已迁移到 ~/.cache（issue #238），同时保留 /tmp 旧路径兜底
    # 注意：~/.cache/hermes-acp-homes 是 ACP 模式持久化目录（state.db 续传），不清理；
    # 只清理 hermes-iso-*（executor 临时隔离目录，异常退出时残留）。
    _cache_home = os.path.expanduser("~/.cache")
    _dir_prefixes = [
        f"{_cache_home}/hermes-iso-*",
        f"{_cache_home}/hermes-sandbox-*",
        f"{_cache_home}/hermes-snap-*",
        "/tmp/hermes-acp-*",
        "/tmp/hermes-iso-*",
        "/tmp/hermes-sandbox-*",
        "/tmp/hermes-snap-*",
        "/tmp/tf-deploy-*",
        "/tmp/tf-sys-xhy*",
        "/tmp/blog-build-*",
        "/tmp/blog-deploy*",
        "/tmp/blog-gen*",
        "/tmp/blog-publish-*",
        "/tmp/pip-unpack-*",
        "/tmp/pip-metadata-*",
        "/tmp/pytest-of-root",
        "/tmp/tirith-install-*",
        "/tmp/scene_sync_zip_*",   # scene sync 临时包泄漏（2026-10 曾 2486 个/5.7G 打满 /tmp）
    ]
    for pattern in _dir_prefixes:
        for path in _glob.glob(pattern):
            try:
                mtime = os.path.getmtime(path)
                if _now - mtime < _TMP_TTL_SECONDS:
                    continue  # 仍在使用中
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    cleaned += 1
                elif os.path.isfile(path):
                    os.remove(path)
                    cleaned += 1
            except Exception:
                pass

    # Issue #2225：/tmp/opencode 治理（平铺固定名文件 + 超 TTL 会话子目录）
    cleaned += _sweep_opencode_tmpdirs(_now)

    # 文件类清理（hermes-results 目录内的文件 + 散落文件）
    _file_targets = [
        "/tmp/hermes-results",
        "/tmp/tf-state-backup.json",
        "/tmp/inventory_state.json",
        "/tmp/inventory*.json",
        "/tmp/blog-sys-*.tar.gz",
        "/tmp/.*.so",
    ]
    for pattern in _file_targets:
        for path in _glob.glob(pattern):
            try:
                if os.path.isdir(path):
                    # 目录内逐文件清理（保留目录本身）
                    for f in os.listdir(path):
                        fp = os.path.join(path, f)
                        try:
                            mtime = os.path.getmtime(fp)
                            if _now - mtime < _TMP_TTL_SECONDS:
                                continue
                            if os.path.isfile(fp):
                                os.remove(fp)
                                cleaned += 1
                        except Exception:
                            pass
                elif os.path.isfile(path):
                    mtime = os.path.getmtime(path)
                    if _now - mtime < _TMP_TTL_SECONDS:
                        continue
                    os.remove(path)
                    cleaned += 1
            except Exception:
                pass

    return cleaned

def _sweep_stale_workspace_repos() -> int:
    """清理不活跃 repo 的 project-info / codebase 目录。

    grape-workspaces/{owner}/{repo}/ 下有两个 repo 级别的大目录：
    - project-info/：codegraph 基线数据（单个可达 496M）
    - codebase/：代码克隆快照（单个可达 275M）

    这些目录不属于单个 issue workdir，sweeper 的 issue 清理逻辑覆盖不到。
    当 repo 没有任何活跃 issue（dispatched/running）且目录超过 TTL 未修改时清理。

    安全策略：
    - 只清理 project-info 和 codebase 两个子目录，不删整个 repo 目录
    - 只清理非活跃 repo（无 dispatched/running 任务）
    - 超过 3 天未修改才清理
    """
    import time as _time
    _REPO_TTL = 3 * 86400  # 3 天
    _now = _time.time()
    cleaned = 0

    # 获取有活跃任务的 repo_key 集合
    active_repos = set()
    try:
        with _sweep_cursor() as c:
            c.execute("SELECT DISTINCT repo_key FROM agent_tasks WHERE status IN ('dispatched','running')")
            active_repos = {r[0] for r in c.fetchall()}
    except Exception:
        pass

    workspace = config.WORKSPACE_DIR
    if not os.path.isdir(workspace):
        return 0

    try:
        for owner in os.listdir(workspace):
            owner_dir = os.path.join(workspace, owner)
            if not os.path.isdir(owner_dir):
                continue
            for repo_name in os.listdir(owner_dir):
                repo_dir = os.path.join(owner_dir, repo_name)
                if not os.path.isdir(repo_dir):
                    continue
                # 构造 repo_key（workspace 用 owner/repo 结构，检查两种 provider 前缀）
                _base_key = f"{owner}/{repo_name}"
                from src.kernel.repo_key import repo_key_candidates
                if any(_rk in active_repos for _rk in repo_key_candidates(_base_key)):
                    continue  # 有活跃任务，跳过
                # 检查 issue 目录是否有活跃任务
                issues_root = os.path.join(repo_dir, "issues")
                has_active_issue = False
                if os.path.isdir(issues_root):
                    for num in os.listdir(issues_root):
                        issue_dir = os.path.join(issues_root, num)
                        if not os.path.isdir(issue_dir):
                            continue
                        try:
                            mtime = os.path.getmtime(issue_dir)
                            if _now - mtime < _REPO_TTL:
                                has_active_issue = True
                                break
                        except Exception:
                            pass
                if has_active_issue:
                    continue
                # 清理 project-info 和 codebase
                for subdir in ("project-info", "codebase"):
                    target = os.path.join(repo_dir, subdir)
                    if not os.path.isdir(target):
                        continue
                    try:
                        mtime = os.path.getmtime(target)
                        if _now - mtime < _REPO_TTL:
                            continue
                        shutil.rmtree(target, ignore_errors=True)
                        cleaned += 1
                        log.info(f"sweeper cleaned stale workspace dir: {target}")
                    except Exception as e:
                        log.warning(f"sweeper workspace cleanup failed {target}: {e}")
    except Exception as e:
        log.warning(f"sweeper workspace repo scan failed: {e}")
    return cleaned

async def sweep_loop():
    """服务端后台周期清理（main.py lifespan 挂载）。"""
    import asyncio as _aio
    while True:
        _lock = _ReplicaLock("grape:sweeper:merged")
        if not await _aio.to_thread(_lock.acquire):
            log.debug("sweeper(merged): 另一副本正在清扫，本轮跳过")
            await _aio.sleep(_SWEEP_INTERVAL)
            continue
        _t0 = time.time()
        try:
            _n = await _aio.to_thread(sweep_once)
            _el = time.time() - _t0
            _log_round("merged", _n, _el)
        except Exception as e:
            log.warning(f"sweeper loop error: {e}")
        finally:
            await _aio.to_thread(_lock.release)
        await asyncio.sleep(_SWEEP_INTERVAL)

async def sweep_events_loop() -> None:
    """Event 进程：僵尸事件回收 + IM 实例清理 + workdir 清理。

    workdir 清理依赖 issue_state（Event 独占表），放在 Event 进程合理。
    """
    import asyncio as _aio
    while True:
        _lock = _ReplicaLock("grape:sweeper:events")
        if not await _aio.to_thread(_lock.acquire):
            log.debug("Event sweeper: 另一副本正在清扫，本轮跳过")
            await _aio.sleep(_SWEEP_INTERVAL)
            continue
        _t0 = time.time()
        try:
            cleaned = 0
            # 全部同步工作（pymysql 查询 / git 子进程 / ls-remote）必须进线程池：
            # 直接在事件循环里跑会把整个 loop 卡住（health 无响应、Grape 心跳停发 →
            # 引擎被判失联 → 健康分强制 0；py-spy 实证 MainThread 停在
            # sweep_stuck_events_once 的 SSLSocket.read）。
            try:
                cleaned += await _aio.to_thread(sweep_stuck_events_once)
            except Exception as e:
                log.warning(f"Event sweeper stuck-event sweep failed: {e}")
            try:
                from src.domains.runtime.im.bot_lease import purge_stale_instances
                cleaned += await _aio.to_thread(purge_stale_instances, older_than_hours=24)
            except Exception as e:
                log.debug(f"Event sweeper im-instance purge skipped: {e}")

            active = await _aio.to_thread(_active_issue_sessions)
            # Issue #2041：远端事实复核需 ls-remote 鉴权；单次 sweep 每仓解析一次。
            _tok_cache: dict = {}

            async def _tok(key: str) -> str:
                if key not in _tok_cache:
                    _tok_cache[key] = await _aio.to_thread(_resolve_repo_git_token, key)
                return _tok_cache[key]

            for repo_key, issue_id in await _aio.to_thread(_done_issues):
                num = str(issue_id or "").lstrip("#")
                if not num or num == "0":
                    continue
                for agent_type in _GIT_AGENTS:
                    if (repo_key, agent_type, num) in active:
                        continue
                    issue_dir = repogit.issue_dir_for(repo_key, agent_type, num)
                    if not os.path.isdir(issue_dir):
                        continue
                    try:
                        # Issue #2041：同 sweep_once——守卫拒绝时不计数（目录保留）。
                        # cleanup_issue_worktree 内含 git 子进程与 ls-remote，必须入线程池。
                        # Issue #2281：未推送判定以场景开发基线（GRAPE_WORK_BRANCH）为对比 ref。
                        _gone = await _aio.to_thread(
                            repogit.cleanup_issue_worktree, repo_key, agent_type, num,
                            token=await _tok(repo_key),
                            base_branch=_sweep_base_branch(repo_key),
                            reason="event_sweep_done_issue")
                        if _gone:
                            cleaned += 1
                    except Exception as e:
                        log.warning(f"Event sweeper cleanup failed {issue_dir}: {e}")
            if cleaned:
                log.info(f"Event sweeper: cleaned {cleaned} items")
            _log_round("events", cleaned, time.time() - _t0)
        except Exception as e:
            log.warning(f"Event sweeper error: {e}")
        finally:
            await _aio.to_thread(_lock.release)
        await _aio.sleep(_SWEEP_INTERVAL)

async def sweep_tasks_loop() -> None:
    """Worker 进程：悬空 agent_tasks 回收。

    sweep_stuck_agent_tasks_once 内部会重派 queued 任务，依赖 Worker 的
    local_claimer 在线认领，放在 Worker 进程合理。
    """
    import asyncio as _aio
    while True:
        _lock = _ReplicaLock("grape:sweeper:tasks")
        if not await _aio.to_thread(_lock.acquire):
            log.debug("Task sweeper: 另一副本正在清扫，本轮跳过")
            await _aio.sleep(_SWEEP_INTERVAL)
            continue
        _t0 = time.time()
        try:
            # 同 sweep_events_loop：同步 pymysql 回收必须入线程池，否则把 worker 的
            # 事件循环卡住 → 心跳停发 → 引擎被判失联（健康分强制 0）。
            cleaned = await _aio.to_thread(sweep_stuck_agent_tasks_once)
            if cleaned:
                log.info(f"Task sweeper: cleaned {cleaned} stuck tasks")
            _log_round("tasks", cleaned, time.time() - _t0)
        except Exception as e:
            log.warning(f"Task sweeper error: {e}")
        finally:
            await _aio.to_thread(_lock.release)
        await _aio.sleep(_SWEEP_INTERVAL)

# ── Issue #996：Issue 产物（docs/issue-N/*）清理预检 ─────────────────────────
# 由 weekly-10 迁入（原实现 src/domains/runtime/engine/cleanup.py）；守卫命中时保守跳过，绝不绕过。
from dataclasses import dataclass as _dc

@_dc(frozen=True)
class ArtifactScope:
    """产物清理定位参数（Issue #996）：仓库/分支/Issue 与访问凭据。"""
    repo_key: str
    ref: str
    issue_number: object
    token: str = ""
    provider: str = ""

def precheck_issue_artifacts(scope: ArtifactScope, *,
                             targets: list | None = None,
                             scripts: dict | None = None) -> dict:
    """只读预检：返回待删路径与守卫影响，不执行任何删除（命令/查询分离）。

    返回 {"targets": 原始条目, "paths": 路径列表, "guard_impact": {...}|None}。
    调用方据 guard_impact.breaking 决定是否删除；命中时由调用方自行写入
    事件 payload，避免本函数隐式修改外部对象。
    """
    from src.domains.runtime.vcs.guard_impact import assess_guard_impact, list_issue_artifacts

    resolved = list(targets) if targets is not None else list_issue_artifacts(
        scope.repo_key, scope.ref, scope.issue_number,
        token=scope.token, provider=scope.provider)
    if not resolved:
        return {"targets": [], "paths": [], "guard_impact": None}

    paths = [t.get("path") if isinstance(t, dict) else str(t) for t in resolved]
    impact = assess_guard_impact(scope.repo_key, scope.ref, paths,
                                 token=scope.token, provider=scope.provider,
                                 scripts=scripts)
    return {"targets": resolved, "paths": paths, "guard_impact": impact}

def clean_issue_artifacts(scope: ArtifactScope, *,
                          targets: list | None = None,
                          scripts: dict | None = None,
                          remover=None) -> dict:
    """产物清理入口（Issue #996）：删除 docs/issue-N/* 前做守卫影响预检。

    - 守卫脚本（scripts/check-*、tests/check-*）引用待删路径 → 跳过删除，
      返回 guard_impact{scripts, breaking:true}；
    - 无引用 → 调用 remover(targets) 删除，返回值不含 guard_impact 字段。

    清理动作（remover）由调用方注入（本地 worktree 删除 / VCS 删除 API 均可），
    本函数只负责守卫影响决策，保证保守侧：宁可保留产物，不让守卫由绿转红。
    """
    pre = precheck_issue_artifacts(scope, targets=targets, scripts=scripts)
    paths = pre["paths"]
    if not paths:
        return {"deleted": [], "skipped": []}

    impact = pre.get("guard_impact") or {}
    if impact.get("breaking"):
        log.warning(f"artifact cleanup skipped: guard scripts reference target paths "
                    f"repo={scope.repo_key} ref={scope.ref} issue={scope.issue_number} "
                    f"scripts={impact.get('scripts')}")
        return {"deleted": [], "skipped": paths, "guard_impact": impact}
    if remover is None:
        log.warning(f"artifact cleanup skipped: no remover injected "
                    f"repo={scope.repo_key} ref={scope.ref} issue={scope.issue_number}")
        return {"deleted": [], "skipped": paths, "noop": True}
    try:
        remover(pre["targets"])
    except Exception as e:
        log.warning(f"artifact cleanup remover failed repo={scope.repo_key}: {e}")
        return {"deleted": [], "skipped": paths, "error": str(e)[:200]}
    return {"deleted": paths, "skipped": []}

from dataclasses import dataclass

from datetime import datetime, timedelta, timezone

from src import config

def _sweep_im_tables() -> int:
    """A7/F4：IM 只增表按 created_at TTL 清理（sqlite / MySQL 双后端同一写法）。

    时间比较走**应用层预计算 cutoff + `?` 参数**：`datetime('now','-N hours')` 是
    SQLite 私有修饰符方言，mysql_compat 只翻译无参形式，带修饰符会在 MySQL 报 1305
    （B1 强制约束，参照 src/domains/runtime/engine/branch_guard.py / src/domains/runtime/engine/table_maintenance.py）。

    - grape_t_channel_inbound_dedup：去重窗口仅 90s，历史行无保留价值 → 24h
    - grape_t_channel_inbound_message：入站消息流水（问题定位用）→ 24h
    - grape_t_channel_outbound_card：卡片台账（重启后回调反查用）→ 7d
    失败记 warning（不静默），单表失败不影响其余表。
    """
    cleaned = 0
    specs = (
        ("grape_t_channel_inbound_dedup", "created_at", 24),
        ("grape_t_channel_inbound_message", "created_at", 24),
        ("grape_t_channel_outbound_card", "created_at", 24 * 7),
    )
    now = datetime.now(timezone.utc)
    try:
        from src.kernel.session_manager import db, db_lock
        for table, col, hours in specs:
            try:
                cutoff = (now - timedelta(hours=int(hours))).strftime("%Y-%m-%d %H:%M:%S")
                with _sweep_mgmt_cursor() as c:
                    c.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                    n = c.rowcount or 0
                    _commit()
                cleaned += n
                if n:
                    log.info(f"sweeper: purged {n} rows from {table} (older than {hours}h)")
            except Exception as e:
                from src.kernel.db_errors import missing_schema
                if missing_schema(e):
                    log.debug(f"sweeper im-table purge skipped ({table}, no local table): {e}")
                else:
                    log.warning(f"sweeper im-table purge failed ({table}): {e}")
    except Exception as e:
        from src.kernel.db_errors import missing_schema
        if missing_schema(e):
            log.debug(f"sweeper im-table purge skipped (no local tables): {e}")
        else:
            log.warning(f"sweeper im-table purge failed: {e}")
    return cleaned

def _sweep_branch_guard_state() -> int:
    """Issue #993 R9：按 updated_at TTL 清理 branch_guard_state（每分支一行、只增不减）。

    MySQL 后端下不能放在 `_has_db_tables` 判定块里（sqlite_master 检测恒 False）。
    有 pending 候选的行由 prune_guard_state 保留，避免漏一次漂移比对。
    """
    try:
        from src.domains.runtime.engine.branch_guard import prune_guard_state
        return prune_guard_state()
    except Exception as e:
        log.debug(f"sweeper branch-guard prune skipped: {e}")
        return 0

def _has_issue_state_tables() -> bool:
    """worker 端本地 SQLite 可能无 events/issue_state 表——统一检测一次。

    与 sweep_once 同语义：表缺失时跳过 DB 依赖的清理。
    注意：该检测查 SQLite 的 sqlite_master，MySQL 后端恒为 False
    （sweep_once 注释的实测结论），因此 MySQL 侧需另行兜底——
    直接 try/except 每个清理步骤（下方两个循环已按此处理）。
    """
    try:
        with _sweep_cursor() as c:
            # 与 sweep_once 同一个坑：MySQL 下 sqlite_master 必抛 → 恒 False →
            # sweep_events_once 整段跳过，僵尸事件永不回收。改用后端无关探测。
            c.execute("SELECT 1 FROM issue_state LIMIT 1")
            c.fetchall()
            return True
    except Exception:
        return False

def sweep_events_once() -> int:
    """B 维度清理（单次）：僵尸事件回收 + IM 实例清理 + workdir/目录类清理。

    从 sweep_once 拆出（设计文档 §5.2）：
    - sweep_stuck_events_once：回收卡 running 的 events（B 独占表）；
    - purge_stale_instances：IM 实例心跳表清理（A/B 共库可清）；
    - done-issue workdir 清理（读 issue 表 + issue_agent_session，与事件生命周期强相关）；
    - 僵尸 workdir / chat / feishu / vod / history 目录 TTL 清理；
    - /tmp 残留与 workspace repo 级清理（本机文件系统操作，B 所在主机执行）。
    """
    cleaned = 0
    try:
        _has_db_tables = _has_issue_state_tables()
        # 兜底回收卡 running 僵尸事件（入队/执行失败未更新终态的历史遗留）
        if _has_db_tables:
            try:
                cleaned += sweep_stuck_events_once()
            except Exception as e:
                log.warning(f"sweeper stuck-event sweep failed: {e}")
        # IM 实例心跳表只增不减（每次重启留一行）→ 周期清理 24h 前的行；
        # 注意不能放进 _has_db_tables 块（MySQL 后端该检测恒 False，实测踩过）。
        try:
            from src.domains.runtime.im.bot_lease import purge_stale_instances
            cleaned += purge_stale_instances(older_than_hours=24)
        except Exception as e:
            log.debug(f"sweeper im-instance purge skipped: {e}")
        # Issue #993 R9：分支守卫状态表只增不减（每分支一行），按 updated_at 做 TTL 清理。
        cleaned += _sweep_branch_guard_state()
        # A7/F4：IM 只增表（去重/入站流水/卡片台账）TTL 清理
        cleaned += _sweep_im_tables()

        from src.domains.runtime.engine import repogit
        if _has_db_tables:
            active = _active_issue_sessions()
            # Issue #2281：与 sweep_once / sweep_events_loop 同口径——清理前过
            # worktree_guard 保全闸，且未推送判定以场景开发基线为对比 ref。
            _tok_cache: dict = {}

            def _tok(key: str) -> str:
                if key not in _tok_cache:
                    _tok_cache[key] = _resolve_repo_git_token(key)
                return _tok_cache[key]

            for repo_key, issue_id in _done_issues():
                num = str(issue_id or "").lstrip("#")
                if not num or num == "0":
                    continue
                for agent_type in _GIT_AGENTS:
                    if (repo_key, agent_type, num) in active:
                        continue
                    issue_dir = repogit.issue_dir_for(repo_key, agent_type, num)
                    if not os.path.isdir(issue_dir):
                        continue
                    try:
                        if repogit.cleanup_issue_worktree(
                                repo_key, agent_type, num,
                                token=_tok(repo_key),
                                base_branch=_sweep_base_branch(repo_key),
                                reason="event_sweep_done_issue"):
                            cleaned += 1
                    except Exception as e:
                        log.warning(f"sweeper cleanup failed {issue_dir}: {e}")
        # 僵尸兜底
        ttl = _ZOMBIE_TTL_DAYS * 86400
        for p in _zombie_issue_dirs(ttl):
            try:
                # Issue #2281：僵尸路径同样过保全闸（与 sweep_once 一致）。
                from src.domains.runtime.engine.worktree_guard import (
                    guard_issue_workdir_removal, parse_issue_workdir)
                _zrk, _znum, _zag = parse_issue_workdir(p)
                if not guard_issue_workdir_removal(p, reason="zombie_ttl",
                                                   token=_resolve_repo_git_token(_zrk)):
                    continue
                shutil.rmtree(p, ignore_errors=True)
                cleaned += 1
            except Exception as e:
                log.warning(f"sweeper zombie cleanup failed {p}: {e}")
        # chat / feishu / vod / history 目录 TTL 清理
        session_ttl = _SESSION_DIR_TTL_DAYS * 86400
        session_cleaned = 0
        for p in _stale_session_dirs(session_ttl):
            try:
                shutil.rmtree(p, ignore_errors=True)
                session_cleaned += 1
            except Exception as e:
                log.warning(f"sweeper session dir cleanup failed {p}: {e}")
        if session_cleaned:
            log.info(f"sweeper cleaned {session_cleaned} stale session dir(s)")
            cleaned += session_cleaned
        # /tmp 全局兜底清理（见 sweep_once 注释）
        tmp_cleaned = _sweep_tmp_residuals()
        if tmp_cleaned:
            log.info(f"sweeper cleaned {tmp_cleaned} /tmp residual dir(s)/file(s)")
            cleaned += tmp_cleaned
        # workspace repo 级别清理：project-info/codebase 目录堆积
        ws_cleaned = _sweep_stale_workspace_repos()
        if ws_cleaned:
            log.info(f"sweeper cleaned {ws_cleaned} stale workspace repo dir(s)")
            cleaned += ws_cleaned
        if cleaned:
            log.info(f"event sweeper cleaned {cleaned} item(s)")
        return cleaned
    except Exception as e:
        log.warning(f"sweep_events_once failed: {e}")
        return 0

def sweep_tasks_once() -> int:
    """C 维度清理（单次）：悬空 agent_tasks 回收。

    从 sweep_once 拆出（设计文档 §5.2）。回收依赖 C 进程 local_claimer
    在线认领（回收的 queued 重派需要执行方），故归 C 进程。
    """
    cleaned = 0
    try:
        cleaned += sweep_stuck_agent_tasks_once()
    except Exception as e:
        log.warning(f"sweeper stuck-task sweep failed: {e}")
    return cleaned
