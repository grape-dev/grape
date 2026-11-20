"""Issue #1616：worker 启动链路契约完整性校验 + 缓存重建。

故障形态：runtime-only 契约（54 endpoints，仅 /api/runtime/*）覆盖 CLI 全量缓存
→ git-repo/issues/scenes/a2a/knowledge/repos 六组业务命令整组消失。

校验口径（Spec）：端点数量 ≥ 全量基线（261）+ 六个必备 path 前缀；
缺失即从主控重新拉取全量契约重建缓存并告警，禁止静默进入 runtime-only；
不 fail-fast（重建失败仅告警，不阻断 worker 启动）。

Issue #2085：检出退化/重建失败在 say() 日志之外外发结构化告警信号
（可注入 alert_sink）——经 http_alert_sink 上报主控 observability 管线，
落 grape_contract_guard_failures_total 指标，供 deploy/alerts.yml
GrapeContractGuardFailed 规则消费（退化与重建失败按 outcome 分开计数）。

seam（公开接口）：
- contract_issues(schema) 纯校验；
- run_startup_check(...) 注入 config_dir/fetch/alert_sink 的启动检查入口；
- http_alert_sink(...) worker→主控信号上报（死地址落本地兜底文件）；
- python -m src.domains.runtime.engine.contract_guard --preflight（启动脚本 preflight）。
"""
import json
import os
import sys
import time
import urllib.request

# 自包含模块：不 import src.api.*（openapi.py 不在 worker 包 MANIFEST，
# worker 节点只有 src/runtime 闭包）。
# 同口径说明（QA 复审④）：REQUIRED_PATH_PREFIXES/DEFAULT_MIN_ENDPOINTS 为六组前缀与
# 基线的权威定义，测试侧一律 import 不重复定义；<version>:<source> 缓存 key、
# 缓存读取（_cache_version_key/load_cached_contract）与拉取优先级（CONTRACT_FETCH_PATHS，
# 镜像 grape-cli contracts._CONTRACT_PATHS）与 CLI 侧为自包含双实现——CLI 改缓存
# 命名/路径会静默漂移，口径由 tests/test_issue_1616_contract_guard.py::
# test_guard_cache_key_matches_server_openapi_module 及 grape-cli 仓跟踪看护。

# 六组必备业务 path 前缀（Issue #1616 Spec 验收口径）
REQUIRED_PATH_PREFIXES = (
    "/api/git-repo/",
    "/api/issues",
    "/api/scenes",
    "/api/a2a",
    "/api/knowledge",
    "/api/repos",
)

# 全量契约端点基线（2026-11 实测 261；runtime-only 退化形态约 54）
DEFAULT_MIN_ENDPOINTS = 261

# CLI 拉取契约的优先路径（与 grape-cli contracts._CONTRACT_PATHS 一致：
# /static/openapi.json 公开无鉴权优先，/openapi.json 兼容兜底）
CONTRACT_FETCH_PATHS = ("/static/openapi.json", "/openapi.json")

GUARD_DISABLE_ENV = "GRAPE_CONTRACT_GUARD_DISABLED"
MIN_ENDPOINTS_ENV = "GRAPE_CONTRACT_MIN_ENDPOINTS"
CONFIG_DIR_ENV = "GRAPE_CONFIG_DIR"
FETCH_TIMEOUT_ENV = "GRAPE_CONTRACT_FETCH_TIMEOUT"

# Issue #2085：guard 失败信号外发口径（权威定义，测试 import 不重复）。
# - degraded：检出 CLI 缓存退化/缺失（即便随后重建成功也计——退化本身是事件）；
# - rebuild_failed：从主控重建缓存失败（warn-only，worker 继续带退化契约跑）。
# 与 src/observability.CONTRACT_GUARD_OUTCOMES 为自包含双实现（本模块不 import
# src.observability——worker 包闭包内没有它），口径由漂移看护测试锁定。
ALERT_OUTCOMES = ("degraded", "rebuild_failed")

# worker→主控的信号上报端点（src/api/routes/runtime.py 承接 → observability 计数）
ALERT_SIGNAL_PATH = "/api/runtime/contract-guard/signal"

# 上报失败（死地址/无 token）的本地兜底落点：cache_dir 内追加写 JSONL，
# 值守可从此文件发现「上报链路自身不可用」的 worker（恰逢死地址的场景）。
ALERT_FALLBACK_FILE = "contract-guard-alerts.jsonl"

# 重建拉取超时（秒）：启动链路调用，双路径最坏 2×timeout，取小值避免拖慢 worker 启动
FETCH_TIMEOUT_S = 8.0


def fetch_timeout() -> float:
    """重建拉取超时（GRAPE_CONTRACT_FETCH_TIMEOUT 可覆盖；非法值回退默认）。"""
    raw = (os.environ.get(FETCH_TIMEOUT_ENV) or "").strip()
    try:
        return float(raw) if raw else FETCH_TIMEOUT_S
    except ValueError:
        return FETCH_TIMEOUT_S


def min_endpoints() -> int:
    """端点数量基线（可用 GRAPE_CONTRACT_MIN_ENDPOINTS 覆盖；非法值回退默认）。"""
    raw = (os.environ.get(MIN_ENDPOINTS_ENV) or "").strip()
    try:
        return int(raw) if raw else DEFAULT_MIN_ENDPOINTS
    except ValueError:
        return DEFAULT_MIN_ENDPOINTS


def contract_issues(schema: dict | None, floor: int | None = None) -> list[str]:
    """契约完整性校验：返回问题列表（空列表=健康）。

    - 非 dict / 无 paths → 契约非法；
    - paths 数 < 基线 → 端点数量不足（runtime-only 退化特征）；
    - 六组业务 path 前缀任一缺失 → 业务命令组缺失。
    """
    floor = min_endpoints() if floor is None else floor
    if not isinstance(schema, dict):
        return ["contract is not an object"]
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        return ["contract has no paths"]
    issues: list[str] = []
    if len(paths) < floor:
        issues.append(f"endpoint count {len(paths)} < full baseline {floor} (runtime-only degradation?)")
    for prefix in REQUIRED_PATH_PREFIXES:
        if not any(str(p).startswith(prefix) for p in paths):
            issues.append(f"missing business path prefix: {prefix}")
    return issues


def cache_dir(config_dir: str | os.PathLike | None = None):
    """CLI 契约缓存目录（~/.grape；GRAPE_CONFIG_DIR 可覆盖，测试注入用）。"""
    import pathlib
    if config_dir:
        return pathlib.Path(config_dir)
    override = (os.environ.get(CONFIG_DIR_ENV) or "").strip()
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".grape"


def _cache_version_key(name: str) -> tuple:
    """与 grape-cli contracts._cache_version_key 同口径：纯数字点分版本按数值比较，
    其余（含带来源后缀）视为 (0,)——保证这里选中与 CLI 相同的缓存文件。"""
    stem = name[len("openapi-"):]
    if stem.endswith(".json"):
        stem = stem[:-len(".json")]
    parts = stem.split(".")
    if parts and all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return (0,)


def load_cached_contract(config_dir: str | os.PathLike | None = None) -> dict | None:
    """读取 CLI 缓存中版本最高的契约（与 CLI _read_cache 同口径），读不到返回 None。"""
    d = cache_dir(config_dir)
    try:
        candidates = sorted(d.glob("openapi-*.json"),
                            key=lambda p: _cache_version_key(p.name), reverse=True)
    except Exception:
        return None
    for f in candidates:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def resolve_base_url(config_dir: str | os.PathLike | None = None) -> str:
    """主控地址解析：GRAPE_SERVER_URL > GRAPE_BASE_URL > ~/.grape/config.json base_url。"""
    for env in ("GRAPE_SERVER_URL", "GRAPE_BASE_URL"):
        v = (os.environ.get(env) or "").strip()
        if v:
            return v.rstrip("/")
    try:
        cfg_file = cache_dir(config_dir) / "config.json"
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        v = str(cfg.get("base_url") or "").strip()
        if v:
            return v.rstrip("/")
    except Exception:
        pass
    return ""


def worker_server_url(config_dir: str | os.PathLike | None = None) -> str:
    """信号上报用主控地址：RUNTIME_SERVER_URL（worker 运行时权威）> resolve_base_url 同口径。"""
    v = (os.environ.get("RUNTIME_SERVER_URL") or "").strip()
    if v:
        return v.rstrip("/")
    return resolve_base_url(config_dir)


def http_alert_sink(base_url: str | None = None, token: str | None = None,
                    config_dir: str | os.PathLike | None = None,
                    timeout: float | None = None):
    """构造 guard 失败信号外发 sink（worker→主控 observability 管线，Issue #2085）。

    - base_url 缺省 worker_server_url()；token 缺省读 RUNTIME_TOKEN；
    - 信号经 urllib POST {base_url}/api/runtime/contract-guard/signal 上报主控，
      主控承接后计 grape_contract_guard_failures_total{outcome=...} 指标
      （deploy/alerts.yml GrapeContractGuardFailed 规则消费）；
    - 上报失败（无地址/无 token/网络不可达/非 2xx）→ 追加写
      cache_dir/contract-guard-alerts.jsonl 本地兜底落点并返回 False——
      死地址场景恰是要告警的场景，上报链路自身不可用必须可诊断；
    - **永不抛异常**（warn-only：告警链路故障不得阻断 worker 启动）。

    返回 sink(payload) -> bool：True=已送达主控；False=已落本地兜底。
    payload 至少含 {"outcome": degraded|rebuild_failed, "detail": str}，
    sink 就地补充 host/pid/ts 供主控日志与兜底文件甄别。
    """
    if timeout is None:
        timeout = fetch_timeout()
    if token is None:
        token = (os.environ.get("RUNTIME_TOKEN") or "").strip()

    def _sink(signal: dict) -> bool:
        import socket
        payload = dict(signal)
        payload.setdefault("host", socket.gethostname())
        payload.setdefault("pid", os.getpid())
        payload.setdefault("ts", time.time())
        url = (base_url if base_url is not None else worker_server_url(config_dir)).rstrip("/")
        ok = False
        if url and token:
            try:
                req = urllib.request.Request(
                    f"{url}{ALERT_SIGNAL_PATH}",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = getattr(resp, "status", None)
                    if status is None:
                        status = resp.getcode()
                    ok = status is not None and 200 <= status < 300
            except Exception:
                ok = False
        if not ok:
            try:
                d = cache_dir(config_dir)
                d.mkdir(parents=True, exist_ok=True)
                line = dict(payload)
                line["fallback"] = "alert-sink-unreachable"
                with open(d / ALERT_FALLBACK_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
            except Exception:
                pass  # 兜底落点也失败（如磁盘不可写）：仅静默，不阻断启动
        return ok

    return _sink


def fetch_full_contract(base_url: str, timeout: float | None = None,
                        fetch_paths: tuple = CONTRACT_FETCH_PATHS) -> dict:
    """按 CLI 同款优先级拉取全量契约（/static/openapi.json → /openapi.json）。

    timeout 缺省与重建链路同口径（fetch_timeout()：GRAPE_CONTRACT_FETCH_TIMEOUT
    可覆盖，默认 FETCH_TIMEOUT_S=8.0）；QA 复审②：不再保留独立的 15.0 默认值，
    避免直接调用方拿到与生产意图相悖的超时。显式传参仍最高优先。
    任一路成功即返回；两路都失败抛 OSError（调用方告警降级，不阻断启动）。
    """
    if timeout is None:
        timeout = fetch_timeout()
    if not base_url:
        raise OSError("no base_url resolved (set GRAPE_SERVER_URL or ~/.grape/config.json)")
    last_err: Exception | None = None
    for path in fetch_paths:
        url = f"{base_url}{path}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("paths"), dict):
                return data
            last_err = OSError(f"contract malformed (missing paths): {path}")
        except Exception as e:
            last_err = e
    raise OSError(f"contract unreachable from any of {list(fetch_paths)} ({last_err})")


def _contract_source(schema: dict) -> str:
    """读契约来源标识（info.x-contract-source），未标识返回空串（旧契约兼容）。

    与 src/api/openapi.py contract_source 同口径；本模块自包含不 import src.api
    （openapi.py 不在 worker 包 MANIFEST），双实现口径由测试锁定。
    """
    return str((schema.get("info") or {}).get("x-contract-source") or "")


def _contract_cache_key(schema: dict) -> str:
    """带来源标识的缓存 key：`<version>:<source>`（与 src/api/openapi.py
    contract_cache_key 同口径，QA 复审① TA-2 接线的 guard 侧消费点）。

    CLI 槽位文件名必须保持 openapi-<version>.json 纯版本命名（grape-cli
    读缓存的兼容约束，CLI 侧同名消费由 grape-dev/grape-cli#134 跟踪）；
    同版本号 full/runtime 的区分只经本 key 表达——guard 用它做槽位写入
    校验，重建结果日志携带 key 供甄别。
    """
    ver = str((schema.get("info") or {}).get("version") or "unknown")
    src = _contract_source(schema) or "unknown"
    return f"{ver}:{src}"


def _write_cache_slot(config_dir: str | os.PathLike | None, schema: dict) -> str:
    """把契约原子写入 CLI 缓存槽位 openapi-<version>.json，返回目标文件名。

    来源标识参与写入校验（TA-2）：显式 x-contract-source=runtime 的契约
    禁止占用 CLI 全量槽位（#1616 故障形态的写入口径兜底）；未标识（旧
    契约兼容）与 full 放行。
    """
    src = _contract_source(schema)
    if src == "runtime":
        raise ValueError(
            f"refuse {src} contract in CLI full-contract slot ({_contract_cache_key(schema)})")
    ver = str((schema.get("info") or {}).get("version") or "unknown")
    d = cache_dir(config_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"openapi-{ver}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return target.name


def rebuild_contract_cache(config_dir: str | os.PathLike | None, base_url: str,
                           timeout: float | None = None, fetch=None) -> tuple[bool, str]:
    """重建 CLI 契约缓存：拉全量契约 → 原子写入 openapi-<version>.json（CLI 缓存槽位）。

    timeout 缺省与 fetch_full_contract 同口径（fetch_timeout()，默认 8.0，env 可覆盖）；
    QA 复审②：直接调用方不再拿到独立的 15.0 默认值。
    fetch 可注入（测试）；默认走 fetch_full_contract（/static → /openapi.json）。
    返回 (是否成功, 描述)。写同版本号文件即以全量契约占住 CLI 缓存槽位，
    消除 runtime-only 与全量契约同版本号互相覆盖的退化态。
    """
    try:
        schema = fetch(base_url) if fetch is not None else fetch_full_contract(base_url, timeout=timeout)
    except Exception as e:
        return False, f"fetch failed: {e}"
    issues = contract_issues(schema)
    if issues:
        return False, f"fetched contract unhealthy: {'; '.join(issues)}"
    try:
        name = _write_cache_slot(config_dir, schema)
        return True, f"rebuilt {name} [{_contract_cache_key(schema)}] ({len(schema['paths'])} paths)"
    except Exception as e:
        return False, f"cache write failed: {e}"


def run_startup_check(config_dir: str | os.PathLike | None = None,
                      base_url: str | None = None,
                      fetch=None,
                      log=None,
                      alert_sink=None) -> dict:
    """worker 启动契约完整性检查入口（seam）。

    - 缓存健康 → {"status": "ok", "action": "none"}；
    - 缓存缺失/退化 → 告警 + 重建（fetch 可注入供测试；默认走 fetch_full_contract）；
    - 重建失败仅告警（返回 status=failed），不抛异常、不阻断启动。
    - Issue #2085：检出退化/重建失败在日志之外经 alert_sink 外发结构化信号
      （outcome=degraded / rebuild_failed，两类事件分开计数）；alert_sink=None
      时行为与 #1616 完全一致；sink 故障按 warn-only 处理（不阻断、留日志）。
    """
    say = log or (lambda msg: None)

    def _emit(signal: dict) -> None:
        if alert_sink is None:
            return
        try:
            delivered = alert_sink(signal)
        except Exception as e:
            say(f"contract guard: alert sink error ({e}) — signal dropped, startup continues")
            return
        if delivered is False:
            say(f"contract guard: alert signal fell back to local {ALERT_FALLBACK_FILE} "
                f"(master unreachable or not authenticated)")

    cached = load_cached_contract(config_dir)
    issues = contract_issues(cached)
    if not issues:
        ver = str((cached.get("info") or {}).get("version") or "unknown")
        return {"status": "ok", "action": "none", "detail": f"cache healthy (v{ver}, {len(cached['paths'])} paths)"}
    reason = "; ".join(issues) if issues else "cache missing"
    say(f"contract guard: CLI contract cache degraded ({reason}) — rebuilding from server")
    _emit({"outcome": "degraded", "detail": reason})
    url = base_url if base_url is not None else resolve_base_url(config_dir)
    ok, detail = rebuild_contract_cache(config_dir, url, timeout=fetch_timeout(), fetch=fetch)
    if not ok:
        say(f"contract guard: rebuild failed ({detail}) — worker continues with degraded contract")
        _emit({"outcome": "rebuild_failed", "detail": detail})
        return {"status": "failed", "action": "rebuild", "detail": detail}
    say(f"contract guard: {detail}")
    return {"status": "rebuilt", "action": "rebuild", "detail": detail}


def main(argv: list[str] | None = None) -> int:
    """preflight 入口（启动脚本调用 `python -m src.domains.runtime.engine.contract_guard --preflight`）。

    warn-only：检查/重建失败退出码 3（调用方仅打印告警），不阻断 worker 启动。
    GRAPE_CONTRACT_GUARD_DISABLED=true 跳过检查（测试/CI 逃生口）。
    """
    argv = [a for a in (sys.argv[1:] if argv is None else argv) if a not in ("--preflight", "-q")]
    if (os.environ.get(GUARD_DISABLE_ENV) or "").strip().lower() in ("1", "true", "yes"):
        return 0

    def _log(msg: str):
        print(f"[start-worker] {msg}", file=sys.stderr)

    # Issue #2085：preflight 链路同样外发失败信号（运行时 env 已由启动脚本 source，
    # RUNTIME_TOKEN/RUNTIME_SERVER_URL 可用；上报失败由 http_alert_sink 落本地兜底）。
    result = run_startup_check(log=_log, alert_sink=http_alert_sink())
    if result.get("status") in ("ok", "rebuilt"):
        return 0
    _log(f"WARN: {result.get('detail')} (non-blocking; agent startup re-checks in-process)")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
