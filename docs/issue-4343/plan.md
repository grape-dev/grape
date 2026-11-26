# Plan — Issue #4343 单仓 issue 解析前缀直选 + 404 负缓存 + 调用方快速失败

> 基线分支：`arch-weekly-11-ljt` ｜ 开发分支：`feat/issue-4343`
> Spec：Issue #4343 评论（issue agent 203626737）

## 现状根因（代码取证）

一次 `GET /api/issues/{owner}/{repo}/{n}/pipeline`（issue 不存在、内部未登记）的解析链：

1. `_vcs_issue_lookup`（`src/domains/runtime/routes/issues.py:3312`）遍历全部
   `_PROVIDER_MODULES`（4 平台）逐 provider 上游 GET——每 provider 404 在
   `src/api/git_repo_api.py:646` 落 1 条 WARNING（404 被当上游错误告警）。
2. `_sync_missing_issue_row`（`routes/issues.py:3104`）遍历 `repo_key_candidates`
   候选集（裸键 + 全平台前缀，**即使 repo_key 已带 provider 前缀也扇出**）逐键
   `fetch_issue_via_vcs` → `_vcs_get_issue`：
   - token 解析 `TokenResolver` → `src/kernel/token_resolver.py:632` 全局 bot token
     缺失 WARNING **每候选 provider 1 条**；
   - `VcsApiClient.get_issue` 404 → `src/domains/runtime/vcs/api_client.py:1115`
     WARNING 每 repo_key 1 条。
3. 单次 ≈ 5 条 WARNING；调用方（worker 侧 agent 按 runbook 20s 间隔轮询 pipeline
   查询）99 次/小时 × 5 ≈ 495 条 WARNING/小时，与生产实测精确吻合。

**注意**：#3891 已合入的 404→200 回落（`_resolve_issue_presence`，外部托管仓
issue_id=null 返回 200 + diagnostics）只改**响应语义**，不改**解析链扇出与告警口径**
——本 Issue 与其不重叠，负缓存作用于同一解析链的上游调用面。

## 改动设计（4 项，对齐 Spec In Scope）

### 1. 解析入口前缀直选（routes/issues.py）

- `_repo_keys_of(owner, repo)`：owner/repo 带 provider 前缀（`gitcode:` 等，
  白名单 = `provider_prefixes()`）→ **只返回单键**（前缀即权威，Spec 设计决策）；
  无前缀保留原候选集。
- `_vcs_issue_lookup`：owner 带前缀 → 只探测该 provider；逐候选失败**聚合为
  1 条 WARNING**（含候选数与失败码），404 不再逐条告警。
- 无前缀场景保留候选链（回归用例覆盖两分支）。

### 2. ref 级 404 负缓存（src/domains/runtime/vcs/api_client.py）

复用 `src/api/issue_vcs_index.py` 的进程内 TTL 模式（不另建设施，#3759 口径）：

- 模块级缓存：`(repo_key, ident) → (expires_at, payload)`；TTL env 可调
  `GRAPE_VCS_REF_404_TTL_SECONDS`，默认 60s，夹取 [60, 300]。
- `VcsApiClient.get_issue`：先查负缓存（命中直接返回，不打上游、不告警）；
  上游 404 → 写入（写入口落 **1 条可归因 WARNING**，TTL 窗口内唯一）；
  401/403/5xx/网络异常 **不缓存、照常告警**。
- `_do_request` 错误 dict 增补 `error_code` 分类：
  `404→not_found`、`401/403→auth_failed`、`>=500→upstream_error`、
  其余 `http_error`、异常 `network_error`；GET+404 告警降 DEBUG（读路径 404 =
  "不存在"语义，非运行故障；写路径 404 保持 WARNING，防路由拼错静默）。
- `git_repo_api._vcs_req`：GET+404 同步降 DEBUG（同口径）。
- `_vcs_get_issue`（event_pipeline.py）：token 解析**之前**先查负缓存——
  命中直接 `("missing", {})`，杜绝缓存命中路径的 token 解析告警与 1.3s 上游空转。

### 3. token-missing 告警降噪（src/kernel/token_resolver.py）

`GlobalBotToken._fetch`（631–636 行）改为**窗口聚合**：同 provider 300s 窗口内
首次缺失落 1 条 WARNING（可归因），窗口内后续降 INFO（配置缺失是静态事实，
非每次请求的运行故障）。窗口 env 可调 `GRAPE_GLOBAL_TOKEN_MISSING_WARN_WINDOW_SECONDS`。

### 4. 调用方 404 快速失败 + 指数退避（worker 轮询 pipeline 路径）

仓内不存在固定 20s 轮询循环（全仓检索取证：轮询由 worker 侧 agent 按技能
runbook 生成的 shell 驱动）。落两个可执行件：

- **服务端**：`_detail_404` 响应带 `Retry-After` 头（= 负缓存 TTL），
  404 即快速失败（负缓存命中路径无上游耗时）；合规调用方按头退避。
- **runbook 面**：`.grape/skills/qa/code-quality/domain-qa/references/grape-cli-reference.md`
  轮询终态指引增补 404 快速失败语义：`issues pipeline` 返回 404 → 立即停止重试；
  必须重试时指数退避（20s→40s→80s）且上限 3 次后终止并如实上报——
  这是产生"20s 99 次/小时"轮询行为的指令源头（worker 轮询路径）。

## 回归用例（5 类，对齐 Spec）

tests/runtime/test_issue_4343_prefix_select_404_negcache.py：

1. 前缀直选：owner 带 `gitcode:` 前缀 → 上游 issue GET 仅 1 次、仅探测 gitcode
   provider（无 github/gitee/gitlab 探测与 token-missing 日志）。
2. 无前缀候选链：候选失败聚合为 1 条 WARNING（caplog 断言 WARNING 计数 ≤1）。
3. 负缓存命中/过期：TTL 内上游调用 = 1（mock 计数）；TTL 过期后重新探测。
4. 错误分流：401/403/5xx 不入负缓存、保持 WARNING 与原重试语义；
   error_code 区分 ref_not_found / auth_failed / upstream_error / network_error。
5. 调用方退避终止：404 响应带 Retry-After 头；连续 404 第二次请求零上游调用。

## 验证

```bash
python3 -m pytest tests/runtime/test_issue_4343_prefix_select_404_negcache.py \
  tests/runtime/test_issue_3891_pipeline_shared_resolution.py \
  tests/runtime/test_issue_667_detail_fallback.py \
  tests/runtime/test_issue_672_dual_path_resolution.py -q
make lint 2>/dev/null || python3 -m flake8 src/domains/runtime/vcs/api_client.py \
  src/kernel/token_resolver.py src/domains/runtime/routes/issues.py --max-line-length=120
```

## 待决策（数值证据，B2 五源闸门 · PR #2245 复审）

| 数值 | 位置 | 缺哪种测量 | 现状 |
|---|---|---|---|
| `_REF_404_MAX_ENTRIES_DEFAULT = 10000` | `src/domains/runtime/vcs/api_client.py` | 单进程负缓存常驻内存、404 命中率 | 按 300B/条估算 ≈3MB；env `GRAPE_VCS_REF_404_MAX_ENTRIES` 可调，下限 100；未实测 |
| `engine_select.py:943` 阈值 0.9 | `src/domains/runtime/engine/engine_select.py` | 选择器命中分布 | 有设计记录、无实测 |
| `token_resolver.py:115` TTL 300s | `src/domains/runtime/vcs/token_resolver.py` | 凭据缓存命中率/失效延迟 | 有设计记录、无实测 |
| `deploy/alerts.yml` 阈值 `>0.8 / >0.9` 与 `for: 10m / 5m` | `deploy/alerts.yml` | 生产告警误报/漏报率 | 有设计记录、无实测 |
