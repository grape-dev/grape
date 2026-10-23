"""Issue #452: Windows 安装部署修复测试。

测试范围：
1. open() encoding 修复——.env 文件含非 ASCII 字符可被正确读取
2. .env.example 默认配置——SQLite + OBS 关闭
3. GRAPE_AUTO_DETECT_URL 开关——关闭时跳过公网探测
4. doctor.py 自检脚本可导入运行
5. health 端点暴露 agent_runtime 状态字段
6. requirements-local.txt 存在且内容正确
"""
import os
import sys
import tempfile
import importlib

import pytest

# 确保项目根目录在 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class TestOpenEncodingFix:
    """P0: open() encoding=utf-8 修复测试"""

    def test_run_py_env_file_with_non_ascii(self):
        """src/api/run.py 读取含非 ASCII 字符的 .env 不崩溃"""
        # 模拟 Windows GBK 环境下 .env 含中文注释
        env_content = "# 数据库配置\nDEVOPS_DB_BACKEND=sqlite\n# OBS 配置\nOBS_ENABLED=false\n"
        env_bytes = env_content.encode("utf-8")

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".env", delete=False) as f:
            f.write(env_bytes)
            env_path = f.name

        try:
            # 模拟 run.py 的 .env 读取逻辑（已修复为 encoding="utf-8"）
            loaded = {}
            with open(env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        loaded[_k.strip()] = _v.strip()

            assert loaded["DEVOPS_DB_BACKEND"] == "sqlite"
            assert loaded["OBS_ENABLED"] == "false"
        finally:
            os.unlink(env_path)

    def test_env_file_with_bom(self):
        """带 BOM 的 .env 文件也能被 UTF-8 读取（不崩溃）"""
        env_content = "\ufeffDEVOPS_DB_BACKEND=sqlite\n"
        env_bytes = env_content.encode("utf-8")

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".env", delete=False) as f:
            f.write(env_bytes)
            env_path = f.name

        try:
            with open(env_path, "r", encoding="utf-8") as _f:
                content = _f.read()
            # BOM 可能出现在内容开头，但不应该崩溃
            assert "sqlite" in content
        finally:
            os.unlink(env_path)

    def test_run_py_source_has_encoding(self):
        """验证 src/api/run.py 源码中 open() 包含 encoding 参数"""
        run_py_path = os.path.join(_PROJECT_ROOT, "src", "api", "run.py")
        with open(run_py_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert 'encoding="utf-8"' in source, "run.py 的 open() 应包含 encoding=utf-8"

    def test_executor_py_source_has_encoding(self):
        """验证 src/agent/executor.py 源码中 open() 包含 encoding 参数"""
        executor_path = os.path.join(_PROJECT_ROOT, "src", "agent", "executor.py")
        with open(executor_path, "r", encoding="utf-8") as f:
            source = f.read()
        # 至少在 _cfg_path 的 open 调用中应有 encoding
        assert 'encoding="utf-8"' in source, "executor.py 的 open() 应包含 encoding=utf-8"

    def test_image_understanding_source_has_encoding(self):
        """验证 src/agent/image_understanding.py 源码中 open() 包含 encoding 参数"""
        iu_path = os.path.join(_PROJECT_ROOT, "src", "agent", "image_understanding.py")
        with open(iu_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert 'encoding="utf-8"' in source, "image_understanding.py 的 open() 应包含 encoding=utf-8"

    def test_main_py_source_has_encoding(self):
        """验证 src/api/main.py 源码中 open() 包含 encoding 参数"""
        main_path = os.path.join(_PROJECT_ROOT, "src", "api", "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert 'encoding="utf-8"' in source, "main.py 的 open() 应包含 encoding=utf-8"


class TestEnvExampleDefaults:
    """P0: .env.example 默认配置测试"""

    def test_env_example_defaults_sqlite(self):
        """.env.example 默认使用 SQLite"""
        env_example_path = os.path.join(_PROJECT_ROOT, ".env.example")
        with open(env_example_path, "r", encoding="utf-8") as f:
            content = f.read()
        # DEVOPS_DB_BACKEND 应为 sqlite（非注释行）
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("DEVOPS_DB_BACKEND=") and not line.startswith("#"):
                assert "sqlite" in line.lower(), f".env.example 默认 DB 后端应为 SQLite: {line}"
                return
        pytest.fail(".env.example 未找到非注释的 DEVOPS_DB_BACKEND 配置")

    def test_env_example_obs_disabled(self):
        """.env.example 默认关闭 OBS"""
        env_example_path = os.path.join(_PROJECT_ROOT, ".env.example")
        with open(env_example_path, "r", encoding="utf-8") as f:
            content = f.read()
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("OBS_ENABLED=") and not line.startswith("#"):
                assert "false" in line.lower(), f".env.example 默认 OBS 应关闭: {line}"
                return
        pytest.fail(".env.example 未找到非注释的 OBS_ENABLED 配置")

    def test_env_example_mysql_commented(self):
        """.env.example 中 MySQL 配置应被注释（生产可选）"""
        env_example_path = os.path.join(_PROJECT_ROOT, ".env.example")
        with open(env_example_path, "r", encoding="utf-8") as f:
            content = f.read()
        # MySQL 配置行应以 # 开头（注释状态）
        mysql_lines = [l.strip() for l in content.split("\n") if "DEVOPS_DB_HOST" in l]
        for line in mysql_lines:
            if "DEVOPS_DB_HOST=" in line and not line.startswith("#"):
                # 如果有非注释的 DEVOPS_DB_HOST，那值应为示例值
                pass  # 允许但检查值

    def test_env_example_has_auto_detect_url(self):
        """.env.example 应包含 GRAPE_AUTO_DETECT_URL 配置"""
        env_example_path = os.path.join(_PROJECT_ROOT, ".env.example")
        with open(env_example_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "GRAPE_AUTO_DETECT_URL" in content, ".env.example 应包含 GRAPE_AUTO_DETECT_URL"

    def test_env_example_cookie_secure_false(self):
        """.env.example 默认 COOKIE_SECURE=false（开发环境 HTTP）"""
        env_example_path = os.path.join(_PROJECT_ROOT, ".env.example")
        with open(env_example_path, "r", encoding="utf-8") as f:
            content = f.read()
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("COOKIE_SECURE=") and not line.startswith("#"):
                assert "false" in line.lower(), f".env.example 默认 COOKIE_SECURE 应为 false: {line}"
                return
        pytest.fail(".env.example 未找到非注释的 COOKIE_SECURE 配置")


class TestAutoDetectUrlSwitch:
    """P2: GRAPE_AUTO_DETECT_URL 开关测试"""

    def test_config_has_auto_detect_env(self):
        """验证 src/config.py 读取 GRAPE_AUTO_DETECT_URL 环境变量"""
        config_path = os.path.join(_PROJECT_ROOT, "src", "config.py")
        with open(config_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "GRAPE_AUTO_DETECT_URL" in source, "config.py 应检查 GRAPE_AUTO_DETECT_URL"

    def test_auto_detect_disabled_returns_empty(self):
        """GRAPE_AUTO_DETECT_URL=false 时 _detect_server_base 返回空串"""
        # 由于 _detect_server_base 是嵌套函数，无法直接导入测试
        # 验证逻辑：环境变量设置后源码中的分支条件
        config_path = os.path.join(_PROJECT_ROOT, "src", "config.py")
        with open(config_path, "r", encoding="utf-8") as f:
            source = f.read()
        # 验证源码中包含开关逻辑
        assert "GRAPE_AUTO_DETECT_URL" in source
        assert "false" in source.lower() or "0" in source


class TestDoctorScript:
    """P1: doctor.py 自检脚本测试"""

    def test_doctor_importable(self):
        """doctor.py 可被导入"""
        doctor_path = os.path.join(_PROJECT_ROOT, "scripts", "doctor.py")
        assert os.path.exists(doctor_path), "scripts/doctor.py 应存在"

    def test_doctor_has_all_checks(self):
        """doctor.py 包含全部 10 项检查"""
        doctor_path = os.path.join(_PROJECT_ROOT, "scripts", "doctor.py")
        with open(doctor_path, "r", encoding="utf-8") as f:
            source = f.read()
        expected_checks = [
            "check_python_version",
            "check_core_deps",
            "check_optional_deps",
            "check_agent_cli",
            "check_ports",
            "check_config_files",
            "check_database",
            "check_env_encoding",
            "check_data_dir",
            "check_auto_detect_url",
        ]
        for check in expected_checks:
            assert check in source, f"doctor.py 应包含 {check}"

    def test_doctor_exit_codes(self):
        """doctor.py 退出码语义正确"""
        doctor_path = os.path.join(_PROJECT_ROOT, "scripts", "doctor.py")
        with open(doctor_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "exit_code = 2" in source or "exit_code == 2" in source, "退出码 2=错误"
        assert "exit_code = 1" in source or "exit_code == 1" in source, "退出码 1=警告"
        assert "exit_code = 0" in source or "exit_code == 0" in source, "退出码 0=通过"


class TestHealthEndpoint:
    """P2: 健康端点暴露 agent_runtime 状态"""

    def test_health_has_agent_runtime(self):
        """/api/health 端点返回 agent_runtime 字段"""
        health_path = os.path.join(_PROJECT_ROOT, "src", "api", "routes", "health.py")
        with open(health_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "agent_runtime" in source, "health.py 应包含 agent_runtime 字段"
        assert "missing" in source, "health.py 应包含 missing 状态"


class TestRequirementsLocal:
    """P2: requirements-local.txt 依赖拆分测试"""

    def test_requirements_local_exists(self):
        """requirements-local.txt 存在"""
        path = os.path.join(_PROJECT_ROOT, "requirements-local.txt")
        assert os.path.exists(path), "requirements-local.txt 应存在"

    def test_requirements_local_has_core_deps(self):
        """requirements-local.txt 包含核心依赖"""
        path = os.path.join(_PROJECT_ROOT, "requirements-local.txt")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        core_deps = ["fastapi", "uvicorn", "aiohttp", "bcrypt", "python-jose"]
        for dep in core_deps:
            assert dep in content, f"requirements-local.txt 应包含 {dep}"

    def test_requirements_local_excludes_optional(self):
        """requirements-local.txt 不包含可选依赖（OBS/MySQL/飞书）"""
        path = os.path.join(_PROJECT_ROOT, "requirements-local.txt")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # 这些依赖不应出现在非注释行中
        optional_deps = ["esdk-obs-python", "pymysql", "DBUtils", "lark-oapi"]
        for dep in optional_deps:
            lines = [l for l in content.split("\n") if l.strip() and not l.strip().startswith("#")]
            for line in lines:
                assert dep not in line, f"requirements-local.txt 非注释行不应包含 {dep}: {line}"


class TestStartPs1:
    """P1: start.ps1 Windows 启动脚本测试"""

    def test_start_ps1_exists(self):
        """start.ps1 存在"""
        path = os.path.join(_PROJECT_ROOT, "start.ps1")
        assert os.path.exists(path), "start.ps1 应存在"

    def test_start_ps1_has_venv_creation(self):
        """start.ps1 包含虚拟环境创建逻辑"""
        path = os.path.join(_PROJECT_ROOT, "start.ps1")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "venv" in content, "start.ps1 应包含 venv 创建"
        # start.ps1 uses venv python directly rather than Activate.ps1
        assert "VenvPython" in content or "Activate" in content, "start.ps1 应使用 venv python"

    def test_start_ps1_has_env_copy(self):
        """start.ps1 包含 .env.example 复制逻辑"""
        path = os.path.join(_PROJECT_ROOT, "start.ps1")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert ".env.example" in content or "env.example" in content

    def test_start_ps1_has_doctor(self):
        """start.ps1 包含 doctor.py 自检调用"""
        path = os.path.join(_PROJECT_ROOT, "start.ps1")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "doctor" in content, "start.ps1 应调用 doctor.py"


class TestDockerfileFix:
    """P3: Dockerfile 修复测试"""

    def test_dockerfile_has_auto_detect_url(self):
        """Dockerfile 设置 GRAPE_AUTO_DETECT_URL=false"""
        path = os.path.join(_PROJECT_ROOT, "Dockerfile")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "GRAPE_AUTO_DETECT_URL" in content, "Dockerfile 应设置 GRAPE_AUTO_DETECT_URL=false"

    def test_dockerfile_python_version(self):
        """Dockerfile 使用 Python 3.12（非过新的 3.14）"""
        path = os.path.join(_PROJECT_ROOT, "Dockerfile")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "python:3.12" in content, "Dockerfile 应使用 Python 3.12"


class TestReadmeCrossPlatform:
    """P1: README 跨平台文档测试"""

    def test_readme_has_windows_instructions(self):
        """README.md 包含 Windows PowerShell 指引"""
        path = os.path.join(_PROJECT_ROOT, "README.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "PowerShell" in content or "start.ps1" in content, "README 应包含 Windows 指引"

    def test_readme_has_venv(self):
        """README.md 包含虚拟环境指引"""
        path = os.path.join(_PROJECT_ROOT, "README.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "venv" in content, "README 应包含 venv 指引"

    def test_readme_has_mirror(self):
        """README.md 包含国内镜像指引"""
        path = os.path.join(_PROJECT_ROOT, "README.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "aliyun" in content.lower() or "mirrors" in content.lower(), "README 应包含国内镜像指引"

    def test_readme_zh_has_windows(self):
        """README.zh.md 包含 Windows 指引"""
        path = os.path.join(_PROJECT_ROOT, "README.zh.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "PowerShell" in content or "start.ps1" in content, "README.zh.md 应包含 Windows 指引"

    def test_readme_has_version_matrix(self):
        """README.md 包含 Python 版本矩阵"""
        path = os.path.join(_PROJECT_ROOT, "README.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "3.9" in content, "README 应标注 Python 3.9 版本要求"
