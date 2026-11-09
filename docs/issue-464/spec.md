# Spec: Issue #464 — 消息撤回前端可达性与可读性修复

> 来源：Issue #464 Spec（issue agent 已发布于 Issue 评论）｜Target Branch: `weekly-XX`

## 目标
修复 #442 上线的消息撤回功能两个前端缺陷：
1. **撤回按钮 hover 不可达**：`.msg-recall-btn` 绝对定位在气泡外左侧（`left:-58px`），气泡与按钮之间存在无 hover 命中的间隙；鼠标从气泡移向按钮跨越间隙时 `.msg.r:hover` 失效，按钮立即隐藏，无法点击。
2. **「重新编辑」白色不可读**：`.chat-body .msg.r a{color:#fff}`（#233 为蓝色气泡背景设计的全局白链接规则）命中撤回提示条 `.chat-msg-recalled` 内链接；提示条背景已被改为浅色 `var(--bg)`（#f5f6f8），白字不可读。

## 范围
- `src/api/static/css/app.css`：hover 触发区域桥接 + `.chat-msg-recalled` 链接配色覆盖。
- `tests/frontend/`：新增 CSS 静态回归测试。
- Out of scope：撤回语义（二次确认→删除+终止执行+提示占位+重新编辑追加）、后端、i18n、chat.js（CSS 足以保证可达，无需 JS 改动）。

## 验收标准
1. 鼠标气泡→按钮全程按钮保持显示、可点击（hover 命中区覆盖气泡-按钮间隙）。
2. 撤回提示条「重新编辑」浅色背景对比度 ≥4.5:1（`var(--a)` #3168e0 on `var(--bg)` #f5f6f8 = 4.64:1，实测通过）。
3. #442 既有语义不变；`node tests/frontend/test_chat_recall_442.js` 通过。
