# 内测申请表单重设计方案

## 1. 需求变化

后端 POST 请求体从原来的 4 字段（accountType/accountName/email/reason）扩展为：
- accountType（必填，gitcode/github）
- accountName（必填）
- email（必填，邮箱格式校验）
- identityType（必填，student/enterprise/opc/other）
- phone（选填）
- reason（选填）
- surveyData（选填，按身份类型不同结构）

核心变化：**身份类型条件分支**——用户选择身份后，动态展开不同的后续问题。

## 2. 表单流程设计

### 2.1 整体流程

```
Step 1: 基础信息（账号平台 + 用户名 + 邮箱 + 手机号）
Step 2: 身份选择（学生/企业/OPC/其他）—— 4 个大卡片
Step 3: 条件分支（按身份展开不同问题）
Step 4: 申请理由（选填）
Step 5: 提交
```

### 2.2 表单布局（独立页 beta-apply.html）

```
┌──────────────────────────────────────────────┐
│              [Grape Logo]                     │
│           申请内测                            │
│        加入 Grape 早期体验计划                 │
│                                              │
│  ─── Step 1：基础信息 ────────────────────── │
│                                              │
│  平台 *                                      │
│  [GitCode ▼]  (GitCode / GitHub)            │
│                                              │
│  用户名 *                                    │
│  [___________________________]               │
│                                              │
│  邮箱 *                                      │
│  [___________________________]               │
│                                              │
│  手机号                                      │
│  [___________________________]  (选填)       │
│                                              │
│  ─── Step 2：身份选择 ────────────────────── │
│                                              │
│  您的身份是？ *                               │
│  ┌──────────┐ ┌──────────┐                   │
│  │ 🎓 学生   │ │ 🏢 企业   │                   │
│  └──────────┘ └──────────┘                   │
│  ┌──────────┐ ┌──────────┐                   │
│  │ 💼 OPC    │ │ 📋 其他   │                   │
│  └──────────┘ └──────────┘                   │
│                                              │
│  ─── Step 3：条件分支（动态展开）────────── │
│                                              │
│  [根据身份选择展开不同问题，见 §2.3]          │
│                                              │
│  ─── Step 4：申请理由 ────────────────────── │
│                                              │
│  申请理由                                    │
│  [___________________________]               │
│  [___________________________]  (选填)       │
│                                              │
│              [提交申请]                       │
└──────────────────────────────────────────────┘
```

### 2.3 条件分支设计

#### 学生（student）

选择「学生」后展开：

```
您目前的学历是？ *
○ 本科生
○ 硕士研究生
○ 博士研究生
○ 其他

您的专业方向是？ *（多选）
□ 计算机 / 软件工程
□ 人工智能
□ 数据科学
□ 电子 / 自动化
□ 其他理工科
□ 经管 / 商科
□ 其他
```

surveyData 结构：
```json
{
  "identity": "student",
  "education": "本科生",
  "major": ["计算机/软件工程", "人工智能"]
}
```

#### 企业（enterprise）

选择「企业」后展开：

```
您所在企业的规模？ *
○ 1-10人
○ 11-50人
○ 51-200人
○ 201-1000人
○ 1000人以上

您在企业中的角色？ *
○ 开发人员
○ 技术负责人 / 架构师
○ 产品经理
○ 项目经理
○ 管理人员
○ 其他

您计划将产品用于什么场景？ *（多选）
□ 软件开发
□ AI Agent / Agent 开发
□ 自动化办公
□ 企业内部工具
□ 数据分析
□ 运维 / DevOps
□ 其他
```

surveyData 结构：
```json
{
  "identity": "enterprise",
  "companySize": "11-50人",
  "role": "开发人员",
  "useCases": ["软件开发", "AI Agent / Agent 开发"]
}
```

> 注意：后端接口定义中 enterprise 的 surveyData 没有 companySize 字段，但前端收集后可以放入 surveyData（后端不校验 surveyData 内部结构，前端传什么存什么）。如果后端需要 companySize 字段，需要同步调整后端。当前设计将 companySize 放在 surveyData 中。

#### OPC（opc）

选择「OPC」后展开：

```
您目前主要从事什么？ *
○ 软件 / IT
○ AI / Agent
○ 电商
○ 内容创作
○ SaaS
○ 咨询 / 服务
○ 其他

您目前主要使用产品解决什么问题？ *（多选）
□ 产品开发
□ 编程开发
□ Agent 开发
□ 自动化任务
□ 内容生产
□ 数据处理 / 分析
□ 运营
□ 其他
```

surveyData 结构：
```json
{
  "identity": "opc",
  "occupation": "软件/IT",
  "useCases": ["产品开发", "Agent开发"]
}
```

#### 其他（other）

选择「其他」后展开：

```
请简单描述您的身份/职业。 *
[___________________________]  (填空)

您目前主要使用 AI 产品解决什么问题？ *（多选）
□ 学习
□ 工作
□ 编程
□ 创业
□ 内容创作
□ 数据分析
□ 自动化
□ 其他
```

surveyData 结构：
```json
{
  "identity": "other",
  "description": "自由职业者",
  "useCases": ["学习", "工作"]
}
```

## 3. 交互设计

### 3.1 身份选择卡片

4 个卡片以 2x2 网格排列，选中后高亮边框 + 展开下方条件分支问题。切换身份时清空之前填写的分支答案。

```
┌──────────┐ ┌──────────┐
│ 🎓       │ │ 🏢       │
│ 学生      │ │ 企业      │
│ 学生用户   │ │ 企业团队   │
└──────────┘ └──────────┘
     ↑ 选中边框变蓝色 + 背景浅蓝

┌──────────┐ ┌──────────┐
│ 💼       │ │ 📋       │
│ OPC      │ │ 其他      │
│ 独立开发者 │ │ 其他身份   │
└──────────┘ └──────────┘
```

### 3.2 条件展开动画

选择身份后，下方条件分支区域用 `max-height` 过渡动画展开（0 → auto），展开后滚动到可见区域。

### 3.3 表单校验

| 字段 | 校验规则 | 时机 |
|------|---------|------|
| accountType | 必填，gitcode/github | 默认 gitcode |
| accountName | 必填，非空 | 提交时 |
| email | 必填，邮箱格式 | 提交时 |
| phone | 选填，不校验格式 | — |
| identityType | 必填 | 选择身份卡片时 |
| 分支问题 | 必填（单选/多选至少选一项） | 提交时 |
| reason | 选填 | — |

### 3.4 提交流程

1. 前端校验所有必填字段
2. 组装 surveyData（按身份类型构建 JSON 对象）
3. POST /api/settings/beta-apply/submit
4. 成功：显示成功弹窗 + 3 秒倒计时跳转
5. 失败：显示错误信息（如重复提交返回错误提示）

## 4. 需要修改的文件

### 4.1 beta-apply.html（独立页面，完全重写）
- 基础信息区（平台/用户名/邮箱/手机号）
- 身份选择卡片（4 个，2x2 网格）
- 条件分支区（动态展开/隐藏）
- 申请理由
- 提交逻辑（组装 surveyData + POST）

### 4.2 app.js 中的 openBetaApply / submitBetaApply（弹窗版本）
- 同步修改弹窗版表单，支持身份选择 + 条件分支
- 弹窗高度自适应（内容多时滚动）

### 4.3 settings.py 中的 BetaApplyBody（后端代理）
- 新增字段：identityType / phone / surveyData
- 转发完整 body 到 admin

### 4.4 i18n 翻译文件（可选）
- 新增表单文案的英文翻译

## 5. 与后端接口的差异说明

| 字段 | 后端接口 | 前端设计 | 说明 |
|------|---------|---------|------|
| companySize | surveyData 中无此字段 | enterprise 分支收集 | 放入 surveyData 对象中 |
| education | surveyData.education | student 分支收集 | 一致 |
| major | surveyData.major | student 分支收集 | 一致 |
| role | surveyData.role | enterprise 分支收集 | 一致 |
| useCases | surveyData.useCases | enterprise/opc/other 分支收集 | 一致 |
| occupation | surveyData.occupation | opc 分支收集 | 一致 |
| description | surveyData.description | other 分支收集 | 一致 |

所有分支数据都放入 surveyData，后端不校验 surveyData 内部结构，前端传什么存什么。companySize 是前端新增的收集字段，放入 surveyData.companySize 中。
