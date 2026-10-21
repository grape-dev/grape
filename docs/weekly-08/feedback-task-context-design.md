# 问题反馈自动携带任务上下文设计（feedback-task-context-design.md）

## 1. 背景与诉求

当前问题反馈（顶栏「问题反馈」弹窗，Issue #174）只提交：分类/标题/描述/page_url/repo_key/附件。
定位问题所需的三个关键信息缺失：

- 出问题的**仓库**（repo_key 有，但用户手动提交时常为空或不准）
- 出问题的**任务编号**（agent_tasks.id，完全没有）
- 当时运行任务的**引擎**（runtime/backend + 引擎实例 device/ip，完全没有）

诉求：
1. 反馈弹窗增加「任务上下文」信息区（额外的信息输入框思路的落地形态）。
2. **任务失败时**，event 界面该事件行出现「问题反馈」按钮，点击时自动搜集对应上下文并带入反馈弹窗。

## 2. 总体方案（一句话）

失败事件行加「反馈」按钮 → 前端带 event_seq 调新接口 `GET /feedback/context/{event_seq}` → 服务端权威搜集（仓库/issue/agent/任务id/引擎实例/健康分/失败摘要）→ 弹窗展示只读上下文卡片 + 原有输入项 → 提交时随 FeedbackBody 落库 user_feedback 新增列 → grape-admin 详情页/列表页展示并支持按仓库/任务id搜索。

关键原则：**上下文由服务端按 event_seq 现查现返回，不信任前端拼装**（前端只传 seq；repo/engine 等全部服务端取自 DB，杜绝伪造/过期）。

## 3. 数据库变更（grape_test_mys.user_feedback）

加 5 列（均 nullable，旧行为完全兼容）：

| 列 | 类型 | 说明 |
|---|---|---|
| event_seq | int NULL | 触发反馈的事件 seq |
| task_id | varchar(191) NULL | agent_tasks.id |
| issue_number | varchar(32) NULL | issue 编号（展示用，issue 表关联键为整型 id，另存 issue_id int） |
| agent_type | varchar(32) NULL | vod/issue/coding/qa/sre/chat |
| engine_info | text NULL | JSON：{runtime, backend, engine_device, engine_ip, engine_name, health_score, task_status} |

注：issue_id 也加一列 int NULL（card 主键，`--issue_id` 用），与 issue_number 并存（一个是内部id一个是GitCode展示号）。

建表语句走 db_setup.py 既有 ADD COLUMN IF NOT EXISTS 迁移模式（对齐现有表演进写法）。

## 4. grape 服务端（src/api/routes/feedback.py + events.py 参考）

### 4.1 新接口 GET /api/feedback/context/{event_seq}

- 鉴权：get_current_user（与提交一致）。
- 复用 events.py `_load_event(seq)` + 引擎归因逻辑（agent_tasks.runtime_id → 场景节点 → devops_agent_session，即 events 详情已实现的同一段），返回：

```json
{
  "event_seq": 123,
  "repo_key": "grape-dev/grape",
  "issue_id": 45, "issue_number": "379",
  "agent_type": "coding",
  "task": {"task_id": "tsk_xxx", "task_status": "failed",
           "exit_code": 1, "started_at": "...", "completed_at": "..."},
  "engine": {"runtime": "hermes", "backend": "hermes", "engine_instance": "dev-mac",
             "engine_device": "MacBook Pro", "engine_ip": "192.168.0.9",
             "health_score": 87},
  "fail_summary": "result_text 前 500 字（失败原因，events 详情同源）"
}
```

- 事件不存在/无权限 → 404/403（与 events 详情一致）。

### 4.2 POST /api/feedback 扩展

FeedbackBody 增加可选字段：`event_seq: int = 0`。
- 提交时若带 event_seq，服务端**再次现查**一遍 4.1 的上下文（不信任前端传回的引擎信息），将 repo_key/task_id/issue_number/issue_id/agent_type/engine_info(JSON) 写入新列；前端传的 repo_key 仅在无 event_seq 时生效（保持旧路径）。

## 5. grape 前端（static/js/pages/）

### 5.1 events.js

- 失败事件行（`data.status === 'failed'`，即 `_redispatchVisible` 同一判定处）追加按钮：

```
<button class="bt bt-o bt-s" onclick="openFeedbackDialog({event_seq: SEQ})">反馈</button>
```

- 按钮文案用 i18n key `events.btn_feedback`，中文「反馈」，缺失时回落中文（新文案直接中文，对齐 I18N 陷阱约定）。

### 5.2 feedback.js — openFeedbackDialog(opts)

- 签名改为 `openFeedbackDialog(opts)`，opts 可省（顶栏入口行为不变）；顶栏按钮调用处同步加 `()` 空参（已兼容）。
- opts.event_seq 存在时：弹窗打开瞬间先调 `GET /feedback/context/{seq}`，在类型选择按钮**上方**渲染一张只读「任务上下文」卡片（浅灰底 rgba(0,0,0,.04) 圆角10px，iOS 风格，样式 inline 对齐现有弹窗写法）：

```
📋 任务上下文（自动搜集，只读）
仓库   grape-dev/grape
Issue  #379
任务   tsk_xxx（failed，exit 1）
引擎   hermes · dev-mac · 192.168.0.9 · 健康分87
失败摘要 result 前200字…（可折叠）
```

- 加载失败：卡片显示「上下文获取失败」但**不阻塞**用户继续手填提交（此时 event_seq 仍会随请求带上，服务端会再试一次现查）。
- 提交 body 增加 `event_seq`；category 默认仍 bug。
- 顶栏手动打开（无 event_seq）：不显示上下文卡片，但保留现有 repo_key 采集逻辑 —— 与原行为零差异。

## 6. grape-admin（Java + Vue）

### 6.1 后端 FeedbackController

- 列表 SQL 的 SELECT 列表增加 event_seq, task_id, issue_number, agent_type, engine_info；DTO 透传（engine_info 原样传 JSON 字符串，前端解析）。
- 搜索参数增加 `task_id`、`repo_key` 精确/前缀匹配（where 拼接处加两个可选条件）。

### 6.2 前端

- FeedbackManage.vue：表格增加「仓库」「任务」「引擎」三列（任务列显示 task_id 前 8 位 + 状态色点；引擎列显示 runtime + engine_instance）；筛选区加「按任务ID/仓库搜索」输入框。
- FeedbackDetail.vue：在 repo_key 详情项之后追加详情项：Issue 编号、agent_type、任务ID（+状态）、引擎信息（runtime/实例/设备/IP/健康分，engine_info 解析后分行展示）、失败摘要；task_id 提供跳转工作台任务日志的提示文案（跨系统跳转链接本期不做，只展示 ID——工作台 URL 属部署环境配置，避免硬编码）。

## 7. i18n

新 key 全部中文直落 locales（`events.btn_feedback`、`feedback.context_*` 系列），不依赖 t() 回落链（I18N t() 找不到返回 key 本身的陷阱：新文案一律中文原文兜底 `|| '中文'`）。

## 8. 兼容与风险

| 项 | 评估 |
|---|---|
| 旧反馈 | 新列全 nullable，历史行不受影响；admin 读旧行新列显示「—」 |
| 无 event_seq 的反馈 | 走原路径，零变化 |
| 前端伪造 | 服务端按 seq 现查，前端传的引擎信息不落库 |
| admin 与 grape 双写 | 无：user_feedback 仍只有 grape 工作台写，admin 只读（现状不变） |
| 事件未派发/无任务 | context 返回 task=null/engine=null，卡片对应行显示「未派发」，仍可提交 |

## 9. 验收清单

1. 顶栏「问题反馈」：行为与现状一致（回归）。
2. events 页：失败事件行出现「反馈」按钮；成功/运行中事件不出现。
3. 点击按钮 → 弹窗顶部出现上下文卡片，字段与 events 详情页该事件的仓库/任务/引擎一致。
4. 提交后 MySQL user_feedback 新行包含 event_seq/task_id/agent_type/engine_info/issue_number。
5. admin 反馈列表新列可见、可按任务ID/仓库搜索；详情页完整展示引擎与失败摘要。
6. admin 打开一条**旧反馈**：新列显示「—」，无报错。

## 10. 改动文件清单

grape：
- src/api/db_setup.py（user_feedback 加列迁移）
- src/api/routes/feedback.py（context 接口 + 提交扩展）
- src/api/static/js/pages/events.js（失败行反馈按钮）
- src/api/static/js/pages/feedback.js（opts + 上下文卡片）
- src/api/static/locales/*（i18n 新 key）
- （引擎归因逻辑从 events.py 提为可复用函数或直接 import 复用，避免复制粘贴）

grape-admin：
- backend FeedbackController.java（SQL + DTO + 搜索）
- frontend/src/views/FeedbackManage.vue、FeedbackDetail.vue
