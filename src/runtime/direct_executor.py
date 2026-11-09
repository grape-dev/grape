"""本机 direct 执行器——从 event_pipeline.py 迁移（P0, Issue #596）。

负责本机引擎的 Agent 执行（stream_devops_agent → 结果回写 → 事件更新）。
迁移后 event_pipeline.py (B 进程) 只负责入队，不再直接执行；
本模块由 C 进程的 local_claimer 认领后调用。

设计文档：docs/weekly-09/grape-three-process-split-design.md §3
"""
import os, threading, traceback
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from src.config import log

_DIRECT_POOL = ThreadPoolExecutor(
    max_workers=int(os.environ.get("RUNTIME_MAX_CONCURRENT_TASKS", "20")),
    thread_name_prefix="ag-direct",
)
# {event_seq: Future} — 供 sweeper 取消排队中的 direct 任务
_DIRECT_FUTURES: dict[int, Future] = {}
_DIRECT_FUTURES_LOCK = threading.Lock()


def start_direct_agent(fn, args, name=""):
    """提交一个本机 direct 执行任务到有界池（webhook / run_pipeline 共用）。"""
    future = _DIRECT_POOL.submit(fn, *args)
    # 从 args 中提取 seq（约定第一个参数是 repo_key，第二个是 seq）
    seq = args[1] if len(args) > 1 else 0
    if seq:
        with _DIRECT_FUTURES_LOCK:
            _DIRECT_FUTURES[seq] = future
    return future


def _execute_agent(repo_key: str, seq: int, agent_type: str,
                   backend: str, skill_name: str, evt: dict, runtime_id: int = 0,
                   task_id: str = ""):
    """后台执行 Agent：构建 prompt → stream_agent → 更新 event（result/status/agent_log）
    
    顶层 try/except 兜底：防止线程内未捕获异常导致事件永久卡在 running
    （如 runtime_instance_name / skill 加载 / stream_agent 启动前崩溃）。
    """
    # P0 (Issue #596): lazy import from event_pipeline to avoid circular import
    from src.api.event_pipeline import _update_direct_task, _cleanup_direct_workdir
    try:
        _execute_agent_inner(repo_key, seq, agent_type, backend, skill_name, evt, runtime_id, task_id)
    except Exception as e:
        import traceback
        _tb = traceback.format_exc()
        log.error(f"Exec agent: [Agent:{agent_type}|repo:{repo_key}] UNHANDLED CRASH: {e}\n{_tb}")
        from datetime import datetime, timezone
        try:
            from src.api.event_store import update_event_complete
            update_event_complete(seq, "failed", exit_code=1,
                                  completed_at=datetime.now(timezone.utc).isoformat(),
                                  result=f"执行崩溃：{e}")
            if task_id:
                from src.runtime.service import update_task_status
                update_task_status(task_id, "failed", exit_code=1, result_text=f"执行崩溃：{e}")
            else:
                _update_direct_task(seq, "failed", exit_code=1, result_text=f"执行崩溃：{e}")
        except Exception:
            pass
    finally:
        # 清理 Future 记录
        with _DIRECT_FUTURES_LOCK:
            _DIRECT_FUTURES.pop(seq, None)
        # Issue-45 M2：direct 会话结束 → 会话级临时 token 即时失效
        try:
            from src.api.auth_token import revoke_session_token
            revoke_session_token(task_id or f"d_{seq}")
        except Exception as e:
            log.warning(f"Exec agent: revoke session token failed: {e}")
        # 任务级工位清理（direct 端）：轻文件 + 确认已 push 的 worktree
        try:
            _cleanup_direct_workdir(
                repo_key, agent_type,
                evt.get("issue_id", "") or (evt.get("payload") or {}).get("issue_number", "") or ""
            )
        except Exception:
            pass



def _execute_agent_inner(repo_key: str, seq: int, agent_type: str,
                         backend: str, skill_name: str, evt: dict, runtime_id: int = 0,
                         task_id: str = ""):
    """_execute_agent 的实际逻辑（被顶层 try/except 包裹）。"""
    # P0 (Issue #596): lazy import from event_pipeline to avoid circular import
    from src.api.event_pipeline import (
        _fail_event, _inject_bot_agent_env, _inject_issue_pr_env,
        _inject_linked_repo_tokens, _inject_scene_env, _issue_num_from,
        _materialize_direct_workdir, _post_all_subtasks_done,
        _post_result_to_issue, _post_result_to_pr,
        _post_subtask_completion_to_parent, _read_scene_linked_repos,
        _redispatch_vod_after_recommend, _submit_direct_linked_prs,
        _sync_issue_comments_to_conversation, _update_direct_task,
        get_agent_display_name, get_agent_workdir, resolve_actor_account,
        register_scene_deploy_url, register_sre_deploy_url,
    )
    from src.runtime.executor import stream_devops_agent
    from src.prompt_builder import build_devops_agent_prompt
    from src.api.event_store import (
        update_event_config, update_event_status, update_event_complete, evaluate_agent_output, _extract_tool_command,
        append_skipped_event,
    )
    from src.runtime.instances import runtime_instance_name

    # 引擎实例名（`{type}-{device}`，与 UI 同源）；查不到回退类型名
    inst_name = runtime_instance_name(runtime_id) or backend or ""

    # direct 执行开始 → 更新 task status 为 running（direct 路径无 worker task_start 调用）
    if task_id:
        try:
            from src.runtime.service import update_task_status
            update_task_status(task_id, "running")
            log.info(f"direct exec: task {task_id} status -> running")
            # Write back actual runtime_id for plugin broadcast self-run tasks
            # (task created with runtime_id=0 for broadcast, but executed locally)
            if runtime_id and runtime_id > 0:
                from src.session_manager import db as _sdb, db_lock as _sdl
                with _sdl:
                    c = _sdb.cursor()
                    c.execute("UPDATE agent_tasks SET runtime_id=? WHERE id=? AND (runtime_id IS NULL OR runtime_id=0)", (runtime_id, task_id))
                    _sdb.commit()
                log.info(f"direct exec: task {task_id} runtime_id -> {runtime_id}")
        except Exception as e:
            log.warning(f"direct exec: failed to update task {task_id} to running: {e}")

    from src.repo_key import from_repo_key
    repository = evt.get("repository", from_repo_key(repo_key))
    issue_id = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "") or ""
    ws = get_agent_workdir(repo_key, agent_type, issue_id)
    os.makedirs(ws, exist_ok=True)

    # 读取上次的 oc_session_id（用于延续对话上下文）——Issue 级执行会话优先，回退 repo 级
    # 注意：有关联 Issue 时只用该 Issue 自己的会话（每 Issue 独立 session，见 vod-agent-hang-rootcause-fix.md 方案 A）；
    #       repo 级 session 仅用于无关联 Issue（push/#0）。
    repo_name = repo_key
    oc_session_id = ""
    issue_session_id = ""
    _has_issue = bool(_issue_num_from(issue_id))
    try:
        if _has_issue:
            from src.api.session_store import get_or_create_issue_session, guard_issue_session_pointer
            _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
            issue_session_id = _isess.get("id") or ""
            # session 引擎守卫：backend 即本次引擎名，跨引擎指针弃用（防 hermes resume
            # opencode 会话号静默挂死）；引擎未知 → 放行。
            oc_session_id = guard_issue_session_pointer(_isess, backend or "")
        # 无关联 Issue 任务不执行（push 忽略），不再回退读 repo 级 runtime_session_id。
    except Exception:
        pass

    log.info(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] type={agent_type}, backend={inst_name}, skill={skill_name}, oc_session={oc_session_id[:20] if oc_session_id else 'none'}")

    # 获取 bot token（按 provider 分流：GitHub 仓库用 get_github_token，其他用 get_repo_token_with_refresh）
    repo_gitcode_token = ""
    try:
        if repo_key.startswith("github:"):
            from src.user.db import get_github_token
            repo_gitcode_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_repo_token_with_refresh
            repo_gitcode_token = get_repo_token_with_refresh(repo_key=repo_key) or ""
    except Exception as e:
        log.warning(f"Exec agent: get bot token failed: {e}")
    log.info(f"Exec agent: bot_token={'set' if repo_gitcode_token else 'none'} (provider={'github' if repo_key.startswith('github:') else 'gitcode'})")

    # R20.25.4: 获取 user_token 用于脱敏（路径 B 仓库的 host 用户 token，agent 可能复述）
    _user_token_for_redact = ""
    try:
        from src.user.db import get_host_user_id
        _host_uid = get_host_user_id(repo_key)
        if _host_uid > 0:
            from src.auth.grape_account_db import get_grape_account_by_id
            _host_acc = get_grape_account_by_id(_host_uid)
            if _host_acc and _host_acc.get("username"):
                if repo_key.startswith("github:"):
                    from src.api.deps import get_user_github_token_with_refresh
                    _user_token_for_redact = get_user_github_token_with_refresh(_host_acc["username"], _host_uid) or ""
                else:
                    from src.api.deps import get_user_token_with_refresh
                    _user_token_for_redact = get_user_token_with_refresh(_host_acc["username"], _host_uid) or ""
    except Exception:
        pass

    # Agent 显示名（注入 AGENT_NAME 环境变量，skill 中 [$AGENT_NAME] 使用）
    agent_name = get_agent_display_name(agent_type)

    try:
        from src.prompt_builder import build_devops_agent_prompt
        evt["repo_key"] = repo_key
        evt["issue_id"] = issue_id
        # 智能推荐等入队时已构建 prompt 的任务（local_claimer 认领的广播任务）：
        # 优先复用任务行已落库的 prompt，禁止重建——重建会用 skill_name→agent_type
        # fallback 拼出技能 prompt（{{SKILL_DIR}}），把推荐器 prompt 替换掉，
        # 导致 agent 按技能剧本跑而非输出 scene_id JSON，且后置「智能推荐器」
        # prompt 识别条件永远失配（scene_id 落库/VOD 重派全断，2026-10-21 实锤）。
        # 与 worker 执行路径对齐（worker 直接用任务行 prompt）。
        _reused_prompt = ""
        if task_id:
            try:
                from src.session_manager import db as _pdb, db_lock as _pdl
                with _pdl:
                    c = _pdb.cursor()
                    c.execute("SELECT prompt FROM agent_tasks WHERE id=?", (task_id,))
                    _pr = c.fetchone()
                    _reused_prompt = (_pr[0] or "").strip() if _pr else ""
            except Exception as _pe:
                log.warning(f"Exec agent: read task prompt failed: {_pe}")
        if _reused_prompt:
            prompt = _reused_prompt
            log.info(f"Exec agent: reuse enqueued task prompt ({len(prompt)} chars, task={task_id}), skip rebuild")
        else:
            prompt = build_devops_agent_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
                                  skill_path=evt.get("_skill_path", ""), runtime_id=runtime_id, agent_type=agent_type)
        # 直接模式（direct）：组件4 统一物化 project-info / skill / knowledge 到 ws 内
        # （替代旧的「占位符替换为服务端绝对路径：仓库级 project-info 原件 / 全局 SKILLS_DIR」），
        # 使 direct 端与 worker 端 cwd 布局一致，后续 bwrap 锁只需挂 ws。
        prompt = _materialize_direct_workdir(
            ws, repo_key, agent_type, skill_name, evt, issue_id, prompt,
            engine_name=inst_name or backend or ""
        )
        # Issue #887：direct 端执行前一致性硬校验——绑定 skill 未物化而工位本地缓存
        # 存在其它技能时拒绝执行（fail-fast，显式报错，不静默跑错流程）。
        # 契约键缺失（物化异常降级）→ 显式告警后放行，不静默当作通过。
        try:
            from src.runtime.skill_materialize import (SKILL_CONSISTENCY_KEY as _SCK,
                                                        skill_mismatch_error as _sme)
        except Exception:  # noqa: BLE001 - 门禁依赖缺失不得中断执行链
            _SCK = "_skill_consistency"
            def _sme(sv):
                return ("[skill_mismatch] "
                        + str((sv or {}).get("reason") or "skill consistency check failed"))
        _sv_887 = evt.get(_SCK) or {}
        if not _sv_887:
            log.warning(f"Exec agent: seq={seq} skill consistency verdict missing "
                        f"(materialize degraded); degraded pass")
        if _sv_887.get("ok") is False:
            _sv_err = _sme(_sv_887)
            log.error(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] "
                      f"skill consistency refused: {_sv_err}")
            if task_id:
                try:
                    from src.session_manager import db as _sdb, db_lock as _sdl
                    with _sdl:
                        _sc = _sdb.cursor()
                        _sc.execute(
                            "UPDATE agent_tasks SET status='failed', exit_code=1, "
                            "result_text=?, errors=?, completed_at=? WHERE id=? "
                            "AND status IN ('running','queued','dispatched')",
                            (_sv_err, _sv_err,
                             datetime.utcnow().isoformat(), task_id))
                        _sdb.commit()
                except Exception as _se:
                    log.warning(f"Exec agent: mark task failed failed: {_se}")
            _fail_event(seq, _sv_err, diag={
                "code": "skill_mismatch",
                "skill": _sv_887.get("bound") or skill_name or "",
                "actual": _sv_887.get("actual") or "",
                "agent_type": agent_type, "repo_key": repo_key,
                "reason": (_sv_887.get("reason") or "")[:300]})
            return
        log.info(f"Exec agent: prompt built, {len(prompt)} chars, skill={skill_name or agent_type}: {prompt[:2000]}")
        # 回写 prompt 到 agent_tasks（direct 模式创建时 prompt 为空，此处补写）
        log.info(f"Exec agent: prompt writeback check: task_id={task_id!r}, prompt_len={len(prompt)}")
        if task_id and prompt:
            try:
                from src.session_manager import db as _wdb, db_lock as _wdl
                with _wdl:
                    c = _wdb.cursor()
                    c.execute("UPDATE agent_tasks SET prompt=? WHERE id=? AND prompt=''", (prompt, task_id))
                    _wdb.commit()
                log.info(f"Exec agent: prompt writeback OK for task={task_id}, rows={c.rowcount}")
            except Exception as _pe:
                log.warning(f"Exec agent: prompt writeback failed for task={task_id}: {_pe}")
    except Exception as e:
        log.warning(f"Exec agent: prompt build failed: {e}")
        _prompt_skip_iid = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "")
        if _prompt_skip_iid and not str(_prompt_skip_iid).startswith("#"):
            _prompt_skip_iid = f"#{_prompt_skip_iid}"
        append_skipped_event(repo_key, evt.get("type", ""), "prompt_build_failed", str(e),
                             issue_id=_prompt_skip_iid)
        _fail_event(seq, f"执行失败：prompt 构建异常 {str(e)[:150]}")
        return

    exec_account = evt.get("account") or repository
    if not repo_gitcode_token:
        log.warning(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] no bot token available, agent will fail on API calls")

    # 记录执行配置（执行完成有产出后统一写入 event）
    started_at = datetime.utcnow().isoformat()
    log.info(f"Exec agent: [Agent:{agent_type}|repo:{repo_name}] spawning {inst_name}")

    exit_code = -1
    final_text = ""
    new_oc_session_id = ""
    _exec_done_errors = []  # R20.8a.1: done 事件 errors 用于 401 标记
    _exec_done_stderr = ""
    tool_uses = []
    agent_logs = []
    # Issue #564：技能物化降级标注落入事件 agent_logs（不静默空注入）
    try:
        _sk_mat = evt.get("_skill_materialize") or {}
        if _sk_mat.get("status") in ("fallback", "degraded"):
            agent_logs.append({
                "time": datetime.utcnow().isoformat(),
                "type": "skill_materialize",
                "msg": f"skill materialize {_sk_mat.get('status')}: {_sk_mat.get('detail')}",
            })
    except Exception:
        pass
    # Issue #887：技能来源标注（绑定值 vs 实际值）落入事件 agent_logs
    try:
        from src.runtime.skill_materialize import SKILL_CONSISTENCY_KEY as _SCK2
        _sk_src = evt.get(_SCK2) or {}
        if _sk_src:
            from src.runtime.task_log_stream import build_skill_source_message as _bssm
            _sk_line = _bssm(_sk_src.get("bound") or "", _sk_src.get("actual") or "",
                             _sk_src.get("source") or "", ok=bool(_sk_src.get("ok")),
                             reason=_sk_src.get("reason") or "",
                             source_info=_sk_mat.get("source") or None,
                             drift=_sk_mat.get("drift") or None)
            agent_logs.append({
                "time": datetime.utcnow().isoformat(),
                "type": "skill_source",
                "msg": ((_sk_line.get("data") or {}).get("text") or ""),
            })
    except Exception:
        pass
    # Issue #1660：REPO_OWNER/REPO_NAME 一律由 REPO_FULL_NAME（repository）派生，缺失回退 repo_key；
    # **禁止**用 exec_account（触发人账号/带前缀全名）充当 REPO_OWNER——否则触发人≠仓库 owner 时
    # 会注入 REPO_OWNER=<触发人>、REPO_NAME=""（反馈 2874）。Issue #94 的前缀剥离由派生入口统一处理。
    from src.repo_key import derive_repo_identity as _derive_env
    _full_name_env, _repo_owner_env, _repo_name_env = _derive_env(repository, repo_key)
    env_extra = {
        "REPO_FULL_NAME": _full_name_env or repository,
        "REPO_OWNER": _repo_owner_env,
        "REPO_NAME": _repo_name_env,
        # Issue #1668：统一 actor 解析（a2a_event 回退 payload.account/Issue creator），
        # 禁止用 exec_account（仓库全名）冒充个人账号。
        "ACCOUNT": resolve_actor_account(evt, repo_key),
    }
    # 注入场景环境变量（scene_id / work_branch / custom_fields；direct 模式同样注入，保持与 worker 模式一致）
    # Issue #427：PR 订阅 direct 派发（evt 带 _pr_sub_scene_id）同样按订阅场景注入
    try:
        from src.runtime.repogit import _read_scene_work_branch
        _wb = _read_scene_work_branch(repo_key, issue_id)
        if _wb:
            env_extra["GRAPE_WORK_BRANCH"] = _wb
    except Exception:
        pass
    _inject_scene_env(env_extra, repo_key, issue_id, agent_type,
                      scene_id_override=evt.get("_pr_sub_scene_id", ""))
    # Issue #242: 注入关联仓库列表（coding agent 读取后初始化多仓库 worktree）
    try:
        import json
        _linked_repos = _read_scene_linked_repos(repo_key, issue_id)
        if _linked_repos:
            env_extra["GRAPE_LINKED_REPOS"] = json.dumps(_linked_repos, ensure_ascii=False)
            # Issue #466: 关联仓 token 随 env 分发（worker 端本地无库，纯本地解析必为空）
            _inject_linked_repo_tokens(env_extra, env_extra["GRAPE_LINKED_REPOS"])
    except Exception:
        pass
    # Issue #573：BOT_USERNAME/AGENT_NAME 注入全部 devops agent（原仅 coding 注 BOT_USERNAME）；
    # direct 路径 agent_name 参数仍同步传 stream_devops_agent（幂等同值）。
    _inject_bot_agent_env(env_extra, repo_key, agent_type)
    # 事件级扩展环境变量（如 onboarding 的 ONBOARDING_RESULT / ONBOARDING_TARGET）
    _ext = evt.get("_env_extra") or {}
    if isinstance(_ext, dict):
        env_extra.update(_ext)
    _inject_issue_pr_env(env_extra, evt, repo_key)

    # git worktree（coding/qa/sre，direct 模式；见 multi-issue-isolation-design.md §3.4.1）
    # P2（vcs-env-token-boundary-audit）：不再把 GIT_ASKPASS 路径注入 agent env——
    # agent 侧 git 操作统一走 `grape git`（服务端 git-credential 会话凭据），不依赖 askpass env。
    try:
        from src.runtime import repogit
        _gnum = repogit.issue_num_from(issue_id)
        if repogit.is_git_agent(agent_type) and _gnum and issue_session_id:
            _wb = env_extra.get("GRAPE_WORK_BRANCH", "")
            _wt = repogit.ensure_issue_worktree(repo_key, agent_type, _gnum,
                                                token=repo_gitcode_token, work_branch=_wb)
    except Exception as e:
        log.warning(f"Exec agent: git worktree ensure failed: {e}")

    # Issue #242: 关联仓库 worktree 初始化（direct 模式 coding/sre agent 多仓库并行操作）
    # 与 worker 端 _execute_task 中的逻辑等价：遍历 GRAPE_LINKED_REPOS → ensure_linked_repo_worktrees
    # → 注入 GRAPE_LINKED_WORKTREES（供 _submit_linked_repo_prs 消费 / SRE 构建部署消费）
    # Issue #466: 传 env（含 GRAPE_LINKED_REPO_TOKENS）——direct 端虽连库，但 env 优先口径与 worker 一致
    try:
        from src.runtime import repogit
        _linked_repos_env_dr = env_extra.get("GRAPE_LINKED_REPOS", "")
        if _linked_repos_env_dr and agent_type in ("coding", "sre", "qa"):
            import json as _json_lr_dr
            _linked_repos_list_dr = _json_lr_dr.loads(_linked_repos_env_dr) if _linked_repos_env_dr else []
            if _linked_repos_list_dr:
                _gnum_lr = repogit.issue_num_from(issue_id)
                _wt_list_dr = repogit.ensure_linked_repo_worktrees(
                    _linked_repos_list_dr, agent_type, _gnum_lr,
                    token=repo_gitcode_token, env=env_extra)
                if _wt_list_dr:
                    env_extra["GRAPE_LINKED_WORKTREES"] = _json_lr_dr.dumps(
                        [{"repo_key": w["repo_key"], "branch": w["branch"], "dir": w["worktree_dir"]}
                         for w in _wt_list_dr], ensure_ascii=False)
                    log.info(f"Exec agent: linked repos: {len(_wt_list_dr)} worktrees initialized for issue={_gnum_lr} agent={agent_type}")
    except Exception as e:
        log.warning(f"Exec agent: linked repos worktree init failed: {e}")

    # Issue-45 M2：direct 会话签发会话级临时 token（session_key=task_id 或 d_{seq}）
    # Issue #990：已入队任务优先复用/同步快照 token，避免 direct 再签一个 token 造成
    # shell env 快照（agent_tasks.env_extra）与运行时注入值不一致（旧快照 token → 401）。
    try:
        from src.runtime.service import session_token_for_execution
        if repo_key and agent_type:
            token = session_token_for_execution(
                task_id, session_key_fallback=f"d_{seq}",
                repo_key=repo_key, agent_type=agent_type, user_id=0)
            if token:
                env_extra["GRAPE_SESSION_TOKEN"] = token
                # CLI 命令按 token 作用域过滤：agent 持 tk_ 会话 token → 只暴露 dual+session_only 命令
                # （见 grape-cli-mode-filter-design.md；避免 agent 打 user_only 接口 403 重试）
                env_extra["GRAPE_CLI_MODE"] = "WORKER"
            else:
                log.warning("Exec agent: no session token resolved; "
                            "skip injecting unpersisted token to avoid snapshot drift")
    except Exception as e:
        log.warning(f"Exec agent: issue session token failed: {e}")

    # 用量采集（审计 2026-10-17）：DIRECT 模式跑在主控进程里，以前一条 token 都不记；
    # 有 agent_tasks 行 → run_kind='task'，归属自动反查，幂等键与 worker 侧一致。
    from src.observability.master_usage import for_task as _master_for_task
    _mu = _master_for_task(task_id, backend, cwd=ws, session_id=oc_session_id, seq=seq,
                           repo_key=repo_key, agent_type=agent_type,
                           engine_id=int(runtime_id or 0))
    _mu.start()

    # Issue #596 P1-P3 审查修复：direct 模式跨进程终止（状态驱动取消）。
    # 三进程拆分后 terminate 请求落在 B 进程，cancel_direct_task 只能操作
    # 本进程 _DIRECT_FUTURES（direct 执行在 C），B 侧 futures 恒为空。
    # 参照 worker 模式的心跳匹配取消：B 侧 fail_task/update_event_status 先落库，
    # C 侧执行流周期性探测 DB，发现任务/事件已被外部置终态（failed/cancelled）
    # 即刻杀引擎子进程并中断流（终态回写有幂等保护，不会翻转状态）。
    import time as _probe_time
    _PROBE_INTERVAL = 10.0
    _last_status_probe = 0.0

    def _external_terminal() -> str:
        """探测本任务/事件是否已被其它进程（terminate/超时等）置为终态。"""
        try:
            from src.session_manager import db as _pdb, db_lock as _pdl
            if task_id:
                with _pdl:
                    c = _pdb.cursor()
                    c.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,))
                    trow = c.fetchone()
                if trow and trow[0] in ("failed", "cancelled"):
                    return f"task:{trow[0]}"
            with _pdl:
                c = _pdb.cursor()
                c.execute("SELECT status FROM events WHERE seq=?", (seq,))
                erow = c.fetchone()
            if erow and erow[0] in ("failed", "cancelled"):
                return f"event:{erow[0]}"
        except Exception as _pe:
            log.debug(f"direct exec status probe failed (seq={seq}): {_pe}")
        return ""

    try:
        for evt_type, data in stream_devops_agent(
            prompt, ws, session_id=oc_session_id, event_type=evt.get("type", ""),
            backend_name=backend, account=exec_account, bot_token=repo_gitcode_token,
            agent_name=agent_name,
            agent_label=f"[Agent:{agent_type}|repo:{repo_name}|engine:{inst_name}]",
            env_extra=env_extra,
            repo_key=repo_key,
        ):
            # 写库兜底脱敏：即使上游 executor 已脱敏，此处再防御一次（agent 可能在 text/result 复述 token）
            _msg = str(data)[:500]
            # R20.25.2: 脱敏覆盖所有注入的 token 值（repo_gitcode_token + user_token）
            if repo_gitcode_token:
                _msg = _msg.replace(repo_gitcode_token, "[REDACTED]")
            if _user_token_for_redact:
                _msg = _msg.replace(_user_token_for_redact, "[REDACTED]")
            agent_logs.append({"time": datetime.utcnow().isoformat(), "type": evt_type, "msg": _msg})
            # 状态驱动取消探测：周期性查库，外部 terminate 已置终态 → 杀引擎并中断流
            if evt_type != "done":
                _now_probe = _probe_time.monotonic()
                if _now_probe - _last_status_probe >= _PROBE_INTERVAL:
                    _last_status_probe = _now_probe
                    _ext_term = _external_terminal()
                    if _ext_term:
                        log.warning(
                            f"Exec agent: seq={seq} externally terminated ({_ext_term}), "
                            f"killing engine subprocess (state-driven cancel)"
                        )
                        try:
                            from src.runtime.agent import _kill_stale_engine
                            _kill_stale_engine({"cwd": ws, "id": task_id}, backend)
                        except Exception as _ke:
                            log.warning(f"Exec agent: state-driven kill failed (seq={seq}): {_ke}")
                        break
            # 实时推送：direct 模式复用 worker 的 report_messages（落库 task_messages + fanout SSE）
            # 让前端「实时日志」面板在 hermes 执行期间就能看到逐步输出，不用等执行完成
            _mu.feed(evt_type, data)
            if task_id and evt_type != "done":
                try:
                    from src.runtime.service import report_messages
                    report_messages(task_id, [{"type": evt_type, "data": data}])
                except Exception as _re:
                    log.warning(f"direct report_messages failed (task={task_id}): {_re}")
            if evt_type == "done":
                d = data
                exit_code = d.get("exit_code", -1)
                final_text = d.get("text", "")
                new_oc_session_id = d.get("oc_session_id", "")
                # R20.8a.1: 保存 errors/stderr 用于 401 标记
                _exec_done_errors = d.get("errors") or []
                _exec_done_stderr = d.get("stderr") or ""
                log.info(f"Agent done: [Agent:{agent_type}|repo:{repo_name}] exit={exit_code}, text_len={len(final_text)}, new_oc_session={new_oc_session_id[:20] if new_oc_session_id else 'none'}")
                # 智能推荐完成处理：解析 scene_id → 持久化 → 重派 VOD
                if agent_type == "vod" and final_text and issue_id:
                    _task_prompt = ""
                    try:
                        from src.session_manager import db as _rdb, db_lock as _rdb_lock
                        with _rdb_lock:
                            c = _rdb.cursor()
                            c.execute("SELECT prompt FROM agent_tasks WHERE id=?", (task_id,))
                            _trow = c.fetchone()
                            _task_prompt = _trow[0] if _trow else ""
                    except Exception:
                        pass
                    # 判断是否为智能推荐任务：prompt 含"智能推荐器"且无 {{SKILL_DIR}}
                    if _task_prompt and "智能推荐器" in _task_prompt and "{{SKILL_DIR}}" not in _task_prompt:
                        try:
                            import re as _re
                            _rec_scene = ""
                            # 优先解析 JSON
                            try:
                                from src.api.event_store import parse_agent_output_json
                                _scene_out = parse_agent_output_json(final_text) or {}
                                _rec_scene = _scene_out.get("scene_id", "") or ""
                            except Exception:
                                pass
                            # 正则兜底
                            if not _rec_scene:
                                _m_rec = _re.search(r'"scene_id"\s*:\s*"([^"]*)"', final_text)
                                _rec_scene = _m_rec.group(1) if _m_rec else ""
                            if _rec_scene:
                                from src.api.session_store import set_issue_scene
                                from src.runtime.engine_select import _repo_user_id
                                _vod_uid = _repo_user_id(repo_key, issue_creator=evt.get("actor", ""))
                                set_issue_scene(repo_key, issue_id, _rec_scene, scene_user_id=_vod_uid)
                                log.info(f"智能推荐完成: scene_id={_rec_scene} set for {issue_id}")
                                # 重派 VOD（用匹配场景的 vod 技能）
                                from src.api.pipeline_config import get_first_node, get_pipeline_config
                                _vod_node = get_first_node(_rec_scene, repo_key=repo_key)
                                if not _vod_node:
                                    _pcfg = get_pipeline_config(_rec_scene, repo_key=repo_key)
                                    _nodes = (_pcfg or {}).get("nodes", [])
                                    if _nodes:
                                        _vod_node = _nodes[0]
                                if _vod_node:
                                    _vod_skill = _vod_node.get("skill", "vod")
                                    log.info(f"智能推荐完成: redispatch VOD with skill={_vod_skill} for {issue_id}")
                                    _redispatch_vod_after_recommend(repo_key, issue_id, _vod_skill, evt, runtime_id)
                                else:
                                    log.warning(f"智能推荐完成: no vod node for scene={_rec_scene}, skip redispatch")
                            else:
                                log.info(f"智能推荐完成: no scene_id for {issue_id}, pipeline terminated")
                        except Exception as _se:
                            log.warning(f"智能推荐完成处理失败: {_se}")
            elif evt_type == "error":
                log.warning(f"Agent error: [Agent:{agent_type}|repo:{repo_name}] {data}")
            elif evt_type == "step_start":
                log.info(f"[agent {agent_type}|repo:{repo_name}] {data}")
            elif evt_type == "tool_use":
                tool_uses.append(data if isinstance(data, dict) else {})
                log.info(f"[agent {agent_type}|repo:{repo_name}] TOOL: {str(data)[:160]}")
            elif evt_type == "text":
                log.info(f"[agent {agent_type}|repo:{repo_name}] TEXT: {str(data)[:160]}")
            elif evt_type == "reasoning":
                log.info(f"[agent {agent_type}|repo:{repo_name}] REASONING: {str(data)[:160]}")
            else:
                log.info(f"[agent {agent_type}|repo:{repo_name}] {evt_type}: {str(data)[:160]}")
    except Exception as e:
        log.warning(f"Exec agent failed: [Agent:{agent_type}|repo:{repo_name}] {e}")

    # 用量落台账（流结束后）：放 here 是因为引擎进程已退出、账本已落盘；
    # finish 自身幂等 + 吞异常，绝不影响下面的终态/会话回写。
    try:
        _mu.finish(session_id=new_oc_session_id)
    except Exception as _ue:      # 用量是观测数据，任何异常都不该影响任务终态
        log.warning(f"Exec agent: usage finish failed (task={task_id}): {_ue}")

    # 更新 oc_session_id（持久化对话上下文）——仅写回 Issue 级执行会话
    # repo 级 devops_agent_session.runtime_session_id 已废弃（无消费方），不再回写。
    # 同步记录 session_engine（本次执行引擎），供消费端跨引擎守卫比对。
    if new_oc_session_id and new_oc_session_id != oc_session_id:
        try:
            if issue_session_id:
                from src.api.session_store import update_issue_session
                update_issue_session(issue_session_id, runtime_session_id=new_oc_session_id,
                                     session_engine=backend or "")
        except Exception as e:
            log.warning(f"Exec agent: failed to update oc_session_id: {e}")

    # ── 单信号收尾（C，stream-end-finalize-single-signal-design.md）──
    # 引擎流结束即终态触发点；stream_ended 仅作仪器记录，不再按技能类型区分/门控。
    if task_id:
        try:
            from src.runtime.service import set_task_signal
            set_task_signal(task_id, "stream_ended")
        except Exception as _e:
            log.warning(f"Exec agent: set stream_ended failed for {task_id}: {_e}")

    # 判定 Agent 是否有明确答复产出
    has_output, result_text = evaluate_agent_output(agent_type, tool_uses, final_text)
    # R20.25.1: 脱敏覆盖所有注入的 token 值（repo_gitcode_token + user_token）
    if result_text:
        if repo_gitcode_token:
            result_text = result_text.replace(repo_gitcode_token, "[REDACTED]")
        if _user_token_for_redact:
            result_text = result_text.replace(_user_token_for_redact, "[REDACTED]")
    # 若 Agent 已显式 self_report（结构化结论），以其为权威（产物而非闸门）
    if task_id:
        try:
            from src.runtime.service import get_task
            _t = get_task(task_id) or {}
            if _t.get("self_reported") == 1 and _t.get("result_text"):
                import json as _json
                _stored = _t.get("result_text") or ""
                _sd = None
                try:
                    _sd = _json.loads(_stored)
                except Exception:
                    _sd = None
                if _sd and isinstance(_sd, dict) and (_sd.get("actions") or _sd.get("conclusion")):
                    result_text = _stored
        except Exception as _e:
            log.warning(f"Exec agent: self-reported result merge failed for {task_id}: {_e}")
    completed_at = datetime.utcnow().isoformat()
    duration = None
    try:
        s = datetime.fromisoformat(started_at)
        e = datetime.fromisoformat(completed_at)
        duration = round((e - s).total_seconds(), 1)
    except Exception:
        pass

    # 有产出 → 更新该 event（写入配置 + 结果 + 结论 + 执行日志）
    # exit_code < 0 表示进程被信号终止（如 -15=SIGTERM, -9=SIGKILL），
    # 但 has_output=True 说明 Agent 已完成业务产出（PR/测试/评论/结构化 conclusion），
    # 信号终止常发生在引擎完成输出后的进程清理阶段，不应判为失败。
    if exit_code == 0 or (has_output and exit_code < 0):
        new_status = "completed"
        # R21.7.1: agent 完成后立即更新 devops_agent_session status='idle'
        # 缩小 session 兜底误拦窗口（R21.7.4: status=idle → 兜底查询不命中 → 无误拦）
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE devops_agent_session SET status='idle' "
                    "WHERE repo_key=? AND status='active'",
                    (repo_key,)
                )
                db.commit()
        except Exception:
            pass
        if exit_code < 0:
            log.info(f"Exec agent: seq={seq} exit_code={exit_code} but has_output=True, mark completed (signal death after work done)")
        # ── 假完成硬门槛（issue-P5，关卡B）──
        # 引擎 exit 0 不再是成功的充分条件：devops 类任务（issue/coding/qa/sre）还须有
        # 可信产出证据之一，否则降级 failed（防模型流挂死/空 end_turn 静默收场标绿）：
        #   证据1 self_reported==1 且有 result_text —— grape tasks complete 服务端盖章自报；
        #   证据2 执行日志（tool_uses，实际执行过的命令）中有真实写操作：
        #         POST .../comments（评论）/ POST .../pulls（PR）/ POST /api/a2a（A2A交棒）。
        # 注意只看执行证据，不看 agent 文字自述（思考里"打算交棒"不算做过）。
        # vod 等非 devops agent（智能推荐路径）不加门槛，避免误伤。
        if exit_code == 0 and agent_type in ("issue", "coding", "qa", "sre") and not has_output:
            _self_ok = False
            if task_id:
                try:
                    from src.runtime.service import get_task as _gt
                    _trow = _gt(task_id) or {}
                    _self_ok = (_trow.get("self_reported") == 1 and bool(_trow.get("result_text")))
                except Exception:
                    _self_ok = False
            _write_action = False
            if not _self_ok:
                for _tu in (tool_uses or []):
                    _cmd = _tu.get("cmd", "") if isinstance(_tu, dict) else str(_tu)
                    _cmd = _extract_tool_command(_tu) if (not isinstance(_cmd, str) or "POST" not in _cmd.upper()) else _cmd
                    _up = _cmd.upper()
                    # A2A 有两种形态：直接 curl POST /api/a2a，或 grape CLI 的 `grape a2a handoff`
                    # （CLI 内部才发 POST，命令字符串里没有 POST 关键字，须单独匹配）
                    if (("POST" in _up and any(k in _up for k in ("/COMMENTS", "/PULLS", "MERGE_REQUESTS")))
                            or ("A2A HANDOFF" in _up) or ("POST" in _up and "/API/A2A" in _up)):
                        _write_action = True
                        break
            if not (_self_ok or _write_action):
                new_status = "failed"
                log.warning(f"Exec agent: seq={seq} exit_code=0 but no verified output "
                            f"(self_reported={_self_ok}, write_action={_write_action}), "
                            f"demoting to failed (fake completion guard)")
    else:
        new_status = "failed"
    # R20.8a.1: direct 模式 401 标记——不经过 fail_task，需在此补充标记
    # 基于 done 事件的 errors/stderr 字段判断，不扫 final_text
    if exit_code != 0 and repo_key:
        _exec_err_detail = " ".join(str(e) for e in _exec_done_errors) + " " + str(_exec_done_stderr)
        if _exec_err_detail.strip():
            _err_lower = _exec_err_detail.lower()
            from src.token_service import is_auth_401_error
            _is_401 = is_auth_401_error(_err_lower)
            if _is_401:
                try:
                    from src.token_service import detect_and_mark_expired
                    _pipeline_error_code = detect_and_mark_expired(repo_key) or ""
                    if _pipeline_error_code:
                        log.warning(f"Exec agent: token expired during direct exec (error_code={_pipeline_error_code}), repo={repo_key}")
                except Exception as _pe:
                    log.warning(f"Exec agent: post-exec 401 detect failed: {_pe}")
    # Issue #403：瞬时上游抖动自动重试（判定层收口，direct 执行路径）。
    # new_status 判定完成后、终态写入（任务+事件）之前拦截：可重试（瞬时分类
    # ∧ 无写操作证据 ∧ attempts 未耗尽）则退避 requeue 并跳过终态写入。
    _retry_info = {}
    if new_status == "failed" and task_id:
        try:
            from src.runtime.transient_retry import retry_transient_task
            _err_txt = " ".join(str(e) for e in (_exec_done_errors or [])) + " " + str(_exec_done_stderr or "")
            if retry_transient_task(task_id, exit_code, _err_txt, has_output=has_output,
                                    tool_uses=tool_uses, result_text=result_text or ""):
                log.info(f"Exec agent: seq={seq} transient retry scheduled, skip terminal write")
                return
        except Exception as _e:
            log.warning(f"Exec agent: transient retry check failed for seq={seq}: {_e}")
    # Issue #403：耗尽/不可重试判死 → 分类失败摘要写 result_text（区分上游抖动
    # vs 配置错误 + 重试次数 + 原始错误摘要）。非重试场景（无 #403 记录）零变化。
    if new_status == "failed" and task_id and not (result_text or "").strip():
        try:
            from src.runtime.service import _task_errors_json
            from src.runtime.transient_retry import format_stream_failure_result
            _retries = [e for e in _task_errors_json(task_id)
                        if isinstance(e, dict) and e.get("error_code") in
                        ("TRANSIENT_RETRY_EXHAUSTED", "TRANSIENT_RETRY")]
            if _retries:
                _ex = next((e for e in _retries
                            if e.get("error_code") == "TRANSIENT_RETRY_EXHAUSTED"), _retries[-1])
                _retry_info = {"class": _ex.get("error_class") or "",
                               "attempts": _ex.get("attempts") or len(_retries),
                               "max": _ex.get("max") or len(_retries)}
                result_text = format_stream_failure_result(
                    _retry_info["class"], _retry_info["attempts"], _retry_info["max"],
                    _ex.get("reason") or "", exit_code=exit_code,
                    errors=_exec_done_errors)
        except Exception as _e:
            log.warning(f"Exec agent: failure summary build failed for seq={seq}: {_e}")
    # Issue #494：失败收口归因双写——done 事件的 errors 必须落 agent_tasks.errors
    # （旧路径只写 status/exit_code/result_text，超时失败 errors 与 stderr 双空，
    # 用户只看到 failed/exit_code=-1 无任何原因）。同时 result_text 为空时构造
    # 可读失败摘要透出到事件 result（前端失败原因卡片数据源）。
    _fail_errors_json = None
    if new_status == "failed" and _exec_done_errors:
        try:
            from src.agent.failure_attribution import failure_summary
            if not (result_text or "").strip():
                result_text = failure_summary(_exec_done_errors, exit_code)
        except Exception as _e:
            log.warning(f"Exec agent: failure attribution summary failed for seq={seq}: {_e}")
            if not (result_text or "").strip():
                result_text = f"任务失败（exit_code={exit_code}）"
        # 合并而非覆盖：保留 #403 transient retry 等已写入的诊断记录
        try:
            import json
            _merged = []
            if task_id:
                from src.runtime.service import _task_errors_json
                _merged = [e for e in (_task_errors_json(task_id) or [])
                           if str(e).strip()]
            for _e in _exec_done_errors:
                _s = str(_e).strip()[:500]
                if _s and _s not in _merged:
                    _merged.append(_s)
            if _merged:
                _fail_errors_json = json.dumps(_merged)
        except Exception as _e:
            log.warning(f"Exec agent: errors merge for seq={seq} failed: {_e}")
    if task_id:
        from src.runtime.service import update_task_status
        if _fail_errors_json is not None:
            update_task_status(task_id, new_status, exit_code=exit_code,
                               result_text=result_text, errors=_fail_errors_json)
        else:
            update_task_status(task_id, new_status, exit_code=exit_code, result_text=result_text)
    else:
        _update_direct_task(seq, new_status, exit_code=exit_code,
                            result_text=result_text, errors=_fail_errors_json)
    update_event_config(seq, inst_name, skill_name, started_at)
    update_event_complete(seq, new_status, exit_code=exit_code, duration=duration,
                          agent_logs=agent_logs, completed_at=completed_at, result=result_text)
    log.info(f"Exec agent: seq={seq} -> {new_status}, duration={duration}s, logs={len(agent_logs)}, result={result_text[:80]}\n")

    # Issue #242: coding agent 成功完成后，向关联仓库提交 PR（direct 模式）
    # 遍历 GRAPE_LINKED_WORKTREES，对每个有改动的关联仓库 worktree 执行 push + create_pr_for_repo
    if new_status == "completed" and agent_type == "coding" and env_extra.get("GRAPE_LINKED_WORKTREES"):
        try:
            _submit_direct_linked_prs(env_extra, repo_key, issue_id, repo_gitcode_token,
                                      task_id=task_id)
        except Exception as _le:
            log.warning(f"Exec agent: direct linked PR submit failed: {_le}")

    # 多任务模式：父任务 Issue Agent 完成后 → stage=done（不 A2A coding，子任务由 webhook 自动触发）
    if new_status == "completed" and agent_type == "issue" and issue_id:
        try:
            from src.api.session_store import is_subtask_mode_enabled, set_issue_stage
            if is_subtask_mode_enabled(repo_key, issue_id):
                set_issue_stage(repo_key, issue_id, "done", "issue")
                log.info(f"Multi-task mode: parent issue {issue_id} stage=done (subtasks will trigger independently)")
        except Exception as _e:
            log.warning(f"Multi-task mode stage=done check failed: {_e}")

    # 执行完成 → 自动回帖到 Issue（仅 sre 等有实质产出的 Agent，且有关联 Issue）
    # PR 订阅的伪 issue_id（#pr_123_scene_id）不是真实 Issue，跳过 Issue 回帖
    if new_status == "completed" and result_text and issue_id and not evt.get("_pr_sub_pr_number"):
        _issue_num = _issue_num_from(issue_id)
        if _issue_num and repo_gitcode_token:
            try:
                _post_result_to_issue(repository, _issue_num, result_text, repo_gitcode_token, agent_type, repo_key,
                                      agent_result=True, task_id=task_id, seq=seq)
            except Exception as _pe:
                # R20.3.2: _post_result_to_issue 失败时 append RESULT_POST_FAILED
                log.warning(f"Exec agent: post result to issue failed: {_pe}")
                if task_id:
                    try:
                        from src.session_manager import db, db_lock
                        import json
                        with db_lock:
                            c = db.cursor()
                            c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
                            _row = c.fetchone()
                            _errs = []
                            if _row and _row[0]:
                                try:
                                    _errs = json.loads(_row[0])
                                    if not isinstance(_errs, list):
                                        _errs = [str(_errs)]
                                except (json.JSONDecodeError, TypeError):
                                    _errs = [str(_row[0])]
                            _has_rpf = any(isinstance(e, dict) and e.get("error_code") == "RESULT_POST_FAILED" for e in _errs)
                            if not _has_rpf:
                                _errs.append({"error_code": "RESULT_POST_FAILED", "message": "result posted but issue comment failed"})
                                # 首条保护式（同 UC7-3 家族）：头部诊断不被挤掉
                                _errs = _errs[:1] + _errs[1:][-4:] if len(_errs) > 5 else _errs
                                c.execute("UPDATE agent_tasks SET errors=? WHERE id=?", (json.dumps(_errs), task_id))
                                db.commit()
                    except Exception:
                        pass
                # R20.3.3: 回帖失败不影响 task 状态（task 仍标记 completed）
    # PR 订阅场景 → 回帖到 PR 评论
    if new_status == "completed" and result_text and evt.get("_pr_sub_pr_number"):
        _pr_num = str(evt["_pr_sub_pr_number"])
        if _pr_num and repo_gitcode_token:
            _post_result_to_pr(repository, _pr_num, result_text, repo_gitcode_token, agent_type, repo_key,
                               agent_result=True, task_id=task_id, seq=seq)

    # SRE 完成后 → 从 result_text 提取部署 URL 并写入 managed_repos.deploy_url
    # Issue #468: 登记逻辑抽为共享函数 register_sre_deploy_url，与远程 worker
    # complete_task() 收尾路径统一调用（根治双路径钩子清单不同步缺陷）
    if new_status == "completed" and agent_type == "sre" and result_text:
        try:
            register_sre_deploy_url(repo_key, result_text)
        except Exception as _e:
            log.warning(f"SRE deploy_url extraction failed: {_e}")
        # Issue #482: 场景级登记——按 issue 绑定 scene_id 更新
        # devops_pipeline_scene.deploy_url（无绑定/失败不影响收尾主流程）
        try:
            register_scene_deploy_url(repo_key, issue_id, result_text)
        except Exception as _e:
            log.warning(f"scene deploy_url register failed: {_e}")

    # Issue #51: SRE（最后节点）完成后 → 设 stage=pending_review 等待开发者审核
    if new_status == "completed" and agent_type == "sre" and issue_id:
        try:
            from src.api.session_store import get_issue_stage, set_issue_stage
            _st = get_issue_stage(repo_key, issue_id)
            _cur_stage = (_st or {}).get("stage", "") if _st else ""
            if _cur_stage == "sre":
                set_issue_stage(repo_key, issue_id, "pending_review", "sre")
                log.info(f"SRE completed → pending_review (awaiting user review) issue={issue_id}")
        except Exception as _e:
            log.warning(f"SRE → pending_review failed: {_e}")

    # 子任务 SRE 完成后 → 登记完成情况到父 Issue
    if new_status == "completed" and agent_type == "sre" and issue_id:
        try:
            from src.api.session_store import get_parent_issue, get_subtask_issues
            _parent = get_parent_issue(repo_key, issue_id)
            if _parent and repo_gitcode_token:
                _parent_num = _issue_num_from(_parent)
                _sub_num = _issue_num_from(issue_id)
                if _parent_num and _sub_num:
                    _post_subtask_completion_to_parent(repository, _sub_num, _parent_num, result_text, repo_gitcode_token, repo_key=repo_key)
                    # 检查所有子任务是否全部完成
                    _subs = get_subtask_issues(repo_key, _parent)
                    _all_done = all(s.get("stage") == "done" or s.get("issue_id") == issue_id for s in _subs)
                    if _all_done and len(_subs) > 0:
                        _post_all_subtasks_done(repository, _parent_num, _subs, repo_gitcode_token, repo_key=repo_key)
        except Exception as _e:
            log.warning(f"Subtask completion post failed: {_e}")

    # 执行完成 → 同步 agent 结果到任务会话（用户「去对话」可见 agent 澄清/报告）
    # 独立于 _post_result_to_issue：agent 可能自己用 curl 发了 Issue 评论，
    # 但 task conversation 仍需写入才能在 Chat 页展示。
    if new_status == "completed" and result_text and issue_id:
        _issue_num = _issue_num_from(issue_id)
        if _issue_num:
            try:
                from src.repo_key import to_repo_key
                from src.session_manager import db as _db, db_lock as _dl, add_message as _am
                _rk = to_repo_key(repo_key)
                with _dl:
                    _c = _db.cursor()
                    _c.execute("SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
                               (_rk, _issue_num))
                    _row = _c.fetchone()
                if _row and _row[0]:
                    _am(_row[0], "agent", f"[{agent_type.upper()} Agent] {result_text[:3000]}")
                    log.info(f"Exec agent: synced result to task conversation {_row[0]} (issue #{_issue_num})")
            except Exception as _e:
                log.warning(f"Exec agent: sync to task conversation failed: {_e}")

    # 执行完成 → 从 GitCode Issue 拉取 agent 发的评论，同步到任务会话
    # （agent 通过技能 curl 直接在 Issue 上发的澄清追问等评论，上面 result_text 同步的是
    #   结构化输出摘要，不含 Issue 上的实际评论内容；此处补拉 Issue 评论去重写入会话）
    if new_status == "completed" and issue_id:
        _issue_num = _issue_num_from(issue_id)
        if _issue_num and repo_gitcode_token:
            _sync_issue_comments_to_conversation(repo_key, _issue_num, repo_gitcode_token, agent_type, repository)
