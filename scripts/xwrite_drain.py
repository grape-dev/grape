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
            # P3-5d：巡检任务（runtime 进程写 mgmt 基表）
            out["autopilot_run_info_set"] = lambda p: mg.set_autopilot_run_info(
                int(p["task_id"]), p.get("last_run_at", ""), p.get("next_run_at", ""))
            out["autopilot_engine_ids_set"] = lambda p: mg.set_autopilot_engine_ids(
                int(p["task_id"]), p.get("engine_runtime_ids", ""))
    except Exception:  # noqa: BLE001
        pass
    # P3-5g：mgmt / insight 进程写 runtime 基表（会话、任务、trace 回填）
    try:
        from src.domains.runtime import public as rt
        if rt.available():
            out["user_rows_purge"] = lambda p: rt.purge_user_rows(str(p["user_id"]))
            out["chat_conversations_delete"] = lambda p: rt.delete_chat_conversations(
                p.get("im_channel_id", ""))
            out["vod_conversations_delete"] = lambda p: rt.delete_vod_conversations(
                agent_session_ids=p.get("agent_session_ids") or None,
                repo_key=p.get("repo_key") or "")
            out["agent_sessions_detach"] = lambda p: rt.detach_agent_sessions(
                [int(x) for x in (p.get("runtime_ids") or [])])
            out["agent_session_runtime_clear"] = lambda p: rt.clear_agent_session_runtime(
                p["repo_key"], p["agent_type"])
            out["agent_session_ensure"] = lambda p: rt.ensure_agent_session(
                p["session_id"], p["repo_key"], p["agent_type"], p.get("status", "idle"))
            out["agent_tasks_stamp_traces"] = lambda p: rt.stamp_task_traces(
                [tuple(x) for x in (p.get("pairs") or [])])
            # P3-5h：杂项（解绑审计 / 巡检执行记录 / 通知偏好 / 事件过滤规则）
            out["detach_log_rebound"] = lambda p: rt.mark_detach_rebound(
                int(p["scene_row_id"]), p.get("node_tag", ""), p.get("rebound_at", ""))
            out["autopilot_executions_delete"] = lambda p: rt.delete_autopilot_executions(
                int(p["task_id"]))
            out["autopilot_findings_set"] = lambda p: rt.set_autopilot_findings(
                int(p["exec_id"]), p.get("findings_json", ""))
            out["repo_im_notifications_set"] = lambda p: rt.set_repo_im_notifications(
                int(p.get("auto_impl_enabled", 0)), int(p.get("notify_enabled", 0)))
            out["event_action_filter_add"] = lambda p: rt.add_event_action_filter(
                dict(p or {}), p.get("created_at", ""))
            out["event_action_filter_update"] = lambda p: rt.update_event_action_filter(
                int(p["rule_id"]), dict(p.get("fields") or {}), p.get("updated_at", ""))
            out["event_action_filter_delete"] = lambda p: rt.delete_event_action_filter(
                int(p["rule_id"]))
            # P3-5f：技能（mgmt 进程写 runtime 基表 skill）
            out["skill_content_update"] = lambda p: rt.update_skill_content(
                int(p["skill_id"]), p.get("content_hash", ""), p.get("file_path", ""))
            out["skill_obs_update"] = lambda p: rt.update_skill_obs(
                int(p["skill_id"]), p.get("object_key", ""), p.get("display_name", ""),
                p.get("agent_type", ""), p.get("description", ""), p.get("category_id", ""),
                p.get("updated_by", ""), p.get("content_hash", ""))
            out["skill_obs_insert"] = lambda p: rt.insert_skill(
                p["skill_name"], p.get("display_name") or p["skill_name"],
                p.get("description", ""), p.get("agent_type", ""), p.get("file_path", ""),
                p.get("scope", "user"), p.get("owner_id", ""), p.get("created_by", ""),
                p.get("created_by", ""), p.get("category_id", ""), p.get("content_hash", ""))
            out["skill_row_create"] = lambda p: rt.create_skill_row(
                p["skill_name"], p.get("description", ""), p.get("agent_type", ""),
                p.get("scope", "user"), p.get("owner_id", ""), p.get("created_by", ""))
            out["skill_row_delete"] = lambda p: rt.delete_skill_row(
                p["skill_name"], p.get("scope", "user"), p.get("owner_id", ""))
            out["skill_delete_by_id"] = lambda p: rt.delete_skill_by_id(int(p["skill_id"]))
            out["skill_like_bump"] = lambda p: rt.adjust_skill_like_count(
                int(p["skill_id"]), int(p.get("delta", 1)))
            # P3-5f-3：凭据（mgmt 进程写 runtime 基表 runtime_credentials / grape_t_repo_credentials）
            out["runtime_credential_insert"] = lambda p: rt.insert_runtime_credential(
                p.get("token_hash", ""), p["name"], p.get("scope", "user"),
                p.get("owner_id", ""), p.get("owner_name", ""), p.get("repo_key", ""),
                p.get("org_id", ""))
            out["runtime_credential_rename"] = lambda p: rt.rename_runtime_credential(
                str(p["cid"]), p["name"])
            out["runtime_credential_scope_set"] = lambda p: rt.set_runtime_credential_scope(
                str(p["cid"]), p.get("scope", "user"), p.get("repo_key", ""),
                p.get("owner_id", ""), p.get("org_id", ""))
            out["runtime_credential_delete"] = lambda p: rt.delete_runtime_credential(
                str(p["cid"]))
            out["repo_credential_upsert"] = lambda p: rt.upsert_repo_credential(
                p["repo_key"], p["cred_type"], p.get("value", ""), p.get("updated_at", ""))
            out["repo_credential_clear"] = lambda p: rt.clear_repo_credential(
                p["repo_key"], p.get("cred_type", "bot"), p.get("updated_at", ""))
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