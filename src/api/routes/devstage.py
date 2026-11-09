"""DevStage 云开发环境 API 路由
使用 AK/SK 签名（IAM5.0）调用 DevStage API。
"""
from fastapi import APIRouter, HTTPException, Depends
import os
from pydantic import BaseModel
from src.api.deps import require_no_session_token
from src.user import db as user_db
from src.im.devstage_client import (
    DevStageError,
    list_environments as _list_envs,
    create_environment as _create_env,
    start_environment as _start_env,
    stop_environment as _stop_env,
    delete_environment as _delete_env,
    list_specs as _list_specs,
    _resolve_base_url,
    _request,
)
from src.config import log

router = APIRouter(prefix="/api/devstage", tags=["devstage"])


def _get_ak_sk(user: dict) -> tuple:
    """从 user_credentials 表获取华为云 AK/SK（D32: 不查 user_accounts）"""
    cred = user_db.get_credential(user["id"], "huawei_cloud") or {}
    ak = (cred.get("ak") or "").strip()
    sk = (cred.get("sk") or "").strip()
    if not ak or not sk:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "请在 General Settings 配置 Huawei Cloud Access Key/Secret Key"
        })
    return ak, sk


def _annotate_worker_online(envs: list) -> None:
    """为每个环境附带 worker_online 字段。
    环境创建时凭据 name = env.name（见 _genCredForEnv / create_environment），
    所以 runtime_credentials.name == env.name 即为对应 Worker。
    用该凭据的 status + last_seen_at 判断 worker 是否真正上线（心跳 <5min）。
    """
    if not envs:
        return
    from src.session_manager import db, db_lock
    from src.runtime.health import derive_runtime_health
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc).timestamp()
    # 一次性查出所有 active 凭据的 name → health
    cred_map: dict[str, str] = {}
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT name, status, last_seen_at FROM runtime_credentials "
                "WHERE status != 'revoked'"
            )
            for cname, cstatus, last_seen in c.fetchall():
                if not cname:
                    continue
                h = derive_runtime_health(cstatus or "offline", last_seen, now_ts)
                cred_map[cname] = h
    except Exception as e:
        log.warning(f"_annotate_worker_online: DB query failed: {e}")
    # 注入到每个环境对象
    for e in envs:
        env_name = e.get("name") or ""
        health = cred_map.get(env_name, "offline")
        e["worker_online"] = (health == "online")


class CreateEnvBody(BaseModel):
    name: str
    grape_server: str = ""
    grape_token: str = ""


@router.get("/config", operation_id="devstage_config_get", summary="Get DevStage config", description="Gets the DevStage config state.")
def get_devstage_config(user: dict = Depends(require_no_session_token)):
    """获取 DevStage 配置状态"""
    base_url = _resolve_base_url()
    cred = user_db.get_credential(user["id"], "huawei_cloud") or {}
    return {
        "base_url": base_url,
        "configured": bool(base_url),
        "ak_exists": bool(cred.get("ak")),
        "sk_exists": bool(cred.get("sk")),
    }


@router.put("/config", operation_id="devstage_config_update", summary="Update DevStage config", description="Updates the DevStage config.")
def update_devstage_config(body: dict = {}, user: dict = Depends(require_no_session_token)):
    """更新 DevStage 配置"""
    url = (body.get("base_url") or "").strip().rstrip("/")
    if url:
        if not url.startswith("http"):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "API 地址必须以 http:// 或 https:// 开头"})
        user_db.set_meta_value("devstage_base_url", url)
    ak = (body.get("ak") or "").strip()
    sk = (body.get("sk") or "").strip()
    if ak or sk:
        # D32: 写入 user_credentials 表，不写 user_accounts
        cred = user_db.get_credential(user["id"], "huawei_cloud") or {}
        if ak:
            cred["ak"] = ak
        if sk:
            cred["sk"] = sk
        user_db.set_credential(user["id"], "huawei_cloud", cred)
    return {"message": "DevStage 配置已更新"}


@router.get("/templates", operation_id="devstage_templates", summary="List available templates", description="Lists available templates.")
def list_templates(user: dict = Depends(require_no_session_token)):
    """查询可用模板列表"""
    ak, sk = _get_ak_sk(user)
    import json
    try:
        r = _request("GET", "/open-api-public/v1/templates", ak, sk)
        content = (r if isinstance(r, dict) else {}).get("content", [])
        templates = []
        for t in content:
            cfg = t.get("config") or {}
            img = t.get("image_info") or {}
            templates.append({
                "uuid": t.get("uuid", ""),
                "name": t.get("name", ""),
                "image_name": img.get("name", ""),
                "container_type": t.get("container_type", ""),
                "is_public": t.get("is_public", False),
                "description": t.get("description", ""),
                "conditions": cfg.get("conditions", {}),
            })
        return {"templates": templates}
    except DevStageError as e:
        raise HTTPException(status_code=502, detail={"code": e.code, "message": str(e)})


@router.get("/specs", operation_id="devstage_specs", summary="List available specs", description="Lists available specs.")
def list_specs(template_id: str = "", user: dict = Depends(require_no_session_token)):
    """查询可用规格"""
    ak, sk = _get_ak_sk(user)
    try:
        specs = _list_specs(ak, sk, template_id)
        return {"specs": specs}
    except DevStageError as e:
        raise HTTPException(status_code=502, detail={"code": e.code, "message": str(e)})


def GRAPE_AI_DEVSPACE_TEMPLATE() -> str:
    """Grape AI Devspace 模板 ID 单一定义（Issue #470：原先模块级常量 +
    create_environment 内两处重复写死，现收敛到统一配置 getter）。

    优先级：管理台 meta > configs/runtime-config.json > 默认（现有模板）。
    保留原名称（list_environments 过滤、tests 引用），仅从常量变函数。
    """
    from src.config import get_devstage_template_id
    return get_devstage_template_id()


def _create_env_call_args(body: dict) -> dict:
    """create_environment 的云侧调用参数组装（Issue #470 抽取，便于单测）。

    规格从统一 getter 取（env GRAPE_DEVSTAGE_* > 管理台 meta > runtime-config.json
    > 默认 3c6g/ARM）——原写死 flavor_id/cores/memory/arch 与重复 template_id 已收敛。
    """
    from src.config import get_devstage_flavor
    f = get_devstage_flavor()
    return {
        "template_id": GRAPE_AI_DEVSPACE_TEMPLATE(),
        "flavor_id": f["flavor_id"],
        "cores": f["cores"],
        "memory": f["memory"],
        "arch": f["arch"],
    }


@router.get("/environments", operation_id="devstage_environments", summary="List dev environments", description="Lists DevStage environments created from the Grape AI Devspace template, with worker_online flags.")
def list_environments(user: dict = Depends(require_no_session_token)):
    """查询所有 DevStage 开发环境（仅返回 Grape AI Devspace 模板创建的环境）
    附带 worker_online 字段：环境对应的 Worker（device_name=env.name）是否已上线。
    """
    ak, sk = _get_ak_sk(user)
    try:
        envs = _list_envs(ak, sk)
        # 过滤：只显示通过 Grape 创建的环境
        # 条件 1: template_id 匹配 Grape AI Devspace 模板
        # 条件 2: environment_variables 中含 GRAPE_DEV_SERVER（Grape 创建时注入，
        #         用户在控制台手动用同模板创建的环境不会有此变量）
        filtered = []
        for e in envs:
            tpl = (e.get("template_id") or "").strip()
            env_vars = e.get("_env_vars") or {}
            if tpl == GRAPE_AI_DEVSPACE_TEMPLATE() and env_vars.get("GRAPE_DEV_SERVER"):
                filtered.append(e)
        # 防御：如果过滤后为空但原始列表非空，说明 v1 detail 可能获取失败，
        # 此时回退到仅按 template_id 过滤（放宽条件，至少排除非 Grape 模板的环境）
        if not filtered and envs:
            tpl_only = [e for e in envs if (e.get("template_id") or "").strip() == GRAPE_AI_DEVSPACE_TEMPLATE()]
            if tpl_only:
                log.warning(f"GRAPE_DEV_SERVER 过滤后为空，回退到仅按 template_id 过滤，匹配 {len(tpl_only)} 个")
                filtered = tpl_only
            else:
                # template_id 也匹配不到，全部回退
                log.warning(f"过滤后为空但原始列表有 {len(envs)} 个环境，回退返回全部")
                filtered = envs

        # 脱敏：_env_vars 含 GRAPE_DEV_TOKEN 等凭据，仅用于过滤，不返回前端
        for e in filtered:
            e.pop("_env_vars", None)

        # 附带 Worker 上线状态：环境创建时 --device {env.name}，所以
        # runtime_credentials.device_name == env.name 即为对应 Worker。
        _annotate_worker_online(filtered)

        return {"environments": filtered}
    except DevStageError as e:
        raise HTTPException(status_code=502, detail={"code": e.code, "message": str(e)})


@router.post("/environments", operation_id="devstage_environment_create", summary="Create dev environment", description="Creates a dev environment (default AI DEVSPACE template; auto specs, auto power-on, auto credential injection).")
def create_environment(body: CreateEnvBody, user: dict = Depends(require_no_session_token)):
    """创建开发环境（默认 AI DEVSPACE 模板，自动注入凭据）。

    规格写死（2026-10-15 调整）：不再先查 /v1/specs 模板规格——直接使用固定
    flavor_id/cores/memory 创建，避免模板规格查询失败（模板未发布/排序变化）阻塞环境创建；
    云侧规格下线/变更时可用 GRAPE_DEVSTAGE_* 环境变量覆盖兜底，失败错误码透传前端可定位。
    """
    ak, sk = _get_ak_sk(user)
    # Issue #470：模板 ID + 固定规格从统一配置取（env GRAPE_DEVSTAGE_* 覆盖兜底保留，
    # 云侧规格下线/变更时可经 env/管理台/runtime-config.json 调整，不再改代码）。
    _args = _create_env_call_args(body.model_dump())
    template_id = _args["template_id"]
    flavor_id = _args["flavor_id"]
    cores = _args["cores"]
    memory = _args["memory"]
    arch = _args["arch"]
    try:
        env_vars = None
        init_scripts = None
        if body.grape_server and body.grape_token:
            # 2026-10-15 修复（devspace 注入错地址）：前端传来的 grape_server 是
            # window.location.origin（用户浏览页面用的地址，如生产 www.grapedev.cn），
            # 不等于 CLI 可达的服务端基址（worker 心跳实际用 worker 回连地址，
            # 如 worker 回连地址）。以服务端权威值为准：前端值仅与
            # worker_connect_url 一致时采纳（dev 环境两者恰好相同），否则一律用
            # get_worker_connect_url()——从源头保证 RUNTIME_SERVER_URL 正确。
            # Issue #471 E1：改用拆分后的 worker 回连地址（未配置回退 server_base_url）。
            _server_base = ""
            try:
                from src.user.db import get_worker_connect_url
                _server_base = (get_worker_connect_url() or "").strip().rstrip("/")
            except Exception:
                pass
            _grape_server = (body.grape_server or "").strip().rstrip("/")
            if _server_base and _grape_server != _server_base:
                log.info(
                    f"devstage: grape_server overridden by worker_connect_url "
                    f"(frontend={_grape_server} -> {_server_base})")
                _grape_server = _server_base
            env_vars = {
                "GRAPE_DEV_SERVER": _grape_server,
                "GRAPE_DEV_TOKEN": body.grape_token,
                "AK": ak,
                "SK": sk,
            }
            # init_script：DevStage 不允许命令含 && ; | > < 等 shell 操作符（HD.32821013），
            # 所以拆成两条独立命令：先下载安装脚本，再执行。
            # DevSpace 容器为 root 用户，安装脚本自动检测 systemd 可用性
            # （容器无 systemd 时回退 nohup/setsid，不再强制 --root）。
            init_scripts = [
                {"command": f"curl -fsSL {_grape_server}/api/runtime/install -o /tmp/install.sh"},
                {"command": (
                    f"bash /tmp/install.sh --token {body.grape_token}"
                    f" --server {_grape_server}"
                    f" --device {body.name}"
                    f" --root"
                )},
            ]
        devstage_id = _create_env(
            ak, sk, name=body.name, cores=cores,
            arch=arch, memory=memory,
            flavor_id=flavor_id, template_id=template_id,
            env_vars=env_vars, init_scripts=init_scripts,
        )
        # 不自动开机：后台自动 start 会与用户手动点击开机按钮冲突，
        # 双重 start 导致环境状态回退到 cde.0001（创建中）。
        # 环境创建后进入 cde.0004（就绪），由用户在页面上点击开机。
        return {"devstage_id": devstage_id, "message": "创建成功"}
    except DevStageError as e:
        # 将 DevStageError 的 code + message 透传给前端，前端按 code 做精准提示
        raise HTTPException(status_code=502, detail={"code": e.code, "message": str(e)})


@router.post("/environments/{action}", operation_id="devstage_environment_action", summary="Dev environment start/stop", description="Starts/stops an environment.")
def environment_action(action: str, body: dict = {}, user: dict = Depends(require_no_session_token)):
    """开机/关机"""
    if action not in ("start", "stop"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "action must be start/stop"})
    devstage_id = (body.get("devstage_id") or "").strip()
    if not devstage_id:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "devstage_id required"})
    ak, sk = _get_ak_sk(user)
    try:
        if action == "start":
            return _start_env(ak, sk, devstage_id)
        else:
            return _stop_env(ak, sk, devstage_id)
    except DevStageError as e:
        raise HTTPException(status_code=502, detail={"code": e.code, "message": str(e)})


@router.delete("/environments/{devstage_id}", operation_id="devstage_environment_delete", summary="Delete dev environment", description="Deletes a dev environment and its worker credential.")
def delete_environment(devstage_id: str, user: dict = Depends(require_no_session_token)):
    """删除开发环境，同时删除对应的 Worker 凭据。

    Issue #513/#413：删除不存在的环境返回 404 统一包络（不回传上游云厂商
    错误码/文案），上游真实故障维持 502。两层判别：
    1. 前置 _list_envs 可枚举且无该 id → 直接 404（不发起注定失败的删除调用）；
    2. 删除调用抛 DevStageError 时按 not_found 分类（上游 parameter invalid 类
       错误码白名单，见 devstage_client._is_not_found_error）。
    """
    ak, sk = _get_ak_sk(user)
    # 先查环境名（用于匹配凭据）。列表可用时该 id 不在列表 = 环境不存在：
    # 直接 404（此前「不存在」信号在此被吞掉后仍发起删除，由上游报
    # parameter invalid 再被误映射 502）。
    env_name = ""
    found = False
    list_ok = False
    try:
        envs = _list_envs(ak, sk)
        list_ok = True
        for e in envs:
            if e.get("id") == devstage_id:
                found = True
                env_name = e.get("name", "")
                break
    except DevStageError:
        # 列表接口本身故障（网络/上游异常）：不能据此断定资源不存在，
        # 交由删除调用结果分类（避免把上游 list 故障误报成 404）。
        log.warning(f"devstage delete: pre-check list failed for {devstage_id}, fall through to delete")
    if list_ok and not found:
        raise HTTPException(status_code=404, detail={
            "code": 404,
            "message": "DevStage environment not found",
        })
    # 删除 DevStage 环境
    try:
        result = _delete_env(ak, sk, devstage_id)
    except DevStageError as e:
        if e.not_found:
            # 上游明确「资源不存在」（如 HD.97420111 parameter invalid）：
            # 翻译为本地 404 统一包络，不泄露上游错误码/文案
            log.info(f"devstage delete: upstream not-found for {devstage_id}: {e}")
            raise HTTPException(status_code=404, detail={
                "code": 404,
                "message": "DevStage environment not found",
            })
        raise HTTPException(status_code=502, detail={"code": e.code, "message": str(e)})
    # 删除对应的 Worker 凭据和 runtime（精确匹配环境名）
    if env_name:
        try:
            from src.session_manager import db as sm_db, db_lock as sm_lock
            with sm_lock:
                c = sm_db.cursor()
                c.execute("SELECT id FROM runtime_credentials WHERE name=?", (env_name,))
                row = c.fetchone()
                if row:
                    cid = row[0]
                    try:
                        c.execute("SELECT id FROM agent_runtimes WHERE credential_id=?", (cid,))
                        rids = [r[0] for r in c.fetchall()]
                        if rids:
                            ph = ",".join("?" * len(rids))
                            c.execute(f"DELETE FROM devops_agent_runtime_binding WHERE runtime_id IN ({ph})", rids)
                            c.execute(f"UPDATE devops_agent_session SET runtime_id=0 WHERE runtime_id IN ({ph})", rids)
                        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
                        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
                        log.info(f"删除环境 {env_name} 的凭据 (id={cid}) 及关联 runtime")
                    except Exception as ex:
                        log.warning(f"清理环境 {env_name} 的凭据失败: {ex}")
                sm_db.commit()
        except Exception as ex:
            log.warning(f"删除环境 {env_name} 的 Worker 清理失败: {ex}")
    return result
