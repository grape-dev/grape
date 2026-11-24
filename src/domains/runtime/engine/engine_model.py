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
    "read_opencode_model_config",
    "validate_engine_model",
    "probe_engine_model_call",
    "is_model_not_found_error",
    "split_model_prefix",
    "MODEL_CHECK_DISABLED_ENV",
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


def _parse_scalar_or_flow(s: str, warnings: list | None = None):
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
            result[_unquote(k)] = _parse_scalar_or_flow(v, warnings)
        return result
    if s.startswith("[") and s.endswith("]"):
        # Issue #3874：流式列表同样降级为非阻断 warning（返回空串占位）。
        msg = ("fallback parser cannot parse YAML flow sequences (value: %r); "
               "that value is skipped and model validation may be incomplete" % s)
        if warnings is not None:
            warnings.append(msg)
        else:
            # 调用方未传列表时也必须留信号，不得静默丢弃（Issue #3874 审查建议）
            sys.stderr.write(f"Warning: {msg}\n")
        return ""
    return _unquote(s)


def _fallback_parse(text: str, warnings: list | None = None) -> dict:
    """降级 YAML 解析器：覆盖 hermes config.yaml 实际子集。

    支持：缩进嵌套映射 / 标量 / 内联 {k: v} 流映射 / 注释 / **块序列**（`- ` 项，映射或
    标量）——`fallback_providers` 是列表（Issue #3822：此前整份配置被判「无法解析」→ 跳过
    校验，2 个 404 id 正是从该缺口漏过）；不支持锚点/多行标量等，遇到即抛 ValueError，
    由上层按「无法校验」告警（worker 安装不受阻，Issue #3874：无法校验 ≠ 配置错误）。

    warnings 为 None 时内部自建列表并把降级提示写 stderr（Issue #3874 审查建议：
    调用方漏传列表时不得整块静默丢弃、无任何信号）。
    """
    emit_to_stderr = warnings is None
    if warnings is None:
        warnings = []
    lines = []
    for raw in text.splitlines():
        content = _strip_comment(raw.rstrip("\n"))
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        if "\t" in content[:indent + 1]:
            raise ValueError("fallback parser does not support tab indentation")
        lines.append((indent, content.strip()))

    def _is_item(content: str) -> bool:
        return content == "-" or content.startswith("- ")

    def parse_seq(i: int, indent: int):
        """块序列：`- scalars` / `- {k: v}` / `- k: v` + 同项续行（更深缩进映射）。"""
        # Issue #3874 非阻断信号兼容：块序列在降级模式下解析成功也保留 warning 提示，
        # 校验覆盖可能不完整（含 "sequences" 关键字，供既有守门用例断言）。
        warnings.append(
            "fallback parser parsed YAML block sequences (line: %r) in degraded mode; "
            "model validation may be incomplete" % lines[i][1])
        items = []
        while i < len(lines):
            cur_indent, content = lines[i]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise ValueError(f"unexpected indent at line: {content!r}")
            if not _is_item(content):
                break
            rest = content[1:].strip()
            if not rest:
                # `-` 后空：子块（映射或下一层序列）
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    child, i = parse_block(i + 1, lines[i + 1][0])
                    items.append(child)
                else:
                    items.append("")
                    i += 1
                continue
            if rest[0] in "{[":
                items.append(_parse_scalar_or_flow(rest))
                i += 1
                continue
            k, v = _split_key_value(rest)
            if not k:
                items.append(_unquote(rest))       # `- scalar`
                i += 1
                continue
            item = {}
            if v:
                item[_unquote(k)] = _parse_scalar_or_flow(v)
                i += 1
            else:
                item[_unquote(k)] = {}
                i += 1
            # 同一项的其余键（`- provider: p1` 之后的 `  model: m1` 等）
            if i < len(lines) and lines[i][0] > indent and not _is_item(lines[i][1]):
                more, i = parse_block(i, lines[i][0])
                if isinstance(more, dict):
                    item.update(more)
            items.append(item)
        return items, i

    def parse_block(i: int, indent: int):
        if i < len(lines) and lines[i][0] == indent and _is_item(lines[i][1]):
            return parse_seq(i, indent)
        result = {}
        while i < len(lines):
            cur_indent, content = lines[i]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise ValueError(f"unexpected indent at line: {content!r}")
            if _is_item(content):
                raise ValueError("fallback parser: sequence item inside mapping block")
            k, v = _split_key_value(content)
            if not k:
                raise ValueError(f"cannot parse line: {content!r}")
            if v:
                result[_unquote(k)] = _parse_scalar_or_flow(v, warnings)
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
    if emit_to_stderr:
        for w in warnings:
            sys.stderr.write(f"Warning: {w}\n")
    return parsed


def parse_hermes_config(text: str, warnings: list | None = None) -> dict:
    """解析 hermes config.yaml 文本为 dict（pyyaml 优先，降级解析器兜底）。

    warnings（可选）由调用方传入列表：降级解析器遇到不支持的 YAML 结构（列表等）
    时不抛异常，而是向其追加非阻断 warning（Issue #3874）。
    """
    if _yaml is not None:
        loaded = _yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    return _fallback_parse(text, warnings)


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


def _resolve_provider_gateway(provider_name: str, cfg: dict, env) -> tuple[str, str]:
    """`providers.<name>` 条目的网关（`api` + `key_env`/`api_key`），缺失返回空串。"""
    providers = cfg.get("providers") or {}
    if not isinstance(providers, dict) or not provider_name:
        return "", ""
    p = providers.get(provider_name)
    if not isinstance(p, dict):
        return "", ""
    url = str(p.get("api") or p.get("base_url") or "").strip().rstrip("/")
    return url, _resolve_entry_key(p, env, key_env_first=True)


def _fallback_entries(cfg: dict, warnings: list) -> list[dict]:
    """`fallback_providers`（主）+ 旧键 `fallback_model`（兜底）的有效条目。

    与 hermes `get_fallback_chain` 同口径：两者都是列表或单对象；按
    `(provider, model, base_url)` 去重，`fallback_providers` 顺序优先。
    Issue #3822：此前 fallback 链完全不参与校验，本机 2 个 404 id 正是从该缺口漏过。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for key in ("fallback_providers", "fallback_model"):
        raw = cfg.get(key)
        if isinstance(raw, str) and raw.strip():
            # 旧键 `fallback_model` 的历史写法是裸模型名标量；同样纳入校验（网关回落主网关）
            candidates = [{"model": raw.strip()}]
        elif isinstance(raw, dict):
            candidates = [raw]
        elif isinstance(raw, list):
            candidates = raw
        else:
            if raw not in (None, "", [], {}):
                warnings.append("%s 不是列表/映射，已跳过（格式无法识别）" % key)
            continue
        for entry in candidates:
            if not isinstance(entry, dict):
                warnings.append("%s 含非映射条目，已跳过" % key)
                continue
            provider = str(entry.get("provider") or "").strip()
            model = str(entry.get("model") or "").strip()
            if not model:
                continue
            base_url = str(entry.get("base_url") or "").strip().rstrip("/")
            ident = (provider.lower(), model.lower(), base_url.lower())
            if ident in seen:
                continue
            seen.add(ident)
            out.append({"provider": provider, "model": model,
                        "base_url": base_url, "raw": entry})
    return out


def _resolve_fallback_gateway(entry: dict, cfg: dict, env, default_url: str,
                              default_key: str) -> tuple[str, str]:
    """fallback 条目的网关：条目自带 base_url → 只用条目自己的 key（不借主网关 key）。

    独立网关没有可解析 key 时返回空 key，由 `_target` 告警并跳过该网关——绝不把主网关的
    key 发到第三方 base_url（凭证串用）。仅当条目没有独立网关（回落主网关/providers 段）时
    才允许借用默认 key。
    """
    raw = entry.get("raw") or {}
    base_url = entry.get("base_url") or ""
    api_key = _resolve_entry_key(raw, env, key_env_first=False)
    if not api_key:
        api_key_env = str(raw.get("api_key_env") or "").strip()
        if api_key_env:
            api_key = env.get(api_key_env, "")
    if base_url:
        # 同一网关（base_url 与主网关一致）可沿用主 key；第三方网关绝不借 key（凭证串用）
        if not api_key and default_url and base_url.rstrip("/") == default_url.rstrip("/"):
            api_key = default_key
        return base_url, api_key
    p_url, p_key = _resolve_provider_gateway(entry.get("provider") or "", cfg, env)
    if p_url:
        return p_url, (api_key or p_key)
    return default_url, (api_key or default_key)


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
    parse_warnings: list[str] = []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = parse_hermes_config(f.read(), parse_warnings)
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

    warnings: list[str] = list(parse_warnings)
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

    # Issue #3822：fallback 链（fallback_providers + 旧 fallback_model）纳入校验——
    # 主模型对齐不代表 fallback 对齐，错位的 fallback 一旦被选中即 100% Model not found。
    for entry in _fallback_entries(cfg, warnings):
        f_url, f_key = _resolve_fallback_gateway(entry, cfg, env, base_url, api_key)
        t = _target(f_url, f_key)
        if t and entry["model"] not in t["models"]:
            t["models"].append(entry["model"])

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


# ===== Issue #3820：opencode 模型名对齐校验（启动/注册 + 探活共用）=====

MODEL_CHECK_DISABLED_ENV = "GRAPE_MODEL_CHECK_DISABLED"
#: opencode provider 的 apiKey 插值语法 `{env:VAR}`（与 `${VAR}` 两种形态都解析）
_OPENCODE_ENV_REF = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
_TRUTHY = ("1", "true", "yes", "on")
#: 模型名不存在短语（`Model not found` / `model_not_found` / `model-not-found`）
_MODEL_MISSING_RE = re.compile(r"model[\s_-]*(?:is\s+)?not[\s_-]*found", re.I)
#: 错误码形态（带下划线/连字符）本身就是 API 契约，足以判定模型不存在
_MODEL_CODE_RE = re.compile(r"model[_\-]not[_\-]?found|model[_\-]?not[_\-]found", re.I)
#: 404 状态上下文：裸 `404` / `Not Found:` 状态标签 / `NotFoundError`
_MODEL_404_CONTEXT_RE = re.compile(r"\b404\b|\bnot\s*found\s*:|\bnotfound(?:error)?\b", re.I)


def is_model_not_found_error(text: str) -> bool:
    """404 + `Model not found` 双条件判定（Issue #3820，分类/探活共用）。

    双条件防文本匹配过宽：业务文案里的 `model ... not found`（如 "model file not found in
    expected list"）没有 404 状态上下文，不得误判为配置错误。错误码形态
    `model_not_found` / `model-not-found` 本身即 API 契约，直接判定。
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    if _MODEL_CODE_RE.search(raw):
        return True
    return bool(_MODEL_MISSING_RE.search(raw) and _MODEL_404_CONTEXT_RE.search(raw))


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _model_check_disabled(env=None) -> bool:
    """进程级运维开关：显式 env 或 os.environ 任一为真即关闭校验。"""
    if _is_truthy(os.environ.get(MODEL_CHECK_DISABLED_ENV)):
        return True
    if env is None or env is os.environ:
        return False
    return _is_truthy(env.get(MODEL_CHECK_DISABLED_ENV))


def opencode_config_path(config_path: str | None = None) -> str:
    """opencode 配置路径：显式入参 > ~/.config/opencode/opencode.json > opencode.jsonc。"""
    if config_path:
        return config_path
    base = os.path.expanduser("~/.config/opencode")
    for name in ("opencode.json", "opencode.jsonc"):
        candidate = os.path.join(base, name)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(base, "opencode.json")


def _strip_jsonc(text: str) -> str:
    """去掉 JSONC 的 `//` 行注释与尾逗号（字符串内的 `//` 不处理）。"""
    lines = []
    for line in text.splitlines():
        in_single = in_double = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "'" and not in_double:
                in_single = not in_single
            elif not in_single and not in_double and ch == "/" and i + 1 < len(line) \
                    and line[i + 1] == "/":
                cut = i
                break
            i += 1
        lines.append(line[:cut])
    return re.sub(r",(\s*[}\]])", r"\1", "\n".join(lines))


def _resolve_opencode_key(raw, env) -> str:
    """opencode apiKey：`{env:VAR}` / `${VAR}` → 环境变量值；字面量原样。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    for pattern in (_OPENCODE_ENV_REF, ENV_PATTERN):
        m = pattern.match(s)
        if m:
            return env.get(m.group(1), "")
    return s


def _opencode_models(provider: dict) -> list[str]:
    models = provider.get("models")
    out: list[str] = []
    if isinstance(models, dict):
        out = [str(k) for k in models if str(k)]
    elif isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
            elif isinstance(item, str) and item:
                out.append(item)
    return out


def split_model_prefix(model: str, provider_ids=None) -> tuple[str, str]:
    """`provider/model-id` → `(provider, bare)`；无前缀（或前缀不是已知 provider）→ `("", model)`。

    Issue #3822：网关 `/v1/models` 只认**裸 id**（`glm-5.3`），带 provider 前缀的写法
    （`opengw/glm-5.3`）会 HTTP 404 `Model not found`；而 opencode 本地按裸 id 查 provider 的
    `models` 映射，键写成 `opengw/glm-5.3` 时本地先报 `Model not found: opengw/glm-5.3`。
    两个比对口径都必须以裸 id 为准，否则会出现「网关清单里有、却报 Model not found」。

    `provider_ids` 为空表示「不做白名单校验」（首段即前缀）；非空时仅当首段在集合内才拆，
    避免把自带 `/` 的模型名（`org/model-x`）误归一。
    """
    raw = str(model or "").strip()
    head, sep, rest = raw.partition("/")
    if not sep or not head or not rest:
        return "", raw
    if provider_ids is None:
        return head, rest
    try:
        known = {str(p).strip().lower() for p in provider_ids if str(p).strip()}
    except TypeError:
        known = set()
    if not known or head.lower() in known:
        return head, rest
    return "", raw


def _opencode_candidate_paths() -> list[str]:
    base = os.path.expanduser("~/.config/opencode")
    return [os.path.join(base, n) for n in ("opencode.json", "opencode.jsonc")]


def _peek_opencode_model(path: str) -> str:
    """尽力读取某份 opencode 配置的顶层 `model`（失败/无值返回空串，不抛）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            cfg = json.loads(raw)
        except ValueError:
            cfg = json.loads(_strip_jsonc(raw))
        return str((cfg or {}).get("model") or "").strip() if isinstance(cfg, dict) else ""
    except Exception:
        return ""


def _pick_opencode_config(path: str, explicit: str | None) -> tuple[str, str]:
    """两份配置（`opencode.json` / `opencode.jsonc`）并存时的取用口径。

    Issue #3822 实测：同一台机器上 `opencode.json` 有完整 provider/model，`opencode.jsonc`
    只有 `$schema` 空壳；反之也有仅维护 jsonc 的主机（SRE 轨 A 整改的正是 jsonc）。
    `opencode_config_path` 的固定顺序（json 优先）会在「生效配置其实是 jsonc」的主机上
    读到空壳 → 校验结论与实际运行期不符（可能误拒/漏报）。故：显式路径不动；非显式时
    **优先取有顶层 model 的那份**，两份都有 model 则维持既有顺序（json 优先）。

    返回 `(chosen, other)`：`other` 为另一份存在的文件路径（无则空串）。
    """
    if explicit:
        return path, ""
    candidates = _opencode_candidate_paths()
    others = [p for p in candidates if os.path.isfile(p) and p != path]
    other = others[0] if others else ""
    if other and not _peek_opencode_model(path) and _peek_opencode_model(other):
        return other, path
    return path, other


def read_opencode_model_config(config_path: str | None = None,
                               env: dict | None = None) -> dict | None:
    """读取 opencode 模型配置，返回与 hermes 同构的校验视角（Issue #3820）。

    返回 None = 配置文件不存在（调用方跳过校验）。返回结构：
      {
        "config_path": str,
        "default_model": str,   # 顶层 model 的 provider 前缀已剥离（网关只认裸 model-id）
        "targets": [{"base_url": str, "api_key": str, "models": [str, ...]}],
        "warnings": [str, ...],
      }
    网关与密钥来自 `provider.<id>.options.baseURL` / `options.apiKey`（支持 `{env:VAR}`）；
    已配置模型全集 = `provider.<id>.models` 键集 + default 模型。
    """
    env = dict(os.environ) if env is None else env
    path = opencode_config_path(config_path)
    if not os.path.isfile(path):
        return None
    chosen, alt = _pick_opencode_config(path, config_path)
    path = chosen
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            cfg = json.loads(raw)
        except ValueError:
            cfg = json.loads(_strip_jsonc(raw))
    except Exception as e:
        return {"config_path": path, "default_model": "", "targets": [], "local_issues": [],
                "warnings": ["cannot parse opencode config (%s); validation skipped" % e]}
    if not isinstance(cfg, dict):
        return {"config_path": path, "default_model": "", "targets": [], "local_issues": [],
                "warnings": ["opencode config root is not an object; validation skipped"]}

    providers_cfg = cfg.get("provider")
    providers: dict = providers_cfg if isinstance(providers_cfg, dict) else {}
    provider_ids = {str(k) for k in providers}

    raw_model = str(cfg.get("model") or "").strip()
    provider_id, default_model = split_model_prefix(raw_model, provider_ids)
    if not provider_id:
        # Issue #3822 审查 Required：顶层 `model` 恒为 opencode 的 `provider/model-id` 写法，
        # provider 段未在 `provider` 块声明（内置 provider 形态，如 `deepseek/<id>`）时
        # split 会原样保留整串 → 探活/比对把带前缀名发给网关 → 404 Model not found 误判
        # unhealthy。此处仍剥前缀取裸 id，provider_id 留空（跳过「provider not found」误告警）。
        head, sep, rest = raw_model.partition("/")
        if sep and head and rest:
            default_model = rest
    default_model = default_model.strip()

    targets: list[dict] = []
    warnings: list[str] = []
    if alt:
        warnings.append("检测到两份 opencode 配置（%s / %s），本次按生效的一份（%s）校验；"
                        "两份不一致时请人工确认实际生效配置" % (path, alt, path))
    #: Issue #3822 本地查表类诊断（网关清单对齐 ≠ 运行期可用）：
    #:   prefixed_key       —— `models` 键重复 provider 前缀（opencode 本地按裸 id 查表）
    #:   undeclared_default —— default 裸 id 未在该 provider 的 `models` 声明
    local_issues: list[dict] = []

    #: default 无 provider 前缀且配置里只有一个 provider（opencode 允许省略前缀）时，
    #: 本地查表仍落在这个唯一 provider 的 `models` 映射上——按它做 undeclared_default 判定。
    declared_provider = provider_id
    if not declared_provider and default_model:
        _dict_providers = [str(k) for k, v in providers.items() if isinstance(v, dict)]
        if len(_dict_providers) == 1:
            declared_provider = _dict_providers[0]

    def _target(base_url: str, api_key: str) -> dict | None:
        if not base_url:
            return None
        for t in targets:
            if t["base_url"] == base_url and t["api_key"] == api_key:
                return t
        if not api_key:
            warnings.append("gateway %s has no resolvable api_key; its models are not validated"
                            % base_url)
            return None
        t = {"base_url": base_url, "api_key": api_key, "models": []}
        targets.append(t)
        return t

    for pid, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        opts_cfg = provider.get("options")
        options: dict = opts_cfg if isinstance(opts_cfg, dict) else {}
        base_url = str(options.get("baseURL") or options.get("base_url") or "").strip().rstrip("/")
        api_key = _resolve_opencode_key(options.get("apiKey") or options.get("api_key"), env)
        raw_models = _opencode_models(provider)
        models: list[str] = []
        for raw_id in raw_models:
            # Issue #3822 前缀归一：`opengw/<id>` 与 `<id>` 是同一模型（比对口径取裸 id）；
            # 但键本身带重复 provider 前缀时 opencode 本地查表会失败 → 记录可执行诊断。
            prefix, bare = split_model_prefix(raw_id, {pid})
            if prefix and bare:
                local_issues.append({"code": "prefixed_key", "provider": pid,
                                     "configured": raw_id, "expected": bare})
                models.append(bare)
            else:
                models.append(raw_id)
        declared = {m for m in models if m}
        if str(pid).lower() == str(declared_provider).lower() and default_model:
            if declared and default_model not in declared:
                local_issues.append({"code": "undeclared_default", "provider": pid,
                                     "model": default_model, "declared": sorted(declared)})
            if default_model not in models:
                models.insert(0, default_model)
        target = _target(base_url, api_key)
        if target:
            for model in models:
                if model not in target["models"]:
                    target["models"].append(model)

    if not default_model:
        warnings.append("opencode config model is empty; default model is not validated")
    elif provider_id and not any(str(p).lower() == str(provider_id).lower() for p in providers):
        warnings.append("opencode provider '%s' not found; default model is not validated"
                        % provider_id)
    else:
        # 探活最小调用固定打 targets[0]——default 模型所在网关必须排首位
        for idx, t in enumerate(targets):
            if default_model in t["models"]:
                if idx:
                    targets.insert(0, targets.pop(idx))
                break
    return {"config_path": path, "default_model": default_model,
            "targets": targets, "warnings": warnings, "local_issues": local_issues}


def _engine_model_config(engine_name: str, config_path: str | None = None,
                         env: dict | None = None) -> dict | None:
    """按引擎名读取模型配置（hermes / opencode 共用校验视角）。"""
    name = str(engine_name or "").strip().lower()
    if name.startswith("opencode"):
        return read_opencode_model_config(config_path, env=env)
    if name.startswith("hermes"):
        return read_hermes_model_config(config_path, env=env)
    return None


def _check_status(check: dict) -> str:
    """把 `check_models_against_gateway` 结果收敛为注册/启动判定口径。

    http_error / invalid_response 同 `unreachable`：无法校验时放行（不误拒），
    仅 misaligned（确认缺失）才拒绝。
    """
    if check.get("status") == "misaligned":
        return "misaligned"
    detail = check.get("detail") or []
    if any(d.get("status") in ("unreachable", "http_error", "invalid_response") for d in detail):
        return "unreachable"
    if not any(d.get("status") == "ok" for d in detail):
        return "no_validatable_target"
    return "ok"


def _misaligned_message(engine_name: str, detail: list) -> str:
    parts = []
    for d in detail:
        if d.get("status") != "misaligned":
            continue
        parts.append(
            "%s 配置模型名不在网关可用列表：配置=%s；缺失=%s；%s 可用=%s" % (
                engine_name, ", ".join(d.get("configured") or []),
                ", ".join(d.get("missing") or []),
                gateway_models_url(d.get("base_url") or ""),
                ", ".join(d.get("available") or [])))
    return "；".join(parts)


def _local_issues_message(engine_name: str, cfg: dict, detail: list | None = None) -> str:
    """本地查表类诊断的差异文案（Issue #3822）：配置原值 + 建议值 + 网关可用列表。

    这类问题网关 `/v1/models` 看起来「有模型」，但 opencode 运行期本地按裸 id 查 provider 的
    `models` 映射会直接 `Model not found: <provider>/<id>`——必须在注册期判死并给出可执行改写。
    """
    issues = [i for i in (cfg.get("local_issues") or []) if isinstance(i, dict)]
    if not issues:
        return ""
    parts = []
    for it in issues:
        if it.get("code") == "prefixed_key":
            parts.append(
                "provider '%s' 的 models 键 '%s' 重复 provider 前缀（本地只认裸 id，"
                "运行期会报 Model not found: %s），应改为 '%s'"
                % (it.get("provider", ""), it.get("configured", ""),
                   it.get("configured", ""), it.get("expected", "")))
        elif it.get("code") == "undeclared_default":
            parts.append(
                "default 模型 '%s' 未在 provider '%s' 的 models 中声明"
                "（本地查表会报 Model not found: %s/%s），请把该 id 加入 models 或在已声明模型中改选"
                % (it.get("model", ""), it.get("provider", ""),
                   it.get("provider", ""), it.get("model", "")))
    available: list[str] = []
    for d in detail or []:
        if d.get("status") in ("ok", "misaligned"):
            available.extend(d.get("available") or [])
    msg = "%s 本地模型查表配置错误（运行期必报 Model not found，非上游抖动）：%s" % (
        engine_name, "；".join(parts))
    if available:
        msg += "；网关可用=%s" % ", ".join(sorted({m for m in available if m}))
    return msg


def validate_engine_model(engine_name: str, config_path: str | None = None,
                          env: dict | None = None, fetch=None,
                          timeout: float = GATEWAY_TIMEOUT) -> dict:
    """启动/注册阶段模型可用性校验（Issue #3820）。

    返回 {"engine", "status", "default_model", "message", "detail"}：
      ok / no_config / no_validatable_target / disabled → 放行；
      misaligned → 调用方拒绝健康注册（message 即「配置模型名 vs /v1/models 可用列表」差异）；
      unreachable → 网关不可达/响应不可用，放行并告警（风险缓解：不误拒）。
    """
    name = str(engine_name or "").strip().lower()
    out = {"engine": name, "status": "not_applicable", "default_model": "",
           "message": "", "detail": []}
    if _model_check_disabled(env):
        out.update(status="disabled",
                   message="model check disabled by %s" % MODEL_CHECK_DISABLED_ENV)
        return out
    if not (name.startswith("opencode") or name.startswith("hermes")):
        out["message"] = "%s has no model-alignment validation; skipped" % name
        return out
    cfg = _engine_model_config(name, config_path=config_path, env=env)
    if cfg is None:
        out.update(status="no_config",
                   message="%s 未找到模型配置文件，跳过模型校验" % name)
        return out
    out["default_model"] = cfg.get("default_model") or ""
    check = check_models_against_gateway(cfg, fetch=fetch, timeout=timeout)
    out["detail"] = check.get("detail") or []
    status = _check_status(check)
    out["status"] = status
    warnings = [w for w in (cfg.get("warnings") or []) if w]
    # Issue #3822：本地查表类诊断（前缀重复 / default 未声明）——网关清单可能对齐，
    # 但运行期依然 100% Model not found，故同样判 misaligned（真错位才拒，不误拒）。
    local_msg = _local_issues_message(name, cfg, out["detail"])
    if status == "misaligned":
        out["message"] = _misaligned_message(name, out["detail"])
        if local_msg:
            out["message"] = (out["message"] + "；" + local_msg) if out["message"] else local_msg
        if warnings:
            out["message"] += "；" + "；".join(warnings)
    elif local_msg:
        out["status"] = "misaligned"
        out["message"] = local_msg
        if warnings:
            out["message"] += "；" + "；".join(warnings)
    elif status == "unreachable":
        errors = [str(d.get("error") or "") for d in out["detail"] if d.get("status") != "ok"]
        out["message"] = ("%s 模型可用性校验 SKIPPED（网关不可达/响应不可用：%s），不阻断注册"
                          % (name, "; ".join(errors)[:300]))
    elif status == "ok":
        models = sorted({m for d in out["detail"] if d.get("status") == "ok"
                         for m in (d.get("configured") or [])})
        out["message"] = "%s 模型 %s 均在网关 /v1/models 可用列表中" % (
            name, ", ".join(models))
        if warnings:
            out["message"] += "；" + "；".join(warnings)
    else:
        out["message"] = "%s 无可校验的网关目标（配置/密钥缺失），跳过模型校验" % name
        if warnings:
            out["message"] += "；" + "；".join(warnings)
    return out


def probe_engine_model_call(engine_name: str, config_path: str | None = None,
                            env: dict | None = None, timeout: float = GATEWAY_TIMEOUT,
                            post=None, fetch=None) -> tuple[bool, dict, str]:
    """探活期最小模型调用（max_tokens=1，Issue #3820，opencode/hermes 共用）。

    返回 (ok, detail, error_message)：
      200 / 网络不可达 / 无配置 → (True, detail, "")            不误判；
      404 模型不存在            → (False, detail, 差异文案)      探针置 unhealthy + 运行时页展示。
    """
    name = str(engine_name or "").strip().lower()
    if _model_check_disabled(env):
        return True, {"model_call": "skipped: model check disabled"}, ""
    cfg = _engine_model_config(name, config_path=config_path, env=env)
    if cfg is None:
        return True, {"model_call": "skipped: no model config"}, ""
    ok, detail = minimal_model_call(cfg, timeout=timeout, post=post)
    if ok:
        return True, detail, ""
    message = "%s 模型配置错误（不可重试）：%s" % (name, detail.get("model_call", ""))
    try:
        check = check_models_against_gateway(cfg, fetch=fetch, timeout=timeout)
        diff = _misaligned_message(name, check.get("detail") or [])
        if diff:
            message += "；" + diff
        loc = _local_issues_message(name, cfg, check.get("detail") or [])
        if loc:
            message += "；" + loc
    except Exception as e:
        message += "；(/v1/models 对照不可用: %s)" % e
    return False, detail, message


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
