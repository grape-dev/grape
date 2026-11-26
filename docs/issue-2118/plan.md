# Implementation Plan: Issue #2118

## Overview

先用测试固定组织节点、场景文件、入口校验和技能文档契约，再以最小改动补齐代码常量、
配置文件、技能文档与组织入口校验。

## Architecture Decisions

- 组织节点 id 保留 `org_*` 语义，tag 复用 `issue/coding/qa`，因此不扩展 A2A 白名单。
- `configs/pipelines.json` 与 `ORG_SCENE_NODES` 保持同一完整节点形状。
- 组织场景写 `scene_type="org"` 与 `org_id`，入口校验同时检查组织仓库、组织场景和请求组织上下文。
- `engine_runtime_ids=[]` 作为 unbound 广播语义，不绑定具体引擎。
- 三技能只固化 #2112 的 `grape org dispatches ...` CLI 契约，不实现下派 API 本身；
  契约命令缺失时 fail-closed，禁止 curl 兜底。
- 组织运行态 API 使用 dual scope，worker 会话 token 可调用 `org repos list` 与
  `org dispatches ...`，权限继续由组织成员/管理员校验把关。

## Task List

### Phase 1: Contracts

- [x] Task 1: 新增组织场景、入口校验与技能静态失败测试。
- [x] Task 2: 新增 Issue Spec、Plan、Task 文档。

### Checkpoint: Contracts

- [x] 新测试在实现前按预期失败，证明契约先于实现。

### Phase 2: Implementation

- [x] Task 3: 补齐 `ORG_SCENE_NODES` 与 `configs/pipelines.json`。
- [x] Task 4: 组织场景写入 `scene_type=org`，组织入口校验复用组织上下文。
- [x] Task 5: 新增三份组织技能与 references。

### Checkpoint: Implementation

- [x] 新测试通过，组织 provisioning / 仓库绑定回归通过。

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| 组织节点 id 与角色校验冲突 | High | 组织场景由专用创建入口落库，不修改公开 A2A 白名单 |
| 组织入口校验误伤普通仓库 | High | 仅当仓库或场景任一具有组织属性时执行严格校验 |
| 技能文档与 #2112 CLI 契约漂移 | Med | 静态契约测试固化命令、字段和错误码；命令缺失 fail-closed |
| worker 会话 token 被组织端点拒绝 | High | 组织运行态端点改用 dual scope 依赖，具体路由保留成员/管理员校验 |
| 官方 org 模板暴露给普通仓库 | Med | 场景市场以 `scope='org'` 隔离，应用入口显式拒绝 org 模板 |
| 存量库错过一次性官方场景种子 | Med | 官方场景改为每次启动幂等补种缺失模板，不覆盖既有行 |
| 组织节点 id 与 tag 不一致导致 A2A 409 | High | `_scene_node_check`/阶段推进按 id 或 tag 匹配，`_scene_node_order` 同时暴露 id/tag 别名 |
| 组织技能 sid=0 被建单预校验误拦 | High | `scene_precheck` 在 skill 表未命中时复用 `verify_skill_materializable` 官方包门禁 |
| 组织技能未交办导致停在 org_intake | High | 三份技能显式 `grape a2a handoff`；complete_task 在 stage 未推进时按 next/handoff 兜底自动交办 |
| 组织场景 issue_opened 被 vod 路由拦截 | High | 组织场景 issue_opened 按首节点（org_vod/tag=vod 或 org_intake/tag=issue）启动，stage 同步初始化 |
| issue_opened 早于 set_issue_scene 到达 → 回落 vod | High | 路由前先等待（3×0.5s）场景绑定落库，再把解析到的 scene_id 传给 org 首节点判定 |
| org_aggregate 未全终态被提前置 pending_review / 无下一轮 | High | remaining/next_step=a2a-qa 时延迟安排下一轮聚合；达到轮次上限才转待审核 |
| 自动推进先改 stage 后派发且忽略 failed | High | 先派发并校验 status=ok 且 seq 匹配，成功后才写 stage；失败保持原节点并 `_fail_event` 留痕 |
| 存量 org 场景仍未标记 scene_type=org | High | 注册式迁移按 organization.org_scene_id + custom-org-* 前缀幂等回填，只升级 standard 行 |

## Open Questions

无。
