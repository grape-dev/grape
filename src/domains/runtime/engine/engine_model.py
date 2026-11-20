"""hermes 引擎模型名统一读取与网关对齐校验（Issue #2545）。

hermes chat 的 `HTTP 404: Model not found` 根因是 config.yaml 配置的模型名
不在 API key 所属网关的可用列表（/v1/models）中。本模块把「模型名来源」收敛为
单一实现，供三处共用（Linux/Windows 行为一致，Spec 决策「统一复用 engine_model.py」）：

  1. 安装期校验：install-worker.sh / install-worker.ps1 调
     `python -m src.runtime.engine_model --check`，模型名不在可用列表 → exit 1
     并打印对照；网关不可达降级为告警（exit 0，不误伤离线安装）。
  2. 探活期最小调用：local_probe.py 在 ACP 握手后调 minimal_model_call()
     发 1-token 补全，404/模型不存在 → healthy=False + 原因。
  3. 部署自检：运维按 docs/deployment/model-gateway-alignment.md 手动执行 --check。

依赖约束：worker 侧 venv 仅 stdlib + aiohttp/click/httpx（无 pyyaml），故 config.yaml
解析优先用 pyyaml（服务端可用），缺失时走内置降级解析器（覆盖 hermes config.yaml
实际使用的子集：嵌套映射 / 标量 / 内联 {k: v} 流映射）；解析异常按「无法校验」告警
而非误杀（Spec 风险缓解：解析容错）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

__all__ = [
    "read_hermes_model_config",
    "parse_hermes_config",
    "check_models_against_gateway",
    "minimal_model_call",
    "main",
]

try:
    import yaml as _yaml  # 服务端可用时优先；worker venv 缺失时走降级解析器
except ImportError:
    _yaml = None

ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
HERMES_HOME_ENV = "HERMES_HOME"
GATEWAY_TIMEOUT = 8          # /v1/models 与最小调用超时（秒），短超时防拖慢安装
GATEWAY_RETRIES = 1          # 网络失败重试一次（Spec：失败可重试一次）


# ===== config.yaml 解析 =====

def _strip_comment(line: str) -> str:
    out, quote = [], ""
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _split_flow_items(s: str) -> list:
    """按顶层逗号切分流映射内各项：{a: 1, b: {c: 2}} → ["a: 1", "b: {c: 2}"]。"""
    items, depth, quote, cur = [], 0, "", []
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return items


def _split_key_value(s: str) -> tuple[str, str]:
    """取第一个不在引号/花括号内的冒号作为 k:v 分隔（值内含 `https://` 冒号时不受影响）。"""
    depth, quote = 0, ""
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ":" and depth == 0:
            return s[:i].strip(), s[i + 1:].strip()
    return "", ""


def _parse_scalar_or_flow(s: str):
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        result = {}
        if not inner:
            return result
        for item in _split_flow_items(inner):
            k, v = _split_key_value(item)
            if not k:
                raise ValueError(f"cannot parse flow mapping item: {item!r}")
            result[_unquote(k)] = _parse_scalar_or_flow(v)
        return result
    if s.startswith("[") and s.endswith("]"):
        raise ValueError("fallback parser does not support flow sequences")
    return _unquote(s)


def _fallback_parse(text: str) -> dict:
    """降级 YAML 解析器：覆盖 hermes config.yaml 实际子集。

    支持：缩进嵌套映射 / 标量 / 内联 {k: v} 流映射 / 注释；不支持列表、锚点等
    ——遇到即抛 ValueError，由上层按「无法校验」告警（worker 安装不受阻）。
    """
    lines = []
    for raw in text.splitlines():
        content = _strip_comment(raw.rstrip("\n"))
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        if "\t" in content[:indent + 1]:
            raise ValueError("fallback parser does not support tab indentation")
        lines.append((indent, content.strip()))

    def parse_block(i: int, indent: int):
        result = {}
        while i < len(lines):
            cur_indent, content = lines[i]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise ValueError(f"unexpected indent at line: {content!r}")
            if content.startswith("- "):
                raise ValueError("fallback parser does not support sequences")
            k, v = _split_key_value(content)
            if not k:
                raise ValueError(f"cannot parse line: {content!r}")
            if v:
                result[_unquote(k)] = _parse_scalar_or_flow(v)
                i += 1
            else:
                if i + 1 < len(lines) and lines[i + 1][0] > cur_indent:
                    child, i = parse_block(i + 1, lines[i + 1][0])
                    result[_unquote(k)] = child
                else:
                    result[_unquote(k)] = {}
                    i += 1
        return result, i

    if not lines:
        return {}
    parsed, _ = parse_block(0, lines[0][0])
    return parsed


def parse_hermes_config(text: str) -> dict:
    """解析 hermes config.yaml 文本为 dict（pyyaml 优先，降级解析器兜底）。"""
    if _yaml is not None:
        loaded = _yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    return _fallback_parse(text)


# ===== 模型名/网关提取 =====

def _resolve_env_value(raw, env) -> str:
    """${VAR} → 环境变量值；字面量原样；非字符串（int 等）转 str。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    m = ENV_PATTERN.match(s)
    return env.get(m.group(1), "") if m else s


def _resolve_entry_key(entry: dict, env, key_env_first: bool = True) -> str:
    """providers / custom_providers 条目统一取 key：api_key 字面量与 key_env→${ENV}
    两种形态（此前在 _resolve_gateway 与 custom_providers 循环两处重复同一惯用法）。
    key_env_first=True：key_env 优先（providers 段惯例）；False：api_key 优先、
    key_env 兜底（custom_providers 段既有行为，保持不变）。"""
    key_env = entry.get("key_env")
    if key_env_first and key_env:
        return _resolve_env_value("${%s}" % key_env, env)
    return (_resolve_env_value(entry.get("api_key"), env)
            or _resolve_env_value("${%s}" % key_env if key_env else None, env))


def _resolve_gateway(model_cfg: dict, cfg: dict, env) -> tuple[str, str]:
    """由 model 段解析 default 模型网关：base_url + api_key（含 providers.key_env 回退）。"""
    base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
    api_key = _resolve_env_value(model_cfg.get("api_key"), env)
    if not api_key:
        provider_name = str(model_cfg.get("provider") or "").strip()
        providers = cfg.get("providers") or {}
        if isinstance(providers, dict) and provider_name:
            p = providers.get(provider_name)
            if isinstance(p, dict):
                api_key = _resolve_entry_key(p, env, key_env_first=True)
                base_url = base_url or str(p.get("api") or "").strip().rstrip("/")
    return base_url, api_key


def read_hermes_model_config(hermes_home: str | None = None, env: dict | None = None) -> dict | None:
    """读取 hermes 模型配置，返回统一校验视角。

    返回 None = config.yaml 不存在（调用方跳过校验）。返回结构：
      {
        "config_path": str,
        "default_model": str,
        "targets": [{"base_url": str, "api_key": str, "models": [str, ...]}],  # 按网关分组
        "warnings": [str, ...],  # 无法解析的别名/自定义 provider（不阻断，仅提示）
      }
    模型名全集 = model.default + model_aliases[*].model + custom_providers[*].model
    （Spec 风险缓解：校验 default 与显式 model_aliases/custom_providers.model 全集，
    防别名绕过校验）。
    """
    env = dict(os.environ) if env is None else env
    home = hermes_home or os.path.expanduser(os.environ.get(HERMES_HOME_ENV, "~/.hermes"))
    config_path = os.path.join(home, "config.yaml")
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = parse_hermes_config(f.read())
    except Exception as e:
        return {"config_path": config_path, "default_model": "", "targets": [],
                "warnings": [f"cannot parse config.yaml ({e}); validation skipped"]}

    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    default_model = str(model_cfg.get("default") or "").strip()
    base_url, api_key = _resolve_gateway(model_cfg, cfg, env)

    targets: list[dict] = []

    def _target(b: str, k: str) -> dict | None:
        """按 (base_url, api_key) 分组聚合模型名；无 key 的网关跳过并告警。"""
        if not b:
            return None
        for t in targets:
            if t["base_url"] == b and t["api_key"] == k:
                return t
        if not k:
            warnings.append(f"gateway {b} has no resolvable api_key; its models are not validated")
            return None
        t = {"base_url": b, "api_key": k, "models": []}
        targets.append(t)
        return t

    warnings: list[str] = []
    if default_model:
        t = _target(base_url, api_key)
        if t:
            t["models"].append(default_model)
    else:
        warnings.append("model.default is not configured; default model is not validated")

    aliases = cfg.get("model_aliases") if isinstance(cfg.get("model_aliases"), dict) else {}
    for alias, a_cfg in aliases.items():
        if not isinstance(a_cfg, dict):
            continue
        alias_model = str(a_cfg.get("model") or "").strip()
        if not alias_model:
            continue
        a_url = str(a_cfg.get("base_url") or "").strip().rstrip("/")
        a_key = _resolve_env_value(a_cfg.get("api_key"), env)
        if not a_url or (a_url == base_url and not a_key):
            a_url, a_key = base_url, (a_key or api_key)
        t = _target(a_url, a_key)
        if t and alias_model not in t["models"]:
            t["models"].append(alias_model)

    custom = cfg.get("custom_providers") or {}
    custom_items = custom.values() if isinstance(custom, dict) else (
        custom if isinstance(custom, list) else [])
    for c in custom_items:
        if not isinstance(c, dict):
            continue
        c_model = str(c.get("model") or "").strip()
        if not c_model:
            continue
        c_url = str(c.get("base_url") or c.get("api") or "").strip().rstrip("/")
        c_key = _resolve_entry_key(c, env, key_env_first=False)
        t = _target(c_url, c_key)
        if t and c_model not in t["models"]:
            t["models"].append(c_model)

    return {"config_path": config_path, "default_model": default_model,
            "targets": targets, "warnings": warnings}

# ===== 网关 /v1/models 对齐校验（安装期，Seam A-2）=====

def gateway_models_url(base_url: str) -> str:
    """网关可用列表 URL：base 已含 /v1 则补 /models，否则补 /v1/models（兼容两种写法）。"""
    base = base_url.rstrip("/")
    if base.endswith("/models"):
        return base
    return base + ("/models" if base.endswith("/v1") else "/v1/models")


def gateway_chat_url(base_url: str) -> str:
    """最小补全 URL：与 gateway_models_url 同口径归一化——base 已含 /v1 则补
    /chat/completions，否则补 /v1/chat/completions（否则 base 不含 /v1 的配置
    探活会打错路由，普通路由 404 被误判 model_not_found → healthy=False 误报）。"""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")


def _parse_models_payload(text: str) -> list[str]:
    """解析 /v1/models 响应：data[].id 为主，容错顶层 list / models[].id（OpenAI 兼容差异）。"""
    try:
        payload = json.loads(text)
    except Exception:
        raise ValueError("gateway /v1/models returned non-JSON payload")
    items = None
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None and isinstance(payload.get("models"), list):
            items = payload["models"]
    elif isinstance(payload, list):
        items = payload
    if not isinstance(items, list):
        raise ValueError("gateway /v1/models payload has unexpected shape")
    ids = []
    for it in items:
        mid = it.get("id") if isinstance(it, dict) else it
        if mid:
            ids.append(str(mid))
    return ids


def _default_fetch(url: str, api_key: str, timeout: float, method: str = "GET", body: dict | None = None):
    """真实 HTTP（urllib，stdlib）：返回 (status_code, text)；异常 = 网络不可达。"""
    req = urllib.request.Request(url, method=method)
    if api_key:
        req.add_header("Authorization", "Bearer %s" % api_key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")
    last_err = None
    for _ in range(GATEWAY_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
    raise ConnectionError(str(last_err))


def check_models_against_gateway(config: dict, fetch=None, timeout: float = GATEWAY_TIMEOUT) -> dict:
    """校验配置模型名是否都在 key 所属网关的 /v1/models 可用列表。

    三分支（Spec 验收：合法/非法/不可达）：
      ok          —— 全部在列表（fetch 可注入以便无网络测试）
      misaligned  —— 有模型名不在列表（对照输出由 CLI/调用方打印）
      unreachable —— 网关连不上（fetch 抛异常 / 网关 5xx 视为不可达）→ 降级告警
      响应无法解析 → invalid_response（按「无法校验」告警，不误杀）
    """
    fetch = fetch or _default_fetch
    detail, misaligned = [], []
    for t in config.get("targets", []):
        url = gateway_models_url(t["base_url"])
        try:
            status, text = fetch(url, t["api_key"], timeout)
        except Exception as e:
            detail.append({"base_url": t["base_url"], "status": "unreachable", "error": str(e),
                           "models": t["models"]})
            continue
        if status >= 500 or status in (408, 429):
            detail.append({"base_url": t["base_url"], "status": "unreachable",
                           "error": "HTTP %s" % status, "models": t["models"]})
            continue
        if status != 200:
            detail.append({"base_url": t["base_url"], "status": "http_error",
                           "error": "HTTP %s" % status, "models": t["models"]})
            continue
        try:
            available = _parse_models_payload(text)
        except ValueError as e:
            detail.append({"base_url": t["base_url"], "status": "invalid_response",
                           "error": str(e), "models": t["models"]})
            continue
        missing = [m for m in t["models"] if m not in available]
        detail.append({"base_url": t["base_url"], "status": "ok" if not missing else "misaligned",
                       "configured": t["models"], "available": available, "missing": missing})
        if missing:
            misaligned.append({"base_url": t["base_url"], "missing": missing, "available": available})

    if misaligned:
        status = "misaligned"
    elif any(d["status"] == "unreachable" for d in detail):
        status = "unreachable"
    elif not detail:
        status = "no_validatable_target"
    else:
        status = "ok"
    return {"status": status, "detail": detail,
            "warnings": list(config.get("warnings", []))}


# ===== 探活期最小模型调用（Seam B 后端，Issue #2545）=====

def minimal_model_call(config: dict, timeout: float = GATEWAY_TIMEOUT, post=None) -> tuple[bool, dict]:
    """1-token 最小 chat 补全：真实暴露「模型名不在该 key 可用列表」。

    返回 (ok, detail)：
      200                   → (True,  {"model_call": "ok", "model": <default_model>})
      404 / 模型不存在       → (False, {"model_call": "model_not_found: <model>"})  # 探针据此置 unhealthy
      连接异常/超时           → (True,  {"model_call": "skipped: network unreachable"})  # 不误判
      其它 HTTP 状态          → (True,  {"model_call": "http_<code> (not model-not-found, ignored)"})
    """
    targets = config.get("targets") or []
    if not targets:
        return True, {"model_call": "skipped: no gateway target"}
    t = targets[0]
    model = config.get("default_model") or ""
    if not model or not t.get("api_key"):
        return True, {"model_call": "skipped: model or api_key unresolved"}
    post = post or (lambda url, key, to, body: _default_fetch(url, key, to, method="POST", body=body))
    url = gateway_chat_url(t["base_url"])
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    try:
        status, text = post(url, t["api_key"], timeout, body)
    except Exception:
        return True, {"model_call": "skipped: network unreachable"}
    low = text.lower()
    if status == 200:
        return True, {"model_call": "ok", "model": model}
    if status == 404 or "model not found" in low or "model_not_found" in low:
        return False, {"model_call": "model_not_found: %s" % model}
    return True, {"model_call": "http_%s (not model-not-found, ignored)" % status}


# ===== CLI（安装脚本/人工自检统一入口）=====

def _print_check_result(cfg: dict, result: dict) -> int:
    for w in result.get("warnings", []):
        print("[model-check] Warning: %s" % w)
    for d in result.get("detail", []):
        if d["status"] == "ok":
            print("[model-check] OK: gateway %s has all configured models: %s"
                  % (d["base_url"], ", ".join(d["configured"])))
        elif d["status"] == "misaligned":
            print("[model-check] MISALIGNED at %s" % d["base_url"])
            print("[model-check]   configured model(s): %s" % ", ".join(d["configured"]))
            print("[model-check]   missing from gateway: %s" % ", ".join(d["missing"]))
            print("[model-check]   available models:     %s" % ", ".join(d["available"]))
        elif d["status"] == "unreachable":
            print("[model-check] Warning: gateway %s unreachable (%s) -- model validation SKIPPED, "
                  "check network/gateway manually later" % (d["base_url"], d.get("error", "")))
        else:
            print("[model-check] Warning: gateway %s response unusable (%s: %s) -- validation skipped"
                  % (d["base_url"], d["status"], d.get("error", "")))
    st = result.get("status")
    if st == "misaligned":
        print("[model-check] FAILED: configured model name(s) not in gateway /v1/models list.")
        print("[model-check] Fix: edit model.default / model_aliases in %s to a model from the "
              "available list above (see docs/deployment/model-gateway-alignment.md), or switch "
              "to a key that covers them." % cfg.get("config_path", "config.yaml"))
        return 1
    if st == "no_validatable_target":
        print("[model-check] Warning: nothing to validate (no resolvable gateway target in config)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    hermes_home = None
    if "--check" in argv:
        args = [a for a in argv if a != "--check"]
        for i, a in enumerate(args):
            if a == "--hermes-home" and i + 1 < len(args):
                hermes_home = args[i + 1]
    else:
        print("usage: python -m src.runtime.engine_model --check [--hermes-home <dir>]")
        return 2
    cfg = read_hermes_model_config(hermes_home)
    if cfg is None:
        home = hermes_home or os.path.expanduser(os.environ.get(HERMES_HOME_ENV, "~/.hermes"))
        print("[model-check] No hermes config.yaml at %s -- nothing to validate, skipping." % home)
        return 0
    if not cfg.get("targets"):
        for w in cfg.get("warnings", []):
            print("[model-check] Warning: %s" % w)
        print("[model-check] Warning: no resolvable gateway target; validation skipped")
        return 0
    return _print_check_result(cfg, check_models_against_gateway(cfg))


if __name__ == "__main__":
    sys.exit(main())


# ===== Issue #865 服务端兜底段（!1657 移植漏带，自平台线整段补齐）=====
# 平台线单机解析器 + _PARSERS + 指纹缓存 + detect_engine_model（settings.py #865 引用）
_MAX_MODEL_LEN = 120

# 平台线单机解析器 + _PARSERS + 指纹缓存 + detect_engine_model（settings.py #865 引用）
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


def _aishell_dsh_model() -> str:
    """acp profile 的 agent-default-model 段 → "provider/model"。

    路径：~/.aishell/profiles/acp/*.patch.yml（HERMES_HOME 无关，AISHELL_HOME
    可覆盖）。dsh profile 是 patch 列表，取 id=agent-default-model 段的
    config.provider + config.model；只取模型名，apiKeyEnv/base_url 不进返回值
    （Issue #865 安全约束）。worker 可能无 pyyaml——正则兜底（对齐 hermes 分支）。
    """
    import glob
    home = os.environ.get("AISHELL_HOME", os.path.expanduser("~/.aishell"))
    paths = sorted(glob.glob(os.path.join(home, "profiles", "acp", "*.patch.yml")),
                   reverse=True)
    for path in paths:
        if not os.path.isfile(path):
            continue
        # GBK 兼容：Windows 手工保存的 profile 可能是 ANSI/GBK（中文注释），
        # utf-8 硬读会 UnicodeDecodeError（真机实证 2026-11-09）——先 utf-8 后 gbk
        with open(path, "rb") as f:
            raw_bytes = f.read()
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw_bytes.decode("gbk", errors="replace")
        try:
            import yaml  # type: ignore
            for item in (yaml.safe_load(raw) or []):
                if isinstance(item, dict) and item.get("id") == "agent-default-model":
                    cfg = item.get("config") or {}
                    # 只返回模型名，不带 provider 前缀（provider 名属内部网关标识，
                    # 开源合规不外泄；opencode 的 provider/modelid 先例不适用此场景）
                    return _sanitize(cfg.get("model"))
        except Exception:
            pass
        # 正则兜底（无 pyyaml）：agent-default-model 段内的 model。
        # [^\S\n]* 只吃行内空白——贪婪 \s* 会吞掉换行+缩进使 (?:\n...) 起点落空，
        # 块匹配恒空（真机实证 2026-11-10：worker 无 pyyaml 走此分支必得空串）
        m = re.search(r"- id:\s*agent-default-model[^\S\n]*(?:\n(?!\s*-\s+id:)[^\n]*)*",
                      raw)
        if m:
            block = m.group(0)
            mm = re.search(r"^\s+model:\s*([^\s#]+)", block, re.M)
            if mm:
                return _sanitize(mm.group(1))
    return ""


_PARSERS = {
    "hermes": _hermes_model,
    "opencode": _opencode_model,
    "codearts": _codearts_model,
    "aishell-dsh": _aishell_dsh_model,
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
