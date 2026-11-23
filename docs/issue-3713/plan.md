# Issue #3713 · 实现方案与落地顺序

目标分支（Spec 指定的代码基线）：`feat/clone-endpoint-single-source`
（main 上 `src/kernel/token_resolver.py` 尚不存在，路径为 `src/token_resolver.py`）。
开发分支：`feat/issue-3713`，PR base = `feat/clone-endpoint-single-source`。

## 一、决定点唯一化（为什么是链首一级）

| 候选 | 结论 | 理由 |
|------|------|------|
| 给 `RepoBotToken` 加 `bypass_path_b=True` | ❌ | 会**同时**解锁 RepoBot 与 GlobalBot 两级 → path-B 私仓拿到全局 bot 身份（通常不是协作者 → push 403，且看着走得通） |
| 两个调用点各读一遍 rc 行（现状） | ❌ | 就是本 Issue：两份实现、两种规则、两条链两种答案 |
| `TokenResolver` 链首新增 `ExplicitRepoBotToken` | ✅ | 单一决定点；path-B 守卫语义不动；不触碰 global bot；建树链的 12+18 处消费方自动对齐 |

链序：`ExplicitRepoBotToken(rc_bot)` → `RepoBotToken` → `GlobalBotToken` → `UserToken`。

「行存在但不可用」的语义收在策略级 `stage()`，三态分开：
无行 → 回落原链；解不开 → `undecryptable`；值被清空 → `empty`；已标过期 → `expired`。

## 二、错误面：分类统一 + 渲染分离

| 面 | 渲染 | 说明 |
|----|------|------|
| 换券（HTTP） | `424 + code=BOT_CREDENTIAL_INVALID + stage` | 前端/CLI 要能区分「该轮换」还是「该重新配置」；对外契约不变 |
| 任务（worker） | task errors 里的 `credential_<stage>: <hint>` | HTTP 语义不得漏进 worker 任务上下文；不再渲染成「git 元数据只读」 |

分类枚举只在 kernel 定义一次（`CREDENTIAL_STAGES`），两面只做渲染。

## 三、落地顺序（实做记录）

1. **旧码行为探针**（.probe_3713.py，临时脚本）：用旧码跑五个场景，实锤分叉点——
   旧码 path-B + rc 有值时：换券 `rc-bot-token` / 建树 `owner-pat`（不同值）；
   rc 空/过期时：换券 424、建树却仍给出 `owner-pat`（一边失败一边放行）。
2. **写 6+1 条守卫用例**（跨入口同日断言），在旧码上跑红（缺 `ExplicitRepoBotToken`
   / `resolve_exec_token` / `worktree_credential_error`，且行为断言本就打不中）。
3. 链首新增 `ExplicitRepoBotToken` + `resolve_exec_token` + `get_token/resolve` 链首短路。
4. 换券路由删除三段实现，改调选择器（只留渲染）。
5. 两处收口：`create_action_handler` 的 `rc_bot` 映射、`get_repo_bot_token_no_global` 的过期语义。
6. 任务面：`repogit.worktree_credential_error()` + `agent.credential_block_result()` 接线。
7. 全量回归（api/mgmt/kernel/runtime）→ 自审查 → PR。

## 四、风险与对策

| 风险 | 对策 |
|------|------|
| TokenResolver 是全平台共用敏感件 | 守卫用例覆盖链首三个失败态 + 身份面（bypass 路径）回归 + 原有 4 个套件全量回归 |
| `read_stored_token` 契约（不查过期）与鉴权面冲突 | 不下改契约，改由 `get_repo_bot_token_no_global` 显式摘掉会读 rc 的两级 |
| 任务面阻断改变「只读降级」行为 | 只在「建树返回空 **且** 凭据确实不可用」时阻断；凭据可用仍走原告警路径 |
| path-B 身份越界 | 新增一级不带任何 bypass 开关，用例源码级锁死 `bypass_path_b=` 不出现 |