"""任务 env 凭据安全（Issue #2423 M0/M1）—— env_extra 落库加密 + 静态 OV conf 检测。

M0（共享引擎停用静态路径）：
- 引擎主机机器级 ~/.openviking/ovcli.conf 是 C2 混账号根因（全任务共用一个
  OV 账号、身份隔离为零）。任务凭据统一收敛到服务端链路（env 不带 OV 凭据 +
  tk_ 兑换），worker 侧 detect_static_ov_conf 提供停用迁移可见性（检测 + 告警）。

M1（env_extra 落库加密）：
- agent_tasks.env_extra 含服务端注入的凭据（云 AK/SK、tk_、关联仓 token），
  明文落库即 C3。加密信封 enc:v1:<credential_crypto 密文>；密钥与轮换复用
  src.auth.credential_crypto（AES-256-GCM，security_config 单一事实源）。
- 读取兼容：信封解密；存量明文 JSON 直接解析并告警（迁移期降级，迁移后消除）。

M1-c（Issue #3770，存量明文行收口）：
- 读侧命中明文行**就地自愈**（重新加密后条件 UPDATE 回写：`AND env_extra NOT LIKE 'enc:v1:%'`
  + 原值 CAS `AND env_extra=?`，保证幂等/并发安全——不覆盖已加密行、不用旧快照覆盖并发新值）；
  自愈失败或不可自愈（无行 id / 无 runtime 库）才告警；
- 告警按 `(host, 行类型)` 节流——每进程每小时 ≤1 条，超限只计数；计数器
  `grape_env_extra_plaintext_rows_total{outcome}` 承担「不再刷屏但仍可见」；
- outcome 口径（QA #3770 审查）：`healed` 仅在 UPDATE 真正改行（rowcount>0）时计；
  rowcount=0（已被并发加密/删除/覆盖）计 `heal_skipped`；`report` 策略与无行 id 的读点
  计 `report_only`（按设计不自愈）；`heal_failed` 只留给自愈真失败；
- 文案指向真实 backfill `202611220003`（旧文案的 `202611130001` 是无关迁移），日志只输出
  键名/行 id，绝不输出值。

M2（Issue #4367 C，信封解密失败可观测）：
- 写入侧曾误发 kid=dev 公开常量信封（3,430 行），执行侧解不开只落一条 WARNING 就
  按空 env 继续（每小时 52 个任务拿不到下发凭据却不可告警）；
- except 分支保留 WARNING，同时递增 `grape_env_extra_decrypt_failures_total{code,kid}`
  （insight /metrics 出口，告警规则 GrapeEnvExtraDecryptFailing），并写
  `agent_tasks.errors` 结构化诊断槽（code=env_extra_unknown_kid 携 kid，同 code 幂等
  覆盖、保留 5 条、diagnostic=True）；嵌套在 claim 的 db_lock 段内时跳过诊断槽写
  （计数/告警不受影响），观测一律 best-effort，不拖垮读链路。
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from src.kernel.config import log
from src.kernel.scene_custom_env import is_secret_name

# 加密信封前缀（区别于明文 JSON 与 credential_crypto 裸密文）
ENVELOPE_PREFIX = "enc:v1:"

# 机器级静态 OV conf 路径（C2：共享引擎全任务共用一个 OV 账号）
_STATIC_OV_CONF_SUBPATH = os.path.join(".openviking", "ovcli.conf")

# OAuth 身份类 provider：token 在运行期按需解析（get_task_token 等），不上行 env
# （公开常量：service.py 等调用方跨模块引用——私有下划线命名属跨模块私有导入，QA 200711627 Standards #3）
OAUTH_IDENTITY_PROVIDERS = ("gitcode", "github")  # 公开（跨模块复用；#2423 移植）

# 裸凭据名（不带约定后缀的常见凭据字段，如 password/token）——上行 env 时同样拦截
_BARE_SECRET_NAMES = {
    "token", "access_token", "password", "passwd", "secret", "pwd",
    "api_key", "apikey", "api-key", "ak", "sk", "key",
}


def is_env_secret_key(name: str) -> bool:
    """env 上行凭据判定：命名约定后缀（is_secret_name）+ 裸凭据名。"""
    if is_secret_name(name):
        return True
    return (name or "").strip().lower() in _BARE_SECRET_NAMES


def detect_static_ov_conf(home: str = "") -> str:
    """检测引擎主机是否存在机器级静态 OV conf（M0 停用迁移可见性）。

    返回存在的 conf 绝对路径；不存在返回空串。存在即 C2 风险信号：
    共享引擎全任务共用一个 OV 账号，多用户身份隔离为零——凭据应改为
    管理端凭据格子（vault）+ 执行期 tk_ 兑换，并移除该静态文件。
    """
    home_dir = home or os.path.expanduser("~")
    path = os.path.join(home_dir, _STATIC_OV_CONF_SUBPATH)
    return path if os.path.isfile(path) else ""


def warn_static_ov_conf_once(home: str = "") -> str:
    """worker 任务执行路径调用：检测到静态 conf 打一条告警（60s 日志去重由上层日志采样）。"""
    path = detect_static_ov_conf(home)
    if path:
        log.warning(
            "static-ov-conf detected (C2 多用户混账号风险): %s —— 任务 OV 凭据已收敛到"
            " tk_ 兑换链路（服务端进程内持明文），请移除机器级静态 conf 并为成员配置"
            "凭据格子（PUT /api/orgs/{org_id}/ov-credentials/{user_id}）", path)
    return path


# ---------- M1：env_extra 落库加密信封 ----------

def encrypt_env_extra(env: dict) -> str:
    """env_extra 落库加密：dict → enc:v1:<密文>。

    加密失败（如密钥未配置且非开发模式）时抛出，调用方按任务入队失败处理——
    凭据不允许静默降级为明文落库。
    """
    from src.domains.mgmt.auth.credential_crypto import encrypt
    payload = json.dumps(env or {}, ensure_ascii=False)
    try:
        sealed = encrypt(payload)
    except Exception as e:
        raise RuntimeError(f"env_extra 加密失败（拒绝明文落库）: {e}") from e
    return ENVELOPE_PREFIX + sealed


# ---------- M1-c（#3770）：明文行自愈 / 节流告警 / 计数 ----------

# 告警节流窗口（秒）：同一 (host, 行类型) 每进程每小时最多 1 条 WARNING（#3770）
PLAINTEXT_ALERT_INTERVAL_SEC = 3600

# 明文行处理策略：
# - "heal"：就地自愈（需要行 id + runtime 库）；失败退化为报告；
# - "report"：不自愈，只计数 + 节流告警——调用方在同一操作里马上会以加密态回写该行
#   （如 sync_task_session_token）时用，避免 open 事务内嵌套写。
PLAINTEXT_POLICIES = ("heal", "report")

# 明文行支持的「行类型」（当前仅 agent_tasks.env_extra）
PLAINTEXT_ROW_KINDS = ("agent_tasks",)

_plaintext_alert_at: dict = {}      # (host, kind) → 上次告警时间戳
_plaintext_alert_lock = threading.Lock()


def _alert_host() -> str:
    """告警维度里的主机标识（四台主控各自可观测）：与引擎身份同源。"""
    try:
        from src.kernel.host_identity import engine_host
        return engine_host() or "unknown"
    except Exception:
        return "unknown"


def reset_plaintext_alert_throttle() -> None:
    """清空节流状态（测试 seam / 进程内告警重置）。"""
    with _plaintext_alert_lock:
        _plaintext_alert_at.clear()


def _plaintext_alert_allowed(host: str, kind: str, now: float | None = None) -> bool:
    """(host, 行类型) 每小时 ≤1 条：允许则登记时间戳并返回 True。"""
    ts = time.time() if now is None else now
    key = (host, kind)
    with _plaintext_alert_lock:
        last = _plaintext_alert_at.get(key)
        if last is not None and ts - last < PLAINTEXT_ALERT_INTERVAL_SEC:
            return False
        _plaintext_alert_at[key] = ts
        return True


def _record_plaintext(outcome: str) -> None:
    """计数转发（insight 域 /metrics 出口）；任何失败静默——观测不得拖垮读链路。

    worker 包没有 prometheus_client 时 insight.observability 自带 no-op 桩。
    """
    try:
        from src.domains.insight.observability import record_env_extra_plaintext_row
        record_env_extra_plaintext_row(outcome)
    except Exception as _obs_e:  # noqa: BLE001 - 观测失败只降级为 debug 日志，不拖垮读链路
        log.debug(f"env_extra plaintext 计数转发失败（outcome={outcome}）: {_obs_e}")


def record_env_extra_write_refused() -> None:
    """写入侧加密失败（拒绝明文回落，任务判失败）计数钩子。"""
    _record_plaintext("write_refused")


def record_env_extra_plaintext(outcome: str) -> None:
    """明文行事件计数（公开入口：迁移等非读路径复用同一计数器，口径可对账）。"""
    _record_plaintext(outcome)


# ---------- M2（#4367 C）：信封解密失败可观测（诊断槽 + 计数） ----------

# 解密失败 code 口径：
# - env_extra_unknown_kid：信封 kid 不在本进程密钥环（生产事故主体是写入侧误发的
#   kid=dev 公开常量信封——配了真实 credential_keys 的执行侧永远解不开）；
# - env_extra_decrypt_failed：其余解密失败（密文损坏/cryptography 缺失/密钥拒发）。
ENV_EXTRA_UNKNOWN_KID = "env_extra_unknown_kid"
ENV_EXTRA_DECRYPT_FAILED = "env_extra_decrypt_failed"

_DECRYPT_DIAG_MESSAGES = {
    ENV_EXTRA_UNKNOWN_KID: (
        "env_extra envelope sealed with a kid missing from this process's key ring "
        "(often the public built-in dev key); task continues with an empty env — "
        "re-issue credentials and fix the writer's configs/security.json (Issue #4367)"),
    ENV_EXTRA_DECRYPT_FAILED: (
        "env_extra envelope failed to decrypt; task continues with an empty env — "
        "re-issue credentials (Issue #4367)"),
}


def _record_decrypt_failure(code: str, kid: str = "") -> None:
    """解密失败计数转发（insight 域 /metrics 出口）；任何失败静默——观测不得拖垮读链路。

    worker 包没有 prometheus_client 时 insight.observability 自带 no-op 桩。
    """
    try:
        from src.domains.insight.observability import record_env_extra_decrypt_failure
        record_env_extra_decrypt_failure(code, kid)
    except Exception as _obs_e:  # noqa: BLE001 - 观测失败只降级为 debug 日志，不拖垮读链路
        log.debug(f"env_extra 解密失败计数转发失败（code={code}）: {_obs_e}")


def _persist_env_extra_decrypt_diag(task_id: str, code: str, kid: str,
                                    reason: str = "") -> None:
    """解密失败写 agent_tasks.errors 结构化诊断槽（Issue #4367 C；best-effort）。

    与 service._persist_task_diagnostic 同口径：同 error_code 幂等覆盖、保留最近
    5 条、diagnostic=True（friendly_failure_hint 不把它当真实失败原因）。本模块随
    worker 包分发，不反向依赖 service，故本地实现；写通道复用
    runtime_agent_tasks_write（单库只持 session_manager.db_lock、三库只持
    owner_lock("runtime")，不嵌套——QA #3770 Critical 锁序口径）。调用方须先过
    _nested_under_process_db_lock（嵌套持锁时跳过写，避免提前提交外层事务）。
    """
    if not task_id:
        return
    entry = {
        "error_code": code,
        "kid": (kid or "").strip()[:40] or "",
        "reason": (reason or "")[:500],
        "message": _DECRYPT_DIAG_MESSAGES.get(code, reason or code),
        "diagnostic": True,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with runtime_agent_tasks_write() as _db:
            cur = _db.cursor()
            cur.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
            if not row:
                return
            existing: list = []
            if row[0]:
                try:
                    parsed = json.loads(row[0])
                    existing = parsed if isinstance(parsed, list) else [row[0]]
                except Exception:
                    existing = [row[0]]
            existing = [e for e in existing
                        if not (isinstance(e, dict) and e.get("error_code") == code)]
            existing = (existing + [entry])[-5:]
            cur.execute("UPDATE agent_tasks SET errors=? WHERE id=?",
                        (json.dumps(existing, ensure_ascii=False), task_id))
            _db.commit()
    except Exception as _diag_e:  # noqa: BLE001 - 诊断失败只降级 debug，不拖垮读链路
        log.debug(f"env_extra 解密失败诊断槽写入失败（task={task_id} code={code}）: {_diag_e}")


def _report_decrypt_failure(code: str, kid: str, *, task_id: str, error: Exception) -> None:
    """解密失败统一观测（#4367 C）：WARNING（既有检索词不变）+ 计数 + 诊断槽。"""
    _record_decrypt_failure(code, kid)
    log.warning(f"env_extra 解密失败（按空 env 处理，任务凭据需重新下发）: {error}")
    if task_id and not _nested_under_process_db_lock():
        _persist_env_extra_decrypt_diag(task_id, code, kid, reason=str(error))


@contextmanager
def runtime_agent_tasks_write():
    """agent_tasks 写通道（#3770 heal / backfill 迁移共用；QA 审查 R1/R2 锁序修复）。

    连接取 owner 域代理（`db_pair("runtime")`）：四进程拆分下 agent_tasks 归 runtime 域，
    单库拓扑自动回落进程库。锁按拓扑**二选一，不嵌套**（QA #3770 Critical）：

    - 单库/同域拓扑（`not uses_own_dsn("runtime")`）：连接就是 `session_manager.db`
      （autocommit=False 共享连接）——全仓 agent_tasks 写路径（claim / sync_task_session_token
      等）都以 `session_manager.db_lock` 串行，这里**只持** `session_manager.db_lock`。
      此前「同持 owner_lock("runtime") + db_lock」与 claim 路径的
      `db_lock → owner_lock("runtime")`（claim 事务内经 health_scorer 现算健康分）构成
      锁序反转，可互等挂死派发链路。
    - 三库拓扑（`uses_own_dsn("runtime")`）：连接是 owner DSN 独立连接，**只持**
      `owner_lock("runtime")`（与 health_scorer 同锁、不叠加进程 db_lock）。
    """
    from src.kernel.owner_db import db_pair
    db, domain_lock = db_pair("runtime")
    try:
        from src.kernel import db_registry
        own_dsn = bool(db_registry.uses_own_dsn("runtime"))
    except Exception as _dom_e:  # noqa: BLE001 - 域判定失败按单库（连接回落进程库）执行
        own_dsn = False
        log.debug(f"runtime_agent_tasks_write: domain 路由判定失败，按单库 db_lock 执行: {_dom_e}")
    if own_dsn:
        with domain_lock:
            yield db
        return
    from src.kernel.session_manager import db_lock as _process_db_lock
    with _process_db_lock:
        yield db


def _nested_under_process_db_lock() -> bool:
    """单库/同域拓扑下当前线程是否已持有 `session_manager.db_lock`（读路径嵌套）。

    QA #3770 Required：`get_task` 会在 claim 的 `db_lock` 段内被调用，此时自愈的 commit
    会把外层未完成事务一并提交。三库拓扑 heal 走独立 owner 连接（不持进程 db_lock），
    不存在该风险，返回 False。
    """
    try:
        from src.kernel import db_registry
        if db_registry.uses_own_dsn("runtime"):
            return False
        from src.kernel.session_manager import db_lock as _process_db_lock
        is_owned = getattr(_process_db_lock, "_is_owned", None)
        return bool(is_owned and is_owned())
    except Exception:
        return False


def heal_env_extra_plaintext_row(row_id: str, sealed: str, *,
                                 row_kind: str = "agent_tasks",
                                 expected_raw: str | None = None) -> bool:
    """读侧就地自愈：把存量明文行改写为 `sealed`（enc:v1 信封）。

    幂等/并发安全：UPDATE 带 `AND env_extra NOT LIKE 'enc:v1:%'`——已被别的路径加密的行
    不会被二次覆盖（返回 False）。`expected_raw`（读到该行时的原文）再叠加原值 CAS
    `AND env_extra=?`——读与写之间该行被换成新明文（如刚轮换的 token）时不以旧快照覆盖
    新值，交由下一次读重新自愈。返回是否真正改写了该行。异常由调用方捕获（自愈失败
    只影响观测，不影响读 env）。

    连接/锁走 ``runtime_agent_tasks_write()``（单库/同域拓扑只持
    ``session_manager.db_lock``，三库拓扑只持 ``owner_lock("runtime")``，不嵌套，
    见其 docstring）。
    """
    kind = (row_kind or "agent_tasks").strip() or "agent_tasks"
    if not row_id or not sealed or kind not in PLAINTEXT_ROW_KINDS:
        return False
    cas = " AND env_extra=?" if expected_raw is not None else ""
    params = (sealed, row_id, expected_raw) if expected_raw is not None else (sealed, row_id)
    with runtime_agent_tasks_write() as db:
        cur = db.cursor()
        cur.execute(
            "UPDATE agent_tasks SET env_extra=? WHERE id=? "
            "AND env_extra IS NOT NULL AND env_extra<>'' "
            f"AND env_extra NOT LIKE 'enc:v1:%'{cas}",
            params)
        changed = (getattr(cur, "rowcount", 0) or 0) > 0
        db.commit()
    return changed


def _plaintext_report(outcome: str, *, task_id: str = "", kind: str = "agent_tasks",
                      env=None, err: str = "") -> None:
    """计数 + 节流告警（文案只含 host/行类型/行 id/键名，不含值）。"""
    _record_plaintext(outcome)
    host = _alert_host()
    if not _plaintext_alert_allowed(host, kind):
        return
    keys = ",".join(sorted(env)) if isinstance(env, dict) and env else "-"
    log.warning(
        "env_extra: 存量明文落库行（C3）host=%s kind=%s row=%s outcome=%s keys=%s%s —— "
        "读侧自愈%s；请执行迁移 202611220003（issue2423-env-extra-encrypt-backfill）加密存量行；"
        "同一 (host, 行类型) 每小时最多 1 条告警，计数见 grape_env_extra_plaintext_rows_total",
        host, kind, task_id or "-", outcome, keys,
        (" err=" + err) if err else "",
        "失败或不可用" if outcome == "heal_failed" else "不适用",
    )


def _parse_env_json(text: str) -> dict:
    try:
        env = json.loads(text)
    except Exception:
        return {}
    return env if isinstance(env, dict) else {}


def _handle_plaintext_row(raw: str, *, task_id: str, row_kind: str,
                          plaintext_policy: str) -> dict:
    """存量明文行：按策略自愈/报告，返回解析出的 env（坏值 → {}）。"""
    kind = (row_kind or "agent_tasks").strip() or "agent_tasks"
    policy = (plaintext_policy or "heal").strip().lower()
    if policy not in PLAINTEXT_POLICIES:
        policy = "heal"
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if not isinstance(parsed, dict):
        # 坏值（非 JSON 对象）：无法加密 → 保留原值，计数 + 节流告警
        _plaintext_report("unparseable", task_id=task_id, kind=kind)
        return {}
    env = parsed
    if policy == "heal" and task_id:
        # QA #3770 Required：嵌套在 claim 的 db_lock 段内（单库/同域拓扑）时不自愈
        # ——heal 的 commit 会把外层未提交事务一并提交；降级为 report_only（只计数）。
        if _nested_under_process_db_lock():
            _plaintext_report("report_only", task_id=task_id, kind=kind, env=env)
            return env
        err = ""
        sealed = ""
        try:
            sealed = encrypt_env_extra(env)
        except Exception as e:      # 只留异常类型：告警不得带出值/密钥细节
            err = type(e).__name__
        if sealed:
            try:
                changed = heal_env_extra_plaintext_row(
                    task_id, sealed, row_kind=kind, expected_raw=raw)
                if changed:
                    # rowcount>0：本行确实被本次读改写为信封（QA #3770：healed 只计真改行）
                    _record_plaintext("healed")
                else:
                    # rowcount=0：行已被并发路径加密/删除/覆盖——按 heal_skipped 计数
                    # （不告警）；若被覆盖为新明文，下一次读会再次自愈
                    _record_plaintext("heal_skipped")
                return env
            except Exception as e:
                err = type(e).__name__
        _plaintext_report("heal_failed", task_id=task_id, kind=kind, env=env, err=err)
        return env
    # report 策略 / 无行 id（无 DB 写通道）：按设计不自愈——计数 + 节流告警
    # （QA #3770：与真失败分开计，heal_failed 不虚高）
    _plaintext_report("report_only", task_id=task_id, kind=kind, env=env)
    return env


def decrypt_env_extra(raw, *, task_id: str = "", row_kind: str = "agent_tasks",
                      plaintext_policy: str = "heal") -> dict:
    """env_extra 回读：enc:v1 信封解密；存量明文 JSON 按策略自愈/告警；坏值 → {}。

    参数（#3770 新增，默认值保持旧调用点行为不变）：
    - ``task_id``：agent_tasks 行 id——有它才可能就地自愈；
    - ``row_kind``：行类型（节流维度，当前仅 ``agent_tasks``）；
    - ``plaintext_policy``：见 ``PLAINTEXT_POLICIES``。
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    if not raw.startswith(ENVELOPE_PREFIX):
        return _handle_plaintext_row(raw, task_id=task_id, row_kind=row_kind,
                                     plaintext_policy=plaintext_policy)
    text = raw[len(ENVELOPE_PREFIX):]
    if text.lstrip().startswith("{"):
        # 开发模式（无 cryptography 库）信封内为明文 JSON（credential_crypto 降级）
        return _parse_env_json(text)
    from src.domains.mgmt.auth.credential_crypto import UnknownKeyError, decrypt
    try:
        return _parse_env_json(decrypt(text))
    except Exception as e:
        # Issue #4367 C：不再只是一条 WARNING + 空 env 静默降级——计数器让
        # 「env 被清空」可告警（deploy/alerts.yml GrapeEnvExtraDecryptFailing），
        # agent_tasks.errors 诊断槽记录 kid（结构化 code=env_extra_unknown_kid），
        # 供任务详情/追溯按行定位「哪条信封解不开、为什么」。
        _kid = getattr(e, "kid", "") or ""
        if not _kid:
            # 非 UnknownKeyError（密文损坏/库缺失等）异常不带 kid——从信封体补齐，
            # 保证计数器与诊断槽的 kid 维度一致可聚合。
            _kid = text.split(":", 1)[0]
        _code = ENV_EXTRA_UNKNOWN_KID if isinstance(e, UnknownKeyError) else ENV_EXTRA_DECRYPT_FAILED
        _report_decrypt_failure(_code, _kid, task_id=task_id, error=e)
        return {}
