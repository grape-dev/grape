"""首次托管引导（Onboarding）触发与后台执行。

仅在「模板一键创建并托管」的项目上异步触发一次：
- 读取仓库开发场景（managed_repos.dev_scene）→ 按场景查 agent-skill-mapping.json 的 onboarding 技能
- 模拟 GitCode `issue_opened` webhook 等价体 → 提交 run_pipeline 完全走普通事件链路（§11 v1.5）
- experience / explore 各执行一次，各创建一个对应类型的引导 Issue
- 广播（plugin）模式靠 pool_tag 认领，不依赖仓库 runtime_id 绑定
- issue_state：新 Issue 处于 vod 阶段（stage=vod, owner=vod），由 VOD 先收敛需求（阶段矩阵 D8）
- 幂等：meta 表 `onboarded:{repo_key}` 标记 + target 级完成自愈
"""
import os
import json
import threading

from src.config import log, CONFIG_DIR
from src.session_manager import db, db_lock

# 引导目标类型：experience（快速部署体验）/ explore（探索方向引导），各执行一次
ONBOARDING_TARGETS = ("experience", "explore")

# target → 主事件类型 映射
ONBOARDING_EVENT_TYPE = {"experience": "onboarding_exp", "explore": "onboarding_explore"}


def _target_main_event_done(repo_key: str, target: str) -> bool:
    """该 target 主事件是否已成功完成（status=completed 且已产出对应引导 Issue）。

    用于 target 级幂等：不因 `onboarded:*` 全量标记一次性放弃两个 target
    （§5.3 引导状态自愈，见 agent-tasks-direct-create-binding-fix-design.md）。
    """
    if target not in ONBOARDING_EVENT_TYPE:
        return False
    _expected = ONBOARDING_TARGETS.index(target) + 1
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq FROM events WHERE repo_key=? AND event_type=? AND status='completed' "
                "ORDER BY seq DESC LIMIT 1",
                (repo_key, ONBOARDING_EVENT_TYPE[target]),
            )
            if not c.fetchone():
                return False
            c.execute(
                "SELECT 1 FROM events WHERE repo_key=? AND event_type='onboarding_issue' AND issue_id=? LIMIT 1",
                (repo_key, f"#{_expected}"),
            )
            return c.fetchone() is not None
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] target done check failed: {e}")
        return False


def _scene_onboarding_skill(scene_id: str) -> str:
    """读 agent-skill-mapping.json，返回场景的 onboarding 技能 key；无则空串。

    plugin 模式下 dev_scene 可能为 `repo:{scene}`（scope 前缀），此处剥离后查配置。
    """
    _lookup = (scene_id or "").strip()
    if _lookup.startswith("repo:") or _lookup.startswith("user:"):
        _lookup = _lookup.split(":", 1)[1]
    try:
        _path = os.path.join(CONFIG_DIR, "agent-skill-mapping.json")
        with open(_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("scenes", {}).get(_lookup, {}).get("onboarding", "") or ""
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


def _submit_onboarding_webhook(repo_key: str, owner: str, scene_id: str, skill: str,
                               skill_path: str, skill_id: int, target: str):
    """单 target 引导：构造 GitCode `issue_opened` webhook 等价体 → 提交 run_pipeline（§11 v1.5）。

    完全走普通事件链路：
    - route: issue_opened → vod（原生路由，无需注册 onboarding_*）
    - enable 检查 / 会话查找 / 引擎门禁 / 任务登记（_create_direct_task|enqueue）/ sweeper 保护全部复用
    - 广播（plugin）模式：engine_id=0 → pool_tag 认领，**不依赖仓库 runtime_id 绑定**
    - payload._onboarding 上下文（target/skill/scene/result_path）供 run_pipeline 覆盖技能并注入 ONBOARDING_* 环境变量
    """
    import json as _json
    from src.repo_key import from_repo_key
    from src.api.event_pipeline import run_pipeline

    repository = from_repo_key(repo_key)
    _expected_issue = ONBOARDING_TARGETS.index(target) + 1  # experience→#1, explore→#2
    _title = "引导·快速部署体验" if target == "experience" else "引导·探索方向"
    _result_path = os.path.join(_onboarding_workdir(repo_key, target), f"onboarding-result-{target}.json")

    # 构造 GitCode issue_opened 等价 webhook 体（object_attributes.open → normalize → issue_opened → vod）
    body = _json.dumps({
        "object_kind": "issue",
        "project": {"path_with_namespace": repository, "name": repository.split("/")[-1]},
        "repository": {"full_name": repository, "name": repository.split("/")[-1]},
        "object_attributes": {
            "action": "open",
            "iid": _expected_issue, "number": _expected_issue,
            "title": _title, "state": "opened", "description": f"首次托管引导·{target}",
            "labels": [], "assignee_id": None, "updated_at": _json.dumps(None),
        },
        "user": {"username": owner or ""},
        "user_name": owner or "",
        "_onboarding": {
            "target": target, "skill": skill, "skill_id": skill_id,
            "scene": scene_id, "result_path": _result_path,
        },
    }).encode("utf-8")
    headers = {
        "X-GitCode-Event": "Issue Hook",
        "X-GitCode-Delivery": "onboarding-sim",
        "User-Agent": "grape-onboarding-sim",
        "Content-Type": "application/json",
    }
    # 与 webhook.py:115 同路径：account=仓库真名、repo_key_override=带前缀 repo_key
    _rp = run_pipeline(body, headers, account=repository, repo_key_override=repo_key)
    _seq = (_rp or {}).get("seq", 0) or 0
    log.info(f"[Onboarding|repo:{repo_key}] simulated issue webhook submitted, target={target}, seq={_seq}")
    # 终态兜底补库：等待事件终态后 _finalize_onboarding 补 issue_state + 子事件
    if _seq > 0:
        threading.Thread(target=_await_event_and_finalize, args=(
            repo_key, _seq, owner, target, _result_path,
        ), daemon=True, name=f"onboarding-fin-{target}-{_seq}").start()
    return _seq


def _await_event_and_finalize(repo_key: str, seq: int, owner: str, target: str, result_path: str):
    """轮询主事件终态（completed/failed）→ 触发 _finalize_onboarding 兜底补库。

    事件由 run_pipeline 正常执行并回报终态（direct: _execute_agent 更新；queue: worker 上报），
    此处仅在完成后做引导专属补库（issue_state + onboarding_issue 子事件），不做状态机判定。
    """
    import time as _t
    for _ in range(600):  # 最长 60min
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT status FROM events WHERE seq=?", (seq,))
                row = c.fetchone()
            if not row:
                log.warning(f"[Onboarding|repo:{repo_key}] event {seq} disappeared, skip finalize")
                return
        except Exception as e:
            log.warning(f"[Onboarding|repo:{repo_key}] event poll failed: {e}")
            return
        if row[0] in ("completed",):
            _finalize_onboarding(repo_key, seq, owner, result_path)
            return
        if row[0] in ("failed", "cancelled", "terminated"):
            log.warning(f"[Onboarding|repo:{repo_key}] target={target} event {seq} -> {row[0]}, no finalize")
            return
        _t.sleep(6)
    log.warning(f"[Onboarding|repo:{repo_key}] target={target} event {seq} poll timeout")
    # 轮询结束后事件仍在 running：不擅动（sweeper 会兜底），仅告警


def trigger_onboarding(repo_key: str, owner: str):
    """模板一键创建完成后调用：触发引导（target 级幂等，支持单 target 自愈）。

    仅在 templates.create_from_template 中调用（手动托管 /repos/host 不调用）。
    场景从 managed_repos.dev_scene 读取（不依赖请求参数）。

    §11 v1.5：构造 `issue_opened` webhook 等价体 → `run_pipeline` 完全走普通事件链路。
    广播（plugin）模式靠 pool_tag 认领，**无需仓库绑定 runtime_id**；引擎门禁/任务登记/
    sweeper/重派全部复用普通事件，不隐式 opencode。

    引导状态自愈（§5.3）：不因 `onboarded:*` 全量标记一次性放弃两个 target——
    已 failed 且未产出对应 Issue 的 target 会在此重试（补齐），已完成的不重复。
    """
    try:
        from src.user.db import get_repo_scene
        scene_id = get_repo_scene(repo_key) or "default"
        skill_path, skill_id = "", 0
        skill = _scene_onboarding_skill(scene_id)
        if not skill:
            log.info(f"[Onboarding|repo:{repo_key}] scene='{scene_id}' 无 onboarding 技能，跳过")
            return
        # skill_id/skill_path 解析（与普通事件取技能一致）：已发布技能查库，未发布用名
        try:
            from src.user.db import resolve_skill_id, get_skill_by_id
            _sid = resolve_skill_id(skill)
            if _sid:
                skill_id = _sid
                _sk = get_skill_by_id(_sid)
                skill_path = (_sk.get("skill_path") or skill) if _sk else skill
        except Exception as e:
            log.warning(f"[Onboarding|repo:{repo_key}] skill resolve failed: {e}")
        if not skill_path:
            try:
                from src.config import resolve_official_skill_path as _resolve_skill
                skill_path = _resolve_skill(skill) or skill
            except Exception:
                skill_path = skill
        if _is_onboarded(repo_key) and all(_target_main_event_done(repo_key, t) for t in ONBOARDING_TARGETS):
            log.info(f"[Onboarding|repo:{repo_key}] 已引导且双 target 均完成，跳过")
            return
        _mark_onboarding_started(repo_key)  # 防并发重复

        # 逐 target：构造 webhook → run_pipeline（已完成的不重复提交）
        for target in ONBOARDING_TARGETS:
            if _target_main_event_done(repo_key, target):
                log.info(f"[Onboarding|repo:{repo_key}] target={target} 已完成，跳过")
                continue
            _submit_onboarding_webhook(repo_key, owner, scene_id, skill,
                                       skill_path, skill_id, target)
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] trigger failed: {e}")


def retry_onboarding_target(repo_key: str, owner: str, target: str) -> dict:
    """target 级引导自愈：主事件 failed 且未产出对应 Issue → 重新触发该 target。

    供重派（events redispatch）调用，避免依赖 `onboarded:*` 全量标记。
    §11 v1.5：复用 _submit_onboarding_webhook（模拟 issue_opened → run_pipeline），广播模式不需绑定引擎。
    返回 {"ok": bool, "seq": int, "reason": str}。
    """
    if target not in ONBOARDING_TARGETS:
        return {"ok": False, "reason": f"unknown target: {target}"}
    if _target_main_event_done(repo_key, target):
        return {"ok": True, "seq": 0, "reason": "target already complete, no retry needed"}
    if _is_onboarded(repo_key):
        # 全量标记已置位且该 target 未完成 → 允许补做（否则历史 failed 的 target 永远救不回）
        log.info(f"[Onboarding|repo:{repo_key}] onboarded=1 but target={target} incomplete, retry")
    try:
        from src.user.db import get_repo_scene
        scene_id = get_repo_scene(repo_key) or "default"
        skill = _scene_onboarding_skill(scene_id)
        if not skill:
            return {"ok": False, "reason": f"scene='{scene_id}' 无 onboarding 技能"}
        skill_path = skill
        skill_id = 0
        try:
            from src.user.db import resolve_skill_id, get_skill_by_id
            _sid = resolve_skill_id(skill)
            if _sid:
                skill_id = _sid
                _sk = get_skill_by_id(_sid)
                skill_path = (_sk.get("skill_path") or skill) if _sk else skill
        except Exception:
            pass
        _mark_onboarding_started(repo_key)
        seq = _submit_onboarding_webhook(repo_key, owner, scene_id, skill,
                                         skill_path, skill_id, target)
        return {"ok": True, "seq": seq, "reason": ""}
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] retry target={target} failed: {e}")
        return {"ok": False, "reason": str(e)}
