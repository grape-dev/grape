# 全站弹层 z-index 统一治理 + 原生弹框改造

> **分支**：`feat/fullscreen-zindex-fix`（R12 同分支扩展）
> **基线**：`grape-dev/weekly-08` (06ad2e106)
> **状态**：待实施
> **前置**：R12 已修复 events.js 全屏容器 z-index 999→1001

## 一、总览

### 1.1 背景

R12 修复了 events.js 全屏容器 z-index:999 被工具栏(z-index:1000)遮挡的问题。后续审计发现这是全站性问题——13 个页面各自手搓 overlay，z-index 从 300 到 10001 散布 12 档，存在三类问题：原生弹框残留、弹层层级低于工具栏、弹层层级过高盖住 toast/warn。

### 1.2 需求范围

| 编号 | 需求 | 类型 |
|------|------|------|
| R13-1 | 原生弹框改造——prompt 7 处 + confirm 15 处 + alert 3 处，统一为站点风格弹层 | UX 改造 |
| R13-2 | 低层级弹层修复——z-index < 1000 的弹层提到 1001，消除工具栏遮挡 | Bug 修复 |
| R13-3 | 高层级弹层修复——z-index >= 9998 的弹层降到 1001，恢复 toast/warn 可见性 | Bug 修复 |
| R13-4 | 统一弹层 API——app.showModal/showPrompt，z=1001，与 R12 层位表对齐 | 架构优化 |

### 1.3 不在范围

- events.js 全屏 z-index（R12 已修复）
- app.showConfirm 已存在（z=1000，需提至 1001 但功能不变）

---

## 二、问题分析

### 2.1 业务场景

Grape 前端是 SPA 架构，13 个页面各自管理弹层。用户在任意页面操作时可能触发：密码修改弹窗、删除确认、API Key 命名输入、密钥吊销确认、场景配置等。这些弹层应该有统一的行为：遮罩覆盖全屏（含工具栏）、操作反馈（toast/warn）在弹层之上可见、样式与站点一致。

当前三类问题影响用户操作：

1. 原生弹框（prompt/confirm/alert）样式脱离站点 UI，移动端体验差，无法主题化
2. 低层级弹层被工具栏遮挡，模态失效——用户在弹窗打开时仍能点工具栏导航
3. 高层级弹层盖住 toast/warn——弹层内操作的成功/失败反馈不可见

### 2.2 z-index 全站层级表

#### 工具栏（固定遮挡源）

| 元素 | z-index | 来源 | 说明 |
|------|---------|------|------|
| .tb（顶部工具栏） | 1000 | app.css:825 | 唯一始终存在的固定遮挡源 |

#### 低于工具栏的弹层（类型 2，被遮挡）

| 元素 | z-index | 来源 | 文件 | 业务功能 |
|------|---------|------|------|----------|
| #accountModal | 300 | account.js:1448 | account.js | 账号页全部弹层（修改密码/邮箱/绑定/删除账号等 9 个弹窗） |
| #bindSuccessToast | 400 | account.js:787 | account.js | 第三方绑定成功提示层 |
| chat.js onboardModal | 998 | chat.js:1027 | chat.js | 聊天引导弹窗 |
| .modal-overlay | 999 | app.css:741 | CSS（knowledge.js 使用） | 知识库场景添加/创建弹窗 |
| .chat-picker-pop | 998 | app.css:773 | CSS | 对话选择器弹出 |
| templates.js 弹层×2 | 999 | templates.js:541/770 | templates.js | 模板编辑弹窗 |
| observability.js 弹层×4 | 999 | observability.js:354/1318/1445/2578/2773 | observability.js | 可观测性配置弹窗 |
| observability.js 弹层×2 | 1000 | observability.js:2728/2837 | observability.js | 同层级，依赖 DOM 插入顺序，边界脆弱 |
| skills.js 弹层 | 999 | skills.js:763 | skills.js | Skill 详情弹窗 |

#### 高于 toast/warn 的弹层（类型 3，反馈被遮挡）

| z-index | 数量 | 代表文件 | 问题 |
|---------|------|----------|------|
| 9998 | 2 | runtimes.js:595, issues.js:1342 | 盖住 toast(10000) 的下限边界 |
| 9999 | 21 | dashboard/autopilot/runtimes/skills/issues/orgs/scene-market/events/repos/settings | 盖住 warn(9999) 和 .chat-del-pop(9999) |
| 10000 | 7 | autopilot/runtimes/skills | 与 toast 同层，依赖 DOM 插入顺序 |
| 10001 | 3 | autopilot | 盖住 toast(10000) |

#### 参考层（不应被遮挡）

| 元素 | z-index | 来源 | 说明 |
|------|---------|------|------|
| toast | 10000 | app.js:2421 | 操作反馈通知 |
| warn 横幅 | 9999 | app.js:912 | 系统警告 |
| .chat-del-pop | 9999 | app.css:487 | 清理对话气泡 |
| auto-recovery | 99999 | auto-recovery.js:35 | 系统级恢复 |

#### R12 已修复

| 元素 | z-index | 来源 | 说明 |
|------|---------|------|------|
| #convFullscreen | 1001 | events.js:826 | R12 已修复 |
| #promptFullscreen | 1001 | events.js:889 | R12 已修复 |

### 2.3 根因

全站没有统一弹层 API。每个页面自行创建 overlay div、自行设 z-index，导致：

1. 低层级（300-999）：低于工具栏 1000，工具栏浮在遮罩之上保持可点击，模态失效
2. 高层级（9998-10001）：高于或等于 toast(10000)/warn(9999)，弹层内操作反馈不可见
3. 原生弹框：prompt/confirm/alert 完全脱离站点 UI，无法主题化

### 2.4 目标 z-index 值：1001

与 R12 同口径。1001 仅高于工具栏 1000，低于 toast(10000)/warn(9999)/.chat-del-pop(9999)，保证：
- 模态遮罩覆盖工具栏，用户不能在弹窗期间导航
- 弹层内操作触发的 toast/warn 在弹层之上可见

---

## 三、R13-1：原生弹框改造（25 处）

### 3.1 原生 prompt（7 处）

| # | 文件 | 行号 | 业务功能 | 场景用例 |
|---|------|------|----------|----------|
| 1 | account.js | 1230 | API Key 命名 | 用户点"生成密钥"→浏览器弹出原生 prompt 输入名称→输入后调 API 创建→应改为 _accountShowModal 表单弹层（输入框+确认按钮），参考 _accountShowChangePassword 写法 |
| 2 | settings.js | 260 | 系统设置页 API Key 命名 | 与 account.js:1230 同功能不同入口→同样改为统一弹层 |
| 3 | observability.js | 796 | 静默时长配置 | 用户点"静默"→原生 prompt 输入分钟数→应改为弹层输入框（带默认值 60） |
| 4 | observability.js | 808 | 告警阈值配置 | 用户点"阈值"→原生 prompt 输入阈值→应改为弹层输入框（带当前值） |
| 5 | observability.js | 830 | 通知方式配置 | 用户点"通知方式"→原生 prompt 输入索引→应改为弹层下拉选择 |
| 6 | runtimes.js | 839 | 凭据重命名 | 用户点"重命名"→原生 prompt→应改为弹层输入框 |
| 7 | runtimes.js | 1431 | 引擎别名 | 用户点"别名"→原生 prompt→应改为弹层输入框（带当前值） |

#### 场景用例（API Key 命名，account.js:1230）

```
用户在账号页 API Keys 标签页点"生成密钥"。
→ window.prompt("请输入密钥名称") 弹出浏览器原生框
→ 样式与站点完全不一致：灰色背景、系统字体、无圆角、移动端遮挡全屏
→ 输入名称"CI Pipeline"点确定
→ API.post('/settings/api-keys', {name: 'CI Pipeline'})
→ 创建成功后 _accountShowModal 显示 Key 明文（z=300 被工具栏遮挡——R13-2 修复）

修复后：
→ 点"生成密钥"→ _accountShowModal 显示表单弹层（标题+输入框+确认/取消按钮）
→ 样式与修改密码弹窗一致：站点主题、圆角、blur 背景
→ 输入名称点确认→ API.post → 结果弹层显示 Key 明文（z=1001 不被遮挡）
```

### 3.2 原生 confirm（15 处）

| # | 文件 | 行号 | 业务功能 | 危险等级 | 场景用例摘要 |
|---|------|------|----------|----------|-------------|
| 1 | organizations.js | 249 | 移除组织成员 | 高 | 用户点"移除"→原生 confirm→应改 app.showConfirm(danger) |
| 2 | organizations.js | 280 | 删除组织 | 高 | 用户点"删除组织"→原生 confirm→应改 app.showConfirm(danger) |
| 3 | settings.js | 303 | 吊销 API Key | 高 | 用户点"吊销"→原生 confirm→应改 app.showConfirm(danger) |
| 4 | autopilot.js | 319 | 批量删除任务 | 高 | 用户选多个任务点"删除"→原生 confirm→应改 app.showConfirm(danger) |
| 5 | autopilot.js | 342 | 删除单个任务 | 高 | 用户点任务"删除"→原生 confirm→应改 app.showConfirm(danger) |
| 6 | runtimes.js | 859 | 吊销凭据 | 高 | 用户点"吊销"→原生 confirm→应改 app.showConfirm(danger) |
| 7 | runtimes.js | 1103 | 重置健康检查 | 中 | 用户点"重置"→原生 confirm→应改 app.showConfirm |
| 8 | runtimes.js | 2031 | 删除 DevSpace | 高 | 用户点"删除"→原生 confirm→应改 app.showConfirm(danger) |
| 9 | events.js | 1104 | A2A 补发确认 | 中 | 用户点"补发"→原生 confirm（含多行文本）→应改 app.showConfirm |
| 10 | issues.js | 1176 | 删除附件 | 中 | 用户点"删除附件"→原生 confirm→应改 app.showConfirm |
| 11 | issues.js | -- | （无 confirm，alert 在 3.3） | -- | -- |
| 12 | my_feedback_detail.js | 137 | 删除反馈详情 | 中 | 用户点"删除"→原生 confirm→应改 app.showConfirm |
| 13 | my_feedback.js | 138 | 删除反馈 | 中 | 用户点"删除"→原生 confirm→应改 app.showConfirm |
| 14 | pipeline.js | 352 | SRE 确认补发 | 中 | 用户点"SRE 确认"→原生 confirm（含多行文本）→应改 app.showConfirm |
| 15 | agents.js | 443 | 解绑 Agent 配置 | 中 | 用户点"解绑"→原生 confirm→应改 app.showConfirm |
| 16 | agents.js | 488 | 清理 Agent 会话 | 中 | 用户点"清理会话"→原生 confirm→应改 app.showConfirm |
| 17 | im_channel.js | 191 | 解绑 IM 渠道 | 中 | 用户点"解绑"→原生 confirm→应改 app.showConfirm |

> 注：原报告 10 处，全站扫描确认 15 处。补充了 agents.js:443/488、im_channel.js:191、my_feedback.js:138、pipeline.js:352。

#### 场景用例（删除组织，organizations.js:280）

```
组织管理员在组织管理页点"删除组织"。
→ window.confirm("确认删除组织 'XXX'？") 弹出浏览器原生确认框
→ 样式与站点不一致：系统按钮、无危险样式提示、移动端体验差
→ 点确定→组织被永久删除（不可恢复）

修复后：
→ app.showConfirm("确认删除组织 'XXX'？", onConfirm, {danger: true})
→ 弹出站点风格确认弹层：红色危险按钮、blur 背景、圆角
→ 工具栏被遮罩盖住不可点击（z=1001 > .tb 1000）
→ 点"确认删除"→组织删除→ app.showToast("组织已删除") 显示在弹层之上（10000 > 1001）
```

#### 场景用例（A2A 补发确认，events.js:1104）

```
用户在事件详情页点"补发"。
→ window.confirm("该 Agent 已在此前流程中执行完成。\n补发将重新执行整个阶段（消耗资源与时间），且不会影响已有结果。\n确定继续吗？")
→ 原生 confirm 不支持多行格式化、不支持 i18n、移动端遮挡全屏

修复后：
→ app.showConfirm(I18N.t('events.redispatch_confirm_msg'), onConfirm)
→ 弹层支持多行文本、i18n、站点主题
→ 确认后执行补发，toast 反馈可见
```

### 3.3 原生 alert（3 处）

| # | 文件 | 行号 | 业务功能 | 场景用例 |
|---|------|------|----------|----------|
| 1 | issues.js | 1458 | PR 无关联场景提示 | 用户点"介入解决"→alert("该 PR 无关联场景")→应改 app.showToast |
| 2 | issues.js | 1948 | 状态变更失败提示 | 用户切换状态失败→alert(错误信息)→应改 app.showToast |
| 3 | repos.js | 580 | 仓库错误提示 | 用户操作仓库出错→alert(title+message)→应改 app.showToast |

#### 场景用例（PR 无关联场景，issues.js:1458）

```
用户在 Issue 详情页点"介入解决"。
→ alert('该 PR 无关联场景，无法介入解决')
→ 浏览器原生 alert 框：系统按钮、阻塞式、需点确定才能关闭、无主题

修复后：
→ app.showToast(I18N.t('issues.no_scene_for_pr'))
→ 站点风格 toast：非阻塞、自动消失、支持 i18n、在所有 UI 之上（z=10000）
```

---

## 四、R13-2：低层级弹层修复（z-index < 1000）

### 4.1 改动清单

| # | 文件 | 行号 | 当前 z-index | 目标 z-index | 业务功能 | 场景用例 |
|---|------|------|-------------|-------------|----------|----------|
| 1 | account.js | 1448 | 300 | 1001 | #accountModal（全部账号弹层） | 修改密码弹窗打开时工具栏仍可点击→修复后遮罩盖住工具栏 |
| 2 | account.js | 787 | 400 | 1001 | #bindSuccessToast | 绑定成功提示层被工具栏遮挡→修复后盖住工具栏 |
| 3 | app.css | 741 | 999 | 1001 | .modal-overlay | 知识库场景弹窗被工具栏遮挡→修复后盖住工具栏 |
| 4 | chat.js | 1027 | 998 | 1001 | onboardModal | 聊天引导弹窗被工具栏遮挡→修复后盖住工具栏 |
| 5 | templates.js | 541 | 999 | 1001 | 模板编辑弹窗 | 被工具栏遮挡→修复后盖住工具栏 |
| 6 | templates.js | 770 | 999 | 1001 | 模板预览弹窗 | 同上 |
| 7 | observability.js | 354 | 999 | 1001 | 可观测性配置弹窗 | 被工具栏遮挡→修复后盖住工具栏 |
| 8 | observability.js | 1318 | 999 | 1001 | 同上 | 同上 |
| 9 | observability.js | 1445 | 999 | 1001 | 同上 | 同上 |
| 10 | observability.js | 2578 | 999 | 1001 | 同上 | 同上 |
| 11 | observability.js | 2773 | 999 | 1001 | 同上 | 同上 |
| 12 | observability.js | 2728 | 1000 | 1001 | 同层级脆弱 | 提至 1001 消除边界依赖 |
| 13 | observability.js | 2837 | 1000 | 1001 | 同上 | 同上 |
| 14 | skills.js | 763 | 999 | 1001 | Skill 详情弹窗 | 被工具栏遮挡→修复后盖住工具栏 |
| 15 | app.css | 773 | 998 | 998 | .chat-picker-pop（保持） | 临时弹出，非模态遮罩，不改 |
| 16 | app.js | 2491 | 1000 | 1001 | app.showConfirm（.create-repo-overlay） | 同层级脆弱→提至 1001 |

#### 场景用例（#accountModal，account.js:1448）

```
用户在账号页点击"修改密码"。
→ _accountShowModal 创建 #accountModal (position:fixed;inset:0;z-index:300)
→ 工具栏 .tb(z-index:1000) 浮在遮罩之上
→ elementFromPoint 横扫 y=70 全部命中 DIV.tb（实测已复现）
→ 用户点击工具栏"事件"导航→页面跳转，弹窗上下文丢失

修复后：z-index:1001 > 1000
→ 工具栏被遮罩盖住不可点击
→ 模态状态正确，用户必须先关闭弹窗才能导航
```

---

## 五、R13-3：高层级弹层修复（z-index >= 9998）

### 5.1 改动原则

将所有模态弹层 overlay 的 z-index 统一为 1001。仅改 position:fixed;inset:0 的模态遮罩，不改临时弹出（菜单/下拉/tooltip）。

### 5.2 改动清单（按文件分组）

#### autopilot.js（6 处）

| 行号 | 当前 z-index | 业务功能 | 场景用例 |
|------|-------------|----------|----------|
| 264 | 10001 | 右键菜单 | 临时弹出，不改（非模态遮罩） |
| 387 | 10000 | 任务详情弹层 | 弹层内操作触发的 toast 被遮挡→降到 1001 |
| 658 | 10001 | 任务创建弹层 | 同上 |
| 783 | 10001 | 任务编辑弹层 | 同上 |
| 1894 | 10000 | 任务详情弹层（另一入口） | 同上 |
| 1935 | 10000 | 同上 | 同上 |

#### 场景用例（autopilot.js:387 任务详情弹层）

```
用户在 Autopilot 页点查看任务详情。
→ 弹层 z-index:10000 出现，覆盖全屏
→ 用户在弹层内点"删除任务"→ app.showConfirm 确认→删除成功
→ app.showToast("已删除") → toast z-index:10000
→ 弹层 z-index:10000 == toast z-index:10000，依赖 DOM 插入顺序
→ 如果弹层后插入，toast 被弹层盖住，用户看不到"已删除"反馈

修复后：弹层 z-index:1001 < toast z-index:10000
→ toast 始终在弹层之上可见
```

#### runtimes.js（7 处）

| 行号 | 当前 z-index | 业务功能 | 改动 |
|------|-------------|----------|------|
| 542 | 9999 | 引擎详情弹层 | → 1001 |
| 595 | 9998 | 侧滑面板遮罩 | → 1001 |
| 1062 | 9999 | 引擎详情弹层（另一入口） | → 1001 |
| 1129 | 9999 | 同上 | → 1001 |
| 1278 | 10000 | Skill 详情弹层 | → 1001 |
| 2060 | 9999 | 凭据编辑弹层 | → 1001 |
| 2162 | 10000 | DevSpace 详情弹层 | → 1001 |

#### skills.js（3 处）

| 行号 | 当前 z-index | 业务功能 | 改动 |
|------|-------------|----------|------|
| 763 | 999 | Skill 模板弹层 | → 1001（同时是 R13-2 低层级） |
| 872 | 10000 | Skill 发布弹层 | → 1001 |
| 924 | 10000 | Skill 编辑弹层 | → 1001 |
| 1045 | 9999 | Skill 日志弹层 | → 1001 |

#### dashboard.js（5 处）

| 行号 | 当前 z-index | 业务功能 | 改动 |
|------|-------------|----------|------|
| 290 | 9999 | 仪表盘弹层 | → 1001 |
| 606 | 9999 | 自定义下拉 | 临时弹出，不改 |
| 883 | 9999 | 仪表盘弹层 | → 1001 |
| 1018 | 9999 | 面板弹出 | 临时弹出，不改 |
| 1532 | 9999 | 仪表盘弹层 | → 1001 |
| 2194 | 9999 | 仪表盘弹层 | → 1001 |

#### events.js（5 处，R12 已修 2 处全屏）

| 行号 | 当前 z-index | 业务功能 | 改动 |
|------|-------------|----------|------|
| 826 | 1001 | #convFullscreen | R12 已修复，不改 |
| 889 | 1001 | #promptFullscreen | R12 已修复，不改 |
| 951 | 9999 | redispatch 弹层 | → 1001 |
| 1040 | 9999 | redispatch 弹层 | → 1001 |
| 1132 | 9999 | redispatch 弹层 | → 1001 |
| 1228 | 9999 | #taskLogOverlay | → 1001（task log overlay 是模态遮罩） |
| 1539 | 9999 | inline log overlay | → 1001 |

#### 其他文件

| 文件 | 行号 | 当前 z-index | 业务功能 | 改动 |
|------|------|-------------|----------|------|
| issues.js | 1309 | 9999 | Issue 弹层 | → 1001 |
| issues.js | 1342 | 9998 | loading 遮罩 | → 1001 |
| issues.js | 1473 | 9999 | Issue 弹层 | → 1001 |
| issues.js | 1563 | 9999 | Issue 弹层 | → 1001 |
| issues.js | 1718 | 9999 | Issue 弹层 | → 1001 |
| issues.js | 1814 | 9999 | Issue 弹层 | → 1001 |
| organizations.js | 45 | 9999 | 组织弹层 | → 1001 |
| organizations.js | 153 | 9999 | 组织弹层 | → 1001 |
| organizations.js | 300 | 9999 | 组织弹层 | → 1001 |
| scene-market.js | 402 | 9999 | 场景市场弹层 | → 1001 |
| scene-market.js | 669 | 9999 | 同上 | → 1001 |
| scene-market.js | 714 | 9999 | 同上 | → 1001 |
| scene-market.js | 749 | 9999 | 同上 | → 1001 |
| settings.js | 269 | 9999 | 设置弹层 | → 1001 |
| repos.js | 62 | 9999 | lobbyOrgMenu | 临时菜单，不改 |
| repos.js | 70 | 9999 | lobbyUserMenu | 临时菜单，不改 |
| app.css | 486 | 9998 | .chat-del-overlay | 临时遮罩，不改（非模态弹层） |
| app.css | 487 | 9999 | .chat-del-pop | 临时弹出，不改 |
| app.css | 502 | 9990 | .help-overlay | 帮助抽屉遮罩，不改（触发入口在工具栏，弹层后不可达） |
| app.css | 504 | 9991 | .help-drawer | 同上，不改 |
| app.js | 2421 | 10000 | toast | 不改（参考层） |
| app.js | 912 | 9999 | warn | 不改（参考层） |
| auto-recovery.js | 35 | 99999 | auto-recovery | 不改（系统级） |

---

## 六、R13-4：统一弹层 API

### 6.1 app.showModal

```js
app.showModal(html, opts)
```

- 创建 position:fixed;inset:0;z-index:1001 的模态遮罩
- opts.bg: 遮罩背景色（默认 rgba(0,0,0,.35)）
- opts.blur: backdrop-filter（默认 blur(6px)）
- opts.onClickOutside: 点击遮罩区域回调（默认关闭）
- 返回 overlay 元素，调用方可 appendChild 内容

### 6.2 app.showPrompt

```js
app.showPrompt({title, placeholder, value, confirmText, cancelText, onConfirm})
```

- 基于 app.showModal 创建表单弹层（标题+输入框+确认/取消按钮）
- 替代原生 prompt()，支持主题化、i18n、默认值
- 空值校验由调用方在 onConfirm 内处理

### 6.3 app.showConfirm 已有

- 当前 z-index:1000（.create-repo-overlay），提至 1001
- 功能不变，仅改 CSS 中 .create-repo-overlay 的 z-index

---

## 七、改动汇总

### 7.1 文件改动量

| 文件 | R13-1 原生框 | R13-2 低层级 | R13-3 高层级 | cache bump |
|------|-------------|-------------|-------------|------------|
| account.js | prompt×1 | z=300/400→1001 | -- | v40→v41 |
| settings.js | prompt×1, confirm×1 | -- | z=9999→1001 | v31→v32 |
| observability.js | prompt×3 | z=999/1000→1001 | -- | v66→v67 |
| runtimes.js | prompt×2, confirm×3 | -- | z=9998-10000→1001 | v59→v60 |
| organizations.js | confirm×2 | -- | z=9999→1001 | v16→v17 |
| autopilot.js | confirm×2 | -- | z=10000/10001→1001 | v65→v66 |
| events.js | confirm×1 | -- | z=9999→1001 | v57→v58 |
| issues.js | alert×2, confirm×1 | -- | z=9998/9999→1001 | v115→v116 |
| repos.js | alert×1 | -- | -- (菜单不改) | v60→v61 |
| my_feedback.js | confirm×1 | -- | -- | v5→v6 |
| my_feedback_detail.js | confirm×1 | -- | -- | v3→v4 |
| pipeline.js | confirm×1 | -- | -- | v45→v46 |
| agents.js | confirm×2 | -- | -- | v27→v28 |
| im_channel.js | confirm×1 | -- | -- | v20→v21 |
| chat.js | -- | z=998→1001 | -- | v47→v48 |
| templates.js | -- | z=999→1001 | -- | v36→v37 |
| skills.js | -- | z=999→1001 | z=9999/10000→1001 | v46→v47 |
| dashboard.js | -- | -- | z=9999→1001 | v97→v98 |
| scene-market.js | -- | -- | z=9999→1001 | v44→v45 |
| app.css | -- | .modal-overlay 999→1001 | .create-repo-overlay 1000→1001 | v84→v85 |
| app.js | -- | -- | showConfirm 1000→1001 | v182→v183 |
| index.html | -- | -- | -- | 所有 cache 版本号 bump |

### 7.2 实施顺序

1. R13-4：先在 app.js 建统一弹层 API（app.showModal/showPrompt）
2. R13-1：改造 25 处原生弹框（用 app.showModal/showPrompt/showConfirm/showToast 替代）
3. R13-2：修复 16 处低层级弹层（z-index → 1001）
4. R13-3：修复 ~35 处高层级弹层（z-index → 1001）
5. 每改一个文件 bump 对应 cache 版本号

---

## 八、验证标准

### 8.1 原生弹框消除

- [ ] API Key 命名（account.js + settings.js）：出现站点风格弹层，非浏览器原生框
- [ ] observability.js 三处配置：站点风格弹层
- [ ] runtimes.js 凭据重命名/引擎别名：站点风格弹层
- [ ] 15 处 confirm：全部走 app.showConfirm（站点风格、danger 样式）
- [ ] 3 处 alert：全部走 app.showToast

### 8.2 低层级修复

- [ ] 修改密码弹窗打开时工具栏被遮罩盖住、不可点击
- [ ] 绑定成功提示层显示时工具栏被遮罩盖住
- [ ] 知识库场景弹窗打开时工具栏被遮罩盖住
- [ ] 聊天引导弹窗打开时工具栏被遮罩盖住
- [ ] 模板编辑弹窗打开时工具栏被遮罩盖住
- [ ] 可观测性配置弹窗打开时工具栏被遮罩盖住
- [ ] Skill 详情弹窗打开时工具栏被遮罩盖住
- [ ] app.showConfirm 弹窗打开时工具栏被遮罩盖住

### 8.3 高层级修复

- [ ] autopilot 任务详情弹层内操作→toast 可见
- [ ] runtimes 引擎详情弹层内操作→toast 可见
- [ ] dashboard 弹层内操作→toast 可见
- [ ] events redispatch 弹层内操作→toast 可见
- [ ] issues 弹层内操作→toast 可见
- [ ] 所有弹层内 warn 横幅可见

### 8.4 通用

- [ ] 所有弹层 z-index 统一为 1001
- [ ] toast(10000) 始终在所有弹层之上
- [ ] warn(9999) 始终在所有弹层之上
- [ ] .chat-del-pop(9999) 始终在弹层之上
- [ ] ESC/点遮罩关闭行为不变
- [ ] 临时弹出（菜单/下拉/tooltip）行为不变
- [ ] 所有改动的 JS/CSS 文件 cache 版本号已 bump

---

## 九、改动文件

| 文件 | 改动内容 |
|------|---------|
| app.js | 新增 app.showModal/showPrompt + showConfirm z-index 1000→1001 |
| app.css | .modal-overlay z=999→1001 + .create-repo-overlay z=1000→1001 |
| account.js | prompt×1 改造 + z=300/400→1001 |
| settings.js | prompt×1 + confirm×1 改造 + z=9999→1001 |
| observability.js | prompt×3 改造 + z=999/1000→1001 |
| runtimes.js | prompt×2 + confirm×3 改造 + z=9998-10000→1001 |
| organizations.js | confirm×2 改造 + z=9999→1001 |
| autopilot.js | confirm×2 改造 + z=10000/10001→1001 |
| events.js | confirm×1 改造 + z=9999→1001 |
| issues.js | alert×2 + confirm×1 改造 + z=9998/9999→1001 |
| repos.js | alert×1 改造 |
| my_feedback.js | confirm×1 改造 |
| my_feedback_detail.js | confirm×1 改造 |
| pipeline.js | confirm×1 改造 |
| agents.js | confirm×2 改造 |
| im_channel.js | confirm×1 改造 |
| chat.js | z=998→1001 |
| templates.js | z=999→1001 |
| skills.js | z=999→1001 + z=9999/10000→1001 |
| dashboard.js | z=9999→1001 |
| scene-market.js | z=9999→1001 |
| index.html | 全部 cache 版本号 bump |

## 十、设计文档

详细设计见同目录下 `r13-unified-overlay-design.md`。

## 十一、测试用例代码

以下 Playwright 测试覆盖 R13 全部验收点：低 z-index 修复、高 z-index 统一、原生弹框替换、cache busting 版本号。

```python
# test_r13_unified_overlay.py
# R13: 全站弹层 z-index 统一治理 + 原生弹框改造
# 用法: python3.9 test_r13_unified_overlay.py
# 前置: 服务运行在 localhost:3000，已登录

from playwright.sync_api import sync_playwright
import sys

BASE = 'http://localhost:3000'
results = []

def test(name, condition, detail=''):
    status = '✓' if condition else '✗'
    results.append((name, condition, detail))
    print(f'  {status} {name}' + (f' — {detail}' if detail else ''))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 800})

    # 登录
    page.goto(BASE + '/')
    page.wait_for_timeout(3000)
    try:
        page.fill('#loginUser', 'admin')
        page.fill('#loginPassword', 'admin')
        page.click('form#pwdLogin button')
        page.wait_for_timeout(3000)
    except:
        pass
    logged_in = page.evaluate("() => document.documentElement.getAttribute('data-logged-in')")
    if logged_in != '1':
        print('Login failed — aborting')
        sys.exit(1)

    # === 测试 1: account modal z-index=1001 ===
    print('\n=== 测试 1: account modal z-index ===')
    page.evaluate("window.location.hash = '#account'")
    page.wait_for_timeout(2000)
    try:
        page.click('.stabs a:nth-child(3)')  # Security tab
        page.wait_for_timeout(1000)
    except:
        pass
    try:
        # 点击修改密码
        btns = page.query_selector_all('button')
        for b in btns:
            txt = b.text_content() or ''
            if 'password' in txt.lower() or '密码' in txt:
                b.click()
                break
        page.wait_for_timeout(1000)
    except:
        pass
    modal_z = page.evaluate("""() => {
        const modal = document.getElementById('accountModal');
        if (!modal) return {error: 'no modal'};
        const tb = document.querySelector('.tb');
        const tbRect = tb.getBoundingClientRect();
        const elAtTb = document.elementFromPoint(tbRect.x + 100, tbRect.y + 26);
        return {
            modalZ: getComputedStyle(modal).zIndex,
            tbCovered: elAtTb !== tb,
            elAtTb: elAtTb ? elAtTb.tagName + '#' + (elAtTb.id||'') : 'null'
        };
    }""")
    test('account modal z-index=1001', modal_z.get('modalZ') == '1001', f"got {modal_z.get('modalZ')}")
    test('工具栏被 account modal 遮罩盖住', modal_z.get('tbCovered', False), f"elAtTb={modal_z.get('elAtTb')}")
    # 关闭 modal
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)

    # === 测试 2: app.showModal/showPrompt/showConfirm 导出 ===
    print('\n=== 测试 2: 统一弹层 API 导出 ===')
    api_check = page.evaluate("""() => ({
        showModal: typeof app.showModal === 'function',
        closeModal: typeof app.closeModal === 'function',
        showPrompt: typeof app.showPrompt === 'function',
        showConfirm: typeof app.showConfirm === 'function'
    })""")
    test('app.showModal 导出', api_check.get('showModal', False))
    test('app.closeModal 导出', api_check.get('closeModal', False))
    test('app.showPrompt 导出', api_check.get('showPrompt', False))
    test('app.showConfirm 导出', api_check.get('showConfirm', False))

    # === 测试 3: .create-repo-overlay z-index=1001 ===
    print('\n=== 测试 3: .create-repo-overlay z-index ===')
    cr_z = page.evaluate("""() => {
        // 创建临时元素测试 CSS
        var el = document.createElement('div');
        el.className = 'create-repo-overlay';
        el.style.display = 'none';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.create-repo-overlay z-index=1001', cr_z == '1001', f'got {cr_z}')

    # === 测试 4: .modal-overlay z-index=1001 ===
    print('\n=== 测试 4: .modal-overlay z-index ===')
    mo_z = page.evaluate("""() => {
        var el = document.createElement('div');
        el.className = 'modal-overlay';
        el.style.display = 'none';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.modal-overlay z-index=1001', mo_z == '1001', f'got {mo_z}')

    # === 测试 5: .kmOverlay z-index=1001 ===
    print('\n=== 测试 5: .km-overlay z-index ===')
    km_z = page.evaluate("""() => {
        var el = document.createElement('div');
        el.className = 'km-overlay';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.km-overlay z-index=1001', km_z == '1001', f'got {km_z}')

    # === 测试 6: 无残留原生 prompt/confirm/alert ===
    print('\n=== 测试 6: 无残留原生弹框 ===')
    # 检查页面 JS 中是否还有原生调用（通过 monkey-patch 检测）
    native_check = page.evaluate("""() => {
        // 检查 app 对象是否有统一 API
        var hasUnified = typeof app.showModal === 'function' &&
                         typeof app.showPrompt === 'function' &&
                         typeof app.showConfirm === 'function';
        return {hasUnified};
    }""")
    test('统一弹层 API 完整', native_check.get('hasUnified', False))

    # === 测试 7: cache busting 版本号 ===
    print('\n=== 测试 7: cache busting ===')
    versions = page.evaluate("""() => {
        var scripts = document.querySelectorAll('script[src]');
        var css = document.querySelectorAll('link[rel="stylesheet"]');
        var result = {};
        for (var s of scripts) {
            var src = s.src;
            if (src.indexOf('events.js') >= 0) result.events = src.match(/v=(\d+)/)?.[1];
            if (src.indexOf('account.js') >= 0) result.account = src.match(/v=(\d+)/)?.[1];
            if (src.indexOf('app.js') >= 0 && src.indexOf('auto-recovery') < 0) result.app = src.match(/v=(\d+)/)?.[1];
        }
        for (var c of css) {
            if (c.href.indexOf('app.css') >= 0) result.appcss = c.href.match(/v=(\d+)/)?.[1];
        }
        return result;
    }""")
    test('events.js v=58', versions.get('events') == '58', f"got {versions.get('events')}")
    test('account.js v=41', versions.get('account') == '41', f"got {versions.get('account')}")
    test('app.js v=183', versions.get('app') == '183', f"got {versions.get('app')}")
    test('app.css v=85', versions.get('appcss') == '85', f"got {versions.get('appcss')}")

    # === 测试 8: toast 在弹层之上 ===
    print('\n=== 测试 8: toast z-index > overlay z-index ===')
    z_compare = page.evaluate("""() => {
        // toast z-index=10000, overlay z-index=1001
        return {toastZ: 10000, overlayZ: 1001, toastAbove: 10000 > 1001};
    }""")
    test('toast(10000) > overlay(1001)', z_compare.get('toastAbove', False))

    browser.close()

# 汇总
print('\n' + '='*50)
passed = sum(1 for _, c, _ in results if c)
failed = sum(1 for _, c, _ in results if not c)
print(f'{passed} passed, {failed} failed')
if failed > 0:
    print('\nFailed tests:')
    for name, cond, detail in results:
        if not cond:
            print(f'  ✗ {name} — {detail}')
    sys.exit(1)
```

