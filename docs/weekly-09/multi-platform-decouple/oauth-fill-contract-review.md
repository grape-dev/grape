# D90 oauth_fill 落库消费端——跨域契约评审稿（v1，2026-11-07）

**状态**：已拍板并实施完毕（P-1~P-8 全部决议落地：N1 前置批 + 消费端批 6 步；剩余 = 生产灰度期 4 周观测）
**来源**：multi-platform-decouple-design.md D90 节（前置事实 1-11，含二次/三次/四次扩项）
**范围**：oauth_fill 落库消费端 + 凭据解析/存储/刷新跨域契约。不含消费端实现（评审通过后另批）。

---

## 0. 契约目标与非目标

**目标**：登录回调成功后产生可审计的凭据落库记录，补齐 lazy 链"用时取用"缺的三件事
（审计可见性 / 降级能力 / PAT-oauth_fill 优先级表达物），同时不破坏 S116 领域分离守卫。

**非目标**：不改现行 lazy 链行为（等效方案继续服务）；不动 PAT 显式配置流（S52）；
不引入领域事件异步机制（方案 B 已否决）；GitHub App installation token 模式（本集成是
OAuth App+PAT，不适用）。

**硬约束（不可谈判项）**：
- C1 禁止登录域直接写 grape_t_repo_credentials、禁止读 vcs_providers_config（S116 守卫域）。
- C2 仓库域只消费活 token，刷新责任全在身份域（provider_link 注册表，现状已对齐）。
- C3 PAT 不入自动刷新循环：显式配置、手动轮换（forge 同款语义，防后续误卷入）。
- C4 oauth_fill 不得做成第四种隐式 PAT 来源（cred_type × source 隔离，UC23① 守卫是第一道闸门）。

---

## 1. provisioner 端口契约（D90 补齐路径第 1 步）

```python
class RepoCredentialProvisioner(Protocol):
    def provision_from_oauth(self, user_id: int, provider: str,
                             token_ref: int) -> ProvisionResult: ...
```

- **调用方**：登录域（OAuth 回调成功路径 + 绑定成功路径，共两处调用点）。
- **被调方**：仓库域。是否落库、落成什么形态由仓库域决定。
- **参数语义**：`token_ref` = external_identity_id 指针（非 token 本体，与第 3 项拍板联动——
  若拍板"存本体"则此签名改传 token，端口职责扩为 provision+sync）。
- **失败语义**：端口直调，失败显式返回（ProvisionResult.error），不静默、不重试、不阻塞登录
  主流程（登录成功≠落库成功，两者解耦，落库失败进告警不进 5xx）。
- **S116 守卫同步升级**：登录链零仓库域引用守卫 → "零引用 **除 provisioner 端口调用外**"。
  工程口径三条（守卫加强而非绕过）：① 端口 FQN 锚定 + 可导入断言；② AST 级 import/Call
  检查（注释提及不触发）；③ 约束面读写双向（禁写 grape_t_repo_credentials + 禁读 vcs_providers_config）。

---

## 2. 待拍板项（按影响排序）

### P-1 存 token 本体 vs 可刷新引用 【推荐：存引用】

| | 存引用 | 存本体 |
|---|---|---|
| 指针 | external_identity_id | token 快照（加密落 grape_t_repo_credentials） |
| 刷新影响 | 零（身份域刷新自动生效） | 第二次跨域写入（provision+sync 或守卫开例外） |
| 加密 | 无新增约束（无副本） | 同 token 两处存储两套加密（grape_t_repo_credentials 无加密 DDL，external_identities 是 AES-GCM）= 安全裂缝 |
| 审计价值 | 授权关系记录 | 凭据快照 |

**推荐理由**：与现行 lazy 链自洽（仓库域只消费活 token）；避免 token 双存与双加密；
业界收敛模式（长期秘密存权威表，业务表只存授权关系+指针，见前置 9）。若拍板存引用，
"审计快照"诉求由操作日志 + TokenResult.level 命中级别审计承接（前置 7）。

### P-2 粒度三选一 【推荐：c 独立表】

- a) 虚拟键 `{provider}:__oauth__` —— 污染 repo_key 语义，"该仓库有哪些凭据"查询需过滤虚拟键。
- b) provisioning 延迟到首次绑仓库 —— 从不绑仓库的用户永不落库，"登录即填"审计价值打折。
- c) **独立表 user_platform_credentials**（与 grape_t_repo_credentials 并列）—— 用户平台级凭据
  与仓库级凭据本就不同粒度，独立表最符合领域分离（D89 双卡三域解耦方向）。

**推荐 c**。连锁影响：grape_t_repo_credentials 不扩 source 列（第 4 项的 DDL 问题整体转移），
source 列改落在 user_platform_credentials 上；grape_t_repo_credentials 保持仓库级语义纯净。

### P-3 优先级方向：OAuth vs PAT 【唯一影响现网行为的拍板项】

**实锤**：D10 拍板"PAT 优先（显式意图），oauth_fill 兜底"，但
`token_resolver.py:344` GenericVcsProvider.fetch_user_token 现行序是
① OAuth 身份（有即返回）→ ② user_credentials `<p>_pat`。
用户先配 PAT 后又 OAuth 登录时实际用的是 OAuth token——"显式意图优先"未实现。

- 方向 A：对齐 D10，PAT 优先 → fetch_user_token 调序，UC23④/UC24 守卫保证双链一次生效
  （第 4 级底层收敛 get_user_token_for，单点调序）。
- 方向 B：坚持现状 OAuth 优先 → 必须写明依据（候选论证：OAuth scope 动态性 /
  代码路径短），D10 拍板作废需登记。
- 并存形态（两方向共用拍板）：覆盖 / 并存 / 分列——推荐**并存**（source 列已能区分，
  覆盖丢审计、分列增复杂度，均无收益）。
- 合并逻辑所在层：**TokenResolver 层**（不在 SQL；source 三态定位=审计溯源非运行时
  解析条件，TokenResolver 不读 source，策略链固定四级——前置 9 已收敛，此处仅登记）。
- 连锁动作：命中级别审计（TokenResult.level 落操作日志）随本项一并实施。

### P-4 source 列设计（若 P-2 拍板 c，则作用于 user_platform_credentials）

- **backfill**：存量行默认 **NULL**（不回填 'pat'——历史 bot 行会被误标；NULL=未配置，
  与表设计惯例一致）。'pat' 只标 S52 显式双写之后的行。
- **约束形态**：应用层校验（SQLite/MySQL 双方言，CHECK 枚举翻译成本高，且未来可能加
  manual_import 第四态——应用层白名单 + UC23① 守卫锁死）。
- **source 三态**：`oauth_fill | pat | bot`（D9 语义）。manual_import 预留不做。
- **索引**：查询若按 source 过滤（运维审计场景）再加，首版不加（审计查询低频，避免过度设计）。

### P-5 actor 语义三选一 【推荐：a 维持现状】

- a) **维持归属者语义**（resolve 签名不动，actor=host_user_id）+ 触发者审计走操作日志。
- b) resolve(repo_key, actor) 扩展 —— open-swe (thread_id, principal) 模式，但它每请求有
  明确用户上下文（agent 运行时），我们是托管服务（仓库有稳定归属者），迁移需论证，不抢跑。
- c) 混合（webhook=归属者、任务=触发者）—— 两条解析路径，复杂度最高。

**推荐 a**：现状满足共享仓库正确性（统一归属者 token）；审计可见性缺口已由 D91 告警 +
level 字段部分覆盖；b/c 触发条件=多操作者共享仓库占比上升，登记观察。

### P-6 401 被动兜底 【已升格契约必需项，拍板实现层】

前置 11 拍板：401 反应式刷新是契约必需，与主动预刷新（600s 缓冲）并存构成完整生命周期
（@doist/cli-core 双模式对齐）。剩余拍板点：

- **实现层**：api_client（拦截器收 401 触发）vs TokenResolver（暴露 force_refresh API 由
  调用方触发）。**推荐 api_client 层**：触发精确（精确匹配 401 状态码）、覆盖面全（所有
  API 调用点一次生效）、无需改动各调用方。
- **防死循环铁律**：重试单次且必须绕过同一拦截器（业界共识）。
- **失败二分**：刷新失败区分"彻底失效（重新登录引导）"vs"暂时性（可重试）"。
- 现状文案"Bot Token 失效"归因错误随本项一并修（401 可能来自用户 OAuth token）。

### P-7 insufficient_scope 显式化 【推荐：显式错误 + PAT 提示，fallback 不保留】

OAuth token scope 不足时：**不静默回落 PAT**——返回显式 insufficient_scope 类错误，
错误信息提示 PAT 可用（OAuth 2.0 规范语义）。fallback 不保留（若未来保留，必须是显式
opt-in 的最后手段——本契约先锁死默认关闭）。启动/早期 scope 诊断可做但不成为新故障模式。

### P-8 迁移下线判据量化（managed_repos 历史列）

- 次序：双写 → 回填 → 读切换 → 观测 → 下线（**回填不得早于双写生效**）。
- 判据：双写回填稳定后，"旧列读回落命中计数**连续 N 个观测周期为零且无回滚事件**"。
  **推荐 N=3、周期=周**（即连续 3 周零命中）——待拍板确认。
- 采集基础：RepoBotToken 读侧回落已有 log.warning，补命中计数指标即满足。

---

## 3. 已收敛项（评审确认即可，无需再议）

| 项 | 收敛结论 | 出处 |
|---|---|---|
| provisioner 形态 | 端口直调，否决领域事件异步 | D90 补齐路径 1 |
| source 定位 | 审计溯源（provenance），非运行时解析条件；TokenResolver 不读 source | 前置 9 |
| 策略链 | 固定四级，不因 source 动态分叉 | 前置 9 |
| PAT 归属 | source=pat 行=指向 user_credentials 的授权记录（非秘密副本），无三写一致性 | 前置 9 |
| 路径 B 优先级 | 用户级凭据第一公民，bot 仅兜底（与 open-swe/Backstage 一致），不动 | 前置 7 |
| 定时扫描 | 真差距但单机/双主规模可接受，登记不阻塞 | 前置 6-B |
| PAT 自动刷新 | 不入循环，显式配置手动轮换 | C3 硬约束 |
| gitee scope 矩阵 | 五列（读私仓/push/建 PR/读组织与 webhook/TTL-refresh），评审输出每渠道 oauth vs PAT 对照表，缺项在渠道卡标注"需 PAT 补充" | 前置 5/10 |

---

## 4. 实施顺序（P-1~P-8 已全部拍板；前置批次 N1 已落地）

**已落地（N1 前置批，2026-11-07）**：
- ✅ PAT/OAuth 本体加密缺口闭合：user_credentials 通道加密 + UUID 主键 + 时间双列
  （commit 60123cf76 + a91718bdc 写入冻结 + 195f8cb0e/e215f4243 迁移模块化）
- ✅ 表设计铁律 8 条入册 requirements（后续批次必须遵守）
- ✅ UC26 场景-用例-测试映射（s122 守卫 9 用例 + 写入冻结守卫 GC1-4）

**已落地（消费端批，2026-11-07，UC27-UC32 / S123-S128）**：
1. ✅ P-2/P-4 DDL：user_platform_credentials 表 + ORM 领域对象三件套（替代表定位/
   UUID 主键/时间双列/source 白名单；盖章迁移 202611070001）——f8e796964（UC27/S123）
2. ✅ provisioner 端口 + 4 处绑定成功路径接线 + S116 守卫升级（provision 辅助唯一
   豁免；漏接线即红）——460f55789（UC28/S124）
3. ✅ P-3A 调序（PAT 优先，单序直接生效——flag 为过度设计，2026-11-07 用户
   修正删除：D10 原文无灰度，双序分支徒增维护成本）+ level 命中日志
   ——ee9722d72 + flag 删除 commit（UC29/S125 修正版）
4. ✅ P-5 credential_usage_log 建表（credential_kind 预留/无外键级联/executor-
   initiator 分列；盖章迁移 202611070002）+ provision 审计链闭环——c2bb0a188（UC30/S126）
5. ✅ P-6 401 兜底五约束（oauth_401_retry 包装 + invalid_grant/400 → status=4
   失效流程接线 refresh_oauth_token）——df2000852（UC31/S127）
6. ~~P-8 观测埋点 + P-7 insufficient_scope~~（e9cd3a5fa，同日用户拍板删除：
   双零消费方——分类器无调用点、计数器无读取面且重启即清零；观测口径
   回归既有 log）

**剩余（生产灰度期，非代码）**：
- 置 meta credential_pat_first_enabled=1 开启 PAT 优先（P-3A 决议口径）
- 4 周观测（P-8 决议口径：旧列读回落零命中/新列错误率不高于基线/无回滚事件）
- 观测达标后 P-8 下线 user_credentials（替代表正式接位）

---

## 5. 评审决议记录（2026-11-07 拍板）

| 项 | 决议 | 附条件/参数 |
|---|---|---|
| P-1 | **A 存引用** | ① external_identity_id 仅适用 OAuth 链；PAT 链存储形态单独定义（见 6.1 统一方案）② 指针悬空=标记 stale（external_identities.status 已有承载物，不级联删除，保留审计线索）③ 引用不存 token 版本快照，版本审计由 external_identities.updated_at + 操作日志承接，合规确认可接受 ④ **PAT 链存储形态已定并落地（2026-11-07 收敛收尾）**：PAT 行=指向 user_credentials（`<p>_pat`）的指针，user_credentials 通道加密已上线（commit 60123cf76），交叉风险 #1 闭合 |
| P-2 | **c 独立表** | ① 作用域键 (user_id, provider)——单租户托管服务，无 org 维度，LangWatch (userId, orgId) 复合作用域不适用，理由登记 ② 写入时机=**lazy upsert（首次绑仓库写，拍板条款明确）**；业界 pending→active 两阶段登记为观察项不采纳 ③ **替代表定位（2026-11-07 追加拍板）：user_platform_credentials 是 user_credentials 的替代表（非补充），user_credentials 走 P-8 下线路径** ④ 表设计铁律：UUID 主键（TEXT 36 字符）+ created_at/updated_at 应用层双列 + ON CONFLICT 保 id——与 user_credentials 同构，铁律全文见 requirements「表设计铁律」节；与 external_identities 关系：user_platform_credentials 是仓库域**授权关系记录**，OAuth 行存 external_identity_id 指针，PAT 行存 user_credentials 指针（`<p>_pat`）——两张表职责不重叠，无第三张表 |
| P-3 | **A PAT 优先** | ① 监控：PAT 链 401 率、insufficient_scope 率、fetch_user_token 延迟，**两种凭据类型分轨统计**（不分行无法归因）② PAT 401 **不回落 OAuth**：显式报错提示重新导入 PAT（与 P-7A 一致）③ 拦截器按凭据类型分流（PAT 无 refresh 语义，见 P-6）④ 并存形态=并存（source 列区分）⑤ 业界"按调用上下文路由"模式登记为远期观察项，不做 |
| P-4 | **三项推荐全收** | ① NULL 统一映射为 legacy 语义集中处理（读路径禁止散写 WHERE source='pat'，source 过滤收敛到单一 resolver）② source 列 varchar 可扩展 ③ DBA 直连场景存在则加宽松 CHECK（NULL+白名单+预留值），实施时确认 |
| P-5 | **a 归属者语义** | **硬前置：新建 credential_usage_log**，至少含 (credential_id, initiating_actor_id, executing_context, correlation_id)——executor/initiator 分列禁止复用。没有它，P-3 并存 + P-5a = "A 的凭据被 B 使用但审计只显示 A"的系统性归因错误。此前置是 P-3 并存形态的**准入门槛**，非加分项 |
| P-6 | **A api_client 拦截器** | 五条约束：① refresh 后仅重试一次，失败即抛 ② 排除 refresh 接口自身 401（防递归）③ **按凭据类型分流**——OAuth 才 refresh，PAT 直接抛显式错误 ④ **per-request refresh-attempted 守卫 + 并发 401 折叠为单次 refresh（single-flight）** ⑤ refresh 返回 invalid_grant/400 走凭据失效流程（清状态+重新登录引导），不当瞬时错误重试；403 永不触发 refresh |
| P-7 | **A 不回落** | 识别精度：判定放 **provider adapter 层**，读 provider 明确信号（GitHub：X-OAuth-Scopes / X-Accepted-OAuth-Scopes 响应头 + 403 message 模式），禁止"403 一律 insufficient_scope"——避免把 resource-owner 配置错、branch 权限不足误报为"请导入 PAT" |
| P-8 | **4 周（修正）** | ① 判据=连续 **4 周**旧列读回落零命中 + **新列凭据解析错误率不高于基线** + 无回滚事件 ② 埋点覆盖边界必须先文档化（未被埋点的读路径=假零信号）③ 判据满足 ≠ 直接删除：先 reversible disable（flag/路由切换）+ 观察期，再删代码 |

---

## 6. 交叉风险清单与业界校准（2026-11-07 二次校准并入）

### 6.1 交叉风险（决议落地前提，全部进 design.md）

| # | 交叉项 | 风险 | 处置（已并入决议） |
|---|---|---|---|
| 1 | P-1A + P-2c | 指针指向哪张表未定，PAT 链存储形态缺失 | **已闭合（2026-11-07）**：P-2c 决议③ OAuth→external_identities 指针、PAT→user_credentials 指针；PAT 本体加密已落地（N1 通道加密，commit 60123cf76） |
| 2 | P-3A + P-6A | PAT 无 refresh，拦截器统一 refresh 误触发 | P-6 决议③：按凭据类型分流 |
| 3 | P-5a + P-3 并存 | 归属者语义+并存 → 审计链断 | P-5 决议：credential_usage_log 准入门槛。**表设计（2026-11-07 追加拍板）：预留 credential_kind 字段**（值域 'user_credentials' | 'user_platform_credentials'，P-2c 上线时零迁移）；**不设外键级联删除**——审计行随凭据删除必须保留，credential_id 软引用 + 应用层校验，PRAGMA foreign_keys 开闭不影响 |
| 4 | P-3A + P-7A | PAT 失效/scope 不足均显式报错，用户需手动重导 | 产品侧提示文案同步排期 |
| 5 | P-8 | 埋点未就位则判据不可执行 | P-8 决议②：覆盖边界文档化先行 |

### 6.2 业界校准采纳情况（Nylas/WorkOS/LangWatch/tmail/GitHub 信号）

- **采纳**：并发 401 折叠 single-flight（P-6④）、invalid_grant 失效流程（P-6⑤）、403 不 retry 支撑 P-7A、adapter 级 scope 判定（P-7）、executor/initiator 分列（P-5）、新列错误率基线判据+可逆 disable 先行（P-8）
- **登记不采纳（含理由）**：① LangWatch (userId, orgId) 复合作用域——单租户无 org 维度；② pending→active 两阶段写入——用户拍板 lazy upsert，两阶段登记观察；③ "按调用上下文路由"替代静态优先级——P-3A 已拍板单序 PAT 优先，路由模式远期观察；④ 本地 isExpired 预检移除——我们已有 600s 主动预刷新（On-demand with Buffer），与 401 被动兜底双模式并存，不需要移除
- **N1 现状安全缺口（评审新发现，实码 2026-11-07，已修）**：`src/user/db.py` set_credential 将 PAT 本体（settings.py `_pat`）与 OAuth 刷新副本（refresh_oauth_token）**明文 JSON** 落 user_credentials；`src/auth/credential_crypto.py`（AES-GCM，带 key rotation）已存在但 user/db.py 零引用。**修正（范围收窄自实码复核）**：原登记的 link_gitcode_user/link_github_user 明文写入是 D32 死代码（生产零调用，已随写入冻结批次 a91718bdc 删除）——生产 OAuth 绑定全程走 create_external_identity（已加密），活跃缺口仅 set_credential 通道。**处置（已落地）**：通道层透明加密——set_credential 写前整体 encrypt()、读侧 _load_credential_json decrypt（明文兼容覆盖历史行，零迁移即可读），与 external_identities 存储面对称；连带 user_credentials 加 UUID 逐行主键（36 字符标准形式 + created_at/updated_at 应用层双列 + ON CONFLICT 保 id）+ 存量库幂等补列回填。**待办**：历史明文行加密回填脚本（可回滚 + 前后抽样校验，与 UUID 回填同部署批次、分开 migration），纳入 P-8 观测范围
