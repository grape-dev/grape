# DevOps Bot Skills 库

本目录是 DevOps Bot 的**技能库**，每个子目录对应一个 Skill，包含 `SKILL.md`（技能指令）和 `references/`（参考文档）。

Bot 通过事件路由（`configs/event-routing.json`）将事件分发到对应 Skill，Bot 读取 `SKILL.md` 拼入 Prompt 后交给 Agent 执行。

## 技能列表

| Skill | 说明 | 触发事件 | 后端 |
|-------|------|---------|------|
| `issue` | Issue 分诊、标签、CODEOWNERS 分配 | `issue_opened`、`issue_comment` | opencode |
| `pr` | PR 审查（代码审查、IaC 扫描、合并检查） | `pull_request_opened`、`pull_request_updated`、`note_hook_pr` | opencode |
| `skill-pr` | Skill 仓库专用 PR 审查（同 pr 技能，用于 huaweicloud-skills） | `pull_request_opened`、`pull_request_updated` | opencode |
| `implementation` | 自主开发实现（spec → plan → TDD → 增量代码 → PR） | `issue_labeled` | opencode |
| `build-debug` | 构建失败诊断与修复建议 | `build_failure` | opencode |
| `deploy-guard` | 部署安全守卫与合规检查 | `deploy_start`、`deploy_failure` | opencode |
| `incident-triage` | 事件分诊与定级 | 手动触发 | opencode |
| `changelog-gen` | 变更日志生成与发布说明 | `release_created` | opencode |
| `cloud-deploy` | 云部署自动化 | `pull_request_merged` | opencode |
| `im-notify` | 飞书通知 — 接收 GitCode 事件，按事件类型决策发送通知 | 所有非 IM 事件自动前置路由 | opencode |
| `chat` | 飞书 IM 对话、意见反馈、实施审批 | 飞书消息 | opencode |
| `agent` | 5 个 agent-skills 子技能（spec-driven-dev / planning / TDD / incremental-impl / code-review） | 被其他技能引用 | opencode |

## 路由机制

```
事件 → event_processor.py
  → event-routing.json（事件类型 → Skill 映射）
    → 支持 repo_routing（按仓库名覆盖技能）
      → 读取 skills/{skill}/SKILL.md → 拼入 Prompt → Agent 执行
```

## 仓库特定路由

`configs/event-routing.json` 中的 `repo_routing` 支持按仓库名指定不同技能：

```json
"repo_routing": {
  "Jason7602/huaweicloud-skills": {
    "pull_request_opened": "skill-pr",
    "pull_request_updated": "skill-pr"
  }
}
```

未配置的仓库走全局 `routing` 映射。

## 目录结构

```
skills/
├── README.md                    # 本文件
├── SKILL-SPEC.md                # Skill 规范定义
├── agent/                       # 子技能集
├── build-debug/                 # 构建诊断
├── changelog-gen/               # 变更日志
├── chat/                        # 飞书对话
├── cloud-deploy/                # 云部署
├── deploy-guard/                # 部署守卫
├── im-notify/                   # 飞书通知
├── implementation/              # 自主开发
├── incident-triage/             # 事件分诊
├── issue/                       # Issue 运营
├── pr/                          # PR 审查
└── skill-pr/                    # Skill 仓库 PR 审查
```

每个技能目录包含：
- `SKILL.md` — 技能指令
- `references/` — 参考文档（API 参考、审查规范等）

## 如何创建新 Skill

1. 在 `skills/` 下创建目录，如 `skills/my-skill/`
2. 创建 `SKILL.md`，按 [SKILL-SPEC.md](./SKILL-SPEC.md) 规范编写
3. 在 `configs/event-routing.json` 中添加事件类型 → Skill 映射
4. （可选）如果指定仓库使用，在 `repo_routing` 中添加

## 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `DEVOPS_SKILLS_DIR` | Skills 目录的绝对路径 | `/app/skills` |
| `DEVOPS_EVENT_TYPE` | 当前事件类型 | `issue_opened` |
