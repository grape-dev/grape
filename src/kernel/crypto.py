"""AES-256-GCM 加密/解密工具——用于 grape_t_user_llm_credentials.api_key_enc。

AES-256-GCM 是 AEAD（认证加密）——加密和认证一体化，比 CBC + HMAC 更安全更高效。
密钥从环境变量 GRAPE_DB_ENCRYPTION_KEY 读取（32 字节 base64url 编码）。
密文格式：base64(nonce(12B) + ciphertext + tag(16B))。
nonce 每次加密随机生成——同一明文每次密文不同。
"""
from __future__ import annotations

import os
import base64
import logging

log = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None
    log.warning("cryptography package not installed — LLM credential encryption unavailable")

_aesgcm = None  # type: ignore

# configs/ 目录（crypto.py 位于 src/kernel/，上溯三级到仓根）
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "configs")


def _get_aesgcm():
    global _aesgcm
    if _aesgcm is None:
        if AESGCM is None:
            raise RuntimeError("cryptography package not installed")
        # 优先环境变量，其次配置文件 configs/crypto.json
        key_b64 = os.environ.get("GRAPE_DB_ENCRYPTION_KEY", "")
        if not key_b64:
            try:
                import json as _json
                _crypto_path = os.path.join(_CONFIG_DIR, "crypto.json")
                if os.path.isfile(_crypto_path):
                    # 权限检查：配置文件含明文密钥，必须 0600
                    _st = os.stat(_crypto_path)
                    if _st.st_mode & 0o077:
                        raise PermissionError(f"crypto.json permissions too open ({oct(_st.st_mode & 0o777)}), must be 0600")
                    with open(_crypto_path, "r") as f:
                        _cfg = _json.load(f)
                    key_b64 = _cfg.get("encryption_key", "")
            except PermissionError:
                raise  # fail-closed——权限过宽的 crypto.json 不允许加载
            except Exception as _e:
                # 文件在、但读不出来（JSON 损坏/权限不足/IO 错误）要与"根本没配"区分开，
                # 否则 key_status 会把运维引向"没配"而不是"配坏了"。
                raise RuntimeError(f"crypto.json unreadable ({_CONFIG_DIR}): {_e}") from _e
        if not key_b64:
            raise RuntimeError("GRAPE_DB_ENCRYPTION_KEY not set (env or configs/crypto.json)")
        # key 是 base64url 编码的 32 字节（256 位）密钥
        try:
            key = base64.urlsafe_b64decode(key_b64)
        except Exception as _e:
            raise RuntimeError(f"GRAPE_DB_ENCRYPTION_KEY is not valid base64url: {_e}") from _e
        if len(key) != 32:
            raise RuntimeError(f"GRAPE_DB_ENCRYPTION_KEY must be 32 bytes (256-bit), got {len(key)}")
        _aesgcm = AESGCM(key)
    return _aesgcm


def encrypt(plaintext: str) -> str:
    """AES-256-GCM 加密，返回 base64(nonce + ciphertext + tag)。"""
    import os as _os
    aesgcm = _get_aesgcm()
    nonce = _os.urandom(12)  # 96-bit nonce，每次随机
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt(ciphertext: str) -> str:
    """AES-256-GCM 解密，返回明文。

    密钥轮换后旧密文无法解密会抛 InvalidTag——调用方应捕获并处理
    （如提示重新配置 LLM 凭据），不应静默吞掉导致 Key 为空。
    """
    aesgcm = _get_aesgcm()
    raw = base64.urlsafe_b64decode(ciphertext)
    nonce = raw[:12]
    ct = raw[12:]
    try:
        return aesgcm.decrypt(nonce, ct, None).decode()
    except Exception as e:
        # InvalidTag: 密钥不匹配或密文损坏（密钥轮换后旧密文会触发）
        from cryptography.exceptions import InvalidTag as _InvalidTag
        if isinstance(e, _InvalidTag):
            raise ValueError(f"decrypt failed: ciphertext integrity check failed (key mismatch or corrupted data)") from e
        raise


def generate_key() -> str:
    """生成新密钥（运维部署时用）。返回 base64url 编码的 32 字节。"""
    import os as _os
    return base64.urlsafe_b64encode(_os.urandom(32)).decode()


def crypto_config_path() -> str:
    """crypto.json 的期望路径。

    供启动自检把"该配哪个文件"告诉运维；路径按运行时部署根目录拼出，
    源码里不写死部署绝对路径（仓库有脱敏门禁会拦）。
    """
    return os.path.join(_CONFIG_DIR, "crypto.json")


def key_status() -> dict:
    """探测数据库加密密钥是否可用（不抛异常，供启动自检/健康检查用）。

    返回 {configured, source, error}：
      configured=True  → source 为 "env" 或 "file"，error 为空
      configured=False → error 说明缺什么（缺失/长度不对/权限过宽/依赖缺失）

    与 encrypt/decrypt 共用 _get_aesgcm 的同一套加载规则，避免"自检通过但
    真正加密时失败"的口径分裂。
    """
    import os as _os
    status = {"configured": False, "source": "", "error": ""}
    if AESGCM is None:
        status["error"] = "cryptography package not installed"
        return status
    if _os.environ.get("GRAPE_DB_ENCRYPTION_KEY", ""):
        status["source"] = "env"
    else:
        _crypto_path = _os.path.join(_CONFIG_DIR, "crypto.json")
        if _os.path.isfile(_crypto_path):
            status["source"] = "file"
    try:
        _get_aesgcm()
    except Exception as e:  # noqa: BLE001 — 自检要把原因透出，不当异常往上抛
        status["error"] = str(e)
        status["configured"] = False
        return status
    status["configured"] = True
    return status
