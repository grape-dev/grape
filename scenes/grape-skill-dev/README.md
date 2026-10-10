# Grape Skill 开发流程

该场景可根据实际业务需求，全新生成或改造现有 Skill，产出满足 Grape 规范的技能。

## 技能链路

1. **VOD** — 需求收集与声音分析（grape-vod-cli）
2. **Issue** — 技能任务规划（grape-skill-issue）
3. **Coding** — 技能代码生成（grape-skill-generator）
4. **QA** — 技能规范验证（grape-skill-validator）
5. **SRE** — 技能打包上传（skill-uploader）

## 推荐模板

hello-skill — 技能开发脚手架模板

## 适用场景

- grape-cli 技能开发与发布
- 仓库级自定义技能生成
- 技能规范合规改造

## 与通用 Skill 开发场景的区别

通用 Skill 开发场景（common-skill-dev）使用通用 skill-creator/skill-tester/skill-publish 组合，
本场景使用 grape-cli 专属的 grape-skill-generator/grape-skill-validator/skill-uploader 组合，
针对 grape-cli 契约驱动的 skill 开发流程做了深度适配。
