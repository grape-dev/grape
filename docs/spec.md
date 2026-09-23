# Spec — Issue #238: Hermes ACP 临时目录权限修复

## 1. 需求概述

将 hermes ACP / CLI 模式的临时工作目录从系统公共 `/tmp/` 迁移到当前用户家目录下 `~/.cache/`，解决多用户环境下 `/tmp` 权限冲突导致后启动者 hermes ACP 启动失败（Permission denied）的问题。

## 2. 背景与根因

同一台机器上多个用户各自运行 grape worker + hermes 引擎时，先启动的用户以自身属主创建 `/tmp/hermes-acp-homes/`（默认权限 700），后启动用户无法在其中创建子目录，导致 `[Errno 13] Permission denied`，任务在 1-3 秒内失败、0 条 task_messages。

涉及两处代码：
- `src/agent/hermes_acp.py` ~89行：ACP 模式 HERMES_HOME overlay 根路径
- `src/agent/executor.py` ~459行：CLI 模式 hermes 隔离目录

## 3. 技术方案

利用家目录天然用户隔离，将临时目录根路径从 `/tmp/` 改为 `~/.cache/`：
- **hermes_acp.py**：`tempfile.gettempdir()/hermes-acp-homes` → `~/.cache/hermes-acp-homes`
- **executor.py**：`tempfile.mkdtemp(prefix="hermes-iso-")` → 先建 `~/.cache/hermes-iso` 再 `mkdtemp(dir=)`

仅用 Python 标准库（`os.path`/`tempfile`/`shutil`），已在文件内 import。清理逻辑（executor.py ~745 `shutil.rmtree(_iso_home)`）无需改动。

详细方案见 `docs/weekly-04/hermes-acp-homedir-fix.md`。

## 4. 验收标准

1. 多用户机器上 grape/gtd 分别启动 worker，后启动者不再报 Permission denied
2. `~/.cache/hermes-acp-homes/` 正确创建
3. hermes 任务正常执行（有 task_messages 产出）
4. 两用户 overlay 目录互不干扰
5. 清理逻辑正常（executor.py ~745 shutil.rmtree 无需改动）
