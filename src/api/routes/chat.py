"""Chat Agent 路由：对话、消息、配置、反馈"""
import json, asyncio, hashlib, tempfile, os, threading, time

from src.config import WORKSPACE_DIR, CHAT_CWD_BASE, log
from src.prompt_builder import build_general_chat_prompt, resolve_task_server_env
from src.auth.grape_account_db import get_grape_account_by_username as _get_acc
from src.token_service import is_auth_401_error

def _get_chat_cwd(user_id: int, repo_key: str, conv_id: str) -> str:
    """每个 conversation 独立工作目录，避免相互影响。

    统一结构 WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}，与飞书 IM 严格一致
    （同一 conv_id 同一路径，见 web-chat-no-response-cwd-fix.md §4）。
    远端 worker 执行时按 _remap_cwd 映射到各自用户 home；本机 direct 执行 root 可写。
    """
    from src.repo_key import workspace_repo_dir
    path = os.path.join(workspace_repo_dir(repo_key or ""),
                        "chat", str(user_id), conv_id)
    os.makedirs(path, exist_ok=True)
    return path
# 限制同时运行的 opencode 子进程数（防止大量用户并发导致服务器崩溃）
_CHAT_AGENT_SEMAPHORE = threading.Semaphore(20)
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.api.deps import get_current_user_or_api_key, require_no_session_token
from src.user import db as user_db
from src.session_manager import (
    create_conversation, get_user_conversations, get_conversation,
    add_message, get_messages, touch_conversation, update_conversation,
    delete_messages_by_conv, delete_conversation,
    get_chat_config as _get_chat_config,
    set_chat_config as _set_chat_config,
    mark_task_processing_by_conversation,
    get_message, recall_message, recall_agent_replies_by_task, is_task_recalled,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 支持的 IM 渠道（Issue #956 provider 化 → #1254 加 wecom → #1309 加 slack）。
# 单一来源：回显归一、保存校验、解绑清理三处共用，避免新增渠道时漏改其中一处。
# Slack 是双令牌渠道：im_app_id 存 Bot Token（xoxb-）、im_app_secret 存 App Token（xapp-）。
_IM_PROVIDERS = ("feishu", "dingtalk", "wecom", "slack", "telegram", "wechat")


def _safe_im_app_ref(app_id: str, provider: str = "") -> str:
    """日志里的 IM app 标识：Slack 的 app_id 位存 Bot Token → 只输出不可逆摘要。

    非 Slack 渠道保持既有前缀；Slack 渠道或形如 xoxb-/xapp- 的值一律摘要，
    绝不把令牌片段写进日志。
    """
    value = str(app_id or "")
    prov = str(provider or "").strip().lower()
    if prov == "slack" or value.startswith(("xoxb-", "xapp-")):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"slack:{digest}"
    if prov == "telegram" or _looks_like_telegram_token(value):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"telegram:{digest}"
    if prov == "wechat":
        # 微信 ilink bot_token 敏感；app_id 位存 ilink_bot_id（非密），日志仍摘要化避免串号
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"wechat:{digest}"
    return value[:30]


def _looks_like_telegram_token(value: str) -> bool:
    """Telegram Bot Token 形态 `123456:ABC-DEF…` → 日志只留摘要，不落明文片段。"""
    head, sep, tail = str(value or "").partition(":")
    return bool(sep and head.isdigit() and len(tail) >= 10)


class ChatMessage(BaseModel):
    message: str
    repo_key: str = ""
    conversation_id: str = ""
    image_data: str = ""  # JSON array of base64 strings, or single base64 string
    video_data: str = ""  # base64 encoded video data


class FeedbackBody(BaseModel):
    seq: int
    feedback_type: str = "create_issue"
    comment: str = ""


# ====== Conversations ======

@router.get("/analyze-image")
async def analyze_image(request: Request):
    """供 agent curl 调用：下载图片 → VLM 分析 → 返回文字描述。

    agent 在 issue body 里看到图片链接后，curl 调用本端点获取图片内容描述。
    VLM 管线三级降级：qwen2.5-vl-72b 真视觉 → OCR+LLM 猜图 → 纯 OCR。

    注意：用 Request 而非 url 参数，因为 OBS 签名 URL 含未编码的 &（AccessKeyId=…&Signature=…），
    FastAPI 的 query parameter 解析会按 & 截断，导致 url 只拿到第一个参数。
    从原始 query string 手动提取 url= 后的完整内容。
    """
    from urllib.parse import urlparse
    import tempfile, urllib.request

    # 从原始 query string 提取 url= 后的完整值（含 & 未编码的 OBS 签名参数）
    raw_qs = request.url.query
    if raw_qs.startswith("url="):
        url = raw_qs[4:]
    else:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing url parameter"})

    # 域名白名单（防 SSRF）
    parsed = urlparse(url)
    _allowed = ("cdn-img.gitcode.com", "cdn-files.gitcode.com")
    _allowed_suffixes = (".myhuaweicloud.com",)  # OBS 附件域名（桶名动态）
    if parsed.hostname not in _allowed and not any(parsed.hostname.endswith(s) for s in _allowed_suffixes):
        return {"error": "domain not allowed", "url": url}

    # 下载图片到临时文件
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    try:
        urllib.request.urlretrieve(url, tmp.name)
    except Exception as e:
        os.unlink(tmp.name)
        return {"error": f"download failed: {e}", "url": url}

    # 调 image_understanding 分析
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("image_understanding",
            os.path.join(os.path.dirname(__file__), "..", "..", "agent", "image_understanding.py"))
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        desc = _mod.understand_image(tmp.name)
    except Exception as e:
        log.warning(f"analyze-image: understanding failed: {e}")
        desc = ""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    return {"url": url, "description": desc or "(无法分析图片内容)"}


@router.get("/image/{conv_id}/{filename}")
def serve_chat_image(conv_id: str, filename: str, thumb: str = Query(default="")):
    """Serve chat image file. No auth — images are referenced by unguessable timestamp filenames.

    When thumb=1 is passed, serves a dynamically compressed thumbnail (max 300px, JPEG q75).
    """
    import os as _os
    from fastapi.responses import FileResponse, Response
    _grape_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    # Sanitize path — only allow alphanumeric/dash/underscore in conv_id and filename
    import re as _re
    if not _re.match(r'^[a-zA-Z0-9_\-]+$', conv_id) or not _re.match(r'^[a-zA-Z0-9_\-]+\.(png|jpg|jpeg)$', filename):
        raise HTTPException(status_code=400, detail="Invalid path")
    fpath = _os.path.join(_grape_root, "data", "chat_images", conv_id, filename)
    if not _os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Image not found")

    # Thumbnail mode: dynamically compress to small JPEG
    if thumb == "1":
        try:
            from PIL import Image as _PILImg
            import io as _io
            pil_img = _PILImg.open(fpath)
            if pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")
            max_dim = 300
            w, h = pil_img.size
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                pil_img = pil_img.resize((int(w * scale), int(h * scale)), getattr(_PILImg, 'Resampling', _PILImg).LANCZOS)
            buf = _io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=75, optimize=True)
            return Response(content=buf.getvalue(), media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
        except Exception:
            pass  # fallback to original

    _mt = "image/jpeg" if fpath.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return FileResponse(fpath, media_type=_mt, headers={"Cache-Control": "public, max-age=86400"})

@router.get("/conversations", operation_id="chat_conversations", summary="List conversations", description="Lists conversations (optionally filtered by repo_key).")
def list_conversations(repo_key: str = Query(default=""), user: dict = Depends(require_no_session_token)):
    convs = get_user_conversations(user["id"], repo_key=repo_key)
    return {"conversations": convs}


class NewConversationBody(BaseModel):
    repo_key: str = ""


@router.post("/conversations", operation_id="chat_conversations_create", summary="Create conversation", description="Creates a new chat conversation for a repo_key.")
def new_conversation(body: NewConversationBody, user: dict = Depends(require_no_session_token)):
    cid = create_conversation(user["id"], repo_key=body.repo_key)
    return {"conversation_id": cid}


@router.delete("/conversations/{conv_id}", operation_id="chat_conversations_delete", summary="Delete conversation", description="Deletes a conversation and its messages.")
def remove_conversation(conv_id: str, user: dict = Depends(require_no_session_token)):
    conv = get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "对话未找到"})
    delete_messages_by_conv(conv_id)
    delete_conversation(conv_id)
    return {"message": "对话已删除"}


class EnsureIssueConvBody(BaseModel):
    repo_key: str
    issue_number: int


@router.post("/conversations/ensure-issue", operation_id="chat_conversations_ensure_issue", summary="Ensure issue conversation", description="Finds or creates an Issue-scoped conversation by repo_key + issue_number (used by task-center jump). Rebuilds a deleted 'Issue #N' conversation with task context.")
def ensure_issue_conversation(body: EnsureIssueConvBody, user: dict = Depends(require_no_session_token)):
    """基于 repo_key + issue_number 查找或新建任务会话（Task Center 跳转用）。

    会话被删除后，Task Center 双击跳转时调用此接口重建「Issue #N」会话，
    并写入任务上下文消息 + 从 GitCode Issue 补拉 agent 评论。
    """
    from src.api.issues_center import _ensure_task_conversation, _get_issue_title
    from src.repo_key import to_repo_key
    rk = to_repo_key(body.repo_key)
    title = ""
    try:
        title = _get_issue_title(rk, body.issue_number)
    except Exception:
        pass
    conv_id = _ensure_task_conversation(user["id"], rk, body.issue_number, title=title)
    if not conv_id:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "创建会话失败"})
    # 更新 task 表中的 conversation_id 关联
    try:
        from src.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            c.execute("UPDATE issue SET conversation_id=? WHERE repo_key=? AND issue_number=?",
                      (conv_id, rk, body.issue_number))
            _db.commit()
    except Exception:
        pass
    # 从 GitCode Issue 补拉 agent 评论到新会话
    try:
        from src.api.event_pipeline import _sync_issue_comments_to_conversation
        from src.repo_key import from_repo_key
        from src.repo import Repo
        _repo = from_repo_key(rk)
        _token = Repo(rk).bot_token
        if _repo and _token:
            _sync_issue_comments_to_conversation(rk, body.issue_number, _token, repository=_repo)
    except Exception:
        pass
    return {"conversation_id": conv_id}


# ====== Messages ======

@router.get("/messages/{conv_id}", operation_id="chat_messages", summary="List conversation messages", description="Lists messages of a conversation (limit/before pagination).")
def list_messages(conv_id: str, before: int = Query(0, ge=0), user: dict = Depends(require_no_session_token)):
    conv = get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "对话未找到"})
    # 任务会话：打开时自动从 GitCode Issue 补拉 agent 评论到会话
    # （agent 通过技能 curl 在 Issue 上发的澄清追问等评论，可能未同步到会话）
    if not before and conv.get("conv_type") == "task" and conv.get("issue_number"):
        try:
            from src.api.event_pipeline import _sync_issue_comments_to_conversation
            from src.repo_key import from_repo_key
            from src.repo import Repo
            _rk = conv.get("repo_key", "")
            _repo = from_repo_key(_rk) if _rk else ""
            _token = Repo(_rk).bot_token if _rk else ""
            if _repo and _token:
                _sync_issue_comments_to_conversation(_rk, conv["issue_number"], _token, repository=_repo)
        except Exception:
            pass  # 旁路同步，不影响消息加载
    msgs = get_messages(conv_id, before_id=before)
    return {"messages": msgs, "conversation": conv}


# Issue #442: chat_task_id → 远端 enqueue_tid 映射（撤回时经 #136 cancel_task 终止远端任务）
_CHAT_ENQUEUE_MAP: dict[str, str] = {}


@router.post("/messages/{msg_id}/recall", operation_id="chat_recall_message", summary="Recall a message", description="Soft-deletes own user message and terminates the agent execution it triggered (reuses #136 stop mechanism). The original text is kept for re-editing.")
async def recall_chat_message(msg_id: int, user: dict = Depends(require_no_session_token)):
    """撤回本人已发送消息（Issue #442）。

    - 仅限本人会话内的 user 消息（他人消息/agent 消息拒绝）
    - 软删除（deleted=1，保留原文供「重新编辑」），同轮 agent 回复一并软删除
    - 先置撤回状态，再终止该轮执行：
      - direct 模式：ChatTaskManager.cancel（cancelled 后丢弃后续状态推送）
      - remote 模式：service.cancel_task(enqueue_tid)（复用 #136，worker 心跳感知杀引擎）
    - 幂等：已撤回消息重复调用返回 200 {recalled: true}
    """
    from src.api.chat_task_manager import ChatTaskManager

    msg = get_message(msg_id)
    if not msg:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "消息不存在"})
    conv = get_conversation(msg["conversation_id"])
    if not conv or conv["user_id"] != user["id"]:
        # 不泄露他人消息存在性
        raise HTTPException(status_code=404, detail={"code": 404, "message": "消息不存在"})
    if msg["role"] != "user":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仅支持撤回自己发送的消息"})
    if msg.get("deleted"):
        task_id = msg.get("task_id") or ""
        return {"recalled": True, "message_id": msg_id, "task_id": task_id, "terminated": False}

    task_id = msg.get("task_id") or ""
    # 1) 先置撤回状态（竞态防护：推送侧/落库侧据此丢弃）
    recall_message(msg_id)
    # 2) 同轮 agent 回复一并软删除
    recall_agent_replies_by_task(task_id)
    # 3) 终止该轮执行
    terminated = ChatTaskManager.cancel(task_id, reason="用户撤回消息，终止执行")
    # 远端模式终止（复用 #136）：进程内映射是同主控的快路径；跨主控时映射缺失，此时
    # 由任务归属主控的轮询循环（remote_agent_fn）感知撤回标记后自行 cancel_task（≤0.5s）。
    # 若消息 task_id 本身就是 agent_tasks.id（如 t_xxx），直接 DB 置 cancelled（跨节点安全）。
    enqueue_tid = _CHAT_ENQUEUE_MAP.get(task_id, "")
    if not enqueue_tid and task_id and not str(task_id).startswith("chat_"):
        enqueue_tid = task_id
    if enqueue_tid:
        try:
            from src.runtime.service import cancel_task as _svc_cancel_task
            _svc_cancel_task(enqueue_tid)
            terminated = True
        except Exception as e:
            log.warning(f"[Chat recall] cancel remote task failed: enqueue_tid={enqueue_tid} err={e}")
    elif task_id:
        # 跨主控：撤回请求落在非归属主控（进程内映射不可见）→ 无法在此终止。
        # 由归属主控的轮询循环（remote_agent_fn）感知共享库撤回标记后 cancel_task（≤0.5s）。
        log.info(f"[Chat recall] cross-master recall: local map miss task_id={task_id}; "
                 f"the owning master's loop will cancel the enqueued task")
    log.info(f"[Chat recall] msg={msg_id} conv={msg['conversation_id']} task={task_id} "
             f"terminated={terminated} user={user['username']}")
    return {"recalled": True, "message_id": msg_id, "task_id": task_id, "terminated": terminated}


# ====== Send ======

def _auto_title(conv: dict, message: str):
    """首次消息时自动生成对话标题"""
    if conv and not conv.get("title") and message:
        title = message.strip()[:30]
        if len(message) > 30:
            title += "..."
        update_conversation(conv["id"], title=title)


_feishu_push_ctx_cache: dict = {}


def _feishu_push_ctx(app_id: str, app_secret: str):
    """按 app_id 复用 BotContext（token 缓存挂在 ctx 上，避免每次推送重新换 token）。"""
    ctx = _feishu_push_ctx_cache.get(app_id)
    if ctx is None:
        from src.im.feishu_async import BotContext
        ctx = BotContext(app_id=app_id, app_secret=app_secret, bot_type="chat_bot")
        _feishu_push_ctx_cache[app_id] = ctx
    return ctx


async def _push_reply_to_feishu(conv: dict | None, user: dict, text: str):
    """web 答复推送到飞书（feishu-web-chat-unified-design.md §3.2）。

    conversation 已被飞书接入（im_channel_id 非空）时，用该用户当前 **活跃的飞书安装**
    （读 grape_t_channel_installation，旧 IM 下线 A2 M2 后不再经 BotManager）主动推送答复。

    说明：每次推送按 app_id 复用一份 FeishuAsync（同一主事件循环内使用）。
    """
    try:
        im_channel_id = (conv or {}).get("im_channel_id", "")
        if not im_channel_id:
            return
        from src.api.issues_center import _get_feishu_open_id
        _repo_key = (conv or {}).get("repo_key", "")
        open_id = _get_feishu_open_id(user.get("username", ""), _repo_key)
        if not open_id and im_channel_id.startswith("ou_"):
            open_id = im_channel_id
        if not open_id:
            return
        from src.im.engine_wiring import get_active_installation
        inst = get_active_installation("user", owner_user_id=str(user.get("id") or ""),
                                       channel_type="feishu")
        if not inst:
            return
        cfg = inst.get("config") or {}
        app_id, app_secret = str(cfg.get("app_id", "")), str(cfg.get("app_secret", ""))
        if not app_id or not app_secret:
            return
        from src.im.engine_wiring import feishu_api
        from src.im.feishu_async import BotContext
        api = feishu_api(app_id)
        ctx = _feishu_push_ctx(app_id, app_secret)
        await api.send_card_to_user(ctx, open_id, f"[来自Web] {text[:1800]}", title="Chat Agent")
        log.info(f"Chat web->feishu push: conv={(conv or {}).get('id')} "
                 f"open_id={open_id[:20]} text_len={len(text)}")
    except Exception as e:
        log.warning(f"Chat web->feishu push failed: {e}")


def _resolve_gitcode_token(user_token: str, repo_key: str = "") -> str:
    """Resolve GitCode token for agent injection (Issue #344: no session cache).

    R18.6.14: 改为调 TokenResolver.resolve()，统一走策略链。
    - 有 user_token 时直接用（Chat 场景传入的当前用户 token）
    - 无 user_token 时调 resolve_repo_token() 走 TokenResolver 策略链获取
    R20.8.4: token 过期时不重试（TokenResolver 短路返回空串，agent 不注入 token）。
    """
    if user_token:
        return user_token
    if repo_key:
        try:
            from src.token_service import resolve_repo_token
            _result = resolve_repo_token(repo_key)
            if _result.token:
                return _result.token
            # R20.8.1: token 过期或未配置 → 检测并标记
            from src.token_service import detect_and_mark_expired
            _error_code = detect_and_mark_expired(repo_key)
            if _error_code:
                log.warning(f"Chat resolve token: token expired (error_code={_error_code}), repo={repo_key}")
        except Exception as e:
            log.warning(f"Chat resolve token failed: {e}")
    return ""


def _build_web_user_info(user: dict, acc: dict | None = None) -> str:
    """构建 Web Chat prompt 的用户基本信息（与飞书 user_info 格式一致）。

    user 为 FastAPI 当前用户 dict（username 即 GitCode 登录名）；
    acc 为 grape_accounts 行（含 nickname）。注入后使 Web Agent
    感知说话者身份（chat-prompt-overview.md §2.1，补 Web Chat 无 user_info 缺口）。
    """
    try:
        username = (user.get("username") or "").strip()
        if not username:
            return ""
        lines = ["## 用户基本信息"]
        lines.append(f"  GitCode login: {username}")
        nickname = (acc or {}).get("nickname") or ""
        if nickname and nickname != username:
            lines.append(f"  GitCode 昵称：{nickname}")
        return "\n".join(lines)
    except Exception:
        return ""


def _save_chat_images(image_data: str, conv_id: str) -> list:
    """Save image(s) from base64 data to filesystem, return list of URL paths."""
    if not image_data:
        return []
    try:
        import base64 as _b64, os as _os, time as _time
        s = image_data.strip()
        if s.startswith("["):
            images = json.loads(s)
        else:
            images = [s]
        # Save to grape project data/chat_images/
        _grape_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
        img_dir = _os.path.join(_grape_root, "data", "chat_images", conv_id)
        _os.makedirs(img_dir, exist_ok=True)
        urls = []
        for i, b64 in enumerate(images):
            if not b64:
                continue
            raw = b64
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[-1]
            try:
                img_bytes = _b64.b64decode(raw)
            except Exception:
                continue
            # Compress with PIL: resize max 800px, save as JPEG q85
            try:
                from PIL import Image as _PILImg
                import io as _io
                pil_img = _PILImg.open(_io.BytesIO(img_bytes))
                # Convert RGBA/L/P → RGB for JPEG
                if pil_img.mode in ("RGBA", "LA", "P"):
                    pil_img = pil_img.convert("RGB")
                elif pil_img.mode != "RGB":
                    pil_img = pil_img.convert("RGB")
                # Resize if larger than 800px
                max_dim = 800
                w, h = pil_img.size
                if max(w, h) > max_dim:
                    scale = max_dim / max(w, h)
                    pil_img = pil_img.resize((int(w * scale), int(h * scale)), getattr(_PILImg, 'Resampling', _PILImg).LANCZOS)
                buf = _io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85, optimize=True)
                img_bytes = buf.getvalue()
            except Exception as e:
                log.warning(f"Chat: image compress failed, using raw: {e}")
            ts = int(_time.time() * 1000)
            fname = f"{ts}_{i}.jpg"
            fpath = _os.path.join(img_dir, fname)
            with open(fpath, "wb") as f:
                f.write(img_bytes)
            urls.append(f"/api/chat/image/{conv_id}/{fname}")
            log.info(f"Chat: saved image {fpath} ({len(img_bytes)} bytes)")
        return urls
    except Exception as e:
        log.warning(f"Chat: save images failed: {e}")
        return []


def _build_image_markers(urls: list) -> str:
    """Build [chat_img:url] markers for message content."""
    if not urls:
        return ""
    return "\n" + "\n".join(f"[chat_img:{u}]" for u in urls)


def _understand_image(image_data: str) -> str:
    """Analyze image(s) from base64 data and return description for prompt injection.

    Supports: JSON array of base64 strings (multi-image), or single base64 string.
    """
    if not image_data:
        return ""

    try:
        import importlib.util as _ilu
        import os as _os
        _spec = _ilu.spec_from_file_location("image_understanding",
            _os.path.join(_os.path.dirname(__file__), "..", "..", "agent", "image_understanding.py"))
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)

        # Parse image_data: could be JSON array or single string
        images = []
        s = image_data.strip()
        if s.startswith("["):
            images = json.loads(s)
        else:
            images = [s]

        parts = []
        for i, b64 in enumerate(images):
            if not b64:
                continue
            desc = _mod.understand_image_from_base64(b64)
            if desc:
                if len(images) > 1:
                    parts.append(f"### 图片 {i+1}\n{desc}")
                else:
                    parts.append(desc)
            else:
                if len(images) > 1:
                    parts.append(f"### 图片 {i+1}\n用户上传了一张图片，但未能提取有效信息。")
                else:
                    parts.append("用户上传了一张图片，但未能提取有效信息。")

        if not parts:
            return ""
        result = "\n\n".join(parts)
        if len(images) > 1:
            result = f"## 用户上传了 {len(images)} 张图片\n\n{result}\n\n## 请基于以上图片信息回答用户问题"
        else:
            result = result + "\n\n## 请基于以上图片信息回答用户问题"
        log.info(f"Web Chat: {len(images)} image(s) understood, {len(result)} chars total")
        return result
    except Exception as e:
        log.warning(f"Web Chat: image understanding failed: {e}")
        return f"用户上传了图片，但分析出错: {e}"


def _understand_video(video_data: str) -> str:
    """Analyze video from base64 data and return description for prompt injection."""
    if not video_data:
        return ""
    try:
        import importlib.util as _ilu
        import os as _os
        _spec = _ilu.spec_from_file_location("image_understanding",
            _os.path.join(_os.path.dirname(__file__), "..", "..", "agent", "image_understanding.py"))
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)

        raw = video_data
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        import base64 as _b64
        desc = _mod.understand_video_from_bytes(_b64.b64decode(raw))
        if desc:
            log.info(f"Web Chat: video understood, {len(desc)} chars")
            return desc
        return "用户上传了一个视频，但未能提取有效信息。"
    except Exception as e:
        log.warning(f"Web Chat: video understanding failed: {e}")
        return f"用户上传了视频，但分析出错: {e}"


# direct 模式 chat 会话凭据注入（chat-direct-session-token-injection-design.md / feishu-task 版）：
# 公共 helper 与飞书 IM 共用，防两组逻辑漂移（src/api/chat_session_env.py）。
from src.api.chat_session_env import chat_session_env_extra


@router.post("/send", operation_id="chat_send", summary="Send message", description="Sends a chat message and returns the full assistant reply (text/structured).")
async def send_message(body: ChatMessage, user: dict = Depends(require_no_session_token)):
    conv_id = body.conversation_id
    # Issue #344: 不再缓存明文 token 到 chat_conversations，agent 端实时从 external_identities 获取
    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
    else:
        conv_id = create_conversation(user["id"], body.repo_key)
        conv = get_conversation(conv_id)

    add_message(conv_id, "user", body.message + _build_image_markers(_save_chat_images(body.image_data, conv_id)))

    # Issue #51: Chat Agent 指令检测（任务完成/取消任务/恢复任务）
    _msg_stripped = body.message.strip()
    if conv_id and conv and conv.get("conv_type") == "task" and conv.get("issue_number"):
        _task_issue_num = conv["issue_number"]
        _task_repo_key = conv.get("repo_key", "")
        import logging as _log_mod
        _chat_log = _log_mod.getLogger("devops-bot")
        if _msg_stripped in ["任务完成", "完成任务", "/done", "确认完成"]:
            try:
                from src.api.issues_center import confirm_issue_done
                confirm_issue_done(_task_repo_key, _task_issue_num, confirmed_by="chat_agent")
                add_message(conv_id, "assistant", "✅ 任务已确认完成")
                return {"conversation_id": conv_id, "reply": "✅ 任务已确认完成", "task_command": "done"}
            except Exception as _e:
                _chat_log.warning(f"Chat command confirm_done failed: {_e}")
        elif _msg_stripped in ["取消任务", "/cancel"]:
            try:
                from src.api.issues_center import cancel_issue_task
                cancel_issue_task(_task_repo_key, _task_issue_num)
                add_message(conv_id, "assistant", "任务已取消")
                return {"conversation_id": conv_id, "reply": "任务已取消", "task_command": "cancel"}
            except Exception as _e:
                _chat_log.warning(f"Chat command cancel failed: {_e}")
        elif _msg_stripped in ["恢复任务", "/resume"]:
            try:
                from src.api.issues_center import resume_issue_task
                resume_issue_task(_task_repo_key, _task_issue_num)
                add_message(conv_id, "assistant", "任务已恢复")
                return {"conversation_id": conv_id, "reply": "任务已恢复", "task_command": "resume"}
            except Exception as _e:
                _chat_log.warning(f"Chat command resume failed: {_e}")

    # 任务会话收到真实用户消息 → active→processing（task-status-processing-trigger-fix.md §3.1）
    mark_task_processing_by_conversation(conv_id)

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    skill_name = cfg.get("skill", "") or "chat"
    skill_id = cfg.get("skill_id", 0) or 0
    acc = _get_acc(user["username"]) or {}
    history = get_messages(conv_id, limit=20) if body.conversation_id else []
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    # Issue #344: 实时获取用户 token（带预刷新），不再从会话缓存读取
    from src.api.deps import get_user_token_with_refresh
    user_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
    gitcode_token = _resolve_gitcode_token(user_token, repo_key)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    # 任务会话走独立 prompt（注入 Issue 上下文 + 场景: task）；一般会话走通用 prompt
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_issue_chat_prompt
        prompt = build_issue_chat_prompt(body.message, repo_key, history, conv, account=user["username"], runtime_id=cfg.get("runtime_id", 0), has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(body.message, repo_key, history, skill_name, skill_id,
                                           backend_name=backend_name, user_info=_build_web_user_info(user, acc),
                                           has_valid_session=has_valid_session)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""

    from src.config import log
    log.info(f"Chat send: backend={backend_name or '(none)'}, skill={skill_name}, has_token={bool(gitcode_token)}, user_msg={body.message[:100]}")

    # R20.8.1: token 过期/未配置时处理
    # 仅对 task 会话阻断（回帖 Issue 需要 token）；普通对话不阻断（agent 可做纯聊天/非 VCS 操作）
    if not gitcode_token and repo_key:
        _is_task_conv = conv and conv.get("conv_type") == "task"
        if _is_task_conv:
            # task 会话需要 token 回帖 Issue → 阻断
            _warn = "⚠️ Token 已过期或未配置，请在设置页重新绑定 Token 后重试"
            add_message(conv_id, "agent", _warn)
            return {"reply": _warn, "conversation_id": conv_id, "error_code": "TOKEN_EXPIRED"}
        else:
            # 普通对话不阻断，降级为 warning（agent 可做纯聊天）
            log.warning(f"Chat send: no token for repo={repo_key}, but non-task conv — agent will start without VCS token")

    # 图片理解注入
    if body.image_data:
        prompt += "\n\n" + _understand_image(body.image_data)

    # 视频理解注入
    if body.video_data:
        prompt += "\n\n" + _understand_video(body.video_data)

    # 无任何引擎（未配置本机直连/远端）→ 明确不可用，绝不隐式跑 opencode（engine-selection-design.md v2）
    if not backend_name:
        _warn = "⚠️ 无可用引擎（未配置本机直连或远端引擎）"
        add_message(conv_id, "agent", _warn)
        await _push_reply_to_feishu(conv, user, _warn)
        return {"reply": "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎", "conversation_id": conv_id}

    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
    _rid = int(cfg.get("runtime_id") or 0)      # 本路由的引擎 id（send 路径没有 rid 这个变量）
    kind = runtime_execution_kind(_rid)
    if kind != EXEC_MODE_DIRECT:
        # M2 D5：入队 + 前端轮询任务状态（打字机效果降级）
        return _enqueue_chat(user, conv_id, body.message, repo_key, skill_name, skill_id, gitcode_token, body.image_data, body.video_data)

    from src.runtime.executor import stream_chat_agent
    loop = asyncio.get_event_loop()
    collected = []
    new_runtime_session_id = ""
    _send_error = ""  # R20.8.1: 保存 error 信息用于 401 标记

    def run_agent():
        nonlocal new_runtime_session_id, _send_error
        from src.observability.master_usage import MasterRun, new_run_id
        # 用量采集（审计 2026-10-17 + P2-1 口径 B）：本机直连 chat 引擎以前不记 token
        _mu = MasterRun("chat", new_run_id("chat"), engine=backend_name,
                        cwd=_get_chat_cwd(user["id"], repo_key, conv_id),
                        session_id=runtime_session_id,
                        attr={"repo_key": repo_key, "scene_id": conv_id,
                              "agent_type": "chat", "user_id": user["id"],
                              "engine_id": _rid})
        _mu.start()
        try:
            with _CHAT_AGENT_SEMAPHORE:
                for evt_type, data in stream_chat_agent(
                    prompt=prompt, cwd=_get_chat_cwd(user["id"], repo_key, conv_id), session_id=runtime_session_id,
                    backend_name=backend_name, user_id=user["id"],
                    account=user["username"], user_token=gitcode_token, agent_name="Chat Agent",
                    agent_label=f"[Agent:chat|user:{user['username']}]",
                    repo_key=repo_key,
                    env_extra=chat_session_env_extra(
                        user["id"], conv_id, repo_key, runtime_id=_rid,
                        issue_number=(conv or {}).get("issue_number") or ""),
                ):
                    _mu.feed(evt_type, data)   # 用量采集
                    # 日志输出
                    if evt_type == "step_start":
                        log.info(f"  ▶ {data}")
                    elif evt_type == "text":
                        log.info(f"  💬 {data[:120]}")
                    elif evt_type == "reasoning":
                        log.info(f"  🤔 {data[:120]}")
                    elif evt_type == "tool_use":
                        log.info(f"  🔧 {str(data)[:150]}")
                    elif evt_type == "step_finish":
                        log.info(f"  ✅ {data}")
                    elif evt_type == "done":
                        log.info(f"  ✅ Done: exit={data.get('exit_code')}, text_len={len(data.get('text',''))}")
                        # R20.8.1: 保存 done 事件的 errors/stderr 用于 401 标记
                        if isinstance(data, dict):
                            _done_errs = data.get("errors") or []
                            _done_stderr = data.get("stderr") or ""
                            if _done_errs:
                                _send_error = " ".join(str(e) for e in _done_errs) + " " + str(_done_stderr)
                            elif _done_stderr:
                                _send_error = str(_done_stderr)

                    if evt_type == "text":
                        text = data.get("text", "") if isinstance(data, dict) else str(data)
                        if text:
                            collected.append(text)
                    elif evt_type == "done":
                        if isinstance(data, dict):
                            nsid = data.get("oc_session_id", "")
                            if nsid:
                                new_runtime_session_id = nsid
                            if not collected:
                                done_text = data.get("text", "")
                                if done_text:
                                    collected.append(done_text)
                    elif evt_type in ("error"):
                        _send_error = str(data)[:500]
                        break
        finally:
            # 收尾写用量：**已释放并发额度**（拿回复不占坑），异常路径也不漏
            _mu.finish(session_id=new_runtime_session_id)
        return "".join(collected)

    reply = await loop.run_in_executor(None, run_agent)
    if not reply:
        reply = f"收到您的消息：「{body.message}」\n\n当前使用 {backend_name} 后端处理。"

    # R20.8.1: agent 执行后 401 检测——agent 执行中 token 可能过期
    # 基于 error 事件数据和 done 事件的 errors/stderr 字段判断，不扫 reply 文本
    _error_code = ""
    if repo_key and _send_error:
        _err_lower = _send_error.lower()
        _is_401 = is_auth_401_error(_err_lower)
        if _is_401:
            try:
                from src.token_service import detect_and_mark_expired
                _error_code = detect_and_mark_expired(repo_key) or ""
            except Exception as _e:
                log.warning(f"Chat post-exec 401 detect failed: {_e}")
            if _error_code:
                _pName = "GitHub" if repo_key.startswith("github:") else "GitCode"
                reply = f"⚠️ {_pName} Token 已过期或被撤销，请在设置页重新绑定 Token 后重试。"

    add_message(conv_id, "agent", reply)
    touch_conversation(conv_id)
    # web→飞书推送：该对话已被飞书接入时推送给用户（feishu-web-chat-unified-design.md §3.2）
    await _push_reply_to_feishu(conv, user, reply)
    # Save opencode session ID for continuity：恒回写新 session
    # （旧值可能已过期（opencode 静默 EOF），必须用本次成功的新 session 覆盖）
    if new_runtime_session_id:
        update_conversation(conv_id, runtime_session_id=new_runtime_session_id)
    _result = {"reply": reply, "conversation_id": conv_id}
    if _error_code:
        _result["error_code"] = _error_code
    return _result


@router.post("/send/stream", operation_id="chat_send_stream", summary="Send message (SSE stream)", description="Sends a chat message over SSE: data:{delta} ... then data:{done}.")
async def send_message_stream(body: ChatMessage, user: dict = Depends(require_no_session_token)):
    conv_id = body.conversation_id
    # Issue #344: 不再缓存明文 token 到 chat_conversations
    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
    else:
        conv_id = create_conversation(user["id"], body.repo_key)
    conv = get_conversation(conv_id)
    _auto_title(conv, body.message)

    import uuid as _uuid
    chat_task_id = "chat_" + _uuid.uuid4().hex[:12]
    add_message(conv_id, "user", body.message + _build_image_markers(_save_chat_images(body.image_data, conv_id)), task_id=chat_task_id, generate_status=1)
    # 任务会话收到真实用户消息 → active→processing（task-status-processing-trigger-fix.md §3.1）
    mark_task_processing_by_conversation(conv_id)

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    skill_name = cfg.get("skill", "") or "chat"
    skill_id = cfg.get("skill_id", 0) or 0
    acc = _get_acc(user["username"]) or {}
    history = get_messages(conv_id, limit=20)
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    # Issue #344: 实时获取用户 token（带预刷新），不再从会话缓存读取
    from src.api.deps import get_user_token_with_refresh
    user_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
    gitcode_token = _resolve_gitcode_token(user_token, repo_key)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    # 任务会话走独立 prompt（注入 Issue 上下文 + 场景: task）；一般会话走通用 prompt
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_issue_chat_prompt
        prompt = build_issue_chat_prompt(body.message, repo_key, history, conv, account=user["username"], runtime_id=cfg.get("runtime_id", 0), has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(body.message, repo_key, history, skill_name, skill_id,
                                           backend_name=backend_name, user_info=_build_web_user_info(user, acc),
                                           has_valid_session=has_valid_session)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""

    from src.config import log
    log.info(f"Chat send/stream: backend={backend_name or '(none)'}, skill={skill_name}, has_token={bool(gitcode_token)}, user_msg={body.message[:100]}")

    # 图片理解注入
    if body.image_data:
        prompt += "\n\n" + _understand_image(body.image_data)

    # 视频理解注入
    if body.video_data:
        prompt += "\n\n" + _understand_video(body.video_data)

    # 无引擎 → 明确不可用，绝不隐式跑 opencode（engine-selection-design.md v2）
    if not backend_name:
        _reply = "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎"
        add_message(conv_id, "agent", _reply)
        await _push_reply_to_feishu(conv, user, _reply)
        return StreamingResponse(
            iter([f"data: {json.dumps({'type': 'done', 'text': _reply, 'conversation_id': conv_id})}\n\n"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
    # chat 默认兜底本机直连（engine-selection-design.md §8.2）：未显式配置引擎时
    # 选健康本机引擎（credential_id=0），恒走 direct；不再按名现挑/建 opencode 默认。
    rid = cfg.get("runtime_id") or 0
    if not rid:
        try:
            from src.runtime.engine_select import resolve_local_engine
            rid = resolve_local_engine(user.get("id") or 0) or 0
        except Exception:
            rid = 0
    kind = runtime_execution_kind(rid)
    # 本机引擎（credential_id=0）强制走 direct（queue_remote 模式下本机引擎入队 = 黑洞）
    from src.runtime.service import runtime_credential_id
    if (runtime_credential_id(rid) or 0) == 0:
        kind = EXEC_MODE_DIRECT
    if kind != EXEC_MODE_DIRECT:
        # 本机直连行（credential_id=0）在 queue_remote 下无 claim 方：
        # 入队 = 永久 queued 黑洞（chat-task-blackhole-local-direct-2026-08.md）
        # rid=0 同样拦截（runtime_credential_id(0)=None → (None or 0)==0）
        from src.runtime.service import runtime_credential_id
        if (runtime_credential_id(rid) or 0) == 0 and backend_name:
            # 配置行残留本机直连/未绑定 → queue_remote 下按 backend 名解析在线远端引擎
            #（存量 sess-cfg-* 行被 onboarding 写死 1 的兜底，解析不到才真拦截）
            from src.session_manager import _resolve_chat_runtime_id
            _rid2 = _resolve_chat_runtime_id(backend_name) or 0
            if _rid2:
                rid = _rid2
                kind = runtime_execution_kind(rid)
        # 远端引擎离线/心跳超时 → 同样解析在线远端引擎兜底（与 handler_async 同源修复）
        if kind != EXEC_MODE_DIRECT:
            from src.runtime.service import _remote_runtime_ready
            if not _remote_runtime_ready(rid) and backend_name:
                from src.session_manager import _resolve_chat_runtime_id
                _rid3 = _resolve_chat_runtime_id(backend_name) or 0
                if _rid3 and _remote_runtime_ready(_rid3):
                    rid = _rid3
                    kind = runtime_execution_kind(rid)
        if (runtime_credential_id(rid) or 0) == 0:
            _reply = "⚠️ 未配置远端引擎，任务会话需先在 Agent 配置中绑定远端引擎"
            add_message(conv_id, "agent", _reply)
            await _push_reply_to_feishu(conv, user, _reply)
            return StreamingResponse(
                iter([f"data: {json.dumps({'type': 'done', 'text': _reply, 'conversation_id': conv_id})}\n\n"]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # M2 D5 入队 + 轮询：任务由远端 RuntimeAgent claim 执行，SSE 以终态结果返回
        import json as _json
        from src.runtime.service import enqueue_task, wait_task_terminal
        from src.api.session_store import get_or_create_agent_session

        session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name, runtime_id=rid)
        runtime_id = rid  # 已由本机直连兜底解析（§8.2）；远端引擎才走队列

        async def event_stream():
            yield f"data: {json.dumps({'type': '思考中', 'text': '', 'conversation_id': conv_id})}\n\n"
            final_text = ""
            new_sid = runtime_session_id
            if runtime_id:
                tid = enqueue_task(
                    agent_session_id=session.get("id") or "",
                    runtime_id=runtime_id,
                    event_seq=None, issue_id="",
                    repo_key=repo_key or f"chat:{user['id']}", agent_type="chat",
                    skill=skill_name or "chat",
                    skill_id=skill_id,
                    prompt=prompt, cwd=_get_chat_cwd(user["id"], repo_key, conv_id),
                    runtime_session_id=runtime_session_id,
                    event_type="chat",
                    account=user.get("username") or "", user_id=user["id"],
                    user_gitcode_token="",  # Issue #344: no plaintext token in task payload
                    env_extra={**chat_session_env_extra(
                                  user["id"], conv_id, repo_key, runtime_id=runtime_id,
                                  issue_number=(conv or {}).get("issue_number") or ""),
                               **resolve_task_server_env(runtime_id)},
                )
                log.info(f"Chat stream enqueued: task={tid} user={user['id']} conv={conv_id}")
                task = await wait_task_terminal(tid)
                final_text = task.get("result_text") or ""
                try:
                    usage = _json.loads(task.get("usage_json") or "{}") or {}
                    new_sid = usage.get("session_id") or runtime_session_id
                except Exception:
                    new_sid = runtime_session_id
                if final_text:
                    yield f"data: {json.dumps({'type': '生成中', 'text': final_text, 'conversation_id': conv_id})}\n\n"
            else:
                log.warning(f"Chat stream: no runtime for backend={backend_name}, fallback reply")

            if not final_text:
                final_text = f"收到您的消息：「{body.message}」\n\n当前使用 {backend_name} 后端处理。"
            add_message(conv_id, "agent", final_text, task_id=chat_task_id, generate_status=2)
            touch_conversation(conv_id)
            # web→飞书推送（feishu-web-chat-unified-design.md §3.2）
            await _push_reply_to_feishu(conv, user, final_text)
            if new_sid and new_sid != runtime_session_id:
                update_conversation(conv_id, runtime_session_id=new_sid)
            yield f"data: {json.dumps({'type': 'done', 'text': final_text, 'conversation_id': conv_id})}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    from src.runtime.executor import stream_chat_agent

    async def event_stream():
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()
        captured_session = ""

        def agent_worker():
            nonlocal captured_session
            _worker_final_text = ""
            _stream_error_detail = ""  # R20.8.6: 保存 error 信息用于 401 标记
            log.info(f"[Agent:chat|user:{user['username']}] agent_worker starting (backend={backend_name})")
            if not _CHAT_AGENT_SEMAPHORE.acquire(blocking=True):
                log.warning(f"[Agent:chat|user:{user['username']}] semaphore NOT acquired (exhausted)")
                return
            collected = []
            worker_error = ""
            from src.observability.master_usage import MasterRun
            # 用量采集（审计 2026-10-17）：SSE 本机直连路径同样漏记 token
            _mu = MasterRun("chat", chat_task_id, engine=backend_name,
                            cwd=_get_chat_cwd(user["id"], repo_key, conv_id),
                            session_id=runtime_session_id,
                            attr={"repo_key": repo_key, "scene_id": conv_id,
                                  "agent_type": "chat", "user_id": user["id"],
                                  "engine_id": int(rid or 0)})
            _mu.start()
            try:
                for evt_type, data in stream_chat_agent(
                    prompt=prompt, cwd=_get_chat_cwd(user["id"], repo_key, conv_id), session_id=runtime_session_id,
                    backend_name=backend_name, user_id=user["id"],
                    account=user["username"], user_token=gitcode_token, agent_name="Chat Agent",
                    agent_label=f"[Agent:chat|user:{user['username']}]",
                    repo_key=repo_key,
                    env_extra=chat_session_env_extra(
                        user["id"], conv_id, repo_key, runtime_id=rid,
                        issue_number=(conv or {}).get("issue_number") or ""),
                ):
                    _mu.feed(evt_type, data)
                    # 日志输出（对齐 master 分支 im/handler.py）
                    if evt_type == "step_start":
                        log.info(f"  ▶ {data}")
                    elif evt_type == "text":
                        t = data.get("text", "") if isinstance(data, dict) else str(data)
                        if t:
                            collected.append(t)
                        log.info(f"  💬 {data[:120]}")
                    elif evt_type == "reasoning":
                        log.info(f"  🤔 {data[:120]}")
                    elif evt_type == "tool_use":
                        log.info(f"  🔧 {str(data)[:150]}")
                    elif evt_type == "step_finish":
                        log.info(f"  ✅ {data}")
                    elif evt_type == "done":
                        log.info(f"  ✅ Done: exit={data.get('exit_code')}, text_len={len(data.get('text',''))}")

                    if evt_type == "done" and isinstance(data, dict):
                        nsid = data.get("oc_session_id", "")
                        if nsid:
                            captured_session = nsid
                        done_text = data.get("text", "")
                        if done_text:
                            _worker_final_text = done_text
                        # R20.8.6: 保存 done 事件的 errors/stderr 用于 401 标记
                        _d_errs = data.get("errors") or []
                        _d_stderr = data.get("stderr") or ""
                        if _d_errs:
                            _stream_error_detail = " ".join(str(e) for e in _d_errs) + " " + str(_d_stderr)
                        elif _d_stderr:
                            _stream_error_detail = str(_d_stderr)
                    elif evt_type == "text":
                        text_val = data.get("text", "") if isinstance(data, dict) else str(data)
                        if text_val:
                            _worker_final_text += text_val
                    # 跨线程投递事件到 event loop（asyncio.Queue.put_nowait 非线程安全）
                    loop.call_soon_threadsafe(q.put_nowait, (evt_type, data))
            except Exception as e:
                import traceback as _tb
                log.warning(f"[Agent:chat|user:{user['username']}] agent_worker EXC: {e}")
                _tb.print_exc()
                _stream_error_detail = str(e)[:500]
                loop.call_soon_threadsafe(q.put_nowait, ("error", str(e)))
            finally:
                # 汇总回复并落库 + 回写 session——不依赖 event_stream 是否被客户端取消，
                # 保证即使 SSE 断连，回复与会话也已持久化/刷新（修复死 session 永驻）。
                #（chat-prompt-history-injection-design.md §5.3）
                try:
                    final_text = "".join(collected).strip()
                    if not final_text and not worker_error:
                        final_text = f"收到您的消息：「{body.message}」\n\n当前使用 {backend_name} 后端处理。"
                    if final_text:
                        add_message(conv_id, "agent", final_text)
                        touch_conversation(conv_id)
                        # 恒回写新 session（覆盖可能过期的旧值）
                        if captured_session:
                            update_conversation(conv_id, runtime_session_id=captured_session)
                except Exception as e:
                    log.warning(f"[Agent:chat|user:{user['username']}] persist reply failed: {e}")
                # R20.8.6: 401 标记——防止 token 过期后用户重发 send_stream 死循环
                # 基于 done 事件的 errors/stderr 和 exception 信息判断，不扫 final_text
                if repo_key and _stream_error_detail:
                    _err_lower = _stream_error_detail.lower()
                    _is_401 = is_auth_401_error(_err_lower)
                    if _is_401:
                        try:
                            from src.token_service import detect_and_mark_expired
                            _stream_error_code = detect_and_mark_expired(repo_key) or ""
                            if _stream_error_code:
                                log.warning(f"[Agent:chat|user:{user['username']}] token expired during stream (error_code={_stream_error_code}), repo={repo_key}")
                        except Exception as _se:
                            log.warning(f"[Agent:chat|user:{user['username']}] stream post-exec 401 detect failed: {_se}")
                log.info(f"[Agent:chat|user:{user['username']}] agent_worker finished")
                # 连接断开后 event_stream 的 yield 会抛异常，add_message 不会被执行
                # 在 finally 中直接写 DB（带 task_id + generate_status，跨机器可见）
                if _worker_final_text:
                    try:
                        add_message(conv_id, "agent", _worker_final_text, task_id=chat_task_id, generate_status=2)
                        touch_conversation(conv_id)
                        log.info(f"[Agent:chat|user:{user['username']}] DB saved in finally (task_id={chat_task_id}, final_text_len={len(_worker_final_text)})")
                    except Exception as _de:
                        log.warning(f"[Agent:chat|user:{user['username']}] DB save in finally failed: {_de}")
                else:
                    try:
                        add_message(conv_id, "agent", "（无返回内容）", task_id=chat_task_id, generate_status=3)
                        touch_conversation(conv_id)
                    except Exception as _de:
                        log.warning(f"[Agent:chat|user:{user['username']}] DB save (empty) in finally failed: {_de}")
                # 客户端先拿到流结束信号、并发额度先释放，用量收尾（可能含 1s 重试）放最后
                loop.call_soon_threadsafe(q.put_nowait, None)
                _CHAT_AGENT_SEMAPHORE.release()
                _mu.finish(session_id=captured_session)

        # 保持 Future 引用（防止 GC 回收导致 agent_worker 不执行）
        _agent_future = loop.run_in_executor(None, agent_worker)
        # 让 event loop 有机会调度线程池任务
        await asyncio.sleep(0.1)

        yield f"data: {json.dumps({'type': '思考中', 'text': '', 'conversation_id': conv_id})}\n\n"

        final_text = ""
        _last_sse = 0.0
        while True:
            # 看门狗：Agent 长时间无输出（如 opencode 卡住/超慢）时主动报错，
            # 避免 SSE 流一直挂着直到连接被代理/浏览器掐断 → 前端落到无意义兜底
            try:
                item = await asyncio.wait_for(q.get(), timeout=60)
            except asyncio.TimeoutError:
                if not final_text:
                    yield f"data: {json.dumps({'type': 'error', 'text': '⚠️ Agent 长时间无响应，未收到回复，请稍后重试。', 'conversation_id': conv_id})}\n\n"
                return
            if item is None:
                break
            evt_type, data = item
            if evt_type == "text":
                text = data.get("text", "") if isinstance(data, dict) else str(data)
                if text:
                    final_text += text
                    _last_sse = time.time()
                    yield f"data: {json.dumps({'type': '生成中', 'text': final_text, 'conversation_id': conv_id})}\n\n"
            elif evt_type == "error":
                err_msg = str(data)
                yield f"data: {json.dumps({'type': 'error', 'text': f'后端错误: {err_msg}', 'conversation_id': conv_id})}\n\n"
                return
            elif evt_type == "done":
                if not final_text:
                    done_text = data.get("text", "") if isinstance(data, dict) else ""
                    if done_text:
                        final_text = done_text
                break
            # SSE 心跳：长时间无事件时主动发 ping，防止代理/浏览器因 idle 掐断长连接
            elif _last_sse and (time.time() - _last_sse) > 15:
                _last_sse = time.time()
                yield f": ping\n\n"

        if not final_text:
            final_text = f"收到您的消息：「{body.message}」\n\n当前使用 {backend_name} 后端处理。"

        add_message(conv_id, "agent", final_text, task_id=chat_task_id, generate_status=2)
        touch_conversation(conv_id)
        # web→飞书推送（feishu-web-chat-unified-design.md §3.2）
        await _push_reply_to_feishu(conv, user, final_text)
        # Save opencode session ID for continuity
        if captured_session and not runtime_session_id:
            update_conversation(conv_id, runtime_session_id=captured_session)
        yield f"data: {json.dumps({'type': 'done', 'text': final_text, 'conversation_id': conv_id, 'task_id': chat_task_id})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


def _enqueue_chat(user, conv_id, message, repo_key, skill_name, skill_id, gitcode_token, image_data="", video_data=""):
    """M2 D5：chat 入队 + 轮询任务状态（打字机效果降级，见 implementation-plan §三）。

    返回 {task_id, reply(占位), conversation_id}；前端轮询 GET /api/runtime/tasks/{id}/status。
    """
    from src.runtime.service import enqueue_task, get_execution_mode
    from src.api.session_store import get_or_create_agent_session
    from src.config import log as _log
    import os as _os
    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    # 无引擎 → 明确不可用（engine-selection-design.md v2），不隐式跑 opencode
    if not backend_name:
        return {"reply": "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎", "conversation_id": conv_id}
    # 优先用用户配置的 runtime_id（chat_agent_session，会话引擎指针），未配置按本机直连兜底
    #（engine-selection-design.md §8.2）；Issue #400：解析出的唯一引擎同时落 devops_agent_session 指针
    runtime_id = cfg.get("runtime_id") or 0
    if not runtime_id:
        try:
            from src.runtime.engine_select import resolve_local_engine
            runtime_id = resolve_local_engine(user.get("id") or 0) or 0
        except Exception:
            runtime_id = 0
    if not runtime_id:
        return {"reply": "⚠️ 无可用 runtime，请先在 Agent Runtimes 页刷新", "conversation_id": conv_id}
    # chat 会话：user_id + repo_key 绑定（chat_agent_session 是 chat 专用；此处用 devops_agent_session 保证 D9 串行）
    session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name, runtime_id=runtime_id)
    # 本机直连（credential_id=0）无 claim 方：队列任务端点不能投递到本机，需走 direct 会话
    from src.runtime.service import runtime_credential_id
    if (runtime_credential_id(runtime_id) or 0) == 0 and backend_name:
        # 配置行残留本机直连/未绑定 → 按 backend 名解析在线远端引擎（与 send/stream 同款兜底）
        from src.session_manager import _resolve_chat_runtime_id
        _rid2 = _resolve_chat_runtime_id(backend_name) or 0
        if _rid2:
            runtime_id = _rid2
    if (runtime_credential_id(runtime_id) or 0) == 0:
        return {"reply": "⚠️ 未配置远端引擎，任务会话需先在 Agent 配置中绑定远端引擎", "conversation_id": conv_id}

    cwd = _get_chat_cwd(user["id"], repo_key, conv_id)
    _os.makedirs(cwd, exist_ok=True)
    # 任务会话走独立 prompt（注入 Issue 上下文 + 场景: task + 任务描述 + 历史）；
    # 一般会话走通用 prompt（queue 模式也带最近历史，与 direct 一致）
    #（见 task-conversation-prompt-align-design.md §3.2）
    conv = get_conversation(conv_id) if conv_id else None
    history = get_messages(conv_id, limit=10) if conv_id else []
    acc = _get_acc(user.get("username") or "") or {}
    _rsid = session.get("runtime_session_id") or ""
    has_valid_session = bool(_rsid.startswith("ses_"))
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_issue_chat_prompt
        prompt = build_issue_chat_prompt(message, repo_key, history, conv, account=user.get("username") or "", runtime_id=runtime_id, has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(message, repo_key, history, skill_name, skill_id,
                                           user_info=_build_web_user_info(user, acc), has_valid_session=has_valid_session)
    # 图片理解注入
    if image_data:
        prompt += "\n\n" + _understand_image(image_data)
    # 视频理解注入
    if video_data:
        prompt += "\n\n" + _understand_video(video_data)
    tid = enqueue_task(
        agent_session_id=session.get("id") or "",
        runtime_id=runtime_id,
        event_seq=None, issue_id="",
        repo_key=f"chat:{user['id']}", agent_type="chat", skill=skill_name or "chat",
        skill_id=skill_id,
        prompt=prompt, cwd=cwd,
        runtime_session_id=session.get("runtime_session_id") or "",
        event_type="chat", account=user.get("username") or "", user_id=user["id"],
        repo_gitcode_token="",  # Issue #344: no plaintext token in task payload
        env_extra={**chat_session_env_extra(
                      user["id"], conv_id, repo_key, runtime_id=runtime_id,
                      issue_number=(conv or {}).get("issue_number") or ""),
                   **resolve_task_server_env(runtime_id)},
    )
    _log.info(f"Chat enqueued: task={tid} user={user['id']} conv={conv_id} mode={get_execution_mode()}")
    return {"task_id": tid, "reply": "", "conversation_id": conv_id, "queued": True}


# ====== Feedback ======

@router.post("/feedback", operation_id="chat_feedback", summary="Submit feedback", description="Submits feedback on a chat turn (seq + type + comment).")
def submit_feedback(body: FeedbackBody, user: dict = Depends(require_no_session_token)):
    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT repo_key, event_type, title, payload FROM events WHERE seq=?", (body.seq,))
            row = c.fetchone()
        except Exception:
            row = None

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件未找到"})

    repo_key = row[0]
    a2a_event = {
        "type": "user_feedback",
        "source": "chat_agent",
        "target": "vod",
        "payload": {
            "source_seq": body.seq,
            "feedback_type": body.feedback_type,
            "comment": body.comment,
            "user": user["username"],
        }
    }

    # R12.13: 改用 get_repo_token_with_refresh（路径 B 仓库返回用户 token）
    from src.user.db import get_repo_token_with_refresh
    bot_token = get_repo_token_with_refresh(repo_key) if repo_key else ""

    return {
        "message": "反馈已提交（⚠️ 已废弃：请改用 Event 对话 POST /api/events/{seq}/chat）",
        "deprecated": True,
        "a2a_event": a2a_event,
        "bot_token_available": bool(bot_token),
    }


# ====== Config ======

def _chat_dedupe_backends(backends: list[dict]) -> list[dict]:
    """按引擎唯一键去重（Issue #400）：个人机器 user/repo 双档或历史重复行合并为单条。

    键语义与 src/runtime/engine_select.engine_unique_key 对齐（本机直连以
    **类型名+runtime_id** 为键——多主控机同名直连引擎是两台不同机器，不能按类型名
    互并，否则前端下拉只剩一台 master 的引擎；个人/远端以实例名为键）；
    保留首个出现项（查询 id 排序下的权威行）。
    """
    seen: set = set()
    out: list[dict] = []
    for b in backends:
        scope = (b.get("scope") or "").strip().lower()
        nm = (b.get("name") or "").strip()
        if not nm:
            out.append(b)
            continue
        if scope == "local":
            # runtime_id 进键：不同主控机的同名直连引擎各自保留
            key = f"local:{nm.lower()}#{int(b.get('runtime_id') or 0)}"
        else:
            key = f"{scope}:{nm.lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


def _chat_selected_engine(cfg: dict, backends: list[dict]) -> dict | None:
    """解析唯一选中引擎（Issue #400 单选语义）。

    以会话引擎指针（cfg.runtime_id）为权威：
    1. 指针命中候选列表（含个人实例）→ 返回该候选；
    2. 指针指向列表外引擎（已配置的远端/其它仓库引擎）→ 按 runtime_id 反查 agent_runtimes；
    3. 指针缺失/失效 → 按 backend 名精确匹配（本机直连类型名/user 实例名），
       不做前缀匹配（杜绝多个候选同时命中 —— 多选显示根因）。

    返回单一 dict（与 _backends 项同构）或 None；绝不返回数组。
    """
    rid = int(cfg.get("runtime_id") or 0)
    backend = (cfg.get("backend") or "").strip()
    if rid:
        for b in backends:
            if int(b.get("runtime_id") or 0) == rid:
                return b
        try:
            from src.runtime.instances import instance_name
            from src.session_manager import db as _db, db_lock as _dl
            with _dl:
                c = _db.cursor()
                c.execute(
                    "SELECT ar.name, ar.version, rc.device_name, rc.owner_name, ar.credential_id, rc.scope "
                    "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id "
                    "WHERE ar.id=?", (rid,))
                row = c.fetchone()
            if row and row[0]:
                ename, ver, dev, owner, cid, rscope = row
                cid = cid or 0
                rscope = (rscope or "global") if cid else "local"
                if cid:
                    ins = instance_name(ename, dev or "", cid)
                    return {"name": ins, "display_name": f"{ins} · {ename} v{ver or ''}",
                            "scope": rscope, "owner": owner or "", "runtime_id": rid}
                return {"name": ename, "display_name": f"{ename} v{ver or ''}（本机直连）",
                        "scope": "local", "owner": "", "runtime_id": rid}
        except Exception:
            pass
    if not backend:
        return None
    for b in backends:
        if b.get("name") == backend:
            return b
    for b in backends:
        if b.get("scope") == "local" and b.get("name") and b["name"].lower() == backend.lower():
            return b
    return None


@router.get("/config", operation_id="chat_config_get", summary="Get chat config", description="Returns chat config (backend/settings/personal engines).")
def get_chat_config(user: dict = Depends(require_no_session_token), repo: str = ""):
    cfg = _get_chat_config(user["id"])
    # Chat 后端可选面：本机直连（默认兜底，engine-selection-design.md §8.2）+ 个人引擎（scope=user，显式选择）
    from src.runtime.instances import instance_name
    from src.user.db import is_known_engine_name
    from src.session_manager import db, db_lock
    backends = []
    with db_lock:
        c = db.cursor()
        # 本机直连引擎（credential_id=0，已知类型且已装且健康——与执行 resolve_local_engine 口径一致，
        # local-direct-health-probe-design.md 方案 C，避免"页面有、执行无"）
        # host_name 进列表：多主控机同名直连引擎都展示（名称标注归属机，区分显示）
        try:
            c.execute("SELECT id, name, version, COALESCE(host_name,'') FROM agent_runtimes WHERE credential_id=0 AND installed=1 AND healthy=1 ORDER BY id")
            for r in c.fetchall():
                _rid, ename, ver, _hn = r
                if not is_known_engine_name(ename or ""):
                    continue
                _label = f"{ename} v{ver or ''}"
                if _hn:
                    # 主控机短名（ecs-grape-master-001 → master-001）便于前端区分双主
                    _short = _hn.replace("ecs-grape-", "")
                    _label += f"（{_short} 直连）"
                else:
                    _label += "（本机直连）"
                backends.append({"name": ename, "display_name": _label, "scope": "local", "owner": _hn, "runtime_id": _rid})
        except Exception:
            pass
        # 个人引擎（scope=user，仅当前用户 + active）
        try:
            c.execute("""
                SELECT ar.id, ar.name, ar.version, rc.device_name, rc.owner_name, rc.id
                FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
                WHERE rc.scope = 'user' AND rc.status='active' AND rc.owner_name = ?
            """, (user["username"],))
            for r in c.fetchall():
                _rid, ename, ver, dev, owner, cid = r
                ins = instance_name(ename, dev or "", cid or 0)
                backends.append({"name": ins, "display_name": f"{ins} · {ename} v{ver or ''}", "scope": "user", "owner": owner or "", "runtime_id": _rid})
        except Exception:
            pass
    # Load chat skills from skill table（官方 + 个人 + 当前仓库，agent_type='chat'）
    chat_skills = []
    from src.repo_key import to_repo_key
    # 跨平台：查 DB 确定 repo_key 实际前缀
    _rk_candidates = []
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        _rk_candidates = [f"gitcode:{_base}", f"github:{_base}"]
    with db_lock:
        c = db.cursor()
        try:
            if _rk_candidates:
                c.execute(
                    "SELECT skill_name, display_name, description, skill_path, scope FROM skill WHERE is_active=1 AND agent_type='chat' AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id IN ({}))) ORDER BY scope, skill_name".format(",".join("?" * len(_rk_candidates))),
                    [str(user["id"])] + _rk_candidates
                )
            else:
                c.execute(
                    "SELECT skill_name, display_name, description, skill_path, scope FROM skill WHERE is_active=1 AND agent_type='chat' AND (scope='official' OR (scope='user' AND owner_id=?)) ORDER BY scope, skill_name",
                    (str(user["id"]),)
                )
            chat_skills = [{"name": r[0], "display_name": r[1] or r[0], "description": r[2], "skill_path": r[3] or r[0], "scope": r[4] or ""} for r in c.fetchall()]
        except Exception:
            pass

    # A2 收尾：凭证状态改读 grape_t_channel_installation（legacy im_* 三列不再是真相源）
    # 按渠道分别回显：每个渠道取**该用户自己的**最新一条可用安装（active/disabled），
    # 而不是"最近更新的那一条"——多渠并存时后者只有最后绑的那个渠道能回显
    # （企微明明连着也显示"未接入"，前端据此误判）。
    # 任何情况不回传 secret（仅布尔）。
    _inst = _active_im_installation(user["id"])
    im_provider = str((_inst or {}).get("channel_type", "") or "").strip().lower()
    if im_provider not in _IM_PROVIDERS:
        # 空值/历史脏值归一 feishu（#825 前旧行均为飞书）
        im_provider = "feishu"
    _user_installs = _user_im_installations(user["id"])

    def _im_echo(provider: str) -> dict:
        entry = _user_installs.get(provider) or {}
        cfg = entry.get("config") or {}
        app_id = str(cfg.get("app_id", "") or "")
        secret = str(cfg.get("app_secret", "") or "")
        echo_app_id = app_id
        if provider in ("slack", "telegram") and app_id:
            # Issue #1309 / #1613：Slack 的 app_id 位存 Bot Token，Telegram 只有单 Bot Token
            # （均敏感）→ 只回显脱敏占位；前端未改动时原样回传 ********，后端按"保留原值"处理
            echo_app_id = "********"
        return {
            "connected": bool(app_id),   # 兼容旧字段：= 已配置；真连接看 installation_status
            "app_id": echo_app_id,
            "app_secret_exists": bool(secret),
            "installation_status": str(entry.get("status", "") or ""),
        }

    # 当前配置技能的 scope（用于前端下拉回显同名技能）
    skill_scope = cfg.get("skill_scope", "")
    if not skill_scope and cfg.get("skill_id"):
        try:
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT scope FROM skill WHERE id=? AND is_active=1", (cfg["skill_id"],)).fetchone()
                skill_scope = row[0] if row else ""
        except Exception:
            pass

    backends = _chat_dedupe_backends(backends)
    selected_engine = _chat_selected_engine(cfg, backends)

    return {
        "backend": {
            "type": cfg.get("backend", ""),
        },
        "selectedEngine": selected_engine,
        "skill": cfg.get("skill", ""),
        "skill_id": cfg.get("skill_id", 0) or 0,
        "skill_scope": skill_scope,
        "_backends": backends,
        "_skills": chat_skills,
        "im": {
            # Issue #956：当前绑定渠道（feishu|dingtalk），前端回显当前选中卡
            "provider": im_provider,
            "feishu": _im_echo("feishu"),
            "wecom": _im_echo("wecom"),
            "dingtalk": _im_echo("dingtalk"),
            "slack": _im_echo("slack"),
            "telegram": _im_echo("telegram"),
            "wechat": _im_echo("wechat"),
        }
    }


def _active_im_installation(user_id):
    """当前用户 active 的 IM 安装（A2 收尾后凭证真相源；无则 {}）。"""
    try:
        from src.im.engine_wiring import get_active_installation
        return get_active_installation("user", owner_user_id=str(user_id)) or {}
    except Exception as e:
        log.warning(f"Chat IM: read installation failed: {e}")
        return {}


def _get_provider_installation(user_id, channel_type: str) -> dict:
    """按渠道取该用户 active 安装（多渠并存时按渠道隔离旧值，不能取"全局最近一条"）。"""
    try:
        from src.im.engine_wiring import get_active_installation
        return get_active_installation("user", owner_user_id=str(user_id),
                                       channel_type=str(channel_type or "")) or {}
    except Exception as e:
        log.warning(f"Chat IM: read installation({channel_type}) failed: {e}")
        return {}


def _user_im_installations(user_id) -> dict:
    """该用户每个渠道最新一条可用安装（active/disabled）→ {channel_type: {status, config}}。

    前端多渠并存（如微信 + 企业微信同时启用）时，各渠道表单需要各自的凭证回显；
    只取"全局最近一条"会让其余渠道恒为空 → 误显示"未接入"。
    """
    out: dict = {}
    try:
        from src.im.engine_wiring import _connect, default_db_path
        conn = _connect(default_db_path())
        try:
            rows = conn.execute(
                "SELECT channel_type, status, config FROM grape_t_channel_installation "
                "WHERE scope='user' AND owner_user_id=? AND status IN ('active','disabled') "
                "ORDER BY updated_at DESC", (str(user_id),)).fetchall()
        finally:
            conn.close()
        for ctype, st, raw in rows or []:
            key = str(ctype or "").strip().lower()
            if not key or key in out:
                continue
            try:
                cfg = json.loads(raw or "{}")
            except Exception:
                cfg = {}
            out[key] = {"status": str(st or ""), "config": cfg if isinstance(cfg, dict) else {}}
    except Exception as e:
        log.warning(f"Chat IM: read user installations failed: {e}")
    return out


def _purge_chat_im_binding(user_id: int, gitcode_user: str, old_app_id: str,
                            provider: str = "feishu") -> None:
    """Chat IM 解绑清理（Issue #294，#956 补 provider 维度）：注册表释放 + 渠道会话清理。

    - 该 app_id 无其他活跃安装引用 → 清空该 app 的飞书映射（A2 收尾：不再有 legacy 登记行）；
    - 飞书（provider=feishu）才清空 unified_user_mapping 的 open_id/feishu_name/app_id
      （app 级 open_id 为飞书专属概念，钉钉解绑不碰）；
    - 仍被其他用户引用 → 只清空当前用户自己的映射行（仅飞书）；
    - 该渠道（im_channel_id=old_app_id）的 chat_conversations 一律删除（对齐换绑清理逻辑）；
    - repo_im_group_binding / bot_seen_messages 与渠道无关，一律清理；
    - feishu_onboard_task 为飞书扫码专属，仅 feishu 解绑清理（钉钉无此链路）。
    """
    if not old_app_id:
        return
    from datetime import datetime as _dt
    from src.session_manager import db as _db, db_lock as _db_lock
    try:
        with _db_lock:
            c = _db.cursor()
            # A2 收尾：引用判定改看 active 安装行（不含当前用户本人的这条）
            other_refs = 0
            try:
                from src.im.engine_wiring import _connect, default_db_path
                _conn = _connect(default_db_path())
                try:
                    other_refs = int(_conn.execute(
                        "SELECT COUNT(*) FROM grape_t_channel_installation "
                        "WHERE legacy_app_id=? AND status='active' "
                        "  AND NOT (scope='user' AND owner_user_id=?)",
                        (old_app_id, str(user_id))).fetchone()[0])
                finally:
                    _conn.close()
            except Exception as e:
                log.warning(f"Chat IM: installation ref check failed: {e}")
            _now = _dt.now().isoformat()
            is_feishu = (provider or "feishu").strip().lower() == "feishu"
            if other_refs == 0:
                if is_feishu:
                    c.execute("UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id='', updated_at=? WHERE app_id=?",
                              (_now, old_app_id))
            elif gitcode_user and is_feishu:
                # FR-21.10: 按 user_id 精确清当前用户的飞书映射——不按用户名扫列
                # 原代码 WHERE gitcode_user=? 用注册名扫列，存量 um 行可能存注册名也可能存平台真 login
                # 用 OR github_login=? 会误清同名 GitHub 用户；用 user_id 精确匹配无歧义
                c.execute("UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id='', updated_at=? WHERE user_id=?",
                          (_now, user_id))
            c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (old_app_id,))
            # 解绑干净：该 bot 名下的群绑定（repo_im_group_binding）一并清空，避免残留脏数据
            # 导致后续 resolve_repo 仍命中旧仓库（chat-config-duplicate-row-unbind-fix-design.md）
            c.execute("DELETE FROM repo_im_group_binding WHERE im_app_id=?", (old_app_id,))
            # 解绑干净：消息去重缓存一并清，避免该 app 的历史数据残留
            #（bot_seen_messages=去重 ID；feishu_onboard_task 含 app_secret/qr_url 敏感信息，飞书专属）
            c.execute("DELETE FROM bot_seen_messages WHERE app_id=?", (old_app_id,))
            if is_feishu:
                c.execute("DELETE FROM feishu_onboard_task WHERE app_id=?", (old_app_id,))
            _db.commit()
        log.info(f"Chat IM: unbound old app_id={_safe_im_app_ref(old_app_id, provider)} provider={provider} user={user_id} (other_refs={other_refs})")
    except Exception as e:
        log.warning(f"Chat IM: unbind cleanup failed: {e}")


@router.put("/config", operation_id="chat_config_update", summary="Update chat config", description="Updates chat config.")
async def update_chat_config(body: dict, request: Request,
                             user: dict = Depends(require_no_session_token), repo: str = ""):
    # Issue #956：provider 化载荷解析（新形态 im.provider + 平铺 app_id/app_secret，
    # 兼容旧形态 im.feishu / im.dingtalk 子 dict；无 im 键仍为普通保存，#431 语义不变）
    im = body.get("im", {})
    provider = str(im.get("provider") or "").strip().lower()
    if provider and provider not in _IM_PROVIDERS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"暂不支持该 IM 渠道：{provider}（当前支持 feishu / dingtalk / wecom / slack / telegram / wechat）"})
    channel = im.get(provider) if provider else None
    if channel is None and not provider:
        if "feishu" in im:
            provider, channel = "feishu", im["feishu"]
        elif "dingtalk" in im:
            provider, channel = "dingtalk", im["dingtalk"]
    if provider and channel is None:
        # 新形态平铺载荷：im = {provider, app_id, app_secret}
        channel = {"app_id": im.get("app_id", ""), "app_secret": im.get("app_secret", "")}
    if not isinstance(channel, dict):
        channel = {}
    # Issue #1309：Slack 是双令牌渠道，Bot Token（app_id 位）属敏感信息，GET /config
    # 只回显 ********；前端未改动时原样回传占位符 → 这里按"保留存量值"处理，
    # 绝不把占位符写进 im_app_id（否则下一次连接拿 ******** 当 token）。
    if str(channel.get("app_id") or "") == "********":
        channel = dict(channel)
        channel["app_id"] = str((_get_provider_installation(user["id"], provider).get("config") or {}).get("app_id", "") or "")
    # Issue #1613：Telegram 单 Bot Token —— 前端只提交一栏 app_id；内部把 token 同时作为
    # secret 位用于安装行创建（secret 语义键不透出、日志摘要化），避免"单凭据渠道因
    # 缺 secret 建不出安装"。
    if provider == "telegram" and channel.get("app_id") and not channel.get("app_secret"):
        channel = dict(channel)
        channel["app_secret"] = channel["app_id"]
    # Issue #1728：微信单 bot_token —— 手动保存时 token 同时作为 secret 位建安装
    if provider == "wechat" and channel.get("app_id") and not channel.get("app_secret"):
        channel = dict(channel)
        channel["app_secret"] = channel["app_id"]
    # Issue #431：body 未携带 im 键（或 im 下无渠道键）时为普通保存（换引擎/技能），不写任何 im_
    # 字段，保持现有 IM 配置不变；仅显式携带渠道载荷且 app_id 为空才视为解绑
    # （沿用 _purge_chat_im_binding 清理链路，Issue #294；#956 按 provider 维度清理）
    channel_hit = ("im" in body) and bool(provider) and (("feishu" in im) or ("dingtalk" in im) or "provider" in im)
    explicit_unbind = channel_hit and not channel.get("app_id")
    kwargs = {}
    # A2 收尾：IM 凭证唯一真相源 = grape_t_channel_installation（legacy im_* 三列不再写）
    # 带渠道载荷时旧值必须按渠道取：多渠并存（微信 + 企微同时 active）时若取"全局最近一条"，
    # 保存企微会拿微信当旧值 → 误删微信会话并 revoke 微信安装。
    _hit_prov = provider or "feishu"
    if channel_hit:
        _old_inst = _get_provider_installation(user["id"], _hit_prov)
        # 解绑兜底（#956 口径）：载荷声明的 provider 自己没有生效安装时（旧前端/脏载荷），
        # 按"存量绑定渠道"清理，否则飞书专属映射/会话会漏清。
        if explicit_unbind and not str(((_old_inst or {}).get("config") or {}).get("app_id", "") or ""):
            _old_inst = _active_im_installation(user["id"])
    else:
        _old_inst = _active_im_installation(user["id"])
    old_app_id = str((_old_inst.get("config") or {}).get("app_id", "") or "")
    old_channel = str((_old_inst or {}).get("channel_type", "") or "").strip().lower()
    if old_channel not in _IM_PROVIDERS:
        old_channel = _hit_prov if channel_hit else "feishu"

    if channel.get("app_id"):
        app_id = channel["app_id"]
        # 强约束：一个 app_id 全局唯一。查 active 安装行：repo 轨=被 VOD 占用，user 轨他人=被别人占用
        from src.im.engine_wiring import _connect, default_db_path
        _conn = _connect(default_db_path())
        try:
            _holders = _conn.execute(
                "SELECT scope, owner_user_id FROM grape_t_channel_installation "
                "WHERE legacy_app_id=? AND status='active'", (app_id,)).fetchall()
        finally:
            _conn.close()
        if any(str(r[0]) == "repo" for r in _holders):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "该凭证已被 VOD Agent 使用，不能同时绑定 Chat Agent 和 VOD Agent"})
        if any(str(r[0]) == "user" and str(r[1]) != str(user["id"]) for r in _holders):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "该凭证已被其他用户使用，不能重复绑定"})

        if old_app_id and old_app_id != app_id:
            # 更换 app_id：清旧渠道会话（安装行由下方 sync/revoke 处理，不再动 legacy 表）
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (old_app_id,))
                db.commit()
            log.info(f"Chat IM: replaced old app_id={_safe_im_app_ref(old_app_id, old_channel)} "
                     f"with new app_id={_safe_im_app_ref(app_id, provider)}")
        log.info(f"Chat IM: saved im config for user={user['id']} provider={provider} "
                 f"app_id={_safe_im_app_ref(app_id, provider)}")
    elif explicit_unbind:
        kwargs["im_app_secret"] = ""
    if channel.get("app_secret") and channel["app_secret"] != "********":
        kwargs["im_app_secret"] = channel["app_secret"]

    # Issue #508：backend 无类型校验直传落库 → _resolve_chat_runtime_id（str 形参）
    # → TypeError → 500（GET /config 返回对象形态 {"type": ...} 被调用方回填即触发）。
    # 显式校验：仅接受字符串（本 Issue 口径：对象形态显式 400 拒绝，不归一——
    # 与 #504 的归一落库口径分歧，以两 Issue 各自验收为准）。
    # 空串/None/缺键 → 跳过写入（#431 普通 200 保存不变量保持）；空 dict {} 等畸形值
    # 必须走校验拒绝（验收：{"backend":{}} → 4xx），故用显式空值判断而非真值判断。
    if body.get("backend") is not None and body["backend"] != "":
        _be = body["backend"]
        if not isinstance(_be, str):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "backend 必须为字符串"})
        kwargs["backend"] = _be
    if body.get("runtime_id") is not None:
        kwargs["runtime_id"] = body["runtime_id"]
    if body.get("skill_id") is not None:
        # 新方式：前端直传 skill_id
        skill_id = body["skill_id"]
        skill_name = ""
        skill_scope = ""
        if skill_id:
            from src.user.db import get_skill_by_id
            _sk = get_skill_by_id(skill_id)
            if _sk:
                skill_name = _sk.get("skill_name") or ""
                skill_scope = _sk.get("scope") or ""
            else:
                skill_id = 0
        kwargs["skill"] = skill_name
        kwargs["skill_id"] = skill_id
        kwargs["skill_scope"] = skill_scope
    elif body.get("skill") is not None:
        # 旧方式兼容：按名称+scope 查 id
        # Issue #508：同根因区域一致防御——skill 非 str 直传 SQL 绑定 →
        # ProgrammingError → 500，显式 400（与 backend 校验同一修法口径）。
        if not isinstance(body["skill"], str):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "skill 必须为字符串"})
        kwargs["skill"] = body["skill"]
        skill_name = body["skill"]
        skill_scope = body.get("skill_scope", "")
        skill_id = 0
        if skill_name:
            from src.session_manager import db as _db, db_lock as _db_lock
            def _find_skill(scope, owner_id=""):
                """精确按 scope(+owner) 查 skill_id，不走 official 优先"""
                with _db_lock:
                    c = _db.cursor()
                    if owner_id:
                        row = c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? AND is_active=1 LIMIT 1",
                                        (skill_name, scope, owner_id)).fetchone()
                    else:
                        row = c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND is_active=1 LIMIT 1",
                                        (skill_name, scope)).fetchone()
                    return row[0] if row else 0

            if skill_scope == "repo" and repo:
                from src.repo_key import to_repo_key
                skill_id = _find_skill("repo", to_repo_key(repo))
            elif skill_scope == "user":
                skill_id = _find_skill("user", str(user["id"]))
            elif skill_scope == "official":
                skill_id = _find_skill("official")
            else:
                if repo:
                    from src.repo_key import to_repo_key
                    skill_id = _find_skill("repo", to_repo_key(repo))
                if not skill_id:
                    skill_id = _find_skill("user", str(user["id"]))
                if not skill_id:
                    skill_id = _find_skill("official")
        kwargs["skill_id"] = skill_id
        kwargs["skill_scope"] = skill_scope
    _set_chat_config(user["id"], **kwargs)

    # Issue #294：解绑（显式携带渠道载荷且 app_id 置空）→ 清理全局注册表/渠道会话 + unified_user_mapping 对账
    # Issue #956：按 provider 维度清理（钉钉不碰飞书专属 unified_user_mapping/feishu_onboard_task）；
    # 清理维度取「存量绑定的渠道」（安装行 channel_type），载荷 provider 仅兜底——避免旧绑为飞书、
    # 载荷却声明 dingtalk 时漏清飞书映射
    if explicit_unbind and old_app_id:
        _purge_provider = old_channel
        if _purge_provider not in _IM_PROVIDERS:
            _purge_provider = "feishu"
        _purge_chat_im_binding(user["id"], user.get("username", ""), old_app_id, provider=_purge_provider)

    # 手动配置换 app：对账 unified_user_mapping（feishu-app-scoped-openid-fix.md §2.3）
    # 新旧 im_app_id 不同 → 刷新 app_id 并清空 open_id/feishu_name，要求用户重新 p2p 确认
    _new_app = str(channel.get("app_id") or "")
    if _new_app and old_app_id and old_app_id != _new_app:
        try:
            # FR-18.7: 注册名语境——用 bind_feishu_identity 只清飞书三列，不碰平台列
            from src.session_manager import bind_feishu_identity
            bind_feishu_identity(user["id"], open_id="", app_id=_new_app)
            log.info(f"Chat IM: app changed {_safe_im_app_ref(old_app_id, old_channel)} -> "
                     f"{_safe_im_app_ref(_new_app, provider)}, reconciled unified_user_mapping (open_id cleared)")
        except Exception as e:
            log.warning(f"Chat IM: reconcile unified_user_mapping failed: {e}")

    # Issue #871 写方向反转：保存/解绑/换 app 直写 grape_t_channel_installation（请求内生效，
    # 不依赖扫描窗口）；new 模式下写桥自动反向回写 legacy。失败只告警不阻断。
    # Issue #956：revoke 按「旧渠道类型」匹配安装行（钉钉行走 channel_type=dingtalk，
    # 不传会用默认 feishu 导致 revoke 落空 → 安装行不 revoked、引擎不归零）
    try:
        from src.im.engine_wiring import revoke_installation, sync_installation
        _uid = str(user["id"])
        _old_channel = old_channel
        if explicit_unbind and old_app_id:
            revoke_installation("user", owner_user_id=_uid, app_id=old_app_id,
                                       channel_type=_old_channel)
        elif _new_app:
            # Issue #956：写入口透传 channel_type=provider（feishu|dingtalk），
            # 引擎侧按渠道类型路由到对应工厂（#936 DINGTALK 工厂已注册）
            # Issue #1309：Slack 双令牌 → config 额外落 bot_token/app_token 语义键
            _extra = None
            if (provider or "") == "slack":
                _extra = {"bot_token": _new_app}
                _app_token = kwargs.get("im_app_secret", "") or ""
                if _app_token:
                    _extra["app_token"] = _app_token
            elif (provider or "") == "telegram":
                # Issue #1613：单 Bot Token → config 落 bot_token 语义键，工厂据此建连
                _extra = {"bot_token": _new_app}
            elif (provider or "") == "wechat":
                # Issue #1728：微信 ilink bot_token → config 落 bot_token 语义键
                _extra = {"bot_token": kwargs.get("im_app_secret", "") or _new_app}
            sync_installation("user", owner_user_id=_uid, app_id=_new_app,
                                     app_secret=kwargs.get("im_app_secret", "") or "",
                                     channel_type=provider or "feishu",
                                     extra_config=_extra)
        if _new_app and old_app_id and old_app_id != _new_app:
            revoke_installation("user", owner_user_id=_uid, app_id=old_app_id,
                                       channel_type=_old_channel)
    except Exception as e:
        log.warning(f"Chat IM: sync grape_t_channel_installation failed: {e}")

    # C3 审计：IM 凭证保存/解绑留痕（detail 只记渠道与 app 摘要，绝不落 secret）
    if channel_hit:
        try:
            from src.auth import audit as auth_audit
            _ip = ""
            try:
                _ip = (request.client.host if request.client else "") or ""
            except Exception:
                _ip = ""
            auth_audit.log_audit(
                "im_installation_change",
                user_id=int(user["id"]),
                operator_id=int(user["id"]),
                ip=_ip,
                detail={"action": "unbind" if explicit_unbind else "credential_save",
                        "provider": _hit_prov,
                        "app_ref": _safe_im_app_ref(channel.get("app_id", "") or "", _hit_prov),
                        "scope": "repo" if repo else "user",
                        "repo": repo or ""},
            )
        except Exception as _ae:
            log.warning(f"Chat IM audit log failed: {_ae}")

    return {"message": "配置已更新"}


# ===== 微信 ilink 扫码绑定（Issue #1728 §5.1）=====

@router.post("/im/wechat/login/start", operation_id="chat_wechat_login_start",
             summary="Start WeChat ilink QR login",
             description="Requests an ilink bot QR code and returns login_id + qrcode_img_content; poll status until confirmed.")
async def chat_wechat_login_start(user: dict = Depends(require_no_session_token)):
    """取微信 ilink 绑定二维码（进程内状态机 TTL 10 分钟）。"""
    from src.im.adapters.wechat.login import default_login_manager
    manager = default_login_manager()
    try:
        return await manager.start()
    except Exception as e:
        log.warning(f"Chat IM: wechat login start failed: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": "获取微信绑定二维码失败，请稍后重试"})


def _persist_wechat_login(user: dict, credentials: dict) -> bool:
    """confirmed 凭证落 `grape_t_channel_installation`（A2 收尾：不再回写 legacy im_* 三列）。"""
    bot_token = str(credentials.get("bot_token") or "")
    bot_id = str(credentials.get("ilink_bot_id") or "")
    if not bot_token or not bot_id:
        return False
    baseurl = str(credentials.get("baseurl") or "")
    ilink_user_id = str(credentials.get("ilink_user_id") or "")
    from datetime import datetime as _dt
    extra = {
        "bot_token": bot_token,
        "ilink_user_id": ilink_user_id,
        "bound_at": _dt.now().isoformat(),
    }
    if baseurl:
        extra["baseurl"] = baseurl
    try:
        from src.im.engine_wiring import sync_installation
        sync_installation(
            "user", owner_user_id=str(user["id"]), app_id=bot_id,
            app_secret=bot_token, channel_type="wechat", extra_config=extra)
    except Exception as e:
        log.warning(f"Chat IM: wechat login installation write failed: {e}")
        return False
    log.info(f"Chat IM: wechat bound app_id={_safe_im_app_ref(bot_id, 'wechat')} user={user['id']}")
    # C3 审计：扫码绑定=凭证写入类操作，必须留痕（detail 只带 app 摘要，绝不含 bot_token）
    try:
        from src.auth import audit as auth_audit
        auth_audit.log_audit(
            "im_installation_change",
            user_id=int(user["id"]), operator_id=int(user["id"]),
            detail={"action": "wechat_qr_bind", "provider": "wechat",
                    "app_ref": _safe_im_app_ref(bot_id, "wechat"), "scope": "user"},
        )
    except Exception as _ae:
        log.warning(f"Chat IM: wechat bind audit failed: {_ae}")
    return True


@router.get("/im/wechat/login/status", operation_id="chat_wechat_login_status",
            summary="Get WeChat ilink login status",
            description="Short-poll proxy for ilink qrcode status: wait|scaned|need_verifycode|confirmed|expired. On confirmed the credential is stored to grape_t_channel_installation.")
async def chat_wechat_login_status(id: str = Query(""), verify_code: str = Query(""),
                                   user: dict = Depends(require_no_session_token)):
    """扫码状态短轮询；`need_verifycode` 时前端提交 PIN（verify_code 透传）。"""
    from src.im.adapters.wechat.login import default_login_manager
    manager = default_login_manager()
    session = manager.get(id)
    if session is None:
        return {"status": "expired", "message": "登录会话不存在或已过期"}
    try:
        result = await manager.status(id, verify_code=verify_code)
    except Exception as e:
        log.warning(f"Chat IM: wechat login status failed: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": "查询扫码状态失败，请稍后重试"})
    credentials = result.pop("credentials", None)
    if result.get("status") == "confirmed" and isinstance(credentials, dict):
        if not credentials.get("_bound"):
            if _persist_wechat_login(user, credentials):
                credentials["_bound"] = True
    result.pop("_bound", None)
    return result


# ===== Chat Agent v2: submit/stream 分离（学习 Admin AI AiTaskManager）=====

def _format_chat_failure_text(enqueue_tid: str) -> str:
    """远端 chat 任务失败 → 友好提示（chat-remote-failure-hint-design §2.4）。

    失败原因此前只写 services.errors 列而此处只读 result_text（失败路径恒空），
    用户只见光秃秃「查询失败」。现统一走 friendly_failure_hint 输出
    「发生了什么 + 下一步行动」。enqueue_tid 取不到任务时给出稳定兜底文案。
    """
    try:
        from src.runtime.service import get_task, friendly_failure_hint
        tk = get_task(enqueue_tid) or {}
        hint, action = friendly_failure_hint(tk)
    except Exception:
        hint, action = "任务执行失败", "请稍后重试，必要时联系管理员查看 worker 日志"
    return f"查询失败：{hint}；{action}"

@router.post("/submit", operation_id="chat_submit", summary="Submit (task scenario)", description="Submits a chat query and immediately returns {task_id, conversation_id, status}; the agent runs in the background.")
async def chat_submit(body: ChatMessage, user: dict = Depends(require_no_session_token)):
    """提交 chat 查询 → 立即返回 {task_id, conversation_id, status}。

    学习 Admin AI submit/stream 分离：
    - submit 立即返回 task_id，后台 ChatTaskManager 执行 agent
    - 前端用 GET /task/{task_id}/stream 订阅 SSE（可断开重连，缓冲重放）
    - 页面切换/会话切换不中断后端执行
    """
    from src.api.chat_task_manager import ChatTaskManager
    from src.api.deps import get_user_token_with_refresh

    conv_id = body.conversation_id
    # Issue #344: 不再缓存明文 token 到 chat_conversations
    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
    else:
        conv_id = create_conversation(user["id"], body.repo_key)
    conv = get_conversation(conv_id)
    _auto_title(conv, body.message)

    import uuid as _uuid
    chat_task_id = "chat_" + _uuid.uuid4().hex[:12]
    message_id = add_message(conv_id, "user", body.message + _build_image_markers(_save_chat_images(body.image_data, conv_id)), task_id=chat_task_id, generate_status=1)
    mark_task_processing_by_conversation(conv_id)

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    skill_name = cfg.get("skill", "") or "chat"
    skill_id = cfg.get("skill_id", 0) or 0
    acc = _get_acc(user["username"]) or {}
    history = get_messages(conv_id, limit=20)
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    # Issue #344: 实时获取用户 token（带预刷新），不再从会话缓存读取
    from src.api.deps import get_user_token_with_refresh
    user_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
    gitcode_token = _resolve_gitcode_token(user_token, repo_key)

    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_issue_chat_prompt
        prompt = build_issue_chat_prompt(body.message, repo_key, history, conv,
                                        account=user["username"], runtime_id=cfg.get("runtime_id", 0),
                                        has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(body.message, repo_key, history, skill_name, skill_id,
                                           backend_name=backend_name,
                                           user_info=_build_web_user_info(user, acc),
                                           has_valid_session=has_valid_session)

    log.info(f"Chat submit: backend={backend_name or '(none)'}, skill={skill_name}, "
             f"has_token={bool(gitcode_token)}, user_msg={body.message[:100]}")

    # 图片理解注入
    if body.image_data:
        prompt += "\n\n" + _understand_image(body.image_data)

    # 视频理解不在 submit 同步执行（耗时 30-60s 会导致前端 timeout），
    # 改为在 agent_fn 内部执行，通过 SSE 推送"正在分析视频"步骤
    _video_data = body.video_data

    # 无引擎 → 直接返回错误
    if not backend_name:
        _reply = "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎"
        add_message(conv_id, "agent", _reply, task_id=chat_task_id, generate_status=3)
        await _push_reply_to_feishu(conv, user, _reply)
        return {"task_id": chat_task_id, "conversation_id": conv_id, "status": "error",
                "error": _reply}

    # 引擎路由（与 send/stream 一致）
    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, runtime_credential_id
    rid = cfg.get("runtime_id") or 0
    if not rid:
        try:
            from src.runtime.engine_select import resolve_local_engine
            rid = resolve_local_engine(user.get("id") or 0) or 0
        except Exception:
            rid = 0
    kind = runtime_execution_kind(rid)
    if (runtime_credential_id(rid) or 0) == 0:
        kind = EXEC_MODE_DIRECT

    if kind != EXEC_MODE_DIRECT:
        # 远端引擎离线兜底
        from src.runtime.service import _remote_runtime_ready
        if not _remote_runtime_ready(rid) and backend_name:
            from src.session_manager import _resolve_chat_runtime_id
            _rid3 = _resolve_chat_runtime_id(backend_name) or 0
            if _rid3 and _remote_runtime_ready(_rid3):
                rid = _rid3
                kind = runtime_execution_kind(rid)
        if (runtime_credential_id(rid) or 0) == 0:
            _reply = "⚠️ 未配置远端引擎，任务会话需先在 Agent 配置中绑定远端引擎"
            add_message(conv_id, "agent", _reply, task_id=chat_task_id, generate_status=3)
            await _push_reply_to_feishu(conv, user, _reply)
            return {"task_id": chat_task_id, "conversation_id": conv_id, "status": "error",
                    "error": _reply}

    cwd = _get_chat_cwd(user["id"], repo_key, conv_id)

    if kind == EXEC_MODE_DIRECT:
        # 本机直连：ChatTaskManager 启动 agent_fn
        def agent_fn():
            """agent generator — yield 事件 + finally 写 DB"""
            from src.runtime.executor import stream_chat_agent
            from src.observability.master_usage import MasterRun
            _final_text = ""
            _new_sid = ""
            _ctm_error_detail = ""  # R20.8.7: 保存 error 信息用于 401 标记
            _last_error = ""
            # 用量采集（审计 2026-10-17）：本机直连的 chat 引擎以前**一条 token 都不记**。
            # 归属口径 B：无 agent_tasks 行 → task_id 空、主体 run_id=chat_task_id；
            # 采集/写库失败只打日志，绝不影响对话主链路。
            _mu = MasterRun("chat", chat_task_id, engine=backend_name, cwd=cwd,
                            attr={"repo_key": repo_key, "scene_id": conv_id,
                                  "agent_type": "chat", "user_id": user["id"],
                                  "engine_id": int(rid or 0)})
            _mu.start()
            # 视频理解在 agent_fn 内部异步执行（避免 submit 同步阻塞导致前端 timeout）
            if _video_data:
                yield ("step_start", "正在分析视频，可能需要 30-60 秒...")
                _vdesc = _understand_video(_video_data)
                if _vdesc:
                    prompt_with_video = prompt + "\n\n" + _vdesc
                else:
                    prompt_with_video = prompt
                yield ("step_finish", "视频分析完成")
            else:
                prompt_with_video = prompt
            try:
                for evt_type, data in stream_chat_agent(
                    prompt=prompt_with_video, cwd=cwd, session_id=runtime_session_id,
                    backend_name=backend_name, user_id=user["id"],
                    account=user["username"], user_token=gitcode_token,
                    agent_name="Chat Agent",
                    agent_label=f"[Agent:chat|user:{user['username']}]",
                    repo_key=repo_key,
                    env_extra=chat_session_env_extra(
                        user["id"], conv_id, repo_key, runtime_id=rid,
                        issue_number=(conv or {}).get("issue_number") or ""),
                ):
                    # Issue #442 跨主控兜底：撤回请求可能落在另一台主控（ChatTaskManager 是进程内
                    # 状态），此时本地 cancel 不会触发。撤回标记写在共享库（chat_messages.deleted），
                    # 因此每个事件都复查一次，感知到即中断本轮生成（direct 模式生成即本循环）。
                    if is_task_recalled(chat_task_id):
                        log.info(f"[Chat submit] task recalled, break direct generation "
                                 f"task_id={chat_task_id}")
                        break
                    _mu.feed(evt_type, data)
                    if evt_type == "text":
                        text = data if isinstance(data, str) else (
                            data.get("text", "") if isinstance(data, dict) else str(data))
                        if text:
                            _final_text += text
                            yield ("text", text)
                    elif evt_type == "done":
                        if isinstance(data, dict):
                            done_text = data.get("text", "")
                            if done_text and not _final_text:
                                _final_text = done_text
                            nsid = data.get("oc_session_id", "")
                            if nsid:
                                _new_sid = nsid
                            # R20.8.7: 保存 errors/stderr 用于 401 标记
                            _d_errs = data.get("errors") or []
                            _d_stderr = data.get("stderr") or ""
                            if _d_errs:
                                _ctm_error_detail = " ".join(str(e) for e in _d_errs) + " " + str(_d_stderr)
                            elif _d_stderr:
                                _ctm_error_detail = str(_d_stderr)
                        # session 重试后仍无文本 → 用 error 信息作为回复
                        if not _final_text and _last_error:
                            _final_text = _last_error
                        yield ("done", {"text": _final_text})
                    elif evt_type == "error":
                        # 不 break — 让 stream_agent 继续，使 session 失效重试逻辑
                        # (_stale_fail 检测 + _run_once("") 重跑) 有机会执行。
                        # 记住 error，仅在 done 无有效文本时才作为最终结果。
                        _last_error = str(data)
                        _ctm_error_detail = str(data)[:500]
                        yield ("error", str(data))
                    else:
                        yield (evt_type, data)
            finally:
                # DB 写入（finally 保证，连接断开也不丢）
                # R20.8.7: 401 标记——防止 token 过期后用户重发死循环
                if repo_key and _ctm_error_detail:
                    _err_lower = _ctm_error_detail.lower()
                    _is_401 = is_auth_401_error(_err_lower)
                    if _is_401:
                        try:
                            from src.token_service import detect_and_mark_expired
                            _ctm_error_code = detect_and_mark_expired(repo_key) or ""
                            if _ctm_error_code:
                                log.warning(f"[Chat submit] token expired (error_code={_ctm_error_code}), repo={repo_key}")
                        except Exception as _ce:
                            log.warning(f"[Chat submit] 401 detect failed: {_ce}")
                if _final_text:
                    # Issue #442: 用户消息已撤回 → 不落库、不推送（执行已被终止）
                    if is_task_recalled(chat_task_id):
                        log.info(f"[Chat submit] task recalled, skip DB save task_id={chat_task_id}")
                    else:
                        try:
                            add_message(conv_id, "agent", _final_text,
                                        task_id=chat_task_id, generate_status=2)
                            touch_conversation(conv_id)
                            log.info(f"[Chat submit] DB saved task_id={chat_task_id} len={len(_final_text)}")
                        except Exception as _de:
                            log.warning(f"[Chat submit] DB save failed: {_de}")
                elif not is_task_recalled(chat_task_id):
                    try:
                        add_message(conv_id, "agent", "（无返回内容）",
                                    task_id=chat_task_id, generate_status=3)
                        touch_conversation(conv_id)
                    except Exception as _de:
                        log.warning(f"[Chat submit] DB save (empty) failed: {_de}")
                # 用量落台账：**放在最后**——累计型账本那次 1s 短重试不该拖慢"回复落库/推送"（用户可见）
                _mu.finish(session_id=_new_sid)
                # 飞书推送（投递到主事件循环；后台线程不能跨 loop 复用共享 aiohttp session）
                if _final_text and not is_task_recalled(chat_task_id):
                    try:
                        import asyncio as _aio
                        _dispatch_push_to_main_loop(_push_reply_to_feishu(conv, user, _final_text))
                    except Exception:
                        pass
                # 保存 session_id
                if _new_sid and not runtime_session_id:
                    try:
                        update_conversation(conv_id, runtime_session_id=_new_sid)
                    except Exception:
                        pass

        ChatTaskManager.submit(chat_task_id, conv_id, user["id"], user["username"], agent_fn)
    else:
        # 远端引擎：入队 + ChatTaskManager 轮询 task_messages
        from src.runtime.service import enqueue_task
        from src.api.session_store import get_or_create_agent_session
        session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name, runtime_id=rid)
        enqueue_tid = enqueue_task(
            agent_session_id=session.get("id") or "",
            runtime_id=rid,
            event_seq=None, issue_id="",
            repo_key=repo_key or f"chat:{user['id']}", agent_type="chat",
            skill=skill_name or "chat",
            skill_id=skill_id,
            prompt=prompt, cwd=cwd,
            runtime_session_id=runtime_session_id,
            event_type="chat",
            account=user.get("username") or "", user_id=user["id"],
            repo_gitcode_token="",  # Issue #344: no plaintext token in task payload
            env_extra={**chat_session_env_extra(
                          user["id"], conv_id, repo_key, runtime_id=rid,
                          issue_number=(conv or {}).get("issue_number") or ""),
                       **resolve_task_server_env(rid)},
        )
        log.info(f"Chat submit enqueued: enqueue_tid={enqueue_tid} chat_task_id={chat_task_id}")
        # Issue #442: 记录映射，撤回时经 #136 cancel_task 终止远端任务
        _CHAT_ENQUEUE_MAP[chat_task_id] = enqueue_tid

        # 远端轮询 agent_fn
        def remote_agent_fn():
            """轮询远端 task_messages → yield 事件"""
            import time as _t
            from src.runtime.service import get_task
            _final_text = ""
            last_seq = 0
            while True:
                _t.sleep(0.5)
                # Issue #442 跨主控兜底：撤回请求可能落在另一台主控（_CHAT_ENQUEUE_MAP 是进程内
                # 状态，跨节点不可见）。撤回标记写在共享库，因此本循环（任务归属主控）复查后
                # 主动终止远端任务 → worker 心跳感知杀引擎，不再依赖撤回方那台机器的内存映射。
                if is_task_recalled(chat_task_id):
                    try:
                        from src.runtime.service import cancel_task as _svc_cancel_task
                        _svc_cancel_task(enqueue_tid)
                        log.info(f"[Chat submit remote] task recalled, cancelled remote task "
                                 f"enqueue_tid={enqueue_tid} chat_task_id={chat_task_id}")
                    except Exception as _rce:
                        log.warning(f"[Chat submit remote] cancel recalled task failed: {_rce}")
                    break
                # 检查任务终态
                tk = get_task(enqueue_tid)
                if tk and tk.get("status") in ("completed", "failed", "cancelled"):
                    _final_text = tk.get("result_text") or ""
                    if not _final_text and tk.get("status") == "completed":
                        _final_text = "Done"
                    elif tk.get("status") == "failed":
                        _final_text = _format_chat_failure_text(enqueue_tid)
                    break
            # DB 写入（Issue #442: 已撤回则跳过）
            if is_task_recalled(chat_task_id):
                log.info(f"[Chat submit remote] task recalled, skip DB save task_id={chat_task_id}")
                return
            try:
                add_message(conv_id, "agent", _final_text,
                           task_id=chat_task_id, generate_status=2)
                touch_conversation(conv_id)
            except Exception as _de:
                log.warning(f"[Chat submit remote] DB save failed: {_de}")
            # 飞书推送（后台线程 → 投递主 loop）
            try:
                import asyncio as _aio
                _dispatch_push_to_main_loop(_push_reply_to_feishu(conv, user, _final_text))
            except Exception:
                pass
            yield ("done", {"text": _final_text})

        ChatTaskManager.submit(chat_task_id, conv_id, user["id"], user["username"], remote_agent_fn)

    return {"task_id": chat_task_id, "conversation_id": conv_id, "status": "running",
            "message_id": message_id}


@router.get("/task/{task_id}/stream", operation_id="chat_task_stream", summary="Async task stream", description="SSE subscribe to task output: replays the buffer then appends live events; DB fallback for finished tasks.")
async def chat_task_stream(task_id: str, user: dict = Depends(require_no_session_token)):
    """SSE 订阅任务输出 — 先重放缓冲，再实时追加（学习 Admin AI subscribe）。

    - 内存有任务 → 重放 events 缓冲 + 加入 subscribers 接收后续
    - 内存无任务 → 查 DB 获取最终结果（已完成/跨机器）
    """
    from src.api.chat_task_manager import ChatTaskManager

    loop = asyncio.get_running_loop()
    q, task = ChatTaskManager.subscribe(task_id, loop)

    # 内存无任务 → 可能是跨机器 submit，DB 有 user 消息但 agent 还在执行
    # 轮询 DB 等待 agent 回复（总超时 = CHAT_STREAM_TIMEOUT，可配置，Issue #399），而不是立刻报「查询已中断」
    if not q:
        from src.session_manager import db as _msg_db, db_lock as _msg_lock
        from src.config import get_chat_stream_timeout, CHAT_STREAM_HEARTBEAT_INTERVAL as _hb_interval

        async def _poll_db_stream():
            import time as _t
            conv_id = ""
            timeout_s = get_chat_stream_timeout()
            # 先发思考中占位
            yield f"data: {json.dumps({'type': '思考中', 'text': ''})}\n\n"
            deadline = _t.time() + timeout_s
            last_heartbeat = _t.time()
            while _t.time() < deadline:
                with _msg_lock:
                    c = _msg_db.cursor()
                    c.execute("SELECT role, generate_status, content, conversation_id FROM chat_messages WHERE task_id=? ORDER BY id", (task_id,))
                    rows = c.fetchall()
                if not rows:
                    # 连 user 消息都没有 → 任务不存在
                    yield f"data: {json.dumps({'type': 'error', 'text': '任务不存在', 'task_id': task_id})}\n\n"
                    return
                has_agent = any(r[0] == "agent" for r in rows)
                if has_agent:
                    for r in rows:
                        if r[0] == "agent":
                            content = r[2] or ""
                            conv_id = r[3] or ""
                            gen_status = r[1]
                            evt_type = "done" if gen_status == 2 else "error"
                            yield f"data: {json.dumps({'type': evt_type, 'text': content, 'conversation_id': conv_id, 'task_id': task_id})}\n\n"
                            return
                # 有 user 无 agent → agent 仍在执行（可能在另一台机器），继续等
                conv_id = rows[0][3] if rows else ""
                # 等待期间周期性推送「思考中」心跳（Issue #399）
                if _t.time() - last_heartbeat >= _hb_interval:
                    yield f"data: {json.dumps({'type': '思考中', 'text': '仍在处理，请稍候…'})}\n\n"
                    last_heartbeat = _t.time()
                await asyncio.sleep(1)
            # 超时（时长动态取配置）
            yield f"data: {json.dumps({'type': 'error', 'text': f'查询超时（{timeout_s}s），请重试', 'conversation_id': conv_id, 'task_id': task_id})}\n\n"

        return StreamingResponse(
            _poll_db_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    # 内存有任务 → SSE 流
    async def event_stream():
        import time as _t
        from src.config import get_chat_stream_timeout, CHAT_STREAM_HEARTBEAT_INTERVAL as _hb_interval
        timeout_s = get_chat_stream_timeout()
        deadline = _t.time() + timeout_s
        # 先发一个思考中占位（如果缓冲为空）
        if not task.events:
            yield f"data: {json.dumps({'type': '思考中', 'text': ''})}\n\n"
        # 消费 queue（重放缓冲 + 实时事件）
        while True:
            remaining = deadline - _t.time()
            if remaining <= 0:
                yield f"data: {json.dumps({'type': 'error', 'text': f'超时（{timeout_s}s 无输出）'})}\n\n"
                ChatTaskManager.unsubscribe(task_id, q)
                return
            try:
                # 短超时心跳 + 总超时：每 _hb_interval 无输出推一次「思考中」，累计 timeout_s 才报超时（Issue #399）
                item = await asyncio.wait_for(q.get(), timeout=max(min(_hb_interval, remaining), 0.1))
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': '思考中', 'text': '仍在处理，请稍候…'})}\n\n"
                continue
            if item is None:  # 结束信号
                break
            yield f"data: {item}\n\n"
        # 发送 done 事件
        final_text = task.final_text if task else ""
        yield f"data: {json.dumps({'type': 'done', 'text': final_text, 'task_id': task_id})}\n\n"
        ChatTaskManager.unsubscribe(task_id, q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# ===== Chat Agent 持久化查询（旧 v1 机制，保留兼容）=====
# 内存任务缓冲（task_id → {status, events, result, conv_id, created_at, finished_at}）
import threading as _threading
_chat_tasks: dict = {}
_chat_tasks_lock = _threading.Lock()
_CHAT_TASK_TTL = 30 * 60  # 30 分钟后清理已完成任务


def _chat_task_cleanup():
    """清理过期的已完成任务缓冲。"""
    import time as _t
    with _chat_tasks_lock:
        now = _t.time()
        expired = [tid for tid, t in _chat_tasks.items()
                   if t.get("status") != "running" and t.get("finished_at", 0) > 0
                   and now - t["finished_at"] > _CHAT_TASK_TTL]
        for tid in expired:
            _chat_tasks.pop(tid, None)


@router.post("/send/async", operation_id="chat_send_async", summary="Send message (async)", description="Async message send: DB/engine work runs in executors. Local engines return a fallback stream; remote engines return task_id.")
async def send_message_async(body: ChatMessage, user: dict = Depends(require_no_session_token)):
    """异步发送消息：所有同步 DB/引擎操作在 run_in_executor 中执行，不阻塞 event loop。

    本机直连 → 返回 fallback_stream（前端回退到 send/stream）
    远端引擎 → 返回 task_id（前端轮询 status）
    """
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _sync_send_async, body, user)
    # 远端引擎 → 启动异步轮询（在主 event loop 中，不阻塞）
    if result.get("_enqueue_task_id"):
        asyncio.ensure_future(_poll_worker(result["task_id"], result["_enqueue_task_id"], result["conversation_id"]))
    result.pop("_enqueue_task_id", None)
    return result


def _sync_send_async(body: ChatMessage, user: dict) -> dict:
    """send/async 的同步实现（在线程池中执行，不阻塞 event loop）。"""
    import time as _t, uuid as _uuid
    conv_id = body.conversation_id
    # Issue #344: 不再缓存明文 token 到 chat_conversations
    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
    else:
        conv_id = create_conversation(user["id"], body.repo_key)
    conv = get_conversation(conv_id)
    _auto_title(conv, body.message)

    import uuid as _uuid
    chat_task_id = "chat_" + _uuid.uuid4().hex[:12]
    add_message(conv_id, "user", body.message + _build_image_markers(_save_chat_images(body.image_data, conv_id)), task_id=chat_task_id, generate_status=1)
    mark_task_processing_by_conversation(conv_id)

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    skill_name = cfg.get("skill", "") or "chat"
    skill_id = cfg.get("skill_id", 0) or 0
    acc = _get_acc(user["username"]) or {}
    history = get_messages(conv_id, limit=20)
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    # Issue #344: 实时获取用户 token（带预刷新），不再从会话缓存读取
    from src.api.deps import get_user_token_with_refresh
    user_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
    gitcode_token = _resolve_gitcode_token(user_token, repo_key)

    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_issue_chat_prompt
        prompt = build_issue_chat_prompt(body.message, repo_key, history, conv, account=user["username"], runtime_id=cfg.get("runtime_id", 0), has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(body.message, repo_key, history, skill_name, skill_id, backend_name=backend_name, user_info=_build_web_user_info(user, acc), has_valid_session=has_valid_session)

    # 图片理解注入
    if body.image_data:
        prompt += "\n\n" + _understand_image(body.image_data)

    # 视频理解注入
    if body.video_data:
        prompt += "\n\n" + _understand_video(body.video_data)

    rid = cfg.get("runtime_id") or 0
    if not rid:
        try:
            from src.runtime.engine_select import resolve_local_engine
            rid = resolve_local_engine(user.get("id") or 0) or 0
        except Exception:
            rid = 0

    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, runtime_credential_id
    kind = runtime_execution_kind(rid)
    if kind != EXEC_MODE_DIRECT and (runtime_credential_id(rid) or 0) == 0 and backend_name:
        from src.session_manager import _resolve_chat_runtime_id
        rid = _resolve_chat_runtime_id(backend_name) or 0
        kind = runtime_execution_kind(rid)

    # 本机直连 → 回退到 send/stream
    if kind == EXEC_MODE_DIRECT:
        return {"task_id": "", "conversation_id": conv_id, "status": "fallback_stream",
                "_enqueue_task_id": ""}

    # 远端引擎 → 入队
    task_id = "chat_" + _uuid.uuid4().hex[:12]
    try:
        from src.runtime.service import enqueue_task
        from src.api.session_store import get_or_create_agent_session
        session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name, runtime_id=rid)
        tid = enqueue_task(
            agent_session_id=session.get("id") or "",
            runtime_id=rid,
            event_seq=None, issue_id="",
            repo_key=repo_key or f"chat:{user['id']}", agent_type="chat",
            skill=skill_name or "chat",
            skill_id=skill_id,
            prompt=prompt, cwd=_get_chat_cwd(user["id"], repo_key, conv_id),
            runtime_session_id=runtime_session_id,
            event_type="chat",
            account=user.get("username") or "", user_id=user["id"],
            repo_gitcode_token="",  # Issue #344: no plaintext token in task payload
            env_extra={**chat_session_env_extra(
                          user["id"], conv_id, repo_key, runtime_id=rid,
                          issue_number=(conv or {}).get("issue_number") or ""),
                       **resolve_task_server_env(rid)},
        )
        with _chat_tasks_lock:
            _chat_tasks[task_id] = {
                "status": "running", "events": [], "result": "", "error": "",
                "conv_id": conv_id, "user_id": user["id"],
                "created_at": _t.time(), "finished_at": 0,
                "enqueue_task_id": tid,
            }
        return {"task_id": task_id, "conversation_id": conv_id, "status": "running",
                "_enqueue_task_id": tid}
    except Exception as e:
        return {"task_id": "", "conversation_id": conv_id, "status": "error", "message": str(e)[:200]}


async def _poll_worker(chat_task_id: str, enqueue_task_id: str, conv_id: str):
    """轻量级异步轮询：每 2s 查 worker 任务状态，完成时写入结果。"""
    import time as _t
    while True:
        await asyncio.sleep(2)
        from src.runtime.service import get_task
        tk = get_task(enqueue_task_id)
        if tk and tk.get("status") in ("completed", "failed", "cancelled"):
            final_text = tk.get("result_text") or ""
            if not final_text and tk.get("status") == "completed":
                final_text = "Done"
            elif tk.get("status") == "failed":
                final_text = _format_chat_failure_text(enqueue_task_id)
            add_message(conv_id, "agent", final_text)
            touch_conversation(conv_id)
            with _chat_tasks_lock:
                t = _chat_tasks.get(chat_task_id)
                if t:
                    t["status"] = "done"
                    t["result"] = final_text
                    t["finished_at"] = _t.time()
            break


def _execute_chat_task(task_id: str):
    """后台执行 chat 任务（direct 模式 stream_agent / queue 模式 enqueue + wait）。"""
    import asyncio as _aio
    with _chat_tasks_lock:
        task = _chat_tasks.get(task_id)
        if not task:
            return
        task["status"] = "running"

    try:
        backend_name = task.get("backend", "")
        rid = task.get("runtime_id", 0)
        prompt = task.get("prompt", "")
        conv_id = task.get("conv_id", "")
        conv = task.get("conv", {})
        user = task.get("user", {})
        skill_name = task.get("skill_name", "chat")
        skill_id = task.get("skill_id", 0)
        repo_key = task.get("repo_key", "")
        runtime_session_id = task.get("runtime_session_id", "")
        cwd = task.get("cwd", "")

        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
        kind = runtime_execution_kind(rid)
        _exec_error = ""  # R20.8.1: 保存 error 信息用于 401 标记（direct 模式填充）

        if kind == EXEC_MODE_DIRECT:
            # 本机直连：stream_agent（同步 generator，不需要 event loop）
            from src.runtime.executor import stream_agent
            final_text = ""
            try:
                for evt_type, data in stream_agent(
                    rid, prompt, cwd=cwd,
                    skill=skill_name, skill_id=skill_id,
                    runtime_session_id=runtime_session_id,
                    repo_gitcode_token="",  # Issue #344: no plaintext token in task payload
                    repo_key=repo_key,
                ):
                    if evt_type == "text" or evt_type == "token":
                        text = data if isinstance(data, str) else str(data)
                        final_text += text
                        with _chat_tasks_lock:
                            t = _chat_tasks.get(task_id)
                            if t:
                                t["events"].append({"type": "token", "data": text})
                                t["result"] = final_text
                    elif evt_type == "done" or evt_type == "complete":
                        # R20.8.1: 保存 done 事件的 errors/stderr 用于 401 标记
                        if isinstance(data, dict):
                            _exec_errors = data.get("errors") or []
                            _exec_stderr = data.get("stderr") or ""
                            if _exec_errors:
                                _exec_error = " ".join(str(e) for e in _exec_errors) + " " + str(_exec_stderr)
                            elif _exec_stderr:
                                _exec_error = str(_exec_stderr)
                        if data:
                            final_text = data if isinstance(data, str) else (data.get("text", "") if isinstance(data, dict) else str(data))
                        break
                    elif evt_type == "error":
                        _exec_error = str(data)[:500]
                        with _chat_tasks_lock:
                            t = _chat_tasks.get(task_id)
                            if t:
                                t["error"] = str(data)[:500]
                        break
            except Exception as e:
                final_text = f"查询异常: {e}"
                with _chat_tasks_lock:
                    t = _chat_tasks.get(task_id)
                    if t:
                        t["error"] = str(e)[:500]
        else:
            # 远端引擎：enqueue + wait
            from src.runtime.service import enqueue_task, wait_task_terminal_sync
            from src.api.session_store import get_or_create_agent_session
            session = get_or_create_agent_session(f"chat:{user.get('id', 0)}", "chat", backend_name, runtime_id=rid)
            tid = enqueue_task(
                agent_session_id=session.get("id") or "",
                runtime_id=rid,
                event_seq=None, issue_id="",
                repo_key=repo_key or f"chat:{user.get('id', 0)}", agent_type="chat",
                skill=skill_name or "chat",
                skill_id=skill_id,
                prompt=prompt, cwd=cwd,
                runtime_session_id=runtime_session_id,
                event_type="chat",
                account=user.get("username") or "", user_id=user.get("id", 0),
                repo_gitcode_token="",  # Issue #344: no plaintext token in task payload
                env_extra={**chat_session_env_extra(
                              user.get("id", 0), conv_id, repo_key, runtime_id=rid,
                              issue_number=(conv or {}).get("issue_number") or ""),
                           **resolve_task_server_env(rid)},
            )
            # 同步等待任务完成
            import time as _t
            while True:
                from src.runtime.service import get_task
                tk = get_task(tid)
                if tk and tk.get("status") in ("completed", "failed", "cancelled"):
                    final_text = tk.get("result_text") or ""
                    if not final_text and tk.get("status") == "completed":
                        final_text = "Done"
                    elif tk.get("status") == "failed":
                        final_text = _format_chat_failure_text(tid)
                    break
                _t.sleep(1)

        # 写入结果
        if not final_text:
            final_text = f"收到您的消息：「{task.get('body_message', '')}」\n\n当前使用 {backend_name} 后端处理。"

        # R20.8.1: direct 模式 agent 执行后 401 标记（防 token 过期死循环）
        # 基于 error 事件数据和 done 事件的 errors/stderr 字段判断，不扫 final_text
        _task_repo_key = task.get("repo_key", "")
        if _task_repo_key and not _task_repo_key.startswith("chat:") and _exec_error:
            _err_lower = _exec_error.lower()
            _is_401 = is_auth_401_error(_err_lower)
            if _is_401:
                try:
                    from src.token_service import detect_and_mark_expired
                    _task_error_code = detect_and_mark_expired(_task_repo_key) or ""
                    if _task_error_code:
                        _pName = "GitHub" if _task_repo_key.startswith("github:") else "GitCode"
                        final_text = f"⚠️ {_pName} Token 已过期或被撤销，请在设置页重新绑定 Token 后重试。"
                except Exception as _te:
                    from src.config import log as _cte_log
                    _cte_log.warning(f"Chat task post-exec 401 detect failed: {_te}")

        add_message(conv_id, "agent", final_text)
        touch_conversation(conv_id)
        # 推送到飞书（投递主 loop，避免跨 loop 复用共享 aiohttp session）
        try:
            _dispatch_push_to_main_loop(_push_reply_to_feishu(conv, user, final_text))
        except Exception:
            pass
        if runtime_session_id and not task.get("runtime_session_id"):
            update_conversation(conv_id, runtime_session_id=runtime_session_id)

        with _chat_tasks_lock:
            t = _chat_tasks.get(task_id)
            if t:
                t["status"] = "done"
                t["result"] = final_text
                t["finished_at"] = _t.time()
                t["events"].append({"type": "done", "data": final_text})
    except Exception as e:
        import time as _t
        with _chat_tasks_lock:
            t = _chat_tasks.get(task_id)
            if t:
                t["status"] = "error"
                t["error"] = str(e)[:500]
                t["finished_at"] = _t.time()
        from src.config import log
        log.warning(f"Chat task {task_id} failed: {e}")


@router.get("/task/{task_id}/status", operation_id="chat_task_status", summary="Async task status", description="Queries chat task status (cross-machine; memory -> legacy cache -> DB fallback).")
def chat_task_status(task_id: str, user: dict = Depends(require_no_session_token)):
    """查询 chat 任务状态（跨机器可查，对齐 Admin AI getTaskFinalStatus）。

    ChatTaskManager 内存优先（v2）→ 旧 _chat_tasks 内存 → DB 兜底。
    generate_status: 1=生成中(user已写) 2=成功(assistant已写) 3=失败 4=中断
    """
    import time as _t

    # 1. ChatTaskManager 内存优先（v2 新增）
    from src.api.chat_task_manager import ChatTaskManager
    mem = ChatTaskManager.get_status(task_id)
    if mem:
        return {
            "task_id": task_id,
            "status": mem["status"],
            "result": mem["text"],
            "text": mem["text"],
            "error": mem["error"],
            "source": "memory_v2",
        }

    # 2. 旧 _chat_tasks 内存（v1 兼容）
    with _chat_tasks_lock:
        task = _chat_tasks.get(task_id)
        if task:
            return {
                "task_id": task_id,
                "status": task["status"],
                "result": task.get("result", ""),
                "text": task.get("result", ""),
                "error": task.get("error", ""),
                "conversation_id": task.get("conv_id", ""),
                "elapsed_ms": int(_t.time() * 1000 - task.get("created_at", 0) * 1000),
                "source": "memory_v1",
            }

    # 3. DB 兜底（chat_messages 表 by task_id）
    from src.session_manager import db as _msg_db, db_lock as _msg_lock
    with _msg_lock:
        c = _msg_db.cursor()
        c.execute("SELECT role, generate_status, content, conversation_id FROM chat_messages WHERE task_id=? ORDER BY id", (task_id,))
        rows = c.fetchall()
    has_user = any(r[0] == "user" for r in rows)
    has_assistant = any(r[0] == "agent" for r in rows)
    if has_assistant:
        # assistant 消息已写入 → 任务已完成
        for r in rows:
            if r[0] == "agent":
                gen_status = r[1]
                content = r[2] or ""
                conv_id = r[3] or ""
                status = "done" if gen_status == 2 else "error"
                return {"task_id": task_id, "status": status, "result": content,
                        "text": content,
                        "conversation_id": conv_id, "error": "" if gen_status == 2 else "生成失败",
                        "source": "db"}
    if has_user and not has_assistant:
        # user 已写但 assistant 未写 → agent 可能仍在另一台机器执行
        # 返回 running 让前端继续等待 + 重连 SSE 轮询 DB（跨机器容灾）
        conv_id = rows[0][3] if rows else ""
        return {"task_id": task_id, "status": "running", "result": "",
                "text": "",
                "conversation_id": conv_id, "error": "",
                "source": "db"}
    return {"task_id": task_id, "status": "not_found"}


