"""install-worker.sh 测试 harness（Issue #3418）。

把 `scripts/install-worker.sh` 里的「实例身份 / 限域停止 / 实例注册表」代码段与
`restore_upgrade_backup()` 原样抽出，在隔离 sandbox（临时安装目录 + 临时注册表）
中运行，用于验证同机多 worker 的限域行为。不启动真实 worker、不访问主控。

抽取而不是复制：被测逻辑只有脚本里的一份，测试不会与被测实现漂移。
"""
import os
import subprocess

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT_PATH = os.path.join(ROOT, "scripts", "install-worker.sh")

# 实例身份块：从「0. 实例身份」到「目标机架构」之间（含 0.1 限域停止），
# 依赖 INSTALL_DIR / VENV / START_SCRIPT / ROOT_MODE / SERVICE_NAME_ARG 与 log/fail。
INSTANCE_BLOCK_START = "# ===== 0. 实例身份"
INSTANCE_BLOCK_END = "# 目标机架构"


def script_text():
    with open(SCRIPT_PATH, encoding="utf-8") as f:
        return f.read()


def extract_function(name, text=None):
    """按顶层大括号平衡抽取 `name() { ... }` 函数定义原文。"""
    text = text if text is not None else script_text()
    marker = f"{name}() {{"
    start = text.find(marker)
    assert start != -1, f"install-worker.sh 应包含 {name}()"
    depth = 0
    for i in range(start + len(marker) - 1, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"未能定位 {name} 的闭合大括号")


def instance_block(text=None):
    """抽取实例身份 + 限域停止整段（保持与脚本同一份实现）。"""
    text = text if text is not None else script_text()
    start = text.find(INSTANCE_BLOCK_START)
    end = text.find(INSTANCE_BLOCK_END, start)
    assert start != -1 and end != -1, "install-worker.sh 应包含实例身份代码段"
    return text[start:end]


def build_harness(body, *, install_dir, venv=None, start_script=None, module=None,
                  service_name_arg="", root_mode=0, registry_dir=None,
                  registry_lock=None, extra_functions=(), calls_log=None,
                  stub_sleep=True, cred_type="worker", cred_key=""):
    """拼装可直接交给 `bash -c` 的 harness 文本。"""
    venv = venv or os.path.join(install_dir, ".venv")
    start_script = start_script or os.path.join(install_dir, "start-worker.sh")
    calls_log = calls_log or os.path.join(install_dir, "_calls.log")
    prelude = [
        "#!/usr/bin/env bash",
        # 与脚本实跑保持同一套严格模式（scripts/install-worker.sh 的
        # `set -euo pipefail`）：抽取段内的 errexit/pipefail 回归在测试中同样可见。
        "set -euo pipefail",
        f"INSTALL_DIR={install_dir!r}",
        f"VENV={venv!r}",
        f"START_SCRIPT={start_script!r}",
        f"_upgrade_backup_dir={os.path.join(install_dir, '.upgrade-backup')!r}",
        f"ROOT_MODE={root_mode}",
        f"SERVICE_NAME_ARG={service_name_arg!r}",
        f"CRED_TYPE={cred_type!r}",
        f"CRED_KEY={cred_key!r}",
    ]
    if module:
        import shlex as _shlex
        prelude.append(f"WORKER_MODULE={module!r}")
        # arch 线在实例身份块之前就完成模块转义（block 外），harness 需一并注入
        esc = module.replace(".", "\\.")
        prelude.append(f"WORKER_MODULE_ESC={_shlex.quote(esc)}")
        prelude.append(f"WORKER_MODULE_PAT={_shlex.quote(esc + '( |$)')}")
    if registry_dir:
        prelude.append(f"GRAPE_WORKER_REGISTRY_DIR={registry_dir!r}")
    if registry_lock:
        prelude.append(f"GRAPE_WORKER_REGISTRY_LOCK={registry_lock!r}")
    prelude += [
        f"CALLS={calls_log!r}",
        'log() { echo "$*" >> "$CALLS"; }',
        'fail() { echo "ERROR: $*" >&2; exit 1; }',
    ]
    if stub_sleep:
        # 大多数用例只关心匹配/限域逻辑，真实 sleep 只会拖慢（分支不变）
        prelude.append('sleep() { :; }')
    extras = [extract_function(fn) for fn in extra_functions]
    return "\n".join(prelude) + "\n" + instance_block() + "\n" + "\n".join(extras) + "\n" + body


def run(harness, env=None, timeout=90):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                          timeout=timeout, env=merged)