# configs/skill-description.json description 同步修改方案

## 背景

configs/skill-description.json 里的 skill description 和 SKILL.md frontmatter description 不一致。
需要同步三个 skill：skill-creator / skill-tester / skill-publish。

## 改动文件

只改 configs/skill-description.json（3 处 description）

---

## 1. skill-creator

### 当前（configs）
```json
"description": "按需求自动创建华为云 skill（借鉴 skillsopr 六阶段）：查重 → 需求澄清（确认门控）→ CLI/SDK/OpenAPI 三级调研 → 骨架生成 → 提交用户代码仓 → A2A 交 skill-tester 测试"
```

### 改为
```json
"description": "按需求自动创建华为云 skill：查重 → 读取 Spec → CLI/SDK/OpenAPI 三级调研 → 骨架生成 → 自测 → 创建 PR → A2A 交棒 QA Agent 测试"
```

### 逐项说明

| # | 当前 | 改为 | 原因 |
|---|------|------|------|
| 1 | 按需求自动创建华为云 skill（借鉴 skillsopr 六阶段） | 按需求自动创建华为云 skill | 去掉"借鉴 skillsopr 六阶段"——不需要引用其他框架 |
| 2 | 需求澄清（确认门控） | 读取 Spec | Step 1 已改为读取 Spec——不做需求澄清/确认门控 |
| 3 | 提交用户代码仓 | 自测 → 创建 PR | 和 SKILL.md 流程一致——先自测再创建 PR |
| 4 | A2A 交 skill-tester 测试 | A2A 交棒 QA Agent 测试 | 用角色名 QA Agent 不用 skill 名 skill-tester |

### 和 SKILL.md frontmatter 对照
SKILL.md: "按需求自动创建华为云 skill：查重 → 读取 Spec（从 issue 产出的 Spec 获取确定的方案）→ 三级技术调研（CLI→SDK→OpenAPI）→ 骨架生成 → 自测 → 创建 PR → 清理 → A2A 交棒 QA Agent 测试。生成物进用户代码仓（用户资产——版本管理）——不进系统技能池。"

configs 是简化版（去掉括号说明和"清理"步骤），核心流程一致。

---

## 2. skill-tester

### 当前（configs）
```json
"description": "skill 功能验证（创建后 QA 门控）：跑 skill 验证功能 + 规范脚本化检查（frontmatter/结构）+ gitleaks 密钥扫描——报告通过/失败（qa 技能本身不动）"
```

### 改为
```json
"description": "skill 功能验证（创建后 QA 门控）：跑 skill 验证功能正确性 + 规范脚本化检查（frontmatter/文件结构）+ gitleaks 密钥扫描（安全）——输出 Issue 报告 + PR 评论（✅ 通过 / 🔴 失败+原因）。测试通过后调合并脚本 + A2A 交棒 SRE Agent"
```

### 逐项说明

| # | 当前 | 改为 | 原因 |
|---|------|------|------|
| 1 | 跑 skill 验证功能 | 跑 skill 验证功能正确性 | 补充"正确性"——和 SKILL.md 一致 |
| 2 | frontmatter/结构 | frontmatter/文件结构 | 更明确——和 SKILL.md 一致 |
| 3 | gitleaks 密钥扫描 | gitleaks 密钥扫描（安全） | 补充"（安全）"——和 SKILL.md 一致 |
| 4 | 报告通过/失败（qa 技能本身不动） | 输出 Issue 报告 + PR 评论（✅ 通过 / 🔴 失败+原因）。测试通过后调合并脚本 + A2A 交棒 SRE Agent | 补充完整流程——"qa 技能本身不动"描述不对，改为完整输出 + 合并 + A2A 交棒 |
| 5 | （无） | SRE Agent | 用角色名 SRE Agent（大写）不用小写 sre |

### 和 SKILL.md frontmatter 对照
SKILL.md: "skill 功能验证（创建后 QA 门控——qa agent 执行）：跑 skill 验证功能正确性 + 规范脚本化检查（frontmatter/文件结构）+ gitleaks 密钥扫描（安全）——输出 Issue 报告 + PR 评论（✅ 通过 / 🔴 失败+原因）。测试通过后调合并脚本 + A2A 交棒 sre。"

configs 去掉了"——qa agent 执行"（角色名在后面 A2A 交棒里体现），核心流程一致。

注意：SKILL.md frontmatter 里的"qa agent 执行"和"sre"也是小写——后续也需要同步改为大写角色名（但不在本次 configs 改动范围）。

---

## 3. skill-publish

### 当前（configs）
```json
"description": "将 grape 生成的 skill 发布/下架到外部平台（ClawHub/GitHub/GitCode）。Triggers: 同步 skill, 发布 skill, 下架 skill, ClawHub, GitHub, GitCode, 撤回发布"
```

### 改为
```json
"description": "将 skill 发布/下架到外部平台（ClawHub/GitHub/GitCode）：幂等探测 → bump 重试（≤3 次）→ 多平台发布/下架 → 校验 → 汇总回复。ClawHub 支持组织发布（ownerHandle，可选）；403/404/401 严格区分，防止误判"
```

### 逐项说明

| # | 当前 | 改为 | 原因 |
|---|------|------|------|
| 1 | 将 grape 生成的 skill 发布/下架 | 将 skill 发布/下架 | 去掉"grape 生成的"——和 SKILL.md 一致 |
| 2 | Triggers: 同步 skill, 发布 skill... | 幂等探测 → bump 重试（≤3 次）→ 多平台发布/下架 → 校验 → 汇总回复 | Triggers 列表改为流程描述——和 SKILL.md 一致 |
| 3 | （无） | ClawHub 支持组织发布（ownerHandle，可选）；403/404/401 严格区分，防止误判 | 补充关键特性——和 SKILL.md 一致 |

### 和 SKILL.md frontmatter 对照
SKILL.md: "将 skill 发布/下架到外部平台（ClawHub/GitHub/GitCode）：幂等探测 → bump 重试（≤3 次）→ 多平台发布/下架 → 校验 → 汇总回复。ClawHub 支持组织发布（ownerHandle，可选）；403/404/401 严格区分，防止误判。"

configs 和 SKILL.md 完全一致（去掉末尾句号）。

---

## 不改动的

- skill-issue description（已加，保持不动）
- 其他 skill 的 description（不在本次范围）
- SKILL.md frontmatter description（已在之前 commit 改过——但 skill-tester 的"qa agent 执行"和"sre"小写问题后续需单独处理）

## 注意事项

- 只改 configs/skill-description.json 里 3 个 skill 的 description
- 改完后 JSON 语法验证
- 改完后提交到本地
