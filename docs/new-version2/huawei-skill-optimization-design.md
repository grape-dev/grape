# 华为云 Skill 技能优化——方案设计文档

> 本文档合并了本次优化的所有方案设计，按模块分组。

---

## agent-name-prefix-fix

# 评论前缀 $AGENT_NAME → 硬编码修复方案

## 问题

skill-creator/tester/publish 用 `[$AGENT_NAME]` 环境变量做评论前缀，但 agent 是 LLM，不一定读环境变量或不展开 shell 变量，导致评论里出现 `[coding]` / `[qa]` / `[sre]` 而不是 `[Coding Agent]` / `[QA Agent]` / `[SRE Agent]`。

issue/vod 已改为硬编码固定前缀（`[Issue Agent]` / `[VOD Agent]`），运行可靠。

## 修复范围（3 个 skill）

| skill | agent_type | $AGENT_NAME → 硬编码 |
|-------|-----------|---------------------|
| skill-creator | coding | [Coding Agent] |
| skill-tester | qa | [QA Agent] |
| skill-publish | sre | [SRE Agent] |

> skill-pr / qa 不在本次范围。
> issue / issue-advance / vod 已硬编码，不需要改。

## 逐 skill 改动点

### skill-creator（4 处）

| # | 行号 | 当前 | 改后 |
|---|------|------|------|
| 1 | 43 | `[$AGENT_NAME]` 开头 | `[Coding Agent]` 开头 |
| 2 | 44 | `[$AGENT_NAME] {回复}` | `[Coding Agent] {回复}` |
| 3 | 120 | `[$AGENT_NAME] 🔴 Skill 创建失败` | `[Coding Agent] 🔴 Skill 创建失败` |
| 4 | 224 | `[$AGENT_NAME] ✅ Skill 已创建并提交 PR` | `[Coding Agent] ✅ Skill 已创建并提交 PR` |

### skill-tester（11 处）

**SKILL.md（7 处）：**

| # | 行号 | 当前 | 改后 |
|---|------|------|------|
| 1 | 40 | `[$AGENT_NAME]` 开头 | `[QA Agent]` 开头 |
| 2 | 42 | `[$AGENT_NAME] ✅ 测试通过` | `[QA Agent] ✅ 测试通过` |
| 3 | 43 | `[$AGENT_NAME] 🔴 测试失败` | `[QA Agent] 🔴 测试失败` |
| 4 | 67 | `"[$AGENT_NAME] ✅ 测试通过"标记` | `"[QA Agent] ✅ 测试通过"标记` |
| 5 | 119 | `[$AGENT_NAME] ✅ 测试通过`（评论模板） | `[QA Agent] ✅ 测试通过` |
| 6 | 129 | `[$AGENT_NAME] ✅ 测试通过`（PR 评论） | `[QA Agent] ✅ 测试通过` |
| 7 | 188 | `"[$AGENT_NAME] 🔴 测试失败"开头` | `"[QA Agent] 🔴 测试失败"开头` |

**references/test-report-format.md（4 处）：**

| # | 行号 | 当前 | 改后 |
|---|------|------|------|
| 8 | 7 | `[$AGENT_NAME] ✅ 测试通过` | `[QA Agent] ✅ 测试通过` |
| 9 | 8 | `[$AGENT_NAME] 🔴 测试失败` | `[QA Agent] 🔴 测试失败` |
| 10 | 15 | `[$AGENT_NAME] ✅ 测试通过`（报告模板） | `[QA Agent] ✅ 测试通过` |
| 11 | 45 | `[$AGENT_NAME] 🔴 测试失败`（报告模板） | `[QA Agent] 🔴 测试失败` |

### skill-publish（4 处）

| # | 行号 | 当前 | 改后 |
|---|------|------|------|
| 1 | 29 | `[$AGENT_NAME]` 开头 | `[SRE Agent]` 开头 |
| 2 | 30 | `[$AGENT_NAME] {回复}` | `[SRE Agent] {回复}` |
| 3 | 133 | `[$AGENT_NAME] ✅ Skill 发布完成` | `[SRE Agent] ✅ Skill 发布完成` |
| 4 | 139 | `[$AGENT_NAME] ✅ Skill 发布完成`（示例） | `[SRE Agent] ✅ Skill 发布完成` |

## 不改的

| skill | 原因 |
|-------|------|
| issue | 已硬编码 `[Issue Agent]` ✅ |
| vod | 已硬编码 `[VOD Agent]` ✅ |
| issue-advance | 已硬编码 `[issue agent]`（小写问题另议） |
| skill-pr | 不在本次范围 |
| qa | 不在本次范围 |

## 改动统计

- 3 个 skill（4 个文件：3 个 SKILL.md + 1 个 test-report-format.md）
- 共 19 处 $AGENT_NAME → 硬编码
  - skill-creator/SKILL.md：4 处
  - skill-tester/SKILL.md：7 处
  - skill-tester/references/test-report-format.md：4 处
  - skill-publish/SKILL.md：4 处
- scripts/ 无 AGENT_NAME，不涉及
- 不改代码（src/），只改 SKILL.md + references/
- 不改 agent-description.json（保留 $AGENT_NAME 环境变量注入，其他 skill 仍可用）


---

## skill-chain-optimization

# skill 链路全面优化方案（P0-P2）

## 优化项

| # | 优先级 | 优化项 | 涉及 skill | 原因 |
|---|--------|--------|-----------|------|
| 1 | P0 | skill-issue 加任务结束 JSON 段落 | skill-issue | 缺失——grape 无法解析结果 |
| 2 | P1 | "不得用长流程叙述替代 JSON 块"约束 | creator + tester + publish + issue | agent 可能忘输出 JSON |
| 3 | P1 | 长命令 timeout + 禁止前台长驻 | skill-creator + skill-tester | pip install SDK / hcloud CLI 可能 hang |
| 4 | P2 | 代码自审查 checklist | skill-creator | PR 前发现代码质量/安全问题，减少 tester 返工 |

## P0：skill-issue 加任务结束 JSON

### 原因

issue 基础版有任务结束 JSON（第 185 行），但 issue-advance 复制时漏了。skill-issue 基于 issue-advance 复制——也漏了。

### 改动

在 skill-issue/SKILL.md 末尾（## 参考之后）加：

```markdown
## 任务结束

完成所有业务动作（评论/打标签/分配/A2A 交办/危险操作确认）后，**必须在最终答复末尾输出结构化 JSON 块**，然后结束任务：

```json
{
  "actions": ["做了什么，如 analyzed:issue / generated:spec / confirmed:dangerous-operation / a2a:coding"],
  "conclusion": "处理结论（已 A2A coding / 等待用户确认危险操作 / 需求不清晰需 vod 澄清）",
  "artifacts": ["产生的产物：Spec 文档评论 / 标签 / 分配 / 危险操作确认评论"],
  "next_step": "下一步建议",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```

注意：
- 只输出一个 JSON 块，放在答复末尾
- 不得用长流程叙述替代 JSON 块
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造
```

## P1：4 个 skill 加"不得用长流程叙述替代 JSON 块"约束

### 原因

implementation 有这个约束——agent 是 LLM，可能读完流程后输出长流程叙述就结束了，忘了输出 JSON 块。

### 改动

在 4 个 skill 的任务结束 JSON 段落里加：

```
- 不得用长流程叙述替代 JSON 块
```

| skill | 当前有"不得用" | 改后 |
|-------|:---:|------|
| skill-creator | ❌ | 加"不得用长流程叙述替代 JSON 块" |
| skill-tester | ❌ | 加"不得用长流程叙述替代 JSON 块" |
| skill-publish | ❌ | 加"不得用长流程叙述替代 JSON 块" |
| skill-issue | ❌（连 JSON 都没有） | P0 加 JSON 时一并加 |

## P1：skill-creator + skill-tester 加"长命令 timeout + 禁止前台长驻"

### 原因

skill-creator 生成华为云 skill——Step 2 技术调研涉及 hcloud CLI / pip install huaweicloudsdk，Step 3.5 自测可能跑 pip install -r requirements.txt。这些都是长命令，可能 hang。

skill-tester 测试华为云 skill——可能跑 pip install / hcloud CLI / API 调用。

### 改动

在 skill-creator/SKILL.md 和 skill-tester/SKILL.md 的安全硬约束段落加 2 条：

```
N. **长命令执行会静默**：`pip install`、`hcloud CLI`、大测试等长命令运行期间无输出。执行这类命令时**加 `timeout` 限制（如 `timeout 300 pip install ...`）**并一次只装必要依赖，避免长时间无输出影响执行。
N+1. **禁止前台运行长驻/服务类命令**：`python -m http.server`、`npm run dev` 等会阻塞不退出的命令，严禁前台直接执行。改用后台启动+验证+关闭模式。
```

| skill | 当前有 | 改后 |
|-------|--------|------|
| skill-creator | ❌ 无 | 加 2 条（安全硬约束第 7、8 条） |
| skill-tester | ❌ 无 | 加 2 条（安全硬约束第 7、8 条） |
| skill-publish | 不涉及 | 不改 |
| skill-issue | 不涉及 | 不改 |

## P2：skill-creator 加代码自审查 checklist

### 原因

implementation/iac-coding 都在"创建 PR 前必须先自审查"——检查代码质量/安全/功能正确性。

skill-creator 只有 spec_check（格式检查）——不检查代码质量/安全。
代码质量问题推到 tester → 返工 → 浪费 A2A 往返。

### 改动

在 skill-creator/SKILL.md 的 Step 3.5 自测流程里加代码自审查 checklist：

```markdown
- **代码自审查 checklist**（创建 PR 前必须逐项确认）：
  - [ ] 无硬编码 AK/SK/Token（凭据从环境变量/项目知识读取）
  - [ ] 无 mock 模式/假数据/MOCK_DATA
  - [ ] 错误处理完整（API 错误有提示，不静默崩溃）
  - [ ] timeout=30 设置（所有 API 调用有超时）
  - [ ] 无死代码/注释掉的代码块
  - [ ] 子命令参数完整（--help 显示所有子命令）
  - [ ] base_url 从项目知识获取（_get_base_url 有项目知识扫描）
  - [ ] capability-list 子命令存在
```

## 不改的

| 项 | 原因 |
|----|------|
| skill-publish 加长命令 timeout | 不涉及——只发布代码不跑长命令 |
| skill-issue 加长命令 timeout | 不涉及——只分析+评论不跑长命令 |
| skill-tester 加代码自审查 | 不需要——tester 是测试别人的代码 |
| issue-advance 加任务结束 JSON | 不在本次范围 |
| 输出规范段落（publish/issue） | P3 非必须 |

## 改动范围

| 文件 | 改动内容 |
|------|---------|
| skills/skill-issue/SKILL.md | P0：加任务结束 JSON 段落 + P1：加"不得用长流程叙述替代" |
| skills/skill-creator/SKILL.md | P1：加"不得用长流程叙述替代" + P1：加长命令 timeout 2 条 + P2：加代码自审查 checklist |
| skills/skill-tester/SKILL.md | P1：加"不得用长流程叙述替代" + P1：加长命令 timeout 2 条 |
| skills/skill-publish/SKILL.md | P1：加"不得用长流程叙述替代" |

共 4 个文件。


---

## skill-creator-responsibility-fix

# skill-creator 职责优化方案（v3——全链路）

## 问题

skill-creator Step 1"需求澄清（苏格拉底式 + 确认门控）"不合理：

1. **职责越界**——creator 是代码生成器，应该"按 Spec 生成代码"，不应该做需求收集
2. **职责重叠**——issue-advance 已经生成 Spec，creator 又问 5 个维度
3. **确认门控卡住 A2A 链路**——等用户确认导致任务超时
4. **苏格拉底式多轮提问不适合 A2A**——每次 A2A 是单次任务

## A2A 链路职责划分

```
vod → issue → issue-advance → creator → tester → publish
```

| 环节 | 职责 | 产出 | 不做什么 |
|------|------|------|---------|
| vod | 收集声音 → 创建 Issue | Issue（原始需求） | 不分析需求 |
| issue | 分类/标签/分配/回复评论 | 分类结果 + 原始需求 | 不生成 Spec / 不写代码 |
| issue-advance | 深度可开发性分析 → 生成 Spec | Spec（确定的方案） | 不写代码 |
| creator | 按 Spec → 生成代码 | PR | 不收集/澄清需求 |
| tester | 测试代码 | 报告+合并 | 不写/修代码 |
| publish | 发布代码 | 发布结果 | 不测试/不写代码 |

## 各环节能力不丢失

### vod（不改）

- 场景 A：开发者声音 → 聚类分析 → 创建 Issue（标注 auto-submitted by VOD Agent）
- 场景 B：接管 issue_opened → 审视非 VOD 创建的 Issue → A2A 交办 Issue Agent
- 产出：Issue（含原始需求）
- 不改 ✅

### issue 基础版（不改）

- 接收 Issue → 分类 → 有效性检查 → 打标签 → 分配负责人 → 回复评论
- A2A 交棒 coding（构造 context 传原始需求 + 分析结论）
- 不生成 Spec
- 不改 ✅
- ⚠️ 注意：issue 基础版直接交棒 creator 时，creator 会请求 Spec（因为 creator 不再做需求澄清）。用户需要让 issue-advance 生成 Spec 后再触发 creator。

### issue-advance（不改）

- 项目知识基线 → 深度可开发性分析 → 分类/标签/分配 → Spec 生成 → A2A 触发开发
- ready → 生成 Spec 文档 → 贴 Issue 评论（以 `[Issue Agent] 📋 Spec Document` 开头）→ 打 spec-ready 标签 → A2A 通知 coding
- not-ready → 追问/补充/说明不可行
- 产出：Spec 文档（确定的方案）
- 不改 ✅

### creator（改）

Step 1 从"需求澄清"改为"读取 Spec"：

**有 Spec（issue-advance 触发）：**
- 读 Issue → 找 Spec 文档（`[Issue Agent] 📋 Spec Document` 开头）
- 从 Spec 提取技术方案信息
- 直接进入 Step 2（不等确认）

**无 Spec（issue 基础版触发）：**
- 读 Issue → 找不到 Spec 文档
- 评论请求生成 Spec（建议 issue-advance 处理）→ 任务结束（不等待）
- 不自己做需求澄清/苏格拉底式提问

### tester（不改）

- 功能验证 / 规范检查 / 安全扫描 / 报告 / 合并 / A2A
- 不改 ✅

### publish（不改）

- 发布到 ClawHub/GitHub/GitCode
- 不改 ✅

## 修改方案

### 改动 1：职责描述更新

**当前（第 27 行）：**
```
负责查重、需求澄清、技术调研、骨架生成、创建 PR。
```

**改后：**
```
负责查重、读取 Spec、技术调研、骨架生成、创建 PR。
```

### 改动 2：依赖前序分析更新

**当前：**
```
通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。
issue skill 触发 → 摘要包含需求分析结论 + Spec 文档。此摘要可作快速参考，但不保证完整性，必须通过 GitCode API 获取 Issue 完整信息后再开始工作。
```

**改后：**
```
通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。
issue-advance 触发 → Issue 评论里已有 Spec 文档（以 [Issue Agent] 📋 Spec Document 开头，含 spec-ready 标签）。A2A context 含 Spec 摘要（Objective / Scope / Technical Approach / Acceptance Criteria）。
creator 读 A2A context 摘要后，通过 GitCode API 获取 Issue 完整信息（含 Spec 评论），按 Spec 执行。
issue 基础版触发 → 无 Spec → creator 评论请求 issue-advance 生成 Spec → 任务结束。
```

### 改动 3：Step 1 从"需求澄清"改为"读取 Spec"

**当前：**
```
### Step 1: 需求澄清（苏格拉底式 + 确认门控）
- 从 Issue 获取完整内容（标题/正文/标签/评论）
- 每次只问一个问题，覆盖以下 5 个维度：
  1. 目标服务 — 华为云哪个服务？
  2. 功能范围 — Skill 做什么？
  3. 执行模式 — CLI / SDK / API？
  4. CLI 操作 — 涉及哪些操作？
  5. 触发场景 — 什么时候用？
- 覆盖所有维度后 → 展示需求摘要表 → 等用户确认
- 必须用户确认后才进入 Step 2
```

**改后：**
```
### Step 1: 读取 Spec（从 Issue 评论获取确定的方案）
- 从 Issue 获取完整内容（标题/正文/标签/评论）
- 优先从 Issue 评论里找 Spec 文档（以 `[Issue Agent] 📋 Spec Document` 开头）
- 从 Spec 提取技术方案信息：
  1. **目标服务** — Spec 的 Affected Modules / Technical Approach
  2. **功能范围** — Spec 的 Scope (In Scope)
  3. **执行模式** — Spec 的 Technical Approach（CLI / SDK / API）
  4. **操作列表** — Spec 的 Acceptance Criteria
  5. **触发场景** — Spec 的 Objective
- Spec 存在且信息完整 → 直接进入 Step 2（不等用户确认）
- Spec 不存在 → 在 Issue 评论请求 issue-advance 生成 Spec → 任务结束（不等待，不自己澄清需求）

> ⚠️ creator 不做需求收集 / 不做苏格拉底式提问 / 不做确认门控——这是 issue/issue-advance 的职责。
> creator 收到的是 issue-advance 产出的"确定的、可以实施的方案"（Spec），直接按 Spec 生成代码。
```

### 改动 4：场景识别更新

**当前：**
```
| worktree 已有 skills/{name}/ 目录 | 修复 skill | Step 0（跳过查重）→ Step 1（跳过确认）→ Step 2 → ... |
```

**改后：**
```
| worktree 已有 skills/{name}/ 目录 | 修复 skill | Step 0（跳过查重）→ Step 1（跳过 Spec 读取，用已有产物）→ Step 2 → ... |
```

## 不改的

| 项 | 原因 |
|----|------|
| vod | 职责合理（收集声音 → 创建 Issue）✅ |
| issue 基础版 | 职责合理（分类/标签/分配/回复评论）✅ |
| issue-advance | 职责合理（生成 Spec）✅ |
| tester | 职责合理（测试代码）✅ |
| publish | 职责合理（发布代码）✅ |
| Step 0 查重 | 合理保留 |
| Step 2 技术调研 | 合理保留 |
| Step 3 骨架生成 | 合理保留 |
| Step 3.5 自测 | 合理保留 |
| Step 4-6 | 合理保留 |
| 安全硬约束/评论规范/硬规则 | 合理保留 |

## 影响分析

### 正面影响

1. creator 回归"代码生成器"定位——按 Spec 生成代码 ✅
2. 职责不重叠——issue-advance 生成 Spec，creator 读 Spec ✅
3. 不卡 A2A 链路——不等用户确认 ✅
4. 需求不完整时优雅退出——请求生成 Spec → 任务结束 ✅
5. vod/issue/issue-advance 能力不丢失 ✅

### 负面影响

1. issue 基础版直接交棒 creator → creator 请求 Spec → 任务结束
   → 用户需要让 issue-advance 生成 Spec 后再触发 creator
   → 但这比卡住更好——至少任务正常结束

2. creator 不再做苏格拉底式引导用户思考
   → 需求不清晰时不会引导
   → 但这是 issue / vod 的职责——不是代码生成器的职责

## 改动范围

- 只改 skills/skill-creator/SKILL.md（1 个文件，4 处改动）
- 不改 vod / issue / issue-advance / tester / publish
- 不改 src/ 代码
- 不需要重新部署


---

## skill-issue-design

# skill-issue 新增方案

## 目标

基于 issue-advance 新建 skill-issue，保留全部能力，新增 2 个能力：
1. 危险场景识别（付费资源/删除/不可逆操作）
2. 危险场景用户确认（标记+两次任务，不卡 A2A 链路）

## 链路

```
vod → skill-issue（分析+Spec+危险识别+确认）→ creator → tester → publish
```

skill-dev 场景下 skill-issue 替代 issue-advance。

## 和 issue-advance 的区别

| 能力 | issue-advance | skill-issue |
|------|:---:|:---:|
| 项目知识基线 | ✅ | ✅ 复用 |
| 深度可开发性分析 | ✅ | ✅ 复用 |
| 分类/标签/分配 | ✅ | ✅ 复用 |
| Spec 生成 | ✅ | ✅ 复用 |
| A2A 通知 coding | ✅ | ✅ 复用 |
| **危险场景识别** | ❌ | ✅ 新增 |
| **危险场景用户确认** | ❌ | ✅ 新增 |

## 新增能力 1：危险场景识别

### 识别维度

| 危险类型 | 关键词 | 示例 |
|---------|--------|------|
| 付费资源 | Create/Purchase/Buy + ECS/OBS/RDS/BSS/VPC/EIP/SLB | Create ECS（创建云服务器=付费） |
| 删除操作 | Delete/Remove/Drop | Delete OBS Bucket（删除存储桶） |
| 不可逆操作 | Hard Delete/Force/Terminate/Purge | Hard Delete（永久删除不可恢复） |

### 识别时机

在 Step 5 生成 Spec 时识别——读取 Spec 的 Technical Approach / Affected Modules / Acceptance Criteria，匹配危险关键词。

### 识别结果

在 Spec 的 Risks & Mitigations 段落里标注：

```
| Risk | Impact | Mitigation |
| ⚠️ 涉及付费资源（Create ECS） | High | 变更类操作需用户确认 |
| ⚠️ 涉及不可逆操作（Hard Delete） | High | 变更类操作需用户确认 |
```

## 新增能力 2：危险场景用户确认

### 确认流程（标记+两次任务，不卡 A2A 链路）

**第一次执行（识别到危险场景）：**
1. 正常执行 issue-advance 全流程（分析→评级→生成 Spec）
2. 识别危险场景 → Spec Risks 标注
3. Issue 评论提示："⚠️ 本 skill 涉及危险操作（付费资源/不可逆操作），请回复'确认'继续开发"
4. 任务结束（不等待，不 A2A creator）

**第二次执行（用户回复"确认"后）：**
1. issue_comment 事件触发 skill-issue
2. 前置判断：读 Issue 评论 → grep "确认" → 找到
3. 跳过分析（已做过）→ 读已有 Spec
4. A2A creator（context 带"用户已确认危险操作"）

**无危险场景：**
- 正常执行 → 直接 A2A creator（和 issue-advance 一样）

### 评论标记格式

危险提示评论（第一次）：
```
[Issue Agent] ⚠️ 危险操作确认

本 skill 涉及以下危险操作：
- 付费资源：Create ECS（创建云服务器会产生费用）
- 不可逆操作：Hard Delete（永久删除不可恢复）

Spec 已生成，请确认后回复"确认"继续开发。
完整 Spec 见上方评论。
```

确认标记（用户回复后 grep 用）：
```
[Issue Agent] ⚠️ 危险操作确认
```
→ skill-issue 第二次执行时 grep 这个标记 + 用户的"确认"回复

## 实现方式

### 文件结构

```
skills/skill-issue/
├── SKILL.md                                    # 基于 issue-advance + 新增 2 个能力
└── references/
    ├── assignee-guide.md                       # 从 issue-advance 复制
    ├── codegraph-commands.md                   # 从 issue-advance 复制
    ├── codegraph-project-analysis.md           # 从 issue-advance 复制
    ├── codeowners-guide.md                     # 从 issue-advance 复制
    ├── comment-guide.md                        # 从 issue-advance 复制
    ├── gitcode-api-reference.md                # 从 issue-advance 复制
    ├── issue-developability-analysis.md        # 从 issue-advance 复制
    ├── issue-validity-check.md                 # 从 issue-advance 复制
    ├── labeling-guide.md                       # 从 issue-advance 复制
    └── spec-generation.md                      # 从 issue-advance 复制 + 加危险场景维度
```

### SKILL.md 改动（基于 issue-advance）

#### 改动 1：frontmatter

```yaml
---
name: skill-issue
version: 1.0.0
description: |
  Issue 运营（skill-dev 专用）：项目知识基线 → 深度可开发性分析 → Spec 生成 → 危险场景识别 → 用户确认 → A2A 触发开发。
  基于 issue-advance，新增危险场景识别（付费资源/删除/不可逆操作）+ 用户确认门控。
tools:
  - codegraph
---
```

#### 改动 2：评论前缀

```
[Issue Agent]（固定前缀，不用 $AGENT_NAME）
```

#### 改动 3：Step 5 新增危险场景识别

在"按评级执行动作"里，ready → 生成 Spec 后加：

```
- ready → 生成 Spec → 识别危险场景：
  - 无危险 → 直接 A2A coding（和 issue-advance 一样）
  - 有危险 → Spec Risks 标注 → Issue 评论提示"⚠️ 危险操作确认" → 任务结束（不 A2A，不等待）
- 前置判断（每次执行）：读 Issue 评论 → grep "⚠️ 危险操作确认" + "确认" → 已确认 → A2A coding
```

#### 改动 4：硬约束新增

```
| 涉及付费资源/不可逆操作时必须用户确认 | ✅ 必须做（不跳过） |
| 危险场景确认用标记+两次任务（不等待） | ✅ 必须做（不卡 A2A 链路） |
```

### spec-generation.md 改动

在 Risks & Mitigations 段落加危险场景识别维度：

```
### Step 7: 填写 Risks & Mitigations

【新增】危险场景识别：
- 扫描 Technical Approach / Affected Modules / Acceptance Criteria 里的关键词
- 付费资源：Create/Purchase/Buy + ECS/OBS/RDS/BSS/VPC/EIP/SLB
- 删除操作：Delete/Remove/Drop
- 不可逆操作：Hard Delete/Force/Terminate/Purge
- 识别到 → Risks 表加行：⚠️ 涉及{危险类型}（{具体操作}） | High | 变更类操作需用户确认
```

## 配置改动

### configs/agent-skill-mapping.json

skill-dev 场景：
```json
"skill-dev": {
  "name": "Skill开发",
  "mapping": {
    "vod": "vod",
    "issue": "skill-issue",      ← 改这里（issue-advance → skill-issue）
    "coding": "skill-creator",
    "qa": "skill-tester",
    "sre": "skill-publish"
  }
}
```

其他场景不变（仍用 issue-advance）。

## 不改的

| 项 | 原因 |
|----|------|
| issue-advance | 其他场景仍用，不改 |
| vod | 不改 |
| skill-creator | 不改（本次方案里 creator 读 Spec 的改动另议） |
| skill-tester | 不改 |
| skill-publish | 不改 |
| issue 基础版 | 不改（不在链路里） |

## 影响分析

### 正面影响

1. 危险场景（付费资源/不可逆操作）在 issue 阶段就被识别 ✅
2. 用户确认后才 A2A creator——不会误操作付费资源 ✅
3. 标记+两次任务不卡 A2A 链路 ✅
4. 其他场景不受影响（仍用 issue-advance）✅
5. 不改 src/ 代码 ✅

### 负面影响

1. 涉及危险场景的 skill 需要用户确认 → 多一轮交互
   → 但这是必要的——付费资源不能自动执行
2. 改 agent-skill-mapping.json 需要重启 grape 服务
   → 配置文件改了不会自动重新加载

## 改动范围

| # | 文件 | 改动 |
|---|------|------|
| 1 | skills/skill-issue/SKILL.md | 新建（基于 issue-advance + 新增 2 个能力） |
| 2 | skills/skill-issue/references/*.md | 新建（从 issue-advance 复制，spec-generation.md 加危险场景维度） |
| 3 | configs/agent-skill-mapping.json | 改 skill-dev 场景 issue → skill-issue |


---

## skill-issue-huawei-spec-design

# skill-issue Spec 华为云字段增强方案

## 问题

skill-issue 生成的 Spec 是通用格式——不包含华为云专用字段。
creator 读 Spec 后不知道：
- 命名应该用什么 product/function
- 执行模式是 CLI / SDK / API
- 涉及哪些 CLI 操作
- IAM 权限需要什么

huawei-cloud-skill-creator 的 Phase 1 覆盖 5 个维度——直接决定生成的 skill 结构。
skill-issue 的 Spec 缺这些维度——creator 只能猜。

## 改动

### 1. spec-generation.md 加华为云专用段落（条件性）

在 Technical Approach 段落后加一个条件性段落：

```markdown
## Huawei Cloud Skill Details
> 仅当 Issue 涉及华为云 skill 开发时填写

| 维度 | 值 | 说明 |
|------|-----|------|
| Target Service | {VPC/ECS/OBS/RDS/...} | 华为云目标服务 |
| Feature Scope | {Query/Manage/Deploy/Monitor/Diagnose} | 功能范围 |
| Execution Mode | {CLI/SDK/API} | 执行模式 |
| CLI Operations | {List/Show/Create/Delete/Update} | 涉及操作 |
| Skill Name | huawei-cloud-{product}-{function} | 遵循官方命名规范 |
| IAM Permissions | {service}:{action} | 最小权限 |
| Trigger Scenarios | {何时触发} | 触发场景 |
```

### 2. spec-generation.md Step 2 加华为云识别逻辑

在 Step 2（填写 Objective）后加 Step 2b：

```
### Step 2b: 识别华为云需求（条件性）

扫描 Issue 正文/评论，匹配华为云关键词：
  - hcloud / huaweicloudsdk / 华为云 / Huawei Cloud
  - ECS / VPC / OBS / RDS / CCE / IAM / BSS / ModelArts
  - AK/SK / hcloud CLI

匹配到 → 填写 Huawei Cloud Skill Details 段落
未匹配 → 跳过此段落
```

### 3. skill-issue SKILL.md Step 5 加华为云识别提示

在 Step 5（生成 Spec）的 ready 分支加：
- 识别到华为云关键词 → Spec 必须包含 Huawei Cloud Skill Details 段落
- 未识别到 → Spec 不包含此段落

## 不改的

- Spec 通用段落（Objective / Scope / Technical Approach / Affected Modules / Risks）不变
- 非华为云 skill 不受影响
- creator 的 Step 1 读 Spec 逻辑不变——只是 Spec 多了段落 creator 能读到

## 改动范围

| 文件 | 改动 |
|------|------|
| skills/skill-issue/references/spec-generation.md | 加 Huawei Cloud Skill Details 段落模板 + Step 2b 识别逻辑 |
| skills/skill-issue/SKILL.md | Step 5 加华为云识别提示 |

## 验证

- 模拟一个华为云 VPC 查询 Issue → 检查 Spec 是否包含 Huawei Cloud Skill Details
- 模拟一个非华为云 Issue → 检查 Spec 不包含此段落
- creator 读带华为云字段的 Spec → 检查生成的 skill 命名/脚本类型/命令格式是否正确


---

## skill-publish-fix-design

# skill-publish 修复方案

## 问题

sre agent 发布 blog-manager 时回复的 Issue 评论有 3 个问题：

1. ClawHub URL 用了 API URL（`/api/v1/skills/blog-manager`）不是页面 URL（`/{ownerHandle}/skills/{slug}`）
2. 版本号不一致（ClawHub v0.2.0 vs GitCode v0.1.0），评论模板用单一 {version} 无法表达
3. GitHub 跳过措辞不准确——回复"未配置"但实际配置了（GITHUB_PUBLISH_REPO + GITHUB_TOKEN 都有）

## 修复方案

### 修复 1：ClawHub URL——强制校验获取 owner.handle

位置：第 7 条校验步骤 + 评论模板前

改动：
- 第 7 条已有"ClawHub 校验时从 GET /api/v1/skills/{slug} 返回的 owner.handle 拼接页面 URL"——保留
- 评论模板前新增提示："⚠️ 评论里的 ClawHub URL 必须用第 7 步校验获取的 owner.handle 拼接，不得用 API URL。owner.handle 为空时填'（待确认）'，不得用 /api/v1/skills/{slug} 兜底"

影响：
- 只影响 ClawHub 发布成功的评论
- 不影响发布逻辑
- 如果校验失败（API 超时/404），URL 填"（待确认）"而不是 API URL

### 修复 2：版本号——评论模板每平台独立

位置：评论模板（curl -d body + 格式示例）

改动：
- 单一 {version} → 每平台独立：
  - ClawHub: v{clawhub_version}（bump 后的实际版本）
  - GitHub: v{github_version}（frontmatter 版本）
  - GitCode: v{gitcode_version}（frontmatter 版本）
- 跳过的平台版本填"—"
- 说明部分加"ClawHub 可能因版本占用自动 bump，版本号与其他平台不同"

影响：
- agent 需要分别记录每个平台的实际版本号
- ClawHub 版本从 POST 返回或 GET 确认
- GitCode/GitHub 版本从 SKILL.md frontmatter 读取
- 不影响发布逻辑

### 修复 3：GitHub/GitCode 跳过措辞——必须说明真实原因

位置：第 8 条目标仓库解析

改动：
- 第 8 条现有："未配置 → 报告「未配置发布目标仓库，请到设置页配置」→ 跳过该平台"——保留
- 新增："⚠️ 跳过平台时必须说明真实原因，不得用'未配置'兜底：
  - 令牌缺失（环境变量和 JSON 都没有）→ 「未配置 {平台} 令牌」
  - 令牌存在但无权限（403）→ 「{平台} 令牌无写入权限」
  - 仓库不存在（404）→ 「目标仓库 {repo} 不存在」
  - 配置了但 push 失败 → 「发布失败：{具体原因}」
  - 不得笼统报'未配置'"

影响：
- agent 需要区分"未配置"和"配置了但失败"
- 影响 GitHub 和 GitCode 的跳过场景
- 不影响发布逻辑

## 不修复

- 备份分支创建失败：非阻断，新 skill 目录不存在不需要备份，agent 判断正确

## 影响范围

- 文件：skills/skill-publish/SKILL.md
- 不影响：发布脚本 sync_skill_platform.sh、A2A 流程、其他 skill


---

## skill-publish-idempotency-fix

# skill-publish 幂等探测 404 修复方案

## 问题

skill 被 DELETE 软删除后，ClawHub API GET 返回 404。但 skill-publish 的幂等探测没明确 404 的处理——agent 误判为"已发布"→ 跳过发布。导致：
1. skill 没重新发布（ClawHub 上 Skill not found）
2. 第 7 步校验拿不到 owner.handle → agent 自己拼 URL → URL 错误

## 修复方案

### 修复 1：幂等探测明确 404 = 直接发布

位置：发布流程第 3 步幂等探测

改动：ClawHub GET 返回 404 时明确"skill 不存在（含从未发布或已删除）→ 直接发布，不跳过"

```
当前：
  ClawHub: GET /api/v1/skills/{slug} → 已发布 → 比对指纹 → 一致 = 跳过 / 不同 = bump 后发布
  （404 的处理不明确）

改后：
  ClawHub: GET /api/v1/skills/{slug}
    → 200（skill 存在）→ 比对指纹 → 一致 = 跳过 / 不同 = bump 后发布
    → 404（skill 不存在：从未发布或已删除）→ 直接发布，不跳过
```

### 修复 2：POST 失败时区分软删除 slug 占用

位置：发布流程第 5 步 + 版本 bump 规则第 4 步

改动：POST 返回 "slug already exists" 或类似错误时，明确报"skill slug 被软删除占用，请等待 30 天或联系 ClawHub 支持"，不笼统报"发布失败"

### 修复 3：owner.handle 拿不到时填「待确认」

位置：评论模板前提示（已有，加强）

改动：当前已有"owner.handle 为空时填「（待确认）」"——保留，确认这个兜底在

## 影响范围

- 文件：skills/skill-publish/SKILL.md
- 不影响：发布脚本 sync_skill_platform.sh、A2A 流程、其他 skill
- 不引入新问题（5 个场景分析通过）

## 不修复

- gitsync 的 manifest 第 1 层同样有这个问题——但改 gitsync Python 代码不在本次范围


---

## skill-publish-ownerhandle-fix

# skill-publish ownerHandle 获取 + URL 拼接修复方案

## 问题

ownerHandle 为空（个人发布）时，agent 拿不到 owner.handle → 自己拼 URL → 漏 /skills/ → URL 错误。

当前兜底是填「（待确认）」——偷懒，个人发布时 ownerHandle 就是当前账号名，可以获取到。

## 发现

ClawHub 有 `GET /api/v1/whoami` 端点（用 Bearer token 鉴权）：
```json
{"user":{"handle":"yangaiwu","displayName":"yangaiwu","role":"user"}}
```

用 token 调 whoami 就能拿到当前账号的 handle。

## 修复方案

### ownerHandle 获取优先级

```
1. 用户指令指定（发布时用户明确说了组织/owner）
2. 环境变量 $CLAWHUB_OWNER_HANDLE / repo-credentials.json config.CLAWHUB_OWNER_HANDLE
3. GET /api/v1/whoami 返回的 user.handle（个人发布时用 token 账号名兜底）
4. GET /api/v1/skills/{slug} 返回的 owner.handle（发布后校验时获取，二次确认）
```

### URL 拼接逻辑

```
有 ownerHandle → https://clawhub.ai/{ownerHandle}/skills/{slug}
没有 ownerHandle → 调 GET /api/v1/whoami 获取 handle → 拼 URL
拼好 URL 后 → GET /api/v1/skills/{slug} 确认 skill 存在
  → 200 = 发布成功，URL 正确
  → 404 = skill 不存在，走发布失败逻辑
```

### 具体改动

位置：ClawHub OwnerHandle 段落第 8 条 + 评论模板前提示

第 8 条改为：
```
8. ClawHub 页面 URL 格式（已验证）：https://clawhub.ai/{ownerHandle}/skills/{slug}
   - 个人发布：https://clawhub.ai/{用户名}/skills/{slug}
   - 组织发布：https://clawhub.ai/{org}/skills/{slug}
   - ⚠️ 不是 https://clawhub.ai/skills/{slug}（会 307 重定向到 /skills/skills/{slug}，页面空 skeleton）
   - ⚠️ 不是 https://clawhub.ai/{用户名}/{slug}（少了 /skills/ 段，307 重定向）
   - ownerHandle 获取优先级：
     a. 发布时使用的 ownerHandle（用户指令/环境变量/JSON config）
     b. GET /api/v1/skills/{slug} 返回的 owner.handle（发布后校验时获取）
     c. GET /api/v1/whoami 返回的 user.handle（个人发布兜底——用 Bearer token 调，返回当前账号 handle）
   - 拼好 URL 后必须 GET /api/v1/skills/{slug} 确认 skill 存在（200=正确，404=发布失败）
   - 不得自己拼 URL——必须用上述优先级获取 ownerHandle
```

评论模板前提示改为：
```
⚠️ 评论里的 ClawHub URL 必须用 ownerHandle 拼接（https://clawhub.ai/{ownerHandle}/skills/{slug}）。
ownerHandle 获取优先级：发布时使用的 > GET /api/v1/skills/{slug} 返回的 owner.handle > GET /api/v1/whoami 返回的 user.handle。
不得用 API URL（/api/v1/skills/{slug}）兜底，不得漏 /skills/ 段，不得自己拼 URL。
```

### 去掉「待确认」兜底

个人发布时一定能通过 whoami 拿到 handle——不需要「待确认」兜底。

## 影响范围

- 文件：skills/skill-publish/SKILL.md
- 不影响：发布脚本、A2A 流程、其他 skill
- ClawHub whoami 端点已验证可用（返回 user.handle）

## 与之前修复的关系

- 幂等探测 404 = 直接发布（已修）→ skill 重新发布
- ownerHandle 获取优先级 + whoami 兜底（本次修）→ URL 正确拼接
- 两个修复配合：发布成功 + URL 正确


---

## skill-tester-fix-design

# skill-tester 完整修复方案

## 修复项 1：A2A curl 加 --max-time 30（P0）

**位置：** Step 5 的 2 处 A2A curl（MERGED=1 交棒 sre + 测试失败回 coding）

**当前：**
```bash
curl -X POST {{SERVER_URL}}/api/a2a \
```

**改后：**
```bash
curl -s --max-time 30 -X POST {{SERVER_URL}}/api/a2a \
```

**原因：** A2A 服务不可达时 hang，安全硬约束第 5 条已写"所有 curl 设 30s 超时"但 A2A curl 没遵守。

---

## 修复项 2：变更类逐项确认 → 自动执行 + 最小规格 + 费用提示（P0）

**位置：** Step 1 测试策略 + Step 1.x 变更类操作确认

**当前（第 86-87 行）：**
```
- 变更类（Create/Update/Delete）→ 逐项确认后真实执行 + 记录资源变更（resource_type/resource_id/change_type）
- 变更类资源生命周期测试：Create → Query 验证 → Delete 验证清理（每步用户确认）
```

**当前（第 92-96 行，整个 1.x 段落）：**
```
### 1.x 变更类操作确认
- 测试涉及 Create/Update/Delete → 执行前 Issue 评论提示用户确认（逐项）
- 每项确认后执行 → 记录资源变更：resource_type/resource_id/change_type/cleanup_method
- 资源生命周期测试：Create → Query 验证 → Delete 验证清理
- 只读查询类（List/Show）→ 无需确认，自动真实执行
```

**改后（第 86-87 行）：**
```
- 变更类（Create/Update/Delete）→ 自动执行 + 自动清理（不等用户确认——涉及费用风险，通过最小规格+标记+防护保障安全）
- 变更类资源生命周期测试：Create → Query 验证 → Update 验证 → Delete 验证清理（全自动，详见 1.x）
```

**改后（第 92-96 行，整个 1.x 重写）：**
```
### 1.x 变更类操作规范（自动执行 + 资源安全防护）

变更类测试涉及真实云资源创建/修改/删除——有费用风险。以下规范确保安全：

**执行前：**
- 先跑 `create --help` 查看参数列表
- 规格类参数（flavor/spec/instance-type/size 等）→ 用最小值
- 资源名称必须加前缀 `skill-test-{timestamp}`（如 `--name skill-test-20260821-hcs-ecs`）
- Issue 评论提示："⚠️ 即将创建测试资源：{资源类型}（最小规格），测试完成后自动删除"

**执行中（资源 ID 记录）：**
- create 成功后立即 Issue 评论记录："⚠️ 测试资源已创建：{资源类型} ID={resource_id}，正在执行后续测试"
- create 失败 → 评论"创建失败，无残留"→ 测试失败，不执行后续步骤

**清理（3 层防护）：**
1. **try-finally（进程内）**：create 成功后任何步骤失败/异常，finally 必须执行 delete（resource_id）
2. **资源 ID 评论记录（跨进程）**：create 成功后立即评论记录 resource_id；若进程崩溃/超时 kill，resource_id 在评论里
3. **前置判断补删（下次任务启动时）**：grep Issue 评论历史找"⚠️ 测试资源已创建"但没找到对应的"✅ 测试资源已删除"→ 尝试 delete → 删不了 → 告警

**delete 安全检查（防删错）：**
- delete 前必须先 get 确认资源带 `skill-test-` 前缀
- 带前缀 → 执行 delete
- 不带前缀 → ⛔ 不 delete → 评论告警"发现非测试资源，跳过删除"
- 禁止使用 delete-all / 批量删除——只按 resource_id 删单个

**执行后：**
- delete 成功 → 评论"✅ 测试资源已删除：{资源类型} ID={resource_id}"
- delete 失败 → 评论告警"🔴 资源残留：{资源类型} ID={resource_id}，请手动清理：{cleanup 命令}"
- 整个过程在几秒内完成（create→get→update→delete），费用 < 0.01 元

**只读查询类（List/Show/Get）→ 无需以上防护，自动真实执行**
```

**原因：** 逐项确认在当前架构下会卡住（任务超时 + 事件驱动触发新任务 + 无状态恢复）。改为自动执行 + 最小规格 + 资源标记 + 3 层防护 + 费用提示。

---

## 修复项 3：变更类 mode=manual → mode=merge（P0）

**位置：** Step 5 判断操作类型

**当前（第 136-137 行）：**
```
- 只读查询类（List/Show/Get）→ mode=merge
- 变更类（Create/Update/Delete）→ mode=manual
```

**改后：**
```
- 所有操作类型 → mode=merge（测试通过后自动合并）
```

**同时删除 MERGED=0 段落（第 164-167 行）**——不再有 mode=manual，不会出现 MERGED=0。

**原因：** 变更类测试已自动执行 + 资源安全防护保障，测试通过即可自动合并。mode=manual 等人工合并会卡住全链路。

**注意：** MERGED=0 的前置判断补删机制仍保留（用于资源残留补删，不是等人工合并）。

---

## 修复项 4：资源残留防护（P0）

**已包含在修复项 2 的 1.x 段落里**——3 层防护：
1. try-finally（进程内）
2. 资源 ID 评论记录（跨进程）
3. 前置判断补删（下次任务启动时）

**前置判断补删需要加到"前置判断"段落（第 66-75 行）：**

在现有前置判断（grep "✅ 测试通过"标记）后加：

```
### 前置判断：资源残留补删
- grep Issue 评论历史找"⚠️ 测试资源已创建"但没找到对应的"✅ 测试资源已删除"
- 找到未清理的资源 → get 确认带 skill-test- 前缀 → 带前缀 → 尝试 delete
- delete 成功 → 评论"✅ 补删测试资源：{资源类型} ID={resource_id}"
- delete 失败 → 评论告警"🔴 仍有残留：{资源类型} ID={resource_id}，请手动清理：{cleanup 命令}"
- 不带 skill-test- 前缀 → ⛔ 不 delete → 告警"发现非测试资源，跳过删除"
```

---

## 修复项 5：防删错（P0）

**已包含在修复项 2 的 1.x 段落里**——4 条规则：
1. create 时资源名称加 `skill-test-` 前缀
2. create 后 get 验证带前缀
3. delete 前 get 确认带前缀
4. 禁止批量删除

---

## 修复项 6：补判断标准段落（P3）

**位置：** 在"参考"段前加新段落

**新增：**
```
## 判断标准

- 功能验证：只读类 API 调用返回有效数据（非空/字段完整/退出码 0）= 通过
- 功能验证：变更类 create→get→update→delete 全链路成功 + 资源已清理 = 通过
- 规范检查：spec_check.py 退出码 0 = 通过
- 安全扫描：gitleaks 无密钥发现 = 通过
- 资源清理：所有测试资源已删除（无 orphaned）= 通过
- 资源残留：delete 失败但有告警 = 测试失败（不影响其他检查项）
- 以上全部通过 = 测试通过 → 自动合并 → A2A sre
- 任一项失败 = 测试失败 → A2A 回 coding
```

---

## 不改的项

| 项 | 原因 |
|---|------|
| 只读类自动测试 | 已经是对的 |
| 评论固定标记 | 已经是对的（grep 依赖保障格式稳定） |
| 前置判断 grep "✅ 测试通过" | 已经是对的 |
| 安全硬约束 | 已经是对的 |

---

## 影响范围

- 文件：skills/skill-tester/SKILL.md（仅此一个文件）
- 不影响：skill-creator / skill-publish / huawei-cloud-skill-creator / huawei-cloud-skill-tester / skill-auto-test-pr
- 不影响：A2A 流程 / event_pipeline / webhook
- 不改架构：只改 SKILL.md 规范文本


---

