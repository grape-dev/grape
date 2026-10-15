"""安全配置统一读取模块。

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

# 不从 src.config 导入（避免循环导入——config.py 导入本模块的 is_production）
CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "configs"))
_SECURITY_CONFIG_PATH = os.path.join(CONFIG_DIR, "security.json")
_config = None
log = logging.getLogger("devops-bot")


def _load():
    """加载 security.json（模块级缓存，首次调用时读取文件）。"""
    global _config
    if _config is not None:
        return _config
    _config = {}
    try:
        if os.path.exists(_SECURITY_CONFIG_PATH):
            with open(_SECURITY_CONFIG_PATH, encoding="utf-8") as f:
                _config = json.load(f) or {}
    except Exception as e:
        log.warning(f"Failed to load security.json: {e}")
    return _config


def reload():
    """热加载——配置文件修改后调用以重新读取。"""
    global _config
    _config = None
    return _load()


def get_env() -> str:
    """生产环境标识。security.json env 字段 > 默认 development。"""
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
    """凭据加密密钥。security.json credential_keys > 从 JWT_SECRET 派生 > 默认测试密钥。"""
    cfg = _load()
    val = cfg.get("credential_keys", "").strip()
    if val:
        return val
    # 兜底：从 JWT_SECRET 派生 32 字节密钥
    jwt_secret = os.environ.get("JWT_SECRET", "")
    if jwt_secret and len(jwt_secret) >= 32:
        derived = hashlib.sha256(jwt_secret.encode()).digest()
        return "derived:" + base64.b64encode(derived).decode()
    return "dev:MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
