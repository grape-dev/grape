# Grape Skills

Grape 官方技能仓库。技能内容按 `agent_type` 分组存放于 `skills/` 目录，供
Grape 系统在构建/部署时拉取使用（方案：构建期 clone / 卷挂载，见 grape 仓库
`docs/new-version2/skills-standalone-repo-build-time-pull.md`）。

## 仓库结构

```
grape-skills/
├── skill-description.json     # 官方技能清单（agent_type/path/description），随本仓发版
├── skills/
│   ├── vod/                   # VOD Agent 技能（开发者声音分析）
│   ├── issue/                 # Issue Agent 技能（Issue 运营）
│   ├── coding/                # Coding Agent 技能（代码实现）
│   ├── qa/                    # QA Agent 技能（审查验证）
│   ├── sre/                   # SRE Agent 技能（构建部署/运维）
│   ├── chat/                  # Chat Agent 技能（对话/IM 通知）
│   └── _template/             # 技能模板（上传/创建的脚手架）
```

## 技能清单

| agent_type | 技能 |
|---|---|
| vod | vod、repo-onboarding、event-dialogue、blog-vod、iac-vod |
| issue | issue、issue-advance、skill-issue、blog-issue、iac-issue |
| coding | implementation、skill-creator、rest-api-skill-creator、iac-coding、wan-video-generation、qwen-image-generation、ai-blog-generation |
| qa | qa、pr、skill-pr、skill-tester、rest-api-skill-tester、iac-qa |
| sre | cloud-deploy、build-debug、deploy-guard、changelog-gen、incident-triage、skill-publish、huawei-cloud、runtime-install、blog-publish、blog-manager、iac-sre、os-disk-list |
| chat | chat、im-notify |

> 完整字段（name/agent_type/path/description）以 `skill-description.json` 为准。

## 技能格式

每个技能是一个独立目录，含 `SKILL.md`（frontmatter 含 `name`/`description`/`version`
等元信息），以及可选的 `references/`、`scripts/`、`templates/`：

```
skills/<agent_type>/<skill>/
├── SKILL.md
├── references/        # 可选的参考资料
├── scripts/           # 可选的辅助脚本
└── templates/         # 可选的模板
```

新增技能时：在对应 `agent_type` 下建目录，并同步更新
`skill-description.json`（path = `<agent_type>/<skill>`）。

## 版本管理

- `main` 为默认分支；
- 版本 tag 语义化（如 `v1.0.0`）；向后不兼容变更升主版本号，Grape 发布物锁定
  `tag` 或 `commit`。

## 运行时目录说明

`skills/custom/`（用户上传技能）与 `skills/repo/`（仓库技能）是 Grape 运行时
动态产生的目录，**不属于本仓库**，勿提交。

## 使用

本仓库不直接运行；由 Grape 系统在构建/部署时拉取并铺入其部署根 `skills/`：

```bash
# 预览
git clone https://gitcode.com/grape-dev/grape-skills.git
# 部署侧（Grape 维护的 sync 脚本）
# git clone --depth 1 --branch <SKILLS_REF> <SKILLS_REPO> <stage>
# cp -a <stage>/skills/<agent> <deploy-root>/skills/<agent>