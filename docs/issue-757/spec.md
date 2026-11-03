# Spec: Issue #757 — bash 工具将 heredoc/python3 -c 内容中的路径字符串误解析为目标路径

> 来源：Issue #757（Agent feedback #610，转交自 grape-skills#1143）| 优先级 P2 | 类型 bug | 开发基线：weekly-08-mys

## Objective

修复 Agent 运行环境工具执行层对 bash 命令的「目标路径」提取逻辑：heredoc 定界内容块
（`cat > file <<EOF ... EOF`）与 `python3 -c` 引号包裹的脚本字符串中的路径字面量不得被
误判为命令目标路径；路径解析仅保留命令自身的文件重定向目标（`> file` / `>> file`）。

## 现状与根因分析（代码级）

- 反馈来源任务（t_05729876d6de，事件 seq 42575，runtime=`codearts-worker-ecs-grape-wo-0001`）
  中，coding agent 使用 heredoc 写文件 / `python3 -c` 传路径字符串时，引擎 bash 工具报
  「从命令中解析目标路径失败」，被迫改用不含完整路径字符串的替代方案绕过。
- grape 仓（服务端 runtime / 工具执行层）此前没有语法感知的命令路径提取实现：任何对
  bash 命令文本做路径提取的消费方（引擎侧 bash 工具权限判定、服务端命令审计/分析）
  只能按裸正则扫描命令全文，heredoc 正文与 `python3 -c` 字符串里的路径字面量必然被
  误判为目标路径（`cat > file <<EOF` 的正文中恰好常含 `xxx/yyy.md` 形态字符串）。
- 结论：需要一个**单一权威实现**（heredoc/python3 -c 语法剥离 + 仅重定向目标提取），
  供 bash 工具路径解析各消费方复用，并以回归测试锁定行为契约。

## Scope

### In Scope
- `src/agent/command_paths.py`（新增）：语法感知的命令文本处理与写入目标提取
  - `mask_heredoc_bodies(command)`：识别 `<<`/`<<-` 定界符（含 `<<'EOF'`/`<<"EOF"`/
    裸定界符），将定界内容块替换为等长空白（保留行结构，便于下游按偏移解析）
  - `mask_python_c_literals(command)`：识别 `python`/`python3`/`python3.x -c` 后引号
    包裹的脚本字面量（单引号/双引号），将字面量内容替换为等长空白
  - `extract_write_targets(command)`：先做上述两种剥离，再仅对命令自身的重定向
    目标（`>`、`>>`、`&>`、`&>>`、`N>`、`N>>`）提取路径 token；引号包裹的目标去引号；
    `>&`/`<&` fd 复制（如 `2>&1`）不产生目标
- `tests/test_command_paths.py`（新增）：验收三场景 + 边界回归用例

### Out of Scope
- 其他工具（write/pptx 等）的路径解析逻辑
- bash 工具其余能力（超时、环境变量、沙箱、网络策略等）
- 引擎二进制（codearts CLI 等）内部实现（外部组件，见 PR 描述中的边界说明）

## 技术方案

1. **heredoc 剥离**：`re` 定位 `<<-?\s*(['"]?)(\w[\w-]*)\1`，自定界符行之后逐行扫描，
   至「定界符独立行」（`<<-` 允许前导 tab）结束；块内非换行字符替换为空格。块内出现
   的任何路径/重定向形态（如 `foo > bar`）随之失效，不再参与目标提取。
2. **python -c 剥离**：`re` 定位 `\bpython(?:3(?:\.\d+)?)?\s+...-c\s+` 后紧跟的单/双
   引号字面量，字面量内容替换为空（保留引号对）。脚本字符串内的 `Path('...')` 路径
   随之失效。
3. **重定向目标提取**：在剥离后的文本上按字符扫描（跟踪单/双引号与反斜杠转义状态），
   命中 `>>`/`>`/`&>>`/`&>`/`(\d+)>>`/`(\d+)>` 时取后续首个非空白 token 为目标路径
   （去引号；`/dev/fd/N`、`2>&1` 类 fd 复制跳过）。无命中返回空列表。
4. **行为契约**：三验收场景 + `<<-EOF`（tab 缩进）、裸 `<<EOF`、heredoc 正文中含
   `>` 与路径、`python -c` 单引号形态、`python3.11 -c` 形态、无重定向纯命令等回归用例。

## Acceptance Criteria
- [ ] `cat > /tmp/test.md <<'EOF'` 内容中含路径字符串（如 `skills/issue/references/issue-runbook.md`）
      时，`extract_write_targets` 仅返回 `["/tmp/test.md"]`，不报/不含内容路径
- [ ] `python3 -c "import pathlib; p = pathlib.Path('skills/issue/references/issue-runbook.md')"`
      返回 `[]`
- [ ] 命令本身重定向目标（`cat > /tmp/out.txt`、`echo x >> /tmp/a`）仍被正确解析为写入路径
- [ ] 所有现有测试通过，新增测试覆盖以上三场景及边界用例

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| heredoc 边界误匹配（定界符非独立行） | Med | 按定界符独立行匹配（`<<-` 允许前导 tab），覆盖引号/裸定界符；未闭合块保守不剥离（宁可多保留原文） |
| 剥离逻辑破坏既有重定向解析 | Med | 仅新增前置剥离层 + 回归测试锁定 `> file`/`>> file` 行为 |
| python -c 无引号脚本/多命令拼接形态 | Low | 仅剥离引号包裹字面量（可判定安全）；拼接命令中的 -c 逐个命中 |

## Open Questions
- 无
