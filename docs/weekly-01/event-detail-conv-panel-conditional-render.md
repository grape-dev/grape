# 事件详情页对话面板条件渲染方案（Issue #35）

> 版本：v1.0 ｜ 日期：2026-09-05
> Issue：#35「对话产出的记录在仓库所有的事件都展示，同时还占据了 event detail 的较大版面，导致没法查阅 Agent 处理记录和答复结论」
> 状态：方案 A（条件渲染）——待评审后实施

---

## 一、问题复现（生产实况）

| 指标 | 值 |
|---|---|
| 事件总数 | 1679 |
| 有对话记录的事件（`event_thread.anchor_seq`） | **16** |
| 有对话事件占比 | **1.0%** |

→ 其余 99% 事件（a2a_event、pull_request_*、issue_opened 等纯流水线事件）在详情页**无对话**，
却仍显示右侧空对话面板，压缩了左侧「Agent 处理记录与答复结论」的展示空间。

---

## 二、根因（前端布局层）

**`src/api/static/js/pages/events.js:79`** 硬编码两列 Grid：

```js
<div style="display:grid;grid-template-columns:1fr 300px;gap:14px;height:calc(100vh - 160px);overflow:hidden">
  <!-- 左栏：Agent 处理记录与答复结论 (flex:1) -->
  <!-- 右栏 convPanel：对话面板，固定 300px -->
</div>
```

- `grid-template-columns: 1fr 300px` **无条件**为右栏分配 300px，即使该事件无任何对话。
- `:154` `loadConversation()` 每次渲染都调用；`:258` 无消息时显示占位「开始对话」，空面板常驻。
- 左栏「Agent 处理记录与答复结论」（`:134-139`）含最长 3000 字符 `result` + `structuredHtml`，空间被压缩，核心内容难以查阅。

**后端正确**：`get_event_thread`（events.py:955）按 `anchor_seq` 查 `_list_thread`，
无记录返回 `messages=[]`，问题纯在前端布局层。

---

## 三、方案 A：条件渲染（本方案）

### 3.1 思路

**有对话才显示右栏对话面板；无对话则左栏满宽 `1fr` 单列**，Agent 处理记录与答复结论占满全宽。

- 渲染顺序：先渲染左栏单列 → `loadConversation()` 拿到 `messages` 后再决定是否切双列。
- 有对话（`messages.length > 0`）→ 动态改 `grid-template-columns: 1fr 300px`，显示 convPanel。
- 无对话 且 门禁关闭（`gate.closed`，即 issue 已 done / 无对话价值）→ 保持单列，隐藏右栏。
- 无对话 但 门禁开放（`gate.open`，对话刚开放、还没消息）→ **可保留右栏**（提示「开始对话」），
  因为此时对话是被鼓励的入口，不该隐藏。

### 3.2 改动点

| 文件 | 改动 |
|---|---|
| `src/api/static/js/pages/events.js` | ① `renderEventDetail`：外层 Grid 默认 `1fr`（单列），convPanel `display:none` 起始 ② `loadConversation`：拿到 `d.messages` 后条件切换：`messages.length>0 || gate.open` → 双列 + 显示 panel；否则保持单列隐藏 ③ 全屏切换（toggleConvFullscreen）逻辑对齐：无对话时全屏按钮也应隐藏/禁用 |
| `src/api/static/i18n`（zh/en） | 无需新增 key（复用 `events.conv_start_hint` 与既有面板标题） |

### 3.3 交互细节

- **有对话事件**：与现状一致，右侧对话面板同宽 300px。
- **纯流水线事件**：左栏占满，无对话面板、无「开始对话」占位，`convInputRow` 不显示。
- **门禁开放但空对话**：保留面板（提示开始对话），因为这是用户可主动发起的会话入口。
- **布局切换无闪烁**：`loadConversation` 是异步（AJAX），切换在拿到数据后一次性设置，避免先双列后变单列的跳动。

---

## 四、测试

### 4.1 前端（手工回归）

| 场景 | 预期 |
|---|---|
| 有对话事件（如 2012/2014/1750 等） | 右栏显示，对话消息正常，`1fr 300px` |
| a2a_event / pull_request 等纯事件（99%） | 左栏满宽 `1fr`，无右栏、无「开始对话」占位 |
| 无对话但 gate.open（issue 未结束） | 右栏保留，提示「开始对话」，输入框可用 |
| 全屏对话按钮 | 无对话时不显示全屏按钮（或禁用） |
| 左右导航 ‹ › / prev/next | 切换到不同事件时布局按各自有无对话正确切换 |

### 4.2 后端

无需后端改动；`get_event_thread` 维持返回 `messages`/`gate`。

---

## 五、影响面与风险

| 项 | 说明 |
|---|---|
| 影响 | 仅 `events.js` 前端布局逻辑；不改后端、不改数据结构 |
| 风险 | 低——纯展示层；无对话事件从「空面板」变为「满宽」，信息不丢失 |
| 兼容 | `toggleConvFullscreen`/`loadConversation`/`renderOutcome` 其余逻辑不变 |

---

## 六、可选项（非本 issue 必须）

- **Q2：result 长文本折叠** —— 左栏「答复结论」对 >N 字 result 默认折叠、点击展开，
  进一步优化长文本查阅。可作后续增强单独评审。

---

*方案 A 待评审；确认后实施（改动 events.js + 手工回归 + 部署）。*