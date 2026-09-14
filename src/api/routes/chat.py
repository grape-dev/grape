"""Chat Agent 路由：对话、消息、配置、反馈"""
import json, asyncio, tempfile, os, threading, time

from src.config import WORKSPACE_DIR, CHAT_CWD_BASE, log
from src.prompt_builder import build_general_chat_prompt

def _get_chat_cwd(user_id: int, repo_key: str, conv_id: str) -> str:
    """每个 conversation 独立工作目录，避免相互影响。

    统一结构 WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}，与飞书 IM 严格一致
    （同一 conv_id 同一路径，见 web-chat-no-response-cwd-fix.md §4）。
    远端 worker 执行时按 _remap_cwd 映射到各自用户 home；本机 direct 执行 root 可写。
    """
    from src.repo_key import parse_repo_key
    _, owner, repo = parse_repo_key(repo_key or "")
    path = os.path.join(WORKSPACE_DIR, owner or "__none__", repo or "__none__",
                        "chat", str(user_id), conv_id)
    os.makedirs(path, exist_ok=True)
    return path
# 限制同时运行的 opencode 子进程数（防止大量用户并发导致服务器崩溃）
_CHAT_AGENT_SEMAPHORE = threading.Semaphore(20)
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.api.deps import get_current_user, get_current_user_or_api_key, require_no_session_token
from src.user import db as user_db
from src.session_manager import (
    create_conversation, get_user_conversations, get_conversation,
    add_message, get_messages, touch_conversation, update_conversation,
    delete_messages_by_conv, delete_conversation,
    get_chat_config as _get_chat_config,
    set_chat_config as _set_chat_config,
    mark_task_processing_by_conversation,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])


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
async def analyze_image(url: str):
    """供 agent curl 调用：下载图片 → VLM 分析 → 返回文字描述。

    agent 在 issue body 里看到图片链接后，curl 调用本端点获取图片内容描述。
    VLM 管线三级降级：qwen2.5-vl-72b 真视觉 → OCR+LLM 猜图 → 纯 OCR。
    """
    from urllib.parse import urlparse
    import tempfile, urllib.request

    # 域名白名单（防 SSRF）
    parsed = urlparse(url)
    _allowed = ("cdn-img.gitcode.com", "cdn-files.gitcode.com")
    if parsed.hostname not in _allowed:
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

@router.get("/conversations", operation_id="chat_conversations", summary="会话列表")
def list_conversations(repo_key: str = Query(default=""), user: dict = Depends(require_no_session_token)):
    convs = get_user_conversations(user["id"], repo_key=repo_key)
    return {"conversations": convs}


class NewConversationBody(BaseModel):
    repo_key: str = ""


@router.post("/conversations")
def new_conversation(body: NewConversationBody, user: dict = Depends(get_current_user)):
    cid = create_conversation(user["id"], repo_key=body.repo_key)
    return {"conversation_id": cid}


@router.delete("/conversations/{conv_id}")
def remove_conversation(conv_id: str, user: dict = Depends(get_current_user)):
    conv = get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "对话未找到"})
    delete_messages_by_conv(conv_id)
    delete_conversation(conv_id)
    return {"message": "对话已删除"}


class EnsureIssueConvBody(BaseModel):
    repo_key: str
    issue_number: int


@router.post("/conversations/ensure-issue")
def ensure_issue_conversation(body: EnsureIssueConvBody, user: dict = Depends(get_current_user)):
    """基于 repo_key + issue_number 查找或新建任务会话（Task Center 跳转用）。

    会话被删除后，Task Center 双击跳转时调用此接口重建「Issue #N」会话，
    并写入任务上下文消息 + 从 GitCode Issue 补拉 agent 评论。
    """
    from src.api.routes.tasks import _ensure_task_conversation, _get_issue_title
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
        from src.user.db import get_gitcode_token
        from src.repo_key import from_repo_key
        _repo = from_repo_key(rk)
        _token = get_gitcode_token(repo_key=rk) or ""
        if _repo and _token:
            _sync_issue_comments_to_conversation(rk, body.issue_number, _token, repository=_repo)
    except Exception:
        pass
    return {"conversation_id": conv_id}


# ====== Messages ======

@router.get("/messages/{conv_id}", operation_id="chat_messages", summary="会话消息列表")
def list_messages(conv_id: str, before: int = Query(0, ge=0), user: dict = Depends(require_no_session_token)):
    conv = get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "对话未找到"})
    # 任务会话：打开时自动从 GitCode Issue 补拉 agent 评论到会话
    # （agent 通过技能 curl 在 Issue 上发的澄清追问等评论，可能未同步到会话）
    if not before and conv.get("conv_type") == "task" and conv.get("issue_number"):
        try:
            from src.api.event_pipeline import _sync_issue_comments_to_conversation
            from src.user.db import get_gitcode_token
            from src.repo_key import from_repo_key
            _rk = conv.get("repo_key", "")
            _repo = from_repo_key(_rk) if _rk else ""
            _token = get_gitcode_token(repo_key=_rk) or ""
            if _repo and _token:
                _sync_issue_comments_to_conversation(_rk, conv["issue_number"], _token, repository=_repo)
        except Exception:
            pass  # 旁路同步，不影响消息加载
    msgs = get_messages(conv_id, before_id=before)
    return {"messages": msgs, "conversation": conv}


# ====== Send ======

def _auto_title(conv: dict, message: str):
    """首次消息时自动生成对话标题"""
    if conv and not conv.get("title") and message:
        title = message.strip()[:30]
        if len(message) > 30:
            title += "..."
        update_conversation(conv["id"], title=title)


def _dispatch_push_to_main_loop(coro):
    """把 web→飞书推送协程投递到主事件循环执行。

    agent_fn 运行在 ChatTaskManager 后台线程，`asyncio.run(coro)` 会新建事件循环，
    而复用的共享 aiohttp ClientSession 绑定主 loop → 跨 loop 抛
    "Timeout context manager should be used inside a task"。这里用 run_coroutine_threadsafe
    投递回 BotManager 保存的主 loop，与共享 session 同 loop。

    返回主 loop 的 Future（调用方不等待——fire-and-forget），失败仅告警。
    """
    import asyncio
    from src import api as _api_pkg
    bm = getattr(getattr(_api_pkg, "main", None), "bot_manager", None)
    loop = getattr(bm, "loop", None)
    if loop is not None and loop.is_running():
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            fut.add_done_callback(lambda f: f.exception() if not f.cancelled() and f.exception() else None)
            return fut
        except Exception as e:
            log.warning(f"Chat web->feishu push dispatch failed: {e}")
    # 无主 loop（启动异常/测试）→ 兜底同步跑
    try:
        asyncio.run(coro)
    except Exception as e:
        log.warning(f"Chat web->feishu push (fallback) failed: {e}")
    return None


async def _push_reply_to_feishu(conv: dict | None, user: dict, text: str):
    """web 答复推送到飞书（feishu-web-chat-unified-design.md §3.2）。

    conversation 已被飞书接入（im_channel_id 非空）时，用该用户配置的 chat_bot
    主动推送答复给其飞书 open_id。P2P 场景 im_channel_id 即用户 open_id。
    """
    try:
        im_channel_id = (conv or {}).get("im_channel_id", "")
        if not im_channel_id:
            return
        from src.session_manager import db, db_lock
        from src.api.routes.tasks import _get_feishu_open_id
        open_id = _get_feishu_open_id(user.get("username", ""))
        if not open_id and im_channel_id.startswith("ou_"):
            open_id = im_channel_id
        if not open_id:
            return
        from src.api import main as api_main
        bm = api_main.bot_manager
        if not bm or not bm.api:
            return
        app_id = ""
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT im_app_id FROM chat_agent_session WHERE user_id=? AND im_app_id!='' LIMIT 1", (user.get("id") or 0,)).fetchone()
            if row:
                app_id = row[0]
        if not app_id:
            return
        ctx = bm.get_bot(app_id)
        if not ctx:
            return
        await bm.api.send_card_to_user(ctx, open_id, f"[来自Web] {text[:1800]}", title="Chat Agent")
        from src.config import log
        log.info(f"Chat web->feishu push: conv={(conv or {}).get('id')} open_id={open_id[:20]} text_len={len(text)}")
    except Exception as e:
        from src.config import log
        log.warning(f"Chat web->feishu push failed: {e}")


def _resolve_gitcode_token(conv: dict | None, conv_id: str, user_token: str, repo_key: str = "") -> str:
    """解析注入 agent 的 GitCode token：优先用户当前 token（user_accounts），
    并在会话缓存过期/为空时刷新 conversation.user_gitcode_token。

    修复：会话创建时缓存旧 token（曾 401 Bad credentials），非空缓存永不被回填刷新
    （chat.py 原逻辑仅空值才覆盖）。见 2026-08-16 修复。

    兜底：用户 token 与会话缓存均为空时，取仓库 api token
    （get_gitcode_token：devops_agent_session.repo_gitcode_token → managed_repos.bot_gitcode_token
    → 全局 bot token），避免 chat agent 拿空 USER_GITCODE_TOKEN 调 GitCode API 报 401。
    """
    if user_token:
        cached = (conv or {}).get("user_gitcode_token", "") if conv else ""
        if conv_id and cached != user_token:
            try:
                update_conversation(conv_id, user_gitcode_token=user_token)
            except Exception as e:
                log.warning(f"Chat resolve token refresh failed: {e}")
        return user_token
    cached = (conv or {}).get("user_gitcode_token", "") if conv else ""
    if cached:
        return cached
    if repo_key:
        try:
            from src.user.db import get_gitcode_token
            repo_tok = get_gitcode_token(repo_key=repo_key) or ""
            if repo_tok:
                log.warning(f"Chat resolve token: user token empty, fallback to repo token (repo={repo_key})")
                return repo_tok
        except Exception as e:
            log.warning(f"Chat resolve token fallback failed: {e}")
    return ""


def _build_web_user_info(user: dict, acc: dict | None = None) -> str:
    """构建 Web Chat prompt 的用户基本信息（与飞书 user_info 格式一致）。

    user 为 FastAPI 当前用户 dict（username 即 GitCode 登录名）；
    acc 为 user_accounts 行（含 nickname/gitcode_id）。注入后使 Web Agent
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


@router.post("/send")
async def send_message(body: ChatMessage, user: dict = Depends(get_current_user)):
    conv_id = body.conversation_id
    acc = user_db.get_user_account(user["username"]) or {}
    # Issue #52: 用带预刷新的 token 获取（token 临近过期时自动刷新）
    from src.api.deps import get_user_token_with_refresh
    user_token = get_user_token_with_refresh(user["username"], user_id=user["id"])

    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
        if not conv.get("user_gitcode_token") and user_token:
            update_conversation(conv_id, user_gitcode_token=user_token)
            conv = get_conversation(conv_id)
    else:
        conv_id = create_conversation(user["id"], body.repo_key, user_gitcode_token=user_token)
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
                from src.api.routes.tasks import confirm_issue_done
                confirm_issue_done(_task_repo_key, _task_issue_num, confirmed_by="chat_agent")
                add_message(conv_id, "assistant", "✅ 任务已确认完成")
                return {"conversation_id": conv_id, "reply": "✅ 任务已确认完成", "task_command": "done"}
            except Exception as _e:
                _chat_log.warning(f"Chat command confirm_done failed: {_e}")
        elif _msg_stripped in ["取消任务", "/cancel"]:
            try:
                from src.api.routes.tasks import cancel_issue_task
                cancel_issue_task(_task_repo_key, _task_issue_num)
                add_message(conv_id, "assistant", "任务已取消")
                return {"conversation_id": conv_id, "reply": "任务已取消", "task_command": "cancel"}
            except Exception as _e:
                _chat_log.warning(f"Chat command cancel failed: {_e}")
        elif _msg_stripped in ["恢复任务", "/resume"]:
            try:
                from src.api.routes.tasks import resume_issue_task
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
    history = get_messages(conv_id, limit=20) if body.conversation_id else []
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    gitcode_token = _resolve_gitcode_token(conv, conv_id, user_token, repo_key)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    # 任务会话走独立 prompt（注入 Issue 上下文 + 场景: task）；一般会话走通用 prompt
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        prompt = build_task_chat_prompt(body.message, repo_key, history, conv, account=user["username"], runtime_id=cfg.get("runtime_id", 0), has_valid_session=has_valid_session)
    else:
        prompt = build_general_chat_prompt(body.message, repo_key, history, skill_name, skill_id,
                                           backend_name=backend_name, user_info=_build_web_user_info(user, acc),
                                           has_valid_session=has_valid_session)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""

    from src.config import log
    log.info(f"Chat send: backend={backend_name or '(none)'}, skill={skill_name}, has_token={bool(gitcode_token)}, user_msg={body.message[:100]}")

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
    kind = runtime_execution_kind(cfg.get("runtime_id") or 0)
    if kind != EXEC_MODE_DIRECT:
        # M2 D5：入队 + 前端轮询任务状态（打字机效果降级）
        return _enqueue_chat(user, conv_id, body.message, repo_key, skill_name, skill_id, gitcode_token, body.image_data, body.video_data)

    from src.runtime.executor import stream_chat_agent
    loop = asyncio.get_event_loop()
    collected = []
    new_runtime_session_id = ""

    def run_agent():
        nonlocal new_runtime_session_id
        with _CHAT_AGENT_SEMAPHORE:
            for evt_type, data in stream_chat_agent(
                prompt=prompt, cwd=_get_chat_cwd(user["id"], repo_key, conv_id), session_id=runtime_session_id,
                backend_name=backend_name, user_id=user["id"],
                account=user["username"], user_token=gitcode_token, agent_name="Chat Agent",
                agent_label=f"[Agent:chat|user:{user['username']}]",
            ):
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
                    break
        return "".join(collected)

    reply = await loop.run_in_executor(None, run_agent)
    if not reply:
        reply = f"收到您的消息：「{body.message}」\n\n当前使用 {backend_name} 后端处理。"

    add_message(conv_id, "agent", reply)
    touch_conversation(conv_id)
    # web→飞书推送：该对话已被飞书接入时推送给用户（feishu-web-chat-unified-design.md §3.2）
    await _push_reply_to_feishu(conv, user, reply)
    # Save opencode session ID for continuity：恒回写新 session
    # （旧值可能已过期（opencode 静默 EOF），必须用本次成功的新 session 覆盖）
    if new_runtime_session_id:
        update_conversation(conv_id, runtime_session_id=new_runtime_session_id)
    return {"reply": reply, "conversation_id": conv_id}


@router.post("/send/stream")
async def send_message_stream(body: ChatMessage, user: dict = Depends(get_current_user)):
    conv_id = body.conversation_id
    acc = user_db.get_user_account(user["username"]) or {}
    # Issue #52: 用带预刷新的 token 获取（token 临近过期时自动刷新）
    from src.api.deps import get_user_token_with_refresh
    user_token = get_user_token_with_refresh(user["username"], user_id=user["id"])

    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
        if not conv.get("user_gitcode_token") and user_token:
            update_conversation(conv_id, user_gitcode_token=user_token)
            conv = get_conversation(conv_id)
    else:
        conv_id = create_conversation(user["id"], body.repo_key, user_gitcode_token=user_token)
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
    history = get_messages(conv_id, limit=20)
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    gitcode_token = _resolve_gitcode_token(conv, conv_id, user_token, repo_key)
    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    # 任务会话走独立 prompt（注入 Issue 上下文 + 场景: task）；一般会话走通用 prompt
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        prompt = build_task_chat_prompt(body.message, repo_key, history, conv, account=user["username"], runtime_id=cfg.get("runtime_id", 0), has_valid_session=has_valid_session)
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
            rid = resolve_local_engine() or 0
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

        session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name)
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
                    user_gitcode_token=gitcode_token,
                    env_extra={"USER_ID": str(user["id"]), "CONVERSATION_ID": conv_id},
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
            log.info(f"[Agent:chat|user:{user['username']}] agent_worker starting (backend={backend_name})")
            if not _CHAT_AGENT_SEMAPHORE.acquire(blocking=True):
                log.warning(f"[Agent:chat|user:{user['username']}] semaphore NOT acquired (exhausted)")
                return
            collected = []
            worker_error = ""
            try:
                for evt_type, data in stream_chat_agent(
                    prompt=prompt, cwd=_get_chat_cwd(user["id"], repo_key, conv_id), session_id=runtime_session_id,
                    backend_name=backend_name, user_id=user["id"],
                    account=user["username"], user_token=gitcode_token, agent_name="Chat Agent",
                    agent_label=f"[Agent:chat|user:{user['username']}]",
                ):
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
                loop.call_soon_threadsafe(q.put_nowait, None)
                _CHAT_AGENT_SEMAPHORE.release()

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
    import src.user.db as _user_db
    acc = _user_db.get_user_account(user.get("username") or "") or {}

    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    # chat 会话：user_id + repo_key 绑定（chat_agent_session 是 chat 专用；此处用 devops_agent_session 保证 D9 串行）
    session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name)
    # 无引擎 → 明确不可用（engine-selection-design.md v2），不隐式跑 opencode
    if not backend_name:
        return {"reply": "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎", "conversation_id": conv_id}
    # 优先用用户配置的 runtime_id（chat_agent_session），未配置按本机直连兜底（engine-selection-design.md §8.2）
    runtime_id = cfg.get("runtime_id") or 0
    if not runtime_id:
        try:
            from src.runtime.engine_select import resolve_local_engine
            runtime_id = resolve_local_engine() or 0
        except Exception:
            runtime_id = 0
    if not runtime_id:
        return {"reply": "⚠️ 无可用 runtime，请先在 Agent Runtimes 页刷新", "conversation_id": conv_id}
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
    _rsid = session.get("runtime_session_id") or ""
    has_valid_session = bool(_rsid.startswith("ses_"))
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        prompt = build_task_chat_prompt(message, repo_key, history, conv, account=user.get("username") or "", runtime_id=runtime_id, has_valid_session=has_valid_session)
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
        repo_gitcode_token=gitcode_token,
        env_extra={"USER_ID": str(user["id"]), "CONVERSATION_ID": conv_id},
    )
    _log.info(f"Chat enqueued: task={tid} user={user['id']} conv={conv_id} mode={get_execution_mode()}")
    return {"task_id": tid, "reply": "", "conversation_id": conv_id, "queued": True}


# ====== Feedback ======

@router.post("/feedback")
def submit_feedback(body: FeedbackBody, user: dict = Depends(get_current_user)):
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

    from src.user.db import get_bot_token_by_repo
    from src.repo_key import split_owner_repo
    _owner, _repo = split_owner_repo(repo_key)
    bot_token = get_bot_token_by_repo(_owner, _repo) if _owner and _repo else ""

    return {
        "message": "反馈已提交（⚠️ 已废弃：请改用 Event 对话 POST /api/events/{seq}/chat）",
        "deprecated": True,
        "a2a_event": a2a_event,
        "bot_token_available": bool(bot_token),
    }


# ====== Config ======

@router.get("/config")
def get_chat_config(user: dict = Depends(get_current_user), repo: str = ""):
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
        try:
            c.execute("SELECT id, name, version FROM agent_runtimes WHERE credential_id=0 AND installed=1 AND healthy=1 ORDER BY id")
            for r in c.fetchall():
                _rid, ename, ver = r
                if not is_known_engine_name(ename or ""):
                    continue
                backends.append({"name": ename, "display_name": f"{ename} v{ver or ''}（本机直连）", "scope": "local", "owner": "", "runtime_id": _rid})
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

    # 从用户级 chat_agent_session 读取 Chat Agent 凭证状态（每用户独立）
    # 不再查 global_im_bot_config（该表仅作类型注册，不承载用户 IM 配置）
    im_app_id = cfg.get("im_app_id", "") or ""
    im_app_secret = cfg.get("im_app_secret", "") or ""

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

    return {
        "backend": {
            "type": cfg.get("backend", ""),
        },
        "skill": cfg.get("skill", ""),
        "skill_id": cfg.get("skill_id", 0) or 0,
        "skill_scope": skill_scope,
        "_backends": backends,
        "_skills": chat_skills,
        "im": {
            "feishu": {
                "connected": bool(im_app_id),
                "app_id": im_app_id,
                "app_secret_exists": bool(im_app_secret),
            },
            "wecom": {"connected": False, "app_id": ""},
            "dingtalk": {"connected": False, "app_id": ""},
        }
    }


@router.put("/config")
async def update_chat_config(body: dict, user: dict = Depends(get_current_user), repo: str = ""):
    im = body.get("im", {})
    feishu = im.get("feishu", {})
    kwargs = {}
    if feishu.get("app_id"):
        app_id = feishu["app_id"]
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # 强约束：一个 app_id 全局唯一。类型注册表校验 VOD 占用 + 其他用户占用
            c.execute("SELECT bot_type FROM global_im_bot_config WHERE bot_app_id=? AND is_active=1", (app_id,))
            row = c.fetchone()
            if row and row[0] == "vod_bot":
                raise HTTPException(status_code=400, detail={"code": 400, "message": "该凭证已被 VOD Agent 使用，不能同时绑定 Chat Agent 和 VOD Agent"})
            # 其他用户是否已占用（app_id 全局唯一）
            c.execute("SELECT user_id FROM chat_agent_session WHERE im_app_id=? AND user_id>0 AND user_id!=? AND im_app_id!='' LIMIT 1", (app_id, user["id"]))
            other = c.fetchone()
            if other:
                raise HTTPException(status_code=400, detail={"code": 400, "message": "该凭证已被其他用户使用，不能重复绑定"})

            old_cfg = _get_chat_config(user["id"])
            old_app_id = old_cfg.get("im_app_id", "")
            if old_app_id and old_app_id != app_id:
                # 更换 app_id：检查旧 app_id 是否还被其他用户/其他记录引用
                c.execute("SELECT COUNT(*) FROM chat_agent_session WHERE im_app_id=? AND user_id!=? AND im_app_id!=''", (old_app_id, user["id"]))
                other_refs = c.fetchone()[0]
                if other_refs == 0:
                    c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id=? AND bot_type='chat_bot'", (old_app_id,))
                c.execute("DELETE FROM chat_conversations WHERE im_channel_id=?", (old_app_id,))
                log.info(f"Chat IM: replaced old app_id={old_app_id[:30]} with new app_id={app_id[:30]}")

            app_secret = feishu.get("app_secret", "")
            if app_secret and app_secret != "********":
                c.execute(
                    "INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type, bot_name) VALUES (?, ?, '')",
                    (app_id, "chat_bot")
                )
                db.commit()
                kwargs["im_app_id"] = app_id
                kwargs["im_app_secret"] = app_secret
                log.info(f"Chat IM: saved im config for user={user['id']} app_id={app_id[:30]}")
            else:
                kwargs["im_app_id"] = app_id
    else:
        old_cfg = _get_chat_config(user["id"])
        old_app_id = old_cfg.get("im_app_id", "")
        kwargs["im_app_id"] = ""
        kwargs["im_app_secret"] = ""
    if feishu.get("app_secret") and feishu["app_secret"] != "********":
        kwargs["im_app_secret"] = feishu["app_secret"]
    if body.get("backend"):
        kwargs["backend"] = body["backend"]
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

    # 手动配置换 app：对账 unified_user_mapping（feishu-app-scoped-openid-fix.md §2.3）
    # 新旧 im_app_id 不同 → 刷新 app_id 并清空 open_id/feishu_name，要求用户重新 p2p 确认
    _new_app = kwargs.get("im_app_id") or ""
    if _new_app and old_app_id and old_app_id != _new_app:
        try:
            from src.session_manager import update_unified_user_info
            update_unified_user_info(user.get("username", ""), open_id="", app_id=_new_app)
            log.info(f"Chat IM: app changed {old_app_id[:30]} -> {_new_app[:30]}, reconciled unified_user_mapping (open_id cleared)")
        except Exception as e:
            log.warning(f"Chat IM: reconcile unified_user_mapping failed: {e}")

    # Chat IM：通过 BotManager 动态增删 Bot
    try:
        from src.api.main import bot_manager
        if bot_manager and bot_manager.api:
            if feishu.get("app_id") and feishu.get("app_id") == app_id:
                new_secret = kwargs.get("im_app_secret", "")
                if new_secret and new_secret != "********":
                    if old_app_id and old_app_id != app_id:
                        await bot_manager.remove_bot(old_app_id)
                        log.info(f"Chat IM: removed old bot {old_app_id[:30]} from BotManager")
                    if app_id in bot_manager._bots:
                        await bot_manager.remove_bot(app_id)
                    await bot_manager.add_bot(app_id, new_secret, bot_type="chat_bot")
                    log.info(f"Chat IM: added bot {app_id[:30]} to BotManager")
            elif not feishu.get("app_id") and old_app_id:
                await bot_manager.remove_bot(old_app_id)
                log.info(f"Chat IM: removed bot {old_app_id[:30]} from BotManager")
    except Exception as e:
        log.warning(f"Chat BotManager update failed: {e}")

    return {"message": "配置已更新"}


# ===== Chat Agent v2: submit/stream 分离（学习 Admin AI AiTaskManager）=====

@router.post("/submit")
async def chat_submit(body: ChatMessage, user: dict = Depends(get_current_user)):
    """提交 chat 查询 → 立即返回 {task_id, conversation_id, status}。

    学习 Admin AI submit/stream 分离：
    - submit 立即返回 task_id，后台 ChatTaskManager 执行 agent
    - 前端用 GET /task/{task_id}/stream 订阅 SSE（可断开重连，缓冲重放）
    - 页面切换/会话切换不中断后端执行
    """
    from src.api.chat_task_manager import ChatTaskManager

    conv_id = body.conversation_id
    acc = user_db.get_user_account(user["username"]) or {}
    # Issue #52: 用带预刷新的 token 获取
    from src.api.deps import get_user_token_with_refresh
    user_token = get_user_token_with_refresh(user["username"], user_id=user["id"])

    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
        if not conv.get("user_gitcode_token") and user_token:
            update_conversation(conv_id, user_gitcode_token=user_token)
            conv = get_conversation(conv_id)
    else:
        conv_id = create_conversation(user["id"], body.repo_key, user_gitcode_token=user_token)
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
    history = get_messages(conv_id, limit=20)
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    gitcode_token = _resolve_gitcode_token(conv, conv_id, user_token, repo_key)

    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        prompt = build_task_chat_prompt(body.message, repo_key, history, conv,
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
            rid = resolve_local_engine() or 0
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
            _final_text = ""
            _new_sid = ""
            _last_error = ""
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
                ):
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
                        # session 重试后仍无文本 → 用 error 信息作为回复
                        if not _final_text and _last_error:
                            _final_text = _last_error
                        yield ("done", {"text": _final_text})
                    elif evt_type == "error":
                        # 不 break — 让 stream_agent 继续，使 session 失效重试逻辑
                        # (_stale_fail 检测 + _run_once("") 重跑) 有机会执行。
                        # 记住 error，仅在 done 无有效文本时才作为最终结果。
                        _last_error = str(data)
                        yield ("error", str(data))
                    else:
                        yield (evt_type, data)
            finally:
                # DB 写入（finally 保证，连接断开也不丢）
                if _final_text:
                    try:
                        add_message(conv_id, "agent", _final_text,
                                   task_id=chat_task_id, generate_status=2)
                        touch_conversation(conv_id)
                        log.info(f"[Chat submit] DB saved task_id={chat_task_id} len={len(_final_text)}")
                    except Exception as _de:
                        log.warning(f"[Chat submit] DB save failed: {_de}")
                else:
                    try:
                        add_message(conv_id, "agent", "（无返回内容）",
                                   task_id=chat_task_id, generate_status=3)
                        touch_conversation(conv_id)
                    except Exception as _de:
                        log.warning(f"[Chat submit] DB save (empty) failed: {_de}")
                # 飞书推送（投递到主事件循环；后台线程不能跨 loop 复用共享 aiohttp session）
                if _final_text:
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
        session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name)
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
            repo_gitcode_token=gitcode_token,
            env_extra={"USER_ID": str(user["id"]), "CONVERSATION_ID": conv_id},
        )
        log.info(f"Chat submit enqueued: enqueue_tid={enqueue_tid} chat_task_id={chat_task_id}")

        # 远端轮询 agent_fn
        def remote_agent_fn():
            """轮询远端 task_messages → yield 事件"""
            import time as _t
            from src.runtime.service import get_task
            _final_text = ""
            last_seq = 0
            while True:
                _t.sleep(0.5)
                # 检查任务终态
                tk = get_task(enqueue_tid)
                if tk and tk.get("status") in ("completed", "failed", "cancelled"):
                    _final_text = tk.get("result_text") or ""
                    if not _final_text and tk.get("status") == "completed":
                        _final_text = "Done"
                    elif tk.get("status") == "failed":
                        _final_text = f"查询失败: {tk.get('result_text', '')}"
                    break
            # DB 写入
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

    return {"task_id": chat_task_id, "conversation_id": conv_id, "status": "running"}


@router.get("/task/{task_id}/stream")
async def chat_task_stream(task_id: str, user: dict = Depends(get_current_user)):
    """SSE 订阅任务输出 — 先重放缓冲，再实时追加（学习 Admin AI subscribe）。

    - 内存有任务 → 重放 events 缓冲 + 加入 subscribers 接收后续
    - 内存无任务 → 查 DB 获取最终结果（已完成/跨机器）
    """
    from src.api.chat_task_manager import ChatTaskManager

    loop = asyncio.get_running_loop()
    q, task = ChatTaskManager.subscribe(task_id, loop)

    # 内存无任务 → 可能是跨机器 submit，DB 有 user 消息但 agent 还在执行
    # 轮询 DB 等待 agent 回复（最多 120s），而不是立刻报「查询已中断」
    if not q:
        from src.session_manager import db as _msg_db, db_lock as _msg_lock

        async def _poll_db_stream():
            import time as _t
            conv_id = ""
            # 先发思考中占位
            yield f"data: {json.dumps({'type': '思考中', 'text': ''})}\n\n"
            deadline = _t.time() + 120
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
                await asyncio.sleep(1)
            # 超时
            yield f"data: {json.dumps({'type': 'error', 'text': '查询超时（120s），请重试', 'conversation_id': conv_id, 'task_id': task_id})}\n\n"

        return StreamingResponse(
            _poll_db_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    # 内存有任务 → SSE 流
    async def event_stream():
        # 先发一个思考中占位（如果缓冲为空）
        if not task.events:
            yield f"data: {json.dumps({'type': '思考中', 'text': ''})}\n\n"
        # 消费 queue（重放缓冲 + 实时事件）
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=120)
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'error', 'text': '超时（120s 无输出）'})}\n\n"
                ChatTaskManager.unsubscribe(task_id, q)
                return
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


@router.post("/send/async")
async def send_message_async(body: ChatMessage, user: dict = Depends(get_current_user)):
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
    acc = user_db.get_user_account(user["username"]) or {}
    # Issue #52: 用带预刷新的 token 获取
    from src.api.deps import get_user_token_with_refresh
    user_token = get_user_token_with_refresh(user["username"], user_id=user["id"])

    if conv_id:
        conv = get_conversation(conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "对话不存在"})
        if not conv.get("user_gitcode_token") and user_token:
            update_conversation(conv_id, user_gitcode_token=user_token)
            conv = get_conversation(conv_id)
    else:
        conv_id = create_conversation(user["id"], body.repo_key, user_gitcode_token=user_token)
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
    history = get_messages(conv_id, limit=20)
    repo_key = conv.get("repo_key", "") if conv else body.repo_key
    gitcode_token = _resolve_gitcode_token(conv, conv_id, user_token, repo_key)

    runtime_session_id = conv.get("runtime_session_id", "") if conv else ""
    if runtime_session_id and not runtime_session_id.startswith("ses_"):
        runtime_session_id = ""
    has_valid_session = bool(runtime_session_id.startswith("ses_"))
    if conv and conv.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        prompt = build_task_chat_prompt(body.message, repo_key, history, conv, account=user["username"], runtime_id=cfg.get("runtime_id", 0), has_valid_session=has_valid_session)
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
            rid = resolve_local_engine() or 0
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
        session = get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name)
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
            repo_gitcode_token=gitcode_token,
            env_extra={"USER_ID": str(user["id"]), "CONVERSATION_ID": conv_id},
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
                final_text = f"查询失败: {tk.get('result_text', '')}"
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
        gitcode_token = task.get("gitcode_token", "")
        skill_name = task.get("skill_name", "chat")
        skill_id = task.get("skill_id", 0)
        repo_key = task.get("repo_key", "")
        runtime_session_id = task.get("runtime_session_id", "")
        cwd = task.get("cwd", "")

        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
        kind = runtime_execution_kind(rid)

        if kind == EXEC_MODE_DIRECT:
            # 本机直连：stream_agent（同步 generator，不需要 event loop）
            from src.runtime.executor import stream_agent
            final_text = ""
            try:
                for evt_type, data in stream_agent(
                    rid, prompt, cwd=cwd,
                    skill=skill_name, skill_id=skill_id,
                    runtime_session_id=runtime_session_id,
                    repo_gitcode_token=gitcode_token,
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
                        if data:
                            final_text = data if isinstance(data, str) else str(data)
                        break
                    elif evt_type == "error":
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
            session = get_or_create_agent_session(f"chat:{user.get('id', 0)}", "chat", backend_name)
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
                repo_gitcode_token=gitcode_token,
                env_extra={"USER_ID": str(user.get("id", 0)), "CONVERSATION_ID": conv_id},
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
                        final_text = f"查询失败: {tk.get('result_text', '')}"
                    break
                _t.sleep(1)

        # 写入结果
        if not final_text:
            final_text = f"收到您的消息：「{task.get('body_message', '')}」\n\n当前使用 {backend_name} 后端处理。"
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


@router.get("/task/{task_id}/status")
def chat_task_status(task_id: str, user: dict = Depends(get_current_user)):
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


