# Spec — Issue #3427 凭据读侧失败原因可分辨

## Objective
凭据读侧（GitHub token 取用链）可诊断：错误码分层、凭据解析单点化、仓库级 bot token 显式绑定接口、托管失败落库、a2a 403 与 token 错误解耦。

## In Scope
1. 错误码分层：`TOKEN_MISSING` / `TOKEN_DECRYPT_FAILED` / `TOKEN_REJECTED_UPSTREAM(401)` / `REPO_NOT_BOUND_TO_USER(403)`，各带可行动文案。
2. 凭据解析器单点：固定顺序「仓库级 bot（grape_t_repo_credentials, cred_type=bot）→ 全局 bot → 用户身份（external_identities）」，命中即用，解析来源（rc/global/user）写日志。
3. 新增 `PUT /api/repos/{owner}/{repo}/bot-token` 显式绑定接口，写入即调 GitHub `GET /user` 校验。
4. 托管失败写 `managed_repos.host_error`。
5. a2a 403 与 token 错误解耦，文案指向 `user_repos` 未关联。

## Out of Scope
#3425 托管流程改造、OAuth 绑定流程改造、k1 加密体系重构、其他 VCS 平台凭据链改造。

## Acceptance Criteria
见 Issue Spec 评论（7 条）。核心：三情形错误码各异且文案可行动；解析顺序固定且日志含来源；显式绑定写入即校验；host 失败 host_error 有值；同一有效 PAT 复现 host 不再误报；现有测试通过并新增覆盖。

## 补记（QA 复审 dd11face7 后增补）
1. **补码 `TOKEN_VERIFY_UNAVAILABLE`(502)**：绑定端点 `PUT /api/repos/{owner}/{repo}/bot-token` 在 provider 构造失败/网络异常导致 `GET /user` 校验通道不可用时使用；不属于三情形读侧错误码集合，是绑定写侧的通道异常补码（映射 502）。
2. **过期标记归一 401**：本地已标记过期（rc `bot_token_status='expired'` / 全局 bot meta expired）的凭据跳过该层后，全链未命中按 `TOKEN_REJECTED_UPSTREAM(401)` 归因，不再与「未配置」同码 `TOKEN_MISSING`；优先级 `rejected_upstream > marked_expired > decrypt_failed > missing`。
3. **路径 B 与固定顺序的张力说明**：Spec 的「固定顺序」指路径内层序（rc → global → user）；路径 B（host_user_id 非空）整体跳过 rc/global 是既有策略链语义（路径 B 用用户自己的身份），与 plan.md 的解释一致——非对固定顺序的违反。
4. **状态码与错误码映射一致性**：`TOKEN_REJECTED_UPSTREAM` 恒映射 HTTP 401（含建单入口 bot 来源 401，不套 502）；bot 502 仅保留给 `TOKEN_VERIFY_UNAVAILABLE` 等通道异常类。
5. **解析器架构归位**：`resolve_credential_detailed` 归属 mgmt 域（`src/domains/mgmt/auth/credential_resolver.py`），kernel 不得 import domains（check-arch R1）；HTTP 状态提取保持在 domain 侧（runtime.vcs.token_error）。
