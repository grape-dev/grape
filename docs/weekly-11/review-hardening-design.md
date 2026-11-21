# 评审补强四条 —— 设计

对应需求：`review-hardening-requirements.md`（R1–R5）。

## 1. settings：校验前移（R1）

代码：`src/domains/mgmt/routes/settings.py`

```
_assert_bot_username_free(name)        # 新增局部函数：查 grape_accounts，命中即 400
  冲突 → HTTPException(400, BOT_USERNAME_CONFLICT)
  查询异常 → 放行（不因基础设施失败阻断配置）

顺序（唯一正确序）：
  1 verify_token → _verified_login
  2 effective = body.bot_username or _verified_login
  3 _assert_bot_username_free(effective)      ← 校验
  4 update_managed_repo_bot_token(...)        ← 写令牌
  5 update_managed_repo_bot_username(...)     ← 写用户名
无令牌路径（只改用户名）：写用户名之前同样调用 _assert_bot_username_free。
```

为什么校验“最终值”而不是 `body.bot_username`：自动回填发生在令牌校验之后、写之前，
只看请求体里的名字会漏掉“令牌所属账号与真人同名”的冲突。

## 2. 写冻结守卫路径（R2）

代码：`tests/mgmt/test_user_accounts_write_freeze_guard.py`

```
_SRC = _TEST_DIR.parents[1] / "src"
   tests/mgmt/x.py → parents[1] = 仓库根
（原 .parent/"src" → tests/src，不存在 → 扫 0 文件 → 恒绿）
```

GC1–GC3 的形状不变（INSERT / UPDATE / DELETE `user_accounts`），只是终于打在真实源码上。

## 3. 全仓扫描 + 数据流断言（R3）

代码：`tests/runtime/test_clone_endpoint_multiplatform.py`

```
正则：https://(gitcode|gitee|github)\.com/  命中且同行含 ".git" → 视为自拼仓库地址
扫描范围：src/**/*.py
棘轮：_KNOWN_CLONE_HOST_LITERALS
  · repo_manager.py / org_context_repo.py —— 存量自拼（待收口，不在本轮）
  · main.py / server_common.py / executor.py / event_pipeline.py / scene_sync.py /
    repo.py / worker_package.py —— runbook 文案里的示例地址（文本，非构造）
数据流断言：
  build_context({"repo_key":"gitee:a/b","env_extra":{"REPO_KEY":"gitee:a/b","REPO_FULL_NAME":"a/b"}})
  → repo_url 必须是 https://gitee.com/a/b.git
```

为什么按“同行含 .git”收敛：只看 host 字面量会把 API 地址、webhook 展示、帮助文案全部误报；
真正危险的是“据此拼仓库地址”。数据流断言补上“值被覆盖”这类字面量扫描抓不到的情况。

## 4. OAuth 退避与重授权（R4）

代码：`src/domains/mgmt/user/db.py`、`src/api/oauth_token.py`

```
db.py
  _REFRESH_MIN_INTERVAL_S = 60
  _REFRESH_LAST[(user_id, provider)] = monotonic()      # 刷新成功后记录
  refresh_oauth_token() 入口：
      若距上次成功刷新 < 60s → 不调 OAuth，返回库里现有 access_token（不轮换 refresh_token）
  mark_identity_reauth_required(user_id, provider, reason)
       UPDATE external_identities SET status=4 ... WHERE status=1
       invalid_grant/400 与 post-refresh-401 共用

api/oauth_token.py::with_oauth_token
  预刷新 → 请求 → token 错误 → refresh → 重试
  重试仍 token 错误 → mark_identity_reauth_required(..., "post_refresh_401") → 提示重新授权
```

不变式：
- 同一身份 60s 内最多一次 OAuth 往返（防轮换风暴）；
- “刷新有效但上游拒绝”必须落 status=4，而不是无限重试；
- 退避只影响 `refresh_oauth_token`，不影响读路径（预刷新命中退避时用现有 token 继续）。

## 5. 场景矩阵

| 编号 | 场景 | 期望 | 用例 |
|---|---|---|---|
| S1 | 存令牌且用户名与真人冲突 | 400，令牌与用户名都不变 | `test_settings_bot_username_order.py::test_conflict_check_precedes_both_writes` |
| S2 | 只改用户名（无令牌）冲突 | 400，用户名不变 | 同上 `::test_no_write_before_the_early_check_for_tokenless_updates` |
| S3 | 冻结守卫扫真实源码 | 通过（零写语句） | `test_user_accounts_write_freeze_guard.py` |
| S4 | gitee/github/gitlab clone | 各自平台 host | `test_sandbox_clone_provider.py` S1–S3 |
| S5 | REPO_KEY 与裸名并存 | 取带前缀 key | 同文件 S4 + `test_clone_endpoint_multiplatform.py::test_sandbox_clone_uses_the_prefixed_key_not_the_bare_name` |
| S6 | 全仓新增自拼地址 | 扫描红 | `::test_repo_wide_no_new_hardcoded_clone_hosts` |
| S7 | 60s 内重复刷新 | 不调 OAuth，返回现有 token | `test_oauth_refresh_backoff.py::test_s1` |
| S8 | 退避到期 | 正常刷新并记录时间戳 | `::test_s2` |
| S9 | 打 status=4 | SQL 参数与提交正确 | `::test_s3` |
| S10 | 刷新成功但重试 401 | 打 status=4 + 重新授权 | `::test_s4` |
| S11 | 重试路径的 repo_key | 底层 `_run_git` 收到 repo_key | `test_run_git_platform_auth.py::test_s7` |
| S12 | linked push / 远端探测 | 两个调用点都传 repo_key | 同文件 `::test_s8` |
| S14 | DDL + ownership 同进程 | 全绿（无全局污染） | `::test_s14` |
| S15 | 测试恢复纪律 | 只用 monkeypatch | `::test_s15` |
| S16 | 云沙箱 executor 平台判定 | REPO_KEY 优先（与 sandbox_context 同序） | `test_sandbox_clone_provider.py::test_s6` |
| S17 | 脱敏红线 | 本分支变更文件零真实账号/库名/个人路径 | `test_public_docs_redaction.py` |

## 6. 验证命令（一次一个文件）

```
PY=<venv>/bin/python
$PY -m pytest tests/mgmt/test_settings_bot_username_order.py -q
$PY -m pytest tests/mgmt/test_user_accounts_write_freeze_guard.py -q
$PY -m pytest tests/runtime/test_clone_endpoint_multiplatform.py -q
$PY -m pytest tests/mgmt/test_oauth_refresh_backoff.py -q
$PY -m pytest tests/mgmt/test_oauth_reauth_scenarios.py -q
$PY -m pytest tests/arch/test_public_docs_redaction.py -q
$PY -m pytest tests/runtime/test_sandbox_clone_provider.py -q
$PY -m pytest tests/mgmt/test_issue_3563_reauth_401_split.py -q
$PY -m pytest tests/mgmt/test_issue_3563_github_token_refresh.py -q
```

## 7. 传输凭据的调用点清单（R6）

```
必须传 repo_key 的调用点（网络动作）：
  repogit._run_git_retry → _run_git         （重试路径；旧实现死参数）
  repogit 基线 fetch / _branch_vs_baseline fetch / ls-remote
  branch_guard.guarded_push
  event_pipeline 直连 PR push（repo_key=_rk）
  agent.py linked-repo push（repo_key=_rk）
  worktree_guard：collect_reset_risk → _collect_unpushed → _remote_branch_head（ls-remote）
  project_info/builder 两处 fetch

本地动作（rev-parse/diff/show-ref）不传也能过，但传了无害；不列入验收。

不变式：
  · 重试封装必须透传 repo_key（否则“同源”只在首次尝试成立）；
  · 远端探测失败必须与“分支未推送”区分（probed=False 回退本地判定）；
  · 派生失败（clone_params 异常）回落 x_access_token，并留 warning。
```

## 8. 已知失败登记

```
tests/arch/test_silent_except_ratchet.py  2 failed —— 基线 dcb3759e7 同样失败
  （基线 json 计数落后于代码：repogit 3→5、mysql_compat 7→9、db_registry 未登记）
  属既有失败；本分支未新增静默 except（逐文件比对与基线计数一致），不改断言、不上调基线。
tests/mgmt/test_user_db.py::TestAliases::test_aliases_delegate —— 基线同样失败
```

## 9. 云沙箱 executor 的平台判定（R9）

```
src/domains/runtime/agents/executor.py::_run_cloud_sandbox
  repo_full_name = proc_env.get("REPO_KEY") or proc_env.get("REPO_FULL_NAME", "")
  → clone_params(repo_full_name) 得到 clone_url / clone_auth / username
  → bot_token_from_env(proc_env, repo_full_name) 得到对应平台的 BOT_<PROVIDER>_TOKEN

为什么顺序不能反：派发侧 env_extra 的 REPO_FULL_NAME 是剥前缀裸名（runbook 拼 URL 用），
cloud_worker 先写的带前缀 repo_key 随后被 env.update(env_extra) 覆盖；只读裸名 →
parse_repo_key 默认 gitcode → 非 gitcode 仓 clone 到错平台、令牌键也错。

不变式：任何“从 task/env 推平台”的位置都必须先读 REPO_KEY；
REPO_FULL_NAME 只用于拼展示/runbook URL。
```

## 10. 脱敏约定（R10）

| 真实值类型 | 占位符 | 示例 |
|---|---|---|
| 个人目录 | `<venv>` / `<home>` | `PY=<venv>/bin/python` |
| 账号名（含平台账号） | `<ACCOUNT>` / `<platform_account>` | `gitee:<ACCOUNT>/demo` |
| 库名 | `<db_mgmt>` / `<db_insight>` | `--mgmt-dsn mysql://…/<db_mgmt>` |
| 备份目录 | `<BACKUP_DIR>` | 脚本默认 `RC_BACKUP_DIR` 或相对目录 `./backup-rc-<ts>` |
| 主机/IP | `<host_a>` / `<server_ip>` / `<public_ip>` | 运维文档 |
| 测试夹具仓 | `acme/<repo>` | `gitee:acme/demo` |

守门用例：`tests/arch/test_public_docs_redaction.py` —— 只扫**本分支变更文件**
（`git diff --name-only <merge-base>...HEAD`），基线历史命中登记在 allowlist。

## 11. 提交署名（R11）

```
author/committer/Signed-off-by = grape-bot <bot@grapedev.cn>
历史遗留个人署名：合入前用 filter-branch 重写（本轮已清洗 2 个 commit），
重写后所有 sha 变化，PR head 与 QA 历史的 sha 需重新对账。
```

## 12. 测试隔离纪律（R8）

```
禁止：orig = mod.global; mod.global = fake; ... finally: mod.global = orig
  反例：tests/api/test_destructive_ddl_stamp_scenarios.py S3 曾把 _cleanup_gate_passed
        恢复成另一个函数的原值（hasattr 恒真）→ 同进程 table_ownership s10 读假 gate 变红。
一律：monkeypatch.setattr(mod, "global", fake)，由夹具恢复。
守门用例：S14（同进程跑两个文件全绿）+ S15（源码里不得再出现手工赋值恢复）。
```

## 13. 回滚

```
R1 顺序改动：单文件回退 settings.py（无数据影响）
R2/R3 只用例改动：回退测试文件
R4：回退 db.py 的 _REFRESH_LAST/mark_identity_reauth_required 与 oauth_token.py 的调用；
    已写入的 status=4 行需要人工按绑定关系恢复（重新授权即恢复，属常规流程）
```