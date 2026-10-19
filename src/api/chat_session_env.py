"""Chat 会话凭据注入公共 helper（chat-direct / feishu-direct-session-token-injection 共用）。

任务会话（绑定真实仓库）的 Chat Agent 在 direct（本机直连）执行时，与 queue 语义对齐，
签发 tk_ 会话 token 注入 GRAPE_SESSION_TOKEN，使 agent 能调用仓库作用域内部端点
（如 git-repo issue comment 回写澄清结论）；并按 tk_ 作用域过滤 CLI 命令（GRAPE_CLI_MODE=WORKER）。

Web（src/api/routes/chat.py）与飞书 IM（src/im/handler_async.py）共用本 helper，防两组逻辑漂移。
仅真实仓库注入；一般对话（repo_key 为空 / `chat:{id}` 伪仓库）不注入，行为不变。
"""
from src.config import log


def chat_session_env_extra(user_id: int, conv_id: str, repo_key: str) -> dict:
    """任务会话（真实仓库）chat 会话凭据注入。

    返回 env dict：恒含 USER_ID / CONVERSATION_ID；repo_key 为真实仓库时附加
    GRAPE_SESSION_TOKEN（tk_，session_key=`conv:{conv_id}`）与 GRAPE_CLI_MODE=WORKER。
    issue_session_token 抛异常（DB 不可用等）时仅缺 token 字段、不抛，chat 仍可正常回复。
    """
    env = {"USER_ID": str(user_id), "CONVERSATION_ID": conv_id or ""}
    if repo_key and not repo_key.startswith("chat:"):
        try:
            from src.api.auth_token import issue_session_token
            env["GRAPE_SESSION_TOKEN"] = issue_session_token(
                session_key=f"conv:{conv_id}",
                repo_key=repo_key, agent_type="chat", user_id=user_id)
            env["GRAPE_CLI_MODE"] = "WORKER"
        except Exception as e:
            log.warning(f"chat_session_env_extra: issue session token failed for conv={conv_id}: {e}")
    return env