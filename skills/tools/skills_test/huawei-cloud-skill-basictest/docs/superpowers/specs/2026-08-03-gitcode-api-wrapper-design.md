# 设计文档：GitCode 交互统一封装

- 日期：2026-08-03
- 状态：已确认

## 目标

所有与 GitCode 的交互统一封装到 `scripts/gitcode_api.sh`，智能体指令与 basictest 脚本全部接入，消除散落的直接 API 调用（curl/POST/PATCH）。

## 封装脚本 `scripts/gitcode_api.sh`

| 子命令 | 对应接口 | 说明 |
|--------|---------|------|
| `issue create <owner> <repo> --title --body` | `POST /repos/{o}/{r}/issues` | 创建 issue，输出 number/html_url |
| `issue get <owner> <repo> <number>` | `GET /repos/{o}/{r}/issues/{n}` | 查 issue，输出 JSON（含 labels） |
| `issue labels-set <owner> <repo> <number> <label...>` | `PUT /repos/{o}/{r}/issues/{n}/labels` + 裸数组 | 替换标签，失败退出码非 0 + 明确错误 |
| `pr comment <owner> <repo> <number> --body` | `POST /repos/{o}/{r}/pulls/{n}/comments` | PR 评论（上传总报告） |
| `pr tester-assign <owner> <repo> <number> <username>` | `POST /repos/{o}/{r}/pulls/{n}/testers` | **指派测试人**（正确接口） |
| `pr assignee-assign <owner> <repo> <number> <username>` | `POST /repos/{o}/{r}/pulls/{n}/assignees` | 指派审查人 |
| `pr get <owner> <repo> <number>` | `GET /repos/{o}/{r}/pulls/{n}` | 查 PR 信息 |

## 关键设计

- **Token 统一读取**：脚本内读 `GITCODE_TOKEN` env，缺失读 `~/.gitcode_token`（0600）；调用方不传 token
- **失败处理**：接口不生效（HTTP 200 但无变化 / 4xx / 5xx）时退出码非 0 + 输出明确错误信息；调用方据此决定是否降级，不静默不循环
- **正确接口**：标签用 `PUT /issues/{n}/labels` + 裸数组 body；测试人用 `/testers`（此前误用 `/assignees`）

## 接入改造

1. **智能体指令**（agent 9baece55）：
   - 第三节创建 BUG issue → `gitcode_api.sh issue create`
   - 第三节加 BUG 标签 → `gitcode_api.sh issue labels-set`（尝试一次不重试）
   - 第五节 PR 评论 → `gitcode_api.sh pr comment`
   - 第五节指派测试人 → `gitcode_api.sh pr tester-assign <王杰登录名>`（原 /assignees 改 /testers）
2. **basictest fetch_issues.py**：调用 `gitcode_api.sh issue list/get` 获取 issue 数据
3. **SKILL.md**：注明所有 GitCode 交互通过统一封装脚本

## 边界

- 脚本不包含业务逻辑，只做 API 封装与错误传递
- 接口不可用时返回非 0 + 错误信息，由调用方决策（记录/降级），不自动重试
