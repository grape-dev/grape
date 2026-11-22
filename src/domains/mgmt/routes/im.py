"""IM 渠道引擎路由（Issue #825 前端接入）。

前端「IM 渠道」页看的是新引擎的真实状态（`grape_t_channel_installation` + Supervisor）；
本路由提供三个测试杠杆：

  GET  /api/im/status                       引擎总开关/实例/已连接渠道数/安装统计
  GET  /api/im/installations?repo=          安装列表（secret 掩码 + 是否已连上）
  PUT  /api/im/installations/{id}           引擎侧启停（active | disabled）
  POST /api/im/notify-test                  通知扇出自检（给当前用户所有已连接渠道发一条）

说明：本路由**只动新引擎侧**（grape_t_channel_installation）。配置的保存/解绑仍走既有
/chat/config、/agents/vod/*（它们直写 grape_t_channel_installation）。
A2 收尾（Phase B）后 legacy 三列与 `global_im_bot_config` 整表已 DROP，不存在"双轨"。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.api.deps import check_repo_access, get_current_user_or_api_key
from src.kernel.config import log

# src.auth 只存在于主控（审计实现归主控）；worker 包不含该目录。
# 少数构建会把 mgmt 路由装进 worker：此时审计必须静默跳过，不能每个 IM 请求
# 都刷一条 WARNING（下游日志实测会被淹没）。按模块名去重，只记一次 debug。
_IM_AUDIT_MISSING_WARNED: set[str] = set()

router = APIRouter(prefix="/api/im", tags=["im"])


class InstallationStatusBody(BaseModel):
    status: str


def _audit_im(user: dict, request: Request | None, action: str, *,
              installation_id: str = "", channel_type: str = "", scope: str = "",
              extra: dict | None = None) -> None:
    """IM 安装变更审计（F3/C3）：只记动作与标识，绝不记 secret/模板明文。"""
    try:
        from src.auth import audit as auth_audit
        ip = ""
        try:
            ip = (request.client.host if request and request.client else "") or ""
        except Exception:
            ip = ""
        detail = {"action": action, "installation_id": installation_id,
                  "channel_type": channel_type, "scope": scope}
        if extra:
            detail.update({k: v for k, v in extra.items() if v is not None})
        auth_audit.log_audit("im_installation_change",
                             user_id=int(user.get("id") or 0),
                             operator_id=int(user.get("id") or 0),
                             ip=ip, detail=detail)
    except ImportError as _ie:  # 审计模块不在本构建（worker 包无 src.auth）
        if "src.auth" not in _IM_AUDIT_MISSING_WARNED:
            _IM_AUDIT_MISSING_WARNED.add("src.auth")
            log.debug(f"IM audit unavailable in this build ({_ie}); audit skipped")
    except Exception as e:      # 审计失败不得阻断业务
        log.warning(f"IM audit log failed (action={action}): {e}")


def _db_conn():
    from src.domains.runtime.public import im_db_connection
    return im_db_connection()


def _engine():
    """本进程的 IM 引擎（4 进程口径：只有 A/mgmt 进程有；B/C/D 返回 None）。

    不再 import src.api.main（四进程部署下不跑单进程入口），统一走 notify.resolve_engine
    的 注册点 → A 进程模块属性 → app.state 顺序。
    """
    from src.domains.runtime.public import resolve_engine
    return resolve_engine()


def _masked_config(raw: str) -> dict:
    try:
        cfg = json.loads(raw or "{}")
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    out = {"app_id": str(cfg.get("app_id", ""))}
    out["app_secret"] = "********" if cfg.get("app_secret") else ""
    return out


def _connected_ids() -> set:
    """Supervisor._channels 的 key 就是 installation_id（真连上的渠道）。"""
    eng = _engine()
    sup = getattr(eng, "supervisor", None) if eng else None
    try:
        return set(sup._channels) if sup else set()
    except Exception:
        return set()


def _instance_id() -> str:
    """本进程 IM 引擎实例 id（与 /api/im/status 的 instance_id 同源）。"""
    eng = _engine()
    return str(getattr(eng, "instance_id", "") or "") if eng else ""


def _lease_connected_owners() -> dict:
    """全集群口径：租约活着 = 某台主控正在维护这条渠道。

    四台主控按 `grape_t_channel_lease` 分片，每条安装只在一台上连；只按
    本进程 `supervisor._channels` 判 connected，会让连在另外三台的渠道在
    本机页面上显示成「已配置 · 未连接」（Issue：IM 页误报）。租约表是跨
    进程唯一真源：`live_channel_lease_owners()` 已按「owner 非空 + 未过期」
    过滤，两种到期文本口径（空格 / ISO 8601）的判定收口在那里。
    """
    try:
        from src.domains.mgmt.public import live_channel_lease_owners
        owners = live_channel_lease_owners()
    except Exception as e:
        log.warning(f"IM lease snapshot read failed: {e}")
        return {}
    return {str(i): str(o) for i, o in (owners or {}).items()}


@router.get("/status")
def im_engine_status(user: dict = Depends(get_current_user_or_api_key)):
    """IM 引擎状态汇总——前端用来显示总开关与真连上的渠道数。

    注：旧 BotManager 已下线（A2 M2），active_engine 只剩 new/idle 两态。
    Issue #2261 C：新增 outbox 段（未送达计数 + 最近未送达明细）。
    """
    from src.domains.runtime.public import engine_mode
    eng = _engine()
    mode = engine_mode()
    health = {}
    if eng is not None:
        try:
            health = eng.health()
        except Exception:
            health = {}
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM grape_t_channel_installation GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    counts = {str(r[0]): int(r[1]) for r in rows or []}
    health_rows = _channel_health_map()
    return {
        "mode": mode,                                   # on | off（data/im_engine_mode 优先）
        "active_engine": "new" if eng is not None else "idle",  # 旧 legacy 链路已下线
        "instance_id": health.get("instance_id", ""),
        "running": bool(health.get("running")),
        "channels": int(health.get("channels", 0) or 0),
        "installations": {
            "active": counts.get("active", 0),
            "disabled": counts.get("disabled", 0),
            "revoked": counts.get("revoked", 0),
        },
        "scan_interval_sec": 30,
        "notify": _notify_stats(),
        "outbox": _outbox_status(),
        # Issue #3799：渠道风控暂停总览（明细见 GET /api/im/channels/health）
        "channel_health": {
            "paused": sum(1 for r in health_rows.values() if r.get("paused")),
            "firing": sum(1 for r in health_rows.values()
                          if r.get("alert_state") == "firing"),
            "tracked": len(health_rows),
        },
    }


def _outbox_status() -> dict:
    """通知 outbox 状态（#2261 C）：计数 + 最近 20 条未送达。读失败降级空段。"""
    try:
        from src.domains.runtime.public import outbox_counts, outbox_recent_undelivered
        return {
            "counts": outbox_counts(),
            "recent_undelivered": outbox_recent_undelivered(limit=20),
        }
    except Exception:
        return {"counts": {}, "recent_undelivered": []}


@router.get("/outbound")
def im_outbound(status: str = "", limit: int = 50,
                user: dict = Depends(get_current_user_or_api_key)):
    """通知出站明细（#2261 C）：status 可筛（pending/sent/failed/expired），倒序。"""
    status = (status or "").strip().lower()
    if status and status not in ("pending", "sending", "sent", "failed", "expired"):
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "status 只支持 pending/sending/sent/failed/expired 或留空"})
    from src.domains.runtime.public import outbox_list_rows
    rows = outbox_list_rows(status=status, limit=limit)
    return {"outbound": rows, "total": len(rows)}


@router.post("/outbound/{out_track_id}/retry")
def im_outbound_retry(out_track_id: str,
                      user: dict = Depends(get_current_user_or_api_key)):
    """手动重发（#2261 C）：failed/expired → pending（消费者 ≤1s 接力重投）。"""
    from src.domains.runtime.public import outbox_get_row, outbox_requeue_for_retry
    row = outbox_get_row(out_track_id)
    if not row:
        raise HTTPException(status_code=404, detail={
            "code": 404, "message": "outbox 行不存在", "out_track_id": out_track_id})
    if row.get("status") not in ("failed", "expired"):
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "只有 failed/expired 状态的行可以重发",
            "status": row.get("status")})
    ok = outbox_requeue_for_retry(out_track_id)
    if not ok:
        raise HTTPException(status_code=409, detail={
            "code": 409, "message": "重发失败（状态已变化）", "status": row.get("status")})
    log.info(f"IM outbox retry: {out_track_id} requeued by user={user.get('id')}")
    _audit_im(user, None, "outbound_retry", installation_id=str(row.get("installation_id") or ""),
              channel_type=str(row.get("channel_type") or ""),
              extra={"out_track_id": out_track_id, "kind": (row.get("payload") or {}).get("kind")})
    return {"out_track_id": out_track_id, "status": "pending", "requeued": True}


def _notify_stats() -> dict:
    """通知扇出计数（A4：任务分配/待澄清/待审核/完成 全渠道）。"""
    try:
        from src.domains.runtime.public import notify_stats
        return notify_stats()
    except Exception:
        return {}


@router.get("/installations")
def im_installations(
    repo: str = "",
    user: dict = Depends(get_current_user_or_api_key),
):
    """安装列表：user 轨按 owner_user_id 过滤，repo 轨按仓库访问权限过滤；secret 掩码。

    Issue #3799：每条安装带渠道健康字段（`paused / last_pause_at / next_retry_at /
    consecutive_pauses / alert_state`），前端渠道卡片直接消费，无需二次请求。
    """
    uid = str(user.get("id") or "")
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT id, channel_type, scope, owner_user_id, repo_key, config, status, "
            "updated_at FROM grape_t_channel_installation ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()

    local_connected = _connected_ids()
    lease_owners = _lease_connected_owners()
    connected = set(local_connected) | set(lease_owners)
    health_map = _channel_health_map()
    out = []
    for r in rows or []:
        inst_id, ctype, scope, owner, repo_key, config, status, updated = r
        scope = str(scope or "user")
        if scope == "user":
            if uid and str(owner or "") not in ("", uid):
                continue
        else:
            if repo:
                if str(repo_key or "") != repo:
                    continue
            else:
                try:
                    check_repo_access(user, str(repo_key or ""))
                except Exception:
                    continue
        health = health_map.get(str(inst_id)) or {}
        out.append({
            "installation_id": inst_id,
            "channel_type": ctype,
            "scope": scope,
            "owner_user_id": str(owner or ""),
            "repo_key": str(repo_key or ""),
            "config": _masked_config(config),
            "status": str(status or ""),
            "connected": inst_id in connected,
            "connected_on": str(_instance_id() if inst_id in local_connected
                                else lease_owners.get(str(inst_id), "")),
            "updated_at": str(updated or ""),
            # Issue #3799：渠道健康（未记录过的安装 → 默认 active）
            "paused": bool(health.get("paused")),
            "last_pause_at": health.get("last_pause_at", ""),
            "next_retry_at": health.get("next_retry_at", ""),
            "health": health,
        })
    return {"installations": out, "total": len(out)}


def _channel_health_map() -> dict:
    """渠道健康快照（Issue #3799）：表归 runtime，跨域只经 runtime public 读。"""
    try:
        from src.domains.runtime.public import channel_health_rows
        rows = channel_health_rows() or []
    except Exception as e:
        log.warning(f"IM channel health snapshot read failed: {e}")
        return {}
    return {str(r.get("installation_id") or ""): r for r in rows if isinstance(r, dict)}


class RecoverBody(BaseModel):
    """人工恢复动作（Issue #3799）。"""

    action: str = "retry_now"


@router.get("/channels/health")
def im_channel_health(user: dict = Depends(get_current_user_or_api_key)):
    """渠道健康面板（Issue #3799）：paused / last_pause_at / next_retry_at / 告警态。

    告警可检索入口之一：`alert_state=firing` + `alert_reason`（另见 ERROR 日志
    `[im-channel-health] alert=wechat_channel_unavailable`）。
    """
    rows = _channel_health_map()
    values = sorted(rows.values(),
                    key=lambda r: r.get("last_pause_at_ts") or 0.0, reverse=True)
    return {
        "channels": values,
        "total": len(values),
        "paused": sum(1 for r in values if r.get("paused")),
        "firing": sum(1 for r in values if r.get("alert_state") == "firing"),
    }


@router.post("/installations/{installation_id}/recover")
def im_installation_recover(installation_id: str, body: RecoverBody,
                            request: Request,
                            user: dict = Depends(get_current_user_or_api_key)):
    """人工恢复入口（Issue #3799）：`relogin / clear_pause / retry_now`。

    - `retry_now`：唤醒渠道暂停等待，立即重新 getupdates（成功即 paused 清除）；
    - `relogin`：同 retry_now + 打断扫描窗口（重连重新执行 notifystart）；
    - `clear_pause`：只清暂停态/看板，不动连接。
    无引擎进程（B/C/D）返回 409 `engine_unavailable`——不改状态、不假装成功。
    """
    action = (body.action or "").strip().lower()
    if action not in ("relogin", "clear_pause", "retry_now"):
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "action 只支持 relogin / clear_pause / retry_now"})

    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT scope, owner_user_id, repo_key, channel_type "
            "FROM grape_t_channel_installation WHERE id=?",
            (installation_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "安装不存在"})
    scope, owner, repo_key, channel_type = [str(x or "") for x in row[:4]]
    uid = str(user.get("id") or "")
    if scope == "user" and owner and uid and owner != uid:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作其他用户的安装"})
    if scope == "repo":
        check_repo_access(user, repo_key)

    from src.domains.runtime.public import channel_health_rows, channel_recover_channel
    res = channel_recover_channel(installation_id, action, engine=_engine())
    if not res.get("ok") and res.get("reason") in ("engine_unavailable", "channel_not_running"):
        message = ("本进程没有 IM 引擎（恢复需在主控/引擎进程执行）"
                   if res.get("reason") == "engine_unavailable"
                   else "该渠道在当前进程没有运行中的连接（租约在其它主控或已让渡），请稍后重试")
        raise HTTPException(status_code=409, detail={
            "code": 409, "message": message, "installation_id": installation_id,
            "action": action, "reason": res.get("reason")})
    log.info(f"IM engine: recover {installation_id} action={action} "
             f"ok={res.get('ok')} reason={res.get('reason')} (by user={uid})")
    _audit_im(user, request, f"channel_{action}", installation_id=installation_id,
              channel_type=channel_type, scope=scope,
              extra={"ok": bool(res.get("ok")), "reason": str(res.get("reason") or "")})
    health = {}
    try:
        for item in channel_health_rows() or []:
            if str(item.get("installation_id") or "") == installation_id:
                health = item
                break
    except Exception as e:
        log.warning(f"IM channel health read after recover failed: {e}")
    return {"installation_id": installation_id, "channel_type": channel_type,
            "action": action, "ok": bool(res.get("ok")),
            "reason": str(res.get("reason") or ""),
            "health": health or res.get("health") or {}}


class NotifyTestBody(BaseModel):
    kind: str = "task"
    text: str = "这是一条多渠道通知测试。"
    repo_key: str = ""
    issue_number: int = 0


@router.post("/notify-test")
async def im_notify_test(body: NotifyTestBody, user: dict = Depends(get_current_user_or_api_key)):
    """通知扇出自检（A4 杠杆）：给当前用户的**全部已连接渠道**发一条通知，返回逐渠结果。

    用于真机验证「任务分配 / 待澄清 / 待审核 / 完成」通知是否各渠道都收到；
    走与线上完全相同的 notify_user 路径（不去重，方便重复点）。
    """
    from src.domains.runtime.public import notify_stats, notify_user
    kind = body.kind if body.kind in ("task", "awaiting", "pending_review", "done", "error") else "task"
    res = await notify_user(int(user.get("id") or 0), kind,
                            (body.text or "这是一条多渠道通知测试。"),
                            repo_key=body.repo_key, issue_number=body.issue_number,
                            dedupe=False)
    return {"ok": bool(res.get("sent")), **res, "stats": notify_stats()}


@router.put("/installations/{installation_id}")
def im_set_installation_status(installation_id: str, body: InstallationStatusBody,
                               request: Request,
                               user: dict = Depends(get_current_user_or_api_key)):
    """引擎侧启停：active（引擎扫描会捡起来）/ disabled（下一轮扫描优雅让渡）。

    只改 grape_t_channel_installation.status，不动 legacy 表——所以重启后的启动回填不会把它改回去
    （回填的 upsert 只更新 config/fingerprint，不覆盖 status）。
    """
    new_status = (body.status or "").strip().lower()
    if new_status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "status 只支持 active / disabled"})

    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT scope, owner_user_id, repo_key FROM grape_t_channel_installation WHERE id=?",
            (installation_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "安装不存在"})
        scope, owner, repo_key = str(row[0] or "user"), str(row[1] or ""), str(row[2] or "")
        uid = str(user.get("id") or "")
        if scope == "user" and owner and uid and owner != uid:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作其他用户的安装"})
        if scope == "repo":
            check_repo_access(user, repo_key)
        conn.execute(
            "UPDATE grape_t_channel_installation SET status=?, updated_at=datetime('now') WHERE id=?",
            (new_status, installation_id),
        )
        conn.commit()
    finally:
        conn.close()
    log.info(f"IM engine: installation {installation_id} status -> {new_status} (by user={user.get('id')})")
    _audit_im(user, request, f"installation_{new_status}",
              installation_id=installation_id, channel_type="", scope="")
    return {"installation_id": installation_id, "status": new_status}


# 旧 IM（legacy BotManager）已下线（A2 M2）：不再提供 on/off 双链路切换。
# 引擎总开关仍可用文件/env（data/im_engine_mode / GRAPE_IM_ENGINE）→ 5s watcher 生效。

# ---------------------------------------------------------------- 钉钉互动卡片（批 5 · A2）

class DingtalkCardSetupBody(BaseModel):
    installation_id: str
    template_id: str = ""       # 空 = 关闭互动卡片（回落文字卡 + 回数字）


def _load_dingtalk_install(conn, installation_id: str, user: dict) -> dict:
    row = conn.execute(
        "SELECT channel_type, scope, owner_user_id, repo_key, config "
        "FROM grape_t_channel_installation WHERE id=?", (installation_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "安装不存在"})
    ctype, scope, owner, repo_key, raw = [str(x) for x in row[:4]] + [row[4]]
    if ctype != "dingtalk":
        raise HTTPException(status_code=400, detail={"code": 400,
                                                     "message": "该安装不是钉钉渠道"})
    uid = str(user.get("id") or "")
    if scope == "user" and owner and uid and owner != uid:
        raise HTTPException(status_code=403, detail={"code": 403,
                                                     "message": "无权操作其他用户的安装"})
    if scope == "repo":
        check_repo_access(user, repo_key)
    try:
        cfg = json.loads(raw or "{}")
    except Exception:
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _card_setup_view(installation_id: str, cfg: dict) -> dict:
    cards = cfg.get("dingtalk_cards") if isinstance(cfg.get("dingtalk_cards"), dict) else {}
    tpl = str((cards or {}).get("card") or "")
    return {
        "installation_id": installation_id,
        "channel_type": "dingtalk",
        "template_id": tpl,
        "configured": bool(tpl),
        "enabled": bool(tpl and cfg.get("cards_enabled")),
        "mode": "interactive" if (tpl and cfg.get("cards_enabled")) else "action_card",
    }


@router.get("/dingtalk/card-setup")
def im_dingtalk_card_setup(
    installation_id: str = Query(..., description="钉钉安装 id"),
    user: dict = Depends(get_current_user_or_api_key),
):
    """互动卡片接入状态（前端「卡片按钮」一栏读它）。"""
    conn = _db_conn()
    try:
        cfg = _load_dingtalk_install(conn, installation_id, user)
    finally:
        conn.close()
    return _card_setup_view(installation_id, cfg)


@router.post("/dingtalk/card-setup")
async def im_dingtalk_card_setup_save(
    body: DingtalkCardSetupBody,
    request: Request,
    user: dict = Depends(get_current_user_or_api_key),
):
    """一键启用：校验模板 + 权限 → 写安装配置 → 引擎 ≤30s 热重建生效。

    A2（当前）：用户先在卡片平台导入 `docs/weekly-09/templates/im_card_v1.json` 并发布，
    把 cardTemplateId 交给这里；本接口调一次 create 探活，成功才算配置正确
    （探活只建实例不投放，用户侧无感知）。
    A1（后续增强）：template_id 传空 + `auto=true` 时改为调「卡片平台模板复制」自动开模板。
    """
    conn = _db_conn()
    try:
        cfg = _load_dingtalk_install(conn, body.installation_id, user)
        tpl = (body.template_id or "").strip()
        if tpl:
            app_id = str(cfg.get("app_id") or "")
            secret = str(cfg.get("app_secret") or "")
            if not app_id or not secret:
                raise HTTPException(status_code=400, detail={
                    "code": 400, "message": "安装缺少 app_id/app_secret，先把钉钉应用凭证配好"})
            from src.domains.runtime.public import dingtalk_card_client, new_out_track_id
            client = dingtalk_card_client(app_id, secret)
            ok, data = await client.create_instance(
                tpl, new_out_track_id("probe"), {"title": "grape", "body_md": "card probe"})
            if not ok:
                status = data.get("status")
                hint = ("应用未开通「互动卡片实例写权限」，或模板未发布/ID 不属于本应用"
                        if status in (401, 403) or not status
                        else "模板 ID 不正确或卡片平台侧未发布")
                raise HTTPException(status_code=400, detail={
                    "code": 400, "message": "模板/权限校验失败", "hint": hint,
                    "upstream": data})
            cfg["dingtalk_cards"] = {"card": tpl}
            cfg["cards_enabled"] = True
        else:
            cfg["dingtalk_cards"] = {"card": str((cfg.get("dingtalk_cards") or {}).get("card") or "")}
            cfg["cards_enabled"] = False

        from src.domains.runtime.public import installation_fingerprint, set_card_mode_file
        conn.execute(
            "UPDATE grape_t_channel_installation SET config=?, config_fingerprint=?, "
            "updated_at=datetime('now') WHERE id=?",
            (json.dumps(cfg, ensure_ascii=False), installation_fingerprint(cfg), body.installation_id),
        )
        conn.commit()
        # 全局开关：还有别的安装启用着就不能关（订阅 topic 是进程级/渠道级）
        enabled_other = False
        if not body.template_id.strip():
            for (raw_cfg,) in conn.execute(
                    "SELECT config FROM grape_t_channel_installation WHERE channel_type='dingtalk' "
                    "AND status='active'"):
                try:
                    _c = json.loads(raw_cfg or "{}")
                except Exception:
                    continue
                if isinstance(_c, dict) and _c.get("cards_enabled") and _c.get("dingtalk_cards"):
                    enabled_other = True
                    break
        set_card_mode_file(bool(body.template_id.strip()) or enabled_other)
    finally:
        conn.close()
    log.info(f"IM dingtalk card setup: inst={body.installation_id} tpl={body.template_id or '-'} "
             f"enabled={bool(body.template_id)} by user={user.get('id')}")
    _audit_im(user, request, "dingtalk_card_setup", installation_id=body.installation_id,
              channel_type="dingtalk",
              extra={"configured": bool((body.template_id or "").strip())})
    out = _card_setup_view(body.installation_id, cfg)
    out["effective_within_sec"] = 30
    return out
