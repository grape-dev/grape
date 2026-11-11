# D102-P：职责分域——第三方账号=OAuth 域 / 仓库凭据=PAT 域（方案稿与实施记录）

> 状态：已实施（2026-11-08，口径 v6 定稿）
> 前置：D102-N 设置页拆分 / D102-O 传输层 ident 保形
> 触发：用户实弹连续质询——"PAT 配置后在第三方账号看到已绑定/解绑很奇怪"、
> "PAT 和 OAuth token 根本是两个东西，放一起带来隐患"、"外部复核验证框架"
> （读写分离错位 C 情形判定）

---

## 〇、裁决基线（实码验证）

外部复核验证框架四项全部实测命中 **C 情形（读写分离错位）**：

1. **TokenResolver 有独立 PAT 读取分支**：`fetch_user_token`（token_resolver.py）
   解析序 = ① 凭据域 `get_credential(user_id, "<p>_pat")` → ② OAuth 身份
   `get_user_token_for`（external_identities），`level=pat/oauth` 分轨审计。
   → 移出 ei 是**归位修正**而非高风险迁移。
2. **读写分离错位实锤**：PAT 写入走 uc（`set_credential("<p>_pat")`）+ ei 双写
   （access_token=PAT），但 repos member/sync 读取走 `with_oauth_token →
   get_user_token_for → ei.access_token`——写读不同源。
3. **三种污染实锤**（历史形态）：撤销污染（解绑软删清字段 → ei 读取型消费方
   断供）、有效性污染（PAT 行被 status=4 过期标记/refresh 波及）、生命周期
   污染（OAuth refresh 轮换 `update_external_identity_tokens` 覆盖同字段 PAT）。
4. **职责裁定（用户，2026-11-08）**：第三方账号=OAuth 域（绑定登录关系 +
   OAuth 授权/解绑）；仓库凭据=PAT 域（配置/删除）；PAT 配置不得在第三方
   账号行产生"已绑定/解绑"展示。

## 一、改动点

| # | 位置 | 改动 |
|---|---|---|
| W1 | settings.py platform_tokens 写入 | 归属行 access_token **恒空**（PAT 不再进 ei token 字段）；PAT 权威存储=凭据域 `<p>_pat`；写后 `mark_identity_pat` 打标 `metadata.source='pat'` |
| W2 | grape_account_db `get_user_identities` | 新增 `has_oauth_token`（布尔，不含 token 值）= access_token 非空——PAT 归属行 token 恒空 → 自然 false |
| W3 | grape_account_db `mark_identity_pat` | 新增：metadata.source='pat' + pat_login 打标（幂等，保留既有键） |
| W4 | grape_account_db `backfill_pat_identity_tokens` | 启动一次性回填：扫凭据域 `<p>_pat` → 解密**精确比对** ei.access_token==PAT 才迁移（清空+打标），幂等防 OAuth 行误清 |
| W5 | main.py lifespan | 注册 W4 回填（non-fatal 包裹） |
| W6 | repos.py member/sync | token 取数归位 `fetch_user_token` 双通道（① uc PAT ② OAuth），读写同源 |
| W7 | account.js 安全 tab 行 | 绑定态只认 OAuth（`oauthBound`，has_oauth_token 判定）；PAT 归属行显示蓝标"已配置仓库凭据(PAT)，可在 凭据管理 管理或删除；绑定 OAuth 后可授权登录"——**无解绑按钮** |
| W8 | account.js `_accountBindProvider` | 上下文分域：安全 tab（allowPat 缺省 false）= 仅 OAuth 弹窗（未配 → 指路凭据管理）；引导页（true）= 双模式（OAuth 页签 + PAT 表单直达） |
| W9 | app.js `bindProvider` | 引导页入口传 `true`（不变语义，注释归位） |

## 二、边界裁决（本轮钉死）

- **解绑 OAuth 不连带 PAT**：解绑=字段级软删（status=0 + 清 ei token 字段），
  PAT 权威在凭据域不受影响；且 W1 后 ei 不再承载 PAT，连带路径物理消失。
- **has_oauth_token=存在语义**：true=有 OAuth token（绑定关系成立）；
  false=无 OAuth token（未绑定或 PAT 归属）。过期态由
  `token_expired_providers`/status=4 表达，不混入本字段。
- **PAT-only 行删除引导**：蓝标文案含"可在 凭据管理 管理或删除"路径指引
  （与 OAuth 未配置时的凭据引导对称）。

## 三、验收（实跑）

- 行为锁：`tests/test_d102p_pat_domain_split.py` 4 用例（写路径不进 ei token /
  供给链凭据域优先 / 启动回填注册 / 回填解密精确比对语义）
- 上下文分域锁：`tests/test_bind_context.py`（分域签名 / 安全 tab 纯 OAuth
  弹窗 / 引导页 true / 安全行单参调用）
- 同族回归：guide/bind/凭据/分域 7 文件 **36 passed**（口径 v5→v6 行为锁
  同步重写）
- 全家族（含 d101/d102a/c/d/e/g）：**36+ passed**

## 三.5 读路径缺口补收（实弹发现 2026-11-09）

- **GET /settings/credentials 从未暴露泛型 `<p>_pat` 存在性**（D32 时代固定
  形状：gitcode/github/huawei_cloud/codearts 四专用字段）→ 凭据 tab `_hasPat`
  恒 false → 清除按钮永不出现（"配置了 PAT 不支持清理"的根因；PUT 清除链路
  本就存在，缺的是读半截）。
- **修复**：get_credentials 增泛型段——遍历仓库平台卡目录（存量
  gitcode/github 走专用字段不重复），每平台回 `{exists, login}`（只回存在性
  +login，不回 token 值）；凭据 tab 清除按钮判定数据源打通。
- **运维注意**：切换 JWT_SECRET/密钥后，旧密钥加密的 PAT 行变孤儿（读侧
  解密失败）——需用户重新粘贴一次 PAT（当前密钥下重存）。

## 四、遗留

- `_repoWebHost._KNOWN_HOSTS`（四平台字面量兜底表）——集群配置收敛需求
  （grape-req-config-cluster.md）里随"目录即权威"一并收敛；
- 华为自动建号 username=sub 的可读化已由 D102-C 完成（name 派生）。
