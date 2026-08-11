"""Agent Session 管理 — per (repo_key, agent_type) 独立会话"""
import uuid, json
from datetime import datetime
from src.session_manager import db, db_lock
from src.config import log


def get_agent_type_enable(agent_type: str) -> int:
    """读 agent_type.enable（全局默认，sre 默认关）。无记录返回 1（默认开）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT enable FROM agent_type WHERE agent_type=?", (agent_type,))
            row = c.fetchone()
            if row and row[0] is not None:
                return int(row[0])
    except Exception:
        pass
    return 1


def is_agent_enabled(repo_key: str, agent_type: str) -> bool:
    """仓库级 Agent 是否启用：优先 devops_agent_session.enable，无会话回退 agent_type.enable。

    见 agent-enable-toggle-design.md §五 拦截语义。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT enable FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_key, agent_type))
            row = c.fetchone()
            if row and row[0] is not None:
                return bool(int(row[0]))
    except Exception:
        pass
    return bool(get_agent_type_enable(agent_type))


# ===== Issue 生命周期状态机（issue_state，见 vod-skill-upgrade-plan.md §3.0.2）=====

def get_issue_stage(repo_key: str, issue_id: str) -> dict | None:
    """读 Issue 当前阶段。无记录返回 None。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, stage, owner_agent, a2a_seq, updated_at, assignee FROM issue_state WHERE repo_key=? AND issue_id=?",
                      (repo_key, issue_id))
            row = c.fetchone()
            if row:
                return {"id": row[0], "stage": row[1], "owner_agent": row[2],
                        "a2a_seq": row[3], "updated_at": row[4], "assignee": row[5] or ""}
    except Exception:
        pass
    return None


def set_issue_stage(repo_key: str, issue_id: str, stage: str, owner_agent: str,
                    a2a_seq: int | None = None, assignee: str = ""):
    """写入/更新 Issue 阶段（幂等 upsert，兼容 SQLite/MySQL）。

    原实现 INSERT OR REPLACE ... VALUES ((SELECT id FROM issue_state ...)) 在
    MySQL 下报 1093（自引用子查询），改为先查后改/插。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM issue_state WHERE repo_key=? AND issue_id=?", (repo_key, issue_id))
        row = c.fetchone()
        if row:
            if assignee:
                c.execute(
                    "UPDATE issue_state SET stage=?, owner_agent=?, a2a_seq=?, assignee=?, updated_at=datetime('now') WHERE id=?",
                    (stage, owner_agent, a2a_seq, assignee, row[0]),
                )
            else:
                c.execute(
                    "UPDATE issue_state SET stage=?, owner_agent=?, a2a_seq=?, updated_at=datetime('now') WHERE id=?",
                    (stage, owner_agent, a2a_seq, row[0]),
                )
        else:
            c.execute(
                "INSERT INTO issue_state (repo_key, issue_id, stage, owner_agent, a2a_seq, assignee, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                (repo_key, issue_id, stage, owner_agent, a2a_seq, assignee),
            )
        db.commit()
    from src.config import log
    log.info(f"IssueState: repo={repo_key} issue={issue_id} stage={stage} owner={owner_agent} assignee={assignee}")


def get_issue_owner(repo_key: str, issue_id: str) -> str:
    """返回 Issue 当前归属 agent（无记录默认 'vod'）。"""
    st = get_issue_stage(repo_key, issue_id)
    return st["owner_agent"] if st else "vod"


def get_issue_stage_name(repo_key: str, issue_id: str) -> str:
    """返回 Issue 当前阶段名（无记录默认 'vod'）。"""
    st = get_issue_stage(repo_key, issue_id)
    return st["stage"] if st else "vod"


def get_issue_assignee(repo_key: str, issue_id: str) -> str:
    """返回 Issue 责任人（GitCode assignee 用户名；无记录空）。"""
    st = get_issue_stage(repo_key, issue_id)
    return (st["assignee"] if st else "") or ""


def get_or_create_agent_session(repo_key: str, agent_type: str,
                                 backend: str = "opencode") -> dict:
    """获取或创建 Agent 会话，每个 (repo_key, agent_type) 独立"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, runtime_session_id, status, last_active_at, enable FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                (repo_key, agent_type)
            )
            row = c.fetchone()
        except Exception:
            row = None

        if row:
            return {
                "id": row[0],
                "runtime_session_id": row[1] or "",
                "status": row[2] or "idle",
                "last_active_at": row[3] or "",
                "enable": bool(row[4]) if row[4] is not None else True,
                "new": False,
                "repo_key": repo_key,
                "agent_type": agent_type,
            }

        sid = str(uuid.uuid4())
        now = datetime.now().isoformat()
        runtime_id = 0
        if backend:
            from src.user.db import ensure_default_backend, get_runtime_id_by_name
            runtime_id = get_runtime_id_by_name(backend) or ensure_default_backend(backend) or 0
        _enable = get_agent_type_enable(agent_type)
        try:
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, last_active_at, enable) VALUES (?,?,?,?,?,?,?)",
                (sid, repo_key, agent_type, runtime_id, "idle", now, _enable)
            )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to create agent session: {e}")
            return {"id": sid, "runtime_session_id": "", "status": "idle",
                    "last_active_at": now, "enable": bool(_enable), "new": True,
                    "repo_key": repo_key, "agent_type": agent_type}

        return {"id": sid, "runtime_session_id": "", "status": "idle",
                "last_active_at": now, "enable": bool(_enable), "new": True,
                "repo_key": repo_key, "agent_type": agent_type}


def upsert_agent_session(*, repo_key: str, agent_type: str,
                         runtime_id: int = 0, skill: str = "", skill_id: int = 0,
                         repo_gitcode_token: str = "", status: str = "idle",
                         enable: int | None = None, subscribe_events: str | None = None,
                         im_app_id: str | None = None, im_app_secret: str | None = None,
                         im_provider: str | None = None) -> str:
    """创建或更新 Agent 会话，保留既有主键 id。

    背景（2026-08 事故）：id 是 TEXT 主键 + UNIQUE(repo_key, agent_type)。旧实现每次用
    新 uuid + INSERT OR REPLACE，REPLACE 遇 UNIQUE 冲突会删旧行插新行 → id 变化，
    导致 vod_conversations.agent_session_id 悬空（"agent_session_id not found, auto-rebind"）。
    修复：已存在 → UPDATE 保留 id；不存在 → INSERT。

    enable/subscribe_events/im_* 传 None = 保留既有值（UPDATE 不改；INSERT 用默认）；
    传具体值 = 覆盖（im 三字段仅显式传值才写，防 apply_scene/ensure_default 误清飞书绑定）。
    返回会话 id。
    """
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
            (repo_key, agent_type)).fetchone()
        if row:
            sid = row[0]
            sets = ["runtime_id=?", "skill=?", "skill_id=?", "repo_gitcode_token=?",
                    "status=?", "last_active_at=datetime('now')"]
            params = [runtime_id, skill, skill_id, repo_gitcode_token, status]
            if enable is not None:
                sets.append("enable=?")
                params.append(enable)
            if subscribe_events is not None:
                sets.append("subscribe_events=?")
                params.append(subscribe_events)
            for col, val in (("im_app_id", im_app_id), ("im_app_secret", im_app_secret),
                             ("im_provider", im_provider)):
                if val is not None:
                    sets.append(f"{col}=?")
                    params.append(val)
            params.append(sid)
            c.execute(f"UPDATE devops_agent_session SET {', '.join(sets)} WHERE id=?", params)
            db.commit()
            return sid
        sid = str(uuid.uuid4())
        _enable = enable if enable is not None else get_agent_type_enable(agent_type)
        _sub = subscribe_events if subscribe_events is not None else "[]"
        c.execute(
            "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, skill_id, repo_gitcode_token, status, enable, subscribe_events, im_app_id, im_app_secret, im_provider, last_active_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (sid, repo_key, agent_type, runtime_id, skill, skill_id, repo_gitcode_token,
             status, _enable, _sub, im_app_id or "", im_app_secret or "", im_provider or ""))
        db.commit()
        return sid


def update_agent_session(session_id: str, status: str = "idle",
                         oc_session_id: str = None):
    """更新 Agent 会话状态"""
    with db_lock:
        c = db.cursor()
        try:
            if oc_session_id:
                c.execute(
                    "UPDATE devops_agent_session SET status=?, runtime_session_id=?, last_active_at=? WHERE id=?",
                    (status, oc_session_id, datetime.now().isoformat(), session_id)
                )
            else:
                c.execute(
                    "UPDATE devops_agent_session SET status=?, last_active_at=? WHERE id=?",
                    (status, datetime.now().isoformat(), session_id)
                )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update session {session_id}: {e}")


def list_agent_sessions(repo_key: str) -> list[dict]:
    """列出仓库的所有 Agent 会话"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, agent_type, runtime_id, runtime_session_id, status, last_active_at, enable FROM devops_agent_session WHERE repo_key=? ORDER BY agent_type",
                (repo_key,)
            )
            result = []
            for r in c.fetchall():
                backend_name = ""
                if r[2]:
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (r[2],))
                    brow = c.fetchone()
                    if brow and brow[0]:
                        backend_name = brow[0]
                result.append({"id": r[0], "agent_type": r[1], "backend": backend_name,
                               "runtime_session_id": r[3] or "", "status": r[4],
                               "last_active_at": r[5] or "",
                               "enable": bool(r[6]) if r[6] is not None else True})
            return result
        except Exception:
            return []


def delete_agent_session(repo_key: str, agent_type: str) -> bool:
    """删除单个 Agent 会话"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                      (repo_key, agent_type))
            db.commit()
            return c.rowcount > 0
        except Exception:
            return False


def ensure_default_agent_sessions(repo_key: str):
    """基于 agent-skill-mapping.json 的 default 场景，为仓库创建默认 Agent 会话"""
    import os
    _path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-skill-mapping.json")
    if not os.path.exists(_path):
        log.warning(f"ensure_default_agent_sessions: {_path} not found, skip")
        return

    with open(_path, "r", encoding="utf-8") as _f:
        _cfg = json.load(_f)
    _mapping = _cfg.get("scenes", {}).get("default", {}).get("mapping", {})
    if not _mapping:
        return

    from src.repo_key import split_owner_repo
    _owner, _rname = split_owner_repo(repo_key)
    from src.user.db import get_bot_token_by_repo
    _token = get_bot_token_by_repo(_owner, _rname)

    with db_lock:
        c = db.cursor()
        from src.user.db import ensure_default_backend
        _default_runtime_id = ensure_default_backend("opencode") or 0
        for _at, _sk in _mapping.items():
            try:
                from src.user.db import resolve_skill_id
                _skill_id = resolve_skill_id(_sk)
                # 保留既有主键 id 与用户配置（enable/订阅/飞书绑定），不因重建换 uuid
                #（见 upsert_agent_session 背景注释：INSERT OR REPLACE 新 uuid 会悬空 vod_conversations）
                upsert_agent_session(
                    repo_key=repo_key, agent_type=_at,
                    runtime_id=_default_runtime_id, skill=_sk, skill_id=_skill_id,
                    repo_gitcode_token=_token, status="idle",
                    enable=None, subscribe_events=None,
                    im_app_id=None, im_app_secret=None, im_provider=None,
                )
            except Exception as e:
                log.warning(f"Failed to create session for {_at}: {e}")
        db.commit()

    log.info(f"Auto-created agent sessions for repo={repo_key} from default scene")


def remove_agent_sessions(repo_key: str) -> int:
    """删除仓库的所有 Agent 会话"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
            db.commit()
            _cnt = c.rowcount
            if _cnt:
                log.info(f"Removed {_cnt} agent session(s) for repo={repo_key}")
            return _cnt
        except Exception as e:
            log.warning(f"Failed to remove sessions for {repo_key}: {e}")
            return 0


# ===== Issue 级执行会话（多 Issue 并行，见 multi-issue-isolation-design.md §3.3）=====
# 与 devops_agent_session（repo+agent 配置）分离：issue_agent_session 只承载 Issue 级
# 会话续接指针（runtime_session_id）+ 状态，作为 agent_tasks.issue_session_id 的串行粒度。

def get_or_create_issue_session(repo_key: str, agent_type: str, issue_id: str) -> dict:
    """获取或创建 Issue 级执行会话，per (repo_key, agent_type, issue_id) 独立。

    每个 Issue 拥有完全独立的 opencode session：新 Issue 首次创建时 runtime_session_id 为空
    （不继承 repo 级 devops_agent_session 的指针，避免多 Issue 共用同一 session 导致 cwd/上下文错位，
    见 vod-agent-hang-rootcause-fix.md 方案 A）；第一次执行由 opencode 生成全新 session 后回写本行。
    """
    issue_id = issue_id or ""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, runtime_session_id, status, last_active_at FROM issue_agent_session "
                "WHERE repo_key=? AND agent_type=? AND issue_id=?",
                (repo_key, agent_type, issue_id)
            )
            row = c.fetchone()
        except Exception:
            row = None
        if row:
            return {"id": row[0], "runtime_session_id": row[1] or "",
                    "status": row[2] or "idle", "last_active_at": row[3] or "", "new": False}

        # 每 Issue 独立 session：新 Issue 不继承 repo 级 session 指针，_seed 恒为空
        _seed = ""
        sid = str(uuid.uuid4())
        now = datetime.now().isoformat()
        try:
            c.execute(
                "INSERT INTO issue_agent_session (id, repo_key, agent_type, issue_id, runtime_session_id, status, last_active_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, repo_key, agent_type, issue_id, _seed, "idle", now)
            )
            db.commit()
        except Exception as e:
            log.warning(f"Failed to create issue session: {e}")
            return {"id": sid, "runtime_session_id": _seed, "status": "idle",
                    "last_active_at": now, "new": True}
        return {"id": sid, "runtime_session_id": _seed, "status": "idle",
                "last_active_at": now, "new": True}


def update_issue_session(session_id: str, runtime_session_id: str = None, status: str = None):
    """更新 Issue 级执行会话：续接指针 / 状态。指针为空则不覆盖（避免丢上下文）。"""
    if not session_id:
        return
    sets = []
    params = []
    if runtime_session_id is not None:
        sets.append("runtime_session_id=?")
        params.append(runtime_session_id)
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if not sets:
        return
    sets.append("last_active_at=?")
    params.append(datetime.now().isoformat())
    params.append(session_id)
    with db_lock:
        try:
            c = db.cursor()
            c.execute(f"UPDATE issue_agent_session SET {', '.join(sets)} WHERE id=?", params)
            db.commit()
        except Exception as e:
            log.warning(f"Failed to update issue session {session_id}: {e}")


def get_issue_session(repo_key: str, agent_type: str, issue_id: str) -> dict | None:
    """按 (repo, agent, issue) 查 Issue 级执行会话；无记录返回 None。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, runtime_session_id, status, last_active_at FROM issue_agent_session "
                  "WHERE repo_key=? AND agent_type=? AND issue_id=?",
                  (repo_key, agent_type, issue_id))
        row = c.fetchone()
        if not row:
            return None
        return {"id": row[0], "runtime_session_id": row[1] or "",
                "status": row[2] or "idle", "last_active_at": row[3] or ""}


def list_issue_sessions(repo_key: str, agent_type: str = "") -> list[dict]:
    """列出仓库的 Issue 级执行会话（运维/前端展示并行情况）。"""
    sql = "SELECT id, repo_key, agent_type, issue_id, runtime_session_id, status, last_active_at, created_at FROM issue_agent_session WHERE repo_key=?"
    params = [repo_key]
    if agent_type:
        sql += " AND agent_type=?"
        params.append(agent_type)
    sql += " ORDER BY last_active_at DESC"
    with db_lock:
        c = db.cursor()
        try:
            rows = c.execute(sql, params).fetchall()
        except Exception:
            return []
        return [{"id": r[0], "repo_key": r[1], "agent_type": r[2], "issue_id": r[3],
                 "runtime_session_id": r[4] or "", "status": r[5], "last_active_at": r[6],
                 "created_at": r[7]} for r in rows]


def remove_issue_sessions(repo_key: str):
    """删除仓库的所有 Issue 级执行会话（随仓库移除清理）。"""
    with db_lock:
        try:
            c = db.cursor()
            c.execute("DELETE FROM issue_agent_session WHERE repo_key=?", (repo_key,))
            db.commit()
            return c.rowcount
        except Exception as e:
            log.warning(f"Failed to remove issue sessions for {repo_key}: {e}")
            return 0
