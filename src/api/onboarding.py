"""首次托管引导（Onboarding）触发与后台执行。

Issue #46 重构：移除「模拟 issue_opened 事件 + 预分配 Issue 号」整套机制，
改为按模板任务定义列表，直接在任务中心创建标准任务（真实 GitCode Issue → ensure_issue_task 建档），
与手动创建任务完全一致（issue-46-onboarding-task-notification-fix-design.md §四）。

- 任务集合由 `onboarding-tasks.json` 模板决定：仓库根目录自定义优先，无则用 configs/onboarding-templates.json 默认
- 创建层幂等：按 title（同仓库同 open title 不重复建 Issue）
- 完成层自愈：`onboarded:{repo_key}` 总闸 + `onboarded_task:{repo_key}:{type}` 按任务类型记录
- 触发点：templates.create_from_template（/repos/host 手动托管不触发）
"""
import os
import json

from src.config import log, CONFIG_DIR
from src.session_manager import db, db_lock


# 引导任务类型（模板配置可扩展，此处仅作默认标题派生/兜底说明，不写全量枚举）
_DEFAULT_TITLES = {
    "experience": "引导·快速部署体验",
    "dev": "引导·二次开发",
}


def _is_onboarded(repo_key: str) -> bool:
    from src.user.db import get_meta_value
    return get_meta_value(f"onboarded:{repo_key}") == "1"


def _mark_onboarding_started(repo_key: str):
    from src.user.db import set_meta_value
    set_meta_value(f"onboarded:{repo_key}", "1")


def _mark_task_done(repo_key: str, task_type: str):
    """按任务 type 记录完成态（稳定键，不随标题变更丢失）。"""
    from src.user.db import set_meta_value
    set_meta_value(f"onboarded_task:{repo_key}:{task_type}", "1")


def _task_done(repo_key: str, task_type: str) -> bool:
    from src.user.db import get_meta_value
    return get_meta_value(f"onboarded_task:{repo_key}:{task_type}") == "1"


def clear_onboarding_mark(repo_key: str):
    """清除 onboarding 幂等标记（带/不带 gitcode: 前缀两种 key，含 task 级标记）。

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
                _ob_keys = []
                for k in keys:
                    _ob_keys.append(f"onboarded:{k}")
                    # task 级完成标记：onboarded_task:{repo}:{type}
                    c.execute("SELECT `key` FROM meta WHERE `key` LIKE ?", (f"onboarded_task:{k}:%",))
                    _ob_keys += [r[0] for r in c.fetchall()]
                _ob_ph = ",".join("?" for _ in _ob_keys)
                if _ob_keys:
                    c.execute(f"DELETE FROM meta WHERE `key` IN ({_ob_ph})", _ob_keys)
                    total += c.rowcount
            except Exception:
                pass
            db.commit()
            if total:
                log.info(f"cleanup_stale_repo_data: cleaned {total} rows for {repo_key}")
    except Exception as e:
        log.warning(f"cleanup_stale_repo_data: failed for {repo_key}: {e}")


def _load_completed_tasks(repo_key: str, tasks: list) -> list:
    """返回 tasks 列表中已完成（不必再创建）的任务 type 集合。"""
    return [t.get("type", "") for t in tasks if t.get("type") and _task_done(repo_key, t["type"])]


def _load_onboarding_template(repo_key: str, scene_id: str = "", template_id: str = "") -> list:
    """读取 onboarding 任务模板，返回任务定义列表。

    解析规则（按优先级）：
    1. 仓库根目录 `onboarding-tasks.json`（若存在）优先；支持 `{"tasks": [...]}` 简写
       或 `{"default": {...}, "<scene_id>": {...}}` 结构（scene_id 键命中则优先，否则用 default）。
    2. 模板源目录 `examples/{template_id}/onboarding-tasks.json`（模板创建时远端仓库尚未克隆到本地，
       repo root 必然为空，此路经从服务器本地 examples/ 读取，确保模板专属 onboarding 即时生效）。
    3. 无仓库自定义 → `configs/onboarding-templates.json`，同样 scene_id 优先、default 兜底。
    4. 返回任务定义列表；空列表 / 缺文件 → 返回 []（调用方跳过，不报错）。

    任务项合法化：缺 title/description 时从 `_DEFAULT_TITLES` 派生兜底，忽略非法项。
    """
    def _extract(data: dict) -> list:
        if not isinstance(data, dict):
            return []
        if scene_id and isinstance(data.get(scene_id), dict):
            seg = data[scene_id]
        elif isinstance(data.get("default"), dict):
            seg = data["default"]
        elif isinstance(data.get("tasks"), list):
            seg = data
        else:
            seg = {}
        seg = seg or {}
        return seg.get("tasks", []) if isinstance(seg.get("tasks"), list) else []

    def _normalize(raw_tasks: list) -> list:
        out = []
        for t in raw_tasks:
            if not isinstance(t, dict):
                continue
            _type = str(t.get("type", "") or "").strip()
            _title = str(t.get("title", "") or "").strip()
            _desc = str(t.get("description", "") or "").strip()
            if not _type:
                continue
            if not _title:
                _title = _DEFAULT_TITLES.get(_type, "")
            if not _title:
                continue
            labels = t.get("labels") or []
            if not isinstance(labels, list):
                labels = []
            out.append({"type": _type, "title": _title, "description": _desc,
                        "labels": [str(x) for x in labels if str(x).strip()]})
        return out

    # 1. 仓库根目录自定义模板
    #    repository 物化根目录 = {WORKSPACE_DIR}/{provider}/{owner}/{repo}（与 repogit codebase mirror 同基）
    try:
        from src.repo_key import workspace_repo_dir
        _repo_root = workspace_repo_dir(repo_key)
        _p = os.path.join(_repo_root, "onboarding-tasks.json")
        if os.path.isfile(_p):
            try:
                with open(_p, encoding="utf-8") as f:
                    data = json.load(f)
                tasks = _normalize(_extract(data))
                if tasks:
                    log.info(f"[Onboarding|repo:{repo_key}] use repo custom template: {_p}")
                    return tasks
            except Exception as e:
                log.warning(f"[Onboarding|repo:{repo_key}] repo template parse failed ({_p}): {e}")
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] repo custom template check failed: {e}")

    # 2. 模板源目录 examples/{template_id}/onboarding-tasks.json
    #    模板创建时 push_template_to_repo 用临时目录推完即清理，本地 repo root 尚无文件；
    #    从服务器本地 examples/ 读取模板专属 onboarding（与 push 时用的同一份文件）。
    if template_id:
        try:
            from src.config import EXAMPLE_DIR
            _p = os.path.join(EXAMPLE_DIR, template_id, "onboarding-tasks.json")
            if os.path.isfile(_p):
                try:
                    with open(_p, encoding="utf-8") as f:
                        data = json.load(f)
                    tasks = _normalize(_extract(data))
                    if tasks:
                        log.info(f"[Onboarding|repo:{repo_key}] use template source: {_p}")
                        return tasks
                except Exception as e:
                    log.warning(f"[Onboarding|repo:{repo_key}] template source parse failed ({_p}): {e}")
        except Exception as e:
            log.warning(f"[Onboarding|repo:{repo_key}] template source check failed: {e}")

    # 3. 配置默认模板
    try:
        _path = os.path.join(CONFIG_DIR, "onboarding-templates.json")
        with open(_path, encoding="utf-8") as f:
            data = json.load(f)
        return _normalize(_extract(data))
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] default template load failed: {e}")
        return []


def _create_onboarding_tasks(repo_key: str, owner: str, template_id: str = ""):
    """按模板任务列表创建 onboarding 标准任务（真实 Issue 先创建 → ensure_issue_task 建档）。

    - 任务集合由模板决定（默认：体验部署 + 二次开发）
    - template_id 传入时优先从 examples/{template_id}/onboarding-tasks.json 读取模板专属任务
    - 创建层幂等：create_standard_task 内按 title 查重，不重复建 Issue
    - 完成层自愈：仅创建 `onboarded_task:{repo}:{type}` 未完成的任务；全部成功后置位总闸
    - 全部任务同步创建（同请求内完成），每个任务仅 1 条任务上下文消息
    """
    from src.api.routes.tasks import create_standard_task
    task_defs = _load_onboarding_template(repo_key, template_id=template_id)
    if not task_defs:
        log.info(f"[Onboarding|repo:{repo_key}] 无 onboarding 任务模板，跳过")
        return
    from src.repo import Repo
    bot_token = Repo(repo_key).bot_token
    if not bot_token:
        log.warning(f"[Onboarding|repo:{repo_key}] 无仓库 bot token，跳过 onboarding 任务创建")
        return
    pending = [t for t in task_defs if not _task_done(repo_key, t["type"])]
    if not pending:
        log.info(f"[Onboarding|repo:{repo_key}] 全部 onboarding 任务已完成，跳过")
        return
    _mark_onboarding_started(repo_key)
    ok_count = 0
    for t in pending:
        _type, _title, _desc, _labels = t["type"], t["title"], t["description"], t["labels"]
        try:
            r = create_standard_task(repo_key, _title, _desc, bot_token, assignee=owner, labels=_labels)
        except Exception as e:
            log.warning(f"[Onboarding|repo:{repo_key}] create task type={_type} failed: {e}")
            continue
        if r and r.get("ok"):
            _mark_task_done(repo_key, _type)
            ok_count += 1
            log.info(f"[Onboarding|repo:{repo_key}] 任务已创建 type={_type} issue=#{r.get('issue_number')} "
                     f"created={'yes' if r.get('created') else 'reuse'}")
        else:
            log.warning(f"[Onboarding|repo:{repo_key}] task type={_type} create failed: {r}")
    if ok_count == len(task_defs):
        log.info(f"[Onboarding|repo:{repo_key}] 全部 {ok_count} 个 onboarding 任务就绪")


def trigger_onboarding(repo_key: str, owner: str, template_id: str = ""):
    """模板一键创建完成后调用：触发引导（按模板任务列表创建，任务级幂等自愈）。

    仅在 templates.create_from_template 中调用（手动托管 /repos/host 不调用）。
    场景信息不再用于选技能（模拟事件机制已移除），任务集完全由模板决定。
    template_id 传入时优先从 examples/{template_id}/onboarding-tasks.json 读取模板专属任务。
    """
    try:
        _create_onboarding_tasks(repo_key, owner, template_id=template_id)
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] trigger failed: {e}")


def retry_onboarding_task(repo_key: str, owner: str, task_type: str) -> dict:
    """任务级引导自愈：按 type 补建未完成的 onboarding 任务。

    供重派（events redispatch）调用，避免依赖 `onboarded:*` 全量标记。
    仅当该 type 在模板中存在且尚未完成时创建。
    """
    task_defs = _load_onboarding_template(repo_key)
    target = next((t for t in task_defs if t.get("type") == task_type), None)
    if not target:
        return {"ok": False, "reason": f"task type not in template: {task_type}"}
    if _task_done(repo_key, task_type):
        return {"ok": True, "reason": "task already complete, no retry needed"}
    from src.api.routes.tasks import create_standard_task
    from src.repo import Repo
    bot_token = Repo(repo_key).bot_token
    if not bot_token:
        return {"ok": False, "reason": "no repo bot token"}
    try:
        r = create_standard_task(repo_key, target["title"], target["description"],
                                 bot_token, assignee=owner, labels=target["labels"])
    except Exception as e:
        log.warning(f"[Onboarding|repo:{repo_key}] retry type={task_type} failed: {e}")
        return {"ok": False, "reason": str(e)}
    if r and r.get("ok"):
        _mark_task_done(repo_key, task_type)
        return {"ok": True, "issue_number": r.get("issue_number"), "reason": ""}
    return {"ok": False, "reason": str(r)}