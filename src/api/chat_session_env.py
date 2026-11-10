"""Chat 会话凭据注入公共 helper（chat-direct / feishu-direct-session-token-injection 共用）。

任务会话（绑定真实仓库）的 Chat Agent 在 direct（本机直连）执行时，与 queue 语义对齐，
签发 tk_ 会话 token 注入 GRAPE_SESSION_TOKEN，使 agent 能调用仓库作用域内部端点
（如 git-repo issue comment 回写澄清结论）；并按 tk_ 作用域过滤 CLI 命令（GRAPE_CLI_MODE=WORKER）。

Web（src/api/routes/chat.py）与飞书 IM（src/im/handler_async.py）共用本 helper，防两组逻辑漂移。
仅真实仓库注入；一般对话（repo_key 为空 / `chat:{id}` 伪仓库）不注入，行为不变。
"""
from src.config import log


def chat_repo_env(repo_key: str) -> dict:
    """真实仓库 chat → REPO_FULL_NAME/REPO_OWNER/REPO_NAME（由 REPO_FULL_NAME 统一派生）。

    Issue #1660：chat 注入路径此前缺失仓库身份变量；此处与事件/重派路径同源派生，
    保证 `$REPO_OWNER/$REPO_NAME` 始终等于 REPO_FULL_NAME 拆分结果。伪仓库（`chat:`）/空 → {}。
    """
    rk = (repo_key or "").strip()
    if not rk or rk.startswith("chat:"):
        return {}
    from src.repo_key import derive_repo_identity, from_repo_key
    full_name, owner, name = derive_repo_identity(from_repo_key(rk), rk)
    if not full_name:
        return {}
    return {"REPO_FULL_NAME": full_name, "REPO_OWNER": owner, "REPO_NAME": name}


def _resolve_issue_number(issue_number, conv_id: str) -> str:
    """解析任务会话 Issue 编号（仅 conv_type == "task" 且为纯数字时返回，否则空串）。

    issue_number 入参为空时按 conv_id 兜底查会话；#528 口径：非数字/占位值不注入。
    """
    value = issue_number
    if not value and conv_id:
        try:
            from src.session_manager import get_conversation
            conv = get_conversation(conv_id)
            if conv and conv.get("conv_type") == "task":
                value = conv.get("issue_number")
        except Exception as e:
            log.warning(f"chat_session_env_extra: resolve issue number failed for conv={conv_id}: {e}")
            value = ""
    text = str(value or "").lstrip("#").strip()
    return text if text.isdigit() else ""


def chat_session_env_extra(user_id: int, conv_id: str, repo_key: str, *,
                           runtime_id: int = 0, issue_number="") -> dict:
    """任务会话（真实仓库）chat 会话凭据注入。

    返回 env dict：恒含 USER_ID / CONVERSATION_ID / GRAPE_SERVER_URL；repo_key 为真实仓库时
    附加 REPO_FULL_NAME / REPO_OWNER / REPO_NAME、GRAPE_SESSION_TOKEN（tk_，
    session_key=`conv:{conv_id}`）与 GRAPE_CLI_MODE=WORKER；任务会话且 Issue 编号为纯数字时
    附加 ISSUE_NUMBER。伪仓库/空仓库不注入 REPO_* / ISSUE_NUMBER（键不出现）。
    各外部解析抛异常时仅缺对应字段、不抛，chat 仍可正常回复。
    """
    env = {"USER_ID": str(user_id), "CONVERSATION_ID": conv_id or ""}

    # 恒注入业务 API 基址（Issue #1577：GRAPE_SERVER_URL 承载业务端点，非 runtime 回连地址）；
    # 解析失败仅缺该键，不中断聊天。
    try:
        from src.prompt_builder import _resolve_business_server_url
        env["GRAPE_SERVER_URL"] = _resolve_business_server_url(runtime_id or 0)
    except Exception as e:
        log.warning(f"chat_session_env_extra: resolve server url failed for conv={conv_id}: {e}")

    env.update(chat_repo_env(repo_key))
    if repo_key and not repo_key.startswith("chat:"):
        try:
            from src.api.auth_token import issue_session_token
            env["GRAPE_SESSION_TOKEN"] = issue_session_token(
                session_key=f"conv:{conv_id}",
                repo_key=repo_key, agent_type="chat", user_id=user_id)
            env["GRAPE_CLI_MODE"] = "WORKER"
        except Exception as e:
            log.warning(f"chat_session_env_extra: issue session token failed for conv={conv_id}: {e}")
        _issue = _resolve_issue_number(issue_number, conv_id)
        if _issue:
            env["ISSUE_NUMBER"] = _issue
    return env
