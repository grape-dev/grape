"""首次托管引导（Onboarding）触发与后台执行。

仅在「模板一键创建并托管」的项目上异步触发一次：
- 读取仓库开发场景（managed_repos.dev_scene）→ 按场景查 agent-skill-mapping.json 的 onboarding 技能
- 由 VOD Agent 执行引导技能（agent_type=vod，复用 VOD 会话引擎配置，direct/queue 分派）
- experience / explore 各执行一次，各创建一个对应类型的引导 Issue
- 落库：两个主事件（onboarding_exp / onboarding_explore，issue_id=#0）+ 每个 Issue 独立事件（onboarding_issue）
- issue_state：新 Issue 处于 vod 阶段（stage=vod, owner=vod），由 VOD 先收敛需求（阶段矩阵 D8）
- 幂等：meta 表 `onboarded:{repo_key}` 标记，仅首次执行；换场景不重跑
"""
import asyncio
import os
import json
import threading

from src.config import log, SKILLS_DIR, CONFIG_DIR

# 引导目标类型：experience（快速部署体验）/ explore（探索方向引导），各执行一次
ONBOARDING_TARGETS = ("experience", "explore")


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


def clear_onboarding_mark(repo_key: str):
    """清除 onboarding 幂等标记（带/不带 gitcode: 前缀两种 key）。

    用于模板一键创建新仓库场景：仓库是全新创建的（GitCode 上此前不存在），
    残留的 onboarded 标记必然来自被删除的旧仓库（如用户直接在 GitCode 网页
    删除，未走解除托管流程，标记残留）——作废它，使新仓库重新触发引导。
    """
    try:
        from src.session_manager import db, db_lock
        keys = [f"onboarded:{repo_key}"]
        if repo_key.startswith("gitcode:"):
            keys.append(f"onboarded:{repo_key[8:]}")
        with db_lock:
            c = db.cursor()
            ph = ",".join("?" for _ in keys)
            c.execute(f"DELETE FROM meta WHERE `key` IN ({ph})", keys)
            if c.rowcount:
                log.info(f"onboarding: cleared stale mark(s) for new repo {repo_key}: {c.rowcount}")
            db.commit()
    except Exception as e:
        log.warning(f"onboarding: clear mark failed for {repo_key}: {e}")


def cleanup_stale_repo_data(repo_key: str):
    """清理同名旧仓库残留的 DB 数据（不删 managed_repos / workspace / skill 目录）。

    当用户在 GitCode 网页直接删除仓库后重建同名仓库时，grape 不知道旧仓库被删，
    events / issue_state / agent_tasks / devops_agent_session 等表残留旧数据。
    新仓库的 repo_key 与旧仓库完全相同，前端查到旧数据导致界面混乱。

    在 trigger_onboarding 之前调用（repo_created=True 时），确保新仓库干净启动。
    与 _cleanup_repo_data 的区别：不删 managed_repos / workspace / skill 目录
    （新仓库刚托管，managed_repos 是刚写入的；workspace 可能含新模板代码）。
    """
    try:
        from src.session_manager import db, db_lock
        keys = [repo_key]
        if repo_key.startswith("gitcode:"):
            keys.append(repo_key[8:])
        else:
            keys.append(f"gitcode:{repo_key}")
        keys = list(dict.fromkeys(k for k in keys if k))

        with db_lock:
            c = db.cursor()
            ph = ",".join("?" for _ in keys)
            total = 0
            # 1. vod_conversations（依赖 devops_agent_session.id，需先删）
            try:
                c.execute(
                    f"DELETE FROM vod_conversations WHERE agent_session_id IN "
                    f"(SELECT id FROM devops_agent_session WHERE repo_key IN ({ph}))",
                    keys,
                )
                total += c.rowcount
            except Exception:
                pass
            # 2. chat_messages（依赖 chat_conversations.id，需先删）
            try:
                c.execute(f"SELECT id FROM chat_conversations WHERE repo_key IN ({ph})", keys)
                conv_ids = [r[0] for r in c.fetchall()]
                if conv_ids:
                    cid_ph = ",".join("?" for _ in conv_ids)
                    c.execute(f"DELETE FROM chat_messages WHERE conversation_id IN ({cid_ph})", conv_ids)
                    total += c.rowcount
            except Exception:
                pass
            # 3. 直接按 repo_key 匹配的表
            for tbl in (
                "events", "event_skipped", "chat_conversations", "chat_agent_session",
                "repo_im_group_binding", "pipeline_instances", "devops_agent_session",
                "issue_agent_session", "issue_state", "agent_tasks",
            ):
                try:
                    c.execute(f"DELETE FROM {tbl} WHERE repo_key IN ({ph})", keys)
                    total += c.rowcount
                except Exception as e:
                    log.warning(f"cleanup_stale_repo_data: {tbl} failed: {e}")
            # 4. 仓库 skill 记录（scope='repo'）
            try:
                c.execute(
                    f"DELETE FROM skill WHERE scope='repo' AND owner_id IN ({ph})",
                    keys,
                )
                total += c.rowcount
            except Exception:
                pass
            # 5. onboarding 幂等标记
            try:
                _ob_keys = [f"onboarded:{k}" for k in keys]
                _ob_ph = ",".join("?" for _ in _ob_keys)
                c.execute(f"DELETE FROM meta WHERE `key` IN ({_ob_ph})", _ob_keys)
                total += c.rowcount
            except Exception:
                pass
            db.commit()
            if total:
                log.info(f"cleanup_stale_repo_data: cleaned {total} rows for {repo_key}")
    except Exception as e:
        log.warning(f"cleanup_stale_repo_data: failed for {repo_key}: {e}")


def _onboarding_issue_id(target: str) -> str:
    """引导预分配 Issue 号（与主事件 issue_id 预分配一致）：experience→#1, explore→#2。"""
    try:
        return f"#{ONBOARDING_TARGETS.index(target) + 1}"
    except ValueError:
        return ""


def _onboarding_workdir(repo_key: str, target: str) -> str:
    """引导工作目录（按 Issue 统一隔离，见 workdir-issue-centric-design.md §2.1）。

    onboarding 是 vod agent 的特殊执行，cwd 与真实 Issue 共用：
    experience→ {repo}/issues/1/vod/，explore→ {repo}/issues/2/vod/
    """
    from src.api.event_pipeline import get_agent_workdir
    return get_agent_workdir(repo_key, "vod", _onboarding_issue_id(target))


def _load_vod_session(repo_key: str) -> tuple[str, int, str, int]:
    """查 VOD 会话配置，返回 (backend, runtime_id, skill_path, skill_id)。缺失时 backend=''（无硬编码默认，见 engine-selection-design.md v2）。"""
    backend, runtime_id, skill_path, skill_id = "", 0, "", 0
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod'", (repo_key,))
            row = c.fetchone()
        if row:
            runtime_id = row[0] or 0
            if row[2]:
                skill_id = row[2]
                from src.user.db import get_skill_by_id
                _sk = get_skill_by_id(row[2])
                if _sk:
                    skill_path = _sk.get("skill_path") or ""
        if runtime_id:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
                _r = c.fetchone()
            if _r and _r[0]:
                backend = _r[0]
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] vod session lookup failed: {e}")
    return backend, runtime_id, skill_path, skill_id


def _finalize_onboarding(repo_key: str, seq: int, owner: str, result_path: str = ""):
    """单次引导完成补库：读技能产出 → 该次引导创建的 1 个 Issue 建 vod 阶段 + 独立事件。

    主事件（onboarding_exp/explore）已由 _execute_agent 收尾；本函数补充：
    主事件 issue_id 校准 + Issue 阶段 + 子事件。
    """
    from src.api.event_store import append_event, update_event_issue_id
    from src.api.session_store import set_issue_stage
    if not (result_path and os.path.exists(result_path)):
        log.warning(f"[Onboarding|repo:{repo_key}] result not found: {result_path}")
        return
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] read result failed: {e}")
        return

    num = str(data.get("number", "")).strip()
    title = data.get("title", "") or f"Issue #{num}"
    if not num or num in ("0",):
        log.warning(f"[Onboarding|repo:{repo_key}] invalid issue number in result: {data}")
        return
    # 【校准】主事件 issue_id 预分配（#1/#2）可能与实际不符 → 回写纠正
    try:
        update_event_issue_id(seq, f"#{num}")
        log.info(f"[Onboarding|repo:{repo_key}] master event seq={seq} issue_id calibrated to #{num}")
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] master event issue_id calibration failed: {e}")
    # 新 Issue 处于 vod 阶段：VOD 需先收敛用户需求（阶段矩阵 D8），收敛后 A2A 再推进
    set_issue_stage(repo_key, f"#{num}", "vod", "vod",
                    assignee=(data.get("assignee") or owner or ""))
    append_event(
        repo_key=repo_key, event_type="onboarding_issue",
        title=title, issue_id=f"#{num}", agent_type="vod",
        source="template", actor=owner or "",
        summary=f"VOD 引导创建的 Issue #{num}：{title}（待 VOD 收敛需求）",
        status="completed",
    )
    log.info(f"[Onboarding|repo:{repo_key}] issue_state=vod for #{num} ({title})")


def _execute_agent_wrapper(repo_key, seq, owner, skill, backend, runtime_id, evt, skill_path, target):
    """direct 模式：复用 _execute_agent 执行 repo-onboarding 技能，注入 ONBOARDING_RESULT/TARGET。"""
    from src.api.event_pipeline import _execute_agent
    _ws = _onboarding_workdir(repo_key, target)
    os.makedirs(_ws, exist_ok=True)
    _result_path = os.path.join(_ws, f"onboarding-result-{target}.json")
    evt.setdefault("_env_extra", {})["ONBOARDING_RESULT"] = _result_path
    evt.setdefault("_env_extra", {})["ONBOARDING_TARGET"] = target
    _execute_agent(repo_key, seq, "vod", backend, skill, evt, runtime_id)
    _finalize_onboarding(repo_key, seq, owner, _result_path)


def _enqueue_onboarding_task(repo_key, seq, backend, runtime_id, owner, skill, target, skill_id=0):
    """queue 模式：入队由本地/远端 worker 执行（本地无 opencode 也可运行）。

    方案 B：按预分配 issue 号（experience→#1, explore→#2）建 Issue 级会话，传 issue_session_id，
    使两个引导任务按不同 Issue 会话并行（claim 串行槽隔离，见 multi-issue-isolation-design.md §3.3）。
    """
    from src.runtime.service import enqueue_task
    from src.repo_key import from_repo_key
    from src.user.db import get_gitcode_token
    repository = from_repo_key(repo_key)
    ws = _onboarding_workdir(repo_key, target)
    os.makedirs(ws, exist_ok=True)
    result_path = os.path.join(ws, f"onboarding-result-{target}.json")
    from src.prompt_builder import build_prompt
    from src.api.session_store import get_or_create_issue_session
    # 预分配 issue 号（与主事件 issue_id 预分配一致）：experience→#1, explore→#2
    expected_issue = ONBOARDING_TARGETS.index(target) + 1
    issue_id = f"#{expected_issue}"
    _isess = get_or_create_issue_session(repo_key, "vod", issue_id)
    issue_session_id = _isess.get("id") or ""
    evt = {"type": "onboarding", "repository": repository, "source": "template",
           "actor": owner or "", "account": repository, "issue_id": issue_id,
           "payload": {"repository": repository, "owner": owner, "onboarding_target": target}}
    try:
        prompt = build_prompt(evt, [], "", [skill], skill_path=os.path.join(SKILLS_DIR, skill, "SKILL.md"),
                              runtime_id=runtime_id, agent_type="vod")
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] prompt build failed: {e}")
        return
    tid = enqueue_task(
        agent_session_id=f"vod:{repo_key}", runtime_id=runtime_id,
        event_seq=seq, issue_id=issue_id, repo_key=repo_key, agent_type="vod",
        skill=skill, prompt=prompt, cwd=ws,
        runtime_session_id="", event_type="onboarding",
        account=repository,
        repo_gitcode_token=get_gitcode_token(repo_key=repo_key) or "",
        skill_path=os.path.join(SKILLS_DIR, skill, "SKILL.md"),
        skill_id=skill_id,
        issue_session_id=issue_session_id,
        env_extra={"REPO_FULL_NAME": repository, "REPO_OWNER": owner,
                   "ONBOARDING_RESULT": result_path, "ONBOARDING_TARGET": target},
    )
    log.info(f"[Onboarding|repo:{repo_key}] enqueued task={tid} seq={seq} target={target} issue={issue_id} issue_session={issue_session_id}")
    # queue 模式终态后补库：轮询任务终态，触发 _finalize_onboarding
    try:
        from src.runtime.service import wait_task_terminal
        asyncio.create_task(_await_task_and_finalize(repo_key, seq, tid, owner, result_path))
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] task await setup failed: {e}")


async def _await_task_and_finalize(repo_key, seq, tid, owner, result_path):
    """queue 模式：等待 worker 任务终态后触发 _finalize_onboarding。"""
    try:
        from src.runtime.service import wait_task_terminal
        task = await wait_task_terminal(tid)
        log.info(f"[Onboarding|repo:{repo_key}] task {tid} done, exit={task.get('exit_code')}")
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] wait task {tid} failed: {e}")
    _finalize_onboarding(repo_key, seq, owner, result_path)


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

        # 1. 查 VOD 会话配置（backend/runtime_id/skill_path，走 VOD Agent）
        backend, runtime_id, skill_path, skill_id = _load_vod_session(repo_key)
        # engine-selection-design.md v2：按引擎集合/会话主引擎选引擎；无引擎 → 跳过引导（不隐式 opencode）
        from src.runtime.engine_select import pick_engine, engine_name
        engine_id = pick_engine(repo_key, "vod", fallback_runtime_id=runtime_id) or 0
        if not engine_id:
            log.warning(f"[Onboarding|repo:{repo_key}] no available engine, skip onboarding")
            return
        backend = engine_name(engine_id) or backend
        runtime_id = engine_id

        # 2. 落库两个主事件 + 两次引导分派（各 target 一次）
        #    执行模式跟随选中引擎（kind = runtime_execution_kind(engine_id)，与 vod_handler_async 一致）
        #    主事件 issue_id 预分配：新仓库 Issue 从 1 递增 → experience=#1, explore=#2
        from src.repo_key import from_repo_key
        from src.api.event_store import append_event
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
        kind = runtime_execution_kind(runtime_id or 0)
        for target in ONBOARDING_TARGETS:
            _title = "引导·快速部署体验" if target == "experience" else "引导·探索方向"
            _event_type = "onboarding_exp" if target == "experience" else "onboarding_explore"
            _expected_issue = ONBOARDING_TARGETS.index(target) + 1  # experience→#1, explore→#2
            seq = append_event(
                repo_key=repo_key, event_type=_event_type, title=_title,
                issue_id=f"#{_expected_issue}", agent_type="vod", source="template",
                actor=owner or "", summary=f"{_title}（VOD Agent）：创建对应类型的引导 Issue",
                status="running",
            )
            evt = {"type": _event_type, "repository": from_repo_key(repo_key), "source": "template",
                   "actor": owner or "", "account": from_repo_key(repo_key), "issue_id": f"#{_expected_issue}",
                   "payload": {"repository": from_repo_key(repo_key), "owner": owner, "onboarding_target": target}}
            if kind != EXEC_MODE_DIRECT:
                _enqueue_onboarding_task(repo_key, seq, backend, runtime_id, owner, skill, target, skill_id=skill_id)
            else:
                evt["_skill_path"] = skill_path
                threading.Thread(target=_execute_agent_wrapper, args=(
                    repo_key, seq, owner, skill, backend, runtime_id, evt, skill_path, target,
                ), daemon=True, name=f"onboarding-{target}-{seq}").start()
            log.info(f"[Onboarding|repo:{repo_key}] 触发引导 target={target}, event seq={seq}, backend={backend}")
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] trigger failed: {e}")
