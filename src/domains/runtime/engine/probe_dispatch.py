"""引擎探活单点分派器（P0b B5，2026-11-13 用户拍板「探活两档保留裁定」）。

合一收「分派知识」不收「探活档位」——本模块是深探（probe_deep）的唯 一
名字分支消解点；浅探参数经 registry.light_probe_args 由 local_probe 消费。

两入口语义：
- probe_deep(engine_name, cli_path)：真实会话级探活（ACP 握手 / CLI 测试
  对话）。worker 与 insight 的 10 分钟循环都走这里——原两侧各持一份名字
  分支且互相漂移（worker 缺 atomcode、insight 缺 dsh/cursor/hwcloud，
  互补缺口实证），现单点派发零漂移。
- 浅探参数（light_args）不在此执行：local_probe 保留自己的线程池/超时
  循环策略（循环策略非探活知识），仅从 descriptor 取参数。

派发数据源 = engine descriptor probe 字段：
- probe.mode=acp_handshake → 按 probe.impl 从 _ACP_IMPLS 取实现（引擎
  quirk 代码留在实现本体，token 声明在 descriptor——新引擎复用现有 impl
  或新增实现+token，两档配置同写一份 JSON，drift 守卫校验齐备性）。
- probe.mode=cli_run → 按 probe.command 跑测试对话。
- probe.env_hook → codearts folder-trust 数据目录 quirk 等显式钩子。

行为注记（合一语义对齐）：insight 侧原自带 hermes 2 步浅版探活副本已删，
统一走 worker 侧 agent 实现（3 步全真：initialize + session/new + prompt，
Issue #441 口径）——与 aishell-dsh「insight import agent 实现」单源先例
一致；insight 侧 hwcloud 探活缺口（原落命令表报 skip）随派发自然修复。

参考: docs/todo/2026-11-13-p0-引擎接入解耦实施回执.md「探活两档保留裁定」；
实现单源先例 src/domains/runtime/engine/agent.py:_probe_aishell_dsh_acp。
"""
from __future__ import annotations

import subprocess
import time

from src.kernel.config import log

_PROBE_TIMEOUT = 30  # cli_run 测试对话超时（秒），沿 worker 既有值

# ACP 探活实现表：token → (模块, 函数)。实现留在 worker 侧 agent（含
# Windows 兼容层），本表只做 token → 符号的声明式映射。
_ACP_IMPLS: dict[str, tuple[str, str]] = {
    "hermes_acp": ("src.domains.runtime.engine.agent", "_probe_hermes_acp"),
    "hwcloud_acp": ("src.domains.runtime.engine.agent", "_probe_hwcloud_acp"),
    "aishell_dsh_acp": ("src.domains.runtime.engine.agent", "_probe_aishell_dsh_acp"),
}

# 显式 env 钩子表：token → (模块, 函数)，返回进程环境增量 dict。
_ENV_HOOKS: dict[str, tuple[str, str]] = {
    # Issue #471 C2：codearts 探测走专用数据目录（folder-trust 闸门），
    # 探测进程无 TTY，未信任 cwd 会 exit 1 误报不健康。
    "codearts_probe_env": ("src.domains.runtime.engine.codearts", "probe_env"),
}


def _load_hook(table: dict, token: str):
    import importlib
    mod_name, fn_name = table[token]
    mod = importlib.import_module(mod_name)
    return getattr(mod, fn_name)


def probe_deep(engine_name: str, cli_path: str = "") -> dict:
    """深探单点分派：按 descriptor probe.mode/impl/command 派发，无名字分支。

    返回口径沿既有 worker/insight 结果 dict：{status: ok|fail|skip,
    latency, error[, output]}。
    """
    from src.domains.runtime.engine.descriptor import get_registry
    d = get_registry().get(engine_name)
    if d is None:
        return {"status": "skip", "latency": 0, "error": f"unknown engine: {engine_name}"}
    probe = d["probe"]
    mode = probe.get("mode")

    if mode == "acp_handshake":
        token = probe.get("impl")
        if not token or token not in _ACP_IMPLS:
            return {"status": "fail", "latency": 0,
                    "error": f"probe.mode=acp_handshake 但 impl 未注册: {token!r}"}
        fn = _load_hook(_ACP_IMPLS, token)
        return fn(cli_path)

    if mode == "none":
        return {"status": "skip", "latency": 0, "error": "probe.mode=none"}

    # Issue #3820: opencode deep-probe precheck - minimal model call
    # (max_tokens=1). Non-empty model_err means the configured model name is
    # not in the gateway allowlist: fail fast with the diff text so the
    # runtimes page probe_error surfaces it before a real task hits 404.
    # (Ported from the former agent.py:_probe_engine_worker precheck when the
    # B5 dispatch merge moved deep-probe knowledge into this module.)
    if engine_name.startswith("opencode"):
        model_err = _opencode_model_probe_error()
        if model_err:
            return {"status": "fail", "latency": 0, "error": model_err}

    # mode == "cli_run"
    cmd = list(probe["command"])
    if cli_path:
        cmd = [cli_path] + cmd[1:]
    env = None
    hook_token = probe.get("env_hook")
    if hook_token:
        if hook_token not in _ENV_HOOKS:
            return {"status": "fail", "latency": 0,
                    "error": f"probe.env_hook 未注册: {hook_token!r}"}
        import os
        try:
            hook = _load_hook(_ENV_HOOKS, hook_token)
            env = {**os.environ, **hook(os.getcwd())}
        except Exception as e:
            log.warning(f"{engine_name} probe env hook failed: {e}")
    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT, stdin=subprocess.DEVNULL, env=env,
        )
        latency = round(time.time() - start, 1)
        if result.returncode == 0:
            output = (result.stdout or "").strip()[:200]
            return {"status": "ok", "latency": latency, "error": "", "output": output}
        err = (result.stderr or result.stdout or "").strip()[:200]
        return {"status": "fail", "latency": latency, "error": f"exit={result.returncode}: {err}"}
    except subprocess.TimeoutExpired:
        return {"status": "fail", "latency": round(time.time() - start, 1),
                "error": f"timeout ({_PROBE_TIMEOUT}s)"}
    except FileNotFoundError:
        return {"status": "fail", "latency": 0, "error": "CLI not found"}
    except Exception as e:
        return {"status": "fail", "latency": round(time.time() - start, 1), "error": str(e)[:200]}

def _opencode_model_probe_error() -> str:
    """opencode deep-probe precheck: minimal model call (Issue #3820).

    Returns non-empty when the configured model name is missing from the
    gateway allowlist (includes diff text); empty when ok or unverifiable
    (never let a probe failure take down other engines).
    """
    try:
        from src.domains.runtime.engine import engine_model
        ok, detail, error = engine_model.probe_engine_model_call("opencode")
        if ok:
            return ""
        return error or str(detail.get("model_call") or "model config error")
    except Exception as e:  # noqa: BLE001
        log.warning(f"opencode model probe failed: {e}")
        return ""
