"""Worker 一键安装脚本端点测试（worker-install-optimization-plan.md 方案 B）。

GET /api/runtime/install 返回 install-worker.sh 内容（只读、无敏感信息，token 由调用方注入）。
"""
import os
import pytest


def test_install_endpoint_returns_script():
    from fastapi.testclient import TestClient
    from src.api.main import app
    client = TestClient(app)
    r = client.get("/api/runtime/install")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    body = r.text
    # 应含脚本关键内容，且不含真实 token（脚本用占位参数）
    assert "install-worker" in body
    assert "--token" in body
    assert "runtime-agent.service" in body
    assert "RUNTIME_SERVER_URL" in body
    # 脚本不应硬编码任何真实 rta_ token（RUNTIME_TOKEN 用 $TOKEN 变量引用，token 由调用方传入）
    assert "RUNTIME_TOKEN=$TOKEN" in body


def test_install_script_exists_on_disk():
    """脚本文件应存在于仓库 scripts/ 目录，且包含当前安装流程关键要素。"""
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.sh")
    assert os.path.isfile(p), "scripts/install-worker.sh 应存在"
    content = open(p, encoding="utf-8").read()
    # 核心要素：安装入口标记 / 系统 Python 检测（>=3.10，含 3.12）/ token 占位 / 服务注册
    assert "install-worker" in content
    assert "python3.12" in content or "python3.11" in content
    assert "RUNTIME_TOKEN=$TOKEN" in content
    assert "RUNTIME_SERVER_URL=$SERVER" in content
    assert "runtime-agent" in content


def test_install_ps1_endpoint_returns_script():
    """GET /api/runtime/install.ps1 返回 PowerShell 安装脚本内容。"""
    from fastapi.testclient import TestClient
    from src.api.main import app
    client = TestClient(app)
    r = client.get("/api/runtime/install.ps1")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    body = r.text
    # 应含 PowerShell 脚本关键内容
    assert "install-worker" in body
    assert "Token" in body or "-Token" in body
    assert "RUNTIME_SERVER_URL" in body
    assert "src.runtime.agent" in body
    # 不应硬编码任何真实 rta_ token
    assert "rta_" not in body or "rta_" in body.split("$Token")[0]  # token 由参数注入


def test_install_ps1_script_exists_on_disk():
    """PowerShell 脚本文件应存在于仓库 scripts/ 目录。"""
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.ps1")
    assert os.path.isfile(p), "scripts/install-worker.ps1 应存在"
    content = open(p, encoding="utf-8").read()
    # 核心要素
    assert "install-worker" in content
    assert "Token" in content
    assert "RUNTIME_SERVER_URL" in content
    assert "runtime-agent" in content
    assert "venv" in content
    assert "Start-Process" in content


def test_start_worker_ps1_exists_on_disk():
    """PowerShell 启动脚本应存在。"""
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "start-worker.ps1")
    assert os.path.isfile(p), "scripts/start-worker.ps1 应存在"
    content = open(p, encoding="utf-8").read()
    assert "src.runtime.agent" in content
    assert "Restart" in content or "restart" in content.lower()
    assert "daemon" in content.lower() or "NoDaemon" in content


# ===== B1 (Issue #257): venv 三级自愈测试 =====

class TestVenvSelfHealing:
    """B1: install-worker.sh / .ps1 venv pip 损坏三级自愈。"""

    def test_install_sh_has_pip_health_check(self):
        """install-worker.sh 应检测 pip 可用性（不只是文件存在）。"""
        p = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.sh")
        content = open(p, encoding="utf-8").read()
        # 应含 pip --version 健康检测
        assert "pip --version" in content or "_venv_pip_ok" in content
        # 应含三级自愈
        assert "ensurepip" in content
        assert "get-pip" in content or "bootstrap.pypa.io" in content
        assert "rm -rf" in content and "venv" in content.lower()  # Level 3: 重建 venv

    def test_install_ps1_has_pip_health_check(self):
        """install-worker.ps1 应检测 pip 可用性（不只是 Test-Path）。"""
        p = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.ps1")
        content = open(p, encoding="utf-8").read()
        # 应含 pip --version 健康检测
        assert "_VenvPipOk" in content or "pip --version" in content
        # 应含三级自愈
        assert "ensurepip" in content
        assert "get-pip" in content or "bootstrap.pypa.io" in content
        assert "Rebuild" in content or "rebuild" in content  # Level 3: 重建 venv

    def test_install_sh_three_level_progression(self):
        """install-worker.sh 应有三级递进自愈结构。"""
        p = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.sh")
        content = open(p, encoding="utf-8").read()
        # 三级标记
        assert "[1/3]" in content
        assert "[2/3]" in content
        assert "[3/3]" in content


# ===== B4 (Issue #257): Windows GBK 乱码修复测试 =====

class TestInstallPs1Bom:
    """B4 复盘（2026-09-29）：install.ps1 端点不预置 BOM（字符串执行场景），start-worker.ps1 端点预置。"""

    def test_install_ps1_endpoint_returns_bom(self):
        """GET /api/runtime/install.ps1 返回内容应无 BOM（字符串执行：irm | iex / [scriptblock]::Create）。

        BOM 会被 irm 解码为 U+FEFF 残留在脚本串开头，PS5.1 解析 param() 失败。
        文件执行场景由 _perform_upgrade 下载侧补 BOM（agent.py startswith 检测）。
        """
        from fastapi.testclient import TestClient
        from src.api.main import app
        client = TestClient(app)
        r = client.get("/api/runtime/install.ps1")
        assert r.status_code == 200
        raw = r.content
        assert raw[:3] != b"\xef\xbb\xbf", "install.ps1 response must NOT start with BOM (breaks [scriptblock]::Create)"
        assert raw[:1] == b"#", "install.ps1 should start with comment header"

    def test_start_worker_ps1_endpoint_returns_bom(self):
        """GET /api/runtime/start-worker.ps1 返回内容应预置 UTF-8 BOM（irm -OutFile 写文件场景）。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        client = TestClient(app)
        r = client.get("/api/runtime/start-worker.ps1")
        assert r.status_code == 200
        raw = r.content
        assert raw[:3] == b"\xef\xbb\xbf", "start-worker.ps1 response should start with UTF-8 BOM (file scenario)"

    def test_install_ps1_log_messages_are_ascii(self):
        """install-worker.ps1 的 Log/Fail 消息应 ASCII 化（无中文）。"""
        p = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.ps1")
        content = open(p, encoding="utf-8").read()
        # 提取所有 Log "..." 和 Fail "..." 行
        import re
        log_fail_lines = re.findall(r'(?:Log|Fail)\s*"([^"]*)"', content)
        for msg in log_fail_lines:
            # Log/Fail 消息中不应含非 ASCII 字符
            non_ascii = [c for c in msg if ord(c) > 127]
            assert not non_ascii, f"Log/Fail message contains non-ASCII: {msg}"

    def test_worker_requirements_are_ascii(self):
        """requirements-worker.txt 随 worker 包分发，必须纯 ASCII——pip auto_decode
        在非 UTF-8 locale（如中文 Windows GBK）下按系统首选编码解码该文件，
        非 ASCII 字节会直接 UnicodeDecodeError（Issue #1409 实测）。"""
        p = os.path.join(os.path.dirname(__file__), "..", "requirements-worker.txt")
        raw = open(p, "rb").read()
        bad = sorted({b for b in raw if b > 0x7F})
        assert not bad, (
            "requirements-worker.txt contains non-ASCII bytes; it is shipped in "
            "the worker package and must stay ASCII-only (Issue #1409): "
            + ", ".join(f"0x{b:02x}" for b in bad)
        )

    def test_install_ps1_pythonutf8_scoped_to_pip_calls(self):
        """install-worker.ps1 的 PYTHONUTF8=1 应收敛在 Invoke-PipQuiet 内（设完即恢复），
        不得在脚本顶层设置——顶层设置会被 Start-Process 拉起的常驻 worker daemon
        继承，静默改变其运行时默认编码（Issue #1409 PR #1010 M1 整改）。"""
        p = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.ps1")
        content = open(p, encoding="utf-8").read()
        assert '$env:PYTHONUTF8 = "1"' in content, "PYTHONUTF8=1 sentinel missing"
        # 顶层设置 = 出现在 Invoke-PipQuiet 函数体之外
        fn_start = content.index("function Invoke-PipQuiet")
        fn_end = content.index("}", content.index("return $exitCode", fn_start))
        outside = content[:fn_start] + content[fn_end:]
        assert '$env:PYTHONUTF8 = "1"' not in outside, (
            "PYTHONUTF8=1 must only be set inside Invoke-PipQuiet (restore after "
            "each call), not at script top level where the resident worker daemon "
            "inherits it"
        )
        # 函数体内应有恢复逻辑
        body = content[fn_start:fn_end]
        assert "prevUTF8" in body and "Remove-Item Env:PYTHONUTF8" in body

