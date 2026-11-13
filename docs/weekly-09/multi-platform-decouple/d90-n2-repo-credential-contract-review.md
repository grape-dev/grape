# D90-N2：仓库凭据契约评审前置三项——实码核对与拍板稿（2026-11-07）

**来源**：design.md 挂账 #10（D90 前置事实 6-8，2026-11-07 用户评审沉淀）。
三项均为**契约级决策**：先实码核对现状 → 给出方案选项 → 用户拍板后才动代码。

**状态**：已拍板（三项全 A）并落地——519757650（①值域守卫/②链序锁定/
③docstring 定性）；守卫测试 test_n2_1（test_s119）。派生挂账 (a)(b) 见
N3/N4 评审稿。

---

## 前置①：PAT 权威存储归属——grape_t_repo_credentials 存不存 PAT？

### 现状（实码核对）

| 存储面 | 现状 | 证据 |
|---|---|---|
| user_credentials `<p>_pat` | **PAT 唯一落库点**（UserToken 级） | settings.py:552 `set_credential(user_id, f"{_p}_pat", ...)` |
| external_identities | PAT 双写归属源（S52 双写，status 语义） | settings.py:231 注释 |
| grape_t_repo_credentials | **零 PAT 行**——cred_type 值域只有 bot 级（bot_gitcode_token/bot_github_token 等），全仓无写入点写 `<p>_pat` | grep 全仓：grape_t_repo_credentials 写入只有 db_setup 回填（managed_repos 列搬迁）与 ORM Repository（零 PAT 调用方） |
| TokenResolver 第 4 级 | UserToken 级 PAT 消费走 `get_credential(user_id, "<p>_pat")` | token_resolver.py fetch_user_token |

**结论：grape_t_repo_credentials 从未存过 PAT。**「裂缝」只在**语义层**：grape_t_repo_credentials.source
列（D92）三态（sync/derived/legacy）定义的是"仓库级凭据来源"，而 PAT 根本不在
这张表里——若未来有人把 PAT 同步进 grape_t_repo_credentials（比如图方便复用表结构），
source=pat 的语义无定义可依。

### 选项

- **A（推荐）禁入式**：grape_t_repo_credentials **永不存 PAT**——cred_type 值域白名单
  守卫（UC23① 已锁写入文件面）追加语义层：`*_pat` 后缀禁止写入 grape_t_repo_credentials。
  PAT 权威 = user_credentials（用户级），仓库消费永远经 TokenResolver 第 4 级
  动态取。与 D90 P-2c 替代表定位同构：**一张表一个粒度**。
- **B 同步副本式**：PAT 允许同步进 grape_t_repo_credentials（source=pat，查询视图语义）。
  收益：仓库维度直接查；代价：双写一致性、审计双源、P-8 下线口径复杂化。

**建议 A**。理由：PAT 是"用户对平台的显式意图"，不是仓库属性；S116 已锁登录域
零仓库域引用，B 会引入第二个 PAT 落库点=亲手制造双权威。

---

## 前置②：TokenResolver 策略链——固定四级还是按 source/cred_type 动态排序？

### 现状（实码核对）

- 策略链**固定四级**（token_resolver.py:1198）：session → RepoBotToken →
  GlobalBotToken → UserToken。
- 唯一的"动态性"在第 4 级内部：P-3A flag（credential_pat_first_enabled）切换
  PAT/OAuth 优先（ee9722d72）。
- caller 侧有两处手工剪裁（`strategies[1:]` 跳过第 1 级：db.py get_repo_bot_token、
  s128 测试），属调用方语义裁剪，非链排序变化。

### 选项

- **A（推荐）固定序 + 级内策略 flag**：链序=架构常量（每级语义=存储面优先级，
  变更=架构变更走评审）；source/cred_type 只做**元数据**（观测分轨/审计归因/
  级内优先级 flag），不驱动排序。与 P-3A 已落地形态一致，P-8 观测口径稳定。
- **B 动态排序**：按行 source/cred_type 重排策略链。收益：灵活；代价：同一
  repo_key 不同凭据形态命中不同级——P-8「旧列读回落零命中」判据失去意义
  （命中序列不可比），观测面作废，回滚不可逆。

**建议 A**。B 直接摧毁刚落地的 P-8 灰度判据（挂账 #12 同理）。

---

## 前置③：UserToken actor 语义显式化

### 现状（实码核对）

两处调用方，actor 语义**不同**：
1. TokenResolver 第 4 级（token_resolver.py:1121）：`fetch_user_token(host 账号
   username, host_user_id)`——**user_id = 仓库归属者（host_user_id）**，路径 B
   托管场景取"托管人的凭据"，非 API 调用者。
2. webhook.py:1002 A2A fallback：`fetch_user_token(user_hint.username, user_hint.id)`
   ——user_hint 是**任务上下文携带的用户身份**（触发相关）。

**设计意图（既有实现自洽）**：token 的语义是"用谁的凭据"（归属者），不是
"谁发起的请求"（触发者）。触发者审计已由 auth_audit.log_audit +
credential_usage_log（步 4 落地，initiating_actor_id 列）承担。

### 选项

- **A（推荐）定性确认 + 注释锁定**：user_id 参数语义 = **凭据归属者**
  （credential owner），非 API 调用者；触发者审计走 usage_log 的
  initiating_actor_id。fetch_user_token docstring 显式声明（防后人当 bug 修签名）。
- **B 改双参数**：拆 owner_id / actor_id。代价：签名变更波及全部调用方+测试，
  收益为零（审计已有专门表列）。

**建议 A**。挂账 #14 已锁定三级全空失败语义，A 补齐后 actor 语义闭环。

---

## 拍板请求（三项）

| 项 | 建议 | 影响 |
|---|---|---|
| ① PAT 权威存储 | A 禁入式（grape_t_repo_credentials 永不存 PAT + 守卫） | 语义闭环，零迁移 |
| ② 策略链排序 | A 固定序 + 级内 flag（source=元数据） | 保住 P-8 观测判据 |
| ③ actor 语义 | A 归属者定性 + docstring 锁定 | 防误修，零签名变更 |

三项全 A = 一个小批次落地（守卫+docstring+文档同步），无现网行为变更。
