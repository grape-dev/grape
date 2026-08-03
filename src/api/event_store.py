"""Event Store — 事件记录与查询（shared events table）"""
import json, threading
from datetime import datetime
from src.session_manager import db, db_lock
from src.config import log

# ====== Per-repo 锁（从旧 event_processor 迁移）======
_repo_locks = {}
_repo_locks_lock = threading.Lock()


def acquire_repo_lock(repo_key: str) -> threading.Lock:
    with _repo_locks_lock:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]


def append_event(repo_key: str, event_type: str, title: str = "",
                 issue_id: str = "", agent_type: str = "", source: str = "gitcode",
                 actor: str = "", summary: str = "",
                 payload: dict = None, status: str = "completed",
                 result: str = "") -> int:
    """写入事件到 events 表，返回 seq。result 为 Agent 答复结论（纯文本）。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status, result) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (repo_key, issue_id, event_type, source, agent_type, title, actor, summary,
                 json.dumps(payload or {}), status, result)
            )
            db.commit()
            seq = c.lastrowid
            payload_keys = list((payload or {}).keys())
            log.info(f"Event stored: seq={seq}, repo={repo_key}, type={event_type}, "
                     f"agent={agent_type}, issue={issue_id}, actor={actor}, source={source}, "
                     f"status={status}, result={result[:80]}, payload_keys={payload_keys}")
            return seq
        except Exception as e:
            log.warning(f"Event store FAILED: seq=0, repo={repo_key}, type={event_type}, "
                        f"agent={agent_type}, error={e}")
            return 0


def append_skipped_event(repo_key: str, event_type: str, reason: str, detail: str = ""):
    """写入被跳过/无产出的事件审计记录到 event_skipped 表"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("INSERT INTO event_skipped (repo_key, event_type, reason, detail) VALUES (?,?,?,?)",
                      (repo_key, event_type, reason, detail))
            db.commit()
            log.info(f"Event skipped: repo={repo_key}, type={event_type}, reason={reason}, detail={detail[:80]}")
        except Exception as e:
            log.warning(f"Event skipped FAILED: {e}")


def evaluate_agent_output(agent_type: str, tool_uses: list, final_text: str) -> tuple:
    """判定 Agent 是否有明确答复产出，返回 (has_output: bool, result: str)

    tool_uses: stream_agent 产生的 tool_use 事件输入列表（含 bash 命令）
    final_text: Agent 最终答复文本
    """
    text = final_text or ""
    has_output = False
    result = ""

    # 通用：final_text 含 GitCode 产出链接
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

    # 按 agent_type 判定
    if agent_type == "coding":
        # 检测创建 PR 的写操作
        for t in tool_uses:
            cmd = str(t.get("input", {}).get("command", "")) if isinstance(t, dict) else ""
            if "POST" in cmd.upper() and ("pulls" in cmd or "merge_requests" in cmd):
                result = "提交了代码变更（PR）"
                has_output = True
                break
    elif agent_type == "issue":
        # issue skill 的最终步骤是"必须回复评论"——final_text 非空即视为已处理（有答复产出）
        for t in tool_uses:
            cmd = str(t.get("input", {}).get("command", "")) if isinstance(t, dict) else ""
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
            cmd = str(t.get("input", {}).get("command", "")) if isinstance(t, dict) else ""
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
            cmd = str(t.get("input", {}).get("command", "")) if isinstance(t, dict) else ""
            if "DEPLOY" in cmd.upper() or ("POST" in cmd.upper() and "deploy" in cmd.lower()):
                result = "执行了部署/构建操作"
                has_output = True
                break
    elif agent_type == "vod":
        if re.search(r'/issues/\d+', text):
            m = re.search(r'/issues/(\d+)', text)
            result = f"创建了 Issue #{m.group(1) if m else '?'}"
            has_output = True

    return has_output, result


def update_event_status(seq: int, status: str):
    """更新事件状态"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("UPDATE events SET status=? WHERE seq=?", (status, seq))
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event {seq}: {e}")


def update_event_config(seq: int, backend: str, skill: str, started_at: str):
    """写入执行配置（backend, skill, started_at）"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("UPDATE events SET backend=?, skill=?, started_at=? WHERE seq=?",
                      (backend, skill, started_at, seq))
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event config {seq}: {e}")


def update_event_complete(seq: int, status: str, exit_code: int = None,
                          duration: float = None, agent_logs: list = None,
                          completed_at: str = ""):
    """写入执行结果（status, exit_code, duration, agent_logs, completed_at）"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute(
                "UPDATE events SET status=?, exit_code=?, duration=?, agent_logs=?, completed_at=? WHERE seq=?",
                (status, exit_code, duration, json.dumps(agent_logs or []), completed_at, seq)
            )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event complete {seq}: {e}")


def get_events(repo_key: str, agent_type: str = "", issue_id: str = "",
               status: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
    """查询事件列表，支持按 agent/issue/status 过滤"""
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
            c.execute(
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, backend, skill, exit_code, duration, started_at, completed_at FROM events WHERE {where} ORDER BY seq DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            )
            return [
                {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "",
                 "title": r[4] or r[2], "actor": r[5] or "", "summary": r[6] or r[4] or r[2],
                 "status": r[7], "created_at": r[8],
                 "backend": r[9] or "", "skill": r[10] or "",
                 "exit_code": r[11], "duration": r[12],
                 "started_at": r[13] or "", "completed_at": r[14] or ""}
                for r in c.fetchall()
            ]
        except Exception:
            return []


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
            "status": r[7], "created_at": r[8],
        })
    return grouped


def get_issue_summary(repo_key: str, max_issues: int = 10) -> str:
    """按 Issue 聚类生成事件摘要文本，用于 Prompt 注入"""
    grouped = get_events_grouped_by_issue(repo_key)
    if not grouped:
        return ""

    lines = []
    sorted_issues = sorted(grouped.keys(), key=lambda iid: grouped[iid][-1]["created_at"], reverse=True)[:max_issues]

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

    return "Issue Pipeline:\n" + "\n".join(lines)
