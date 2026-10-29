# Spec: Issue #479 — 流水线页场景列写死标签覆盖后端真实场景名

> 来源：https://gitcode.com/grape-dev/grape/issues/479（bug）。目标分支：基于 weekly-09 的 feat/issue-479-scene-label

## 问题

「流水线」页需求列表「场景」列把所有 scene_id 命中写死表的条目一律显示内置标签
（最典型：default →「通用开发」），优先级高于后端按 (scene_id, repo_key) 解析出的
真实场景名。仓库 default 场景被改造/改名后（实际案例：GTDTeam/General-Product
default 场景改名「【开放能力】SDK」），流水线页显示「通用开发」，与需求列表、
需求详情页不一致。

实测复现（本机 dev 环境，改 hello-flask-2 default 场景 display_name 后调
get_pipeline 路由）：

```
issue=#999 scene_id='default' scene_name='【复现】SDK'   ← 后端字段正确
前端 pipeline.js:291 显示 -> '通用开发'（后端真实名='【复现】SDK'）
```

## 根因（已实测闭环）

- 前端 src/api/static/js/pages/pipeline.js:290-291（修复前行号）：写死映射表
  `_sceneLabels = {default, cloud-native, iac, agent-dev, skill-dev,
  api-skill-dev, blog-publish}`，取值 `_sceneLabels[_sceneId] || p.scene_name`，
  硬编码优先。
- 后端链路无缺陷：routes/pipeline.py（流水线列表）、issues_center.py（需求列表）、
  routes/issues.py（需求详情）均按 (scene_id, repo_key) 调
  get_pipeline_config（DB-first 查 devops_pipeline_scene，repo_key 作用域，
  带 60s 缓存与写失效）取 display_name 填入 scene_name 字段。

## 相关场景（受影响的展示面盘点）

| # | 场景 | 数据来源 | 修复前表现 | 修复后表现 |
|---|------|----------|-----------|-----------|
| S1 | 流水线页需求列表「场景」列，仓库 default 场景被改名 | p.scene_name（后端 display_name） | 写死「通用开发」，遮蔽真实名 | 显示真实名 |
| S2 | 同上，scene_id 为 cloud-native/iac/agent-dev/skill-dev/api-skill-dev/blog-publish 且仓库级场景被改名 | 同上 | 同类遮蔽（未暴露但同根因） | 显示真实名 |
| S3 | 仓库场景与市场模板同名（default 仍叫「通用开发」等） | 同上 | 显示写死标签（恰好正确） | 显示后端名，结果一致，无回归 |
| S4 | 后端异常/场景配置缺失导致 scene_name 为空 | p.scene_name 为空 | 显示写死标签（可能误导） | 兜底显示原始 scene_id（诚实） |
| S5 | scene_id 为空（PR 订阅等无场景条目） | — | 显示 '—' | 不变，显示 '—' |
| S6 | 需求列表页 / 需求详情页场景名 | issues_center.py / issues.py | 本就正确（display_name 优先） | 不改动，保持一致 |
| S7 | 跨场景徽章、任务页 scene_default_badge | tasks/dashboard locale | 正常（键仍有引用） | 不受清理影响 |

## 方案

纯前端展示层修复，后端零改动（后端"按 ID 查真实场景名"的链路已存在且正确）：

1. pipeline.js 删除 `_sceneLabels` 表，场景列改为
   `p.scene_name || _sceneId`（真实名优先，缺失兜底显示原始 ID）。
2. 清理 locales/{zh,en}/pipeline.json 中失去代码引用的 7 条 scene_* 文案
   （通用开发/云原生/IaC/Agent开发/Skill开发/API Skill/博客发布）。
   locales/{zh,en}/{tasks,dashboard}.json 的同名键其他页面仍在引用，不动。

## 家族扫描结论（深度核查）

同族模式 = 「按 scene_id 写死文案且优先/替代后端真实名」。全库扫描结果：

| 位置 | 模式 | 判定 |
|------|------|------|
| pipeline.js _sceneLabels（本 issue） | 写死标签覆盖后端名 | 已修复 |
| pipeline.js _renderCrossSceneBadge | 读 ev.scene_id，但 stages API latest_event 白名单曾不含该字段 → 徽章曾永远只显示「跨场景移交」不带目标场景名；后端 webhook.py:443 已解析 _target_scene_name 但只埋在 title 文本 | 同族真实缺陷，**已在本分支修复**（改动 ~30 行，见方案 3） |
| runtimes.js _SCENE_TAG_COLORS | 写死颜色表，显示名走 /runtime/scene-tags display_name（_sceneDisplayName 正确） | 仅装饰 fallback，非缺陷 |
| templates.js _PIPELINE_STAGES | 按 scene_id 写死节点标签 | 场景市场模板预览（展示模板定义本身），语义正确 |
| dashboard.js _SCENE_ICONS | 图标映射 | 仅装饰，非缺陷 |
| knowledge.js:102 / issues.js 全部 | scene_display_name \|\| scene_name \|\| scene_id | 顺序正确 |
| autopilot.js 派发弹窗场景下拉 | s.name \|\| s.id（后端 get_scenes 已返回 display_name 但未用） | 同族轻微不一致，**已在本分支修复**（UC9） |
| locales/{zh,en}/dashboard.json scene_* 7 键 | 无任何 JS 引用的死键 | 本次一并清理（补充项） |
| db_setup.py:2282 种子「通用开发」 | DB 初始种子（改名前的原始值） | 正常 |

### 跨场景移交徽章（同族缺陷，本分支已修复）

原链路断点：webhook.py 计算 _target_scene_name → 只进 title 文本；pipeline.py
stages 的 latest_event 白名单无 scene_id/scene_name；前端徽章读 ev.scene_id
恒为 undefined。

修复（复用现有数据，webhook/payload/A2A 协议零改动）：
1. routes/pipeline.py：latest_event 白名单透传事件级 scene_id（events 行本就
   携带，compute_node_statuses 塞入原始 dict，只是白名单丢了）；新增
   _scene_label_for_event 按 (scene_id, repo_key) 解析真实场景名（复用
   get_pipe_cfg 缓存回调，异常吞掉返回空串——纯展示层不影响主数据）。
2. pipeline.js _renderCrossSceneBadge：crossSceneName = ev.scene_name ||
   ev.scene_id || ''（真实名优先，兜底原始 ID）。

兼容性：latest_event 为纯增量字段，老前端不读不坏。

## 场景用例（验收时在目标环境执行）

| 用例 ID | 场景 | 步骤 | 预期 |
|---------|------|------|------|
| UC1 | S1 改名 default | 仓库应用「通用开发」模板 → 编辑 default 场景改名 → 建需求触发流水线 → 打开流水线页 | 场景列显示改名后的名称，与需求列表/详情一致 |
| UC2 | S2 改名非 default 场景 | 对 cloud-native 等场景做同样操作 | 同 UC1 |
| UC3 | S3 模板同名 | 未改名的仓库查看流水线页 | 场景列显示与之前一致（通用开发等），无回归 |
| UC4 | S4 scene_name 缺失 | 构造场景配置不可得的条目 | 场景列显示原始 scene_id，不显示可能错误的中文标签 |
| UC5 | S5 无场景 | 查看 PR 订阅类条目 | 场景列显示 '—' |
| UC6 | S7 回归 | 打开任务页/看板 | scene_default_badge 等文案正常，未受 locale 清理影响 |
| UC7 | 行为契约 | （pytest 自动化）种改名场景 + 事件 → 调 get_pipeline | 响应 scene_name == 改名后的 display_name |
| UC8 | 跨场景移交徽章 | 仓库 A 的 Issue 触发跨场景移交到改名后的场景 → 打开流水线页看该 Issue 流程区 | 徽章显示「跨场景移交 → <目标场景真实名>」而非裸文案；latest_event 携带 scene_id/scene_name |
| UC9 | Autopilot 派发弹窗场景下拉 | 场景 display_name 与 name 不同的仓库 → 打开 Autopilot 派发弹窗看场景下拉 | 下拉优先显示 display_name（与其他页面场景名展示一致），value 仍为场景 ID |

对应自动化测试：tests/test_issue_479_scene_label.py
（T1-T3 源码检查类 + UC7 行为契约类，见 task.md）。
