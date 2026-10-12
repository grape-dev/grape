"""install-worker.sh 回滚最小回归用例（Issue #271 / PR #385 follow-up）。

QA 审查发现：restore_upgrade_backup() nohup 分支（被注册超时路径 :468 调用）起旧版前
未 pkill 残留新进程 → 两进程并存，残留新进程可能反向注册击穿回滚。修复：起旧版前先
用限域模式（按本安装目录 venv 绝对路径匹配）pkill 残留新进程。

本用例为最小回归：从 scripts/install-worker.sh 提取 restore_upgrade_backup 函数体，
在隔离 sandbox 中以 mock pgrep/pkill/nohup/setsid 运行 nohup 回滚路径，断言：
- pkill 先于 nohup 启动旧版执行（顺序正确）；
- pkill 使用限域模式（含 $VENV/bin/python 前缀）而非裸 "python -m src.runtime.agent"。
"""
import os
import subprocess
import tempfile

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


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
        # 而非裸 "python -m src.runtime.agent"
        pat = pkill_lines[0]
        assert f"{install_dir}/.venv/bin/python".replace(".", r"\.") in pat, f"pkill 应为限域模式: {pat}"
        assert "-m src\\.runtime\\.agent" in pat
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