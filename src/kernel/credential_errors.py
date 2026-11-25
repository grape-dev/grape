"""Issue #3427：凭据读侧错误码分层。

三种失败情形 + 权限类 403 各自带稳定 error_code 与可行动文案，
供凭据解析器（domains/mgmt/auth/credential_resolver.resolve_credential_detailed）与
repos host / POST /api/issues / a2a 三入口共用。

设计约束（Spec「关键设计决策」）：
- 解密失败（密钥变更/密文损坏）不与「未配置」合并——排障需要区分；
- 上游 401 在解析成功后立即探活（GET /user）暴露，避免误报「未配置」；
- a2a 403 与 token 类错误解耦，指向 user_repos 未关联。
"""
from __future__ import annotations

from enum import Enum


class CredentialErrorCode(str, Enum):
    """凭据读侧稳定错误码（API detail.error_code 值域）。"""

    TOKEN_MISSING = "TOKEN_MISSING"                    # 全链未配置任何凭据 → 400
    TOKEN_DECRYPT_FAILED = "TOKEN_DECRYPT_FAILED"      # 密文解密失败 → 400
    TOKEN_REJECTED_UPSTREAM = "TOKEN_REJECTED_UPSTREAM"  # 上游 401（失效/吊销）→ 401
    REPO_NOT_BOUND_TO_USER = "REPO_NOT_BOUND_TO_USER"  # user_repos 未关联 → 403
    TOKEN_VERIFY_UNAVAILABLE = "TOKEN_VERIFY_UNAVAILABLE"  # 校验通道异常（provider 构造失败/网络）→ 502


# 各错误码的可行动文案（用户可直接照做）
CREDENTIAL_ERROR_MESSAGES: dict[str, str] = {
    CredentialErrorCode.TOKEN_MISSING.value: (
        "未检测到任何可用凭据（仓库级 Bot Token / 全局 Bot Token / 用户绑定均未配置）："
        "请调用 PUT /api/repos/{owner}/{repo}/bot-token 绑定仓库级 Bot Token，"
        "或在个人设置绑定平台账号 / PAT 后重试"),
    CredentialErrorCode.TOKEN_DECRYPT_FAILED.value: (
        "凭据密文解密失败（加密密钥变更或密文损坏，存量失效密文）："
        "请调用 PUT /api/repos/{owner}/{repo}/bot-token 重新绑定仓库级 Bot Token，"
        "或重新走 OAuth 绑定流程刷新用户凭据"),
    CredentialErrorCode.TOKEN_REJECTED_UPSTREAM.value: (
        "凭据已被上游平台拒绝（401，token 已失效或被吊销）："
        "请在平台重新生成 PAT，并调用 PUT /api/repos/{owner}/{repo}/bot-token 重新绑定"),
    CredentialErrorCode.REPO_NOT_BOUND_TO_USER.value: (
        "当前用户未关联该仓库（user_repos 无关联记录，与凭据无关）："
        "请先同步/加入该仓库，或由托管人在仓库成员中添加后再发起 A2A"),
    CredentialErrorCode.TOKEN_VERIFY_UNAVAILABLE.value: (
        "Bot Token 校验通道暂时不可用（provider 构造失败或网络异常）：请稍后重试"),
}

# 错误码 → HTTP 状态码（Spec：401/403 分层）
CREDENTIAL_ERROR_HTTP_STATUS: dict[str, int] = {
    CredentialErrorCode.TOKEN_MISSING.value: 400,
    CredentialErrorCode.TOKEN_DECRYPT_FAILED.value: 400,
    CredentialErrorCode.TOKEN_REJECTED_UPSTREAM.value: 401,
    CredentialErrorCode.REPO_NOT_BOUND_TO_USER.value: 403,
    CredentialErrorCode.TOKEN_VERIFY_UNAVAILABLE.value: 502,
}


def credential_error_detail(error_code: str, provider_name: str = "") -> dict:
    """构造 HTTPException.detail dict：code + error_code + 可行动 message。

    provider_name 仅用于拼接上下文前缀；不泄露任何 token/密文内容。
    """
    message = CREDENTIAL_ERROR_MESSAGES.get(error_code, f"凭据读取失败（{error_code}）")
    prefix = f"{provider_name}: " if provider_name else ""
    return {
        "code": CREDENTIAL_ERROR_HTTP_STATUS.get(error_code, 400),
        "error_code": error_code,
        "message": f"{prefix}{message}",
    }


def credential_error_code_for(resolution) -> str:
    """凭据解析结果 → 失败原因码的单一出口（收敛 `(cred.error_code if cred
    else "") or "TOKEN_MISSING"` 重复 switch）；resolution 为 None/空值时按
    TOKEN_MISSING 处理。"""
    return getattr(resolution, "error_code", "") or CredentialErrorCode.TOKEN_MISSING.value


def credential_error_detail_for(resolution, provider_name: str = "") -> dict:
    """凭据解析结果 → HTTPException.detail 的单一出口。

    收敛 host / 建单等入口的重复判断；resolution 为 None/空值时按
    TOKEN_MISSING 处理。
    """
    return credential_error_detail(credential_error_code_for(resolution), provider_name)


__all__ = [
    "CredentialErrorCode",
    "CREDENTIAL_ERROR_MESSAGES",
    "CREDENTIAL_ERROR_HTTP_STATUS",
    "credential_error_detail",
    "credential_error_code_for",
    "credential_error_detail_for",
]
