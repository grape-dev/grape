# Acceptance Criteria — Huawei Cloud Skill Reporter v1

## Skill Report Plugin Readiness

| # | Criterion | Phase | Status |
|---|-----------|-------|--------|
| 1 | SKILL.md 符合华为云 Skill 规范 | P1 | ⬜ |
| 2 | Frontmatter name 匹配目录名，不含 version 字段 | P1 | ⬜ |
| 3 | description 包含触发词（Triggers include） | P1 | ⬜ |
| 4 | 包含所有必需章节：Overview、Prerequisites、Workflow、Core Commands、Parameter Confirmation | P1 | ⬜ |
| 5 | references/iam-policies.md 已生成 | P1 | ⬜ |
| 6 | references/dataflow-diagram.md 已生成 | P1 | ⬜ |
| 7 | references/verification-method.md 已生成 | P1 | ⬜ |
| 8 | references/acceptance-criteria.md 已生成 | P1 | ⬜ |
| 9 | scripts/validate-skill.sh 脚本存在且可执行 | P2 | ⬜ |
| 10 | templates/test-vars.json 存在且格式正确 | P2 | ⬜ |
| 11 | Herms Python 插件可正常导入 | P3 | ⬜ |
| 12 | Opencode JavaScript 插件可正常加载 | P3 | ⬜ |
| 13 | setup.js 可正常运行 | P3 | ⬜ |
| 14 | setup.ps1 可正常运行（Windows） | P3 | ⬜ |
| 15 | setup.sh 可正常运行（Linux/macOS） | P3 | ⬜ |
| 16 | 上报数据格式正确 | P4 | ⬜ |
| 17 | Fire-and-forget 模式不影响 Skill 执行 | P4 | ⬜ |
| 18 | 环境变量优先级正确（ENV > 配置文件 > 默认值） | P4 | ⬜ |
| 19 | 插件注册到正确路径 | P5 | ⬜ |
| 20 | Hermes config.yaml 更新正确 | P5 | ⬜ |
| 21 | 安全扫描通过（无硬编码 AK/SK） | P5 | ⬜ |
| 22 | 文件总数 ≤30 | P5 | ⬜ |
| 23 | SKILL.md 行数 ≤500 | P5 | ⬜ |
| 24 | 所有文件使用允许的扩展名 | P5 | ⬜ |
| 25 | PR 仅修改一个 Skill 目录 | P5 | ⬜ |

## Quality Gates

| Gate | Must Pass Before |
|------|------------------|
| Phase 1 complete | Phase 2 开始 |
| Phase 2 complete | Phase 3 开始 |
| Phase 3 complete | Phase 4 开始 |
| Phase 4 complete | Phase 5 开始 |
| Phase 5 complete | Skill 上线 |
