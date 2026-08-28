# DevOps Grape Skills

本目录是 DevOps Grape 的**官方技能库**，技能按 `agent_type` 一层分组存放：

```
skills/
├── vod/            # VOD Agent 技能（开发者声音分析）
├── issue/          # Issue Agent 技能（Issue 运营）
├── coding/         # Coding Agent 技能（代码实现）
├── qa/             # QA Agent 技能（审查验证）
├── sre/            # SRE Agent 技能（构建部署/运维）
├── chat/           # Chat Agent 技能（对话/IM 通知）
├── custom/         # 运行时：用户上传技能（不入 git）
├── repo/           # 运行时：仓库技能（不入 git）
└── _template/      # 技能模板
```

> **重要**：官方技能内容（`vod/ issue/ coding/ qa/ sre/ chat/ _template`）已拆分至
> **独立技能仓库** `https://gitcode.com/grape-dev/grape-skills.git`，由构建/部署脚本
> `scripts/sync_skills.sh` 拉取铺入本目录（方案见
> `docs/new-version2/skills-standalone-repo-build-time-pull.md`）。本仓库不再直接维护
> 官方技能文件。

## 技能清单

37 个官方技能的分类与字段以 `configs/skill-description.json` 为准
（构建期由 `sync_skills.sh` 从技能仓库同步铺入）。

| agent_type | 技能 |
|---|---|
| vod | vod、repo-onboarding、event-dialogue、blog-vod、iac-vod |
| issue | issue、issue-advance、skill-issue、blog-issue、iac-issue |
| coding | implementation、skill-creator、rest-api-skill-creator、iac-coding、wan-video-generation、qwen-image-generation、ai-blog-generation |
| qa | qa、pr、skill-pr、skill-tester、rest-api-skill-tester、iac-qa |
| sre | cloud-deploy、build-debug、deploy-guard、changelog-gen、incident-triage、skill-publish、huawei-cloud、runtime-install、blog-publish、blog-manager、iac-sre、os-disk-list |
| chat | chat、im-notify |

## 目录结构（单个技能）

```
skills/<agent_type>/<skill>/
├── SKILL.md            # 技能指令（frontmatter + 正文）
├── references/         # 参考文档（API 参考、审查规范等）
├── scripts/            # 辅助脚本（可选）
└── templates/          # 模板（可选）
```

## 技能格式

见 [SKILL-SPEC.md](./SKILL-SPEC.md)。

## 如何新增 / 修改官方技能

技能内容的增删改在独立技能仓库 `grape-skills` 中进行：

1. 在 `grape-skills` 的 `skills/<agent_type>/` 下建/改技能目录；
2. 同步更新 `grape-skills/skill-description.json`（path = `<agent_type>/<skill>`）；
3. 提交 PR 合并后打 tag；grape 侧部署时执行 `bash scripts/sync_skills.sh` 更新。

## 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `DEVOPS_SKILLS_DIR` | Skills 目录的绝对路径 | `/app/skills` |
| `DEVOPS_EVENT_TYPE` | 当前事件类型 | `issue_opened` |