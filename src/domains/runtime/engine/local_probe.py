"""本机直连引擎真实健康探测（方案 C，local-direct-health-probe-design.md）。

远端 worker 已在其主机跑 `--version` 探测本机 CLI；本机直连引擎同样可探测，
统一走此模块：线程池 + 单引擎超时 + 总时长限制，不阻塞事件循环。
"""
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _CFTimeout

from src.kernel.config import log

LOCAL_PROBE_TIMEOUT = 5          # 单引擎 --version 超时（秒），与 worker 现有实现（agent.py:46）一致
# 全量探测总上限（秒）。Issue #2545：25 → 35——hermes 探活由两段扩为三段
# （--version 5s + ACP 握手预算 + 最小模型调用 5s），原 25s 预算下最坏情形
# 会撞总闸被误标 healthy=False（Spec 验收：不误判）。
LOCAL_PROBE_MAX_TOTAL = 35
MODEL_CALL_TIMEOUT = 5           # hermes 最小模型调用超时（秒，Issue #2545）——沿用单引擎超时预算


def _hermes_model_call_probe(timeout: float = MODEL_CALL_TIMEOUT) -> tuple[bool, dict]:
    """hermes 最小模型调用探针（Issue #2545）。

    ACP 握手只能证明引擎可起，暴露不了「模型名不在该 key 所属网关可用列表」——
    该错配此前 healthy 但实际 chat 404 Model not found。此处在握手通过后补一次
    真实 1-token 补全（engine_model.minimal_model_call，统一模型名来源）：
      404/模型不存在 → (False, {"model_call": "model_not_found: <模型名>"}) → 探针置 unhealthy
      其余（200/其它状态/网络不可达/无配置）→ (True, detail) 不误判。
    任何异常都不让探针崩（单引擎失败不影响其他引擎）。
    """
    from src.domains.runtime.engine import engine_model
    try:
        cfg = engine_model.read_hermes_model_config()
        if not cfg:
            return True, {}
        return engine_model.minimal_model_call(cfg, timeout=timeout)
    except Exception as e:
        return True, {"model_call": "skipped: probe error (%s)" % e}


def _probe_one(eng: dict) -> dict:
    """探测单个引擎：跑 [cli, --version]（非 hermes 可用 probe_args 覆盖），healthy=returncode==0。

    Issue #1982 C1：保留非 hermes 引擎的 probe_args 覆盖能力（自定义探测命令，
    假 CLI/非标准 CLI 场景）；hermes 不消费 probe_args，统一 `--version` 取版本号、
    健康态走真实 ACP 握手（acp_probe=True）：`--version` 探测不出 ACP 依赖缺失
    （可选 extra 不被触碰），且 Issue #1982 起不再自动补装，只用握手结果 + 诊断
    信息反映真实可用性。
    """
    cli = eng.get("cli_path") or ""
    if not cli or not eng.get("installed"):
        return {**eng, "version": "", "healthy": False, "probe_status": "failed", "probe_latency": 0}
    try:
        _t0 = time.monotonic()
        _probe_detail = {}   # Issue #2545：模型调用探针详情（ACP 分支填充，全路径可见）
        # hermes 固定 --version（健康由 acp_probe 握手判定）；其余引擎保留 probe_args 覆盖
        if (eng.get("name") or "").lower() == "hermes":
            _args = ["--version"]
        else:
            _args = eng.get("probe_args") or ["--version"]
        res = subprocess.run([cli] + _args, capture_output=True, text=True,
                             timeout=LOCAL_PROBE_TIMEOUT)
        latency_ms = int((time.monotonic() - _t0) * 1000)
        healthy = res.returncode == 0
        version = (res.stdout or "").strip().split("\n")[0][:60] if healthy else ""
        if healthy and eng.get("acp_probe"):
            from src.domains.runtime.engine.acp_bootstrap import _acp_check
            # Issue #1982 R1：握手等待显式传剩余预算（≤LOCAL_PROBE_TIMEOUT 单引擎口径），
            # 不落 ACP_CHECK_TIMEOUT 默认 20s（服务端心跳口径，超出本机直连单引擎预算）。
            _budget = round(max(1.0, LOCAL_PROBE_TIMEOUT - (time.monotonic() - _t0)), 2)
            healthy = _acp_check(cli, timeout=_budget)
            # Issue #2545：握手通过后再做一次最小模型调用——模型名 vs 网关 key
            # 可用列表不对齐 → healthy=False + probe_detail 带 model_not_found 原因
            # （而非 healthy 但实际 chat 404 Model not found）。arch11 版 probe_status
            # 语义保持（ok/failed 随最终 healthy 落库）。
            if healthy:
                healthy, _probe_detail = _hermes_model_call_probe()
        # probe_status 必须随结果一起返回：upsert 侧只认这几个字段，
        # 缺了它 agent_runtimes.probe_status 就永远停在 pending，
        # 健康判定把"从未探测"当成"低分/连败"，claim 直接被拒（2026-11-15 实证）。
        out = {**eng, "version": version, "healthy": healthy,
               "probe_status": "ok" if healthy else "failed",
               "probe_latency": latency_ms}
        if _probe_detail:
            out["probe_detail"] = _probe_detail
        out.pop("probe_args", None)
        return out
    except Exception:
        out = {**eng, "version": "", "healthy": False, "probe_status": "failed", "probe_latency": 0}
        out.pop("probe_args", None)
        return out


def probe_local_engines(engines: list | None = None) -> list[dict]:
    """探测本机已安装引擎，返回 [{name, version, healthy, cli_path, installed, ...}]。

    engines 缺省 = `discover_backends()`（含 cli_path/installed）。
    hermes 自动置 acp_probe=True（健康走真实 ACP 握手，ACP 依赖缺失 → healthy=False，
    与 worker 注册口径一致，见 hermes-acp-auto-bootstrap-design.md）；非 hermes 引擎
    保留 probe_args 覆盖（缺省 ["--version"]）。
    线程池并发 + 单引擎 5s 超时 + 总时长 ≤ LOCAL_PROBE_MAX_TOTAL（超时未完成引擎置 healthy=False）。
    """
    if engines is None:
        from src.domains.runtime.engine.executor import discover_backends
        engines = discover_backends()
    engines = [
        {**e, "acp_probe": True}
        if (e.get("name") or "").lower() == "hermes" and e.get("installed")
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
                    results.append({**pending[fut], "version": "", "healthy": False, "probe_status": "failed", "probe_latency": 0})
        except _CFTimeout:
            for fut, eng in pending.items():
                if not fut.done():
                    results.append({**eng, "version": "", "healthy": False, "probe_status": "failed", "probe_latency": 0})
    return results
