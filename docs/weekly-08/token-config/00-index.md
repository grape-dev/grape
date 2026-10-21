# Token 配置处理逻辑 + 过期统一框架 — 文档索引

> 本目录是 `repo-token-config-requirements.md` / `repo-token-config-design.md` 的按场景拆分版。
> 原始文档保留在 `docs/weekly-08/` 作为归档，本目录供 AI 实现时按需加载。

## 文档清单

| 文件 | 需求范围 | 场景 | 说明 |
|------|---------|------|------|
| 00-index.md | — | — | 本索引 + 场景矩阵 + 依赖关系 |
| 01-repo-token-config.md | R15 | 2.1-2.2, 8 | 仓库级 token 配置（7 步流程 + 部分失败保护 + 旧 bot 移除） |
| 02-expiry-framework.md | R16 + R21.2 | 1.2, 2.3-2.5, 7, 25-28 | 统一过期框架（检测 + 标记 + 短路 + 恢复 + per-provider key） |
| 03-health-check.md | R17 + R21.3 | 4.1-4.5, 6 | 健康检查（主动发现过期 + 超时误标记修复） |
| 04-token-resolver.md | R18 | — | 策略模式 + TokenActionHandler 职责链 + VCSProvider 接口 + 二维矩阵 |
| 05-path-b-self-loop.md | R20.1-R20.6 + R21.5-R21.7 | 12-20 | 路径 B 自循环检测（四条件 + pipeline 步骤 + session 兜底误拦） |
| 06-token-scenario-coverage.md | R20.7-R20.11 | 21-25 | 29 场景覆盖 + Repo.bot_token 统一入口 + service 层 |
| 07-token-injection.md | R20.24-R20.25 | 29-30 | token 注入链路（env_extra + GIT_ASKPASS fallback） + 脱敏全覆盖 |
| 08-token-security.md | R20.26-R20.29 | 31 | 关联仓库 token 独立获取 + OAuth refresh 分布式锁 + 飞书 user_token + redispatch |
| 09-webhook-a2a-token.md | R20.12-R20.14 | 26-27 | webhook 验证 token + A2A 鉴权 read_stored_token + 建单用户 token |
| 10-global-token-swap.md | R21.1-R21.3 | — | 全局 bot token 更换 + per-provider 过期 key + 超时误标记修复 |
| 11-r21-gaps.md | R21.4-R21.11 | — | OAuth 竞态 + PR 维度 + session 兜底 + 仓库权限定义 + GitHub 路径 B + 测试清单 |
| 12-i18n-frontend.md | R19 | 5, 10, 11 | 前端 i18n + 日志脱敏 + PAT 无续期提示 + 解绑清理 |
| 13-scenarios.md | — | 1-43 | 全部 43 个场景用例（独立于需求项，供测试参考） |
| 14-compatibility-matrix.md | R9.6-R9.8 + R22.8-R22.9 + R23.1 + R24.4 | — | 向前兼容矩阵 + 双向状态机 + F1/F2/F3 勘误 + D1 session守卫 + R15.5 webhook_secret保留 |

## token 场景分类模型（二维矩阵）

| 来源 \ 消费 | obtain（保证有效） | peek（原始值比对） |
|------------|------------------|-------------------|
| bot | 执行 token：pipeline / project_info / onboarding / events / autopilot / Issues / vcs / Repo.bot_token | A2A 鉴权：read_stored_token |
| user | 身份 token：建单 / Chat / scenes / templates verify | — |
| config | — | webhook 验证 → **不属于 token 体系** |

## 依赖关系

```
04-token-resolver（基础：策略链 + Handler + VCSProvider）
  ├── 01-repo-token-config（R15 配置流程，依赖 VCSProvider 接口）
  ├── 02-expiry-framework（R16 过期框架，依赖策略链短路机制）
  ├── 03-health-check（R17 健康检查，依赖策略链标记机制）
  ├── 09-webhook-a2a-token（A2A 鉴权，依赖 read_stored_token）
  └── 06-token-scenario-coverage（29 场景，依赖 Repo.bot_token → service 层 → TokenResolver）
       ├── 05-path-b-self-loop（自循环检测，依赖 Handler.is_self_loop_actor）
       ├── 07-token-injection（注入链路，依赖 task 行 token + env_extra）
       └── 08-token-security（关联仓库 + OAuth 锁，依赖 Repo.bot_token 独立获取）

10-global-token-swap（R21.1-R21.3，依赖 02-expiry-framework 的 per-provider key）
11-r21-gaps（R21.4-R21.11，依赖 05-path-b-self-loop 的 check_running_task）
12-i18n-frontend（R19，独立，无代码依赖）
13-scenarios（场景用例，供测试参考，无代码依赖）
```

## 实现顺序建议

1. 04-token-resolver — 基础设施（策略链 + Handler + VCSProvider + service 层）
2. 02-expiry-framework — 过期框架（标记 + 短路 + 恢复 + per-provider key）
3. 01-repo-token-config — 仓库级 token 配置流程
4. 03-health-check — 健康检查
5. 10-global-token-swap — 全局 token 更换 + 超时修复
6. 06-token-scenario-coverage — 29 场景统一到 TokenResolver
7. 05-path-b-self-loop — 路径 B 自循环检测
8. 07-token-injection — token 注入链路 + GIT_ASKPASS fallback + 脱敏
9. 08-token-security — 关联仓库 + OAuth 锁 + 飞书 + redispatch
10. 09-webhook-a2a-token — webhook 验证 + A2A 鉴权
11. 11-r21-gaps — 竞态 + PR 维度 + session + 鉴权 + GitHub + 测试
12. 12-i18n-frontend — 前端 i18n + 脱敏 + PAT 提示
