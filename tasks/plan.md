# Plan — Issue #238: Hermes ACP 临时目录权限修复

## 任务类型：bug 修复（简单 — 单文件 <10 行变更 ×2）

## 任务拆解

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1 | hermes_acp.py: 临时目录根路径 /tmp → ~/.cache | src/agent/hermes_acp.py ~89 | ✅ |
| 2 | executor.py: mkdtemp 指定 dir=~/.cache/hermes-iso | src/agent/executor.py ~459 | ✅ |
| 3 | py_compile 语法校验 | 两文件 | ✅ |
| 4 | 路径创建逻辑功能验证 | python3 -c 模拟 | ✅ |
| 5 | 自审查（五轴） | — | ⬜ |
| 6 | 建 PR + body/label | — | ⬜ |
| 7 | Issue 评论告知 | — | ⬜ |
| 8 | A2A 通知 QA | — | ⬜ |

## 依赖

无外部依赖，仅用 Python 标准库（os.path/tempfile/shutil），已在文件内 import。

## 风险

- **低**：`~/.cache` 在极端环境下可能不可写（无家目录的用户），但 grape/gtd 均有正常家目录，风险可忽略。
- **低**：已有 `/tmp/hermes-acp-homes` 残留目录不影响新逻辑（新路径独立）。
