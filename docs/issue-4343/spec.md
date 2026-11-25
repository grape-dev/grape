# Spec — Issue #4343（需求文档）

> 权威 Spec 由 issue agent 发布在 Issue #4343 评论（id 203626737），此处仅存
> 摘要供离线检索；实现方案见同目录 `plan.md`，进度见 `task.md`。

## 目标

- 单仓 issue 解析按 repo_key provider 前缀直选（不跨 4 平台扇出）
- 上游永久性 404（Issue Not Found）建 repo_key+ident 负缓存（TTL 60–300s，env 可调）
  并与鉴权/网络故障分流（error_code 区分）
- 调用方对 404 快速失败 + 指数退避 + 终止条件
- token-missing 类必然失败告警降噪（降 INFO / 窗口聚合）

## 关键验收

- 单请求 WARNING ≤1；负缓存 TTL 内上游调用 = 1
- 1 小时 404 请求 ≤3（调用方退避）
- 401/403/5xx 语义不回退（不得把鉴权故障缓存为不存在）
- 回归用例覆盖 5 类场景：前缀直选 / 无前缀候选聚合 / 负缓存命中与过期 /
  404 与鉴权错误分流 / 调用方退避终止
