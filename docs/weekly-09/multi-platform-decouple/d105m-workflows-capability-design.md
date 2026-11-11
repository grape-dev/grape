# D105-18：凭据级 workflows 写能力三态设计（2026-11-09）

## 背景

D105-17 收口时登记了两处 github 专属比较的审阅保留（"dialect 无法区分，
治理留待适配器端口收口"）。其中模板 push 的 `.github/workflows` 删除怪癖
本批升级为凭据级能力三态：判定维度从【平台名】移到【凭据声明】。

依据（gh-aw ADR-0002 显式声明制）：GitHub 权限模型是 requested ∩ granted，
平台不暴露 token scope，运行时内省不可行——高权限是凭据配置时显式声明、
可审计的事实，不静默推断。

## 语义（五态）

| 声明态 | 判定 | push 含 .github/workflows 的模板 |
|---|---|---|
| workflows_write=true | 凭据声明有写权限 | 不删 |
| workflows_write=false | 凭据声明受限（新能力） | 删 |
| 未声明 + github 平台 | 回退平台名旧逻辑 | 删（基线冻结） |
| 未声明 + gitcode 平台 | 回退平台名旧逻辑 | 不删（零回归的真正断言） |
| OAuth 路径（无 <p>_pat 声明） | None → 保守回退 | 保守删（OAuth scope 不可内省，预留 oauth_scopes 声明入口） |

## 改动点

1. `src/token_resolver.py`：`credential_workflows_write(user_id, provider)`
   三态查询（True/False/None），读凭据域 `<p>_pat` 配置的
   `workflows_write` 键；异常吞掉返 None（不阻断 push）。
2. `src/user/repo_manager.py`：`push_template_to_repo` 加 `user_id` 参数；
   workflows 目录删除判定改 `_wf_denied = (provider == "github") if _wf is None else (not _wf)`；
   github 分支保留为 None 回退路径（D105-17 审阅保留的收窄形态）。
3. `src/api/routes/settings.py`：
   - `update_credentials` 泛型 `platform_tokens` 值支持对象形态
     `{"token": ..., "workflows_write": bool}`；字符串形态向后兼容=未声明；
     显式 False 也落库（三态不丢）。
   - `get_credentials` 存在性响应回显 `workflows_write`（凭据 tab 能力/清除
     按钮判定数据源）。
4. `src/api/routes/templates.py`：模板创建链路补传 `user_id`（不传则能力
   查询恒 None，功能死代码——接线是本批必要件）。

## 不做

- 不做运行时 scope 内省（平台不暴露，见背景）。
- 不做 OAuth `oauth_scopes` 声明入口（预留，本批只留 None 回退语义）。
- 不做前端表单控件（对象形态 API 先行，前端接入随凭据 tab 迭代）。

## 验收

- 行为锁 `tests/test_d105m_workflows_capability.py`（6 用例：三态单元 +
  四态 push 分流 + OAuth 保守删）。
- 场景用例 `tests/test_s103_workflows_capability_scenarios.py`（S94，4 用例：
  声明落库+回显 / false 落库 / 旧前端字符串形态零隐式声明 / github 声明 true
  端到端不删）。
- 存量零回归：未声明路径 github 删 / gitcode 不删，与 D105-17 基线一致。
