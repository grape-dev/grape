"""Issue #3418：install-worker.sh 同机多 worker 限域回归（arch 线）。

背景：某生产主机上两个 worker 互相 SIGKILL，6 秒一轮死循环。本线（arch）worker 入口为
`src.domains.runtime.engine.agent`，沙箱 worker 为
`src.domains.runtime.engine.cloud_worker`；此前普通 worker 的 systemd unit 名写死
`runtime-agent`，同机第二个实例的安装/升级/回滚会直接作用到第一个实例的 unit。

覆盖三条路径（Spec Acceptance Criteria）：
1. 异构入口模块：对 A 实例执行限域停止，B 实例（含沙箱 cloud_worker）进程必须存活；
2. 同目录重装：实例键稳定、登记可覆盖，他实例占用同 unit 名时 fail-fast；
3. 升级失败回滚：回滚只动本实例进程，且回滚后本实例登记被重写。

被测代码从脚本原样抽取（tests/install_worker_harness.py），不复制实现。
"""
import os
import re
import shlex
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import install_worker_harness as harness  # noqa: E402

AGENT_MODULE = "src.domains.runtime.engine.agent"
SANDBOX_MODULE = "src.domains.runtime.engine.cloud_worker"


def _spawn_fake_worker(venv_dir, module):
    """起一个假 worker：cmdline 形如 `<venv>/bin/python -m <module> ...`（真实 worker 形态）。"""
    os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
    argv0 = f"{venv_dir}/bin/python -m {module}"
    proc = subprocess.Popen(["bash", "-c", f"exec -a {shlex.quote(argv0)} sleep 120"])
    time.sleep(0.3)
    assert proc.poll() is None, "假 worker 进程未起来"
    return proc


def _died_within(proc, timeout=8):
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _kill_quietly(proc):
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - 清理路径不关心失败原因
        pass


def _kill_by_pattern(venv_dir, module):
    subprocess.run(["pkill", "-f", f"{re.escape(venv_dir + '/bin/python')}.*-m {module}"],
                   capture_output=True)
    subprocess.run(["pkill", "-f", re.escape(venv_dir + "/start-worker.sh")],
                   capture_output=True)


def _sandbox(tmp_path):
    dir_a = os.path.join(str(tmp_path), "grape-a")
    dir_b = os.path.join(str(tmp_path), "grape-b")
    for d in (dir_a, dir_b):
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
    registry = os.path.join(str(tmp_path), "registry")
    os.makedirs(registry, exist_ok=True)
    return dir_a, dir_b, registry, os.path.join(str(tmp_path), "registry.lock")


def _calls(install_dir):
    return open(os.path.join(install_dir, "_calls.log")).read()


# --------------------------------------------------------------------------
# 路径 1：异构入口模块 —— 停 A 不伤 B
# --------------------------------------------------------------------------

@pytest.mark.parametrize("a_module,b_module", [
    (AGENT_MODULE, SANDBOX_MODULE),
    (SANDBOX_MODULE, AGENT_MODULE),
    (AGENT_MODULE, "src.domains.runtime.engine.agent_pool"),  # 同前缀模块，验证右边界
])
def test_stop_own_worker_isolates_heterogeneous_instances(tmp_path, a_module, b_module):
    dir_a, dir_b, registry, lock = _sandbox(tmp_path)
    proc_a = _spawn_fake_worker(os.path.join(dir_a, ".venv"), a_module)
    proc_b = _spawn_fake_worker(os.path.join(dir_b, ".venv"), b_module)
    try:
        h = harness.build_harness(
            "stop_own_worker\necho done\n",
            install_dir=dir_a, module=a_module,
            registry_dir=registry, registry_lock=lock)
        r = harness.run(h)
        assert r.returncode == 0, f"harness 失败: {r.stderr}"
        assert _died_within(proc_a), "本实例进程应被停掉"
        assert proc_b.poll() is None, f"同机异构实例（module={b_module}）不得被误杀"
    finally:
        _kill_quietly(proc_a)
        _kill_quietly(proc_b)


def test_sandbox_and_worker_instances_use_distinct_units(tmp_path):
    """普通 worker 与沙箱 worker 的 unit 名必须天然不同（同机共存不互杀）。"""
    dir_a, dir_b, registry, lock = _sandbox(tmp_path)
    body = 'echo "SERVICE=$SERVICE_NAME KEY=$WORKER_INSTANCE_KEY" >> "$CALLS"\necho done\n'
    h_worker = harness.build_harness(body, install_dir=dir_a, module=AGENT_MODULE,
                                     registry_dir=registry, registry_lock=lock)
    h_sandbox = harness.build_harness(body, install_dir=dir_b, module=SANDBOX_MODULE,
                                      cred_type="sandbox", cred_key="deadbeef",
                                      registry_dir=registry, registry_lock=lock)
    assert harness.run(h_worker).returncode == 0
    assert harness.run(h_sandbox).returncode == 0

    worker_line = [ln for ln in _calls(dir_a).splitlines() if ln.startswith("SERVICE=")][0]
    sandbox_line = [ln for ln in _calls(dir_b).splitlines() if ln.startswith("SERVICE=")][0]
    assert "SERVICE=runtime-agent-" in worker_line, worker_line
    assert "SERVICE=sandbox-worker-deadbeef" in sandbox_line, sandbox_line
    assert worker_line != sandbox_line


def test_no_bare_module_match_and_all_patterns_are_scoped():
    """静态断言：不再以裸模块名匹配进程，模式必须限域到本实例。"""
    text = harness.script_text()
    assert "-m src\\\\.domains\\\\.runtime\\\\.engine\\\\.agent\"" not in text, \
        "install-worker.sh 不得再出现写死模块名的匹配模式"
    scoped = ("_venv_pat", "_start_pat", "WORKER_MODULE_PAT", "_pat", "_mod", "_kpat")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("pkill ", "pkill -9 ", "pgrep ")) and \
           "_kill_pids_matching \"" not in stripped:
            continue
        assert any(m in stripped for m in scoped), \
            f"杀进程/判活模式必须限域到本实例: {stripped}"
    assert "[ \"$_kp\" = \"$$\" ]" in text, \
        "限域兜底必须跳过自身 PID，避免发起安装的 shell 被自身模式命中"
    assert 'regex_escape_path() {' in text, "应统一走 regex_escape_path 转义模式"


def test_regex_escape_path_covers_all_metacharacters(tmp_path):
    """$VENV 路径含正则元字符时，转义后的模式只能匹配它自己。"""
    tricky = f"{tmp_path}/grape (a)/{{b}}+c?[d].*^$|"
    install_dir = os.path.join(str(tmp_path), "grape-esc")
    os.makedirs(os.path.join(install_dir, "data"), exist_ok=True)
    body = (
        f'target={shlex.quote(tricky)}\n'
        'pat="$(regex_escape_path "$target")"\n'
        'printf "%s\\n" "$target" | grep -qE "^${pat}$" && echo SELF_MATCH >> "$CALLS"\n'
        'printf "%s\\n" "${target}X" | grep -qE "^${pat}$" && echo OVERMATCH >> "$CALLS"\n'
        'echo done\n'
    )
    r = harness.run(harness.build_harness(body, install_dir=install_dir))
    assert r.returncode == 0, f"harness 失败: {r.stderr}"
    log = _calls(install_dir)
    assert "SELF_MATCH" in log, f"转义后应能匹配自身: {log}"
    assert "OVERMATCH" not in log, f"转义后不得越界匹配: {log}"


# --------------------------------------------------------------------------
# 路径 2：同目录重装 / 跨实例 unit 名冲突
# --------------------------------------------------------------------------

def test_same_dir_reinstall_reuses_identity_and_overwrites_registry(tmp_path):
    dir_a, _, registry, lock = _sandbox(tmp_path)
    h = harness.build_harness(
        'echo "UNIT=$SERVICE_NAME KEY=$WORKER_INSTANCE_KEY" >> "$CALLS"\n'
        'registry_guard && echo "GUARD_OK" >> "$CALLS"\n'
        'registry_register 4242\n'
        'echo "REG=$(tr "\\n" ";" < "$REGISTRY_DIR/worker-$WORKER_INSTANCE_KEY.registry")" >> "$CALLS"\n'
        'echo done\n',
        install_dir=dir_a, module=AGENT_MODULE,
        registry_dir=registry, registry_lock=lock)
    r1 = harness.run(h)
    assert r1.returncode == 0, f"首次安装 harness 失败: {r1.stderr}"
    first = _calls(dir_a)
    unit_line = [ln for ln in first.splitlines() if ln.startswith("UNIT=")][0]
    key = unit_line.split("KEY=")[1]
    assert "GUARD_OK" in first

    r2 = harness.run(h)
    assert r2.returncode == 0, f"同目录重装 harness 失败: {r2.stderr}"
    second = _calls(dir_a)
    assert "GUARD_OK" in second, f"同目录重装不应被判冲突: {second}"
    assert f"UNIT=runtime-agent-{key} KEY={key}" in second, \
        f"同目录重装必须复用同一实例身份: {second}"
    assert "pid=4242" in second, f"本实例登记应被重写覆盖: {second}"
    entries = sorted(f for f in os.listdir(registry) if f.endswith(".registry"))
    assert entries == [f"worker-{key}.registry"], f"只应有一条本实例登记: {entries}"


def test_live_instance_conflict_fails_fast_without_touching_other_worker(tmp_path):
    dir_a, dir_b, registry, lock = _sandbox(tmp_path)
    proc_b = _spawn_fake_worker(os.path.join(dir_b, ".venv"), SANDBOX_MODULE)
    try:
        h_b = harness.build_harness(
            f"registry_register {proc_b.pid}\necho done\n",
            install_dir=dir_b, module=AGENT_MODULE,
            registry_dir=registry, registry_lock=lock)
        r_b = harness.run(h_b)
        assert r_b.returncode == 0, f"B 登记失败: {r_b.stderr}"
        b_entry = [f for f in os.listdir(registry) if f.endswith(".registry")]
        assert len(b_entry) == 1
        b_unit = [ln.split("=", 1)[1] for ln in
                  open(os.path.join(registry, b_entry[0])).read().splitlines()
                  if ln.startswith("unit=")][0]

        h_a = harness.build_harness(
            'registry_guard\necho "SHOULD_NOT_REACH" >> "$CALLS"\n',
            install_dir=dir_a, module=AGENT_MODULE, service_name_arg=b_unit,
            registry_dir=registry, registry_lock=lock)
        r_a = harness.run(h_a)
        assert r_a.returncode != 0, "目标 unit 已被其他存活实例占用时必须中止"
        assert "Instance conflict" in r_a.stderr, f"应给出明确冲突原因: {r_a.stderr}"
        assert "SHOULD_NOT_REACH" not in _calls(dir_a)
        assert proc_b.poll() is None, "冲突中止不得影响另一个实例"
    finally:
        _kill_quietly(proc_b)


def test_systemd_unit_owned_by_other_dir_fails_fast():
    text = harness.script_text()
    assert 'grep -qxF "WorkingDirectory=$INSTALL_DIR"' in text, \
        "写 unit 前必须校验既有 unit 的归属"
    assert "is owned by another worker instance" in text, \
        "归属冲突应 fail-fast 并给出明确提示"


# --------------------------------------------------------------------------
# PIDFile 限域层：_pid_is_own / _stop_pidfile（QA 审查前零覆盖，补齐第三层）
# --------------------------------------------------------------------------

def test_stop_pidfile_spares_foreign_pid_and_clears_file(tmp_path):
    """PIDFile 指向他实例存活 PID：只清文件、绝不 kill 他实例进程。"""
    dir_a, dir_b, registry, lock = _sandbox(tmp_path)
    proc_b = _spawn_fake_worker(os.path.join(dir_b, ".venv"), SANDBOX_MODULE)
    try:
        agent_pf = os.path.join(dir_a, "data", "worker.agent.pid")
        daemon_pf = os.path.join(dir_a, "data", "worker.pid")
        with open(agent_pf, "w") as f:
            f.write(f"{proc_b.pid}\n")
        with open(daemon_pf, "w") as f:
            f.write(f"{proc_b.pid}\n")
        body = (
            '_stop_pidfile "$AGENT_PIDFILE"\n'
            '_stop_pidfile "$DAEMON_PIDFILE"\n'
            f'if _pid_is_own "{proc_b.pid}"; then echo "OWN" >> "$CALLS"; '
            'else echo "FOREIGN" >> "$CALLS"; fi\n'
            'echo done\n'
        )
        h = harness.build_harness(body, install_dir=dir_a, module=AGENT_MODULE,
                                  registry_dir=registry, registry_lock=lock)
        r = harness.run(h)
        assert r.returncode == 0, f"harness 失败: {r.stderr}"
        assert proc_b.poll() is None, "PIDFile 记录他实例 PID 时不得误杀他实例进程"
        assert not os.path.exists(agent_pf), "指向他实例 PID 的 pidfile 应被清理"
        assert not os.path.exists(daemon_pf), "指向他实例 PID 的 pidfile 应被清理"
        assert "FOREIGN" in _calls(dir_a), \
            "_pid_is_own 应判定他实例 PID（cmdline 不含本实例 venv/启动脚本）为非本实例"
    finally:
        _kill_quietly(proc_b)


def test_stop_pidfile_kills_own_residual_pid(tmp_path):
    """PIDFile 指向本实例残留进程：限域停止本实例进程并清理 pidfile。"""
    dir_a, _, registry, lock = _sandbox(tmp_path)
    venv_a = os.path.join(dir_a, ".venv")
    proc_a = _spawn_fake_worker(venv_a, AGENT_MODULE)
    try:
        agent_pf = os.path.join(dir_a, "data", "worker.agent.pid")
        with open(agent_pf, "w") as f:
            f.write(f"{proc_a.pid}\n")
        body = (
            f'if _pid_is_own "{proc_a.pid}"; then echo "OWN" >> "$CALLS"; else echo "FOREIGN" >> "$CALLS"; fi\n'
            '_stop_pidfile "$AGENT_PIDFILE"\n'
            'echo done\n'
        )
        h = harness.build_harness(body, install_dir=dir_a, module=AGENT_MODULE,
                                  registry_dir=registry, registry_lock=lock)
        r = harness.run(h)
        assert r.returncode == 0, f"harness 失败: {r.stderr}"
        assert "OWN" in _calls(dir_a), "cmdline 指向本实例 venv 的 PID 应判定为本实例"
        assert _died_within(proc_a), "PIDFile 指向的本实例残留进程应被限域停止"
        assert not os.path.exists(agent_pf), "停止后 pidfile 应被清理"
    finally:
        _kill_quietly(proc_a)


def test_stop_pidfile_missing_or_garbage_is_noop(tmp_path):
    """PIDFile 缺失 / 内容非 PID：不 kill、不报错，仅清掉垃圾文件。"""
    dir_a, _, registry, lock = _sandbox(tmp_path)
    garbage_pf = os.path.join(dir_a, "data", "worker.pid")
    with open(garbage_pf, "w") as f:
        f.write("not-a-pid\n")
    body = (
        '_stop_pidfile "$INSTALL_DIR/data/worker.agent.pid"\n'
        '_stop_pidfile "$DAEMON_PIDFILE"\n'
        'echo "done" >> "$CALLS"\n'
    )
    h = harness.build_harness(body, install_dir=dir_a, module=AGENT_MODULE,
                              registry_dir=registry, registry_lock=lock)
    r = harness.run(h)
    assert r.returncode == 0, f"harness 失败: {r.stderr}"
    assert "done" in _calls(dir_a)
    assert not os.path.exists(garbage_pf), "垃圾 pidfile 应被清理"


# --------------------------------------------------------------------------
# --service-name 合法性校验（拼 root 路径 / systemctl 参数，必须 fail-fast）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad_name", [
    "../escape", "a b", "a;b", "a|b", "a(b)", "a/b", "a`id`b", "a$b",
])
def test_service_name_validation_rejects_unsafe_names(tmp_path, bad_name):
    """非法 unit 名在构造 SERVICE_FILE 前必须 fail-fast（防穿越/注入）。"""
    dir_a, _, registry, lock = _sandbox(tmp_path)
    h = harness.build_harness(
        'echo "UNIT=$SERVICE_NAME" >> "$CALLS"\n',
        install_dir=dir_a, module=AGENT_MODULE, service_name_arg=bad_name,
        registry_dir=registry, registry_lock=lock)
    r = harness.run(h)
    assert r.returncode != 0, f"非法 --service-name({bad_name!r}) 必须 fail-fast"
    assert "invalid --service-name" in r.stderr, f"应给出明确的非法提示: {r.stderr}"
    # 校验在写任何调用日志之前失败（fail 只走 stderr，不落 CALLS）
    calls_path = os.path.join(dir_a, "_calls.log")
    assert not os.path.exists(calls_path) or "UNIT=" not in open(calls_path).read(), \
        "校验失败后不得继续执行"


def test_service_name_validation_accepts_allowed_charset(tmp_path):
    """合法字符集（字母/数字/_/./-）的 unit 名正常通过校验。"""
    dir_a, _, registry, lock = _sandbox(tmp_path)
    h = harness.build_harness(
        'echo "UNIT=$SERVICE_NAME" >> "$CALLS"\necho done\n',
        install_dir=dir_a, module=AGENT_MODULE, service_name_arg="runtime-agent.custom_1-x",
        registry_dir=registry, registry_lock=lock)
    r = harness.run(h)
    assert r.returncode == 0, f"合法 --service-name 不应失败: {r.stderr}"
    assert "UNIT=runtime-agent.custom_1-x" in _calls(dir_a)


# --------------------------------------------------------------------------
# 路径 3：升级失败回滚 —— 只动本实例，回滚后重写登记
# --------------------------------------------------------------------------

def test_upgrade_failure_rollback_is_scoped_to_own_instance(tmp_path):
    dir_a, dir_b, registry, lock = _sandbox(tmp_path)
    os.makedirs(os.path.join(dir_a, ".upgrade-backup", "src"), exist_ok=True)
    os.makedirs(os.path.join(dir_a, "src"), exist_ok=True)
    with open(os.path.join(dir_a, ".upgrade-backup", "VERSION"), "w") as f:
        f.write("v-old\n")
    venv_a = os.path.join(dir_a, ".venv")
    argv0 = f"{venv_a}/bin/python -m {AGENT_MODULE}"
    start_script = os.path.join(dir_a, "start-worker.sh")
    with open(start_script, "w") as f:
        f.write(f"#!/usr/bin/env bash\nexec -a {shlex.quote(argv0)} sleep 120\n")
    os.chmod(start_script, 0o755)

    residual = _spawn_fake_worker(venv_a, AGENT_MODULE)
    proc_b = _spawn_fake_worker(os.path.join(dir_b, ".venv"), SANDBOX_MODULE)
    try:
        with open(os.path.join(registry, "stale.registry"), "w") as f:
            f.write(f"unit=runtime-agent-stale\ninstall_dir={dir_b}\n"
                    f"module={SANDBOX_MODULE}\ncred_type=worker\npid=999999\nupdated_at=0\n")

        h = harness.build_harness(
            "restore_upgrade_backup nohup\n"
            'echo "RB=$?" >> "$CALLS"\n'
            'echo "OWN=$(own_agent_pids | tr "\\n" ",")" >> "$CALLS"\n'
            'echo "REG=$(tr "\\n" ";" < "$REGISTRY_DIR/worker-$WORKER_INSTANCE_KEY.registry" 2>/dev/null || true)" >> "$CALLS"\n'
            'echo done\n',
            install_dir=dir_a, module=AGENT_MODULE, start_script=start_script,
            registry_dir=registry, registry_lock=lock, stub_sleep=False,
            extra_functions=["restore_upgrade_backup"])
        r = harness.run(h, timeout=120)
        assert r.returncode == 0, f"回滚 harness 失败: {r.stderr}"

        log = _calls(dir_a)
        assert "RB=0" in log, f"回滚应判定旧版已恢复: {log}"
        assert residual.poll() is not None, "回滚必须先停掉本实例残留的新进程"
        assert proc_b.poll() is None, "回滚不得动同机异构实例"
        assert f"install_dir={dir_a}" in log, f"回滚成功后应重写本实例登记: {log}"
        assert not os.path.exists(os.path.join(registry, "stale.registry")), \
            "陈旧登记应在启动时自愈清理"
    finally:
        _kill_by_pattern(venv_a, AGENT_MODULE)
        _kill_quietly(residual)
        _kill_quietly(proc_b)