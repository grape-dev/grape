"""配置解析：CLI 参数 > 环境变量 > ~/.grape/config.json。

token 优先级：--token 参数 > `GRAPE_SESSION_TOKEN`（任务级临时 token，worker 优先）> `GRAPE_API_TOKEN`（用户级）> config.json。
"""
import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".grape"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_file_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def resolve(base_url: str = "", token: str = "", refresh: bool = False) -> tuple[str, str]:
    """返回 (base_url, token)。缺任一 → 抛 if config incomplete."""
    cfg = load_file_config() if not refresh else {}
    env = os.environ

    _base = (base_url or "").strip() or (env.get("GRAPE_BASE_URL") or "").strip() or str(cfg.get("base_url") or "").strip()
    if not _base:
        _base = "http://localhost:8000"

    _token = (token or "").strip() or (env.get("GRAPE_SESSION_TOKEN") or "").strip() or \
             (env.get("GRAPE_API_TOKEN") or "").strip() or str(cfg.get("token") or "").strip()
    return _base.rstrip("/"), _token


def save_config(base_url: str, token: str = ""):
    cfg = load_file_config()
    if base_url:
        cfg["base_url"] = base_url
    if token:
        cfg["token"] = token
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")