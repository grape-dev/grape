"""主控自身代码版本（Issue #536：cleanup 迁移 min_code_version 门槛用）。

为什么不用 src/worker_package.worker_code_version()：它计算的是 **worker 运行闭包**
的 manifest 指纹，manifest 不含 src/api/ 全部服务端文件——主控代码变了它可能不变，
作为「master 自身版本」不可靠。这里给出主控视角的版本号：

1. git commit（优先）：开发/部署环境均从 git 检出，取提交日期 + 短哈希
   （g-YYYY.MM.DD-<hash>）。**日期前缀保证 min_code_version 门槛可比较**
   （db_setup._version_ge 逐段数值比较），短哈希后缀保留提交粒度唯一性；
2. 回退：src/ 目录关键文件内容指纹（无 git 元数据时，如解包部署），
   形态 fp-<hash>。该形态无比对前缀，_version_ge 仅相等放行（保守）。

格式统一为字符串；日期式（2026.12.01）与 git 形态（g-2026.10.30-abc123）都
带数值点分前缀，_version_ge 可逐段比较；部署脚本 set_min_code_version.py 要求
写入值同样带数值点分前缀（见其形状校验）。
"""
import hashlib
import os
import subprocess

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_cached_version = None
_cached_fingerprint_ts = None

# 参与回退指纹的服务端关键路径（master 代码变更的核心面，够用即可，无需全量）
_FINGERPRINT_PATHS = (
    "src/api/db_setup.py",
    "src/api/main.py",
    "src/config.py",
    "src/session_manager.py",
    "src/mysql_compat.py",
)


def current_code_version() -> str:
    """主控当前代码版本（git commit 优先，回退内容指纹）。带进程级缓存。"""
    global _cached_version
    if _cached_version:
        return _cached_version
    _cached_version = _compute_version()
    return _cached_version


def _compute_version() -> str:
    ver = _git_version()
    if ver:
        return ver
    return "fp-" + _content_fingerprint()[:12]


def _git_version() -> str:
    """git HEAD commit：日期式前缀 + 短哈希（g-2026.10.30-abc123def456）。

    日期前缀与部署脚本写入的 min_code_version（2026.12.01）同形可比——
    修复 QA R2：旧形态（纯 g-短哈希）与日期式 min 恒不相等导致 cleanup 永久跳过。
    无 git / 失败返回空串。
    """
    try:
        out = subprocess.run(
            ["git", "-C", _ROOT, "log", "-1", "--format=%cd-%H",
             "--date=format:%Y.%m.%d"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return _git_version_from_output(out.stdout.strip())
    except Exception:
        pass
    return ""


def _git_version_from_output(line: str) -> str:
    """git log 输出（<YYYY.MM.DD>-<full hash>）→ 版本串 g-<date>-<short hash>。"""
    line = line.strip()
    date_part, sep, full_hash = line.partition("-")
    if not sep or not date_part or not full_hash:
        return ""
    return f"g-{date_part}-{full_hash[:12]}"


def _content_fingerprint() -> str:
    """关键文件内容 sha256（无 git 元数据的部署形态回退）。"""
    h = hashlib.sha256()
    for rel in _FINGERPRINT_PATHS:
        path = os.path.join(_ROOT, rel)
        try:
            with open(path, "rb") as f:
                h.update(rel.encode())
                h.update(f.read())
        except OSError:
            h.update(rel.encode())
    return h.hexdigest()