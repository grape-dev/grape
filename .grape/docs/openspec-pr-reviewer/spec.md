# Spec: openspec-pr-reviewer（PR 开源合规审查技能）

> 来源：Issue #311 issue agent 交付的 Spec（评论 comment id=193494736，评级 ready）+ 附件规范文档《开源项目正式开源前通用优化方案（历史清洗 + 安全脱敏 + 提交规范化）》全文（ef25dd8c894541aa98070cc12cee94cc.md）。

## Objective

- 构建 qa 型技能 `openspec-pr-reviewer`：基于《开源项目正式开源前通用优化方案》规范文档对当前仓库 PR 做逐条对照合规审查，在 PR 评论中输出严格、详细、可追溯的审查报告。
- 用户价值：仓库存在 main-opensource 开源分支，开源前需自动拦截敏感信息泄漏、非英文提交、内部信息残留，替代人工逐条审查。
- 用户：开源准备流程中的 qa 环节（PR 审查方）。
- 成功标准：任一 PR 输入后产出含三态结论 + 证据定位的完整报告，误报/漏报可人工复核。

## Scope

### In Scope

- 读取 PR 全量信息：diff（`git-repo pulls files` 的 patch 全文）、变更文件清单、commit message（`git-repo commits` 按 head sha 对照）、新增文件内容（`git-repo contents --ref`）。
- 按规范文档逐章（§0-§7）建立检查项清单，逐项给出 符合/不符合/不适用 三态结论与证据（文件路径+行号 / commit hash）：
  - §0 决策审计吊销、§1 敏感信息清洗（覆盖 §1.1 全部类型：云厂商密钥、GitHub/GitLab PAT、数据库口令、SSH/服务器口令、内网资产 192.168.*/10.*/172.16-31.*、弹性公网 IP、*.intra、内部用户名/账号、私有依赖）、§2 提交记录英文化（英文 + Conventional Commits，保留 (Closes #N)/(Issue #N) 后缀）、§3 开源收尾质量清单、§7 任务计划表。
- 审查报告以 PR 评论发布（`git-repo pulls comments create`）：总体结论（通过/不通过）+ 逐项明细表 + 整改建议清单。

### Out of Scope

- 不修改任何文件、不执行任何 git 写操作、不合并 PR（纯审查）。
- 不做代码逻辑正确性/功能质量审查（仅开源合规性）。

## Technical Approach

- 纯只读审查流：PR 事件触发 → 拉取 PR diff/文件/commits → 按规范逐章规则比对（证据定位）→ PR 评论发布报告。
- 新增技能目录：SKILL.md（qa 型）+ references/（runbook + 规范 §0-§7 展开的检查清单 + cli 子集参考）。
- 关键设计决策：检查项来源=规范全文展开为 references 检查清单（逐条对照可追溯）；存疑项标注「需人工复核」（降低误报）。
- 无自定义环境变量。

## Acceptance Criteria

- [ ] 输入一个 PR，能读取其 diff、变更文件列表与全部 commit message
- [ ] 按规范文档逐章（§0-§7）建立检查项清单，逐项给出三态结论与证据（文件路径+行号/commit hash）
- [ ] 敏感信息检查覆盖附件 §1.1 全部类型（云厂商密钥/PAT/数据库口令/SSH口令/内网网段与域名/内部账号/私有依赖）
- [ ] commit message 检查：英文 + Conventional Commits 格式 + 保留 (Closes #N)/(Issue #N) 后缀
- [ ] 报告以 PR 评论发布，含总体结论、逐项明细表、整改建议清单，可逐条追溯
- [ ] 纯审查：零文件修改、零 git 写操作、零合并
- [ ] 新增测试覆盖检查规则（含 §1.1 样例正反用例）

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 误报/漏报 | Med | 每项结论附证据定位；存疑项标注「需人工复核」而非直接判死 |
| 大 PR（千级变更文件）审查超时 | Med | 分批拉取 diff，聚焦新增/修改内容与 commit message |
