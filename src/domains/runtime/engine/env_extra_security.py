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
- 读侧命中明文行**就地自愈**（重新加密后条件 UPDATE 回写，`AND env_extra NOT LIKE 'enc:v1:%'`
  保证幂等/并发安全）；自愈失败或不可自愈（无行 id / 无 runtime 库）才告警；
- 告警按 `(host, 行类型)` 节流——每进程每小时 ≤1 条，超限只计数；计数器
  `grape_env_extra_plaintext_rows_total{outcome}` 承担「不再刷屏但仍可见」；
- 文案指向真实 backfill `202611220003`（旧文案的 `202611130001` 是无关迁移），日志只输出
  键名/行 id，绝不输出值。
"""
from __future__ import annotations

import json
import os
import threading
import time

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
    except Exception:
        pass


def record_env_extra_write_refused() -> None:
    """写入侧加密失败（拒绝明文回落，任务判失败）计数钩子。"""
    _record_plaintext("write_refused")


def record_env_extra_plaintext(outcome: str) -> None:
    """明文行事件计数（公开入口：迁移等非读路径复用同一计数器，口径可对账）。"""
    _record_plaintext(outcome)


def heal_env_extra_plaintext_row(row_id: str, sealed: str, *,
                                 row_kind: str = "agent_tasks") -> bool:
    """读侧就地自愈：把存量明文行改写为 `sealed`（enc:v1 信封）。

    幂等/并发安全：UPDATE 带 `AND env_extra NOT LIKE 'enc:v1:%'`——已被别的路径加密的行
    不会被二次覆盖（返回 False）。返回是否真正改写了该行。异常由调用方捕获（自愈失败
    只影响观测，不影响读 env）。

    连接用 owner 域代理（`owner_db("runtime")`）：四进程拆分下 agent_tasks 归 runtime 域，
    单库拓扑自动回落进程库。
    """
    kind = (row_kind or "agent_tasks").strip() or "agent_tasks"
    if not row_id or not sealed or kind not in PLAINTEXT_ROW_KINDS:
        return False
    from src.kernel.owner_db import db_pair
    db, db_lock = db_pair("runtime")
    with db_lock:
        cur = db.cursor()
        cur.execute(
            "UPDATE agent_tasks SET env_extra=? WHERE id=? "
            "AND env_extra IS NOT NULL AND env_extra<>'' "
            "AND env_extra NOT LIKE 'enc:v1:%'",
            (sealed, row_id))
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
        err = ""
        sealed = ""
        try:
            sealed = encrypt_env_extra(env)
        except Exception as e:      # 只留异常类型：告警不得带出值/密钥细节
            err = type(e).__name__
        if sealed:
            try:
                heal_env_extra_plaintext_row(task_id, sealed, row_kind=kind)
                # 命中或已被并发路径加密（幂等 UPDATE 未命中）都算收敛，一次读只记一次 healed
                _record_plaintext("healed")
                return env
            except Exception as e:
                err = type(e).__name__
        _plaintext_report("heal_failed", task_id=task_id, kind=kind, env=env, err=err)
        return env
    # report 策略 / 无行 id（无 DB 写通道）：只计数 + 节流告警
    _plaintext_report("heal_failed", task_id=task_id, kind=kind, env=env)
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
    from src.domains.mgmt.auth.credential_crypto import decrypt
    try:
        return _parse_env_json(decrypt(text))
    except Exception as e:
        log.warning(f"env_extra 解密失败（按空 env 处理，任务凭据需重新下发）: {e}")
        return {}
