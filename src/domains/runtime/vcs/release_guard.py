"""Issue #2225：发布前 Issue/PR 号一致性断言。

背景：共享 worker 上并发会话的固定名暂存文件互相覆写，存在「把其他 Issue 的正文
写进线上 Issue/PR」的真实路径。本模块在写操作落上游前做一次一致性校验：

- 仅对 `tk_` 会话 token（agent 会话）生效；用户 JWT / `grape_` API Key 放行。
- 只锚定正文的「主语」Issue 号（首个显式 `Issue #N`）与 PR 标题首个 `#N`，
  正文里后续的跨 Issue 引用/关联不受影响（避免误伤正常评论文本）。
- 校验不一致时由调用方抛 409（CLI 非零退出），不产生任何线上写入。
- `allow_cross_issue=True` 为显式覆盖语义，供确实需要跨 Issue 写入的场景使用。
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading

from src.kernel.config import log

# 显式主语锚点：Issue #N / Issue N / Spec: Issue #N（大小写不敏感，允许复数 issues）。
# 不用 \b：中文紧邻（如「实现Issue #2225」）时中文字符也属 \w，边界不成立会漏判。
_ANCHOR_RE = re.compile(r"(?i)(?<![a-z])issues?\s*#?\s*(\d+)")
# 标题/正文通用引用：#N
_REF_RE = re.compile(r"#(\d+)")

# fail-open 计数：守卫因查询失败而静默放行时累加，便于从业务侧/日志观测失效。
# 写路径运行在多线程（请求工作线程）下，`+=` 非原子，必须加锁避免并发少记。
_FAIL_OPEN_STATS = {"count": 0}
_FAIL_OPEN_LOCK = threading.Lock()


def _record_fail_open(session_key: str, exc: Exception, mysql_backend: bool) -> None:
    """记录一次 fail-open（守卫未生效），保留 fail-open 语义不阻断写操作。"""
    with _FAIL_OPEN_LOCK:
        _FAIL_OPEN_STATS["count"] += 1
        count = _FAIL_OPEN_STATS["count"]
    backend = "mysql" if mysql_backend else "sqlite"
    log.warning(
        f"release guard: task issue lookup fail-open (guard disabled) "
        f"count={count} backend={backend} "
        f"session_key={session_key}: {exc}"
    )


def _as_int(value):
    try:
        return int(str(value).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def first_issue_anchor(text: str) -> int | None:
    """正文首个显式 `Issue #N` 主语锚点（跳过引用行）。

    引用行（`>` / `>>` 开头的行，含嵌套）是他号正文的原文摘录，不是本正文的
    主语——QA 报告等以 `> Issue #N` 引用开头是合法形态，锚点必须落在首个
    非引用段落里，否则误杀合法正文（F4）。
    """
    body = text or ""
    non_quote = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith(">")
    )
    m = _ANCHOR_RE.search(non_quote)
    return int(m.group(1)) if m else None


def first_issue_ref(text: str) -> int | None:
    """首个 Issue 引用：优先显式 `Issue #N`，否则首个 `#N`。"""
    m = _ANCHOR_RE.search(text or "")
    if m:
        return int(m.group(1))
    m = _REF_RE.search(text or "")
    return int(m.group(1)) if m else None


def _read_task_issue_id(session_key: str) -> str | None:
    """读取 `agent_tasks.issue_id` 原始值（查不到返回 None）。

    QA 复审 Nice-to-have：原实现每个写请求都在全局 `db_lock` 内 SELECT，写路径互相
    排队。改用只读连接（`mode=ro`）查询，不参与写锁；只读连接不可用时才退回共享
    连接 + `db_lock` 兜底。
    QA round-2 F5：只读快路径仅限 SQLite 后端——MySQL 后端下真实数据在 MySQL，
    `_db_path` 仍会拼出 sqlite 文件路径，若机器上残留旧 sessions.db 会读到陈旧库
    （误拦或守卫形同虚设）。
    Issue #3811：`agent_tasks` 属 runtime 域表，三库拓扑下进程主库是 mgmt，裸 SQL 恒
    1146。MySQL 分支改为按域路由（`domain_db/domain_lock/t_runtime`），无自有 DSN 回落
    进程库时告警；查询失败记录 fail-open 计数/告警，仍放行不阻断。
    """
    try:
        from src.kernel import session_manager as sm
    except Exception as e:
        log.warning(f"release guard: session_manager unavailable: {e}")
        return None
    mysql_backend = False
    try:
        mysql_backend = bool(sm._is_mysql_backend())
    except Exception as e:
        log.warning(f"release guard: db backend probe failed, assume sqlite: {e}")
    db_path = "" if mysql_backend else str(getattr(sm, "_db_path", "") or "")
    if db_path and os.path.exists(db_path):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT issue_id FROM agent_tasks WHERE id=?", (session_key,)
                ).fetchone()
            finally:
                conn.close()
            return None if not row else str(row[0] or "")
        except Exception as e:
            log.warning(f"release guard: read-only issue lookup failed ({session_key}): {e}")
    try:
        from src.kernel import db_registry
        from src.kernel.db import t_runtime

        own_dsn = db_registry.uses_own_dsn("runtime")
        # 二次探测与 own 联合判定：`uses_own_dsn()` 内部已探过一次 `domain_db()`，
        # 此处再取连接可能命中瞬时不可用窗口（探活失败/退避重试）返回 None。
        # 若此时仍按 own=True 走裸表名，会拿进程库 `sm.db` 读 runtime 表（1146），
        # 且回落告警与跨域告警都不触发。二次探测失败即视为回落，显式告警并改用
        # 进程库限定表名。
        domain_conn = db_registry.domain_db("runtime")
        own = own_dsn and domain_conn is not None
        if own:
            db = domain_conn
            lock = db_registry.domain_lock("runtime")
            table = "agent_tasks"
        else:
            if own_dsn:
                log.warning(
                    f"release guard: runtime domain dsn configured but "
                    f"domain_db() unavailable, fall back to process db "
                    f"(session_key={session_key})"
                )
            db = domain_conn or sm.db
            db_registry.warn_if_cross_domain_fallback("runtime")
            lock = sm.db_lock
            table = t_runtime("agent_tasks")
        with lock:
            row = db.cursor().execute(
                f"SELECT issue_id FROM {table} WHERE id=?", (session_key,)
            ).fetchone()
    except Exception as e:
        _record_fail_open(session_key, e, mysql_backend)
        return None
    return None if not row else str(row[0] or "")


def resolve_session_task_issue(user: dict) -> int | None:
    """从会话 token 解析当前任务 Issue 号（session_key = agent_tasks.id）。

    非会话 token / chat 会话 / 伪 Issue id / 缺省占位 `#0`·`0` / 查不到任务
    → None（fail-open，不阻断）。
    """
    if not user or user.get("auth_type") != "session_token":
        return None
    if (user.get("agent_type") or "") == "chat":
        return None
    session_key = str(user.get("session_key") or "").strip()
    if not session_key:
        return None
    raw = str(_read_task_issue_id(session_key) or "").strip()
    if not raw or raw.startswith("#pr_") or raw.startswith("autopilot-") or "pr_" in raw:
        return None
    m = re.search(r"(\d+)", raw)
    if not m:
        return None
    issue_id = int(m.group(1))
    # QA 复审 R2：`#0`/`0` 是「无 Issue 上下文」的缺省占位，必须按 None（不校验）处理；
    # 若当作 0 参与断言，该会话所有正文写入/PR 标题引用都会被 409 拦死。
    return issue_id if issue_id > 0 else None


def check_issue_consistency(*, task_issue, target_issue=None, title: str = "", body: str = "",
                            related_issue_nums=None, allow_cross_issue: bool = False,
                            operation: str = "write") -> tuple[bool, str]:
    """返回 (ok, message)。仅在任务 Issue 可确定（正整数）且未显式放行时执行校验。"""
    if allow_cross_issue:
        return True, ""
    expected = _as_int(task_issue)
    # QA 复审 R2：缺省占位 0（`#0`/`0`）与无法解析同义——无 Issue 上下文，直接放行。
    if expected is None or expected <= 0:
        return True, ""

    # 写正文到其他 Issue（元数据类更新不拦截，正文写入才拦截）
    target = _as_int(target_issue)
    if target is not None and (body or "").strip() and target != expected:
        return False, (
            f"发布前一致性断言失败：{operation} 目标 Issue #{target} 与当前任务 "
            f"Issue #{expected} 不一致，已中止且未写入线上（疑似暂存内容来自其他会话，"
            f"请核对当前任务的暂存正文后再发布）")

    if (title or "").strip():
        ref = first_issue_ref(title)
        if ref is not None and ref != expected:
            return False, (
                f"发布前一致性断言失败：{operation} 标题引用 #{ref} 与当前任务 "
                f"Issue #{expected} 不一致，已中止且未写入线上（疑似暂存内容来自其他会话，"
                f"请核对当前任务的暂存标题后再发布）")

    if (body or "").strip():
        anchor = first_issue_anchor(body)
        if anchor is not None and anchor != expected:
            return False, (
                f"发布前一致性断言失败：{operation} 正文主语为 Issue #{anchor}，与当前任务 "
                f"Issue #{expected} 不一致（疑似暂存文件被其他会话覆写），已中止且未写入线上"
                f"")

    if related_issue_nums:
        nums = [n for n in (_as_int(x) for x in related_issue_nums) if n is not None]
        if nums and expected not in nums:
            return False, (
                f"发布前一致性断言失败：{operation} 关联 Issue {nums} 未包含当前任务 "
                f"Issue #{expected}，已中止且未写入线上（疑似关联列表来自其他会话，"
                f"请核对当前任务的关联后再发布）")

    return True, ""
