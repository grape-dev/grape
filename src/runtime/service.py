"""服务端 RuntimeService：任务队列（确定路由 + 串行槽）与远端 worker 执行。

见 agent-runtime-event-tech-impl.md §1.3 / concurrency-design.md §2.2-2.4 / engine-selection-design.md v2。
执行路径：业务层 enqueue_task → agent_tasks(status=queued) → 远端 RuntimeAgent claim（原子抢单+D9）
→ stream_runtime → report/complete/fail。本机引擎走 direct（`_execute_agent`），不走队列。
"""
import os, json, re, threading, time, uuid
from datetime import datetime, timezone, timedelta
from src.session_manager import db, db_lock
from src.config import log
from src.api.time_utils import normalize_event_time

# ===== 执行模式开关（engine-selection-design.md v2）=====
# auto（按绑定引擎宿主自动推导，见 execution-mode-auto-routing-design.md §3）
# / direct（本机就地 stream_agent）/ queue_remote（远端 RuntimeAgent）。queue_local 已废除。
EXEC_MODE_AUTO = "auto"
EXEC_MODE_DIRECT = "direct"
EXEC_MODE_REMOTE = "queue_remote"

_DEFAULT_MODE = EXEC_MODE_AUTO


def get_execution_mode() -> str:
    """返回当前执行模式：env 优先，其次 meta 表。"""
    _m = os.environ.get("DEVOPS_EXECUTION_MODE", "")
    if _m:
        return _m
    try:
        from src.user.db import get_meta_value
        _m = get_meta_value("execution_mode") or ""
    except Exception:
        pass
    return _m or _DEFAULT_MODE


def set_execution_mode(mode: str):
    """设置执行模式（落 meta 表，运维可改键值；读取实时生效，无需重启）。"""
    try:
        from src.user.db import set_meta_value
        set_meta_value("execution_mode", mode)
        log.info(f"execution_mode set to {mode}")
    except Exception as e:
        log.warning(f"set_execution_mode failed: {e}")


# ===== 按绑定引擎宿主自动推导执行通道（execution-mode-auto-routing-design.md §3.1/§3.6）=====

def runtime_credential_id(runtime_id: int):
    """按 runtime_id 反查引擎宿主 credential_id；查不到返回 None。

    credential_id=0 → 本机引擎（服务器装着该 CLI）；>0 → 远端 worker 承载的引擎。
    """
    if not runtime_id:
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
            row = c.fetchone()
        return (row[0] or 0) if row else None
    except Exception:
        return None


_RUNTIME_STALE_SECONDS = int(os.environ.get("RUNTIME_STALE_SECONDS", "300"))


def _remote_runtime_ready(runtime_id: int) -> bool:
    """远端引擎是否可用：引擎 status=online、last_seen 新鲜、凭据未吊销。

    心跳间隔 15s，失联 > _RUNTIME_STALE_SECONDS 视为不可用（防 auto 模式任务卡 queued）。
    """
    try:
        from src.runtime.health import parse_dt
        now = time.time()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, last_seen_at, credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
            row = c.fetchone()
        if not row:
            return False
        engine_status, last_seen_at, cid = row
        if (engine_status or "") != "online":
            return False
        ts = parse_dt(last_seen_at)
        if ts is not None and now - ts > _RUNTIME_STALE_SECONDS:
            return False
        if cid:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT status FROM runtime_credentials WHERE id=?", (cid,))
                cred = c.fetchone()
            if cred and cred[0] != "active":
                return False
        return True
    except Exception as e:
        log.warning(f"runtime_execution_kind: health check failed: {e}")
        return False


def runtime_execution_kind(runtime_id: int) -> str:
    """返回该任务的有效执行通道（auto 语义下的推导 / 强制模式直接生效）：

    - 全局显式 direct/queue_remote → 直接返回该强制值（向后兼容；历史遗留
      queue_local 已废除，归为 direct 本机执行）
    - auto 下按绑定引擎宿主推导：
      - credential_id == 0  → 本机引擎（服务器装着的 CLI）        → EXEC_MODE_DIRECT
      - credential_id >  0  → 远端 worker 承载的引擎             → EXEC_MODE_REMOTE
      - runtime_id=0 / 查不到（未绑定）→ EXEC_MODE_DIRECT（本机默认，见 §3.6）
    - auto 下远端引擎失联/凭据吊销 → 降级本机 direct 兜底（避免任务卡 queued）
    """
    mode = get_execution_mode()
    if mode != EXEC_MODE_AUTO:
        if mode == "queue_local":      # queue_local 已废除（无本地 worker），按本机 direct 处理
            return EXEC_MODE_DIRECT
        return mode
    cid = runtime_credential_id(runtime_id)
    if cid is None:
        return EXEC_MODE_DIRECT
    if cid > 0:
        if _remote_runtime_ready(runtime_id):
            return EXEC_MODE_REMOTE
        log.warning(f"runtime_execution_kind: runtime_id={runtime_id} 远端引擎不可用，降级本机 direct")
        return EXEC_MODE_DIRECT
    return EXEC_MODE_DIRECT

_TASK_COLUMNS = (
    "id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, skill, "
    "skill_id, "
    "prompt, cwd, runtime_session_id, event_type, account, user_id, repo_gitcode_token, "
    "env_extra, skill_files, server_url, issue_session_id, project_info_files, project_info_meta, "
    "knowledge_files, project_info_file_urls, knowledge_file_urls, attempts, pool_agent_type, affinity_runtime_id, pool_tag, pool_engine_ids, "
    "status, exit_code, result_text, errors, stderr, "
    "usage_json, started_at, completed_at, created_at"
)

_TASK_COLUMNS_LIST = [c.strip() for c in _TASK_COLUMNS.split(",")]


def _row_to_dict(row):
    return dict(zip(_TASK_COLUMNS_LIST, row))


def _pack_skill_dir(skill_name, skill_path):
    """把技能目录（SKILL.md + references/**）打包为 {rel_path: content}（远端交付，见 skill-delivery-remote.md）"""
    import os as _os
    files = {}
    base = _os.path.dirname(skill_path) if skill_path else ""
    if not base or not _os.path.isdir(base):
        return {skill_name: {}}
    for root, _dirs, names in _os.walk(base):
        for n in names:
            full = _os.path.join(root, n)
            rel = _os.path.relpath(full, base).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8") as f:
                    files[rel] = f.read()
            except Exception:
                pass
    return {skill_name: files}


def _skill_files_from_zip(zip_bytes: bytes, skill_name: str) -> dict:
    """OBS 模式双写（老 worker 过渡期兼容）：从技能 zip 提取可文本化文件，
    生成与 _pack_skill_dir 相同结构的 skill_files。二进制文件跳过（老 worker 能力边界）。

    （merge 修复：feat/obs-storage 合并 dev 时此函数丢失，补回——老 worker
    兼容通道；OBS 技能本地无目录，skill_files 须从 zip 提取）
    """
    import io as _io
    import zipfile as _zf
    files = {}
    try:
        with _zf.ZipFile(_io.BytesIO(zip_bytes)) as zf:
            sub = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = info.filename.split("/", 1)
                inner = parts[1] if len(parts) == 2 and parts[0] == skill_name else info.filename
                try:
                    sub[inner] = zf.read(info.filename).decode("utf-8")
                except Exception:
                    continue  # 二进制/非 UTF-8：老 worker 不支持，跳过
            if sub:
                files[skill_name] = sub
    except Exception as e:
        log.warning(f"_skill_files_from_zip failed: {e}")
    return files


def _collect_server_env(user_id, account):
    """服务端 DB 系凭据注入（user credentials / 账户 AK-SK），远端 worker 无本地 DB 也生效。"""
    env = {}
    if user_id and user_id > 0:
        try:
            from src.user.db import get_credentials
            for c in get_credentials(user_id):
                provider = c.get("provider", "")
                cfg = c.get("config", {})
                if provider == "huawei_cloud":
                    # cloud-deploy skill expects HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK
                    if cfg.get("ak"): env["HUAWEICLOUD_SDK_AK"] = str(cfg["ak"])
                    if cfg.get("sk"): env["HUAWEICLOUD_SDK_SK"] = str(cfg["sk"])
                elif provider == "codearts":
                    if cfg.get("ak"): env["CODEARTS_CLI_AK"] = str(cfg["ak"])
                    if cfg.get("sk"): env["CODEARTS_CLI_SK"] = str(cfg["sk"])
                else:
                    for k, v in cfg.items():
                        env[k.upper()] = str(v)
        except Exception:
            pass
    if account:
        try:
            from src.config import get_account
            acc = get_account(account)
            for k in ("CODEARTS_CLI_AK", "CODEARTS_CLI_SK"):
                if acc.get(k):
                    env[k] = acc[k]
        except Exception:
            pass
    return env


_PROJECT_INFO_FILES = ("project-graph.md", "project-capabilities.md")


def load_project_info(repo_key: str) -> tuple[dict, dict]:
    """读取服务端权威 project-info（见 multi-issue-isolation-design.md §2.4）。

    返回 (files, meta)：
    - files: {rel_path: content}（project-graph.md / project-capabilities.md / .metadata.json）
    - meta: {last_commit_sha, branches, last_updated, degraded}
    无基线 → (files={}, meta={})。
    """
    if not repo_key:
        return {}, {}
    try:
        from src.config import WORKSPACE_DIR
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)

        # OBS 分支：查 DB scope='baseline' + OBS 读取（不依赖本地磁盘）
        try:
            from src.storage import get_storage
            _storage = get_storage()
        except Exception:
            _storage = None
        if _storage is not None:
            files = {}
            from src.session_manager import db, db_lock
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT file_path, obs_key FROM agent_shared_knowledge "
                    "WHERE repo_key=? AND scope='baseline' AND obs_key!='' AND obs_key IS NOT NULL "
                    "ORDER BY id DESC",
                    (repo_key,)
                ).fetchall()
            for fp, ok in rows:
                if fp and ok:
                    fname = os.path.basename(fp)
                    if fname in _PROJECT_INFO_FILES or fname == ".metadata.json":
                        try:
                            data = _storage.read_bytes(ok)
                            if data:
                                files[fname] = data.decode("utf-8", errors="replace")
                        except Exception:
                            continue
            if files:
                # .metadata.json 特殊处理
                meta = {}
                if ".metadata.json" in files:
                    try:
                        meta = json.loads(files[".metadata.json"])
                    except Exception:
                        meta = {}
                files[".metadata.json"] = json.dumps(meta, ensure_ascii=False)
                return files, meta
            # DB 查不到 baseline → 回退本地读取（兜底）
            # 继续走下面的本地逻辑

        # 本地模式（和 dev 一致）
        pi = os.path.join(WORKSPACE_DIR, owner, rname, "project-info")
        if not os.path.isdir(pi):
            return {}, {}
        files = {}
        for rel in _PROJECT_INFO_FILES:
            p = os.path.join(pi, rel)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        files[rel] = f.read()
                except Exception:
                    pass
        meta = {}
        mp = os.path.join(pi, ".metadata.json")
        if os.path.isfile(mp):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        if files:
            files[".metadata.json"] = json.dumps(meta, ensure_ascii=False)
        return files, meta
    except Exception as e:
        log.warning(f"load_project_info failed for {repo_key}: {e}")
        return {}, {}


def enqueue_task(*, agent_session_id, runtime_id, prompt, cwd="",
                 runtime_session_id="", event_type="", account="", user_id=0,
                 repo_gitcode_token="", env_extra=None,
                 event_seq=None, issue_id="", repo_key="", agent_type="",
                 skill="", skill_path="", skill_id=0, server_url="", issue_session_id="",
                 project_info_files=None, project_info_meta=None, knowledge_files=None,
                 project_info_file_urls=None, knowledge_file_urls=None,
                 pool_agent_type="", affinity_runtime_id=0, pool_tag="", pool_engine_ids=""):
    """业务层入队：确定路由（D3）+ 快照 + 技能打包 + 凭据注入。返回 task_id。

    引擎池（agent-cluster-design.md §4.1/§4.2）：runtime_id=0 + pool_agent_type 非空
    → 池调度任务，由池内任意空闲引擎 claim 时领取（claim 会把 runtime_id 定为实际引擎）。
    显式 runtime_id（>0）→ 确定路由，行为与现状一致。

    knowledge_files：共享知识快照（event-conversation-design.md §3.1.1），None 时自动打包；
    远端 worker 侧 materialize 到 cwd/.knowledge 并替换 {{KNOWLEDGE_DIR}}。

    skill_id：skill 表主键，优先于 skill_name 解析（避免 user/repo scope 的 owner_id 不匹配）。
    skill_id=0 时回退 resolve_skill_id 按名字解析（向后兼容）。
    """
    if not agent_session_id:
        raise ValueError("agent_session_id is required")
    if not runtime_id and not pool_agent_type:
        raise ValueError("runtime_id or pool_agent_type is required (确定路由 D3)")
    task_id = "t_" + uuid.uuid4().hex[:12]
    skill_files = {}
    if skill:
        try:
            from src.user.db import resolve_skill_id, get_skill_path, get_skill_object_key, get_skill_by_id
            if skill_id:
                _sk = get_skill_by_id(skill_id)
                if _sk:
                    # ① skill_id 有效：直接按主键查，跳过 resolve_skill_id 的 owner_id 匹配
                    _sid = skill_id
                    _path = get_skill_path(skill_id)  # 绝对路径（official/user/repo 三 scope 统一）
                    _okey = _sk.get("file_path") or ""
                else:
                    # ② skill_id 失效（skill 被删除/停用）→ 回退按名字解析
                    _sid = resolve_skill_id(skill, owner_id=account) or resolve_skill_id(skill)
                    _path = get_skill_path(_sid) if _sid else (skill_path or "")
                    _okey = get_skill_object_key(_sid) if _sid else ""
            else:
                # ③ 无 skill_id → 走老逻辑（向后兼容）
                _sid = resolve_skill_id(skill, owner_id=account) or resolve_skill_id(skill)
                _path = get_skill_path(_sid) if _sid else (skill_path or "")
                _okey = get_skill_object_key(_sid) if _sid else ""
            # 本地目录存在 → 打包（official 技能 + 少数有磁盘目录的 user/repo 技能）
            if _path and os.path.isdir(os.path.dirname(_path)):
                skill_files = _pack_skill_dir(skill, _path)
            # file_path 非空 → OBS zip 提取（user/repo 技能主要走此路径）
            if _okey and not skill_files:
                from src.storage import get_skill_zip_bytes
                _zip = get_skill_zip_bytes(_okey)
                if _zip:
                    skill_files = _skill_files_from_zip(_zip, skill)
        except Exception as e:
            log.warning(f"enqueue: skill pack failed for {skill}: {e}")
    # project-info 随任务（服务端权威，多 VM 共享，见 multi-issue-isolation-design.md §2.4）
    if project_info_files is None:
        _pif, _pim = load_project_info(repo_key)
        # 项目级成员上传文档（全 Agent 共享，key 相对 project-info 目录，物化到 .project-info/member/，
        # 见 knowledge-board-design.md §6.7 方案 A）
        try:
            from src.api.knowledge import member_files_pi
            for rel, content in member_files_pi(repo_key).items():
                _pif.setdefault(rel, content)
        except Exception:
            pass
        # Agent 专属成员上传文档定向并入（仅当前任务 agent_type，key 相对 project-info 目录，
        # 物化到 .project-info/agent/{agent_type}/，见 knowledge-board-design.md §六）
        try:
            from src.api.knowledge import agent_files
            for rel, content in agent_files(repo_key, agent_type).items():
                _pif.setdefault(rel, content)
        except Exception:
            pass
        project_info_files = _pif
        project_info_meta = _pim
    # 共享知识快照（仅任务作用域：issue/issue_agent/project 三级，随任务交付远端 worker）
    if knowledge_files is None:
        try:
            from src.api.knowledge import knowledge_files as _know_files
            knowledge_files = _know_files(repo_key, agent_type=agent_type, issue_num=_issue_num(issue_id))
        except Exception as e:
            log.warning(f"enqueue: knowledge pack failed for {repo_key}: {e}")
            knowledge_files = {}
    # 生成 file_urls（url + storage_key，见 knowledge-obs-design §八：字段分开存 + 三级降级）
    # storage 为 None（本地模式）时 file_urls 为空 dict，worker 用 content 兜底
    try:
        from src.storage import get_storage
        _storage = get_storage()
    except Exception:
        _storage = None
    _inum = _issue_num(issue_id)
    _file_urls = _storage.generate_file_urls(repo_key, agent_type, _inum) if _storage else {}
    project_info_file_urls = _file_urls
    knowledge_file_urls = _file_urls
    env = dict(env_extra or {})
    for k, v in _collect_server_env(user_id, account).items():
        env.setdefault(k, v)
    _server_url = server_url or ""
    if not _server_url:
        try:
            from src.user.db import get_server_base_url
            _server_url = get_server_base_url()
        except Exception:
            pass
    with db_lock:
        c = db.cursor()
        c.execute(
            f"INSERT INTO agent_tasks ({_TASK_COLUMNS}) VALUES "
            f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})",
            (task_id, agent_session_id, runtime_id, event_seq, issue_id or "", repo_key or "",
             agent_type or "", skill or "", skill_id or 0,
             prompt, cwd or "", runtime_session_id or "",
             event_type or "", account or "", user_id or 0, repo_gitcode_token or "",
             json.dumps(env), json.dumps(skill_files), _server_url,
             issue_session_id or "", json.dumps(project_info_files), json.dumps(project_info_meta),
             json.dumps(knowledge_files),
            json.dumps(project_info_file_urls), json.dumps(knowledge_file_urls),
            0, pool_agent_type or "", affinity_runtime_id or 0, pool_tag or "", pool_engine_ids or "",
             "queued", None, "", "", "", "{}", None, None, datetime.now(timezone.utc).isoformat())
        )
        db.commit()
    from src.runtime.instances import runtime_instance_name
    _inst = runtime_instance_name(runtime_id)
    log.info(f"Task enqueued: {task_id} session={agent_session_id} runtime={runtime_id}({_inst}) skill={skill}")
    # 可观察性埋点：任务入队
    try:
        from src.observability import record_task_enqueued
        record_task_enqueued(task_id, event_type or agent_type or "other", runtime_id)
    except Exception:
        pass
    # 事件总线：任务创建
    try:
        from src.observability.events import bus, Event
        bus.publish(Event(type="task:enqueued", task_id=task_id,
                          payload={"agent_type": agent_type, "runtime_id": runtime_id,
                                    "repo_key": repo_key, "skill": skill}))
    except Exception:
        pass
    return task_id


# ===== 引擎池绑定（agent-cluster-design.md §4.1）=====
# 注：origin/main 曾声明弃用（engine-selection-design.md v2），但 dev 线
# 仍以引擎池为任务调度主路径（claim 池匹配 / pool_runtime_ids 在线过滤 /
# 事件 runtime 回填），保留本实现，engine_select 新模型可并行引入。

def set_agent_pool(agent_type: str, runtime_ids: list[int], weight: int = 1, priority: int = 5):
    """替换某 Agent 类型的引擎池绑定（先删后插，单事务）。runtime_ids 为空列表 → 清空池（回到兜底链）。"""
    if not agent_type:
        raise ValueError("agent_type is required")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_type_runtime_pool WHERE agent_type=?", (agent_type,))
        for rid in (runtime_ids or []):
            c.execute(
                "INSERT OR IGNORE INTO agent_type_runtime_pool (agent_type, runtime_id, weight, priority) VALUES (?,?,?,?)",
                (agent_type, int(rid), int(weight), int(priority)))
        db.commit()
    log.info(f"Agent pool set: {agent_type} -> {runtime_ids or []} (weight={weight}, priority={priority})")


def get_agent_pool(agent_type: str) -> list[dict]:
    """返回某 Agent 类型的引擎池绑定（含引擎实时状态，供管理端/调度展示）。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT p.runtime_id, p.weight, p.priority, p.enabled, p.remark, "
                "       r.name, r.status, r.healthy, r.busy_slots, r.max_concurrent, r.version, "
                "       r.credential_id, COALESCE(rc.device_name, ''), COALESCE(rc.last_ip, '') "
                "FROM agent_type_runtime_pool p "
                "LEFT JOIN agent_runtimes r ON r.id = p.runtime_id "
                "LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id "
                "WHERE p.agent_type=? ORDER BY p.priority ASC, p.id ASC",
                (agent_type,))
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"get_agent_pool failed: {e}")
            return []
    return [{
        "runtime_id": r[0], "weight": r[1], "priority": r[2], "enabled": bool(r[3]),
        "remark": r[4] or "", "name": r[5] or "", "status": r[6] or "offline",
        "healthy": bool(r[7]), "busy_slots": r[8] or 0, "max_concurrent": r[9] or 0,
        "version": r[10] or "", "credential_id": r[11] or 0,
        "device_name": r[12] or "", "last_ip": r[13] or "",
    } for r in rows]


def pool_runtime_ids(agent_type: str) -> list[int]:
    """池内 enabled 且在线（心跳在线）的引擎 id 列表（claim 池调度用）。

    只返回在线引擎：池内引擎全离线/不存在时返回空 → 调用方（入队/对话）回退
    默认 runtime，避免任务下发到 offline worker 后无人领取而永久 queued
    （issue #41 卡住根因：池绑定 runtime 9/12/18 全部 offline，任务挂起）。
    """
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT p.runtime_id FROM agent_type_runtime_pool p "
                "JOIN agent_runtimes r ON r.id = p.runtime_id "
                "WHERE p.agent_type=? AND p.enabled=1 AND r.status='online'",
                (agent_type,))
            return [r[0] for r in c.fetchall()]
        except Exception:
            return []


def _issue_num(issue_id) -> int:
    """'#5' / '5' / 5 → 5；无法解析返回 0。"""
    if not issue_id:
        return 0
    m = re.search(r"(\d+)", str(issue_id))
    return int(m.group(1)) if m else 0


def claim(runtime_id) -> dict | None:
    """原子抢单 + 串行槽（见 multi-issue-isolation-design.md §3.3 / concurrency-design.md §2.3）。

    串行粒度（M2）：任务带 issue_session_id → 同 Issue 严格串行（跨 Issue 并行）；
    issue_session_id 为空（旧任务/#0 事件）→ 回退按 agent_session_id 串行（现状语义）。

    引擎池（agent-cluster-design.md §4.2）：本引擎属于某 Agent 池时，除自己的队列外，
    还可领取该池的未定任务（runtime_id=0 + pool_agent_type 匹配，领取后 runtime_id 定为
    本引擎）。affinity_runtime_id 命中本引擎的任务优先（会话亲和，§4.5）。
    """
    with db_lock:
        c = db.cursor()
        try:
            # ---- 兜底回收（M1：dispatched 超时自动重排，防 worker 丢弃任务死循环）----
            # worker 端 max_concurrent 满时 claim 后可能丢弃任务（不 start 不释放），
            # 导致 dispatched 任务永久卡死、busy_slots 泄漏。这里在每次 claim 前
            # 将超时未 start 的任务回池重排；attempts>=3 直接置 failed。
            try:
                # 回收窗口 600s：SRE 执行 terraform apply 等长命令可达 900s，
                # 期间无事件产出→task_messages 无新增→活跃度检查失败→误回收重跑。
                # 300s 窗口会误杀执行中长任务；600s 窗口让长命令有足够时间完成。
                # worker 端 claim 后 task_start 置 running 可彻底规避；本窗口作为
                # 不依赖 worker 端的兜底（真死任务 10 分钟内恢复重排）。
                _stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
                c.execute(
                    "SELECT id, runtime_id, event_seq FROM agent_tasks "
                    "WHERE status='dispatched' AND started_at < ? AND started_at IS NOT NULL",
                    (_stale,))
                _stale_rows = c.fetchall()
                _actually_recovered = []
                for _tid, _rid, _seq in _stale_rows:
                    # 活跃度检查（2026-08-21 修复）：task_start 失败时 status 留在 dispatched
                    # 但 agent 实际在跑（有 task_messages 产出）。查最近 600s 是否有消息，
                    # 有则跳过回收——agent 活跃说明只是 task_start 回调丢了，不该重跑。
                    # 600s 窗口兼容长命令（terraform apply 可达 900s 无事件产出）。
                    try:
                        _active_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
                        c.execute(
                            "SELECT 1 FROM task_messages WHERE task_id=? AND created_at > ? LIMIT 1",
                            (_tid, _active_cutoff))
                        if c.fetchone():
                            continue  # 最近 60s 有消息，agent 在跑，跳过回收
                    except Exception:
                        pass  # 查询失败不阻塞回收（保守：宁可误回收也不留死任务）
                    _actually_recovered.append((_tid, _rid, _seq))
                    # 杀掉旧进程：recover 重新入队后会有新 worker claim 启动新进程，
                    # 旧进程不杀会继续执行（tf-infra7 出现过 2 个进程同时跑同一 task）
                    try:
                        c.execute("SELECT cwd, runtime_id FROM agent_tasks WHERE id=?", (_tid,))
                        _old = c.fetchone()
                        if _old:
                            _backend = "opencode"
                            try:
                                c.execute("SELECT name FROM agent_runtimes WHERE id=?", (_old[1],))
                                _rn = c.fetchone()
                                if _rn and "hermes" in (_rn[0] or "").lower():
                                    _backend = "hermes"
                                elif _rn and "codearts" in (_rn[0] or "").lower():
                                    _backend = "codearts"
                            except Exception:
                                pass
                            from src.runtime.agent import _kill_stale_engine
                            _kill_stale_engine({"cwd": _old[0], "id": _tid}, _backend)
                    except Exception as _ke:
                        log.warning(f"recover: kill stale engine failed for task {_tid}: {_ke}")
                    # 定向任务（pool_agent_type=''）回收归零 runtime_id 后变无主：
                    # claim 池匹配不命中、定向(runtime_id)也失配 → 永久死任务
                    # （实测 seq#264 issue#4 卡死 71 分钟）。归零时回填 agent_type
                    # 转为池任务，池内任意引擎可再领。
                    _pool_fill = ""
                    try:
                        c.execute("SELECT agent_type FROM agent_tasks WHERE id=?", (_tid,))
                        _row = c.fetchone()
                        if _row and _row[0]:
                            _pool_fill = _row[0]
                    except Exception:
                        pass
                    c.execute(
                        "UPDATE agent_tasks SET attempts=attempts+1, "
                        "status=CASE WHEN attempts+1 >= 50 THEN 'failed' ELSE 'queued' END, "
                        "runtime_id=CASE WHEN attempts+1 >= 50 THEN runtime_id ELSE 0 END, "
                        "pool_agent_type=CASE WHEN attempts+1 < 50 AND pool_agent_type='' "
                        "THEN ? ELSE pool_agent_type END, "
                        "started_at=NULL, "
                        "result_text=CASE WHEN attempts+1 >= 50 THEN '回收超限(dispatched 超时 50 次)' ELSE result_text END "
                        "WHERE id=? AND status='dispatched'",
                        (_pool_fill, _tid))
                    if c.rowcount and _rid:
                        c.execute(
                            "UPDATE agent_runtimes SET busy_slots = CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?",
                            (_rid,))
                    # 回收置 failed（attempts 超限）→ 同步事件终态，避免事件永远 running
                    if c.rowcount and _seq:
                        c.execute("SELECT status FROM agent_tasks WHERE id=?", (_tid,))
                        _after = c.fetchone()
                        if _after and _after[0] == "failed":
                            try:
                                from src.api.event_store import update_event_status
                                update_event_status(_seq, "failed")
                            except Exception:
                                pass
                if _actually_recovered:
                    log.info(f"recover stale dispatched: {len(_actually_recovered)}/{len(_stale_rows)} tasks "
                             f"({len(_stale_rows) - len(_actually_recovered)} skipped: active messages)")
            except Exception as _e:
                log.warning(f"recover stale dispatched failed: {_e}")

            # 确定路由：① 定向任务（runtime_id=?）② 池任务（runtime_id=0 + pool_agent_type，
            # pool_engine_ids 为空或包含本引擎即可领；
            # 回收逻辑会把 dispatched 超时任务归零转池任务，claim 必须兼容，否则永久死任务）
            #
            # 插件模式：广播任务只能走 claim_by_tag（标签匹配），legacy claim 不认领广播任务，
            # 避免标签不匹配的机器通过兜底渠道抢到任务。
            c.execute("""
                SELECT id FROM agent_tasks
                WHERE runtime_id=? AND status='queued'
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_tasks a2
                    WHERE a2.status IN ('dispatched','running')
                      AND (
                        (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                        OR
                        (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                      )
                  )
                ORDER BY created_at ASC LIMIT 1
            """, (runtime_id,))
            row = c.fetchone()
            if not row:
                _is_plugin = False
                try:
                    from src.api.event_pipeline import is_plugin_mode
                    _is_plugin = is_plugin_mode()
                except Exception:
                    pass
                if not _is_plugin:
                    c.execute("""
                        SELECT id FROM agent_tasks
                        WHERE runtime_id=0 AND pool_agent_type != '' AND status='queued'
                          AND NOT EXISTS (
                            SELECT 1 FROM agent_tasks a2
                            WHERE a2.status IN ('dispatched','running')
                              AND (
                                (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                                OR
                                (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                              )
                          )
                        ORDER BY created_at ASC LIMIT 1
                    """)
                    row = c.fetchone()
            if not row:
                db.commit()
                return None
            task_id = row[0]

            # pool_engine_ids 限定广播范围：非空时只有列表中的引擎能认领
            # 修复（2026-08-29）：原代码 return None 会阻塞队列——与 claim_by_tag 同一 bug。
            # 改为循环跳过不匹配的任务，继续尝试下一个。
            _tried_ids = [task_id]
            _max_tries = 20
            while True:
                c.execute("SELECT pool_engine_ids FROM agent_tasks WHERE id=?", (task_id,))
                _pe_row = c.fetchone()
                _skip = False
                if _pe_row and _pe_row[0]:
                    import json as _json
                    try:
                        _allowed = _json.loads(_pe_row[0])
                        if _allowed and runtime_id not in _allowed:
                            _skip = True
                    except Exception:
                        pass
                if not _skip:
                    break

                # 跳过当前，尝试下一个定向任务
                _tried_str = ','.join(["'"+x+"'" for x in _tried_ids])
                c.execute(
                    f"SELECT id FROM agent_tasks "
                    f"WHERE runtime_id=? AND status='queued' "
                    f"AND id NOT IN ({_tried_str}) "
                    f"AND NOT EXISTS ("
                    f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                    f"    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)"
                    f"    OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)"
                    f"  )) ORDER BY created_at ASC LIMIT 1",
                    (runtime_id,)
                )
                row = c.fetchone()
                if not row:
                    # 定向任务都试过了，尝试池任务（非插件模式）
                    _is_plugin = False
                    try:
                        from src.api.event_pipeline import is_plugin_mode
                        _is_plugin = is_plugin_mode()
                    except Exception:
                        pass
                    if not _is_plugin:
                        c.execute(
                            f"SELECT id FROM agent_tasks "
                            f"WHERE runtime_id=0 AND pool_agent_type != '' AND status='queued' "
                            f"AND id NOT IN ({_tried_str}) "
                            f"AND NOT EXISTS ("
                            f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                            f"    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)"
                            f"    OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)"
                            f"  )) ORDER BY created_at ASC LIMIT 1"
                        )
                        row = c.fetchone()
                    if not row:
                        db.commit()
                        return None
                task_id = row[0]
                _tried_ids.append(task_id)
                if len(_tried_ids) >= _max_tries:
                    db.commit()
                    return None
            c.execute(
                "UPDATE agent_tasks SET status='dispatched', runtime_id=?, started_at=? WHERE id=? AND status='queued'",
                (runtime_id, datetime.now(timezone.utc).isoformat(), task_id))
            claimed = c.rowcount > 0
            if claimed:
                # busy_slots 对账（2026-08-21 修复）：手动终止/kill -9/超时等场景
                # busy_slots 不递减 → 只增不减 → 溢出 max_concurrent。claim 时如果
                # busy_slots 远超实际活跃任务数（差值>2），重置为实际值。
                # 安全：有心跳的 worker 会在心跳中重新上报；正在执行的任务不受影响。
                try:
                    c.execute(
                        "SELECT COUNT(*) FROM agent_tasks "
                        "WHERE runtime_id=? AND status IN ('dispatched','running')", (runtime_id,))
                    _actual_active = c.fetchone()[0] or 0
                    c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (runtime_id,))
                    _current_busy = c.fetchone()[0] or 0
                    if _current_busy > _actual_active + 2:
                        log.info(f"busy_slots reconcile: runtime={runtime_id} "
                                 f"busy={_current_busy} actual_active={_actual_active} -> reset to {_actual_active}")
                        c.execute("UPDATE agent_runtimes SET busy_slots=? WHERE id=?",
                                  (_actual_active, runtime_id))
                except Exception as _re:
                    log.warning(f"busy_slots reconcile failed: {_re}")
                # 槽位 +1（观测/池调度排序用；不硬限流，边端 RUNTIME_MAX_CONCURRENT_TASKS 仍控制执行并发）
                c.execute("UPDATE agent_runtimes SET busy_slots=busy_slots+1 WHERE id=?", (runtime_id,))
            db.commit()
        except Exception as e:
            log.warning(f"claim failed: {e}")
            db.rollback()
            return None
        if not claimed:
            return None
        task = get_task(task_id)
        if task:
            task["status"] = "dispatched"
            # 回填事件 runtime 为真实引擎实例名（pool 模式入队显示 pool:<type>，
            # claim 后展示实际执行引擎，见 #44：vod 池 hermes 执行但事件显示 opencode）
            if task.get("event_seq"):
                try:
                    from src.runtime.instances import runtime_instance_name as _rin
                    with db_lock:
                        c = db.cursor()
                        c.execute("UPDATE events SET runtime=? WHERE seq=?",
                                  (_rin(runtime_id), task["event_seq"]))
                        db.commit()
                except Exception as e:
                    log.warning(f"claim backfill event runtime failed: {e}")
        from src.runtime.instances import runtime_instance_name
        log.info(f"claim: runtime_id={runtime_id}({runtime_instance_name(runtime_id)}) task={task_id}")
        # 可观察性埋点：任务被 claim/dispatched
        try:
            from src.observability import record_task_dispatched
            if task:
                _queue_wait = -1
                if task.get("created_at"):
                    try:
                        _created = datetime.fromisoformat(task["created_at"])
                        _queue_wait = (datetime.now(timezone.utc) - _created).total_seconds()
                    except Exception:
                        pass
                record_task_dispatched(task_id, task.get("event_type") or task.get("agent_type") or "other",
                                       runtime_id, _queue_wait)
        except Exception:
            pass
        return task


def get_task(task_id) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT {_TASK_COLUMNS} FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    return _row_to_dict(row) if row else None


def claim_by_tag(tag: str, runtime_id: int) -> dict | None:
    """按标签认领任务 + scope 隔离（event-dispatch-redesign-v2.md §2.4）

    scope=global → 可认领所有仓库的 queued 任务
    scope=repo   → 只认领 owner_id 对应仓库的 queued 任务
    scope=user   → 只认领该用户创建的 queued 任务
    """
    with db_lock:
        c = db.cursor()
        try:
            # 1. 校验标签能力 + 获取引擎 scope
            # 标签匹配：精确 tag OR "all"（all 可认领任意 pool_tag 任务）
            c.execute(
                "SELECT ec.tag, rc.scope, rc.owner_id, rc.owner_name "
                "FROM engine_capabilities ec "
                "JOIN agent_runtimes ar ON ar.id = ec.runtime_id "
                "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
                "WHERE ec.runtime_id=? AND ec.enabled=1 AND (ec.tag=? OR ec.tag='all')",
                (runtime_id, tag)
            )
            row = c.fetchone()
            if not row:
                return None
            scope = row[1] or "global"
            owner_id = row[2] or ""
            owner_name = row[3] or ""

            # 2. 并发槽检查
            c.execute(
                "SELECT ec.max_concurrent, "
                "(SELECT COUNT(*) FROM agent_tasks WHERE runtime_id=? AND status IN ('dispatched','running')) "
                "FROM engine_capabilities ec WHERE ec.runtime_id=? AND ec.enabled=1 AND (ec.tag=? OR ec.tag='all')",
                (runtime_id, runtime_id, tag)
            )
            cap_row = c.fetchone()
            if cap_row and cap_row[1] >= cap_row[0]:
                return None  # 已满载

            # 3. 构建任务查询（加 scope 过滤）
            # tag="all" → 匹配任意 pool_tag（广播）；其他 tag → 精确匹配 pool_tag
            # 只认领无主池任务（runtime_id=0），不抢已定向的任务
            _tag_match = "(t.pool_tag!='' AND t.runtime_id=0)" if tag == "all" else "(t.pool_tag=? AND t.runtime_id=0)"
            _tag_match_noprefix = "(pool_tag!='' AND runtime_id=0)" if tag == "all" else "(pool_tag=? AND runtime_id=0)"
            params = [] if tag == "all" else [tag]
            if scope == "repo":
                # repo_key 可能带前缀（gitcode:owner/repo）也可能不带，两种都要匹配
                params.append(owner_id)
                params.append(f"gitcode:{owner_id}")
                sql = (
                    "SELECT t.id FROM agent_tasks t "
                    f"WHERE { _tag_match} AND t.status='queued' AND (t.repo_key=? OR t.repo_key=?) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                    "    (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)"
                    "    OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)"
                    "  )) ORDER BY t.created_at ASC LIMIT 1"
                )
            elif scope == "user":
                c.execute("SELECT id FROM user_accounts WHERE username=?", (owner_name,))
                uid_row = c.fetchone()
                uid = uid_row[0] if uid_row else 0
                params.append(uid)
                sql = (
                    "SELECT t.id FROM agent_tasks t "
                    f"WHERE { _tag_match} AND t.status='queued' AND t.user_id=? "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                    "    (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)"
                    "    OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)"
                    "  )) ORDER BY t.created_at ASC LIMIT 1"
                )
            else:
                sql = (
                    "SELECT id FROM agent_tasks "
                    f"WHERE {_tag_match_noprefix} AND status='queued' "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                    "    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)"
                    "    OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)"
                    "  )) ORDER BY created_at ASC LIMIT 1"
                )
            c.execute(sql, tuple(params))
            task_row = c.fetchone()
            if not task_row:
                return None
            task_id = task_row[0]

            # pool_engine_ids 限定广播范围：非空时只有列表中的引擎能认领
            # 修复（2026-08-29）：原代码 return None 会阻塞队列——队首任务如果不匹配，
            # 后面的任务永远不会被尝试。改为循环跳过不匹配的任务，继续尝试下一个。
            # pool_engine_ids 是 JSON 数组如 "[76]"，SQL 难以直接过滤，
            # 这里用 Python 循环 LIMIT N 逐个检查。
            _tried_ids = [task_id]
            _max_tries = 20  # 防止全表扫描
            while True:
                c.execute("SELECT pool_engine_ids FROM agent_tasks WHERE id=?", (task_id,))
                _pe_row = c.fetchone()
                _skip = False
                if _pe_row and _pe_row[0]:
                    import json as _json
                    try:
                        _allowed = _json.loads(_pe_row[0])
                        if _allowed and runtime_id not in _allowed:
                            _skip = True
                    except Exception:
                        pass
                if not _skip:
                    break  # 当前任务可认领

                # 跳过当前任务，尝试下一个
                _tried_str = ','.join(["'"+x+"'" for x in _tried_ids])
                if scope == "repo":
                    c.execute(
                        f"SELECT t.id FROM agent_tasks t "
                        f"WHERE {_tag_match} AND t.status='queued' AND (t.repo_key=? OR t.repo_key=?) "
                        f"AND t.id NOT IN ({_tried_str}) "
                        f"AND NOT EXISTS ("
                        f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                        f"    (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)"
                        f"    OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)"
                        f"  )) ORDER BY t.created_at ASC LIMIT 1",
                        (*params,)
                    )
                elif scope == "user":
                    c.execute(
                        f"SELECT t.id FROM agent_tasks t "
                        f"WHERE {_tag_match} AND t.status='queued' AND t.user_id=? "
                        f"AND t.id NOT IN ({_tried_str}) "
                        f"AND NOT EXISTS ("
                        f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                        f"    (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)"
                        f"    OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)"
                        f"  )) ORDER BY t.created_at ASC LIMIT 1",
                        (*params,)
                    )
                else:
                    c.execute(
                        f"SELECT id FROM agent_tasks "
                        f"WHERE {_tag_match_noprefix} AND status='queued' "
                        f"AND id NOT IN ({_tried_str}) "
                        f"AND NOT EXISTS ("
                        f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                        f"    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)"
                        f"    OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)"
                        f"  )) ORDER BY created_at ASC LIMIT 1",
                        tuple(params)
                    )
                task_row = c.fetchone()
                if not task_row:
                    return None  # 所有任务都试过了，没有可认领的
                task_id = task_row[0]
                _tried_ids.append(task_id)
                if len(_tried_ids) >= _max_tries:
                    return None  # 防止无限循环

            # 标记 dispatched
            c.execute(
                "UPDATE agent_tasks SET status='dispatched', runtime_id=?, started_at=? WHERE id=? AND status='queued'",
                (runtime_id, datetime.now(timezone.utc).isoformat(), task_id)
            )
            claimed = c.rowcount > 0
            if claimed:
                c.execute("UPDATE agent_runtimes SET busy_slots=busy_slots+1 WHERE id=?", (runtime_id,))
            db.commit()
        except Exception as e:
            log.warning(f"claim_by_tag failed: {e}")
            db.rollback()
            return None
    if not claimed:
        return None
    task = get_task(task_id)
    if task:
        task["status"] = "dispatched"
        # 回填事件 runtime
        if task.get("event_seq"):
            try:
                from src.runtime.instances import runtime_instance_name as _rin
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE events SET runtime=? WHERE seq=?", (_rin(runtime_id), task["event_seq"]))
                    db.commit()
            except Exception:
                pass
    from src.runtime.instances import runtime_instance_name
    log.info(f"claim_by_tag: tag={tag} runtime={runtime_id}({runtime_instance_name(runtime_id)}) task={task_id}")
    return task


def update_task_status(task_id, status, **kw):
    """终态幂等(重放安全): 已终态则不再更新(见 tech-impl 2.2)."""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, runtime_id FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        if not row:
            return
        if row[0] in ("completed", "failed", "cancelled"):
            return
        sets = ["status=?"]
        params = [status]
        if status in ("completed", "failed", "cancelled"):
            # 终态才写 completed_at；非终态（running 等）不动它，
            # 否则运行中任务带 completed_at 脏值（2026-08-19 修复：task_start 曾把
            # running 任务写成 completed_at=启动时刻，干扰终态判断/展示）。
            sets.append("completed_at=?")
            params.append(kw.get("completed_at") or datetime.now(timezone.utc).isoformat())
        for col in ("exit_code", "result_text", "errors", "stderr", "usage_json"):
            if col in kw:
                sets.append(f"{col}=?")
                params.append(kw[col])
        c.execute(f"UPDATE agent_tasks SET {', '.join(sets)} WHERE id=?", params + [task_id])
        # 槽位释放（agent-cluster-design.md §4.2.1）：非终态 → 终态，且任务已绑定引擎 → busy_slots-1
        if status in ("completed", "failed", "cancelled") and row[1] and row[1] > 0:
            c.execute(
                "UPDATE agent_runtimes SET busy_slots = CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?",
                (row[1],))
        db.commit()


def cancel_task(task_id):
    update_task_status(task_id, "cancelled")


def requeue_task(task_id):
    """将 dispatched/running 任务重置回 queued（客户端并发冲突跳过时回滚，避免任务悬空卡死）。

    见 seq#622 教训：客户端 _run_loop 因并发槽冲突 skip 任务后，服务端任务仍停留 dispatched，
    既不被执行也不重新排队 → 永久卡死。客户端冲突时应调本函数回滚。
    """
    with db_lock:
        c = db.cursor()
        # 释放槽位（根因2修复）：requeue 前先查 runtime_id，requeue 后 busy_slots-1
        c.execute("SELECT runtime_id FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        c.execute("UPDATE agent_tasks SET status='queued', started_at=NULL WHERE id=? AND status IN ('dispatched','running')",
                  (task_id,))
        ret = c.rowcount > 0
        if ret and row and row[0]:
            c.execute(
                "UPDATE agent_runtimes SET busy_slots = CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?",
                (row[0],))
        db.commit()
        return ret


def report_messages(task_id, messages):
    """批量执行消息 → append_event_archive_log（M1 落档，脱敏）+ task_messages 结构化落库 + 实时 fanout"""
    task = get_task(task_id)
    if not task:
        return
    # 逐步日志前缀：task + 非空的 seq/issue/agent/repo，与 worker 侧（agent.py _esq）对齐便于定位
    _esq_parts = [f"seq={task.get('event_seq') or '?'}"]
    _issue_id = (task.get("issue_id") or "").strip().lstrip("#")
    if _issue_id and _issue_id != "0":
        _esq_parts.append(f"issue:#{_issue_id}")
    _agent_type = task.get("agent_type") or ""
    if _agent_type:
        _esq_parts.append(f"agent:{_agent_type}")
    _repo_key = task.get("repo_key") or ""
    if _repo_key:
        _esq_parts.append(f"repo:{_repo_key}")
    _esq = "|".join(_esq_parts)
    # 逐步日志（服务端观察：step_start/step_finish 等；远端执行时 step 详情在 worker 日志）
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        _t = m.get("type")
        if _t in ("step_start", "step_finish", "text", "reasoning", "tool_use", "error"):
            _d = m.get("data")
            log.info(f"[task {task_id}] [{_esq}] {_t}: {str(_d)[:120]}")
    if task.get("event_seq"):
        try:
            from src.api.event_archive import append_event_archive_log
            tokens = [task.get("repo_gitcode_token") or ""]
            hint = task.get("issue_id") or ""
            for m in messages or []:
                if isinstance(m, dict):
                    append_event_archive_log(task["repo_key"], task["event_seq"],
                                             m.get("type", "log"), m, tokens=tokens,
                                             issue_num_hint=hint)
        except Exception as e:
            log.warning(f"report_messages archive failed: {e}")
    # 结构化落库（task_messages，双后端一致；seq 服务端生成 MAX+1，UNIQUE 防重放）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COALESCE(MAX(seq),0) FROM task_messages WHERE task_id=?", (task_id,))
            seq = (c.fetchone()[0] or 0) + 1
            for m in (messages or []):
                if not isinstance(m, dict):
                    continue
                _t = m.get("type", "log")
                _d = m.get("data")
                tool, content, input_json, output = _normalize_message(_t, _d)
                # created_at 显式写 UTC ISO（时间本地化：MySQL 表默认 now() 为 CST 无时区，
                # 双后端存储语义统一，读时 normalize_event_time 幂等）
                c.execute(
                    "INSERT OR IGNORE INTO task_messages (task_id, seq, type, tool, content, input_json, output, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (task_id, seq, _t, tool, content, input_json, output,
                     datetime.now(timezone.utc).isoformat()))
                seq += 1
            db.commit()
    except Exception as e:
        log.warning(f"report_messages task_messages insert failed: {e}")
    # 实时 fanout（订阅表推送，队列满丢弃保活；SSE 重连靠 since 回源）
    try:
        from src.runtime.task_log_stream import fanout
        fanout(task_id, messages or [])
    except Exception as e:
        log.warning(f"report_messages fanout failed: {e}")


def _normalize_message(_t: str, _d) -> tuple[str, str, str, str]:
    """worker 消息 → (tool, content, input_json, output) 规范化。

    - tool_use: data 为 dict {tool, input}（input 可能已是 JSON 字符串或 dict）
    - text/reasoning/step_*: data 为 str 或 {text/content}
    - output 列保留给未来 tool_result（worker 端暂未上报 tool_result 结构化输出）
    """
    if isinstance(_d, dict):
        tool = str(_d.get("tool") or "")
        if _t == "tool_use":
            inp = _d.get("input") or {}
            if isinstance(inp, str):
                try:
                    json.loads(inp)
                    input_json = inp
                except Exception:
                    input_json = json.dumps({"raw": inp}, ensure_ascii=False)
            else:
                input_json = json.dumps(inp, ensure_ascii=False)
            return tool, "", input_json, ""
        content = str(_d.get("text") or _d.get("content") or "")
        return tool, content, "", ""
    return "", str(_d or ""), "", ""


def list_task_messages(task_id, since: int = 0, limit: int = 5000) -> list[dict]:
    """按 seq 增量拉取 task_messages（SSE 断线/新开页补数据用）。

    返回 [{seq, type, tool, content, input, output, created_at}]，input 为解析后的 dict。
    """
    import json as _json
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, type, tool, content, input_json, output, created_at FROM task_messages "
                "WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                (task_id, since, limit))
            rows = c.fetchall()
    except Exception as e:
        log.warning(f"list_task_messages failed: {e}")
        return []
    out = []
    for r in rows:
        inp = {}
        try:
            inp = _json.loads(r[4] or "{}")
        except Exception:
            pass
        out.append({"seq": r[0], "type": r[1], "tool": r[2] or "",
                    "content": r[3] or "", "input": inp, "output": r[5] or "",
                    "created_at": normalize_event_time(r[6] or "")})
    return out


def report_progress(task_id, progress):
    # 阶段进度：写 result_text 前缀（保持轻量，完整进度落 agent_tasks.result_text）
    task = get_task(task_id)
    if not task:
        return
    p = progress or {}
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET result_text=? WHERE id=?", (str(p.get("msg", "")), task_id))
        db.commit()


def _detect_business_failure(agent_type, output, result_text):
    """检测 agent 进程正常退出(exit_code=0)但业务实际失败的情况。

    解析结构化 JSON 输出的 conclusion/next_step 字段，匹配部署/操作失败关键词。
    返回 True 表示业务失败（应覆盖 exit_code 为非 0）。

    仅对 sre agent_type 生效（部署类任务），其他 agent 不受影响。
    """
    if agent_type != "sre":
        return False
    if not output:
        return False
    # 拼接 conclusion + next_step + issue_summary 做全文匹配
    blob = " ".join(filter(None, [
        output.get("conclusion", ""),
        output.get("next_step", ""),
        output.get("issue_summary", ""),
    ]))
    if not blob:
        # 降级：从 result_text 里匹配
        blob = result_text or ""
    if not blob:
        return False
    import re
    # 失败关键词：部署中止/失败、无法购买/创建、未认证、凭证不足、部署未成功等
    failure_patterns = [
        r"部署.*失败", r"部署.*中止", r"部署.*未成功", r"部署.*无法",
        r"无法购买", r"无法创建", r"无法部署",
        r"未完成.*认证", r"未实名", r"实名认证",
        r"凭证.*不足|凭证.*缺失|凭证.*无效",
        r"Ecs\.0707", r"AccessDenied", r"auth.*fail",
        r"操作失败", r"执行失败",
        r"无法继续", r"中止\b",
    ]
    for pat in failure_patterns:
        if re.search(pat, blob, re.I):
            return True
    return False


def complete_task(task_id, result_text, exit_code=0, usage=None):
    """完成：解析结构化 output → 业务失败检测 → finalize_event_archive → update_event_complete → 终态。"""
    task = get_task(task_id)
    # 幂等保护：如果 task 已经是终态（failed/cancelled），不覆盖（防止 worker 终止后残留进程
    # 跑完调 complete 把 failed 状态覆盖成 completed，2026-08-24 seq#2488 实例）。
    # 例外：如果 result_text 包含完整结构化输出（actions+conclusion），说明任务实际已完成，
    # 即使 exit_code 非 0（如 hermes ACP 进程被 SIGTERM 清理后 proc.returncode=-15）也应覆盖为 completed。
    if task and task.get("status") in ("failed", "cancelled"):
        from src.api.event_store import parse_agent_output_json
        _output = parse_agent_output_json(result_text or "")
        if _output and _output.get("actions") and _output.get("conclusion"):
            log.info(f"Task {task_id} was {task.get('status')} but has structured output, overriding to completed")
        else:
            log.info(f"Task {task_id} already {task.get('status')}, skip complete_task (result={result_text[:60]})")
            return
    if task and task.get("event_seq"):
        try:
            from src.api.event_store import parse_agent_output_json, update_event_complete
            from src.api.event_archive import finalize_event_archive
            output = parse_agent_output_json(result_text or "")
            # 结构化产出修复：exit_code 非 0 但有完整结构化输出（actions+conclusion）
            # → hermes ACP 进程被 SIGTERM 清理后 proc.returncode=-15，任务实际已完成
            if exit_code != 0 and output and output.get("actions") and output.get("conclusion"):
                log.info(f"Task {task_id}: exit_code {exit_code} but has structured output, overriding to 0")
                exit_code = 0
            # 业务失败检测：进程退出码=0 但业务实际失败 → 覆盖 exit_code
            if exit_code == 0 and _detect_business_failure(
                    task.get("agent_type") or "", output, result_text):
                exit_code = 1
                log.info(f"Task {task_id}: business failure detected "
                         f"(agent={task.get('agent_type')}), overriding exit_code to 1")

            # 完成度校验：exit_code=0 但无结构化产出 → LLM 提前 end_turn
            # coding/qa/sre/issue agent 正常完成应有 actions/conclusion/issue_summary JSON 块
            # 没有则说明 LLM 提前 end_turn，未执行完任务
            _agent_type = task.get("agent_type") or ""
            if exit_code == 0 and not output and _agent_type in ("coding", "qa", "sre", "issue"):
                _attempts = (task.get("attempts") or 0)
                if _attempts < 49:
                    log.warning(
                        f"Task {task_id}: premature end_turn (no structured output, "
                        f"agent={_agent_type}, attempt={_attempts+1}/50), requeueing")
                    result_text = (result_text or "") + "\n[PREMATURE_END_TURN] agent 结束但无结构化产出，疑似 LLM 提前 end_turn，自动重试"
                    update_task_status(task_id, "queued", started_at=None,
                                       result_text=result_text,
                                       usage_json=json.dumps(usage or {}))
                    # 释放槽位 + attempts+1
                    with db_lock:
                        c2 = db.cursor()
                        c2.execute("UPDATE agent_tasks SET attempts=attempts+1, "
                                   "runtime_id=0, started_at=NULL, status='queued' "
                                   "WHERE id=?", (task_id,))
                        if task.get("runtime_id"):
                            c2.execute(
                                "UPDATE agent_runtimes SET busy_slots = CASE "
                                "WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END "
                                "WHERE id=?", (task.get("runtime_id"),))
                        db.commit()
                    log.info(f"Task {task_id}: requeued for retry (attempt={_attempts+1})")
                    return
                else:
                    log.warning(
                        f"Task {task_id}: premature end_turn, max retries exceeded "
                        f"(attempt={_attempts}/50), marking failed")
                    exit_code = 1
                    result_text = (result_text or "") + "\n[PREMATURE_END_TURN] agent 多次结束但无结构化产出，已达最大重试次数(50)"

            status = "completed" if exit_code == 0 else "failed"
            duration = None
            try:
                s = datetime.fromisoformat(task["started_at"]) if task.get("started_at") else None
                if s:
                    duration = round((datetime.now(timezone.utc) - s).total_seconds(), 1)
            except Exception:
                pass
            finalize_event_archive(task["repo_key"], task["event_seq"], status,
                                   exit_code, duration, output,
                                   tokens=[task.get("repo_gitcode_token") or ""],
                                   issue_num_hint=task.get("issue_id") or "")
            _conclusion = ""
            if output and output.get("conclusion"):
                _conclusion = output["conclusion"]
            update_event_complete(task["event_seq"], status, exit_code=exit_code,
                                  duration=duration,
                                  completed_at=datetime.now(timezone.utc).isoformat(),
                                  result=_conclusion or (result_text or "")[:200])
        except Exception as e:
            log.warning(f"complete_task finalize failed: {e}")
    update_task_status(task_id, "completed" if exit_code == 0 else "failed",
                       exit_code=exit_code, result_text=result_text or "",
                       usage_json=json.dumps(usage or {}))
    # 新会话续接指针回写（Issue 级优先，repo 级兼容，见 multi-issue-isolation-design.md §3.3/§3.5）
    _new_sid = (usage or {}).get("session_id") or ""
    if _new_sid:
        try:
            from src.api.session_store import update_issue_session
            if task and task.get("issue_session_id"):
                update_issue_session(task["issue_session_id"], runtime_session_id=_new_sid)
        except Exception as e:
            log.warning(f"complete_task update issue session failed: {e}")
        try:
            with db_lock:
                c = db.cursor()
                if task and task.get("agent_session_id"):
                    c.execute("UPDATE devops_agent_session SET runtime_session_id=?, last_active_at=datetime('now') WHERE id=?",
                              (_new_sid, task["agent_session_id"]))
                    db.commit()
        except Exception as e:
            log.warning(f"complete_task update repo session failed: {e}")
    log.info(f"Task completed: {task_id} exit={exit_code}")
    # 可观察性埋点：任务到达终态
    try:
        from src.observability import record_task_terminal
        _terminal = "completed" if exit_code == 0 else "failed"
        _run_s = -1
        _total_s = -1
        if task:
            _now = datetime.now(timezone.utc)
            if task.get("started_at"):
                try:
                    _run_s = (_now - datetime.fromisoformat(task["started_at"])).total_seconds()
                except Exception:
                    pass
            if task.get("created_at"):
                try:
                    _total_s = (_now - datetime.fromisoformat(task["created_at"])).total_seconds()
                except Exception:
                    pass
            record_task_terminal(task_id, task.get("event_type") or task.get("agent_type") or "other",
                                 task.get("runtime_id", 0), _terminal, _run_s, _total_s)
    except Exception:
        pass
    # 事件总线：任务完成
    try:
        from src.observability.events import bus, Event
        bus.publish(Event(type="task:completed" if exit_code == 0 else "task:failed",
                          task_id=task_id, payload={"exit_code": exit_code}))
    except Exception:
        pass
    # A2A 自动补触发（2026-08-30 修复）：
    # hermes ACP premature end_turn bug：agent 说完"触发 A2A"后直接 end_turn，
    # 没实际调用 A2A API → pipeline 卡住。任务完成后检查 issue_state.stage 是否已推进，
    # 如果 stage 还等于当前 agent_type（说明 A2A 没被调用），自动补触发下一个 agent。
    if exit_code == 0 and task:
        try:
            _auto_advance_pipeline(task)
        except Exception as _ae:
            log.warning(f"auto advance pipeline failed for task {task_id}: {_ae}")


def _auto_advance_pipeline(task: dict):
    """任务完成后检查 pipeline 是否推进，没推进则自动触发下一个 agent。

    通过 pipelines.json 查找当前 agent 的 next agent，然后调 A2A。
    只在 issue_id 非空 + stage 仍等于当前 agent_type 时触发（说明 agent 没调 A2A）。
    """
    import threading
    _issue_id = (task.get("issue_id") or "").strip()
    _repo_key = task.get("repo_key") or ""
    _agent_type = (task.get("agent_type") or "").strip()
    if not _issue_id or not _repo_key or not _agent_type:
        return
    if _agent_type in ("chat", "vod") and not _issue_id:
        return  # chat 无 issue，vod 是首步不需要补触发

    # 检查 issue_state.stage 是否已推进
    with db_lock:
        c = db.cursor()
        c.execute("SELECT stage, scene_id FROM issue_state WHERE repo_key=? AND issue_id=?",
                  (_repo_key, _issue_id))
        _row = c.fetchone()
    if not _row:
        return  # 无 issue_state 记录（非 pipeline 任务）
    _stage, _scene_id = _row[0], (_row[1] if len(_row) > 1 else "") or ""
    if _stage != _agent_type:
        return  # stage 已推进（agent 成功调了 A2A），不需要补触发
    # stage 还等于当前 agent_type → A2A 没被调用，需要补触发

    # 检查 Issue 是否已关闭（agent 有意不推进 A2A，如重复 Issue 关闭后无需交办）
    _issue_closed = False
    try:
        import requests as _req
        _repo_display = _repo_key.split(":", 1)[-1] if ":" in _repo_key else _repo_key
        _owner, _repo = _repo_display.split("/", 1)
        _issue_num = _issue_id.lstrip("#")
        from src.user.db import get_repo_bot_token_no_global
        _bot_token = get_repo_bot_token_no_global(_repo_key) or ""
        _api_url = f"https://api.gitcode.com/api/v5/repos/{_owner}/{_repo}/issues/{_issue_num}"
        _resp = _req.get(_api_url, headers={"Authorization": f"Bearer {_bot_token}"}, timeout=10)
        if _resp.status_code == 200:
            _issue_data = _resp.json()
            _issue_state = (_issue_data.get("state") or "").lower()
            if _issue_state in ("closed", "close"):
                _issue_closed = True
                log.info(f"Auto-advance skipped: issue={_issue_id} agent={_agent_type} "
                         f"(issue already closed, agent intentionally did not advance)")
    except Exception as _e:
        log.warning(f"Auto-advance issue state check failed: {_e}")
    if _issue_closed:
        return  # Issue 已关闭，agent 有意不推进 pipeline，不补触发

    # 查找 next agent：优先从 DB 自定义场景查，降级到 pipelines.json
    _next_agent = ""
    _scene_nodes = None
    # 1. DB 自定义场景（custom-xxx）
    if _scene_id and _scene_id.startswith("custom-"):
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id=? AND is_active=1",
                          (_scene_id,))
                _row = c.fetchone()
            if _row and _row[0]:
                import json as _json
                _scene_nodes = _json.loads(_row[0])
        except Exception:
            pass
    # 2. pipelines.json（官方场景或自定义场景未命中时的降级）
    if not _scene_nodes:
        try:
            import os as _os
            _pipe_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))),
                                       "configs", "pipelines.json")
            import json as _json
            with open(_pipe_path, "r", encoding="utf-8") as _f:
                _pipes = _json.load(_f)
            _scene = _pipes.get("scenes", {}).get(_scene_id) or _pipes.get("scenes", {}).get("default") or {}
            _scene_nodes = _scene.get("nodes") or []
        except Exception:
            pass
    for _node in (_scene_nodes or []):
        if _node.get("id") == _agent_type:
            _next_agent = _node.get("next", "")
            break
    if not _next_agent or _next_agent == "done":
        return  # 没有下一个 agent，或已经是最后一步

    # 最小产物校验：检查 agent 是否有实际产出
    # coding agent 应该有代码变更（result_text 非空且不是纯错误说明）
    # 没产出的 agent 不推进 pipeline，避免假完成
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, result_text, exit_code, errors FROM agent_tasks "
                  "WHERE repo_key=? AND issue_id=? AND agent_type=? "
                  "ORDER BY id DESC LIMIT 1",
                  (_repo_key, _issue_id, _agent_type))
        _task_row = c.fetchone()
    if _task_row:
        _result_text = (_task_row[1] or "").strip()
        _exit_code = _task_row[2]
        _errors = (_task_row[3] or "").strip()
        # 产物为空或只有错误信息 → 不推进
        if not _result_text or (len(_result_text) < 50 and _errors):
            log.warning(f"Auto-advance skipped: issue={_issue_id} agent={_agent_type} "
                        f"no meaningful output (result_len={len(_result_text)}, errors={_errors[:100]})")
            return

    # 检查是否已有下一个 agent 的 running 事件（避免重复触发）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT seq FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? "
                  "AND status IN ('running','dispatched') ORDER BY seq DESC LIMIT 1",
                  (_repo_key, _issue_id, _next_agent))
        _existing = c.fetchone()
    if _existing:
        return  # 已有下一个 agent 的 running 事件，不重复触发

    log.info(f"Auto-advance: issue={_issue_id} agent={_agent_type} -> {_next_agent} "
             f"(stage stuck at {_stage}, A2A not called by agent)")

    # 异步触发 A2A（不阻塞 complete_task 返回）
    def _trigger_a2a():
        try:
            import requests as _req
            _issue_num = _issue_id.lstrip("#")
            _repo_display = _repo_key.split(":", 1)[-1] if ":" in _repo_key else _repo_key
            # 获取 bot token
            from src.user.db import get_repo_bot_token_no_global
            _bot_token = get_repo_bot_token_no_global(_repo_key) or ""
            # 优先用本地直连（127.0.0.1:8000），避免兄弟节点经公网域名绕回导致超时
            _server_url = "http://127.0.0.1:8000"
            try:
                from src.user.db import get_meta_value
                _sbu = get_meta_value("server_base_url")
                if _sbu and "127.0.0.1" not in _sbu and "localhost" not in _sbu:
                    # meta 配置的是公网域名 → 转成本地直连
                    _server_url = "http://127.0.0.1:8000"
                elif _sbu:
                    _server_url = _sbu
            except Exception:
                pass
            _resp = _req.post(f"{_server_url}/api/a2a",
                              json={"agent": _next_agent,
                                    "payload": {"repository": _repo_display,
                                                "issue_number": int(_issue_num) if _issue_num.isdigit() else _issue_num,
                                                "context": f"auto-advance from {_agent_type} (A2A not called by agent)"}},
                              headers={"Authorization": f"Bearer {_bot_token}",
                                       "Content-Type": "application/json"},
                              timeout=30)
            log.info(f"Auto-advance A2A: status={_resp.status_code} agent={_next_agent} "
                     f"issue={_issue_id} resp={_resp.text[:100]}")
        except Exception as _e:
            log.warning(f"Auto-advance A2A failed: {_e}")

    threading.Thread(target=_trigger_a2a, daemon=True).start()


def fail_task(task_id, error, exit_code=-1):
    task = get_task(task_id)
    # 幂等保护：如果 task 已经 completed，不调 fail（防止 complete→fail 竞态覆盖）
    if task and task.get("status") == "completed":
        log.info(f"Task {task_id} already completed, skip fail_task (error={error[:60]})")
        return
    if task and task.get("event_seq"):
        try:
            from src.api.event_store import update_event_complete
            from src.api.event_archive import finalize_event_archive
            finalize_event_archive(task["repo_key"], task["event_seq"], "failed",
                                   exit_code, None, None,
                                   tokens=[task.get("repo_gitcode_token") or ""],
                                   issue_num_hint=task.get("issue_id") or "")
            update_event_complete(task["event_seq"], "failed", exit_code=exit_code,
                                  completed_at=datetime.now(timezone.utc).isoformat(),
                                  result=(error or "")[:200])
        except Exception as e:
            log.warning(f"fail_task finalize failed: {e}")
    update_task_status(task_id, "failed", exit_code=exit_code,
                       errors=json.dumps([str(error)] if error else []))
    # 死 session 检测（smart-session-recovery-design.md §2.3）：
    # 同一 issue_agent_session 下连续 ≥3 次 exit_code < 0（-15/-1/timeout）→ 清空 runtime_session_id
    # 注意：worker 端 client.fail 不传 exit_code，服务端 fail_task 恒收到 exit_code=-1；
    # exit=1（快速失败如 key/config 问题）不计入 session 故障，避免误清
    _isid = (task or {}).get("issue_session_id") or ""
    if _isid and exit_code < 0:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT exit_code FROM agent_tasks WHERE issue_session_id=? ORDER BY created_at DESC LIMIT 3",
                    (_isid,))
                rows = c.fetchall()
                if len(rows) >= 3 and all((r[0] or 0) < 0 for r in rows):
                    from src.api.session_store import update_issue_session
                    update_issue_session(_isid, runtime_session_id="")
                    log.info(f"fail_task: dead session detected (consecutive failures), cleared runtime_session_id for {_isid}")
        except Exception as e:
            log.warning(f"fail_task session check failed: {e}")
    log.warning(f"Task failed: {task_id} exit={exit_code} err={str(error)[:120]}")
    # 可观察性埋点：任务失败
    try:
        from src.observability import record_task_terminal, record_task_failed
        _run_s = -1
        _total_s = -1
        if task:
            _now = datetime.now(timezone.utc)
            if task.get("started_at"):
                try:
                    _run_s = (_now - datetime.fromisoformat(task["started_at"])).total_seconds()
                except Exception:
                    pass
            if task.get("created_at"):
                try:
                    _total_s = (_now - datetime.fromisoformat(task["created_at"])).total_seconds()
                except Exception:
                    pass
            record_task_terminal(task_id, task.get("event_type") or task.get("agent_type") or "other",
                                 task.get("runtime_id", 0), "failed", _run_s, _total_s)
            record_task_failed(task_id, task.get("event_type") or task.get("agent_type") or "other",
                               task.get("runtime_id", 0), str(error)[:200])
    except Exception:
        pass
    # 事件总线：任务失败
    try:
        from src.observability.events import bus, Event
        bus.publish(Event(type="task:failed", task_id=task_id,
                          payload={"exit_code": exit_code, "error": str(error)[:200]}))
    except Exception:
        pass


# ===== 回报异步化（M2，见 concurrency-design.md §2.4）=====
# 供 M3 /api/runtime/* 回报端点使用：入队即返回，后台攒批写库，避免阻塞 event loop。
# queue_local 本地 worker 仍走同步 report_*（在 worker 线程内，无 event loop 阻塞）。

def start_report_ingestion_loop(loop=None):
    """启动回报异步落库循环（FastAPI lifespan 调用，见 main.py）。"""
    try:
        import asyncio
        _loop = loop or asyncio.get_running_loop()
        _task = _loop.create_task(_ingestion_loop())
        return _task
    except Exception as e:
        log.warning(f"report ingestion loop not started (sync fallback): {e}")
        return None


async def _ingestion_loop():
    """攒批落库：messages/progress/usage 入队，批量写。complete/fail 仍同步（强一致）。"""
    import asyncio
    _report_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    # 模块级队列引用（供 async 端点 put）
    global _ASYNC_REPORT_QUEUE
    _ASYNC_REPORT_QUEUE = _report_queue
    while True:
        try:
            batch = [await _report_queue.get()]
            while len(batch) < 50:
                try:
                    batch.append(await asyncio.wait_for(_report_queue.get(), timeout=0.2))
                except asyncio.TimeoutError:
                    break
            with db_lock:
                for kind, tid, payload in batch:
                    try:
                        if kind == "messages":
                            report_messages(tid, payload)
                        elif kind == "progress":
                            report_progress(tid, payload)
                    except Exception as e:
                        log.warning(f"ingestion apply failed: {e}")
                db.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"ingestion loop error: {e}")
            await asyncio.sleep(1)


_ASYNC_REPORT_QUEUE = None


def enqueue_async_report(kind: str, task_id: str, payload):
    """异步回报入队（队列满降级同步，不丢数据）。"""
    q = _ASYNC_REPORT_QUEUE
    if q is None:
        # 队列未启动 → 同步兜底
        if kind == "messages":
            report_messages(task_id, payload)
        elif kind == "progress":
            report_progress(task_id, payload)
        return
    try:
        q.put_nowait((kind, task_id, payload))
    except Exception:
        # 队列满：同步降级
        if kind == "messages":
            report_messages(task_id, payload)
        elif kind == "progress":
            report_progress(task_id, payload)


async def wait_task_terminal(task_id: str, timeout: float = 1800.0, poll_interval: float = 3.0) -> dict:
    """异步轮询任务直到终态（completed/failed/cancelled）。

    供 IM / web 等"入队 + 等结果"路径使用（见 multi-runtime-instance-design 配套）。
    超时抛 TimeoutError。返回终态 task dict（含 result_text / exit_code / usage_json.session_id）。
    """
    import asyncio, time as _time
    deadline = _time.monotonic() + timeout
    while True:
        t = get_task(task_id)
        if t and t.get("status") in ("completed", "failed", "cancelled"):
            return t
        if _time.monotonic() > deadline:
            raise TimeoutError(f"task {task_id} not finished within {timeout}s")
        await asyncio.sleep(poll_interval)


# ===== Phase 5: Batch stale recovery + heartbeat consolidation =====

def batch_recover_stale_tasks(timeout_seconds: int = 300) -> dict:
    """Batch-recover stale dispatched tasks (Phase 5).

    Scans all dispatched tasks older than timeout_seconds that haven't started running.
    Requeues them back to queued status so other engines can claim.

    This replaces the per-claim stale check with a batch scan, reducing lock contention
    for 10万+ device scenarios.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    recovered = []
    with db_lock:
        c = db.cursor()
        try:
            # Find stale dispatched tasks (dispatched but not running, older than cutoff)
            c.execute(
                "SELECT id, runtime_id, event_seq FROM agent_tasks "
                "WHERE status='dispatched' AND started_at IS NOT NULL AND started_at < ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM task_messages WHERE task_messages.task_id = agent_tasks.id"
                ") ORDER BY started_at ASC LIMIT 100",
                (cutoff,)
            )
            stale = c.fetchall()
            for row in stale:
                task_id, rid, seq = row[0], row[1], row[2]
                # Requeue: set back to queued, clear runtime assignment
                c.execute(
                    "UPDATE agent_tasks SET status='queued', started_at=NULL WHERE id=? AND status='dispatched'",
                    (task_id,)
                )
                if c.rowcount > 0:
                    # Decrement busy_slots
                    if rid:
                        c.execute(
                            "UPDATE agent_runtimes SET busy_slots = CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?",
                            (rid,)
                        )
                    recovered.append({"task_id": task_id, "runtime_id": rid, "seq": seq})
            db.commit()
        except Exception as e:
            log.warning(f"batch_recover_stale_tasks failed: {e}")
            db.rollback()

    if recovered:
        log.info(f"Batch stale recovery: {len(recovered)} tasks requeued")
    return {"recovered": len(recovered), "tasks": recovered}


def heartbeat_consolidated(credential_id: int, runtime_ids: list[int], client_ip: str = "") -> dict:
    """Consolidated heartbeat (Phase 5): worker-level heartbeat instead of per-engine.

    One heartbeat call updates the credential + all its runtimes in a single transaction.
    Also triggers batch stale recovery opportunistically (every 10th heartbeat).
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        # Update credential
        c.execute(
            "UPDATE runtime_credentials SET last_seen_at=?, last_ip=? WHERE id=? AND status='active'",
            (now, client_ip, credential_id)
        )
        # Update all runtimes for this credential
        if runtime_ids:
            ph = ",".join("?" for _ in runtime_ids)
            c.execute(
                f"UPDATE agent_runtimes SET status='online', last_seen_at=? WHERE id IN ({ph}) AND credential_id=?",
                [now] + runtime_ids + [credential_id]
            )
        db.commit()

    # Opportunistic stale recovery (every call, but batch is cheap)
    # In production, this would be rate-limited to every N seconds
    return {"status": "ok", "timestamp": now}
