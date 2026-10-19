# Spec: Issue #286 — chat agent 对话框气泡左右位置调整

> 来源：Issue #286 Spec（Issue Agent 收敛，Developability: ready）
> 目标分支：weekly-06-lyz（PR base）

## 目标
将 chat agent 对话界面气泡布局调整为符合主流习惯：用户消息靠右（蓝色气泡）、agent 答复靠左（浅色气泡）。

## 现状
- `src/api/static/js/pages/chat.js` 中用户消息渲染为 `class="msg l"`（左、蓝色 `var(--a)`），agent 消息为 `class="msg r"`（右、浅色 `var(--bg)`），与主流习惯相反。
- 相关渲染点共 7 处：历史加载（463）、运行中占位（477/823）、发送消息（691/693）、renderChatMessages（866/868）。
- CSS：`app.css` `.chat-body .msg.l`（蓝、左、尾角左下）/ `.msg.r`（浅、右、尾角右下）；链接高对比度规则（#233）绑定在 `.msg.l a`；`apple-theme.css` 同理。

## 方案（Option A：互换类名分配 + 同步调整样式）
1. JS 互换类名分配：用户消息 → `msg r`，agent 消息/占位 → `msg l`（含历史会话加载、实时发送、任务恢复三条路径，保证刷新不回退）。
2. CSS 互换气泡样式归属：`.msg.r` = 蓝色右对齐（尾角右下）、`.msg.l` = 浅色左对齐（尾角左下）。
3. #233 链接高对比度规则（白字链接）跟随蓝色气泡迁移到 `.msg.r a`（app.css + apple-theme.css），同步更新对应测试选择器——功能（蓝底白链）无回归。
4. #256 草稿保持逻辑不涉及渲染类名，无改动。

## 验收
- 用户气泡右侧、agent 气泡左侧；历史会话加载后规则一致。
- 新增 tests/js/test_issue_286_chat_bubble_layout.js；更新 test_issue_233_link_contrast.js 选择器后全部通过。
