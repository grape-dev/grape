# 需求：派发身份「认人用 id，不用名字」（Issue #21 第 2 步）

> 触发：`github:yangaiwu/hello-blog` · Issue #21 · 任务 `t_1ddb4e8a9ab2` 零交付。
> 第 1 层（空库、令牌失效）已单独处理；本单解决第 3 层：**取令牌时按 owner 名字反查账号**，
> 同名两账号必然查错，且自动触发任务根本不写 `user_id`（约占历史任务 11%）。

## 1. 现状事实（代码级）

```
src/domains/mgmt/user/db.py::get_task_token   只看 task.repo_key；够不着 task.user_id
src/domains/mgmt/user/db.py::get_github_token 最后一层 = 按 owner 用户名反查账号：
                                              get_grape_account_by_username() 无 ORDER BY → 同名取任意一条
src/domains/runtime/pipeline/event_pipeline.py::_create_direct_task
                                              agent_tasks.user_id 恒写 0、account 恒写空
结果：同一个人两个账号（一个有名字、一个有身份）时，取到的是「没有第三方身份」的那个
```

## 2. 需求

```
R1  判定顺序：任务行历史 token → **显式配置的仓库 bot 行（链首 rc_bot）** → 任务 user_id（有效才用）
              → 事件发起人账号（actor / payload.account 反查） → 原链（仓库 bot → 平台 bot → 归属者）
R2  默认 **on**（未设置即生效）：按 id 认人是修复 #21 的必要条件，默认演练等于没修；
    开关 GRAPE_DISPATCH_USER_ID ∈ {off, shadow, on}，显式 shadow 可观察、off 可回滚；
    非法值回落 shadow（拼错时宁可不生效，也不悄悄改口径）
R3  on 才真正生效：派发侧把 id 写进 agent_tasks.user_id（account 同步写入），
    取令牌侧按 id 取个人令牌，取不到再回落原链
R4  平台边界：只有「按 id 取个人令牌」能力的平台（github / gitcode）参与；gitee/gitlab 不猜，直接回落
R5  容错：认人失败（账号库不可达、字段脏）不得阻断派发，也不得阻断取令牌
R6  有效 id 口径：空 / '0' / None 等哨兵都算「没有」（与 D105 的 ''/'0' 一致）
R7  回滚面：设 GRAPE_DISPATCH_USER_ID=off 即回到旧行为（不解析、不写 id、不取个人令牌）
R8  架构：跨域只经 `mgmt.public` 契约；连接选择（主库/域引擎）属 mgmt 自己的实现，
    runtime 不感知域/DSN/引擎；三库漏配 DSN 时显式不可用 + 一次可见告警，禁止静默回落
```

## 3. 场景（对应用例）

```
S1  off         → 返回空、无日志
S0  默认（未设置）→ on 且 applies，日志为 bound user_id=
S2  显式 shadow  → 解析出 id 但返回空，只记一条 would bind（含 mode=shadow）
S3  on          → 返回 id
S3b 非法开关值   → 按 shadow
S4  解析顺序：task.user_id 优先
S5  无效 task id → account，其次 actor
S6  都解析不到   → 空
S7  派发侧接线（on）：_dispatch_ident → (id, source, account)
S8  派发侧（shadow）：返回空 id，仍给出 source/account，行值保持 0
S9  认人抛错   → 返回三元空，不阻断
S1'  rc bot 可用 → 取 rc bot（不再看派发人）
S2'  rc 不可用（expired/undecryptable）→ 继续看派发人令牌
S10 shadow 取令牌 → 走仓库 bot，不碰个人令牌
S11 on + 有效 id → 个人令牌优先
S12 on + 个人无令牌 → 回落仓库链
S12b gitee → 无按 id 取令牌能力 → 直接回落
S13 public 契约两面（mode / dispatch_identity / resolve_actor_user_id）可用
```

## 4. 验收

```
A1  S1–S13 全绿（tests/mgmt/test_dispatch_identity_scenarios.py）
A2  shadow 下既有用例零变化（token 平面、换券授权、建树 stage 全绿）
A3  现网默认即 on：两台部署重启后即按 id 取令牌（无需再配环境变量）；
    如需观察期可显式设 shadow，出问题一键 off
A4  切 on 后：Issue #21 类任务（同人两账号）取到带身份的那个账号的令牌
```

## 5. 非目标

```
· 不改 user_id 的其它消费者（技能归属、引擎调度、统计）
· 不自动改写历史任务的 user_id（只对新派发生效）
· 不改 github/gitcode 的 _REPO_TOKEN_GETTERS 存量 fallback 结构
```