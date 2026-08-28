# skill-issue 下架/撤回路由修复方案

## 背景

Issue #16 下架 huawei-cloud-ecs-servers 技能时，Issue Agent 把"下架"理解成了"删除代码"，走 Coding Agent 创建删除 PR，而不是转 SRE Agent 从外部平台下架。

根因：skill-issue 的判断标准第 2 条太笼统——所有 ready 的需求都走"生成 Spec → A2A 交办 Coding Agent"，没有先判断需求类型。转交表里虽然写了"skill 发布/下架/同步 → sre"，但判断标准没有执行这个路由。

## 问题

下架是 SRE Agent 的职责（skill-publish 从 ClawHub/GitHub/GitCode 发布仓撤回），不需要 Coding Agent 删代码。Issue Agent 把下架当删除，导致：
- Coding Agent 创建了删除 PR（多余操作）
- SRE Agent 又从三平台下架（重复操作）
- 用户只说"下架"时不应删仓库代码

## 方案

在 skill-issue 的判断标准和流程里加需求类型前置判断——下架/撤回类需求短路到 SRE Agent，不生成 Spec、不走 Coding Agent。

### 场景区分

| 需求 | 关键词 | 路由 | 生成 Spec |
|------|--------|------|-----------|
| 下架/撤回 | 下架/撤回/unpublish（不含"删除"） | 直接 A2A 转 SRE Agent | 否 |
| 删除代码 | 删除/移除/remove | 走 Spec → Coding Agent | 是 |
| 创建/生成 | 创建/生成/新建 | 走 Spec → Coding Agent | 是 |
| 其他开发 | - | 走 Spec → Coding Agent | 是 |

"下架+删除"场景由用户分两次触发：先下架（转 SRE Agent），再删除代码（走 Spec → Coding Agent）。不破坏 A2A 单向链路，不违反权责分离。

## 改动点

文件：`skills/skill-issue/SKILL.md`，4 处改动

### 改动 1：判断标准（行 72-74）

当前：
```
判断标准：
1. **用户明确要求做某事，而本技能的「硬约束」表中该行为被标记为 ❌ 禁止** → 说明此任务超出你的范围，查 Agent 列表交办。
2. **可开发性评级为 ready** → 生成 Spec 文档 → 识别危险场景 → 无危险直接 A2A / 有危险等用户确认后 A2A 交办 Coding Agent。
3. **需求不清晰 / 无法确定需求边界时** → 通过 A2A 交办 **vod**（开发者声音引导与需求澄清），由 vod 与开发者沟通收敛需求后再回到 issue 流程。
```

改为：
```
判断标准：
1. **用户明确要求做某事，而本技能的「硬约束」表中该行为被标记为 ❌ 禁止** → 说明此任务超出你的范围，查 Agent 列表交办。
2. **需求类型路由（必须先于 Spec 生成判断）**：
   - **下架/撤回**（关键词：下架/撤回/unpublish，不含"删除"）→ 不生成 Spec，直接 A2A 转 SRE Agent，context 带需求摘要和操作类型（下架）
   - **其他开发需求（含创建/生成/删除）** → 可开发性评级为 ready → 生成 Spec 文档 → 识别危险场景 → 无危险直接 A2A / 有危险等用户确认后 A2A 交办 Coding Agent
3. **需求不清晰 / 无法确定需求边界时** → 通过 A2A 交办 **vod**（开发者声音引导与需求澄清），由 vod 与开发者沟通收敛需求后再回到 issue 流程。
```

### 改动 2：Step 4 事件意图表（行 128）

当前：
```
| 用户要求启动开发/实现功能 | **先确认评级**；ready → 生成 Spec → 识别危险场景 → 无危险直接 A2A / 有危险等确认；未 ready → 按评级处理 |
```

改为（加一行下架/撤回）：
```
| 用户要求下架/撤回 skill | **不生成 Spec**；直接 A2A 转 SRE Agent，context 带需求摘要和操作类型（下架） |
| 用户要求启动开发/实现功能 | **先确认评级**；ready → 生成 Spec → 识别危险场景 → 无危险直接 A2A / 有危险等确认；未 ready → 按评级处理 |
```

### 改动 3：Step 5 流程段（行 139）

当前：
```
- **ready** → 生成 Spec（`references/spec-generation.md`）→ 贴 Issue 评论 → 打 `spec-ready` → **识别危险场景**：
```

改为：
```
- **ready** → 先判断需求类型：
  - **下架/撤回**（关键词：下架/撤回/unpublish，不含"删除"）→ 不生成 Spec，直接 A2A 转 SRE Agent，context 带需求摘要和操作类型（下架）
  - **其他** → 生成 Spec（`references/spec-generation.md`）→ 贴 Issue 评论 → 打 `spec-ready` → **识别危险场景**：
```

### 改动 4：Step 7 A2A 调用段（行 182）

当前：
```
### 7. 需要 A2A 时（⚠️ 危险场景已确认或无危险场景时）
- 查本 prompt 底部 Agent 列表，构造 context，调用 `curl -s --max-time 30 -X POST {{SERVER_URL}}/api/a2a`
- ⚠️ **A2A 必须携带真实 `issue_number`（必填）**
- **危险场景已确认时**，A2A context 需额外带"用户已确认危险操作"
```

改为：
```
### 7. 需要 A2A 时（⚠️ 危险场景已确认或无危险场景时）
- **下架/撤回类需求** → 直接 A2A 转 SRE Agent，不走危险场景判断，context 带需求摘要和操作类型（下架）
- **其他需求**（危险场景已确认或无危险场景时）→ 查本 prompt 底部 Agent 列表，构造 context，调用 `curl -s --max-time 30 -X POST {{SERVER_URL}}/api/a2a`
- ⚠️ **A2A 必须携带真实 `issue_number`（必填）**
- **危险场景已确认时**，A2A context 需额外带"用户已确认危险操作"
```

## 影响分析

| 影响点 | 影响 | 需要改动 |
|--------|------|---------|
| A2A API | 无（支持 issue→sre 转发） | 否 |
| spec-generation.md | 缺转 sre 的 context 示例（可选，prompt 底部已有协议章节） | 否 |
| comment-guide.md | 无（评论前缀规范已覆盖） | 否 |
| issue-developability-analysis.md | 无（下架不走评级） | 否 |
| 事件链路 | 链路变短（VOD→Issue→SRE，少 Coding/QA） | 否（正常） |
| Issue #16 问题 | 修复（下架不再走 Coding Agent） | 是（改动 1-4） |

## 验证

改动后下架链路预期：VOD Agent → Issue Agent（识别下架意图，不生成 Spec）→ SRE Agent（skill-publish 从三平台下架）→ Issue 评论告知下架结果。

不再出现 Coding Agent 删除仓库代码的多余操作。
