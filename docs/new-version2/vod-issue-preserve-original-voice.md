# VOD Issue 审视结构化保留原始声音改造方案

> 版本：v1.0
> 日期：2026-08-10
> 状态：待评审
> 需求：VOD Agent 对用户 Issue 做「审视结构化」后，**必须保留用户的原始声音（原始正文原话）**，
> 不能直接用结构化模板覆盖掉用户原始措辞。
> 触发实例：`gitcode.com/Jason7602/gitcode-pr-assistant/issues/5`「PR创建之后检查assginee责任人的配置是否合理」——
> 该 Issue body 已被「VOD 审视结构化」整体改写为模板（目标/现状/期望行为/验收标准/优先级/实现指引），
> 结尾仅标注 `来源：Issue #5，@Jason7602`，用户原始描述已从可见正文中消失。
> 范围：**技能文件 + 少量代码 + 测试**（先更新本文档，代码实施见 §四）。

---

## 一、问题分析

### 1.1 现状链路（已确认）

| 环节 | 位置 | 行为 |
|------|------|------|
| 触发 | `src/api/routing.py:32` | `issue_opened` 恒给 VOD（D8） |
| 执行 | `skills/vod/SKILL.md:87` 场景 B · B5 | `PATCH /issues/{number}` 将 body **收敛**为模板结构 |
| 模板 | `skills/vod/references/requirement-elicit-guide.md:54-76`（§四） | 仅 目标/现状/期望行为/验收标准/优先级/来源，**无「用户原始诉求/原话」字段** |
| 覆盖结果 | GitCode Issue body | 用户原始措辞被整体替换，仅剩「来源：Issue #5」出处行 |

### 1.2 原始声音当前存留在哪（缺失矩阵）

| 位置 | 是否保留原声 | 说明 |
|------|:---:|------|
| GitCode Issue body | ❌ | **用户可见层丢失**（本方案要解决的） |
| events 表 `payload` 列 | ✅ | `src/api/event_store.py:38` 存入完整 webhook payload（含 `issue.body`/`object_attributes.description`），仅内部 DB |
| VOD prompt 注入 | ⚠️ | `src/prompt_builder.py:286` 仅注入 `Body:` 前 **500 字符** |
| event.json 档案 | ❌ | `src/api/event_archive.py:135` 只存 `input.summary`（标题级），不含正文 |

### 1.3 根因

结构化行为完全由 **Agent 依据技能文案执行**，无代码层强制保留：

1. `SKILL.md` B5 用「**收敛**为模板结构」——措辞暗示整体替换。
2. 结构化模板没有承载「用户原话」的字段，Agent 无从保留。
3. prompt 侧 `Body:` 截断到 500 字符，即便 Agent 想原样引用原文，超长部分也拿不到。
4. 内部虽有 webhook payload 留存，但不可见于用户、也不影响 Issue 展示。

> 结论：**问题不是"要不要结构化"，而是"结构化必须与原声并存"**。

---

## 二、方案设计（决策项）

### 2.1 核心决策

| # | 决策 | 说明 |
|---|------|------|
| V1 | **结构化 = 追加，不替换** | 结构化总结作为独立区块**追加**到原正文之后（或之前），原始 body 一字不改 |
| V2 | **模板新增「用户原始诉求（原话）」字段** | 审视场景 B 的结构化模板顶部原样引用用户 body 原文，作为「原始声音」的显式载体 |
| V3 | **prompt 注入全文 body（放开 500 截断）** | `issue_opened` 场景 body 完整注入，供 Agent 忠实引用原文 |
| V4 | **event.json 档案补存原始 body** | `init_event_json` 的 `input` 增加 `original_body`，内部可追溯 |
| V5 | **来源行保留双标记** | 保留 `来源：Issue #{n}，@{user}` + `结构化：VOD 审视结构化`，并增加「用户原始诉求」区块引用 |

### 2.2 目标 Issue 形态（PATCH 后 body）

```markdown
> 用户原始诉求（VOD 审视前原文，未改动）：
>
> {用户提交的完整 Issue 正文原话，逐字保留}

---

## 结构化总结（VOD 审视结构化）

## 目标
{一句话}

## 现状
{结合知识基线}

## 期望行为
{清晰描述}

## 验收标准
- [ ] {项 1}
- [ ] {项 2}

## 优先级
P0/P1/P2

---
来源：Issue #{n}，@{user}
结构化：VOD 审视结构化
```

要点：

- 顶部引用块 = **用户原始声音**（唯一权威原文，Agent 不得改写）。
- 下半部 = 结构化结论（沿用现有模板字段，供 Issue Agent 快速消费）。
- 既满足「审视结构化」目标，又满足「不覆盖原声」要求。

### 2.3 场景 A（飞书建 Issue）是否受影响

- 场景 A 是 VOD **新建** Issue，正文由 VOD 自己书写，模板含「来源：{飞书用户反馈}」。
- 为保持一致性，模板同样建议**保留每轮引导的关键用户原话**（至少保留最终采纳的需求表述），
  但**不强制**，重点约束在场景 B（外部 Issue 审视）。
- 本方案对场景 A 的改动仅为模板注释提示（可选）。

---

## 三、实现细节

### 3.1 `skills/vod/SKILL.md`（技能核心改动）

- **场景 B · B5 措辞调整**（`SKILL.md:86-87`）：
  - 旧：`PATCH /issues/{number}` 将 body 收敛为模板结构（来源标注「VOD 审视结构化」）
  - 新：`PATCH /issues/{number}` **保留用户原始正文（一字不改，作为顶部引用块「用户原始诉求」），在原始正文之后追加「结构化总结」区块**（来源标注「VOD 审视结构化」）
- **新增硬约束**（`SKILL.md:114` 硬约束区）：
  - `审视总结严禁覆盖用户原始正文`：用户提交的 Issue 原话必须原样保留在 body 顶部引用块中，不得改写、不得删除、不得替换为结构化摘要。
- 结构化产出硬约束（`SKILL.md:121`）同步更新为：建/总结 Issue 用模板（目标/现状/期望/验收/优先级/来源），**且保留用户原始诉求原话**。

### 3.2 `skills/vod/references/requirement-elicit-guide.md`（模板改动）

- **§三 step 4**（`:51`）：`PATCH /issues/{number}` 将 body 收敛为模板结构
  → 改为「PATCH 追加结构化总结，**保留原正文于顶部引用块**」。
- **§四 结构化 Issue 模板**（`:54-76`）：
  - 新增模板项「**用户原始诉求（原话）**」：审视场景下原样引用用户 body 原文。
  - 在模板注释中说明：外部 Issue 审视（场景 B）必须保留原话；飞书建 Issue（场景 A）可选择性保留关键原话。

### 3.3 `src/prompt_builder.py`（放开 body 截断）

- `:286` 当前 `body = json.dumps(...)[:500]`。
- 改法：对 `issue_opened`（及所有含 body 的事件）放开到合理上限（如 `[:8000]`），
  使 VOD Agent 能拿到完整原文用于忠实引用；其余事件类型维持现有上限不变。

### 3.4 `src/api/event_archive.py`（档案补存原声）

- `init_event_json`（`:115`）：`input` 段在 `issue_id`/`summary` 之外增加 `original_body`，
  取自 `evt` 的 `payload.issue.body` / `payload.object_attributes.description`（截断到合理长度，如 4000）。
- 供后续 Agent / 前端从 event.json 追溯用户原始声音（内容平面唯一权威，见 D10 定位）。

### 3.5 `skills/vod/SKILL.md` 来源 A 的表述同步（可选）

- 定位章节（`:26`）「来源 A：开发者在 GitCode 直接提交的 Issue（原始声音）→ 审视、引导、结构化」
  补充「**保留原始声音**」语义，使"原始声音"与"审视结构化"两个目标并列出现。

---

## 四、文件改动清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `skills/vod/SKILL.md` | B5 措辞改为「保留原正文 + 追加结构化总结」；新增「严禁覆盖用户原始正文」硬约束；结构化产出约束同步 |
| 2 | `skills/vod/references/requirement-elicit-guide.md` | §三 step 4 与 §四模板新增「用户原始诉求（原话）」；注明场景 A/B 差异 |
| 3 | `src/prompt_builder.py` | `:286` body 截断 500 → 全文（如 8000），确保 Agent 可引用原文 |
| 4 | `src/api/event_archive.py` | `init_event_json` `input` 增加 `original_body` |
| 5 | `tests/test_vod_skill.py` | 新增断言：SKILL.md 含「保留原话/原始正文/不得覆盖」等语义 |
| 6 | `tests/test_prompt_builder.py` 或 `tests/test_event_store.py` | 新增断言：issue_opened 场景 body 完整注入（不被 500 截断） |

> 说明：Issue 显示形态由 Agent 执行 PATCH 决定，代码层无需渲染改动；event.json 与 prompt 改动为保底与能力支撑。

---

## 五、验收用例

| ID | 场景 | 预期 |
|----|------|------|
| VP-1 | SKILL.md B5 措辞 | 含「保留用户原始正文/追加结构化总结」，不再出现"收敛为模板结构"式覆盖语义 |
| VP-2 | SKILL.md 硬约束 | 含「不得覆盖/不得删除用户原始正文」 |
| VP-3 | 模板含原话字段 | requirement-elicit-guide.md §四含「用户原始诉求（原话）」项 |
| VP-4 | prompt 全文注入 | issue_opened 事件 body 注入超过 500 字符仍完整（如构造 800 字符 body 断言） |
| VP-5 | 档案存原声 | `init_event_json` 后 event.json `input.original_body` 等于原 body（截断内） |
| VP-6 | 回归 | 现有 VOD/事件测试全量通过 |
| VP-7 | 实际 Issue 形态 | 以 issue #5 复测：PATCH 后 body 顶部为原话引用块 + 下方结构化总结（人工核验） |

---

## 六、实施顺序

| 步骤 | 内容 | 验证 |
|------|------|------|
| 1 | `skills/vod/SKILL.md` + `requirement-elicit-guide.md` 文案改造 | VP-1/2/3 + test_vod_skill |
| 2 | `src/prompt_builder.py` 放开 body 截断 | VP-4 + 回归 |
| 3 | `src/api/event_archive.py` `original_body` | VP-5 + 回归 |
| 4 | 新增/更新测试 | VP-1~6 |
| 5 | 以 issue #5 人工复测 | VP-7 |

---

## 七、风险与兜底

| 风险 | 兜底 |
|------|------|
| Agent 仍可能不引用原文 | 硬约束 + 模板强制字段 + prompt 全文注入三重保障；验收 VP-7 人工核验 |
| 原文过长（如 8000+ 字符） | prompt 与档案均设上限（8000/4000），超长截断但保留主要原文；模板要求 Agent 至少保留首段+结尾 |
| 覆盖既有已结构化 Issue（如 #5） | 仅对新审视生效；对 #5 可追加一次「用户原始诉求」修正（从 events.payload 找回原文），作为运维动作不做进代码 |
| 结构化总结定位被模糊 | 引用块与结构化总结分块明确（引用块在上、结构化在下），Issue Agent 读取指引不变 |
| 与 D10「event.json 非状态权威」冲突 | original_body 仅作内容平面档案，不作状态指引，符合既有定位 |
