"""install-worker.sh 回滚最小回归用例（Issue #271 / PR #385 follow-up）。

QA 审查发现：restore_upgrade_backup() nohup 分支（被注册超时路径 :468 调用）起旧版前
未 pkill 残留新进程 → 两进程并存，残留新进程可能反向注册击穿回滚。修复：起旧版前先
用限域模式（按本安装目录 venv 绝对路径匹配）pkill 残留新进程。

本用例为最小回归：从 scripts/install-worker.sh 提取 restore_upgrade_backup 函数体，
在隔离 sandbox 中以 mock pgrep/pkill/nohup/setsid 运行 nohup 回滚路径，断言：
- pkill 先于 nohup 启动旧版执行（顺序正确）；
- pkill 使用限域模式（含 $VENV/bin/python 前缀）而非裸 "python -m src.domains.runtime.engine.agent"。
"""
import os
import subprocess
import tempfile

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _extract_restore_upgrade_backup():
    """提取 scripts/install-worker.sh 中的 restore_upgrade_backup() 函数文本。"""
    script = open(os.path.join(ROOT, "scripts", "install-worker.sh"), encoding="utf-8").read()
    start_marker = "restore_upgrade_backup() {"
    start = script.find(start_marker)
    assert start != -1, "install-worker.sh 应包含 restore_upgrade_backup()"
    # 从起始标记数到顶层闭合大括号
    depth = 0
    for i in range(start + len(start_marker) - 1, len(script)):
        ch = script[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "#!/usr/bin/env bash\n" + script[start:i + 1]
    raise AssertionError("未能定位 restore_upgrade_backup 闭合大括号")


def test_restore_upgrade_backup_nohup_kills_residual_new_first():
    """nohup 回滚路径：pkill 残留新进程必须先于 nohup 起旧版，且 pkill 为限域模式。"""
    func = _extract_restore_upgrade_backup()
    with tempfile.TemporaryDirectory(prefix="grape-rollback-") as tmp:
        install_dir = os.path.join(tmp, "grape")
        # 最小安装目录：备份 + 旧代码 + 数据目录（nohup 重定向到 data/worker.log 需要它存在）
        os.makedirs(os.path.join(install_dir, ".upgrade-backup", "src"), exist_ok=True)
        os.makedirs(os.path.join(install_dir, "src"), exist_ok=True)
        os.makedirs(os.path.join(install_dir, "data"), exist_ok=True)
        with open(os.path.join(install_dir, "VERSION"), "w") as f:
            f.write("v-old\n")
        with open(os.path.join(install_dir, ".upgrade-backup", "VERSION"), "w") as f:
            f.write("v-old\n")
        start_script = os.path.join(install_dir, "start-worker.sh")
        with open(start_script, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(start_script, 0o755)
        call_log = os.path.join(tmp, "calls.log")

        harness = f"""
set -u
{func}
LOG_FILE={call_log!r}
log() {{ echo "$*" >> "$LOG_FILE"; }}
# mock 命令（记录调用顺序）：pgrep 返回 0 使回滚判定成功
pkill()  {{ echo "PKILL $*" >> "$LOG_FILE"; }}
pgrep()  {{ echo "PGREP $*" >> "$LOG_FILE"; return 0; }}
setsid() {{ echo "SETSID $*" >> "$LOG_FILE"; "$@"; }}
nohup()  {{ echo "NOHUP $*" >> "$LOG_FILE"; "$@"; }}
disown() {{ :; }}
sleep()  {{ :; }}
INSTALL_DIR={install_dir!r}
VENV={install_dir!r}/.venv
_upgrade_backup_dir={install_dir!r}/.upgrade-backup
START_SCRIPT={start_script!r}
restore_upgrade_backup nohup
"""
        r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"harness 失败: {r.stderr}"

        calls = open(call_log).read().strip().splitlines()
        pkill_lines = [ln for ln in calls if ln.startswith("PKILL ")]
        assert pkill_lines, f"nohup 回滚必须先 pkill 残留新进程, calls={calls}"
        # ① 顺序：pkill 在 nohup 前（限域匹配 → 先杀新，再起旧）
        pkill_idx = next(i for i, ln in enumerate(calls) if ln.startswith("PKILL "))
        nohup_idx = next(i for i, ln in enumerate(calls) if ln.startswith("NOHUP "))
        assert pkill_idx < nohup_idx, f"pkill 必须先于 nohup 起旧版: {calls}"
        # ② 限域模式：pkill 模式含本安装目录 venv python 绝对路径（点已被 sed 转义为 \.），
        # 而非裸 "python -m src.domains.runtime.engine.agent"
        pat = pkill_lines[0]
        assert f"{install_dir}/.venv/bin/python".replace(".", r"\.") in pat, f"pkill 应为限域模式: {pat}"
        # [rebase 适配 2026-11-12] 恢复函数用 ${WORKER_MODULE_ESC:-宽松兜底}（沙箱线
        # 改造，模块名不再硬编码旧布局路径）——断言收敛为「-m + 转义模块形态」
        assert "-m src\\." in pat and "[a-z_]+" in pat
        # 右边界：不带 ( |$) 时 ...engine.agent_pool / cloud_worker_xxx 会被同模式命中，
        # 判活误成功、pkill 误杀同前缀模块。
        assert "( |$)" in pat, f"pkill/判活模式必须带右边界: {pat}"
        assert not pat.startswith("python "), f"pkill 不得退回裸命令: {pat}"


def test_restore_upgrade_backup_systemd_mode_keeps_restart():
    """systemd 回滚路径仍走 systemctl restart（不回归）。"""
    func = _extract_restore_upgrade_backup()
    with tempfile.TemporaryDirectory(prefix="grape-rollback-") as tmp:
        install_dir = os.path.join(tmp, "grape")
        os.makedirs(os.path.join(install_dir, ".upgrade-backup", "src"), exist_ok=True)
        os.makedirs(os.path.join(install_dir, "src"), exist_ok=True)
        call_log = os.path.join(tmp, "calls.log")
        harness = f"""
set -u
{func}
LOG_FILE={call_log!r}
log() {{ echo "$*" >> "$LOG_FILE"; }}
systemctl() {{ echo "SYSTEMCTL $*" >> "$LOG_FILE"; return 0; }}
pgrep()  {{ echo "PGREP $*" >> "$LOG_FILE"; return 0; }}
sleep()  {{ :; }}
INSTALL_DIR={install_dir!r}
VENV={install_dir!r}/.venv
_upgrade_backup_dir={install_dir!r}/.upgrade-backup
restore_upgrade_backup systemd
"""
        r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"harness 失败: {r.stderr}"
        calls = open(call_log).read()
        assert "restart runtime-agent" in calls, f"systemd 路径应 restart: {calls}"

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
