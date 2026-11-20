"""#2789 回归：安装/启动链路必须清除外部 PYTHONPATH，且进程匹配用 arch 布局模块名。

P0-1：容器注入 PYTHONPATH=/app 后 python -m 抢到错误 src，`grape --version` /
worker 启动报 ModuleNotFoundError。安装脚本、启动脚本、生成的 START_SCRIPT 都要
unset PYTHONPATH，Windows 版移除进程级 Env:PYTHONPATH；安装段还要显式带污染环境
做一次 CLI smoke（顶部 unset 会抹掉前置条件，故 smoke 命令行自带 PYTHONPATH=/app）。

附带回归：install-worker.sh 回滚/存活判定仍按旧扁平模块名 `src.runtime.agent`
匹配，而本分支实际入口是 `src.domains.runtime.engine.agent` → pgrep 永不命中，
安装每次都误判启动失败并回滚。pgrep 认新入口；pkill 必须新旧各杀一次
（从旧扁平布局升级时旧 worker 不能被留下 → 双 worker）。
"""
import os
import re
import subprocess

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
SH = os.path.join(ROOT, "scripts", "install-worker.sh")
START_SH = os.path.join(ROOT, "scripts", "start-worker.sh")
START_PS1 = os.path.join(ROOT, "scripts", "start-worker.ps1")
INSTALL_PS1 = os.path.join(ROOT, "scripts", "install-worker.ps1")

NEW_PAT = "src\\.domains\\.runtime\\.engine\\.agent"
LEGACY_PAT = "src\\.runtime\\.agent"


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def _cmd_lines(content, cmd):
    """只取真实命令行的行（注释里提到命令名不算）。"""
    return [ln for ln in content.splitlines() if re.search(rf"(^|\s){cmd}\s+-f\b", ln)]


def test_install_sh_clears_pythonpath():
    assert "unset PYTHONPATH" in _read(SH)


def test_start_sh_clears_pythonpath():
    assert "unset PYTHONPATH" in _read(START_SH)


def test_generated_start_script_clears_pythonpath():
    content = _read(SH)
    marker = 'cd "$INSTALL_DIR"\nunset PYTHONPATH'
    assert marker in content, "安装生成的 START_SCRIPT 托管区必须 unset PYTHONPATH"


def test_windows_scripts_clear_pythonpath():
    assert "Remove-Item Env:PYTHONPATH" in _read(START_PS1)
    assert "Remove-Item Env:PYTHONPATH" in _read(INSTALL_PS1)


def test_pgrep_lines_use_arch_module_layout():
    for line in _cmd_lines(_read(SH), "pgrep"):
        assert NEW_PAT in line, f"pgrep 应匹配 arch 入口: {line}"
        assert LEGACY_PAT not in line, f"pgrep 不应再认旧扁平入口: {line}"


def test_pkill_lines_stop_both_flat_and_arch_layouts():
    pkill_lines = [ln for ln in _cmd_lines(_read(SH), "pkill") if ".*-m src" in ln]
    assert any(NEW_PAT in ln for ln in pkill_lines), "pkill 必须停新入口"
    assert any(LEGACY_PAT in ln for ln in pkill_lines), (
        "从旧扁平布局升级时必须同时停 src.runtime.agent，否则旧 worker 留下 → 双 worker")


def test_install_sh_cli_smoke_runs_with_polluted_pythonpath(tmp_path):
    """显式带 PYTHONPATH=/app 跑安装后 CLI smoke（不是靠脚本顶部 unset）。"""
    content = _read(SH)
    begin, end = "# #2789 CLI smoke begin", "# #2789 CLI smoke end"
    assert begin in content and end in content
    block = content.split(begin, 1)[1].split(end, 1)[0]

    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    grape = venv / "bin" / "grape"
    grape.write_text('#!/usr/bin/env bash\necho "PYTHONPATH=$PYTHONPATH"\n')
    os.chmod(grape, 0o755)

    harness = (
        "set -euo pipefail\n"
        f"VENV={str(venv)!r}\n"
        f"INSTALL_DIR={str(tmp_path)!r}\n"
        'log() { echo "$*"; }\n'
        f"{block}\n"
    )
    env = {**os.environ, "PYTHONPATH": "/should-not-leak"}
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                       env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "PYTHONPATH=/app" in r.stdout, f"smoke 必须显式带污染环境，实际 {r.stdout}"


def test_empty_engines_is_warning_not_normal():
    """#2789 验收②：engines=[] 必须英文 WARNING + 下一步指引。"""
    content = _read(SH)
    assert "which is normal" not in content
    assert "WARNING: RuntimeAgent registered with no execution engines" in content
    assert "install at least one engine" in content


# ===== #2789 验收② 探针：日志来源 + 本次启动段 =====
# root+systemd 分支的 unit 没有 StandardOutput，worker 日志只在 journal；
# 末段只 grep data/worker.log 永不命中 → engines=[] 告警失效（QA 第二轮）。
# 另：grep 必须只看本次启动之后的新行，旧残留不能误报。

PROBE_BEGIN = "# #2789 engines probe begin"
PROBE_END = "# #2789 engines probe end"
BOOT_BEGIN = "# #2789 boot marker begin"
BOOT_END = "# #2789 boot marker end"
CHECK_BEGIN = "# #2789 engines check begin"
CHECK_END = "# #2789 engines check end"
WARN = "WARNING: RuntimeAgent registered with no execution engines"


def _block(content, begin, end):
    assert begin in content and end in content, f"缺少区块标记 {begin}/{end}"
    return content.split(begin, 1)[1].split(end, 1)[0]


def _run_engines_check(tmp_path, *, worker_log="", fresh_log="", journal="", systemd=True):
    """把安装脚本的探针/判定区块抽出来真跑一遍，返回 CompletedProcess。

    `worker_log` 是启动前就存在的旧内容（上一个版本写入的），`fresh_log` 在 boot 标记
    之后追加——模拟「本次启动新写的行」，用来验证偏移量是否生效。
    """
    content = _read(SH)
    boot = _block(content, BOOT_BEGIN, BOOT_END)
    probe = _block(content, PROBE_BEGIN, PROBE_END)
    check = _block(content, CHECK_BEGIN, CHECK_END)

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    log_path = data / "worker.log"
    if worker_log:
        log_path.write_text(worker_log, encoding="utf-8")

    harness = (
        "set -euo pipefail\n"
        f"INSTALL_DIR={str(tmp_path)!r}\n"
        "ROOT_MODE=1\n"
        "log() { echo \"$*\"; }\n"
        "_has_systemd() { return 0; }\n"
        "journalctl() { printf '%s\\n' \"$FAKE_JOURNAL\"; }\n"
        f"export FAKE_JOURNAL={journal!r}\n"
        f"export FAKE_FRESH={fresh_log!r}\n"
        f"{boot}\n"
        "if [ -n \"$FAKE_FRESH\" ]; then printf '%s' \"$FAKE_FRESH\" >> "
        "\"$INSTALL_DIR/data/worker.log\"; fi\n"
        + (f"_USING_SYSTEMD=1\n" if systemd else "")
        + f"{probe}\n{check}\n"
    )
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)


def test_systemd_path_reads_journal_for_empty_engines(tmp_path):
    """root+systemd：日志在 journal（worker.log 为空），engines=[] 仍必须告警。"""
    r = _run_engines_check(
        tmp_path, journal="RuntimeAgent registered: engines=[] device=w1", systemd=True)
    assert r.returncode == 0, r.stderr
    assert WARN in r.stdout, f"systemd 分支读不到 journal → 告警失效；实际输出 {r.stdout!r}"


def test_nohup_path_still_reads_worker_log(tmp_path):
    """普通用户/容器：worker.log 是权威来源，行为保持不变。"""
    r = _run_engines_check(
        tmp_path, fresh_log="RuntimeAgent registered: engines=[] device=w1\n", systemd=False)
    assert r.returncode == 0, r.stderr
    assert WARN in r.stdout, f"nohup 分支必须继续读 worker.log；实际输出 {r.stdout!r}"


def test_stale_worker_log_does_not_trigger_warning(tmp_path):
    """升级场景：worker.log 里上一轮的 engines=[] 残留不得冒充本次启动结果。"""
    stale = "RuntimeAgent registered: engines=[] device=w1\n"
    r = _run_engines_check(
        tmp_path, worker_log=stale, journal="RuntimeAgent registered: engines=[opencode]",
        systemd=True)
    assert r.returncode == 0, r.stderr
    assert WARN not in r.stdout, f"旧残留误报：{r.stdout!r}"


def test_stale_worker_log_ignored_in_nohup_path(tmp_path):
    """nohup 分支同样只认本次启动后的新行。"""
    stale = "RuntimeAgent registered: engines=[] device=w1\n"
    r = _run_engines_check(
        tmp_path, worker_log=stale,
        fresh_log="RuntimeAgent registered: engines=[opencode]\n", systemd=False)
    assert r.returncode == 0, r.stderr
    assert WARN not in r.stdout, f"旧残留误报：{r.stdout!r}"


def test_engines_check_wiring_uses_probe_and_boot_marker():
    """静态守卫：判定走统一探针；systemd 分支标注日志来源；启动前落 boot 标记。"""
    content = _read(SH)
    check = _block(content, CHECK_BEGIN, CHECK_END)
    assert "_worker_log_has" in check, "engines 判定必须走日志探针，不能只 grep worker.log"
    branch = content.index('if [ "$ROOT_MODE" = "1" ] && _has_systemd; then')
    assert "_USING_SYSTEMD=1" in content[branch:branch + 500], (
        "systemd 分支必须标注 _USING_SYSTEMD=1，探针才知道要查 journal")
    assert content.index(BOOT_BEGIN) < branch, "boot 标记必须落在启动分支之前"
