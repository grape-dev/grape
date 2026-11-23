# Issue #3713 · 取凭据实现两遍：换券链与建树链对 rc bot 行口径不一致

> 需求口径以 Issue 评论 **#202773482**（修订）+ **#202773660**（收敛记录）为准，
> 规格全文见 Issue 评论 **#202773732**（Issue Agent 📋 Spec Document）。
> 本文只记录「实现落地形态」，不复制规格原文。

## Objective

同一仓库在「换券路由」与「建树取 token」两个入口对
`runtime_credentials(repo_key, 'bot')`（rc bot 行）给出不同答案，导致私有 path-B 仓
换券 200 但建树 token 为空 → mirror refs=0 → `git worktree add` 失败 → 平台判
`GRAPE_GIT_WRITABLE=false`（reason=`git dir not found`）→ agent 只读降级、零交付仍 completed。

目标：把「取执行凭据」收成一个决定点，私有 path-B 仓恢复可建 worktree；失败时把分类
（stage）真实带进任务错误，而不是渲染成 git 元数据只读。

## 根因（已在分支 `feat/clone-endpoint-single-source` 上核对）

```
链① 换券   repos.py::_repo_platform_token → _bot():
           直读 rc 行（守卫之外），三段判定 empty/undecryptable/expired → 424
链② 建树   agent.py → _get_task_token → get_repo_token_with_refresh → TokenResolver
           → RepoBotToken._fetch：path-B 守卫（_is_path_b → return ""）在 rc 读取**之前**
           → path-B 仓在策略链里根本读不到这一行 → 落到归属者 PAT（该 PAT 常失效 → 空）
```

分叉点是机制级的：**同一份 rc 行，两条链两种答案**。用 `bypass_path_b=True` 统一
会把 RepoBot 与 GlobalBot 两级同时解锁 → path-B 私仓拿到全局 bot 身份（身份越界口）。
所以选择「链首新增一级」而不是「给现有级加开关」。

## 落地形态

| 层 | 改动 | 说明 |
|----|------|------|
| kernel `TokenResolver` | 链首新增 `ExplicitRepoBotToken`（level=`rc_bot`） | 只读 rc 行、不做 path-B 守卫、不触碰 global bot；行不可用 → `stage()` 分类 |
| kernel `TokenResolver` | 新增 `resolve_exec_token()` + 模块级同名入口 | 唯一执行凭据选择器：两个入口共用；`TokenResult.stage` 携带分类 |
| kernel `TokenResolver` | `get_token()` / `resolve()` 链首显式失败即失败 | 行存在但空/解不开/过期 → 不落链（不把身份换成别人） |
| kernel `create_action_handler` | `rc_bot` → `RepoBotTokenHandler` | 两者共用 `managed_repos.bot_token_status` 一列，回写语义相同 |
| mgmt `repos.py` | 删除 `_repo_platform_token` 里直读 rc + 三段判定 | 改调选择器，路由只做渲染（424 + code + stage 不变） |
| mgmt `db.py` | `get_repo_bot_token_no_global` 过期语义收口 | rc 行不可用时摘掉 rc_bot/repo_bot 两级，绝不外发旧值（A2A 鉴权面） |
| runtime `repogit.py` | 新增 `worktree_credential_error()` | 建树失败后按同一选择器归因，返回 `credential_<stage>: ...` |
| runtime `agent.py` | 新增 `credential_block_result()` 并接线 | 建树失败 + 凭据不可用 → `{"_error": ..., "_started": False}`，stage 进 task errors |

分类枚举唯一定义在 kernel（`CREDENTIAL_STAGE_EMPTY/UNDECRYPTABLE/EXPIRED`），
两个消费面各自渲染：换券面 `424 + code + stage`，任务面 `credential_<stage>`。

## 验收（守卫用例）

6 条跨入口守卫 + 第 7 条鉴权面 + 两处收口，全部在
`tests/api/test_issue_3713_dual_entry_credential.py`、
`tests/kernel/test_issue_3713_action_handler_level.py`、
`tests/runtime/test_issue_3713_task_credential_stage.py` 中。

| # | 判据 | 用例 |
|---|------|------|
| 1 | 同仓两入口 token 相同 | guard1 |
| 2 | rc 行空 → 两入口同一 stage | guard2 |
| 3 | path-B + rc 有值 → 两入口都 `rc_bot` | guard3 |
| 4 | path-B + 无 rc 行 → 回落归属者，绝不落全局 bot | guard4 |
| 5 | rc 过期 → 两入口都短路失败 | guard5 |
| 6 | 幂等：连续两次 token/level 不变 | guard6 |
| 7 | `get_repo_bot_token_no_global` 不外发 rc 旧值 | 鉴权面用例 |
| + | `create_action_handler` 认识 `rc_bot` | handler 用例 |
| + | 回帖身份路径（`bypass_path_b=True`）不受影响 | 身份面用例 |

## Out of Scope

- `get_task_token`（12 处）/ `get_repo_token_with_refresh`（18 处）调用方逐个改造——它们是回归面。
- 推送链路改造（demo 双入口同值后再谈）。
- path-B 身份守卫语义变更（禁止 `bypass_path_b=True`）。
- gitcode/github 的 `_getters` 存量四级 fallback 分支（换券路由的另一半实现）保持原样。

---

## 2026-11-22 合入 `feat/clone-endpoint-single-source` 时的修订

合并方式：rebase 到 `4581feedf`（PR 分支基于更早的 `c978937cc`，直接 merge 会回退
scene_policy / user_accounts 清退等后续提交），并压成一条提交。

修订 1 · 空值 rc 行 = 无行（对齐 840431a8b）

```
原实现：rc 行存在但 value='' → 判 CREDENTIAL_STAGE_EMPTY → 两入口显式失败。
问题  ：840431a8b 定的是「空值 = 没有这行」，写侧已改「空 → 删行」，但存量空行没人清
        （本机 dev 实测 gitee:myfuture_1/innovation 仍是空值行）。判显式失败会让这些仓
        立刻 424 / credential_empty —— 正是当初 6 个 gitee 仓库被卡死的形态。
现状  ：resolve()/stage() 对空值返回 ("", "")，交回原链（path-A→全局 bot，path-B→归属者）；
        常量 CREDENTIAL_STAGE_EMPTY 保留给历史文案，不再由解析路径产生。
用例  ：guard2（空值行两入口落到同一档）+ guard2b（path-B 空值行回落归属者）。
```

修订 2 · 建树归因先判 refs（新增 `empty_repo` 分类）

```
原实现：worktree_credential_error() 只看 token 是否为空 → 空仓也会报 credential_not_configured。
现状  ：先 `repo_base_has_refs()`（git for-each-ref --count=1 refs/remotes/origin）：
          基线存在且 0 分支 → `empty_repo: 仓库还没有任何提交…（这不是凭据问题）`
          基线存在且有分支 → 仍按凭据 stage 归因
          基线不存在/探测失败 → 维持原归因（不猜）
用例  ：test_empty_repository_is_reported_as_empty_repo / test_nonempty_repository_without_credential_is_still_attributed
```

修订 3 · 范围澄清（写进验收口径，避免误判"修完了"）

```
本次只统一未登记 `_REPO_TOKEN_GETTERS` 的平台分支（gitee/gitlab）。
github / gitcode 仍走存量四级 fallback（`get_github_token` 最后一层按 owner 用户名反查），
Issue #21（github 仓）不在本 PR 覆盖范围，依赖 step-2「认人用 id，不用名字」。
鉴权面（`get_repo_bot_token_no_global`）与执行面语义仍不同：前者在 rc 不可用时摘掉
rc_bot/repo_bot 两级继续下降（不能外发过期旧值），后者硬失败。这是有意为之，已写在此。
```


### 修订 4 · 过期不再判失败（2026-11-22 二次评审 C3）

```
原：链首 rc 行标记 expired → 两入口显式失败（换券 424 / 任务 credential_expired）。
改：expired = 跳过本级 → 落下一档（RepoBot → GlobalBot → 归属者），仅记 warning。
    理由：过期是“该轮换”，不是“配置坏了”；一次标记滞后不应把能跑的任务判死。
    基线语义即如此（RepoBotToken 遇 expired return "" 后继续）。
配套：鉴权面 get_repo_bot_token_no_global 仍用 expired() 摘掉 rc_bot/repo_bot 两级，
      保证不外发已知过期的旧值（read_stored_token 不查过期）。
用例：guard5 改为“两入口都落到归属者、stage 为空”；任务侧新增“过期 + 下一档为空 → credential_not_configured”。
```
