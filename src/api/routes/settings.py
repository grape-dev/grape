"""Settings 路由：凭据、通知偏好、GitCode Repo、Backend Runtimes"""
import json
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import get_current_user, get_user_token
from src.config import log
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ====== Credentials ======

@router.get("/credentials")
async def get_credentials(user: dict = Depends(get_current_user), repo_key: str = ""):
    acc = user_db.get_user_account(user["username"]) or {}
    return {
        "gitcode": {
            "api_token_exists": bool(acc.get("gitcode_api_token")),
            "account": user["username"],
            "auto_filled": True,
        },
        "huawei_cloud": {
            "ak_exists": bool(acc.get("huawei_cloud_ak")),
            "sk_exists": bool(acc.get("huawei_cloud_sk")),
            "ak": acc.get("huawei_cloud_ak", ""),
            "sk": acc.get("huawei_cloud_sk", ""),
        },
        "codearts": {
            "ak_exists": bool(acc.get("CODEARTS_CLI_AK")),
            "sk_exists": bool(acc.get("CODEARTS_CLI_SK")),
        },
    }


class UpdateCredentials(BaseModel):
    huawei_cloud_ak: str = ""
    huawei_cloud_sk: str = ""
    codearts_ak: str = ""
    codearts_sk: str = ""
    repo_key: str = ""


# 占位符凭据（2026-08-17 修复，issue #72）：前端曾把 demo 占位符回显进输入框，
# 用户直接保存会把占位符当真实值写库，覆盖真实 AK/SK → 部署任务注入占位符持续 401。
# 后端兜底：任何等于占位符 / demo 前缀 / 含 xxxx 的值一律拒绝写入。
_PLACEHOLDER_VALUES = {
    "AK-demo-access-key-xxxx", "SK-demo-secret-key-xxxx", "ghp_devopsbot_xxxxxxxxxxxxxxxxxxxx",
}


def _is_placeholder(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return True  # 空 = 无值（不写）
    if v in _PLACEHOLDER_VALUES:
        return True
    if v.startswith(("AK-demo-", "SK-demo-", "ghp_devopsbot_")):
        return True
    return "xxxx" in v.lower()


@router.put("/credentials")
async def update_credentials(body: UpdateCredentials, user: dict = Depends(get_current_user)):
    acc = user_db.get_user_account(user["username"]) or {}
    if body.huawei_cloud_ak and not _is_placeholder(body.huawei_cloud_ak):
        acc["huawei_cloud_ak"] = body.huawei_cloud_ak.strip()
    if body.huawei_cloud_sk and not _is_placeholder(body.huawei_cloud_sk):
        acc["huawei_cloud_sk"] = body.huawei_cloud_sk.strip()
    if body.codearts_ak and not _is_placeholder(body.codearts_ak):
        acc["CODEARTS_CLI_AK"] = body.codearts_ak.strip()
    if body.codearts_sk and not _is_placeholder(body.codearts_sk):
        acc["CODEARTS_CLI_SK"] = body.codearts_sk.strip()
    user_db.save_user_account(user["username"], acc)
    return {"message": "凭据已更新"}


# ====== Notifications ======

@router.get("/notifications")
async def get_notifications(user: dict = Depends(get_current_user)):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT auto_impl_enabled, notify_enabled FROM repo_im_group_binding LIMIT 1")
            row = c.fetchone()
            auto_impl = bool(row[0]) if row else True
            notify = bool(row[1]) if row else True
        except Exception:
            auto_impl, notify = True, True
    return {
        "issues": True,
        "pr": True,
        "build": False,
        "release": True,
        "vod": auto_impl,
        "notifications_enabled": notify,
    }


class NotificationPrefs(BaseModel):
    issues: bool = True
    pr: bool = True
    build: bool = False
    release: bool = True
    vod: bool = False
    notifications_enabled: bool = True


@router.put("/notifications")
async def update_notifications(body: NotificationPrefs, user: dict = Depends(get_current_user)):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("UPDATE repo_im_group_binding SET auto_impl_enabled=?, notify_enabled=?",
                      (1 if body.vod else 0, 1 if body.notifications_enabled else 0))
            db.commit()
        except Exception:
            pass
    return {"message": "通知偏好已更新", "prefs": body.model_dump()}


# ====== GitCode Repo ======

@router.get("/gitcode-repo/{owner}/{repo}")
async def get_gitcode_repo_config(owner: str, repo: str, user: dict = Depends(get_current_user)):
    from src.user.db import get_bot_token_by_repo
    from src.repo_key import to_repo_key
    bot_token = get_bot_token_by_repo(owner, repo)
    bot_username = "devops-bot"
    # GitCode name 可能是显示名（≠URL path），按 repo_key 匹配更稳（见 repo_manager.fetch_repos_with_role）
    target_key = to_repo_key(f"{owner}/{repo}")
    repos = user_db.get_managed_repos(user["id"])
    for r in repos:
        if to_repo_key(f'{r.get("repo_owner", "")}/{r.get("repo_name", "")}') == target_key:
            bot_username = r.get("bot_username", "") or "devops-bot"
            break

    return {
        "devops_bot_token_exists": bool(bot_token),
        "webhook_url": user_db.get_webhook_url() or "http://localhost:8080/webhook/gitcode",
        "bot_username": bot_username,
    }


class GitCodeRepoConfig(BaseModel):
    bot_token: str = ""
    bot_username: str = "devops-bot"


@router.put("/gitcode-repo/{owner}/{repo}")
async def update_gitcode_repo_config(owner: str, repo: str, body: GitCodeRepoConfig,
                                     user: dict = Depends(get_current_user)):
    # 将 Bot Token / Bot Username 存储到 managed_repos（webhook_token 由托管流程自动维护）
    from src.user.db import update_managed_repo_bot_token, update_managed_repo_bot_username
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")
    if body.bot_token and not _is_placeholder(body.bot_token):
        update_managed_repo_bot_token(repo_key, body.bot_token.strip())
    if body.bot_username:
        update_managed_repo_bot_username(repo_key, body.bot_username)
    return {"message": "GitCode Repo 配置已更新"}


# ====== OAuth 凭据（管理员配置）======

@router.get("/oauth")
async def get_oauth_config(user: dict = Depends(get_current_user)):
    from src.user.db import get_meta_value
    return {
        "gitcode_oauth_client_id": bool(get_meta_value("oauth_gitcode_client_id")),
        "gitcode_oauth_redirect_uri": get_meta_value("oauth_gitcode_redirect_uri"),
    }


class OAuthConfig(BaseModel):
    gitcode_oauth_client_id: str = ""
    gitcode_oauth_client_secret: str = ""
    gitcode_oauth_redirect_uri: str = ""


@router.put("/oauth")
async def update_oauth_config(body: OAuthConfig, user: dict = Depends(get_current_user)):
    from src.user.db import set_meta_value
    if body.gitcode_oauth_client_id:
        set_meta_value("oauth_gitcode_client_id", body.gitcode_oauth_client_id)
    if body.gitcode_oauth_client_secret:
        set_meta_value("oauth_gitcode_client_secret", body.gitcode_oauth_client_secret)
    if body.gitcode_oauth_redirect_uri:
        set_meta_value("oauth_gitcode_redirect_uri", body.gitcode_oauth_redirect_uri)
    return {"message": "OAuth 配置已更新"}


# ====== Agent Runtimes ======

@router.get("/agent-runtimes")
async def get_agent_runtimes(user: dict = Depends(get_current_user)):
    """从 DB 读取 Agent 执行引擎列表，含健康态派生与唯一引擎名（instance_name）。

    可见性（按当前登录用户）：全局 + 个人（owner=当前用户）+ 我所在仓库的仓库级（仓库所有成员可见，不只 owner）。
    派生字段：instance_name / worker_name / worker_ip / scope / owner（见 runtime-admin-two-view-design.md §7）。
    """
    from src.user import db as user_db
    from src.runtime.health import derive_runtime_health
    from src.runtime.instances import instance_name
    from src.session_manager import db, db_lock

    uid = user.get("id") or 0
    uname = user.get("username") or ""
    user_repos = {r["full_name"] for r in (user_db.get_user_repos(uid) if uid else [])}

    # 获取全部有效用户名，用于判断已删除用户的本地引擎
    valid_users = set()
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT username FROM user_accounts")
            valid_users = {r[0] for r in c.fetchall()}
        except Exception:
            pass

    # 凭据映射：id -> 派生所需字段
    cred_map = {}
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, last_ip FROM runtime_credentials")
        for r in c.fetchall():
            cid, cname, cdev, cscope, cowner_id, cowner_name, cstatus, clast, clast_ip = r
            cred_map[cid] = {"name": cname, "device_name": cdev or "", "scope": cscope,
                             "owner_id": cowner_id or "", "owner_name": cowner_name or "",
                             "status": cstatus, "last_seen_at": clast, "last_ip": clast_ip or ""}

    def _visible_credential(cid: int, cscope: str, cowner_name: str, cowner_id: str) -> bool:
        if not cid:                      # credential_id=0（历史本机引擎）
            # 本机引擎行若有 owner_id，需确认对应用户仍存在
            # 已删除用户的本地引擎不展示（用户删除后系统不会自动清理行）
            return True
        if cscope == "global":
            return True
        if cscope == "user":
            return cowner_name == uname
        if cscope == "repo":
            return cowner_name == uname or cowner_id in user_repos
        return False

    cached = user_db.get_agent_runtimes(scope=None)
    now = __import__("time").time()
    visible_runtimes = []
    for rt in cached:
        rt["health"] = _runtime_health(rt, now)
        cid = rt.get("credential_id") or 0
        cred = cred_map.get(cid) or {"name": "", "device_name": "", "scope": "global",
                                     "owner_name": "", "owner_id": "", "last_ip": ""}
        if not _visible_credential(cid, cred["scope"], cred["owner_name"], cred["owner_id"]):
            continue
        if cid == 0:
            # 本机引擎行可能绑定了已删除用户的 owner_id，需校验用户是否仍存在
            rt_owner = (rt.get("owner_id") or "").strip()
            if rt_owner and rt_owner not in valid_users:
                # 对应用户已删除 → 引擎不展示，标记 health=user_deleted 供排查
                log.info(f"agent-runtimes: skipping local engine '{rt['name']}' owned by deleted user '{rt_owner}'")
                continue
        if cred.get("status") == "revoked":
            # 设备已吊销 → 引擎不再展示（与 Workers 视图只列 active 一致）；
            # DB 行保留 offline，避免历史会话/任务绑定的 runtime_id 悬空
            continue
        rt["instance_name"] = instance_name(rt["name"], cred["device_name"], cid)
        rt["worker_name"] = cred["name"]
        rt["worker_ip"] = cred["last_ip"]
        # 本机直连（cid=0）：owner/scope 取引擎自身 DB 值，不继承空凭据默认值
        if cid == 0:
            rt["owner"] = cred["owner_name"]
            rt["scope"] = rt.get("scope", cred["scope"]) or "global"
            # 本机直连引擎的 worker_name/device_name 用 host_name（区分多台主控机）
            _hn = rt.get("host_name") or ""
            if _hn:
                rt["worker_name"] = _hn
                rt["worker_ip"] = "127.0.0.1"
        else:
            rt["owner"] = cred["owner_name"]
            rt["scope"] = cred["scope"]          # 引擎 scope 随 worker（凭据）一致
        rt["is_local"] = (cid == 0)          # 本机直连（credential_id=0，恒 direct），前端分组/标签用
        # 引擎标签（event-dispatch-redesign-v2.md §2.5）
        try:
            with db_lock:
                _tc = db.cursor()
                _tc.execute("SELECT tag, max_concurrent, priority, enabled FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag", (rt["id"],))
                rt["tags"] = [{"tag": r[0], "max_concurrent": r[1], "priority": r[2], "enabled": bool(r[3])} for r in _tc.fetchall()]
        except Exception:
            rt["tags"] = []
        visible_runtimes.append(rt)

    # 设备级视图（按 credential_id 分组：worker_name + 承载 CLI + 心跳）
    devices = []
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM runtime_credentials WHERE status='active' ORDER BY id DESC")
        for (cid,) in c.fetchall():
            cred = cred_map.get(cid) or {}
            if not _visible_credential(cid, cred.get("scope", "global"), cred.get("owner_name", ""), cred.get("owner_id", "")):
                continue
            c.execute("SELECT id, name, version, healthy, installed, status, last_seen_at, local_skills, skill_scan_result FROM agent_runtimes WHERE credential_id=?", (cid,))
            runtimes = [
                {"id": x[0], "name": x[1], "version": x[2], "healthy": bool(x[3]),
                 "installed": bool(x[4]),
                 "instance_name": instance_name(x[1], cred.get("device_name", ""), cid),
                 "health": derive_runtime_health(x[5], x[6], now),
                 "local_skills": json.loads(x[7] or "[]") if x[7] else [],
                 "skill_scan_result": json.loads(x[8] or "[]") if x[8] else []}
                for x in c.fetchall()
            ]
            # tags from engine_capabilities
            for rt in runtimes:
                _tc2 = db.cursor()
                _tc2.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rt["id"],))
                rt["agent_tags"] = [r[0] for r in _tc2.fetchall()] or ["all"]
            devices.append({"credential_id": cid, "name": cred.get("name", ""), "device_name": cred.get("device_name", ""),
                            "credential_status": cred.get("status", "active"), "last_seen_at": cred.get("last_seen_at"),
                            "worker_ip": cred.get("last_ip", ""), "scope": cred.get("scope", "global"),
                            "owner": cred.get("owner_name", ""),
                            "health": _device_health(cred.get("status", "active"), cred.get("last_seen_at"), now),
                            "runtimes": runtimes})
    # 本机直连引擎（credential_id=0，本地 runtime）：按 host_name 分组为设备列表。
    # 多台主控机各自探测的引擎行通过 host_name 区分，每台主控机一个设备条目。
    # 主控机在 runtime_credentials 中的心跳记录（如 grape-main-001/007）没有绑定引擎，
    # 上面循环会创建空 runtimes 的 device 条目——这里统一过滤掉。
    _local_devices = {}  # host_name -> {runtimes: [...], scope, owner_id, ...}
    for rt in visible_runtimes:
        if (rt.get("credential_id") or 0) != 0:
            continue
        _hn = rt.get("host_name") or "本机"
        if _hn not in _local_devices:
            _local_devices[_hn] = {
                "credential_id": 0, "name": _hn, "device_name": _hn,
                "credential_status": "active", "last_seen_at": None,
                "worker_ip": "127.0.0.1", "scope": rt.get("scope") or "global", "owner": "",
                "owner_id": rt.get("owner_id", ""),
                "health": rt.get("health", "offline"),
                "runtimes": [],
            }
        _local_devices[_hn]["runtimes"].append({
            "id": rt["id"], "name": rt["name"], "version": rt.get("version", ""),
            "healthy": rt.get("healthy", False), "installed": rt.get("installed", False),
            "instance_name": rt.get("instance_name", rt.get("name", "")),
            "health": rt.get("health", "offline"),
            "local_skills": rt.get("local_skills", []),
            "skill_scan_result": rt.get("skill_scan_result", []),
            "agent_tags": rt.get("agent_tags", ["all"])})
    devices.extend(_local_devices.values())
    # AI Devspace 创建的凭据：worker 尚未上线注册时没有 agent_runtimes 行，
    # 但用户需要在 Workers 列表看到这个待接入设备（凭据已生成，环境正在启动）。
    # 保留没有承载引擎但凭据 active 的设备，health=offline 让用户知道还没接入。
    # 主控机在 runtime_credentials 中的心跳记录（credential_id=130/131 但无 agent_runtimes 行绑定）
    # 也保留，但 health 派生会正确反映其离线状态。
    # 本机直连设备（credential_id=0）如果无引擎则过滤（本机探测应该有引擎才显示）。
    devices = [d for d in devices if d.get("runtimes") or (d.get("credential_id", 0) > 0 and d.get("credential_status") == "active")]
    # 本机设备的 health：至少一个引擎 online 则设备 online（主控机本身不会掉线）
    for d in devices:
        if (d.get("credential_id") or 0) == 0:
            d["health"] = "online" if any(rt.get("health") == "online" for rt in d.get("runtimes", [])) else "offline"
    return {"runtimes": visible_runtimes, "devices": devices}


def _runtime_health(rt: dict, now: float) -> str:
    """单引擎健康态：
    - credential_id>0（远端边端）：status+last_seen 四态
    - credential_id=0（本地引擎）：installed/healthy 派生（就绪=online，未装=offline）
    """
    from src.runtime.health import derive_runtime_health
    if rt.get("credential_id"):
        return derive_runtime_health(rt.get("status") or "offline", rt.get("last_seen_at"), now)
    if rt.get("installed") and rt.get("healthy"):
        return "online"
    return "offline"


_DEVICE_ONLINE_WINDOW = 60  # 心跳间隔 15s，容差 60s 内视为在线


def _device_health(cstatus: str, last_seen_at: str | None, now: float) -> str:
    """设备（凭据维度）健康态：由心跳新鲜度派生。

    凭据 `status` 恒为 'active'（不表达在线），不能像 runtime 行那样按 `status='online'` 判定；
    在线 = 最近心跳在 _DEVICE_ONLINE_WINDOW 内；失联 <5min = 刚失联；>6d = 待回收；其余离线。
    """
    from src.runtime.health import parse_dt
    ts = parse_dt(last_seen_at)
    if ts is None:
        return "offline"
    offline_for = now - ts
    if offline_for < _DEVICE_ONLINE_WINDOW:
        return "online"
    if offline_for < 5 * 60:
        return "recently_lost"
    if offline_for > 6 * 24 * 3600:
        return "about_to_gc"
    return "offline"

# 兼容别名：旧路径 /backend-agents（≥2 发布周期，见 agent-runtime-design.md §6.2）
@router.get("/backend-agents")
async def get_backend_agents(user: dict = Depends(get_current_user)):
    cached = user_db_get_backend_agents_alias()
    return {"backends": cached}


def user_db_get_backend_agents_alias():
    from src.user import db as user_db
    return user_db.get_backend_agents()


@router.post("/agent-runtimes/refresh")
async def refresh_agent_runtimes(user: dict = Depends(get_current_user)):
    """手动刷新：发现本地引擎 + 子进程检测 + 写入 DB（探测统一走 local_probe，方案 C）"""
    from src.runtime.registry import discover_runtimes
    from src.runtime.local_probe import probe_local_engines
    from src.runtime.agent import _scan_local_skills
    from src.user import db as user_db
    import socket as _sock
    _hostname = _sock.gethostname()
    discovered = discover_runtimes()
    results = probe_local_engines(discovered)

    # 扫描本机 local_skills 详情并注入
    from src.runtime.agent import _scan_local_skills_with_detail
    for r in results:
        _name = (r.get("name") or "").strip().lower()
        r["local_skills_detail"] = _scan_local_skills_with_detail(_name)
        r["local_skills"] = [s["name"] for s in r["local_skills_detail"]]

    # Only persist runtimes that are actually installed
    results = [r for r in results if r["installed"]]
    user_db.save_agent_runtimes(results, host_name=_hostname)
    return {"runtimes": results}

# 兼容别名：旧路径 /backend-agents/refresh
@router.post("/backend-agents/refresh")
async def refresh_backend_agents(user: dict = Depends(get_current_user)):
    return await refresh_agent_runtimes(user)


# ====== Runtime Credentials（rta_ token，见 agent-runtime-event-full-design.md §4.1 / §10）======

_SCOPE_ALIAS = {"user": "user", "个人": "user", "repo": "repo", "仓库": "repo", "global": "global", "全局": "global"}


class RuntimeCredBody(BaseModel):
    name: str
    scope: str = "user"       # user(个人)/repo(仓库)/global(全局)，默认个人
    repo_key: str = ""        # scope=repo 时必填（目标仓库全名，如 owner/repo）


def _normalize_scope(scope: str) -> str:
    s = _SCOPE_ALIAS.get((scope or "").strip().lower())
    if not s:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 user/repo/global（个人/仓库/全局）"})
    return s


@router.post("/runtime-credentials")
async def create_runtime_credential(body: RuntimeCredBody, user: dict = Depends(get_current_user)):
    """生成 rta_ token：仅展示一次明文，DB 存 sha256。owner=当前登录用户（不可选），scope 默认个人。"""
    import hashlib, secrets
    from src.session_manager import db, db_lock
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "凭据名称不能为空"})
    scope = _normalize_scope(body.scope)
    owner = user.get("username") or ""
    repo_key = ""
    if scope in ("repo", "user"):
        repo_key = (body.repo_key or "").strip()
        if scope == "repo" and not repo_key:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库范围凭据必须指定 repo_key"})
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT INTO runtime_credentials (token_hash, name, scope, owner_id, owner_name, repo_key) VALUES (?,?,?,?,?,?)",
                      (hashlib.sha256(token.encode()).hexdigest(), name, scope, repo_key if scope == "repo" else "", owner, repo_key))
            cred_id = c.lastrowid
            db.commit()
        except Exception as e:
            msg = str(e)
            if ("UNIQUE" in msg or "1062" in msg or "Duplicate" in msg) and "runtime_credentials.name" in msg:
                # sqlite: "UNIQUE constraint failed: ..."; MySQL: (1062, "Duplicate entry ... for key 'runtime_credentials.name'")
                # 只认 "UNIQUE" 字样在 MySQL 下匹配不到 → 误报 500（2026-08-17 实测）
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"凭据名称已存在（全局唯一）: {name}"})
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"生成失败: {e}"})
    # 预构建 worker 安装包（异步）：凭据生成后即可一键安装，避免首次下载才打包
    # （首次构建含依赖 wheels 下载，耗时数秒至数分钟，放后台不阻塞响应）
    try:
        from src.worker_package import ensure_package_ready
        ensure_package_ready()
    except Exception as _e:
        log.warning(f"预构建 worker 安装包失败: {_e}")
    return {"token": token, "id": cred_id, "name": name, "scope": scope, "owner": owner, "message": "token 仅展示一次，请立即保存"}


@router.put("/runtime-credentials/{cid}")
async def rename_runtime_credential(cid: int, body: RuntimeCredBody, user: dict = Depends(get_current_user)):
    """改名凭据（worker 名）：全局唯一，重名拒绝。owner/scope 不可改。"""
    from src.session_manager import db, db_lock
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "凭据名称不能为空"})
    with db_lock:
        c = db.cursor()
        try:
            c.execute("UPDATE runtime_credentials SET name=? WHERE id=? AND status='active'", (name, cid))
            if c.rowcount == 0:
                row = c.execute("SELECT id FROM runtime_credentials WHERE id=?", (cid,)).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
                raise HTTPException(status_code=400, detail={"code": 400, "message": "仅 active 凭据可改名"})
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            msg = str(e)
            if ("UNIQUE" in msg or "1062" in msg or "Duplicate" in msg) and "runtime_credentials.name" in msg:
                # sqlite: "UNIQUE constraint failed: ..."; MySQL: (1062, "Duplicate entry ... for key 'runtime_credentials.name'")
                # 只认 "UNIQUE" 字样在 MySQL 下匹配不到 → 误报 500（2026-08-17 实测）
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"凭据名称已存在（全局唯一）: {name}"})
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"改名失败: {e}"})
    return {"message": f"凭据已改名为 {name}"}


@router.get("/runtime-credentials")
async def list_runtime_credentials(user: dict = Depends(get_current_user)):
    """凭据列表（不含 token_hash / 明文）"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, created_at FROM runtime_credentials ORDER BY id DESC")
        return {"credentials": [
            {"id": r[0], "name": r[1], "device_name": r[2] or "", "scope": r[3],
             "owner_id": r[4], "owner": r[5] or "", "status": r[6], "last_seen_at": normalize_event_time(r[7]), "created_at": normalize_event_time(r[8])}
            for r in c.fetchall()
        ]}


@router.post("/runtime-credentials/{cid}/revoke")
async def revoke_runtime_credential(cid: int, user: dict = Depends(get_current_user)):
    """吊销并删除 worker 记录：删 runtime_credentials 行 + 级联删其 agent_runtimes 行。

    2026-08-17 用户指示「吊销=删除」：删除后 token 即刻失效（_auth_credential
    查不到 → 401）、凭据名释放可复用（全局唯一键不再被 revoked 行占用）。
    级联清理：
    - agent_runtimes：该凭据注册的引擎行一并删除
    - devops_agent_session.runtime_id：置 0（dashboard 红字「无 engine」，如实反映）
    历史 agent_tasks 绑定旧 runtime_id 的任务恢复走 /api/events/{seq}/redispatch
    （pick_engine 重新绑定后 requeue，见 engine-selection-claim-pool-2026-08.md）。
    """
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM runtime_credentials WHERE id=?", (cid,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
        c.execute("SELECT id FROM agent_runtimes WHERE credential_id=?", (cid,))
        rid_list = [r[0] for r in c.fetchall()]
        if rid_list:
            ph = ",".join("?" for _ in rid_list)
            try:
                c.execute(f"UPDATE devops_agent_session SET runtime_id=0 WHERE runtime_id IN ({ph})", rid_list)
            except Exception as _e:
                log.warning(f"revoke {cid}: 清理 session 失败: {_e}")
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()
    return {"message": f"凭据 {cid} 已吊销并删除"}


class ExecModeBody(BaseModel):
    mode: str


@router.get("/execution-mode")
async def get_execution_mode_api(user: dict = Depends(get_current_user)):
    from src.runtime.service import get_execution_mode
    return {"mode": get_execution_mode()}


@router.get("/pipeline-mode")
async def get_pipeline_mode_api(user: dict = Depends(get_current_user)):
    from src.api.event_pipeline import is_plugin_mode
    return {"mode": "plugin" if is_plugin_mode() else "legacy"}


@router.put("/pipeline-mode")
async def set_pipeline_mode_api(body: dict, user: dict = Depends(get_current_user)):
    from src.user.db import set_meta_value
    mode = (body.get("mode") or "").strip().lower()
    if mode not in ("plugin", "legacy"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "mode 必须为 plugin 或 legacy"})
    set_meta_value("pipeline_mode", mode)
    return {"message": f"管线模式已切换为 {'插件模式' if mode == 'plugin' else '传统模式'}", "mode": mode}


@router.put("/execution-mode")
async def set_execution_mode_api(body: ExecModeBody, user: dict = Depends(get_current_user)):
    from src.runtime.service import set_execution_mode
    if body.mode not in ("auto", "direct", "queue_remote"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "mode 必须为 auto/direct/queue_remote（queue_local 已废除）"})
    set_execution_mode(body.mode)
    return {"message": f"执行模式已切换为 {body.mode}", "mode": body.mode}


# ===== 引擎标签管理（event-dispatch-redesign-v2.md §2.4）=====

class EngineTagBody(BaseModel):
    tag: str
    max_concurrent: int = 1
    priority: int = 50


class EngineTagUpdateBody(BaseModel):
    max_concurrent: int = 1
    priority: int = 50
    enabled: int = 1


def _sync_agent_tags(rid: int, cursor):
    """engine_capabilities 变更后，回写 agent_runtimes.agent_tags（JSON 快照）。

    agent_tags 是 engine_capabilities 中 enabled=1 的 tag 列表的 JSON 表示，
    供前端展示用。此函数确保两表一致。
    """
    cursor.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag", (rid,))
    tags = [r[0] for r in cursor.fetchall()] or ["all"]
    cursor.execute("UPDATE agent_runtimes SET agent_tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), rid))


@router.get("/agent-runtimes/{rid}/tags")
async def get_engine_tags(rid: int, user: dict = Depends(get_current_user)):
    """获取引擎的标签列表"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, tag, max_concurrent, priority, enabled, created_at, updated_at "
                "FROM engine_capabilities WHERE runtime_id=? ORDER BY tag",
                (rid,)
            )
            rows = c.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    return {"tags": [
        {"id": r[0], "tag": r[1], "max_concurrent": r[2], "priority": r[3],
         "enabled": bool(r[4]), "created_at": r[5], "updated_at": r[6]}
        for r in rows
    ]}


@router.post("/agent-runtimes/{rid}/tags")
async def add_engine_tag(rid: int, body: EngineTagBody, user: dict = Depends(get_current_user)):
    """为引擎添加标签（幂等：已有则更新）"""
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    tag = (body.tag or "").strip().lower()
    if tag not in ("vod", "issue", "coding", "qa", "sre", "chat"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"无效标签: {tag}，可选: vod/issue/coding/qa/sre/chat"})
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (rid,))
            if not c.fetchone():
                raise HTTPException(status_code=404, detail={"code": 404, "message": "引擎不存在"})
            c.execute(
                "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                "VALUES (?,?,?,?,1,?,?) "
                "ON DUPLICATE KEY UPDATE max_concurrent=VALUES(max_concurrent), priority=VALUES(priority), updated_at=VALUES(updated_at)",
                (rid, tag, body.max_concurrent, body.priority, now, now)
            )
            _sync_agent_tags(rid, c)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            # MySQL ON DUPLICATE KEY may not work with SQLite syntax; fallback
            try:
                c.execute(
                    "REPLACE INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                    "VALUES (?,?,?,?,1,?,?)",
                    (rid, tag, body.max_concurrent, body.priority, now, now)
                )
                _sync_agent_tags(rid, c)
                db.commit()
            except Exception as e2:
                db.rollback()
                raise HTTPException(status_code=500, detail={"code": 500, "message": str(e2)})
    log.info(f"Engine tag added: runtime={rid} tag={tag} concurrent={body.max_concurrent} priority={body.priority}")
    return {"message": f"标签 {tag} 已添加", "tag": tag}


@router.put("/agent-runtimes/{rid}/tags/{tag}")
async def update_engine_tag(rid: int, tag: str, body: EngineTagUpdateBody,
                            user: dict = Depends(get_current_user)):
    """修改引擎标签配置"""
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    tag = tag.strip().lower()
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "UPDATE engine_capabilities SET max_concurrent=?, priority=?, enabled=?, updated_at=? "
                "WHERE runtime_id=? AND tag=?",
                (body.max_concurrent, body.priority, body.enabled, now, rid, tag)
            )
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "标签不存在"})
            _sync_agent_tags(rid, c)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    return {"message": f"标签 {tag} 已更新"}


@router.delete("/agent-runtimes/{rid}/tags/{tag}")
async def delete_engine_tag(rid: int, tag: str, user: dict = Depends(get_current_user)):
    """删除引擎标签"""
    from src.session_manager import db, db_lock
    tag = tag.strip().lower()
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=? AND tag=?", (rid, tag))
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "标签不存在"})
            _sync_agent_tags(rid, c)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    log.info(f"Engine tag deleted: runtime={rid} tag={tag}")
    return {"message": f"标签 {tag} 已删除"}
