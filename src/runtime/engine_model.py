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

_MAX_MODEL_LEN = 120


def _sanitize(value) -> str:
    v = str(value or "").strip()
    return v[:_MAX_MODEL_LEN]


def _hermes_model() -> str:
    home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    path = os.path.join(home, "config.yaml")
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
    path = os.path.expanduser("~/.config/opencode/opencode.json")
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
    path = os.path.expanduser("~/.codeartsdoer/codearts_cli.json")
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


def detect_engine_model(name: str) -> str:
    """按引擎名解析本机配置的模型名；未知名/失败一律返回空串（降级不影响注册）。"""
    key = str(name or "").strip().lower()
    fn = _PARSERS.get(key)
    if not fn:
        return ""
    try:
        return fn()
    except Exception:
        return ""
