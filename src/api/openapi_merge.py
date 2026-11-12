"""对外 OpenAPI 契约聚合（4 进程拆分后的单一事实源）。

背景（2026-11-10 事故）：arch 四进程拆分后，tasks / patrol / pipeline-handoff 等路由
迁到 worker / insight 进程，而 `src/api/server_tasks.py` 的契约落盘只在**当前进程**里
渲染（mgmt），于是 /static/openapi.json 丢了 7 条路径 → 契约驱动的 `grape` CLI
没有 `tasks` 命令组，`grape tasks complete` 不可用。

做法：契约 owner（mgmt）在落盘阶段把另外三个进程的 spec 拉出来合并——
不改变任何路由归属，只补齐对外契约。合并失败时降级为自身 spec（并告警），不影响启动。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

# 合并后的对外契约版本（bump 触发 CLI 自动刷新契约）
CONTRACT_VERSION = "2.2.2"

ENTRY_MODULES = {
    "mgmt": "src.api.grape_server",
    "event": "src.api.grape_event_server",
    "worker": "src.api.grape_worker_server",
    "insight": "src.api.grape_insight_server",
}

_ROLE = (os.environ.get("GRAPE_PROCESS_ROLE") or "").strip().lower()

_RENDER_TPL = (
    "import json,sys;"
    "from importlib import import_module;"
    "from src.api.openapi import custom_openapi;"
    "m=import_module(sys.argv[1]);"
    "print(json.dumps(custom_openapi(m.app), ensure_ascii=False))"
)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def render_peer_spec(module: str, timeout: float = 120.0) -> dict | None:
    """在子进程里渲染某个入口模块的对外 spec（避免把其它 app 拉进本进程）。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _RENDER_TPL, module],
            cwd=_repo_root(),
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired:
        log.warning("OpenAPI merge: render %s timeout %.0fs", module, timeout)
        return None
    if proc.returncode != 0:
        tail = (proc.stderr or b"")[-400:].decode(errors="replace")
        log.warning("OpenAPI merge: render %s failed rc=%s: %s", module, proc.returncode, tail)
        return None
    try:
        return json.loads(proc.stdout.decode() or "{}")
    except Exception as e:
        log.warning("OpenAPI merge: parse %s spec failed: %s", module, e)
        return None


def merge_specs(own: dict, peers: list[dict], version: str = CONTRACT_VERSION) -> dict:
    """把多份 spec 合并成一份：paths 按 (path, method) 去重，components/tags 并集。"""
    merged = json.loads(json.dumps(own))  # deep copy，避免改到 app.openapi_schema
    merged.setdefault("paths", {})
    merged.setdefault("components", {})
    merged.setdefault("tags", [])

    _own_tag_names = {t.get("name") for t in merged["tags"] if isinstance(t, dict)}
    for spec in peers:
        if not isinstance(spec, dict):
            continue
        for path, ops in (spec.get("paths") or {}).items():
            if not isinstance(ops, dict):
                continue
            dst = merged["paths"].setdefault(path, {})
            for method, op in ops.items():
                if method in dst:
                    continue  # 同 path+method 以本进程为准（避免覆盖已渲染的 operationId/security）
                dst[method] = op
        for section, items in (spec.get("components") or {}).items():
            if isinstance(items, dict):
                bucket = merged["components"].setdefault(section, {})
                for name, val in items.items():
                    bucket.setdefault(name, val)
        for tag in spec.get("tags") or []:
            if isinstance(tag, dict) and tag.get("name") not in _own_tag_names:
                merged["tags"].append(tag)
                _own_tag_names.add(tag.get("name"))

    info = merged.setdefault("info", {})
    info["version"] = version
    merged.setdefault("openapi", own.get("openapi", "3.1.0"))
    return merged


_DUMPED = False


def dump_contract(app) -> int:
    """落盘对外契约（幂等）。

    历史上 mgmt 启动有两处各自落盘：server_tasks（合并版 269 条）与 grape_server
    （仅本进程 265 条）——后写的把合并结果覆盖掉，导致 CLI 契约缺 tasks/pipeline-handoff。
    这里收敛为唯一入口：进程内只落一次，合并失败自动降级为自身 spec（不阻断启动）。
    返回写入的路径数。
    """
    global _DUMPED
    if _DUMPED:
        return 0
    from src.kernel.help_page_gen import write_openapi_contract
    schema = build_contract(app)
    n = write_openapi_contract(schema)
    _DUMPED = True
    log.info(f"OpenAPI: contract dumped ({n} paths, version={schema.get('info', {}).get('version')}) "
             f"-> /static/openapi.json")
    return n


def build_contract(own_app) -> dict:
    """构造对外契约：非 mgmt 进程只返回自身 spec；mgmt 合并四进程。"""
    from src.api.openapi import custom_openapi

    own = custom_openapi(own_app)
    if _ROLE and _ROLE != "mgmt":
        return own

    peers = []
    for role, module in ENTRY_MODULES.items():
        if role == "mgmt":
            continue
        spec = render_peer_spec(module)
        if spec:
            peers.append(spec)
    if not peers:
        log.warning("OpenAPI merge: no peer specs collected, publishing own spec only")
        return merge_specs(own, [], version=CONTRACT_VERSION)
    return merge_specs(own, peers, version=CONTRACT_VERSION)