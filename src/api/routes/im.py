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
from src.config import log

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
    except Exception as e:      # 审计失败不得阻断业务
        log.warning(f"IM audit log failed (action={action}): {e}")


def _db_conn():
    from src.im import engine_wiring
    return engine_wiring._connect(engine_wiring.default_db_path())


def _engine():
    from src.api.main import im_engine
    return im_engine


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


@router.get("/status")
def im_engine_status(user: dict = Depends(get_current_user_or_api_key)):
    """IM 引擎状态汇总——前端用来显示总开关与真连上的渠道数。

    注：旧 BotManager 已下线（A2 M2），active_engine 只剩 new/idle 两态。
    """
    from src.im import engine_wiring
    eng = _engine()
    mode = engine_wiring.engine_mode()
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
    }


def _notify_stats() -> dict:
    """通知扇出计数（A4：任务分配/待澄清/待审核/完成 全渠道）。"""
    try:
        from src.im.notify import stats
        return stats()
    except Exception:
        return {}


@router.get("/installations")
def im_installations(
    repo: str = "",
    user: dict = Depends(get_current_user_or_api_key),
):
    """安装列表：user 轨按 owner_user_id 过滤，repo 轨按仓库访问权限过滤；secret 掩码。"""
    uid = str(user.get("id") or "")
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT id, channel_type, scope, owner_user_id, repo_key, config, status, "
            "updated_at FROM grape_t_channel_installation ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()

    connected = _connected_ids()
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
        out.append({
            "installation_id": inst_id,
            "channel_type": ctype,
            "scope": scope,
            "owner_user_id": str(owner or ""),
            "repo_key": str(repo_key or ""),
            "config": _masked_config(config),
            "status": str(status or ""),
            "connected": inst_id in connected,
            "updated_at": str(updated or ""),
        })
    return {"installations": out, "total": len(out)}


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
    from src.im.notify import notify_user, stats
    kind = body.kind if body.kind in ("task", "awaiting", "pending_review", "done", "error") else "task"
    res = await notify_user(int(user.get("id") or 0), kind,
                            (body.text or "这是一条多渠道通知测试。"),
                            repo_key=body.repo_key, issue_number=body.issue_number,
                            dedupe=False)
    return {"ok": bool(res.get("sent")), **res, "stats": stats()}


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
            from src.im.adapters.dingtalk.cards import DingtalkCardClient, new_out_track_id
            client = DingtalkCardClient(app_id, secret)
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

        from src.im.engine.supervisor import fingerprint
        from src.im.adapters.dingtalk.cards import set_card_mode_file
        conn.execute(
            "UPDATE grape_t_channel_installation SET config=?, config_fingerprint=?, "
            "updated_at=datetime('now') WHERE id=?",
            (json.dumps(cfg, ensure_ascii=False), fingerprint(cfg), body.installation_id),
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
