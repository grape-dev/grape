"""本地文件操作封装。

统一处理：
- 原子写入（.tmp → rename，防写入中途崩溃截断）
- 路径安全校验（防穿越 WORKSPACE_DIR）
- 目录自动创建
- 编码统一 utf-8

使用方式：
    from src.localfs import write_text, read_text, safe_join

    write_text(path, content)          # 原子写入
    content = read_text(path)          # 读取（文件不存在返回 None）
    path = safe_join(repo_key, rel)    # 安全拼接路径 + 校验
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

from src.config import WORKSPACE_DIR


def write_text(path: str, content: str, encoding: str = "utf-8") -> None:
    """同目录临时文件 + os.replace 原子替换目标文件。

    避免进程崩溃时目标文件处于半写/截断状态。配合 fsync(tmp) 提高掉电
    持久性；防掉电还需 fsync 目录（下方已补）。多写者需额外加锁。
    """
    _dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(_dir, exist_ok=True)
    _fd, _tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp", prefix=".atomic_")
    try:
        with os.fdopen(_fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, path)
        # fsync 目录：确保 rename 元数据落盘，防掉电后文件名存在但 inode 丢失
        _dir_fd = os.open(_dir, os.O_RDONLY)
        try:
            os.fsync(_dir_fd)
        except OSError:
            pass  # 某些文件系统（如 tmpfs）不支持目录 fsync
        finally:
            os.close(_dir_fd)
    except Exception:
        try:
            os.unlink(_tmp)
        except FileNotFoundError:
            pass
        raise


def write_bytes(path: str, data: bytes) -> None:
    """同目录临时文件 + os.replace 原子替换目标文件。

    避免进程崩溃时目标文件处于半写/截断状态。配合 fsync 提高掉电持久性。
    多写者需额外加锁。
    """
    _dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(_dir, exist_ok=True)
    _fd, _tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp", prefix=".atomic_")
    try:
        with os.fdopen(_fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, path)
        _dir_fd = os.open(_dir, os.O_RDONLY)
        try:
            os.fsync(_dir_fd)
        except OSError:
            pass
        finally:
            os.close(_dir_fd)
    except Exception:
        try:
            os.unlink(_tmp)
        except FileNotFoundError:
            pass
        raise


def read_text(path: str, encoding: str = "utf-8") -> str | None:
    """读取文本文件。文件不存在返回 None。"""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding=encoding) as f:
        return f.read()


def read_bytes(path: str) -> bytes | None:
    """读取二进制文件。文件不存在返回 None。"""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def append_text(path: str, content: str, encoding: str = "utf-8") -> None:
    """追加写入文本（非原子，用于日志/知识追加等场景）。"""
    _dir = os.path.dirname(path)
    os.makedirs(_dir, exist_ok=True)
    with open(path, "a", encoding=encoding) as f:
        f.write(content)


def safe_join(repo_key: str, *rel_parts: str) -> str:
    """安全拼接 WORKSPACE_DIR 下的路径，防止路径穿越。

    返回绝对路径，校验结果必须在 WORKSPACE_DIR 内。
    """
    from src.repo_key import workspace_repo_dir
    base = workspace_repo_dir(repo_key)
    full = os.path.normpath(os.path.join(base, *rel_parts))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError(f"path traversal detected: {full} outside {base}")
    return full


def safe_workspace_path(file_path: str) -> str:
    """将 WORKSPACE_DIR 相对路径转为绝对路径，校验不穿越。"""
    full = os.path.normpath(os.path.join(WORKSPACE_DIR, file_path))
    if full != WORKSPACE_DIR and not full.startswith(WORKSPACE_DIR + os.sep):
        raise ValueError(f"path traversal detected: {full} outside {WORKSPACE_DIR}")
    return full
