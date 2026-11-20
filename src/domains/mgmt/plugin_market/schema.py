"""plugin.schema.json 加载器（缓存）。

schema 是插件市场清单标准的权威（Issue #1079），随 contracts/ 目录分发；
按仓库根（src/plugin_market 的上两级）定位，兼容从任意 CWD 导入。
"""
import json
import os
from pathlib import Path

_CACHE: dict | None = None

SCHEMA_REL = os.path.join("contracts", "plugin.schema.json")

# D7 引擎全集（与 src/agent/executor.py _BACKENDS 保持一致）
PLATFORM_ENGINES = ["opencode", "codearts", "hermes", "codex", "dsh", "hwcloud"]
MCP_ENGINES = ["codex"]


def _repo_root_candidates():
    here = Path(__file__).resolve()
    # arch11 目录层级加深（src/domains/mgmt/plugin_market → 仓库根 4 级）
    yield here.parents[4]
    yield here.parents[2]
    cwd = Path.cwd()
    yield cwd
    yield cwd.parent


def load_plugin_schema() -> dict:
    """返回 plugin.schema.json 内容（进程内缓存）。找不到则抛 FileNotFoundError。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    for root in _repo_root_candidates():
        path = root / SCHEMA_REL
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                _CACHE = json.load(f)
            return _CACHE
    raise FileNotFoundError(
        f"plugin.schema.json 未找到（查找路径: {SCHEMA_REL}，基点依次为模块目录上两级/当前工作目录及其上级）"
    )
