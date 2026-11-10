"""事件处理管道：归一化 → 路由 → 存储 → Agent 执行"""
import json, os, re, time, uuid, threading
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from src.config import log, WORKSPACE_DIR
from src.vcs.adapter import normalize_event, get_event_header
from src.api.routing import NON_ISSUE_EVENT_ROUTE, PR_DEFAULT_AGENT, ISSUE_EVENT_TYPES, PR_EVENT_TYPES
from src.api.event_store import get_issue_summary
from src.session_manager import db, db_lock
from src.token_service import is_auth_401_error

# P0 (Issue #596): _execute_agent 系列已迁移到 src.runtime.direct_executor
# 向后兼容 re-export（现有 import from event_pipeline 仍可工作）
from src.runtime.direct_executor import (
    _DIRECT_POOL, _DIRECT_FUTURES, _DIRECT_FUTURES_LOCK,
    start_direct_agent, _execute_agent, _execute_agent_inner,
)

_COMMENT_EVENT_TYPES = ("issue_comment", "note_hook_pr")


def _get_issue_creator(repo_key: str, issue_number: str) -> str:
    """Issue #1668: 读取 Issue 创建人登录名（issue.created_by，建档固化）。"""
    num = str(issue_number or "").strip().lstrip("#")
    if not repo_key or not num.isdigit():
        return ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT created_by FROM issue WHERE repo_key=? AND issue_number=? "
                "ORDER BY id LIMIT 1", (repo_key, int(num)))
            row = c.fetchone()
        return (row[0] or "").strip() if row else ""
    except Exception as e:
        log.warning(f"resolve issue creator failed repo={repo_key} issue=#{num}: {e}")
        return ""


def resolve_actor_account(evt: dict, repo_key: str = "") -> str:
    """Issue #1668: 统一解析 ACCOUNT/actor（a2a_event 兜底链）。

    优先级：a2a_event 为 payload.account → evt.actor → Issue creator；
    其余事件为 evt.actor → payload.account。
    解析为空时输出结构化告警（event_type/issue_number），**禁止回退仓库属主**。
    结果不写入 evt 缓存：Issue 建档前解析为空不应被永久缓存（下次调用会重新查库）。
    """
    if not isinstance(evt, dict):
        evt = {}
    payload = evt.get("payload") if isinstance(evt.get("payload"), dict) else {}
    event_type = str(evt.get("type") or evt.get("event_type") or "")
    issue_number = str(
        payload.get("issue_number")
        or evt.get("issue_id")
        or evt.get("issue_number")
        or ""
    ).strip().lstrip("#")
    if event_type == "a2a_event":
        # a2a_event 不使用 evt.account（外部归一化可能回退成仓库属主），
        # 严格按 payload.account → actor → Issue creator 解析。
        account = str(payload.get("account") or evt.get("actor") or "").strip()
        if not account:
            account = _get_issue_creator(repo_key, issue_number)
        fallback = "payload.account->actor->issue_creator"
    else:
        account = str(evt.get("actor") or payload.get("account") or "").strip()
        fallback = "actor->payload.account"
    if not account:
        log.warning(
            "ACCOUNT_MISSING "
            + json.dumps({
                "event_type": event_type or "unknown",
                "issue_number": issue_number,
                "repository": evt.get("repository", ""),
                "repo_key": repo_key,
                "fallback": fallback,
                "owner_fallback": False,
            }, ensure_ascii=False)
        )
    return account


def _a2a_actor_for(evt: dict, issue_id: str, repo_key: str) -> str:
    """Issue #1668: 解析派生 a2a_event 的触发人账号。

    订阅 / PR 订阅 / VOD 重派统一走此入口：以派生 issue_id 构造临时 a2a_event，
    按 payload.account → actor → Issue creator 解析，避免三处内联构造重复与优先级漂移。
    """
    payload = evt.get("payload") if isinstance(evt.get("payload"), dict) else {}
    return resolve_actor_account(
        {"type": "a2a_event", "actor": evt.get("actor", ""),
         "issue_id": issue_id, "payload": payload},
        repo_key,
    )


def _payload_with_account(payload, account: str) -> dict:
    """Issue #1668: 复制 payload 并回填解析后的 account（落库与事件 actor 保持一致）。"""
    out = dict(payload) if isinstance(payload, dict) else {}
    out["account"] = account
    return out


def _get_task_repo_token(repo_key: str) -> str:
    """Issue #372 R2.4: 获取 task 行 repo_gitcode_token，区分 token 来源。

    Bot token（路径 A/B/C，不过期）→ 安全写入 task 行。
    用户 OAuth token（路径 D，会过期）→ 返回空，由 _get_task_token 运行时实时获取。

    优化：用 TokenResolver.resolve() 一次调用同时拿到 token + level，
    不再分别调 get_repo_token_with_refresh + _is_user_token_repo（两者内部重复查前三级 + get_host_user_id）。
    """
    if not repo_key:
        return ""
    try:
        from src.token_resolver import TokenResolver, create_provider_from_repo_key
        provider = create_provider_from_repo_key(repo_key)
        result = TokenResolver(provider).resolve(repo_key)
        # level == "user" → 路径 D 用户 token，不写入 task 行
        if result.token and result.level != "user":
            return result.token
        return ""  # 用户 token 或空，运行时实时获取
    except Exception as e:
        log.warning(f"_get_task_repo_token: resolve failed: {e}")
        return ""


def check_running_task(repo_key: str, issue_id: str) -> bool:
    """R20.1.8/R20.10.5: 检查该 issue 是否有正在执行的 task。

    两级查询：
    1. agent_tasks.status=running/dispatched（webhook pipeline 触发的 task）
    2. devops_agent_session.last_active_at > now-60s AND status=active
       （飞书 VOD / Chat direct 等非 webhook 执行场景，R21.7.2 窗口 60s）
    """
    if not repo_key or not issue_id:
        # R21.6.5: issue_id 为空时只走 session 兜底
        if not repo_key:
            return False
        return _check_session_active(repo_key)
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # 1. 查 agent_tasks
            # 'queued' 计入活跃（#403 重试收口）：退避窗口内任务回 queued，
            # 若不查 queued，同 issue 新事件会建第二个任务 → 旧重试任务稍后
            # 被认领 → 双实例并发、双份评论。语义 =「该 issue 有任务在链上」。
            c.execute(
                "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? "
                "AND status IN ('running','dispatched','queued') LIMIT 1",
                (repo_key, issue_id)
            )
            if c.fetchone():
                return True
        # 2. session 兜底
        return _check_session_active(repo_key)
    except Exception as e:
        log.warning(f"check_running_task: query failed: {e}")
        return False


def _check_session_active(repo_key: str) -> bool:
    """R20.10.5/R21.7.2: session 兜底查询——devops_agent_session 最近 60s 活跃。

    N1b 修复：原 `> datetime('now', '-60 seconds')` 是 SQLite 私有函数变体，
    翻译层不覆盖（MySQL 1064 被 except 吞掉 → 恒 False）。改为应用层预计算
    cutoff + 标准参数化 SQL，双方言行为一致（时间串 UTC "%Y-%m-%d %H:%M:%S"，
    与 datetime('now') 写入格式对齐，可字典序比较）。
    """
    import time as _time
    _cutoff = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(_time.time() - 60))
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT 1 FROM devops_agent_session WHERE repo_key=? "
                "AND status='active' AND last_active_at > ? LIMIT 1",
                (repo_key, _cutoff)
            )
            return c.fetchone() is not None
    except Exception:
        return False


def check_developer_command(note_content: str) -> bool:
    """R20.1.2: 检查评论内容是否含开发者指令（/done /cancel /resume 等）。"""
    note_lower = (note_content or "").lower().strip()
    return any(kw in note_lower for kw in [
        "/done", "完成任务", "任务完成", "确认完成",
        "/cancel", "取消任务",
        "/resume", "恢复任务"
    ])


def _is_path_b_self_loop(repo_key: str, actor: str, event_type: str, issue_id: str) -> bool:
    """R20.1.1: 路径 B 自循环检测——四条件组合判断（fallback 路径）。

    主路径走 handler.is_self_loop_actor()，本函数是 handler 调用失败时的 fallback。

    1. 仓库是路径 B（level == "user"）
    2. actor == host_vcs_username
    3. event_type in (issue_comment, note_hook_pr, issue_updated)
    4. 该 issue 有正在执行的 task

    优化：用 TokenResolver.resolve_config_level() 判断路径 B（一次 _fetch 链），
    不再调 _is_user_token_repo / _is_github_user_token_repo（两者各自独立查前三级 + host_user_id）。
    """
    if not actor or not repo_key:
        return False
    # 条件 3: event_type 检查
    if event_type not in ("issue_comment", "note_hook_pr", "issue_updated"):
        return False
    # 条件 1: 路径 B 仓库（level == "user"）
    try:
        from src.token_resolver import TokenResolver, create_provider_from_repo_key
        _provider = create_provider_from_repo_key(repo_key)
        _config_result = TokenResolver(_provider).resolve_config_level(repo_key)
        if _config_result.level != "user":
            return False
    except Exception:
        return False  # 查询失败降级为不检测（R20.1.7）
    # 条件 2: actor == host_vcs_username
    from src.user.db import get_host_vcs_username
    host_vcs_username = get_host_vcs_username(repo_key)
    if not host_vcs_username:
        return False  # R20.1.7: 查询失败降级为不检测
    if actor != host_vcs_username:
        return False  # actor 不是 host_user → 不是自循环
    # 条件 4: 该 issue 有正在执行的 task
    return check_running_task(repo_key, issue_id)


# Issue #565: bot 建单智能推荐仓级滑动窗口限流（进程内实现，不进 DB）
_BOT_OPEN_RECOMMEND_LIMIT = 30
_BOT_OPEN_RECOMMEND_WINDOW_SECONDS = 60.0
_BOT_OPEN_RECOMMEND_WINDOWS: dict = {}


def _bot_open_recommend_rate_limited(repo_key: str) -> bool:
    """同 repo_key 60s 窗口内 bot 建单推荐超过 _BOT_OPEN_RECOMMEND_LIMIT 条 → True（超限不计数）。

    进程内 dict（repo_key → 时间戳列表）：webhook 去重层挡重投，重启丢失可接受。
    """
    import time as _time
    _now = _time.monotonic()
    _window = [t for t in (_BOT_OPEN_RECOMMEND_WINDOWS.get(repo_key) or [])
               if _now - t < _BOT_OPEN_RECOMMEND_WINDOW_SECONDS]
    if len(_window) >= _BOT_OPEN_RECOMMEND_LIMIT:
        _BOT_OPEN_RECOMMEND_WINDOWS[repo_key] = _window
        return True
    _window.append(_now)
    _BOT_OPEN_RECOMMEND_WINDOWS[repo_key] = _window
    return False


def _fail_scene_all_offline(seq: int, scene_id: str, agent_type: str, context: str = "",
                            repo_key: str = ""):
    """Issue #259 Optional：场景引擎全灭时统一置事件 failed 并告警（5 处调用点共用）。

    调用方在 return 前调用本 helper——helper 负责 log + fail，调用方负责 return / return False。

    引擎绑定真实性同步（docs/weekly-06/engine-binding-truth-sync-design.md §4.4）：
    原裸 update_event_status(seq, "failed") 无原因落库，排障需倒查日志。
    现改走 _fail_event 同款通路：人话原因写 events.result + agent_tasks.errors，
    前端 observability/issues 点开即见。文案含引擎名（engine_name 反查）。
    repo_key 必传（调用方都有）：仓库级场景查询依赖它，缺失时 get_pipeline_config
    会回退官方模板导致引擎名查不到（卡点①修复）。
    """
    _ctx = context or "Pipeline"
    log.warning(f"{_ctx}: scene={scene_id} {agent_type} engines all offline, skip (Issue #259)")
    # 反查场景绑定的引擎名列表，拼人话
    _engine_names = []
    try:
        from src.api.pipeline_config import get_scene_engine_ids
        _eids = get_scene_engine_ids(scene_id, agent_type, repo_key=repo_key) or []
        if _eids:
            from src.runtime.engine_select import engine_name as _en
            _seen = set()
            for _eid in _eids:
                _nm = _en(int(_eid))
                if _nm and _nm not in _seen:
                    _seen.add(_nm)
                    _engine_names.append(_nm)
    except Exception as _e:
        log.warning(f"_fail_scene_all_offline: engine name lookup failed: {_e}")
    if _engine_names:
        _reason = (f"绑定的 {len(_engine_names)} 台引擎（{', '.join(_engine_names)}）全部离线，"
                   f"任务无法派发（scene={scene_id}, agent={agent_type}）")
    else:
        # Issue #541：无引擎名可反查 → 用统一原因判定区分「场景仓内不存在 /
        # 场景未绑定引擎 / 引擎全部离线」，不再笼统报「全部离线」
        _reason = f"场景绑定的引擎全部离线，任务无法派发（scene={scene_id}, agent={agent_type}）"
        try:
            from src.runtime.engine_select import scene_dispatch_failure_reason
            _reason = scene_dispatch_failure_reason(scene_id, agent_type, repo_key=repo_key) or _reason
        except Exception as _e541:
            log.warning(f"_fail_scene_all_offline: failure reason lookup failed: {_e541}")
    _fail_event(seq, _reason)


def _notify_scene_precheck_failure(repo_key: str, issue_id: str, problems: list,
                                   trace_id: str = "") -> None:
    """Issue #1245: webhook 侧场景预校验失败 → 在 Issue 评论原因（best-effort）。"""
    num = _issue_num_from(issue_id)
    if not num:
        return
    lines = ["场景可用性预校验未通过，已跳过流水线启动（Issue 已正常创建，无需重复建单）。", ""]
    for p in (problems or [])[:8]:
        node = p.get("node_tag") or "-"
        code = p.get("reason_code") or ""
        msg = p.get("message") or ""
        suggestion = p.get("suggestion") or ""
        lines.append(f"- 节点 `{node}`：{msg}（reason_code: {code}）")
        if suggestion:
            lines.append(f"  - 建议：{suggestion}")
    if trace_id:
        lines.append("")
        lines.append(f"trace_id: {trace_id}")
    lines.append("")
    lines.append("修复场景配置或启动引擎后重新触发即可。")
    body = "\n".join(lines)
    try:
        from src.repo_key import split_owner_repo
        from src.token_service import get_repo_bot_token
        owner, rname = split_owner_repo(repo_key)
        token = get_repo_bot_token(repo_key) or ""
        if not token:
            log.warning(f"scene_precheck notify: no bot token for {repo_key}, skip comment")
            return
        if _is_github_repo(repo_key):
            _github_post_issue_comment(owner, rname, str(num), body, token)
        else:
            _gitcode_post_issue_comment_ledgered(f"{owner}/{rname}", num, body, token,
                                                 repo_key=repo_key)
        log.info(f"scene_precheck notify: posted reason comment to {repo_key}#{num}")
    except Exception as e:
        log.warning(f"scene_precheck notify: comment failed for {repo_key}#{num}: {e}")


def _scene_qa_skill(repo_key: str, scene_id: str, matches: list) -> str:
    """取指定场景 QA 节点的 skill：优先从订阅匹配列表取，查不到再读场景节点表。"""
    for m in matches:
        if m[0] == scene_id and m[1]:
            return m[1]
    try:
        import json as _json
        with db_lock:
            c = db.cursor()
            c.execute("SELECT nodes_json FROM devops_pipeline_scene WHERE repo_key=? AND scene_id=?",
                      (repo_key, scene_id))
            row = c.fetchone()
        if row:
            nodes = _json.loads(row[0] or "[]")
            qa_node = next((n for n in nodes if n.get("tag") == "qa" and n.get("enabled", True)), None)
            if qa_node:
                return qa_node.get("skill", "") or ""
    except Exception:
        pass
    return ""


def _filter_pr_sub_matches(matches: list, issue_scene_id: str, bound_issue: bool = False,
                           issue_qa_skill: str = "") -> list:
    """PR 订阅匹配结果过滤（Issue #426 修复口径 + skill 级去重口径）：
    1. 仅当 PR 绑定了 Issue（bound_issue=True）时，
       排除 PR 关联 Issue 所在场景（绑 Issue 即问题已被他人解决，该场景无需再被订阅唤醒）；
       其余情况订阅场景不再因绑定关系被排除
    2. bound_issue=True 且 issue_qa_skill 非空时，排除与 Issue 场景同 skill 的场景
       （同 skill 审查重复；#426 后补）
    3. 剩余场景中 qa_skill 相同的只保留第一个（同 skill 重复审查浪费资源，#318 保留）

    matches: [(scene_id, qa_skill, qa_engine_ids, sre_skill, sre_engine_ids), ...]
    issue_scene_id: PR 关联 Issue 所在场景（空=无关联 Issue）
    bound_issue: PR 是否绑定了关联 Issue
    issue_qa_skill: Issue 所在场景 QA 节点的 skill（仅 bound_issue=True 时使用）
    """
    # 1. PR 绑定了 Issue 时排除 Issue 所在场景
    if bound_issue and issue_scene_id:
        matches = [m for m in matches if m[0] != issue_scene_id]
    # 2. PR 绑定了 Issue 时排除同 qa_skill 场景
    if bound_issue and issue_qa_skill:
        matches = [m for m in matches if m[1] != issue_qa_skill]
    # 3. 相同 qa_skill 只保留第一个
    seen_skills = set()
    deduped = []
    for m in matches:
        if m[1] not in seen_skills:
            seen_skills.add(m[1])
            deduped.append(m)
    return deduped


def _match_pr_subscription(repo_key: str, evt: dict, event_type: str, pr_branch: str) -> list:
    """PR 无关联 Issue 时，按 PR target_branch 匹配场景 work_branch，唤醒 QA agent。

    支持多个场景命中同一 PR（多 QA 并行审查）。
    返回 [(scene_id, qa_skill, qa_engine_ids, sre_skill, sre_engine_ids), ...] 匹配列表。
    """
    if not pr_branch:
        return []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT scene_id, nodes_json, advanced_options FROM devops_pipeline_scene "
                "WHERE repo_key=? AND pr_subscription_enabled=1 AND is_active=1",
                (repo_key,))
            rows = c.fetchall()
        matches = []
        for scene_id, nodes_json, adv_opts_json in rows:
            # 匹配 work_branch
            import json as _json
            try:
                adv_opts = _json.loads(adv_opts_json or "{}")
            except Exception:
                adv_opts = {}
            work_branch = adv_opts.get("work_branch", "main") or "main"
            if work_branch != pr_branch:
                continue
            nodes = json.loads(nodes_json or "[]")
            qa_node = next((n for n in nodes if n.get("tag") == "qa" and n.get("enabled", True)), None)
            if not qa_node or not qa_node.get("skill"):
                continue
            sre_node = next((n for n in nodes if n.get("tag") == "sre" and n.get("enabled", True)), None)
            sre_skill = sre_node.get("skill", "") if sre_node else ""
            sre_engine_ids = sre_node.get("engine_runtime_ids", []) if sre_node else []
            matches.append((
                scene_id, qa_node["skill"], qa_node.get("engine_runtime_ids", []),
                sre_skill, sre_engine_ids
            ))
        return matches
    except Exception as e:
        log.warning(f"Pipeline: PR subscription match failed: {e}")
        return []


def is_plugin_mode() -> bool:
    """Check if pipeline is in plugin mode.

    plugin mode: uses pipelines.json + tag-based enqueue/claim
    legacy mode: uses agent-skill-mapping.json + runtime_id binding

    Priority: meta table `pipeline_mode` > env var GRAPE_PIPELINE_MODE > default (plugin)
    """
    try:
        from src.user.db import get_meta_value
        _mode = (get_meta_value("pipeline_mode") or "").strip().lower()
        if _mode:
            return _mode == "plugin"
    except Exception:
        pass
    return os.environ.get("GRAPE_PIPELINE_MODE", "plugin").lower() == "plugin"


def resolve_skill_from_pipeline(repo_key: str, agent_type: str, scene_id: str = "", user_id: int = 0) -> str:
    """Look up skill name from DB (plugin mode).

    scene_id 非空时优先使用（Issue 级场景），否则回退到仓库默认场景（managed_repos.dev_scene）。
    issue-39 方案 B：无「default」场景——仓库无场景时 scene_id 为空，返回空技能。
    user_id > 0 时可查用户级场景。
    Returns skill name or empty string.
    """
    try:
        from src.api.pipeline_config import get_node_by_tag
        if not scene_id:
            return ""
        node = get_node_by_tag(scene_id, agent_type, repo_key=repo_key, user_id=user_id)
        if node:
            return node.get("skill", "")
    except Exception as e:
        log.warning(f"resolve_skill_from_pipeline failed: {e}")
    return ""


def resolve_scene_node_skill_id(repo_key: str, agent_type: str,
                                scene_id: str = "", user_id: int = 0) -> int:
    """读场景节点的 skill_id（Issue #979 权威派发标识）。

    节点仅存 skill_name（老场景）或节点缺失 → 返回 0（调用方按名字降级）。
    """
    try:
        from src.api.pipeline_config import get_node_by_tag
        if not scene_id:
            return 0
        node = get_node_by_tag(scene_id, agent_type, repo_key=repo_key, user_id=user_id)
        if node:
            return int(node.get("skill_id") or 0)
    except Exception as e:
        log.warning(f"resolve_scene_node_skill_id failed: {e}")
    return 0


def _task_insert_row(_cols, **kw):
    """按 agent_tasks 列清单顺序组装 INSERT 值。

    以列清单为唯一来源，缺列即抛错——防止列/值数目漂移
    （历史事故：_TASK_COLUMNS_LIST 扩到 39 列后，两处字面量仍 37/36 值导致 INSERT 失败，
    事件卡 running 被 sweeper 置 failed，见 docs/new-version3/agent-tasks-direct-create-binding-fix-design.md）。
    """
    missing = [c for c in _cols if c not in kw]
    if missing:
        raise ValueError(f"agent_tasks INSERT 缺少列值: {missing}")
    return tuple(kw[c] for c in _cols)


def _create_direct_task(repo_key, seq, agent_type, backend, skill_name,
                        skill_path, engine_id, evt, issue_id="", skill_id=0,
                        skip_event_config=False):
    """direct 执行前创建 agent_tasks + 设 started_at，防 sweeper 误杀。

    skip_event_config=True 时跳过 update_event_config（autopilot 场景：不写 events 表）。
    """
    from src.runtime.service import _TASK_COLUMNS_LIST
    from src.api.session_store import get_or_create_agent_session
    from src.runtime.instances import runtime_instance_name
    from datetime import datetime, timezone

    inst_name = runtime_instance_name(engine_id) or backend or ""
    now = datetime.now(timezone.utc).isoformat()

    # 先设 started_at/runtime/skill，让 sweeper 的 no_start 检查跳过
    # autopilot 不写 events 表，跳过这步
    if not skip_event_config:
        from src.api.event_store import update_event_config
        update_event_config(seq, inst_name, skill_name, now)

    # 查 agent_session_id
    session = get_or_create_agent_session(repo_key, agent_type, backend or "")
    agent_session_id = session.get("id", "")
    if not agent_session_id:
        log.warning(f"create_direct_task: no agent_session_id for {repo_key}/{agent_type}")
        return ""

    # 更新 session 的 runtime_id 为实际执行引擎（pick_engine 结果可能与创建时不同）
    if engine_id:
        try:
            with db_lock:
                c2 = db.cursor()
                c2.execute("UPDATE devops_agent_session SET runtime_id=? WHERE id=?", (engine_id, agent_session_id))
                db.commit()
        except Exception:
            pass

    task_id = "t_" + uuid.uuid4().hex[:12]
    # issue_id 优先用传参，兜底从 evt 解析
    if not issue_id:
        issue_id = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "") or ""
    from src.repo_key import from_repo_key

    # 获取 bot token（按 provider 分流：GitHub 仓库用 get_github_token）
    repo_gitcode_token = _get_task_repo_token(repo_key)

    try:
        row = _task_insert_row(_TASK_COLUMNS_LIST,
            id=task_id, agent_session_id=agent_session_id, runtime_id=engine_id,
            event_seq=seq, issue_id=issue_id, repo_key=repo_key or "",
            agent_type=agent_type or "", skill=skill_name or "", skill_id=skill_id or 0,
            prompt="", cwd="", runtime_session_id="", event_type="", account="",
            user_id=0, repo_gitcode_token=repo_gitcode_token or "",
            env_extra="{}", skill_files="{}", server_url="", issue_session_id="",
            project_info_files="{}", project_info_meta="{}", knowledge_files="{}",
            project_info_file_urls="{}", knowledge_file_urls="{}", attempts=0,
            pool_agent_type="", affinity_runtime_id=0, pool_tag="", pool_engine_ids="",
            pool_engine_source="",
            status="queued", stream_ended=0, self_reported=0,
            exit_code=None, result_text="", errors="", stderr="",
            usage_json="{}", started_at=now, completed_at=None, created_at=now,
            pending_since=None,
            user_gitcode_token="",
            trace_context="")
        with db_lock:
            c = db.cursor()
            c.execute(
                f"INSERT INTO agent_tasks ({','.join(_TASK_COLUMNS_LIST)}) VALUES "
                f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})", row)
            db.commit()
        log.info(f"Direct task created: {task_id} seq={seq} engine={engine_id}({inst_name}) skill={skill_name}")
    except Exception as e:
        # 排障关键：这里是"任务行没落库"的唯一出口，必须 ERROR 级别带栈（2026-10-19 事故：
        # 缺 pool_engine_source 列值 → ValueError 被 warning 吞掉 → 排障断链）
        log.error(f"create_direct_task failed for seq={seq}（任务行未落库，事件将卡 running 被 sweeper 判 failed）: {e}",
                  exc_info=True)
        return ""
    # 方案 E：direct 任务创建即开跑 → 事件置 running（@skip_event_config 的 autopilot 不写 events 表，跳过）
    if seq and not skip_event_config:
        try:
            from src.api.event_store import update_event_status
            update_event_status(seq, "running")
        except Exception as _ee:
            log.warning(f"create_direct_task: set event {seq} running failed: {_ee}")
    return task_id


def _create_failed_task(repo_key, seq, agent_type, backend, skill_name,
                        skill_path, skill_id, evt, issue_id="", runtime_id=0):
    """引擎不可用时创建 failed 状态的 task 快照（供重派复用参数）。

    重派逻辑查 agent_tasks WHERE event_seq=?，无记录则报"无关联任务"。
    此函数确保引擎不可用时也有一条 failed task 记录，重派可走"终态→复用快照重建"路径。
    """
    from src.runtime.service import _TASK_COLUMNS_LIST
    from src.api.session_store import get_or_create_agent_session
    from datetime import datetime, timezone
    import os

    # 查 agent_session_id（重派需要）
    session = get_or_create_agent_session(repo_key, agent_type, backend or "")
    agent_session_id = session.get("id", "")
    if not agent_session_id:
        log.warning(f"create_failed_task: no agent_session_id for {repo_key}/{agent_type}")
        return

    task_id = "t_" + uuid.uuid4().hex[:12]
    if not issue_id:
        issue_id = evt.get("issue_id", "") or evt.get("payload", {}).get("issue_number", "") or ""

    # 构建 prompt（重派复用）
    prompt = ""
    try:
        from src.prompt_builder import build_devops_agent_prompt
        evt["repo_key"] = repo_key
        evt["issue_id"] = issue_id
        evt["_skill_path"] = skill_path
        evt["_skill_id"] = skill_id
        prompt = build_devops_agent_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
                              skill_path=skill_path, runtime_id=runtime_id, agent_type=agent_type)
    except Exception as e:
        log.warning(f"create_failed_task: prompt build failed: {e}")

    # cwd
    try:
        cwd = get_agent_workdir(repo_key, agent_type, issue_id)
        os.makedirs(cwd, exist_ok=True)
    except Exception:
        cwd = ""

    # env_extra
    repository = evt.get("repository", "")
    # Issue #1660：REPO_OWNER/REPO_NAME 一律由 REPO_FULL_NAME 派生（缺失回退 repo_key），
    # 禁止以触发人账号充当 REPO_OWNER；REPO_FULL_NAME 归一（剥 provider 前缀）供 runbook 拼 URL。
    from src.repo_key import derive_repo_identity as _derive
    _full_name, _owner, _repo_name = _derive(repository, repo_key)
    env_extra = {
        "REPO_FULL_NAME": _full_name,
        "REPO_OWNER": _owner,
        "REPO_NAME": _repo_name,
        "ACCOUNT": resolve_actor_account(evt, repo_key),
    }

    # repo_gitcode_token（按 provider 分流，用户 token 写空由运行时实时获取）
    repo_gitcode_token = _get_task_repo_token(repo_key)

    now = datetime.now(timezone.utc).isoformat()
    _reason = json.dumps(["无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎"][:500])
    _pool_tag = agent_type or "" if is_plugin_mode() else ""
    try:
        row = _task_insert_row(_TASK_COLUMNS_LIST,
            id=task_id, agent_session_id=agent_session_id, runtime_id=runtime_id,
            event_seq=seq, issue_id=issue_id, repo_key=repo_key or "",
            agent_type=agent_type or "", skill=skill_name or "", skill_id=skill_id or 0,
            prompt=prompt, cwd=cwd, runtime_session_id="",
            event_type=evt.get("type", "") or "",
            account=repository or evt.get("account", "") or "", user_id=0,
            repo_gitcode_token=repo_gitcode_token,
            env_extra=json.dumps(env_extra), skill_files="{}", server_url="", issue_session_id="",
            project_info_files="{}", project_info_meta="{}", knowledge_files="{}",
            project_info_file_urls="{}", knowledge_file_urls="{}", attempts=0,
            pool_agent_type=_pool_tag, affinity_runtime_id=0, pool_tag=_pool_tag, pool_engine_ids="",
            pool_engine_source="",
            status="failed", stream_ended=1, self_reported=0,
            exit_code=1, result_text="", errors=_reason, stderr="",
            usage_json="{}", started_at=now, completed_at=now, created_at=now,
            pending_since=None,
            user_gitcode_token="",
            trace_context="")
        with db_lock:
            c = db.cursor()
            c.execute(
                f"INSERT INTO agent_tasks ({','.join(_TASK_COLUMNS_LIST)}) VALUES "
                f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})", row)
            db.commit()
        log.info(f"Failed task snapshot created: {task_id} seq={seq} agent={agent_type} (engine_unavailable)")
    except Exception as e:
        # 排障关键：失败快照写不进去 = 重派无参数可复用 + 失败原因不可见（2026-10-19 事故 seq=6573/6557）
        log.error(f"create_failed_task failed for seq={seq}（失败快照未落库，重派与排障受影响）: {e}",
                  exc_info=True)


def _update_direct_task(seq, status, exit_code=0, result_text="", errors=None):
    """direct 执行完成后更新 agent_tasks 状态（通过 event_seq 反查）。

    Issue #494：失败时 errors（JSON 字符串）合并写入——旧路径不写 errors，
    超时失败任务详情零归因。None = 不动 errors 列（保持既有语义）。
    """
    try:
        from datetime import datetime, timezone
        with db_lock:
            c = db.cursor()
            if errors is not None:
                # 合并而非覆盖（与 _fail_event 的 COALESCE 口径不同：这里 done
                # errors 是权威增量，读取已有记录去重追加）
                c.execute(
                    "SELECT errors FROM agent_tasks WHERE event_seq=? "
                    "AND status IN ('queued','running','dispatched')",
                    (seq,))
                _row = c.fetchone()
                _merged = []
                if _row and _row[0]:
                    try:
                        _merged = [e for e in json.loads(_row[0])
                                   if isinstance(e, str) and e.strip()]
                    except (json.JSONDecodeError, TypeError):
                        _merged = [_row[0]]
                try:
                    _new = [str(e).strip()[:500] for e in json.loads(errors)
                            if str(e).strip()] if errors else []
                except (json.JSONDecodeError, TypeError):
                    _new = []
                for _e in _new:
                    if _e not in _merged:
                        _merged.append(_e)
                c.execute(
                    "UPDATE agent_tasks SET status=?, exit_code=?, result_text=?, "
                    "completed_at=?, errors=? "
                    "WHERE event_seq=? AND status IN ('queued','running','dispatched')",
                    (status, exit_code, result_text or "",
                     datetime.now(timezone.utc).isoformat(),
                     json.dumps(_merged) if _merged else errors, seq)
                )
            else:
                c.execute(
                    "UPDATE agent_tasks SET status=?, exit_code=?, result_text=?, completed_at=? "
                    "WHERE event_seq=? AND status IN ('queued','running','dispatched')",
                    (status, exit_code, result_text or "", datetime.now(timezone.utc).isoformat(), seq)
                )
            db.commit()
    except Exception as e:
        log.warning(f"update_direct_task failed for seq={seq}: {e}")


def cancel_direct_task(seq: int) -> bool:
    """取消排队/运行中的 direct 任务。Future 在队列中 → cancel；
    已在运行 → 杀掉对应的 opencode/hermes/codearts 子进程。"""
    cancelled = False
    with _DIRECT_FUTURES_LOCK:
        future = _DIRECT_FUTURES.pop(seq, None)
    if future is not None:
        if not future.done():
            cancelled = future.cancel()
            if cancelled:
                log.info(f"cancel_direct_task: seq={seq} removed from thread pool queue")
            else:
                # Future 已在运行中 → 杀掉对应子进程（按 seq 查 task 的 cwd + engine）
                log.info(f"cancel_direct_task: seq={seq} running, killing engine subprocess")
                try:
                    from src.session_manager import db, db_lock
                    with db_lock:
                        c = db.cursor()
                        c.execute("SELECT id, runtime_id, cwd, agent_type FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1", (seq,))
                        tr = c.fetchone()
                    if tr:
                        _task = {"id": tr[0], "runtime_id": tr[1], "cwd": tr[2], "agent_type": tr[3]}
                        # 推断 backend
                        _backend = "opencode"
                        try:
                            with db_lock:
                                c2 = db.cursor()
                                c2.execute("SELECT name FROM agent_runtimes WHERE id=?", (tr[1],))
                                rn = c2.fetchone()
                            if rn and "hermes" in (rn[0] or "").lower():
                                _backend = "hermes"
                            elif rn and "codearts" in (rn[0] or "").lower():
                                _backend = "codearts"
                        except Exception:
                            pass
                        from src.runtime.agent import _kill_stale_engine
                        _kill_stale_engine(_task, _backend)
                        cancelled = True
                except Exception as e:
                    log.warning(f"cancel_direct_task: kill process failed for seq={seq}: {e}")
        else:
            # Future 已完成，清理
            cancelled = True
    return cancelled


def get_agent_display_name(agent_type: str) -> str:
    """从 configs/agent-description.json 读取 Agent 显示名（Issue Agent / QA Agent / Coding Agent 等）。

    注入给 agent 的环境变量 AGENT_NAME 使用（skill 中 [$AGENT_NAME]）。
    未配置时返回 agent_type 本身。
    """
    try:
        _path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-description.json")
        if os.path.exists(_path):
            with open(_path, "r", encoding="utf-8") as _f:
                _data = json.load(_f)
            return (_data.get(agent_type) or {}).get("name", "") or agent_type
    except Exception as e:
        log.warning(f"get_agent_display_name failed for {agent_type}: {e}")
    return agent_type


def _is_pr_payload(payload: dict) -> bool:
    """判断是否为 PR 事件 payload（PR webhook 带 pull_request/merge_request 键）。

    Issue #89 修复：旧逻辑 `"issues" in payload` 会误判 Issue Hook（GitCode Issue Hook
    payload 可能含 issues 交叉引用键）为 PR 事件，导致 resolve_issue_id 跳过
    object_attributes 解析 → 子任务 issue_id=#0。
    改为：仅当有 pull_request/merge_request 键，或 object_attributes 含 PR 特有字段
    （source_branch/target_branch）时才判定为 PR。

    分工（Issue #1802）：本函数只服务 `resolve_issue_id`——决定 Issue 编号该从
    `object_attributes` 还是 PR 关联 issues 解析。为保证既有 issue_id 契约与 #89
    用例不变，这里对 `source_branch` 等结构键从宽判 PR。PR_NUMBER 的实体准入另由
    `_is_pr_entity_event` 负责（Issue 实体优先、已知 Issue 事件一律判 False），两者
    口径不同是有意的，请勿互相复用。
    """
    if bool(payload.get("pull_request")) or bool(payload.get("merge_request")):
        return True
    obj = payload.get("object_attributes", {})
    if isinstance(obj, dict):
        if obj.get("source_branch") or obj.get("target_branch"):
            return True
        # noteable_type 明确为 Issue → 非 PR
        if obj.get("noteable_type") == "Issue":
            return False
    return False


def resolve_issue_card_id(repo_key: str, issue_number) -> str:
    """需求卡片 id（issue 表主键）：awaiting-input 等 --issue_id 用（≠ Issue number）。查无返回空串。"""
    try:
        from src.session_manager import db as _db, db_lock as _dl
        _n = str(issue_number or "").lstrip("#")
        if not _n.isdigit():
            return ""
        with _dl:
            _r = _db.cursor().execute(
                "SELECT id FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                (repo_key, int(_n))).fetchone()
        return str(_r[0]) if _r else ""
    except Exception:
        return ""


def _resolve_bot_username(repo_key: str) -> str:
    """解析仓库 bot 用户名（Issue #573 抽公共 helper，三级兜底与原 coding 逻辑一致）。

    优先级：
    1. managed_repos.bot_username（repo 级，GitCodeRepoConfig 来源）；
    2. 全局 bot username（github 走 get_global_github_bot_username，其余 get_global_bot_usernames[0]）；
    3. Issue #372 路径 B：host_user 的平台用户名（external_login）。

    全部为空 → 返回空串（下游 skill 按空值降级，不注入占位值）。
    任何异常静默返回空串（与原注入块行为一致，不阻断任务派发）。
    """
    try:
        from src.session_manager import db, db_lock
        from src.repo_key import to_repo_key
        _rk = to_repo_key(repo_key)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT bot_username FROM managed_repos WHERE repo_key=? AND bot_username!='' LIMIT 1", (_rk,))
            row = c.fetchone()
            _bot_name = row[0] if row else ""
        if _bot_name:
            return _bot_name
        # Issue #345: 不用 repo owner 兜底——owner 不是 bot，当 bot name 会导致
        # git commit 身份错乱、PR 审查人失败。改为按平台取全局 bot username。
        from src.repo_key import parse_repo_key
        _prov, _o, _r = parse_repo_key(_rk)
        if _prov == "github":
            from src.user.db import get_global_github_bot_username
            _bot_name = get_global_github_bot_username()
        else:
            from src.user.db import get_global_bot_usernames
            _us = get_global_bot_usernames()
            _bot_name = _us[0] if _us else ""
        if _bot_name:
            return _bot_name
        # Issue #372: 路径 B 无全局 bot username → 用 host_user 的平台用户名
        from src.user.db import get_host_user_id
        _hid = get_host_user_id(_rk)
        if _hid > 0:
            try:
                from src.auth.grape_account_db import get_user_identities
                for _ident in get_user_identities(_hid):
                    if _ident.get("provider") == _prov:
                        return _ident.get("external_login") or ""
            except Exception:
                pass
        return ""
    except Exception:
        return ""


def _inject_bot_agent_env(env_extra: dict, repo_key: str, agent_type: str) -> None:
    """Issue #573：BOT_USERNAME / AGENT_NAME 注入全部 devops agent 任务 env。

    原实现仅 `agent_type == "coding"` 注入 BOT_USERNAME（create_skill_pr.sh 审查人用），
    issue agent（issue-runbook Step 4 机器人接管 assignee）等其余 agent 拿不到值；
    AGENT_NAME 仅 direct 模式经 stream_devops_agent 参数注入，worker 模式 env 取自
    env_extra 快照从未写入。本 helper 三个注入点统一调用：
    - BOT_USERNAME：_resolve_bot_username(repo_key)（可能为空串，空值时 skill 侧降级）
    - AGENT_NAME：get_agent_display_name(agent_type)（agent runtime 显示名）

    幂等：env_extra 已显式设置（上层调用方手工调参）时不覆盖。
    """
    try:
        env_extra.setdefault("BOT_USERNAME", _resolve_bot_username(repo_key))
    except Exception:
        pass
    try:
        env_extra.setdefault("AGENT_NAME", get_agent_display_name(agent_type))
    except Exception:
        pass


_PR_URL_NUMBER_RE = re.compile(r"/(?:merge_requests|pulls|pull)/(\d+)(?:[#/?]|$)")

# Issue #1802 复审：URL 兜底取到的 PR 号做一次存在性校验（issue 反馈建议第 2 条）。
# 只对 `_pr_source == "url"` 这条罕见路径生效，避免给正常 PR 事件加网络依赖。
_PR_EXISTS_CACHE: dict = {}
_PR_EXISTS_TTL = 60.0
_PR_EXISTS_MAX = 128


def _pr_number_exists(repo_key: str, pr_num: str) -> bool:
    """PR 号是否真实存在（仓库内）。仅 URL 来源调用，结果 60s 缓存。

    只有明确的 404/未找到才判不存在；网络异常、无 token、其它错误一律按「存在」处理，
    避免把一次抖动变成「PR_NUMBER 消失」的新故障。
    """
    if not repo_key or not str(pr_num or "").isdigit():
        return True
    key = (repo_key, str(pr_num))
    now = time.time()
    cached = _PR_EXISTS_CACHE.get(key)
    if cached and now - cached[0] < _PR_EXISTS_TTL:
        return cached[1]
    exists = True
    try:
        from src.user.db import get_gitcode_token
        from src.vcs.api_client import create_client
        token = get_gitcode_token(repo_key=repo_key) or ""
        if token:
            resp = create_client(repo_key, token).get_pr(int(pr_num))
            if not isinstance(resp, dict) or not resp:
                exists = False
            elif "error" in resp:
                err = str(resp.get("error") or "").lower()
                exists = not any(m in err for m in ("404", "not found", "不存在"))
    except Exception as e:
        log.info(f"Pipeline env: PR #{pr_num} existence check skipped ({e})")
        exists = True
    if len(_PR_EXISTS_CACHE) >= _PR_EXISTS_MAX:
        for _k in list(_PR_EXISTS_CACHE.keys())[:_PR_EXISTS_MAX // 2]:
            _PR_EXISTS_CACHE.pop(_k, None)
    _PR_EXISTS_CACHE[key] = (now, exists)
    return exists


def _numeric_value(value) -> str:
    """返回去 # 前缀后的纯数字字符串；非数字/空返回空串。"""
    text = str(value or "").strip().lstrip("#")
    return text if text.isdigit() else ""


def _pr_number_from_url(*values) -> str:
    """从 PR/MR 结构化 URL（.../merge_requests/<N>、.../pulls/<N>）提取编号。"""
    for value in values:
        match = _PR_URL_NUMBER_RE.search(str(value or ""))
        if match and match.group(1) != "0":
            return match.group(1)
    return ""


# Issue #1802：PR/MR 实体事件白名单。只有这些事件才允许从对象结构里取 PR 号；
# Issue 实体的 object_attributes.iid 是 Issue 自身编号，永不作 PR 号来源。
# 直接复用 src/api/routing.py 的 PR_EVENT_TYPES（单一事实源）；历史别名
# pr_opened/pr_updated/pr_closed/pr_merged/pr_comment 由下方 "pr_" 前缀统一兜住，
# 不再维护第二份清单（避免后续漂移）。
_PR_ENTITY_EVENT_TYPES = set(PR_EVENT_TYPES)

# payload 中 PR/MR 实体的结构特征（GitLab MR hook 的 object_attributes 含这些键；
# Issue hook 的 object_attributes 不含）。
_PR_OBJECT_ATTR_KEYS = ("source_branch", "target_branch", "merge_status")

# PR 评论事件别名（canonical = note_hook_pr；兼容历史/外部事件名）。
_NOTE_HOOK_EVENT_TYPES = {"note_hook_pr", "pr_comment"}


def _noteable_type(payload: dict) -> str:
    """取 object_attributes.noteable_type 归一化小写；缺失/非法返回空串。"""
    if not isinstance(payload, dict):
        return ""
    obj_attrs = payload.get("object_attributes")
    if not isinstance(obj_attrs, dict):
        return ""
    return str(obj_attrs.get("noteable_type") or "").strip().lower()


def _is_pr_entity_event(event_type: str, payload: dict) -> bool:
    """Issue #1802：判定事件实体是否为 PR/MR。

    准入 = 事件类型白名单 **或** payload 带 PR/MR 实体结构特征。目的是把
    「Issue 事件的 object_attributes.iid」挡在 PR 号候选链之外，避免 Issue 编号
    被当成不存在的 PR 编号注入。

    Issue 实体优先：`object_attributes.noteable_type == Issue` 一律判 False，
    不再被 note_hook_pr 等事件白名单短路。已知 Issue 事件（`ISSUE_EVENT_TYPES`）
    即使携带 `source_branch` 等结构键也判 False——此处是 PR_NUMBER 的严格准入，
    与 `_is_pr_payload`（服务 `resolve_issue_id`、对结构键从宽判 PR）分工不同。
    """
    noteable = _noteable_type(payload)
    if noteable == "issue":
        return False
    et = str(event_type or "").strip().lower()
    if et in _PR_ENTITY_EVENT_TYPES:
        return True
    if et.startswith(("pull_request", "merge_request", "pr_")):
        return True
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("pull_request"), dict) or isinstance(payload.get("merge_request"), dict):
        return True
    if noteable in ("mergerequest", "pullrequest"):
        return True
    if et in ISSUE_EVENT_TYPES:
        # 已知 Issue 实体事件：object_attributes 属于 Issue 自身，结构键不可作 PR 依据
        return False
    obj_attrs = payload.get("object_attributes")
    if isinstance(obj_attrs, dict) and any(k in obj_attrs for k in _PR_OBJECT_ATTR_KEYS):
        return True
    return False


def _as_payload_dict(value) -> dict:
    """把 events.payload（可能是 JSON 字符串）归一为 dict；非法值返回空 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _extract_pr_number(evt: dict, payload: dict, event_type: str) -> tuple[str, str]:
    """Issue #1579：统一 PR 号解析链，note_hook_pr 与普通事件共用同一来源。

    返回 (PR 号, 来源)。来源：payload（结构化字段，含 URL 兜底）；失败返回 ("", "")。
    note_hook_pr 不使用 object_attributes.iid：Note Hook 中该字段是评论自身 id，
    误用会把评论号当成 PR 号；改为从 object_attributes.url（.../merge_requests/<N>）
    等结构化 URL 兜底。仅消费结构化字段，不从事件标题猜号——真实事件标题是 MR/Issue
    标题（如 `fix: #1412 ...`），不含 `note_hook_pr #<N>` 标记。

    Issue #1802：按事件实体类型设准入闸门。payload.pr_number / evt._pr_sub_pr_number
    是显式 PR 号来源，始终可信；pull_request / merge_request / object_attributes
    等对象结构来源只对 PR/MR 实体事件开放。Issue Hook 的 object_attributes 就是
    该 Issue 自身，其 iid 即 Issue 编号，不得落入 PR 号候选链。
    """
    payload = _as_payload_dict(payload)
    merge_req = payload.get("merge_request")
    pull_req = payload.get("pull_request")
    obj_attrs = payload.get("object_attributes")
    merge_req = merge_req if isinstance(merge_req, dict) else {}
    pull_req = pull_req if isinstance(pull_req, dict) else {}
    obj_attrs = obj_attrs if isinstance(obj_attrs, dict) else {}

    candidates = [
        payload.get("pr_number"),
        evt.get("_pr_sub_pr_number"),
    ]
    # 单次解析只判定一次实体类型，避免重复解析（Issue #1802 复审）
    is_pr_entity = _is_pr_entity_event(event_type, payload)
    if is_pr_entity:
        if str(event_type or "").strip().lower() in _NOTE_HOOK_EVENT_TYPES:
            candidates.extend([
                merge_req.get("iid"),
                merge_req.get("number"),
                pull_req.get("iid"),
                pull_req.get("number"),
            ])
            # noteable_type=Issue 时 noteable_iid 是 Issue 编号，永不作 PR 号来源
            if _noteable_type(payload) != "issue":
                candidates.append(obj_attrs.get("noteable_iid"))
        else:
            candidates.extend([
                pull_req.get("number"),
                pull_req.get("iid"),
                merge_req.get("number"),
                merge_req.get("iid"),
                obj_attrs.get("iid"),
            ])
    for value in candidates:
        num = _numeric_value(value)
        if num:
            return num, "payload"

    if is_pr_entity:
        url_num = _pr_number_from_url(
            obj_attrs.get("url"),
            merge_req.get("web_url"),
            merge_req.get("url"),
            pull_req.get("web_url"),
            pull_req.get("url"),
        )
        if url_num:
            return url_num, "url"
    return "", ""


def _inject_issue_pr_env(env_extra: dict, evt: dict, repo_key: str) -> None:
    """Issue #528：ISSUE_NUMBER/PR_NUMBER 仅注入纯数字值。

    PR 订阅场景（PR 无关联 Issue）事件 issue_id 为伪 id（#pr_{PR号}_{scene_id}，
    参与去重/会话键），非数字 → 不注入 ISSUE_NUMBER/GRAPE_ISSUE_ID（下游 A2A
    交办接口要求 issue_number 为数字，注入占位值会被拒）；PR 号由 PR_NUMBER
    独立承载（payload.pr_number 优先，evt._pr_sub_pr_number 兜底），非数字同样
    不注入。关联 Issue 的场景（issue_id 为真实编号）行为不变。

    Issue #595：PR 关联真实 Issue 时 webhook 原始 payload 无顶层 pr_number，
    _pr_sub_pr_number 又仅订阅分支设置 → PR_NUMBER 恒为空。增加嵌套键兜底：
    前两来源均空时依次尝试 payload.pull_request.number（GitHub/GitCode）→
    payload.merge_request.number（GitLab MR）→ payload.object_attributes.iid
    （GitLab hook），沿用 lstrip("#").isdigit() 校验。

    Issue #1579：note_hook_pr 与普通事件统一走 `_extract_pr_number` 解析链——
    payload 结构化字段优先（含 Note Hook noteable_iid），缺失时从
    object_attributes.url / merge_request、pull_request 的 URL（.../merge_requests/<N>）
    兜底；不从事件标题猜号。解析失败注入 PR_NUMBER_PARSE_FAILED=1 并告警，
    禁止用 ISSUE_NUMBER 顶替。

    Issue #1802：无真实关联 PR 时静默不注入 PR_NUMBER（不写占位值）。
    仅 PR/MR 实体事件解析失败保留 PR_NUMBER_PARSE_FAILED=1 告警；Issue 实体事件
    本就不该有 PR，不写告警，避免把「Issue 无 PR」误报成解析故障。
    """
    _issue_num = str(evt.get("issue_id", "") or (evt.get("payload", {}) or {}).get("issue_number", "") or "").lstrip("#")
    if _issue_num.isdigit():
        env_extra["ISSUE_NUMBER"] = _issue_num
        # 需求卡片 id（issue 表主键）：awaiting-input 等 --issue_id 用（≠ ISSUE_NUMBER）
        # Issue #667：解析为空（run_pipeline step 3.2 被提前 return / 旁路失败未落行）时，
        # 先 ensure_issue_task 补建行再重解析——事件本身即 Issue 存在性证据，无需额外 VCS 校验。
        _card_id = resolve_issue_card_id(repo_key, _issue_num)
        if not _card_id:
            try:
                from src.api.issues_center import ensure_issue_task
                _oa = (evt.get("payload", {}) or {}).get("object_attributes", {})
                _t = str(evt.get("title", "") or (isinstance(_oa, dict) and _oa.get("title", "") or "") or "")
                ensure_issue_task(repo_key, int(_issue_num), title=_t, event_type=evt.get("event_type", ""))
                _card_id = resolve_issue_card_id(repo_key, _issue_num)
                if _card_id:
                    log.info(f"Pipeline env: issue #{_issue_num} card row missing, backfilled id={_card_id} (Issue #667)")
            except Exception as _e:
                log.warning(f"Pipeline env: backfill issue card #{_issue_num} failed: {_e}")
        env_extra["GRAPE_ISSUE_ID"] = _card_id
    elif _issue_num:
        log.info(f"Pipeline env: non-numeric issue_id '{_issue_num}', skip ISSUE_NUMBER/GRAPE_ISSUE_ID injection (Issue #528)")
    _payload = _as_payload_dict(evt.get("payload", {}))
    _event_type = str(evt.get("type", "") or evt.get("event_type", "") or "")
    _pr_num, _pr_source = _extract_pr_number(evt, _payload, _event_type)
    if _pr_num and _pr_source == "url" and not _pr_number_exists(repo_key, _pr_num):
        log.warning(f"Pipeline env: PR #{_pr_num} resolved from event URL does not exist "
                    f"(issue=#{_issue_num or '-'} repo={repo_key}); skip PR_NUMBER injection")
        _pr_num, _pr_source = "", ""
    if _pr_num:
        env_extra["PR_NUMBER"] = _pr_num
        if _pr_source == "url":
            log.info(f"Pipeline env: PR #{_pr_num} resolved from event URL (Issue #1579)")
    elif _is_pr_entity_event(_event_type, _payload):
        log.warning(f"Pipeline env: {_event_type or 'pr'} PR number parse failed "
                    f"(issue=#{_issue_num or '-'}, title={evt.get('title', '') or '-'})")
        env_extra["PR_NUMBER_PARSE_FAILED"] = "1"


def _norm_issue_id(s) -> str:
    """Issue #680: issue_id 规范化——入口/出口统一调用。

    修复双井号拼接缺陷：payload/标题里的编号可能已带 `#`（如 `#274`），
    旧逻辑直接 `f"#{num}"` 会拼出 `##274`。规范化规则：
    去首尾空白 → 剥掉全部前导 `#` → 仅保留纯数字 → 统一单 `#` 前缀；
    非数字 / 编号为 0 / 空值 → 返回 ""。
    """
    if s is None:
        return ""
    t = str(s).strip().lstrip("#").strip()
    if not t.isdigit() or t == "0":
        return ""
    return f"#{t}"


def _max_issue_from_issues(issues, exclude_num: str = "") -> str:
    """Issue #680: issues 关联列表取编号最大（原"取第一个"会误取正文背景引用的旧 issue）。

    GitCode pull_request_* payload 的 issues 列表同时含闭环目标与背景引用（如
    [#282, #274]），第一个未必是闭环目标；编号最大者为最新闭环目标。
    exclude_num：PR 自身编号（issues 列表里可能混入 PR 号，跳过）。
    非数字的 exclude_num（Issue #528 已证实存在 non-numeric pr_number）安全跳过，不抛异常。
    """
    _excl = str(exclude_num or "").strip().lstrip("#")
    _exclude_n = int(_excl) if _excl.isdigit() else None
    best = 0
    if isinstance(issues, list):
        for it in issues:
            if not isinstance(it, dict):
                continue
            norm = _norm_issue_id(it.get("number", it.get("iid", "")))
            if not norm:
                continue
            n = int(norm.lstrip("#"))
            if _exclude_n is not None and n == _exclude_n:
                continue
            if n > best:
                best = n
    return f"#{best}" if best else ""


def resolve_issue_id(payload: dict) -> str:
    """从事件 payload 提取 Issue 编号。

    注意：PR 事件不返回 PR 号（其 object_attributes.iid 是 PR 号，会误当 issue_id）；
    PR 事件的真实关联 Issue 用 resolve_pr_issue_id（title/分支/issues 列表）。

    Issue #89 修复：Issue Hook payload 可能含 issues 交叉引用键（父任务），
    旧逻辑先查 issues 列表 → 返回父任务编号而非当前 Issue。
    修正：非 PR 事件优先查 object_attributes，issues 列表仅作 PR 事件兜底。

    Issue #680：issues 兜底段改"取编号最大"（原取第一个，同型歧义一并收敛）；
    出口统一过 _norm_issue_id（修 ##274 双井号拼接缺陷）。
    """
    if not isinstance(payload, dict):
        return ""
    # A2A payload 用 issue_number 字段
    num = payload.get("issue_number", "")
    if num:
        # Issue #755：跨仓转交 payload 沿用源仓单号时（编号 == carried_context.source_issue_number），
        # 若 carried_context 显式标记了 target_issue_number（真实落地单号）则以其为准——
        # 下游任务注入的 ISSUE_NUMBER 即命中目标仓 VCS 真实 Issue（非 404）。
        # 仅当 payload 单号被显式标记为源单号才替换，避免污染同仓正常 A2A。
        _cc = payload.get("carried_context")
        if isinstance(_cc, dict):
            _tgt = str(_cc.get("target_issue_number", "") or "").strip().lstrip("#")
            _src = str(_cc.get("source_issue_number", "") or "").strip().lstrip("#")
            if _tgt.isdigit() and _src == str(num).strip().lstrip("#") and _tgt != _src:
                return f"#{_tgt}"
        return _norm_issue_id(num)
    # Issue 类事件（issue_comment 等）顶层 issue 对象
    obj = payload.get("issue", {})
    if isinstance(obj, dict):
        num = obj.get("number", obj.get("iid", ""))
        if num:
            return _norm_issue_id(num)
    # Issue #89: 非 PR 事件优先查 object_attributes（当前 Issue 编号）
    # 旧逻辑先查 issues 列表 → 对含交叉引用的 Issue Hook 返回父任务编号
    _is_pr = _is_pr_payload(payload)
    if not _is_pr:
        obj = payload.get("object_attributes", {})
        if isinstance(obj, dict):
            num = obj.get("number", obj.get("iid", obj.get("noteable_iid", "")))
            if num:
                return _norm_issue_id(num)
    # PR payload 中显式关联的 issues 列表（GitCode pull_request_*）：
    # Issue #680 —— 取编号最大（闭环目标为最新 Issue，背景引用的旧 Issue 编号更小）
    issues = payload.get("issues")
    if isinstance(issues, list):
        _max = _max_issue_from_issues(issues)
        if _max:
            return _max
    # PR 事件的 object_attributes 不查（其 iid 是 PR 号，会误当 issue_id）
    return ""


def resolve_pr_issue_id(payload: dict, api_token: str = "", repo_key: str = "") -> str:
    """从 PR 事件 payload 解析关联的 Issue 编号。

    Issue #680 优先级重排（原 title → issues 第一个 → 分支 → API 会误取正文
    背景引用的旧 issue，如 #274 而非闭环目标 #282）：
    1. 分支名 issue-N（强信号：开发分支即闭环目标）
    2. PR 标题 #N
    3. issues 列表取编号最大（[#282, #274] → #282）
    4. PR 详情 API 兜底（同规则：head.ref 分支 → title → issues 最大）

    Issue #252: 当 issue 与 PR 编号相同时，通过 entity_type 区分——
    从文本提取编号时检查 URL 路径：/pulls/N 是 PR 自引用（跳过），/issues/N 是关联 Issue（采用）；
    issues 列表里混入 PR 自身编号时同样跳过。
    Issue #680: 出口统一过 _norm_issue_id（修 ##274 双井号拼接缺陷）。
    """
    import re, json, urllib.request
    from src.api.event_store import _extract_issue_id_from_text, _extract_entity_type_from_text

    if not isinstance(payload, dict):
        return ""

    # PR 自身编号（用于自引用检测）
    obj = payload.get("object_attributes", {}) or {}
    pr_obj = payload.get("pull_request", {}) or payload.get("merge_request", {}) or {}
    _pr_self_num = str(obj.get("iid", obj.get("number", "")) or pr_obj.get("number", "")).lstrip("#")

    # 1. 分支名 issue-N（Issue #680 强信号，最优先）
    branch = obj.get("source_branch", "") or obj.get("ref", "")
    if not branch and isinstance(pr_obj.get("head"), dict):
        branch = (pr_obj.get("head") or {}).get("ref", "")
    m = re.search(r'issue[-_](\d+)', str(branch))
    if m and m.group(1) != "0":
        return f"#{m.group(1)}"

    # 2. 从 object_attributes / pull_request 的 title 提取 #N
    title = obj.get("title", "") or pr_obj.get("title", "")
    issue = _extract_issue_id_from_text(title)
    if issue:
        _et = _extract_entity_type_from_text(title)
        _issue_num = issue.lstrip("#")
        # Issue #252: 跳过 PR 自引用（编号==PR自身编号 + URL路径为 /pulls/）
        if not (_et == "pull_request" and _issue_num == _pr_self_num):
            return _norm_issue_id(issue)

    # 3. 从 payload.issues 关联列表提取（GitCode pull_request_*）：
    # Issue #680 —— 取编号最大（原取第一个会误取背景引用的旧 issue），跳过 PR 自身编号
    issue = _max_issue_from_issues(payload.get("issues"), exclude_num=_pr_self_num)
    if issue:
        return issue

    # 4. 同步调 PR 详情 API（同规则：head.ref 分支 → title → issues 最大）
    pr_number = obj.get("iid", obj.get("number", "")) or pr_obj.get("number", "")
    if pr_number and api_token:
        repo_path = payload.get("project", {}).get("path_with_namespace", "") if isinstance(payload.get("project"), dict) else ""
        if not repo_path:
            repo = payload.get("repository", {})
            if isinstance(repo, dict):
                repo_path = repo.get("full_name", "")
        if repo_path and "/" in repo_path:
            try:
                if _is_github_repo(repo_key):
                    url = f"https://api.github.com/repos/{repo_path}/pulls/{pr_number}"
                    hdrs = {"Authorization": f"Bearer {api_token}",
                            "Accept": "application/vnd.github+json",
                            "User-Agent": "Grape-DevOps"}
                else:
                    url = f"https://gitcode.com/api/v5/repos/{repo_path}/pulls/{pr_number}"
                    hdrs = {"Authorization": f"Bearer {api_token}"}
                req = urllib.request.Request(url, headers=hdrs)
                data = json.loads(urllib.request.urlopen(req, timeout=10).read())
                # 4.1 head.ref 分支名 issue-N（同规则优先）
                head = data.get("head", {}) or {}
                m = re.search(r'issue[-_](\d+)', str(head.get("ref", "")))
                if m and m.group(1) != "0":
                    return f"#{m.group(1)}"
                # 4.2 title #N（含 Issue #252 自引用跳过）
                _api_title = str(data.get("title", ""))
                issue = _extract_issue_id_from_text(_api_title)
                if issue:
                    _et = _extract_entity_type_from_text(_api_title)
                    _issue_num = issue.lstrip("#")
                    if not (_et == "pull_request" and _issue_num == _pr_self_num):
                        return _norm_issue_id(issue)
                # 4.3 issues 列表取最大（GitHub API 无此字段，GitCode 有则同规则）
                issue = _max_issue_from_issues(data.get("issues"), exclude_num=_pr_self_num)
                if issue:
                    return issue
            except Exception:
                return ""
    return ""


def _resolve_event_issue_id(evt: dict, repo_key: str, api_token: str = "") -> str:
    """Issue #680: 事件级 issue_id 统一解析入口（每事件只解析一次）。

    背景：原 7 处调用点各自调 resolve_pr_issue_id / resolve_issue_id，带 token
    与不带 token 的口径分裂（issues 列表"取第一个"时不同调用点可能解析出不同
    issue），导致场景校验与 SRE 推进用的编号不一致。本入口把结果挂在
    evt["_resolved_issue_id"]，全链复用同一值：

    - PR 类事件：resolve_pr_issue_id（分支 → 标题 → issues 最大 → PR 详情 API）→ resolve_issue_id 兜底
    - Issue 类/其它事件：resolve_issue_id（object_attributes/issue_number 等）
    - 最终兜底 evt["issue_id"]（归一化层已解析的值）

    token 补偿：首次无 token 解析为空、后续调用带 token 时允许补一次 PR 详情
    API 兜底（非空结果一经产生即锁定，不再重算）——"只计算一次"指非空结果
    单一来源；全空场景的 API 兜底是唯一例外且结果同样写回缓存。
    """
    if not isinstance(evt, dict):
        return ""
    cached = evt.get("_resolved_issue_id")
    if cached:
        return cached
    # 已带 token 尝试过仍为空 → 不再重算（避免重复 PR 详情 API 调用）
    if evt.get("_resolved_issue_id_token_tried"):
        return ""
    event_type = evt.get("type", "") or ""
    payload = evt.get("payload", {}) or {}
    issue_id = ""
    if event_type in PR_EVENT_TYPES:
        issue_id = resolve_pr_issue_id(payload, api_token, repo_key=repo_key) or resolve_issue_id(payload) or ""
    else:
        issue_id = resolve_issue_id(payload) or ""
    if not issue_id:
        # QA review: 缓存的 evt["issue_id"] 可能缺 # 前缀，统一过 _norm_issue_id
        issue_id = _norm_issue_id(evt.get("issue_id", ""))
    if api_token:
        evt["_resolved_issue_id_token_tried"] = True
    evt["_resolved_issue_id"] = issue_id or ""
    return evt["_resolved_issue_id"]


# L2 阶段分派映射表（Issue #49 §5.3）：stage × 事件类型 → Agent
# - 对应列 stage 命中 → 目标 Agent；"—" 表示该阶段下该事件不分发（返回空）
# - 特例：PR updated + stage=coding + QA 有历史 → qa（复审）
_STAGE_EVENT_AGENT_MAP = {
    "issue_opened":        {"vod": "vod", "issue": "issue", "coding": "issue", "qa": "issue", "sre": "issue", "pending_review": "", "done": ""},
    "issue_comment":       {"vod": "vod", "issue": "issue", "coding": "coding", "qa": "qa", "sre": "sre", "pending_review": "issue", "done": ""},
    # close 已由 L1 副作用短路，此处安全兜底：close 一律不分发（issue_updated 其余 action 归 issue）
    "issue_updated":       {"vod": "issue", "issue": "issue", "coding": "issue", "qa": "issue", "sre": "issue", "pending_review": "", "done": ""},
    "issue_labeled":       {"vod": "vod", "issue": "issue", "coding": "issue", "qa": "issue", "sre": "issue", "pending_review": "", "done": ""},
    "pull_request_opened": {"vod": "issue", "issue": "issue", "coding": "coding", "qa": "qa", "sre": "sre", "pending_review": "", "done": ""},
    "pull_request_updated": {"vod": "issue", "issue": "issue", "coding": "qa", "qa": "qa", "sre": "sre", "pending_review": "", "done": ""},
    "pull_request_merged": {"vod": "issue", "issue": "issue", "coding": "coding", "qa": "sre", "sre": "sre", "pending_review": "", "done": ""},
    "note_hook_pr":        {"vod": "issue", "issue": "issue", "coding": "coding", "qa": "qa", "sre": "sre", "pending_review": "", "done": ""},
    "pull_request_review": {"vod": "issue", "issue": "issue", "coding": "coding", "qa": "qa", "sre": "sre", "pending_review": "", "done": ""},
}


def _resolve_issue_stage_agent(repo_key: str, event_type: str, payload: dict,
                               default_agent: str, evt: dict = None) -> str:
    """阶段矩阵路由（D9，见 vod-skill-upgrade-plan.md §3.0.3）——表驱动（Issue #49 §5.3）。

    - issue_opened 恒给 vod（D8）
    - 其余按 issue_state.stage × event_type 查 `_STAGE_EVENT_AGENT_MAP`
    - 特例：pull_request_updated + stage=coding + QA 有历史 → qa 复审
    - 无 issue_state / 非关联事件 → default_agent（现状 get_domain_agent 结果）
    - stage=done → 一律不分发
    """
    from src.api.session_store import get_issue_stage

    if event_type == "issue_opened":
        return "vod"

    if event_type not in ISSUE_EVENT_TYPES and event_type not in PR_EVENT_TYPES:
        return default_agent

    # 解析关联 issue_id：Issue #680 —— 统一入口（一次解析存 evt["_resolved_issue_id"]，全链同值）
    if evt is not None:
        issue_id = _resolve_event_issue_id(evt, repo_key)
    elif event_type in PR_EVENT_TYPES:
        issue_id = resolve_pr_issue_id(payload, repo_key=repo_key) or resolve_issue_id(payload) or ""
    else:
        issue_id = resolve_issue_id(payload) or ""
    if not issue_id:
        return default_agent

    st = get_issue_stage(repo_key, issue_id)
    if not st:
        # 无记录：issue_opened 已由上面处理；其余关联事件默认 vod（新 Issue 先审视）
        return "vod" if event_type in ISSUE_EVENT_TYPES else default_agent
    stage = st["stage"]

    # Issue #51: cancelled 任务不再分发（防止取消后 A2A 继续流转）
    from src.session_manager import db as _ck_db, db_lock as _ck_lock
    try:
        _ck_issue_num = str(issue_id).lstrip("#")
        if _ck_issue_num.isdigit():
            with _ck_lock:
                _ck_c = _ck_db.cursor()
                _ck_c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=? AND status='cancelled' LIMIT 1",
                              (repo_key, int(_ck_issue_num)))
                if _ck_c.fetchone():
                    return ""
    except Exception as _ck_err:
        log.warning(f"Pipeline: cancelled-task check failed for {repo_key}/{issue_id}: {_ck_err} — defaulting to no-dispatch (safe fail)")
        return ""

    # 表驱动查找；显式列缺失视为不分发
    _row = _STAGE_EVENT_AGENT_MAP.get(event_type)
    _agent = (_row or {}).get(stage, "") if _row else ""

    # pending_review → 审核点 owner（issue-comment-stage-owner-routing-design.md §3.1）：
    # 评论应回给「设审核点/当前 stage owner」的 agent（如 QA 设待审核则由 QA 收口 review-pass/reject）
    if stage == "pending_review" and _agent:
        _owner = (st.get("owner_agent") or "")
        if _owner in ("vod", "issue", "coding", "qa", "sre"):
            _agent = _owner

    # 特例已由 _STAGE_EVENT_AGENT_MAP 表驱动处理：
    # pull_request_updated + coding → qa（weekly-02 修复 Issue #46：QA↔coding 回流闭环）
    # 无需额外 QA 历史记录检查

    return _agent


def resolve_actor(payload: dict, evt: dict) -> str:
    """从 payload 提取执行者。

    FR-12.5: 多级 fallback 防止 actor 为空——
    evt.actor > payload.user/sender > payload.comment.user > payload.issue.user > payload.pusher > "unknown"
    """
    actor = evt.get("actor", "")
    if actor:
        return actor
    for key in ("user", "sender"):
        obj = payload.get(key, {})
        if isinstance(obj, dict):
            _login = obj.get("username", obj.get("login", ""))
            if _login:
                return _login
    # FR-12.5: comment 作者兜底
    _comment = payload.get("comment", {})
    if isinstance(_comment, dict):
        _c_user = _comment.get("user", {})
        if isinstance(_c_user, dict):
            _login = _c_user.get("username", _c_user.get("login", ""))
            if _login:
                return _login
    # FR-12.5: issue author 兜底
    _issue = payload.get("issue", {})
    if isinstance(_issue, dict):
        _i_user = _issue.get("user", {})
        if isinstance(_i_user, dict):
            _login = _i_user.get("username", _i_user.get("login", ""))
            if _login:
                return _login
    # FR-12.5: pusher 兜底
    _pusher = payload.get("pusher", "")
    if _pusher and isinstance(_pusher, str):
        return _pusher
    return "unknown"


def resolve_pr_target_branch(evt: dict) -> str:
    """从 PR 事件提取合入目标分支（GitCode object_attributes.target_branch / GitHub base.ref）。

    归一化的 branch 仅 push 事件有值；PR 合入事件的目标分支需从 payload 补充。
    见 agent-event-subscription-design.md §五。
    """
    branch = evt.get("branch") or ""
    if branch:
        return branch
    payload = evt.get("payload", {}) or {}
    obj = payload.get("object_attributes", {}) or {}
    pr_obj = payload.get("pull_request", {}) or payload.get("merge_request", {}) or {}
    _base = pr_obj.get("base", {}) or {}
    _b = obj.get("target_branch", "") or _base.get("ref", "")
    return str(_b or "").strip()


def _match_repo_subscription(repo_key: str, event_type: str, branch: str) -> str:
    """仓库级事件订阅兜底路由（agent-event-subscription-design.md §六）。

    读取 devops_agent_session.subscribe_events（JSON 列表，元素 {event_type, branch}），
    匹配 event_type 且（订阅 branch 为空=不限分支 / 等于事件分支）→ 返回 agent_type。
    未命中返回空。仅当主路由结果为空时由 run_pipeline 调用。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT agent_type, subscribe_events FROM devops_agent_session WHERE repo_key=? AND subscribe_events IS NOT NULL AND subscribe_events!='' AND subscribe_events!='[]'",
                (repo_key,)
            )
            for row in c.fetchall():
                _agent, _raw = row[0], row[1]
                try:
                    subs = json.loads(_raw or "[]")
                except Exception:
                    continue
                if not isinstance(subs, list):
                    continue
                for s in subs:
                    if not isinstance(s, dict):
                        continue
                    if s.get("event_type") != event_type:
                        continue
                    # 分支：空=不限；逗号分隔多分支（前端输入），精确匹配任一
                    _sb = str(s.get("branch", "") or "").replace("，", ",").strip()
                    _subs = [b.strip() for b in _sb.split(",") if b.strip()] if _sb else []
                    if _subs and branch not in _subs:
                        continue
                    return _agent
    except Exception as e:
        log.warning(f"Pipeline: repo subscription match failed: {e}")
    return ""


def _dispatch_pr_subscription_extra(repo_key: str, evt: dict, event_type: str, match: tuple):
    """多场景命中同一 PR 时，为额外场景并行分发 QA 任务。

    match = (scene_id, qa_skill, qa_engine_ids, sre_skill, sre_engine_ids)
    """
    try:
        scene_id, qa_skill, qa_engine_ids, _, _ = match
        # 构造 evt 副本，写入额外场景信息
        _extra_evt = dict(evt)
        _extra_evt["_pr_sub_scene_id"] = scene_id
        _extra_evt["_pr_sub_skill"] = qa_skill
        _extra_evt["_pr_sub_engine_ids"] = qa_engine_ids
        # 提取 PR 号用于回帖（统一走 _extract_pr_number，Issue 实体 iid 不作 PR 号来源）
        _payload = _as_payload_dict(evt.get("payload", {}))
        _pr_num, _ = _extract_pr_number(_extra_evt, _payload, event_type)
        if _pr_num:
            _extra_evt["_pr_sub_pr_number"] = _pr_num
        # 构造伪 issue_id（带 scene_id 区分）
        _pseudo_id = f"#pr_{_pr_num}_{scene_id}" if _pr_num else ""
        # 创建事件
        from src.api.event_store import append_event
        _title = f"PR 订阅 QA: {scene_id}"
        _a2a_actor = _a2a_actor_for(evt, _pseudo_id, repo_key)
        seq = append_event(
            repo_key=repo_key, event_type="a2a_event",
            title=_title, issue_id=_pseudo_id, source="a2a",
            agent_type="qa", actor=_a2a_actor,
            summary=f"PR 订阅并行: scene={scene_id} → qa",
            payload=_payload_with_account(evt.get("payload"), _a2a_actor), status="pending",
            scene_id=scene_id,
            entity_type="pull_request",
        )
        log.info(f"Pipeline: PR subscription extra dispatch seq={seq} scene={scene_id}")
        # 分派执行：跟主场景 run_pipeline 一致的判断逻辑
        # engine_id=0 + plugin + auto(非direct) → broadcast enqueue（远端worker认领）
        # 仅 direct 模式或选中具体本机引擎时走 DIRECT
        from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, get_execution_mode
        _engine_id = pick_engine(repo_key, "qa", fallback_runtime_id=0, scene_id=scene_id)
        if _engine_id is None:
            log.info(f"Pipeline: PR subscription extra: no engine for scene={scene_id}, mark failed")
            _fail_event(seq, f"PR 订阅派发失败：无可用引擎（scene={scene_id}, agent=qa），请检查引擎在线状态")
            return
        _be = engine_name(_engine_id) or ""
        _is_broadcast = (_engine_id == 0 and is_plugin_mode()
                         and get_execution_mode() != EXEC_MODE_DIRECT)
        if _is_broadcast:
            # 复用 _enqueue_pipeline_task：自动构建 prompt/env_extra/会话/init_event_json
            # Issue #259：get_scene_pool_engine_ids 已按健康过滤；None = 场景引擎全灭（方案A，不入队）
            _pool_ids = get_scene_pool_engine_ids(scene_id, "qa", repo_key=repo_key)
            if _pool_ids is None:
                _fail_scene_all_offline(seq, scene_id, "qa", "Pipeline PR sub", repo_key=repo_key)
                return
            _enqueue_pipeline_task(repo_key, seq, "qa", _be, qa_skill, _extra_evt,
                                   0, qa_skill, _engine_id, _pseudo_id,
                                   pool_engine_ids=_pool_ids or "")
        else:
            # P0 (Issue #596): direct 路径改走 enqueue_task，由 local_claimer 认领执行
            _pool_ids = get_scene_pool_engine_ids(scene_id, "qa", repo_key=repo_key)
            if _pool_ids is None:
                _fail_scene_all_offline(seq, scene_id, "qa", "Pipeline PR sub else-branch", repo_key=repo_key)
                return
            _enqueue_pipeline_task(repo_key, seq, "qa", _be, qa_skill, _extra_evt,
                                   0, qa_skill, _engine_id, _pseudo_id,
                                   pool_engine_ids=_pool_ids or "")
    except Exception as e:
        log.warning(f"Pipeline: PR subscription extra dispatch failed: {e}")


def _dispatch_pr_subscription_redispatch(evt: dict, seq: int, scene_id: str) -> bool:
    """PR 订阅事件重派：在原事件上直接重新创建 task，不创建新事件。

    用于 redispatch 路径 — 当 PR 订阅事件的 task 派发失败（如远程 enqueue 报错）、
    需要手动重派时调用。不能走 A2A 端点（payload 是 webhook 原始格式，无 issue_number）。

    返回 True = 派发成功，False = 引擎不可用或无 QA 节点。
    """
    try:
        from src.session_manager import db, db_lock

        repo_key = evt.get("repo_key", "") or ""

        # 从 devops_pipeline_scene 查 scene_id 对应的 QA 节点 skill
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT scene_id, nodes_json FROM devops_pipeline_scene "
                "WHERE repo_key=? AND scene_id=? AND is_active=1",
                (repo_key, scene_id))
            row = c.fetchone()
        if not row:
            log.warning(f"Pipeline: PR subscription redispatch: scene={scene_id} not found")
            return False

        nodes = json.loads(row[1] or "[]")
        qa_node = next((n for n in nodes if n.get("tag") == "qa" and n.get("enabled", True)), None)
        if not qa_node or not qa_node.get("skill"):
            log.warning(f"Pipeline: PR subscription redispatch: no QA node in scene={scene_id}")
            return False

        qa_skill = qa_node["skill"]
        qa_engine_ids = qa_node.get("engine_runtime_ids", [])

        # 提取 PR 号（统一走 _extract_pr_number，Issue 实体 iid 不作 PR 号来源）
        event_type = str(evt.get("type", "") or evt.get("event_type", "") or "")
        _payload = _as_payload_dict(evt.get("payload", {}))
        _pr_num, _ = _extract_pr_number(evt, _payload, event_type)

        # 伪 issue_id（跟 _dispatch_pr_subscription_extra 一致）
        _pseudo_id = f"#pr_{_pr_num}_{scene_id}" if _pr_num else ""

        # 选引擎
        from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, get_execution_mode
        _engine_id = pick_engine(repo_key, "qa", fallback_runtime_id=0, scene_id=scene_id)
        if _engine_id is None:
            log.info(f"Pipeline: PR subscription redispatch: no engine for scene={scene_id}")
            return False

        _be = engine_name(_engine_id) or ""
        _is_broadcast = (_engine_id == 0 and is_plugin_mode()
                         and get_execution_mode() != EXEC_MODE_DIRECT)
        _extra_evt = dict(evt)
        _extra_evt["payload"] = _payload  # 确保是 dict（_load_event 返回 JSON 字符串）
        # 补全 build_devops_agent_prompt 必需的字段（_load_event 不返回 repository/source）
        from src.repo_key import from_repo_key
        _extra_evt["repository"] = _extra_evt.get("repository") or from_repo_key(repo_key)
        _extra_evt["source"] = _extra_evt.get("source") or (repo_key.split(":", 1)[0] if ":" in repo_key else "gitcode")
        _extra_evt["repo_key"] = repo_key
        _extra_evt["_pr_sub_scene_id"] = scene_id
        _extra_evt["_pr_sub_skill"] = qa_skill
        _extra_evt["_pr_sub_engine_ids"] = qa_engine_ids
        if _pr_num:
            _extra_evt["_pr_sub_pr_number"] = _pr_num

        if _is_broadcast:
            # 复用 _enqueue_pipeline_task：自动构建 prompt/env_extra/会话/init_event_json
            # Issue #259：get_scene_pool_engine_ids 已按健康过滤；None = 场景引擎全灭（方案A，不入队）
            _pool_ids = get_scene_pool_engine_ids(scene_id, "qa", repo_key=repo_key)
            if _pool_ids is None:
                _fail_scene_all_offline(seq, scene_id, "qa", "Pipeline PR sub", repo_key=repo_key)
                return
            _enqueue_pipeline_task(repo_key, seq, "qa", _be, qa_skill, _extra_evt,
                                   0, qa_skill, _engine_id, _pseudo_id,
                                   pool_engine_ids=_pool_ids or "")
        else:
            # P0 (Issue #596): direct 路径改走 enqueue_task，由 local_claimer 认领执行
            _pool_ids = get_scene_pool_engine_ids(scene_id, "qa", repo_key=repo_key)
            if _pool_ids is None:
                _fail_scene_all_offline(seq, scene_id, "qa", "Pipeline PR sub else-branch", repo_key=repo_key)
                return
            _enqueue_pipeline_task(repo_key, seq, "qa", _be, qa_skill, _extra_evt,
                                   0, qa_skill, _engine_id, _pseudo_id,
                                   pool_engine_ids=_pool_ids or "")

        # 重派成功：把事件状态改回 running（重派前可能是 failed）
        from src.api.event_store import update_event_status
        update_event_status(seq, "running")
        log.info(f"Pipeline: PR subscription redispatch ok, seq={seq} scene={scene_id} engine={_engine_id}")
        return True
    except Exception as e:
        log.warning(f"Pipeline: PR subscription redispatch failed: {e}")
        return False


def _dispatch_subscription_agent(repo_key: str, evt: dict, event_type: str,
                                 agent_type: str, main_agent: str):
    """订阅并行分发（event-routing-subscription-design.md §2.2）。
    为订阅命中的 Agent 创建独立事件并分派任务，不改 issue_state.stage。
    """
    try:
        from src.session_manager import db, db_lock
        backend = "opencode"
        runtime_id = 0
        skill_name = ""
        skill_id = 0
        skill_path = ""
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                      (repo_key, agent_type))
            row = c.fetchone()
            if row:
                runtime_id = row[0] or 0
                if row[2]:
                    skill_id = row[2]
                    from src.user.db import get_skill_by_id
                    _sk = get_skill_by_id(row[2])
                    if _sk:
                        skill_name = _sk.get("skill_name") or skill_name
                        skill_path = _sk.get("skill_path") or skill_name
                skill_name = skill_name or row[1] or ""
                if row[0]:
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[0],))
                    _br = c.fetchone()
                    if _br and _br[0]:
                        backend = _br[0]
        if not skill_name:
            # Plugin mode: try pipelines.json first
            if is_plugin_mode():
                _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_type)
                if _pipe_skill:
                    skill_name = _pipe_skill
                    log.info(f"Pipeline subscription (plugin): skill for {agent_type}={_pipe_skill}")
            if not skill_name:
                # Legacy fallback: agent-skill-mapping.json
                import json as _json
                _mapping_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-skill-mapping.json")
            if os.path.exists(_mapping_path):
                with open(_mapping_path, "r", encoding="utf-8") as _f:
                    _defaults = _json.load(_f)
                _scene = _defaults.get("scenes", {}).get("default", {})
                # 读仓库开发场景（managed_repos.dev_scene），按场景读映射
                try:
                    from src.user.db import get_repo_scene
                    _scene_id = get_repo_scene(repo_key) or "default"
                    _scene = _defaults.get("scenes", {}).get(_scene_id, _defaults.get("scenes", {}).get("default", {}))
                except Exception:
                    pass
                _mapping = _scene.get("mapping", {}) if isinstance(_scene, dict) else {}
                skill_name = _mapping.get(agent_type, "")
            if not skill_name:
                log.warning(f"Pipeline subscription: no skill for agent={agent_type}, skip extra dispatch")
                from src.api.event_store import append_skipped_event
                _sub_skip_iid = _resolve_event_issue_id(evt, repo_key)
                append_skipped_event(repo_key, evt.get("type", ""), "subscription_no_skill", agent_type,
                                      issue_id=_sub_skip_iid)
                return
        # Agent 启用检查
        from src.api.session_store import is_agent_enabled
        if not is_agent_enabled(repo_key, agent_type):
            log.info(f"Pipeline subscription: agent={agent_type} disabled, skip extra dispatch")
            from src.api.event_store import append_skipped_event
            _sub_skip_iid2 = _resolve_event_issue_id(evt, repo_key)
            append_skipped_event(repo_key, evt.get("type", ""), "subscription_agent_disabled", agent_type,
                                 issue_id=_sub_skip_iid2)
            return
        # 创建事件
        from src.api.event_store import append_event
        issue_id = _resolve_event_issue_id(evt, repo_key) or "#0"
        # 从 tasks 表获取 Issue 真实标题（避免 Pipeline 列表显示 "A2A (订阅): xxx"）
        _sub_title = ""
        _sub_iss_num = str(issue_id).lstrip("#")
        if _sub_iss_num and _sub_iss_num.isdigit():
            try:
                from src.api.issues_center import _get_issue_title
                _sub_title = _get_issue_title(repo_key, int(_sub_iss_num)) or ""
            except Exception:
                pass
        if not _sub_title:
            _sub_title = f"A2A (订阅): {agent_type}"
        # Issue #252: 订阅事件继承原事件实体类型
        _sub_entity_type = "pull_request" if event_type in PR_EVENT_TYPES else ("issue" if event_type in ISSUE_EVENT_TYPES else "")
        _a2a_actor = _a2a_actor_for(evt, issue_id, repo_key)
        seq = append_event(
            repo_key=repo_key, event_type="a2a_event",
            title=_sub_title, issue_id=issue_id, source="a2a",
            agent_type=agent_type, actor=_a2a_actor,
            summary=f"订阅触发: {event_type} → {agent_type}（主路由: {main_agent}）",
            payload=_payload_with_account(evt.get("payload"), _a2a_actor), status="pending",
            entity_type=_sub_entity_type,
        )
        log.info(f"Pipeline subscription: event created seq={seq} agent={agent_type}")
        # 执行分派
        from src.runtime.engine_select import pick_engine, engine_name
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
        engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=runtime_id or 0)
        if not engine_id:
            log.info(f"Pipeline subscription: no engine for agent={agent_type}, mark failed")
            _fail_event(seq, f"订阅派发失败：无可用引擎（agent={agent_type}），请检查引擎在线状态")
            return
        _be = engine_name(engine_id) or backend
        # P0 (Issue #596): direct 路径改走 enqueue_task，由 local_claimer 认领执行
        from src.runtime.engine_select import _repo_user_id
        _sub_uid = _repo_user_id(repo_key, issue_creator=evt.get("actor", ""))
        _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                               skill_id, skill_path, engine_id, issue_id, user_id=_sub_uid)
        log.info(f"Pipeline subscription: dispatched seq={seq} agent={agent_type} mode={kind}")
    except Exception as e:
        log.warning(f"Pipeline subscription dispatch failed: {e}")


def _evaluate_skill_scope(skill_name: str, repo_key: str, skill_id: int = 0,
                          node_tag: str = "", scene_id: str = "") -> dict:
    """派发门禁统一 scope 判定（Issue #1758）：直接调用单一判官。"""
    from src.api.scene_skill_scope import evaluate
    return evaluate({"skill": skill_name, "skill_id": skill_id,
                     "tag": node_tag or "", "id": node_tag or ""},
                    repo_key, "repo", scene_id=scene_id)


def _fail_skill_resolve_event(repo_key: str, skill_name: str, scene_id: str,
                              agent_type: str, reason: str,
                              issue_id: str = "", seq: int = 0,
                              scope_info: dict | None = None) -> dict:
    """派发前技能解析失败 fail-fast（Issue #657）。

    - seq>0（事件已创建）→ 走 _fail_event 同款通路：既有事件置 failed、
      agent_tasks 同步 failed（result 带 [skill_resolve_failed] 前缀 + diag）；
    - seq=0（事件尚未创建）→ 落 skill_resolve_failed 错误事件
      （status=failed，result 带 [skill_resolve_failed] 前缀文案，
      含场景/节点/技能名/原因）。

    返回 result 字典（调用方直接 return，不再派发 → 替代
    pending_self_report 1800s timeout_failed 静默超时）。
    """
    _scope = scope_info or {}
    _skill_scope = _scope.get("actual_scope") or ""
    _scene_scope = _scope.get("expected_scope") or "repo"
    _rebind = list(_scope.get("rebind_candidates") or [])
    _result = {"seq": seq, "event_type": "skill_resolve_failed", "skill": skill_name,
               "repo_key": repo_key, "ok": False, "status": "failed",
               "skill_resolve_failed": True,
               "skill_scope": _skill_scope, "scene_scope": _scene_scope,
               "rebind_candidates": _rebind}
    _msg = f"[skill_resolve_failed] {reason}"
    try:
        if seq:
            _fail_event(seq, _msg, diag={
                "code": "skill_resolve_failed", "skill": skill_name,
                "scene_id": scene_id, "agent_type": agent_type,
                "skill_scope": _skill_scope, "scene_scope": _scene_scope,
                "rebind_candidates": _rebind,
                "reason": reason[:300]})
        else:
            from src.api.event_store import append_event
            from src.vcs.event_types import SKILL_RESOLVE_FAILED
            _seq = append_event(
                repo_key=repo_key, event_type=SKILL_RESOLVE_FAILED,
                title=f"技能解析失败: {skill_name}",
                issue_id=issue_id, agent_type=agent_type, source="pipeline",
                summary=reason[:200],
                payload={"skill": skill_name, "scene_id": scene_id,
                         "agent_type": agent_type, "reason": reason[:500],
                         "skill_scope": _skill_scope, "scene_scope": _scene_scope,
                         "rebind_candidates": _rebind},
                status="failed", result=_msg, scene_id=scene_id)
            _result["seq"] = _seq or 0
    except Exception as e:
        log.warning(f"Pipeline: _fail_skill_resolve_event failed: {e}")
    log.warning(f"Pipeline: skill resolve failed (fail-fast), skill={skill_name}, "
                f"scene={scene_id}, agent={agent_type}: {reason[:200]}")
    return _result


def run_pipeline(raw_body: bytes, headers: dict, account: str = "",
                 repo_key_override: str = "") -> dict:
    """事件处理管道入口：归一化 → 路由 → 存储 → 返回结果"""
    result = {"seq": 0, "event_type": "", "skill": "", "repo_key": "", "ok": False}

    # 1. 归一化
    event_header, source = get_event_header(headers)
    evt = normalize_event(raw_body, event_header, source)
    evt["account"] = account

    repo_key = repo_key_override or f"{source}:{evt['repository']}"
    # 统一 repo_key：带平台前缀贯穿（gitcode:owner/repo），与 DB 内部一致
    from src.repo_key import to_repo_key, split_owner_repo
    repo_key = to_repo_key(repo_key)
    # account 兜底用仓库 owner 段（无 default 概念；避免全名作为 ACCOUNT）
    if not account and evt.get("repository"):
        _o_acc, _ = split_owner_repo(evt["repository"])
        evt["account"] = _o_acc
    event_type = evt.get("type", "unknown")

    # Issue #996：变更类事件补齐文件清单（payload 优先，provider API 兜底）
    from src.vcs.event_types import FILE_EVENT_TYPES
    if event_type in FILE_EVENT_TYPES:
        try:
            from src.vcs.files_enrich import enrich_event_files
            enrich_event_files(evt, repo_key)
        except Exception as _fe:
            log.warning(f"Pipeline: files enrich failed type={event_type} repo={repo_key}: {_fe}")

    log.info(f"Pipeline: start, raw_type={event_header}, source={source}, repo={evt['repository']}")

    # Issue #89: 提前解析 issue_id 供 skipped 事件审计（所有提前 return 路径共用）
    # Issue #680: 改走统一入口（一次解析存 evt["_resolved_issue_id"]，7 处调用点全链同值）
    _pre_issue_id = _resolve_event_issue_id(evt, repo_key)

    if not evt["repository"] or event_type == "unknown":
        log.info(f"Pipeline skipped: repository='{evt['repository']}' or event_type='{event_type}' not recognized")
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, "unrecognized_event", issue_id=_pre_issue_id)
        return result

    # 提前解析 actor + bot 判定（Issue #318：1.5 预匹配需区分 bot/非 bot）
    actor = resolve_actor(evt.get("payload", {}), evt)
    log.info(f"Pipeline: actor={actor}, event_type={event_type}")
    _bot_usernames_early = []
    if actor:
        from src.user.db import get_all_bot_usernames
        _bot_usernames_early = get_all_bot_usernames()
    _is_bot_actor = actor in _bot_usernames_early if actor else False

    # 1.5 PR 订阅预匹配（仅 bot actor：绕过自循环检测让 bot PR 也能触发 QA；
    #   人类用户跳过预派发，由主路由单次派发——Issue #318 修复双重派发）
    _pr_branch_early = resolve_pr_target_branch(evt)
    if _pr_branch_early and not evt.get("branch"):
        evt["branch"] = _pr_branch_early
    if event_type in PR_EVENT_TYPES:
        # PR 订阅触发条件：
        # - pull_request_opened：新建 PR，必定有首次 commit → 订阅
        # - pull_request_updated：需有新 commit（oldrev 非空或有 commits 字段）→ 订阅，否则跳过
        # - pull_request_merged：已合并，审查无意义 → 不订阅
        _pr_sub_payload = evt.get("payload", {}) or {}
        _pr_sub_action = str(_pr_sub_payload.get("_raw_action", "") or
                             (_pr_sub_payload.get("object_attributes", {}) or {}).get("action", "")).lower()
        if event_type == "pull_request_merged" or _pr_sub_action == "merge":
            log.info(f"Pipeline: PR subscription skip: merged (no need to review)")
            _pr_sub_matches_early = []
        elif _pr_sub_action == "update" and event_type == "pull_request_updated":
            _pr_sub_oa = _pr_sub_payload.get("object_attributes", {}) or {}
            _pr_sub_has_commit = bool(_pr_sub_payload.get("commits")) or bool(_pr_sub_oa.get("oldrev"))
            if not _pr_sub_has_commit:
                log.info(f"Pipeline: PR subscription skip: no_commit_on_pr_update (action=update, no new commit)")
                _pr_sub_matches_early = []
            else:
                _pr_sub_matches_early = _match_pr_subscription(repo_key, evt, event_type, _pr_branch_early)
        else:
            _pr_sub_matches_early = _match_pr_subscription(repo_key, evt, event_type, _pr_branch_early)
        if _pr_sub_matches_early:
            _issue_scene_id_early = ""
            if _pre_issue_id:
                try:
                    from src.api.session_store import get_issue_scene
                    _issue_scene_id_early, _ = get_issue_scene(repo_key, _pre_issue_id)
                except Exception:
                    pass
            # Issue #426：PR 绑定 Issue 即过滤同场景/同 skill 订阅场景（绑 Issue = 问题已被
            # 他人解决，无需再被订阅唤醒），其余场景并行共存
            _issue_qa_skill_early = ""
            if _issue_scene_id_early:
                _issue_qa_skill_early = _scene_qa_skill(repo_key, _issue_scene_id_early, _pr_sub_matches_early)
            _pr_sub_before_filter_early = _pr_sub_matches_early
            _pr_sub_matches_early = _filter_pr_sub_matches(_pr_sub_matches_early, _issue_scene_id_early, bool(_issue_scene_id_early), _issue_qa_skill_early)
            if not _pr_sub_matches_early and _pr_sub_before_filter_early:
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "pr_sub_filtered_bound_issue",
                                     issue_id=_pre_issue_id)
            if _pr_sub_matches_early:
                if _is_bot_actor:
                    _early_dispatched = set()
                    for _extra in _pr_sub_matches_early:
                        _dispatch_pr_subscription_extra(repo_key, evt, event_type, _extra)
                        _early_dispatched.add(_extra[0])
                    evt["_pr_sub_early_dispatched"] = _early_dispatched
                    log.info(f"Pipeline: PR subscription pre-match -> scenes={[m[0] for m in _pr_sub_matches_early]} (bot actor, before self-loop check)")
                else:
                    log.info(f"Pipeline: PR subscription pre-match skipped (actor={actor} not bot, deferred to main route)")

    # 2.0b 开发者指令检测（/done /cancel /resume）+ 澄清/审核回复自动恢复
    # 等待态恢复标记：用户澄清回复把 awaiting_input/pending_review 翻回 processing 后，
    # 本期台账（2.0a）跳过不再成立（线段恢复 > 台账抑制，见 issue-comment-resume-outbound-ledger-fix-design.md）
    _resume_applied = False
    # R20.6.2: 开发者指令检测移到自循环检测之前——用户指令优先处理，不因自循环检测被跳过
    # Issue #453 修复点 2（S4，审查 D1）：指令与状态变更通道身份前置校验——bot actor
    # （含 agent 引用用户原文的回声，原文里的指令关键词）不参与指令解析与澄清恢复，
    # 否则 bot 评论含"确认完成"仍会先执行 confirm_issue_done 再被 1418 入口拦截
    # （副作用已发生）。仅人类 actor 的指令语义生效。
    if event_type in ("issue_comment", "note_hook_pr") and not _is_bot_actor:
        try:
            _payload = evt.get("payload", {}) or {}
            _oa = _payload.get("object_attributes", {}) or {}
            _note = str(_oa.get("note") or (_payload.get("comment") or {}).get("body") or _payload.get("body") or "")

            # Issue #51: 开发者指令检测（/done /cancel /resume）
            _note_lower = _note.lower().strip()
            _issue_num = ""
            try:
                # GitCode Note Hook 的 issue 号在 noteable_iid（iid 是评论自身的 id，非 issue 号）——
                # 与 resolve_issue_id（对齐 noteable_iid）语义一致，漏读会导致恢复/指令永不到达
                # （issue-comment-resume-outbound-ledger-fix-design.md RC-B）
                _issue_num = str(_oa.get("iid") or _oa.get("number") or _oa.get("noteable_iid")
                                 or (_payload.get("issue", {}) or {}).get("number") or "").lstrip("#")
            except Exception:
                pass
            if _issue_num and _issue_num.isdigit():
                _issue_num_int = int(_issue_num)
                if any(kw in _note_lower for kw in ["/done", "完成任务", "任务完成", "确认完成"]):
                    from src.api.issues_center import confirm_issue_done
                    confirm_issue_done(repo_key, _issue_num_int, confirmed_by="issue_comment")
                    log.info(f"Issue #{_issue_num} confirmed done via issue comment")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "user_confirm_done")
                    return result
                elif any(kw in _note_lower for kw in ["/cancel", "取消任务"]):
                    from src.api.issues_center import cancel_issue_task
                    cancel_issue_task(repo_key, _issue_num_int)
                    log.info(f"Issue #{_issue_num} cancelled via issue comment")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "user_cancel")
                    return result
                elif any(kw in _note_lower for kw in ["/resume", "恢复任务"]):
                    from src.api.issues_center import resume_issue_task
                    resume_issue_task(repo_key, _issue_num_int)
                    log.info(f"Issue #{_issue_num} resumed via issue comment")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "user_resume")
                    return result

            # 澄清/审核回复自动恢复（§3.6/§3.5）：非 bot 用户评论且 issue 处 awaiting_input 或 pending_review →
            # 恢复 processing。bot 自评在下方 2.0 自循环检测跳过；note_hook_pr 不适用（PR 讨论非 issue 澄清）。
            if event_type == "issue_comment" and _issue_num and _issue_num.isdigit() and int(_issue_num) > 0:
                try:
                    from src.api.issues_center import _resume_issue_waiting
                    if _resume_issue_waiting(repo_key, int(_issue_num)):
                        _resume_applied = True  # 等待态恢复已消费用户输入 → 2.0a 台账跳过不成立
                        log.info(f"Pipeline: issue #{_issue_num} awaiting/pending_review → processing (issue_comment reply)")
                except Exception as _re:
                    log.warning(f"Pipeline: issue_comment resume failed: {_re}")
        except Exception as e:
            log.warning(f"Pipeline: developer command / resume check failed: {e}")

    # 2.0 自循环检测（R20.6.1/R18.6.8: 统一走 handler.is_self_loop_actor()）
    # 路径 A（bot token）：actor in bot_usernames → skip
    # 路径 B（user token）：四条件组合判断 → skip
    # 通过 resolve_config_level 获取配置级别，创建对应 handler 判断
    if actor and repo_key:
        # Issue #453: 评论类事件入口统一拦截——actor 命中 bot 用户名表（managed_repos.bot_username
        # + 全局 bot）即自循环，不依赖 resolve_config_level 的级别分派。背景：issue-372 的
        # read_stored_token() 会把托管人 PAT 抄写进 devops_agent_session，使新托管仓库的级别
        # 解析返回 session → SessionTokenHandler.is_self_loop_actor 降级不检测 → bot 评论闭环死循环。
        # 本拦截在 2.0b 之后（保留开发者指令/澄清恢复处理），仅覆盖评论族 issue_comment/note_hook_pr；
        # issue_opened 的「## 父任务」子任务豁免与 1.5 PR 订阅预匹配（pull_request_* 族）不受影响。
        if event_type in ("issue_comment", "note_hook_pr") and _is_bot_actor:
            log.info(f"Pipeline skip: self-loop detected (actor={actor} in bot_usernames, level-independent, repo={repo_key})")
            from src.api.event_store import append_skipped_event
            append_skipped_event(repo_key, event_type, "self_loop", actor, issue_id=_pre_issue_id)
            return result
        try:
            from src.token_resolver import TokenResolver, create_provider_from_repo_key, create_action_handler
            _provider = create_provider_from_repo_key(repo_key)
            _resolver = TokenResolver(_provider)
            _config_result = _resolver.resolve_config_level(repo_key)
            _config_result.issue_id = _pre_issue_id or ""
            if _config_result.level:
                _handler = create_action_handler(_config_result)
                if _handler:
                    # 多任务豁免：bot 创建的子任务 issue_opened（body 含「## 父任务」）不跳过
                    _allow_through = False
                    if event_type == "issue_opened" and _is_bot_actor:
                        # Issue #565: bot 创建的建单 issue_opened 一律放行 → 进入流水线触发
                        # 智能推荐（VOD 判定场景写 scene_id）。评论族拦截（上方）不动，
                        # 防死循环最后闸门保留。
                        _allow_through = True
                        evt["_bot_opened"] = True  # 推荐完成回调据此短路执行棒（不重派 VOD）
                    elif event_type == "issue_opened" and _config_result.level != "user":
                        try:
                            _payload = evt.get("payload", {}) or {}
                            _oa = _payload.get("object_attributes", {}) or {}
                            _body = _oa.get("description", "") or ""
                            if "## 父任务" in _body:
                                _allow_through = True
                        except Exception:
                            pass
                    if _allow_through:
                        if evt.get("_bot_opened"):
                            log.info(f"Pipeline: bot={actor} opened issue, allow through for scene recommend (Issue #565)")
                        else:
                            log.info(f"Pipeline: bot={actor} created subtask issue, allow through")
                    else:
                        _is_self_loop = _handler.is_self_loop_actor(actor, event_type, _config_result)
                        if _is_self_loop:
                            _loop_type = "self_loop_path_b" if _config_result.level == "user" else "self_loop"
                            log.info(f"Pipeline skip: self-loop detected (actor={actor}, level={_config_result.level}, repo={repo_key})")
                            from src.api.event_store import append_skipped_event
                            append_skipped_event(repo_key, event_type, _loop_type, actor, issue_id=_pre_issue_id)
                            return result
        except Exception as _e:
            # 降级：handler 调用失败时 fallback 到原有逻辑
            log.warning(f"Pipeline: handler.is_self_loop_actor failed, fallback to legacy check: {_e}")
            # fallback: 路径 A 原有检测
            bot_usernames = _bot_usernames_early
            if actor in bot_usernames:
                _allow_through = False
                if event_type == "issue_opened":
                    # Issue #565: bot 建单 issue_opened 一律放行（与主路径同构）→ 智能推荐
                    _allow_through = True
                    evt["_bot_opened"] = True
                if _allow_through:
                    log.info(f"Pipeline: bot={actor} opened issue, allow through for scene recommend (fallback, Issue #565)")
                else:
                    log.info(f"Pipeline skip: bot={actor} self-action (fallback)")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "self_loop", actor, issue_id=_pre_issue_id)
                    return result
            # fallback: 路径 B 原有检测
            if _is_path_b_self_loop(repo_key, actor, event_type, _pre_issue_id or ""):
                log.info(f"Pipeline skip: path B self-loop detected (fallback, actor={actor}, repo={repo_key})")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "self_loop_path_b", actor, issue_id=_pre_issue_id)
                return result

    # 2.0a 系统回帖标记检测：含 <!-- grape-auto-comment --> 的评论是系统自动回帖的回声，跳过
    # Issue #372: 标记检测在自循环检测之后——自循环检测优先处理 bot actor，标记检测处理系统回帖回声
    # Issue #453 代发台账（读侧）：查表插在 marker 检测之前——台账是主判定（精确 ID 命中），
    # marker 降级为 fallback（GitHub 等不净化 HTML 注释的平台仍有效；GitCode 必 miss）。
    # 查表异常/未命中一律放行，兜底判断（bot 名单，2.0 节已先行）接手。
    # issue_opened 特例（S16-3）：「## 父任务」多任务豁免优先于台账——我们建的子任务 issue 在台账
    # 必有记录，但多任务模式恰恰依赖"bot 建子 issue → webhook 触发子流水线"，先判豁免再查表。
    # _grape_internal_trigger：建单入口的同步直调（issue 刚入账、立即要开工）——跳过台账判定。
    if event_type == "issue_opened":
        try:
            _payload = evt.get("payload", {}) or {}
            if not _payload.get("_grape_internal_trigger"):
                _oa_io = _payload.get("object_attributes", {}) or {}
                _body_io = str(_oa_io.get("description") or _payload.get("issue", {}).get("body") or "")
                if "## 父任务" not in _body_io:
                    from src.repo_key import parse_repo_key
                    _provider_name = parse_repo_key(repo_key)[0] if repo_key else ""
                    from src.outbound_ledger import is_our_outbound
                    _issue_key = str(_oa_io.get("iid") or _oa_io.get("number")
                                     or (_payload.get("issue", {}) or {}).get("number") or "")
                    if _issue_key and is_our_outbound(_provider_name, repo_key, "issue", _issue_key):
                        log.info(f"Pipeline skip: outbound ledger hit (issue_opened, issue={_issue_key}, repo={repo_key})")
                        from src.api.event_store import append_skipped_event
                        append_skipped_event(repo_key, event_type, "outbound_ledger_hit", issue_id=_pre_issue_id or _issue_key)
                        return result
        except Exception as e:
            log.warning(f"Pipeline: outbound ledger issue_opened check failed (allow-through): {e}")
    # 2.0c Issue #565: bot 建单推荐仓级滑动窗口限流——放行后、路由判定前。
    # 同 repo_key 60s 内最多 _BOT_OPEN_RECOMMEND_LIMIT 条 bot 建单进推荐（防 bot 批量建单打爆模型调用），
    # 超限记 event_skipped(bot_open_recommend_rate_limited) 并终止（进程内 dict，不进 DB）。
    # 「## 父任务」子任务开单是多任务流水线内部通道，不占推荐配额。
    if event_type == "issue_opened" and _is_bot_actor:
        _rl_subtask = False
        try:
            _rl_payload = evt.get("payload", {}) or {}
            _rl_body = str((_rl_payload.get("object_attributes", {}) or {}).get("description", "")
                           or (_rl_payload.get("issue", {}) or {}).get("body") or "")
            if "## 父任务" in _rl_body:
                _rl_subtask = True
        except Exception:
            pass
        if not _rl_subtask and _bot_open_recommend_rate_limited(repo_key):
            log.info(f"Pipeline skip: bot open recommend rate limited (repo={repo_key}, actor={actor}, issue={_pre_issue_id})")
            from src.api.event_store import append_skipped_event
            append_skipped_event(repo_key, event_type, "bot_open_recommend_rate_limited", actor, issue_id=_pre_issue_id)
            return result
    # Issue #453 台账（读侧新增）：issue_closed / PR 族——关闭与 PR 事件的
    # 回流对账（老问题 3/26 顺手覆盖）。查表异常一律放行，PR 订阅预匹配（1.5 节）不受影响。
    # S19：issue_reopened / issue_assigned / issue_labeled 元数据回声——update_issue
    # 非 body 修改记 issue_meta（编号键），命中即拦（bot 接管/reopen/打标签回声）。
    # S24 例外（对齐 1.5 预匹配语义）：bot 建的 PR 必须放行给 QA 审查——PR opened 是
    # 流水线必需环节（coding→qa），bot 动作回流默认拦唯独此通道保留（问题 3 原设计）。
    # S26/E2 精确语义：pull_request_updated 命中台账时若有新 commit（oldrev/commits）
    # 放行给 qa 复审（Issue #46 QA↔coding 回流闭环）——拦回声（纯文本编辑）不拦流转。
    # 审查 D2 修正：事件名对齐 event_types.py 真实值（pull_request_opened/updated/merged，
    # 原 pr_opened/pr_updated/pr_merged 为不存在的假名——该段曾是死代码）。
    if event_type in ("issue_closed", "pull_request_opened", "pull_request_updated", "pull_request_merged",
                      "issue_reopened", "issue_assigned", "issue_labeled"):
        try:
            _payload = evt.get("payload", {}) or {}
            _pr_opened_exempt = (event_type == "pull_request_opened" and _is_bot_actor)
            _pr_update_has_commit = False
            if event_type == "pull_request_updated":
                _oa_pc = _payload.get("object_attributes", {}) or {}
                _pr_update_has_commit = bool(_payload.get("commits")) or bool(_oa_pc.get("oldrev"))
            if not _pr_opened_exempt and not _pr_update_has_commit:
                from src.repo_key import parse_repo_key
                _provider_name = parse_repo_key(repo_key)[0] if repo_key else ""
                from src.outbound_ledger import is_our_outbound
                _oa_cl = _payload.get("object_attributes", {}) or {}
                _num_cl = str(_oa_cl.get("iid") or _oa_cl.get("number")
                              or (_payload.get("issue", {}) or {}).get("number") or "")
                if _num_cl:
                    if event_type == "issue_closed":
                        _ct = "issue_close"
                    elif "merge" in event_type:
                        _ct = "pr_merge"
                    elif event_type.startswith("pr") or event_type.startswith("pull_request"):
                        _ct = "pr"
                    else:
                        _ct = "issue_meta"  # reopened / assigned / labeled
                    if is_our_outbound(_provider_name, repo_key, _ct, _num_cl):
                        log.info(f"Pipeline skip: outbound ledger hit ({_ct}, num={_num_cl}, repo={repo_key})")
                        from src.api.event_store import append_skipped_event
                        append_skipped_event(repo_key, event_type, "outbound_ledger_hit", issue_id=_pre_issue_id or _num_cl)
                        return result
        except Exception as e:
            log.warning(f"Pipeline: outbound ledger close/pr check failed (allow-through): {e}")
    if event_type in ("issue_comment", "note_hook_pr"):
        try:
            _payload = evt.get("payload", {}) or {}
            from src.repo_key import parse_repo_key
            _provider_name = parse_repo_key(repo_key)[0] if repo_key else ""
            from src.outbound_ledger import is_our_outbound, extract_event_content_id, content_hash_md5
            _cid = extract_event_content_id(_provider_name, _payload)
            _note = str((_payload.get("object_attributes") or {}).get("note")
                        or (_payload.get("comment") or {}).get("body") or _payload.get("body") or "")
            if _cid and is_our_outbound(_provider_name, repo_key, "issue_comment", _cid,
                                        content_hash=content_hash_md5(_note)):
                if event_type == "issue_comment" and _resume_applied:
                    # 等待态用户澄清回复已消费（awaiting_input/pending_review → processing）：
                    # 台账记录源于「经 grape VCS 代理（git-repo API）代发的用户回复」，非 bot/系统自回帖，
                    # 恢复优先于台账抑制（issue-comment-resume-outbound-ledger-fix-design.md RC-A）。bot 自回帖
                    # 已在更早的 2.0 自循环检测被拦，不会走到此豁免。
                    log.info(f"Pipeline: outbound ledger hit but resume supersedes "
                             f"(user clarification reply consumed for issue={_pre_issue_id}), allow through")
                else:
                    log.info(f"Pipeline skip: outbound ledger hit (id={_cid}, repo={repo_key}, event={event_type})")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "outbound_ledger_hit", issue_id=_pre_issue_id)
                    return result
        except Exception as e:
            log.warning(f"Pipeline: outbound ledger check failed (allow-through): {e}")
        try:
            _payload = evt.get("payload", {}) or {}
            _oa = _payload.get("object_attributes", {}) or {}
            _note = str(_oa.get("note") or (_payload.get("comment") or {}).get("body") or _payload.get("body") or "")
            if "<!-- grape-auto-comment -->" in _note:
                log.info(f"Pipeline skip: system auto-comment marker detected, event={event_type}")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "system_auto_comment", issue_id=_pre_issue_id)
                return result
        except Exception as e:
            log.warning(f"Pipeline: auto-comment marker check failed: {e}")

    # 2.0a2 R12.9: issue_updated description 编辑标记检测——agent 编辑 issue description 的回声
    # Issue #453 代发台账（读侧，S14）：编辑回声无 comment_id，按 (repo+剥离 marker 指纹) 对账；
    # 指纹不匹配只告警不拦（is_our_outbound 内部实现），ID 精确命中才拦。
    if event_type == "issue_updated":
        try:
            _payload = evt.get("payload", {}) or {}
            from src.repo_key import parse_repo_key
            _provider_name = parse_repo_key(repo_key)[0] if repo_key else ""
            from src.outbound_ledger import is_our_outbound, content_hash_md5
            _oa_ed = _payload.get("object_attributes", {}) or {}
            _desc_ed = str(_oa_ed.get("description") or "")
            _edit_key = str(_oa_ed.get("iid") or _oa_ed.get("number")
                            or (_payload.get("issue", {}) or {}).get("number") or "")
            if _desc_ed and _edit_key:
                if is_our_outbound(_provider_name, repo_key, "issue_edit", _edit_key,
                                   content_hash=content_hash_md5(_desc_ed)):
                    log.info(f"Pipeline skip: outbound ledger hit (issue_edit, issue={_edit_key}, repo={repo_key})")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "outbound_ledger_hit", issue_id=_pre_issue_id)
                    return result
        except Exception as e:
            log.warning(f"Pipeline: outbound ledger issue_edit check failed (allow-through): {e}")
        try:
            _payload = evt.get("payload", {}) or {}
            _oa = _payload.get("object_attributes", {}) or {}
            _desc = str(_oa.get("description") or "")
            if not _desc:
                # GitHub: changes.body.from
                _changes = _payload.get("changes", {}) or {}
                _desc = str((_changes.get("body") or {}).get("from") or "")
            if "<!-- grape-auto-comment -->" in _desc:
                log.info(f"Pipeline skip: auto-comment marker in issue_updated description, event={event_type}")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "system_auto_comment", issue_id=_pre_issue_id)
                return result
        except Exception as e:
            log.warning(f"Pipeline: issue_updated marker check failed: {e}")

    # 2.1 issue_updated 纯标签变更（changes.labels 有变化、其余内容未动）→ bot 打标签回声，跳过
    if event_type == "issue_updated":
        try:
            _payload = evt.get("payload", {}) or {}
            _changes = _payload.get("changes", {}) or {}
            _content_keys = [k for k in ("title", "description") if k in _changes]
            _label_changed = "labels" in _changes
            if _label_changed and not _content_keys:
                log.info(f"Pipeline skip: issue_updated with only label change (likely bot label echo)")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "label_only_change", issue_id=_pre_issue_id)
                return result
        except Exception as e:
            log.warning(f"Pipeline: issue_updated label-only check failed: {e}")

    # 2.2 L1 事件准入过滤（Issue #49，可配置规则 event_action_filter）
    # 按 action + 操作者 + changes 判定：过滤无意义动作、edit 需 description 变更、PR update 按操作者、
    # close 副作用-only（执行关闭后短路不进入 agent）。
    try:
        from src.api.event_rule import evaluate_event_action
        _vcs_source = evt.get("source", "") or ""
        _d = evaluate_event_action(event_type, evt.get("payload", {}) or {}, vcs_source=_vcs_source)
        if _d["decision"] == "skip":
            log.info(f"Pipeline skip: {_d['reason']} (event_action_filter, actor={_d['actor_type']})")
            from src.api.event_store import append_skipped_event
            append_skipped_event(repo_key, event_type, _d["reason"], issue_id=_pre_issue_id)
            return result
        if _d["decision"] == "side_effect":
            # 执行副作用后短路：close → close_issue_tasks
            if _d.get("side_effect") == "close_issue_tasks":
                try:
                    from src.api.issues_center import close_issue_tasks
                    _ce_id = (evt.get("payload", {}) or {}).get("object_attributes", {}) or {}
                    _ce_num = str(_ce_id.get("iid") or _ce_id.get("number") or "").lstrip("#")
                    if _ce_num.isdigit() and int(_ce_num) > 0:
                        _ce_n = close_issue_tasks(repo_key, int(_ce_num))
                        log.info(f"Pipeline (L1 side_effect): closed tasks for issue #{_ce_num} ({_ce_n} rows)")
                except Exception as _ce:
                    log.warning(f"Pipeline L1 side_effect close_issue_tasks failed: {_ce}")
            else:
                log.info(f"Pipeline side_effect: {_d['side_effect'] or 'n/a'} (event_action_filter)")
            from src.api.event_store import append_skipped_event
            append_skipped_event(repo_key, event_type, f"side_effect:{_d.get('side_effect', '')}",
                                 issue_id=_pre_issue_id)
            return result
    except Exception as e:
        log.warning(f"Pipeline: event_action_filter check failed: {e}")

    # 3. 路由判定（D9 阶段矩阵 + 非 Issue 静态映射，替代 agent_type.subscribe_events，见 vod-skill-upgrade-plan §3.0.4）
    _is_scene_recommend = False  # 智能推荐模式标记
    if event_type == "issue_opened":
        # Issue #627 方案 C：发布子单由发布入口直通 sre（创建时已 stage=sre + 派发 sre a2a），
        # 平台 webhook 回声不再路由 vod/智能推荐，避免二次派发污染子单时间线。
        _rel_rid = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
        if _rel_rid:
            try:
                from src.api.issues_center import is_release_subissue
                if is_release_subissue(repo_key, _rel_rid):
                    log.info(f"Pipeline: release sub-issue {_rel_rid} skip vod (direct sre dispatch, Issue #627 Plan C)")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "release_subissue", issue_id=_rel_rid)
                    return result
            except Exception as _rel_e:
                log.warning(f"Pipeline: release sub-issue check failed, fallback to vod: {_rel_e}")
        agent_type = "vod"
        log.info(f"Pipeline: issue_opened -> VOD (stage init)")
        # 判断是否需要推荐：issue_state 有 scene_id → 用户已选场景，跳过推荐
        # GitCode 直接创建的 Issue 无 scene_id → 走智能推荐
        # Task Center 创建 Issue 后 GitCode webhook 可能比 set_issue_scene 先到达，
        # 最多重试 3 次、每次间隔 0.5s，等 scene_id 写入
        _rid_early = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
        _is_scene_recommend = True  # 默认走推荐
        if _rid_early:
            import time as _time
            for _attempt in range(3):
                try:
                    # Issue #681: 推荐入口判定只认真实绑定（issue_state.scene_id → issue 表
                    # scene_id），不回退 managed_repos.dev_scene（dev_scene 仅作展示兜底，
                    # 见 #415）；用 get_issue_scene 会被 dev_scene 短路、整体跳过推荐。
                    from src.api.session_store import get_issue_bound_scene
                    _existing_scene, _ = get_issue_bound_scene(repo_key, _rid_early)
                    if _existing_scene:
                        _is_scene_recommend = False
                        log.info(f"Pipeline: issue_opened 已有场景 {_existing_scene}（attempt={_attempt+1}），跳过推荐")
                        break
                    if _attempt < 2:
                        _time.sleep(0.5)
                except Exception as _re:
                    log.warning(f"Pipeline: issue_opened scene check failed: {_re}")
                    break
        # project-info 后台刷新（服务端 codegraph 生成基线，不阻塞 webhook；见 codegraph-builder-plan.md）
        # Issue #200: 传入 bot token，私有仓库 git fetch 需认证 header
        try:
            from src.project_info.builder import maybe_refresh_project_info
            from src.repo import Repo
            _pi_token = Repo(repo_key).bot_token
            threading.Thread(target=maybe_refresh_project_info, args=(repo_key, _pi_token), daemon=True).start()
        except Exception as e:
            log.warning(f"Pipeline: project-info refresh trigger failed: {e}")
    else:
        if event_type in PR_EVENT_TYPES:
            _default = PR_DEFAULT_AGENT
        elif event_type in ISSUE_EVENT_TYPES:
            _default = "issue"          # Issue 类事件无关联 issue 时默认 Issue Agent（审视）
        else:
            _default = NON_ISSUE_EVENT_ROUTE.get(event_type, "")
        agent_type = _resolve_issue_stage_agent(repo_key, event_type, evt.get("payload", {}) or {}, _default, evt=evt)
        log.info(f"Pipeline: routing result -> agent_type='{agent_type}'")
    # 仓库级事件订阅匹配（提前，供 fallback 与双路径共用）：
    # - 主路由为空 → 订阅兜底（现状）
    # - 主路由非空且订阅命中不同 Agent → 双路径并行（event-routing-subscription-design.md §2.2）
    _pr_branch = resolve_pr_target_branch(evt)
    if _pr_branch and not evt.get("branch"):
        evt["branch"] = _pr_branch
    # PR 订阅与 stage matrix 路由并行共存（Issue #426：PR 关联 Issue 时不再整体放弃）：
    # - 主路由为空（PR 无关联 Issue）→ 第一个匹配作为主 agent（原行为）
    # - 主路由非空（PR 关联 Issue，stage matrix 已设 agent_type）→ 订阅场景全部走
    #   _dispatch_pr_subscription_extra 并行派发（a2a_event），与主路由 agent 互不干扰；
    #   仅当主路由已派发 QA 时跳过同场景订阅（避免同场景重复审查）
    # 1.5 预匹配仅 bot actor 时预派发（Issue #318），
    # 人类用户只走主路由单次派发。
    if event_type in PR_EVENT_TYPES:
        # PR 订阅触发条件（与 1.5 预匹配口径一致）：
        # - pull_request_merged：已合并，审查无意义 → 不订阅
        # - pull_request_updated 无新 commit → 不订阅
        _pr_sub_payload = evt.get("payload", {}) or {}
        _pr_sub_action = str(_pr_sub_payload.get("_raw_action", "") or
                             (_pr_sub_payload.get("object_attributes", {}) or {}).get("action", "")).lower()
        if event_type == "pull_request_merged" or _pr_sub_action == "merge":
            _pr_sub_matches = []
        elif _pr_sub_action == "update" and event_type == "pull_request_updated":
            _pr_sub_oa = _pr_sub_payload.get("object_attributes", {}) or {}
            _pr_sub_has_commit = bool(_pr_sub_payload.get("commits")) or bool(_pr_sub_oa.get("oldrev"))
            _pr_sub_matches = (_match_pr_subscription(repo_key, evt, event_type, _pr_branch)
                               if _pr_sub_has_commit else [])
        else:
            _pr_sub_matches = _match_pr_subscription(repo_key, evt, event_type, _pr_branch)
        if _pr_sub_matches:
            # Issue #426：仅当主路由已派发 QA 时排除同场景（原为无条件排除 Issue 场景）
            _issue_scene_id = ""
            if _pre_issue_id:
                try:
                    from src.api.session_store import get_issue_scene
                    _issue_scene_id, _ = get_issue_scene(repo_key, _pre_issue_id)
                except Exception:
                    pass
            # Issue #426：PR 绑定 Issue 即过滤同场景/同 skill 订阅场景
            _pr_sub_matches_before_filter = _pr_sub_matches
            _pr_sub_matches = _filter_pr_sub_matches(
                _pr_sub_matches, _issue_scene_id, bool(_issue_scene_id),
                (_scene_qa_skill(repo_key, _issue_scene_id, _pr_sub_matches)
                 if _issue_scene_id else ""))
            if not _pr_sub_matches and _pr_sub_matches_before_filter:
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "pr_sub_filtered_bound_issue",
                                     issue_id=_pre_issue_id)
            # Issue #318 兜底去重：排除 1.5 预匹配已派发的场景（防御 bot actor 未被自循环拦截的边缘情况）
            _early_dispatched = evt.get("_pr_sub_early_dispatched")
            if _early_dispatched:
                _pr_sub_matches = [m for m in _pr_sub_matches if m[0] not in _early_dispatched]
            if _pr_sub_matches:
                if not agent_type:
                    _first = _pr_sub_matches[0]
                    evt["_pr_sub_scene_id"] = _first[0]
                    evt["_pr_sub_skill"] = _first[1]
                    evt["_pr_sub_engine_ids"] = _first[2]
                    if _first[3]:
                        evt["_pr_sub_sre_skill"] = _first[3]
                        evt["_pr_sub_sre_engine_ids"] = _first[4]
                    # 多场景补派（#318 回归修复）：人类用户只走主路由，若只取 _first
                    # 则第 2+ 场景失去唯一触发通道（修复前靠 1.5 预匹配 bug 路径捎带）。
                    # 对称 bot 路径的 1.5 预派发：剩余场景走 _dispatch_pr_subscription_extra
                    # 并行派发（a2a_event）。单场景时 [1:] 为空，不产生额外事件（禁令：
                    # 单场景不得双重触发——#318 修复成果的锁死不变量）。
                    # bot actor 不可达此分支（自循环检测已 return），行为零变化。
                    for _extra in _pr_sub_matches[1:]:
                        _dispatch_pr_subscription_extra(repo_key, evt, event_type, _extra)
                    agent_type = "qa"
                    log.info(f"Pipeline: PR subscription triggered -> scene={_first[0]} agent=qa branch={_pr_branch}")
                else:
                    # Issue #426：主路由已由 stage matrix 设定（PR 关联 Issue）——
                    # PR 订阅场景全部并行派发（a2a_event），主路由 agent 正常处理
                    for _extra in _pr_sub_matches:
                        _dispatch_pr_subscription_extra(repo_key, evt, event_type, _extra)
                    log.info(f"Pipeline: PR subscription parallel dispatch -> scenes={[m[0] for m in _pr_sub_matches]} (main='{agent_type}')")
    _sub_agent = _match_repo_subscription(repo_key, event_type, _pr_branch)
    if not agent_type:
        # 主路由为空 → 订阅兜底
        if _sub_agent:
            agent_type = _sub_agent
            log.info(f"Pipeline: repo subscription matched (fallback) -> agent_type='{_sub_agent}'")
    elif _sub_agent and _sub_agent != agent_type:
        # 双路径并行：主路由 agent 正常处理 + 订阅 Agent 独立分派
        _dispatch_subscription_agent(repo_key, evt, event_type, _sub_agent, agent_type)
        log.info(f"Pipeline: subscription double-path -> extra event for '{_sub_agent}' (main='{agent_type}')")
    if not agent_type and event_type == "pull_request_merged":
        # Issue #544 L4：merge 订阅兜底推广——所有启用 A2A 的仓库，PR 合并默认 sre 收尾。
        # 原先仅显式配置 sre 订阅的仓库在 merge 时触发 sre；现无任何路由命中时默认派发
        # （sre agent 被禁用的仓库除外）。配合 #496 派发级重试 + 断链 Issue 提示，
        # 消除「merge webhook 一次性投递 + 无兜底 → 收尾断链」。
        try:
            from src.api.session_store import is_agent_enabled as _iae_l4
            if _iae_l4(repo_key, "sre"):
                agent_type = "sre"
                log.info(f"Pipeline: merged-PR default sre fallback (Issue #544 L4), repo={repo_key}")
        except Exception as _l4_e:
            log.warning(f"Pipeline: merged-PR sre fallback check failed: {_l4_e}")
    if not agent_type:
        _reason = "issue_done" if (event_type in ISSUE_EVENT_TYPES or event_type in PR_EVENT_TYPES) else "no_route"
        log.info(f"Pipeline: no route for type={event_type} ({_reason}), skip")
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, _reason, issue_id=_pre_issue_id)
        return result

    # 3.1 Issue 生命周期阶段初始化（D8/D9）：issue_opened 建 vod 阶段；其余命中 vod 且无记录时补建
    from src.api.session_store import set_issue_stage
    if event_type == "issue_opened":
        _rid = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
        if _rid:
            _asg = _fetch_issue_assignee(repo_key, _rid)
            # Issue #49: assignee 为空时 fallback 到 issue 创建者（actor），而非仓库 owner
            if not _asg:
                _asg = actor or ""
                if _asg:
                    log.info(f"Pipeline: issue_assignee API empty, fallback to actor={_asg} for {_rid}")
            set_issue_stage(repo_key, _rid, "vod", "vod", assignee=_asg)
            log.info(f"Pipeline: issue_state init vod for {_rid} assignee={_asg or 'none'}")
            # 子任务识别：Issue body 含 "## 父任务" + Issue 链接 → 标记 parent_issue_id
            _parent = _parse_parent_issue_from_payload(evt.get("payload", {}) or {})
            if _parent:
                try:
                    from src.api.session_store import set_parent_issue
                    set_parent_issue(repo_key, _rid, _parent)
                    log.info(f"Pipeline: subtask detected, issue={_rid} parent={_parent}")
                except Exception:
                    pass
    elif agent_type == "vod":
        from src.api.session_store import get_issue_stage
        _rid2 = resolve_issue_id(evt.get("payload", {}) or {}) or evt.get("issue_id", "")
        if _rid2 and not get_issue_stage(repo_key, _rid2):
            set_issue_stage(repo_key, _rid2, "vod", "vod")
            log.info(f"Pipeline: issue_state init vod for {_rid2}")
    # PR merged 跳过 QA 直接路由 SRE：补推 stage qa→sre（原由 QA A2A handoff 推进）
    if event_type == "pull_request_merged" and agent_type == "sre":
        # Issue #680: 改走统一入口（一次解析全链同值）；带 repo token 触发
        # PR 详情 API 兜底（分支/标题/issues 列表全空时）。merge payload 的
        # object_attributes.iid 是 PR 号，resolve_issue_id 会跳过（其 docstring
        # 自警告此陷阱）→ PR 类事件必须先走 resolve_pr_issue_id 规则链。
        try:
            from src.user.db import get_repo_token_with_refresh
            _sre_api_token = get_repo_token_with_refresh(repo_key) or ""
        except Exception:
            _sre_api_token = ""
        _rid_sre = _resolve_event_issue_id(evt, repo_key, api_token=_sre_api_token)
        if _rid_sre:
            from src.api.session_store import get_issue_stage as _gis
            _st_sre = _gis(repo_key, _rid_sre)
            if _st_sre and _st_sre.get("stage") == "qa":
                # Issue #1573: PR 合并视同审核通过——qa 阶段 pending_review 时用户直接合并
                # PR 会将卡片永久留在待审核列；此处先恢复 processing，失败降级不阻断后续推进。
                try:
                    from src.api.session_store import get_issue_status as _gist
                    if _gist(repo_key, _rid_sre) == "pending_review":
                        _num_sre = str(_rid_sre).lstrip("#")
                        if _num_sre.isdigit():
                            from src.api.issues_center import _resume_issue_waiting
                            if _resume_issue_waiting(repo_key, int(_num_sre)):
                                log.info(f"Pipeline: PR merged treated as review pass, "
                                         f"pending_review→processing for {_rid_sre}")
                except Exception as _resume_e:
                    log.warning(f"Pipeline: PR merged resume pending_review failed "
                                f"for {_rid_sre}: {_resume_e}")
                set_issue_stage(repo_key, _rid_sre, "sre", "sre")
                log.info(f"Pipeline: PR merged skip QA, advance stage qa→sre for {_rid_sre}")

    # 3.2 任务中心全覆盖（task-center-issue-coverage-design.md §3-A）：
    # Issue 事件幂等建档；closed 动作自动完成对应任务（P5 webhook 兜底）。
    # 旁路：失败仅告警，不阻塞事件处理。
    try:
        from src.api.issues_center import ensure_issue_task, close_issue_tasks
        _iid3 = resolve_issue_id(evt.get("payload", {}) or {}) or evt.get("issue_id", "") or ""
        if _iid3:
            _num3 = str(_iid3).lstrip("#")
            if _num3.isdigit() and int(_num3) > 0:
                _p3 = evt.get("payload", {}) or {}
                _o3 = _p3.get("object_attributes", {}) or {}
                _act3 = str(_o3.get("action", "") or "").lower()
                _closed = _act3 in ("close", "closed") or str(_o3.get("state", "") or "").lower() == "closed"
                if _closed:
                    _n3 = close_issue_tasks(repo_key, int(_num3))
                    log.info(f"Pipeline: issue closed -> complete tasks #{_num3} ({_n3} rows)")
                elif event_type in ISSUE_EVENT_TYPES:
                    _asg3 = ""
                    try:
                        _al3 = _p3.get("assignees") or []
                        if isinstance(_al3, list) and _al3 and isinstance(_al3[0], dict):
                            _asg3 = _al3[0].get("username") or _al3[0].get("login") or ""
                    except Exception:
                        pass
                    if not _asg3:
                        try:
                            _asg3 = _fetch_issue_assignee(repo_key, _iid3) or ""
                        except Exception:
                            pass
                    # Issue #178: 提取 webhook 操作用户（sender）作为 assignee fallback
                    _sender3 = ""
                    try:
                        _snd = _p3.get("sender") or _p3.get("user") or {}
                        if isinstance(_snd, dict):
                            _sender3 = _snd.get("username") or _snd.get("login") or ""
                    except Exception:
                        pass
                    # Issue #805: 提取创建人（creator）——优先 payload 原生 user，逐级兜底：
                    # object_attributes/user → issue/user → VCS 详情（仅 open 类事件，避免每事件一次 API）
                    # → sender。创建人建档固化（created_by 仅空补写），不随 assignee 流转变化。
                    _creator3 = ""
                    try:
                        _cu3 = _o3.get("user") if isinstance(_o3.get("user"), dict) else None
                        if not _cu3:
                            _ci3 = _p3.get("issue") or {}
                            _cu3 = _ci3.get("user") if isinstance(_ci3, dict) and isinstance(_ci3.get("user"), dict) else None
                        if _cu3:
                            _creator3 = _cu3.get("username") or _cu3.get("login") or ""
                    except Exception:
                        pass
                    if not _creator3 and _act3 in ("open", "create", "opened", "created"):
                        try:
                            _st_v, _data_v = fetch_issue_via_vcs(repo_key, int(_num3))
                            _u_v = (_data_v or {}).get("user") or {}
                            if isinstance(_u_v, dict):
                                _creator3 = _u_v.get("username") or _u_v.get("login") or ""
                        except Exception:
                            pass
                    if not _creator3:
                        _creator3 = _sender3
                    _t3 = str(_o3.get("title", "") or "") if isinstance(_o3, dict) else ""
                    if not _t3:
                        _t3 = evt.get("title", "") or ""
                    ensure_issue_task(repo_key, int(_num3), title=_t3, assignee=_asg3,
                                      event_type=event_type, sender=_sender3, creator=_creator3)
                    log.info(f"Pipeline: ensure task #{_num3} assignee={_asg3 or _sender3 or '(owner)'} creator={_creator3 or '(unknown)'}")
    except Exception as _e:
        log.warning(f"Pipeline: task-center sync failed: {_e}")

    # 3.3 Agent 启用检查（阶段路由后的最终 agent）
    from src.api.session_store import is_agent_enabled
    if not is_agent_enabled(repo_key, agent_type):
        log.info(f"Pipeline: agent '{agent_type}' disabled for repo={repo_key}, skip")
        from src.api.event_store import append_skipped_event
        append_skipped_event(repo_key, event_type, "agent_disabled", agent_type, issue_id=_pre_issue_id)
        return result

    # 3.4 场景节点校验：webhook 事件路由的 agent 不在当前 Issue 场景的 pipeline nodes 里 → 拦截
    # 防止 issue_comment 等事件被路由到场景未配置的 agent（如"需求方案分析"场景只有 vod+issue，
    # 但评论内容含 [Coding Agent] 前缀时被路由到 coding，绕过了 A2A 的 not_in_scene 检查）
    if _pre_issue_id and agent_type and event_type not in ("a2a_event", "cross_scene_handoff"):
        try:
            from src.api.routes.webhook import _scene_node_check
            _scene_ck = _scene_node_check(repo_key, _pre_issue_id.lstrip("#"), agent_type)
            if not _scene_ck.get("in_scene"):
                log.info(f"Pipeline: agent '{agent_type}' not in scene '{_scene_ck.get('scene_id')}' for issue=#{_pre_issue_id}, skip (event_type={event_type}, nodes={[n.get('id') for n in _scene_ck.get('nodes') or []]})")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "not_in_scene", agent_type, issue_id=_pre_issue_id)
                return result
        except Exception as _scene_e:
            log.warning(f"Pipeline: scene node check failed: {_scene_e}")

    # 4. 查找 Agent 配置（backend + skill），找不到则不处理
    backend = None
    runtime_id = 0
    skill_name = ""
    skill_id = 0
    skill_path = ""
    repo_name = repo_key  # repo_key 带平台前缀，devops_agent_session/events 统一用带前缀值
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_name, agent_type))
            row = c.fetchone()
            if row:
                runtime_id = row[0] or 0
                backend = ""
                if row[0]:
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[0],))
                    brow = c.fetchone()
                    if brow and brow[0]:
                        backend = brow[0]
                skill_id = row[2] or 0
                skill_name = row[1] or ""
                # skill_id 优先：关联 skill 表拿 skill_name + skill_path
                if skill_id:
                    from src.user.db import get_skill_by_id
                    _sk = get_skill_by_id(skill_id)
                    if _sk:
                        skill_name = _sk.get("skill_name") or skill_name
                        skill_path = _sk.get("skill_path") or skill_name
                log.info(f"Pipeline: agent session found, backend={backend}, skill={skill_name or '(default)'}, skill_id={skill_id}")
    except Exception as e:
        log.warning(f"Pipeline: agent session lookup failed: {e}")

    # 提前解析 issue_id + 场景（所有路径都需要，pick_engine 用 _issue_scene 选引擎）
    # Issue #680: 改走统一入口（一次解析全链同值）
    _issue_scene = "default"
    _issue_scene_uid = 0
    _early_issue_id = _resolve_event_issue_id(evt, repo_key)
    if _early_issue_id:
        try:
            from src.api.session_store import get_issue_scene
            _issue_scene, _issue_scene_uid = get_issue_scene(repo_key, _early_issue_id)
            _issue_scene = _issue_scene or ""   # 方案 B：无「default」场景，空=无场景
        except Exception as _se653:
            # Issue #653：场景解析失败曾是静默路径（节点查不到 → 派发回退全局池），必须留痕
            log.warning(f"Pipeline: get_issue_scene failed for repo={repo_key} "
                        f"issue={_early_issue_id}: {_se653}")
    # scene_user_id=0 时兜底反查（旧数据 is_join=0 导致 scene_user_id 未写入）
    # 优先用 issue 创建者（actor）查 unified_user_mapping，避免多人仓库取错用户
    if not _issue_scene_uid:
        try:
            from src.runtime.engine_select import _repo_user_id
            _issue_scene_uid = _repo_user_id(repo_key, issue_creator=actor)
        except Exception:
            pass
    log.info(f"Pipeline: scene resolved -> scene_id={_issue_scene} uid={_issue_scene_uid} issue={_early_issue_id or '(none)'}")
    # PR 订阅场景：直接从场景 nodes 取 skill，不走 agent_session / issue_scene
    _is_pr_sub = bool(evt.get("_pr_sub_scene_id"))
    if _is_pr_sub:
        skill_name = evt["_pr_sub_skill"]
        skill_id = 0
        skill_path = skill_name
        _issue_scene = evt["_pr_sub_scene_id"]
        # Issue #979: 节点 skill_id 权威优先；同时补传 user_id（此前 PR 订阅
        # 只传 repo_key，个人同名技能被静默跳过落到 official）
        _sub_sid = resolve_scene_node_skill_id(repo_key, "qa", scene_id=_issue_scene,
                                               user_id=_issue_scene_uid)
        try:
            from src.user.db import resolve_skill_dispatch
            _res = resolve_skill_dispatch(skill_name, _sub_sid, repo_key=repo_key,
                                          user_id=_issue_scene_uid,
                                          scene_id=_issue_scene, node_tag="qa")
            skill_id = _res["skill_id"]
            if _res.get("skill_name"):
                skill_name = _res["skill_name"]
            if _res.get("skill_path"):
                skill_path = _res["skill_path"]
        except Exception as _se979:
            log.warning(f"Pipeline (PR-sub): skill dispatch resolve failed: {_se979}")
        # Issue #657/#1758: 派发前技能 scope 校验（单一判官）——显式 skill_id
        # 同样需要 scope 校验；user scope 或查询异常 → fail-fast 落错误事件。
        _ev_sub = _evaluate_skill_scope(
            skill_name, repo_key, skill_id=_sub_sid,
            node_tag=agent_type, scene_id=_issue_scene)
        if not _ev_sub["ok"]:
            return _fail_skill_resolve_event(
                repo_key, skill_name, _issue_scene, agent_type,
                _ev_sub["reason"], issue_id=_pre_issue_id or "",
                scope_info=_ev_sub)
        backend = ""  # PR 订阅不走 agent_session，backend 置空让 pick_engine 自动选
        log.info(f"Pipeline (PR-sub): skill from scene nodes: {skill_name} sid={skill_id} (scene={_issue_scene})")
    # Issue 级场景优先：scene 非 default 时，用 issue scene 的 skill 覆盖 session 的 skill
    # 智能推荐模式：跳过 skill 解析（场景未知，prompt 不用 skill）
    if _is_scene_recommend:
        skill_name = ""
        skill_id = 0
        skill_path = ""
        log.info("Pipeline (plugin): 智能推荐模式, skip skill resolution")
    elif is_plugin_mode() and _issue_scene and not _is_pr_sub:
        # 2026-10-10：去掉 != "default" —— default 场景的 skill 改动同样生效（前端已无
        # Agent 配置页，场景 nodes 成为唯一可编辑入口），与 webhook.py a2a 路径口径一致
        _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_type, scene_id=_issue_scene, user_id=_issue_scene_uid)
        if _pipe_skill:
            skill_name = _pipe_skill
            # 同步更新 skill_id 和 skill_path，否则执行时仍用旧 skill_id 查到旧 skill
            # Issue #979: 节点 skill_id 权威优先（三档同名锁定用户选择的档位），
            # 无 id/失效 → 名字降级（user→repo→official）+ warning
            from src.user.db import resolve_skill_dispatch
            _node_sid = resolve_scene_node_skill_id(repo_key, agent_type, scene_id=_issue_scene,
                                                    user_id=_issue_scene_uid)
            _res = resolve_skill_dispatch(_pipe_skill, _node_sid, repo_key=repo_key,
                                          user_id=_issue_scene_uid,
                                          scene_id=_issue_scene, node_tag=agent_type)
            # Issue #657/#1758: 单一判官校验（显式 skill_id 仍需 scope 校验）
            _ev_pipe = _evaluate_skill_scope(
                _pipe_skill, repo_key, skill_id=_node_sid,
                node_tag=agent_type, scene_id=_issue_scene)
            if not _ev_pipe["ok"]:
                return _fail_skill_resolve_event(
                    repo_key, _pipe_skill, _issue_scene, agent_type,
                    _ev_pipe["reason"], issue_id=_pre_issue_id or "",
                    scope_info=_ev_pipe)
            if _res["skill_id"]:
                skill_id = _res["skill_id"]
                skill_path = _res.get("skill_path") or _pipe_skill
            else:
                skill_path = _pipe_skill
                skill_id = 0
            log.info(f"Pipeline (plugin): issue scene skill override: {agent_type}={_pipe_skill} sid={skill_id} (scene={_issue_scene})")
    if not backend or not skill_name:
        if _is_pr_sub:
            pass  # PR 订阅已设好 skill，跳过 fallback
        elif _is_scene_recommend:
            skill_name = ""
            backend = ""
            log.info("Pipeline (plugin): 智能推荐模式, using recommender prompt (no skill)")
        elif is_plugin_mode():
            _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_type, scene_id=_issue_scene, user_id=_issue_scene_uid)
            if _pipe_skill:
                skill_name = _pipe_skill
                backend = backend or ""
                log.info(f"Pipeline (plugin): skill for {agent_type}={_pipe_skill} (scene={_issue_scene})")
                # Issue #657/#1758: 派发前技能 scope 校验（单一判官）——显式 skill_id
                # 同样需要 scope 校验，不再按“id 显式绑定即授权”跳过。
                _fb_sid = resolve_scene_node_skill_id(
                    repo_key, agent_type, scene_id=_issue_scene,
                    user_id=_issue_scene_uid)
                _ev_fb = _evaluate_skill_scope(
                    _pipe_skill, repo_key, skill_id=_fb_sid,
                    node_tag=agent_type, scene_id=_issue_scene)
                if not _ev_fb["ok"]:
                    return _fail_skill_resolve_event(
                        repo_key, _pipe_skill, _issue_scene, agent_type,
                        _ev_fb["reason"], issue_id=_pre_issue_id or "",
                        scope_info=_ev_fb)
        if not skill_name and not _is_scene_recommend:
            # 推荐模式不需要 skill（用 build_scene_recommender_prompt 内置 prompt），
            # 跳过 fallback 的 no_agent_session 检查，直接走引擎选择 → 广播入队
            # Legacy fallback: agent-skill-mapping.json（用 Issue 级场景）
            try:
                _defaults_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "agent-skill-mapping.json")
                if os.path.exists(_defaults_path):
                    with open(_defaults_path, "r", encoding="utf-8") as _f:
                        _defaults = json.load(_f)
                    _scene = _defaults.get("scenes", {}).get(_issue_scene, _defaults.get("scenes", {}).get("default", {}))
                    _mapping = _scene.get("mapping", {}) if isinstance(_scene, dict) else {}
                    _default_skill = _mapping.get(agent_type, "")
                    if _default_skill:
                        backend = ""
                        skill_name = _default_skill
                        log.info(f"Pipeline: using default skill for {agent_type}={_default_skill} (scene={_issue_scene})")
                    else:
                        log.info(f"Pipeline: no agent session and no default skill for {agent_type}, skip")
                        from src.api.event_store import append_skipped_event
                        append_skipped_event(repo_key, event_type, "no_skill_config", agent_type, issue_id=_pre_issue_id)
                        return result
                else:
                    log.info(f"Pipeline: no agent session for repo={repo_name}, agent={agent_type}, skip")
                    from src.api.event_store import append_skipped_event
                    append_skipped_event(repo_key, event_type, "no_agent_session", agent_type, issue_id=_pre_issue_id)
                    return result
            except Exception as e:
                log.warning(f"Pipeline: failed to load default skill config: {e}")
                from src.api.event_store import append_skipped_event
                append_skipped_event(repo_key, event_type, "skill_config_error", str(e)[:200], issue_id=_pre_issue_id)
                return result

    # 4. 查找 Agent 配置（backend + skill），找不到则不处理
    payload = evt.get("payload", {})
    if event_type in ("note_hook_pr", "pull_request_opened", "pull_request_updated", "pull_request_merged"):
        # PR 类事件：仅用 resolve_pr_issue_id 规则链（分支/标题/issues 最大/PR 详情 API）。
        # 不用 resolve_issue_id——GitCode MR payload 的 object_attributes.iid 是 PR 号，
        # 会误当 issue_id（_is_pr_payload 只识别带 pull_request/issues 键的 payload）。
        # Issue #680: 改走统一入口（一次解析全链同值）；带 repo token 触发 PR 详情 API 兜底。
        try:
            from src.user.db import get_repo_token_with_refresh
            _pr_api_token = get_repo_token_with_refresh(repo_key) or ""
        except Exception as e:
            log.warning(f"Pipeline: get repo token failed: {e}")
            _pr_api_token = ""
        issue_id = _resolve_event_issue_id(evt, repo_key, api_token=_pr_api_token)
        if issue_id:
            log.info(f"Pipeline: PR 关联 Issue -> {issue_id}")
        else:
            log.info(f"Pipeline: PR 无关联 Issue，issue_id={(evt.get('issue_id') or '(none)')}")
    else:
        issue_id = _resolve_event_issue_id(evt, repo_key)
    # PR 订阅：用 PR 号 + scene_id 构造伪 issue_id 参与去重（区分不同场景）
    if not issue_id and evt.get("_pr_sub_scene_id"):
        # 统一走 _extract_pr_number：Issue 实体 iid 不作 PR 号来源（Issue #1802 复审）
        pr_num, _ = _extract_pr_number(evt, _as_payload_dict(payload), event_type)
        if pr_num:
            _sub_scene = evt["_pr_sub_scene_id"]
            issue_id = f"#pr_{pr_num}_{_sub_scene}"
            evt["_pr_sub_pr_number"] = pr_num
            log.info(f"Pipeline: PR subscription pseudo issue_id={issue_id}")
    title = evt.get("title", "") or event_type
    log.info(f"Pipeline: issue_id={issue_id or '(none)'}, title={title}")

    # 5.5 构建摘要描述（纯内存操作，放在锁外）
    actor = evt.get("actor", "")
    branch = evt.get("branch", "")
    if event_type == "issue_opened":
        summary = f"新 Issue {issue_id}: {title}" if issue_id else f"新 Issue: {title}"
    elif event_type == "issue_updated":
        summary = f"Issue {issue_id} 已更新" if issue_id else "Issue 已更新"
    elif event_type == "issue_comment":
        summary = f"@{actor} 评论了 Issue {issue_id}" if issue_id else f"@{actor} 发表了评论"
    elif event_type == "note_hook_pr":
        summary = f"@{actor} 评论了 PR {issue_id}" if issue_id else f"@{actor} 评论了 PR"
    elif event_type == "pull_request_opened":
        summary = f"新 PR {issue_id}: {title}" if issue_id else f"新 PR: {title}"
    elif event_type == "pull_request_updated":
        summary = f"PR {issue_id} 已更新" if issue_id else "PR 已更新"
    elif event_type == "pull_request_merged":
        summary = f"PR {issue_id} 已合并: {title}" if issue_id else f"PR 已合并: {title}"
    elif event_type == "push":
        summary = f"@{actor} 推送到 {branch}" if branch else f"@{actor} 推送了代码"
    elif event_type == "tag_created":
        summary = f"创建标签: {title}" if title else "创建新标签"
    elif event_type == "release_created":
        summary = f"发布 Release: {title}" if title else "发布新 Release"
    elif event_type == "build_start":
        summary = "构建开始"
    elif event_type == "build_failure":
        summary = f"构建失败: {title}" if title else "构建失败"
    elif event_type == "deploy_start":
        summary = "部署开始"
    elif event_type == "deploy_failure":
        summary = "部署失败"
    elif event_type == "issue_labeled":
        summary = f"Issue {issue_id} 已打标签" if issue_id else "Issue 已打标签"
    elif event_type == "vod_signal":
        summary = f"VOD 信号: {title}" if title else "VOD 信号"
    elif event_type == "a2a_event":
        summary = f"A2A: {actor} → {agent_type}"
    else:
        summary = title or event_type
    log.info(f"Pipeline: summary={summary}")

    # Issue #252: 实体类型区分（issue vs pull_request），编号相同时不混淆
    _entity_type = "pull_request" if event_type in PR_EVENT_TYPES else ("issue" if event_type in ISSUE_EVENT_TYPES else "")

    # 5.1+6 去重检查 + 写入事件（合并到同一个 db_lock 临界区，消除竞态窗口）
    # 原因：GitHub 对同一 Issue 创建会发多个 webhook（opened/assigned/labeled 等），
    # 如果 action 不细分会导致同一 issue 产生重复任务。
    # 豁免：a2a_event（已有自己的去重）
    _scene_id_for_event = evt.get("_pr_sub_scene_id", "") or _issue_scene
    _seq = 0
    # issue_comment 豁免 webhook 去重（issue-comment-stage-owner-routing-design.md §3.3）：
    # 评论是人操作、频率低，被「同 (issue,agent) 有 running 事件」的 dedup 挡掉会导致评论丢失；
    # 改为始终新建事件 + 入队（agent_tasks），由 issue_agent_session 的串行执行自然排队，
    # 待当前事件终态后再跑，不丢弃。bot 自评回声/系统回帖已在上游过滤，不会因豁免循环触发。
    if issue_id and agent_type and event_type not in (("a2a_event",) + _COMMENT_EVENT_TYPES):
        # 方案 A/C（duplicate-issue-opened-event-fix-design.md）：
        # - A：event_active_guard 唯一约束原子抢占（跨进程互斥，替代依赖进程内 db_lock 的
        #   "同 (repo,issue,agent) 无 running 事件" SELECT）；
        # - C：近 60s 同类事件（含已终态）也视为重复，处理 webhook 重发/终态后二次投递。
        # 解决 GitCode/GitHub 双 webhook 或 create_task 同步补发 + 真实 webhook 并发双投。
        from src.api.event_store import append_skipped_event, _utc_now_str, find_recent_same_event
        try:
            with db_lock:
                _dc = db.cursor()
                # ① 原子抢占守卫（跨进程唯一约束；SQLite OR IGNORE / MySQL INSERT IGNORE）
                _dc.execute(
                    "INSERT OR IGNORE INTO event_active_guard "
                    "(repo_key, issue_id, agent_type, seq, created_at) VALUES (?,?,?,0,?)",
                    (repo_key, issue_id or "#0", agent_type, _utc_now_str()))
                if _dc.rowcount == 0:
                    log.info(f"Pipeline: dedup skip — (issue,agent)=({issue_id},{agent_type}) "
                             f"guard already held (active/recent)")
                    append_skipped_event(repo_key, event_type,
                                         f"dedup:active_{agent_type}_for_{issue_id}",
                                         issue_id=issue_id)
                    result["skipped"] = "duplicate_active_task"
                    return result
                # ② 近窗去重：同 (repo,issue,agent) 且同 event_type 60s 内已存在（含已终态）→ 跳过
                # 时间口径必须与 events.created_at 的写入口径一致（MySQL=CST 墙钟 /
                # SQLite=UTC），见 time_utils.db_now_str。用 _utc_now_str(-60) 会在 MySQL
                # 下把窗口放大成 8h+，PR 评论等 webhook 事件被静默判重吞掉（Issue #1487 衍生）
                if find_recent_same_event(repo_key, issue_id or "#0", agent_type, event_type, 60):
                    log.info(f"Pipeline: dedup skip — recent {event_type} event within 60s "
                             f"for (issue,agent)=({issue_id},{agent_type})")
                    append_skipped_event(repo_key, event_type,
                                         f"dedup:recent_{event_type}_60s_for_{agent_type}",
                                         issue_id=issue_id)
                    _dc.execute(
                        "DELETE FROM event_active_guard WHERE repo_key=? AND issue_id=? AND agent_type=?",
                        (repo_key, issue_id or "#0", agent_type))
                    db.commit()
                    result["skipped"] = "duplicate_recent_window"
                    return result
                # ⑧ 去重通过 → 直接 INSERT（不再调 append_event 函数，避免重复加锁开销）
                _dc.execute(
                    "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type, title, actor, summary, payload, status, result, scene_id, entity_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (repo_key, issue_id or "#0", event_type, source, agent_type, title, actor, summary,
                     json.dumps(payload or {}), "pending", "", _scene_id_for_event, _entity_type)
                )
                db.commit()
                _seq = _dc.lastrowid or 0
                # 守卫行回填当前事件 seq（终态释放时按 seq 精确删除）
                _dc.execute(
                    "UPDATE event_active_guard SET seq=? WHERE repo_key=? AND issue_id=? AND agent_type=? AND seq=0",
                    (_seq, repo_key, issue_id or "#0", agent_type))
                db.commit()
                # 验证行确实写入（防止 WAL 并发写入冲突时 lastrowid 返回旧值）
                _dc.execute("SELECT seq FROM events WHERE seq=?", (_seq,))
                if not _dc.fetchone():
                    db.rollback()
                    log.warning(f"Pipeline: INSERT returned lastrowid={_seq} but row not found (WAL conflict?), repo={repo_key}, type={event_type}")
        except Exception as _de:
            log.warning(f"Pipeline: dedup+insert failed: {_de}")
            try:
                with db_lock:
                    _dc2 = db.cursor()
                    _dc2.execute(
                        "DELETE FROM event_active_guard WHERE repo_key=? AND issue_id=? AND agent_type=? AND seq IN (0,?)",
                        (repo_key, issue_id or "#0", agent_type, _seq or 0))
                    db.commit()
            except Exception:
                pass
    else:
        # a2a_event 或无 issue_id/agent_type：直接写入（无需 dedup）
        from src.api.event_store import append_event
        _comment_cid = ""
        _idem_key = ""
        if event_type in ("issue_comment", "note_hook_pr"):
            try:
                from src.repo_key import parse_repo_key
                from src.outbound_ledger import extract_event_content_id
                _provider = parse_repo_key(repo_key)[0] if repo_key else ""
                _comment_cid = extract_event_content_id(_provider, payload)
                if _comment_cid:
                    _idem_key = f"comment:{_comment_cid}"
            except Exception as _ike:
                log.warning(f"Pipeline: comment idempotency key build failed: {_ike}")
        _seq = append_event(
            repo_key=repo_key,
            event_type=event_type,
            title=title,
            issue_id=issue_id or "#0",
            source=source,
            agent_type=agent_type,
            actor=actor,
            summary=summary,
            payload=payload,
            status="pending",
            scene_id=_scene_id_for_event,
            entity_type=_entity_type,
            idempotency_key=_idem_key,
        )
        if _seq < 0:
            from src.api.event_store import append_skipped_event
            append_skipped_event(repo_key, event_type, "duplicate_comment_delivery",
                                 detail=_comment_cid, issue_id=issue_id or "#0")
            result["skipped"] = "duplicate_comment_delivery"
            return result
    seq = _seq
    # WAL checkpoint 在 db_lock 释放后执行（避免阻塞其他需要 db_lock 的操作）
    if seq and issue_id and agent_type and event_type != "a2a_event":
        try:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
    log.info(f"Pipeline: event created, seq={seq}, repo_key={repo_key}, agent={agent_type}, issue_id={issue_id or '#0'}")

    from src.repo_key import from_repo_key
    result.update({"seq": seq, "event_type": event_type, "skill": agent_type,
                   "backend": backend, "skill_name": skill_name,
                   "repo_key": from_repo_key(repo_key), "ok": True, "issue_id": issue_id,
                   "repository": evt["repository"]})

    # Issue #1245: webhook issue_opened 场景可用性预校验（不拒单，只不启动流水线）。
    # 事件已落库 → 失败时置 failed（含 reason_code）+ 评论原因，与 _fail_scene_all_offline 同款终态路径。
    # 身份口径：webhook 的建单人 = actor（issue 创建者），不是场景 owner（_issue_scene_uid）。
    # 用 actor 解析其 grape uid 作为 user 档归属基准，避免建单人≠场景 owner 时误判。
    if event_type == "issue_opened" and _issue_scene:
        _pc_user = {"username": actor}
        try:
            from src.runtime.engine_select import _repo_user_id
            _pc_uid = _repo_user_id(repo_key, issue_creator=actor)
            if _pc_uid:
                _pc_user["id"] = _pc_uid
        except Exception as _uid_err:
            log.warning(f"Pipeline: resolve precheck actor uid failed (Issue #1245): {_uid_err}")
        try:
            from src.api.scene_precheck import precheck_scene
            _precheck = precheck_scene(_issue_scene, repo_key, _pc_user)
        except Exception as _pc_err:
            # fail-open：仅预校验本身异常放行；后续失败终态副作用不得被此 try 吞掉
            log.warning(f"Pipeline: scene precheck failed, allow dispatch (Issue #1245): {_pc_err}")
            _precheck = None
        if _precheck and not _precheck.get("ok"):
            from src.api.scene_precheck import problem_reason_codes, summarize_problems
            _pc_problems = _precheck.get("problems") or []
            _pc_codes = problem_reason_codes(_pc_problems)
            _pc_summary = summarize_problems(_pc_problems)
            _reason = (f"场景可用性预校验未通过（{','.join(_pc_codes) or 'unknown'}）："
                       f"{_pc_summary}（scene={_issue_scene}, agent={agent_type}）")
            log.warning(f"Pipeline: scene precheck blocked dispatch seq={seq}: {_reason}")
            _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                                skill_path, skill_id, evt, issue_id, runtime_id or 0)
            _fail_event(seq, _reason, diag={
                "code": _pc_codes[0] if _pc_codes else "scene_precheck_failed",
                "phase": "scene_precheck",
                "scene_id": _issue_scene,
            })
            _notify_scene_precheck_failure(
                repo_key, issue_id or "", _pc_problems, _precheck.get("trace_id") or "")
            return result

    # 执行分派：从引擎集合选引擎（专属→全局→本机，engine-selection-design.md v2）；
    # 选中 remote → 入队该引擎（queue_remote）；选中本机 → direct 就地执行。
    from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
    from src.runtime.service import runtime_execution_kind, enqueue_task, EXEC_MODE_DIRECT, EXEC_MODE_REMOTE, get_execution_mode
    try:
        engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=runtime_id or 0,
                                scene_id=_issue_scene, user_id=_issue_scene_uid)
        log.info(f"Pipeline: pick_engine returned engine_id={engine_id} for repo={repo_key} agent={agent_type} seq={seq}")
    except Exception as _pe:
        log.warning(f"Pipeline: pick_engine exception: {_pe}")
        engine_id = None
    if engine_id is None:
        log.info(f"Pipeline: no available engine for repo={repo_key} agent={agent_type}, seq={seq}, skip")
        # 创建 failed 状态的 task 快照（供重派复用参数，否则重派报"无关联任务"）
        _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                            skill_path, skill_id, evt, issue_id, runtime_id or 0)
        _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
        return result
    # Plugin 模式 + engine_id=0：无主池任务（广播），多个引擎通过 claim_by_tag 竞争认领。
    # 非 direct 模式均走广播（queue_remote + auto）；仅 direct 模式走本机 direct（自动选引擎）。
    # 场景 engine_runtime_ids 限定广播范围（写入 pool_engine_ids 字段）。
    # Issue #112 修复：所有模式都获取 pool_engine_ids，确保场景引擎绑定生效
    # Issue #259：写入前已按健康过滤（get_scene_pool_engine_ids 内部）。
    _pool_engine_ids = get_scene_pool_engine_ids(_issue_scene, agent_type, repo_key=repo_key, user_id=_issue_scene_uid)
    if _pool_engine_ids is None:
        # 方案A：场景绑定的引擎全部离线/失联 → 不入队，事件置 failed 并明确提示
        log.warning(f"Pipeline: scene={_issue_scene} agent={agent_type} engines all offline, skip (Issue #259)")
        _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                            skill_path, skill_id, evt, issue_id, runtime_id or 0)
        # Issue #541：报错区分「场景仓内不存在 / 未绑定引擎 / 引擎全部离线」
        _reason_offline = f"场景绑定的引擎全部离线（scene={_issue_scene}, agent={agent_type}），请检查引擎在线状态"
        try:
            from src.runtime.engine_select import scene_dispatch_failure_reason
            _reason_offline = scene_dispatch_failure_reason(
                _issue_scene, agent_type, repo_key=repo_key, user_id=_issue_scene_uid) or _reason_offline
        except Exception as _e541:
            log.warning(f"Pipeline: scene dispatch failure reason lookup failed: {_e541}")
        # Issue #496：PR merged → sre 收尾链路（merge webhook 一次性投递，无二次触发机会）
        # 保留 #259 即时终态语义的同时追加派发级自动重试（引擎恢复后自动重派）。
        if event_type == "pull_request_merged" and agent_type == "sre":
            _retry_scheduled = False
            try:
                from src.api.pr_merged_sre_retry import schedule_pr_merged_sre_retry
                _retry_scheduled = schedule_pr_merged_sre_retry(
                        repo_key, seq, agent_type, backend or "", skill_name or "",
                        skill_id or 0, skill_path or "", evt, issue_id=issue_id or "",
                        runtime_id=runtime_id or 0, scene_id=_issue_scene or "",
                        scene_uid=_issue_scene_uid or 0)
                if _retry_scheduled:
                    _reason_offline += "；已安排自动重试（引擎恢复后自动重派，Issue #496）"
            except Exception as _e496:
                log.warning(f"Pipeline: schedule #496 sre merge retry failed: {_e496}")
            if not _retry_scheduled:
                # Issue #544 L4：未安排上自动重试（参数不全/异常）→ 断链即时提示 Issue
                try:
                    from src.api.pr_merged_sre_retry import _notify_issue_broken_chain
                    _notify_issue_broken_chain(repo_key, issue_id or "", _reason_offline)
                except Exception as _ne544:
                    log.warning(f"Pipeline: #544 broken-chain notify failed: {_ne544}")
        _fail_event(seq, _reason_offline)
        return result
    if engine_id == 0 and is_plugin_mode() and get_execution_mode() != EXEC_MODE_DIRECT:
        _be = backend or "opencode"
        # 智能推荐模式：强制限定 opencode 引擎认领（推荐任务轻量，opencode 最快）
        if _is_scene_recommend:
            try:
                import json as _json
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT id FROM agent_runtimes WHERE name='opencode' AND status='online'")
                    _oc_ids = [r[0] for r in c.fetchall()]
                if _oc_ids:
                    _pool_engine_ids = _json.dumps(_oc_ids)
                    log.info(f"Pipeline (plugin broadcast): 智能推荐限定 opencode engines={_oc_ids}")
                else:
                    log.info("Pipeline (plugin broadcast): 智能推荐无 online opencode，不限定（兜底放行）")
            except Exception as _oe:
                log.warning(f"Pipeline: 智能推荐 opencode 过滤失败: {_oe}")
        log.info(f"Pipeline (plugin broadcast): engine_id=0 pool_tag={agent_type} pool_engine_ids={_pool_engine_ids} seq={seq}")
        if _be and seq > 0 and (skill_name or _is_scene_recommend):
            # local-claimer 设计：场景绑定 → 'scene'（#253 硬约束，全灭不降级）；
            # 智能推荐的 opencode 限定是系统自动选择 → 'auto'（全灭可降级放行）
            _pes = "auto" if _is_scene_recommend else "scene"
            _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                                   skill_id, skill_path, engine_id, issue_id,
                                   pool_engine_ids=_pool_engine_ids, user_id=_issue_scene_uid,
                                   is_scene_recommend=_is_scene_recommend,
                                   pool_engine_source=_pes)
        return result
    # backend 必须取已注册引擎的类型名（不沿用会话/字面默认），确保执行 CLI 与选中引擎一致
    _be = engine_name(engine_id) or backend
    # 智能推荐模式 backend 被清空 → 兜底 opencode（auto-select 会覆盖为实际引擎名）
    if not _be and _is_scene_recommend:
        _be = "opencode"
    if not _be or seq <= 0:
        return result
    # 智能推荐模式允许 skill_name 为空（纯 prompt，不用 skill 文件）
    if not skill_name and not _is_scene_recommend:
        return result
    # engine_id=0：plugin direct/auto 兜底 → 自动选一个本机在线引擎执行
    if not engine_id:
        # Issue #253 改动4：有显式绑定时 direct 自动选机只允许在绑定集合内挑（宁失败不跑飞）。
        # 原行为：全站 online/healthy 查询（opencode>codearts>其他），hermes 等池外引擎可被选中。
        _pool_ids_253 = []
        if _pool_engine_ids:
            try:
                import json as _json253
                _pool_ids_253 = [int(x) for x in _json253.loads(_pool_engine_ids) if x]
            except Exception:
                _pool_ids_253 = []
        if _pool_ids_253:
            # 绑定集合内选在线健康引擎；全灭 → failed + 人话原因，不碰全站
            _bind_names_253 = []
            try:
                from src.runtime.engine_select import engine_name as _en253
                _bind_names_253 = [_en253(int(x)) or str(x) for x in _pool_ids_253]
            except Exception:
                _bind_names_253 = [str(x) for x in _pool_ids_253]
            _picked_253 = 0
            try:
                from src.session_manager import db as _db, db_lock as _dbl
                with _dbl:
                    _c = _db.cursor()
                    _ph253 = ",".join("?" * len(_pool_ids_253))
                    _c.execute(
                        f"SELECT id, name FROM agent_runtimes WHERE status='online' AND healthy=1 "
                        f"AND id IN ({_ph253}) "
                        f"ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END LIMIT 1",
                        tuple(_pool_ids_253))
                    _row = _c.fetchone()
                if _row:
                    _picked_253 = _row[0]
                    engine_id = _row[0]
                    _be = _row[1] or _be
                    log.info(f"Pipeline: direct auto-select within scene binding {list(set(_bind_names_253))} "
                             f"-> id={engine_id} name={_be}, seq={seq} (Issue #253)")
            except Exception as _e:
                log.warning(f"Pipeline: binding-constrained engine query failed: {_e}, seq={seq}")
            if not _picked_253:
                log.info(f"Pipeline: scene binding {list(set(_bind_names_253))} all offline for direct "
                         f"auto-select, seq={seq}, skip (Issue #253)")
                _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                                    skill_path, skill_id, evt, issue_id, runtime_id or 0)
                _fail_event(seq, f"场景绑定的引擎（{', '.join(sorted(set(_bind_names_253)))}）"
                                 f"全部不可用，direct 模式无法选机（Issue #253）")
                return result
        else:
            # 无绑定（空 pool）→ 原全站自动选机行为不变
            log.info(f"Pipeline: engine_id=0, auto-selecting local engine for seq={seq}")
            # 尝试取一个本机在线引擎（优先 opencode，其次 codearts，最后 hermes）
            # org_id 隔离：从 managed_repos 推断仓库 org_id，非个人组织只选该组织引擎
            from src.user.db import get_meta_value as _gmv
            try:
                from src.session_manager import db as _db, db_lock as _dbl
                from src.user.org_db import is_org_personal
                log.info(f"Pipeline: acquiring db_lock for engine query, seq={seq}")
                with _dbl:
                    _c = _db.cursor()
                    # 推断仓库关联的 org_id
                    _c.execute("SELECT org_id FROM managed_repos WHERE repo_key=? ORDER BY org_id DESC LIMIT 1", (repo_key,))
                    _mr = _c.fetchone()
                    _mr_org_id = (_mr[0] if _mr and _mr[0] else 0)
                    _is_personal = is_org_personal(_mr_org_id) if _mr_org_id else True
                    if _mr_org_id and not _is_personal:
                        # 非个人组织：只选 org_id 匹配的引擎（不含 global org_id=0）
                        _c.execute("SELECT id, name FROM agent_runtimes WHERE status='online' AND healthy=1 AND org_id=? ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END LIMIT 1", (_mr_org_id,))
                    else:
                        # 个人组织/无组织：选 org_id=0 的本机引擎（兼容全局）
                        _c.execute("SELECT id, name FROM agent_runtimes WHERE status='online' AND healthy=1 AND (org_id=0 OR org_id=?) ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END LIMIT 1", (_mr_org_id,))
                    _row = _c.fetchone()
                log.info(f"Pipeline: engine query done, row={_row}, seq={seq}")
                if _row:
                    engine_id = _row[0]
                    _be = _row[1] or _be
                    log.info(f"Pipeline: auto-selected local engine id={engine_id} name={_be}, seq={seq}")
                else:
                    log.info(f"Pipeline: engine_id=0 no online engine, seq={seq}, skip")
                    _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                                        skill_path, skill_id, evt, issue_id, runtime_id or 0)
                    _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
                    return result
            except Exception as _e:
                log.warning(f"Pipeline: auto-select engine failed: {_e}")
                _create_failed_task(repo_key, seq, agent_type, backend or "", skill_name,
                                    skill_path, skill_id, evt, issue_id, runtime_id or 0)
                _fail_event(seq, "无可用引擎（engine_unavailable），请配置仓库/全局/本机引擎")
                return result
    kind = runtime_execution_kind(engine_id)
    try:
        # Issue #372 §12.7: dispatch 段包 try/except，异常时 _fail_event 释放 guard
        # P0 (Issue #596): direct 路径改走 enqueue_task，由 local_claimer 认领执行
        if _be and skill_name and seq > 0:
            _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                                   skill_id, skill_path, engine_id, issue_id,
                                   pool_engine_ids=_pool_engine_ids, user_id=_issue_scene_uid,
                                   is_scene_recommend=_is_scene_recommend)
        elif _be and seq > 0 and (skill_name or _is_scene_recommend):
            _enqueue_pipeline_task(repo_key, seq, agent_type, _be, skill_name, evt,
                                   skill_id, skill_path, engine_id, issue_id,
                                   pool_engine_ids=_pool_engine_ids, user_id=_issue_scene_uid,
                                   is_scene_recommend=_is_scene_recommend)
            log.info(f"Pipeline: enqueued agent task, seq={seq}, repo={repo_key}, agent={agent_type}")
    except Exception as _de:
        log.warning(f"Pipeline: dispatch exception for seq={seq}: {_de}")
        _fail_event(seq, f"pipeline dispatch exception: {_de}")

    return result


# ====== VCS provider 分流辅助 ======

def _is_github_repo(repo_key: str) -> bool:
    """判断 repo_key 是否为 GitHub 仓库（provider 前缀 github:）。"""
    return bool(repo_key) and repo_key.startswith("github:")


def _get_repo_api_token(repo_key: str) -> str:
    """根据 repo_key 的 provider 获取对应 VCS API token。

    GitHub → get_github_token；GitCode → get_repo_token_with_refresh。
    """
    if _is_github_repo(repo_key):
        try:
            from src.user.db import get_github_token
            return get_github_token(repo_key=repo_key) or ""
        except Exception:
            return ""
    try:
        from src.user.db import get_repo_token_with_refresh
        return get_repo_token_with_refresh(repo_key) or ""
    except Exception:
        return ""


def _github_fetch_issue(owner: str, repo: str, issue_num: str, token: str) -> dict:
    """通过 GitHub API 获取 Issue 详情。失败抛异常。"""
    from src.user.repo_manager import _github_req
    return _github_req("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}", token=token)


def _github_list_issue_comments(owner: str, repo: str, issue_num: str, token: str) -> list:
    """通过 GitHub API 获取 Issue 评论列表。失败抛异常。"""
    from src.user.repo_manager import _github_req
    result = _github_req("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}/comments?per_page=100", token=token)
    return result if isinstance(result, list) else []


def _github_post_issue_comment(owner: str, repo: str, issue_num: str, body: str, token: str) -> dict:
    """通过 GitHub API 发 Issue 评论。失败抛异常。"""
    from src.user.repo_manager import _github_req
    return _github_req("POST", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}/comments", token=token, body={"body": body})


def _ledger_record_comment(provider: str, repo_key: str, repository: str, issue_num, body: str, resp,
                           content_type: str = "issue_comment", origin_task: str = "") -> None:
    """Issue #453 代发台账（S12 写入点3/4）：系统发评论成功后记账。best-effort 不抛异常。

    - provider: gitcode / github（分平台只影响 ID 提取字段）
    - resp: VCS 响应（dict 带 id）；GitHub 路径由 _github_post_issue_comment 返回 dict；
      GitCode 裸 urllib 路径由调用方读响应 JSON 后传入
    - 指纹记剥离 marker 后正文的 md5（仅用于旁路告警核对，不参与拦截）
    """
    try:
        from src.outbound_ledger import record_outbound, extract_content_id, content_hash_md5
        _rk = repo_key or ""
        if not _rk and repository:
            from src.repo_key import to_repo_key
            _rk = to_repo_key(repository, provider)
        _cid = extract_content_id(resp, content_type) if isinstance(resp, dict) else ""
        if not _cid:
            from src.config import log as _log
            _log.warning(f"outbound_ledger: comment response carries no content ID "
                         f"(provider={provider}, repo={repo_key}, issue={issue_num}) — silent-miss risk, "
                         f"check provider response field mapping")
        record_outbound(provider, _rk, content_type, _cid, issue_number=str(issue_num or ""),
                        origin_channel="webhook", origin_task=origin_task,
                        content_hash=content_hash_md5(body))
    except Exception as e:
        log.warning(f"outbound_ledger record comment failed (non-blocking): {e}")


def _gitcode_post_issue_comment_ledgered(repository: str, issue_num, comment: str, bot_token: str,
                                          repo_key: str = "", origin_task: str = "") -> None:
    """GitCode 裸 urllib 发 Issue 评论 + 记账（S16-1：该路径原先连响应体都不读）。

    供 _post_result_to_issue / _post_subtask_completion_to_parent / _post_all_subtasks_done
    共用；失败抛异常（调用方原有 try/except 兜底）。
    """
    import urllib.request, json as _json
    _data = _json.dumps({"body": comment}).encode("utf-8")
    _url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{issue_num}/comments"
    _req = urllib.request.Request(_url, data=_data,
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}, method="POST")
    _resp = urllib.request.urlopen(_req, timeout=15)
    _body_resp = b""
    try:
        _body_resp = _resp.read()
    except Exception:
        pass
    _parsed = {}
    try:
        _parsed = _json.loads(_body_resp) if _body_resp else {}
    except Exception:
        _parsed = {}
    _ledger_record_comment("gitcode", repo_key, repository, issue_num, comment,
                           _parsed if isinstance(_parsed, dict) else {}, origin_task=origin_task)


def _fetch_issue_assignee(repo_key: str, issue_id: str) -> str:
    """调 VCS API 获取 Issue 责任人（assignee.login）。失败返回空。

    根据 repo_key provider 分流 GitHub/GitCode。
    用于 pipeline 责任人显示（issue_state.assignee），取代"从事件 payload 猜 assignee"。
    """
    num = _issue_num_from(issue_id)
    if not num:
        return ""
    try:
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)

        if _is_github_repo(repo_key):
            from src.user.db import get_github_token
            tok = get_github_token(repo_key=repo_key) or ""
            if not tok:
                # FR-12.1: token 空时记 warning（不再静默）
                log.warning(f"_fetch_issue_assignee: no github token for {repo_key}, assignee resolution degraded")
                return ""
            d = _github_fetch_issue(owner, rname, num, tok)
            asg = d.get("assignee") or {}
            login = asg.get("login", "") if isinstance(asg, dict) else ""
            return login or ""
        # GitCode 原逻辑
        import urllib.request
        from src.user.db import get_repo_token_with_refresh
        tok = get_repo_token_with_refresh(repo_key) or ""
        if not tok:
            # FR-12.1: token 空时记 warning（不再静默）
            log.warning(f"_fetch_issue_assignee: no gitcode token for {repo_key}, assignee resolution degraded")
            return ""
        req = urllib.request.Request(
            f"https://gitcode.com/api/v5/repos/{owner}/{rname}/issues/{num}",
            headers={"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"})
        import json as _json
        d = _json.load(urllib.request.urlopen(req, timeout=15))
        asg = d.get("assignee") or {}
        login = asg.get("login", "") if isinstance(asg, dict) else ""
        return login or ""
    except Exception:
        return ""


def _issue_num_from(issue_id: str) -> str:
    s = (issue_id or "").strip().lstrip("#")
    return s if s and s != "0" else ""


def _fetch_issue_title_via_api(repo_key: str, issue_id: str) -> str:
    """调 VCS API 获取 Issue 标题。失败返回空。

    根据 repo_key provider 分流 GitHub/GitCode。
    用于 pipeline 展示兜底：当 tasks/events 表中 title 均为占位符时，
    通过 API 获取真实标题并回写。
    """
    num = _issue_num_from(issue_id)
    if not num:
        return ""
    try:
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)

        if _is_github_repo(repo_key):
            from src.user.db import get_github_token
            tok = get_github_token(repo_key=repo_key) or ""
            if not tok:
                return ""
            d = _github_fetch_issue(owner, rname, num, tok)
            return d.get("title", "") or ""
        # GitCode 原逻辑
        import urllib.request
        from src.user.db import get_repo_token_with_refresh
        tok = get_repo_token_with_refresh(repo_key) or ""
        if not tok:
            return ""
        req = urllib.request.Request(
            f"https://gitcode.com/api/v5/repos/{owner}/{rname}/issues/{num}",
            headers={"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"})
        import json as _json
        d = _json.load(urllib.request.urlopen(req, timeout=15))
        return d.get("title", "") or ""
    except Exception:
        return ""


def fetch_issue_via_vcs(repo_key: str, issue_number) -> tuple:
    """Issue #667：按 repo_key + issue_number 调 VCS API 核实 Issue 存在性并取详情。

    返回 (state, data)：
    - ("exists", {issue dict})：VCS 侧存在（data 至少含 title，可能为空标题）
    - ("missing", {})：VCS 侧明确 404（Issue 已删除/不存在）
    - ("unknown", {})：无法核实（无 token / 网络异常 / 非法编号），调用方按降级语义处理
    """
    num = _issue_num_from(str(issue_number or ""))
    if not num:
        return ("unknown", {})
    import urllib.error
    from src.repo_key import to_repo_key, split_owner_repo
    _rk = to_repo_key(repo_key)
    owner, rname = split_owner_repo(_rk)
    if not owner or not rname:
        return ("unknown", {})
    try:
        if _is_github_repo(_rk):
            from src.user.db import get_github_token
            tok = get_github_token(repo_key=_rk) or ""
            if not tok:
                return ("unknown", {})
            d = _github_fetch_issue(owner, rname, num, tok)
            return ("exists", d if isinstance(d, dict) else {})
        import urllib.request
        import json as _json
        from src.user.db import get_repo_token_with_refresh
        tok = get_repo_token_with_refresh(_rk) or ""
        if not tok:
            return ("unknown", {})
        req = urllib.request.Request(
            f"https://gitcode.com/api/v5/repos/{owner}/{rname}/issues/{num}",
            headers={"Authorization": "Bearer " + tok, "User-Agent": "Mozilla/5.0"})
        d = _json.load(urllib.request.urlopen(req, timeout=15))
        return ("exists", d if isinstance(d, dict) else {})
    except urllib.error.HTTPError as _he:
        if int(_he.code) == 404:
            return ("missing", {})
        return ("unknown", {})
    except Exception:
        return ("unknown", {})


def _parse_parent_issue_from_payload(payload: dict) -> str:
    """Parse parent issue ID from Issue body."""
    import re
    body = ""
    obj = payload.get("object_attributes") or payload.get("issue") or {}
    if isinstance(obj, dict):
        body = obj.get("description") or obj.get("body") or ""
    if not body and isinstance(payload, dict):
        body = payload.get("description") or payload.get("body") or ""
    if not body:
        return ""
    m = re.search(r'父任务.{0,30}?#?(\d+)', body, re.I | re.S)
    if m:
        return f"#{m.group(1)}"
    m2 = re.search(r'parent.?issue.{0,30}?#?(\d+)', body, re.I | re.S)
    if m2:
        return f"#{m2.group(1)}"
    return ""


def get_agent_workdir(repo_key: str, agent_type: str, issue_id: str = "") -> str:
    """Agent 工作目录（按 Issue 统一隔离，见 workdir-issue-centric-design.md §二）。

    - 有关联 Issue：#5 → {provider}/{owner}/{repo}/issues/5/{agent_type}/
    - 无关联 Issue（push/#0）→ {provider}/{owner}/{repo}/{agent_type}-agent（兼容现状）
    """
    from src.repo_key import workspace_repo_dir
    _base = workspace_repo_dir(repo_key)
    num = _issue_num_from(issue_id)
    if num:
        return os.path.join(_base, "issues", num, agent_type)
    return os.path.join(_base, f"{agent_type}-agent")


def _fail_event(seq: int, reason: str, exit_code: int = 1, diag: dict = None):
    """事件入队/启动失败 → 立即置 failed 终态（防卡 running 僵尸）。

    事件在 append_event 时即为 running，若后续入队/启动失败而不更新，
    事件将永远 running（无任务支撑、无详细记录）。所有失败路径统一走这里收口。
    同步更新 agent_tasks 表 status=failed（防止 events 已 failed 但 agent_tasks 仍 running）。

    Issue #418：补 exit_code 形参（默认 1，兼容既有调用点），webhook.py A2A
    早退分支统一走本入口，回写 result/exit_code/completed_at 三字段。
    Issue #476：扩展 diag 诊断上下文——
    - result 内嵌机器可读错误码前缀 `[<code>] <reason>`（免表结构变更，前端可解析）；
    - diag 字典（code/target_stage/phase/ref_seq 等）合并写入 events.payload._fail_diag，
      幂等：已存在 _fail_diag 的终态事件不被二次覆盖（与 result 终态保护同语义）。
    幂等：update_event_complete 自带终态保护（terminal→terminal 跳过），
    agent_tasks 更新限定 status IN ('running','queued','dispatched')，
    重复触发同一 seq 早退不覆盖已写入的终态诊断字段（双主安全）。
    """
    if not seq:
        return
    try:
        from datetime import datetime, timezone
        from src.api.event_store import update_event_complete
        _result = reason
        _code = (diag or {}).get("code") or ""
        if _code:
            _result = f"[{_code}] {reason}"
        _now = datetime.now(timezone.utc).isoformat()
        update_event_complete(seq, "failed", exit_code=exit_code,
                              completed_at=_now, result=_result)
        # Issue #476：诊断上下文写入 events.payload._fail_diag（幂等：已存在不覆盖）
        if diag:
            try:
                from src.session_manager import db, db_lock
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT status, payload FROM events WHERE seq=?", (seq,))
                    _cur = c.fetchone()
                    if _cur and _cur[0] == "failed":
                        _payload = {}
                        try:
                            _payload = json.loads(_cur[1] or "{}") if _cur[1] else {}
                            if not isinstance(_payload, dict):
                                _payload = {}
                        except Exception:
                            _payload = {}
                        if "_fail_diag" not in _payload:
                            _payload["_fail_diag"] = dict(diag)
                            c.execute("UPDATE events SET payload=? WHERE seq=?",
                                      (json.dumps(_payload, ensure_ascii=False), seq))
                            db.commit()
            except Exception as _de:
                log.warning(f"Pipeline: _fail_event diag write failed seq={seq}: {_de}")
        # 同步更新 agent_tasks 表（如果有关联的 task）
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE agent_tasks SET status='failed', completed_at=?, "
                "exit_code=COALESCE(exit_code, ?), "
                "errors=COALESCE(errors, ?) "
                "WHERE event_seq=? AND status IN ('running','queued','dispatched')",
                (_now, exit_code,
                 json.dumps([_result][:500]) if _result else None, seq)
            )
            db.commit()
            if c.rowcount > 0:
                log.info(f"Pipeline: synced agent_tasks event_seq={seq} -> failed")
        log.info(f"Pipeline: event {seq} -> failed ({_result[:80]})")
    except Exception as e:
        log.warning(f"Pipeline: _fail_event failed for seq={seq}: {e}")


# 排队派发重入锁（Issue #1667 审查修复④）：
# 终态钩子 → drain → 派发失败 _fail_event → 再次终态 → 钩子 的递归链在此短路；
# 同一进程内并发终态回调也只会有一路执行 drain（其余跳过，排队行仍由原子抢占保证仅派发一次）。
_A2A_DRAIN_LOCK = threading.Lock()
# 仍视为「前序未结束」的状态：排队事件只在这些前序终态后才应派发。
_A2A_PRIOR_ACTIVE = ("running", "dispatched", "queued")


def _claim_and_dispatch_queued(qseq: int, repo_key: str, agent_type: str, issue_id: str,
                               payload: dict, qinfo: dict, prior_seq: int) -> bool:
    """原子抢占一条排队事件（queued→running）并派发；返回是否成功派发。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE events SET status='running', started_at=? "
                "WHERE seq=? AND status='queued'", (now_iso, qseq))
            claimed = c.rowcount == 1
            if claimed:
                qinfo["dispatched_at"] = now_iso
                payload["_a2a_queue"] = qinfo
                c.execute("UPDATE events SET payload=? WHERE seq=?",
                          (json.dumps(payload, ensure_ascii=False), qseq))
            db.commit()
    except Exception as _ce:
        log.warning(f"a2a queue drain: claim failed queue_seq={qseq}: {_ce}")
        return False
    if not claimed:
        return False
    dispatch_info = qinfo.get("dispatch") or {}
    if not isinstance(dispatch_info, dict):
        dispatch_info = {}
    skill_id = dispatch_info.get("skill_id") or 0
    try:
        skill_id = int(skill_id)
    except Exception:
        skill_id = 0
    runtime_id = dispatch_info.get("runtime_id") or 0
    try:
        runtime_id = int(runtime_id)
    except Exception:
        runtime_id = 0
    _issue_number = str(payload.get("issue_number") or (issue_id or "")).lstrip("#")
    evt = dispatch_info.get("evt") or {}
    if not isinstance(evt, dict):
        evt = {}
    try:
        from src.api.a2a_dispatch import run_a2a_dispatch
        result = run_a2a_dispatch(
            repo_key, qseq, agent_type or "", evt,
            skill_name=dispatch_info.get("skill_name") or (agent_type or ""),
            skill_id=skill_id,
            skill_path=dispatch_info.get("skill_path") or "",
            runtime_id=runtime_id,
            backend=dispatch_info.get("backend") or "",
            issue_number=_issue_number,
            repository=payload.get("repository") or "",
            ref_seq=int(payload.get("ref_seq") or 0),
        )
        if not isinstance(result, dict) or result.get("status") == "failed" or not result.get("seq"):
            _reason = (result or {}).get("error") if isinstance(result, dict) else ""
            _fail_event(qseq, f"A2A 排队派发失败（queue_seq={qseq}，前序 seq={prior_seq}）: "
                              f"{str(_reason or 'dispatch returned no task')[:200]}",
                        diag={"code": "a2a_queue_dispatch_failed",
                              "phase": "queue_drain", "ref_seq": prior_seq})
            return False
        log.info(f"a2a queue drain: dispatched queue_seq={qseq} "
                 f"agent={agent_type} prior_seq={prior_seq}")
        return True
    except Exception as _de:
        log.warning(f"a2a queue drain: dispatch exception queue_seq={qseq}: {_de}")
        try:
            _fail_event(qseq, f"A2A 排队派发异常（queue_seq={qseq}，前序 seq={prior_seq}）: "
                              f"{str(_de)[:150]}",
                        diag={"code": "a2a_queue_dispatch_exception",
                              "phase": "queue_drain", "ref_seq": prior_seq})
        except Exception:
            pass
        return False


def _parse_queued_row(row):
    """把 events 行解析为 (qseq, repo_key, agent_type, issue_id, payload, qinfo, linked_seq)。"""
    qseq, repo_key, agent_type, issue_id, payload_raw = row
    if not qseq or not repo_key:
        return None
    try:
        payload = json.loads(payload_raw or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    qinfo = payload.get("_a2a_queue") or {}
    if not isinstance(qinfo, dict):
        qinfo = {}
    try:
        _linked_seq = int(qinfo.get("existing_seq") or 0)
    except Exception:
        _linked_seq = 0
    return qseq, repo_key, agent_type, issue_id or "", payload, qinfo, _linked_seq


def drain_queued_a2a(prior_seq: int) -> list:
    """Issue #1667：前序 A2A 事件终态后，派发等待在其后的排队 A2A 事件。

    queue=true 的请求在命中活跃事件时先落一条 status='queued' 的 a2a_event
    （payload._a2a_queue.existing_seq 关联前序事件），本函数在前序事件进入终态时
    取出这些排队事件，原子置 running 并复用 run_a2a_dispatch 派发。

    - 原子 UPDATE（status='queued' → 'running'）保证并发终态回调下「仅派发一次」；
    - 派发失败 → _fail_event 置 failed（不无限重试，避免排障黑洞）；
    - 查询按前序事件的 (repo, issue, agent) 收窄（不再全仓扫描，审查修复④）；
    - 重入锁保证 drain 期间由 _fail_event 触发的二次终态钩子不递归；
    - 无排队记录或查询失败 → 返回空列表，不影响终态写入主流程。
    """
    if not prior_seq:
        return []
    if not _A2A_DRAIN_LOCK.acquire(blocking=False):
        log.info(f"a2a queue drain: another drain in progress, skip prior_seq={prior_seq}")
        return []
    dispatched = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key, issue_id, agent_type FROM events WHERE seq=? LIMIT 1",
                      (prior_seq,))
            _prior = c.fetchone()
        if not _prior:
            return []
        _prior_repo, _prior_issue, _prior_agent = _prior[0] or "", _prior[1] or "", _prior[2] or ""
        if not _prior_repo:
            return []
        _clauses = ["repo_key=?", "event_type='a2a_event'", "status='queued'"]
        _params = [_prior_repo]
        if _prior_issue:
            _clauses.append("issue_id=?")
            _params.append(_prior_issue)
        if _prior_agent:
            _clauses.append("agent_type=?")
            _params.append(_prior_agent)
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, repo_key, agent_type, issue_id, payload FROM events WHERE "
                + " AND ".join(_clauses) + " ORDER BY seq ASC", tuple(_params))
            rows = c.fetchall() or []
        for row in rows:
            parsed = _parse_queued_row(row)
            if not parsed:
                continue
            qseq, repo_key, agent_type, issue_id, payload, qinfo, _linked_seq = parsed
            if _linked_seq != int(prior_seq):
                continue
            if _claim_and_dispatch_queued(qseq, repo_key, agent_type, issue_id,
                                          payload, qinfo, prior_seq):
                dispatched.append(qseq)
        return dispatched
    except Exception as _e:
        log.warning(f"a2a queue drain: query/dispatch failed prior_seq={prior_seq}: {_e}")
        return dispatched
    finally:
        _A2A_DRAIN_LOCK.release()


def reconcile_queued_a2a(repo_key: str = "") -> list:
    """Issue #1667：排队滞留兜底对账扫描（Spec 风险表要求）。

    覆盖两类 drain 钩子触达不到的场景（审查修复①）：
    - 前序事件经直接 SQL 写终态的旁路（runtime/service 任务级联、issues admin 确认）；
    - 「活跃判定 → 排队落库」之间的竞态窗口：前序在间隙内完成，排队行落库时
      已无「终态回调」可触发。

    谓词：queued 事件的 `_a2a_queue.existing_seq` 指向的事件**不再活跃**
    （不在 running/dispatched/queued，或已不存在）→ 立即按 drain 同样的原子抢占派发。
    """
    if not _A2A_DRAIN_LOCK.acquire(blocking=False):
        log.info("a2a queue reconcile: another drain/reconcile in progress, skip")
        return []
    try:
        with db_lock:
            c = db.cursor()
            if repo_key:
                c.execute(
                    "SELECT seq, repo_key, agent_type, issue_id, payload FROM events "
                    "WHERE repo_key=? AND event_type='a2a_event' AND status='queued' "
                    "ORDER BY seq ASC", (repo_key,))
            else:
                c.execute(
                    "SELECT seq, repo_key, agent_type, issue_id, payload FROM events "
                    "WHERE event_type='a2a_event' AND status='queued' ORDER BY seq ASC")
            rows = c.fetchall() or []
        parsed_rows = [p for p in (_parse_queued_row(r) for r in rows) if p]
        if not parsed_rows:
            return []
        _prior_seqs = sorted({p[6] for p in parsed_rows if p[6]})
        _prior_status = {}
        if _prior_seqs:
            _ph = ",".join("?" * len(_prior_seqs))
            with db_lock:
                c = db.cursor()
                c.execute(f"SELECT seq, status FROM events WHERE seq IN ({_ph})",
                          tuple(_prior_seqs))
                for _s, _st in (c.fetchall() or []):
                    _prior_status[int(_s)] = _st or ""
        dispatched = []
        for qseq, _repo, agent_type, issue_id, payload, qinfo, _linked_seq in parsed_rows:
            if not _linked_seq:
                # 无关联前序（历史/异常行）：无法判定，交由人工排障，不盲目派发
                continue
            _pst = _prior_status.get(_linked_seq)
            if _pst in _A2A_PRIOR_ACTIVE:
                continue
            if _claim_and_dispatch_queued(qseq, _repo, agent_type, issue_id,
                                          payload, qinfo, _linked_seq):
                dispatched.append(qseq)
        if dispatched:
            log.info(f"a2a queue reconcile: dispatched {len(dispatched)} stranded queued "
                     f"event(s) repo={repo_key or '*'}")
        return dispatched
    except Exception as _e:
        log.warning(f"a2a queue reconcile failed repo={repo_key or '*'}: {_e}")
        return []
    finally:
        _A2A_DRAIN_LOCK.release()


# _pool_only_local_direct（方案B/方案A self-run 兜底）已随 self-run 块一并移除
# （local-claimer 设计第二个 PR）。本机 direct 引擎的发现逻辑由
# src/runtime/local_claimer.py 的 _local_direct_engine_ids 承接。


def _enqueue_pipeline_task(repo_key, seq, agent_type, backend, skill_name, evt,
                           skill_id, skill_path, runtime_id, issue_id="", pool_engine_ids="", user_id=0,
                           is_scene_recommend=False, pool_engine_source=""):
    """init_event_json -> enqueue_task (deterministic routing runtime_id).
    Local worker / remote RuntimeAgent consume.

    Plugin mode (GRAPE_PIPELINE_MODE=plugin): sets pool_tag on enqueue
    so workers with matching engine tags can claim via claim_by_tag.

    pool_engine_source（local-claimer 设计）：pool_engine_ids 来源标记。
    场景绑定路径传 'scene'，引擎自动选择传 'auto'；空=未标记（历史语义，
    降级判定按 scene 保守处理）。调用方未显式传时：带 pool_engine_ids 且
    evt 带 scene 上下文 → 'scene'，否则 'auto'。
    """
    from src.api.event_archive import init_event_json
    from src.runtime.service import enqueue_task, get_execution_mode
    from src.runtime.instances import runtime_instance_name
    from src.repo_key import from_repo_key
    import os as _os

    # local-claimer 设计：未显式指定时按上下文推导来源（带场景上下文的绑定 = scene）
    if pool_engine_ids and not pool_engine_source:
        _scene_ctx = evt.get("scene_id") or evt.get("_scene_id") or ""
        pool_engine_source = "scene" if _scene_ctx else "auto"

    issue_id = issue_id or evt.get("issue_id", "") or "#0"
    # 1) 路由判定：
    #    runtime_id>0 → 确定性路由（指定引擎）
    #    runtime_id=0 + plugin 模式 → 无主池任务（广播），设 pool_agent_type 让引擎竞争认领
    #    runtime_id=0 + 传统模式 → 派发失败
    _pool_agent_type = ""
    if not runtime_id:
        if is_plugin_mode():
            _pool_agent_type = agent_type
            log.info(f"Pipeline enqueue (broadcast): runtime_id=0 pool_agent_type={agent_type} seq={seq}")
        else:
            _fail_event(seq, "入队失败：无可用引擎（runtime_id 为空）")
            return
    # Issue #630：技能物化完整性校验门禁（enqueue 前，fail-fast）——配置了技能但
    # 内容源全缺失（空壳技能行，如 sid=3276 vod-cli-crossrepo）时显式告警并阻断派发，
    # 不静默降级；技能未配置（推荐模式/无 skill）不阻断，official 内置包兜底放行。
    if skill_name:
        from src.runtime.service import verify_skill_materializable
        _gate_ok, _gate_reason, _gate_sid = verify_skill_materializable(
            skill_name, skill_id=skill_id or 0, repo_key=repo_key, user_id=user_id)
        if not _gate_ok:
            log.warning(f"Pipeline skill gate BLOCKED seq={seq} agent={agent_type} "
                        f"skill={skill_name} sid={_gate_sid}: {_gate_reason}")
            _fail_event(seq, f"技能物化校验未通过，已阻断派发（skill={skill_name}）: "
                             f"{_gate_reason[:200]}")
            return
    # 引擎实例名（`{type}-{device}`，与 UI 同源）：runtime_id 反查，查不到回退类型名。
    # 注意：engine_select v2 派发已选定具体引擎（runtime_id>0），无 pool_mode 路径；
    # 历史 pool 模式（runtime_id=0 + pool_agent_type）由 service.claim 兼容处理。
    inst_name = runtime_instance_name(runtime_id) or backend or ""
    # 2) event.json 骨架（#0 跳过，见 event_archive._extract_num）
    _evt_copy = dict(evt)
    _evt_copy["issue_id"] = issue_id
    init_event_json(repo_key, seq, _evt_copy, agent_type, runtime=inst_name, skill=skill_name)

    repository = evt.get("repository", from_repo_key(repo_key))
    ws = get_agent_workdir(repo_key, agent_type, issue_id)
    _os.makedirs(ws, exist_ok=True)

    # 3) 会话：配置会话（repo+agent）+ 执行会话（Issue 级，串行粒度，见 multi-issue-isolation-design.md §3.3）
    #    每 Issue 独立 session（见 vod-agent-hang-rootcause-fix.md 方案 A）：有关联 Issue 只用 Issue 会话；
    #    repo 级会话仅用于无关联 Issue（push/#0）。
    from src.api.session_store import get_or_create_agent_session, get_or_create_issue_session, guard_issue_session_pointer
    from src.api.event_store import update_event_config
    update_event_config(seq, inst_name, skill_name, datetime.now(timezone.utc).isoformat())
    session = get_or_create_agent_session(repo_key, agent_type, backend or "")
    # 更新 session 的 runtime_id 为实际执行引擎
    if runtime_id:
        try:
            with db_lock:
                _c = db.cursor()
                _c.execute("UPDATE devops_agent_session SET runtime_id=? WHERE id=?", (runtime_id, session.get("id","")))
                db.commit()
        except Exception:
            pass
    oc_session_id = ""
    issue_session_id = ""
    if _issue_num_from(issue_id):
        _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
        issue_session_id = _isess.get("id") or ""
        # session 引擎守卫：runtime_id>0（确定性路由）时校验指针引擎与目标引擎同家，
        # 跨引擎指针弃用（hermes resume opencode 会话号会静默挂死）；广播/查不到 → 放行，
        # 由 worker 侧执行时兜底校验。
        _cur_engine = ""
        if runtime_id:
            from src.runtime.instances import engine_type_of_runtime
            _cur_engine = engine_type_of_runtime(runtime_id)
        oc_session_id = guard_issue_session_pointer(_isess, _cur_engine)
    # repo 级 devops_agent_session.runtime_session_id 已废弃（无消费方），不再回退读取。

    repo_gitcode_token = _get_task_repo_token(repo_key)

    # 4) 构建 prompt（含技能占位符化，见 skill-delivery-remote.md）
    try:
        evt["repo_key"] = repo_key
        evt["issue_id"] = issue_id
        evt["_skill_path"] = skill_path
        if is_scene_recommend:
            from src.prompt_builder import build_scene_recommender_prompt
            prompt = build_scene_recommender_prompt(evt, repo_key)
        else:
            from src.prompt_builder import build_devops_agent_prompt
            prompt = build_devops_agent_prompt(evt, [], "", [skill_name] if skill_name else [agent_type],
                                  skill_path=skill_path, runtime_id=runtime_id, agent_type=agent_type)
    except Exception as e:
        log.warning(f"Pipeline enqueue: prompt build failed: {e}")
        _fail_event(seq, f"入队失败：prompt 构建异常 {str(e)[:150]}")
        return

    from src.prompt_builder import resolve_task_server_env
    # Issue #1660：REPO_OWNER/REPO_NAME 由 REPO_FULL_NAME 派生（缺失回退 repo_key 并告警），
    # REPO_FULL_NAME 归一（剥 provider 前缀）与 owner/name 同源口径。
    from src.repo_key import derive_repo_identity as _derive2
    _full_name2, _owner2, _repo_name2 = _derive2(repository, repo_key)
    env_extra = {
        "REPO_FULL_NAME": _full_name2,
        "REPO_OWNER": _owner2,
        "REPO_NAME": _repo_name2,
        "ACCOUNT": resolve_actor_account(evt, repo_key),
        # Issue #1577：GRAPE_SERVER_URL 承载业务 API 基址（issues/git-repo 全量端点），
        # worker runtime / A2A 回连基址由 RUNTIME_SERVER_URL 独立承载（语义拆分）。
        # 统一复用 resolve_task_server_env，与 build_task_env / chat 入队同源，防双实现漂移。
        **resolve_task_server_env(runtime_id),
    }
    # 注入场景环境变量（scene_id / work_branch / custom_fields；值不暴露在 prompt 文本里）
    # Issue #427：PR 订阅任务（evt 带 _pr_sub_scene_id）场景按订阅场景注入——
    # PR 有关联 Issue 时 issue_id 是真实编号，按其解析会拿到 Issue 场景，
    # 订阅场景 custom_fields（口令/IP）全丢；无关联 Issue 时 override 与伪 id
    # 正则解析结果同值，行为不变；普通任务无该键 → 空串回退。
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
        _linked_repos = _read_scene_linked_repos(repo_key, issue_id)
        if _linked_repos:
            env_extra["GRAPE_LINKED_REPOS"] = json.dumps(_linked_repos, ensure_ascii=False)
            # Issue #466: 关联仓 token 随 env 分发（worker 端本地无库，纯本地解析必为空）
            _inject_linked_repo_tokens(env_extra, env_extra["GRAPE_LINKED_REPOS"])
    except Exception:
        pass
    # 事件级扩展环境变量（evt["_env_extra"] 通用机制；onboarding 已不使用，Issue #46）
    _evt_ext = evt.get("_env_extra") or {}
    if isinstance(_evt_ext, dict):
        env_extra.update(_evt_ext)
    # Issue #573：BOT_USERNAME/AGENT_NAME 注入全部 devops agent（原仅 coding 注 BOT_USERNAME，
    # issue-runbook Step 4 机器人接管 assignee 拿不到值）；源/兜底见 _inject_bot_agent_env。
    _inject_bot_agent_env(env_extra, repo_key, agent_type)
    _inject_issue_pr_env(env_extra, evt, repo_key)

    # 方案 D 防御：有关联 Issue 时，Issue 级会话指针不得指向其他 Issue 的 workdir（防 cwd/session 错位回归）
    try:
        _num = _issue_num_from(issue_id)
        if _num and oc_session_id:
            from src.repo_key import workspace_repo_dir
            _expected_ws = os.path.join(workspace_repo_dir(repo_key), "issues", _num, agent_type)
            if ws != _expected_ws:
                log.warning(f"Pipeline enqueue: cwd/session mismatch, issue={issue_id} ws={ws} expected={_expected_ws}")
    except Exception:
        pass

    try:
        _pool_tag = agent_type if is_plugin_mode() else ""
        tid = enqueue_task(
            agent_session_id=session.get("id") or "",
            runtime_id=runtime_id,
            event_seq=seq, issue_id=issue_id,
            repo_key=repo_key, agent_type=agent_type, skill=skill_name,
            prompt=prompt, cwd=ws,
            runtime_session_id=oc_session_id,
            issue_session_id=issue_session_id,
            event_type=evt.get("type", ""),
            account=evt.get("account") or "",
            user_id=user_id,
            repo_gitcode_token=repo_gitcode_token,
            env_extra=env_extra,
            skill_path=skill_path,
            skill_id=skill_id,
            pool_tag=_pool_tag,
            pool_agent_type=_pool_agent_type,
            pool_engine_ids=pool_engine_ids,
        )
    except Exception as e:
        log.warning(f"Pipeline enqueue: enqueue_task failed for seq={seq}: {e}")
        _fail_event(seq, f"enqueue_task error {str(e)[:150]}")
        return
    _mode_tag = "plugin" if is_plugin_mode() else "legacy"
    log.info(f"Pipeline: enqueued task={tid} seq={seq} repo={repo_key} agent={agent_type} engine={runtime_id} issue_session={issue_session_id or '-'} mode={get_execution_mode()} dispatch={_mode_tag} pool_tag={_pool_tag or '-'}")

    # 方案 B self-run 兜底已移除（local-claimer 设计第二个 PR，2026-10-14 issue #6 复发后）：
    # 本机 direct 引擎现由 local_claimer_loop 以一等认领者身份参与广播（与远端 worker 同一条
    # claim_by_tag 检查链：白名单/健康分/满载/scope 全约束）。此处的 self-run 会在入队同一毫秒
    # 抢跑绑定引擎的任务（2026-10-14 pvs-zombies-demo2 issue #4/#6 两轮事故根因），不再需要。


def _resolve_scene_id(repo_key: str, issue_id: str = "") -> str:
    """解析 Issue 当前绑定开发场景（issue-219：env 注入 GRAPE_SCENE_ID 用）。

    优先读 issue 卡片 scene_id（get_issue_scene → issue 单表权威），空则回退
    managed_repos.dev_scene（与 runtime/repogit._read_scene_work_branch 同源，保证同场景）。
    未选定返回空串——agent 借此区分「场景未判定」，不回落 default（避免误导）。
    """
    scene_id = ""
    if issue_id:
        try:
            from src.api.session_store import get_issue_scene
            scene_id, _ = get_issue_scene(repo_key, issue_id)
        except Exception:
            scene_id = ""
    if not scene_id:
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
                row = c.fetchone()
            scene_id = (row[0] if row else "") or ""
        except Exception:
            scene_id = ""
    # Issue #1745 复审修复：绑定的场景已被删除时，不注入幽灵 scene_id（scenes detail 必 404），
    # 回退仓库默认场景；也不可用则按「未判定=空串」返回，与 reconcile 侧同源。
    if scene_id and not _scene_binding_valid(repo_key, scene_id):
        log.warning(f"[scene-env] resolved scene no longer resolvable "
                    f"(scene={scene_id} repo={repo_key}); falling back to dev_scene")
        scene_id = _repo_dev_scene(repo_key) or ""
        if scene_id and not _scene_binding_valid(repo_key, scene_id):
            scene_id = ""
    return scene_id


def _issue_authoritative_scene(repo_key: str, issue_id: str = ""):
    """Issue 真实归属场景（`issues detail.scene_id` 同源，Issue #1745）。

    与 `_resolve_scene_id` 的区别：只读真实绑定（issue_state → issue 单表），
    空时**不**回退仓库默认场景，用于识别 `GRAPE_SCENE_ID` 与 Issue 归属的
    不一致（诊断/执行前校正）。PR 订阅伪 id（`#pr_<PR号>_<scene_id>`）由
    `get_issue_bound_scene` 统一解析，不在此处重复手写。

    返回值三态（Issue #1745 复审修复）：
    - 非空字符串：Issue 已绑定该场景（权威真值）；
    - 空字符串：查询成功且确认 Issue 未绑定场景（调用方按「未选定=空串」处理）；
    - `None`：查询异常/缺少 issue_id，**未判定**——调用方必须跳过校正，
      不得把正确的 GRAPE_SCENE_ID 误清空。
    """
    _iid = str(issue_id or "")
    if not _iid:
        return None
    try:
        from src.api.session_store import get_issue_bound_scene
        _sid, _ = get_issue_bound_scene(repo_key, _iid)
    except Exception:
        log.warning(f"[scene-env] authoritative scene lookup failed: "
                    f"issue={_iid} repo={repo_key}")
        return None
    if not _sid:
        return ""
    # Issue #1745 复审修复：绑定场景已删除时不得继续注入幽灵 scene_id
    # （agent 拿它调 scenes detail 必 404，症状与原报障相同）→ 按「未绑定」处理。
    if not _scene_binding_valid(repo_key, _sid):
        log.warning(f"[scene-env] issue binding points to a deleted scene "
                    f"(scene={_sid} issue={_iid} repo={repo_key}); treat as unbound")
        return ""
    return _sid


def _scene_binding_valid(repo_key: str, scene_id: str) -> bool:
    """场景是否仍可解析（仓库副本 / 官方模板 / 内置 pipelines.json）。

    Issue #1745 复审：只用 `get_pipeline_config` 这一条解析链（与 scenes detail 同源），
    避免另造一份存在性口径；解析异常一律按「可用」处理，不因 DB 抖动误清空场景。
    """
    if not scene_id:
        return False
    try:
        from src.api.pipeline_config import get_pipeline_config
        cfg = get_pipeline_config(scene_id, repo_key)
    except Exception as e:
        log.warning(f"[scene-env] scene existence check failed (treat as valid): "
                    f"scene={scene_id} repo={repo_key}: {e}")
        return True
    if not cfg:
        return False
    # 解析链回落到了别的场景（如 default/full fallback）→ 该 scene_id 实际不存在
    return not cfg.get("fallback")


def _repo_dev_scene(repo_key: str):
    """仓库默认开发场景（managed_repos.dev_scene）；查询异常返回 None（未判定）。"""
    if not repo_key:
        return None
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,)).fetchone()
        return (row[0] if row else "") or ""
    except Exception as e:
        log.warning(f"[scene-env] dev_scene lookup failed for {repo_key}: {e}")
        return None


def scene_env_mismatch_diagnostic(injected: str, authoritative: str) -> str:
    """场景注入值与 Issue 真实归属不一致时的可操作诊断（Issue #1745，不阻断）。"""
    return (
        "场景环境诊断：GRAPE_SCENE_ID 与 Issue 真实归属场景不一致，"
        f"注入值={injected or '(空)'}，真实值={authoritative or '(空)'}。"
        "请以 `grape issues detail` 返回的 scene_id 为准执行 "
        "`grape scenes detail --scene_id <真实值>` 重取节点后重试；"
        "仅当真实场景确实不含目标节点时才进入待确认。"
    )


_PR_SUB_ISSUE_ID_RE = re.compile(r"^#?pr_\d+_")
_PR_SUB_PROMPT_MARKERS = ("本任务来自 PR 订阅场景", "## PR 审查任务")


def _pr_subscription_state(issue_id: str = "", event_seq: int = 0,
                           event_type: str = "", title: str = "",
                           prompt: str = ""):
    """判定任务是否属于 PR 订阅场景，返回三态（Issue #1745 复审修复）。

    - `True`：确认是 PR 订阅任务；
    - `False`：确认不是（普通 Issue/A2A/评论任务）；
    - `None`：未判定（events 查询异常/事件行缺失），调用方应保守跳过校正。

    命中路径（Issue #427 语义）：
    1. issue_id 为 PR 订阅伪 id（`#pr_<PR号>_<scene_id>`）；
    2. event_type 为 PR 事件类型（PR 关联 Issue 的主路由订阅任务）；
    3. 事件标题为「PR 订阅」派发（真实关联 Issue 号 + a2a_event 的既有形态）；
    4. 关联 events 行的 event_type / title 命中 2/3；
    5. 事件行不可用（event_seq=0/缺失）时，回退任务 prompt 快照中的订阅标记。
    """
    if _PR_SUB_ISSUE_ID_RE.match(str(issue_id or "")):
        return True
    try:
        from src.api.routing import PR_EVENT_TYPES
    except Exception:
        return None
    if (event_type or "") in PR_EVENT_TYPES:
        return True
    if str(title or "").startswith("PR 订阅"):
        return True
    if any(_m in str(prompt or "") for _m in _PR_SUB_PROMPT_MARKERS):
        return True
    if event_seq:
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                _row = db.cursor().execute(
                    "SELECT event_type, title FROM events WHERE seq=?",
                    (event_seq,)).fetchone()
        except Exception:
            log.warning(f"[scene-env] events lookup failed for seq={event_seq}, "
                        f"skip scene reconcile conservatively")
            return None
        if not _row:
            log.warning(f"[scene-env] events row missing for seq={event_seq}, "
                        f"skip scene reconcile conservatively")
            return None
        if (_row[0] or "") in PR_EVENT_TYPES:
            return True
        if str(_row[1] or "").startswith("PR 订阅"):
            return True
        return False
    return False


def _is_pr_subscription_task(issue_id: str = "", event_seq: int = 0,
                             event_type: str = "", title: str = "",
                             prompt: str = "") -> bool:
    """判断任务是否属于 PR 订阅场景（Issue #427 语义，Issue #1745 唯一判定入口）。

    保守口径：未判定（`None`）也视为 PR 订阅/需保留订阅场景，避免 PR 订阅任务被
    误校正为 Issue 场景而重现 #427。详见 `_pr_subscription_state` 三态说明。
    """
    return _pr_subscription_state(
        issue_id, event_seq, event_type, title, prompt) is not False


def reconcile_task_scene_env(task_id: str, env: dict) -> str:
    """执行前用 Issue 归属场景校正任务快照中的 GRAPE_SCENE_ID（Issue #1745）。

    worker 执行前拉取服务端最新 env（`GET /runtimes/{id}/tasks/{task_id}/env`），
    此前只同步 AK/SK 与会话 token，`GRAPE_SCENE_ID` 仍是入队时快照。跨场景转交后，
    转交前入队的任务（或重派沿用了源事件场景的任务）会拿到源场景。此处在执行前按
    `agent_tasks → issue 当前归属场景`（`get_issue_bound_scene`，与
    `issues detail.scene_id` 同源且不回退 dev_scene）重算并覆盖陈旧快照；
    PR 订阅任务保持订阅场景语义不校正；Issue 未绑定场景时按「未选定=空串」口径处理；
    PR 订阅判定未定（events 查询异常/事件行缺失）或 Issue 归属查询异常时一律跳过，
    不覆盖快照，避免误清空正确的 GRAPE_SCENE_ID。

    返回校正后的 scene_id（无需校正时为快照原值）。
    """
    if not isinstance(env, dict):
        return ""
    _snapshot = str(env.get("GRAPE_SCENE_ID") or "")
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            _row = db.cursor().execute(
                "SELECT repo_key, issue_id, event_seq, event_type, prompt "
                "FROM agent_tasks WHERE id=?",
                (task_id,)).fetchone()
    except Exception:
        return _snapshot
    if not _row:
        return _snapshot
    _repo_key = _row[0] or ""
    _issue_id = _row[1] or ""
    _event_seq = _row[2] or 0
    _event_type = _row[3] or ""
    _prompt = _row[4] or ""
    if not _repo_key or not _issue_id:
        return _snapshot
    if _pr_subscription_state(_issue_id, _event_seq, _event_type,
                              prompt=_prompt) is not False:
        return _snapshot
    _authoritative = _issue_authoritative_scene(_repo_key, _issue_id)
    if _authoritative is None:
        return _snapshot
    if not _authoritative:
        # Issue #1745 复审修复：未绑定场景时与 DIRECT(`_resolve_scene_id`) 同源，
        # 回退仓库默认场景（dev_scene），不再把快照清成空串——否则同一任务
        # 本机执行拿 dev_scene、远端 worker 拿空串，出现口径分裂。
        _dev = _repo_dev_scene(_repo_key)
        if _dev is None:
            return _snapshot
        _authoritative = _dev
    if _authoritative != _snapshot:
        env["GRAPE_SCENE_ID"] = _authoritative
        log.info(scene_env_mismatch_diagnostic(_snapshot, _authoritative) +
                 f" [scene-env reconcile] task={task_id} issue={_issue_id} repo={_repo_key}")
        return _authoritative
    return _snapshot


def _inject_scene_env(env_extra: dict, repo_key: str, issue_id: str = "",
                      agent_type: str = "", scene_id_override: str = "") -> None:
    """注入场景相关环境变量：GRAPE_SCENE_ID + 高级选项 custom_fields（Issue #236 直接注入）。

    值不暴露在 prompt 文本里，agent 通过 printenv / os.environ 直接读取。
    - GRAPE_SCENE_ID：当前 Issue 绑定的场景标识；未选定注入空串（不回落 default，agent 判「未判定」）。
    - custom_fields：以自定义变量名（name）直接作为环境变量名注入（如 MY_API_KEY=xxx）。
      注入前做二次冲突/大写校验（兜底存量或旁路数据），冲突项跳过并告警。
      scope 过滤：字段 scope 含 "all" 或含当前 agent_type 才注入；scope 为空/未传
      agent_type 时全量注入（保持现状行为）。
    - scene_id_override（Issue #427）：PR 订阅派发任务的订阅场景。PR 有关联 Issue 时
      issue_id 会解析为真实 Issue 编号，场景被误判为 Issue 场景，订阅场景 custom_fields
      （口令/IP 等）全部丢失；显式传入订阅场景（evt._pr_sub_scene_id）短路 issue_id
      解析。空串/未传 → 回退现有 _resolve_scene_id 逻辑，普通任务零破坏。
      Issue #1745：普通 Issue/A2A 重派不得再把事件行旧 scene_id 作为 override
      （跨场景转交后事件行仍是源场景），否则会覆盖 Issue 当前归属场景。
    """
    try:
        _override = (scene_id_override or "").strip()
        if _override:
            _authoritative = _issue_authoritative_scene(repo_key, issue_id)
            if _authoritative and _override != _authoritative:
                log.info(scene_env_mismatch_diagnostic(_override, _authoritative) +
                         f" [scene-env inject] issue={issue_id} repo={repo_key}; "
                         f"PR subscription overrides are expected")
        scene_id = _override or _resolve_scene_id(repo_key, issue_id)
        env_extra["GRAPE_SCENE_ID"] = scene_id
        if not scene_id:
            return
        from src.session_manager import db, db_lock
        from src.scene_custom_env import is_upper_name, is_reserved_name, normalize_scope
        import json as _json
        # 读 advanced_options 的 custom_fields
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
        if not row or not row[0]:
            return
        opts = _json.loads(row[0]) if row[0] else {}
        fields = [f for f in (opts.get("custom_fields", []) or []) if f.get("name")]
        for f in fields:
            name = (f.get("name") or "").strip()
            if not name:
                continue
            if not is_upper_name(name) or is_reserved_name(name):
                log.warning(f"scene custom env '{name}' rejected: reserved or not uppercase")
                continue
            # scope 多选过滤：["all"] 或包含当前 agent_type 才注入
            scope = normalize_scope(f.get("scope"))
            if agent_type and "all" not in scope and agent_type not in scope:
                continue
            env_extra[name] = str(f.get("value", ""))
    except Exception as _e:
        log.warning(f"inject scene env failed: {_e}")


def _inject_linked_repo_tokens(env: dict, linked_repos_json: str) -> None:
    """Issue #466 断点二: 服务端解析各关联仓 bot token 随 env 分发。

    worker 端本地 sqlite 无 managed_repos 数据（无 MySQL），Repo(rk).bot_token
    必为空 → 关联仓 worktree 全部 skip。服务端托管着有效 token，此处按
    GRAPE_LINKED_REPOS 逐仓解析注入 GRAPE_LINKED_REPO_TOKENS（JSON
    {repo_key: token}），与 GRAPE_SESSION_TOKEN 同一 env 分发链路。
    token 只进 env_extra（随 agent_tasks 存储与任务同生命周期），不进
    prompt / 日志 / 落盘。解析失败或无 token 的仓不入 map（worker 端回退
    本地解析）。
    """
    if not linked_repos_json:
        return
    import json as _json
    try:
        linked = _json.loads(linked_repos_json)
    except Exception:
        return
    if not isinstance(linked, list):
        return
    tokens = {}
    for lr in linked:
        rk = (lr or {}).get("repo_key") or "" if isinstance(lr, dict) else ""
        if not rk:
            continue
        try:
            from src.token_service import get_repo_bot_token
            tok = get_repo_bot_token(rk)
        except Exception:
            tok = ""
        if tok:
            tokens[rk] = tok
    if tokens:
        env["GRAPE_LINKED_REPO_TOKENS"] = _json.dumps(tokens, ensure_ascii=False)


def _read_scene_linked_repos(repo_key: str, issue_id: str = "") -> list:
    """读取场景关联仓库列表（Issue #242）。

    优先读 issue_state.scene_id 对应场景的 linked_repos 列，
    空则读 advanced_options JSON 中的 linked_repos。
    返回 [{repo_key, branch}] 列表。
    """
    try:
        scene_id = _resolve_scene_id(repo_key, issue_id)
        if not scene_id:
            return []
        from src.session_manager import db, db_lock
        import json as _json
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT linked_repos, advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
        if not row:
            return []
        # 优先读独立列
        if row[0]:
            try:
                return _json.loads(row[0]) or []
            except Exception:
                pass
        # 回退读 advanced_options JSON
        if row[1]:
            try:
                opts = _json.loads(row[1])
                return opts.get("linked_repos", []) or []
            except Exception:
                pass
        return []
    except Exception:
        return []


def build_task_env(repo_key: str, agent_type: str, issue_id: str, runtime_id: int = 0,
                   account: str = "", issue_number: str = "", pr_number: str = "",
                   scene_id_override: str = "") -> dict:
    """构建任务完整 env_extra（正常入队/重派/对话入队共用，防重派丢变量）。

    2026-10-10 事故：events.py 重派路径 enqueue_task(env_extra={}) 丢了
    GRAPE_SERVER_URL（远程 worker 上 CLI 落 localhost:8000，A2A 交办全连不上），
    同时丢 GRAPE_WORK_BRANCH / GRAPE_SCENE_ID / custom_fields / GRAPE_LINKED_REPOS
    （worktree 建错分支、场景自定义变量失效）。本函数对齐 _enqueue_pipeline_task
    （原 L2243-2293）的 env 组装，重派处调用方按 old.runtime_id 重建。

    与正常入队的差异（调用方已持有或无意义的不重复注入）：
    - ISSUE_NUMBER/PR_NUMBER：参数传入则注入
    - BOT_USERNAME/AGENT_NAME（Issue #573）：与正常入队同逻辑（全部 devops agent）
    - evt._env_extra 事件级扩展：重派无 evt，无法还原（快照已在 prompt 里），不注入
    - scene_id_override（Issue #427）：重派 PR 订阅任务时传旧任务/事件的订阅场景，
      防止 env 被重新解析回 Issue 场景；空串回退 issue_id 解析（普通任务零破坏）。
    """
    import json as _json
    env = {}
    # ── 仓库/服务地址基础四项 ──
    # Issue #1660：REPO_FULL_NAME/OWNER/NAME 统一由派生入口产出；重派只有 repo_key，
    # 以其无前缀全名作为 REPO_FULL_NAME 事实源（不再出现 REPO_NAME 为空却回退带前缀全名）。
    from src.repo_key import derive_repo_identity as _derive_env
    from src.repo_key import from_repo_key as _from_rk_env
    _full_name, _owner, _repo_name = _derive_env(_from_rk_env(repo_key), repo_key)
    env["REPO_FULL_NAME"] = _full_name or repo_key
    env["REPO_OWNER"] = _owner
    env["REPO_NAME"] = _repo_name
    env["ACCOUNT"] = account
    from src.prompt_builder import resolve_task_server_env
    # Issue #1577：GRAPE_SERVER_URL=业务 API 基址，RUNTIME_SERVER_URL=worker runtime 基址。
    env.update(resolve_task_server_env(runtime_id))
    # ── 场景环境（work_branch / scene_id / custom_fields / linked_repos）──
    try:
        from src.runtime.repogit import _read_scene_work_branch
        _wb = _read_scene_work_branch(repo_key, issue_id)
        if _wb:
            env["GRAPE_WORK_BRANCH"] = _wb
    except Exception:
        pass
    _inject_scene_env(env, repo_key, issue_id, agent_type,
                      scene_id_override=scene_id_override)
    try:
        _linked = _read_scene_linked_repos(repo_key, issue_id)
        if _linked:
            env["GRAPE_LINKED_REPOS"] = _json.dumps(_linked, ensure_ascii=False)
            # Issue #466: 关联仓 token 随 env 分发（worker 端本地无库，纯本地解析必为空）
            _inject_linked_repo_tokens(env, env["GRAPE_LINKED_REPOS"])
    except Exception:
        pass
    # ── Issue #573：BOT_USERNAME/AGENT_NAME 注入全部 devops agent（原仅 coding 注 BOT_USERNAME）──
    _inject_bot_agent_env(env, repo_key, agent_type)
    # ── issue/pr 编号（Issue #528：仅注入纯数字，伪 id/占位值不注入）──
    _in = str(issue_number or "").lstrip("#")
    if _in.isdigit():
        env["ISSUE_NUMBER"] = _in
    _pn = str(pr_number or "").lstrip("#")
    if _pn.isdigit():
        env["PR_NUMBER"] = _pn
    return env


def get_prompt_context(repo_key: str) -> str:
    """构建 Prompt 用的 Issue 聚类上下文"""
    summary = get_issue_summary(repo_key)
    if not summary:
        return ""
    return f"\n\n[Event History]\n{summary}\n"


def _materialize_direct_workdir(ws: str, repo_key: str, agent_type: str, skill_name: str,
                                evt: dict, issue_id: str, prompt: str,
                                engine_name: str = "") -> str:
    """组件4：direct 端统一物化 project-info / skill / knowledge 到工位 ws 下。

    替代旧的「占位符替换为服务端绝对路径（仓库级 project-info 原件 / 全局 SKILLS_DIR）」，
    使 direct 端与 worker 端 cwd 布局一致（.project-info/.skills/.knowledge 均在 ws 内），
    后续 bwrap 锁只需挂 ws，无需放行仓库级目录或全局 SKILLS_DIR。

    与 worker 端 _execute_task 物化语义对齐：
    - .project-info/  ← load_project_info + member_files_pi + agent_files（rel 相对 project-info）
    - .skills/{skill}/ ← OBS zip 解压 或 本地技能目录 copytree
    - .knowledge/      ← knowledge_files（rel 相对 WORKSPACE_DIR，与 worker 原样一致）

    任一步失败仅告警并跳过（不抛异常、不阻断执行），保留其它占位符继续替换。
    """
    _issue = issue_id or evt.get("issue_id", "") or (evt.get("payload") or {}).get("issue_number", "") or ""

    # 1) project-info + knowledge 物化（Issue #95：统一调 collect_knowledge_files）
    proj_dir = os.path.join(ws, ".project-info")
    know_dir = os.path.join(ws, ".knowledge")
    try:
        from src.runtime.service import collect_knowledge_files
        _collected = collect_knowledge_files(repo_key, agent_type=agent_type, issue_id=_issue)
        # project-info 物化（baseline + member）
        for rel, meta in (_collected.get("project_info") or {}).items():
            if rel == ".metadata.json":
                continue
            _dest = os.path.join(proj_dir, rel)
            try:
                os.makedirs(os.path.dirname(_dest), exist_ok=True)
                _content = meta.get("content", "") if isinstance(meta, dict) else str(meta)
                with open(_dest, "w", encoding="utf-8") as f:
                    f.write(_content)
            except Exception:
                pass
        prompt = prompt.replace("{{PROJECT_INFO_DIR}}", proj_dir)
        # knowledge 物化（agent 沉淀知识）
        for rel, meta in (_collected.get("knowledge") or {}).items():
            _dest = os.path.join(know_dir, rel)
            try:
                os.makedirs(os.path.dirname(_dest), exist_ok=True)
                _content = meta.get("content", "") if isinstance(meta, dict) else str(meta)
                with open(_dest, "w", encoding="utf-8") as f:
                    f.write(_content)
            except Exception:
                pass
        prompt = prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)
    except Exception as e:
        log.warning(f"direct knowledge materialize failed for {repo_key}: {e}")

    # 2) skill 物化（OBS zip / 本地技能目录 / skill_path → ws/.skills/{skill_name}/）
    #    Issue #564：原子物化（tmp+rename）+ 完整性校验（SKILL.md+references 按定义清单）
    #    + 官方技能包回退 + 降级标注（evt._skill_materialize / prompt 降级说明），不静默空注入。
    skill_dir = os.path.join(ws, ".skills")
    try:
        _primary_meta = {}  # Issue #1635：记录主渠道技能来源（skill_id/scope）

        def _skill_primary():
            _skill_local = ""  # 本地技能根目录（非空则 copytree）
            from src.user.db import get_skill_by_id, get_skill_path
            _skill_id = evt.get("_skill_id") or 0
            if _skill_id:
                _sk = get_skill_by_id(_skill_id)
                if _sk:
                    _primary_meta["skill_id"] = int(_skill_id)
                    _primary_meta["scope"] = _sk.get("scope") or ""
                    _okey = _sk.get("file_path") or ""
                    if _okey:
                        # OBS 技能：zip 根 = {skill_name}/，原子解压到 ws/.skills/
                        from src.storage import get_skill_zip_bytes
                        _zip = get_skill_zip_bytes(_okey)
                        if _zip:
                            return ("zip", _zip)
                        return ("none", f"skill zip unavailable: {_okey}")
                    _full = get_skill_path(_skill_id)
                    if _full and os.path.exists(_full):
                        _skill_local = os.path.dirname(_full)
            else:
                from src.config import resolve_official_skill_path as _resolve_skill_path
                _real_skill_md = _resolve_skill_path(skill_name or "")
                if _real_skill_md:
                    # review：按名命中官方路径须置 scope=official，否则漂移校验被跳过
                    _primary_meta["scope"] = "official"
                else:
                    _sp = evt.get("_skill_path", "") or ""
                    if _sp and os.path.isfile(_sp):
                        _real_skill_md = _sp
                    elif _sp and os.path.isfile(os.path.join(_sp, "SKILL.md")):
                        _real_skill_md = os.path.join(_sp, "SKILL.md")
                if _real_skill_md:
                    _skill_local = os.path.dirname(_real_skill_md)
            if _skill_local and skill_name and os.path.isdir(_skill_local):
                return ("dir", _skill_local)
            return ("none", f"skill source not found for {skill_name}")

        if skill_name:
            from src.runtime.skill_materialize import (materialize_skill_with_fallback,
                                                        write_skill_fallback)

            def _agent_type_checker_resolver(at):
                # Issue #617：按名未命中 → 按 agent_type 回退官方检查器（DB 侧解析）
                try:
                    from src.user.db import resolve_official_checker_skill, get_skill_path
                    _fb = resolve_official_checker_skill(at)
                    if not _fb:
                        return None
                    _p = get_skill_path(_fb.get("id")) or _fb.get("skill_path") or ""
                    _d = os.path.dirname(_p) if _p and os.path.isfile(_p) else ""
                    if not _d:
                        return None
                    return {"skill_name": _fb.get("skill_name"),
                            "skill_id": _fb.get("id") or 0, "skill_dir": _d}
                except Exception:
                    return None

            # Issue #1635 review：_primary_meta 由 _skill_primary() 执行时才写入，
            # 参数在调用前求值会恒得空值；改为传取值 callable，primary 执行后再解析。
            _mat = materialize_skill_with_fallback(
                skill_dir, skill_name, _skill_primary,
                agent_type=agent_type, agent_type_fallback=_agent_type_checker_resolver,
                scope=lambda: _primary_meta.get("scope") or "",
                skill_type=agent_type, skill_id=lambda: _primary_meta.get("skill_id") or 0)
            # 降级标注写入 evt（_execute_agent_inner 落事件 agent_logs）
            evt["_skill_materialize"] = _mat
            if _mat.get("status") in ("fallback", "degraded"):
                _id_hint = ""
                if _mat.get("skill_id"):
                    _id_hint = (f" (fallback checker: "
                                f"`grape skills detail --skill_id {_mat.get('skill_id')}`)")
                _note = (f"\n\n[Skill Materialization Notice] DEVOPS_SKILLS_DIR={skill_dir} "
                         f"({_mat.get('status')}): {_mat.get('detail')}.{_id_hint} "
                         f"If skill files under this path are missing, fetch the skill via "
                         f"`grape skills detail --skill_id <id>` or the platform skills API "
                         f"before proceeding.")
                prompt += _note
                log.warning(f"direct skill materialize {skill_name}: {_mat.get('status')} "
                            f"({_mat.get('detail')})")
                # Issue #607：官方包回退也失败（degraded）→ 写 FALLBACK.md 降级指引，
                # 与 worker 端兜底口径一致（grape skills detail 权威取回）。
                if _mat.get("status") == "degraded":
                    write_skill_fallback(skill_dir, skill_name,
                                         _mat.get("detail", ""), stage="direct")
            else:
                log.info(f"direct skill materialize {skill_name}: ok")
        prompt = prompt.replace("{{SKILL_DIR}}", skill_dir)
        # Issue #887：direct 端执行前一致性校验（fail-fast 判定落 evt，_execute_agent
        # 拒绝派发引擎）+ 来源标注（agent_logs）。绑定未物化且本地缓存存在其它技能 →
        # ok=False（缓存不得覆盖绑定，拒绝执行）。键为 SKILL_CONSISTENCY_KEY 契约常量
        # （scheduler direct 端同键消费，物化后判定，口径一致）。
        # Issue #887（QA 复审修复）：direct 端同样扫描引擎原生技能目录
        # （engine_skill_dirs，按 engine_name 推导后端），与 worker 门
        # （agent._skill_consistency_gate）扫描口径对齐——绑定未物化、工位 .skills
        # 为空、残留落在引擎原生缓存（seq=46108 变体）时不再静默放行。
        # scheduler direct 端消费同一 evt 判定，一并覆盖。
        _extra_dirs: list[str] = []
        try:
            from src.runtime.agent import _backend_for_engine
            from src.runtime.os_adapter import engine_skill_dirs as _esd
            _extra_dirs = _esd(_backend_for_engine(engine_name))
        except Exception as _ede:
            log.warning(f"direct skill consistency: engine skill dirs resolve "
                        f"failed for engine '{engine_name}': {_ede}")
        try:
            from src.runtime.skill_materialize import (verify_skill_execution_consistency,
                                                        SKILL_CONSISTENCY_KEY)
            evt[SKILL_CONSISTENCY_KEY] = verify_skill_execution_consistency(
                skill_name or "", skill_dir, extra_scan_dirs=_extra_dirs)
        except Exception as _sc_e:
            log.warning(f"direct skill consistency verify failed: {_sc_e}")
    except Exception as e:
        log.warning(f"direct skill materialize failed for {skill_name}: {e}")
        evt["_skill_materialize"] = {"status": "degraded", "detail": str(e), "skill_dir": ""}
        # Issue #607：direct 端物化异常分支同样写 FALLBACK.md 降级指引 + 留痕
        try:
            from src.runtime.skill_materialize import write_skill_fallback
            write_skill_fallback(skill_dir, skill_name,
                                 f"direct materialize exception: {e}", stage="direct")
        except Exception:
            pass

    return prompt


def _submit_direct_linked_prs(env_extra: dict, repo_key: str, issue_id: str,
                              repo_gitcode_token: str = "", task_id: str = ""):
    """Issue #242: direct 模式 coding agent 完成后，向关联仓库提交 PR。

    与 worker 端 _submit_linked_repo_prs 等价：遍历 GRAPE_LINKED_WORKTREES，
    对每个有改动的关联仓库 worktree 执行 push + create_pr_for_repo。
    失败只告警，不阻塞任务完成与主仓库 PR 流程。
    """
    import json as _json
    import os as _os
    import subprocess as _sp
    from src.runtime import repogit
    from src.config import log as _log

    _linked_wt_json = env_extra.get("GRAPE_LINKED_WORKTREES", "")
    if not _linked_wt_json:
        return
    try:
        _wts = _json.loads(_linked_wt_json)
    except Exception:
        return
    if not _wts or not isinstance(_wts, list):
        return

    _issue_num = repogit.issue_num_from(issue_id)
    if not _issue_num:
        _log.warning("direct linked PR: cannot determine issue_num, skip")
        return

    _wt_branch = f"feat/issue-{_issue_num}"
    _bot_name = env_extra.get("BOT_USERNAME") or "grape_bot"
    _results = []

    for wt in _wts:
        if not isinstance(wt, dict):
            continue
        _rk = wt.get("repo_key") or ""
        _wt_dir = wt.get("dir") or ""
        _base_branch = wt.get("branch") or "main"
        if not _rk or not _wt_dir or not _os.path.isdir(_wt_dir):
            _log.warning(f"direct linked PR: skip invalid worktree: rk={_rk} dir={_wt_dir}")
            continue

        # Issue #993（M1）：分支级写租约须先于 ensure_feature_branch 获取（其可能写分支）。
        # 与 worker 端共用 branch_lease 上下文，租约获取/释放生命周期不再各写一遍。
        from src.runtime import branch_guard as _branch_guard
        with _branch_guard.branch_lease(
                _rk, _wt_branch, task_id=str(task_id or "")) as _lease:
            if not _lease.get("ok"):
                _log.warning(f"direct linked PR: branch lease blocked {_rk}:{_wt_branch}: "
                             f"{_lease.get('message')}")
                _results.append({"repo_key": _rk, "status": "blocked",
                                 "reason": _lease.get("reason", "branch_locked"),
                                 "message": _lease.get("message", "")})
                continue

            try:
                # 0. 分支护栏：检查当前分支是否是 feat/issue-{N}，禁止直推基线分支
                from src.runtime.repogit import ensure_feature_branch
                ensure_feature_branch(_wt_dir, _wt_branch, _base_branch, logger=_log)

                # 1. git identity（防 commit 被拒）
                _sp.run(["git", "-C", _wt_dir, "config", "user.name", _bot_name],
                        capture_output=True, text=True, timeout=10)
                _sp.run(["git", "-C", _wt_dir, "config", "user.email",
                         f"{_bot_name}@noreply.git.com"],
                        capture_output=True, text=True, timeout=10)

                # 2. stage 未提交改动
                _sp.run(["git", "-C", _wt_dir, "add", "-A"],
                        capture_output=True, text=True, timeout=60)
                _r_diff = _sp.run(["git", "-C", _wt_dir, "diff", "--cached", "--quiet"],
                                  capture_output=True, text=True, timeout=30)
                _has_staged = _r_diff.returncode != 0

                # 3. 检查是否有领先于 base 分支的提交
                _r_ahead = _sp.run(
                    ["git", "-C", _wt_dir, "rev-list", "--count",
                     f"origin/{_base_branch}..HEAD"],
                    capture_output=True, text=True, timeout=15)
                _ahead = 0
                if _r_ahead.returncode == 0:
                    try:
                        _ahead = int((_r_ahead.stdout or "0").strip() or "0")
                    except ValueError:
                        _ahead = 0
                else:
                    _ahead = 1  # origin/{base} 不存在 → 保守认为有改动

                if not _has_staged and _ahead == 0:
                    _log.info(f"direct linked PR: no changes in {_rk}, skip")
                    _results.append({"repo_key": _rk, "status": "skipped", "reason": "no changes"})
                    continue

                # Issue #752（M2）：commit 前冲突标记推送守卫（与 worker 端 _submit_linked_repo_prs
                # 同口径）：暂存硬命中阻断提交/推送；软命中告警不阻断。
                # 保真告警不在推送口（PR #874 审查修正）：agent 正常编码必然修改功能文件，
                # 推送口告警全是噪声；保真校验保留在清理口（同函数尾部 cleanup 段）。
                try:
                    from src.runtime.worktree_guard import scan_staged_files
                    _g = scan_staged_files(_wt_dir)
                    if _g.get("hard"):
                        _log.warning(f"direct linked PR: conflict markers in staged files, "
                                     f"BLOCKED push {_rk}: {_g['hard']}")
                        _results.append({"repo_key": _rk, "status": "blocked",
                                         "reason": f"conflict markers in staged files: "
                                                   f"{list(_g['hard'].keys())[:5]}"})
                        continue
                    if _g.get("soft"):
                        _log.warning(f"direct linked PR: soft conflict markers (allowed) {_rk}: {_g['soft']}")
                except Exception as _ge:
                    _log.warning(f"direct linked PR: worktree guard failed {_rk}: {_ge}")

                # 4. commit staged changes
                if _has_staged:
                    _msg = f"feat: #{_issue_num} linked repo changes"
                    _r_commit = _sp.run(["git", "-C", _wt_dir, "commit", "-m", _msg],
                                        capture_output=True, text=True, timeout=60)
                    if _r_commit.returncode != 0:
                        _log.warning(f"direct linked PR: commit failed {_rk}: {(_r_commit.stderr or '')[:200]}")
                        _results.append({"repo_key": _rk, "status": "failed", "reason": "commit failed"})
                        continue

                # 5. push（token 经 http.extraheader 注入）
                # R20.26.3: 关联仓库独立获取 token
                from src.repo import Repo
                _linked_token = Repo(_rk).bot_token
                if not _linked_token:
                    _log.warning(f"direct linked PR: no token for {_rk}, skip push")
                    _results.append({"repo_key": _rk, "status": "failed", "reason": "no token (expired or not configured)"})
                    continue
                # Issue #993（M2）：direct 自动推送路径统一走 branch_guard.guarded_push。
                _push = _branch_guard.guarded_push(
                    _rk, _wt_branch, _wt_dir, token=_linked_token,
                    run_git=repogit._run_git)
                if not _push.get("ok"):
                    _log.warning(f"direct linked PR: push blocked/failed {_rk}:{_wt_branch}: "
                                 f"{_push.get('message')}")
                    _status = "blocked" if _push.get("stage") == "preflight" else "failed"
                    _results.append({"repo_key": _rk, "status": _status,
                                     "reason": _push.get("reason", "guard_blocked"),
                                     "message": _push.get("message", "")})
                    continue

                # 6. create PR
                from src.vcs.api_client import create_pr_for_repo
                _pr_title = f"feat: #{_issue_num} linked repo ({_rk})"
                _pr_body = (f"跨仓库需求开发 PR（Issue #{_issue_num}）\n\n"
                            f"由 coding agent 自动提交，关联主仓库 {repo_key}。\n\n"
                            f"Closes #{_issue_num}")
                _pr_result = create_pr_for_repo(
                    repo_key=_rk, token=_linked_token,
                    title=_pr_title, head=_wt_branch, base=_base_branch,
                    body=_pr_body)
                if isinstance(_pr_result, dict) and "error" not in _pr_result:
                    _pr_num = _pr_result.get("number") or ""
                    _pr_url = _pr_result.get("html_url") or ""
                    _log.info(f"direct linked PR: created #{_pr_num} for {_rk}: {_pr_url}")
                    _results.append({"repo_key": _rk, "status": "created",
                                     "pr_number": _pr_num, "pr_url": _pr_url})
                else:
                    _err = _pr_result.get("error", "unknown") if isinstance(_pr_result, dict) else "unknown"
                    _log.warning(f"direct linked PR: create_pr failed {_rk}: {_err}")
                    _results.append({"repo_key": _rk, "status": "failed", "reason": f"PR create: {_err}"})
            except Exception as e:
                _log.warning(f"direct linked PR: error for {_rk}: {e}")
                _results.append({"repo_key": _rk, "status": "error", "reason": str(e)[:200]})

    _created = [r for r in _results if r.get("status") == "created"]
    _skipped = [r for r in _results if r.get("status") == "skipped"]
    _failed = [r for r in _results if r.get("status") in ("failed", "error")]
    _log.info(f"direct linked PR summary: {len(_created)} created, {len(_skipped)} skipped, {len(_failed)} failed")


def _cleanup_direct_workdir(repo_key: str, agent_type: str, issue_id: str = ""):
    """direct 端任务级工位清理：删轻文件（.skills/.project-info/.knowledge）+ 安全卸载 worktree。

    在 _execute_agent 的 finally 中调用，覆盖正常/失败/异常全部路径且幂等。
    清理失败只告警，不影响事件结果上报。

    Issue #242: coding agent 多仓库时，同步清理关联仓库 worktree。
    """
    try:
        ws = get_agent_workdir(repo_key, agent_type, issue_id or "")
        import shutil as _shutil
        for _sub in (".skills", ".project-info", ".knowledge"):
            _p = os.path.join(ws, _sub)
            try:
                if os.path.isdir(_p):
                    _shutil.rmtree(_p, ignore_errors=True)
            except Exception:
                pass
        from src.runtime import repogit
        _num = repogit.issue_num_from(issue_id or "")
        if _num:
            _token = ""
            try:
                from src.repo import Repo
                _token = Repo(repo_key).bot_token
            except Exception:
                pass
            # Issue #752（M2）：清理前保真校验（与 worker 端 _cleanup_task_workdir 同口径）
            try:
                from src.runtime.worktree_guard import verify_fidelity
                _fid_dr = verify_fidelity(ws)
                if not _fid_dr.get("baseline_missing") and (_fid_dr.get("modified") or _fid_dr.get("removed")):
                    log.warning(f"direct workdir cleanup fidelity alarm {repo_key}/{agent_type}: "
                                f"modified={_fid_dr.get('modified')} removed={_fid_dr.get('removed')}")
            except Exception as _fe:
                log.warning(f"direct cleanup fidelity check failed: {_fe}")
            repogit.remove_issue_worktree_if_pushed(repo_key, agent_type, _num, token=_token)
            # Issue #242: 清理关联仓库 worktree（coding agent 多仓库）
            # 关联 worktree 路径由 repogit.issue_dir_for(repo_key, "coding", issue_num) 推导，
            # 通过查询场景 linked_repos 获取关联仓库列表（与初始化路径对称）
            if agent_type == "coding":
                try:
                    _linked_repos = _read_scene_linked_repos(repo_key, issue_id)
                    for lr in (_linked_repos or []):
                        if not isinstance(lr, dict):
                            continue
                        _lrk = lr.get("repo_key") or ""
                        if _lrk:
                            repogit.remove_issue_worktree_if_pushed(
                                _lrk, "coding", _num, token=_token)
                except Exception as _le:
                    log.warning(f"direct linked worktree cleanup failed: {_le}")
        # 连空 ws 目录一起删（non-git agent 删完物化文件后为空；git agent 的 worktree 已被整目录删掉）
        if os.path.isdir(ws) and not os.listdir(ws):
            _shutil.rmtree(ws, ignore_errors=True)
    except Exception as e:
        log.warning(f"direct workdir cleanup failed for {repo_key}/{agent_type}: {e}")


def _extract_deploy_url(result_text: str) -> str:
    """Issue #468/#482: 从 result_text 提取并校验部署 URL（双登记路径共享）。

    提取策略：结构化优先（artifacts / issue_summary 中的 `deploy_url: <url>` 条目），
    回落全文完整 URL 正则（保留资源路径不截断）。
    校验 http(s) 前缀 + 长度 ≤512；未命中/拒绝返回空串。
    """
    text = (result_text or "").strip()
    if not text:
        return ""
    url = ""
    # ① 结构化优先：result_text 为 JSON 时，在 artifacts / issue_summary 中找 deploy_url: 条目
    try:
        _parsed = json.loads(text)
        if isinstance(_parsed, dict):
            for _key in ("artifacts", "issue_summary"):
                _val = _parsed.get(_key)
                _candidates = [str(x) for x in _val] if isinstance(_val, list) else ([_val] if isinstance(_val, str) else [])
                for _cand in _candidates:
                    _m = re.search(r"deploy_url:\s*(https?://\S+)", _cand)
                    if _m:
                        url = _m.group(1)
                        break
                if url:
                    break
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # ② 回落：全文完整 URL 正则（保留资源路径，不截断）
    if not url:
        _urls = re.findall(r'https?://[^\s"\'\)\]，。]+', text)
        if _urls:
            url = _urls[0]
    url = (url or "").strip().rstrip('.,;')
    # ③ 校验：http(s) 前缀 + 长度 ≤512（沿用 #446 规则）
    if not url or not url.lower().startswith(("http://", "https://")) or len(url) > 512:
        return ""
    return url


def register_sre_deploy_url(repo_key: str, result_text: str) -> str:
    """Issue #468: SRE 完成后从 result_text 提取部署 URL 登记到 managed_repos.deploy_url。

    直连 _execute_agent_inner 与远程 worker complete_task() 两条收尾路径统一调用。
    提取校验逻辑见 _extract_deploy_url；成功打日志并返回登记到的 URL；
    未登记（无 URL / 校验拒绝 / repo 不存在）返回空串。
    """
    url = _extract_deploy_url(result_text)
    if not url:
        return ""
    # 登记（repo 不存在时 rowcount=0，不写库）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE managed_repos SET deploy_url=? WHERE repo_key=?", (url, repo_key))
            if c.rowcount > 0:
                db.commit()
                log.info(f"SRE deploy_url auto-registered: {url} for {repo_key}")
                return url
    except Exception as _e:
        log.warning(f"SRE deploy_url extraction failed: {_e}")
    return ""


def register_scene_deploy_url(repo_key: str, issue_id: str, result_text: str) -> str:
    """Issue #482: SRE 完成后把部署 URL 登记到需求绑定场景的 devops_pipeline_scene.deploy_url。

    与 register_sre_deploy_url（仓库级，#468）并列，双收尾路径统一调用：
    - 场景定位：get_issue_scene 读 issue 绑定 scene_id（Issue 级快照），
      仅更新该绑定场景（scene_id+repo_key+is_active=1），无绑定跳过不报错；
    - 提取校验：复用 _extract_deploy_url；
    - UPDATE 幂等覆盖（与手动 PUT /api/scenes/{scene_id}/deploy-url 同语义，后写为准）；
    - 登记失败仅记日志，不影响任务收尾主流程；未登记返回空串。
    """
    scene_id = ""
    try:
        from src.api.session_store import get_issue_scene
        scene_id, _ = get_issue_scene(repo_key, issue_id)
    except Exception as _e:
        log.warning(f"scene deploy_url register: resolve issue scene failed: {_e}")
        return ""
    if not scene_id:
        return ""
    url = _extract_deploy_url(result_text)
    if not url:
        return ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE devops_pipeline_scene SET deploy_url=? "
                "WHERE scene_id=? AND repo_key=? AND is_active=1",
                (url, scene_id, repo_key))
            if c.rowcount > 0:
                db.commit()
                log.info(f"scene deploy_url auto-registered: {url} for scene {scene_id} ({repo_key})")
                return url
    except Exception as _e:
        log.warning(f"scene deploy_url register failed: {_e}")
    return ""


# ===== Issue #1545: agent 产出回帖策略（阻止引擎报错文本外泄到 Issue/PR） =====
_ERROR_TEXT_PATTERNS = (
    "model not found", "model_not_found", "traceback (most recent call last)",
    "econnrefused", "etimedout", "rate limit", "quota exceeded",
    "sockettimeout", "upstream", "bad gateway", "service unavailable",
)
_RESULT_POST_POLICY_WARNED = False


def _looks_like_error_text(text: str) -> bool:
    """Issue #1545 B1：判断文本是否为引擎/上游报错文本（固定规则集，不得增删）。"""
    t = text or ""
    lower = t.lower()
    if re.search(r"http\s*[45]\d\d", t, re.IGNORECASE):
        return True
    if any(p in lower for p in _ERROR_TEXT_PATTERNS):
        return True
    if t.lstrip().startswith("\u274c"):
        return True
    if "exit_code=" in lower:
        return True
    if "http 401" in lower or "http 403" in lower:
        return True
    return False


def _result_post_policy() -> str:
    """Issue #1545 B2：读取 GRAPE_RESULT_POST_POLICY（strict 默认 / clean / off）。

    非法值回落 strict，进程内只告警一次；有效值不缓存，便于运行时与测试切换。
    """
    global _RESULT_POST_POLICY_WARNED
    raw = (os.environ.get("GRAPE_RESULT_POST_POLICY") or "").strip().lower()
    if raw in ("strict", "clean", "off"):
        return raw
    if raw and not _RESULT_POST_POLICY_WARNED:
        log.warning(f"invalid GRAPE_RESULT_POST_POLICY={raw!r}, fallback to strict")
        _RESULT_POST_POLICY_WARNED = True
    return "strict"


def _parse_task_errors(raw):
    """把 agent_tasks.errors 原始值解析为列表（兼容历史纯字符串）。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [str(raw)]
    if isinstance(parsed, list):
        return parsed
    return [parsed]


def _record_post_suppressed(task_id, seq, repo_key, issue_number, agent_type, reason, raw_text):
    """Issue #1545 C1/C2：把一次结果回帖抑制留痕写入 agent_tasks.errors。

    复用既有 errors JSON 去重追加通路，不新增列。幂等键 = 同 issue + raw_text 前 200 字；
    DB 异常只告警，绝不影响任务终态与主流程。
    """
    try:
        _rec = {
            "error_code": "RESULT_POST_SUPPRESSED",
            "policy": _result_post_policy(),
            "reason": reason,
            "agent_type": agent_type or "",
            "issue_number": str(issue_number or ""),
            "raw_text": (raw_text or "")[:500],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _idem_key = (raw_text or "")[:200]
        _where_col = ""
        _where_val = None
        _existing = None
        with db_lock:
            c = db.cursor()
            if task_id:
                c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
                row = c.fetchone()
                if row:
                    _where_col, _where_val, _existing = "id", task_id, _parse_task_errors(row[0])
            elif seq is not None:
                c.execute(
                    "SELECT errors FROM agent_tasks WHERE event_seq=? "
                    "ORDER BY created_at DESC LIMIT 1", (seq,))
                row = c.fetchone()
                if row:
                    _where_col, _where_val, _existing = "event_seq", seq, _parse_task_errors(row[0])
            else:
                # 无任务上下文（系统通知等）→ 按 repo_key + issue 兜底定位最近任务
                _issue_num = str(issue_number or "").lstrip("#")
                c.execute(
                    "SELECT id, errors FROM agent_tasks WHERE repo_key=? AND "
                    "(issue_id=? OR issue_id=? OR issue_id LIKE ?) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (repo_key or "", str(issue_number or ""), _issue_num, f"%{_issue_num}%"))
                row = c.fetchone()
                if row:
                    _where_col, _where_val, _existing = "id", row[0], _parse_task_errors(row[1])
            if _where_col and _existing is not None:
                for e in _existing:
                    if (isinstance(e, dict) and e.get("error_code") == "RESULT_POST_SUPPRESSED"
                            and (e.get("raw_text") or "")[:200] == _idem_key
                            and str(e.get("issue_number") or "") == _rec["issue_number"]):
                        return
                _existing = [e for e in _existing if str(e).strip()]
                _existing.append(_rec)
                if len(_existing) > 5:
                    _existing = _existing[:1] + _existing[1:][-4:]
                c.execute(f"UPDATE agent_tasks SET errors=? WHERE {_where_col}=?",
                          (json.dumps(_existing, ensure_ascii=False), _where_val))
                db.commit()
                return
        log.warning(f"record_post_suppressed: no task row for {repo_key}#{issue_number}")
    except Exception as e:
        log.warning(f"record_post_suppressed failed: {e}")


def _apply_agent_result_policy(result_text: str, structured_conclusion: str, plain_text: str,
                               task_id, seq, repo_key, issue_number, agent_type):
    """Issue #1545 A3/B3：对 agent 产出套用回帖策略。

    返回应当发布的文本；返回空串表示抑制（已留痕）。仅 agent_result=True 时调用。
    """
    _policy = _result_post_policy()
    if _policy == "off":
        # 修复（2026-11-09）：off 的语义是「关闭抑制、恢复照贴」，原实现记录 policy_off 后
        # 返回空串＝照样全量抑制，与开关含义相反（QA 复核发现）。
        return structured_conclusion or (plain_text or "")[:3000]
    if _policy == "strict" and not structured_conclusion:
        _record_post_suppressed(task_id, seq, repo_key, issue_number, agent_type,
                                "strict_no_conclusion", result_text)
        log.info(f"Exec agent: suppress result comment by policy=strict (no structured conclusion) "
                 f"({repo_key}#{issue_number})")
        return ""
    if _policy == "clean" and not structured_conclusion and _looks_like_error_text(plain_text):
        _record_post_suppressed(task_id, seq, repo_key, issue_number, agent_type,
                                "clean_error_text", result_text)
        log.info(f"Exec agent: suppress result comment by policy=clean (error text) "
                 f"({repo_key}#{issue_number})")
        return ""
    # Issue #1545 B3：无结构化 conclusion 时沿用既有 3000 字截断，避免长正文整段外发。
    return structured_conclusion or (plain_text or "")[:3000]


def _post_result_to_issue(repository: str, issue_number, result_text: str, bot_token: str, agent_type: str,
                          repo_key: str = "", *, agent_result: bool, task_id: str = "", seq=None):
    """Agent 执行完成后，将结果作为评论发回 VCS Issue。

    根据 repo_key provider 分流 GitHub/GitCode。
    方案 B：先 GET Issue 评论列表，若已有 agent 发的评论则跳过系统回帖，
    避免与 agent 自己发的评论重复（"一鱼两吃"问题）。
    agent 忘了发评论时系统补发，保证 Issue 不会静悄悄。

    Issue #1545：`agent_result` 必填以区分「agent 产出」（True，套用回帖策略，
    阻止引擎/上游报错文本外泄）与「系统通知」（False，行为完全不变）。
    """
    if not repository or not issue_number or not bot_token:
        return
    # 跳过场景推荐结果（借 VOD 身份做的推荐，非真正工作产出，不应发到 Issue）
    try:
        import json as _json
        _parsed = _json.loads(result_text)
        if isinstance(_parsed, dict) and _parsed.get("scene_id") and _parsed.get("reason") and not _parsed.get("conclusion"):
            log.info(f"Skip posting scene recommendation result to issue {issue_number}")
            return
    except Exception:
        pass
    # 过滤：优先提取结构化 conclusion，避免思考过程/JSON 块泄漏到 Issue 评论
    _conclusion = ""
    try:
        from src.api.event_store import parse_agent_output_json
        _parsed = parse_agent_output_json(result_text)
        if _parsed and _parsed.get("conclusion"):
            _conclusion = _parsed["conclusion"]
    except Exception:
        pass
    _structured_conclusion = _conclusion
    _plain_text = ""
    # conclusion 提取失败时：保留有效 result_text（纯文本），避免思考过程/JSON 块泄漏到 Issue 评论；
    # result_text 为空或纯 JSON 时无内容可发，直接跳过回帖（Issue #679：不发占位文案）
    if not _conclusion:
        _stripped = (result_text or "").replace("<!-- grape-auto-comment -->", "").strip()
        if _stripped and not _stripped.startswith("{") and not _stripped.startswith("["):
            _plain_text = _stripped
            _conclusion = _stripped[:3000]
        else:
            if agent_result and (result_text or "").strip():
                _record_post_suppressed(task_id, seq, repo_key, issue_number, agent_type,
                                        "no_conclusion", result_text)
            log.info(f"Exec agent: skip system comment, no conclusion or valid text to post to {repository}/issues/{issue_number}")
            return
    if agent_result:
        _conclusion = _apply_agent_result_policy(
            result_text, _structured_conclusion, _plain_text,
            task_id, seq, repo_key, issue_number, agent_type)
        if not _conclusion:
            return

    # 根据 repo_key provider 分流
    if _is_github_repo(repo_key):
        try:
            from src.repo_key import split_owner_repo
            _owner, _repo = split_owner_repo(repo_key)
            # 先查 Issue 已有评论，若已有 agent 评论则跳过（方案 B）
            _existing = _github_list_issue_comments(_owner, _repo, str(issue_number), bot_token)
            _agent_tag_lower = agent_type.lower()
            for _c in _existing:
                _body = (_c.get("body") or "").lower()
                if _agent_tag_lower in _body and "agent]" in _body:
                    log.info(f"Exec agent: skip system comment, agent already posted to {repository}/issues/{issue_number}")
                    return
            # agent 没发评论，系统补发（只发 conclusion，不发原始 result_text）
            _comment = f"[{agent_type.upper()} Agent] {_conclusion}"
            _comment += "\n<!-- grape-auto-comment -->"
            _gh_resp = _github_post_issue_comment(_owner, _repo, str(issue_number), _comment, bot_token)
            _ledger_record_comment("github", repo_key, repository, issue_number, _comment, _gh_resp)
            log.info(f"Exec agent: posted system comment (agent did not post) to {repository}/issues/{issue_number}")
        except Exception as e:
            log.warning(f"Exec agent: failed to post result comment (GitHub): {e}")
        return

    # GitCode 原逻辑
    try:
        import urllib.request, json as _json
        # 先查 Issue 已有评论，若已有 agent 评论则跳过（方案 B）
        _list_url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{issue_number}/comments?per_page=100"
        _list_req = urllib.request.Request(_list_url, headers={
            "Authorization": f"Bearer {bot_token}",
        })
        _resp = urllib.request.urlopen(_list_req, timeout=15)
        _existing = _json.loads(_resp.read())
        _agent_tag_lower = agent_type.lower()
        for _c in _existing:
            _body = (_c.get("body") or "").lower()
            # 匹配评论前缀中包含 agent_type 的（如 [blog-vod Agent] 匹配 vod）
            if _agent_tag_lower in _body and "agent]" in _body:
                log.info(f"Exec agent: skip system comment, agent already posted to {repository}/issues/{issue_number}")
                return
        # agent 没发评论，系统补发（只发 conclusion，不发原始 result_text）
        _comment = f"[{agent_type.upper()} Agent] {_conclusion}"
        _comment += "\n<!-- grape-auto-comment -->"
        _gitcode_post_issue_comment_ledgered(repository, issue_number, _comment, bot_token,
                                             repo_key=repo_key)
        log.info(f"Exec agent: posted system comment (agent did not post) to {repository}/issues/{issue_number}")
    except Exception as e:
        log.warning(f"Exec agent: failed to post result comment: {e}")



def _post_subtask_completion_to_parent(repository: str, sub_issue_num: str, parent_issue_num: str,
                                        result_text: str, bot_token: str, repo_key: str = ""):
    """子任务完成后，在父 Issue 评论登记完成情况。"""
    try:
        import urllib.request, json as _json
        from src.api.event_store import parse_agent_output_json
        _out = parse_agent_output_json(result_text)
        _summary = (_out.get("conclusion", "") if _out else "") or result_text[:200]
        _comment = f"### \u5b50\u4efb\u52a1 #{sub_issue_num} \u5df2\u5b8c\u6210\n\n{_summary}\n\n---\n\u5b50\u4efb\u52a1\u6d41\u6c34\u7ebf\u5df2\u5168\u90e8\u5b8c\u6210\u3002"
        _comment += "\n<!-- grape-auto-comment -->"
        if _is_github_repo(repo_key):
            from src.repo_key import split_owner_repo
            _owner, _repo = split_owner_repo(repo_key)
            _gh_resp = _github_post_issue_comment(_owner, _repo, parent_issue_num, _comment, bot_token)
            # Issue #453 台账（审查 P1-1 补漏）：subtask 完成回帖入账
            _ledger_record_comment("github", repo_key, repository, parent_issue_num, _comment, _gh_resp)
        else:
            _gitcode_post_issue_comment_ledgered(repository, parent_issue_num, _comment, bot_token,
                                                 repo_key=repo_key)
        log.info(f"Subtask #{sub_issue_num} completion posted to parent #{parent_issue_num}")
    except Exception as e:
        log.warning(f"Post subtask completion failed: {e}")


def _post_all_subtasks_done(repository: str, parent_issue_num: str, subtasks: list, bot_token: str,
                            repo_key: str = ""):
    """所有子任务完成后，在父 Issue 发汇总报告。"""
    try:
        import urllib.request, json as _json
        _lines = [f"### \u5168\u90e8\u5b50\u4efb\u52a1\u5df2\u5b8c\u6210\n"]
        for s in subtasks:
            _sid = s.get("issue_id", "?")
            _stage = s.get("stage", "?")
            _status = "\u2705" if _stage == "done" else "\u274c"
            _lines.append(f"- {_status} {_sid} ({_stage})")
        _lines.append(f"\n\u7236\u4efb\u52a1 #{parent_issue_num} \u7684\u6240\u6709\u5b50\u4efb\u52a1\u5df2\u5b8c\u6210\u3002")
        _comment = "\n".join(_lines)
        _comment += "\n<!-- grape-auto-comment -->"
        if _is_github_repo(repo_key):
            from src.repo_key import split_owner_repo
            _owner, _repo = split_owner_repo(repo_key)
            _gh_resp = _github_post_issue_comment(_owner, _repo, parent_issue_num, _comment, bot_token)
            # Issue #453 台账（审查 P1-1 补漏）：全部完成汇总回帖入账
            _ledger_record_comment("github", repo_key, repository, parent_issue_num, _comment, _gh_resp)
        else:
            _gitcode_post_issue_comment_ledgered(repository, parent_issue_num, _comment, bot_token,
                                                 repo_key=repo_key)
        log.info(f"All subtasks done summary posted to parent #{parent_issue_num}")
    except Exception as e:
        log.warning(f"Post all subtasks done failed: {e}")


def _post_result_to_pr(repository: str, pr_number: str, result_text: str, bot_token: str,
                        agent_type: str, repo_key: str = "", *, agent_result: bool,
                        task_id: str = "", seq=None):
    """PR 订阅场景：Agent 执行完成后，将结果作为评论发回 PR。

    GitCode: POST /repos/{owner}/{repo}/pulls/{pr_number}/comments
    GitHub:  POST /repos/{owner}/{repo}/issues/{pr_number}/comments（PR 共用 Issue 评论 API）

    Issue #1545：`agent_result` 必填以区分「agent 产出」与「系统通知」，语义同 Issue 回帖。
    """
    if not repository or not pr_number or not bot_token:
        return
    # 提取 conclusion
    _conclusion = ""
    try:
        from src.api.event_store import parse_agent_output_json
        _parsed = parse_agent_output_json(result_text)
        if _parsed and _parsed.get("conclusion"):
            _conclusion = _parsed["conclusion"]
    except Exception:
        pass
    _structured_conclusion = _conclusion
    _plain_text = ""
    if not _conclusion:
        # Issue #679：无 conclusion 时保留有效纯文本正文；为空或纯 JSON 时跳过发帖（不发占位文案）
        _stripped = (result_text or "").replace("<!-- grape-auto-comment -->", "").strip()
        if _stripped and not _stripped.startswith("{") and not _stripped.startswith("["):
            _plain_text = _stripped
            _conclusion = _stripped[:3000]
        else:
            if agent_result and (result_text or "").strip():
                _record_post_suppressed(task_id, seq, repo_key, pr_number, agent_type,
                                        "no_conclusion", result_text)
            log.info(f"Exec agent: skip PR comment, no conclusion to post to {repository}/pulls/{pr_number}")
            return
    if agent_result:
        _conclusion = _apply_agent_result_policy(
            result_text, _structured_conclusion, _plain_text,
            task_id, seq, repo_key, pr_number, agent_type)
        if not _conclusion:
            return
    _comment = f"[{agent_type.upper()} Agent] {_conclusion}"
    _comment += "\n<!-- grape-auto-comment -->"
    try:
        import urllib.request, urllib.error, json as _json
        if _is_github_repo(repo_key):
            # GitHub: PR 共用 Issue 评论 API
            _url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
            _data = _json.dumps({"body": _comment}).encode("utf-8")
            _req = urllib.request.Request(_url, data=_data,
                headers={"Authorization": f"Bearer {bot_token}",
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "Grape-DevOps",
                         "Content-Type": "application/json"}, method="POST")
        else:
            # GitCode: PR 评论 API
            _url = f"https://gitcode.com/api/v5/repos/{repository}/pulls/{pr_number}/comments"
            _data = _json.dumps({"body": _comment}).encode("utf-8")
            _req = urllib.request.Request(_url, data=_data,
                headers={"Authorization": f"Bearer {bot_token}",
                         "Content-Type": "application/json"}, method="POST")
        _resp = urllib.request.urlopen(_req, timeout=15)
        # Issue #453 台账（审查 P1-1 补漏，写点9）：qa 审查结论回帖 PR → note_hook_pr 回流对账。
        # 原先响应丢弃不入账；GitCode PR 评论与 issue 评论 ID 同值域，按 issue_comment 键记。
        try:
            _body_resp = b""
            try:
                _body_resp = _resp.read()
            except Exception:
                pass
            _parsed_pr = {}
            try:
                _parsed_pr = _json.loads(_body_resp) if _body_resp else {}
            except Exception:
                _parsed_pr = {}
            _ledger_record_comment("github" if _is_github_repo(repo_key) else "gitcode",
                                   repo_key, repository, pr_number, _comment,
                                   _parsed_pr if isinstance(_parsed_pr, dict) else {})
        except Exception as _le:
            log.warning(f"outbound_ledger record (_post_result_to_pr) failed: {_le}")
        log.info(f"PR-sub: posted result comment to {repository}/pulls/{pr_number}")
    except urllib.error.HTTPError as e:
        # Issue #754：GitCode 已 merged PR 评论 405（not allowed to be reviewed after being
        # merged）→ 等价留痕通道回写兜底：PR=issue 语义，改走 /issues/{n}/comments。
        if (e.code == 405 and "/pulls/" in getattr(e, "url", "") and "gitcode.com" in getattr(e, "url", "")):
            try:
                _url754 = f"https://gitcode.com/api/v5/repos/{repository}/issues/{pr_number}/comments"
                _req754 = urllib.request.Request(_url754, data=_data,
                    headers={"Authorization": f"Bearer {bot_token}",
                             "Content-Type": "application/json"}, method="POST")
                _resp = urllib.request.urlopen(_req754, timeout=15)
                try:
                    _body_resp = b""
                    try:
                        _body_resp = _resp.read()
                    except Exception:
                        pass
                    _parsed_pr = {}
                    try:
                        _parsed_pr = _json.loads(_body_resp) if _body_resp else {}
                    except Exception:
                        _parsed_pr = {}
                    _ledger_record_comment("gitcode", repo_key, repository, pr_number, _comment,
                                           _parsed_pr if isinstance(_parsed_pr, dict) else {})
                except Exception as _le:
                    log.warning(f"outbound_ledger record (_post_result_to_pr 405-fallback) failed: {_le}")
                log.info(f"PR-sub: posted result comment via issue-channel fallback to "
                         f"{repository}/pulls/{pr_number} (Issue #754)")
                return
            except Exception as e2:
                log.warning(f"PR-sub: post result comment failed (405 fallback also failed): {e2}")
                return
        log.warning(f"PR-sub: post result comment failed: {e}")
    except Exception as e:
        log.warning(f"PR-sub: post result comment failed: {e}")


def _sync_issue_comments_to_conversation(repo_key: str, issue_number, bot_token: str,
                                         agent_type: str = "", repository: str = ""):
    """从 VCS Issue 拉取 agent 发的评论，去重后同步到任务会话。

    根据 repo_key provider 分流 GitHub/GitCode。
    VOD 等 agent 通过技能 curl 直接在 Issue 上发澄清追问评论，这些评论内容
    不会出现在 result_text（结构化输出摘要）中。此函数补拉 Issue 上的实际评论，
    与会话已有消息去重后写入，确保 Chat Agent「去对话」可见完整内容。

    agent_type 为空时同步所有 agent 前缀评论（用于打开会话时全量补拉）。
    """
    import re
    try:
        from src.repo_key import to_repo_key
        from src.session_manager import db as _db, db_lock as _dl, add_message as _am, get_messages as _get_msgs
    except Exception:
        return

    rk = to_repo_key(repo_key)

    # 1) 查关联的 task conversation
    try:
        with _dl:
            c = _db.cursor()
            c.execute("SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
                      (rk, issue_number))
            row = c.fetchone()
        if not row or not row[0]:
            return
        conv_id = row[0]
    except Exception as e:
        log.warning(f"Sync issue comments: find conversation failed: {e}")
        return

    # 2) 从 VCS API 拉取 Issue 评论
    comments = []
    try:
        if _is_github_repo(rk):
            # GitHub API
            from src.repo_key import split_owner_repo
            _owner, _repo = split_owner_repo(rk)
            comments = _github_list_issue_comments(_owner, _repo, str(issue_number), bot_token)
        else:
            # GitCode API 原逻辑
            import urllib.request
            import json as _json
            if not repository:
                from src.repo_key import from_repo_key
                repository = from_repo_key(rk)
            api_url = f"https://gitcode.com/api/v5/repos/{repository}/issues/{issue_number}/comments"
            req = urllib.request.Request(api_url)
            req.add_header("Authorization", f"Bearer {bot_token}")
            with urllib.request.urlopen(req, timeout=15) as resp:
                comments = _json.loads(resp.read())
    except Exception as e:
        log.warning(f"Sync issue comments: fetch from VCS failed: {e}")
        return

    if not comments:
        return

    # 3) 获取会话已有消息，提取内容指纹用于去重
    try:
        existing_msgs = _get_msgs(conv_id, limit=100)
    except Exception:
        existing_msgs = []
    existing_sigs = set()
    # 匹配 [XXX Agent] 标记（可能在引用块 > 之后出现，不要求在 body 开头）
    _agent_tag_re = re.compile(r'\[([A-Za-z_]+)\s+Agent\]', re.IGNORECASE)
    # 系统回帖标记——统一过滤，确保 existing_sigs 和新评论指纹生成方式一致
    _auto_comment_marker = "<!-- grape-auto-comment -->"
    for m in existing_msgs:
        content = m.get("content", "")
        # 统一过滤系统标记 + 去掉 [XXX Agent] 标记后取前 80 字符做指纹
        content = content.replace(_auto_comment_marker, "").rstrip()
        sig = _agent_tag_re.sub("", content).strip()[:80].lower()
        existing_sigs.add(sig)

    # 4) 筛选含 [XXX Agent] 标记的评论，去重后写入会话
    synced = 0
    for cm in comments:
        body = cm.get("body", "")
        if not body:
            continue
        # 过滤系统标记，避免在 Chat 页面显示
        body = body.replace(_auto_comment_marker, "").rstrip()
        # 评论中必须包含 [XXX Agent] 标记（可能在引用块之后）
        tag_match = _agent_tag_re.search(body)
        if not tag_match:
            continue
        # 如果指定了 agent_type，只同步该 agent 的评论
        if agent_type:
            if tag_match.group(1).upper() != agent_type.upper():
                continue
        # 去重：去掉 [XXX Agent] 标记后取前 80 字符做指纹（大小写不敏感）
        sig = _agent_tag_re.sub("", body).strip()[:80].lower()
        if sig in existing_sigs:
            continue
        try:
            _am(conv_id, "agent", body[:4000])
            synced += 1
        except Exception:
            pass

    if synced > 0:
        log.info(f"Sync issue comments: synced {synced} comments to conversation {conv_id} (issue #{issue_number})")


def _redispatch_vod_after_recommend(repo_key: str, issue_id: str, vod_skill: str,
                                     evt: dict, runtime_id: int):
    """智能推荐完成后，重派 VOD 任务（用匹配场景的 vod 技能）。

    R20.29.2: 移除 repo_gitcode_token 参数（冗余，pipeline 内部通过 _get_task_repo_token 重新获取）。
    R20.29.4: redispatch 时 token 短路返回空 → 新 task 行 repo_gitcode_token=空 → agent 运行时实时获取。
    Issue #565: bot 建单只做场景判定（scene_id 已写库），不进执行棒——evt 带 _bot_opened
    标记时短路，不重派 VOD 执行流（不派 issue/coding 棒）。
    2026-11-04 反转：默认放开（GRAPE_BOT_OPENED_FULL_PIPELINE=1），bot 建单推荐完成后
    照常重派 VOD 跑完整流水线；仅当开关显式置 0 时回退旧的 scene-only 短路。
    """
    from src.config import GRAPE_BOT_OPENED_FULL_PIPELINE as _bot_full_pipeline
    if evt.get("_bot_opened") and not _bot_full_pipeline:
        log.info(f"智能推荐完成: bot-opened issue {issue_id}, scene-only, skip redispatch (Issue #565, "
                 f"GRAPE_BOT_OPENED_FULL_PIPELINE=0)")
        return
    if evt.get("_bot_opened"):
        log.info(f"智能推荐完成: bot-opened issue {issue_id}, full pipeline enabled "
                 f"(GRAPE_BOT_OPENED_FULL_PIPELINE=1), redispatch VOD")
    from src.api.event_store import append_event
    from src.runtime.engine_select import pick_engine, engine_name
    from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT
    from src.api.event_pipeline import is_plugin_mode

    # 获取 Issue 标题
    _title = ""
    _issue_num = str(issue_id).lstrip("#")
    try:
        from src.api.issues_center import _get_issue_title
        _title = _get_issue_title(repo_key, int(_issue_num)) or ""
    except Exception:
        pass
    if not _title:
        _title = f"VOD redispatch: {vod_skill}"

    # 创建新 event
    _a2a_actor = _a2a_actor_for(evt, issue_id, repo_key)
    seq = append_event(
        repo_key=repo_key,
        event_type="a2a_event",
        title=_title,
        issue_id=issue_id,
        source="a2a",
        agent_type="vod",
        actor=_a2a_actor,
        summary=f"智能推荐完成 → VOD ({vod_skill})",
        payload={"repository": evt.get("repository", ""), "issue_number": _issue_num,
                 "account": _a2a_actor,
                 "context": "智能推荐完成，重派 VOD 执行完整工作流"},
        status="pending",
        entity_type="issue",
    )
    log.info(f"智能推荐重派: event created, seq={seq}")

    # 构建场景解析
    from src.api.session_store import get_issue_scene
    _scene_id, _scene_uid = get_issue_scene(repo_key, issue_id)
    _scene_id = _scene_id or "default"
    if not _scene_uid:
        from src.runtime.engine_select import _repo_user_id
        _scene_uid = _repo_user_id(repo_key, issue_creator=evt.get("actor", ""))

    # skill 解析 + 单一判官 scope 校验（Issue #657/#1758：显式 id 仍需校验）
    from src.user.db import resolve_skill_id, get_skill_by_id, get_skill_path
    skill_name = vod_skill
    _vod_sid = resolve_scene_node_skill_id(repo_key, "vod", scene_id=_scene_id,
                                           user_id=_scene_uid)
    _ev_vod = _evaluate_skill_scope(vod_skill, repo_key, skill_id=_vod_sid,
                                    node_tag="vod", scene_id=_scene_id)
    if not _ev_vod["ok"]:
        _fail_skill_resolve_event(
            repo_key, vod_skill, _scene_id, "vod",
            _ev_vod["reason"], issue_id=issue_id, seq=seq,
            scope_info=_ev_vod)
        return False
    skill_id = resolve_skill_id(vod_skill, repo_key=repo_key, user_id=_scene_uid) or 0
    skill_path = vod_skill
    if skill_id:
        _sk = get_skill_by_id(skill_id)
        skill_path = (_sk.get("skill_path") or vod_skill) if _sk else vod_skill

    # 构建 evt 副本（_enqueue_pipeline_task 内部会构建 prompt）
    evt_copy = dict(evt)
    evt_copy["repo_key"] = repo_key
    evt_copy["_skill_path"] = skill_path
    evt_copy["issue_id"] = issue_id

    # 选引擎
    engine_id = pick_engine(repo_key, "vod", fallback_runtime_id=runtime_id or 0,
                            scene_id=_scene_id, user_id=_scene_uid)
    if engine_id is None:
        log.warning(f"智能推荐重派: no engine available, skip")
        _fail_event(seq, "无可用引擎（engine_unavailable）")
        return

    _be = engine_name(engine_id) or "opencode"

    # 入队或直接执行（_enqueue_pipeline_task 内部会构建 prompt）
    kind = get_execution_mode()
    if kind != EXEC_MODE_DIRECT and (engine_id > 0 or (engine_id == 0 and is_plugin_mode())):
        _pool_eids = ""
        if is_plugin_mode():
            from src.runtime.engine_select import get_scene_pool_engine_ids
            _pool_eids = get_scene_pool_engine_ids(_scene_id, "vod", repo_key=repo_key, user_id=_scene_uid)
        # Issue #259：None = 场景引擎全灭（方案A，不入队）
        if _pool_eids is None:
            _fail_scene_all_offline(seq, _scene_id, "vod", "智能推荐重派", repo_key=repo_key)
            return False
        _enqueue_pipeline_task(repo_key, seq, "vod", _be, vod_skill, evt_copy,
                               skill_id, skill_path, engine_id, issue_id,
                               pool_engine_ids=_pool_eids, user_id=_scene_uid,
                               is_scene_recommend=False)
        log.info(f"智能推荐重派: enqueued VOD task, seq={seq}, skill={vod_skill}")
    else:
        # P0 (Issue #596): direct 路径改走 enqueue_task，由 local_claimer 认领执行
        _enqueue_pipeline_task(repo_key, seq, "vod", _be, vod_skill, evt_copy,
                               skill_id, skill_path, engine_id, issue_id)
        log.info(f"智能推荐重派: enqueued VOD task, seq={seq}, skill={vod_skill}")
