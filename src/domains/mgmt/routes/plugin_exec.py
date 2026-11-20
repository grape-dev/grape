"""插件执行面（Runner 接线）：grape plugin exec 的服务端编排与安全四件套。

对应形式化验证（OvGate/OvRead，grape-plugin-dev/ov-formal-verification）的四件套：
- 过滤（MemberOnly）：读结果中非本仓的已知仓库目录按仓过滤
- 标记（ClaimsLabeled）：未验证 URI 一律带 claim 标记，写过的 URI 进验证集
- 门禁（VerifiedOnly）：写命令必须带证据（issue/PR 引用）
- 快照（SnapshotCoversWrites）：写命令先向目标实例提交快照，快照失败拒绝写
另有 base_url 白名单（防凭据钓鱼）与 grape_t_plugin_call_logs 审计。

凭据红线：明文只在服务端进程内拼注入位（PluginRunner 出口 redact），
引擎侧调用只携带 tk_ 会话 token，不接触任何插件凭据。
"""
import json
import os
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

from src.kernel.config import log
from src.kernel.id_gen import uuid7_str
from src.kernel.session_manager import db, db_lock

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

_EVIDENCE_RE = re.compile(r"(#\d+|https?://\S+)")
_URI_RE = re.compile(r"viking://[^\s\"'<>\\]+", re.IGNORECASE)
_RESOURCES_RE = re.compile(r"^\s*viking://resources/([^/]+)/", re.IGNORECASE)

_SNAPSHOT_PLUGINS_ENV = "PLUGIN_SNAPSHOT_PLUGINS"
_WHITELIST_ENV = "PLUGIN_ALLOWED_BASE_URLS"
_DEFAULT_SNAPSHOT_PLUGINS = "hd-openviking"


def _ensure_table() -> None:
    """验证集表（幂等建表；会话库随 db_setup 约定）。"""
    with db_lock:
        db.cursor().execute(
            """
            CREATE TABLE IF NOT EXISTS grape_t_plugin_verified_uris (
                uri TEXT PRIMARY KEY,
                plugin TEXT NOT NULL,
                evidence TEXT NOT NULL,
                repo_key TEXT DEFAULT '',
                verified_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        db.commit()


_ensure_table()


class ExecBody(BaseModel):
    command: str
    args: dict = {}
    evidence: str = ""


def _load_manifest_any(name: str) -> dict | None:
    """插件 manifest：市场表（B2 上架必存 manifest 原文）；registry 行仅元信息。"""
    with db_lock:
        row = db.cursor().execute(
            "SELECT manifest FROM grape_t_plugin WHERE name=? AND status='active'", (name,)
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _repo_names() -> set[str]:
    """平台已知仓库名集合（managed_repos 的 name 段）。"""
    with db_lock:
        rows = db.cursor().execute("SELECT repo_key FROM managed_repos").fetchall()
    names = set()
    for (rk,) in rows or []:
        seg = str(rk or "").rsplit("/", 1)
        if len(seg) == 2 and seg[1]:
            names.add(seg[1].lower())
    return names


def _verified_uris(plugin: str) -> set[str]:
    with db_lock:
        rows = db.cursor().execute(
            "SELECT uri FROM grape_t_plugin_verified_uris WHERE plugin=?", (plugin,)
        ).fetchall()
    return {r[0] for r in rows or []}


def _record_verified(uris, plugin: str, evidence: str, repo_key: str) -> None:
    with db_lock:
        c = db.cursor()
        for u in uris:
            c.execute(
                "INSERT OR REPLACE INTO grape_t_plugin_verified_uris (uri, plugin, evidence, repo_key) "
                "VALUES (?,?,?,?)",
                (u, plugin, evidence, repo_key),
            )
        db.commit()


def _collect_uris(obj, out: list) -> None:
    """递归收集 result 里的 viking:// URI。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("uri", "to", "path", "url") and isinstance(v, str):
                out.extend(_URI_RE.findall(v))
            _collect_uris(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_uris(v, out)
    elif isinstance(obj, str):
        out.extend(_URI_RE.findall(obj))


def _filter_and_label(result_text: str, plugin: str, own_repo_name: str):
    """读后处理：成员过滤 + 声明标记（在案返回文本上做，返回结构化摘要）。"""
    filtered, claims, verified_hits = [], [], []
    known_repos = _repo_names()
    verified = _verified_uris(plugin)
    try:
        parsed = json.loads(result_text)
    except Exception:
        parsed = None
    if parsed is None:
        return None, filtered, claims

    def walk(node):
        if isinstance(node, dict):
            uri = node.get("uri")
            if isinstance(uri, str):
                m = _RESOURCES_RE.match(uri)
                seg = m.group(1).lower() if m else None
                if seg and seg != own_repo_name and seg in known_repos:
                    filtered.append(uri)
                    node["_filtered"] = True
                elif uri in verified:
                    node["claim"] = False
                    verified_hits.append(uri)
                else:
                    node["claim"] = True
                    claims.append(uri)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(parsed)
    # 命中过滤的条目从结果中移除（引擎不见越权内容）
    def prune(node):
        if isinstance(node, list):
            kept = []
            for v in node:
                if isinstance(v, dict) and v.get("_filtered"):
                    continue
                prune_in_place(v)
                kept.append(v)
            node[:] = kept
        elif isinstance(node, dict):
            for v in node.values():
                prune_in_place(v)

    def prune_in_place(v):
        if isinstance(v, (dict, list)):
            prune(v)

    prune(parsed)
    return parsed, filtered, claims


def _snapshot_commit(base_url: str, headers: dict, timeout: float,
                     write_uri: str = "") -> None:
    """写前快照：向目标实例提交快照（限定本次写入路径）；失败抛 PluginSnapshotError。

    OV 契约（E2E 实测三轮收敛）：body.message 必填；须显式 paths
    （PERMISSION_DENIED: Snapshot commit requires explicit paths）。
    paths 语义：快照覆盖该路径所在工作区，限定到写入目标。
    """
    import requests

    snap_paths = [write_uri] if write_uri else ["viking://resources/"]
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/api/v1/snapshot/commit",
            json={"message": "grape plugin-exec pre-write snapshot",
                  "paths": snap_paths},
            headers=headers, timeout=timeout,
        )
    except Exception as e:
        raise HTTPException(502, detail={
            "code": 502, "error_code": "PLUGIN_SNAPSHOT_FAILED", "message": f"快照请求失败: {e}"})
    if resp.status_code >= 400:
        raise HTTPException(502, detail={
            "code": 502, "error_code": "PLUGIN_SNAPSHOT_FAILED",
            "message": f"快照提交失败 HTTP {resp.status_code}（写已拒绝）"})


def _audit(plugin: str, command: str, outcome: str, duration_ms: int,
           exit_code, session_key: str, repo_key: str, error: str = "") -> None:
    with db_lock:
        db.cursor().execute(
            "INSERT INTO grape_t_plugin_call_logs (id, dedup_key, ts, plugin, command, adapter, duration_ms, "
            "exit_code, outcome, runtime_id, task_id, repo_key, error_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid7_str(), uuid.uuid4().hex, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
             plugin, command, "rest", duration_ms, exit_code, outcome, "",
             session_key, repo_key, error or ""),
        )
        db.commit()


_authorization = APIKeyHeader(name="Authorization", auto_error=False)


def _session_token_user(authorization: str = Depends(_authorization)) -> dict:
    """exec 仅接受 tk_ 会话 token（任务期、绑 repo+user）。"""
    from src.kernel.auth_sdk.auth_token import verify_session_token
    token = (authorization or "").removeprefix("Bearer ").strip()
    info = verify_session_token(token) if token else None
    if not info:
        raise HTTPException(401, detail={"code": 401, "message": "需要有效的 tk_ 会话 token"})
    return info


@router.post("/{name}/exec", operation_id="plugins_exec",
             summary="Execute plugin command (server-side Runner, tk_ only)",
             description="安全执行面：tk_ 鉴权 → base_url 白名单 → 服务端取凭据 → "
                         "命令白名单/分级 → 读：成员过滤+声明标记；写：证据门禁+落账前快照。"
                         "引擎全程不接触插件凭据。")
def exec_plugin_command(name: str, body: ExecBody,
                        tk: dict = Depends(_session_token_user)):
    from src.domains.mgmt.plugin_runner.runner import PluginRunner, PluginRunnerError
    from src.domains.mgmt.plugin_runner.errors import TransportError, CallTimeout

    manifest = _load_manifest_any(name)
    if not manifest:
        raise HTTPException(404, detail={"code": 404, "message": f"插件不存在: {name}"})

    cmd = next((c for c in (manifest.get("commands") or [])
                if c.get("name") == body.command), None)
    if not cmd:
        raise HTTPException(404, detail={
            "code": 404, "message": f"命令未在插件 manifest 声明: {body.command}"})
    level = str(cmd.get("level") or "read").lower()

    rest = ((cmd.get("adapters") or {}).get("rest"))
    adapter_def = next((a for a in (manifest.get("adapters") or [])), None)
    if not rest or not adapter_def:
        raise HTTPException(400, detail={
            "code": 400, "message": "插件缺 http adapter/rest 定义，exec 暂仅支持 http 型"})
    _cell_base_url = ""   # #2423：凭据格子的实例地址（命中即优先于 manifest 声明）
    base_url = str(adapter_def.get("base_url") or "").rstrip("/")

    # ---- 凭据解析先行（缺凭据 400 优先于白名单报错，见对应测试语义）----
    # ---- 凭据：服务端按 tk_ 的 user_id 取该用户的插件凭据 ----
    # Issue #2423 M1/M2（tk_ 兑换链路）：格子托管凭据解析序——
    # ① 凭据格子（user_id×org，管理端经 vault 入库，M2b；托管分派按 manifest
    #    凭据标记/注册表，见 credential_cell.is_cell_managed_credential）；
    # ② user_credentials(plugin:{name})（存量自配兼容——仅限该用户无吊销记录）；
    # 两者皆无 → 400（缺凭据不静默降级，更不回退机器级静态 conf）。
    # 吊销闭环（QA 200711627 (a)3）：格子已被管理员吊销 → 台账阻断 ② 回退，
    # 吊销不复活对全部解析序成立；重新配格子（org 管理端）方可恢复。
    from src.domains.mgmt.user.db import get_credential
    cred_spec = (manifest.get("credentials") or [None])[0] or {}
    cred_key = cred_spec.get("key") or cred_spec.get("name") or ""
    secret = ""
    cell_revoked = False
    from src.domains.mgmt.auth import credential_cell
    if credential_cell.is_cell_managed_credential(cred_key, cred_spec):
        _uid = tk.get("user_id") or ""
        # org 选格路由（QA 200711627）：任务 repo → managed_repos.org_id 权威口径 → 按 org 取格；
        # repo 未映射 org（org_id 空/0）→ 不限组织取该用户最新一格（与 2423 M2b 语义一致）。
        _task_org = 0
        try:
            _mr = db.cursor().execute(
                "SELECT org_id FROM managed_repos WHERE repo_key=? LIMIT 1",
                (str(tk.get("repo_key") or ""),)).fetchone()
            if _mr and _mr[0]:
                _task_org = int(_mr[0] or 0)
        except Exception:
            _task_org = 0
        try:
            cell = credential_cell.resolve_cell(_uid, org_id=(_task_org or None))
            if cell:
                secret = str(cell.get("secret") or "")
                _cell_base_url = str(cell.get("base_url") or "").rstrip("/")
            elif credential_cell.has_revoked_cell(_uid):
                cell_revoked = True
        except Exception as _ce:
            log.warning(f"exec: credential cell resolve failed for user={tk.get('user_id')}: {_ce}")
    if not secret and not cell_revoked:
        cred_cfg = get_credential(tk.get("user_id") or 0, f"plugin:{name}") or {}
        secret = str(cred_cfg.get(cred_key) or "")
    if not secret:
        _reason = ("用户 OV 凭据格子已被管理员吊销（存量自配不复活，"
                   f"请管理员重新配置格子）: {cred_key}（plugin:{name}）"
                   if cell_revoked else
                   f"用户未配置插件凭据 {cred_key}（plugin:{name}）")
        raise HTTPException(400, detail={
            "code": 400, "error_code": "PLUGIN_CREDENTIAL_MISSING",
            "message": _reason})

    # ---- 实例路由（#2423）：凭据格子的 base_url 优先于 manifest 声明（多实例路由）----
    if _cell_base_url:
        base_url = _cell_base_url

    # ---- base_url 白名单（防凭据钓鱼；未配置即拒绝，不做静默放行）----
    # 检查点在实例路由之后：格子的实例地址同样受白名单约束（防格子指向未授信实例）。
    whitelist = [u.strip().rstrip("/") for u in
                 os.environ.get(_WHITELIST_ENV, "").split(",") if u.strip()]
    if not whitelist:
        raise HTTPException(503, detail={
            "code": 503, "error_code": "PLUGIN_WHITELIST_UNSET",
            "message": f"服务端未配置 {_WHITELIST_ENV}，exec 全局拒绝（deny by default）"})
    if base_url not in whitelist:
        _src = "凭据格子" if _cell_base_url and base_url == _cell_base_url else "插件 manifest"
        raise HTTPException(403, detail={
            "code": 403, "error_code": "PLUGIN_BASE_URL_BLOCKED",
            "message": f"{_src} base_url 不在实例白名单内: {base_url}"})

    # ---- 写路径：证据门禁 + 落账前快照 ----
    snapshot_committed = False
    snapshot_plugins = [p.strip() for p in
                        os.environ.get(_SNAPSHOT_PLUGINS_ENV,
                                       _DEFAULT_SNAPSHOT_PLUGINS).split(",") if p.strip()]
    if level == "write":
        if not _EVIDENCE_RE.search(body.evidence or ""):
            raise HTTPException(400, detail={
                "code": 400, "error_code": "PLUGIN_WRITE_EVIDENCE_REQUIRED",
                "message": "写命令必须带 evidence（issue/PR 引用），无证据不落账"})
        if name in snapshot_plugins:
            snap_headers = {}
            inject = str(cred_spec.get("inject") or "")
            if inject.startswith("header:"):
                snap_headers[inject.split(":", 1)[1]] = secret
            write_uri = str((body.args or {}).get("uri")
                           or (body.args or {}).get("to") or "")
            _snapshot_commit(base_url, snap_headers, 10.0, write_uri)
            snapshot_committed = True

    # ---- 执行：单次调用形状归一（市场 manifest → A2 Runner 形状） ----
    auth = dict(adapter_def.get("auth") or {})
    auth.setdefault("credential", cred_key)
    inject = str(auth.get("inject") or "")
    if inject.startswith("header:") and "header" in inject:
        auth["header"] = inject.split(":", 1)[1].strip()
        auth["inject"] = "header"
    elif inject.startswith("query:"):
        auth["inject"] = "query"
    elif inject.startswith("env:"):
        auth["inject"] = "env"
    runtime_manifest = {
        "name": manifest.get("name") or name,
        "adapters": [{
            "id": "rest", "transport": "http", "url": base_url + rest.get("path", "/"),
            "method": str(rest.get("method") or "GET").upper(),
            "auth": auth,
            "compat": adapter_def.get("compat") or {"engines": ["opencode"]},
        }],
        "commands": [{
            "name": body.command, "adapters": ["rest"], "channels": ["rest"],
            "timeout": int(cmd.get("timeout") or 30),
        }],
        "credentials": {cred_key: {"inject": auth.get("inject") or "header"}},
    }
    t0 = time.monotonic()
    try:
        runner = PluginRunner(runtime_manifest)  # 服务端执行：豁免引擎门禁（门禁只约束引擎侧通道）
        result = runner.run(body.command, body.args or {},
                            credentials={cred_key: secret})
    except (PluginRunnerError, TransportError) as e:
        _audit(name, body.command, "error", int((time.monotonic() - t0) * 1000),
               None, tk.get("session_key", ""), tk.get("repo_key", ""), str(e))
        raise HTTPException(502, detail={"code": 502, "message": f"插件执行失败: {e}"})
    except CallTimeout as e:
        _audit(name, body.command, "timeout", int((time.monotonic() - t0) * 1000),
               None, tk.get("session_key", ""), tk.get("repo_key", ""), str(e))
        raise HTTPException(504, detail={"code": 504, "message": f"插件执行超时: {e}"})

    _audit(name, body.command, result.outcome, result.duration_ms,
           result.exit_code, tk.get("session_key", ""), tk.get("repo_key", ""),
           result.error or "")

    out_text = result.output or ""
    payload: dict = {"ok": result.outcome == "ok",
                    "plugin": name, "command": body.command, "level": level,
                    "output": out_text,
                    "snapshot_committed": snapshot_committed}

    own_repo_name = str(tk.get("repo_key") or "").rsplit("/", 1)[-1].lower()
    if level == "read" and result.outcome == "ok" and out_text:
        parsed, filtered, claims = _filter_and_label(out_text, name, own_repo_name)
        if parsed is not None:
            payload["result"] = parsed
        payload["filtered"] = sorted(set(filtered))
        payload["claims"] = sorted(set(claims))
    if level == "write" and result.outcome == "ok":
        uris = []
        _collect_uris(body.args or {}, uris)
        try:
            data = json.loads(out_text)
            _collect_uris(data, uris)
        except Exception:
            pass
        uri_params = str((body.args or {}).get("to") or (body.args or {}).get("uri") or "")
        uris.extend(_URI_RE.findall(uri_params))
        if uris:
            _record_verified(sorted(set(uris)), name, body.evidence or "",
                             tk.get("repo_key", ""))
        payload["verified_uris"] = sorted(set(uris))

    if result.outcome != "ok":
        raise HTTPException(502, detail={
            "code": 502, "message": f"插件命令失败: {result.error}",
            "output": (result.output or "")[:2000]})
    return payload
