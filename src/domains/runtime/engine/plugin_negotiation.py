"""缺插件协商（D1-a，Issue #1090 / 插件市场 C2）。

职责（Spec In Scope）：
- Agent 检测到任务所需插件不在能力清单中 → 生成结构化「缺插件报告」
  （missing_plugin_report）；
- 由报告构造「建议安装项」（plugin_install_request），对齐 B3（#1086）
  统一插件卡片/确认单字段契约：name(@owner/name)/version/namespace/
  credentials_required/reason/source_task；
- 提交补装请求：契约对接市场服务端（src.api.plugin_market
  .create_plugin_install_request，B3/B1 合入后生效）；未合入或提交失败 →
  降级为 comment 通道（Agent 回帖协商，用户确认后走补装），不抛异常、
  不阻塞任务核心链路（降级先行）。

协商触发（Spec 决策）：Agent 主动报告（模型判断，依据 Prompt 注入的能力
清单与任务需求比对），覆盖面广于硬编码能力探测。
"""
import json
import logging

logger = logging.getLogger(__name__)

REPORT_TYPE = "missing_plugin_report"
REQUEST_TYPE = "plugin_install_request"


def _plugin_name(entry) -> str:
    """容忍 str / dict（{"name": ...}）两种所需能力表达。"""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        return (entry.get("name") or "").strip()
    return ""


def build_missing_plugin_report(required, installed=None, task_ref: str = "") -> dict:
    """比对任务所需插件与已装清单 → 结构化缺插件报告。

    required: list[str | dict]（dict 可带 reason，如 {"name": "@o/x", "reason": "..."}）；
    installed: list[dict]（collect_plugin_capabilities 归一化条目）或 list[str]。
    返回 {"type", "task_ref", "missing": [{name, reason, status}],
          "installed_names": [...], "degraded": bool}。
    """
    installed = installed or []
    installed_names = []
    for it in installed:
        n = _plugin_name(it) or (it.get("name", "") if isinstance(it, dict) else "")
        if n:
            installed_names.append(n)
    installed_set = set(installed_names)

    missing, seen = [], set()
    for entry in (required or []):
        name = _plugin_name(entry)
        if not name or name in seen:
            continue
        seen.add(name)
        if name in installed_set:
            continue
        reason = entry.get("reason", "") if isinstance(entry, dict) else ""
        missing.append({"name": name, "reason": reason or "required by task", "status": "missing"})

    return {
        "type": REPORT_TYPE,
        "task_ref": task_ref,
        "missing": missing,
        "installed_names": sorted(installed_set),
        "degraded": bool(missing),
    }


def build_install_request(report: dict, task_ref: str = "") -> dict:
    """由缺插件报告构造建议安装项（B3 统一确认单契约 payload）。

    契约字段（对齐 B3 统一插件卡片）：
    - name: @owner/name 命名空间全名；namespace: owner 段；version: 缺省 latest
    - credentials_required: 凭据声明（报告未携带时为空数组，卡片上补配）
    - reason: 安装原因（来自报告）；source_task: 发起任务
    """
    items = []
    for m in (report or {}).get("missing", []):
        name = m.get("name", "")
        namespace = name.split("/")[0] if "/" in name else ""
        items.append({
            "name": name,
            "namespace": namespace,
            "version": m.get("version") or "latest",
            "credentials_required": m.get("credentials_required") or [],
            "reason": m.get("reason") or "required by task",
        })
    return {
        "type": REQUEST_TYPE,
        "items": items,
        "source_task": task_ref or (report or {}).get("task_ref", ""),
        "channel": "confirmation_card",
    }


def _enqueue_unified_queue(request: dict, repo_key: str = "", agent_type: str = "",
                           runtime_id: int = 0) -> dict:
    """入 #1089 统一确认队列 plugin_install_requests（B3 确认单落地队列）。

    - source='agent_report'（D1-a 口径，与 skill_ref/console/scene 并列来源）；
    - 幂等：同插件同来源同作用域已存在 queued 请求 → deduped，不重复入队；
    - 表未就绪（#1089 未合入）/写库异常 → {"ok": False}，由调用方继续降级。
    """
    queued, deduped = [], []
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            for it in (request or {}).get("items", []):
                if not isinstance(it, dict):
                    continue
                name = str(it.get("name", "")).strip()
                if not name:
                    continue
                dup = c.execute(
                    "SELECT 1 FROM plugin_install_requests WHERE plugin_name=? "
                    "AND source='agent_report' AND repo_key=? AND status='queued' LIMIT 1",
                    (name, repo_key or "")).fetchone()
                if dup:
                    deduped.append(name)
                    continue
                c.execute(
                    "INSERT INTO plugin_install_requests "
                    "(plugin_name, version, source, runtime_id, repo_key, agent_type, status) "
                    "VALUES (?,?,?,?,?,?,'queued')",
                    (name, str(it.get("version", "") or ""), "agent_report",
                     runtime_id or 0, repo_key or "", agent_type or ""))
                queued.append(name)
            db.commit()
    except Exception:
        return {"ok": False, "queued": [], "deduped": []}
    if not queued and not deduped:
        return {"ok": False, "queued": [], "deduped": []}
    return {"ok": True, "queued": queued, "deduped": deduped}


def submit_install_request(request: dict, repo_key: str = "", agent_type: str = "",
                           runtime_id: int = 0) -> dict:
    """提交补装请求（契约对接 + 降级先行，绝不抛异常）。

    提交源链（命中即返回）：
    1. B3 市场服务端契约：src.api.plugin_market.create_plugin_install_request(request)
       （B1/B3 合入后提供；返回统一确认单引用 dict）→ channel=confirmation_card；
    2. #1089 统一确认队列：plugin_install_requests 入队（source=agent_report，
       幂等去重）→ channel=unified_queue；
    3. 均未就绪/失败 → 降级 comment 通道：
       {"submitted": False, "channel": "comment", "reason": ..., "request": ...}
       Agent 将协商文本回帖/写入报告，用户确认后走补装——不阻塞核心链路。
    """
    try:
        from src.api.plugin_market import create_plugin_install_request
        confirm_ref = create_plugin_install_request(request)
        return {"submitted": True, "channel": "confirmation_card",
                "confirm_ref": confirm_ref or {}, "request": request}
    except ImportError:
        pass
    except Exception as exc:
        # 市场契约存在但异常 → 继续源链降级，不阻塞；debug 留痕便于诊断
        logger.debug("plugin_market create_plugin_install_request failed, "
                     "degrading to next source: %s", exc, exc_info=True)
    enq = _enqueue_unified_queue(request, repo_key=repo_key, agent_type=agent_type,
                                 runtime_id=runtime_id)
    if enq.get("ok"):
        return {"submitted": True, "channel": "unified_queue",
                "queued": enq.get("queued", []), "deduped": enq.get("deduped", []),
                "request": request}
    return {"submitted": False, "channel": "comment",
            "reason": "plugin_market_api_unavailable", "request": request}


def render_negotiation_comment(report: dict, request: dict) -> str:
    """降级 comment 通道：生成用户可见的协商文本（缺插件报告 + 建议安装项 + 确认指引）。"""
    task_ref = (report or {}).get("task_ref") or (request or {}).get("source_task", "")
    lines = [f"[degraded] 缺插件协商（任务 {task_ref or '-'}）",
             "", f"```json\n{json.dumps(report, ensure_ascii=False)}\n```", "",
             "建议安装项（统一确认单）："]
    for it in (request or {}).get("items", []):
        cred = "、".join(it.get("credentials_required") or []) or "无"
        lines.append(
            f"- {it.get('name')}（version: {it.get('version')}，namespace: {it.get('namespace') or '-'}，"
            f"凭据: {cred}）— {it.get('reason')}")
    lines.append("")
    lines.append("安装需经统一确认单确认后执行（@[确认]）；确认前本任务按降级路径继续，不阻塞核心链路。"
                 "补装完成后能力自动恢复。")
    return "\n".join(lines)
