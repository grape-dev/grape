"""契约加载：优先 static/openapi.json → /openapi.json → 缓存 ~/.grape → 内置快照。

拉取优先级（docs/grape-contract-static-fetch-design.md）：
  1) `{base}/static/openapi.json` —— 服务端固定下载 URL（公开、no-cache、绕过 nginx 封禁）
  2) `{base}/openapi.json`        —— 兼容未提供 static 的部署
  3) 安装包内置快照 `src/data/openapi.json`（方案 C 兜底，命令发现不依赖网络）
"""
import json
from importlib.resources import files
from pathlib import Path

from src import config
from src.client import ApiClient, CliError

# 内置快照来源信息：kind ∈ {"remote", "cache", "bundled"}
_last_source = {"kind": "", "detail": ""}

_CONTRACT_PATHS = ("/static/openapi.json", "/openapi.json")


def cache_path(version: str = "") -> Path:
    return config.CONFIG_DIR / f"openapi-{version}.json" if version else config.CONFIG_DIR / "openapi-current.json"


def _cache_version_key(name: str) -> tuple:
    """'openapi-0.10.0.json' -> (0, 10, 0)；非版本化（openapi-current.json）-> (0,)。

    版本号按点分数字段比较，避免字典序在 0.9.0 vs 0.10.0 时选错。
    """
    stem = name[len("openapi-"):]
    if stem.endswith(".json"):
        stem = stem[:-len(".json")]
    parts = stem.split(".")
    if parts and all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return (0,)


def _read_cache() -> dict | None:
    for f in sorted(config.CONFIG_DIR.glob("openapi-*.json"),
                    key=lambda p: _cache_version_key(p.name), reverse=True):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def fetch_schema(base_url: str, token: str) -> dict:
    """按优先级尝试拉取契约：static/openapi.json → /openapi.json。

    契约端点为公开资源（/static/openapi.json），无需 token 即可拉取。
    """
    api = ApiClient(base_url=base_url, token=token, require_token=False)
    last_error: Exception | None = None
    for path in _CONTRACT_PATHS:
        try:
            _s, data = api.request("GET", path)
            if isinstance(data, dict) and "paths" in data:
                return data
            raise CliError(f"contract malformed (missing 'paths'): {path}")
        except Exception as e:
            last_error = e
    raise CliError(f"contract unreachable from any of {_CONTRACT_PATHS} ({last_error})")


def load_bundled() -> dict | None:
    """读取安装包内置的契约快照（方案 C 兜底），失败返回 None。"""
    try:
        schema = json.loads(
            files("src").joinpath("data", "openapi.json").read_text(encoding="utf-8")
        )
        if isinstance(schema, dict) and "paths" in schema:
            return schema
    except Exception:
        pass
    return None


def load_contract(base_url: str = "", token: str = "", refresh: bool = False) -> dict:
    """返回 OpenAPI schema：缓存 > 远端 > 内置快照兜底。

    远端拉取失败时回退到安装包内置快照，并通过 `contract_source()` 暴露本次来源。
    """
    global _last_source
    base_url, token = config.resolve(base_url, token)
    cached = None if refresh else _read_cache()

    if cached and not refresh:
        _ver = str(cached.get("info", {}).get("version", ""))
        versioned = cache_path(_ver)
        if versioned.exists():
            _last_source = {"kind": "cache", "detail": _ver or "current"}
            return cached

    try:
        schema = fetch_schema(base_url, token)
    except Exception as e:
        bundled = load_bundled()
        if bundled is None:
            raise
        _ver = str(bundled.get("info", {}).get("version", ""))
        _last_source = {
            "kind": "bundled",
            "detail": f"v{_ver} (remote unreachable: {e})",
        }
        return bundled

    _ver = str(schema.get("info", {}).get("version", ""))
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cache_path(_ver).write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    _last_source = {"kind": "remote", "detail": _ver or "current"}
    return schema


def contract_source() -> dict:
    """返回最近一次 load_contract 的契约来源：{"kind": remote|cache|bundled, "detail": str}。"""
    return dict(_last_source)