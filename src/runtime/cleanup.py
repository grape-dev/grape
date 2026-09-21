"""sweeper：Issue `done` 后清理 workdir（见 multi-issue-isolation-design.md §3.4.2）。

- 仅清理 `issue_state.stage='done'`（终态）的 Issue workdir，进行中绝不清理；
- 保留 event.json 档案（独立路径）与 issue_agent_session DB 行（审计）；
- 跳过有 dispatched/running 任务的会话；
- 对超 TTL 未活跃的僵尸 workdir 兜底清理；
- 对 chat / feishu / vod / history 目录做 TTL 清理（无活跃会话时删除）。
"""
import asyncio, os, shutil, time
from src import config
from src.config import log, CHAT_CWD_BASE
from src.session_manager import db, db_lock

_SWEEP_INTERVAL = int(os.environ.get("SWEEPER_INTERVAL_SECONDS", "900"))  # 15min
_ZOMBIE_TTL_DAYS = int(os.environ.get("SWEEPER_ZOMBIE_TTL_DAYS", "3"))  # 非 done issue workdir 3 天不活跃清理
# Chat / feishu / vod 目录 TTL（默认 3 天未活跃清理，比 issue 的 7 天更短——这些是临时会话目录）
_SESSION_DIR_TTL_DAYS = int(os.environ.get("SWEEPER_SESSION_DIR_TTL_DAYS", "3"))

_GIT_AGENTS = ("vod", "issue", "coding", "qa", "sre")


def _done_issues() -> list[tuple[str, str]]:
    """返回到达终态（stage='done'）的 (repo_key, issue_id) 列表。Phase C3：读 issue 单表。"""
    with db_lock:
        c = db.cursor()
        rows = c.execute("SELECT repo_key, issue_number FROM issue WHERE stage='done'").fetchall()
        return [(r[0], f"#{r[1]}") for r in rows]


def _active_issue_sessions() -> set[tuple[str, str, str]]:
    """有 dispatched/running 任务的 (repo_key, agent_type, issue_num) 集合（不清理）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT DISTINCT issue_session_id FROM agent_tasks WHERE status IN ('dispatched','running') AND issue_session_id!=''")
        ids = [r[0] for r in c.fetchall()]
    if not ids:
        return set()
    ph = ",".join("?" for _ in ids)
    active = set()
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT repo_key, agent_type, issue_id FROM issue_agent_session WHERE id IN ({ph})", ids)
        for r in c.fetchall():
            active.add((r[0], r[1], str(r[2]).lstrip("#")))
    return active


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
        with db_lock:
            c = db.cursor()
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
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq FROM events WHERE status='running' AND "
                "(started_at IS NULL OR started_at='') AND created_at < ?",
                (no_start_cut,))
            seqs = [r[0] for r in c.fetchall()]
            c.execute(
                "SELECT seq FROM events WHERE status='running' AND started_at != '' "
                "AND started_at IS NOT NULL AND started_at < ?",
                (stale_cut,))
            seqs += [r[0] for r in c.fetchall()]
        seqs = list(dict.fromkeys(seqs))
        if not seqs:
            return 0
        ph = ",".join("?" * len(seqs))
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT DISTINCT event_seq FROM agent_tasks WHERE event_seq IN ({ph}) "
                "AND status IN ('queued','dispatched','running')",
                seqs)
            live = {r[0] for r in c.fetchall()}
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
            try:
                with db_lock:
                    _c = db.cursor()
                    _c.execute(f"SELECT DISTINCT event_seq FROM agent_tasks WHERE event_seq IN ({ph2})", dead)
                    _with_task_rows = {r[0] for r in _c.fetchall()}
                    _c2 = db.cursor()
                    for s in dead:
                        _txt = _timeout_txt if s in _with_task_rows else _no_task_txt
                        _c2.execute(
                            "UPDATE events SET status='failed', exit_code=1, completed_at=?, result=? WHERE seq=?",
                            (now_iso, _txt, s))
                        fixed += _c2.rowcount
                    db.commit()
            except Exception as e:
                log.warning(f"sweeper: per-seq recovery failed, fallback bulk: {e}")
                with db_lock:
                    _c = db.cursor()
                    _c.execute(
                        f"UPDATE events SET status='failed', exit_code=1, completed_at=?, "
                        f"result='{_timeout_txt}' WHERE seq IN ({ph2})",
                        (now_iso, *dead))
                    fixed = _c.rowcount
                    db.commit()
            if fixed:
                log.info(f"sweeper: recovered {fixed} stuck running event(s): {dead}")
            # 取消对应的线程池排队任务（防止 event 已 failed 但子进程还在跑）
            try:
                from src.api.event_pipeline import cancel_direct_task
                for _seq in dead:
                    cancel_direct_task(_seq)
            except Exception as e:
                log.warning(f"sweeper: cancel_direct_task failed: {e}")
    except Exception as e:
        log.warning(f"sweeper event recovery failed: {e}")
    return fixed


def sweep_once() -> int:
    cleaned = 0
    try:
        # worker 端本地 SQLite 可能无 events/issue_state 表，
        # 统一检测一次，跳过所有 DB 依赖的清理（事件回收/issue worktree 清理）
        _has_db_tables = True
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='issue_state'")
                _has_db_tables = c.fetchone() is not None
        except Exception:
            _has_db_tables = False

        # 兜底回收卡 running 僵尸事件（入队/执行失败未更新终态的历史遗留）
        if _has_db_tables:
            try:
                cleaned += sweep_stuck_events_once()
            except Exception as e:
                log.warning(f"sweeper stuck-event sweep failed: {e}")
        from src.repo_key import split_owner_repo
        from src.runtime import repogit
        if _has_db_tables:
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
                        repogit.cleanup_issue_worktree(repo_key, agent_type, num)
                        cleaned += 1
                    except Exception as e:
                        log.warning(f"sweeper cleanup failed {issue_dir}: {e}")
        # 僵尸兜底
        ttl = _ZOMBIE_TTL_DAYS * 86400
        for p in _zombie_issue_dirs(ttl):
            try:
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
        # _cleanup_task_tmp 在任务完成时清理已知的 agent 工作产物，但异常退出/进程被杀
        # 时残留不被清理。这里在 sweeper 周期（15min）兜底扫描 /tmp 清理过期文件。
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


def _sweep_tmp_residuals() -> int:
    """兜底清理 /tmp 下 agent 残留（sweeper 每 15min 调一次）。

    清理目标：
    - hermes-acp-* / hermes-iso-* / hermes-sandbox-* / hermes-snap-*：hermes 引擎隔离目录残留
    - hermes-results：agent call 结果缓存（只增不删）
    - pytest-of-root：测试残留
    - pip-unpack-* / pip-metadata-*：pip 安装临时目录
    - tf-deploy-* / blog-* / ptmpl_*：SRE/VOD agent 工作产物

    安全策略：
    - 只清理 /tmp 下已知前缀的文件/目录，不碰其他进程
    - 目录按 mtime 判断：超过 1 小时未修改才清理（避免清到正在执行的任务）
    - 单文件（如 hermes-results 内的文件）同样按 mtime 判断
    """
    import glob as _glob
    import time as _time

    _TMP_TTL_SECONDS = 3600  # 1 小时
    _now = _time.time()
    cleaned = 0

    # 目录类清理（按前缀 glob + mtime 判断）
    _dir_prefixes = [
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
        with db_lock:
            c = db.cursor()
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
                if f"gitcode:{_base_key}" in active_repos or f"github:{_base_key}" in active_repos:
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
        try:
            await _aio.to_thread(sweep_once)
        except Exception as e:
            log.warning(f"sweeper loop error: {e}")
        await asyncio.sleep(_SWEEP_INTERVAL)
