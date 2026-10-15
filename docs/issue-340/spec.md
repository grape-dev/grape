# Spec: Issue #340 — 解除托管时同步清空仓库全部关联数据

> 来源：Issue #340 + Issue Agent Spec（comment 193875494）| 优先级 P1 | 目标分支 mys-local

## 目标
解除托管（平台侧手动解除 / 代码仓侧删除仓库）时，级联清空该仓库在平台内的全部关联数据（需求中心 Issue 卡片、流水线/事件记录、项目知识基线、场景关联、webhook 订阅等），以 `repo_key` 为索引键。

## 现状缺口（代码走查结论）
- 现有 `_cleanup_repo_data`（src/api/routes/repos.py:1068）未清理：`issue`（需求中心卡片）、`issue_state`、`event_thread`、`agent_tasks`/`task_messages`、`agent_shared_knowledge`/`scene_knowledge_ref`、`devops_pipeline_scene`（场景关联）、`event_route_config`；
- 各表清理独立 try/except，无事务原子性，可能产生半清理状态；
- 无审计日志；
- webhook 未处理仓库删除事件（GitCode 不发删除 webhook，靠 `/repos/member` 探活兜底，但探活走旧清理函数，同样缺表）；
- 解除托管弹窗文案未明确"将清空全部相关数据"。

## 范围
1. 触发路径一：平台"仓库设置 → 解除托管"（POST /repos/remove）触发级联清理；
2. 触发路径二：代码仓侧删除仓库 → webhook 仓库删除事件 + 探活轮询双通道触发等价清理；
3. 清理范围（repo_key 索引）：需求中心（issue/issue_state/issue_agent_session）、事件流水线（events/event_skipped/event_thread/event_route_config/agent_tasks/task_messages）、会话（chat_*/devops_agent_session/vod_conversations）、知识（agent_shared_knowledge/scene_knowledge_ref + workspace 知识基线文件）、场景关联（devops_pipeline_scene）、技能（skill 表 + skills/repo 目录）、IM 绑定（repo_im_group_binding）、onboarding 标记（meta）；
4. 弹窗告知"将清空全部相关数据"（locales/zh|en settings.json）；
5. 事务化清理（DB 单事务 + 文件系统补偿）+ 审计日志（audit_logs 哈希链）；
6. 自动化测试覆盖两条触发路径、事务回滚、dry-run。

## 验收标准
见 Issue #340 Spec（全部沿用）。

## 关键设计决策（沿用 Spec）
- 同步阻塞 + 失败可重试（DB 事务回滚保证无半清理；文件清理用 meta 表 pending 标记补偿）；
- 删除感知 = webhook 事件（实时）+ 探活轮询（兜底）双通道；
- DB 分表单事务 + 文件系统补偿机制；
- 弹窗文案走前端 i18n locales。
