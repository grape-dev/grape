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
"""
from __future__ import annotations

import json
import os

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


def decrypt_env_extra(raw) -> dict:
    """env_extra 回读：enc:v1 信封解密；存量明文 JSON 直接解析（告警）；坏值 → {}。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    text = raw
    if text.startswith(ENVELOPE_PREFIX):
        body = text[len(ENVELOPE_PREFIX):]
        if body.lstrip().startswith("{"):
            # 开发模式（无 cryptography 库）信封内为明文 JSON（credential_crypto 降级）
            text = body
        else:
            from src.domains.mgmt.auth.credential_crypto import decrypt
            try:
                text = decrypt(body)
            except Exception as e:
                log.warning(f"env_extra 解密失败（按空 env 处理，任务凭据需重新下发）: {e}")
                return {}
    else:
        log.warning("env_extra: 存量明文落库行（C3）——请运行 202611130001 迁移加密，"
                    "或升级后重启使启动迁移生效")
    try:
        env = json.loads(text)
    except Exception:
        return {}
    return env if isinstance(env, dict) else {}
