# D104 gitee 实弹流水线端到端验证与重派闭环设计（2026-11-09）

## 一、背景与预期

多平台解耦分支的核心预期：**新仓库平台接入后，完整流水线（事件接入→路由→任务装配→agent 执行→结果回写→QA 审查）无需平台特判即可工作**。以 gitee 作为第一个"新平台"在实弹仓库 `gitee:example-user/grape-livefire` 完成端到端验证（2026-11-08~09）。

## 二、端到端验证矩阵（全部实证）

| 环节 | 实证 |
|---|---|
| A2A 事件接入 | events 表 seq 18/23/29/30 均自 gitee 仓库进入路由（webhook→event_pipeline） |
| 任务装配 | agent_tasks 行：`issue_id=#IKTREU`（字符串 ident 原生保形，D102-G/O）、`skill=qa-cli`、repo_key 正确 |
| issue/coding/vod | seq 18/23/29 completed（exit 0）；coding 回写 gitee 评论实证（comment id=52094548） |
| 跨场景移交 | seq 16/17 cross_scene_handoff → vod completed |
| **qa 审查** | seq 30 重派后 completed（exit 0，98 步）：读单→实跑 18 测试→回写 gitee 审查评论（comment id=52095560）→自报完成 |

结论：gitee 端到端符合预期，流水线代码零平台特判（D102-G/O 字符串 ident 修复是唯一需要的通用化改动，且为平台无关实现）。

## 三、qa 失败三连的根因与重派闭环

- seq 24/28/30 三次 qa `failed(exit_code=-1)`，result 统一为「任务被系统重启/停机中断」——**全部为验证期服务重启杀掉运行中 agent 所致（运维事件），非平台解耦缺陷**（seq 24/28 log_len=2 即启动即被杀；seq 30 log_len=12346 证明装配与派发本身成功）。
- 闭环路径：`POST /api/events/{seq}/redispatch`（events.py，#587 审查修复含管理端审计留痕 `_audit_admin_action`）→ 新任务 t_c3c7e322294b → completed。管理端重派先例 seq 27。
- **运维纪律（固化）**：重启服务前必须确认 `events` 无 `running/pending` 任务；中断任务的 result 文案即重派入口提示。

## 四、UC 与测试

- UC48：gitee（新平台）A2A 流水线端到端——字符串 ident 装配、qa 审查回写。
- UC49：重启中断语义与重派闭环——失败文案、redispatch 端点与审计、重启前队列检查纪律。
- 测试：`tests/test_uc48_gitee_pipeline_e2e.py`（源码锚点行为锁 + 纯函数断言；实弹 E2E 属运行时验证，以本设计稿验证矩阵为档案）。

## 五、边界（不做的）

- 不为 gitee 写任何平台 if/else——本验证的全部意义即"零特判接入"；后续平台接入只做平台卡 + 凭据配置。
- 不做中断任务自动重派（自动重派有重复执行风险，保留人工/管理端显式重派）。
