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
    if is_production():
        raise RuntimeError(
            "credential_keys 未配置且 JWT_SECRET 不足 32 字节：拒绝回落到内置 dev 密钥"
            "（公开常量，等价于明文存凭据）。请在 configs/security.json 配置 "
            "credential_keys=\"<kid>:<base64-32B>\"。"
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
