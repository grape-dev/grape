# Spec: Issue #865 — 引擎页面展示模型（配置模型 + 最近实际使用模型）

> 目标分支: weekly-mys | 双层实现（见 Issue #865 Spec 评论）

## Objective
Runtimes 页面卡片/详情展示每个引擎的「配置模型」（worker 注册上报 RuntimeInfo.model 落 agent_runtimes）与「最近实际使用模型」（obs_llm_usage 按 engine_id 聚合 top3）。展示优先级：配置模型 → 最近模型 → `—`。

## Scope
- A 层：/api/settings/agent-runtimes 列表与详情新增 recent_models（批量单条 GROUP BY，避免 N+1）；runtimes.js 卡片/详情展示 + i18n。
- B 层：RuntimeInfo 增 model 字段；worker 解析 hermes/opencode/codearts 本机配置（失败留空不影响注册）；agent_runtimes 增 model_name/model_source/model_reported_at；本机引擎（credential_id=0）服务端读配置兜底。

## 安全约束
仅返回模型名；base_url/api_key/环境变量值绝不返回前端。

## Acceptance Criteria
- 列表/详情可见「配置模型」；未升级 worker 的远端引擎显示 — 不报错
- 跑过任务的引擎显示「最近使用模型」top3 与最后使用时间
- 三引擎配置解析正确；解析失败不影响注册/心跳
- 明暗主题布局不溢出 + i18n 中英文
- 单测：注册带 model、解析降级、聚合无 N+1；现有测试全过
