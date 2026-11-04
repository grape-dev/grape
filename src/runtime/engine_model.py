"""引擎本机配置模型探测（Issue #865）。

供 worker（注册上报）与服务端（本机直连引擎兜底）共用：
- hermes:   ~/.hermes/config.yaml（HERMES_HOME 可覆盖）→ model.default
- opencode: ~/.config/opencode/opencode.json → model（"provider/modelid"）
- codearts: CODEARTS_MODEL 环境变量优先 → ~/.codeartsdoer/codearts_cli.json → model

安全约束（Issue #865 Spec）：只返回模型名字符串；
base_url / api_key / 环境变量取值绝不进入返回值。
任何解析失败（文件缺失/格式异常/依赖缺失）一律返回空串，
不影响注册/心跳主链路（降级原则）。
"""
from __future__ import annotations

import json
import os
import re
import time

_MAX_MODEL_LEN = 120


def _sanitize(value) -> str:
    v = str(value or "").strip()
    return v[:_MAX_MODEL_LEN]


def _hermes_path() -> str:
    return os.path.join(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")), "config.yaml")


def _opencode_path() -> str:
    return os.path.expanduser("~/.config/opencode/opencode.json")


def _codearts_path() -> str:
    return os.path.expanduser("~/.codeartsdoer/codearts_cli.json")


def _hermes_model() -> str:
    path = _hermes_path()
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    # worker 环境可能无 pyyaml（requirements-worker.txt 未含）——正则兜底：
    # 只在 model: 段内取 default: 值，不碰 api_key/base_url。
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(raw) or {}
        model_cfg = cfg.get("model") or {}
        if isinstance(model_cfg, dict):
            return _sanitize(model_cfg.get("default")
                             or model_cfg.get("model")
                             or model_cfg.get("name"))
        if isinstance(model_cfg, str):
            return _sanitize(model_cfg)
    except Exception:
        pass
    m = re.search(r"^\s*model:\s*\n(?:(?!\n\w).)*?^\s+default:\s*([^\s#]+)",
                  raw, re.MULTILINE | re.DOTALL)
    return _sanitize(m.group(1)) if m else ""


def _opencode_model() -> str:
    path = _opencode_path()
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return _sanitize(cfg.get("model"))


def _codearts_model() -> str:
    # CODEARTS_MODEL 显式覆盖优先（与 runtime/codearts.py build_args 口径一致）
    env_model = _sanitize(os.environ.get("CODEARTS_MODEL"))
    if env_model:
        return env_model
    path = _codearts_path()
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return _sanitize(cfg.get("model"))


_PARSERS = {
    "hermes": _hermes_model,
    "opencode": _opencode_model,
    "codearts": _codearts_model,
}


# 性能（2026-11-03）：本机配置解析实测 5.9ms/次（yaml.safe_load 占 5.83ms），而引擎页
# 每次请求都会为"未上报的本机引擎"现算一次。按**文件指纹**（路径+mtime+大小+相关环境变量）
# 缓存：配置一改立刻失效重算，指纹不变时 60s 内直接命中（体积防护，不必每分钟重解析）。
_MODEL_CACHE_TTL = 60.0
_model_cache: dict[str, tuple[tuple, float, str]] = {}

_CONFIG_PATHS = {
    "hermes": _hermes_path,
    "opencode": _opencode_path,
    "codearts": _codearts_path,
}


def _config_signature(name: str) -> tuple:
    """配置指纹：文件 (路径, mtime_ns, size) + 会覆盖配置的环境变量取值。"""
    sig = []
    fn = _CONFIG_PATHS.get(name)
    if fn:
        path = fn()
        try:
            st = os.stat(path)
            sig.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((path, 0, 0))
    if name == "codearts":
        sig.append(os.environ.get("CODEARTS_MODEL", ""))
    return tuple(sig)


def detect_engine_model(name: str) -> str:
    """按引擎名解析本机配置的模型名；未知名/失败一律返回空串（降级不影响注册）。"""
    key = str(name or "").strip().lower()
    fn = _PARSERS.get(key)
    if not fn:
        return ""
    sig = _config_signature(key)
    now = time.monotonic()
    hit = _model_cache.get(key)
    if hit and hit[0] == sig and now - hit[1] < _MODEL_CACHE_TTL:
        return hit[2]
    try:
        val = fn()
    except Exception:
        val = ""
    _model_cache[key] = (sig, now, val)
    return val
