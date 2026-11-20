"""组织记忆保留与滚动归档（防止上下文随运行次数无限膨胀）。

分层策略（参考 Anthropic/OpenAI context engineering）：
- 热记忆：最近 N 条下派 decision / 经验知识保持 active，进入 UI、仓库镜像与注入候选；
- 温记忆：更早的批次压缩成一条「历史下派经验归档」，只保留一行结论 + 去重经验；
- 冷记忆：原始 decision/knowledge 置为 deprecated/archived，保留在 DB 可查，但不再
  进入组织仓库镜像与默认注入路径；
- 所有列表/渲染路径仍有硬上限，避免一次请求把全部历史拉进内存。

全部函数幂等：重复执行只会在有新增超限条目时产生变更。
"""
from __future__ import annotations

import os
import re

from src.kernel.config import log

_DISPATCH_KEY_RE = re.compile(r"^dispatch-(\d+)$")
_RETRO_NAME_RE = re.compile(r"^下派 #(\d+) 经验沉淀$")
_ARCHIVE_KEY = "dispatch-archive"
_ARCHIVE_TITLE = "历史下派经验归档"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default) or default))
    except Exception:
        return default


def _keep_decisions() -> int:
    return _env_int("ORG_CONTEXT_ACTIVE_DECISIONS", 30)


def _keep_retrospectives() -> int:
    return _env_int("ORG_CONTEXT_ACTIVE_RETROSPECTIVES", 30)


def _archive_entries() -> int:
    return _env_int("ORG_CONTEXT_ARCHIVE_ENTRIES", 200)


def _archive_max_bytes() -> int:
    return _env_int("ORG_CONTEXT_ARCHIVE_MAX_BYTES", 32768)


def _clip(text: object, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def _dispatch_no(row: dict) -> int:
    match = _DISPATCH_KEY_RE.match(str((row or {}).get("ctx_key") or ""))
    return int(match.group(1)) if match else 0


def _decision_conclusions(content: str, limit: int = 2) -> list[str]:
    blocks = re.findall(
        r"\*\*结论\*\*\s*\n+(.+?)(?=\n\n|\n\*\*|\n###|\n##|\Z)",
        content or "", re.S)
    return [_clip(block, 180) for block in blocks[:limit] if _clip(block, 180)]


def _decision_reusable(content: str) -> list[str]:
    match = re.search(r"## 跨仓可复用经验\s*\n(.+?)(?=\n##|\Z)",
                      content or "", re.S)
    if not match:
        return []
    return [_clip(line[2:], 200) for line in match.group(1).splitlines()
            if line.strip().startswith("- ")]


def _build_archive(cold: list[dict]) -> str:
    """把冷 decision 压缩成有界归档：一行批次结论 + 全局去重经验。"""
    lines = [
        "# 历史下派经验归档", "",
        "> 系统自动滚动归档：只保留批次一行结论与去重后的可复用经验；"
        "完整记录仍保留在数据库（deprecated decision），需要时按批次检索。", "",
        "## 批次索引",
    ]
    reusable: list[str] = []
    seen: set[str] = set()
    for row in cold:
        num = _dispatch_no(row)
        content = str(row.get("content") or "")
        title = str(row.get("title") or f"下派 #{num} 结论")
        conclusions = _decision_conclusions(content)
        parts = [f"**#{num}**", _clip(title, 160)]
        parts.extend(conclusions)
        lines.append("- " + " | ".join(p for p in parts if p))
        for item in _decision_reusable(content):
            key = item.lower()
            if key not in seen:
                seen.add(key)
                reusable.append(item)
    if reusable:
        lines += ["", "## 可复用经验（去重）"]
        lines += [f"- {item}" for item in reusable[:40]]
    content = "\n".join(lines).strip() + "\n"
    cap = _archive_max_bytes()
    encoded = content.encode("utf-8")
    if len(encoded) > cap:
        content = encoded[:cap].decode("utf-8", "ignore").rstrip() \
            + "\n\n> （归档超出上限，较早批次已省略）\n"
    return content


def compact_org_memory(org_id: str) -> dict:
    """按保留策略归档旧 decision / retrospective knowledge（幂等）。"""
    from src.domains.mgmt import org_context_store as ctx
    from src.domains.mgmt import org_knowledge as kn

    org_id = str(org_id or "").strip()
    if not org_id:
        return {}
    keep = _keep_decisions()
    archive_entries = _archive_entries()
    # 多取一点：其中可能混有手工 decision / 已 deprecated 行。
    window = keep + archive_entries + 50
    all_rows = [
        r for r in ctx.list_contexts(org_id, kind="decision", limit=window)
        if _DISPATCH_KEY_RE.match(str(r.get("ctx_key") or ""))
    ]
    all_rows.sort(key=_dispatch_no, reverse=True)
    hot = [r for r in all_rows if r.get("lifecycle_status") in ("active", "draft")][:keep]
    hot_ids = {str(r.get("id") or "") for r in hot}
    cold = [r for r in all_rows if str(r.get("id") or "") not in hot_ids][:archive_entries]

    deprecated = 0
    for row in cold:
        if row.get("lifecycle_status") in ("active", "draft"):
            ctx.deprecate_context(org_id, str(row.get("id") or ""), user_id="")
            deprecated += 1

    archive_written = False
    if cold:
        content = _build_archive(cold)
        existing = ctx.find_contexts_by_key(org_id, "decision", _ARCHIVE_KEY)
        old_content = (existing[0].get("content") if existing else "") or ""
        if content.strip() != old_content.strip():
            ctx.upsert_context(
                org_id, "decision", _ARCHIVE_KEY, _ARCHIVE_TITLE, content,
                owner_id="", user_id="")
            archive_written = True

    retro_keep = _keep_retrospectives()
    retros = [
        r for r in kn.list_knowledge(
            org_id, kind="retrospective", status="active",
            limit=retro_keep + archive_entries + 50)
        if _RETRO_NAME_RE.match(str(r.get("name") or ""))
    ]
    archived_knowledge = 0
    for row in retros[retro_keep:]:
        kn.archive_knowledge(org_id, int(row.get("id") or 0))
        archived_knowledge += 1

    result = {
        "org_id": org_id,
        "decisions_deprecated": deprecated,
        "archive_written": archive_written,
        "knowledge_archived": archived_knowledge,
        "active_decision_limit": keep,
        "active_retrospective_limit": retro_keep,
    }
    if any((deprecated, archive_written, archived_knowledge)):
        log.info(f"org memory compacted: {result}")
    return result