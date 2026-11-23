# 设计：fail_task 回帖令牌统一入口

> 需求：`fail-task-comment-token-requirements.md`｜用例：`tests/runtime/test_fail_comment_token_unify.py`

## 1. 改动形态（单点、最小）

```
fail_task 内（service.py，+9 行 / −14 行）：
  在 401 分支开头定义一次闭包：
    def _global_comment() -> str:
        from src.domains.mgmt.public import get_global_bot_token_for
        return get_global_bot_token_for(_expired_provider) or ""     # 异常 → "" + warning
  两处调用点改为 _comment_token = _global_comment()
    · 无 handler（原来二分取全局）
    · handler 抛异常（原来二分取全局；为空再 _get_task_token(task)）
```

为什么不抽成独立模块：只服务于 fail_task 一处，独立文件会让"谁在用"变模糊；
闭包在函数内天然私有，源码扫描型守卫（S57）也能直接锁住"统一入口出现过"。

## 2. 语义对照

```
旧：github → get_global_github_bot_token()；其它 → _get_global_bot_token()
新：provider（TokenResolver 给的 "github"/"gitcode"/…）→ get_global_bot_token_for(provider)
     · github/gitcode 与旧包装等价（D37 特征锁覆盖）
     · gitee 等 config-only 平台 → meta key global_bot_<provider>_token（D15 派生）
     · 异常 → 空串 + warning（回帖 best-effort）
     · 仍为空时（仅异常分支）→ 任务令牌
```

## 3. 风险与回滚

```
风险 1  未知 provider：统一入口按 meta 派生，取不到就是空 —— 与旧行为"走 _get_global_bot_token"
        在非 github 平台上等价（其实就是 gitcode 那套），无行为差异
风险 2  闭包捕获 _expired_provider：作用域在 401 分支内，无跨调用泄漏
回滚    单文件 revert 即可；不涉及数据/迁移
```

## 4. 架构约束

```
· 跨域仍只经 mgmt.public（此前误用私有 _get_global_bot_token，本次一并纠正）
· 无新表、无新进程、无 DSN 变更；四进程拓扑不变
```
