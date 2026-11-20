"""组织仓库地图（MD-first）。

数据流：
- 对组织关联仓库读取 README / project-info / 组织上下文镜像，生成可路由摘要；
- DB 保存机器索引与 UI 快照；组织仓库 `org-context/repo-map/` 保存权威 MD（异步同步）；
- 下派时可按 purpose/domains/keywords/interfaces 召回候选仓库。

当前实现是确定性摘要（不依赖外部 LLM），先跑通地图数据、镜像与 UI 闭环；
后续可在 `_build_summary` 前接入模型润色，不改变存储/接口契约。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from src.domains.mgmt.user.org_db import (
    db,
    db_lock,
    get_org,
    list_org_repos,
)
from src.kernel.config import log
from src.kernel.repo_key import parse_repo_key, workspace_repo_dir

_ORG_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()
_MAX_README = int(__import__("os").environ.get("ORG_REPO_MAP_README_CAP", "20000") or 20000)
DEFAULT_RECALL_LIMIT = int(__import__("os").environ.get("ORG_REPO_MAP_RECALL_LIMIT", "3") or 3)
_MIN_RECALL_SCORE = float(__import__("os").environ.get("ORG_REPO_MAP_MIN_SCORE", "3") or 3)
# none：没有命中就返回空，交人工/显式 targets；top1：只回落最高置信度的一个仓；
# all：兼容旧行为（回落全部关联仓库，扩散风险最大）。
_RECALL_FALLBACK = str(
    __import__("os").environ.get("ORG_REPO_MAP_FALLBACK", "none") or "none"
).strip().lower()
_MAX_QUERY_TOKENS = 120

_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "http", "https", "www",
    "com", "org", "github", "gitcode", "readme", "md", "src", "main", "test",
    "tests", "docs", "doc", "true", "false", "none", "null", "license", "usage",
    "安装", "使用", "项目", "仓库", "服务", "说明", "介绍", "功能", "模块",
    "project", "capabilities", "capability", "tech", "stack", "supported",
    "features", "feature", "endpoints", "endpoint", "external", "integrations",
    "integration", "skills", "agents", "data", "model", "configuration", "config",
    "last", "updated", "n/a", "暂无", "能力基线", "待补充职责说明", "降级",
    "api", "http", "https", "json", "yaml", "yml",
    "仓库上下文", "项目能力摘要", "架构概览摘要", "workflows", "workflow", "n/a",
}

# 需求描述里的连接词/泛化词：参与中文 n-gram 会产生大量噪音，召回前先剔除。
_QUERY_NOISE = {
    "组织", "增加", "新增", "要求", "需要", "实现", "支持", "以及", "进行",
    "用于", "相关", "当前", "这个", "一个", "不同", "多个", "本次", "改造",
    "优化", "完善", "补充", "提供", "可以", "同时", "并且", "或者", "如果",
    "因为", "所以", "对于", "通过", "使用", "基于", "包括", "涉及", "不涉及",
    "不负责", "负责", "范围", "目标", "场景", "需求", "任务", "内容", "部分",
    "高难度", "低难度", "中难度", "验证", "测试", "联调",
}


def _org_lock(org_id: str) -> threading.Lock:
    with _GUARD:
        lock = _ORG_LOCKS.get(str(org_id))
        if lock is None:
            lock = threading.Lock()
            _ORG_LOCKS[str(org_id)] = lock
        return lock


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _clip(text: str, limit: int = _MAX_README) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n> （内容过长，已截断）\n"


def ensure_table() -> None:
    from src.domains.mgmt.schema_ddl import _ORG_REPO_MAP_DDL

    with db_lock:
        c = db.cursor()
        c.execute(_ORG_REPO_MAP_DDL)
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_org_repo_map_org_repo "
            "ON grape_t_org_repo_map(org_id, repo_key)"
        )
        db.commit()
    try:
        ensure_ai_table()
    except Exception as exc:  # noqa: BLE001
        log.debug(f"org_repo_map ensure_ai_table skipped: {exc}")


def ensure_ai_table() -> None:
    """AI 分析台账表（与确定性地图分表；幂等）。"""
    from src.domains.mgmt.schema_ddl import _ORG_REPO_AI_DDL

    try:
        from src.kernel import table_ownership as _own

        if _own.should_skip("grape_t_org_repo_ai"):
            return
    except Exception as exc:  # noqa: BLE001
        log.debug(f"org_repo_ai ownership guard unavailable: {exc}")
    with db_lock:
        c = db.cursor()
        c.execute(_ORG_REPO_AI_DDL)
        db.commit()


def get_ai_status(org_id: str, repo_key: str) -> dict:
    """读取某仓最近一次 AI 分析的台账（无记录返回空 dict）。"""
    ensure_ai_table()
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT org_id, repo_key, status, error, engine_id, source_commit, "
            "result_hash, triggered_by, analyzed_at, updated_at "
            "FROM grape_t_org_repo_ai WHERE org_id=? AND repo_key=?",
            (str(org_id), str(repo_key)),
        )
        row = c.fetchone()
        if not row:
            return {}
        cols = [d[0] for d in c.description]
    return dict(zip(cols, row))


def mark_ai_status(org_id: str, repo_key: str, status: str, *,
                   error: str = "", triggered_by: str = "",
                   engine_id: int = 0) -> None:
    """只更新运行状态，不清空上次成功结果。"""
    ensure_ai_table()
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT result_json, source_commit, result_hash, analyzed_at "
            "FROM grape_t_org_repo_ai WHERE org_id=? AND repo_key=?",
            (str(org_id), str(repo_key)),
        )
        old = c.fetchone() or ("", "", "", "")
        c.execute(
            "DELETE FROM grape_t_org_repo_ai WHERE org_id=? AND repo_key=?",
            (str(org_id), str(repo_key)),
        )
        c.execute(
            "INSERT INTO grape_t_org_repo_ai "
            "(org_id, repo_key, status, result_json, error, engine_id, source_commit, "
            "result_hash, triggered_by, analyzed_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(org_id), str(repo_key), status, old[0] or "", error,
                int(engine_id or 0), old[1] or "", old[2] or "",
                triggered_by, old[3] or "", now, now,
            ),
        )
        db.commit()


def _render_ai_summary(result: dict) -> str:
    """把 AI 结构化结果渲染成组织仓库分支上的 Markdown。"""
    def _list(name: str) -> list:
        raw = result.get(name)
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).strip() for x in (raw or []) if str(x).strip()]

    confidence = result.get("confidence")
    try:
        confidence = round(max(0.0, min(1.0, float(confidence))), 2)
    except (TypeError, ValueError):
        confidence = 0.0
    lines = [
        "---",
        "schema_version: 1",
        f"repo_key: {result.get('repo_key') or ''}",
        f"full_name: {result.get('full_name') or ''}",
        "generated_by: org-repo-analyzer",
        f"confidence: {confidence}",
        f"source_commit: {result.get('source_commit') or ''}",
        f"analyzed_at: {result.get('analyzed_at') or _utc_now()}",
        "---",
        "",
        "# 仓库能力摘要",
        "",
        f"> {result.get('purpose') or '（模型未给出定位）'}",
        "",
        "## 负责的功能",
    ]
    caps = result.get("capabilities") or []
    if isinstance(caps, list) and caps:
        for c in caps:
            if isinstance(c, dict):
                name = str(c.get("name") or "").strip()
                desc = str(c.get("description") or "").strip()
                paths = c.get("paths") or []
                if name:
                    lines.append(f"- **{name}**" + (f"：{desc}" if desc else ""))
                    if paths:
                        lines.append(f"  - 证据：{', '.join(f'`{p}`' for p in paths[:8])}")
    if not lines or lines[-1] == "## 负责的功能":
        lines += [f"- {x}" for x in _list("features")] or ["- （模型未提供功能列表）"]
    lines += ["", "## 对外接口"]
    lines += [f"- `{x}`" for x in _list("interfaces")] or ["- （未发现显式接口）"]
    lines += ["", "## 依赖 / 被依赖"]
    lines += [f"- {x}" for x in _list("depends_on")] or ["- （未发现显式依赖）"]
    lines += ["", "## 不负责什么"]
    lines += [f"- {x}" for x in _list("not_owns")] or ["- （未填写）"]
    lines += ["", "## 关键词", ", ".join(_list("keywords")) or "（无）"]
    lines += ["", "## 证据"]
    lines += [f"- {x}" for x in _list("evidence")] or ["- （模型未给出证据）"]
    risks = _list("risks")
    if risks:
        lines += ["", "## 风险 / 待确认"] + [f"- {x}" for x in risks]
    lines += ["", "> 由 Grape AI 仓库分析自动生成；本文件位于分析分支，可直接编辑后等待下次覆盖。", ""]
    return "\n".join(lines)


def save_ai_result(org_id: str, repo_key: str, result: dict, *,
                   engine_id: int = 0, source_commit: str = "",
                   triggered_by: str = "", result_hash: str = "") -> dict | None:
    """保存 AI 分析结果：更新地图主字段（供召回/UI）+ 记录台账 + 触发分支同步。"""
    if not isinstance(result, dict):
        raise ValueError("result must be a JSON object")
    ensure_table()
    ensure_ai_table()
    org = get_org(str(org_id)) or {}
    full_name = str(result.get("full_name") or "")
    if not full_name:
        for r in list_org_repos(str(org_id)) or []:
            if str(r.get("repo_key") or "") == str(repo_key):
                full_name = str(r.get("full_name") or "")
                break
    purpose = str(result.get("purpose") or "").strip()
    domains = _json_list(result.get("domains"))
    keywords = _json_list(result.get("keywords"))
    interfaces = _json_list(result.get("interfaces"))
    depends_on = _json_list(result.get("depends_on"))
    not_owns = _json_list(result.get("not_owns"))
    try:
        confidence = round(max(0.0, min(1.0, float(result.get("confidence") or 0))), 2)
    except (TypeError, ValueError):
        confidence = 0.0
    source_commit = str(result.get("source_commit") or source_commit or "")
    analyzed_at = str(result.get("analyzed_at") or _utc_now())
    summary_md = _render_ai_summary({**result, "full_name": full_name,
                                     "source_commit": source_commit,
                                     "analyzed_at": analyzed_at,
                                     "confidence": confidence})
    _upsert(
        str(org_id), str(repo_key),
        full_name=full_name, is_org_repo=False, status="ready",
        purpose=purpose, domains=domains, keywords=keywords,
        interfaces=interfaces, depends_on=depends_on, not_owns=not_owns,
        confidence=confidence, source_commit=source_commit,
        summary_md=summary_md, analyzed_at=analyzed_at,
    )
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM grape_t_org_repo_ai WHERE org_id=? AND repo_key=?",
            (str(org_id), str(repo_key)),
        )
        c.execute(
            "INSERT INTO grape_t_org_repo_ai "
            "(org_id, repo_key, status, result_json, error, engine_id, source_commit, "
            "result_hash, triggered_by, analyzed_at, created_at, updated_at) "
            "VALUES (?, ?, 'succeeded', ?, '', ?, ?, ?, ?, ?, ?, ?)",
            (
                str(org_id), str(repo_key),
                json.dumps(result, ensure_ascii=False),
                int(engine_id or 0), source_commit, result_hash,
                triggered_by, analyzed_at, now, now,
            ),
        )
        db.commit()
    try:
        from src.domains.mgmt.org_context_repo import schedule_analysis_sync

        schedule_analysis_sync(str(org_id))
    except Exception as exc:
        log.warning(f"org_repo_map analysis sync schedule failed org={org_id}: {exc}")
    return get_map(str(org_id), str(repo_key))


def analysis_context(org_id: str, repo_key: str, *, max_chars: int = 60000) -> dict:
    """构建给 AI 分析引擎的上下文包（不暴露平台 token）。"""
    org = get_org(str(org_id)) or {}
    row = None
    for r in list_org_repos(str(org_id)) or []:
        if str(r.get("repo_key") or "") == str(repo_key):
            row = r
            break
    if not row or bool(row.get("is_org_repo")):
        return {}
    sources = _repo_sources(org, row)
    bundle = {
        "repo_key": str(repo_key),
        "full_name": str(row.get("full_name") or ""),
        "org_id": str(org_id),
        "org_name": str(org.get("name") or ""),
        "description": str(sources.get("description") or ""),
        "readme": str(sources.get("readme_local") or sources.get("readme") or ""),
        "repo_map_doc": str(sources.get("repo_map_doc") or ""),
        "capabilities": str(sources.get("capabilities") or ""),
        "graph": str(sources.get("graph") or ""),
        "org_context": str(sources.get("org_context") or ""),
        "source_commit": str(sources.get("source_commit") or ""),
    }
    # 总体截断，避免 prompt 爆炸
    total = 0
    for k in ("description", "readme", "repo_map_doc", "capabilities", "graph", "org_context"):
        val = str(bundle.get(k) or "")
        remain = max(0, max_chars - total)
        if remain <= 0:
            bundle[k] = ""
        elif len(val) > remain:
            bundle[k] = val[:remain] + "\n\n> （内容过长，已截断）"
        total += len(bundle[k] or "")
    return bundle


def _row_to_map(row) -> dict:
    cols = [d[0] for d in _cursor_description()]
    data = dict(zip(cols, row))
    return {
        "repo_key": data.get("repo_key") or "",
        "full_name": data.get("full_name") or "",
        "is_org_repo": bool(data.get("is_org_repo")),
        "status": data.get("status") or "none",
        "purpose": data.get("purpose") or "",
        "domains": _json_list(data.get("domains_json")),
        "keywords": _json_list(data.get("keywords_json")),
        "interfaces": _json_list(data.get("interfaces_json")),
        "depends_on": _json_list(data.get("depends_on_json")),
        "not_owns": _json_list(data.get("not_owns_json")),
        "confidence": float(data.get("confidence") or 0),
        "source_commit": data.get("source_commit") or "",
        "summary_md": data.get("summary_md") or "",
        "error": data.get("error") or "",
        "analyzed_at": data.get("analyzed_at") or "",
        "updated_at": data.get("updated_at") or "",
    }


def _cursor_description():
    from src.domains.mgmt.user import org_db as _org_db

    # 兼容 SQLite / MySQL 驱动：统一从游标 description 取列名
    return _org_db.db.cursor().description or []


def get_map(org_id: str, repo_key: str) -> dict | None:
    ensure_table()
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT repo_key, full_name, is_org_repo, status, purpose, domains_json, "
            "keywords_json, interfaces_json, depends_on_json, not_owns_json, confidence, "
            "source_commit, summary_md, error, analyzed_at, updated_at "
            "FROM grape_t_org_repo_map WHERE org_id=? AND repo_key=?",
            (str(org_id), str(repo_key)),
        )
        row = c.fetchone()
        if not row:
            return None
        cols = [d[0] for d in c.description]
    return _row_to_map_from_cols(cols, row)


def _row_to_map_from_cols(cols, row) -> dict:
    data = dict(zip(cols, row))
    return {
        "repo_key": data.get("repo_key") or "",
        "full_name": data.get("full_name") or "",
        "is_org_repo": bool(data.get("is_org_repo")),
        "status": data.get("status") or "none",
        "purpose": data.get("purpose") or "",
        "domains": _json_list(data.get("domains_json")),
        "keywords": _json_list(data.get("keywords_json")),
        "interfaces": _json_list(data.get("interfaces_json")),
        "depends_on": _json_list(data.get("depends_on_json")),
        "not_owns": _json_list(data.get("not_owns_json")),
        "confidence": float(data.get("confidence") or 0),
        "source_commit": data.get("source_commit") or "",
        "summary_md": data.get("summary_md") or "",
        "error": data.get("error") or "",
        "analyzed_at": data.get("analyzed_at") or "",
        "updated_at": data.get("updated_at") or "",
    }


def list_maps(org_id: str) -> list[dict]:
    ensure_table()
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT repo_key, full_name, is_org_repo, status, purpose, domains_json, "
            "keywords_json, interfaces_json, depends_on_json, not_owns_json, confidence, "
            "source_commit, summary_md, error, analyzed_at, updated_at "
            "FROM grape_t_org_repo_map WHERE org_id=?",
            (str(org_id),),
        )
        rows = c.fetchall()
        cols = [d[0] for d in c.description]
    return [_row_to_map_from_cols(cols, r) for r in rows]


def _upsert(org_id: str, repo_key: str, *, full_name: str = "", is_org_repo: bool = False,
            status: str = "none", purpose: str = "", domains=None, keywords=None,
            interfaces=None, depends_on=None, not_owns=None, confidence: float = 0,
            source_commit: str = "", summary_md: str = "", error: str = "",
            analyzed_at: str = "") -> None:
    ensure_table()
    domains = list(domains or [])
    keywords = list(keywords or [])
    interfaces = list(interfaces or [])
    depends_on = list(depends_on or [])
    not_owns = list(not_owns or [])
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM grape_t_org_repo_map WHERE org_id=? AND repo_key=?",
            (str(org_id), str(repo_key)),
        )
        c.execute(
            "INSERT INTO grape_t_org_repo_map "
            "(id, org_id, repo_key, full_name, is_org_repo, status, "
            "purpose, domains_json, keywords_json, interfaces_json, depends_on_json, "
            "not_owns_json, confidence, source_commit, summary_md, error, analyzed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                uuid.uuid4().hex,
                str(org_id), str(repo_key), full_name, 1 if is_org_repo else 0, status,
                purpose, json.dumps(domains, ensure_ascii=False),
                json.dumps(keywords, ensure_ascii=False),
                json.dumps(interfaces, ensure_ascii=False),
                json.dumps(depends_on, ensure_ascii=False),
                json.dumps(not_owns, ensure_ascii=False),
                float(confidence or 0), source_commit, summary_md, error, analyzed_at,
            ),
        )
        db.commit()


def delete_maps_for_repo(repo_key: str) -> None:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_t_org_repo_map WHERE repo_key=?", (str(repo_key),))
            db.commit()
    except Exception as exc:
        log.warning(f"org_repo_map delete failed repo={repo_key}: {exc}")


def _owner_identity(org: dict) -> tuple[str, str]:
    owner_id = str(org.get("owner_id") or "")
    username = f"user-{owner_id}" if owner_id else ""
    try:
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id

        acc = get_grape_account_by_id(owner_id) or {}
        username = acc.get("username") or username
    except Exception:
        pass
    return owner_id, username


def _repo_sources(org: dict, row: dict) -> dict:
    """汇总摘要来源：README、project-info、组织上下文镜像、仓库元信息。"""
    repo_key = str(row.get("repo_key") or "")
    provider, owner, name = parse_repo_key(repo_key)
    sources = {
        "readme": "", "readme_local": "", "repo_map_doc": "",
        "capabilities": "", "graph": "", "org_context": "",
        "description": str(row.get("description") or ""), "source_commit": "",
        "provider": provider, "owner": owner, "name": name,
    }
    repo_root = workspace_repo_dir(repo_key)
    pi_dir = __import__("os").path.join(repo_root, "project-info")
    sources["capabilities"] = _read(__import__("os").path.join(pi_dir, "project-capabilities.md"))
    sources["graph"] = _read(__import__("os").path.join(pi_dir, "project-graph.md"))
    # 仓库自己维护的能力清单/地图（优先级高于自动生成的能力基线）。
    for rel in ("REPO_MAP.md", "CAPABILITIES.md", "docs/repo-capabilities.md",
                "docs/capabilities.md", ".grape/repo-map.md"):
        doc = _read(__import__("os").path.join(repo_root, rel))
        if doc:
            sources["repo_map_doc"] = doc
            break
    for rel in ("README.md", "README.zh.md", "docs/README.md"):
        readme_local = _read(__import__("os").path.join(repo_root, rel))
        if readme_local:
            sources["readme_local"] = _clip(readme_local)
            break
    meta_raw = _read(__import__("os").path.join(pi_dir, ".metadata.json"))
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
            sources["source_commit"] = str(meta.get("last_commit_sha") or "")
            sources["description"] = sources["description"] or str(meta.get("note") or "")
        except Exception:
            pass

    # 组织仓库 org-context 镜像中的仓库条目（如果本地工作副本存在）
    org_repo_key = str(org.get("org_repo_key") or "")
    if org_repo_key:
        sync_dir = __import__("os").path.join(
            workspace_repo_dir(org_repo_key), "org-context-sync", "org-context", "repos",
            provider or "", owner or "", name or "", "context.md")
        sources["org_context"] = _read(sync_dir)

    # README / 能力清单（平台 API；失败不影响本地 project-info 摘要）
    try:
        owner_id, username = _owner_identity(org)
        if username:
            from src.api.deps import get_user_token_for
            from src.domains.runtime import public as runtime_public

            VcsApiClient = runtime_public.VcsApiClient

            token = get_user_token_for(username, user_id=owner_id, provider=provider)
            if token:
                client = VcsApiClient(provider, token, owner, name)
                info = client.get_repo_info() or {}
                if isinstance(info, dict):
                    sources["description"] = sources["description"] or str(info.get("description") or "")
                readme = client.get_readme() or {}
                sources["readme"] = _clip(str(readme.get("content") or ""))
                if not sources["repo_map_doc"]:
                    for rel in ("CAPABILITIES.md", "REPO_MAP.md",
                                "docs/repo-capabilities.md", "docs/capabilities.md"):
                        doc = client.get_file(rel) or {}
                        content = str(doc.get("content") or "")
                        if content:
                            sources["repo_map_doc"] = _clip(content)
                            break
                if not sources["readme_local"] and not sources["readme"]:
                    remote_readme = client.get_file("README.md") or {}
                    sources["readme_local"] = _clip(str(remote_readme.get("content") or ""))
    except Exception as exc:
        log.warning(f"org_repo_map readme fetch failed repo={repo_key}: {exc}")
    return sources


def _extract_section(text: str, names: tuple[str, ...]) -> str:
    lines = str(text or "").splitlines()
    candidates: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if not m:
            continue
        title = m.group(2).strip().lower()
        if any(n.lower() in title for n in names):
            candidates.append((i + 1, len(m.group(1))))
    if not candidates:
        return ""
    # 取最深层级的匹配标题：文档 H1「仓库能力地图」和 H2「能力」都含“能力”时，
    # 应该从 H2 开始截取，否则会把后面的「不负责」章节也吞进能力列表。
    start, level = max(candidates, key=lambda x: x[1])
    out = []
    for line in lines[start:]:
        m = re.match(r"^(#{1,6})\s+", line.strip())
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    return "\n".join(out).strip()


def _bullets(text: str, limit: int = 8) -> list[str]:
    out = []
    for line in str(text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        if re.match(r"^[-*+]\s+", raw) or re.match(r"^\d+[.)]\s+", raw):
            item = re.sub(r"^([-*+]|\d+[.)])\s+", "", raw).strip()
            item = re.sub(r"[`*_#>]", "", item).strip()
            if item and len(item) >= 2:
                out.append(item[:160])
        if len(out) >= limit:
            break
    return out


def _headings(text: str, limit: int = 12) -> list[str]:
    out = []
    for line in str(text or "").splitlines():
        m = re.match(r"^#{1,3}\s+(.*)$", line.strip())
        if not m:
            continue
        title = re.sub(r"[`*_#>]", "", m.group(1)).strip()
        if title and title.lower() not in ("readme", "table of contents"):
            out.append(title[:80])
        if len(out) >= limit:
            break
    return out


def _keywords(text: str, limit: int = 16) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,8}", str(text or ""))
    seen = set()
    out = []
    for token in tokens:
        low = token.lower()
        if low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        out.append(token)
        if len(out) >= limit:
            break
    return out


def _interfaces(text: str, limit: int = 20) -> list[str]:
    out = []
    seen = set()
    pattern = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_{}\-./:*]+)")
    for m in pattern.finditer(str(text or "")):
        item = f"{m.group(1)} {m.group(2)}"
        if item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= limit:
            break
    if not out:
        for m in re.finditer(r"`(/[A-Za-z0-9_{}\-./:*]+)`", str(text or "")):
            item = m.group(1)
            if item not in seen:
                seen.add(item)
                out.append(item)
            if len(out) >= limit:
                break
    return out


def _first_paragraph(text: str) -> str:
    body = re.sub(r"```.*?```", "", str(text or ""), flags=re.S)
    body = re.sub(r"^\s*#.*$", "", body, flags=re.M)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"\[[^\]]*\]\([^)]*\)", "", body)
    for para in re.split(r"\n\s*\n", body):
        raw_lines = [x.strip() for x in para.splitlines() if x.strip()]
        # 组织上下文 mirror 的首段通常是 "> 作用域：仓库 | 适用范围：..." 元信息，
        # 不能当成仓库定位；跳过 blockquote/元信息段，继续找真正的介绍段。
        if not raw_lines or raw_lines[0].startswith(">"):
            continue
        if raw_lines[0].startswith(("- ", "* ", "+ ")):
            continue
        line = " ".join(raw_lines)
        if "作用域：" in line or "适用范围：" in line or "关联时间：" in line:
            continue
        if ("codegraph 不可用" in line or "project-capabilities 已生成" in line
                or "完整内容在目标仓库" in line or "codegraph unavailable" in line
                or "codegraph index missing" in line or "no files listed" in line
                or "inferred" in line or "降级" in line):
            continue
        line = re.sub(r"^[>\-*+\s]+", "", line).strip()
        line = re.sub(r"[`*_#>|]", "", line).strip()
        if len(line) >= 8:
            return line[:240]
    return ""


def _build_summary(org: dict, row: dict, sources: dict) -> dict:
    name = sources.get("name") or row.get("repo_name") or row.get("repo_key") or ""
    readme = sources.get("readme") or sources.get("readme_local") or ""
    doc = sources.get("repo_map_doc") or ""
    caps = sources.get("capabilities") or ""
    graph = sources.get("graph") or ""
    context_md = sources.get("org_context") or ""
    combined = "\n\n".join(x for x in (doc, readme, caps, context_md) if x)

    purpose = _first_paragraph(doc)
    if not purpose:
        purpose = _first_paragraph(readme)
    if not purpose:
        purpose = _first_paragraph(caps)
    if not purpose:
        purpose = _first_paragraph(context_md)
    if not purpose:
        _desc = str(sources.get("description") or "").strip()
        if any(x in _desc for x in (
                "codegraph", "inferred", "降级", "no files listed",
                "完整内容在目标仓库")):
            _desc = ""
        purpose = _desc
    if not purpose:
        purpose = f"{name}（{sources.get('owner') or ''}）仓库，暂无 README/能力基线，待补充职责说明。"

    # 能力/职责章节优先从仓库自维护文档取，其次 README/project-info。
    features = _bullets(_extract_section(doc, ("能力", "职责", "功能", "feature", "capabilit", "scope", "服务")))
    if not features:
        features = _bullets(_extract_section(combined, ("能力", "职责", "功能", "feature", "capabilit", "scope", "服务")))
    if not features:
        features = _headings(doc or readme, limit=8)
    interfaces = _interfaces(combined)
    _owner_l = str(sources.get("owner") or "").lower()
    _name_l = str(sources.get("name") or "").lower()
    _noise = {
        _owner_l, _name_l, f"{_owner_l}/{_name_l}",
        re.sub(r"[^a-z0-9]", "", _owner_l),
        re.sub(r"[^a-z0-9]", "", _name_l),
        str(row.get("repo_key") or "").lower(),
    }
    _noise.discard("")
    # 能力条目本身是最强的路由关键词：保留完整短语，同时补分词结果。
    feature_terms = []
    for item in features:
        term = re.sub(r"[`*_#>|]", "", str(item or "")).strip(" -:：")
        if 2 <= len(term) <= 60 and term.lower() not in _noise:
            feature_terms.append(term)
    domains = [x for x in _keywords(
        " ".join([purpose] + features[:5] + _headings(combined, 6)), limit=14)
        if x.lower() not in _noise][:10]
    keywords = []
    for item in feature_terms + [x for x in _keywords(
            " ".join([purpose] + features + interfaces + _headings(combined, 10)), limit=40)
            if x.lower() not in _noise]:
        if item and item.lower() not in {x.lower() for x in keywords}:
            keywords.append(item)
    keywords = keywords[:30]
    deps_text = _extract_section(combined, ("依赖", "dependenc", "depends"))
    depends_on = [
        x for x in re.findall(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", deps_text)
        if x.strip().lower() not in ("n/a", "none")
    ][:12]
    not_owns = _bullets(_extract_section(
        combined, ("不负责", "不涉及", "非目标", "not owns", "non-goal", "out of scope")), limit=8)

    confidence = 0.25
    if readme:
        confidence += 0.25
    if doc:
        confidence += 0.15
    if caps and "N/A" not in caps[:400]:
        confidence += 0.2
    if features:
        confidence += 0.15
    if interfaces:
        confidence += 0.1
    if context_md:
        confidence += 0.1
    confidence = round(min(confidence, 0.95), 2)

    now = _utc_now()
    fm = [
        "---",
        "schema_version: 1",
        f"repo_key: {row.get('repo_key') or ''}",
        f"full_name: {row.get('full_name') or ''}",
        f"purpose: {json.dumps(purpose, ensure_ascii=False)}",
        f"domains: {json.dumps(domains, ensure_ascii=False)}",
        f"keywords: {json.dumps(keywords, ensure_ascii=False)}",
        f"interfaces: {json.dumps(interfaces, ensure_ascii=False)}",
        f"depends_on: {json.dumps(depends_on, ensure_ascii=False)}",
        f"not_owns: {json.dumps(not_owns, ensure_ascii=False)}",
        f"confidence: {confidence}",
        f"source_commit: {sources.get('source_commit') or ''}",
        f"analyzed_at: {now}",
        "---",
        "",
        f"# {name}",
        "",
        "## 一句话定位",
        purpose,
        "",
        "## 负责的功能",
    ]
    fm += [f"- {x}" for x in features] or ["- （README / project-info 未提供明确功能列表）"]
    fm += ["", "## 对外接口"]
    fm += [f"- `{x}`" for x in interfaces] or ["- （未发现显式 HTTP 接口）"]
    fm += ["", "## 依赖 / 被依赖"]
    fm += [f"- {x}" for x in depends_on] or ["- （未发现显式仓库依赖）"]
    fm += ["", "## 不负责什么"]
    fm += [f"- {x}" for x in not_owns] or ["- （未填写）"]
    fm += ["", "## 关键词", ", ".join(keywords) or "（无）"]
    fm += ["", "## 证据"]
    evidence = []
    if doc:
        evidence.append("- REPO_MAP.md / CAPABILITIES.md / docs/*capabilities*.md")
    if readme:
        evidence.append("- README.md")
    if caps and "N/A" not in caps[:400]:
        evidence.append("- project-info/project-capabilities.md")
    if graph:
        evidence.append("- project-info/project-graph.md")
    if context_md:
        evidence.append("- org-context/repos/.../context.md")
    fm += evidence or ["- （暂无）"]
    fm += ["", "> 由 Grape 仓库地图自动生成；可直接编辑本文件或点击「重新分析」刷新。"]

    return {
        "purpose": purpose,
        "domains": domains,
        "keywords": keywords,
        "interfaces": interfaces,
        "depends_on": depends_on,
        "not_owns": not_owns,
        "confidence": confidence,
        "source_commit": sources.get("source_commit") or "",
        "summary_md": "\n".join(fm) + "\n",
    }


def analyze_repo(org_id: str, repo_key: str, *, actor_id: str = "") -> dict:
    org = get_org(org_id) or {}
    row = None
    for r in list_org_repos(org_id) or []:
        if str(r.get("repo_key") or "") == str(repo_key):
            row = r
            break
    if not row:
        raise ValueError("REPO_NOT_IN_ORG")
    if bool(row.get("is_org_repo")):
        # 组织仓库只承载组织编排/镜像，不参与产品需求路由，不需要生成能力摘要。
        return {
            "repo_key": repo_key,
            "full_name": str(row.get("full_name") or ""),
            "is_org_repo": True,
            "status": "skipped",
            "error": "ORG_REPO_NOT_ANALYZED",
        }
    ensure_table()
    _upsert(
        org_id, repo_key, full_name=str(row.get("full_name") or ""),
        is_org_repo=bool(row.get("is_org_repo")), status="analyzing",
    )
    try:
        sources = _repo_sources(org, row)
        built = _build_summary(org, row, sources)
        _upsert(
            org_id, repo_key,
            full_name=str(row.get("full_name") or ""),
            is_org_repo=bool(row.get("is_org_repo")),
            status="ready",
            purpose=built["purpose"],
            domains=built["domains"],
            keywords=built["keywords"],
            interfaces=built["interfaces"],
            depends_on=built["depends_on"],
            not_owns=built["not_owns"],
            confidence=built["confidence"],
            source_commit=built["source_commit"],
            summary_md=built["summary_md"],
            analyzed_at=_utc_now(),
        )
        log.info(f"org_repo_map analyzed org={org_id} repo={repo_key} confidence={built['confidence']}")
    except Exception as exc:
        _upsert(
            org_id, repo_key, full_name=str(row.get("full_name") or ""),
            is_org_repo=bool(row.get("is_org_repo")), status="failed",
            error=str(exc)[:500],
        )
        log.warning(f"org_repo_map analyze failed org={org_id} repo={repo_key}: {exc}")
    try:
        from src.domains.mgmt.org_context_repo import schedule_sync

        schedule_sync(org_id)
    except Exception:
        pass
    return get_map(org_id, repo_key) or {}


def analyze_all(org_id: str, *, actor_id: str = "") -> dict:
    """后台分析组织全部关联仓库；返回本次入队数量。"""
    rows = [r for r in (list_org_repos(org_id) or [])
            if r.get("repo_key") and not r.get("is_org_repo")]
    repo_keys = [str(r.get("repo_key") or "") for r in rows]
    if not repo_keys:
        return {"queued": 0}
    # 先落 queued，前端轮询能立刻看到进度（已有摘要不清空）。
    ensure_table()
    with db_lock:
        c = db.cursor()
        for row in rows:
            key = str(row.get("repo_key") or "")
            if not key:
                continue
            c.execute(
                "UPDATE grape_t_org_repo_map SET status='queued', error='', updated_at=datetime('now') "
                "WHERE org_id=? AND repo_key=?",
                (str(org_id), key),
            )
            if c.rowcount == 0:
                c.execute(
                    "INSERT INTO grape_t_org_repo_map "
                    "(id, org_id, repo_key, full_name, is_org_repo, status) "
                    "VALUES (?, ?, ?, ?, ?, 'queued')",
                    (uuid.uuid4().hex, str(org_id), key,
                     str(row.get("full_name") or ""),
                     1 if row.get("is_org_repo") else 0),
                )
        db.commit()

    def _worker():
        lock = _org_lock(org_id)
        if not lock.acquire(blocking=False):
            log.info(f"org_repo_map analyze_all skipped (already running) org={org_id}")
            return
        try:
            for key in repo_keys:
                try:
                    analyze_repo(org_id, key, actor_id=actor_id)
                except Exception as exc:
                    log.warning(f"org_repo_map analyze_all item failed org={org_id} repo={key}: {exc}")
        finally:
            lock.release()

    threading.Thread(
        target=_worker, name=f"org-repo-map-{org_id}", daemon=True,
    ).start()
    return {"queued": len(repo_keys)}


def _ai_status_map(org_id: str) -> dict[str, dict]:
    """repo_key -> AI 台账（状态/错误/时间），供组织详情页展示 AI 进度。"""
    try:
        ensure_ai_table()
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT repo_key, status, error, analyzed_at FROM grape_t_org_repo_ai "
                "WHERE org_id=?",
                (str(org_id),),
            )
            rows = c.fetchall()
        return {
            str(r[0]): {"status": str(r[1] or ""), "error": str(r[2] or ""),
                        "analyzed_at": str(r[3] or "")}
            for r in rows
        }
    except Exception as exc:  # noqa: BLE001 - 缺表/分库时退回纯地图视图
        log.debug(f"org_repo_map ai status unavailable org={org_id}: {exc}")
        return {}


def list_repo_map(org_id: str) -> dict:
    """全量视图：组织关联仓库 + 地图状态（未分析的也返回）。"""
    maps = {m["repo_key"]: m for m in list_maps(org_id)}
    ai_status = _ai_status_map(org_id)
    out = []
    for row in list_org_repos(org_id) or []:
        if row.get("is_org_repo"):
            # 组织仓库不进入仓库地图（只做编排/镜像，不接收产品下派）。
            continue
        key = str(row.get("repo_key") or "")
        m = maps.get(key) or {}
        out.append({
            "repo_key": key,
            "full_name": row.get("full_name") or m.get("full_name") or "",
            "is_org_repo": bool(row.get("is_org_repo")),
            "status": m.get("status") or "none",
            "purpose": m.get("purpose") or "",
            "domains": m.get("domains") or [],
            "keywords": m.get("keywords") or [],
            "interfaces": m.get("interfaces") or [],
            "depends_on": m.get("depends_on") or [],
            "not_owns": m.get("not_owns") or [],
            "confidence": m.get("confidence") or 0,
            "source_commit": m.get("source_commit") or "",
            "error": m.get("error") or "",
            "analyzed_at": m.get("analyzed_at") or "",
            "has_summary": bool(m.get("summary_md")),
            "ai_status": (ai_status.get(key) or {}).get("status", ""),
            "ai_error": (ai_status.get(key) or {}).get("error", ""),
            "ai_analyzed_at": (ai_status.get(key) or {}).get("analyzed_at", ""),
        })
    out.sort(key=lambda x: (0 if x["is_org_repo"] else 1, x["full_name"] or x["repo_key"]))
    ready = sum(1 for x in out if x["status"] == "ready")
    return {
        "repos": out,
        "summary": {"total": len(out), "ready": ready, "coverage": round(ready / len(out), 2) if out else 0},
    }


def repo_map_markdown_files(org_id: str) -> dict[str, str]:
    """给 org-context 同步使用的 repo-map 镜像文件。"""
    data = list_repo_map(org_id)
    files: dict[str, str] = {}
    index_items = []
    for item in data.get("repos") or []:
        m = get_map(org_id, item["repo_key"]) or {}
        summary = m.get("summary_md") or ""
        if not summary:
            continue
        provider, owner, name = parse_repo_key(item["repo_key"])
        rel = f"org-context/repo-map/repos/{provider}/{owner}/{name}/summary.md"
        files[rel] = summary
        index_items.append({
            "repo_key": item["repo_key"],
            "full_name": item["full_name"],
            "is_org_repo": item["is_org_repo"],
            "purpose": item["purpose"],
            "domains": item["domains"],
            "keywords": item["keywords"],
            "interfaces": item["interfaces"],
            "confidence": item["confidence"],
            "status": item["status"],
            "analyzed_at": item["analyzed_at"],
            "path": rel,
        })
    files["org-context/repo-map/index.json"] = json.dumps(
        {"org_id": org_id, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "repos": index_items}, ensure_ascii=False, indent=2)
    lines = [
        "# 组织仓库地图", "",
        "> 由 Grape 自动生成；每仓一份 `summary.md`，机器索引见 `index.json`。", "",
        f"覆盖：{len(index_items)}/{data['summary']['total']}", "",
    ]
    for item in index_items:
        lines += [
            f"## {item['full_name'] or item['repo_key']}"
            + ("（组织仓库）" if item["is_org_repo"] else ""),
            f"- 定位：{item['purpose'] or '（未填写）'}",
            f"- 领域：{', '.join(item['domains']) or '（无）'}",
            f"- 关键词：{', '.join(item['keywords'][:12]) or '（无）'}",
            f"- 置信度：{item['confidence']} · 状态：{item['status']} · 更新时间：{item['analyzed_at'] or '—'}",
            f"- 详情：`{item['path']}`", "",
        ]
    files["org-context/repo-map/README.md"] = "\n".join(lines) + "\n"
    return files


def _query_tokens(query: str) -> list[str]:
    """需求文本 -> 召回 token。

    中文按 2/3/4 字滑窗生成 n-gram（解决「用户登录接口」匹配不到 keyword
    「登录」的问题），英文按单词；同时保留完整中文短语。输出做去重和噪音过滤。
    """
    text = str(query or "")
    result: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        token = str(token or "").strip().lower()
        if len(token) < 2 or token in _STOPWORDS or token in _QUERY_NOISE:
            return
        if token.isdigit() or token in seen:
            return
        seen.add(token)
        result.append(token)

    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if 2 <= len(run) <= 12:
            add(run)
        for size in (2, 3, 4):
            if len(run) < size:
                continue
            for i in range(len(run) - size + 1):
                add(run[i:i + size])
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{1,}", text):
        add(token)
    return result[:_MAX_QUERY_TOKENS]


def _value_hit(token: str, values) -> tuple[int, str]:
    """token 与一组候选值的匹配：精确 3 分，包含 2 分。"""
    for value in values:
        v = str(value or "").strip().lower()
        if not v:
            continue
        if token == v:
            return 3, v
        if len(token) >= 2 and (token in v or (len(v) >= 2 and v in token)):
            return 2, v
    return 0, ""


def recall_targets(org_id: str, query: str, *, limit: int = 3) -> dict:
    """按仓库地图召回目标仓库（全自动派发用）。

    规则：purpose/domains/keywords/interfaces 命中打分，取 top-N；组织仓库本身不参与
    产品任务召回。低于 ``ORG_REPO_MAP_MIN_SCORE`` 的候选不返回；无命中时按
    ``ORG_REPO_MAP_FALLBACK`` 决定：none（默认，返回空并标记 needs_review）/
    top1（只回落最高置信度仓库）/ all（旧行为，回落全部关联仓库）。
    """
    try:
        limit = max(1, min(int(limit or DEFAULT_RECALL_LIMIT), 20))
    except (TypeError, ValueError):
        limit = max(1, min(DEFAULT_RECALL_LIMIT, 20))
    maps = {m["repo_key"]: m for m in list_maps(org_id) if m.get("status") == "ready"}
    repos = [r for r in (list_org_repos(org_id) or []) if r.get("repo_key")]
    candidates = []
    # “不涉及/不负责/排除”后面的是负向约束：不参与正向命中，也不触发 not_owns 排除，
    # 否则「不涉及 CSV 报表」会把所有 not_owns 含 CSV 的仓库全部误伤。
    _neg_phrases = re.findall(
        r"(?:不涉及|不负责|不需要|不含|排除)([^。；;\n]*)", str(query or ""))
    _neg_tokens = set(_query_tokens(" ".join(_neg_phrases)))
    tokens = [t for t in _query_tokens(query) if t not in _neg_tokens]
    for row in repos:
        key = str(row.get("repo_key") or "")
        if not key or row.get("is_org_repo"):
            continue
        m = maps.get(key) or {}
        if not m:
            continue
        purpose = str(m.get("purpose") or "").lower()
        domains = [str(x) for x in (m.get("domains") or [])]
        keywords = [str(x) for x in (m.get("keywords") or [])]
        interfaces = [str(x) for x in (m.get("interfaces") or [])]
        not_owns = " ".join(str(x) for x in (m.get("not_owns") or [])).lower()
        _provider, _owner, name = parse_repo_key(key)
        short = str(name or "").lower()
        full = str(row.get("full_name") or m.get("full_name") or "").lower()
        score = 0
        matched: list[str] = []
        excluded: list[str] = []
        for token in tokens:
            # not_owns 是软边界：命中“不负责”的词做降权而不是整仓排除。多域需求
            # 经常同时提到“用户登录”和“库存锁定”，不能因为库存仓不负责登录就把
            # 它整体排除；只有当正向得分为 0 时它才会因低于阈值落选。
            if len(token) >= 3 and token in not_owns:
                if token not in excluded:
                    excluded.append(token)
                    score -= 2
                continue
            hit = 0
            for values, weight in ((keywords, 3), (domains, 2), (interfaces, 4)):
                s, v = _value_hit(token, values)
                if s:
                    hit = max(hit, s * weight)
                    if v not in matched:
                        matched.append(v)
            if token in purpose:
                hit = max(hit, 1)
                if token not in matched:
                    matched.append(token)
            # 需求文本直接点名仓库（owner/repo 或 repo 名）时强命中。
            if token in short or (len(short) >= 2 and short in token) or token in full:
                hit = max(hit, 10)
                if key not in matched:
                    matched.append(key)
            score += hit
        if score < _MIN_RECALL_SCORE:
            continue
        reason = "命中：" + ", ".join(dict.fromkeys(matched))[:200]
        if excluded:
            reason += "；排除项降权：" + ", ".join(dict.fromkeys(excluded))[:120]
        candidates.append({
            "repo_key": key,
            "full_name": row.get("full_name") or m.get("full_name") or "",
            "score": score,
            "reason": reason,
            "confidence": round(min(0.95, 0.45 + score / 40.0), 2),
            "purpose": m.get("purpose") or "",
            "matched": list(dict.fromkeys(matched)),
        })
    candidates.sort(key=lambda x: (-x["score"], -(x.get("confidence") or 0),
                                   x["repo_key"]))
    selected = candidates[:limit]
    fallback = False
    needs_review = False
    if not selected:
        if _RECALL_FALLBACK == "all":
            fallback = True
            for row in repos:
                if row.get("is_org_repo"):
                    continue
                selected.append({
                    "repo_key": str(row.get("repo_key") or ""),
                    "full_name": row.get("full_name") or "",
                    "score": 0,
                    "reason": "无地图命中，回落到全部关联仓库",
                    "confidence": (maps.get(str(row.get("repo_key") or "")) or {}).get("confidence") or 0,
                    "purpose": (maps.get(str(row.get("repo_key") or "")) or {}).get("purpose") or "",
                    "matched": [],
                })
        elif _RECALL_FALLBACK == "top1":
            fallback = True
            ready = [
                r for r in repos
                if not r.get("is_org_repo")
                and maps.get(str(r.get("repo_key") or ""))
            ]
            if ready:
                best = max(
                    ready,
                    key=lambda r: maps.get(str(r.get("repo_key") or ""), {}).get("confidence") or 0,
                )
                key = str(best.get("repo_key") or "")
                selected = [{
                    "repo_key": key,
                    "full_name": best.get("full_name") or "",
                    "score": 0,
                    "reason": "无地图命中，回落最高置信度仓库",
                    "confidence": (maps.get(key) or {}).get("confidence") or 0,
                    "purpose": (maps.get(key) or {}).get("purpose") or "",
                    "matched": [],
                }]
            else:
                needs_review = True
        else:
            needs_review = True
    return {
        "query": query,
        "tokens": tokens,
        "targets": [x["repo_key"] for x in selected if x.get("repo_key")],
        "candidates": selected,
        "fallback": fallback,
        "needs_review": needs_review,
        "min_score": _MIN_RECALL_SCORE,
        "limit": limit,
    }
