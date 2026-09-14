"""沙箱环境预检 + 敏感 env 扫描（Issue #139）。

在 prepare_sandbox 之前检测环境是否满足沙箱运行条件：
- 硬失败（bwrap 不可用 / 非 Linux / /tmp 不可写）→ 拒绝启动沙箱
- 软失败（cgroup 不可用 / CA 证书缺失 / sudo 缺失）→ 降级运行

同时扫描 env 中含敏感关键词但未列入遮蔽清单的变量，warn 提醒。

参考：deepagents-sandbox/detect.py
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    reason: str


def check_platform() -> CheckResult:
    if platform.system() == "Linux":
        return CheckResult("platform", True, "Running on Linux")
    return CheckResult(
        "platform", False,
        f"Not running on Linux (got {platform.system()}). "
        "Sandbox requires Linux with bubblewrap support.",
    )


def check_bwrap() -> CheckResult:
    path = shutil.which("bwrap")
    if path:
        return CheckResult("bwrap", True, f"Found bwrap at {path}")
    return CheckResult(
        "bwrap", False,
        "bubblewrap (bwrap) not found on PATH. "
        "Install: sudo apt install bubblewrap / sudo dnf install bubblewrap",
    )


def check_sudo() -> CheckResult:
    if shutil.which("sudo"):
        return CheckResult("sudo", True, "sudo available")
    return CheckResult(
        "sudo", False,
        "sudo not found — cgroup resource limits will be disabled (degraded)",
    )


def check_cgroup() -> CheckResult:
    """检测 cgroup 可用性（v2 优先，v1 降级）。"""
    # v2: unified hierarchy
    if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return CheckResult("cgroup", True, "cgroup v2 unified available")
    # v1: legacy subsystems
    for subsys in ("memory", "cpu,cpuacct"):
        path = f"/sys/fs/cgroup/{subsys}"
        if not os.path.isdir(path):
            return CheckResult(
                "cgroup", False,
                f"cgroup v1 {subsys} not mounted at {path}. "
                f"Resource limits will be disabled (degraded).",
            )
    return CheckResult("cgroup", True, "cgroup v1 memory+cpu available")


def check_tmp_writable() -> CheckResult:
    test_file = "/tmp/.grape-sandbox-probe"
    try:
        with open(test_file, "w") as f:
            f.write("x")
        os.unlink(test_file)
        return CheckResult("tmp", True, "/tmp writable")
    except Exception as e:
        return CheckResult("tmp", False, f"/tmp not writable: {e}")


def check_ssl_certs() -> CheckResult:
    for p in ("/etc/ssl/certs", "/etc/pki/tls/certs"):
        if os.path.isdir(p):
            return CheckResult("ssl", True, f"CA certs at {p}")
    return CheckResult(
        "ssl", False,
        "No CA certificate directory found — HTTPS verification may fail (degraded)",
    )


# 硬失败项 — 不可降级
HARD_FAILURES = frozenset({"platform", "bwrap", "tmp"})

# 敏感 env 变量名模式
_SENSITIVE_PATTERN = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE
)

# 已知安全的 env 变量（不含敏感凭据，匹配模式但不应告警）
_SAFE_ENV_VARS = frozenset({
    "GRAPE_API_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "NO_PROXY", "GRPC_PROXY", "http_proxy", "https_proxy",
    "all_proxy", "no_proxy", "grpc_proxy",
    "DEVOPS_EVENT_TYPE", "AGENT_NAME", "AGENT_LABEL",
    "GIT_CONFIG_PARAMETERS", "GIT_SSH_COMMAND", "GRAPESOCK",
    "NODE_TLS_REJECT_UNAUTHORIZED", "PATH", "HOME", "TERM",
    "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH",
    "HERMES_HOME", "DEVOPS_SKILLS_DIR",
    "OPENCODE_CONFIG", "OPENCODE_MODE", "OPENCODE_CHANNEL",
    "OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_AUTOUPDATE",
    "OPENCODE_ALWAYS_NOTIFY_UPDATE", "OPENCODE_CONFIG_FILE",
    "PLUGIN_ENV", "SCENARIO", "KERNEL_DATA_DIR", "KERNEL_CONFIG_DIR",
})


def scan_sensitive_env(unset_list: list[str] | None = None) -> list[str]:
    """扫描 env 中含敏感关键词但未列入遮蔽清单的变量。

    返回 warning 列表（变量名），不阻断启动。
    目的：发现未遮蔽的凭据，提醒后续补遮蔽。
    """
    unset_set = set(unset_list or [])
    warnings = []
    for key in os.environ:
        if key in _SAFE_ENV_VARS:
            continue
        if key in unset_set:
            continue
        if _SENSITIVE_PATTERN.search(key):
            warnings.append(key)
    return warnings


def detect_sandbox_env(
    unset_list: list[str] | None = None,
) -> tuple[bool, list[CheckResult], list[str]]:
    """环境预检 + 敏感 env 扫描。

    返回:
        (can_proceed, check_results, env_warnings)
        - can_proceed: False 时不能启动沙箱（硬失败）
        - check_results: 各项检测结果
        - env_warnings: 含敏感关键词但未遮蔽的 env 变量名列表
    """
    results = [
        check_platform(),
        check_bwrap(),
        check_sudo(),
        check_cgroup(),
        check_tmp_writable(),
        check_ssl_certs(),
    ]
    hard_fails = [r for r in results if not r.passed and r.name in HARD_FAILURES]
    env_warnings = scan_sensitive_env(unset_list)
    return len(hard_fails) == 0, results, env_warnings
