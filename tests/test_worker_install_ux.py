"""Issue #499 静态守卫：安装脚本无引擎引导语 + 输出全英文 + 多平台 wheels 离线优先。

纯静态断言（Linux CI 可跑，不执行脚本）：
1. sh/ps1 引擎安装引导段已删除（worker 启动自动发现引擎，安装期无需引导）；
2. 脚本用户可见输出（log/fail/echo/printf / Log/Fail/Write-*）无中文字符；
3. ps1 不再有 wheels 离线安装的 ARM64-only 门槛（多平台 wheels 后全平台先离线）；
4. sh/ps1 离线优先 + 在线兜底路径保留。
"""
import os
import re

SH = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.sh")
PS1 = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.ps1")

_CJK = re.compile(r"[\u4e00-\u9fff]")
# sh 输出语句：log/fail/echo/printf 后的双引号或单引号字符串（注释行跳过）
_SH_OUT = re.compile(r'\b(?:log|fail|echo|printf)\s+"([^"]*)"|\b(?:log|fail|echo|printf)\s+\'([^\']*)\'')
# ps1 输出语句：Log/Fail/Write-Host/Write-Output/Write-Error/throw 后的双引号字符串
_PS1_OUT = re.compile(r'\b(?:Log|Fail|Write-Host|Write-Output|Write-Error|throw)\s+"([^"]*)"')


def _sh():
    assert os.path.isfile(SH), "scripts/install-worker.sh should exist"
    with open(SH, encoding="utf-8") as f:
        return f.read()


def _ps1():
    assert os.path.isfile(PS1), "scripts/install-worker.ps1 should exist"
    with open(PS1, encoding="utf-8-sig") as f:
        return f.read()


def _output_strings(content, pattern):
    out = []
    for line in content.splitlines():
        if line.lstrip().startswith("#"):
            continue  # 注释不是输出
        for m in pattern.finditer(line):
            out.append(m.group(1) or m.group(2) or "")
    return out


class TestNoEngineInstallHints:
    """Issue #499 核心：安装输出不再出现引擎安装引导语。"""

    def test_sh_engine_hint_section_removed(self):
        content = _sh()
        assert "_engine_hint" not in content, "sh 引擎提示段（第 7 节）应整段删除"

    def test_sh_no_engine_guidance_output(self):
        content = _sh()
        assert "当前未检测到执行引擎" not in content
        assert "装好引擎后自动注册" not in content

    def test_ps1_engine_hints_removed(self):
        content = _ps1()
        assert "engineHints" not in content, "ps1 engineHints 引导段应整段删除"
        assert "Some execution engines not detected" not in content

    def test_engine_autodiscover_unaffected(self):
        """引擎自动发现机制保留：runtime 侧 discover_backends 四引擎不受影响。"""
        from src.agent.executor import discover_backends
        names = {r.get("name") for r in discover_backends()}
        for engine in ("opencode", "hermes", "codearts", "codex"):
            assert engine in names, f"{engine} missing from discover_backends"


class TestEnglishOnlyOutput:
    """安装输出统一英文：用户可见输出语句无中文字符。"""

    def test_sh_outputs_english(self):
        offending = [s for s in _output_strings(_sh(), _SH_OUT) if _CJK.search(s)]
        assert not offending, f"install-worker.sh 输出仍含中文: {offending}"

    def test_ps1_outputs_english(self):
        offending = [s for s in _output_strings(_ps1(), _PS1_OUT) if _CJK.search(s)]
        assert not offending, f"install-worker.ps1 输出仍含中文: {offending}"

    def test_ps1_no_cjk_string_literals(self):
        """用户可见字符串字面量（含兜底值如「未知版本」）不得含中文。"""
        content = _ps1()
        assert "未知版本" not in content
        for m in re.finditer(r'"([^"\n]*)"', content):
            assert not _CJK.search(m.group(1)), f"ps1 字符串字面量含中文: {m.group(1)}"


class TestOfflineWheelsFirst:
    """多平台 wheels 预置后：全平台首装先走包内离线，失败在线兜底。"""

    def test_ps1_arm64_only_gate_removed(self):
        content = _ps1()
        assert "PROCESSOR_ARCHITECTURE" not in content, (
            "ps1 不应再有 ARM64-only 门槛（多平台 wheels 后 Windows 也应先离线）"
        )
        assert "Installing deps from bundled wheels (offline)" in content

    def test_ps1_online_fallback_kept(self):
        content = _ps1()
        assert "falling back to online" in content
        assert "Installing deps online (mirror" in content

    def test_sh_offline_first_and_fallback_kept(self):
        content = _sh()
        assert "--no-index" in content and "--find-links" in content
        assert "Installing deps online (mirror" in content
