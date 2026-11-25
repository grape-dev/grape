# Issue #3386 实现规格（编码落地版）

## 目标

GitHub 仓库在 Agent 自动开发中可正常创建/更新 `.github/workflows/**`；权限缺失在
「绑定」与「推送前预检」两处提前暴露，而不是等远端 403/rejected。

## 架构约束（与已发布 Spec 的差异说明）

已发布 Spec 写于 D108-B 之前，要求把 `configs/oauth-providers.example.json` 的 GitHub
登录 scope 改为 `repo user admin:repo_hook workflow offline_access`。目标基线
`weekly-mys` 已落地 D108-B / D109：**OAuth 登录令牌只承担身份 scope（`read:user user:email`），
不再回落仓库读写**，仓库写操作统一使用凭据域 PAT。因此本实现按现行架构落地：

- 登录 OAuth scope 保持 `read:user user:email`（不重新放宽登录应用权限）。
- 真正用于仓库写的「默认 scope」= PAT 创建直达链接预选
  `repo,workflow,admin:repo_hook`（前端 `PAT_CREATE_URLS`，与 GitHub 令牌创建页一致）。
- 文档同步说明 PAT 必须勾选 `repo` + `workflow`。

## 范围

1. **PAT 默认 scope**：`BindDialog.vue` / `PatDialog.vue` / `AccountView.vue` 的 GitHub
   令牌创建直达链接预选 `repo,workflow,admin:repo_hook`；帮助文案加入 workflow。
2. **Scope 探测**：`src/domains/mgmt/user/repo_manager.py` 新增
   `_github_req_with_headers` 与 `get_github_token_scopes(token)`，读取
   `GET /user` 的 `X-OAuth-Scopes`。
3. **绑定预警**：`src/domains/mgmt/routes/settings.py` 在 GitHub PAT 绑定成功后探测
   scope：已知有 workflow → 落 `workflows_write=True`；已知缺失 → 响应带
   `missing_scopes` 与 `scope_warnings`；未知 → 仅提示，不阻断、不落能力位。
   前端绑定弹窗展示缺失预警与重新创建令牌入口（绑定仍成功）。
4. **推送前预检**：`src/domains/runtime/engine/branch_guard.py` 新增
   `decide_workflow_scope_push` / `_workflow_files_changed` / `check_workflow_scope_push`，
   在 pre-push 钩子真正传输前判定：仅 GitHub + 本次 diff 命中 `.github/workflows/**`
   时才检查；显式能力位 True 放行、False 阻断；scope 未知 fail-open 仅提示；明确缺
   `workflow` 时阻断并给出修复指引。
5. **过渡逻辑保留**：`repo_manager.py` 模板初始化时的「无权限删除 .github/workflows」
   逻辑按 Spec 暂留，补 TODO 与下线条件（1/2/3/审批通过并验证后单独跟进移除）。

## 不在范围

- GitHub App installation token 的 workflows 权限模型改造。
- GitCode / GitLab 权限模型调整。
- 直接删除模板 `.github/workflows` 规避逻辑（本单只记录下线条件）。

## 验收

- [x] GitHub PAT 创建链接预选 `repo,workflow,admin:repo_hook`，帮助文案与之一致。
- [x] `get_github_token_scopes` 覆盖有 / 无 / 空 `X-OAuth-Scopes`（未知返回 `None`）。
- [x] 仅含 `repo` 类 scope 的 PAT 绑定时返回 `missing_scopes={"github":["workflow"]}` 并预警。
- [x] 推送 diff 命中 `.github/workflows/` 且 scope 明确缺失时 pre-push 阻断并给出指引，不产生远端 403。
- [x] 具备 workflow scope（或显式能力位 True）时放行。
- [x] 现有测试通过，新增单测覆盖 scope 解析与预检分支。
