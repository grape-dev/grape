#!/usr/bin/env python3
"""Grape 安装环境自检脚本（preflight checker）。

检查项：
  1. Python 版本（主服务 >= 3.9，devbox sandbox >= 3.10）
  2. 核心依赖完整性（fastapi/uvicorn/aiohttp 等）
  3. 可选依赖状态（OBS/MySQL/飞书）
  4. Agent CLI 是否安装（opencode/codearts）
  5. 端口占用检查（3000/8080）
  6. 配置文件完整性（.env / configs/*.json）
  7. 数据库连通性（SQLite/MySQL）
  8. .env 编码安全性（是否含非 ASCII 字符、是否有 encoding 风险）
  9. 目录权限（data/ 可写）
 10. 公网 IP 探测开关状态

用法：
  python scripts/doctor.py          # 全量检查（检查结果含修复建议）

退出码：
  0 = 全部通过
  1 = 有警告（可运行但建议修复）
  2 = 有错误（阻断启动）
"""
import os
import sys
import json
import socket
import platform
import shutil

# 确保项目根目录在 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class CheckResult:
    """单项检查结果。"""

    def __init__(self, name, status, detail="", fix=""):
        self.name = name
        self.status = status  # "ok" / "warn" / "error" / "skip"
        self.detail = detail
        self.fix = fix

    def __str__(self):
        icon = {"ok": "✅", "warn": "⚠️", "error": "❌", "skip": "⏭️"}[self.status]
        line = f"  {icon} {self.name}"
        if self.detail:
            line += f": {self.detail}"
        if self.fix and self.status != "ok":
            line += f"\n     → 修复建议: {self.fix}"
        return line


def _try_import(module_name):
    """尝试导入模块，返回 (成功?, 错误信息)。"""
    try:
        __import__(module_name)
        return True, ""
    except ImportError as e:
        return False, str(e)
    except Exception as e:
        return False, f"导入异常: {e}"


def check_python_version():
    """1. Python 版本检查"""
    ver = sys.version_info
    ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"

    if ver < (3, 9):
        return CheckResult(
            "Python 版本", "error",
            f"当前 {ver_str}，主服务需要 >= 3.9",
            "升级 Python 到 3.9+（推荐 3.12）"
        )
    if ver < (3, 10):
        return CheckResult(
            "Python 版本", "warn",
            f"当前 {ver_str}，主服务可用但 devbox sandbox 需 >= 3.10",
            "如需 devbox 功能，升级到 Python 3.10+"
        )
    return CheckResult("Python 版本", "ok", f"{ver_str} ({platform.python_implementation()})")


def check_core_deps():
    """2. 核心依赖检查"""
    core_modules = [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("aiohttp", "aiohttp"),
        ("pyyaml", "yaml"),
        ("bcrypt", "bcrypt"),
        ("python-jose", "jose"),
    ]
    missing = []
    for pkg, mod in core_modules:
        ok, err = _try_import(mod)
        if not ok:
            missing.append(pkg)

    if missing:
        return CheckResult(
            "核心依赖", "error",
            f"缺失: {', '.join(missing)}",
            f"pip install -r requirements.txt"
        )
    return CheckResult("核心依赖", "ok", "全部已安装")


def check_optional_deps():
    """3. 可选依赖检查"""
    optional = [
        ("esdk-obs-python", "obs", "OBS 对象存储"),
        ("pymysql", "pymysql", "MySQL 数据库"),
        ("DBUtils", "dbutils", "MySQL 连接池"),
        ("lark-oapi", "lark_oapi", "飞书 Bot"),
        ("paramiko", "paramiko", "SSH 远程执行"),
    ]
    results = []
    for pkg, mod, desc in optional:
        ok, _ = _try_import(mod)
        status = "ok" if ok else "warn"
        results.append(f"{pkg} ({desc}): {'已安装' if ok else '未安装（可选）'}")

    return CheckResult("可选依赖", "ok" if all(
        _try_import(m)[0] for _, m, _ in optional
    ) else "warn", "; ".join(results),
        "本地开发可安装 requirements-local.txt（最小集，不含 OBS/MySQL/飞书）")


def check_agent_cli():
    """4. Agent CLI 检查"""
    clis = ["opencode", "codearts"]
    found = []
    missing = []
    for cli in clis:
        path = shutil.which(cli)
        if path:
            found.append(f"{cli} ({path})")
        else:
            missing.append(cli)

    if found:
        return CheckResult("Agent CLI", "ok", f"已安装: {', '.join(found)}")
    return CheckResult(
        "Agent CLI", "warn",
        f"未安装任何 Agent CLI ({', '.join(missing)})",
        "npm install -g opencode 或 pip install codearts-cli"
    )


def check_port(host, port):
    """检查端口是否被占用"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex((host, port))
            return result == 0  # True = 占用
    except Exception:
        return False


def check_ports():
    """5. 端口占用检查"""
    ports_to_check = [
        (3000, "主 API"),
        (8080, "Webhook (生产模式)"),
    ]
    occupied = []
    for port, desc in ports_to_check:
        if check_port("127.0.0.1", port):
            occupied.append(f"{port} ({desc})")

    if occupied:
        return CheckResult(
            "端口占用", "warn",
            f"端口已占用: {', '.join(occupied)}",
            "停止占用进程或修改 GRAPE_PORT 环境变量"
        )
    return CheckResult("端口占用", "ok", "3000/8080 均空闲")


def check_config_files():
    """6. 配置文件检查"""
    config_dir = os.path.join(_PROJECT_ROOT, "configs")
    env_path = os.path.join(_PROJECT_ROOT, ".env")

    issues = []

    # .env 文件
    if not os.path.exists(env_path):
        env_example = os.path.join(_PROJECT_ROOT, ".env.example")
        if os.path.exists(env_example):
            issues.append(".env 不存在（可从 .env.example 复制）")
        else:
            issues.append(".env 和 .env.example 均不存在")
    else:
        # 检查 .env 是否可被 UTF-8 解码（Issue #452 核心）
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                f.read()
        except UnicodeDecodeError:
            issues.append(".env 文件包含非 UTF-8 字符（可能导致 GBK 环境崩溃）")

    # 关键配置文件
    required_configs = ["global-bot-token.json"]
    for cfg in required_configs:
        cfg_path = os.path.join(config_dir, cfg)
        example_path = os.path.join(config_dir, cfg.replace(".json", ".example.json"))
        if not os.path.exists(cfg_path):
            if os.path.exists(example_path):
                issues.append(f"configs/{cfg} 不存在（可从 {cfg.replace('.json', '.example.json')} 复制）")
            else:
                issues.append(f"configs/{cfg} 不存在")

    # db-backend.json
    db_backend = os.path.join(config_dir, "db-backend.json")
    if not os.path.exists(db_backend):
        db_example = os.path.join(config_dir, "db-backend.example.json")
        if os.path.exists(db_example):
            issues.append("configs/db-backend.json 不存在（可从 db-backend.example.json 复制，默认 SQLite）")

    if issues:
        return CheckResult(
            "配置文件", "warn",
            "; ".join(issues),
            "按提示复制 .example 配置文件并填写实际值"
        )
    return CheckResult("配置文件", "ok", ".env 和关键配置文件就绪")


def check_database():
    """7. 数据库连通性检查"""
    # 读取 .env 中的数据库配置
    backend = "sqlite"
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEVOPS_DB_BACKEND="):
                        backend = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass

    # 读取 configs/db-backend.json
    db_cfg_path = os.path.join(_PROJECT_ROOT, "configs", "db-backend.json")
    if os.path.exists(db_cfg_path):
        try:
            with open(db_cfg_path, "r", encoding="utf-8") as f:
                db_cfg = json.load(f)
                backend = db_cfg.get("backend", backend)
        except Exception:
            pass

    if backend == "sqlite":
        db_path = os.path.join(_PROJECT_ROOT, "data", "sessions.db")
        if os.path.exists(db_path):
            return CheckResult("数据库", "ok", f"SQLite ({db_path})")
        return CheckResult("数据库", "ok", "SQLite (首次启动自动创建)")

    if backend == "mysql":
        ok, err = _try_import("pymysql")
        if not ok:
            return CheckResult(
                "数据库", "error",
                "MySQL 后端但 pymysql 未安装",
                "pip install pymysql DBUtils"
            )
        return CheckResult("数据库", "warn", "MySQL 后端，连通性需启动后验证")

    return CheckResult("数据库", "skip", f"未知后端: {backend}")


def check_env_encoding():
    """8. .env 编码安全检查（Issue #452 核心问题）"""
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return CheckResult(".env 编码安全", "skip", ".env 不存在")

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 检查是否有 BOM
        if content.startswith("\ufeff"):
            return CheckResult(
                ".env 编码安全", "warn",
                "文件含 BOM 头",
                "用 UTF-8 无 BOM 重新保存"
            )
        return CheckResult(".env 编码安全", "ok", "UTF-8 编码正常")
    except UnicodeDecodeError:
        return CheckResult(
            ".env 编码安全", "error",
            "文件不是有效 UTF-8（Windows GBK 环境会崩溃）",
            "用文本编辑器以 UTF-8 编码重新保存 .env 文件"
        )


def check_data_dir():
    """9. data 目录权限检查"""
    data_dir = os.path.join(_PROJECT_ROOT, "data")
    try:
        os.makedirs(data_dir, exist_ok=True)
        test_file = os.path.join(data_dir, ".doctor_test")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("test")
        os.remove(test_file)
        return CheckResult("data 目录", "ok", f"可读写 ({data_dir})")
    except PermissionError:
        return CheckResult(
            "data 目录", "error",
            f"无写入权限 ({data_dir})",
            "chmod 755 data 或检查目录属主"
        )
    except Exception as e:
        return CheckResult("data 目录", "error", f"异常: {e}")


def check_auto_detect_url():
    """10. 公网 IP 探测开关检查"""
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    auto_detect = "true"  # 默认

    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GRAPE_AUTO_DETECT_URL="):
                        auto_detect = line.split("=", 1)[1].strip().lower()
                        break
        except Exception:
            pass

    # 也检查环境变量
    auto_detect = os.environ.get("GRAPE_AUTO_DETECT_URL", auto_detect).lower()

    if auto_detect in ("0", "false", "no", "off"):
        return CheckResult("公网 IP 探测", "ok", "已关闭 (GRAPE_AUTO_DETECT_URL=false)，内网启动无等待")
    return CheckResult(
        "公网 IP 探测", "warn",
        "已开启（内网环境可能等待约 40s）",
        "内网环境在 .env 中设置 GRAPE_AUTO_DETECT_URL=false"
    )


def run_all_checks():
    """运行全部检查，返回 (results, exit_code)。"""
    checks = [
        check_python_version(),
        check_core_deps(),
        check_optional_deps(),
        check_agent_cli(),
        check_ports(),
        check_config_files(),
        check_database(),
        check_env_encoding(),
        check_data_dir(),
        check_auto_detect_url(),
    ]

    has_error = any(c.status == "error" for c in checks)
    has_warn = any(c.status == "warn" for c in checks)

    exit_code = 2 if has_error else (1 if has_warn else 0)
    return checks, exit_code


def main():
    print("=" * 60)
    print("  Grape 安装环境自检 (doctor.py)")
    print(f"  平台: {platform.system()} {platform.machine()}")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  项目根: {_PROJECT_ROOT}")
    print("=" * 60)
    print()

    checks, exit_code = run_all_checks()

    for c in checks:
        print(c)
    print()

    # 汇总
    ok_count = sum(1 for c in checks if c.status == "ok")
    warn_count = sum(1 for c in checks if c.status == "warn")
    error_count = sum(1 for c in checks if c.status == "error")
    skip_count = sum(1 for c in checks if c.status == "skip")

    print("-" * 60)
    print(f"  汇总: {ok_count} 通过, {warn_count} 警告, {error_count} 错误, {skip_count} 跳过")

    if exit_code == 0:
        print("  ✅ 环境检查全部通过，可以启动 Grape！")
    elif exit_code == 1:
        print("  ⚠️  有警告项，建议修复后启动（不影响基本运行）。")
    else:
        print("  ❌ 有错误项，必须修复后才能正常启动。")

    print()
    print("  启动命令:")
    print("    python -m src.api.run --dev    # 开发模式")
    print("    python -m src.api.run          # 生产模式")
    print()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
