#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OBS 凭据统一解析（**禁止**把 AK/SK 明文写进任何入库文件）。

背景（2026-10-21 审计）：release_pack.py / release_admin_pack.py / verify_obs_storage.py /
verify_obs_skill_e2e.py 曾把 Access Key 与 Secret Key 明文硬编码在脚本里，随开源清洗分支
（weekly-07-clean）与发布包一起外泄。现统一改为运行时解析，脚本内只留占位说明。

解析顺序：
  1) 环境变量（优先）
       - 发布桶（obs-grape-release）: OBS_RELEASE_AK / OBS_RELEASE_SK
       - 运行桶（obs-grape-file / obs-grape-dev-file）: OBS_ACCESS_KEY / OBS_SECRET_KEY
         （与 /etc/grape/grape.env 同名，便于主控机直接复用）
  2) 非入库配置文件
       - 发布桶: ${GRAPE_RELEASE_OBS_CONFIG:-/etc/grape/release-obs.json}
                 JSON: {"ak": "...", "sk": "..."}；也接受 /etc/grape/release-obs.env 的
                 KEY=VALUE 形式（OBS_RELEASE_AK=… / OBS_RELEASE_SK=…）
       - 运行桶: ${GRAPE_ENV_FILE:-/etc/grape/grape.env} 里的 OBS_* 键
  3) 都取不到 → 抛 RuntimeError 并说明如何提供（不静默降级、不使用占位值、
     绝不回退到"先上传再说"）

配套守卫：scripts/check-secrets.py（全仓明文密钥扫描，已接入 scripts/check-all.sh）。
"""
import json
import os

RELEASE_CONFIG = os.environ.get("GRAPE_RELEASE_OBS_CONFIG", "/etc/grape/release-obs.json")
RELEASE_ENV_FILE = os.environ.get("GRAPE_RELEASE_OBS_ENV", "/etc/grape/release-obs.env")
RUNTIME_ENV_FILE = os.environ.get("GRAPE_ENV_FILE", "/etc/grape/grape.env")


def _parse_env_file(path: str) -> dict:
    """解析 KEY=VALUE 形式的配置文件（容忍引号、注释、空行）。"""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return out


def _release_credentials() -> tuple:
    """取发布桶（obs-grape-release）凭据，缺则返回 ("", "")。"""
    ak = (os.environ.get("OBS_RELEASE_AK") or "").strip()
    sk = (os.environ.get("OBS_RELEASE_SK") or "").strip()
    if ak and sk:
        return ak, sk
    # JSON 配置
    try:
        with open(RELEASE_CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
        ak = ak or (data.get("ak") or data.get("access_key") or "").strip()
        sk = sk or (data.get("sk") or data.get("secret_key") or "").strip()
        if ak and sk:
            return ak, sk
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # env 文件形式
    env = _parse_env_file(RELEASE_ENV_FILE)
    ak = ak or (env.get("OBS_RELEASE_AK") or env.get("OBS_ACCESS_KEY") or "").strip()
    sk = sk or (env.get("OBS_RELEASE_SK") or env.get("OBS_SECRET_KEY") or "").strip()
    return ak, sk


def release_obs_credentials() -> tuple:
    """发布桶凭据；缺失时抛 RuntimeError（含提供方式）。"""
    ak, sk = _release_credentials()
    if not (ak and sk):
        raise RuntimeError(
            "缺少 obs-grape-release 上传凭据。请任选其一：\n"
            "  1) 导出环境变量 OBS_RELEASE_AK / OBS_RELEASE_SK；\n"
            f"  2) 提供非入库配置 {RELEASE_CONFIG}（JSON: {{\"ak\": \"...\", \"sk\": \"...\"}}）"
            f" 或 {RELEASE_ENV_FILE}（OBS_RELEASE_AK=… / OBS_RELEASE_SK=…）。\n"
            "禁止把 AK/SK 明文写回脚本或任何入库文件（见 scripts/check-secrets.py）。"
        )
    return ak, sk


def runtime_obs_env() -> dict:
    """运行桶 OBS_* 环境变量（供 verify_obs_* 脚本注入），缺 /etc/grape/grape.env 时用调用方已有变量。"""
    env = {}
    for key in ("OBS_ENABLED", "OBS_BUCKET", "OBS_ENDPOINT", "OBS_ACCESS_KEY",
                "OBS_SECRET_KEY", "OBS_PREFIX", "OBS_URL_EXPIRES"):
        v = (os.environ.get(key) or "").strip()
        if v:
            env[key] = v
    if env.get("OBS_ACCESS_KEY") and env.get("OBS_SECRET_KEY"):
        return env
    # 回退读主控机 grape.env（非入库文件）
    file_env = _parse_env_file(RUNTIME_ENV_FILE)
    for key, v in file_env.items():
        if key.startswith("OBS_"):
            env.setdefault(key, v)
    return env


def apply_runtime_obs_env() -> dict:
    """把解析到的运行桶配置写入 os.environ，返回生效值（缺凭据则抛 RuntimeError）。"""
    env = runtime_obs_env()
    os.environ.setdefault("OBS_ENABLED", "true")
    for k, v in env.items():
        os.environ[k] = v
    if not (os.environ.get("OBS_ACCESS_KEY") and os.environ.get("OBS_SECRET_KEY")):
        raise RuntimeError(
            "缺少 OBS 运行桶凭据。请导出 OBS_ACCESS_KEY / OBS_SECRET_KEY，"
            f"或提供 {RUNTIME_ENV_FILE}（OBS_ACCESS_KEY=… / OBS_SECRET_KEY=…）。"
        )
    return env