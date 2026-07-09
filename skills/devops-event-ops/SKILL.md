---
name: devops-event-ops
version: 2.0.0
description: |
  非 Issue/PR 事件的路由入口。收到 build_failure / deploy / release 事件时，
  加载对应的 domain skill（build-debug / deploy-guard / changelog-gen）并交给它处理。
  Issue 和 PR 事件已由 prompt 层直接路由。
  本技能不做具体业务逻辑，只做路由。
  不适用于：Issue / PR 事件、飞书 IM 对话。
triggers:
  - event_type: push
  - event_type: build_failure
  - event_type: deploy_start
  - event_type: release_created
tools:
  - curl
---

# 事件路由入口（非 Issue/PR）

部分事件的统一入口。根据事件类型加载对应的 domain skill：

## 路由表

| 收到的事件 | 加载此 skill |
|-----------|-------------|
| build_failure | 打开 `skills/build-debug/SKILL.md` |
| build_success, deploy_start, deploy_failure | 打开 `skills/deploy-guard/SKILL.md`（deploy_failure 同时参考 `incident-triage`） |
| release_created | 打开 `skills/changelog-gen/SKILL.md` |

> 加载 domain skill 后，由该 skill 的处理流程主导，本 skill 不再干预。

## 参考

调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
