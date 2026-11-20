# 自建 GitLab 实例接入（Issue #3580）— 需求

> 范围：把"一个自建 GitLab（含子组 namespace）能被 Grape 当作一等仓库平台接入并全链路可用"
> 这件事写成可验收口径。**不含**通用平台对象管理界面（归 D109 后续批次），**不含** PR #1854
> 的 `GRAPE_GITLAB_INSTANCES` 环境变量形态与 `static/js/pages/gitlab.js` 页面。

## 1. 现状问题（file:line 实测）

| # | 问题 | 证据 |
|---|---|---|
| G1 | 实例卡新增只能靠管理接口写整卡集合，且**只写 meta**：目录/凭据 tab 表优先，永远看不到纯 meta 卡 | `src/domains/mgmt/admin/routes.py:204`（写 `vcs_providers_config`）；`grep -rn "admin/config" src/api/frontend/`（Vue/shell）命中 0 |
| G2 | 实例卡缺代码模块时，网关调用直接 502（"Git provider 未支持"） | `src/domains/runtime/vcs/providers/__init__.py::module_for`；`src/api/git_repo_api.py::_provider_module` |
| G3 | 子组仓库（`grp/sub/proj`）寻址失败：repo_key 按首个 `/` 切分，网关只有两段式路由 | `src/kernel/repo_key.py::parse_repo_key`（已修）；`src/api/git_repo_api.py`（已补 `{owner:path}`） |
| G4 | 私有 CA 只进 git 传输层，API 侧（列仓/托管/建钩/Issue/MR）走默认 SSL 上下文 | `src/kernel/token_resolver.py::_http`、`vcs/api_client.py::_do_request`（已收口到 `src/kernel/upstream_http.py`） |
| G5 | GitLab 无 `git/trees`、`contents` 端点 → 仓库树/文件浏览 404 | 实测 `_tree_family("gitlab") == "v5"` 时 URL 为 `/projects/x/git/trees/main`（已改钩子） |
| G6 | 指派只认数值 id，用户名被 `_drop` → "指派给某人"静默不生效 | `providers/gitlab.py::_RULES`（已改 `assignee_ids` 解析） |
| G7 | 实例接入没有端到端校验手段（卡字段对不对、CA 是否生效、namespace 是否可达）只能靠人肉试 | 本批新增 `scripts/probe_gitlab_instance.py` |

> G2–G6 已在 `63ab62e67` 落地；本批（文档批）补 G1 的**口径与入口**（仍不加界面）与 G7 的**校验脚本**。

## 2. 范围

### 做
1. **实例 = 平台卡行**：`provider` key（`gitlab-corp` 一类）即 repo_key 前缀，一实例一行。
2. **接入路径**：`PUT /api/admin/config`（已有，复用其 Builder 校验 / host 与 webhook_path 唯一性校验）；文档给出完整卡样例与调用方式。
3. **端到端校验脚本**：给定实例卡，逐项验证 token / 列仓 / namespace / tree / 文件 / 建钩（dry-run）/ CA，输出结论表。
4. **守卫测试**：卡样例字段集、namespace 寻址、CA 策略、树/文件钩子、指派解析（已有 26 条）补齐"实例卡经 `PUT /admin/config` 写入后 `/settings/vcs/platforms` 可见 + 网关可寻址"一条链路用例。

### 非目标
- 通用"新增平台/实例"管理页（D109 平台对象模型批次的界面工作）。
- `GRAPE_GITLAB_INSTANCES` 环境变量、`gitlab@corp` 命名形态（与一实例一卡冲突，且 env 是第二真源）。
- GitLab OAuth 登录（`R12`：登录渠道不含 gitlab）。
- GitLab 组织级成员同步、分支保护、合并策略（PR #1854 同样未做）。

## 3. 场景用例

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|---|---|---|---|---|
| S1 | 新增实例卡（**最小配置**：只给 `dialect` + `host`） | 管理员 token | `PUT /api/admin/vcs-platforms` 带 `gitlab-corp` 卡 | 200 + `rows.gitlab-corp=inserted`；`source='manual'` 行落库；`/settings/vcs/platforms` **立即**出现该实例（无需重启）；host/webhook_path 重复被 400 | `test_issue_3580_instance_card_via_admin_config.py::test_minimal_card_derives_gitlab_defaults` / `::test_instance_row_is_manual_and_listed` |
| S2 | 字段非法 | 同上 | `api_base` 非 http、`ca_bundle` 相对路径、`dialect` 未知 | 400 + `errors[]` 明细，meta 与表零写入 | 同上 `::test_instance_card_rejected_when_fields_invalid` |
| S1b | 文件派生 provider 被管理面改写 | `gitcode` 行来自 `configs/platforms/gitcode.json` | `PUT` 带改过的 `gitcode` 卡 | `rows.gitcode=config_owned`，行仍是 `source='config'` 且字段未被改写 | 同上 `::test_file_backed_provider_is_not_overwritten` |
| S3 | 子组寻址 | 卡已接入 | 请求 `/api/git-repo/grp/sub/proj/tree` | 命中 namespace 路由，owner=`grp/sub` | `test_issue_3580_namespace_routes.py`（已绿） |
| S4 | 私有 CA | 实例 API 用企业 CA | 列表/建钩 | 走卡 `ca_bundle`；3xx 不跟随 | `test_issue_3580_upstream_tls.py`（已绿） |
| S5 | 树/文件 | 实例可达 | `GET .../tree`、`.../contents/src/a.py`、`.../contents/src` | 归一为 `{type,mode,path,name,sha,size}`；文件 base64；目录回落 | `test_issue_3580_gitlab_repo_paths.py`（已绿） |
| S6 | 指派 | 建 Issue 带用户名 | `create_issue(assignee="alice")` | payload 带 `assignee_ids=[alice 的 id]` | `test_issue_3580_gitlab_assignee.py`（已绿） |
| S7 | 接入自检 | 实例卡已写入 | `python scripts/probe_gitlab_instance.py <provider>` | 输出 token/列仓/namespace/tree/CA/建钩 dry-run 六项结论 | 脚本 `--self-test` 单测（mock 上游） |
| S9 | 一键建仓到组/子组 | 实例卡 + 用户 PAT（组内 Developer 及以上） | 创建弹窗选"所属空间" `grp/sub` → 建仓 | `/namespaces` 拿到 id → `/projects` 带 `namespace_id` 建成；仓库列表出现 `gitlab-corp:grp/sub/<name>`；组不存在/无权限给出明确错误 | `test_issue_3580_create_space.py` |
| S10 | 空清单与查重 | 同上 | `GET /repos/create-targets`、`GET /repos/check-name?owner=` | 空间清单返回组 full_path（上游失败降级为空 = 只有个人空间）；查重按空间判定，子组同名不误判 | 同上 |
| S8 | 事件接入 | 实例已托管仓库 | 上游投递 MR 事件 | 通用 inbox 幂等入队（`(repo_key, delivery_id)`），worker 消费 | `test_issue_3580_review_fixes.py` + 真库演练 |

## 4. 验收标准
- S1/S2 新增链路用例绿；S3–S6 既有 26 条绿；S7 脚本在 mock 与真实例两种模式下可跑。
- 文档给出可直接复制的 `gitlab-corp` 卡样例（占位值，无真实凭据）。
- 不新增 `/api/admin/repo-platforms` 写接口、不新增前端页面（守住 D109 §23 "无整卡 PUT / 无父容器" 契约）。