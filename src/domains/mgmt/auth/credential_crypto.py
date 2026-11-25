"""第三方凭据加密/解密（D10）。

AES-256-GCM 认证加密，密钥从环境变量读取，支持密钥轮换。

加密值存储格式：{key_id}:{nonce_b64}:{ciphertext_b64}
明文空字符串不加密，直接存储空字符串（无凭据的 OAuth 用户）。
"""
import base64
import os
import secrets
import threading

from src.kernel.config import log

# 密钥缓存
_keys: dict[str, bytes] = {}  # key_id → 32 字节密钥
_current_key_id: str = ""
_loaded = False
_load_lock = threading.Lock()


def _load_keys():
    """从 security_config 加载密钥，格式：kid1:key1,kid2:key2

    2026-11-21 修复两处（配合 security_config._load 的原子发布）：
    1. 一个有效密钥都没解析出来时**不 latch**（旧实现无条件置 _loaded=True，
       首个读到空配置的进程会永久写 dev 信封，且再也解不开 cred1，直到重启）；
    2. 生产环境密钥环只剩内置 dev 常量时直接抛错——那是公开常量，
       写出的信封在配了 credential_keys 的 worker 侧解不开，属于静默数据损坏。
    """
    global _loaded, _current_key_id
    if _loaded:
        return

    with _load_lock:
        if _loaded:
            return

        from src.kernel.security_config import forbid_dev_envelope_reason, get_credential_keys
        raw = get_credential_keys()

        parsed: dict[str, bytes] = {}
        first_kid = ""
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            kid, key_b64 = pair.split(":", 1)
            try:
                key = base64.b64decode(key_b64)
                if len(key) == 32:
                    parsed[kid] = key
                    if not first_kid:
                        first_kid = kid
            except Exception:
                log.warning(f"Invalid credential key for kid={kid}, skip")

        if not parsed:
            # 不 latch：允许下次调用重试（security_config._load 失败也不缓存了）
            log.error("credential key ring is EMPTY (no valid kid:base64-32B pair parsed) — "
                      "encrypt/decrypt would fall back to the built-in dev key. Not latching, "
                      "will retry on next call. 检查 configs/security.json 的 credential_keys。")
            return

        # Issue #4367 B：生产拒 dev 条件从 is_production() 扩展为禁令（判据叠加：
        # 目标库 DSN/env 命中 prod、进程在 prod 主机同样算）——即使 security.json
        # 缺失/误配让 is_production() 恒 False，连着生产库的进程也不得签发 dev 信封。
        _forbidden = forbid_dev_envelope_reason()
        if _forbidden and set(parsed) == {"dev"}:
            raise RuntimeError(
                "refusing built-in 'dev' credential key in production（公开常量，等价明文存凭据）"
                f"：生产判据命中：{_forbidden}。请在 configs/security.json 配置真实 "
                "credential_keys；应急放行需显式设置 GRAPE_ALLOW_DEV_ENVELOPE=1（Issue #4367 B）"
            )

        _keys.update(parsed)
        if not _current_key_id:
            _current_key_id = first_kid
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

    Issue #4367 B：生产判据命中（禁令，判据叠加不单看 is_production）时，
    拒绝明文降级与 kid=dev 信封签发——公开常量信封在生产库里是注定解不开的
    静默数据损坏（3,430 行存量的事故形态），必须让写入方立即可见地失败。
    """
    if not plaintext:
        return ""

    from src.kernel.security_config import forbid_dev_envelope_reason
    _forbidden = forbid_dev_envelope_reason()

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        # 生产环境（密钥非默认值）拒绝降级为明文
        if _forbidden:
            # Issue #4367 B：今日 9 行明文新数据的成因路径——security.json 缺失/误配
            # （is_production 恒 False）+ 无 cryptography → 凭据明文落库。
            raise RuntimeError(
                "cryptography library not installed and dev-envelope issuance is forbidden "
                f"(production signals: {_forbidden}): refusing plaintext fallback. "
                "Install cryptography and configure configs/security.json credential_keys; "
                "emergency override requires explicit GRAPE_ALLOW_DEV_ENVELOPE=1 (Issue #4367 B)."
            )
        from src.kernel.security_config import get_credential_keys
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
    if _forbidden and kid == "dev":
        # 密钥环显式配置了 dev kid（或混配环 dev 排首）时同样拒发——
        # 「当前签发密钥」就是信封 kid，此处是 dev 信封落库前的最后闸口。
        raise RuntimeError(
            "refusing to seal credentials with the built-in 'dev' key while dev-envelope "
            f"issuance is forbidden (production signals: {_forbidden}): the sealed rows "
            "would be undecryptable on credential-key-configured workers. Configure real "
            "credential_keys in configs/security.json; emergency override requires explicit "
            "GRAPE_ALLOW_DEV_ENVELOPE=1 (Issue #4367 B)."
        )
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return f"{kid}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ciphertext).decode()}"


def is_ciphertext(value: str) -> bool:
    """判断一个凭据值是否已是 `{kid}:{nonce_b64}:{ct_b64}` 形态的密文。

    只做形态判定（三段 + nonce 解出来正好 12 字节），不解密、不碰密钥池——
    迁移要判断"这行到底落没落明文"，而 decrypt 对明文是**原样返回**的，
    单看 decrypt 成功与否无法区分两者（D110 实锤：平台行被写进明文 PAT）。
    """
    if not value:
        return False
    parts = value.split(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[0].isprintable():
        return False
    try:
        nonce = base64.b64decode(parts[1], validate=True)
        base64.b64decode(parts[2], validate=True)
    except Exception:
        return False
    return len(nonce) == 12


class UnknownKeyError(ValueError):
    """密文的 kid 不在本进程密钥环里（多半是另一台主机/另一套密钥写的）。

    语义上不是“数据坏了”，而是“这枚钥匙不归我”：按契约视为不可用、回落下一档，
    不能升级成硬失败（2026-11-22 gitee rc 毒行事故的直接触发点）。
    """

    def __init__(self, kid: str):
        self.kid = kid
        super().__init__(f"credential key unknown for kid={kid}")


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
        raise UnknownKeyError(kid)

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
