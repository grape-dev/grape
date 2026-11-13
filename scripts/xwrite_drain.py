"""补偿队列定时排水：`python scripts/xwrite_drain.py [stats|drain]`。

由 systemd timer（deploy/systemd/grape-xwrite-drain.timer）每 5 分钟调用一次。
环境要求：与 mgmt 进程一致（DEVOPS_DB_* 指向本域库），
跨域调用另需 GRAPE_DSN_* + GRAPE_DOMAIN_LIB=1。
"""
from __future__ import annotations

import json
import os
import sys

from src.kernel import cross_write


def _handlers() -> dict:
    """可用的补偿处理器：目标域 lib 必须在环境中可用，否则对应 action 不注册。"""
    out: dict = {}
    try:
        from src.domains.runtime import public as rt
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"warning": f"runtime lib import failed: {exc}"}))
        return out
    if not rt.available():
        print(json.dumps({"warning": "runtime lib unavailable (DSN/lib flag missing)"}))
        return out
    out.update({
        "engine_cap_upsert": lambda p: rt.upsert_engine_capability(
            p["rid"], p["tag"], priority=int(p.get("priority", 50))),
        "engine_cap_update": lambda p: rt.update_engine_capability(
            p["rid"], p["tag"], int(p.get("priority", 50)), int(p.get("enabled", 1))),
        "engine_cap_delete": lambda p: rt.delete_engine_capability(p["rid"], p.get("tag")),
        "engine_cap_delete_all": lambda p: rt.delete_engine_capability(p["rid"]),
    })
    try:
        from src.domains.mgmt import public as mg
        if mg.available():
            out["scene_featured_init"] = lambda p: mg.mark_default_featured(list(p.get("names") or []))
            # P3-2：场景内容同步（runtime 进程写入）。payload 带完整 fields 才能重放。
            out["scene_upsert"] = lambda p: mg.upsert_scene_content(
                p["name"], dict(p.get("fields") or {}))
            # P3-4b：场景 deploy_url / nodes_json（runtime 进程写入 mgmt 基表）
            out["scene_deploy_url"] = lambda p: mg.set_scene_deploy_url(
                p["url"], scene_id=p.get("scene_id") or None,
                row_id=p.get("rid") or None, repo_key=p.get("repo_key") or None,
                require_active=True)
            out["scene_nodes_update"] = lambda p: mg.set_scene_nodes(
                p["nodes_json"], row_id=int(p["rid"]))
            # P3-5a：managed_repos（runtime 进程写入 mgmt 基表）
            out["repo_field_update"] = lambda p: mg.set_repo_fields(
                p["repo_key"], dict(p.get("fields") or {}))
            out["repo_ensure"] = lambda p: mg.ensure_repo_row(
                p["repo_key"],
                (p.get("fields") or {}).get("repo_owner", ""),
                (p.get("fields") or {}).get("repo_name", ""),
                (p.get("fields") or {}).get("full_name", ""))
            # P3-5b：知识库（runtime 进程写入 mgmt 基表）
            out["knowledge_baseline_upsert"] = lambda p: mg.upsert_baseline_knowledge(
                p["repo_key"], p["display_name"], p.get("description", ""),
                p.get("file_path", ""), p.get("obs_key", ""), p.get("content_hash", ""))
            out["knowledge_ref_ensure"] = lambda p: mg.ensure_scene_knowledge_ref(
                int(p["knowledge_id"]))
            out["knowledge_event_delete"] = lambda p: mg.delete_event_knowledge(
                p["repo_key"], p["scope"], p.get("agent_type", ""), int(p.get("issue_num", 0)))
            out["scene_skill_ref_replace"] = lambda p: mg.replace_scene_skill_ref(
                int(p["scene_id"]), list(p.get("refs") or []))
            # P3-5c：用户映射 / 外部身份 / 飞书接入任务（runtime/IM 进程写 mgmt 基表）
            out["user_mapping_feishu_upsert"] = lambda p: mg.upsert_user_mapping_feishu(
                str(p["user_id"]), p.get("open_id", ""), p.get("feishu_name", ""),
                p.get("app_id", ""))
            out["user_mapping_feishu_clear"] = lambda p: mg.clear_user_mapping_feishu(
                user_id=str(p.get("user_id") or ""), app_id=p.get("app_id") or "")
            out["user_mapping_nickname_set"] = lambda p: mg.set_user_mapping_nickname(
                str(p["user_id"]), p.get("nickname", ""))
            out["feishu_onboard_task_delete"] = lambda p: mg.delete_feishu_onboard_task(
                p.get("app_id", ""))
            out["external_identity_expire"] = lambda p: mg.mark_external_identity_expired(
                str(p["grape_user_id"]), p["provider"])
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "drain"
    if cmd == "stats":
        print(json.dumps(cross_write.stats(), ensure_ascii=False))
        return 0
    handlers = _handlers()
    if not handlers:
        print(json.dumps({"skipped": "no handlers available (lib not configured)"}, ensure_ascii=False))
        return 0
    result = cross_write.drain(handlers)
    print(json.dumps({"drain": result, "handlers": sorted(handlers)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())