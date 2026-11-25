"""安全配置统一读取模块（2026-11-18 从 mgmt.auth 迁入 kernel）。

迁移原因：本模块只读 configs/security.json，无 DB、无业务逻辑，属基础设施配置；
但 src/kernel/config.py 与 src/kernel/auth_sdk/jwt_utils.py 都需要它，
它留在 src/domains/mgmt/auth/ 会造成 kernel → domain 的反向依赖（架构底线）。

原始说明：安全配置统一读取模块。

从 configs/security.json 读取安全配置（环境标识、JWT 密钥轮换、凭据加密密钥）。
无 security.json 文件时全部使用默认值（开发模式）。

配置优先级：configs/security.json > 默认值

废弃的环境变量（不再读取）：
  APP_ENV / JWT_SECRET_KEYS / JWT_KID_CURRENT / GRAPE_CREDENTIAL_KEYS

保留的环境变量：
  JWT_SECRET — 基础 JWT 密钥（非本次需求新增，jwt_utils.py 直接读取）
"""
import os
import json
import hashlib
import base64
import logging
import re
import threading

# 不从 src.kernel.config 导入（避免循环导入——config.py 导入本模块的 is_production）
CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "configs"))
_SECURITY_CONFIG_PATH = os.path.join(CONFIG_DIR, "security.json")
_config = None
_load_lock = threading.Lock()
log = logging.getLogger("devops-bot")


def _load():
    """加载 security.json（模块级缓存，首次调用时读取文件）。

    并发安全（2026-11-21）：旧实现先把 ``{}`` 发布到全局 ``_config`` 再读文件，
    并发首读的后来者看到「已加载」就拿到空 dict → ``get_credential_keys()`` 返回空
    → credential_crypto 落到内置 ``dev`` 常量 → 任务信封 ``enc:v1:dev:...`` 在
    worker 侧解不开 → 空 env → 场景 work_branch 丢失 → 假「基线不可达」秒失败。
    现在改为：读文件到局部变量，读成功后再原子赋值；读失败不缓存（下次重试）。
    """
    global _config
    cfg = _config
    if cfg is not None:
        return cfg
    with _load_lock:
        if _config is not None:      # 双检：等锁期间别人已加载
            return _config
        loaded = {}
        try:
            if os.path.exists(_SECURITY_CONFIG_PATH):
                with open(_SECURITY_CONFIG_PATH, encoding="utf-8") as f:
                    loaded = json.load(f) or {}
        except Exception as e:
            log.warning(f"Failed to load security.json: {e}")
            return loaded            # 不发布空缓存，允许下次调用重试
        _config = loaded
        return _config


def reload():
    """热加载——配置文件修改后调用以重新读取。"""
    global _config
    _config = None
    return _load()


def get_env() -> str:
    """生产环境标识。security.json env 字段 > 默认 development。

    判定依据仅两个来源，均为 security.json：
    - 文件不存在（或空 {}）→ "development"（开发/测试）；
    - 文件存在且 env 字段为字面 "production" → "production"。
    其他写法（prod/test/`PRODUCTION`/空字符串）都不算生产。
    """
    cfg = _load()
    val = cfg.get("env", "").strip().lower()
    if val:
        return val
    return "development"


def is_production() -> bool:
    """是否生产环境。"""
    return get_env() == "production"


# ===== dev 信封签发禁令（Issue #4367 B：写入侧 fail-closed）=====
# 事故：security.json 缺失/env 误配让 is_production() 恒 False，两道「生产拒 dev」
# 守卫全部旁路 → 生产进程持续签发 kid=dev 信封（内置公开常量，等价弱密钥加密），
# 执行侧解不开 → 空 env 静默降级（每小时 52 个任务拿不到下发凭据）。
# 因此「是否允许签发 dev 信封」收敛为显式开关（默认关）+ 判据叠加：
# 不再单看 is_production()，目标库 DSN/env 命中 prod、进程在 prod 主机同样禁止。

#: 显式放行开关（应急用）：默认关。仅在生产判据命中时起作用——判据未命中（开发/
#: 测试）dev 信封照常可用，判据命中时必须显式置 1 才放行，不再有静默签发。
ALLOW_DEV_ENVELOPE_ENV = "GRAPE_ALLOW_DEV_ENVELOPE"

#: 显式 prod 主机标记（A 项运维口径）：部署单元在 prod 主机上启动时设置。
PROD_HOST_ENV = "GRAPE_HOST_ENV"

# prod 标记匹配：词边界内含 prod/production（大小写不敏感）。
# `grape_prod_runtime`、`prod-db-01`、`production` 命中；`productbox`/`reproduction`
# /`grape_dev_runtime` 不误伤（"prod" 后紧跟字母即不算）。
_PROD_MARKER_RE = re.compile(r"(?<![a-z0-9])prod(?:uction)?(?![a-z0-9])")


def _has_prod_marker(value: str) -> bool:
    return bool((value or "").strip()) and bool(_PROD_MARKER_RE.search(value.lower()))


def allow_dev_envelope() -> bool:
    """显式开关：是否允许签发 dev 信封（默认关，仅生产判据命中时生效）。"""
    return os.environ.get(ALLOW_DEV_ENVELOPE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _target_db_prod_hits() -> list[str]:
    """目标库判据：能写 agent_tasks.env_extra 的进程所连库的 DSN/env 命中 prod。

    覆盖单库（DEVOPS_DB_*）与三库拓扑（GRAPE_DSN_* / GRAPE_<域>_DB_NAME）两种部署；
    引擎侧无 security.json 的单元只要连着生产库（`grape_prod_runtime` 等）即可被识别，
    不依赖那份可能缺失/误配的 security.json——这正是 #4367 的实际事故形态。
    """
    hits = []
    for name in ("GRAPE_DSN_RUNTIME", "GRAPE_DSN_MGMT", "GRAPE_DSN_INSIGHT",
                 "DEVOPS_DB_NAME", "GRAPE_RUNTIME_DB_NAME", "DEVOPS_DB_HOST", "DEVOPS_DB_PATH"):
        val = (os.environ.get(name, "") or "").strip()
        if val and _has_prod_marker(val):
            hits.append(f"{name} 命中 prod")
    return hits


def _prod_host_hits() -> list[str]:
    """prod 主机判据：显式 GRAPE_HOST_ENV 标记，或引擎主机标识带 prod 段。"""
    hits = []
    marker = (os.environ.get(PROD_HOST_ENV, "") or "").strip().lower()
    if marker in ("prod", "production"):
        hits.append(f"{PROD_HOST_ENV}={marker}")
    host = ""
    try:
        from src.kernel.host_identity import engine_host
        host = (engine_host() or "").strip()
    except Exception:
        host = ""
    if host and _has_prod_marker(host):
        hits.append(f"host={host} 命中 prod")
    return hits


def dev_envelope_forbidden_reasons() -> list[str]:
    """生产判据收集（B：判据叠加，不单看 is_production()）。返回证据清单。"""
    reasons = []
    if is_production():
        reasons.append("security.json env=production")
    reasons += _target_db_prod_hits()
    reasons += _prod_host_hits()
    return reasons


def forbid_dev_envelope_reason() -> str:
    """dev 信封签发禁令（单一事实源）：命中任一生产判据且未显式放行 → 返回原因串；允许则返回空串。

    消费方：get_credential_keys 的 dev 常量回落、credential_crypto 的密钥环/明文降级/
    签发路径。语义见 ALLOW_DEV_ENVELOPE_ENV：判据命中时默认拒绝（fail-closed），
    显式置 GRAPE_ALLOW_DEV_ENVELOPE=1 才放行；开发/测试（判据未命中）行为不变。
    """
    if allow_dev_envelope():
        return ""
    return "; ".join(dev_envelope_forbidden_reasons())


def get_jwt_secret_keys() -> str:
    """JWT 密钥池。security.json jwt.secret_keys > 空（jwt_utils 内部 fallback 到 JWT_SECRET）。"""
    cfg = _load()
    val = cfg.get("jwt", {}).get("secret_keys", "").strip()
    return val


def get_jwt_kid_current() -> str:
    """当前签发密钥 kid。security.json jwt.kid_current > 空。"""
    cfg = _load()
    val = cfg.get("jwt", {}).get("kid_current", "").strip()
    return val


def get_credential_keys() -> str:
    """凭据加密密钥。security.json credential_keys > 从 JWT_SECRET 派生 > 默认测试密钥。

    Issue #3054 P1-3（只加可观测性，不改派生语义）：JWT_SECRET 兜底派生出的 kid 恒为
    ``derived``——两台主机 JWT_SECRET 不同时 kid 相同、密钥字节不同，密文跨主机解不开
    而 kid 对不上号，是"Decrypt failed: 空异常 + 静默回落 meta"的根因。兜底路径打一次
    WARNING，让"没配 credential_keys"在日志里可见。
    """
    cfg = _load()
    val = cfg.get("credential_keys", "").strip()
    if val:
        return val
    # 兜底：从 JWT_SECRET 派生 32 字节密钥
    jwt_secret = os.environ.get("JWT_SECRET", "")
    if jwt_secret and len(jwt_secret) >= 32:
        _warn_derived_keys_once()
        derived = hashlib.sha256(jwt_secret.encode()).digest()
        # kid 必须能区分密钥字节：两台主机 JWT_SECRET 不同时，旧 kid "derived" 相同、
        # 密钥不同 → 解不开却看不出原因（2026-11-22 gitee rc 毒行事故）。
        # 版本化后，异主机的行 kid 互不认识 → 归为 unknown kid → 视为不可用、回落下一档，
        # 而不是把一次配置差异升级成流水线硬失败。
        kid = "derived-" + derived.hex()[:8]
        return kid + ":" + base64.b64encode(derived).decode()
    # 兜底到内置 dev 常量：该密钥是公开常量，等于明文存凭据，且 worker 侧
    # （配有 credential_keys 的进程）根本解不开 → 任务信封全废。
    # 生产环境直接拒绝，让故障在入队时可见，而不是写下一条注定跑不起来的任务。
    # Issue #4367 B：拒绝条件从 is_production() 扩展为禁令（判据叠加：目标库
    # DSN/env 命中 prod、进程在 prod 主机同样算）——security.json 缺失/误配
    # 导致 is_production() 恒 False 时，连着生产库的进程不再静默回落 dev 常量。
    _forbidden = forbid_dev_envelope_reason()
    if _forbidden:
        raise RuntimeError(
            "credential_keys 未配置且 JWT_SECRET 不足 32 字节：拒绝回落到内置 dev 密钥"
            "（公开常量，等价于明文存凭据）。生产判据命中："
            f"{_forbidden}。请在 configs/security.json 配置 "
            "credential_keys=\"<kid>:<base64-32B>\"；应急放行需显式设置 "
            f"{ALLOW_DEV_ENVELOPE_ENV}=1（Issue #4367 B）。"
        )
    _warn_dev_keys_once()
    return "dev:MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


_derived_warned = False
_dev_warned = False


def _warn_derived_keys_once() -> None:
    global _derived_warned
    if _derived_warned:
        return
    _derived_warned = True
    log.warning("credential_keys 未配置：凭据密钥由 JWT_SECRET 派生（kid=derived-<8hex>，"
                "随密钥字节变化）。多主机 JWT_SECRET 不一致时异主机的行会归为 unknown kid，"
                "按「不可用 → 回落」处理；建议在 security.json 显式配置 credential_keys"
                "（kid:base64, ...）以便轮换。")


def _warn_dev_keys_once() -> None:
    global _dev_warned
    if _dev_warned:
        return
    _dev_warned = True
    log.error("credential_keys 未配置且无 JWT_SECRET：改用内置 dev 密钥（公开常量）。"
              "写到库里的 enc:v1:dev:* 信封，配置了 credential_keys 的 worker 解不开，"
              "任务会以空 env 运行（症状：秒级失败 workspace baseline mismatch / "
              "reason=head_unreachable）。请立即配置 configs/security.json 的 credential_keys。")
