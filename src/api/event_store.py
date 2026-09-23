"""Event Store — 事件记录与查询（shared events table）"""
import json, threading, asyncio
from datetime import datetime
from src.session_manager import db, db_lock
from src.config import log
from src.api.time_utils import normalize_event_time

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


def append_event(repo_key: str, event_type: str, title: str = "",
                 issue_id: str = "", agent_type: str = "", source: str = "gitcode",
                 actor: str = "", summary: str = "",
                 payload: dict = None, status: str = "completed",
                 result: str = "", scene_id: str = "") -> int:
    """写入事件到 events 表，返回 seq。result 为 Agent 答复结论（纯文本）。

    issue_id 保证非空：传入为空时按 payload → result → title/summary 顺序兜底解析，
    仍无法解析则用 #0 表示"无关联"。
    scene_id: PR 订阅等场景级事件持久化场景ID，空字符串表示无关联场景。
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
                issue_id = f"#{num}"
        if not issue_id:
            issue_id = _extract_issue_id_from_text(result) or _extract_issue_id_from_text(summary) \
                or _extract_issue_id_from_text(title)
        if not issue_id:
            issue_id = "#0"
    _seq = 0
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status, result, scene_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (repo_key, issue_id, event_type, source, agent_type, title, actor, summary,
                 json.dumps(payload or {}), status, result, scene_id)
            )
            db.commit()
            _seq = c.lastrowid
            # 验证行确实写入（防止 WAL 并发写入冲突时 lastrowid 返回旧值）
            c.execute("SELECT seq FROM events WHERE seq=?", (_seq,))
            if not c.fetchone():
                db.rollback()
                log.warning(f"Event store FAILED: INSERT returned lastrowid={_seq} but row not found (WAL conflict?), repo={repo_key}, type={event_type}")
                return 0
            payload_keys = list((payload or {}).keys())
            log.info(f"Event stored: seq={_seq}, repo={repo_key}, type={event_type}, "
                     f"agent={agent_type}, issue={issue_id}, actor={actor}, source={source}, "
                     f"status={status}, result={result[:80]}, payload_keys={payload_keys}")
        except Exception as e:
            log.warning(f"Failed to store event: {e}")
            return 0
    # WAL checkpoint 在 db_lock 释放后执行（避免阻塞其他需要 db_lock 的操作）
    try:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
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


def update_event_status(seq: int, status: str):
    """更新事件状态（含终态保护）。

    幂等保护：已终态（completed/failed/cancelled）的事件不允许被其他终态覆盖。
    - 已 completed 的事件不允许被 failed 覆盖（防止任务成功后延迟的 failed 信号覆盖结果，
      Issue #130 seq#5304 场景：任务实际成功但事件被错误标记失败）。
    - 已 failed 的事件不允许被 completed 覆盖（防止用户终止后残留进程跑完调 complete 把
      failed 覆盖成 completed，2026-08-24 seq#2488 实例）。
    - 非终态（running/dispatched）→终态或非终态→非终态的更新不受限制。
    """
    _TERMINAL = ("completed", "failed", "cancelled")
    with db_lock:
        try:
            c = db.cursor()
            if status in _TERMINAL:
                c.execute("SELECT status FROM events WHERE seq=?", (seq,))
                row = c.fetchone()
                if row and row[0] in _TERMINAL and row[0] != status:
                    log.info(f"Event {seq} already {row[0]}, skip {status} overwrite (terminal state protection)")
                    return
            c.execute("UPDATE events SET status=? WHERE seq=?", (status, seq))
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event {seq}: {e}")


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
                          completed_at: str = "", result: str = ""):
    """写入执行结果（status, exit_code, duration, agent_logs, completed_at, result）

    幂等保护：终态事件不允许被其他终态覆盖。
    - 已 completed 的事件不允许被 failed 覆盖（防止 complete→fail 竞态）。
    - 已 failed 的事件不允许被 completed 覆盖（防止用户终止后残留进程跑完
      调 complete 把 failed 覆盖成 completed，2026-08-24 seq#2488 实例）。
    """
    with db_lock:
        try:
            c = db.cursor()
            # 检查当前状态：已终态的事件不允许被其他终态覆盖
            c.execute("SELECT status FROM events WHERE seq=?", (seq,))
            row = c.fetchone()
            if row and row[0] in ("completed", "failed", "cancelled") and status in ("completed", "failed", "cancelled"):
                log.info(f"Event {seq} already {row[0]}, skip {status} overwrite (result={result[:60]})")
                return
            c.execute(
                "UPDATE events SET status=?, exit_code=?, duration=?, agent_logs=?, completed_at=?, result=? WHERE seq=?",
                (status, exit_code, duration, json.dumps(agent_logs or []), completed_at, result, seq)
            )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update event complete {seq}: {e}")
    # WAL checkpoint 在 db_lock 释放后执行
    try:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass


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
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, runtime, skill, exit_code, duration, started_at, completed_at FROM events WHERE {where} ORDER BY seq DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            )
            return [
                {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "",
                 "title": r[4] or r[2], "actor": r[5] or "", "summary": r[6] or r[4] or r[2],
                 "status": r[7], "created_at": normalize_event_time(r[8]),
                 "runtime": r[9] or "", "skill": r[10] or "",
                 "exit_code": r[11], "duration": r[12],
                 "started_at": normalize_event_time(r[13] or ""), "completed_at": normalize_event_time(r[14] or "")}
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
            "status": r[7], "created_at": normalize_event_time(r[8]),
        })
    return grouped


def get_issue_summary(repo_key: str, max_issues: int = 10, exclude_issue: str = "") -> str:
    """按 Issue 聚类生成事件摘要文本，用于 Prompt 注入。

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

        # 注入最新已完成事件的 result 摘要（关键结论，如"部署完成 EIP=xxx"）
        last_result = _get_latest_result(repo_key, iid)
        if last_result:
            snippet = last_result[:300].replace("\n", " ").strip()
            if len(last_result) > 300:
                snippet += "…"
            lines.append(f"    最新结论: {snippet}")

    return "Issue Pipeline:\n" + "\n".join(lines)


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
