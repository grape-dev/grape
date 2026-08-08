"""首次托管引导（Onboarding）触发与后台执行。

仅在「模板一键创建并托管」的项目上异步触发一次：
- 读取仓库开发场景（managed_repos.dev_scene）→ 按场景查 agent-skill-mapping.json 的 onboarding 技能
- 后台以 issue agent 执行引导技能：分析项目 → 建 2 个 Issue（快速部署体验 + 探索方向引导）→ 分派 owner → 建任务
- 幂等：meta 表 `onboarded:{repo_key}` 标记，仅首次执行；换场景不重跑
"""
import asyncio
import os
import json

from src.config import log, WORKSPACE_DIR, SKILLS_DIR, CONFIG_DIR


def _scene_onboarding_skill(scene_id: str) -> str:
    """读 agent-skill-mapping.json，返回场景的 onboarding 技能 key；无则空串。"""
    try:
        _path = os.path.join(CONFIG_DIR, "agent-skill-mapping.json")
        with open(_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("scenes", {}).get(scene_id, {}).get("onboarding", "") or ""
    except Exception as e:
        log.warning(f"onboarding: load scene skill failed: {e}")
        return ""


def _is_onboarded(repo_key: str) -> bool:
    from src.user.db import get_meta_value
    return get_meta_value(f"onboarded:{repo_key}") == "1"


def _mark_onboarding_started(repo_key: str):
    from src.user.db import set_meta_value
    set_meta_value(f"onboarded:{repo_key}", "1")


def _build_onboarding_prompt(repo_key: str, owner: str, skill: str) -> str:
    """构建引导 prompt：注入 repo-onboarding SKILL.md + 仓库上下文 + owner 信息。"""
    from src.repo_key import from_repo_key
    parts = []
    parts.append(f"# 项目首次托管引导\n仓库: {from_repo_key(repo_key)}\n仓库 owner: {owner}")
    skill_md = os.path.join(SKILLS_DIR, skill, "SKILL.md")
    parts.append(
        f"Skill 文件：{skill_md}\n"
        f"**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令（不得跳过读取或凭名称猜测）。"
    )
    return "\n\n".join(parts)


async def _run_onboarding(repo_key: str, owner: str, skill: str):
    """后台执行引导：opencode 跑 repo-onboarding 技能。"""
    from src.repo_key import from_repo_key, parse_repo_key
    try:
        repository = from_repo_key(repo_key)
        _provider, _owner, _repo = parse_repo_key(repo_key)
        ws = os.path.join(WORKSPACE_DIR, repository, "onboarding-agent")
        os.makedirs(ws, exist_ok=True)

        from src.user.db import get_gitcode_token
        bot_token = get_gitcode_token(repo_key=repo_key) or ""

        prompt = _build_onboarding_prompt(repo_key, owner, skill)
        log.info(f"[Onboarding|repo:{repo_key}] starting agent, skill={skill}, ws={ws}")

        from src.runtime.executor import consume_stream_async
        final_text = ""
        tool_uses = []
        async for evt_type, data in consume_stream_async(
            prompt, ws, session_id="", event_type="onboarding",
            backend_name="opencode", account=repository, bot_token=bot_token,
            agent_name="Onboarding Agent",
            agent_label=f"[Agent:onboarding|repo:{repo_key}]",
            env_extra={"REPO_FULL_NAME": repository, "REPO_OWNER": owner},
        ):
            # 写库兜底脱敏：agent 可能在 text/result 复述 token
            _msg = str(data)[:500]
            if bot_token:
                _msg = _msg.replace(bot_token, "[REDACTED]")
            if evt_type == "done" and isinstance(data, dict):
                final_text = data.get("text", "") or ""
                log.info(f"[Onboarding|repo:{repo_key}] done, exit={data.get('exit_code')}, text_len={len(final_text)}")
            elif evt_type == "error":
                log.warning(f"[Onboarding|repo:{repo_key}] agent error: {_msg}")
            elif evt_type == "step_start":
                log.info(f"[agent onboarding|repo:{repo_key}] {_msg}")
            elif evt_type == "tool_use":
                tool_uses.append(data if isinstance(data, dict) else {})
                log.info(f"[agent onboarding|repo:{repo_key}] TOOL: {_msg[:160]}")
            elif evt_type == "text":
                log.info(f"[agent onboarding|repo:{repo_key}] TEXT: {_msg[:160]}")
            elif evt_type == "reasoning":
                log.info(f"[agent onboarding|repo:{repo_key}] REASONING: {_msg[:160]}")
            else:
                log.info(f"[agent onboarding|repo:{repo_key}] {evt_type}: {_msg[:160]}")
        log.info(f"[Onboarding|repo:{repo_key}] finished, text_len={len(final_text)}, tool_uses={len(tool_uses)}")
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] failed: {e}")


def trigger_onboarding(repo_key: str, owner: str):
    """模板一键创建完成后调用：仅首次触发引导。

    仅在 templates.create_from_template 中调用（手动托管 /repos/host 不调用）。
    场景从 managed_repos.dev_scene 读取（不依赖请求参数）。
    """
    try:
        from src.user.db import get_repo_scene
        scene_id = get_repo_scene(repo_key) or "default"
        skill = _scene_onboarding_skill(scene_id)
        if not skill:
            log.info(f"[Onboarding|repo:{repo_key}] scene='{scene_id}' 无 onboarding 技能，跳过")
            return
        if _is_onboarded(repo_key):
            log.info(f"[Onboarding|repo:{repo_key}] 已引导过，跳过")
            return
        _mark_onboarding_started(repo_key)  # 防并发重复
        asyncio.create_task(_run_onboarding(repo_key, owner, skill))
        log.info(f"[Onboarding|repo:{repo_key}] 触发引导，scene='{scene_id}', skill='{skill}'")
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] trigger failed: {e}")
