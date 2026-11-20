"""worker 侧 grape CLI 版本核对（Issue #2695；对应 grape-cli #1634 本地拦截）。

背景：`grape issues pending-review --issue_id <非数字>` 在旧 CLI 上会拼出
`/api/issues//pending-review` 并拿到服务端兜底 405（无 detail）。grape-cli #1634 已在
CLI 契约缓存侧加本地拦截：非 `[0-9]+` 的 `--issue_id` 直接 CliError、不发请求，并提示
「用 issues detail 返回的 issue_id 字段（内部 id，不是 GitCode issue number）」。

该修复随 grape-cli 发版生效（外部依赖，不阻塞服务端 400 校验）；worker 侧职责只有
核对版本 + 输出可读升级提示。**warn-only**：探活失败或版本未知不误报、不阻断启动，
且版本门/比较函数统一来自 `src.cli_version`（单一事实源，勿在此二次实现）。

降噪与生效（QA 复审 201047685）：含 #1634 的 v0.3.4 已于 2026-11-14 发布，功能门取 `(0, 3, 4)`；
标准 worker（随包 0.3.3）启动会输出一次升级提示，这是**预期**行为（warn-only，不阻断），
升级到 >= 0.3.4 后即静默。门禁值仍可用环境变量 `GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION` 覆盖。
"""
import shutil
import subprocess

from src.kernel.cli_version import (GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION,  # noqa: F401  供测试/调用方 monkeypatch
                             GRAPE_CLI_MIN_VERSION, format_version, intercept_floor,
                             parse_version, version_below)

__all__ = ["GRAPE_CLI_MIN_VERSION", "GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION",
           "parse_version", "version_below", "format_version", "intercept_floor",
           "issue_id_intercept_hint", "probe_cli_version", "check"]


def issue_id_intercept_hint(version, floor=None) -> str | None:
    """版本低于生效门禁 → 可读升级提示（含 issue_id 来源口径）；达标/未知/门禁未发布 → None。

    floor=None 时取 `intercept_floor()`（环境变量 > 模块常量）。门禁未发布（None）→ None：
    「未知 ≠ 陈旧」，此时给不出「明确缺 #1634」的结论，不提示。
    """
    if floor is None:
        floor = intercept_floor()
    if not floor:
        return None
    cur = parse_version(version)
    if not cur:
        return None
    if not version_below(cur, floor):
        return None
    _cur, _floor = format_version(cur), format_version(floor)
    return (
        f"grape CLI {_cur} 低于 {_floor}（不含 grape-cli #1634 的 issue_id 本地拦截）："
        f"非数字 --issue_id 不会被本地拦截，请求会打到 /api/issues//pending-review（历史症状为误导性 405）。"
        f"处理：把 worker 侧 grape CLI 升级到 >= {_floor}（重跑 install-worker.sh）；"
        f"调用前先用 `grape issues detail --json-out` 取返回体里的 issue_id 字段"
        f"（内部 id，不是 GitCode issue number）。")


def probe_cli_version(binary: str = "", timeout: float = 5.0) -> str:
    """实测 `grape --version`（取最后一行）。未装/调用失败返回 ""。"""
    exe = binary or shutil.which("grape") or ""
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ""
    lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def check(log=None, version: str | None = None, probe=None) -> dict:
    """启动钩子：核对版本并在陈旧时输出提示（不抛异常、不阻断启动）。

    - version=None 时用 probe()（默认实测 `grape --version`）取版本；
    - 返回 {"status": "ok"|"stale"|"unknown", "version": ..., "floor": ...}，供调用方记录/判读。
    """
    say = log or (lambda msg: None)
    if version is None:
        try:
            version = (probe or probe_cli_version)()
        except Exception as e:
            say(f"grape CLI 版本核对跳过（探活失败：{e}）")
            return {"status": "unknown", "version": "", "floor": "", "detail": str(e)}
    floor = intercept_floor()
    hint = issue_id_intercept_hint(version, floor=floor)
    if hint:
        say(hint)
        return {"status": "stale", "version": format_version(parse_version(version)),
                "floor": format_version(floor), "detail": hint}
    return {"status": "ok" if parse_version(version) else "unknown",
            "version": str(version or ""), "floor": format_version(floor) if floor else ""}
