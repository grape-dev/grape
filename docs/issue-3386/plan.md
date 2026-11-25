# Issue #3386 实现方案

## 数据流

```
PAT 创建页（预选 repo,workflow,admin:repo_hook）
  → 绑定 PAT（settings.update_credentials）
      → get_github_token_scopes(token) 读 X-OAuth-Scopes
          ├─ 含 workflow → 凭据落 workflows_write=True
          ├─ 明确缺失   → 响应 missing_scopes + scope_warnings（不阻断绑定）
          └─ 未知       → 仅提示，不落能力位
  → 后续模板初始化：workflows_write=True 不再删 .github/workflows
  → Agent 推送（pre-push 钩子）
      → check_workflow_scope_push
          ├─ 非 github / diff 未命中 .github/workflows → 放行
          ├─ 能力位 True / scope 含 workflow          → 放行
          ├─ 能力位 False / scope 明确缺失            → 阻断 + 修复指引
          └─ scope 未知（无 X-OAuth-Scopes）           → 放行 + 提示
```

## 文件改动

| 文件 | 改动 |
|------|------|
| `src/domains/mgmt/user/repo_manager.py` | `_github_req_with_headers`、`get_github_token_scopes`；删目录规避逻辑补 TODO/下线条件 |
| `src/domains/mgmt/routes/settings.py` | GitHub PAT 两条绑定路径接入 scope 探测，返回 `missing_scopes`/`scope_warnings`，落 `workflows_write` |
| `src/domains/runtime/engine/branch_guard.py` | workflow scope 纯判定 + diff 提取 + 预检入口；pre-push 钩子调用 |
| `src/api/frontend/app/src/components/{BindDialog,PatDialog}.vue` | PAT 令牌链接预选 workflow；绑定结果预警与重新创建令牌入口 |
| `src/api/frontend/app/src/views/AccountView.vue` | PAT 创建链接预选 workflow |
| `src/api/frontend/shell/locales/{zh,en}/app.json` | workflow 帮助与缺失/未知预警文案 |
| `help/developer/02-github-integration.md` | 登录 scope 与 PAT workflow 说明 |
| `tests/mgmt/test_issue_3386_workflow_scope.py` | scope 三态解析、预检分支、绑定 missing_scopes |
| `tests/js/test_token_create_deeplink.js` | PAT 直达链接 scope 断言 |

## 关键决策

- **探测来源**：`X-OAuth-Scopes` 是 classic PAT/OAuth token 唯一可枚举的真实授权来源；
  fine-grained token / App token 不返回该头 → 未知，fail-open。
- **绑定不阻断**：存量用户 PAT 缺 workflow 时绑定仍成功，只给预警与重新创建入口。
- **预检只拦确定缺失**：避免未知场景误阻断正常交付；显式 `workflows_write=False`
  （App token / 用户声明）仍阻断。
- **能力位自动落库**：探测已知时写 `workflows_write`，复用 D105-18 三态模型，
  使模板初始化分支与推送预检共享同一事实。

## 测试

```bash
python3 -m pytest tests/mgmt/test_issue_3386_workflow_scope.py \
  tests/mgmt/test_d105m_workflows_capability.py \
  tests/mgmt/test_s103_workflows_capability_scenarios.py \
  tests/mgmt/test_oauth_channel_consistency.py -q
python3 -m pytest tests/runtime/test_issue_993_branch_guard.py \
  tests/runtime/test_issue_2890_branch_guard_aggregate_warning.py \
  tests/test_issue_2028_branch_guard_fail_open.py -q
node --test tests/js/test_token_create_deeplink.js
```
