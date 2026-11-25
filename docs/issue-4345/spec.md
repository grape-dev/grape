# Issue #4345 需求文档：A2A 人工「重派」基址解析与错误映射修复

> 来源：Issue #4345 正文 + Issue Agent 发布的 Spec（评论 id 203660869）。本文只固化口径，不重复原文。

## 1. 问题

人工「重派」A2A failed 事件时，`redispatch` 分支直接 `resolve_a2a_base()` 后
`POST {base}/api/a2a`，未复用 `takeover._a2a_dispatch_base` 的候选探测语义：

- `runtime_server_url_fallback`（四进程下常落在本进程 / 非 A2A 端口）被直接
  当作派发基址，上游 4xx（400/404/405）被 `except Exception` 统一包装为本端 500；
- 成功回执非 ok 才走 409，与入口幂等/接管/自循环闸的 409 混在一起，同一按钮
  在 500 与 409 间抖动；
- 失败仍落 `admin_action(redispatch_failed)`，形成「审计有据、重派未生效」；
- 同 trace 下 redispatch 失败 error 行双写（一条带 seq、一条 `seq=''`）。

## 2. 目标与非目标

目标：

1. 重派改走候选探测（health + `POST /api/a2a` 双重校验、多候选回退），与
   takeover finish 共用同一份解析器（单向依赖、不复制第二套探活）。
2. 上游 4xx 逐档透传为 422，body 含 `candidate_base_url/source/upstream_status/reason`；
   仅 5xx/传输异常归本端 5xx；入口幂等/接管/自循环闸的 409 语义不变。
3. 失败审计保留 `redispatch_failed` 并标 `dispatch=failed` + `reason` +
   `candidate_base_url`；成功语义仅在真正派发成功时写入。
4. 同 seq 30s 幂等回放上次回执（成功/失败一致），不再 500/409 交替。
5. redispatch 相关 error 行单点 emit，`seq` 为空不落行（消除 `seq=''` 幽灵行）。

非目标：

- 不改 takeover finish 通道本身（#3910 独立单）；
- 不改 CLI 侧 `--base-url` 行为（#925/#1666/#1858）；
- 不重构 `admin_action` 全量审计模型，不清理历史事件号污染。

## 3. 验收标准

| # | 判据 |
|---|------|
| A1 | 回退基址上游 400 → 接口 422，body 含 candidate_base_url/source/upstream_status/reason，不再 500 |
| A2 | 上游 5xx / 传输异常 → 本端 5xx，原因明确标注传输层 |
| A3 | 重派成功：新 seq 接管、原事件 cancelled；失败：事件保持原失败态，审计 redispatch_failed + dispatch=failed |
| A4 | 30s 内同 seq 重复重派回执与上一次完全一致，无 500/409 交替 |
| A5 | 同 trace redispatch error 行唯一，无 `seq=''` 幽灵行 |
| A6 | 回归用例覆盖：回退基址 400、显式基址可用、幂等重放；现有测试全绿（基线已知红除外） |