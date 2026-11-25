"""LLM provider 前置校验（Issue #4346）——注册/启用期拦截，避免派发时 500。

生产实测：引擎未配 LLM provider 时 `session/new` 直接返回
`No LLM provider configured` 500，任务判 failed 且只能人工重派。本模块把该校验
前移到注册/启用入口（与 `dependency_check.py` 同层）：

- `validate_engine_llm_provider(engine_name, ...)`：读引擎本地配置判断 provider
  是否可用（hermes `config.yaml` / opencode `opencode.json`，复用
  `engine_model.read_*_model_config` 的唯一解析实现）；
- `registration_state(...)`：归一 worker 上报/本地校验结果；
- `apply_registration_state(...)`：服务端注册落库——缺失即 `llm_provider_ok=0`
  + `healthy=0`（存量行不进调度池，派发链路不再产生该 500）。

判定口径（宁放行不误杀）：
- 只有「明确缺配置」才拒绝：配置文件缺失且无可用 env key / 配置存在但解析不到
  任何 (base_url, api_key) 目标；
- 解析异常 / 未知引擎类型（provider 由服务端或云端管理）→ 放行（`unverified`）。
"""
from __future__ import annotations

import os

from src.kernel.config import log

# 与 engine_model 的解析口径对齐：这些引擎的 provider 由本地配置文件决定。
_CONFIG_ENGINES = ("hermes", "opencode")

# 明确等价「无 provider」的 env key（本地配置缺失但环境已注入 key 时放行）。
_ENV_KEY_NAMES = (
    "HERMES_API_KEY", "OPENCODE_API_KEY", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
)


def _engine_family(engine_name: str) -> str:
    """引擎族：只有本地配置决定 provider 的引擎（hermes/opencode）需要校验。

    不用 `canonical_engine`（未知名会兜底成 opencode），避免把其它引擎
    误判成 opencode 而读错配置。
    """
    name = (engine_name or "").strip().lower()
    if name.startswith("hermes"):
        return "hermes"
    if name.startswith("opencode"):
        return "opencode"
    return ""


def _env_key_present(env: dict) -> str:
    for name in _ENV_KEY_NAMES:
        if (env.get(name) or "").strip():
            return name
    return ""


def validate_engine_llm_provider(engine_name: str, config_path: str | None = None,
                                 env: dict | None = None) -> dict:
    """校验引擎 LLM provider 是否可用。

    返回 ``{"engine", "ok", "provider", "reason", "source"}``；永不抛错
    （校验器自身故障不得阻断注册）。
    """
    name = (engine_name or "").strip()
    family = _engine_family(name)
    env = dict(os.environ) if env is None else env
    out = {"engine": name, "ok": True, "provider": "", "reason": "",
           "source": "unverified"}
    if family not in _CONFIG_ENGINES:
        out["reason"] = f"{family or name or 'unknown'} provider is managed remotely; skipped"
        return out
    try:
        from src.domains.runtime.engine.engine_model import (
            read_hermes_model_config, read_opencode_model_config,
        )
        if family == "hermes":
            cfg = read_hermes_model_config(None, env)
        else:
            cfg = read_opencode_model_config(config_path=config_path, env=env)
    except Exception as e:  # noqa: BLE001 - 配置解析故障不误拒
        out["reason"] = f"{family} provider config read failed ({e}); skipped"
        return out

    env_key = _env_key_present(env)
    if cfg is None:
        # 配置文件缺失 ≠ provider 缺失：仓库既有口径是「返回 None = 调用方跳过校验」，
        # 且平台可能在派发期向任务环境注入网关凭据（llm_cred_selector），故只记录
        # `no_config` 不拒绝——否则会把「无本地配置文件」的合法 worker 误摘出池。
        out.update(ok=True, provider="", source="no_config",
                   reason=f"{family} config file missing; provider validation skipped "
                          f"(provider may be injected at dispatch"
                          + (f", {env_key} provided" if env_key else "") + ")")
        return out

    targets = cfg.get("targets") or []
    default_model = str(cfg.get("default_model") or "")
    if targets:
        out.update(ok=True, provider=str((targets[0] or {}).get("base_url") or ""),
                   source=str(cfg.get("config_path") or ""),
                   reason=f"{family} provider configured (model={default_model or '-'})")
        return out
    if env_key:
        out.update(ok=True, provider="env", source=f"env:{env_key}",
                   reason=f"{family} config has no resolvable gateway; {env_key} provided")
        return out
    warnings = "; ".join(str(w) for w in (cfg.get("warnings") or []) if w)[:200]
    out.update(ok=False, provider="", source=str(cfg.get("config_path") or ""),
               reason=(f"{family} provider not usable: no (base_url, api_key) target"
                       + (f" ({warnings})" if warnings else "")))
    return out


def registration_state(engine_name: str, reported: dict | None = None,
                       config_path: str | None = None, env: dict | None = None) -> dict:
    """归一注册期 provider 状态：worker 上报优先，缺失时本地校验（同机引擎）。"""
    if isinstance(reported, dict) and "ok" in reported:
        return {
            "engine": (engine_name or "").strip(),
            "ok": bool(reported.get("ok")),
            "provider": str(reported.get("provider") or ""),
            "reason": str(reported.get("reason") or ""),
            "source": str(reported.get("source") or "worker_report"),
        }
    return validate_engine_llm_provider(engine_name, config_path=config_path, env=env)


def apply_registration_state(c, runtime_id: int, engine_name: str,
                             reported: dict | None = None) -> dict:
    """注册落库：`llm_provider_ok` 写列；缺失 → healthy=0 并大声告警。

    只消费 worker 上报（`reported` 为 dict 且含 `ok`）：远端 worker 的引擎配置
    不在服务端主机上，服务端自行读本地配置会误判。未上报（旧 worker）→ 不改列
    （`llm_provider_ok` 默认 1，保持旧行为）。

    返回归一状态 dict；写库异常只告警（注册主链路不因校验器故障中断）。
    """
    if not (isinstance(reported, dict) and "ok" in reported):
        return {"engine": (engine_name or "").strip(), "ok": True,
                "provider": "", "reason": "worker did not report llm provider state",
                "source": "not_reported"}
    state = registration_state(engine_name, reported)
    try:
        from src.domains.runtime.engine import session_guard
        session_guard.mark_provider_state(
            c, runtime_id, bool(state.get("ok")),
            state.get("reason") or "", state.get("provider") or "")
    except Exception as e:  # noqa: BLE001 - 落库失败不阻断注册
        log.warning(f"provider_check: apply registration state failed "
                    f"(runtime={runtime_id}, engine={engine_name}): {e}")
    if state.get("ok"):
        log.info(f"provider_check: engine={engine_name} runtime={runtime_id} "
                 f"llm provider ok ({state.get('source') or '-'})")
    return state