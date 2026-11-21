"""接管（takeover）服务层。

设计说明：见仓库内 takeover 接口约定文档。
职责：
  · start_takeover()   —— 原子地把"正在跑的任务"转交给本地交互式会话（cancel + mark + 签 tko_ + 存会话）
  · get_takeover()     —— 重连 / 续期（重新组装 bundle，secrets 一次一发不落库）
  · finish_takeover()  —— 结束本节点：事件 completed + stage 推进 + 可选自动派发下一节点
  · abort_takeover()   —— 放弃：事件 cancelled、会话 aborted，**不重派**（防同一节点被做第二次）
  · append_takeover_messages() —— 进展写回 task_messages（actor=username）

原则（逐条对应设计文档）：
  1. 一条任务只能有一个 active 接管（takeover_sessions.active_key 唯一索引 + distributed lock）
  2. 上下文由服务端组装，复用 worker 入队同一批函数（_collect_server_env / collect_knowledge_files /
     list_task_messages_recent / get_pipeline_config）
  3. bundle_json 不落 secrets；GET 重连时重新组装（fresh env、重签 URL）
  4. abort 不重派；finish 默认派发（授权点=用户点"完成"）
  5. 接管 token = tko_ + kind='takeover' + session_key='takeover:<task_id>'
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from src.kernel.config import log
from src.kernel.issue_ident import native_issue_number  # Issue #3055
from src.kernel.lock import acquire_lock, release_lock
from src.kernel.owner_db import db_pair
db, db_lock = db_pair("runtime")
# 会话状态
ST_ACTIVE = "active"
ST_FINISHED = "finished"
ST_ABORTED = "aborted"
ST_EXPIRED = "expired"

_TAKEOVER_SESSION_ID_PREFIX = "tkos_"   # 资源 id（与 token 明文区分，避免日志里两者混淆）


class TakeoverError(Exception):
    """带 HTTP 语义的业务错误（路由层直接映射为响应）。"""

    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.extra = extra or {}


# ----------------------------------------------------------------------------- helpers

def _now() -> int:
    return int(time.time())


def _user_key(v: Any) -> str:
    """账号 id 归一为字符串（D105 后 grape_accounts.id 是 uuid，历史行可能是整数）。

    空值统一成 '0'（与原 `int(x or 0)` 的哨兵口径一致），其余原样保留字符串形态，
    这样 uuid / 数字 / 字符串三种存量值都能比较，且写入 VARCHAR(36) 列不丢信息。
    """
    s = str(v if v is not None else "").strip()
    return "0" if s in ("", "None") else s


def _same_user(sess_user_id: Any, user: dict) -> bool:
    """接管会话归属校验：会话记录的用户 == 当前调用者。

    原实现用 int(...) 比较 —— 账号 id 变成 uuid 后对任意端点都会抛
    ValueError（接管接口 500 的真因），故改为字符串归一比较。
    """
    return _user_key(sess_user_id) == _user_key((user or {}).get("id"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_dict(cursor, row) -> dict | None:
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def _issue_num(issue_id: str) -> str:
    return str(issue_id or "").lstrip("#")


def _local_port() -> int:
    try:
        from src.kernel.prompt_builder import _local_port as _lp
        return int(_lp())
    except Exception:
        return 8000


# ----------------------------------------------------------------------------- 查询

def find_running_task(repo_key: str, issue_number: str) -> dict | None:
    """按 Issue 查活动态任务（与旧 takeover-context 同口径）。

    活动态 = running / dispatched / pending_self_report。
    `pending_self_report` 是主控停机或重启时对 REMOTE（worker 承载）任务设置的中间态：
    worker 进程还活着、平台在等它自报，30 分钟内没自报才转 timeout_failed。它和
    `running` 一样属于「正在跑」——issues_center 的 is_running 也把它算作运行中。
    早先这里只认 running/dispatched，导致「主控一重启，接管就报 no_running_task」。
    """
    issue_id = f"#{_issue_num(issue_number)}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, status, agent_type, skill, skill_id, prompt, env_extra, cwd, runtime_id, "
            "       event_seq, repo_key, issue_id, user_id, account, skill_files, server_url, created_at "
            "FROM agent_tasks WHERE repo_key=? AND issue_id=? "
            "AND status IN ('running','dispatched','pending_self_report') "
            "ORDER BY created_at DESC LIMIT 1",
            (repo_key, issue_id),
        )
        return _row_dict(c, c.fetchone())


def get_task(task_id: str) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, status, agent_type, skill, skill_id, prompt, env_extra, cwd, runtime_id, "
            "       event_seq, repo_key, issue_id, user_id, account, skill_files, server_url, created_at "
            "FROM agent_tasks WHERE id=?",
            (task_id,),
        )
        return _row_dict(c, c.fetchone())


def get_session(session_id: str) -> dict | None:
    # takeover_sessions 归运行域：本模块可能挂在 mgmt 进程，走 owner 连接
    from src.kernel.domain_db import cursor as _domain_cursor  # noqa: PLC0415
    with _domain_cursor("runtime") as c:
        c.execute("SELECT * FROM takeover_sessions WHERE id=?", (session_id,))
        return _row_dict(c, c.fetchone())


def active_takeover_for(repo_key: str, issue_id: str, agent_type: str = "",
                        *, readonly: bool = False) -> dict | None:
    """查询该 Issue（可选：该 agent 节点）是否有进行中的接管。

    自愈（Issue #3201）：token 已过期的 active 会话按不活跃处理，并与
    list_takeovers 同口径把它转成 expired，避免过期接管永久冻结 Issue。

    readonly=True 时**只读**：跳过过期会话的落库自愈（仍在 SELECT 里用
    token_expires_at 过滤，语义不变）。列表/详情这类 GET 读路径必须用只读形态——
    否则 N 个 issue 就是 N 次读路径写库（PR #1869 架构审查 M1）。
    """
    now = _now()
    where = "status='active' AND repo_key=? AND issue_id=?"
    args: list[Any] = [repo_key, issue_id]
    if agent_type:
        where += " AND agent_type=?"
        args.append(agent_type)
    # 先把匹配范围内的过期会话转 expired（与 list_takeovers 同口径），
    # 再只取未过期的 active；避免"最新一条恰好过期"时漏掉仍在有效期内的接管。
    if not readonly:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"UPDATE takeover_sessions SET status=?, active_key=NULL, updated_at=? "
                    f"WHERE {where} AND token_expires_at > 0 AND token_expires_at < ?",
                    [ST_EXPIRED, _now_iso(), *args, now],
                )
                db.commit()
        except Exception as e:
            log.warning(f"takeover: expire stale sessions failed: {e}")
    with db_lock:
        c = db.cursor()
        c.execute(
            f"SELECT * FROM takeover_sessions WHERE {where} "
            f"AND (token_expires_at = 0 OR token_expires_at >= ?) "
            f"ORDER BY created_at DESC LIMIT 1",
            [*args, now],
        )
        return _row_dict(c, c.fetchone())


def active_takeover_in_repo(repo_key: str) -> dict | None:
    """查询该仓库是否有进行中的接管（仓储级判定，Issue #3201 R2）。

    autopilot 巡检任务没有真实 Issue 关联（`_autopilot_issue_ref` 产出 `autopilot-<字母>`
    这类纯字母统计合成键），而真实接管会话的 issue_id 恒为 `#<数字>`——按 issue 维度
    永远无法命中。autopilot 是仓库级任务，故以其所属仓库是否存在 active 接管为准。

    自愈口径与 `active_takeover_for` 一致：token 已过期的 active 会话先转 expired，
    再只取仍在有效期内的那条，避免过期接管永久冻结自动派发。
    """
    if not repo_key:
        return None
    now = _now()
    where = "status='active' AND repo_key=?"
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"UPDATE takeover_sessions SET status=?, active_key=NULL, updated_at=? "
                f"WHERE {where} AND token_expires_at > 0 AND token_expires_at < ?",
                (ST_EXPIRED, _now_iso(), repo_key, now),
            )
            db.commit()
    except Exception as e:
        log.warning(f"takeover: expire stale sessions failed: {e}")
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT * FROM takeover_sessions WHERE {where} "
                f"AND (token_expires_at = 0 OR token_expires_at >= ?) "
                f"ORDER BY created_at DESC LIMIT 1",
                [repo_key, now],
            )
            return _row_dict(c, c.fetchone())
    except Exception as e:
        log.warning(f"takeover: query repo takeover failed: {e}")
        return None


def skipped_dispatch_count(sess: dict | None) -> int:
    """本次接管期间已被守门忽略的自动派发条数（events.status=skipped_takeover）。

    以溯源的 event_seq 为下界：只统计接管开始后产生的事件，历史接管的忽略记录
    不会串到本次会话。

    上界（Issue #3201 审查意见）：同 Issue 下一次接管的 event_seq。会话结束后的
    新接管产生的 skipped_takeover 不能让已结束会话的计数继续累加——否则「已忽略 N 条」
    会随后续接管无限增长。用下一会话的溯源 seq 作为边界，无需在 finish 时冻结。
    """
    if not sess:
        return 0
    repo_key = str(sess.get("repo_key") or "")
    issue_id = str(sess.get("issue_id") or "")
    event_seq = int(sess.get("event_seq") or 0)
    if not repo_key or not issue_id:
        return 0
    if not issue_id.startswith("#"):
        issue_id = f"#{issue_id}"
    next_seq = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT MIN(event_seq) FROM takeover_sessions "
                "WHERE repo_key=? AND issue_id=? AND event_seq>?",
                (repo_key, issue_id, event_seq),
            )
            row = c.fetchone()
            next_seq = int(row[0] or 0) if row else 0
    except Exception as e:
        log.warning(f"takeover: query next takeover boundary failed: {e}")
    sql = ("SELECT COUNT(*) FROM events WHERE repo_key=? AND issue_id=? "
           "AND status='skipped_takeover' AND seq>?")
    params: list[Any] = [repo_key, issue_id, event_seq]
    if next_seq > 0:
        sql += " AND seq<?"
        params.append(next_seq)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(sql, params)
            row = c.fetchone()
            return int(row[0] or 0) if row else 0
    except Exception as e:
        log.warning(f"takeover: count skipped dispatches failed: {e}")
        return 0


def skipped_dispatch_message(count: int) -> str:
    return f"本次接管期间已忽略 {int(count or 0)} 条自动派发"


def list_takeovers(repo_key: str, issue_id: str = "", status: str = "active",
                   limit: int = 20) -> list[dict]:
    """按仓库/Issue 查接管会话（面板刷新后仍能找到"接管中"，不依赖浏览器 localStorage）。"""
    sql = "SELECT id, task_id, event_seq, repo_key, issue_id, agent_type, username, status, " \
          "       next_agent, handoff_seq, created_at, updated_at, finished_at, " \
          "       client_session, host, summary, finish_requested_at, token_expires_at, token_issued_at, " \
          "       result_json " \
          "FROM takeover_sessions WHERE repo_key=?"
    args: list[Any] = [repo_key]
    if issue_id:
        sql += " AND issue_id=?"
        args.append(issue_id if str(issue_id).startswith("#") else f"#{issue_id}")
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, int(limit or 20)))
    # 顺手清理过期会话：token 过期后本地会话已经不可能再发指令，
    # 留着 active_key 会让"接管中"永久挂在面板上（实测 #17 coding 就是这样卡的）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE takeover_sessions SET status=?, active_key=NULL, updated_at=? "
                      "WHERE status='active' AND token_expires_at > 0 AND token_expires_at < ?",
                      (ST_EXPIRED, _now_iso(), _now()))
            db.commit()
    except Exception as e:
        log.warning(f"takeover: expire stale sessions failed: {e}")
    with db_lock:
        c = db.cursor()
        c.execute(sql, args)
        return [_row_dict(c, r) for r in c.fetchall()]


def node_advanced_detail(repo_key: str, issue_id: str, since_seq) -> dict:
    """探测「链是否已越过本节点」：返回同 issue 里更新的 a2a_event 的 seq/agent_type。

    Issue #2517 补充发现 #2（实测 #19：start 的 cancel 与云端 agent 自然完成同秒发生）：
    下游节点在该 issue 上已开跑，本次接管与它并行。平台语义允许这种接管（finish 层有
    already_dispatched 跳过逻辑），但必须披露，否则端侧以为「链暂停在本节点」。

    时序约束（#2678 复审）：复现 trace 里下游 a2a 是 cancel 之后约 0.5s 才入库的，
    所以调用方必须在 cancel 之后探测；放在 cancel 之前查永远检不出该窗口。
    """
    try:
        since = int(since_seq or 0)
    except (TypeError, ValueError):
        since = 0
    issue_key = str(issue_id or "")
    if not (repo_key and issue_key and since):
        return {}
    if not issue_key.startswith("#"):
        issue_key = f"#{issue_key}"
    try:
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT seq, agent_type FROM events WHERE repo_key=? AND issue_id=? "
                "AND seq>? AND event_type='a2a_event' ORDER BY seq DESC LIMIT 1",
                (repo_key, issue_key, since),
            ).fetchone()
    except Exception as e:
        log.warning(f"takeover: node-advanced probe failed (allow-through): {e}")
        return {}
    if not row:
        return {}
    return {"seq": int(row[0]), "agent_type": str(row[1] or "")}


_NODE_ADVANCED_CODE = "node_already_advanced"


def attach_node_advanced(detail: dict, warnings: list, *payloads: dict) -> None:
    """把 node_already_advanced 挂到 warnings（纯 code）+ 各 payload 的 warning_details。

    为什么只追加 code（#2678 复审观察 1）：既有 warnings 元素全是稳定 code
    （skill_no_references / skill_not_found / knowledge_empty / project_info_empty ...），
    端侧按 code 映射本地化文案；把「code + 中文散文 + seq 插值」混进同一个串，端侧会落到
    未知 key 分支。结构化字段（seq / agent_type）一律走 warning_details。

    bundle 与 persist（落库版本）在 build_bundle 里共用同一个 warnings list，所以只追加
    一次即可三处同步；这里仍对各自 payload 的 warnings / warning_details 做一次幂等补齐。
    """
    if not detail:
        return
    if _NODE_ADVANCED_CODE not in warnings:
        warnings.append(_NODE_ADVANCED_CODE)
    structured = {"seq": int(detail.get("seq") or 0),
                  "agent_type": str(detail.get("agent_type") or "")}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        details = payload.get("warning_details")
        payload["warning_details"] = {
            **(details if isinstance(details, dict) else {}),
            _NODE_ADVANCED_CODE: structured,
        }
        own = payload.get("warnings")
        if isinstance(own, list) and _NODE_ADVANCED_CODE not in own:
            own.append(_NODE_ADVANCED_CODE)


def downstream_event_exists(repo_key: str, issue_id: str, agent_type: str, since_seq: int) -> dict | None:
    """接管结束前探测：下一节点是不是**已经**被派发过。

    为什么必须有：本地会话里 agent 可能自己调了 a2a（prompt 说不要，但模型不一定听），
    这时 finish 再派一次就是同一节点双跑（实测 #18 coding→qa 出现了 999020/999023 两条）。
    """
    if not (repo_key and issue_id and agent_type):
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, status FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? "
                "AND seq>? AND status IN ('pending','dispatched','running','completed') "
                "ORDER BY seq DESC LIMIT 1",
                (repo_key, issue_id if str(issue_id).startswith("#") else f"#{issue_id}",
                 agent_type, int(since_seq or 0)),
            )
            row = c.fetchone()
        return {"seq": int(row[0]), "status": str(row[1])} if row else None
    except Exception as e:
        log.warning(f"takeover: downstream probe failed: {e}")
        return None


# ----------------------------------------------------------------------------- bundle

def _issue_row(repo_key: str, issue_number: str) -> dict:
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT issue_number, title, status, stage, owner_agent, scene_id, assignee "
                "FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                (repo_key, native_issue_number(issue_number)),
            )
            row = _row_dict(c, c.fetchone())
            return row or {}
    except Exception as e:
        log.warning(f"takeover: load issue failed: {e}")
        return {}


def _segments(repo_key: str, issue_id: str) -> list[dict]:
    try:
        from src.api.event_store import get_events
        evts = get_events(repo_key, issue_id=issue_id, limit=50) or []
        return [{"agent": e.get("agent_type") or "", "seq": e.get("seq") or 0,
                 "status": e.get("status") or ""} for e in evts]
    except Exception as e:
        log.warning(f"takeover: load events failed: {e}")
        return []


def _skill_assets(task: dict, user: dict, warnings: list[str]) -> dict:
    """技能交付：内联 skill_files（入队时已有）→ OBS zip → 本地目录。"""
    name = (task.get("skill") or "").strip()
    assets: dict[str, Any] = {"name": name, "mode": "none", "files": {}, "zip_url": "",
                              "has_references": False}
    if not name:
        return assets
    # ① 入队时下发的内联包（worker 也是用这个兜底）
    try:
        raw = task.get("skill_files") or "{}"
        files = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if isinstance(files, dict) and files:
            assets.update({"mode": "inline", "files": files,
                           "has_references": any(k.startswith("references/") for k in files)})
            if not assets["has_references"]:
                warnings.append("skill_no_references")
            return assets
    except Exception as _e:
        log.debug(f"takeover.py best-effort skipped: {_e}")
    # ② 平台技能表：OBS 对象键 → zip_url（300s）+ SKILL.md
    try:
        repo_key = task.get("repo_key") or ""
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, skill_name, agent_type, scope, skill_path, file_path "
                "FROM skill WHERE skill_name=? AND is_active=1 AND ("
                "  (scope='repo' AND owner_id=?) OR scope='official' OR (scope='user' AND owner_id=?)) "
                "ORDER BY CASE scope WHEN 'repo' THEN 1 WHEN 'user' THEN 2 ELSE 3 END LIMIT 1",
                (name, repo_key, str(user.get("id") or "")),
            )
            row = _row_dict(c, c.fetchone())
        if not row:
            warnings.append("skill_not_found")
            return assets
        from src.domains.runtime.storage import get_skill_storage, skill_md_content
        storage = get_skill_storage()
        obj_key = (row.get("file_path") or "").strip()
        if storage is not None and obj_key:
            content = skill_md_content(obj_key, name) or ""
            try:
                zip_url = storage.temporary_url(obj_key)
            except Exception:
                zip_url = ""
            assets.update({"mode": "zip" if zip_url else "inline",
                           "files": {"SKILL.md": content} if content else {},
                           "zip_url": zip_url, "has_references": bool(zip_url)})
            if not zip_url:
                warnings.append("skill_inline_only")
            return assets
        # ③ 本地目录模式（official 技能随部署内置）：SKILL.md + references/ 一起内联
        sp = (row.get("skill_path") or "").strip()
        md_path = ""
        try:
            from src.kernel.config import SKILLS_DIR, resolve_official_skill_path
            md_path = resolve_official_skill_path(name) or ""
            if not md_path and sp:
                cand = sp if os.path.isabs(sp) else os.path.join(SKILLS_DIR, sp.replace("/", os.sep))
                cand_md = os.path.join(cand, "SKILL.md")
                if os.path.isfile(cand_md):
                    md_path = cand_md
        except Exception:
            md_path = ""
        if md_path and os.path.isfile(md_path):
            files: dict[str, str] = {}
            try:
                with open(md_path, "r", encoding="utf-8", errors="replace") as f:
                    files["SKILL.md"] = f.read()
                ref_dir = os.path.join(os.path.dirname(md_path), "references")
                if os.path.isdir(ref_dir):
                    budget, count = 512 * 1024, 0
                    for root, _dirs, names in os.walk(ref_dir):
                        for fn in sorted(names):
                            if count >= 50 or budget <= 0:
                                break
                            fp = os.path.join(root, fn)
                            rel = os.path.relpath(fp, os.path.dirname(md_path)).replace(os.sep, "/")
                            try:
                                with open(fp, "r", encoding="utf-8", errors="replace") as rf:
                                    text = rf.read()
                            except Exception as _e:
                                log.debug(f"takeover best-effort skipped: {_e}")
                                continue
                            files[rel] = text[: budget]
                            budget -= len(files[rel])
                            count += 1
            except Exception as e:
                log.warning(f"takeover: read local skill failed: {e}")
            has_refs = any(k.startswith("references/") for k in files)
            assets.update({"mode": "inline", "files": files, "has_references": has_refs})
            if not has_refs:
                warnings.append("skill_no_references")
            return assets
        warnings.append("skill_inline_only")
    except Exception as e:
        log.warning(f"takeover: skill assets failed: {e}")
        warnings.append("skill_assets_error")
    return assets


def _knowledge_assets(task: dict, scene_id: str, warnings: list[str]) -> dict:
    """project_info + knowledge：复用 worker 入队同一函数 collect_knowledge_files。"""
    out: dict[str, Any] = {"project_info": {"files": {}, "urls": {}, "meta": {}},
                           "knowledge": {"files": {}, "urls": {}}}
    try:
        from src.domains.runtime.engine.service import collect_knowledge_files
        collected = collect_knowledge_files(
            task.get("repo_key") or "", agent_type=task.get("agent_type") or "",
            issue_id=task.get("issue_id") or "", scene_id=scene_id or "") or {}
        pi = collected.get("project_info") or {}
        kn = collected.get("knowledge") or {}
        out["project_info"]["meta"] = collected.get("meta") or {}
        urls: dict = {}
        try:
            from src.domains.runtime.storage import get_skill_storage
            storage = get_skill_storage()
            if storage is not None and (pi or kn):
                urls = storage.generate_file_urls({**pi, **kn}) or {}
        except Exception:
            urls = {}
        for rel, item in pi.items():
            content = item.get("content") if isinstance(item, dict) else item
            out["project_info"]["files"][rel] = content or ""
        for rel, item in kn.items():
            content = item.get("content") if isinstance(item, dict) else item
            out["knowledge"]["files"][rel] = content or ""
        for rel, u in urls.items():
            target = out["project_info"] if rel in pi else out["knowledge"]
            target["urls"][rel] = u.get("url") if isinstance(u, dict) else str(u)
        if not pi:
            warnings.append("project_info_empty")
        if not kn:
            warnings.append("knowledge_empty")
    except Exception as e:
        log.warning(f"takeover: knowledge assets failed: {e}")
        warnings.append("knowledge_assets_error")
    return out


# prompt / 日志的字符预算（≈ 8k tokens 量级；实测单任务 prompt 最大 7.8k、日志最大 60k）
PROMPT_CHAR_LIMIT = 32768      # 原任务 prompt：全文，超限走「头 16k + 尾 16k」
HISTORY_CHAR_BUDGET = 32000    # 日志总预算
HISTORY_HEAD_CHARS = 8000      # 头部保留（任务怎么起步的）
HISTORY_MAX_ITEMS = 400        # 条数双上限（先到者生效）
A2A_CONTEXT_LIMIT = 3000
CARRIED_CONTEXT_LIMIT = 4000


def _clip_text(text: str, limit: int = PROMPT_CHAR_LIMIT) -> str:
    """全文优先；超过 limit 才「头一半 + 标记 + 尾一半」，保住开头约束和结尾收尾要求。"""
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    omitted = len(text) - 2 * half
    return (text[:half]
            + f"\n\n…（原文 {len(text)} 字，中间已省略 {omitted} 字）…\n\n"
            + text[-half:])


def _load_event_payload(task: dict) -> dict:
    """取任务关联事件的 payload（A2A 交办原文 / carried_context 都在里面）。"""
    seq = int(task.get("event_seq") or 0)
    if not seq:
        return {}
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT payload FROM events WHERE seq=?", (seq,))
            row = c.fetchone()
        return (json.loads(row[0] or "{}") or {}) if row else {}
    except Exception as e:
        log.warning(f"takeover: load event payload failed: {e}")
        return {}


def _msg_cost(m: dict) -> int:
    return (len(str(m.get("content") or "")) + len(str(m.get("output") or ""))
            + len(str(m.get("tool") or "")))


def _select_history(messages: list[dict], *,
                    budget: int = HISTORY_CHAR_BUDGET, head_chars: int = HISTORY_HEAD_CHARS,
                    max_items: int = HISTORY_MAX_ITEMS) -> tuple[list[dict], dict]:
    """按「字符预算 + 头尾保留」选日志，而不是「最近 200 条」硬上限。

    - 尾部优先（续接主要看最近进展），再补头部（任务起步的需求/计划）；
    - 中间不取，插一条 gap 标记，让模型知道前面还有内容；
    - 有一条超预算的大消息时至少保留它（不能让尾段空掉）。
    """
    items = [m for m in (messages or []) if isinstance(m, dict)]
    total_items = len(items)
    total_chars = sum(_msg_cost(m) for m in items)
    if total_items == 0:
        return [], {"total_items": 0, "included_items": 0, "omitted_items": 0,
                    "total_chars": 0, "included_chars": 0, "truncated": False}

    if total_items <= max_items and total_chars <= budget:
        return items, {"total_items": total_items, "included_items": total_items,
                       "omitted_items": 0, "total_chars": total_chars,
                       "included_chars": total_chars, "truncated": False}

    tail_budget = max(budget - head_chars, budget // 2)
    tail: list[dict] = []
    used = 0
    idx = total_items - 1
    while idx >= 0 and (used < tail_budget or not tail) and len(tail) < max_items:
        cost = _msg_cost(items[idx])
        if tail and used + cost > tail_budget:
            break
        tail.append(items[idx])
        used += cost
        idx -= 1
    tail.reverse()

    head: list[dict] = []
    used_head = 0
    j = 0
    while j <= idx and used_head < head_chars and (len(head) + len(tail)) < max_items:
        cost = _msg_cost(items[j])
        if head and used_head + cost > head_chars:
            break
        head.append(items[j])
        used_head += cost
        j += 1

    omitted = items[j:idx + 1]
    selected = head + [{"type": "gap",
                        "content": f"…（省略 {len(omitted)} 条 / {sum(_msg_cost(m) for m in omitted)} 字）…"}] + tail
    included_items = len(head) + len(tail)
    return selected, {
        "total_items": total_items, "included_items": included_items,
        "omitted_items": total_items - included_items,
        "total_chars": total_chars, "included_chars": used_head + used,
        "truncated": True,
    }


# --------------------------------------------------------------------------- 跨平台脚本

ENV_SCRIPT_KEYS = [
    "GRAPE_SERVER_URL", "GRAPE_CLI_MODE", "GRAPE_BOUND_SKILL", "GRAPE_ISSUE_ID",
    "GRAPE_SCENE_ID", "GRAPE_WORK_BRANCH", "ISSUE_ID", "REPO_FULL_NAME", "REPO_OWNER",
    "REPO_NAME", "ISSUE_NUMBER", "ACCOUNT", "AGENT_NAME", "GRAPE_TAKEOVER_ID",
]

SCRIPT_CWD_MARK = "{{CWD}}"


def normalize_host_os(host_os: str) -> str:
    """归一宿主 OS：windows | darwin | linux（缺省/未知一律 linux，兼容旧客户端）。"""
    v = (host_os or "").strip().lower()
    if v in ("windows", "win32", "win", "windows-nt"):
        return "windows"
    if v in ("darwin", "macos", "mac", "osx", "macosx"):
        return "darwin"
    return "linux"


def _posix_q(v: Any) -> str:
    return "'" + str(v).replace("'", "'\\''") + "'"


def _ps_q(v: Any) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _env_pairs(env: dict) -> list[tuple[str, str]]:
    return [(k, str(env[k])) for k in ENV_SCRIPT_KEYS if env.get(k) not in (None, "")]


_POSIX_REPORT = r'''#!/bin/sh
# 上报一条接管进展（tko_ 身份 → task_messages，actor=你本人）
# 用法: sh .grape-takeover-report.sh "已读完 SKILL.md，计划：1) … 2) …"
dir=$(cd "$(dirname "$0")" && pwd)
[ -f "$dir/.grape-takeover-env.sh" ] && . "$dir/.grape-takeover-env.sh"
tok=$(cat "$dir/.grape-takeover-token" 2>/dev/null || true)
[ -z "$tok" ] && { echo "缺少接管 token（.grape-takeover-token）"; exit 1; }
[ -z "$GRAPE_TAKEOVER_ID" ] && { echo "缺少 GRAPE_TAKEOVER_ID"; exit 1; }
msg=${1:-}
[ -z "$msg" ] && { echo "用法: sh .grape-takeover-report.sh 一句话进展"; exit 1; }
body=$(python3 - "$msg" <<'PY'
import json, sys
m = sys.argv[1]
print(json.dumps({"summary": m, "messages": [{"type": "text", "content": m}]}, ensure_ascii=False))
PY
)
curl -sS -X POST "$GRAPE_SERVER_URL/api/takeover/sessions/$GRAPE_TAKEOVER_ID/progress" \
  -H "content-type: application/json" \
  -H "authorization: Bearer $tok" \
  -d "$body" -o /dev/null -w "progress %{http_code}\n"
'''

_POSIX_FINISH = r'''#!/bin/sh
# 提交完成申请（agent 身份，仅记录不推进）：summary + evidence + git 改动清单
dir=$(cd "$(dirname "$0")" && pwd)
[ -f "$dir/.grape-takeover-env.sh" ] && . "$dir/.grape-takeover-env.sh"
tok=$(cat "$dir/.grape-takeover-token" 2>/dev/null || true)
[ -z "$tok" ] && { echo "缺少接管 token（可能已被用户放弃）"; exit 1; }
[ -z "$GRAPE_TAKEOVER_ID" ] && { echo "缺少 GRAPE_TAKEOVER_ID"; exit 1; }
summary=${1:-}
[ -z "$summary" ] && { echo "用法: sh .grape-takeover-request-finish.sh 一句话结论 [证据]"; exit 1; }
evidence=${2:-}
repo=""
for d in "$dir/repo" "$dir/worktree" "$dir"; do
  [ -e "$d/.git" ] && repo="$d" && break
done
files="[]"; commits="[]"; branch=""
if [ -n "$repo" ]; then
  files=$(git -C "$repo" status --porcelain 2>/dev/null | cut -c4- | grep -v __pycache__ | head -50 | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()],ensure_ascii=False))')
  commits=$(git -C "$repo" log --oneline -3 2>/dev/null | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()],ensure_ascii=False))')
  branch=$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
fi
body=$(python3 - "$summary" "$evidence" "$files" "$commits" "$branch" <<'PY'
import json, sys
s, e, f, c, br = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
def _load(x):
    try:
        return json.loads(x)
    except Exception:
        return []
files, commits = _load(f), _load(c)
actions = ["提交 " + x for x in commits]
if files:
    actions.append("修改 %d 个文件" % len(files))
artifacts = []
if br and br != "HEAD":
    artifacts.append("分支 " + br)
artifacts += files[:10]
artifacts += ["commit " + x.split(" ")[0] for x in commits[:5]]
print(json.dumps({"summary": s, "evidence": e, "files_written": files,
                  "actions": actions, "artifacts": artifacts}, ensure_ascii=False))
PY
)
curl -sS -X POST "$GRAPE_SERVER_URL/api/takeover/sessions/$GRAPE_TAKEOVER_ID/request-finish" \
  -H "content-type: application/json" \
  -H "authorization: Bearer $tok" \
  -d "$body" -o /dev/null -w "request-finish %{http_code}\n"
'''

_PS_REPORT = r'''param([string]$Message)
# 上报一条接管进展（tko_ 身份 -> task_messages）
# 用法: pwsh -ExecutionPolicy Bypass -File .\.grape-takeover-report.ps1 "已读完 SKILL.md，计划：1) …"
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
. (Join-Path $dir '.grape-takeover-env.ps1')
$tok = (Get-Content (Join-Path $dir '.grape-takeover-token') -Raw).Trim()
if (-not $tok) { Write-Error '缺少接管 token（.grape-takeover-token）'; exit 1 }
if (-not $env:GRAPE_TAKEOVER_ID) { Write-Error '缺少 GRAPE_TAKEOVER_ID'; exit 1 }
if (-not $Message) { Write-Error '用法: .\.grape-takeover-report.ps1 "一句话进展"'; exit 1 }
$body = @{ summary = $Message; messages = @(@{ type = 'text'; content = $Message }) } |
        ConvertTo-Json -Depth 4 -Compress
Invoke-RestMethod -Method Post `
  -Uri "$env:GRAPE_SERVER_URL/api/takeover/sessions/$env:GRAPE_TAKEOVER_ID/progress" `
  -Headers @{ authorization = "Bearer $tok" } `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
Write-Host 'progress 200'
'''

_PS_FINISH = r'''param([string]$Summary, [string]$Evidence = '')
# 提交完成申请（agent 身份，仅记录不推进）：summary + evidence + git 改动清单
# 用法: pwsh -ExecutionPolicy Bypass -File .\.grape-takeover-request-finish.ps1 "一句话结论" "证据"
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
. (Join-Path $dir '.grape-takeover-env.ps1')
$tok = (Get-Content (Join-Path $dir '.grape-takeover-token') -Raw).Trim()
if (-not $tok) { Write-Error '缺少接管 token（可能已被用户放弃）'; exit 1 }
if (-not $env:GRAPE_TAKEOVER_ID) { Write-Error '缺少 GRAPE_TAKEOVER_ID'; exit 1 }
if (-not $Summary) { Write-Error '用法: .\.grape-takeover-request-finish.ps1 "一句话结论" [证据]'; exit 1 }

$repo = $null
foreach ($d in @((Join-Path $dir 'repo'), (Join-Path $dir 'worktree'), $dir)) {
  if (Test-Path (Join-Path $d '.git')) { $repo = $d; break }
}
$files = @(); $commits = @(); $branch = ''
if ($repo) {
  $files = @(git -C $repo status --porcelain 2>$null | ForEach-Object { $_.Substring(3).Trim() } |
             Where-Object { $_ -and $_ -notmatch '__pycache__' } | Select-Object -First 50)
  $commits = @(git -C $repo log --oneline -3 2>$null | Where-Object { $_ })
  $branch = (git -C $repo rev-parse --abbrev-ref HEAD 2>$null)
}
$actions = @($commits | ForEach-Object { "提交 $_" })
if ($files.Count -gt 0) { $actions += ("修改 {0} 个文件" -f $files.Count) }
$artifacts = @()
if ($branch -and $branch -ne 'HEAD') { $artifacts += "分支 $branch" }
$artifacts += @($files | Select-Object -First 10)
$artifacts += @($commits | Select-Object -First 5 | ForEach-Object { "commit " + ($_ -split ' ')[0] })

$body = @{ summary = $Summary; evidence = $Evidence; files_written = $files
           actions = $actions; artifacts = $artifacts } | ConvertTo-Json -Depth 5 -Compress
Invoke-RestMethod -Method Post `
  -Uri "$env:GRAPE_SERVER_URL/api/takeover/sessions/$env:GRAPE_TAKEOVER_ID/request-finish" `
  -Headers @{ authorization = "Bearer $tok" } `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
Write-Host 'request-finish 200'
'''


def _script_posix(*, env: dict) -> list[dict]:
    env_lines = ["#!/bin/sh", "# 接管时生成：任务级环境变量（0600）"]
    for k, v in _env_pairs(env):
        env_lines.append("export %s=%s" % (k, _posix_q(v)))
    env_lines.append("export GRAPE_AUTH_FILE=%s" % _posix_q(SCRIPT_CWD_MARK + "/.grape-takeover-token"))
    return [
        {"name": "env", "filename": ".grape-takeover-env.sh", "ext": "sh", "mode": "0600",
         "exec": "source", "content": "\n".join(env_lines) + "\n"},
        {"name": "report", "filename": ".grape-takeover-report.sh", "ext": "sh", "mode": "0700",
         "exec": "sh", "content": _POSIX_REPORT},
        {"name": "request_finish", "filename": ".grape-takeover-request-finish.sh", "ext": "sh",
         "mode": "0700", "exec": "sh", "content": _POSIX_FINISH},
    ]


def _script_windows(*, env: dict) -> list[dict]:
    env_lines = ["# 接管时生成：任务级环境变量（0600）   （写文件时用 UTF-8 with BOM，PowerShell 5.1 解析中文必需）"]
    for k, v in _env_pairs(env):
        env_lines.append("$env:%s = %s" % (k, _ps_q(v)))
    env_lines.append("$env:GRAPE_AUTH_FILE = %s" % _ps_q(SCRIPT_CWD_MARK + "\\.grape-takeover-token"))
    return [
        {"name": "env", "filename": ".grape-takeover-env.ps1", "ext": "ps1", "mode": "0600",
         "exec": "dot-source", "content": "\n".join(env_lines) + "\n"},
        {"name": "report", "filename": ".grape-takeover-report.ps1", "ext": "ps1", "mode": "0700",
         "exec": "pwsh -File", "content": _PS_REPORT},
        {"name": "request_finish", "filename": ".grape-takeover-request-finish.ps1", "ext": "ps1",
         "mode": "0700", "exec": "pwsh -File", "content": _PS_FINISH},
    ]


def build_script_assets(*, host_os: str, env: dict, takeover_id: str = "") -> list[dict]:
    """按宿主 OS 生成接管脚本资产（单一事实源；插件只负责落地 + 替换 {{CWD}}）。"""
    if normalize_host_os(host_os) == "windows":
        return _script_windows(env=env)
    return _script_posix(env=env)


def script_invocation(host_os: str, name: str) -> str:
    """prompt 里给 agent 看的调用形式（与 build_script_assets 同源）。"""
    win = normalize_host_os(host_os) == "windows"
    if name == "env":
        return ". .\\.grape-takeover-env.ps1" if win else "source .grape-takeover-env.sh"
    if name == "report":
        return ('pwsh -ExecutionPolicy Bypass -File .\\.grape-takeover-report.ps1 "…"' if win
                else 'sh .grape-takeover-report.sh "…"')
    if name == "request_finish":
        return ('pwsh -ExecutionPolicy Bypass -File .\\.grape-takeover-request-finish.ps1 "…" "…"' if win
                else 'sh .grape-takeover-request-finish.sh "…" "…"')
    return ""


def render_takeover_prompt(*, repo: str, issue_number: str, agent_type: str, skill: str,
                           cancel_ok: bool, cancel_error: str, task_id: str,
                           workspace_created: bool, cwd: str, skill_ok: bool,
                           has_references: bool, messages: list[dict],
                           original_prompt: str, a2a_context: str = "",
                           carried_context: str = "", history_meta: dict | None = None,
                           project_files: list[str] | None = None,
                           knowledge_files: list[str] | None = None,
                           host_os: str = "") -> str:
    """服务端统一渲染接管 prompt（多平台单一事实源，见设计文档 §8.2）。"""
    _win = normalize_host_os(host_os) == "windows"
    _env_file = ".grape-takeover-env.ps1" if _win else ".grape-takeover-env.sh"
    _env_load = script_invocation(host_os, "env")
    _report_cmd = script_invocation(host_os, "report")
    _finish_cmd = script_invocation(host_os, "request_finish")
    _shell_hint = (r"PowerShell（脚本用 `. .\.grape-takeover-env.ps1` 载入，"
                   r"命令用 `pwsh -ExecutionPolicy Bypass -File ...`）" if _win else
                   "sh/bash（脚本用 `source .grape-takeover-env.sh` 载入）")
    lines: list[str] = [
        "## 任务接管",
        f"你接管了 Issue #{issue_number} 的 {agent_type} 任务。",
        f"仓库: {repo}",
        f"技能: {skill}",
        "接管模式: **交互式** —— 决策点先问用户，不要自行推进。",
        "仓库写操作（标签/负责人/里程碑/评论/PR）由你自己用 `grape` CLI 完成 —— "
        "接管 token（tko_）已被授予**本任务仓库**的 git-repo 权限。",
        f"收尾方式：先用 `{_finish_cmd}` 提交结论，再请用户在前端点「确认完成并交回」。"
        "**不要**直接调 /api/takeover/sessions/{id}/finish —— 那是用户的授权点，agent 调会 403。",
        (f"原任务 {task_id} 已取消，由你在本地继续。"
         if cancel_ok else
         f"原任务 {task_id} 取消失败（{cancel_error}）—— 它可能仍在远端执行，注意避免重复操作。"),
        "",
    ]

    lines += [
        "## 动手之前（重要）",
        "平台侧的取消是即时的，但 worker 要等它**下一次上报**收到 410 才真正 kill 引擎（实测可达数分钟）。",
        "所以这个工作目录里可能出现不是你做的改动：动手前先 `git status` 和 `git log --oneline -3` 看一眼；",
        "发现来历不明的改动，先向用户确认再继续 —— 不要覆盖、回滚或删除你不确定的文件。",
        "",
        "## 工作区",
        f"你的工作目录是 `{cwd}`。",
        f"1) 先 `{_env_load}` 载入任务环境变量（仓库/分支/Issue/token）；"
        "token 同时在 `.grape-takeover-token`（0600），CLI 会通过 `GRAPE_AUTH_FILE` 读取。"
        f"当前宿主环境：{_shell_hint}。",
        f"2) 若目录里**还没有代码**（接管时新建，远端产物未迁移）："
        f"`grape git clone {repo}.git .`；再看 `$GRAPE_WORK_BRANCH` 是否需要切分支"
        "（非空且不存在时从默认分支新建同名分支）。已有代码则跳过。",
        (f"3) 技能已物化到 `.grape-takeover-skills/{skill}/`（含 references/），直接读 SKILL.md 并遵守其中规则"
         f"（`.skills/` 是 worker 的目录，可能已过期或被清理，不要用；下文原始 prompt 里的 `.skills/` 路径请按此条替换）。"
         if (skill_ok and has_references) else
         f"3) 技能主文件在 `.grape-takeover-skills/{skill}/SKILL.md`；需要 references/ 而目录里没有时，"
         "用 `grape skills detail` 取回。"),
    ]
    _pf = [x for x in (project_files or []) if x]
    if _pf:
        lines.append(
            "4) 项目图谱已放在工作目录根部（由平台 codegraph 生成，**开工前先读前两个**）："
            "`project-graph.md`（架构总览 / 模块依赖图 / 核心调用链 / 后台任务与调度 / "
            "错误处理 / 鉴权 / 部署 / 关键文件清单 八段）、"
            "`project-capabilities.md`（技术栈 / 功能面 / API 面 / 集成 / 数据模型 / 配置 六段）、"
            "`.metadata.json`（含 last_commit_sha、graph_version、degraded，可判断图谱是否过期/降级）。"
            "图谱与代码不一致时以 `git log` 为准。"
        )
    lines.append("")

    # 沉淀知识：有就给清单，没有就明说（否则 agent 会以为自己漏看了目录）
    _kf = [x for x in (knowledge_files or []) if x]
    if _kf:
        lines += ["## 项目知识（已沉淀）", "以下文件已放在工作目录，按需阅读：", ""]
        lines += [f"- `{rel}`" for rel in _kf[:20]]
        if len(_kf) > 20:
            lines.append(f"- …（共 {len(_kf)} 个文件）")
        lines.append("")
    else:
        _hint = ("需要项目背景时以上面的项目图谱为准。"
                 if _pf else
                 "需要项目背景时读 README / 目录结构，或用 `grape repo` 查看仓库信息。")
        lines += [
            "## 项目知识（已沉淀）",
            "本仓库暂无已沉淀的 agent 知识（`assets.knowledge` 为空），不必在目录里翻找知识文件；"
            + _hint,
            "",
        ]

    if a2a_context or carried_context:
        lines += ["## 上一节点交办（A2A）", ""]
        if a2a_context:
            lines += [a2a_context, ""]
        if carried_context:
            lines += ["结构化交接：", "```json", carried_context, "```", ""]

    if messages:
        rendered: list[str] = []
        for m in messages:
            t = (m.get("type") or "").strip()
            who = "用户" if (m.get("actor") or "") not in ("", "agent") else ""
            tool = (m.get("tool") or "").strip()
            content = str(m.get("content") or "")[:500]
            output = str(m.get("output") or "")[:500]
            prefix = f"[{who}] " if who else ""
            if t == "step_start":
                rendered.append(f"\n--- {content} ---")
            elif t == "reasoning":
                rendered.append(f"{prefix}[思考] {content}")
            elif t == "text":
                rendered.append(f"{prefix}[回复] {content}")
            elif t == "tool_use":
                rendered.append(f"{prefix}[工具调用] {tool}{(': ' + content) if content else ''}")
            elif t == "tool_result":
                rendered.append(f"{prefix}[工具结果] {content or output}")
            elif t == "error":
                rendered.append(f"{prefix}[错误] {content}")
            elif t == "log":
                rendered.append(f"{prefix}[日志] {content}")
            elif t == "gap":
                rendered.append(f"\n{content}\n")
        if rendered:
            _hm = history_meta or {}
            _title = "## 原任务执行日志"
            if _hm.get("truncated"):
                _title += (f"（共 {_hm.get('total_items', 0)} 条，节选 {_hm.get('included_items', 0)} 条 / "
                           f"{_hm.get('included_chars', 0)} 字，中间已省略）")
            else:
                _title += f"（共 {len(messages)} 条）"
            lines += [_title, "", "\n".join(rendered), ""]
            if _hm.get("truncated"):
                lines += [f"需要更早的日志时按需拉取（不要凭空猜测）："
                          f"`grape task logs --task_id {task_id} --since 0`", ""]

    if original_prompt:
        lines += ["## 原始任务 Prompt（任务入队时的完整 prompt）", "", original_prompt, ""]

    lines += [
        f"请基于以上上下文继续处理 Issue #{issue_number}。",
        "",
        "## 进展上报（必须）",
        f"工作区里有 `{('.grape-takeover-report.ps1' if _win else '.grape-takeover-report.sh')}`，"
        "用它把关键进展写回平台（actor=你本人）：",
        f"  `{_report_cmd}`",
        "时机：① 开工第一条（一句话计划）② 每个关键步骤完成后 ③ 遇到阻塞/需要用户决策时。",
        "不要刷屏：一次一句话，写'做了什么/结果如何'，不要贴大段日志。",
        "这条通道只在接管会话内有效（tko_ 身份），不要拿它调别的接口。",
        "",
        "## 完成后的收尾",
        "1) 先把本节点该做的**仓库写入**做完（用 `grape` CLI，tko_ 已获本仓库权限）：",
        "   例：issue 节点 → 打标签 / 设负责人 / 排里程碑 / 发结论评论；coding 节点 → 推分支 / 开 PR。",
        f"2) 提交完成申请：`{_finish_cmd}`（两个参数：一句话结论 / 证据）",
        "   脚本会把 summary / evidence / 改动文件清单写回平台（仅记录，不推进流水线）。",
        "3) 向用户汇报改了哪些文件、当前状态、还有哪些待确认点，请他点「确认完成并交回」——",
        "   **推进到下一节点必须由用户授权**，不要自行 A2A，也不要自己调 finish 接口。",
        "",
    ]
    return "\n".join(lines)


def build_bundle(*, task: dict, session_id: str, token: str, token_expires_at: int,
                 user: dict, platform_meta: dict | None = None,
                 cancel_ok: bool = True, cancel_error: str = "",
                 base_url: str = "", host_os: str = "") -> tuple[dict, list[str], dict]:
    """组装 bundle。返回 (bundle, warnings, persist)。

    persist = 需要落 bundle_json 的部分（去掉所有 secrets：token / AK SK / 签名 URL）。
    """
    repo_key = task.get("repo_key") or ""
    issue_number = _issue_num(task.get("issue_id") or "")
    agent_type = task.get("agent_type") or ""
    warnings: list[str] = []
    if cancel_ok:
        # 取消是即时的，但远端引擎要等下一次上报收到 410 才 kill（实测可达数分钟）→
        # 这期间它可能还在写同一个 worktree。不阻塞用户，改成显式警告 + prompt 规则。
        warnings.append("remote_may_still_run")

    if not (task.get("cwd") or "").strip():
        try:
            from src.domains.runtime.pipeline.event_pipeline import get_agent_workdir
            task["cwd"] = get_agent_workdir(repo_key, agent_type, task.get("issue_id") or "")
        except Exception as e:
            log.warning(f"takeover: derive cwd failed: {e}")

    issue = _issue_row(repo_key, issue_number)
    scene_id = (issue.get("scene_id") or "").strip()
    if not scene_id:
        try:
            from src.api.session_store import get_issue_scene
            sc = get_issue_scene(repo_key, f"#{issue_number}")
            scene_id = (sc[0] if sc and sc[0] else "") or ""
        except Exception:
            scene_id = ""

    pipeline_cfg: dict = {}
    try:
        from src.api.pipeline_config import get_pipeline_config
        pipeline_cfg = get_pipeline_config(scene_id or "default", repo_key=repo_key) or {}
    except Exception as e:
        log.warning(f"takeover: pipeline config failed: {e}")
        warnings.append("pipeline_config_error")

    nodes = [{"id": n.get("id") or n.get("tag") or "", "tag": n.get("tag") or n.get("id") or "",
              "skill": n.get("skill") or "", "next": n.get("next") or "",
              "handoff": n.get("handoff") or "a2a"}
             for n in (pipeline_cfg.get("nodes") or [])]

    # —— env：入队快照 + 服务端 fresh env + 接管专用覆盖 ——
    env: dict[str, Any] = {}
    try:
        from src.domains.runtime.engine.env_extra_security import decrypt_env_extra
        env = decrypt_env_extra(task.get("env_extra")) or {}  # enc:v1（#2423）
    except Exception:
        env = {}
    try:
        from src.domains.runtime.engine.service import _collect_server_env
        fresh = _collect_server_env(task.get("user_id") or 0, task.get("account") or "") or {}
        for k, v in fresh.items():
            env.setdefault(k, v)
    except Exception as e:
        log.warning(f"takeover: fresh env failed: {e}")
        warnings.append("fresh_env_error")

    # 优先用调用方实际访问的地址（request.base_url）：配置里的 worker_connect_url 是给
    # 远端 worker 的（本机常写成 3000），本地接管会话用它必然 connection refused。
    base = (base_url or "").rstrip("/") or env.get("GRAPE_SERVER_URL") or _internal_base_url(0)
    env.update({
        "GRAPE_SERVER_URL": base,
        "GRAPE_SESSION_TOKEN": token,
        "GRAPE_CLI_MODE": "ALL",           # 人是接管者，不再继承 worker 的 WORKER
        "GRAPE_BOUND_SKILL": task.get("skill") or "",
        "REPO_FULL_NAME": env.get("REPO_FULL_NAME") or repo_key.split(":", 1)[-1],
        "REPO_OWNER": env.get("REPO_OWNER") or repo_key.split(":", 1)[-1].split("/")[0],
        "REPO_NAME": env.get("REPO_NAME") or repo_key.split("/")[-1],
        "ISSUE_NUMBER": str(issue_number),
        "AGENT_NAME": agent_type,
        "ISSUE_ID": f"#{issue_number}",
        # 本地会话上报进展/回执用的会话 id（tko_ 身份只允许上报"自己这条"）
        "GRAPE_TAKEOVER_ID": session_id,
    })
    if scene_id:
        env["GRAPE_SCENE_ID"] = scene_id

    # —— history：字符预算 + 头尾保留（不是「最近 200 条」硬上限） ——
    messages: list[dict] = []
    history_meta: dict = {"total_items": 0, "included_items": 0, "omitted_items": 0,
                          "total_chars": 0, "included_chars": 0, "truncated": False}
    try:
        from src.domains.runtime.engine.service import list_task_messages_recent
        raw_msgs = list_task_messages_recent(task.get("id") or "", limit=1000) or []
        for m in raw_msgs:
            m.setdefault("actor", m.get("actor") or "agent")
        messages, history_meta = _select_history(raw_msgs)
    except Exception as e:
        log.warning(f"takeover: messages failed: {e}")
        warnings.append("history_error")

    # —— A2A 交办：从事件 payload 结构化取（prompt 里那段在长 prompt 中可能被护栏截断） ——
    payload = _load_event_payload(task)
    a2a_context = _clip_text(str(payload.get("context") or ""), A2A_CONTEXT_LIMIT)
    _cc = payload.get("carried_context")
    carried_context = ""
    if _cc:
        try:
            carried_context = _clip_text(json.dumps(_cc, ensure_ascii=False), CARRIED_CONTEXT_LIMIT)
        except Exception:
            carried_context = ""

    # —— assets ——
    skill_assets = _skill_assets(task, user, warnings)
    knowledge = _knowledge_assets(task, scene_id, warnings)

    repo_short = repo_key.split(":", 1)[-1]
    prompt = render_takeover_prompt(
        repo=repo_short, issue_number=issue_number, agent_type=agent_type,
        skill=task.get("skill") or agent_type, cancel_ok=cancel_ok,
        cancel_error=cancel_error, task_id=task.get("id") or "",
        workspace_created=bool((platform_meta or {}).get("workspace_created")),
        cwd=task.get("cwd") or "",
        skill_ok=bool(skill_assets.get("files") or skill_assets.get("zip_url")),
        has_references=bool(skill_assets.get("has_references")),
        messages=messages,
        # project_info / knowledge 的实际文件名：prompt 里要点名让 agent 去读，
        # 否则文件写进了 cwd 但没人知道（原缺口 §8 ①②）
        project_files=sorted(((knowledge.get("project_info") or {}).get("files") or {}).keys()),
        knowledge_files=sorted(((knowledge.get("knowledge") or {}).get("files") or {}).keys()),
        original_prompt=_clip_text(task.get("prompt") or ""),
        a2a_context=a2a_context, carried_context=carried_context,
        history_meta=history_meta,
        host_os=host_os,
    )
    scripts = build_script_assets(host_os=host_os, env=env, takeover_id=session_id)

    # clone 地址单点派生（平台卡 host）；过去写死 gitcode.com，非 gitcode 仓的接管
    # 会给 agent 一个错平台的 clone_url。派生失败留空优于给错地址。
    try:
        from src.domains.runtime.vcs.clone_endpoint import clone_url as _clone_url

        _bundle_clone_url = _clone_url(repo_key)
    except Exception as _clone_err:
        log.warning(f"takeover: clone endpoint resolve failed ({repo_key}): {_clone_err}")
        _bundle_clone_url = ""
    bundle = {
        "takeover_id": session_id,
        "task": {"id": task.get("id") or "", "repo_key": repo_key, "issue_number": issue_number,
                 "agent_type": agent_type, "skill": task.get("skill") or "",
                 "event_seq": task.get("event_seq") or 0, "runtime_id": task.get("runtime_id") or 0,
                 "server_url": task.get("server_url") or "",
                 "a2a_context": a2a_context, "carried_context": carried_context},
        "issue": {"number": issue_number, "title": issue.get("title") or "",
                  "stage": issue.get("stage") or "", "owner_agent": issue.get("owner_agent") or "",
                  "scene_id": scene_id, "status": issue.get("status") or "",
                  "assignee": issue.get("assignee") or ""},
        "pipeline": {"scene_id": scene_id, "scene_name": pipeline_cfg.get("display_name") or pipeline_cfg.get("name") or "",
                     "nodes": nodes, "segments": _segments(repo_key, f"#{issue_number}"),
                     "issue_stage": issue.get("stage") or ""},
        "workspace": {"repo_full_name": repo_short,
                      "clone_url": _bundle_clone_url,
                      "branch_hint": env.get("GRAPE_WORK_BRANCH") or "",
                      "work_branch": env.get("GRAPE_WORK_BRANCH") or "",
                      "cwd_hint": task.get("cwd") or ""},
        "instructions": {"prompt": prompt, "mode": "interactive", "template_version": "1",
                         "finish_rules": "完成后由用户点「完成并交回流水线」；不要自行 A2A"},
        "env": env,
        "platform": {"host_os": normalize_host_os(host_os),
                     "host_arch": (platform_meta or {}).get("arch") or "",
                     "host_shell": (platform_meta or {}).get("shell") or ""},
        "assets": {"skill": skill_assets, "scripts": scripts, **knowledge},
        "history": {"messages": messages, "window": 200,
                    "truncated": bool(history_meta.get("truncated")),
                    "meta": history_meta},
        "capabilities": {"supports_progress": True, "supports_finish": True,
                         "supports_abort": True, "token_kind": "tko_",
                         "token_expires_at": token_expires_at},
        "warnings": warnings,
    }

    # 落库版本：去掉 secrets（env 里的 token / fresh AK SK、签名 URL、messages 保留）
    safe_env = {k: v for k, v in env.items() if "TOKEN" not in k.upper() and "SECRET" not in k.upper()
                and "KEY" not in k.upper()}
    persist = {
        **{k: bundle[k] for k in ("task", "issue", "pipeline", "workspace", "instructions",
                                  "capabilities", "warnings", "platform")},
        "env": safe_env,
        "assets": {"skill": {**skill_assets, "zip_url": ""},
                   "scripts": scripts,
                   "project_info": {"files": knowledge["project_info"]["files"],
                                    "meta": knowledge["project_info"]["meta"], "urls": {}},
                   "knowledge": {"files": knowledge["knowledge"]["files"], "urls": {}}},
        "history": {"messages": messages[-50:], "window": 200,
                    "truncated": bool(history_meta.get("truncated")), "meta": history_meta},
    }
    return bundle, warnings, persist


# ----------------------------------------------------------------------------- start

def start_takeover(*, repo_key: str, issue_number: str, user: dict,
                   agent_type: str = "", platform: str = "", host: str = "", base_url: str = "",
                   client_version: str = "", supports: dict | None = None,
                   host_os: str = "", host_arch: str = "", host_shell: str = "") -> dict:
    """开始接管（原子）。返回 {session, bundle, reused}；失败抛 TakeoverError。

    错误面约定（P0-2 加固）：本函数对外只抛 TakeoverError —— 未预期异常统一兜成
    `500 internal_error` + `log.exception`（带 repo/issue/agent，日志同一行有 request_id），
    调用方据此返回 `{ok:false, code, message, request_id}`，不再出现「裸 500 + 无堆栈」。
    """
    try:
        return _start_takeover(
            repo_key=repo_key, issue_number=issue_number, user=user,
            agent_type=agent_type, platform=platform, host=host, base_url=base_url,
            client_version=client_version, supports=supports,
            host_os=host_os, host_arch=host_arch, host_shell=host_shell,
        )
    except TakeoverError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：对外统一错误面，异常写日志
        log.exception(
            f"takeover: unexpected failure repo={repo_key} issue=#{issue_number} "
            f"agent={agent_type or '-'}: {exc}"
        )
        raise TakeoverError(500, "internal_error",
                            "服务端内部错误（已记录日志，请附带 request_id 反馈）") from exc


def _start_takeover(*, repo_key: str, issue_number: str, user: dict,
                    agent_type: str = "", platform: str = "", host: str = "", base_url: str = "",
                    client_version: str = "", supports: dict | None = None,
                    host_os: str = "", host_arch: str = "", host_shell: str = "") -> dict:
    """start_takeover 的实现体（不对外；所有分支只抛 TakeoverError）。"""
    from src.kernel.auth_sdk.auth_token import issue_takeover_token, revoke_takeover_token
    from src.domains.runtime.engine import service as rt

    issue_number = _issue_num(issue_number)
    if not repo_key or not issue_number:
        raise TakeoverError(400, "bad_request", "需要 repo_key 与 issue_number")
    issue_id = f"#{issue_number}"

    # ① 已有进行中的接管 → 幂等返回（重新组装 fresh bundle）
    sess = active_takeover_for(repo_key, issue_id, agent_type)
    if sess:
        # 归属校验：_rebuild_for_session 会吊销原 token 并按调用者重签，
        # 不校验就会让同仓库其它用户接管他人会话并踢掉对方 token。
        if not _same_user(sess.get("user_id"), user):
            raise TakeoverError(403, "not_owner", "该接管会话不属于当前用户")
        return {"reused": True, "session": sess, "bundle": _rebuild_for_session(sess, user)}

    task = find_running_task(repo_key, issue_number)
    if not task:
        raise TakeoverError(404, "no_running_task", f"没有找到 Issue #{issue_number} 正在运行的任务")
    if agent_type and task.get("agent_type") and agent_type != task.get("agent_type"):
        raise TakeoverError(409, "agent_mismatch",
                            f"该任务属于 {task.get('agent_type')} 节点，不是 {agent_type}")
    if task.get("status") not in ("running", "dispatched", "pending_self_report"):
        raise TakeoverError(409, "already_terminal", f"任务 {task.get('id')} 已终态（{task.get('status')}）")

    task_id = task["id"]
    lock_key = f"takeover:{task_id}"
    # 锁不是硬依赖：kernel 端口未绑定（PortNotBound）/锁表不可达等异常一律降级，
    # 并发窗口最终由 takeover_sessions.active_key 唯一索引兜住（冲突 → 409）。
    # 硬依赖的旧行为是锁一抛就冒泡成裸 500（会话未写、任务未取消），现象为通用「服务器内部错误」。
    lock_held = False
    lock_degraded = False
    try:
        lock_held = bool(acquire_lock(lock_key, ttl=60))
    except Exception as exc:  # noqa: BLE001 - 锁不可用不阻断接管
        lock_degraded = True
        log.warning(f"takeover: distributed lock unavailable for {lock_key}, "
                    f"degrade to active_key unique index: {exc}")
    if not lock_held and not lock_degraded:
        raise TakeoverError(409, "taken_by_other", "另一个接管请求正在处理该任务，请稍后重试")
    try:
        # ② 组装 bundle（在 cancel 之前读 prompt/env/messages，避免与 worker 的写竞争）
        session_id = _TAKEOVER_SESSION_ID_PREFIX + secrets.token_hex(12)
        tok = issue_takeover_token(task_id=task_id, repo_key=repo_key,
                                   agent_type=task.get("agent_type") or "",
                                   user_id=_user_key(user.get("id")))
        if not tok:
            raise TakeoverError(503, "token_issue_failed", "接管 token 签发失败")

        # ③ 占位：同一任务只能一个 active（唯一索引兜底跨进程）
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "INSERT INTO takeover_sessions "
                    "(id, active_key, task_id, event_seq, repo_key, issue_id, agent_type, user_id, username, "
                    " runtime_id, platform, host, client_version, status, token_sha256, token_expires_at, "
                    " token_issued_at, bundle_json, warnings_json, created_at, updated_at, "
                    " host_os, host_arch, host_shell) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, task_id, task_id, int(task.get("event_seq") or 0), repo_key, issue_id,
                     task.get("agent_type") or "", _user_key(user.get("id")), user.get("username") or "",
                     int(task.get("runtime_id") or 0), platform or "", host or "", client_version or "",
                     ST_ACTIVE, tok["token_sha256"], int(tok["expires_at"]), _now(), "", "[]",
                     _now_iso(), _now_iso(),
                     normalize_host_os(host_os), host_arch or "", host_shell or ""),
                )
                db.commit()
        except Exception as e:
            revoke_takeover_token(task_id)
            msg = str(e).lower()
            if "unique" in msg or "duplicate" in msg:
                raise TakeoverError(409, "taken_by_other", "该任务已被他人接管")
            raise TakeoverError(500, "start_failed", f"接管会话写入失败：{e}")

        # ④ 取消原任务（内部：槽位释放 / 事件级联 cancelled / 撤销 worker tk_）
        cancel_ok, cancel_err = True, ""
        try:
            rt.cancel_task(task_id)
        except Exception as e:
            cancel_ok, cancel_err = False, str(e)
            log.warning(f"takeover: cancel task {task_id} failed: {e}")

        # ⑤ 事件 → takeover（展示为"进行中"）
        mark_status = ""
        try:
            mark = rt.mark_task_takeover(task_id) or {}
            mark_status = str(mark.get("status") or "")
        except Exception as e:
            log.warning(f"takeover: mark task {task_id} failed: {e}")
            mark_status = "error"

        # ⑥ bundle（cancel_ok 决定 prompt 里的告警文案）
        meta = {"platform": platform, "workspace_created": False, "supports": supports or {},
                "arch": host_arch or "", "shell": host_shell or ""}
        bundle, warnings, persist = build_bundle(
            task=task, session_id=session_id, token=tok["token"],
            token_expires_at=int(tok["expires_at"]), user=user,
            platform_meta=meta, cancel_ok=cancel_ok, cancel_error=cancel_err,
            base_url=base_url, host_os=host_os)
        # ⑥' 竞态探测（Issue #2517 补充发现 #2 / #2678 复审）：必须在 cancel（④）之后执行。
        # 复现 trace 里下游 a2a 是 cancel 之后约 0.5s 才入库的——放在 cancel 之前查会漏掉
        # 整个窗口；这里落在 cancel → bundle 组装之后、落库之前，覆盖 start 全过程的晚到事件。
        # 残余窗口（探测之后才入库）由 finish 侧 downstream_event_exists/already_dispatched
        # 与重连时的同函数复算兜住，不硬拒（对齐平台既有语义）。
        attach_node_advanced(
            node_advanced_detail(repo_key, issue_id, task.get("event_seq")),
            warnings, bundle, persist)

        # ⑦ 回填 bundle 快照（不含 secrets）+ warnings
        try:
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE takeover_sessions SET bundle_json=?, warnings_json=?, updated_at=? WHERE id=?",
                          (json.dumps(persist, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False),
                           _now_iso(), session_id))
                db.commit()
        except Exception as e:
            log.warning(f"takeover: persist bundle failed: {e}")

        sess = get_session(session_id) or {}
        log.info(f"takeover: started {session_id} task={task_id} issue={issue_id} "
                 f"cancel_ok={cancel_ok} mark={mark_status} reused=False")
        return {"reused": False, "session": sess, "bundle": bundle,
                "mark_status": mark_status, "cancel_ok": cancel_ok, "cancel_error": cancel_err}
    finally:
        if lock_held:
            try:
                release_lock(lock_key)
            except Exception as exc:  # noqa: BLE001 - 释放失败只告警：此时事务已提交，不能把成功回滚成 500
                log.warning(f"takeover: release_lock({lock_key}) failed (ignored): {exc}")


def _rebuild_for_session(sess: dict, user: dict) -> dict:
    """重连：重新组装 bundle（fresh env、重签 URL；secrets 只在此刻下发）。"""
    from src.kernel.auth_sdk.auth_token import issue_takeover_token, revoke_takeover_token
    task = get_task(sess.get("task_id") or "")
    if not task:
        raise TakeoverError(404, "task_missing", "原任务已不存在")
    # 旧 token 撤销后重签（同 session_key 会覆盖语义，简单起见直接换一把新的）
    revoke_takeover_token(task["id"])
    tok = issue_takeover_token(task_id=task["id"], repo_key=task.get("repo_key") or "",
                               agent_type=task.get("agent_type") or "",
                               user_id=_user_key(user.get("id")))
    if not tok:
        raise TakeoverError(503, "token_issue_failed", "接管 token 续签失败")
    bundle, warnings, persist = build_bundle(
        task=task, session_id=sess["id"], token=tok["token"],
        token_expires_at=int(tok["expires_at"]), user=user,
        platform_meta={"platform": sess.get("platform") or "", "workspace_created": False})
    # 重连复算（#2678 复审 Spec 可疑 2）：build_bundle 会重新产出一份 warnings 并覆盖
    # warnings_json —— 若不在重连时重算，start 时披露的 node_already_advanced 重连即丢失，
    # 与「必须披露」的语义和 PR 正文「含持久化 warnings_json」不符。
    # 这里按当前库状态复算一次（事件只会越积越多，重算不会漏掉首次 start 的披露）。
    attach_node_advanced(
        node_advanced_detail(task.get("repo_key") or "", sess.get("issue_id") or "",
                             task.get("event_seq")),
        warnings, bundle, persist)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE takeover_sessions SET token_sha256=?, token_expires_at=?, bundle_json=?, "
                      "warnings_json=?, updated_at=? WHERE id=?",
                      (tok["token_sha256"], int(tok["expires_at"]),
                       json.dumps(persist, ensure_ascii=False),
                       json.dumps(warnings, ensure_ascii=False), _now_iso(), sess["id"]))
            db.commit()
    except Exception as e:
        log.warning(f"takeover: persist reconnect failed: {e}")
    return bundle


def attach_client_session(*, session_id: str, user: dict, client_session: str,
                          host: str = "", client_version: str = "") -> dict:
    """记录本地 dsh session id（面板"打开本地会话"用；多端时配合 host 区分机器）。"""
    sess = get_session(session_id)
    if not sess:
        raise TakeoverError(404, "not_found", "接管会话不存在")
    if not _same_user(sess.get("user_id"), user):
        raise TakeoverError(403, "not_owner", "该接管会话不属于当前用户")
    cs = (client_session or "").strip()[:128]
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE takeover_sessions SET client_session=?, host=COALESCE(NULLIF(?, ''), host), "
                      "client_version=COALESCE(NULLIF(?, ''), client_version), updated_at=? WHERE id=?",
                      (cs, host or "", client_version or "", _now_iso(), session_id))
            db.commit()
    except Exception as e:
        raise TakeoverError(500, "attach_failed", f"记录本地会话失败：{e}")
    return {"ok": True, "client_session": cs}


def renew_takeover_session(*, session_id: str, user: dict) -> dict:
    """续期（剩余 <2h 且未到 24h 上限时）。不换 token，只延长 expires_at。"""
    from src.kernel.auth_sdk.auth_token import (TAKEOVER_RENEW_WINDOW, TAKEOVER_TOKEN_MAX_TTL,
                                    TAKEOVER_TOKEN_TTL, renew_takeover_token)
    sess = get_session(session_id)
    if not sess:
        raise TakeoverError(404, "not_found", "接管会话不存在")
    if not _same_user(sess.get("user_id"), user):
        raise TakeoverError(403, "not_owner", "该接管会话不属于当前用户")
    if sess.get("status") != ST_ACTIVE:
        return {"renewed": False, "status": sess.get("status"), "remaining": 0}

    now = _now()
    exp = int(sess.get("token_expires_at") or 0)
    remaining = max(0, exp - now)
    if remaining > TAKEOVER_RENEW_WINDOW:
        return {"renewed": False, "remaining": remaining, "expires_at": exp, "reason": "not_needed"}

    issued = int(sess.get("token_issued_at") or 0) or now
    new_exp = min(now + TAKEOVER_TOKEN_TTL, issued + TAKEOVER_TOKEN_MAX_TTL)
    if new_exp <= now:
        return {"renewed": False, "remaining": remaining, "expires_at": exp, "reason": "max_ttl_reached"}

    renew_takeover_token(sess.get("task_id") or "", new_exp)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE takeover_sessions SET token_expires_at=?, updated_at=? WHERE id=?",
                      (int(new_exp), _now_iso(), session_id))
            db.commit()
    except Exception as e:
        log.warning(f"takeover: renew persist failed: {e}")
    return {"renewed": True, "remaining": new_exp - now, "expires_at": new_exp}


# ----------------------------------------------------------------------------- finish / abort

def _advance_stage(repo_key: str, issue_number: str, current_agent: str) -> tuple[str, bool]:
    """按场景节点链推进 stage。返回 (next_agent, is_done)。"""
    from src.api.session_store import get_issue_scene, set_issue_stage
    scene_id = ""
    try:
        sc = get_issue_scene(repo_key, f"#{issue_number}")
        scene_id = (sc[0] if sc and sc[0] else "") or ""
    except Exception as _e:
        log.debug(f"takeover.py best-effort skipped: {_e}")
    nodes: list[dict] = []
    try:
        from src.api.pipeline_config import get_pipeline_config
        cfg = get_pipeline_config(scene_id or "default", repo_key=repo_key) or {}
        nodes = cfg.get("nodes") or []
    except Exception as e:
        log.warning(f"takeover: advance stage config failed: {e}")
    next_agent, handoff = "", "a2a"
    for n in nodes:
        if (n.get("id") or n.get("tag")) == current_agent:
            next_agent = n.get("next") or ""
            handoff = n.get("handoff") or "a2a"
            break
    if not next_agent:
        return "", False          # 场景里找不到该节点：不动 stage，交给人
    if next_agent == "done":
        try:
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE issue SET status='completed', stage='done', completed_at=?, "
                          "completed_reason='takeover_done' WHERE repo_key=? AND issue_number=?",
                          (_now_iso(), repo_key, native_issue_number(issue_number)))
                db.commit()
        except Exception as e:
            log.warning(f"takeover: mark issue done failed: {e}")
        return "done", True
    try:
        # owner_agent 记录"现在轮到谁"，写 takeover 会让需求中心显示成不存在的 agent
        set_issue_stage(repo_key, f"#{issue_number}", next_agent, next_agent)
    except Exception as e:
        log.warning(f"takeover: set_issue_stage({next_agent}) failed: {e}")
    return next_agent, False


_INTERNAL_BASE_CACHE = ""


def _internal_base_url(runtime_id: int = 0) -> str:
    """内部自调用的基址。

    本机任务优先 127.0.0.1；端口不能信任 config（本地实测 GRAPE_PORT 未设时 config 给 3000，
    但 uvicorn 实际跑在 8000）——所以按候选端口探活 /api/health，取第一个通的并缓存。
    """
    global _INTERNAL_BASE_CACHE
    if _INTERNAL_BASE_CACHE:
        return _INTERNAL_BASE_CACHE
    if int(runtime_id or 0) <= 0:
        import httpx
        cands: list[str] = []
        for v in (os.environ.get("GRAPE_INTERNAL_URL"), os.environ.get("PORT"),
                  os.environ.get("GRAPE_PORT")):
            if v:
                cands.append(v if str(v).startswith("http") else f"http://127.0.0.1:{v}")
        try:
            cands.append(f"http://127.0.0.1:{_local_port()}")
        except Exception as _e:
            log.debug(f"takeover.py best-effort skipped: {_e}")
        cands += ["http://127.0.0.1:8000", "http://127.0.0.1:3000"]
        seen: set[str] = set()
        for base in cands:
            base = base.rstrip("/")
            if base in seen:
                continue
            seen.add(base)
            try:
                # 2.5s：finish 时同进程可能正持着 db_lock（health 若查库会排队），
                # 0.8s 会把「自己在忙」误判成「端口不通」
                r = httpx.get(f"{base}/api/health", timeout=2.5)
                if r.status_code < 500:
                    _INTERNAL_BASE_CACHE = base
                    log.info(f"takeover: internal base = {base}")
                    return base
            except Exception as _e:
                log.debug(f"takeover best-effort skipped: {_e}")
                continue
        # 本机任务：探活全失败也只用本机地址，绝不回退公网/配置域名
        # （实测 config 的 worker_connect_url = http://localhost:3000，本机没人监听）
        # 兜底端口取本部署的既定端口 8000（systemd uvicorn --port 8000）。
        _INTERNAL_BASE_CACHE = "http://127.0.0.1:8000"
        log.warning(f"takeover: health probe all failed, fallback {_INTERNAL_BASE_CACHE}")
        return _INTERNAL_BASE_CACHE
    try:
        from src.kernel.prompt_builder import _resolve_server_url
        base = (_resolve_server_url(int(runtime_id or 0)) or "").rstrip("/")
    except Exception:
        base = ""
    if base:
        _INTERNAL_BASE_CACHE = base
    return base or f"http://127.0.0.1:{_local_port()}"


def _dispatch_next(*, repo_key: str, issue_number: str, next_agent: str, context: str,
                   runtime_id: int, auth_headers: dict | None,
                   base_url: str = "") -> dict:
    """派发下一节点（内部 HTTP 调 /api/a2a，与 resume 同路径；失败可重试）。

    目标地址一律取 ``_internal_base_url()``（本机探活得出的内部基址）。
    调用方传入的 base_url 来自 ``request.base_url``，可被 Host 头伪造，
    用作 SSRF 跳板会把调用方凭据发给攻击者控制的主机，因此这里显式忽略。
    """
    import httpx
    base = _internal_base_url(int(runtime_id or 0)).rstrip("/")
    url = f"{base.rstrip('/')}/api/a2a"
    body = {"agent": next_agent,
            "payload": {"repository": repo_key.split(":", 1)[-1],
                        "issue_number": str(issue_number),
                        "context": (context or "")[:800]}}
    headers = {"content-type": "application/json"}
    for k, v in (auth_headers or {}).items():
        headers.setdefault(k, v)
    last_err = ""
    for attempt in (1, 2):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=15.0)
            if r.status_code < 400:
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                seq = 0
                try:
                    seq = int((data.get("response") or {}).get("seq") or data.get("seq") or 0)
                except Exception:
                    seq = 0
                return {"ok": True, "seq": seq, "status": r.status_code}
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        if attempt == 1:
            time.sleep(1)
    return {"ok": False, "error": last_err}


def request_finish(*, session_id: str, user: dict, summary: str = "", evidence: str = "",
                   files_written: list | None = None, actions: list | None = None,
                   artifacts: list | None = None, next_step: str = "",
                   issue_summary: str = "") -> dict:
    """本地 agent 提交"完成申请"（不推进流水线，派发仍由用户点确认）。

    为什么要这一步：finish 是 user_scope（tko_ 调不动，403），而 agent 才是知道
    这次接管具体做了什么的人。所以把"提交内容"和"授权推进"拆开：
      agent（tko_）→ request-finish 写 summary/evidence/files；
      用户（user_scope）→ finish 才做事件终态 + stage 推进 + 派发。
    """
    sess = get_session(session_id)
    if not sess:
        raise TakeoverError(404, "not_found", "接管会话不存在")
    if str(user.get("auth_type") or "") == "takeover":
        from src.kernel.auth_sdk.auth_token import takeover_session_key as _tko_key
        if str(user.get("session_key") or "") != _tko_key(sess.get("task_id") or ""):
            raise TakeoverError(403, "not_owner", "该接管 token 不属于此会话")
    elif not _same_user(sess.get("user_id"), user):
        raise TakeoverError(403, "not_owner", "该接管会话不属于当前用户")
    if sess.get("status") != ST_ACTIVE:
        raise TakeoverError(409, "not_active", f"接管会话已结束（{sess.get('status')}）")

    import json as _json
    files = [str(f)[:256] for f in (files_written or [])][:50]
    # 结构化结论（与 worker 的 POST /api/tasks/complete 同构）：
    #   conclusion / actions / artifacts / next_step / issue_summary
    # agent 没显式给 actions/artifacts 时，从 evidence 按行拆出来兜底，保证详情页不是一片空白。
    _acts = [str(a)[:300] for a in (actions or [])][:30]
    if not _acts and evidence:
        _acts = [ln.strip(" -•\t")[:300] for ln in str(evidence).splitlines() if ln.strip()][:30]
    _arts = [str(a)[:300] for a in (artifacts or [])][:30]
    if not _arts:
        _arts = [f[:200] for f in files][:30]
    result_json = _json.dumps({
        "conclusion": (summary or "")[:4000],
        "actions": _acts,
        "artifacts": _arts,
        "next_step": (next_step or "")[:200],
        "issue_summary": (issue_summary or summary or "")[:2000],
    }, ensure_ascii=False)
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE takeover_sessions SET summary=?, evidence=?, finish_files=?, "
                      "result_json=?, finish_requested_at=?, updated_at=? WHERE id=?",
                      ((summary or "")[:8000], (evidence or "")[:4000],
                       _json.dumps(files, ensure_ascii=False), result_json,
                       _now(), _now_iso(), session_id))
            db.commit()
    except Exception as e:
        raise TakeoverError(500, "request_finish_failed", f"记录完成申请失败：{e}")
    log.info(f"takeover: request-finish {session_id} summary={len(summary or '')}chars files={len(files)}")
    return {"ok": True, "session": get_session(session_id)}


def finish_takeover(*, session_id: str, user: dict, outcome: str = "completed",
                    summary: str = "", evidence: str = "", files_written: list | None = None,
                    handoff: str = "a2a", next_agent: str = "",
                    actions: list | None = None, artifacts: list | None = None,
                    issue_summary: str = "",
                    auth_headers: dict | None = None, base_url: str = "") -> dict:
    from fastapi import HTTPException
    from src.kernel.auth_sdk.auth_token import revoke_takeover_token
    from src.api.event_store import update_event_complete

    sess = get_session(session_id)
    if not sess:
        raise TakeoverError(404, "not_found", "接管会话不存在")
    if not _same_user(sess.get("user_id"), user):
        raise TakeoverError(403, "not_owner", "该接管会话不属于当前用户")

    # Issue #3201：本次接管期间被守门忽略的自动派发条数（面板可观测）。
    _skipped_count = skipped_dispatch_count(sess)
    _skipped_message = skipped_dispatch_message(_skipped_count)

    # agent 已经用 request-finish 提交过内容 → 用户点确认时直接沿用（不被空 body 覆盖）
    if not summary and sess.get("summary"):
        summary = str(sess.get("summary") or "")
    if not evidence and sess.get("evidence"):
        evidence = str(sess.get("evidence") or "")
    import json as _json
    if not files_written and sess.get("finish_files"):
        try:
            files_written = _json.loads(str(sess.get("finish_files") or "[]"))
        except Exception:
            files_written = []
    # 结构化结论：沿用 agent 在 request-finish 里提交的 actions/artifacts（缺失才用本地采集）
    _stored: dict = {}
    if sess.get("result_json"):
        try:
            _stored = _json.loads(str(sess.get("result_json") or "{}")) or {}
        except Exception:
            _stored = {}
    if not actions:
        actions = _stored.get("actions") or []
    if not artifacts:
        artifacts = _stored.get("artifacts") or []
    if not issue_summary:
        issue_summary = str(_stored.get("issue_summary") or "")
    if not next_agent:
        next_agent = str(_stored.get("next_step") or "")

    # 幂等：已结束 → 仅在需要补派发时重试
    if sess.get("status") != ST_ACTIVE:
        retry = (sess.get("status") == ST_FINISHED and handoff == "a2a" and not sess.get("handoff_seq"))
        if not retry:
            _sess_view = dict(sess)
            _sess_view["skipped_dispatches"] = _skipped_count
            return {"ok": True, "changed": False, "status": sess.get("status"),
                    "session": _sess_view,
                    "skipped_dispatches": _skipped_count,
                    "skipped_message": _skipped_message}
        _retry_handoff(sess, user, next_agent=next_agent, summary=summary,
                       auth_headers=auth_headers, base_url=base_url)
        _sess_view = get_session(session_id) or sess
        _sess_view = dict(_sess_view)
        _sess_view["skipped_dispatches"] = _skipped_count
        return {"ok": True, "changed": False, "status": sess.get("status"),
                "session": _sess_view,
                "skipped_dispatches": _skipped_count,
                "skipped_message": _skipped_message}

    outcome = (outcome or "completed").lower()
    if outcome not in ("completed", "failed"):
        raise TakeoverError(400, "bad_request", "outcome 只能是 completed / failed")

    repo_key = sess.get("repo_key") or ""
    issue_number = _issue_num(sess.get("issue_id") or "")
    event_seq = int(sess.get("event_seq") or 0)
    agent_type = sess.get("agent_type") or ""

    # 结构化结论：与 worker 的 POST /api/tasks/complete 同构
    # （事件详情页「Agent 处理记录与答复结论」读 events.structured_output，
    #   routes/events.py:153 → data.archive.output，前端 _evBuildStructuredHtml 渲染五行）
    _structured = {
        "conclusion": (summary or "")[:4000],
        "actions": [str(a)[:300] for a in (actions or [])][:30],
        "artifacts": [str(a)[:300] for a in (artifacts or [])][:30],
        "next_step": (next_agent or sess.get("next_agent") or "")[:200],
        "issue_summary": (issue_summary or summary or "")[:2000],
    }

    # ① 事件终态
    event_status = ""
    if event_seq:
        try:
            update_event_complete(event_seq, outcome, result=(summary or "")[:4000],
                                  structured_output=_structured,
                                  allow_from_takeover=True)
            event_status = outcome
        except Exception as e:
            raise TakeoverError(500, "event_finalize_failed", f"事件终态写入失败：{e}")

    # ② stage 推进 / Issue 完成
    next_agent_resolved, is_done = "", False
    if outcome == "completed":
        next_agent_resolved, is_done = _advance_stage(repo_key, issue_number, agent_type)
    # next_step 只有推进后才知道 → 补一次 events.structured_output（update_event_complete
    # 对终态事件是幂等跳过，不能二次覆盖，所以这里直接改结构化列）
    if event_seq and next_agent_resolved and _structured.get("next_step") != next_agent_resolved:
        try:
            _structured["next_step"] = next_agent_resolved[:200]
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE events SET structured_output=? WHERE seq=?",
                          (_json.dumps(_structured, ensure_ascii=False), event_seq))
                db.commit()
        except Exception as e:
            log.warning(f"takeover: patch structured_output next_step failed: {e}")

    # ③ 关会话 + 吊销 token（必须先做，否则会被"接管中禁止派发"守卫挡住）
    try:
        with db_lock:
            c = db.cursor()
            _rj = _structured
            c.execute("UPDATE takeover_sessions SET status=?, active_key=NULL, summary=?, evidence=?, "
                      "next_agent=?, result_json=?, finished_at=?, updated_at=? WHERE id=?",
                      (ST_FINISHED, (summary or "")[:8000], (evidence or "")[:4000],
                       (next_agent_resolved or next_agent or "")[:32],
                       _json.dumps(_rj, ensure_ascii=False) if _rj else "",
                       _now_iso(), _now_iso(), session_id))
            db.commit()
    except Exception as e:
        raise TakeoverError(500, "finish_failed", f"会话收尾失败：{e}")
    revoke_takeover_token(sess.get("task_id") or "")

    # ④ 可选派发下一节点
    handoff_result: dict = {"ok": False, "skipped": True}
    if outcome == "completed" and not is_done and handoff == "a2a":
        target = (next_agent or next_agent_resolved or "").strip()
        existing = downstream_event_exists(repo_key, issue_number, target, int(sess.get("event_seq") or 0)) if target else None
        if existing:
            log.info(f"takeover: skip handoff to {target} — already dispatched seq={existing['seq']} "
                     f"status={existing['status']}")
            handoff_result = {"ok": True, "skipped": True, "reason": "already_dispatched",
                              "seq": existing["seq"]}
        if not handoff_result.get("seq"):
            ctx = (f"由用户 {user.get('username') or ''} 在本机接管完成节点 {agent_type}；"
                   f"summary: {summary or ''}；evidence: {evidence or ''}；"
                   f"files: {', '.join(files_written or [])}")
            handoff_result = _dispatch_next(repo_key=repo_key, issue_number=issue_number,
                                            next_agent=target, context=ctx,
                                            runtime_id=int(sess.get("runtime_id") or 0),
                                            auth_headers=auth_headers, base_url=base_url)
            if not handoff_result.get("ok"):
                log.warning(f"takeover: handoff to {target} failed: {handoff_result.get('error')}")
        # 派发成功 或 "下游已自己派发过" 都把真实 seq 记下来（幂等键 & 审计）
        if handoff_result.get("ok") and int(handoff_result.get("seq") or 0):
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE takeover_sessions SET handoff_seq=? WHERE id=?",
                              (int(handoff_result.get("seq") or 0), session_id))
                    db.commit()
            except Exception as _e:
                log.debug(f"takeover.py best-effort skipped: {_e}")

    log.info(f"takeover: finished {session_id} event={outcome} next={next_agent_resolved or '-'} "
             f"handoff_ok={handoff_result.get('ok')} done={is_done} skipped={_skipped_count}")
    _sess_view = get_session(session_id) or sess
    _sess_view = dict(_sess_view)
    _sess_view["skipped_dispatches"] = _skipped_count
    return {"ok": True, "changed": True, "status": ST_FINISHED,
            "event_status": event_status, "issue_stage": ("done" if is_done else next_agent_resolved),
            "next_agent": next_agent_resolved or next_agent,
            "handoff": handoff_result, "session": _sess_view,
            "skipped_dispatches": _skipped_count,
            "skipped_message": _skipped_message}


def _retry_handoff(sess: dict, user: dict, *, next_agent: str = "", summary: str = "",
                   auth_headers: dict | None = None, base_url: str = "") -> None:
    """finish 之后补派发（幂等键 = session_id + next_agent）。"""
    target = (sess.get("next_agent") if sess.get("next_agent") else "") or next_agent
    if not target:
        # 从场景重新推一次
        target, _ = _advance_stage(sess.get("repo_key") or "", _issue_num(sess.get("issue_id") or ""),
                                   sess.get("agent_type") or "")
    if not target or target == "done":
        return
    ctx = f"由用户 {user.get('username') or ''} 接管完成节点 {sess.get('agent_type')}；summary: {summary}"
    res = _dispatch_next(repo_key=sess.get("repo_key") or "", issue_number=_issue_num(sess.get("issue_id") or ""),
                         next_agent=target, context=ctx,
                         runtime_id=int(sess.get("runtime_id") or 0), auth_headers=auth_headers,
                         base_url=base_url)
    if res.get("ok"):
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE takeover_sessions SET handoff_seq=? WHERE id=?", (int(res.get("seq") or 0), sess["id"]))
            db.commit()


def abort_takeover(*, session_id: str, user: dict, reason: str = "", redispatch: bool = True) -> dict:
    """放弃接管：会话 aborted + 吊销 token；节点默认**交还流水线重新派发**。

    为什么默认重派（2026-11-10 用户决策）：放弃 = "我不接了，还是让平台 agent 干"。
    不重派的话事件停在 cancelled、没有任务在跑，整条流水线就死在这个节点上。
    红线仍然守住：重派是**平台自己的重派语义**（redispatch 新建任务、事件回到 running），
    不是"把接管过的内容自动重跑一遍"——本地改动留在同一 cwd，重派的 agent 会看到并自行处理。
    需要"只停不重派"的调用方显式传 redispatch=False（例如接管期间已经手工推了分支）。
    """
    from fastapi import HTTPException
    from src.kernel.auth_sdk.auth_token import revoke_takeover_token
    from src.api.event_store import update_event_complete

    sess = get_session(session_id)
    if not sess:
        raise TakeoverError(404, "not_found", "接管会话不存在")
    if not _same_user(sess.get("user_id"), user):
        raise TakeoverError(403, "not_owner", "该接管会话不属于当前用户")
    if sess.get("status") != ST_ACTIVE:
        return {"ok": True, "changed": False, "status": sess.get("status"), "session": sess}

    event_seq = int(sess.get("event_seq") or 0)
    if event_seq:
        try:
            update_event_complete(event_seq, "cancelled", result=(reason or "")[:2000],
                                  allow_from_takeover=True)
        except Exception as e:
            log.warning(f"takeover: abort event finalize failed: {e}")

    with db_lock:
        c = db.cursor()
        c.execute("UPDATE takeover_sessions SET status=?, active_key=NULL, summary=?, finished_at=?, updated_at=? "
                  "WHERE id=?",
                  (ST_ABORTED, (reason or "")[:8000], _now_iso(), _now_iso(), session_id))
        db.commit()
    revoke_takeover_token(sess.get("task_id") or "")

    # 交还流水线：用平台既有的 redispatch 语义重新派发该节点（新建任务、事件回 running）
    redispatch_info: dict = {}
    if redispatch and event_seq:
        try:
            from src.domains.runtime.routes.events import redispatch_event_internal
            res = redispatch_event_internal(event_seq, user) or {}
            redispatch_info = {"ok": True, **{k: res.get(k) for k in ("status", "task_id", "event_seq")}}
            log.info(f"takeover: aborted {session_id} → redispatch seq={event_seq} "
                     f"task={redispatch_info.get('task_id')} status={redispatch_info.get('status')}")
        except HTTPException as e:  # noqa: F821  (imported below)
            redispatch_info = {"ok": False, "error": str(getattr(e, "detail", e))[:500]}
            log.warning(f"takeover: abort redispatch failed for seq={event_seq}: {e}")
        except Exception as e:
            redispatch_info = {"ok": False, "error": str(e)[:500]}
            log.warning(f"takeover: abort redispatch failed for seq={event_seq}: {e}")

    log.info(f"takeover: aborted {session_id} issue={sess.get('issue_id')} reason={reason[:120]} "
             f"redispatch={redispatch}")
    out = {"ok": True, "changed": True, "status": ST_ABORTED, "session": get_session(session_id)}
    # Issue #2517 / #2678：重派结果顶层披露，且四态可区分——只看 redispatched=false 无法区分
    # 「我没要求重派」「没有事件可重派」「重派失败、链已断」。端侧据此决定是提示「无需处理」
    # 还是显著告警「链未恢复，请手动重派」。
    if not redispatch:
        out["redispatch_status"] = "not_requested"
    elif not event_seq:
        out["redispatch_status"] = "no_event"
    elif redispatch_info.get("ok"):
        out["redispatch_status"] = "redispatched"
    else:
        out["redispatch_status"] = "failed"
        out["redispatch_error"] = str(redispatch_info.get("error") or "")[:500]
    # redispatched 保留给既有端侧（grape-kit 等）消费，语义收敛为「本次回执确实恢复了链」
    out["redispatched"] = out["redispatch_status"] == "redispatched"
    if redispatch_info:
        out["redispatch"] = redispatch_info
    return out


# ----------------------------------------------------------------------------- progress

def append_takeover_messages(*, task_id: str, entries: list[dict], actor: str) -> dict:
    """把本地会话的进展写进 task_messages（actor=username）。

    - 与 worker 共用同一张日志表（设计文档 §5.3）：UI/SSE/增量拉取零改造即复用；
    - seq 在 db_lock 内用 MAX+1 分配，UNIQUE(task_id,seq) 冲突时整体重试一次；
    - 截断规则与 worker 一致（单条 4KB / output 8KB / 单批 ≤50）。
    """
    from src.domains.runtime.engine.service import peek_next_seq
    if not task_id:
        raise TakeoverError(400, "bad_request", "缺少 task_id")
    items = [m for m in (entries or []) if isinstance(m, dict)][:50]
    if not items:
        return {"count": 0, "seq_start": 0, "seq_end": 0}

    def _ins() -> dict:
        with db_lock:
            c = db.cursor()
            base = peek_next_seq(task_id)
            for i, m in enumerate(items):
                c.execute(
                    "INSERT INTO task_messages (task_id, seq, type, tool, content, input_json, output, actor) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (task_id, base + i, str(m.get("type") or "text")[:32], str(m.get("tool") or "")[:64],
                     str(m.get("content") or "")[:4096], "", str(m.get("output") or "")[:8192],
                     (actor or "user")[:128]),
                )
            db.commit()
            return {"count": len(items), "seq_start": base, "seq_end": base + len(items) - 1}

    try:
        return _ins()
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg:
            log.warning(f"takeover: seq conflict on task {task_id}, retrying once")
            return _ins()
        raise TakeoverError(500, "progress_failed", f"progress 写入失败：{e}")
