"""沙箱上下文构建——从 task dict 提取，不查 DB。

基线 claim API 已返回所有需要的字段（JOIN agent_runtimes）：
  - id/issue_id/repo_key/user_id/runtime_id/agent_type → agent_tasks 表列（agent_type 是业务角色 vod/issue/coding）
  - engine_type → runtime_info JSON 的 engine_type 字段（引擎类型 hermes/opencode）
  - sandbox_provider → agent_runtimes.provider（JOIN）
  - runtime_info → agent_runtimes.runtime_info JSON（JOIN）
    bwrap 引擎：provider 空，runtime_info={} → fallback bwrap
    云沙箱引擎：provider 有值，runtime_info 含 template_id → 选云 provider
"""
from __future__ import annotations
from dataclasses import dataclass
import json as _json
import logging

log = logging.getLogger("devops-bot")


@dataclass
class SandboxContext:
    """沙箱上下文——纯标识数据，从 task dict 构建。

    state_reporter / llm_env 由 prepare() 独立参数注入（策略=函数参数）。
    一个任务一个 ctx，一个 user_id——消除双源真相。

    skill_name / skill_id 只在单 skill 场景有意义。
    多 skill 场景的审计归因见 design-skill-egress-domains.md §多 skill 审计归因限制——
    当前记 unknown。
    """
    task_id: str = ""
    issue_id: str = ""
    repo_key: str = ""
    user_id: int = 0
    runtime_id: int = 0
    engine_type: str = ""
    sandbox_provider: str = ""
    template_id: str = ""
    provider_id: "int | str" = 0
    platform: str = ""
    skill_name: str = ""
    skill_id: int = 0
    repo_url: str = ""        # 仓库 clone URL（从 env_extra 提取）
    bot_token: str = ""       # git clone 凭据（BOT_<PROVIDER>_TOKEN）
    clone_username: str = ""  # clone 用户名（basic_password 平台按令牌解析出的账号 login）
    work_branch: str = ""     # 工作分支（GRAPE_WORK_BRANCH）


def build_context(task: dict) -> SandboxContext:
    """从 claim API 返回的 task dict 构建沙箱上下文。纯数据提取，不查 DB。"""
    _ri = {}
    _raw_ri = task.get("runtime_info") or "{}"
    if isinstance(_raw_ri, dict):
        _ri = _raw_ri
    elif isinstance(_raw_ri, str):
        try:
            _parsed = _json.loads(_raw_ri)
            if isinstance(_parsed, dict):
                _ri = _parsed
            else:
                log.warning(f"build_context: runtime_info parsed to {type(_parsed).__name__}, expected dict")
        except Exception as e:
            log.warning(f"build_context: runtime_info parse failed: {e}, raw={_raw_ri[:100]}")
    else:
        log.warning(f"build_context: runtime_info unexpected type {type(_raw_ri).__name__}")

    # 从 env_extra 提取仓库信息（内聚到 ctx，_init_sandbox 第 7 步直接用）
    _env_extra = {}
    _raw_env = task.get("env_extra") or "{}"
    if isinstance(_raw_env, str):
        try:
            # #3649：env_extra 是 enc:v1 信封（#2423），必须解密；直接 json.loads
            # 只会静默得到空 env，REPO_FULL_NAME/BOT token/工作分支全部丢失。
            from src.domains.runtime.engine.env_extra_security import decrypt_env_extra
            _env_extra = decrypt_env_extra(_raw_env)
        except Exception as _e:
            log.warning(f"build_context: env_extra decrypt failed: {_e}")
    elif isinstance(_raw_env, dict):
        _env_extra = _raw_env

    # 平台必须来自带前缀的 key：REPO_FULL_NAME 是裸全名（runbook 用），
    # 单独用它会让 parse_repo_key 默认成 gitcode（gitee 仓被 clone 到 gitcode.com）。
    repo_full_name = (_env_extra.get("REPO_KEY") or task.get("repo_key")
                      or _env_extra.get("REPO_FULL_NAME") or "")
    try:
        from src.domains.runtime.vcs.clone_endpoint import bot_token_from_env as _bot_from_env

        bot_token = (_bot_from_env(_env_extra, repo_full_name)
                     or task.get("repo_gitcode_token") or "")
    except Exception:
        bot_token = _env_extra.get("BOT_GITCODE_TOKEN") or task.get("repo_gitcode_token") or ""
    work_branch = _env_extra.get("GRAPE_WORK_BRANCH", "")
    # clone 地址单点派生（平台卡 host）。过去这里写死 gitcode.com，gitee/github/gitlab
    # 仓会被当成 gitcode 仓去 clone；派生失败留空，由调用方按"无仓库"处理。
    repo_url = ""
    clone_username = ""
    if repo_full_name:
        try:
            from src.domains.runtime.vcs.clone_endpoint import clone_url as _clone_url

            repo_url = _clone_url(repo_full_name)
        except Exception as e:
            log.warning(f"build_context: clone endpoint resolve failed ({repo_full_name}): {e}")
        # 用户名：basic_password 平台（gitee）必须是令牌所属账号 login。
        # 过去 ctx 只有 repo_url/bot_token，云沙箱侧只能回退 "bot" 这种占位名。
        try:
            from src.domains.runtime.vcs.clone_identity import username_for_token as _user_for_token

            clone_username, _cu_src = _user_for_token(
                (repo_full_name.split(":", 1)[0] if ":" in repo_full_name else "gitcode"),
                bot_token, repo_full_name)
            log.debug(f"build_context: clone username source={_cu_src}")
        except Exception as e:
            log.warning(f"build_context: clone username resolve failed ({repo_full_name}): {e}")

    return SandboxContext(
        task_id=task.get("id") or "",
        issue_id=task.get("issue_id") or "",
        repo_key=task.get("repo_key") or "",
        user_id=task.get("user_id") or 0,
        runtime_id=task.get("runtime_id") or 0,
        # engine_type：引擎类型(hermes/opencode)，从 runtime_info 读取。
        # 不 fallback 到业务角色(vod/issue/coding)——那是 agent_tasks.agent_type，不是引擎类型。
        # 为空时: LLM 凭据查询落通用兜底('common')；引擎安装走 backend_for_engine() 推断。
        engine_type=_ri.get("engine_type") or "",
        sandbox_provider=task.get("sandbox_provider") or "",
        template_id=_ri.get("template_id") or "",
        provider_id=_ri.get("provider_id") or 0,
        platform=_ri.get("platform") or "",
        skill_name=task.get("skill") or "",
        skill_id=task.get("skill_id") or 0,
        repo_url=repo_url,
        bot_token=bot_token,
        clone_username=clone_username,
        work_branch=work_branch,
    )
