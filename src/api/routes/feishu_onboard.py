"""飞书扫码一键创建应用 — OAuth 2.0 Device Authorization Grant (RFC 8628)

基于 lark-oapi 的 `aregister_app`：用户扫码创建飞书应用，自动获取 app_id/app_secret，
替代「手动创建应用 + 手动粘贴凭据」。创建成功后落库并动态建立 WS 长连接。

流程：start → 后台 asyncio task 运行 aregister_app → on_qr_code 回调写入 qr_url →
用户扫码确认 → SDK 返回 {client_id, client_secret, user_info} → 落库 + add_bot。

对应设计文档：docs/new-version2/feishu-qr-app-onboarding-plan.md
"""
import asyncio
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from src.api.deps import get_current_user
from src.config import log

router = APIRouter(prefix="/api/feishu/onboard", tags=["feishu-onboard"])

_STATUS_PENDING = "pending"
_STATUS_WAITING = "waiting_confirm"
_STATUS_CONNECTED = "connected"
_STATUS_FAILED = "failed"

# 后台任务注册表（进程内；跨进程状态以 DB 行为准，cancel 仅在同进程生效）
_TASKS: dict[str, asyncio.Task] = {}

# 预填权限/事件订阅：与 src/im/feishu_async.py 实际调用对齐
# 注意：addons 仅允许 scopes/events/callbacks 三个顶层键（lark-oapi 1.7.0 实测
# _normalize_addons 会拒绝 preset 等额外键并抛 ValueError）。平台默认即为基础模板，
# 因此不传 preset 等价于保留平台基础模板；敏感配置（事件 URL/security）不走 addons。
_ADDONS = {
    "scopes": {
        "tenant": [
            "im:message:send_as_bot",  # 发送消息（reply_text / reply_card / send_card_to_user）
            "im:chat:readonly",        # 读取群成员（get_chat_members 需要）
            "contact:user.base:readonly",  # 获取用户基本信息（get_user_name 取 feishu_name 需要，
                                           # feishu-app-scoped-openid-fix.md §2.4）
        ],
    },
    "events": {
        "items": {
            "tenant": ["im.message.receive_v1"],  # 项目唯一消费事件
        },
    },
}


class StartBody(BaseModel):
    bot_type: str = "chat_bot"  # chat_bot | vod_bot
    repo_key: str = ""


def _create_task(task_id: str, bot_type: str, repo_key: str, user_id: int):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO feishu_onboard_task (task_id, bot_type, repo_key, user_id, status) VALUES (?,?,?,?,?)",
            (task_id, bot_type, repo_key, user_id, _STATUS_PENDING),
        )
        db.commit()


def _update_task(task_id: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE feishu_onboard_task SET {cols} WHERE task_id=?", (*vals, task_id))
        db.commit()


def _get_task(task_id: str) -> dict | None:
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT task_id, bot_type, repo_key, user_id, status, app_id, app_secret, qr_url, expire_in, error FROM feishu_onboard_task WHERE task_id=?",
            (task_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "task_id": row[0], "bot_type": row[1], "repo_key": row[2], "user_id": row[3],
        "status": row[4], "app_id": row[5] or "", "app_secret": row[6] or "",
        "qr_url": row[7] or "", "expire_in": row[8] or 0, "error": row[9] or "",
    }


def _on_qr(task_id: str, info: dict):
    """SDK 二维码就绪回调（同步）：写入 qr_url + expire_in，状态 → waiting_confirm"""
    _update_task(
        task_id,
        status=_STATUS_WAITING,
        qr_url=info.get("url", "") or "",
        expire_in=int(info.get("expire_in", 0) or 0),
    )
    log.info(f"Feishu onboard: QR ready for task={task_id}, expire_in={info.get('expire_in')}")


def _apply_chat_credentials(user_id: int, app_id: str, app_secret: str) -> tuple[str, str]:
    """Chat Agent 落库：chat_agent_session + global_im_bot_config + 换绑清理。

    返回 (error_msg, old_app_id)。error_msg 非空表示校验/写入失败。
    """
    from src.session_manager import db, db_lock, get_chat_config, set_chat_config
    old_app_id = get_chat_config(user_id).get("im_app_id", "")
    with db_lock:
        c = db.cursor()
        # 强约束：一个 app_id 全局唯一。类型注册表校验 VOD 占用 + 其他用户占用
        c.execute("SELECT bot_type FROM global_im_bot_config WHERE bot_app_id=? AND is_active=1", (app_id,))
        row = c.fetchone()
        if row and row[0] == "vod_bot":
            return "该凭证已被 VOD Agent 使用，不能同时绑定 Chat Agent 和 VOD Agent", old_app_id
        c.execute(
            "SELECT user_id FROM chat_agent_session WHERE im_app_id=? AND user_id>0 AND user_id!=? AND im_app_id!='' LIMIT 1",
            (app_id, user_id),
        )
        other = c.fetchone()
        if other:
            return "该凭证已被其他用户使用，不能重复绑定", old_app_id
        # 换绑：旧 app_id 释放（无其他引用时从登记表删除）
        if old_app_id and old_app_id != app_id:
            c.execute(
                "SELECT COUNT(*) FROM chat_agent_session WHERE im_app_id=? AND user_id!=? AND im_app_id!=''",
                (old_app_id, user_id),
            )
            other_refs = c.fetchone()[0]
            if other_refs == 0:
                c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=? AND bot_type='chat_bot'", (old_app_id,))
            c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (old_app_id,))
            log.info(f"Feishu onboard: replaced old app_id={old_app_id[:20]} with {app_id[:20]}")
        c.execute(
            "INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?,?)",
            (app_id, "chat_bot"),
        )
        db.commit()
    set_chat_config(user_id, im_app_id=app_id, im_app_secret=app_secret, im_provider="feishu")
    return "", old_app_id


def _apply_vod_credentials(repo_key: str, app_id: str, app_secret: str) -> tuple[str, str]:
    """VOD Agent 落库：devops_agent_session + global_im_bot_config + 换绑清理。

    repo_key 统一经 to_repo_key 归一化（前端传 owner/repo，DB 存 gitcode:owner/repo）；
    若无该 repo 的 vod 配置行则 INSERT 新建（否则 UPDATE 匹配不到行，凭据丢失）。
    返回 (error_msg, old_app_id)。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    import uuid
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT bot_type FROM global_im_bot_config WHERE bot_app_id=? AND is_active=1", (app_id,))
        row = c.fetchone()
        if row and row[0] == "chat_bot":
            return "该凭证已被 Chat Agent 使用，不能同时绑定 VOD Agent 和 Chat Agent", ""
        # 换绑清理（对齐 agents.py update_agent_config）
        c.execute("SELECT id, im_app_id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod'", (repo_key,))
        old_row = c.fetchone()
        old_session_id = old_row[0] if old_row else ""
        old_app_id = old_row[1] if old_row else ""
        if old_app_id and old_app_id != app_id:
            c.execute("SELECT COUNT(*) FROM devops_agent_session WHERE im_app_id=? AND repo_key!=?", (old_app_id, repo_key))
            other_refs = c.fetchone()[0]
            if other_refs == 0:
                c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=?", (old_app_id,))
            c.execute(
                "DELETE FROM vod_conversations WHERE agent_session_id=?",
                (old_session_id,),
            )
            log.info(f"Feishu onboard: replaced vod app_id={old_app_id[:20]} with {app_id[:20]}")
        c.execute(
            "INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?,?)",
            (app_id, "vod_bot"),
        )
        if old_session_id:
            c.execute(
                "UPDATE devops_agent_session SET im_app_id=?, im_app_secret=?, im_provider='feishu' WHERE id=?",
                (app_id, app_secret, old_session_id),
            )
        else:
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, status, last_active_at, im_app_id, im_app_secret, im_provider) VALUES (?,?,?,?,datetime('now'),?,?,?)",
                (str(uuid.uuid4()), repo_key, "vod", "idle", app_id, app_secret, "feishu"),
            )
        db.commit()
    return "", old_app_id


async def _connect_bot(bot_type: str, repo_key: str, app_id: str, app_secret: str, old_app_id: str):
    """通过 BotManager 动态建连（换绑时先移除旧 bot）。"""
    try:
        from src.api.main import bot_manager
        if bot_manager and bot_manager.api:
            if old_app_id and old_app_id != app_id:
                await bot_manager.remove_bot(old_app_id)
                log.info(f"Feishu onboard: removed old bot {old_app_id[:20]}")
            await bot_manager.add_bot(app_id, app_secret, bot_type=bot_type)
            log.info(f"Feishu onboard: added bot {app_id[:20]} ({bot_type})")
    except Exception as e:
        log.warning(f"Feishu onboard add_bot failed: {e}")


async def _run_registration(task_id: str, bot_type: str, repo_key: str, user_id: int, username: str = ""):
    """后台运行 aregister_app 全流程。"""
    try:
        import lark_oapi as lark
        # 按 bot_type 区分飞书 App 使用名：chat_bot=个人助手；vod_bot=VOD 助手
        app_name = "Grape VOD助手" if bot_type == "vod_bot" else "Grape个人助手"
        result = await lark.aregister_app(
            on_qr_code=lambda info: _on_qr(task_id, info),
            addons=_ADDONS,
            app_preset={"name": app_name, "desc": "由 Grape DevOps 平台自动创建并绑定"},
        )
        app_id = result.get("client_id", "")
        app_secret = result.get("client_secret", "")
        # 扫码者（授权用户）的个人飞书 open_id：SDK 在 request_user_info=open_id 时返回，
        # 用于回填 unified_user_mapping.feishu_open_id，使任务/onboarding 飞书通知可用
        #（见 task-conversation-role-fix.md §9）
        open_id = (result.get("user_info") or {}).get("open_id", "")

        if bot_type == "vod_bot":
            err, old_app_id = _apply_vod_credentials(repo_key, app_id, app_secret)
        else:
            err, old_app_id = _apply_chat_credentials(user_id, app_id, app_secret)

        if err:
            _update_task(task_id, status=_STATUS_FAILED, error=err,
                         finished_at=datetime.now().isoformat())
            log.warning(f"Feishu onboard failed: task={task_id} err={err}")
            return

        # 扫码即绑定个人飞书身份（chat_bot / vod_bot 统一用扫码者 username 关联）
        if open_id and username:
            try:
                from src.session_manager import update_unified_user_info
                update_unified_user_info(username, open_id, gitcode_nickname=username, app_id=app_id)
                log.info(f"Feishu onboard: bound open_id for {username} (task={task_id[:8]})")
            except Exception as e:
                log.warning(f"Feishu onboard: bind open_id failed: {e}")

        _update_task(task_id, status=_STATUS_CONNECTED, app_id=app_id, app_secret=app_secret,
                     finished_at=datetime.now().isoformat())
        await _connect_bot(bot_type, repo_key, app_id, app_secret, old_app_id)
        log.info(f"Feishu onboard success: task={task_id} app_id={app_id[:20]}")
    except Exception as e:
        _update_task(task_id, status=_STATUS_FAILED, error=str(e),
                     finished_at=datetime.now().isoformat())
        log.warning(f"Feishu onboard exception: task={task_id} err={e}")
    finally:
        _TASKS.pop(task_id, None)


@router.post("/start")
async def start_onboard(body: StartBody, user: dict = Depends(get_current_user)):
    """创建扫码任务，返回 task_id（前端轮询 status 获取二维码/结果）。"""
    if body.bot_type not in ("chat_bot", "vod_bot"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "bot_type 仅支持 chat_bot / vod_bot"})
    if body.bot_type == "vod_bot" and not body.repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "vod_bot 需要 repo_key"})

    # 并发限制：同用户同时只允许一个进行中的扫码任务
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT COUNT(*) FROM feishu_onboard_task WHERE user_id=? AND status IN ('pending','waiting_confirm')",
            (user["id"],),
        )
        if c.fetchone()[0] > 0:
            raise HTTPException(status_code=409, detail={"code": 409, "message": "已有扫码配置正在进行，请稍候"})

    task_id = uuid.uuid4().hex
    _create_task(task_id, body.bot_type, body.repo_key, user["id"])
    task = asyncio.create_task(_run_registration(task_id, body.bot_type, body.repo_key,
                                                 user["id"], user.get("username", "")))
    _TASKS[task_id] = task
    log.info(f"Feishu onboard start: task={task_id} bot_type={body.bot_type} user={user['id']}")
    return {"task_id": task_id, "status": _STATUS_PENDING}


@router.get("/status")
async def onboard_status(task_id: str = Query(...), user: dict = Depends(get_current_user)):
    """查询扫码任务状态（前端轮询）。"""
    t = _get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    if t["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权查看该任务"})
    return {
        "task_id": t["task_id"],
        "status": t["status"],
        "qr_url": t["qr_url"],
        "expire_in": t["expire_in"],
        "app_id": t["app_id"],
        "app_secret_exists": bool(t["app_secret"]),
        "error": t["error"],
    }


class CancelBody(BaseModel):
    task_id: str = ""


@router.post("/cancel")
async def cancel_onboard(body: CancelBody, user: dict = Depends(get_current_user)):
    """取消进行中的扫码任务。已 connected 的任务取消为幂等，不影响已保存配置。"""
    t = _get_task(body.task_id)
    if not t or t["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    if t["status"] in (_STATUS_CONNECTED, _STATUS_FAILED):
        return {"message": "任务已结束，无需取消", "status": t["status"]}
    task = _TASKS.pop(body.task_id, None)
    if task and not task.done():
        task.cancel()
    _update_task(body.task_id, status=_STATUS_FAILED, error="用户取消",
                 finished_at=datetime.now().isoformat())
    log.info(f"Feishu onboard cancel: task={body.task_id} user={user['id']}")
    return {"message": "已取消", "status": _STATUS_FAILED}
