"""event.json 事件档案读写模块（内容平面，Issue 级结构化产出唯一权威）。

见 agent-runtime-event-full-design.md §4.5 / tech-impl.md §1.2。
任务生命周期（queued→running→complete/fail）驱动 init→append→finalize。
写失败仅 log.warning，不阻断（events 表为可靠底账）。
"""
import json, os, threading, tempfile, re
from datetime import datetime, timezone
from src.config import WORKSPACE_DIR, log

_repo_locks: dict[str, threading.Lock] = {}
_lock_guard = threading.Lock()


def _lock_for(repo_key: str) -> threading.Lock:
    with _lock_guard:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract_num(issue_id: str) -> str:
    """从 issue_id 提取数字（#0 → 空，表示无关联 Issue）"""
    if not issue_id:
        return ""
    m = re.search(r"(\d+)", str(issue_id))
    if not m or m.group(1) == "0":
        return ""
    return m.group(1)


def _trunc_safe(data, limit: int = 1000) -> str:
    s = json.dumps(data, ensure_ascii=False, default=str) if not isinstance(data, str) else data
    return s[:limit]


def _redact(text, tokens):
    for t in tokens or []:
        if t:
            text = text.replace(t, "[REDACTED]")
    return text


def _legacy_base_dir(repo_key: str) -> str:
    """历史带前缀归档基目录（2026-08-15 前 event_archive 直接拼原始 repo_key，如 gitcode:Jason7602）。
    仅供兼容读取；新写入一律走去前缀目录（与 agent 工作区统一）。
    """
    return os.path.join(WORKSPACE_DIR, repo_key, "issues")


def _legacy_no_provider_dir(repo_key: str) -> str:
    """2026-09-14 前的无 provider 隔离路径（{WORKSPACE_DIR}/{owner}/{repo}/issues）。
    仅供兼容读取；新写入一律走 workspace_repo_dir（含 provider 前缀）。
    """
    from src.repo_key import from_repo_key
    return os.path.join(WORKSPACE_DIR, from_repo_key(repo_key), "issues")


def get_event_json_path(repo_key: str, issue_num) -> str:
    """事件归档路径（与 agent 工作区/event_pipeline 统一，见 repo_key.py 约定）。"""
    from src.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), "issues", str(issue_num), "event.json")


def _resolve_event_json_path(repo_key: str, issue_num) -> str:
    """读优先新路径，缺失时兼容历史路径（无 provider 隔离 + 带前缀）。"""
    new = get_event_json_path(repo_key, issue_num)
    if os.path.exists(new):
        return new
    # 兼容旧无 provider 路径（{WORKSPACE_DIR}/{owner}/{repo}/issues/{n}/event.json）
    old_no_provider = os.path.join(_legacy_no_provider_dir(repo_key), str(issue_num), "event.json")
    if os.path.exists(old_no_provider):
        return old_no_provider
    # 兼容更早的带前缀路径
    legacy = _legacy_base_dir(repo_key)
    if ":" in (repo_key or "") and os.path.isdir(legacy):
        old = os.path.join(legacy, str(issue_num), "event.json")
        if os.path.exists(old):
            return old
    return new


def _atomic_write(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Windows 上 os.replace 偶发 WinError 5（目标被短暂占用），重试兜底
        for _attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                import time as _t
                _t.sleep(0.05)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _load(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Archive load failed: {path}: {e}")
        return None


def _locate_by_seq(repo_key: str, seq: int, issue_num_hint: str = "") -> tuple[str, str]:
    """按 event_seq 反查 issue_num 与 event.json 路径。

    优先显式 hint（任务自带 issue_id）；其次 events 表**按 seq 精确查**（无 LIMIT，消除事件量大漏命中）；
    最后扫描 issues 目录（多 provider 兼容）。
    """
    from src.repo_key import from_repo_key
    if issue_num_hint:
        num = _extract_num(issue_num_hint)
        if num:
            return num, _resolve_event_json_path(repo_key, num)
        return "", ""
    # 事件表精确查：by seq（不依赖 limit=200，避免仓库事件量大时漏命中）
    try:
        from src.session_manager import db, db_lock as _dl
        with _dl:
            c = db.cursor()
            c.execute(
                "SELECT issue_id FROM events WHERE repo_key=? AND seq=? LIMIT 1",
                (repo_key, seq))
            row = c.fetchone()
        if row and row[0]:
            num = _extract_num(str(row[0]))
            if num:
                return num, _resolve_event_json_path(repo_key, num)
    except Exception as _e:
        log.warning(f"_locate_by_seq events lookup failed: repo={repo_key} seq={seq}: {_e}")
    # 兜底：扫描 issues 目录（新 provider 隔离 + 旧无 provider + 历史带前缀）
    from src.repo_key import workspace_repo_dir
    for base in (os.path.join(workspace_repo_dir(repo_key), "issues"),
                 _legacy_no_provider_dir(repo_key),
                 _legacy_base_dir(repo_key)):
        if not os.path.isdir(base):
            continue
        for d in sorted(os.listdir(base)):
            p = os.path.join(base, d, "event.json")
            arch = _load(p)
            if arch:
                for seg in arch.get("pipeline", []):
                    if seg.get("seq") == seq:
                        return d, p
    return "", ""


def init_event_json(repo_key, seq, evt, agent_type, runtime="", skill="", tokens=None):
    """触发时建/追加 pipeline 段（阶段 B：停写 event.json——数据由 events 表承载）。

    仅保留日志，不再写文件；段数据（seq/agent/status 等）由事件入队/`append_event`/complete 路径写 events 表。
    #0 跳过。
    """
    issue_num = _extract_num(evt.get("issue_id", ""))
    if not issue_num:
        return
    log.debug(f"Archive init (NOTE: stopped writing event.json): repo={repo_key}, issue=#{issue_num}, seq={seq}")


def append_event_archive_log(repo_key, seq, evt_type, data, tokens=None, issue_num_hint=""):
    """执行中增量写 logs（阶段 B：停写 event.json——执行日志由 report_messages → events.agent_logs 承载）。"""
    log.debug(f"Archive append (NOTE: stopped writing event.json): repo={repo_key}, seq={seq}, type={evt_type}")


def record_a2a_to(repo_key, issue_num, target_agent):
    """A2A 交办记录（阶段 B：停写 event.json——a2a 目标由 A2A 事件（events 表）承载，无需文件）。"""
    log.debug(f"Archive a2a_to (NOTE: stopped writing event.json): repo={repo_key}, issue={issue_num}, target={target_agent}")


def finalize_event_archive(repo_key, seq, status, exit_code=None, duration=None,
                           output=None, tokens=None, issue_num_hint=""):
    """完成收尾（阶段 B：停写 event.json——结构化产出由 update_event_complete → events.structured_output 承载）。

    仅保留 debug 日志，不再写 archive 文件；events 表已由 complete_task/finalize_task 路径写 status/exit_code/
    duration/structured_output/agent_logs。
    """
    log.debug(f"Archive finalize (NOTE: stopped writing event.json): repo={repo_key}, seq={seq}, status={status}")


def load_event_json(repo_key, issue_num) -> dict | None:
    return _load(_resolve_event_json_path(repo_key, issue_num))


def build_issue_context(repo_key, issue_num, max_chars=4000) -> str:
    """生成富上下文文本（供 build_prompt 注入）。

    weekly-05：优先从 events 表组装（结构化 output），无事件时回退 event.json 档案。
    """
    lines = [f"## Issue #{issue_num} 档案摘要"]
    gathered = False
    try:
        from src.api.routes.issues import _group_events_by_issue
        rk = repo_key or ""
        keys = [rk]
        if ":" in rk:
            prov, rest = rk.split(":", 1)
            keys.append((f"github:{rest}" if prov == "gitcode" else f"gitcode:{rest}"))
        _num = str(issue_num).lstrip("#")
        gmap = _group_events_by_issue(keys)
        events = gmap.get(_num) or []
        if events:
            for ev in events:
                agent = ev.get("agent_type", "?")
                st = ev.get("status", "")
                icon = {"completed": "✓", "running": "▶", "failed": "✗", "pending": "○"}.get(st, "?")
                _o = ev.get("output") or {}
                concl = _o.get("conclusion", "") if isinstance(_o, dict) else ev.get("summary", "")
                arts = ", ".join(_o.get("artifacts", [])) if isinstance(_o, dict) else ""
                nxt = _o.get("next_step", "") if isinstance(_o, dict) else ""
                lines.append(f"- [{icon}] {agent}: {concl}{(' | 产出: ' + arts) if arts else ''}{(' | 下一步: ' + nxt) if nxt else ''}")
            if events:
                _last = events[-1].get("summary", "")
                if _last:
                    lines.append(f"Summary: {_last}")
                gathered = True
    except Exception:
        gathered = False
    if gathered:
        return "\n".join(lines)[:max_chars]
    arch = load_event_json(repo_key, str(issue_num).lstrip("#"))
    if not arch:
        return ""
    lines = [f"## Issue #{issue_num} 档案摘要"]
    if arch.get("summary"):
        lines.append(f"Summary: {arch['summary']}")
    for seg in arch.get("pipeline", []):
        agent = seg.get("agent", "?")
        st = seg.get("status", "")
        icon = {"completed": "✓", "running": "▶", "failed": "✗", "pending": "○"}.get(st, "?")
        concl = seg.get("output", {}).get("conclusion", "")
        arts = ", ".join(seg.get("output", {}).get("artifacts", []))
        nxt = seg.get("output", {}).get("next_step", "")
        lines.append(f"- [{icon}] {agent}: {concl}{(' | 产出: ' + arts) if arts else ''}{(' | 下一步: ' + nxt) if nxt else ''}")
    return "\n".join(lines)[:max_chars]
