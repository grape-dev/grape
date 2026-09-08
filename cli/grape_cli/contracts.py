"""契约加载：读 /openapi.json → 缓存 ~/.grape/openapi-<version>.json → 版本检测 → refresh。"""
import json
from pathlib import Path

from grape_cli import config
from grape_cli.client import ApiClient, CliError


def cache_path(version: str = "") -> Path:
    return config.CONFIG_DIR / f"openapi-{version}.json" if version else config.CONFIG_DIR / "openapi-current.json"


def _read_cache() -> dict | None:
    for f in sorted(config.CONFIG_DIR.glob("openapi-*.json"), reverse=True):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def fetch_schema(base_url: str, token: str) -> dict:
    api = ApiClient(base_url=base_url, token=token)
    _, data = api.request("GET", "/openapi.json")
    if not isinstance(data, dict) or "paths" not in data:
        raise CliError("openapi.json 契约格式异常")
    return data


def load_contract(base_url: str = "", token: str = "", refresh: bool = False) -> dict:
    """返回 OpenAPI schema：优先缓存（按 version），refresh 或版本变化时重新拉取。"""
    base_url, token = config.resolve(base_url, token)
    cached = None if refresh else _read_cache()

    if cached and not refresh:
        _ver = str(cached.get("info", {}).get("version", ""))
        versioned = cache_path(_ver)
        if versioned.exists():
            return cached

    schema = fetch_schema(base_url, token)
    _ver = str(schema.get("info", {}).get("version", ""))
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cache_path(_ver).write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return schema