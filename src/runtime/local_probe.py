"""本机直连引擎真实健康探测（方案 C，local-direct-health-probe-design.md）。

远端 worker 已在其主机跑 `--version` 探测本机 CLI；本机直连引擎同样可探测，
统一走此模块：线程池 + 单引擎超时 + 总时长限制，不阻塞事件循环。
"""
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _CFTimeout

from src.config import log

LOCAL_PROBE_TIMEOUT = 5          # 单引擎 --version 超时（秒），与 worker 现有实现（agent.py:46）一致
LOCAL_PROBE_MAX_TOTAL = 25       # 全量探测总上限（秒）


def _probe_one(eng: dict) -> dict:
    """探测单个引擎：跑 [cli, --version]（可用 probe_args 覆盖），healthy=returncode==0。"""
    cli = eng.get("cli_path") or ""
    if not cli or not eng.get("installed"):
        return {**eng, "version": "", "healthy": False}
    try:
        args = eng.get("probe_args") or ["--version"]
        res = subprocess.run([cli] + args, capture_output=True, text=True, timeout=LOCAL_PROBE_TIMEOUT)
        healthy = res.returncode == 0
        version = (res.stdout or "").strip().split("\n")[0][:60] if healthy else ""
        out = {**eng, "version": version, "healthy": healthy}
        out.pop("probe_args", None)
        return out
    except Exception:
        out = {**eng, "version": "", "healthy": False}
        out.pop("probe_args", None)
        return out


def probe_local_engines(engines: list | None = None) -> list[dict]:
    """探测本机已安装引擎，返回 [{name, version, healthy, cli_path, installed, ...}]。

    engines 缺省 = `discover_backends()`（含 cli_path/installed）。
    线程池并发 + 单引擎 5s 超时 + 总时长 ≤ LOCAL_PROBE_MAX_TOTAL（超时未完成引擎置 healthy=False）。
    """
    if engines is None:
        from src.agent.executor import discover_backends
        engines = discover_backends()
    if not engines:
        return []
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(engines), 8)) as ex:
        pending = {ex.submit(_probe_one, e): e for e in engines}
        try:
            for fut in as_completed(pending, timeout=LOCAL_PROBE_MAX_TOTAL):
                try:
                    results.append(fut.result())
                except Exception:
                    results.append({**pending[fut], "version": "", "healthy": False})
        except _CFTimeout:
            for fut, eng in pending.items():
                if not fut.done():
                    results.append({**eng, "version": "", "healthy": False})
    return results
