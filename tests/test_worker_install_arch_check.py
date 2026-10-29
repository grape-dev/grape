"""Issue #515 静态守卫 + 函数 harness：install-worker.sh 包内解释器架构校验。

背景：旧实现 3.5 步仅以 [ -x python/bin/python3 ] 判定包内解释器可用，x86_64 机器
选中 aarch64 解释器后 venv 创建时报 `Exec format error` 直接退出（set -e），无法
到达「依赖离线失败→在线回退」兜底。本次改为双保险：
  ① TARGET_ARCH（uname -m 归一化）选 python-<arch>/ 目录；
  ② 试运行 --version 确认可执行（免解析 ELF）。
任一不通过 → 跳过包内解释器并引导安装系统 Python>=3.10。

测试策略：
- 静态断言：脚本含归一化/分目录选择/试运行/日志/引导提示；
- harness：提取 _pick_bundled_python() 函数体在隔离 sandbox 运行，
  三分支断言（架构匹配可用 / 架构不匹配不可执行 / 旧包 python/ 兜底）。
"""
import os
import re
import subprocess
import tempfile

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SH = os.path.join(ROOT, "scripts", "install-worker.sh")


def _script():
    assert os.path.isfile(SH), "scripts/install-worker.sh should exist"
    with open(SH, encoding="utf-8") as f:
        return f.read()


def _extract_pick_bundled_python():
    """提取 scripts/install-worker.sh 中的 _pick_bundled_python() 函数文本。"""
    script = _script()
    start_marker = "_pick_bundled_python() {"
    start = script.find(start_marker)
    assert start != -1, "install-worker.sh 应包含 _pick_bundled_python()"
    depth = 0
    for i in range(start + len(start_marker) - 1, len(script)):
        ch = script[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "#!/usr/bin/env bash\n" + script[start:i + 1]
    raise AssertionError("未能定位 _pick_bundled_python 闭合大括号")


class TestStaticArchGuard:
    """静态守卫：脚本包含架构归一化 / 分目录选择 / 试运行 / 日志 / 引导提示。"""

    def test_target_arch_normalization_present(self):
        content = _script()
        assert 'TARGET_ARCH="$(uname -m)"' in content, "应前置计算 TARGET_ARCH"
        assert re.search(r'x86_64\|amd64\)[^\n]*TARGET_ARCH="x86_64"', content), "x86_64/amd64 归一化"
        assert re.search(r'aarch64\|arm64\)[^\n]*TARGET_ARCH="aarch64"', content), "aarch64/arm64 归一化"

    def test_per_arch_dir_selection_present(self):
        content = _script()
        assert "python-$TARGET_ARCH/bin/python3" in content, "应按 TARGET_ARCH 选 python-<arch>/ 目录"

    def test_version_trial_run_present(self):
        content = _script()
        assert re.search(r'"\$_cand" --version', content), "应试运行 --version 校验可执行"

    def test_target_arch_log_present(self):
        content = _script()
        assert re.search(r'log "[^"]*(Arch: \$TARGET_ARCH|arch=\$TARGET_ARCH)[^"]*"', content), \
            "安装日志应打印目标机架构（首日志 Arch: 或 3.5 步 arch=）"

    def test_system_python_hint_present(self):
        content = _script()
        assert re.search(r"install a system Python>=3\.10", content), "应引导安装系统 Python>=3.10"
        assert "apt/yum install python3.12 python3.12-venv" in content, "应给出具体安装命令"

    def test_old_python_dir_fallback_kept(self):
        content = _script()
        assert '"$INSTALL_DIR/python/bin/python3"' in content, "旧包 python/ 目录兜底保留"


def _make_pkg_python(install_dir, rel_py, exit_code):
    """构造一个"包内解释器"脚本：运行 --version 时按 exit_code 退出（模拟可执行/Exec format error）。"""
    py = os.path.join(install_dir, rel_py)
    os.makedirs(os.path.dirname(py), exist_ok=True)
    with open(py, "w", encoding="utf-8") as f:
        f.write(f"#!/usr/bin/env bash\nexit {exit_code}\n")
    os.chmod(py, 0o755)


class TestPickBundledPythonHarness:
    """_pick_bundled_python() 三分支行为。"""

    def _run(self, target_arch, install_dir):
        func = _extract_pick_bundled_python()
        out = os.path.join(install_dir, "out.txt")
        harness = f"""
set -u
{func}
LOG_FILE={out!r}
log() {{ echo "$*" >> "$LOG_FILE"; }}
TARGET_ARCH={target_arch!r}
INSTALL_DIR={install_dir!r}
RESULT="$(_pick_bundled_python || true)"
echo "RESULT=$RESULT" >> "$LOG_FILE"
"""
        r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"harness 失败: {r.stderr}"
        with open(out, encoding="utf-8") as f:
            return f.read().strip().splitlines()

    def test_matching_arch_picked(self, tmp_path):
        """x86_64 目标机 + python-x86_64/bin/python3 可执行 → 选中包内解释器。"""
        install_dir = os.path.join(str(tmp_path), "grape")
        _make_pkg_python(install_dir, "python-x86_64/bin/python3", 0)
        lines = self._run("x86_64", install_dir)
        assert any("RESULT=" + os.path.join(install_dir, "python-x86_64/bin/python3") in ln for ln in lines), lines

    def test_mismatched_arch_rejected(self, tmp_path):
        """x86_64 目标机 + 只有 python-aarch64（不可执行，模拟 Exec format error）→ 不选。"""
        install_dir = os.path.join(str(tmp_path), "grape")
        _make_pkg_python(install_dir, "python-aarch64/bin/python3", 1)
        lines = self._run("x86_64", install_dir)
        assert any(ln == "RESULT=" for ln in lines), f"架构不匹配应跳过包内解释器: {lines}"

    def test_old_python_dir_fallback(self, tmp_path):
        """aarch64 目标机 + 旧包 python/ 目录可执行 → 兜底选中（兼容老主控旧包）。"""
        install_dir = os.path.join(str(tmp_path), "grape")
        _make_pkg_python(install_dir, "python/bin/python3", 0)
        lines = self._run("aarch64", install_dir)
        assert any("RESULT=" + os.path.join(install_dir, "python/bin/python3") in ln for ln in lines), lines

    def test_corrupted_bundled_python_rejected(self, tmp_path):
        """目录存在但 --version 不可执行（损坏）→ 不选，等系统 Python 引导。"""
        install_dir = os.path.join(str(tmp_path), "grape")
        _make_pkg_python(install_dir, "python-x86_64/bin/python3", 127)
        _make_pkg_python(install_dir, "python/bin/python3", 127)
        lines = self._run("x86_64", install_dir)
        assert any(ln == "RESULT=" for ln in lines), f"损坏解释器应被拒绝: {lines}"