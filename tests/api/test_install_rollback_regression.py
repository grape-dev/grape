"""install-worker.sh 回滚最小回归用例（Issue #271 / PR #385，Issue #3418 改写）。

背景：restore_upgrade_backup() 的 nohup 分支（注册超时路径调用）起旧版前未停残留新进程
→ 两进程并存，残留新进程可能反向注册击穿回滚。Issue #3418 起该路径统一走
stop_own_worker()（systemd unit / PIDFile / venv 前缀 + 模块右边界三重限域）。

断言：
- nohup 回滚先停本实例进程再起旧版（顺序正确），停进程模式限域到本实例；
- systemd 回滚只 restart 本实例 unit（不再写死 runtime-agent），并回写本实例登记。
"""
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import install_worker_harness as harness  # noqa: E402

AGENT_MODULE = "src.domains.runtime.engine.agent"


def _prepare_install_dir(tmp):
    install_dir = os.path.join(tmp, "grape")
    os.makedirs(os.path.join(install_dir, ".upgrade-backup", "src"), exist_ok=True)
    os.makedirs(os.path.join(install_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(install_dir, "data"), exist_ok=True)
    for path in (os.path.join(install_dir, "VERSION"),
                 os.path.join(install_dir, ".upgrade-backup", "VERSION")):
        with open(path, "w") as f:
            f.write("v-old\n")
    start_script = os.path.join(install_dir, "start-worker.sh")
    with open(start_script, "w") as f:
        f.write("#!/usr/bin/env bash\nexit 0\n")
    os.chmod(start_script, 0o755)
    return install_dir, start_script


def _registry(tmp):
    reg = os.path.join(tmp, "registry")
    os.makedirs(reg, exist_ok=True)
    return reg, os.path.join(tmp, "registry.lock")


def test_restore_upgrade_backup_nohup_stops_own_worker_first(tmp_path):
    """nohup 回滚：先停本实例残留进程（限域模式），再起旧版。"""
    install_dir, start_script = _prepare_install_dir(str(tmp_path))
    reg, lock = _registry(str(tmp_path))
    body = (
        'pgrep() { echo "PGREP $*" >> "$CALLS"; return 1; }\n'
        'nohup() { echo "NOHUP $*" >> "$CALLS"; }\n'
        'setsid() { echo "SETSID $*" >> "$CALLS"; }\n'
        'disown() { :; }\n'
        "restore_upgrade_backup nohup || true\n"
        'echo done\n'
    )
    h = harness.build_harness(body, install_dir=install_dir, module=AGENT_MODULE,
                              start_script=start_script, registry_dir=reg,
                              registry_lock=lock,
                              extra_functions=["restore_upgrade_backup"])
    r = harness.run(h)
    assert r.returncode == 0, f"harness 失败: {r.stderr}"

    calls = open(os.path.join(install_dir, "_calls.log")).read().splitlines()
    stop_lines = [ln for ln in calls if ln.startswith("PGREP ")]
    start_lines = [ln for ln in calls if ln.startswith(("SETSID ", "NOHUP "))]
    assert stop_lines, f"回滚必须先停本实例残留进程: {calls}"
    assert start_lines, f"回滚必须重新拉起旧版: {calls}"
    stop_idx = next(i for i, ln in enumerate(calls) if ln.startswith("PGREP "))
    start_idx = next(i for i, ln in enumerate(calls) if ln.startswith(("SETSID ", "NOHUP ")))
    assert stop_idx < start_idx, f"必须先停残留进程再起旧版: {calls}"

    pat = stop_lines[0]
    venv_pat = f"{install_dir}/.venv/bin/python".replace(".", r"\.")
    assert venv_pat in pat, f"停进程模式必须带本实例 venv 前缀: {pat}"
    assert r"-m src\.domains\.runtime\.engine\.agent( |$)" in pat, \
        f"停进程模式必须带模块右边界: {pat}"
    assert "python -m" not in pat.replace(venv_pat, ""), \
        f"不得退回裸 python -m 匹配: {pat}"


def test_restore_upgrade_backup_systemd_mode_restarts_own_unit(tmp_path):
    """systemd 回滚只 restart 本实例 unit（不回归到写死 runtime-agent）。"""
    install_dir, start_script = _prepare_install_dir(str(tmp_path))
    reg, lock = _registry(str(tmp_path))
    body = (
        'systemctl() { echo "SYSTEMCTL $*" >> "$CALLS"; '
        'case "$*" in *MainPID*) echo 4242 ;; esac; return 0; }\n'
        'pgrep() { return 1; }\n'
        "restore_upgrade_backup systemd || true\n"
        'echo done\n'
    )
    h = harness.build_harness(body, install_dir=install_dir, module=AGENT_MODULE,
                              start_script=start_script, registry_dir=reg,
                              registry_lock=lock, stub_sleep=False,
                              extra_functions=["restore_upgrade_backup"])
    r = harness.run(h, timeout=60)
    assert r.returncode == 0, f"harness 失败: {r.stderr}"

    calls = open(os.path.join(install_dir, "_calls.log")).read()
    assert "restart runtime-agent-" in calls, f"systemd 回滚应 restart 本实例 unit: {calls}"
    assert "restart runtime-agent\n" not in calls, \
        "systemd 回滚不得退回写死的 runtime-agent unit"
    entries = [f for f in os.listdir(reg) if f.endswith(".registry")]
    assert len(entries) == 1, f"回滚成功后应回写本实例登记: {entries}"
    entry = open(os.path.join(reg, entries[0])).read()
    assert "pid=4242" in entry and f"install_dir={install_dir}" in entry, entry
def _extract_function(name):
    """提取 scripts/install-worker.sh 中指定函数（含函数头）的文本。"""
    script = open(os.path.join(ROOT, "scripts", "install-worker.sh"), encoding="utf-8").read()
    start_marker = f"{name}() {{"
    start = script.find(start_marker)
    assert start != -1, f"install-worker.sh 应包含 {name}()"
    depth = 0
    for i in range(start + len(start_marker) - 1, len(script)):
        ch = script[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "#!/usr/bin/env bash\n" + script[start:i + 1]
    raise AssertionError(f"未能定位 {name} 闭合大括号")


def _run(harness):
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=120)


def test_wait_new_agent_alive_ignores_stale_pid():
    """判活只认启动前快照里没有的新 PID：残留旧实例不能顶替本次新起的 worker。"""
    fns = _extract_function("wait_new_agent_alive")
    harness = f"""
set -u
{fns}
sleep() {{ :; }}
_AGENT_START_POLL_MAX=3
_AGENT_START_POLL_INTERVAL=1
PGREP_OUT="${{PGREP_OUT}}"
pgrep() {{ printf '%s\\n' "$PGREP_OUT"; }}
alive="$(wait_new_agent_alive "${{SNAP}}" "PREFIX" "MODULE")" && rc=0 || rc=$?
echo "alive=$alive rc=$rc"
"""
    # 旧 PID 仍在 + 新 PID 出现 → 必须认新 PID
    env = dict(os.environ, PGREP_OUT="100\n200", SNAP=" 100 ")
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert "alive=200 rc=0" in r.stdout, f"应命中新 PID: {r.stdout}"
    # 只有旧 PID（本次没起来）→ 必须判失败
    env = dict(os.environ, PGREP_OUT="100", SNAP=" 100 ")
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60, env=env)
    assert "rc=1" in r.stdout and "alive=" in r.stdout, f"旧 PID 不得判活: {r.stdout}"


def test_registration_watermark_ignores_history():
    """注册判据只看水位之后的新行：历史 registered 不能顶替本次启动的注册。"""
    fns = _extract_function("log_line_watermark") + "\n" + _extract_function("wait_runtime_registered")
    with tempfile.TemporaryDirectory(prefix="grape-watermark-") as tmp:
        log = os.path.join(tmp, "worker.log")
        with open(log, "w") as f:
            f.write("boot\nRuntimeAgent registered: ['old']\n")
        harness = f"""
set -u
{fns}
sleep() {{ :; }}
log() {{ :; }}
REGISTER_LOG="RuntimeAgent registered"
wm="$(log_line_watermark {log!r})"
wait_runtime_registered {log!r} "$((wm + 1))" "$REGISTER_LOG" 2 && rc=0 || rc=$?
echo "wm=$wm hist_rc=$rc"
"""
        r = _run(harness)
        assert r.returncode == 0, r.stderr
        assert "wm=2" in r.stdout, f"水位应为历史行数: {r.stdout}"
        assert "hist_rc=0" not in r.stdout, f"历史注册行不得判成功: {r.stdout}"
        with open(log, "a") as f:
            f.write("RuntimeAgent registered: ['new']\n")
        harness2 = f"""
set -u
{fns}
sleep() {{ :; }}
log() {{ :; }}
REGISTER_LOG="RuntimeAgent registered"
wait_runtime_registered {log!r} 3 "$REGISTER_LOG" 2 && rc=0 || rc=$?
echo "after_rc=$rc"
"""
        r2 = _run(harness2)
        assert "after_rc=0" in r2.stdout, f"水位之后新增的注册行必须认到: {r2.stdout}"


def test_registration_watermark_counts_unterminated_last_line():
    """历史末行无换行时也计入水位（wc -l 会少算 → 历史注册行被重新扫描成假成功）。"""
    fns = _extract_function("log_line_watermark") + "\n" + _extract_function("wait_runtime_registered")
    with tempfile.TemporaryDirectory(prefix="grape-watermark-trunc-") as tmp:
        log = os.path.join(tmp, "worker.log")
        with open(log, "w") as f:
            f.write("boot\nRuntimeAgent registered: ['old']")  # 故意不写结尾换行
        harness = f"""
set -u
{fns}
sleep() {{ :; }}
log() {{ :; }}
REGISTER_LOG="RuntimeAgent registered"
wm="$(log_line_watermark {log!r})"
wait_runtime_registered {log!r} "$((wm + 1))" "$REGISTER_LOG" 2 && rc=0 || rc=$?
echo "wm=$wm rc=$rc"
"""
        r = _run(harness)
        assert "wm=2" in r.stdout, f"末行无换行也必须计入水位: {r.stdout}"
        assert "rc=0" not in r.stdout.replace("wm=2", ""), f"历史末行不得判成功: {r.stdout}"


def test_registration_watermark_sampled_before_agent_start():
    """结构守卫：日志水位与 PID 快照都必须在启动新进程之前采样（C1 缺陷复发守卫）。"""
    script = open(os.path.join(ROOT, "scripts", "install-worker.sh"), encoding="utf-8").read().splitlines()
    wm = [i for i, l in enumerate(script) if l.strip().startswith("_log_start=\"$(log_line_watermark")]
    assert wm, "主路径应有 _log_start 采样"
    snap = [i for i, l in enumerate(script) if l.strip().startswith("_pids_before=")]
    assert snap, "主路径应有 _pids_before 快照"
    starts = [i for i, l in enumerate(script) if 'setsid nohup "$START_SCRIPT"' in l]
    assert starts, "应能找到主路径的 setsid nohup 启动行"
    main_start = max(starts)  # 最后一条是主路径（更早的是回滚函数里的）
    assert wm[0] < main_start, "日志水位必须在启动新进程之前采样"
    assert snap[0] < main_start, "PID 快照必须在启动新进程之前采样"
