# 需求：fail_task 回帖令牌收敛到统一入口（D37/S57 收口）

> 触发：`tests/kernel/test_d37_token_resolution_unify.py::test_s57_fail_task_uses_unified_global_token`
> 长期为红——守卫早就写下「fail_task 不得二分取全局 bot token」，实现一直没跟上。

## 1. 现状事实（代码级）

```
src/domains/runtime/engine/service.py::fail_task
  401 分支里按 repo_key 前缀二分：
    if _rk.startswith("github:"): get_global_github_bot_token()
    else:                          _get_global_bot_token()      ← 还是 mgmt 私有名
  同一件事两套取值，且绕开统一入口 get_global_bot_token_for(provider)（D15 派生 meta key）
```

## 2. 需求

```
R1  fail_task 只经统一入口取全局 bot 令牌：get_global_bot_token_for(provider)
R2  provider 由 TokenResolver 的 provider_name 派生，不再按 repo_key 字符串前缀二分
R3  实现只保留一处（函数内闭包），两处调用（无 handler / handler 抛异常）共用
R4  行为不变：异常分支仍保留「全局为空 → 任务令牌」的最后一档
R5  回帖是 best-effort：统一入口异常必须降级为空串并记 warning，不得影响任务收尾
R6  跨域只走 mgmt.public 契约（不 import mgmt 私有名）
```

## 3. 场景 / 用例

```
S1  fail_task 源码：只有一处 get_global_bot_token_for；无 get_global_github_bot_token、无 _get_global_bot_token
S2  fail_task 保留 _get_task_token(task) 作为最后一档
S3  get_global_bot_token_for 对 github/gitcode 与旧包装等价（既有 D37 特征锁，复用）
S4  未知/大小写 provider：走 meta key 派生（global_bot_<provider>_token）
```

## 4. 验收

```
A1  D37 文件 6 passed（原 S57 由红转绿）
A2  新增 S1/S2/S4 用例全绿
A3  既有 token 平面 / 3713 / 场景作用域用例零变化
A4  不改回帖身份语义（仍为 bot 身份，不落到个人账号）
```

## 5. 非目标

```
· 不改 handler.get_comment_token() 的级别选择逻辑
· 不改 get_global_bot_token_for 内部派生（仅复用）
· 不动其它 fail_* 路径
