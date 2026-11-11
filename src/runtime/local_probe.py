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
    """探测单个引擎：跑 [cli, --version]（可用 probe_args 覆盖），healthy=returncode==0。

    hermes 标记 acp_probe：--version 探测不出 ACP 依赖缺失（可选 extra 不被
    触碰），须对 `hermes acp` 做真实 ACP 握手探测（acp_bootstrap._acp_check，
    Issue #613：`acp --check` 子命令在 hermes v0.9.0 起不存在）。
    """
    cli = eng.get("cli_path") or ""
    if not cli or not eng.get("installed"):
        return {**eng, "version": "", "healthy": False}
    out = {**eng}
    if (eng.get("name") or "").lower() == "atomcode":
        from src.runtime.atomcode_probe import cached_atomcode_probe
        result = cached_atomcode_probe(cli)
        return {**eng, "healthy": result.get("status") == "ok", "version": eng.get("version", "")}
    out.pop("probe_args", None)
    acp_probe = out.pop("acp_probe", None)
    try:
        if acp_probe:
            # hermes：版本走 --version，健康走真实 ACP 握手（与 worker 注册口径一致）
            from src.runtime.acp_bootstrap import _acp_check
            res = subprocess.run([cli, "--version"], capture_output=True, text=True,
                                 timeout=LOCAL_PROBE_TIMEOUT)
            version = (res.stdout or "").strip().split("\n")[0][:60] if res.returncode == 0 else ""
            out.update({"version": version, "healthy": _acp_check(cli)})
            return out
        args = eng.get("probe_args") or ["--version"]
        res = subprocess.run([cli] + args, capture_output=True, text=True, timeout=LOCAL_PROBE_TIMEOUT)
        healthy = res.returncode == 0
        version = (res.stdout or "").strip().split("\n")[0][:60] if healthy else ""
        out.update({"version": version, "healthy": healthy})
        return out
    except Exception:
        out.update({"version": "", "healthy": False})
        return out


def probe_local_engines(engines: list | None = None) -> list[dict]:
    """探测本机已安装引擎，返回 [{name, version, healthy, cli_path, installed, ...}]。

    engines 缺省 = `discover_backends()`（含 cli_path/installed）。
    hermes 自动带 acp_probe=True（真实 ACP 握手探测，ACP 依赖缺失 →
    healthy=False，与 worker 注册口径一致，见 hermes-acp-auto-bootstrap-design.md
    与 Issue #613）。
    线程池并发 + 单引擎 5s 超时 + 总时长 ≤ LOCAL_PROBE_MAX_TOTAL（超时未完成引擎置 healthy=False）。
    """
    if engines is None:
        from src.agent.executor import discover_backends
        engines = discover_backends()
    engines = [
        {**e, "acp_probe": True}
        if (e.get("name") or "").lower() == "hermes" and e.get("installed") and not e.get("acp_probe")
        else e
        for e in engines
    ]
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
