"""LLM 凭据两级查询公共函数——/env 端点和 repository 共用。

从 runtime_info.engine_type 提取引擎类型后，两级查询:
  ① 引擎专属: WHERE user_id=? AND engine_type=<引擎> AND enabled=1
  ② 通用兜底: WHERE user_id=? AND engine_type='common' AND enabled=1
"""
from src.kernel.crypto import decrypt


def select_llm_cred(cursor, user_id: int, engine_type: str = "",
                    mode: str = "user", repo_key: str = "") -> tuple | None:
    """两级查询 LLM 凭据。[repo-llm-credential] mode 路由（用户裁定 2026-10-30）：

    Args:
        cursor: DB cursor（调用方负责 db_lock）
        user_id: 用户 ID（mode='user' 用）
        engine_type: 引擎类型(hermes/opencode)，空串走通用兜底
        mode: 'user'=用户凭据（grape_t_user_llm_credentials，原行为）|
              'repo'=仓库凭据（grape_t_repo_llm_credentials，仓库资产）
        repo_key: mode='repo' 时的仓库键（如 gitcode:owner/repo）

    Returns:
        (provider, api_key_plain, base_url, model) 或 None
        注意: engine_type='common' 时第一段等价于第二段，不影响结果。
        **跨 mode 零回退**（用户裁定：仓库取不到不得降级用户级，反之亦然）。
    """
    # [repo-llm-credential] mode='repo'：查 grape_t_repo_llm_credentials（表内两级同 user 版）
    if mode == "repo":
        if not repo_key:
            return None
        # 表归 mgmt：SQL 在 mgmt public 一份
        from src.domains.mgmt import public as _mg_llm
        if engine_type:
            row = _mg_llm.repo_llm_credential_row(repo_key, engine_type) or None
            if row:
                return (row[0] or "", decrypt(row[1]) if row[1] else "", row[2] or "", row[3] or "")
        # repo 版通用兜底（common）
        row = _mg_llm.repo_llm_credential_row(repo_key, "common") or None
        if row:
            return (row[0] or "", decrypt(row[1]) if row[1] else "", row[2] or "", row[3] or "")
        return None

    # ① 引擎专属（mode='user'，原行为）
    from src.domains.mgmt import public as _mg_ull
    if engine_type:
        row = _mg_ull.user_llm_credential_row(user_id, engine_type) or None
        if row:
            return (row[0] or "", decrypt(row[1]) if row[1] else "", row[2] or "", row[3] or "")

    # ② 通用兜底
    row = _mg_ull.user_llm_credential_row(user_id, "common") or None
    if row:
        return (row[0] or "", decrypt(row[1]) if row[1] else "", row[2] or "", row[3] or "")

    # [沙箱迁移] ③ 系统级兜底：用户无凭据（user_id=0 的 A2A/事件任务，或用户
    # 未配置 LLM 凭据）时查任意启用的 common 凭据（系统凭据语义）。
    # 本地引擎（opencode/hermes 直跑）直接用服务器侧凭据不查此表——云沙箱
    # 必须拿到 LLM 配置否则引擎装了没模型可用（event#29 实锤：用户 1 无凭据
    # → "No LLM provider configured"）。对齐本地引擎能力，两级查不到即兜底。
    row = _mg_ull.user_llm_credential_row(user_id, "common", any_user=True) or None
    if row:
        return (row[0] or "", decrypt(row[1]) if row[1] else "", row[2] or "", row[3] or "")

    return None
