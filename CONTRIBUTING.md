# Grape 开源贡献指南（CONTRIBUTING）

> Grape —— AI Native DevOps，把任意 Git 项目变成「AI 驱动、人把关」的自主开发工作流。
> 本文件定义协作、分支、发布与质量标准。**先读再提交，5 分钟即可上手。**

快速导航

| 你想做什么 | 看这里 |
|------------|--------|
| 第一个贡献（新手） | [新手路线](#16-新手路线--good-first-issue) |
| 提 Bug / 建议 | [Issue 规范](#3-issue--讨论先行) |
| 开始写代码 | [分支模型](#2-分支模型) → [贡献流程](#4-贡献流程从-issue-到合入） |
| 提交信息怎么写 | [提交信息规范](#5-提交信息规范) |
| 测试怎么跑 | [质量要求](#7-代码质量要求) |
| 赶上周五发布 | [周五发布流程](#9-周五发布与打-tag-规范) |
| 紧急缺陷 | [Hotfix 通道](#10-紧急-hotfix-通道) |
| 不写代码 | [不写代码也能贡献](#174-不写代码也能贡献) |

---

## 1. 工作流总览

```
main（主干）───────────────────────────▶ 始终可发布，只接受发布合并
   │                             ▲              ▲
   │ 每周一拉取                     │ 周五合并回      │ 周五合并回
   ▼                             │（MR + no-ff）│（MR + no-ff）
 weekly-01 ────────────────▶ weekly-02 ───────▶ ...
   │  ▲                         │  ▲
   │  │ MR 合入（特性/修复）         │  │
   ▼  │                         ▼  │
 feat/xxx  fix/xxx  docs/xxx  refactor/xxx   chore/xxx
   （从 weekly 拉取，任务粒度）
   │
   ───────────────── hotfix/xxx（紧急：直挂 main + 当前 weekly）
```

- **每期迭代交付一个 weekly 版本**，测试环境随 weekly 分支滚动部署。
- 特性不直接进 main，先进当期 `weekly-xx`，周五统一合并回到 main 并打 tag。
- 冲突、审查、发布量都收敛到「每周一次」，适合团队稳定节奏 + 开源协作。

## 2. 分支模型

### 2.1 分支一览

| 分支 | 命名 | 生命周期 | 谁能合入 | 用途 |
|------|------|----------|----------|------|
| 主干 | `main` | 永久 | 维护者 | 唯一对外可发布分支，始终稳定；周五从 weekly 合并 |
| 周迭代 | `weekly-01`、`weekly-02`… | 每周 | 维护者 | 当期所有特性的汇合靶点；测试环境部署源 |
| 特性 | `feat/xxx` | 短期（≤1 周） | 贡献者自建 | 新功能开发，从当期 weekly 拉取 |
| 修复 | `fix/xxx` | 短期 | 贡献者自建 | 缺陷修复 |
| 重构 | `refactor/xxx` | 短期 | 贡献者自建 | 不改变行为 |
| 文档 | `docs/xxx` | 短期 | 贡献者自建 | 文档、示例 |
| 工程 | `chore/xxx` | 短期 | 贡献者自建 | 构建、工具链、依赖 |
| 紧急修复 | `hotfix/xxx` | 极短 | 维护者 | 生产/测试关键缺陷，不等周五 |

### 2.2 核心规则

- **特性分支从当期 `weekly-xx` 拉取**，**MR 目标也是 `weekly-x`**（不是 main），周五由维护者统一合并回 main。减少冲突、review 干净、合并回可控。
- **`main` 与 `weekly-xx` 受保护**：不允许直接 push，必须 MR + 审查 + CI 全绿（见 [权限与保护](#122-权限与保护)）。
- **分支宁小勿大**：一个 Issue / 一个职责一条分支，能在一周内合完；每周一写完后不再往里塞大特性。
- 分支名用 `前缀/简短描述`（小写 kebab-case），例：`feat/issue-37-repo-template`。

### 2.3 周迭代时间线（建议，可有浮动调整）

| 时点 | 动作 |
|------|------|
| 每周一 09:30 | 从 `main` 最新 HEAD 拉 `weekly-0X`（X=周序，如 `weekly-01`），并推送占位/空提交或首个合并点 |
| 周一～周四 | 贡献者特性 MR → `weekly-0X`；每次 push 触发 CI + 测试环境自动部署并验证 |
| 周五 FREEZE 点（如 15:00） | 当期 weekly 冻结：只允许已批准修复与 hotfix；不再接纳新特性 |
| 周五 | 合并 `weekly-0X` → `main`（MR）、打 tag（`weekly-0X` + semver）、生成 changelog、发 release notes |
| 下周一 | 从新 main 拉 `weekly-0(X+1)`；`weekly-0X` 分支可归档（tag 永久保留） |

## 3. Issue —— 讨论先行

**先提 Issue，确认方向再写代码**，避免白做。

- **Bug 报告**：写明「运行环境（OS/Python 版本）、复现步骤、期望 vs 实际、相关日志」。
- **功能建议**：描述「使用场景、预期行为、可选的实现思路」。建议尽量落到「哪一个 Skill / Agent / 模块」。
- 每个 Issue 会用到的标签建议：`bug`、`feature`、`good-first-issue`、`help-wanted`、`p3`（低优先级）、`p2`、`p1`（高）等。
- 写代码前，在 Issue 下认领或留言，避免与人撞车。

## 4. 贡献流程（从 Issue 到合入）

```
Issue 确认 → 认领 → 拉当期 weekly → 建特性分支 → 开发+提交
   → 本地测试 → 推远 → 提 MR 到 weekly-0X → 审查(人+QA Agent) → CI 绿 → 合入 → 周五统一合并回
```

1. **在 `weekly-0X` 上建分支**：
   ```bash
   git checkout weekly-01
   git pull origin weekly-01
   git checkout -b feat/issue-37-repo-template
   ```
2. **开发 + 提交**（规范见 [§5]）。
3. **本地质量自检**（命令见 [§7]），全部通过再提交。
4. **推送并提 MR**，目标分支选 **当期 `weekly-0X`**，用 [§6] 模板写清楚：
   - 改动是什么、为什么、怎么测的、关联 Issue 号。
5. **等审查**：
   - 项目负责人（`CODEOWNERS` 中的 owner，当前为 `@Jason7602`）或维护者 approve；
   - Grape 自身的 **QA Agent 会辅助审查**（自动跑测试、静态检查）——它是辅助，无法替代人工把关；
   - 有修改意见 → 在**同一分支**继续提交；**合入后不要再改原 MR**。
6. **CI 全绿 + 审查通过** → 由维护者 squash/merge 进 `weekly-0X`。
7. 合入后测试环境自动部署该 weekly 分支，**贡献者建议在测试环境自测一轮**（验证清单见 [§8]）。

> 协作者与外部贡献者最大区别只是「建分支 vs fork」：外部贡献者 fork 后，MR 从 `你的用户名/fork分支` → `weekly-0X` 即可，流程一致。

## 5. 提交信息规范

**提交信息统一使用英文**，采用 **Conventional Commits** + 关联 Issue：

```
<type>(<scope>): <subject>

<body 解释做了什么 / 为什么>
```

- type：`feat`、`fix`、`refactor`、`docs`、`chore`、`style`、`test`、`perf`、`security`。
- scope 可选（模块/后台），subject 全部小写、简洁、动词开头。
- **关联 Issue**（推荐）：`fix(issue-37): fix template auth`。
- 一条提交只做一件事；不做「顺手改别处」。

```bash
# 用法举例
git commit -s -m "feat(skill): add xxx skill description

Closes #37"
```

- **签署者（DCO）**：每个提交尾部带 `Signed-off-by`：
  ```
  Signed-off-by: Your Name <your.email@example.com>
  ```
  （`git commit -s` 自动加，含义见 [§17]）。

正面 / 反面示例：

``` 
# 好
feat(skill): add xxx skill description
fix(issue-37): fix template auth bug
docs: add bilingual README
```
```bash
# 不好
# "update stuff"、大提交混着 UI+DB+文档、没有 issue 引用、没有说明
# 中文 commit message
```

## 6. MR 描述模板

```markdown
## 关联 Issue
Closes #37

## 改动内容
- 简述……

## 测试情况
- [ ] 本地 pytest 全部通过（命令见 §7）
- [ ] 覆盖了新增/改动代码
- [ ] 测试环境验证过（weekly 分支部署后自测）

## 影响范围 / 风险
- 涉及的 Skill / Agent / 配置……
```

## 7. 代码质量要求

| 项 | 要求 |
|----|------|
| 单测 | 新增/改动必须有测试 | 
| 测试命令 | `python -m pytest tests/ -v`（默认跳过 e2e_ui、保证确定性；含 `e2e_ui` 场景用 `python -m pytest tests/ -m e2e_ui`） |
| 覆盖率 | `python -m pytest tests/ --cov`；当前门槛**行覆盖率 ≥ 70%**（`.coveragerc` `fail_under=70`，稳定后再上调） |
| 代码风格 | 与相邻代码一致；不引入未用依赖；不提交敏感信息（token/凭据一律走 `.env`/配置文件，已 gitignore） |
| 安全性 | 不弱化鉴权、不新增外部输入直接拼接 SQL/命令（留意 .env.example 与 configs 权限） |

CI 会在每次 MR push 自动执行：lint + 单测 + 覆盖率门槛 + 构建。

## 8. 测试环境（基于 weekly 分支）

- 每次 `weekly-0X` 有 push，自动构建并部署到测试环境。
- 测试环境只跟当周 `weekly-0X`，**不跟特性分支**，保证测的是「将要发布的集合」。
- 验证清单（回归）：
  - [ ] 多 Agent 流水线跑通（VOD→Issue→Coding→QA→SRE）
  - [ ] 三大入口（GitCode Webhook / 飞书 / WebUI）各自关键路径
  - [ ] 本版本新增特性的端到端场景
  - [ ] 数据迁移/配置变更影响面（如有）

## 9. 周五发布与打 tag 规范

周五是当期 weekly 的「发布窗口」，由维护者/发布值班执行：

1. **FREEZE**：`weekly-0X` 冻结，不再合入新特性（仅已批准修复与 hotfix）。
2. **合并回主干**：把 `weekly-0X` 合并回 `main`。推荐**保留合并提交（`--no-ff`）**，并注明 `weekly-0X → main` 与 MR 号，发布历史可追溯；main 合并后 CI 复跑一次（回归门禁）。
3. **打 tag**（保证 tag 与合并回到 main 的内容一致）：
   ```bash
   git checkout weekly-01
   git tag weekly-01                  # 周版本号，内容与 main 合并结果一致
   git tag -a v0.4.0 -m "weekly-01 发布"   # 建议同步打语义化版本，便于对外引用
   git push origin weekly-01 --tags
   ```
   - `weekly-0X` 是内部发布号；**对外建议再用 SemVer `vX.Y.Z` 指向同一 commit**（生产环境、第三方引用都以 SemVer 为准）。
4. **发布产物**：用 Grape `changelog-gen` Skill（或手写）生成 changelog；写 release notes；关联本期收编的 Issue。
5. **归档**：将当周 weekly 分支归档（`git branch -d weekly-01`），tag 永久保留；**不要 delete tag**。

## 10. 紧急 Hotfix 通道

关键缺陷（测试受阻 / 生产风险）**不必等周五**：

```
main ──────────────────── ... 
   └             └
   └ hotfix/xxx  （从 main 拉取）
   └  → 修复 → MR 直挂 main（+ 当期 weekly-0X 同步，保证测试环境对齐）
   └  → CI 绿 → 立即合入 → 打补丁 tag（如 v0.4.1）
```

1. 从 `main` 拉 `hotfix/xxx`，修好提 MR **目标 `main`**。
2. **同时 cherry-pick 到当期 `weekly-0X`**（或再开一个小 MR），让测试环境也拿到修复。
3. 合 main 后立即补丁版本 tag（`v0.4.1`），不拖到周五。

## 11. 权限、保护与自动化

### 11.1 默认职责清单

| 负责人 | 职责 |
|--------|------|
| 维护者（owner，见 CODEOWNERS） | 审核/合并 MR、weekly 合并回、打 tag、发布、分支保护管理 |
| 贡献者 | 特性/修复开发，测试环境自测 |
| QA Agent（Grape） | 每个 MR 自动辅助审查：测试、静态检查、安全扫描 |

### 11.2 权限与保护

- `main`、`weekly-0X`：**禁止直推**；要求审查（≥1 人 approve、CODEOWNERS 对应模块 owner 可收尾）、CI 全绿后由维护者合并。
- `feat/*` `fix/*` 等：贡献者自建自治，合入必须走 MR。
- 平台级禁用强推（`force-push`）与分支删除 `main`/`weekly`。

### 11.3 建议的自动化

- CI：MR push 触发 lint + 单测 + 覆盖率 + 构建；`weekly-0X` push 额外触发测试环境部署。
- 依赖更新机器人（如需）：禁用自动合并，人工 review DCO/语义后再合，避免周中突增失败。
- 打 tag / release：可用 CI 在合并回 main 后自动打 tag、生成 changelog（GitCode CI / GitLab-CI 均可）。

## 12. 行为准则

我们采纳 [Contributor Covenant 2.1](http://www.contributor-covenant.org/version/2/1/code_of_conduct/)（见仓库 `CODE_OF_CONDUCT.md`）。要点：

- 友善、尊重、就事论事；禁止人身攻击、骚扰、歧视。
- 分歧用公开讨论 + 维护者裁定解决；合入争议由维护者拍板。
- 遇违反请私下联系维护者，避免公开对线。

## 13. 安全漏洞披露

**安全漏洞不要提公开 Issue**（会让所有人一起处于风险）：

- 通过 GitCode **站内私信**联系维护者（`CODEOWNERS` 中的 owner），或按 `SECURITY.md` 指引提交。
- 说明：影响面、触发条件、复现、建议修复；**不公开 PoC**。
- 维护者会在不泄露信息的前提下评估、修复（随下一个 hotfix / weekly 发布），并在版本说明中致谢。

## 14. 社区支持与沟通

- 主仓库：gitcode.com/grape-dev/grape ；Issue/讨论见仓库 Issue 区。
- 相关仓库：官方技能 `grape-dev/grape-skills`、项目模板 `grape-dev/grape-template`。
- 飞书群：接入 Grape 的组织在绑定群内直接反馈。

## 15. FAQ / 常见问题

- **我的 MR 为什么没进 main？** —— 目标分支应为当期 `weekly-0X`，周五统一合并回。
- **周五后新写的东西怎么办？** —— 下周一从新 main 拉 `weekly-02` 继续，不影响本周发布。
- **改了代码但 Issue 没认领？** —— 补在 MR 描述里，审查时统一定。
- **觉得被卡住了？** —— 在 Issue/MR 留言 @维护者，别单打独斗。

## 16. 新手路线（good first issue）

1. 在 Issue 区找带 `good-first-issue` 标签的任务（多为：文档、测试补齐、小工具、边界用例）。
2. 不摸代码也能贡献：见 [§17.4]。
3. 从「小」开始：一个 docs/测试/工具类 MR 就足够建立协作手感。

## 17. 贡献者须知

### 17.1 签署者（DCO）

采用 **Developer Certificate of Origin（DCO）** 模式：每个提交 `Signed-off-by: <姓名> <邮箱>`（`git commit -s`），即表示你确认「这是你原创或你有权贡献、可按项目 License（MIT）分发」。维护者会校验 MR 中每个提交都有签署。

### 17.2 版权与许可

代码遵循本仓库 `LICENSE`（MIT）。贡献即同意按此许可分发。

### 17.3 一致性建议

- 新增文档建议中英双语或至少中文；术语与 README 一致（Skill / Agent / Pipeline 等）。
- 仓库设计文档见 `docs/`（设计文档命名 `xxx-design.md`），改动大模块前先读对应设计文档。

### 17.4 不写代码也能贡献

- **文档**：改进 README、补充 / 修正设计文档与注释。
- **Skills**：向 `grape-dev/grape-skills` 贡献新 Skill（`skills/<agent_type>/<skill>/`，Markdown 即可，无需改后端）—— 这是本项目的「低门槛高价值」方向。
- **项目模板**：为 `configs/project-templates.json` 补一键可创建的模板。
- **Bug 报告**：清晰复现 + 日志（见 §3）。
- **测试用例**：补覆盖边界场景的用例（提升覆盖率即是直接贡献）。
- **社区**：回答问题、写教程、维护非代码资产。

---

有问题：开 Issue 或在飞书群直接问。期待你的贡献，欢迎成为 Grape 贡献者。
