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


def _check_pool_engines_reachable(engine_ids: list, tag: str, repo_key: str = "", task_user_id: int = 0) -> bool:
    """检查 pool_engine_ids 中的引擎是否有任何一个能真正认领该 tag 的任务。

    检查维度：在线 + healthy + probe_status + 心跳新鲜（Issue #259）+ 有 tag 能力 + scope 匹配。
    返回 True = 池里有物理可达的引擎，应保留绑定并排队等待容量。
    返回 False = 池里全不可达，由调用方诚实失败，不能放行池外引擎。
    并发容量由实际认领检查；满槽是暂时忙碌，不是不可达（Issue #306）。

    Issue #134 扩展：健康分维度不在此函数判定——健康分低但物理可达的引擎
    仍然算 reachable（不清空 pool），claim_by_tag 中的健康分检查会阻止它们认领。
    防饿死由 _check_stale_queued_tasks 超时降级处理。

    Lock contract（Issue #259 Optional）：调用方必须已持有 db_lock。
    本函数不再嵌套 with db_lock——生产 db_lock=RLock（可重入，嵌套无碍），
    但测试 monkeypatch 裸 Lock（非重入）时嵌套获取会自死锁。
    所有调用点（claim / claim_by_tag / _check_stale_queued_tasks）均在 with db_lock 块内调用。
    """
    from src.repo_key import from_repo_key
    _repo_norm = (from_repo_key(repo_key) or "").strip()
    for eid in engine_ids:
        try:
            c = db.cursor()
            # 引擎在线 + healthy + probe 正常？
            c.execute("SELECT status, healthy, probe_status, last_seen_at FROM agent_runtimes WHERE id=?", (eid,))
            r = c.fetchone()
            if not r or (r[0] or "") != "online":
                continue
            # Issue #259：心跳新鲜度检查——status=online 但 last_seen_at 过期 = 刚断线的窗口期，
            # 引擎实际不可达（与 _health_ok / reap 同一把尺子），不算 reachable。
            _ls = r[3] or ""
            if _ls:
                from src.runtime.health import parse_dt as _parse_dt
                _ts = _parse_dt(_ls)
                if _ts is not None and time.time() - _ts > _RUNTIME_STALE_SECONDS:
                    continue
            else:
                # 从未心跳过（last_seen_at NULL/空）但 status=online 的幽灵行 → 不可达
                continue
            _healthy = r[1] if r[1] is not None else 0
            _probe = r[2] or ""
            if _healthy != 1:
                continue
            if _probe not in ("ok", "pending", ""):
                continue
            # 有 tag 能力？
            c.execute(
                "SELECT 1 FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                "AND (tag=? OR tag='all') LIMIT 1", (eid, tag))
            if not c.fetchone():
                continue
            # scope 匹配？
            c.execute(
                "SELECT rc.scope, rc.owner_id, rc.owner_name FROM agent_runtimes r "
                "LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id WHERE r.id=?", (eid,))
            sr = c.fetchone()
            if sr:
                _scope = sr[0] or "global"
                _owner = sr[1] or ""
                _owner_name = sr[2] or ""
                if _scope == "repo" and _owner:
                    _owner_norm = (from_repo_key(_owner) or "").strip()
                    if _repo_norm and _owner_norm != _repo_norm:
                        continue  # repo scope 但仓库不匹配
                elif _scope == "user" and task_user_id > 0 and _owner_name:
                    # user scope：引擎 owner_name 对应的 user_id 必须匹配 task.user_id
                    try:
                        c.execute("SELECT id FROM grape_accounts WHERE username=? AND id>0 LIMIT 1", (_owner_name,))
                        _ur = c.fetchone()
                        _engine_uid = _ur[0] if _ur else 0
                        if _engine_uid != task_user_id:
                            continue  # user scope 但用户不匹配
                    except Exception:
                        pass
            # 全部通过：有能干的引擎（物理可达）
            return True
        except Exception:
            continue
    return False




def _sync_event_failed(seq, reason: str, exit_code: int = 1, completed_at: str = ""):
    """任务判死 → 回写关联事件完整终态（Issue #369）。

    对齐 event_pipeline._fail_event 语义：status=failed + result=reason + exit_code +
    completed_at，事件侧具备完整诊断信息（此前判死路径只调 update_event_status，
    事件 result/exit_code/completed_at 全空，排障零信息）。
    幂等：update_event_complete 自带终态保护（terminal→terminal 跳过），
    双主 scp 同步场景下两侧重复回写不冲突、不覆盖。
    """
    if not seq:
        return
    try:
        from src.api.event_store import update_event_complete
        update_event_complete(
            seq, "failed", exit_code=exit_code,
            completed_at=completed_at or datetime.now(timezone.utc).isoformat(),
            result=(reason or "")[:200])
    except Exception as _e:
        log.warning(f"_sync_event_failed failed for seq={seq}: {_e}")


def _fail_task_pool_unreachable(c, task_id: str, engine_ids: list, pool_tag: str = ""):
    """Issue #253 改动2：绑定引擎全部物理不可达 → 任务诚实失败（宁失败，不跑飞）。

    claim / claim_by_tag 认领时发现 pool_engine_ids 内引擎全部不可达（离线/心跳过期/
    无 tag/scope 不符）时调用。满槽继续排队。替代原"清空 pool_engine_ids 放行全站"的降级。

    Lock contract：调用方必须已持有 db_lock（与 _check_pool_engines_reachable 相同）。
    c 为调用方传入的已持有锁事务的 cursor；本函数内部 commit 并同步事件终态。
    """
    from datetime import datetime, timezone
    _names = []
    try:
        for _eid in engine_ids:
            c.execute("SELECT name FROM agent_runtimes WHERE id=?", (int(_eid),))
            _r = c.fetchone()
            if _r and _r[0]:
                _names.append(_r[0])
    except Exception:
        pass
    _now = datetime.now(timezone.utc).isoformat()
    _reason = (f"绑定的 {len(engine_ids)} 台引擎（{', '.join(_names) or ', '.join(str(x) for x in engine_ids)}）"
               f"全部不可达（离线/失联/无对应标签/范围不匹配），任务停止派发"
               f"{'，标签=' + pool_tag if pool_tag else ''}（Issue #253）")
    try:
        c.execute(
            "UPDATE agent_tasks SET status='failed', result_text='', errors=?, "
            "completed_at=?, stream_ended=1 WHERE id=? AND status='queued'",
            (_reason, _now, task_id))
        if c.rowcount:
            db.commit()
            log.warning(f"task {task_id} failed: pool engines {engine_ids} all unreachable (Issue #253)")
            # 同步事件终态（模式同回收逻辑：防事件永远 running）；
            # Issue #369：走 _sync_event_failed 完整回写 result/exit_code/completed_at
            c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,))
            _es = c.fetchone()
            if _es and _es[0]:
                _sync_event_failed(_es[0], _reason, completed_at=_now)
    except Exception as _e:
        log.warning(f"_fail_task_pool_unreachable failed for task {task_id}: {_e}")
        try:
            db.rollback()
        except Exception:
            pass


def _fail_task_pool_unhealthy(c, task_id: str, engine_ids: list, waited_seconds: int):
    """Issue #253 改动3（方案 A）：绑定引擎物理可达但健康分不足 → 限时等待超时后诚实失败。

    _check_stale_queued_tasks 在 120s 窗口耗尽时调用。替代原"无限等待"死区
    （引擎活着但低分：健康分门禁拦它认领 + 可达即不撕证 = 永久 queued）。

    Lock contract：同 _fail_task_pool_unreachable，调用方已持有 db_lock。
    """
    from datetime import datetime, timezone
    _names = []
    try:
        for _eid in engine_ids:
            c.execute("SELECT name FROM agent_runtimes WHERE id=?", (int(_eid),))
            _r = c.fetchone()
            if _r and _r[0]:
                _names.append(_r[0])
    except Exception:
        pass
    _now = datetime.now(timezone.utc).isoformat()
    _name_str = ', '.join(_names) or ', '.join(str(x) for x in engine_ids)
    _reason = (f"绑定的 {len(engine_ids)} 台引擎（{_name_str}）在线但健康分不足，"
               f"等待 {waited_seconds}s 未恢复，任务停止派发（Issue #253）")
    try:
        c.execute(
            "UPDATE agent_tasks SET status='failed', result_text='', errors=?, "
            "completed_at=?, stream_ended=1 WHERE id=? AND status='queued'",
            (_reason, _now, task_id))
        if c.rowcount:
            db.commit()
            log.warning(f"task {task_id} failed: pool engines {engine_ids} unhealthy "
                        f"after {waited_seconds}s wait (Issue #253)")
            c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,))
            _es = c.fetchone()
            if _es and _es[0]:
                # Issue #369：完整回写事件诊断字段（result/exit_code/completed_at）
                _sync_event_failed(_es[0], _reason, completed_at=_now)
    except Exception as _e:
        log.warning(f"_fail_task_pool_unhealthy failed for task {task_id}: {_e}")
        try:
            db.rollback()
        except Exception:
            pass


def _check_pool_has_healthy(engine_ids: list) -> bool:
    """检查 pool_engine_ids 中是否有健康分达标的引擎（Issue #134 扩展）。

    与 _check_pool_engines_reachable 的区别：
    - reachable 只看物理可达性（在线/能力/scope/槽位），不看健康分
    - has_healthy 看健康分，用于防饿死降级判定

    返回 True = 池中有健康引擎，低分引擎应被 claim_by_tag 挡住
    返回 False = 池中全不健康，应触发超时降级（_check_stale_queued_tasks）
    """
    try:
        from src.runtime.health_scorer import is_healthy as _is_engine_healthy
    except Exception:
        return True  # 模块异常时不影响现有逻辑
    for eid in engine_ids:
        try:
            if _is_engine_healthy(eid):
                return True
        except Exception:
            continue
    return False


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


_RUNTIME_STALE_SECONDS = int(os.environ.get("RUNTIME_STALE_SECONDS", "100"))

# Issue #259：running 任务回收阈值——对齐 worker 本地 RUNTIME_TASK_TIMEOUT（默认 3600s）。
# 假死 worker（断连但本地任务还在跑）的 running 任务须等本地超时自杀后才服务端重派，
# 双跑窗口=0。前提：该值 >= worker 端 RUNTIME_TASK_TIMEOUT（部署注意）。
_TASK_RECOVER_STALE_SECONDS = int(os.environ.get("TASK_RECOVER_STALE_SECONDS", "3600"))

# Issue #259 Optional：启动期配置耦合告警——防双跑前提是 TASK_RECOVER_STALE_SECONDS >= worker RUNTIME_TASK_TIMEOUT。
# 二者默认均 3600s（安全），但部署侧单边调大 RUNTIME_TASK_TIMEOUT 而不调 TASK_RECOVER_STALE_SECONDS
# 会导致服务端在 worker 仍本地执行时重派 → 双跑。启动期告警一次，运行期不再重复。
_WORKER_TASK_TIMEOUT_DEFAULT = int(os.environ.get("RUNTIME_TASK_TIMEOUT", "3600"))
if _TASK_RECOVER_STALE_SECONDS < _WORKER_TASK_TIMEOUT_DEFAULT:
    log.warning(
        f"Config coupling risk: TASK_RECOVER_STALE_SECONDS={_TASK_RECOVER_STALE_SECONDS}s < "
        f"RUNTIME_TASK_TIMEOUT={_WORKER_TASK_TIMEOUT_DEFAULT}s — running tasks may be re-dispatched "
        f"before worker local timeout, causing double execution (Issue #259)"
    )


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
    "user_gitcode_token, "
    "env_extra, skill_files, server_url, issue_session_id, project_info_files, project_info_meta, "
    "knowledge_files, project_info_file_urls, knowledge_file_urls, attempts, pool_agent_type, affinity_runtime_id, pool_tag, pool_engine_ids, "
    "pool_engine_source, "
    "status, stream_ended, self_reported, "
    "exit_code, result_text, errors, stderr, "
    "usage_json, started_at, completed_at, created_at"
)

_TASK_COLUMNS_LIST = [c.strip() for c in _TASK_COLUMNS.split(",")]

# 双标记列：INSERT 时让 DB 默认（0）落地，select 时按表实际列过滤（兼容未迁移库）
_TASK_SIGNAL_COLUMNS = ("stream_ended", "self_reported")

_TABLE_COLS_CACHE: dict = {}
_TABLE_COLS_CACHE_DB_ID = id(db)   # db 对象身份标记（测试 monkeypatch 换库 → 缓存自动失效）


def _table_columns(table: str) -> set:
    """表实际存在的列（PRAGMA，进程内缓存）。

    迁移动态列后代码兼容未迁移的旧库（如测试库）与已迁移库。

    Lock contract（Issue #259 Optional）：调用方必须已持有 db_lock。
    本函数不再嵌套 with db_lock——生产 db_lock=RLock（可重入，嵌套无碍），
    但测试 monkeypatch 裸 Lock（非重入）时嵌套获取会自死锁。
    PRAGMA 只读 + 进程内缓存，无锁竞争风险。

    2026-10-09 缓存失效修复：测试连跑时 monkeypatch 换 db（真实库 39 列 →
    内存库 38 列），列缓存仍命中旧库清单 → no such column（顺序依赖假红）。
    以 db 对象身份（id()）为缓存键，db 变更即整体失效。
    """
    global _TABLE_COLS_CACHE_DB_ID
    if id(db) != _TABLE_COLS_CACHE_DB_ID:
        _TABLE_COLS_CACHE.clear()
        _TABLE_COLS_CACHE_DB_ID = id(db)
    _cached = _TABLE_COLS_CACHE.get(table)
    if _cached is not None:
        return _cached
    try:
        _rows = db.cursor().execute(f"PRAGMA table_info({table})").fetchall()
        _col = {r[1] for r in _rows}
    except Exception:
        _col = set()
    _TABLE_COLS_CACHE[table] = _col
    return _col


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
    """服务端 DB 系凭据注入（user credentials / 账户 AK-SK），远端 worker 无本地 DB 也生效。

    huawei_cloud AK/SK 注入为 HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK 环境变量，
    供 SRE agent (cloud-deploy skill) 通过 hcloud CLI 认证使用。
    codearts AK/SK 注入为 CODEARTS_CLI_AK / CODEARTS_CLI_SK。
    """
    env = {}
    if user_id and user_id > 0:
        try:
            from src.user.db import get_credentials
            for c in get_credentials(user_id):
                provider = c.get("provider", "")
                cfg = c.get("config", {})
                if provider == "huawei_cloud":
                    # 注入为 SRE cloud-deploy skill 期望的环境变量名
                    if cfg.get("ak"):
                        env["HUAWEICLOUD_SDK_AK"] = str(cfg["ak"])
                    if cfg.get("sk"):
                        env["HUAWEICLOUD_SDK_SK"] = str(cfg["sk"])
                    continue
                if provider == "codearts":
                    if cfg.get("ak"):
                        env["CODEARTS_CLI_AK"] = str(cfg["ak"])
                    if cfg.get("sk"):
                        env["CODEARTS_CLI_SK"] = str(cfg["sk"])
                    continue
                for k, v in cfg.items():
                    env[k.upper()] = str(v)
        except Exception:
            pass
    return env


_PROJECT_INFO_FILES = ("project-graph.md", "project-capabilities.md", ".metadata.json")


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
                    "AND archived=0 "
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
        from src.repo_key import workspace_repo_dir
        pi = os.path.join(workspace_repo_dir(repo_key), "project-info")
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


def load_project_info_with_obs(repo_key: str) -> tuple[dict, dict]:
    """Issue #95：和 load_project_info 同逻辑，但 files 值改为 {content, obs_key}。

    供 collect_knowledge_files → generate_file_urls 用，不暴露给前端。
    load_project_info 本身不变（repos.py / prompt_builder.py 不受影响）。
    """
    if not repo_key:
        return {}, {}
    try:
        from src.config import WORKSPACE_DIR
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)

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
                    "AND archived=0 "
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
                                files[fname] = {"content": data.decode("utf-8", errors="replace"), "obs_key": ok}
                        except Exception:
                            continue
            if files:
                meta = {}
                if ".metadata.json" in files:
                    try:
                        meta = json.loads(files[".metadata.json"]["content"])
                    except Exception:
                        meta = {}
                files[".metadata.json"] = {"content": json.dumps(meta, ensure_ascii=False), "obs_key": ""}
                return files, meta

        # 本地模式
        from src.repo_key import workspace_repo_dir
        pi = os.path.join(workspace_repo_dir(repo_key), "project-info")
        if not os.path.isdir(pi):
            return {}, {}
        files = {}
        for rel in _PROJECT_INFO_FILES:
            p = os.path.join(pi, rel)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        files[rel] = {"content": f.read(), "obs_key": ""}
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
            files[".metadata.json"] = {"content": json.dumps(meta, ensure_ascii=False), "obs_key": ""}
        return files, meta
    except Exception as e:
        log.warning(f"load_project_info_with_obs failed for {repo_key}: {e}")
        return {}, {}


def collect_knowledge_files(repo_key: str, agent_type: str = "", issue_id: str = "",
                            scene_id: str = "") -> dict:
    """Issue #95：统一过滤知识文件，返回三批 {rel: {content, obs_key}}。

    三源不交叉：
    - load_project_info_with_obs → baseline (source='system')
    - member_files_pi → member (source='manual')
    - agent_knowledge_files → agent (source='agent')

    enqueue_task 和 direct 物化都调此函数，过滤逻辑只写一遍。
    返回 {"project_info": {rel: {content, obs_key}}, "meta": dict, "knowledge": {rel: {content, obs_key}}}
    """
    from src.api.session_store import get_issue_scene
    from src.api.knowledge import member_files_pi, agent_knowledge_files

    _scene_id = scene_id
    if not _scene_id and issue_id:
        try:
            _scene_id, _ = get_issue_scene(repo_key, issue_id)
        except Exception:
            _scene_id = ""
    _inum = _issue_num(issue_id)

    # 1. baseline
    _pif, _pim = load_project_info_with_obs(repo_key)

    # 2. member（并入 baseline）
    try:
        for rel, meta in member_files_pi(repo_key, agent_type, _inum, _scene_id or "").items():
            _pif.setdefault(rel, meta)
    except Exception:
        pass

    # 3. agent 知识（独立）
    _know = {}
    try:
        _know = agent_knowledge_files(repo_key, agent_type=agent_type, issue_num=_inum, scene_id=_scene_id or "")
    except Exception:
        pass

    return {"project_info": _pif, "meta": _pim, "knowledge": _know}


def enqueue_task(*, agent_session_id, runtime_id, prompt, cwd="",
                 runtime_session_id="", event_type="", account="", user_id=0,
                 repo_gitcode_token="", user_gitcode_token="", env_extra=None,
                 event_seq=None, issue_id="", repo_key="", agent_type="",
                 skill="", skill_path="", skill_id=0, server_url="", issue_session_id="",
                 project_info_files=None, project_info_meta=None, knowledge_files=None,
                 project_info_file_urls=None, knowledge_file_urls=None,
                 pool_agent_type="", affinity_runtime_id=0, pool_tag="", pool_engine_ids="",
                 pool_engine_source=""):
    """业务层入队：确定路由（D3）+ 快照 + 技能打包 + 凭据注入。返回 task_id。

    引擎池（agent-cluster-design.md §4.1/§4.2）：runtime_id=0 + pool_agent_type 非空
    → 池调度任务，由池内任意空闲引擎 claim 时领取（claim 会把 runtime_id 定为实际引擎）。
    显式 runtime_id（>0）→ 确定路由，行为与现状一致。

    knowledge_files：共享知识快照（event-conversation-design.md §3.1.1），None 时自动打包；
    远端 worker 侧 materialize 到 cwd/.knowledge 并替换 {{KNOWLEDGE_DIR}}。

    skill_id：skill 表主键，优先于 skill_name 解析（避免 user/repo scope 的 owner_id 不匹配）。
    skill_id=0 时回退 resolve_skill_id 按名字解析（向后兼容）。

    pool_engine_source（local-claimer 设计）：pool_engine_ids 的来源标记——
    'scene' = 用户在场景里显式绑定（白名单全灭时保持 failed，Issue #253 硬约束）；
    'auto'  = 引擎自动选择/兜底产生的绑定（全灭时可降级放行全站，可审计）；
    ''      = 未标记（历史任务，按 scene 对待——保守不降级）。
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
                    _sid = resolve_skill_id(skill, repo_key=repo_key, user_id=user_id) or resolve_skill_id(skill)
                    _path = get_skill_path(_sid) if _sid else (skill_path or "")
                    _okey = get_skill_object_key(_sid) if _sid else ""
            else:
                # ③ 无 skill_id → 走老逻辑（向后兼容）
                _sid = resolve_skill_id(skill, repo_key=repo_key, user_id=user_id) or resolve_skill_id(skill)
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
    # Issue #95：统一调 collect_knowledge_files 过滤知识文件（三源不交叉，过滤只做一次）
    _collected = None
    if project_info_files is None or knowledge_files is None:
        try:
            _collected = collect_knowledge_files(repo_key, agent_type=agent_type, issue_id=issue_id)
            if project_info_files is None:
                project_info_files = {rel: v["content"] for rel, v in _collected["project_info"].items()}
                project_info_meta = _collected["meta"]
            if knowledge_files is None:
                knowledge_files = {rel: v["content"] for rel, v in _collected["knowledge"].items()}
        except Exception as e:
            log.warning(f"enqueue: collect_knowledge_files failed for {repo_key}: {e}")
            if project_info_files is None:
                project_info_files = {}
                project_info_meta = {}
            if knowledge_files is None:
                knowledge_files = {}
            _collected = {"project_info": {}, "knowledge": {}}
    # 生成 file_urls（基于已过滤结果，不查 DB；storage 为 None 时 file_urls 为空 dict，worker 用 content 兜底）
    try:
        from src.storage import get_storage
        _storage = get_storage()
    except Exception:
        _storage = None
    if _storage:
        # 复用 _collected（只在上面已调用过时复用；调用方传了内容时重新调一次取 obs_key）
        if _collected is None:
            try:
                _collected = collect_knowledge_files(repo_key, agent_type=agent_type, issue_id=issue_id)
            except Exception:
                _collected = {"project_info": {}, "knowledge": {}}
        _all_filtered = {}
        _all_filtered.update(_collected.get("project_info") or {})
        _all_filtered.update(_collected.get("knowledge") or {})
        _file_urls = _storage.generate_file_urls(_all_filtered) if _all_filtered else {}
    else:
        _file_urls = {}
    # 分离 URL：project_info_file_urls = baseline + member，knowledge_file_urls = agent
    project_info_file_urls = {k: v for k, v in _file_urls.items()
                              if k.startswith("member/") or k in _PROJECT_INFO_FILES}
    knowledge_file_urls = {k: v for k, v in _file_urls.items()
                           if not k.startswith("member/") and k not in _PROJECT_INFO_FILES}
    env = dict(env_extra or {})
    for k, v in _collect_server_env(user_id, account).items():
        env.setdefault(k, v)
    # Issue-45 M2：签发会话级临时 token 注入 GRAPE_SESSION_TOKEN（存在即有效，删除即失效）。
    # session_key=task_id（精确失效、不误伤并发同类会话）；chat 任务绑定 USER_ID 用户身份，
    # devops 事件任务无用户概念（user_id=0，仓库身份）。
    if repo_key and agent_type:
        try:
            from src.api.auth_token import issue_session_token
            _tk_uid = user_id or 0
            _usr = env.get("USER_ID") or ""
            if str(_usr).isdigit():
                _tk_uid = int(str(_usr))
            env["GRAPE_SESSION_TOKEN"] = issue_session_token(
                session_key=task_id, repo_key=repo_key,
                agent_type=agent_type, user_id=_tk_uid)
            # CLI 命令按 token 作用域过滤：本任务持 tk_ 会话 token → 只暴露 dual+session_only 命令
            # （见 grape-cli-mode-filter-design.md；chat 与 devops 任务均适用）
            env["GRAPE_CLI_MODE"] = "WORKER"
        except Exception as e:
            log.warning(f"enqueue: session token issue failed for {task_id}: {e}")
    _server_url = server_url or ""
    if not _server_url:
        try:
            from src.user.db import get_server_base_url
            _server_url = get_server_base_url()
        except Exception:
            pass
    with db_lock:
        c = db.cursor()
        _avail = _table_columns("agent_tasks")
        _insert_cols = [x for x in _TASK_COLUMNS_LIST if x not in _TASK_SIGNAL_COLUMNS and x in _avail]
        _insert_vals = {
            "id": task_id,
            "agent_session_id": agent_session_id,
            "runtime_id": runtime_id,
            "event_seq": event_seq,
            "issue_id": issue_id or "", "repo_key": repo_key or "",
            "agent_type": agent_type or "", "skill": skill or "", "skill_id": skill_id or 0,
            "prompt": prompt, "cwd": cwd or "", "runtime_session_id": runtime_session_id or "",
            "event_type": event_type or "", "account": account or "", "user_id": user_id or 0,
            "repo_gitcode_token": repo_gitcode_token or "", "user_gitcode_token": user_gitcode_token or "",
            "env_extra": json.dumps(env), "skill_files": json.dumps(skill_files), "server_url": _server_url,
            "issue_session_id": issue_session_id or "",
            "project_info_files": json.dumps(project_info_files), "project_info_meta": json.dumps(project_info_meta),
            "knowledge_files": json.dumps(knowledge_files),
            "project_info_file_urls": json.dumps(project_info_file_urls), "knowledge_file_urls": json.dumps(knowledge_file_urls),
            "attempts": 0, "pool_agent_type": pool_agent_type or "", "affinity_runtime_id": affinity_runtime_id or 0,
            "pool_tag": pool_tag or "", "pool_engine_ids": pool_engine_ids or "",
            "pool_engine_source": pool_engine_source or "",
            "status": "queued", "exit_code": None, "result_text": "", "errors": "", "stderr": "",
            "usage_json": "{}", "started_at": None, "completed_at": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        c.execute(
            f"INSERT INTO agent_tasks ({', '.join(_insert_cols)}) VALUES ({','.join(['?'] * len(_insert_cols))})",
            [_insert_vals[x] for x in _insert_cols],
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
                    "SELECT id, runtime_id, event_seq, attempts FROM agent_tasks "
                    "WHERE status='dispatched' AND started_at < ? AND started_at IS NOT NULL",
                    (_stale,))
                _stale_rows = c.fetchall()
                _actually_recovered = []
                for _tid, _rid, _seq, _attempts in _stale_rows:
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
                    # 2026-10-20 MySQL attempts 语义修复（与 runtime.py recover-orphans
                    # 同款病根）：原写法 SET attempts=attempts+1 且 CASE 里 attempts+1
                    # —— MySQL 后赋值读新值（等效 +2），SQLite 读旧值，两库分叉导致
                    # 封顶轮数不一致。此处判死后有 SELECT status 复核（1058-1061 行）
                    # 所以事件不会悬空，但语义仍需统一。参数化传 _att 强制一致。
                    _att = (_attempts or 0) + 1
                    _STALE_RECOVER_MAX_ATTEMPTS = 50
                    c.execute(
                        "UPDATE agent_tasks SET attempts=?, "
                        "status=CASE WHEN ? >= ? THEN 'failed' ELSE 'queued' END, "
                        "runtime_id=CASE WHEN ? >= ? THEN runtime_id ELSE 0 END, "
                        "pool_tag=CASE WHEN ? < ? AND pool_tag='' "
                        "THEN ? ELSE pool_tag END, "
                        "pool_agent_type=CASE WHEN ? < ? AND pool_agent_type='' "
                        "THEN ? ELSE pool_agent_type END, "
                        "started_at=NULL, "
                        "result_text=CASE WHEN ? >= ? THEN '回收超限(dispatched 超时 50 次)' ELSE result_text END "
                        "WHERE id=? AND status='dispatched'",
                        (_att,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS, _pool_fill,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS, _pool_fill,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS, _tid))
                    if c.rowcount and _rid:
                        release_busy_slot(_rid, cursor=c)
                    # 回收置 failed（attempts 超限）→ 同步事件终态，避免事件永远 running；
                    # Issue #369：完整回写事件诊断字段（result/exit_code/completed_at）
                    if c.rowcount and _seq:
                        c.execute("SELECT status FROM agent_tasks WHERE id=?", (_tid,))
                        _after = c.fetchone()
                        if _after and _after[0] == "failed":
                            _sync_event_failed(
                                _seq,
                                "回收超限：dispatched 超时回收 50 次仍无有效认领，任务判死")
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
                            # 兜底：检查 pool_engine_ids 中的引擎是否全部不可达
                            # 不可达 = 离线 / 无对应 tag 能力 / scope 不匹配
                            # 全部不可达时清空 pool_engine_ids，让 task 可被任意引擎认领
                            _task_tag = ""
                            _task_repo = ""
                            _task_uid = 0
                            try:
                                c.execute("SELECT pool_tag, repo_key, user_id FROM agent_tasks WHERE id=?", (task_id,))
                                _trow = c.fetchone()
                                if _trow:
                                    _task_tag = _trow[0] or ""
                                    _task_repo = _trow[1] or ""
                                    _task_uid = _trow[2] or 0
                            except Exception:
                                pass
                            _has_capable = _check_pool_engines_reachable(_allowed, _task_tag, _task_repo, task_user_id=_task_uid)
                            if not _has_capable:
                                # Issue #253 改动2：绑定引擎全部物理不可达 → 宁失败不跑飞（同 claim_by_tag）。
                                # 原行为：清空 pool_engine_ids 放行任意引擎（池外全局 worker 抢任务）。
                                # 新行为：任务置 failed + 人话原因，保留 pool_engine_ids，同步事件终态。
                                _fail_task_pool_unreachable(c, task_id, _allowed, _task_tag)
                                return None
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
                "UPDATE agent_tasks SET status='running', runtime_id=?, started_at=? WHERE id=? AND status='queued'",
                (runtime_id, datetime.now(timezone.utc).isoformat(), task_id))
            claimed = c.rowcount > 0
            if claimed:
                # 方案 D：展示快照重算（真实 active）；决策点已改读 _real_active_count，本段旧对账/递增不再承担正确性。
                refresh_busy_snapshot(runtime_id, cursor=c)
                # 方案 E：真正被引擎认领才把事件置 running（入队时是 pending）
                try:
                    _evt_seq = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                    if _evt_seq and _evt_seq[0]:
                        c.execute(
                            "UPDATE events SET status='running', completed_at='' "
                            "WHERE seq=? AND status IN ('pending','running')",
                            (_evt_seq[0],))
                except Exception as _ee:
                    log.warning(f"claim: sync event running failed for task {task_id}: {_ee}")
            db.commit()
        except Exception as e:
            log.warning(f"claim failed: {e}")
            db.rollback()
            return None
        if not claimed:
            return None
        task = get_task(task_id)
        if task:
            task["status"] = "running"
            # session 引擎守卫（worker 兜底层）：任务带的续接指针若由异构引擎产出（如
            # 入队时广播模式未知引擎，或存量脏指针），claim 已确定实际引擎 → 比对
            # issue_agent_session.session_engine，跨引擎则置空指针（引擎开新会话），
            # 防 hermes resume opencode 会话号静默挂死。引擎名未知 → 放行（fail-safe）。
            try:
                _isid = task.get("issue_session_id") or ""
                _sid = task.get("runtime_session_id") or ""
                if _isid and _sid:
                    _eng_name = ""
                    with db_lock:
                        c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
                        _er = c.fetchone()
                        _eng_name = (_er[0] or "") if _er else ""
                    with db_lock:
                        c.execute("SELECT session_engine FROM issue_agent_session WHERE id=?", (_isid,))
                        _sr = c.fetchone()
                    _ptr_engine = (_sr[0] or "") if _sr else ""
                    if _eng_name and _ptr_engine and _ptr_engine != _eng_name:
                        task["runtime_session_id"] = ""
                        log.info(f"claim: drop cross-engine session pointer "
                                 f"(ptr={_ptr_engine} engine={_eng_name} task={task_id})")
            except Exception as e:
                log.warning(f"claim session engine guard failed: {e}")
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
        _cols = [x for x in _TASK_COLUMNS_LIST if x in _table_columns("agent_tasks")]
        c.execute(f"SELECT {', '.join(_cols)} FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    return dict(zip(_cols, row)) if row else None


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

            # 2. 引擎级并发槽检查（方案一：标签只做路由，并发纯引擎级）
            # 废弃 engine_capabilities.max_concurrent，统一走 agent_runtimes.max_concurrent
            # 方案 D（2026-10-18）：满载判定以真实 running/dispatched COUNT 为事实源（covering 索引），
            # 不再读 busy_slots 列——busy_slots 仅为展示快照，僵尸残留不阻塞认领。
            c.execute(
                "SELECT max_concurrent FROM agent_runtimes WHERE id=?",
                (runtime_id,)
            )
            rt_row = c.fetchone()
            if rt_row and rt_row[0] and _real_active_count(c, runtime_id) >= rt_row[0]:
                return None  # 引擎已满载（真实 active >= max_concurrent）

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
                c.execute("SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?)", (owner_name,))  # D32-补丁 #348: case-insensitive
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
                            # 兜底：检查 pool_engine_ids 中的引擎是否全部不可达
                            # 不可达 = 离线 / 无对应 tag 能力 / scope 不匹配
                            # 全部不可达时清空 pool_engine_ids，让 task 可被任意引擎认领
                            _task_tag = ""
                            _task_repo = ""
                            _task_uid = 0
                            try:
                                c.execute("SELECT pool_tag, repo_key, user_id FROM agent_tasks WHERE id=?", (task_id,))
                                _trow = c.fetchone()
                                if _trow:
                                    _task_tag = _trow[0] or ""
                                    _task_repo = _trow[1] or ""
                                    _task_uid = _trow[2] or 0
                            except Exception:
                                pass
                            _has_capable = _check_pool_engines_reachable(_allowed, _task_tag, _task_repo, task_user_id=_task_uid)
                            if not _has_capable:
                                # Issue #253 改动2：绑定引擎全部物理不可达 → 宁失败不跑飞。
                                # 原行为：清空 pool_engine_ids 放行任意引擎（池外全局 worker 抢任务）。
                                # 新行为：任务置 failed + 人话原因，保留 pool_engine_ids，同步事件终态。
                                _fail_task_pool_unreachable(c, task_id, _allowed, _task_tag)
                                return None
                            _skip = True
                    except Exception:
                        pass
                if not _skip:
                    # === 健康分检查（Plugin 模式增强层，Issue #134 扩展）===
                    # 引擎健康分过低或连续失败过多时拒绝认领，避免低质量引擎抢任务后失败。
                    # Issue #259/seq#2310：force_refresh=True 现算，避免读 5min 前持久化的 0 分
                    # 快照长期阻塞本机在线引擎；不健康 → 跳过当前任务尝试下一个（防队首阻塞）。
                    # 健康检查异常时保守跳过本轮认领，不放行低分引擎。
                    try:
                        from src.runtime.health_scorer import is_healthy as _is_engine_healthy
                        if not _is_engine_healthy(runtime_id, force_refresh=True):
                            from src.runtime.instances import runtime_instance_name as _rin2
                            log.info(f"claim_by_tag: engine {runtime_id}({_rin2(runtime_id)}) unhealthy (low score/consecutive failures), skip claiming task {task_id}")
                            _skip = True
                    except Exception as _he:
                        log.warning(f"claim_by_tag: health check failed for engine {runtime_id}: {_he}, reject this round")
                        _skip = True
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

            # 标记 running（原 dispatched：worker 认领即开始执行，UI 长时间显示 dispatched
            # 是因为 worker 侧 task_start 回调丢失/批量消息延迟，靠 auto-fix 兜底才转 running。
            # 认领即 running 语义更准确，防 UI 卡 dispatched 窗口）
            c.execute(
                "UPDATE agent_tasks SET status='running', runtime_id=?, started_at=? WHERE id=? AND status='queued'",
                (runtime_id, datetime.now(timezone.utc).isoformat(), task_id)
            )
            claimed = c.rowcount > 0
            if claimed:
                # 方案 D：busy_slots 降级为展示快照，这里重算为真实 active 数（决策已用 _real_active_count）
                refresh_busy_snapshot(runtime_id, cursor=c)
                # 方案 E：真正被引擎认领才把事件置 running（入队时是 pending）
                try:
                    _evt_seq = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                    if _evt_seq and _evt_seq[0]:
                        c.execute(
                            "UPDATE events SET status='running', completed_at='' "
                            "WHERE seq=? AND status IN ('pending','running')",
                            (_evt_seq[0],))
                except Exception as _ee:
                    log.warning(f"claim_by_tag: sync event running failed for task {task_id}: {_ee}")
            db.commit()
        except Exception as e:
            log.warning(f"claim_by_tag failed: {e}")
            db.rollback()
            return None
    if not claimed:
        return None
    task = get_task(task_id)
    if task:
        task["status"] = "running"
        # session 引擎守卫（worker 兜底层，与 claim() 同款）：广播任务 claim 时才确定
        # 实际引擎 → 比对 issue_agent_session.session_engine，跨引擎置空指针（引擎开新
        # 会话），防 hermes resume opencode 会话号静默挂死。引擎未知 → 放行（fail-safe）。
        try:
            _isid = task.get("issue_session_id") or ""
            _sid = task.get("runtime_session_id") or ""
            if _isid and _sid:
                _eng_name = ""
                with db_lock:
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
                    _er = c.fetchone()
                    _eng_name = (_er[0] or "") if _er else ""
                with db_lock:
                    c.execute("SELECT session_engine FROM issue_agent_session WHERE id=?", (_isid,))
                    _sr = c.fetchone()
                _ptr_engine = (_sr[0] or "") if _sr else ""
                if _eng_name and _ptr_engine and _ptr_engine != _eng_name:
                    task["runtime_session_id"] = ""
                    log.info(f"claim_by_tag: drop cross-engine session pointer "
                             f"(ptr={_ptr_engine} engine={_eng_name} task={task_id})")
        except Exception as e:
            log.warning(f"claim_by_tag session engine guard failed: {e}")
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


_BUSY_SLOT_DECREMENT_SQL = (
    "UPDATE agent_runtimes SET busy_slots = "
    "CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?"
)


def _real_active_count(cursor, runtime_id: int) -> int:
    """该引擎当前真实 running/dispatched 任务数（方案 D 唯一事实源）。

    local-engine-busy-slots-zombie-fix-design.md §十：决策点一律以 agent_tasks 真实计数为准，
    依赖 covering 索引 idx_agent_tasks_runtime_status 走纯索引扫描（COVERING INDEX），
    成本与当前 running 数同量级，与历史行数解耦。
    """
    cursor.execute(
        "SELECT COUNT(*) FROM agent_tasks "
        "WHERE runtime_id=? AND status IN ('running','dispatched')", (runtime_id,))
    r = cursor.fetchone()
    return r[0] if r else 0


def refresh_busy_snapshot(runtime_id: int, cursor=None):
    """把 busy_slots 列重算为真实 active 数（展示快照，不参与调度决策）。

    方案 D：busy_slots 降级为展示快照，由调度周期 / claim 路径调用本函数刷新；
    任一递增/递减路径漏调都不影响决策正确性（决策读 _real_active_count）。
    """
    if not runtime_id:
        return
    if cursor is not None:
        _n = _real_active_count(cursor, runtime_id)
        cursor.execute("UPDATE agent_runtimes SET busy_slots=? WHERE id=?", (_n, runtime_id))
        return
    from src.session_manager import db as _db, db_lock as _dl
    with _dl:
        _n = _real_active_count(_db.cursor(), runtime_id)
        _db.cursor().execute("UPDATE agent_runtimes SET busy_slots=? WHERE id=?", (_n, runtime_id))
        _db.commit()


def release_busy_slot(runtime_id, cursor=None):
    """统一释放调度槽（唯一递减入口，防越界 clamp >=0）。

    - cursor 为空：自行获取 db_lock + commit（独立事务）；
    - 传入 cursor：复用调用方事务（与任务状态更新同事务，避免嵌套锁）。
    所有「任务不再占用某引擎槽位」的路径都必须经此收敛
    （complete/fail/cancel/requeue/redispatch/孤儿回收），
    避免僵尸 busy_slots 让 local_claimer/claim_by_tag 误判满载（#256）。
    """
    if not runtime_id:
        return
    if cursor is not None:
        cursor.execute(_BUSY_SLOT_DECREMENT_SQL, (runtime_id,))
        return
    from src.session_manager import db as _db, db_lock as _dl
    with _dl:
        _db.cursor().execute(_BUSY_SLOT_DECREMENT_SQL, (runtime_id,))
        _db.commit()


def validate_run_token(task: dict | None, run_token: str = "") -> str:
    """校验补报实例（run_token = 认领时刻 started_at，Step 4）。

    返回值：
    - "ok"   ：可继续处理（任务处于 running/dispatched，且 started_at 匹配）
    - "noop" ：已终态（幂等，调用方照旧走终态保护即可）
    - "stale"：旧执行实例补报（started_at 不匹配，或任务已回炉/未处于活动态）
      → 调用方应拒绝，防止 recover_orphans 回炉重跑后旧实例误 complete 新执行。
    """
    if not task or not run_token:
        return "ok"
    if task.get("started_at") != run_token:
        return "stale"
    _st = task.get("status")
    if _st in ("completed", "failed", "cancelled"):
        return "noop"
    if _st not in ("running", "dispatched"):
        return "stale"  # queued（回炉未认领）/ 其它 → 旧实例
    return "ok"


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
        # 方案 E：任务真正开始执行（running）→ 事件 pending→running（与 claim 认领语义一致）
        if status == "running":
            try:
                _evt_seq = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                if _evt_seq and _evt_seq[0]:
                    c.execute(
                        "UPDATE events SET status='running', completed_at='' "
                        "WHERE seq=? AND status IN ('pending','running')",
                        (_evt_seq[0],))
            except Exception as _e:
                log.warning(f"update_task_status: sync event running failed for task {task_id}: {_e}")
        # 槽位释放（agent-cluster-design.md §4.2.1）：非终态 → 终态，且任务已绑定引擎 → busy_slots-1
        if status in ("completed", "failed", "cancelled") and row[1] and row[1] > 0:
            release_busy_slot(row[1], cursor=c)
        db.commit()
    # Issue-45 M2：任务终态 → 会话级临时 token 即时失效（存在即有效，删除即失效）
    if status in ("completed", "failed", "cancelled"):
        try:
            from src.api.auth_token import revoke_session_token
            revoke_session_token(task_id)
        except Exception as e:
            log.warning(f"update_task_status: revoke session token failed for {task_id}: {e}")
        # Issue #134：任务终态后刷新健康度缓存（增强层，不改派发/广播语义）
        try:
            from src.runtime.health_scorer import record_task_completion
            record_task_completion(row[1], status)
        except Exception:
            pass


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
            release_busy_slot(row[0], cursor=c)
        # 方案 E：任务回落 queued → 事件同步回 pending（语义：尚未被认领）
        if ret:
            try:
                _evt_seq = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                if _evt_seq and _evt_seq[0]:
                    c.execute(
                        "UPDATE events SET status='pending' WHERE seq=? AND status IN ('running','pending')",
                        (_evt_seq[0],))
            except Exception as _e:
                log.warning(f"requeue_task: sync event pending failed for task {task_id}: {_e}")
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
    # weekly-05：执行消息同时落 events.agent_logs（详情页「处理记录」，替代 event.json logs 依赖）
    if task.get("event_seq"):
        try:
            import json as _json
            _logs = [{"type": m.get("type", "log"),
                      "data": m.get("data"),
                      "ts": datetime.now(timezone.utc).isoformat()}
                     for m in (messages or []) if isinstance(m, dict)]
            if _logs:
                from src.session_manager import db as _edb, db_lock as _edbl
                with _edbl:
                    _c2 = _edb.cursor()
                    _c2.execute(
                        "SELECT agent_logs FROM events WHERE seq=?",
                        (task["event_seq"],))
                    _row = _c2.fetchone()
                    _prev = []
                    try:
                        _prev = _json.loads(_row[0]) if (_row and _row[0]) else []
                    except Exception:
                        _prev = []
                    _c2.execute(
                        "UPDATE events SET agent_logs=? WHERE seq=?",
                        (_json.dumps(_prev + _logs, ensure_ascii=False), task["event_seq"]))
                    _edb.commit()
        except Exception as e:
            log.warning(f"report_messages events.agent_logs failed: {e}")
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

    只解析结构化 JSON 输出的 conclusion 字段做失败关键词匹配。
    返回 True 表示业务失败（应覆盖 exit_code 为非 0）。

    仅对 sre agent_type 生效（部署类任务），其他 agent 不受影响。

    作用域收紧（issue 内部误判案例教训）：
    - 旧逻辑拼接 conclusion+next_step+issue_summary 全文匹配，且无结构化输出时
      降级扫整个 result_text；长距正则 `部署.*失败` 会跨整句把否定语境
      （"部署验证通过：唯一失败用例为基线既有失败（非本 PR 引入）"）连起来，
      把成功的任务误标 failed。
    - 现只扫 conclusion 单字段：它是 agent 对任务结果的一句定性结论，
      表述短、不含过程叙述，长距正则的跨句连词风险被结构性消除；
      next_step/issue_summary/result_text 里的过程性描述不再参与判定。
    - 长距 `.*` 模式一并收紧为短窗口，防止 conclusion 内部仍出现跨从句连词。
    """
    if agent_type != "sre":
        return False
    if not output:
        # 无结构化输出 → 不做业务失败判定（宁缺勿滥：检测面收窄到 conclusion，
        # 降级扫 result_text 的路径删除——那是 内部误判案例的主通道）
        return False
    blob = output.get("conclusion", "") or ""
    if not blob:
        return False
    import re
    # 失败关键词：部署中止/失败、无法购买/创建、未认证、凭证不足、部署未成功等
    # （短窗口：部署后最多隔 2 个常见连接字，不再跨句/跨从句连词）
    failure_patterns = [
        r"部署[了中于]{0,2}失败", r"部署[了中于]{0,2}中止", r"部署[了中于]{0,2}未成功",
        r"部署[了中于]{0,2}无法",
        r"无法购买", r"无法创建", r"无法部署",
        r"未完成.{0,4}认证", r"未实名", r"实名认证",
        r"凭证.{0,4}(?:不足|缺失|无效)",
        r"Ecs\\.0707", r"AccessDenied", r"auth.{0,8}fail",
        r"操作失败", r"执行失败",
        r"无法继续", r"中止\b",
    ]
    for pat in failure_patterns:
        if re.search(pat, blob, re.I):
            return True
    return False


def premature_end_marker() -> str:
    """「疑似提前结束」标记（唯一来源：适配器 src/agent/hermes_acp.py）。

    懒加载转发，避免 runtime.service 与 agent 适配器在导入期互相牵扯；适配器
    不可用（未安装/裁剪）时回退到字面量，保证标记语义不变。
    """
    try:
        from src.agent.hermes_acp import PREMATURE_END_MARKER as _pm
        return _pm
    except Exception:
        return "[PREMATURE_END_TURN]"


def complete_task(task_id, result_text, exit_code=0, usage=None):
    """完成：解析结构化 output → 业务失败检测 → finalize_event_archive → update_event_complete → 终态。"""
    task = get_task(task_id)
    # 可见性（P1）：引擎「疑似提前结束」（模型输出后静默、无收尾记录）时任务仍算
    # completed（exit 0），但把标记显式落到 agent_tasks.errors + 事件 result 前缀，
    # 让「跑绿了但可能没干完」在任务详情/事件列表里一眼可见。
    # 只做可见性：不降 failed、不触发重跑（重跑需幂等约束，另行评估）。
    _pm = premature_end_marker()
    _premature = bool(_pm) and _pm in (result_text or "")
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

            # exit_code=0 但无结构化产出 → 接受 hermes 的 end_turn 结果，直接 completed
            # （旧逻辑会 requeue 最多49次，导致重复评论污染；现改为不重试）

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
            _result_show = _conclusion or (result_text or "")[:200]
            if _premature:
                # 事件列表/详情直接看到「没走到收尾」，不必翻完整 result_text
                _result_show = f"⚠️ {_pm} 疑似提前结束（未走到收尾）：{_result_show}"
            update_event_complete(task["event_seq"], status, exit_code=exit_code,
                                  duration=duration,
                                  completed_at=datetime.now(timezone.utc).isoformat(),
                                  result=_result_show,
                                  structured_output=output or None)
            # Issue #51: 最后节点完成后 → 设 stage=pending_review 等待开发者审核
            # 统一处理：检查 agent 是否是 pipeline 最后节点（非 done 的 next=done）
            if status == "completed" and task.get("issue_id"):
                try:
                    _ag = task.get("agent_type") or ""
                    _rk = task.get("repo_key") or ""
                    _iid = task.get("issue_id") or ""
                    if _ag and _rk and _iid:
                        from src.api.session_store import get_issue_stage, get_issue_scene, set_issue_stage
                        from src.api.pipeline_config import get_pipeline_config
                        _st = get_issue_stage(_rk, _iid)
                        _cur_stage = (_st or {}).get("stage", "") if _st else ""
                        if _cur_stage == _ag:  # 当前 stage 就是完成的 agent → 检查是否最后节点
                            _scene_tuple = get_issue_scene(_rk, _iid)
                            _scene_id = (_scene_tuple[0] if _scene_tuple and _scene_tuple[0] else "") or "default"
                            _pipe_cfg = get_pipeline_config(_scene_id, repo_key=_rk)
                            _nodes = (_pipe_cfg or {}).get("nodes", [])
                            for _n in _nodes:
                                if _n.get("id") == _ag and _n.get("next") == "done":
                                    set_issue_stage(_rk, _iid, "pending_review", _ag)
                                    log.info(f"Last node {_ag} completed → pending_review issue={_iid}")
                                    break
                except Exception as _e:
                    log.warning(f"complete_task: pending_review check failed: {_e}")
        except Exception as e:
            log.warning(f"complete_task finalize failed: {e}")
    # 疑似提前结束：标记落到 agent_tasks.errors（任务详情可见）；状态仍是 completed。
    # 非疑似场景不传 errors，避免覆盖 fail 路径写下的真实错误信息。
    _err_kw = ({"errors": json.dumps(
        [f"{_pm} 未检测到收尾记录（actions/conclusion），疑似提前结束；工作可能未完成"],
        ensure_ascii=False)} if _premature else {})
    update_task_status(task_id, "completed" if exit_code == 0 else "failed",
                       exit_code=exit_code, result_text=result_text or "",
                       usage_json=json.dumps(usage or {}), **_err_kw)
    # 新会话续接指针回写——仅写回 Issue 级执行会话
    # repo 级 devops_agent_session.runtime_session_id 已废弃（无消费方），不再回写。
    # 引擎守卫（B2）：usage.engine 为本次执行引擎（worker done 事件带上），回写时
    # 连同 session_engine 一起覆盖为本次引擎（最新产出者权威），消费端守卫比对用。
    # 陈旧异构指针不跳过而是直接覆盖——跳过会让坏指针永久滞留表里。
    _new_sid = (usage or {}).get("session_id") or ""
    _done_engine = (usage or {}).get("engine") or ""
    if _new_sid:
        try:
            from src.api.session_store import update_issue_session
            if task and task.get("issue_session_id"):
                update_issue_session(task["issue_session_id"], runtime_session_id=_new_sid,
                                     session_engine=_done_engine)
        except Exception as e:
            log.warning(f"complete_task update issue session failed: {e}")
    log.info(f"Task completed: {task_id} exit={exit_code}")
    # remote worker 模式下 _execute_agent 不执行，系统回帖 + 会话同步在此补触发
    if exit_code == 0 and task and task.get("issue_id"):
        try:
            from src.api.event_pipeline import (
                _post_result_to_issue, _issue_num_from,
                _sync_issue_comments_to_conversation,
                _post_subtask_completion_to_parent, _post_all_subtasks_done,
            )
            from src.repo_key import from_repo_key
            _issue_id = task["issue_id"]
            _issue_num = _issue_num_from(_issue_id)
            _repo = from_repo_key(task["repo_key"]) or task.get("account") or ""
            _token = task.get("repo_gitcode_token") or ""
            _agent_type = task.get("agent_type") or ""
            _repo_key = task["repo_key"]
            _result_text = result_text or ""
            # 0. 多任务模式：父 Issue Agent 完成 → stage=done
            if _agent_type == "issue":
                try:
                    from src.api.session_store import is_subtask_mode_enabled, set_issue_stage
                    if is_subtask_mode_enabled(_repo_key, _issue_id):
                        set_issue_stage(_repo_key, _issue_id, "done", "issue")
                        log.info(f"Multi-task mode: parent issue {_issue_id} stage=done (subtasks will trigger independently)")
                except Exception as _se:
                    log.warning(f"complete_task multi-task stage=done failed: {_se}")
            # 1. 系统回帖（含 grape-auto-comment 标记）
            if _issue_num and _repo and _token:
                _post_result_to_issue(_repo, _issue_num, _result_text, _token, _agent_type)
            # 2. 子任务 SRE 完成 → 登记到父 Issue
            if _agent_type == "sre" and _issue_num and _token:
                try:
                    from src.api.session_store import get_parent_issue, get_subtask_issues
                    _parent = get_parent_issue(_repo_key, _issue_id)
                    if _parent:
                        _parent_num = _issue_num_from(_parent)
                        _sub_num = _issue_num
                        if _parent_num and _sub_num and _repo:
                            _post_subtask_completion_to_parent(_repo, _sub_num, _parent_num, _result_text, _token)
                            _subs = get_subtask_issues(_repo_key, _parent)
                            _all_done = all(s.get("stage") == "done" or s.get("issue_id") == _issue_id for s in _subs)
                            if _all_done and len(_subs) > 0:
                                _post_all_subtasks_done(_repo, _parent_num, _subs, _token)
                except Exception as _se:
                    log.warning(f"complete_task subtask post failed: {_se}")
            # 3. 同步结果到任务会话
            if _issue_num and _result_text:
                try:
                    from src.session_manager import db as _db, db_lock as _dl, add_message as _am
                    with _dl:
                        _c = _db.cursor()
                        _c.execute("SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
                                   (_repo_key, _issue_num))
                        _row = _c.fetchone()
                    if _row and _row[0]:
                        _am(_row[0], "agent", f"[{_agent_type.upper()} Agent] {_result_text[:3000]}")
                except Exception as _se:
                    log.warning(f"complete_task sync to conversation failed: {_se}")
            # 4. 拉取 Issue 评论同步到会话
            if _issue_num and _repo and _token:
                try:
                    _sync_issue_comments_to_conversation(_repo_key, _issue_num, _token, _agent_type, _repo)
                except Exception as _se:
                    log.warning(f"complete_task sync issue comments failed: {_se}")
            # 5. VOD 智能推荐完成 → redispatch（remote worker 模式下 _execute_agent 不执行，此处补触发）
            if _agent_type == "vod" and _result_text and _issue_id:
                try:
                    _handle_vod_recommend_complete(_repo_key, _issue_id, _result_text, task_id, task, _token)
                except Exception as _se:
                    log.warning(f"complete_task vod recommend handler failed: {_se}")
        except Exception as _pe:
            log.warning(f"complete_task post-completion hooks failed: {_pe}")
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


# ===== 双重标记完成（issue-219-task-complete-double-signal-design.md）=====

def set_task_signal(task_id: str, signal: str, result_text: str = "") -> bool:
    """条件置双标记之一（stream_ended=worker 隐式 / self_reported=agent 显式）。

    - 仅当标记未置时置 1（重放安全）；置 `self_reported` 时同步写入 result_text（防值丢失）。
    - 返回 True = 本次调用是第一个落该标记的；调用方据此读取另一标记，决定是否 finalize。
    """
    if signal not in ("stream_ended", "self_reported"):
        return False
    try:
        with db_lock:
            c = db.cursor()
            if signal == "self_reported":
                c.execute("UPDATE agent_tasks SET self_reported=1, result_text=? "
                          "WHERE id=? AND self_reported=0", (result_text or "", task_id))
            else:
                c.execute("UPDATE agent_tasks SET stream_ended=1 WHERE id=? AND stream_ended=0", (task_id,))
            _first = c.rowcount > 0
            db.commit()
        return _first
    except Exception as e:
        log.warning(f"set_task_signal({signal}) failed for {task_id}: {e}")
        return False


def finalize_by_stream_end(task_id: str, fallback_text: str = "", exit_code: int = 0,
                           usage: dict | None = None) -> bool:
    """单信号收尾（stream-end-finalize-single-signal-design.md）：引擎流结束即任务终态。

    不再区分「自报/普通」技能、不再等双信号；结构化 self_report 只是产物而非闸门：
    - Agent 已显式 self_report 且存了结构化 JSON → 以其为权威结论；
    - 否则回落调用方传入的流/引擎文本。
    complete_task 负责全部落库副作用（事件、结果、回帖、stage、token 延迟失效）。
    已终态 → 幂等返回 False（补挂走 enrich_task_on_late_report）。
    """
    task = get_task(task_id)
    if not task:
        return False
    if task.get("status") in ("completed", "failed", "cancelled"):
        return False
    result_text = fallback_text or ""
    if task.get("self_reported") == 1 and task.get("result_text"):
        _stored = task.get("result_text") or ""
        try:
            import json as _j
            _d = _j.loads(_stored)
            if isinstance(_d, dict) and (_d.get("actions") or _d.get("conclusion")):
                result_text = _stored
        except Exception:
            pass
    complete_task(task_id, result_text, exit_code, usage=usage or {})
    return True


def enrich_task_on_late_report(task_id: str, result_text: str = "") -> str:
    """单信号下「流结束后才到达的显式 self_report」幂等补挂（宽限窗内，token 仍有效）。

    返回：attached（completed 补挂结构化）/ promoted（failed 占位提升 completed）/ already / noop。
    守卫：仅本任务、仅结构化结论、仅未落库的缺位；failed 提升仅限「no-output 占位失败」
    （exit_code in (0, NULL)），真实 crash（exit_code!=0）不提升。
    只改结果存储（task/event 的 structured_output/result），不重放任何副作用（评论/回帖）。
    """
    task = get_task(task_id)
    if not task:
        return "noop"
    status = task.get("status") or ""
    if status not in ("completed", "failed") or task.get("self_reported") != 1:
        return "noop"
    _raw = result_text or (task.get("result_text") or "")
    structured = None
    try:
        import json as _j
        _d = _j.loads(_raw)
        if isinstance(_d, dict) and (_d.get("actions") or _d.get("conclusion")):
            structured = _raw
    except Exception:
        structured = None
    if structured is None:
        return "noop"

    _event_seq = task.get("event_seq")
    if status == "completed":
        if _event_seq:
            # 已落 event 结构化 → 幂等 already
            try:
                with db_lock:
                    _r = db.cursor().execute(
                        "SELECT structured_output FROM events WHERE seq=?", (_event_seq,)).fetchone()
                if _r and (_r[0] or "").strip() not in ("", "null"):
                    return "already"
            except Exception:
                pass
            _set_task_late_result(task_id, task, structured, status="completed")
            return "attached"
        # 无关联 event：set_task_signal 已把结构化写入 agent_tasks.result_text，无需再附
        return "already"
    # failed：仅占位失败可提升
    if task.get("exit_code") not in (0, None):
        return "already"
    _set_task_late_result(task_id, task, structured, status="completed")
    return "promoted"


def _set_task_late_result(task_id: str, task: dict, result_text: str, status: str):
    """补挂/提升：更新 agent_tasks 与关联 event 的结果存储（无副作用重放）。"""
    _now_iso = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE agent_tasks SET status=?, result_text=?, exit_code=CASE WHEN ?='completed' THEN 0 ELSE exit_code END, "
            "completed_at=? WHERE id=? AND status IN ('completed','failed')",
            (status, result_text, status, _now_iso, task_id))
        _event_seq = task.get("event_seq")
        if _event_seq:
            c.execute(
                "UPDATE events SET status=?, structured_output=?, result=?, completed_at=? WHERE seq=?",
                (status, result_text, result_text, _now_iso, _event_seq))
        db.commit()
    log.info(f"task {task_id} late self-report: {status} + structured attached (enrich)")


def _handle_vod_recommend_complete(repo_key: str, issue_id: str, result_text: str,
                                   task_id: str, task: dict, repo_gitcode_token: str = ""):
    """VOD 智能推荐完成处理：解析 scene_id → 持久化 → 重派 VOD。

    remote worker 模式下 _execute_agent 不执行，此函数在 complete_task 中补触发，
    与 _execute_agent_inner 中的逻辑等价（event_pipeline.py L2261-2310）。
    """
    import re as _re
    # 查 task prompt 判断是否为智能推荐任务
    _task_prompt = ""
    try:
        from src.session_manager import db as _rdb, db_lock as _rdb_lock
        with _rdb_lock:
            c = _rdb.cursor()
            c.execute("SELECT prompt FROM agent_tasks WHERE id=?", (task_id,))
            _trow = c.fetchone()
            _task_prompt = _trow[0] if _trow else ""
    except Exception:
        pass
    if not (_task_prompt and "智能推荐器" in _task_prompt and "{{SKILL_DIR}}" not in _task_prompt):
        return  # 不是智能推荐任务，跳过

    _rec_scene = ""
    try:
        from src.api.event_store import parse_agent_output_json
        _scene_out = parse_agent_output_json(result_text) or {}
        _rec_scene = _scene_out.get("scene_id", "") or ""
    except Exception:
        pass
    if not _rec_scene:
        _m_rec = _re.search(r'"scene_id"\s*:\s*"([^"]*)"', result_text)
        _rec_scene = _m_rec.group(1) if _m_rec else ""

    if not _rec_scene:
        log.info(f"VOD recommend: no scene_id for {issue_id}, pipeline terminated")
        return

    from src.api.session_store import set_issue_scene
    from src.runtime.engine_select import _repo_user_id
    _vod_issue_creator = ""
    try:
        from src.session_manager import db as _evdb, db_lock as _evdb_lock
        with _evdb_lock:
            _ec = _evdb.cursor()
            _ec.execute(
                "SELECT actor FROM events WHERE repo_key=? AND issue_id=? AND actor!='' "
                "ORDER BY seq ASC LIMIT 1",
                (repo_key, issue_id))
            _erow = _ec.fetchone()
            if _erow:
                _vod_issue_creator = _erow[0] or ""
    except Exception:
        pass
    _vod_uid = _repo_user_id(repo_key, issue_creator=_vod_issue_creator)
    set_issue_scene(repo_key, issue_id, _rec_scene, scene_user_id=_vod_uid)
    log.info(f"VOD recommend: scene_id={_rec_scene} set for {issue_id}")

    from src.api.pipeline_config import get_first_node, get_pipeline_config
    _vod_node = get_first_node(_rec_scene, repo_key=repo_key)
    if not _vod_node:
        _pcfg = get_pipeline_config(_rec_scene, repo_key=repo_key)
        _nodes = (_pcfg or {}).get("nodes", [])
        if _nodes:
            _vod_node = _nodes[0]
    if not _vod_node:
        log.warning(f"VOD recommend: no vod node for scene={_rec_scene}, skip redispatch")
        return

    _vod_skill = _vod_node.get("skill", "vod")
    log.info(f"VOD recommend: redispatch VOD with skill={_vod_skill} for {issue_id}")

    # 构建 evt 给 _redispatch_vod_after_recommend（prompt_builder 需要 payload 字段）
    _issue_num = str(issue_id).lstrip("#")
    _evt = {
        "repository": task.get("account") or "",
        "issue_id": issue_id,
        "payload": {"issue_number": _issue_num, "issue": {"number": int(_issue_num) if _issue_num.isdigit() else _issue_num}},
    }
    _runtime_id = task.get("runtime_id") or 0
    from src.api.event_pipeline import _redispatch_vod_after_recommend
    _redispatch_vod_after_recommend(repo_key, issue_id, _vod_skill, _evt, _runtime_id, repo_gitcode_token)


def _task_created_key(value):
    """created_at 归一键（比较用），返回 (可解析标志, epoch 秒, 原串)。

    历史上 agent_tasks.created_at 有 ISO 带时区（`2026-10-19T09:42:07.12+00:00`）与
    本地 `YYYY-MM-DD HH:MM:SS` 两种写法，字符串直接比较会在混写时误判。
    统一成同构三元组：可解析的恒大于不可解析的，同组内按时间戳比较，避免混类型比较。
    """
    _v = (value or "").strip()
    if not _v:
        return (0, 0.0, "")
    try:
        _dt = datetime.fromisoformat(_v.replace(" ", "T"))
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=timezone.utc)
        return (1, _dt.timestamp(), _v)
    except Exception:
        return (0, 0.0, _v)


def fail_task(task_id, error, exit_code=-1):
    task = get_task(task_id)
    # 幂等保护：如果 task 已经 completed，不调 fail（防止 complete→fail 竞态覆盖）
    if task and task.get("status") == "completed":
        log.info(f"Task {task_id} already completed, skip fail_task (error={error[:60]})")
        return
    # 幂等保护 2：已 failed 的 task 重复收到 fail（worker 补报重放 / 多进程重复上报）
    # 直接跳过——否则每次重放都会重跑 finalize_event_archive + update_event_complete，
    # 把人工 redispatch 已 reset 回 running 的事件再写回 failed + 旧失败原因
    # （2026-10-19 补报重放事故：重派后 180ms 被旧任务僵尸上报劫持）。
    if task and task.get("status") == "failed":
        log.info(f"Task {task_id} already failed, skip fail_task (error={str(error)[:60]})")
        return
    # 过期上报丢弃：同一 event_seq 下已存在更晚创建的任务（redispatch / recover_orphans 重跑），
    # 说明本上报属于被取代的旧执行实例 → 不得回写任务终态，更不得回写事件终态。
    if task and task.get("event_seq"):
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT id, created_at FROM agent_tasks WHERE event_seq=?",
                          (task["event_seq"],))
                _sibs = c.fetchall() or []
            _mine = task.get("id") or task_id
            _mykey = _task_created_key(task.get("created_at"))
            _newer = ""
            for _sid, _sca in _sibs:
                if _sid == _mine:
                    continue
                if _task_created_key(_sca) > _mykey:
                    _newer = _sid
                    break
            if _newer:
                log.info(f"Stale fail report discarded: task {task_id} superseded by {_newer} "
                         f"(event_seq={task['event_seq']}); task/event state left intact")
                return
        except Exception as e:
            log.warning(f"fail_task stale-report check failed: {e}")
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
    # exit_code 由 worker fail 上报透传（超时/-1、引擎崩溃码）；不传时默认 -1；
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
                    # 指针与引擎标记一起清，防残留 engine 标记让后续异构引擎回写被误拦
                    update_issue_session(_isid, runtime_session_id="", session_engine="")
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


async def wait_task_terminal(task_id: str, timeout: float = 1800.0, poll_interval: float = 1.0) -> dict:
    """异步轮询任务直到终态（completed/failed/cancelled）。

    供 IM / web 等"入队 + 等结果"路径使用（见 multi-runtime-instance-design 配套）。
    超时抛 TimeoutError。返回终态 task dict（含 result_text / exit_code / usage_json.session_id）。

    poll_interval 从 3s 降为 1s——远端任务完成后飞书/web 等待者感知延迟减少 2/3，
    且不增加 DB 压力（单条 SELECT 查询 <1ms）。
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
                    # Decrement busy_slots（统一收口 release_busy_slot）
                    if rid:
                        release_busy_slot(rid, cursor=c)
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


def reap_stale_runtimes() -> dict:
    """清理僵尸 runtime：把 last_seen_at 过期（> _RUNTIME_STALE_SECONDS）的远端引擎标 offline，
    并把绑定到这些 runtime 上的 dispatched 任务回收为 queued（防任务永久卡死）。

    场景：AI DevSpace 被删后 runtime 行残留 status=online 但 worker 不再心跳。
    由 main.py 每 60 秒调用一次（_reap_stale_loop）。
    """
    from src.runtime.health import parse_dt
    now = time.time()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=_RUNTIME_STALE_SECONDS)).isoformat()
    # Issue #259：回收分级——running 任务的对齐 worker 本地超时（RUNTIME_TASK_TIMEOUT 默认 3600s），
    # 防假死 worker（断连但任务还在本地跑）的服务端重派与本地执行重叠造成双跑。
    # dispatched 任务从未开始执行，无双跑风险，跟心跳阈值走（100s）。
    _recover_cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=_TASK_RECOVER_STALE_SECONDS)).isoformat()
    reaped = []
    with db_lock:
        c = db.cursor()
        try:
            # 查找远端引擎（credential_id>0）status=online 但心跳过期的行
            # Issue #259：去掉 last_seen_at IS NOT NULL——NULL（从未心跳）但 status=online 的
            # 幽灵行也算过期，一起标 offline（原查询会永久漏掉它们）
            c.execute(
                "SELECT id, name, credential_id, last_seen_at FROM agent_runtimes "
                "WHERE status='online' AND credential_id > 0 "
                "AND (last_seen_at IS NULL OR last_seen_at='' OR last_seen_at < ?)",
                (cutoff_iso,)
            )
            stale = c.fetchall()
            for row in stale:
                rid, rname, cid = row[0], row[1], row[2]
                # 标记 offline
                c.execute(
                    "UPDATE agent_runtimes SET status='offline' WHERE id=?",
                    (rid,)
                )
                # 回收该 runtime 上的 dispatched 任务 → queued（从未开始，零双跑风险，100s）
                # runtime_id 置 0（NOT NULL 列不能置 NULL；0 = 无主池任务，可被任意引擎 claim）
                c.execute(
                    "UPDATE agent_tasks SET status='queued', started_at=NULL, runtime_id=0, attempts=attempts+1 "
                    "WHERE runtime_id=? AND status='dispatched'",
                    (rid,)
                )
                recovered = c.rowcount
                # 回收 running 任务 → queued（可能还在假死 worker 本地执行，3600s 对齐 worker
                # 本地 RUNTIME_TASK_TIMEOUT 默认值——worker 本地到点自杀后才重派，双跑窗口=0）
                # 前提：TASK_RECOVER_STALE_SECONDS 必须 >= worker 的 RUNTIME_TASK_TIMEOUT（部署注意）。
                c.execute(
                    "UPDATE agent_tasks SET status='queued', started_at=NULL, runtime_id=0, attempts=attempts+1 "
                    "WHERE runtime_id=? AND status='running' AND (started_at IS NULL OR started_at='' OR started_at < ?)",
                    (rid, _recover_cutoff_iso)
                )
                recovered += c.rowcount
                if recovered > 0:
                    c.execute(
                        "UPDATE agent_runtimes SET busy_slots=0 WHERE id=?",
                        (rid,)
                    )
                reaped.append({"runtime_id": rid, "name": rname, "recovered_tasks": recovered})
            if stale:
                db.commit()
        except Exception as e:
            log.warning(f"reap_stale_runtimes failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass

    # Issue #134 扩展：queued 任务超时降级——防饿死机制
    # 当所有候选引擎健康分过低时，任务会一直 queued 没人认领。
    # 超过 _STALE_QUEUED_THRESHOLD 秒后清空 pool_engine_ids 放行，让任意在线引擎认领。
    try:
        _check_stale_queued_tasks()
    except Exception as _sqe:
        log.warning(f"reap_stale_runtimes: _check_stale_queued_tasks failed: {_sqe}")

    # 引擎绑定真实性同步：离线超阈值（默认 8h）的引擎，真实清理场景绑定 + 审计留痕。
    # 设计文档 docs/weekly-06/engine-binding-truth-sync-design.md §4.2。
    try:
        from src.runtime.engine_select import _detach_dead_engine_bindings
        _detach_dead_engine_bindings()
    except Exception as _de:
        log.warning(f"reap_stale_runtimes: _detach_dead_engine_bindings failed: {_de}")

    return {"reaped": reaped}


_STALE_QUEUED_THRESHOLD = int(os.environ.get("HEALTH_STALE_QUEUED_THRESHOLD", "120"))


def _check_stale_queued_tasks():
    """Issue #253 改动3：queued 任务超时 → 限时等待收敛（宁失败，不跑飞）。

    当任务 queued 超过 _STALE_QUEUED_THRESHOLD（默认 120 秒）且 pool_engine_ids 非空时，
    按 issue #253 硬约束语义收敛（方案 A：低健康分限时等待，超时失败）：
    - 池中有健康引擎 → 等待（引擎可能马上认领）
    - 引擎不健康但物理可达 → 有界等待：超时后按来源分流（local-claimer 设计 §3.3）：
        * pool_engine_source='auto'（系统自动选择的绑定）→ 降级：清空白名单重新入队
          （放行全站认领，含本机 direct 引擎），置 source='degraded' 防反复，result 记录审计
        * 'scene'/''（用户显式绑定/历史未标记）→ 任务 failed（#253 硬约束，不跑飞）
    - 引擎既不健康又不可达 → 同上按来源分流（auto 降级 / scene 失败）

    空绑定任务不进本函数（查询条件 pool_engine_ids!=''），广播语义不受影响。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_STALE_QUEUED_THRESHOLD)).isoformat()
    try:
        with db_lock:
            c = db.cursor()
            # 查询超时的 queued 任务（带 pool_engine_ids 限定）
            c.execute(
                "SELECT id, pool_engine_ids, pool_tag, repo_key, user_id FROM agent_tasks "
                "WHERE status='queued' AND pool_engine_ids!='' AND pool_engine_ids IS NOT NULL "
                "AND created_at <= ?",
                (cutoff,))
            stale_tasks = c.fetchall()
            if not stale_tasks:
                return
            import json as _json
            for tid, peids, task_tag, task_repo, task_uid in stale_tasks:
                # 先检查 pool 中是否有健康引擎——有则等待（正常路径，引擎马上会领）
                try:
                    _engine_ids = _json.loads(peids) if peids else []
                except Exception:
                    _engine_ids = []
                if _engine_ids and _check_pool_has_healthy(_engine_ids):
                    continue  # 池中有健康引擎，继续等待
                # local-claimer 设计 §3.3：白名单全灭时按来源分流（显式绑定不降级，自动绑定放行）
                _source = ""
                try:
                    c.execute("SELECT pool_engine_source FROM agent_tasks WHERE id=?", (tid,))
                    _sr = c.fetchone()
                    _source = (_sr[0] or "") if _sr else ""
                except Exception:
                    _source = ""
                if _source == "auto":
                    # 系统自动选择的绑定全灭 → 降级放行全站（含本机 direct），只降一次
                    c.execute(
                        "UPDATE agent_tasks SET pool_engine_ids='', pool_engine_source='degraded', "
                        "result_text=COALESCE(NULLIF(result_text,''), ?) WHERE id=? AND status='queued'",
                        (f"[degraded] bound engines {_engine_ids} all unhealthy/unreachable at "
                         f"{datetime.now(timezone.utc).isoformat()}; released to global pool "
                         f"(source=auto, local-claimer design)", tid))
                    db.commit()
                    log.info(f"_check_stale_queued_tasks: engine(s) {_engine_ids} all down, "
                             f"source=auto -> degraded release task {tid} to global pool")
                    continue
                # 'scene' / '' / 'degraded'：用户显式绑定（或历史/已降级）→ 保持 #253 硬约束
                # 健康分不达标 → 再检查物理可达性（在线 + 有 tag 能力 + scope 匹配 + 有并发槽）
                # Issue #253 方案 A：物理可达但低分 → 限时等待已到点（120s），诚实失败；
                # 物理不可达 → 直接失败（不撕证放行全站）
                if _engine_ids and _check_pool_engines_reachable(
                    _engine_ids, task_tag or "", task_repo or "", task_user_id=task_uid or 0):
                    log.info(f"_check_stale_queued_tasks: engine(s) {_engine_ids} unhealthy but reachable, "
                             f"wait window elapsed -> fail task {tid} (Issue #253)")
                    _fail_task_pool_unhealthy(c, tid, _engine_ids, _STALE_QUEUED_THRESHOLD)
                else:
                    _fail_task_pool_unreachable(c, tid, _engine_ids, task_tag or "")
    except Exception as e:
        log.warning(f"_check_stale_queued_tasks failed: {e}")
