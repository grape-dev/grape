"""组织上下文/知识库 → 组织仓库 `org-context/` 同步（按设计 D5 / §11）。

设计口径（docs/org-pipeline/org-level-pipeline-design.md）：
- 权威载体：组织仓库 `org-context/`（context.md / standards/*.md /
  knowledge/*.md / repos.json）；
- DB 里的 org_context / agent_shared_knowledge 作为索引与 UI 快照来源。

本模块把「当前生效（active）」的上下文、知识与关联仓库渲染成文件，提交并推送到
组织仓库默认分支。全部 best-effort：失败只告警，不影响组织主流程。

并发：同一组织 1.5s 内的多次变更合并为一次同步（Timer 去抖 + 线程锁）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time

from src.kernel.config import log
from src.kernel.issue_ident import native_issue_number
from src.domains.runtime import public as runtime_public

_SYNC_DEBOUNCE_SECONDS = 1.5
# 镜像硬上限：即使保留策略/手工数据异常，也不能把组织仓库写成无底洞。
_RENDER_MAX_FILES = int(os.environ.get("ORG_CONTEXT_REPO_MAX_FILES", "400") or 400)  # rationale: docs/org-pipeline/org-repo-map-design.md:809
_RENDER_MAX_BYTES = int(os.environ.get("ORG_CONTEXT_REPO_MAX_BYTES", str(2 * 1024 * 1024)) or (2 * 1024 * 1024))
_SYNC_TIMERS: dict[str, threading.Timer] = {}
_SYNC_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()

# 新建组织自动仓库的默认分支：Grape 托管分支统一 grape- 前缀。
# 存量组织仓库保持各自默认分支（不强制改名），读取路径必须按实际默认分支解析。
ORG_REPO_DEFAULT_BRANCH = (os.environ.get("ORG_REPO_DEFAULT_BRANCH") or "grape-repo-knowledge").strip()
# AI 分析产物分支：orphan 分支，只放仓库地图/分析结果，与默认分支隔离。
ORG_REPO_ANALYSIS_BRANCH = (os.environ.get("ORG_REPO_ANALYSIS_BRANCH") or "grape-repo-analysis").strip()


def _org_lock(org_id: str) -> threading.Lock:
    with _GUARD:
        lock = _SYNC_LOCKS.get(org_id)
        if lock is None:
            lock = threading.Lock()
            _SYNC_LOCKS[org_id] = lock
        return lock


def schedule_sync(org_id: str, delay: float = _SYNC_DEBOUNCE_SECONDS) -> None:
    """安排一次组织仓库上下文同步（去抖；失败静默告警）。"""
    org_id = str(org_id or "").strip()
    if not org_id:
        return
    with _GUARD:
        old = _SYNC_TIMERS.pop(org_id, None)
        if old is not None:
            try:
                old.cancel()
            except Exception as exc:  # noqa: BLE001
                log.debug(f"org-analysis timer cancel skipped: {exc}")
        timer = threading.Timer(delay, _fire, args=(org_id,))
        timer.daemon = True
        _SYNC_TIMERS[org_id] = timer
        timer.start()


def _fire(org_id: str) -> None:
    with _GUARD:
        _SYNC_TIMERS.pop(org_id, None)
    try:
        sync_org_context_to_repo(org_id)
    except Exception as e:  # noqa: BLE001 - best-effort
        log.warning(f"org-context repo sync failed org={org_id}: {e}")


def _safe_name(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "-", str(name or "")).strip("-.")
    return (text or "entry")[:80]


# 兼容旧版：清理 decisions/context.md/repos.json，同时清理新版分层目录。
_MANAGED_DIRS = ("decisions", "knowledge", "standards", "repos", "demands", "repo-map")
_MANAGED_ROOT_FILES = (
    "context.md", "repos.json", "README.md", "TRUNCATED.md",
    "rules.md", "profile.md", "architecture.md", "index.json",
)


def _clean_managed_org_context(work: str) -> None:
    """删除上一轮生成、但本轮已不在热集合中的文件（保留用户自定义文件）。"""
    base = os.path.join(work, "org-context")
    for sub in _MANAGED_DIRS:
        directory = os.path.join(base, sub)
        if not os.path.isdir(directory):
            continue
        # 生成目录整棵删除：demands/<id>/、repos/<provider>/<owner>/<repo>/ 都是
        # 多层结构，只删一层文件会留下旧目录，镜像永远清不干净。
        try:
            shutil.rmtree(directory)
        except OSError as e:
            log.warning(f"org-context sync: remove stale dir {directory} failed: {e}")
    for name in _MANAGED_ROOT_FILES:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as e:
                log.warning(f"org-context sync: remove stale {path} failed: {e}")


def _meta_line(scope: str, **fields) -> str:
    parts = [f"作用域：{scope}"]
    for key, value in fields.items():
        text = str(value or "").strip()
        if text:
            parts.append(f"{key}：{text}")
    return "> " + " | ".join(parts)


def _clip_bytes(text: str, limit: int) -> str:
    data = str(text or "")
    raw = data.encode("utf-8")
    if len(raw) <= limit:
        return data
    return raw[:limit].decode("utf-8", "ignore") + "\n\n> （超预算，已截断）\n"


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _org_demand_rows(org_id: str) -> list[dict]:
    from src.domains.mgmt.user.org_db import get_org
    from src.domains.runtime import public as runtime_public

    org = get_org(org_id) or {}
    repo_key = str(org.get("org_repo_key") or "")
    if not repo_key:
        return []
    return runtime_public.recent_issues(repo_key, limit=100)


def _org_dispatch_rows(org_id: str) -> list[dict]:
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, org_issue_number, status, scene_id, plan_json, "
            "context_json, created_at, updated_at FROM grape_t_org_dispatch "
            "WHERE org_id=? ORDER BY created_at DESC, id DESC LIMIT 100",
            (org_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            plan = json.loads(r[4] or "{}")
        except Exception:
            plan = {}
        try:
            context = json.loads(r[5] or "{}")
        except Exception:
            context = {}
        out.append({
            "id": str(r[0] or ""), "org_issue_number": native_issue_number(r[1]),
            "status": r[2] or "", "scene_id": r[3] or "",
            "plan": plan if isinstance(plan, dict) else {},
            "context": context if isinstance(context, dict) else {},
            "created_at": r[6] or "", "updated_at": r[7] or "",
        })
    return out


def _org_aggregate_map(org_id: str) -> dict[str, dict]:
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT a.dispatch_id, a.terminal_status, a.status, a.comment_posted, "
            "a.issue_status, a.error FROM grape_t_org_dispatch_aggregate a "
            "JOIN grape_t_org_dispatch d ON d.id=a.dispatch_id WHERE d.org_id=? "
            "ORDER BY a.created_at DESC, a.id DESC",
            (org_id,),
        ).fetchall()
    out = {}
    for r in rows:
        out[str(r[0] or "")] = {
            "terminal_status": r[1] or "", "status": r[2] or "",
            "comment_posted": int(r[3] or 0), "issue_status": r[4] or "",
            "error": r[5] or "",
        }
    return out


def _org_decision_map(org_id: str) -> dict[str, dict]:
    from src.domains.mgmt.org_context_store import list_contexts

    out = {}
    for row in list_contexts(org_id, kind="decision", limit=300):
        key = str(row.get("ctx_key") or "")
        if key:
            out[key] = row
    return out


def _project_info_summary(repo_key: str) -> tuple[str, str]:
    from src.kernel.repo_key import workspace_repo_dir

    base = os.path.join(workspace_repo_dir(repo_key), "project-info")
    cap = _read_text(os.path.join(base, "project-capabilities.md")).strip()
    graph = _read_text(os.path.join(base, "project-graph.md")).strip()
    markers = ("no files indexed", "run codegraph index", "no relevant code found")
    def _meaningful(text: str) -> bool:
        low = text.lower()
        return bool(text) and not any(m in low for m in markers)
    if not _meaningful(cap):
        cap = "（project-capabilities 已生成，但 codegraph 尚未索引；完整内容在目标仓库）"
    if not _meaningful(graph):
        graph = "（project-graph 已生成，但 codegraph 尚未索引；完整内容在目标仓库）"
    return _clip_bytes(cap, 6000), _clip_bytes(graph, 6000)


def _repo_context_md(org: dict, repo: dict) -> str:
    repo_key = str(repo.get("repo_key") or "")
    provider, owner, name = "", "", ""
    try:
        from src.kernel.repo_key import parse_repo_key
        provider, owner, name = parse_repo_key(repo_key)
    except Exception:
        pass
    caps, graph = _project_info_summary(repo_key)
    lines = [
        f"# 仓库上下文：{repo.get('full_name') or repo_key}",
        "",
        _meta_line("仓库", 适用范围=repo_key, 状态="active",
                  负责人=repo.get("linked_by") or "",
                  版本="1", 更新时间=repo.get("linked_at") or "",
                  来源="managed_repos + project-info"),
        "",
        f"- 仓库：`{repo_key}`",
        f"- Owner：`{repo.get('repo_owner') or owner}`",
        f"- 组织角色：{'组织自动仓库' if repo.get('is_org_repo') else '关联仓库'}",
        f"- 默认场景：`{repo.get('dev_scene') or ''}`",
        f"- 关联时间：{repo.get('linked_at') or ''}",
        f"- 关联人：`{repo.get('linked_by') or ''}`",
        "",
        "## 项目能力摘要",
        caps or "（尚未生成 project-capabilities.md）",
        "",
        "## 架构概览摘要",
        graph or "（尚未生成 project-graph.md）",
        "",
        "> L2 仓库上下文的完整正文仍在目标仓库自身 `project-info/` / `.knowledge/`；"
        "本文件仅作为组织仓库中的目录条目与摘要。",
    ]
    return "\n".join(lines)


def _demand_brief_md(org: dict, demand: dict, dispatches: list[dict]) -> str:
    lines = [
        f"# 组织需求 #{demand['issue_number']}",
        "",
        _meta_line("需求", 适用范围=f"org_issue #{demand['issue_number']}",
                  状态=demand.get("status") or "",
                  负责人=demand.get("created_by") or "",
                  版本="1", 更新时间=demand.get("created_at") or "",
                  来源="organization issue"),
        "",
        f"- 标题：{demand.get('title') or ''}",
        f"- 状态：{demand.get('status') or '-'} / 阶段：{demand.get('stage') or '-'}",
        f"- 创建时间：{demand.get('created_at') or ''}",
        "",
        "## 需求正文",
        demand.get("description") or "（无正文）",
    ]
    if dispatches:
        latest = dispatches[0]
        ctx = latest.get("context") or {}
        lines += ["", "## 下派目标"]
        targets = (latest.get("plan") or {}).get("targets") or []
        lines += [f"- `{x}`" for x in targets] or ["- （未记录）"]
        if ctx.get("summary"):
            lines += ["", "## 目标摘要", str(ctx.get("summary"))]
        if ctx.get("acceptance"):
            lines += ["", "## 验收标准"]
            lines += [f"- {x}" for x in ctx.get("acceptance") or [] if str(x).strip()]
        if ctx.get("constraints"):
            lines += ["", "## 约束"]
            lines += [f"- {x}" for x in ctx.get("constraints") or [] if str(x).strip()]
        if ctx.get("decisions"):
            lines += ["", "## 设计决策"]
            lines += [f"- {x}" for x in ctx.get("decisions") or [] if str(x).strip()]
    return "\n".join(lines)


def _demand_dispatch_plan_md(demand: dict, dispatches: list[dict]) -> str:
    lines = [
        f"# 下派计划：组织需求 #{demand['issue_number']}", "",
        _meta_line("需求", 适用范围=f"org_issue #{demand['issue_number']}",
                  状态=", ".join(d.get("status") or "" for d in dispatches) or "",
                  版本="1",
                  更新时间=dispatches[0].get("updated_at") if dispatches else "",
                  来源="org_dispatch"),
        "",
    ]
    if not dispatches:
        return "\n".join(lines + ["（暂无下派批次）"])
    for d in dispatches:
        plan = d.get("plan") or {}
        ctx = d.get("context") or {}
        lines += [f"## 批次 #{d['id']}（{d.get('status') or '-'}）", ""]
        lines.append(f"- 场景：`{d.get('scene_id') or ''}`")
        lines.append(f"- 创建：{d.get('created_at') or ''} / 更新：{d.get('updated_at') or ''}")
        targets = plan.get("targets") or []
        lines.append("- 目标仓库：" + ("、".join(f"`{x}`" for x in targets) or "-"))
        if ctx.get("summary"):
            lines.append(f"- 目标摘要：{ctx.get('summary')}")
        if ctx.get("acceptance"):
            lines.append("- 验收：" + "；".join(str(x) for x in ctx["acceptance"]))
        if ctx.get("constraints"):
            lines.append("- 约束：" + "；".join(str(x) for x in ctx["constraints"]))
        if plan.get("description"):
            lines += ["", "### 工作说明", str(plan.get("description"))]
        lines.append("")
    return "\n".join(lines)


def _demand_acceptance_md(demand: dict, dispatches: list[dict],
                          aggregates: dict[int, dict],
                          decisions: dict[str, dict]) -> str:
    lines = [
        f"# 组织验收：需求 #{demand['issue_number']}", "",
        _meta_line("需求", 适用范围=f"org_issue #{demand['issue_number']}",
                  状态=demand.get("status") or "",
                  版本="1",
                  更新时间=time.strftime("%Y-%m-%d %H:%M:%S"),
                  来源="org_dispatch_aggregate"),
        "",
    ]
    if not dispatches:
        return "\n".join(lines + ["（暂无下派批次）"])
    for d in dispatches:
        agg = aggregates.get(d["id"]) or {}
        lines += [f"## 批次 #{d['id']}", ""]
        lines.append(f"- 批次状态：{d.get('status') or '-'}")
        lines.append(f"- 聚合：{agg.get('status') or '-'} / 组织 Issue：{agg.get('issue_status') or '-'}")
        lines.append(f"- 聚合评论：{'已写入' if agg.get('comment_posted') else '未写入'}")
        if agg.get("error"):
            lines.append(f"- 错误：{agg['error']}")
        decision = decisions.get(f"dispatch-{d['id']}") or {}
        content = str(decision.get("content") or "")
        if content:
            lines += ["", "### 批次结论", _clip_bytes(content.strip(), 12000)]
        lines.append("")
    return "\n".join(lines)


def _render_files(org_id: str) -> dict[str, str]:
    from src.domains.mgmt.org_context_store import list_contexts
    from src.domains.mgmt.org_knowledge import list_knowledge, read_knowledge_content
    from src.domains.mgmt.user.org_db import get_org, list_org_repos

    org = get_org(org_id) or {}
    if not org:
        return {}
    files: dict[str, str] = {}
    total_bytes = 0
    truncated = False

    def _put(rel: str, content: str) -> bool:
        nonlocal total_bytes, truncated
        data = content if content.endswith("\n") else content + "\n"
        size = len(data.encode("utf-8"))
        if len(files) >= _RENDER_MAX_FILES or total_bytes + size > _RENDER_MAX_BYTES:
            truncated = True
            return False
        files[rel] = data
        total_bytes += size
        return True

    # L0：组织硬规则。规则类 policy 优先，没有则给空骨架。
    policies = list_contexts(org_id, kind="policy", status="active", limit=200)
    rule_rows = [c for c in policies
                 if str(c.get("ctx_key") or "").lower() in ("rules", "hard-rules", "org-rules")]
    rule_lines = [
        "# 组织硬规则", "",
        _meta_line("组织", 适用范围="全组织", 状态="active",
                  负责人=org.get("owner_id") or "",
                  版本="1", 更新时间=time.strftime("%Y-%m-%d %H:%M:%S"),
                  来源="org_context/policy"),
        "",
        "> 优先级最高，不可被仓库规则或任务要求覆盖。",
    ]
    if rule_rows:
        for row in rule_rows:
            rule_lines += ["", f"## {row.get('title') or row.get('ctx_key')}",
                           str(row.get("content") or "")]
    else:
        rule_lines += ["", "（暂无显式组织硬规则；安全、合规、跨仓接口约束应优先补在此文件。）"]
    _put("org-context/rules.md", "\n".join(rule_lines))

    # L1：profile / architecture / standards
    repos = list_org_repos(org_id) or []
    profile = [
        "# 组织简介", "",
        _meta_line("组织", 适用范围="全组织", 状态="active",
                  负责人=org.get("owner_id") or "",
                  版本="1", 更新时间=time.strftime("%Y-%m-%d %H:%M:%S"),
                  来源="organization"),
        "",
        f"- 名称：{org.get('name') or ''}",
        f"- Slug：`{org.get('slug') or ''}`",
        f"- 描述：{org.get('description') or '（未填写）'}",
        f"- 组织仓库：`{org.get('org_repo_key') or ''}`",
        f"- 组织场景：`{org.get('org_scene_id') or ''}`",
        f"- Owner：`{org.get('owner_id') or ''}`",
        f"- 创建时间：{org.get('created_at') or ''}",
        "", f"## 仓库目录（{len(repos)}）",
    ]
    for r in repos:
        profile.append(
            f"- `{r.get('repo_key') or ''}`"
            + ("（组织自动仓库）" if r.get("is_org_repo") else ""))
    _put("org-context/profile.md", "\n".join(profile))

    briefs = list_contexts(org_id, kind="brief", status="active", limit=5)
    arch = (briefs[0].get("content") if briefs else "") or (
        f"# 组织架构与原则\n\n组织：{org.get('name') or ''}\n\n"
        "（暂无组织级 architecture 文档；可在组织上下文中创建 brief。）")
    _arch_text = str(arch).strip()
    _arch_meta = _meta_line("组织", 适用范围="全组织", 状态="active",
                           负责人=org.get("owner_id") or "",
                           版本="1", 更新时间=time.strftime("%Y-%m-%d %H:%M:%S"),
                           来源="org_context/brief")
    _put("org-context/architecture.md", _arch_meta + "\n\n" + _arch_text)

    for c in policies:
        key = str(c.get("ctx_key") or "").lower()
        if key in ("rules", "hard-rules", "org-rules"):
            continue
        name = _safe_name(c.get("ctx_key") or "policy")
        _std_meta = _meta_line("组织", 适用范围="全组织",
                               状态=c.get("lifecycle_status") or "active",
                               负责人=c.get("owner_id") or "",
                               版本=c.get("version") or 1,
                               更新时间=c.get("updated_at") or "",
                               来源=f"org_context/policy/{name}")
        _put(f"org-context/standards/{name}.md",
             f"# {c.get('title') or name}\n\n{_std_meta}\n\n{c.get('content') or ''}")

    # L2：每个关联仓库的目录条目（摘要，不复制完整 project-info/知识）
    repo_index = []
    for r in repos:
        repo_key = str(r.get("repo_key") or "")
        provider, owner, name = "", "", ""
        try:
            from src.kernel.repo_key import parse_repo_key
            provider, owner, name = parse_repo_key(repo_key)
        except Exception:
            pass
        rel = f"org-context/repos/{provider}/{owner}/{name}/context.md"
        if _put(rel, _repo_context_md(org, r)) and repo_key:
            repo_index.append({
                "repo_key": repo_key, "path": rel,
                "is_org_repo": bool(r.get("is_org_repo")),
                "default_scene": r.get("dev_scene") or "",
            })
    _put("org-context/repos/index.json", json.dumps(
        {"org_id": org_id, "repos": repo_index}, ensure_ascii=False, indent=2))

    # 仓库地图（MD-first）：默认只写独立分析分支 grape-repo-analysis，
    # 不再混入组织仓库默认分支（与代码/上下文隔离）。需要兼容旧布局时设
    # ORG_CONTEXT_SYNC_REPO_MAP=1。
    if os.environ.get("ORG_CONTEXT_SYNC_REPO_MAP", "0").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from src.domains.mgmt.org_repo_map import repo_map_markdown_files

            for rel, content in (repo_map_markdown_files(org_id) or {}).items():
                _put(rel, content)
        except Exception as e:
            log.warning(f"org-context sync: render repo-map failed org={org_id}: {e}")

    # 组织知识（active）
    for k in list_knowledge(org_id, status="active", limit=200):
        try:
            content = read_knowledge_content(k) or ""
        except Exception:
            content = ""
        fname = f"{int(k.get('id') or 0)}-{_safe_name(k.get('name'))}.md"
        _k_meta = _meta_line(
            "组织", 适用范围="全组织",
            状态=k.get("lifecycle_status") or "active",
            负责人=k.get("owner_id") or "",
            版本=k.get("version") or 1,
            更新时间=k.get("last_reviewed_at") or k.get("updated_at") or "",
            审核周期=(f"{k.get('review_interval_days')}天"
                      if k.get("review_interval_days") else ""),
            来源=k.get("source") or "manual",
        )
        _put(f"org-context/knowledge/{fname}", _k_meta + "\n\n" + content.strip())

    # L3：每个组织需求一个目录
    demands = _org_demand_rows(org_id)
    dispatches = _org_dispatch_rows(org_id)
    aggregates = _org_aggregate_map(org_id)
    decisions = _org_decision_map(org_id)
    active_decisions = {
        key for key, row in decisions.items()
        if str(row.get("lifecycle_status") or "") == "active"
    }
    for demand in demands:
        by_issue = [d for d in dispatches
                    if str(native_issue_number(d.get("org_issue_number"))) == str(native_issue_number(demand["issue_number"]))]
        if not by_issue:
            # 只生成与实际下派相关的需求目录，避免历史空 Issue 污染组织仓库。
            continue
        # 清理后旧 decision 已删除/归档的需求不再写目录；仅保留：
        # 1) 正在执行的批次；2) 有 active decision 的批次。
        _running = [d for d in by_issue
                    if str(d.get("status") or "") in ("dispatching", "running", "aggregating")]
        _has_active_decision = any(
            f"dispatch-{d.get('id') or ''}" in active_decisions for d in by_issue)
        if not _running and not _has_active_decision:
            continue
        by_issue.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
        base = f"org-context/demands/{int(demand['issue_number'])}"
        _put(f"{base}/brief.md", _demand_brief_md(org, demand, by_issue))
        _put(f"{base}/dispatch-plan.md",
             _demand_dispatch_plan_md(demand, by_issue))
        _put(f"{base}/acceptance.md",
             _demand_acceptance_md(demand, by_issue, aggregates, decisions))

    # 机器索引
    index = []
    for rel, content in sorted(files.items()):
        if rel == "org-context/index.json":
            continue
        index.append({
            "path": rel,
            "size": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "kind": ("org" if rel in ("org-context/rules.md", "org-context/profile.md",
                                      "org-context/architecture.md")
                     or rel.startswith(("org-context/standards/", "org-context/knowledge/"))
                     else "repo" if rel.startswith("org-context/repos/")
                     else "repo_map" if rel.startswith("org-context/repo-map/")
                     else "demand"),
        })
    _put("org-context/index.json", json.dumps(
        {"org_id": org_id, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "files": index}, ensure_ascii=False, indent=2))

    _put("org-context/README.md",
         "# 组织上下文目录\n\n"
         "由 Grape 自动维护，按 L0/L1/L2/L3 分层：\n\n"
         "- `rules.md`：L0 组织硬规则（最高优先级，不可被仓库规则覆盖）\n"
         "- `profile.md` / `architecture.md`：L1 组织简介与架构原则\n"
         "- `standards/`：L1 组织规范\n"
         "- `repos/<provider>/<owner>/<repo>/context.md`：L2 仓库目录条目与摘要；"
         "完整 project-info/知识仍在目标仓库\n"
         "- `knowledge/`：组织知识（active）\n"
         "- `demands/<需求号>/`：L3 需求 brief、下派计划、验收结论\n"
         "- `index.json`：机器可读索引（路径/大小/哈希）\n\n"
         "> 执行日志、完整对话、构建产物不写入本目录；详细内容留在子仓/平台存储。\n")
    if truncated:
        files["org-context/TRUNCATED.md"] = (
            "# 镜像已截断\n\n本次同步达到文件数/字节数上限，部分较早内容未写入；"
            "请查看数据库/归档或调整 `ORG_CONTEXT_REPO_MAX_FILES` / "
            "`ORG_CONTEXT_REPO_MAX_BYTES`。\n")
    return files

def _clone_url(repo_key: str) -> str:
    from src.kernel.repo_key import parse_repo_key

    provider, owner, name = parse_repo_key(repo_key)
    hosts = {
        "gitcode": "https://gitcode.com",
        "github": "https://github.com",
        "gitee": "https://gitee.com",
        "gitlab": "https://gitlab.com",
    }
    return f"{hosts.get(provider, hosts['gitcode'])}/{owner}/{name}.git"


_ANALYSIS_SYNC_TIMERS: dict[str, threading.Timer] = {}


def schedule_analysis_sync(org_id: str, delay: float = _SYNC_DEBOUNCE_SECONDS) -> None:
    """把 AI 分析产物同步到 grape-repo-analysis 分支（去抖）。"""
    org_id = str(org_id or "")
    if not org_id:
        return

    def _fire() -> None:
        with _GUARD:
            _ANALYSIS_SYNC_TIMERS.pop(org_id, None)
        try:
            sync_org_analysis_to_repo(org_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"org-analysis sync failed org={org_id}: {exc}")

    with _GUARD:
        old = _ANALYSIS_SYNC_TIMERS.pop(org_id, None)
        if old is not None:
            old.cancel()
        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        _ANALYSIS_SYNC_TIMERS[org_id] = timer
        timer.start()


def _clear_dir_except_git(work: str) -> None:
    """清空工作副本里的业务文件（保留 .git），用于 orphan 分支首次提交。"""
    try:
        for name in os.listdir(work):
            if name == ".git":
                continue
            path = os.path.join(work, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001
        log.warning(f"org-analysis sync clean failed work={work}: {exc}")


def sync_org_analysis_to_repo(org_id: str) -> bool:
    """把仓库地图/AI 分析产物同步到组织仓库的 grape-repo-analysis 分支。

    分支为 orphan（首次）或独立数据分支：只包含 org-context/repo-map/ 与 README，
    不碰组织仓库默认分支（grape-repo-knowledge 或存量 main）。
    """
    from src.domains.mgmt.org_repo_map import repo_map_markdown_files
    from src.domains.mgmt.user.org_db import get_org
    from src.kernel.repo_key import workspace_repo_dir

    org = get_org(str(org_id)) or {}
    repo_key = str(org.get("org_repo_key") or "")
    if not org or org.get("is_personal") or not repo_key:
        return False
    files = repo_map_markdown_files(str(org_id)) or {}
    if not files:
        return False
    try:
        from src.api.deps import get_user_token_for_repo
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id

        owner_id = str(org.get("owner_id") or "")
        acc = get_grape_account_by_id(owner_id) or {}
        username = acc.get("username") or ""
        token = get_user_token_for_repo(repo_key, username, owner_id) if username else ""
        if not token:
            log.warning(f"org-analysis sync: no token org={org_id} repo={repo_key}")
            return False

        work = os.path.join(workspace_repo_dir(repo_key), "grape-repo-analysis-sync")
        _run = runtime_public.run_git
        _scheme = runtime_public.clone_auth_scheme_for(repo_key)
        if not os.path.isdir(os.path.join(work, ".git")):
            os.makedirs(os.path.dirname(work), exist_ok=True)
            clone = _run(["clone", "--depth", "1", _clone_url(repo_key), work],
                         token=token, timeout=300, scheme=_scheme)
            if clone is None or clone.returncode != 0:
                log.warning(f"org-analysis sync: clone failed repo={repo_key}: "
                            f"{(clone.stderr if clone else 'git exec failed')[:200]}")
                return False

        # 优先切到已存在的分析分支；不存在则创建 orphan 分支并清空主分支内容。
        fetch = _run(["-C", work, "fetch", "origin", ORG_REPO_ANALYSIS_BRANCH,
                      "--depth", "1"], token=token, timeout=180, scheme=_scheme)
        if fetch is not None and fetch.returncode == 0:
            co = _run(["-C", work, "checkout", "-B", ORG_REPO_ANALYSIS_BRANCH, "FETCH_HEAD"],
                      token=token, timeout=120, scheme=_scheme)
        else:
            co = _run(["-C", work, "checkout", "--orphan", ORG_REPO_ANALYSIS_BRANCH],
                      token=token, timeout=120, scheme=_scheme)
            if co is None or co.returncode != 0:
                co = _run(["-C", work, "checkout", "-B", ORG_REPO_ANALYSIS_BRANCH],
                          token=token, timeout=120, scheme=_scheme)
            # orphan 分支会把默认分支文件带进 index；清掉，保证分析分支与代码隔离
            _run(["-C", work, "rm", "-rf", "--cached", "."], token=token, scheme=_scheme)
            _clear_dir_except_git(work)
        if co is None or co.returncode != 0:
            log.warning(f"org-analysis sync: checkout failed repo={repo_key}: "
                        f"{(co.stderr if co else 'git exec failed')[:200]}")
            return False

        # 覆盖写：删掉旧 repo-map，避免已解绑仓库的陈旧文件残留
        shutil.rmtree(os.path.join(work, "org-context", "repo-map"), ignore_errors=True)
        for rel, content in files.items():
            path = os.path.join(work, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content if str(content).endswith("\n") else str(content) + "\n")
        add = _run(["-C", work, "add", "-A"], token=token, scheme=_scheme)
        if add is None or add.returncode != 0:
            return False
        diff = _run(["-C", work, "diff", "--cached", "--quiet"], token=token, scheme=_scheme)
        if diff is not None and diff.returncode == 0:
            return False
        msg = f"chore(grape-repo-analysis): refresh AI repo map ({time.strftime('%Y-%m-%d %H:%M')})"
        cm = _run(["-C", work, "-c", "user.name=Grape", "-c", "user.email=grape@local",
                   "commit", "-m", msg], token=token, timeout=120, scheme=_scheme)
        if cm is None or cm.returncode != 0:
            log.warning(f"org-analysis sync: commit failed repo={repo_key}: "
                        f"{(cm.stderr if cm else 'git exec failed')[:200]}")
            return False
        push = _run(["-C", work, "push", "origin",
                     f"HEAD:refs/heads/{ORG_REPO_ANALYSIS_BRANCH}"],
                    token=token, timeout=180, scheme=_scheme)
        if push is None or push.returncode != 0:
            log.warning(f"org-analysis sync: push failed repo={repo_key}: "
                        f"{(push.stderr if push else 'git exec failed')[:200]}")
            return False
        log.info(f"org-analysis sync: pushed {len(files)} file(s) to "
                 f"{repo_key}#{ORG_REPO_ANALYSIS_BRANCH}")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"org-analysis sync failed org={org_id}: {exc}")
        return False


def sync_org_context_to_repo(org_id: str) -> bool:
    """同步一次组织 `org-context/` 到组织仓库默认分支；返回是否有提交。

    使用独立工作副本（workspace/<org repo>/org-context-sync），不碰代码 agent 的
    bare 基线仓库 / worktree。
    """
    from src.domains.mgmt.user.org_db import get_org
    from src.kernel.repo_key import workspace_repo_dir

    org = get_org(org_id) or {}
    repo_key = str(org.get("org_repo_key") or "")
    if not org or org.get("is_personal") or not repo_key:
        return False

    with _org_lock(org_id):
        files = _render_files(org_id)
        if not files:
            return False
        try:
            from src.api.deps import get_user_token_for_repo
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id

            owner_id = str(org.get("owner_id") or "")
            acc = get_grape_account_by_id(owner_id) or {}
            username = acc.get("username") or ""
            # 按组织仓库的 provider 取 token：此前固定走 get_user_token_with_refresh
            # （=provider gitcode），GitHub 组织仓库拿 GitCode token clone 会 401
            # 「could not read Username」，org-context/ 永远推不上去。
            token = get_user_token_for_repo(repo_key, username, owner_id) if username else ""
            if not token:
                log.warning(f"org-context sync: no token org={org_id} repo={repo_key}")
                return False

            work = os.path.join(workspace_repo_dir(repo_key), "org-context-sync")
            _unborn = False
            _run = runtime_public.run_git
            _scheme = runtime_public.clone_auth_scheme_for(repo_key)
            if not os.path.isdir(os.path.join(work, ".git")):
                os.makedirs(os.path.dirname(work), exist_ok=True)
                clone = _run(["clone", "--depth", "1", _clone_url(repo_key), work],
                             token=token, timeout=300, scheme=_scheme)
                if clone is None or clone.returncode != 0:
                    log.warning(f"org-context sync: clone failed repo={repo_key}: "
                                f"{(clone.stderr if clone else 'git exec failed')[:200]}")
                    return False
            else:
                br = _run(["-C", work, "rev-parse", "--abbrev-ref", "HEAD"], token=token, scheme=_scheme)
                _branch = (br.stdout or "").strip() if br is not None else ""
                if not _branch or _branch == "HEAD":
                    # 空仓（首次 push 前）：不 fetch，直接建默认分支
                    _branch = ORG_REPO_DEFAULT_BRANCH
                    _unborn = True
                if not _unborn:
                    fetch = _run(["-C", work, "fetch", "origin", _branch, "--depth", "1"],
                                 token=token, timeout=180, scheme=_scheme)
                    if fetch is None or fetch.returncode != 0:
                        log.warning(f"org-context sync: fetch failed repo={repo_key}")
                        return False
                    # 单分支浅克隆可能没有 remote.origin.fetch refspec（无 origin/<branch>
                    # 引用），fetch 后统一用 FETCH_HEAD 对齐。
                    pull = _run(["-C", work, "reset", "--hard", "FETCH_HEAD"],
                                token=token, timeout=120, scheme=_scheme)
                    if pull is None or pull.returncode != 0:
                        log.warning(f"org-context sync: reset failed repo={repo_key}: "
                                    f"{(pull.stderr if pull else 'git exec failed')[:200]}")
                        return False

            br = _run(["-C", work, "rev-parse", "--abbrev-ref", "HEAD"], token=token, scheme=_scheme)
            branch = (br.stdout or "").strip() if br is not None else ""
            if not branch or branch == "HEAD":
                branch = ORG_REPO_DEFAULT_BRANCH
                _unborn = True

            _clean_managed_org_context(work)
            for rel, content in files.items():
                path = os.path.join(work, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            add = _run(["-C", work, "add", "org-context"], token=token, scheme=_scheme)
            if add is None or add.returncode != 0:
                return False
            diff = _run(["-C", work, "diff", "--cached", "--quiet", "--", "org-context"], token=token, scheme=_scheme)
            if diff is not None and diff.returncode == 0:
                return False  # 无变化
            msg = f"chore(org-context): sync organization context ({time.strftime('%Y-%m-%d %H:%M')})"
            cm = _run(["-C", work, "-c", "user.name=Grape", "-c", "user.email=grape@local",
                       "commit", "-m", msg], token=token, timeout=120, scheme=_scheme)
            if cm is None or cm.returncode != 0:
                log.warning(f"org-context sync: commit failed repo={repo_key}: "
                            f"{(cm.stderr if cm else 'git exec failed')[:200]}")
                return False
            push = _run(["-C", work, "push", "origin", f"HEAD:{branch}"], token=token, timeout=180, scheme=_scheme)
            if push is None or push.returncode != 0:
                log.warning(f"org-context sync: push failed repo={repo_key}: "
                            f"{(push.stderr if push else 'git exec failed')[:200]}")
                return False
            log.info(f"org-context sync: pushed {len(files)} file(s) to {repo_key}#{branch}")
            if _unborn and branch == ORG_REPO_DEFAULT_BRANCH:
                try:
                    from src.domains.mgmt.user.repo_manager import set_default_branch
                    from src.kernel.repo_key import parse_repo_key

                    _prov, _own, _repo = parse_repo_key(repo_key)
                    if set_default_branch(token, _own, _repo, branch, provider=_prov):
                        log.info(f"org-context sync: default branch set to {branch} for {repo_key}")
                except Exception as _de:
                    log.warning(f"org-context sync: set default branch failed repo={repo_key}: {_de}")
            return True
        except Exception as e:
            log.warning(f"org-context sync failed org={org_id}: {e}")
            return False
