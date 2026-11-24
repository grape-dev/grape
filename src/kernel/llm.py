"""平台 LLM 调用公共封装（Issue #3976）。

技能 AI 填写（`mgmt/routes/skills.py`）与 autopilot 指令润色（`mgmt/routes/autopilot.py`）
此前各自复制了一份 hermes `config.yaml` 读取 / `fallback_providers` 解析 / `urlopen`
调用逻辑。此处收敛为单一实现，调用方只负责传参并把异常映射为各自的 HTTP 状态码。

用法：
    from src.kernel.llm import LLMConfigError, LLMRequestError, chat_completion
    text = chat_completion(prompt, max_tokens=1200)

`force_model` 非空时固定模型（autopilot 润色走轻量模型）；否则用配置里的
`model.model`，缺失时回退 `DEFAULT_MODEL`。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "deepseek-v4.1-flash-1017"

# AI 填写默认调用参数（Issue #3976 QA B2 登记）。
# 五源登记：AI_FILL_MAX_TOKENS = <undecided: 需要真实 SKILL.md 样本上「摘要+描述+五槽」
#           输出的 token 分布与合法 JSON 率实测>，暂用 1200；
#           AI_FILL_TEMPERATURE = <undecided: 同输入多次输出字段可用率实测>，暂用 0.2；
#           AI_FILL_TIMEOUT_SECONDS = <undecided: 上游 p95/p99 时延实测并与前端/网关
#           超时对齐>，暂用 60。
# 均可用环境变量覆盖（GRAPE_AI_FILL_*）；此处为单一来源，调用方不得再各自硬编码。
# 与 autopilot 润色的 1000/0.3/30 是两套独立口径，互不混用。
AI_FILL_MAX_TOKENS = int(os.environ.get("GRAPE_AI_FILL_MAX_TOKENS", "1200"))
AI_FILL_TEMPERATURE = float(os.environ.get("GRAPE_AI_FILL_TEMPERATURE", "0.2"))
AI_FILL_TIMEOUT_SECONDS = int(os.environ.get("GRAPE_AI_FILL_TIMEOUT_SECONDS", "60"))

_log = logging.getLogger("grape.llm")


class LLMConfigError(RuntimeError):
    """hermes 配置缺失、不可读或字段不完整。"""


class LLMRequestError(RuntimeError):
    """LLM 上游调用失败（网络/响应解析）。"""


def load_hermes_config() -> dict:
    """读取 hermes config.yaml；读取失败抛 LLMConfigError。"""
    import yaml

    hermes_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    cfg_path = os.path.join(hermes_home, "config.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        raise LLMConfigError(f"无法读取 LLM 配置: {e}") from e


def _resolve_api_key(raw_key: str) -> str:
    raw_key = str(raw_key or "")
    if raw_key.startswith("${") and raw_key.endswith("}"):
        return os.environ.get(raw_key[2:-1], "")
    return raw_key


def resolve_llm_endpoint(hermes_cfg: dict, *, force_model: str = "") -> tuple[str, str, str]:
    """解析 LLM 端点，返回 (base_url, model, api_key)。

    主配置缺 api_key/base_url 时按 `fallback_providers` 逐项回退；主配置已有模型时
    保留主模型，仅主配置未指定模型才采用 fallback 的模型。`force_model` 非空则固定。
    """
    cfg = hermes_cfg if isinstance(hermes_cfg, dict) else {}
    model_cfg = cfg.get("model", {}) or {}
    base_url = str(model_cfg.get("base_url", "") or "").rstrip("/")
    cfg_model = str(model_cfg.get("model") or "")
    model_name = force_model or cfg_model or DEFAULT_MODEL
    api_key = _resolve_api_key(model_cfg.get("api_key", ""))

    if not api_key or not base_url:
        for fb in cfg.get("fallback_providers", []) or []:
            fb = fb or {}
            fb_url = str(fb.get("base_url", "") or "").rstrip("/")
            key_env = str(fb.get("key_env", "") or "")
            fb_key = os.environ.get(key_env, "") if key_env else str(fb.get("api_key", "") or "")
            fb_model = str(fb.get("model") or "")
            if not fb_url or not fb_key:
                continue
            base_url = base_url or fb_url
            api_key = api_key or fb_key
            if not force_model and not cfg_model and fb_model:
                model_name = fb_model
            break

    if not base_url or not api_key or not model_name:
        raise LLMConfigError("LLM 配置不完整（缺 base_url/api_key/model）")
    return base_url, model_name, api_key


def chat_completion(prompt: str, *, max_tokens: int = AI_FILL_MAX_TOKENS,
                    temperature: float = AI_FILL_TEMPERATURE,
                    timeout: int = AI_FILL_TIMEOUT_SECONDS,
                    force_model: str = "", caller: str = "") -> str:
    """调用 OpenAI 兼容的 chat/completions，返回文本内容（已 strip）。

    配置类失败抛 LLMConfigError；上游调用失败抛 LLMRequestError。
    `caller`：调用方标识（如 "skills.ai_fill"），仅用于接口日志归因，
    不参与请求。日志不含 api_key 与 prompt/正文内容（QA C2）。
    """
    hermes_cfg = load_hermes_config()
    base_url, model_name, api_key = resolve_llm_endpoint(hermes_cfg, force_model=force_model)

    body = json.dumps({
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
    }).encode("utf-8")
    req = urllib.request.Request(f"{base_url}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    _t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _log.warning(f"llm call failed caller={caller or '-'} model={model_name} "
                     f"duration_ms={int((time.monotonic() - _t0) * 1000)} "
                     f"class=http_status status={e.code}")
        raise LLMRequestError(f"HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        is_timeout = isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower()
        _log.warning(f"llm call failed caller={caller or '-'} model={model_name} "
                     f"duration_ms={int((time.monotonic() - _t0) * 1000)} "
                     f"class={'timeout' if is_timeout else 'network'} detail={type(reason).__name__}")
        raise LLMRequestError(str(e)) from e
    except LLMRequestError:
        raise
    except Exception as e:  # 响应非 JSON 等解析类失败
        _log.warning(f"llm call failed caller={caller or '-'} model={model_name} "
                     f"duration_ms={int((time.monotonic() - _t0) * 1000)} "
                     f"class=parse detail={type(e).__name__}")
        raise LLMRequestError(str(e)) from e
    out = ((data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "").strip()
    _log.info(f"llm call caller={caller or '-'} model={model_name} "
              f"duration_ms={int((time.monotonic() - _t0) * 1000)} "
              f"status={status} out_chars={len(out)}")
    return out


__all__ = [
    "AI_FILL_MAX_TOKENS",
    "AI_FILL_TEMPERATURE",
    "AI_FILL_TIMEOUT_SECONDS",
    "DEFAULT_MODEL",
    "LLMConfigError",
    "LLMRequestError",
    "chat_completion",
    "load_hermes_config",
    "resolve_llm_endpoint",
]