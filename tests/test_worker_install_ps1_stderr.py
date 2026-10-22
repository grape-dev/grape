"""Issue #429: install-worker.ps1 pip stderr 终止性错误回归测试。

背景：$ErrorActionPreference="Stop" 下 PowerShell 对原生命令 2>$null 重定向会把 stderr 行
包装成错误记录，pip "new release available" 提示即触发终止性错误，安装中途失败。

本文件为纯静态断言（Linux CI 可跑）：
1. pip 调用点不得再使用裸 2>$null；
2. 脚本顶部设置 PIP_DISABLE_PIP_VERSION_CHECK 从源头静默；
3. Invoke-PipQuiet 辅助函数提供 EAP 防御层（Continue -> 执行 -> 恢复 -> $LASTEXITCODE）；
4. grape-cli 段失败语义不软化（仍显式判断 $LASTEXITCODE 走 fallback/warning）。
"""
import os
import re

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.ps1")


def _content():
    assert os.path.isfile(SCRIPT), "scripts/install-worker.ps1 应存在"
    with open(SCRIPT, encoding="utf-8-sig") as f:
        return f.read()


class TestPipStderrNotFatal:
    """#429 核心：pip stderr 提示不再触发终止性错误。"""

    def test_no_bare_pip_stderr_redirection(self):
        """pip 调用点不得再出现裸 2>$null（Find-Python/tarfile 兜底两处豁免，行为不变）。"""
        content = _content()
        # 找出所有 2>$null 调用行
        offending = []
        for i, line in enumerate(content.splitlines(), 1):
            if "2>$null" not in line:
                continue
            stripped = line.strip()
            # 注释行（文档说明）不算调用点
            if stripped.startswith("#"):
                continue
            # 豁免：Find-Python 的版本探测（失败走外层 catch 兜底）
            if "-c \"import sys" in line:
                continue
            # 豁免：Python tarfile 兜底解压（多行 python -c 命令的结尾行，
            # 外层有 try/catch Fail "Extract failed"，非 pip 路径）
            if "$TmpPkg $Dir 2>$null" in line:
                continue
            offending.append(f"line {i}: {stripped}")
        assert not offending, (
            "install-worker.ps1 仍有裸 2>$null 调用点（Issue #429 修复遗漏）: " + "; ".join(offending)
        )

    def test_pip_version_check_disabled_at_top(self):
        """PIP_DISABLE_PIP_VERSION_CHECK 必须在 EAP=Stop 之后、首个 pip 调用之前设置。"""
        content = _content()
        assert 'PIP_DISABLE_PIP_VERSION_CHECK' in content, "脚本应设置 PIP_DISABLE_PIP_VERSION_CHECK"
        eap_pos = content.find('$ErrorActionPreference = "Stop"')
        env_pos = content.find("$env:PIP_DISABLE_PIP_VERSION_CHECK")
        assert eap_pos != -1, "脚本应设置 $ErrorActionPreference = 'Stop'"
        assert env_pos != -1, "脚本应设置 $env:PIP_DISABLE_PIP_VERSION_CHECK"
        assert env_pos > eap_pos, "PIP_DISABLE_PIP_VERSION_CHECK 应在 EAP=Stop 之后设置"
        # 首个 pip 调用（_VenvPipOk / ensurepip / install）之前
        first_pip = min(
            (p for p in (
                content.find("-m pip"),
                content.find("-m ensurepip"),
                content.find("pip install"),
            ) if p != -1),
            default=len(content),
        )
        assert env_pos < first_pip, "PIP_DISABLE_PIP_VERSION_CHECK 应在首个 pip 调用之前设置"

    def test_invoke_pip_quiet_eap_pattern(self):
        """Invoke-PipQuiet 必须含 EAP 临时切换 + 恢复 + 退出码返回（tar 段同构模式）。"""
        content = _content()
        m = re.search(
            r"function\s+Invoke-PipQuiet\s*\{.*?\n\}", content, re.DOTALL
        )
        assert m, "脚本应定义 Invoke-PipQuiet 函数"
        body = m.group(0)
        assert '$ErrorActionPreference = "Continue"' in body, "Invoke-PipQuiet 应临时切 Continue"
        assert "$ErrorActionPreference = $prevEAP" in body, "Invoke-PipQuiet 应恢复原 EAP"
        assert "$exitCode = $LASTEXITCODE" in body, "Invoke-PipQuiet 应捕获原生命令退出码"
        assert "return $exitCode" in body, "Invoke-PipQuiet 应返回退出码（函数作用域内 $LASTEXITCODE 不向调用者传播）"
        assert "2>&1 | Out-Null" in body.replace("2>&1|Out-Null", "2>&1 | Out-Null"), (
            "Invoke-PipQuiet 应合并 stderr 丢弃输出（2>&1 | Out-Null）"
        )
        # 恢复必须在 try/catch 之后顺序执行（防异常路径漏恢复）
        try_pos = body.find("try {")
        catch_pos = body.find("} catch")
        restore_pos = body.find("$ErrorActionPreference = $prevEAP")
        assert -1 < try_pos < catch_pos < restore_pos, "EAP 恢复应在 try/catch 之后执行"
        # 异常路径默认失败（不读 stale $LASTEXITCODE）
        assert "$exitCode = 1" in body, "Invoke-PipQuiet 异常/默认路径应视为失败（exitCode=1）"

    def test_pip_calls_use_safe_wrapper(self):
        """pip 自愈段（ensurepip/get-pip）与 grape-cli 段的 pip 调用均通过 Invoke-PipQuiet。"""
        content = _content()
        # 自愈段：ensurepip 两处 + get-pip 两处不得再以裸 & $VenvPy 形式调用
        for pat, what in [
            (r"&\s*\$VenvPy\s+-m\s+ensurepip", "ensurepip 应走 Invoke-PipQuiet"),
            (r"&\s*\$VenvPy\s+\$getPkg", "get-pip.py 应走 Invoke-PipQuiet"),
            (r"&\s*\$VenvPip\s+install", "pip install 应走 Invoke-PipQuiet"),
        ]:
            hits = re.findall(pat, content)
            assert not hits, f"{what}（仍发现 {len(hits)} 处裸调用）"
        # 应有足够数量的 Invoke-PipQuiet 调用：自愈 5 处 + grape-cli 3 处 = 至少 8 处
        calls = re.findall(r"Invoke-PipQuiet\s+\$V(?:envPy|envPip)\b", content)
        assert len(calls) >= 8, f"Invoke-PipQuiet 调用应 >= 8 处（实际 {len(calls)}）"

    def test_grape_cli_still_checks_exitcode(self):
        """grape-cli 安装段失败语义不软化：仍显式判断退出码走 fallback / warning。"""
        content = _content()
        # grape-cli 段：离线失败回退在线 + 在线失败仅 warning（两段结构都保留）
        assert "falling back to online" in content, "离线失败回退在线的文案应保留"
        assert "grape CLI installed (online)" in content, "在线安装成功文案应保留"
        assert "grape-cli install failed" in content, "grape-cli 失败 warning 文案应保留（不升级为 Fail）"
        # grape-cli 段必须仍有退出码显式判断（$rc 来自 Invoke-PipQuiet 返回值；
        # $LASTEXITCODE 在函数作用域内不向调用者传播，必须用返回值判断）
        m = re.search(r"安装 grape CLI.*?(?=# ===== 5\.)", content, re.DOTALL)
        assert m, "应存在 grape CLI 安装段"
        seg = m.group(0)
        assert seg.count("$rc = Invoke-PipQuiet") >= 3, (
            f"grape-cli 段应保留 >= 3 处退出码判断（实际 {seg.count('$rc = Invoke-PipQuiet')}）"
        )
        assert seg.count("if ($rc -eq 0)") >= 3, "grape-cli 段应保留 >= 3 处 if ($rc -eq 0) 判断"
        assert "$LASTEXITCODE -eq 0" not in seg, (
            "grape-cli 段不得用 $LASTEXITCODE 判断（函数作用域内不传播，恒读到 stale 值）"
        )

    def test_idempotent_relevant_assertions_unchanged(self):
        """幂等相关要素不受影响：venv/配置复用与备份逻辑仍在。"""
        content = _content()
        assert "Test-Path $VenvPy" in content, "venv 复用判断（Test-Path $VenvPy）应保留"
        assert "Test-Path $EnvFile" in content, "凭据保留判断（Test-Path $EnvFile）应保留"
        assert ".upgrade-backup" in content, "升级备份目录逻辑应保留"
