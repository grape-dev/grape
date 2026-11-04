# Plan: Issue #757 — 命令路径提取（heredoc / python3 -c 语法剥离）

> 对应 spec.md | 开发基线：weekly-08-mys | 分支：feat/issue-757

## 方案

新增 `src/agent/command_paths.py` 作为命令文本「目标路径」提取的单一权威实现：

```
mask_heredoc_bodies()      # <<[-]DELIM ... DELIM 内容块 → 等长空白（保留换行）
mask_python_c_literals()   # python[3[.x]] -c '<script>'/"<script>" 字面量 → 等长空白
extract_write_targets()    # 剥离后仅提取 > / >> / &> / &>> / N> / N>> 的目标 token
```

三层纯函数、无副作用、无外部依赖，消费方（引擎侧 bash 工具路径判定 / 服务端命令
审计）按需组合调用。

## 实现步骤

1. `mask_heredoc_bodies`：正则定位定界符（`<<-?\s*(['"]?)(\w[\w-]*)\1`）→ 逐行找
   定界符独立行 → 块内非换行字符替换为空格；未闭合不剥离。
2. `mask_python_c_literals`：定位 `python... -c` 后的引号字面量 → 内容替换为等长
   空白（保留引号）。
3. `extract_write_targets`：引号/转义状态机扫描，命中重定向操作符后取目标 token，
   去引号返回列表；fd 复制（`2>&1`、`>&2`）跳过。
4. 测试先行（RED→GREEN）：验收三场景 + 边界形态全部入 `tests/test_command_paths.py`。

## 测试策略

- 纯函数单测（无 DB / 无网络）：`pytest tests/test_command_paths.py`
- 回归：`pytest tests/ -k command_paths` 及相邻模块冒烟

## 风险与边界

- 引擎（codearts CLI）内部的 bash 工具实现为外部组件，本仓提供权威提取实现与行为
  契约测试；引擎侧接入以该模块为参照（PR 描述中说明边界）。
- 仅剥离可静态判定的形态（引号字面量、闭合 heredoc），宁保守不误删。
