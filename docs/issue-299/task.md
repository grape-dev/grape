# Task: Issue #299 — 优化工作台我的组织展示

## 任务拆解

### Task 1: index.html — tbOrgBtn 改造
- [ ] 添加 `id="tbOrgName"` 到 span
- [ ] 添加下拉箭头 SVG（与 lobby 一致）
- [ ] 按钮样式微调（border 0.5px、rounded-lg）

### Task 2: app.js — `_updateTbOrgName()` 实现
- [ ] 读取 current_org_id
- [ ] API.get('/orgs') 获取组织列表
- [ ] 填充当前 org name 到 #tbOrgName
- [ ] 回退显示 i18n 文本

### Task 3: tbOrgMenu 样式微调
- [ ] border-radius → 12px（rounded-lg）
- [ ] border → 0.5px

### Task 4: 验证
- [ ] 页面加载后显示当前组织名
- [ ] 切换组织后名称更新
- [ ] 下拉面板样式统一
