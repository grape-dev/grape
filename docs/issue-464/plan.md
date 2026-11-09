# Plan: Issue #464 — 方案设计

## 技术方案（CSS 优先，无 JS 改动）

### 缺陷 1：hover 不可达 → 伪元素桥接 hover 命中区
`.msg.r` 无既有 `::before` 占用。为 `.chat-body .msg.r` 增加 `::before` 透明命中桥：
- 默认 `pointer-events:none`（不影响非 hover 态、不拦截气泡左侧空白区点击）；
- `.chat-body .msg.r:hover::before{pointer-events:auto}` 时激活，桥覆盖 `left:-60px;width:60px;height:100%`（≥ 按钮 `left:-58px` 偏移，覆盖气泡左缘到按钮右缘的全部间隙）。
- 链路：鼠标悬停气泡 → `.msg.r:hover` → 桥激活 → 鼠标左移进入间隙 → 命中桥（伪元素属于 `.msg.r`）→ hover 保持 → 按钮持续显示 → 到达按钮可点击。
- 按钮与桥均为定位子元素，DOM 顺序按钮在后、绘制在上层，点击仍落在按钮上，不被桥拦截。
- `.chat-msg-recalled`（已撤回提示条）本身 `display:none` 撤回按钮，桥存在但无视觉/交互影响。

### 缺陷 2：白字不可读 → 复合选择器提升 specificity 覆盖
新增 `.chat-body .msg.r.chat-msg-recalled a{color:var(--a)}`（0,4,1 > 既有白规则 0,3,1），并同步覆盖 `:link/:visited` 与 `:hover` 态（hover 背景改 `var(--b)`，避免 #233 规则的半透明白底残留）。
- 对比度实测：`var(--a)` #3168e0 on `var(--bg)` #f5f6f8 = **4.64:1**（≥ WCAG AA 4.5:1）；on #fff = 5.02:1。
- 项目仅单一 `:root` token 表（无深色 token 覆盖 `.msg` 区域），深浅外观共用该 token，均满足。

## 测试 seam
新增 `tests/frontend/test_chat_recall_464.js`（node:test 静态断言 app.css 规则文本，与既有 test_chat_recall_442.js 同风格）：
1. 桥接规则存在且 hover 态 `pointer-events:auto`、默认 `none`、覆盖宽度 ≥58px；
2. `.chat-body .msg.r.chat-msg-recalled a` 覆盖规则存在且 `color:var(--a)`，specificity 高于白规则；
3. 回归断言：#442 hover 显示规则、`.chat-msg-recalled` 按钮隐藏规则仍在。

## 提交拆分（TDD 红/绿）
1. `test(#464)`：先提交失败的静态回归测试（红）。
2. `fix(#464)`：app.css 最小实现使其通过（绿）。
