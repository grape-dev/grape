"""第三方凭据加密/解密（D10）。

AES-256-GCM 认证加密，密钥从环境变量读取，支持密钥轮换。

加密值存储格式：{key_id}:{nonce_b64}:{ciphertext_b64}
明文空字符串不加密，直接存储空字符串（无凭据的 OAuth 用户）。
"""
import base64
import os
import secrets

from src.config import log

# 密钥缓存
_keys: dict[str, bytes] = {}  # key_id → 32 字节密钥
_current_key_id: str = ""
_loaded = False


def _load_keys():
    """从 security_config 加载密钥，格式：kid1:key1,kid2:key2"""
    global _loaded, _current_key_id
    if _loaded:
        return

    from src.auth.security_config import get_credential_keys
    raw = get_credential_keys()

    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        kid, key_b64 = pair.split(":", 1)
        try:
            key = base64.b64decode(key_b64)
            if len(key) == 32:
                _keys[kid] = key
                if not _current_key_id:
                    _current_key_id = kid
        except Exception:
            log.warning(f"Invalid credential key for kid={kid}, skip")

    _loaded = True


def _get_current_key() -> tuple[str, bytes]:
    """获取当前签发密钥：(key_id, key_bytes)。"""
    _load_keys()
    if not _keys:
        raise RuntimeError("No credential key configured")
    return _current_key_id, _keys[_current_key_id]


def _get_key_by_id(key_id: str) -> bytes | None:
    """按 key_id 查找密钥（用于解密旧密钥加密的数据）。"""
    _load_keys()
    return _keys.get(key_id)


def encrypt(plaintext: str) -> str:
    """加密明文，返回 {key_id}:{nonce_b64}:{ciphertext_b64}。

    明文空字符串直接返回空字符串，不加密。

    OMIT-2 修复：生产环境（GRAPE_CREDENTIAL_KEYS 已设置）要求 cryptography 库必须安装，
    未安装时抛 RuntimeError 拒绝启动，而非降级为明文存储。
    """
    if not plaintext:
        return ""

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        # 生产环境（密钥非默认值）拒绝降级为明文
        from src.auth.security_config import get_credential_keys
        _keys_raw = get_credential_keys()
        if _keys_raw and not _keys_raw.startswith("dev:"):
            raise RuntimeError(
                "cryptography library not installed but credential_keys is configured. "
                "Install cryptography for production mode."
            )
        # 开发环境：降级为明文存储
        log.warning("cryptography library not installed, storing plaintext (dev mode)")
        return plaintext

    kid, key = _get_current_key()
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return f"{kid}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ciphertext).decode()}"


def decrypt(encrypted: str) -> str:
    """解密 {key_id}:{nonce_b64}:{ciphertext_b64}，返回明文。

    明文空字符串直接返回空字符串。
    兼容明文：无冒号分隔符的值视为明文返回（迁移期）。

    D46 P1-10: 解密失败时抛 ValueError 而非静默返回密文，
    让调用方明确知道解密失败。key_id 不存在也抛异常。
    """
    if not encrypted:
        return ""

    # 兼容明文（无冒号）：迁移期降级
    parts = encrypted.split(":", 2)
    if len(parts) != 3:
        return encrypted

    kid, nonce_b64, ct_b64 = parts
    key = _get_key_by_id(kid)
    if key is None:
        # D46 P1-10: key_id 不存在 → 抛异常，不静默返回密文
        raise ValueError(f"Credential key not found for kid={kid}")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = base64.b64decode(nonce_b64)
        ciphertext = base64.b64decode(ct_b64)
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode()
    except ImportError:
        raise RuntimeError("cryptography library not installed, cannot decrypt")
    except ValueError:
        raise  # 透传 key_id 不存在的 ValueError
    except Exception as e:
        raise ValueError(f"Decrypt failed: {e}")
