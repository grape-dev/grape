"""DevStage 云开发环境 API 路由
使用 AK/SK 签名（IAM5.0）调用 DevStage API。
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.api.deps import get_current_user
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
    """从用户账户获取华为云 AK/SK"""
    acc = user_db.get_user_account(user["username"]) or {}
    ak = (acc.get("huawei_cloud_ak") or "").strip()
    sk = (acc.get("huawei_cloud_sk") or "").strip()
    if not ak or not sk:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "请在 General Settings 配置 Huawei Cloud Access Key/Secret Key"
        })
    return ak, sk


class CreateEnvBody(BaseModel):
    name: str
    grape_server: str = ""
    grape_token: str = ""


@router.get("/config")
async def get_devstage_config(user: dict = Depends(get_current_user)):
    """获取 DevStage 配置状态"""
    base_url = _resolve_base_url()
    acc = user_db.get_user_account(user["username"]) or {}
    return {
        "base_url": base_url,
        "configured": bool(base_url),
        "ak_value": acc.get("huawei_cloud_ak", ""),
        "sk_value": acc.get("huawei_cloud_sk", ""),
    }


@router.put("/config")
async def update_devstage_config(body: dict = {}, user: dict = Depends(get_current_user)):
    """更新 DevStage 配置"""
    url = (body.get("base_url") or "").strip().rstrip("/")
    if url:
        if not url.startswith("http"):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "API 地址必须以 http:// 或 https:// 开头"})
        user_db.set_meta_value("devstage_base_url", url)
    ak = (body.get("ak") or "").strip()
    sk = (body.get("sk") or "").strip()
    if ak or sk:
        acc = user_db.get_user_account(user["username"]) or {}
        if ak:
            acc["huawei_cloud_ak"] = ak
        if sk:
            acc["huawei_cloud_sk"] = sk
        user_db.save_user_account(user["username"], acc)
    return {"message": "DevStage 配置已更新"}


@router.get("/templates")
async def list_templates(user: dict = Depends(get_current_user)):
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


@router.get("/specs")
async def list_specs(template_id: str = "", user: dict = Depends(get_current_user)):
    """查询可用规格"""
    ak, sk = _get_ak_sk(user)
    try:
        specs = _list_specs(ak, sk, template_id)
        return {"specs": specs}
    except DevStageError as e:
        raise HTTPException(status_code=502, detail={"code": e.code, "message": str(e)})


GRAPE_AI_DEVSPACE_TEMPLATE = "4ef59a2b4d3d4909aacd80f6007c00b2"


@router.get("/environments")
async def list_environments(user: dict = Depends(get_current_user)):
    """查询所有 DevStage 开发环境（仅返回 Grape AI Devspace 模板创建的环境）"""
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
            if tpl == GRAPE_AI_DEVSPACE_TEMPLATE and env_vars.get("GRAPE_DEV_SERVER"):
                filtered.append(e)
        # 防御：如果过滤后为空但原始列表非空，说明 v1 detail 可能获取失败，
        # 此时回退到仅按 template_id 过滤（放宽条件，至少排除非 Grape 模板的环境）
        if not filtered and envs:
            tpl_only = [e for e in envs if (e.get("template_id") or "").strip() == GRAPE_AI_DEVSPACE_TEMPLATE]
            if tpl_only:
                log.warning(f"GRAPE_DEV_SERVER 过滤后为空，回退到仅按 template_id 过滤，匹配 {len(tpl_only)} 个")
                return {"environments": tpl_only}
            # template_id 也匹配不到，全部回退
            log.warning(f"过滤后为空但原始列表有 {len(envs)} 个环境，回退返回全部")
            return {"environments": envs}
        return {"environments": filtered}
    except DevStageError as e:
        raise HTTPException(status_code=502, detail={"code": e.code, "message": str(e)})


@router.post("/environments")
async def create_environment(body: CreateEnvBody, user: dict = Depends(get_current_user)):
    """创建开发环境（默认 AI DEVSPACE 模板，自动查规格，自动开机，自动注入凭据）"""
    ak, sk = _get_ak_sk(user)
    # 默认 AI DEVSPACE 模板
    template_id = "4ef59a2b4d3d4909aacd80f6007c00b2"
    try:
        # 查询规格（模板未发布/AK-SK 未授权时此处会抛 DevStageError）
        specs = _list_specs(ak, sk, template_id)
        if not specs:
            raise HTTPException(status_code=502, detail={"code": "NO_SPEC", "message": "未找到可用规格"})
        spec = specs[0]
        flavor_id = spec.get("flavor_id", "")
        cores = spec.get("env_cpu_size", 2)
        memory = spec.get("env_mem_size", 4)
        env_vars = None
        init_scripts = None
        if body.grape_server and body.grape_token:
            env_vars = {
                "GRAPE_DEV_SERVER": body.grape_server,
                "GRAPE_DEV_TOKEN": body.grape_token,
            }
            # init_script：DevSpace 环境为 root 用户，用 --root 走 systemd 管理进程；
            # --device 用环境名作为设备名（比 hostname 更可识别）。
            init_scripts = [{
                "command": (
                    f"curl -fsSL {body.grape_server}/api/runtime/install"
                    f" -o /tmp/install.sh && "
                    f"bash /tmp/install.sh --token {body.grape_token}"
                    f" --server {body.grape_server}"
                    f" --device {body.name}"
                    f" --root"
                )
            }]
        devstage_id = _create_env(
            ak, sk, name=body.name, cores=cores,
            arch="ARM", memory=memory,
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


@router.post("/environments/{action}")
async def environment_action(action: str, body: dict = {}, user: dict = Depends(get_current_user)):
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


@router.delete("/environments/{devstage_id}")
async def delete_environment(devstage_id: str, user: dict = Depends(get_current_user)):
    """删除开发环境，同时删除对应的 Worker 凭据"""
    ak, sk = _get_ak_sk(user)
    # 先查环境名（用于匹配凭据）
    try:
        envs = _list_envs(ak, sk)
        env_name = ""
        for e in envs:
            if e.get("id") == devstage_id:
                env_name = e.get("name", "")
                break
    except DevStageError:
        env_name = ""
    # 删除 DevStage 环境
    try:
        result = _delete_env(ak, sk, devstage_id)
    except DevStageError as e:
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
