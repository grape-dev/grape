# Spec: 沙箱平台管理入口迁移——详情页操作栏 + 设备禁用态 + 路由参数复用详情（页面删除前置 1/2）

- 日期：2026-10-29
- 状态：spec 定稿待开工
- 背景（用户规划）：**最终将去掉云沙箱页面的「沙箱平台」列表视图**（沙箱平台由生成凭据自动创建，
  列表是冗余入口）。本 spec 是删除前的第一步：把管理操作迁到「Provider 详情页」与
  「Worker 设备卡片」，让详情成为唯一必要入口后，下一步才删列表。
- 用户裁决：① 列表行按钮**暂时保留**（过渡期并存，删页面时一起收敛）；
  ② sandbox 路由带参数**完全复用现有详情界面**，不改成弹窗。

## 1. 工作一：去掉「新增沙箱平台」按钮

| 项 | 内容 |
|---|---|
| 删除 | sandbox.js:484 的 `＋ 新增沙箱平台` 按钮 |
| 保留 | `openCreateProvider()`/新增弹窗分支（编辑与新增共用弹窗，696 行三态标题）——仅删入口；后端 POST /sandbox-providers 端点不动（存量手工 Provider 编辑依赖 PUT） |
| 空态文案 | 「暂无沙箱平台」→ 引导语「生成沙箱凭据后将自动创建沙箱平台」 |

## 2. 工作二 A：Provider 详情页操作栏

位置：sandbox.js 543-570「Provider 信息卡片」标题行右侧，加按钮组（复用列表行同款
样式：文字+图标+tooltip，不裸图标）：

```
[✏ 编辑] [🩺 检查] [⏸ 禁用 / ▶ 启用]
```

- **编辑** → `openEditProvider(selectedProvider)`（现成函数，复用编辑弹窗）
- **检查** → `healthCheck(selectedProvider)`（现成）；详情页加健康结果展示位
  （标题下副行：healthy ✓ / reason，复用 `healthResults` 状态）
- **启停** → `toggleProvider(selectedProvider)`（现成）；按钮文案按
  `selectedProvider.enabled` 切换，tooltip 同列表行
- 权限口径与列表行一致（isSystem 判断等现有逻辑）

## 3. 工作二 B：Worker 设备卡片「禁用」状态

### 后端（settings.py runtime-devices 端点）

JOIN `sandbox_provider_config`（按 `runtime_credential_id`）输出两个新字段：

```json
{"provider_id": 38, "platform_enabled": true}
```

- 非沙箱凭据 / 无关联 Provider → `provider_id: 0, platform_enabled: null`

### 前端（runtimes.js renderWorkers）

- 卡片头部 `_healthBadge` 旁加**禁用徽标**（灰底「已禁用」pill）：
  `cred_type==='sandbox' && platform_enabled===false` 时显示——**独立于健康态**
  （离线+禁用两徽标并存，用户要求"即便设备是离线的也显示"）
- 卡片左边框：禁用态灰色（与在线/离线色并列判断，禁用优先）
- 平台行（`☁ platform ↗`）的跳转目标改路由参数（见工作三），`provider_id=0` 时保留原 `#sandbox` 跳转
- 删除按钮不迁移（用户确认：吊销凭据自动级联删平台+模板+沙箱，1aa679876）

## 4. 工作三：路由参数复用详情界面

### 路由格式

```
#sandbox/provider/{provider_id}     → 直达该 Provider 详情视图
```

### sandbox.js 路由解析

- 现有 hash 路由处理处（app.js 分发 `sandbox` 视图时）解析二级段：
  `#sandbox/provider/38` → sandbox 页 mount 时 `selectProvider({id: 38, ...})`
- 深链数据：`selectProvider` 现按列表行对象取全量——深链只有 id，需先 `loadProviders()`
  完成后按 id 匹配再进入详情；匹配不到 → 空态提示
- **返回按钮兼容**：详情页 `backToList`（返回列表）在带参进入时行为不变（列表暂存，
  下一步删列表时改返回 Worker 设备页——本 spec 不做，记删除步清单）

### runtimes.js 入口

设备卡片平台行 `☁ ${d.platform} ↗` → `href="#sandbox/provider/${d.provider_id}"`；
另在平台行旁加显式「详情」按钮（用户要求"添加详情按钮，点击后打开详情页面"——
按钮+可点平台名双入口，按钮带文字+tooltip）。

详情页内容零改动（平台信息卡+模板 tab+沙箱列表 tab 全复用）。

## 5. 实施顺序与验收

| 序 | 改动 | 验收 |
|---|---|---|
| 1 | 删新增按钮 + 空态文案 | 页面无「＋新增」入口；编辑弹窗从列表/详情仍可打开 |
| 2 | 详情页操作栏 | 详情页三按钮功能与列表行等价（编辑保存/检查结果/启停切换生效——禁用后 worker 60s 收敛） |
| 3 | runtime-devices 新字段 | API 返回 provider_id/platform_enabled；非沙箱设备 null |
| 4 | 设备卡片禁用徽标 | 禁用 s1 → 设备卡片显示「已禁用」（在线时显示；离线也显示）；启用后消失 |
| 5 | 路由参数 + 详情跳转 | 设备卡片点「详情」/平台名 → 直达 Provider 详情（含模板 tab 可操作）；刷新页面带参直达可恢复 |
| 6 | 回归 | node --check ×2 + 渲染冒烟 + 相关 pytest（runtime-devices 端点如有测试补 enabled 断言） |

## 6. 边界与不做

- 列表行按钮/列表视图全保留（下一步「删页面」spec 处理：backToList 改向、列表 tab 移除、
  Provider 无凭据存量入口评估——已在删除步清单）
- 后端 POST /sandbox-providers 保留
- 设备卡片「禁用」只是展示态，不做卡片上直接启停（操作仍在详情页——单一操作入口）
- i18n zh/en 同步（~8 词条：禁用徽标/详情按钮/空态引导/健康结果）

## 7. 改动量

- sandbox.js ~40 行（按钮组+健康位+路由解析）
- runtimes.js ~25 行（徽标+跳转+详情按钮）
- settings.py ~10 行（runtime-devices JOIN）
- i18n ~8 词条
- 预估半天；页面验证 + <sandbox-host> 无涉（纯前端+一个读端点）
