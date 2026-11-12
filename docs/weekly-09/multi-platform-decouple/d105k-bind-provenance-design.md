# D105k：绑定域解耦收口（方案 A 无标记版）设计（2026-11-09 定稿）

（用户多轮纠偏驱动：误显示质询 → 推翻"非空即 OAuth"推断判据 → 否决
token_source 过渡标记 → 裁定方案 A 彻底解耦 +"OAuth/PAT 共存能力不要丢失，
门面兼容"）

## 背景与病灶（实查）

D102-P v6 解耦口径：PAT 权威存储=凭据域 `<p>_pat`，ei 归属行 token 恒空。
但解耦**没做完**，残留导致 uid=24 gitcode 行"已绑定+解绑"误显示：

1. **旧接口跨域写**：`PUT /settings/credentials` 专用字段（gitcode_token/
   github_token）仍把 PAT 写进 ei.access_token。未退役原因：`get_user_token`
   （deps）直读 ei，是 tasks/repos 等的供给链。
2. **存量行未迁移**：回填"清空"要求凭据域存在等值副本；专用路径的 PAT 在
   凭据域**无副本**（get_user_token 读的就是 ei 本身，循环比较永不中）→
   永不清洗。且首档循环体引用了不存在的 `json.loads`（函数导入
   `json as _json`），NameError 被内层 except 吞掉——首档在 SQLite 上
   从未生效过（被吞异常的存量缺陷，本批修复）。
3. **前端域口不严**：行渲染 bound 与渠道配置态脱节，"渠道未配置"角标与
   "已绑定+解绑"卡片同屏。

曾评估的 `token_source` 打标被用户否决：彻底解耦后 ei.access_token 只剩
OAuth 一个写入者，"非空即 OAuth"天然成立，标记字段无存在意义。

## 定稿口径

### 终态存储语义（无标记）

- `ei.access_token`：**OAuth 独占**。非空即 OAuth 绑定，
  `has_oauth_token = bool(access_token)` 回归天然语义
- PAT 权威存储=凭据域 `<p>_pat`（token+login）；ei 归属行仅记身份（token 空）

### 门面兼容（OAuth/PAT 共存能力保留——用户裁定"能力不要丢失"）

供给链单入口即门面，双模式兼容不变：

```
get_user_token_for / fetch_user_token（统一门面）
  ① 凭据域 <p>_pat   ← 用户显式配置 PAT=明确意图，生效中（无刷新语义）
  ② ei OAuth token   ← 兜底，带预刷新/自动续期
```

- OAuth 渠道在配 + 用户又配 PAT → **双活**：PAT 生效中（凭据 tab 角标陈述），
  清除 PAT 自动回落 OAuth；两条链互不覆盖
- 本批把 ① 层补进 `get_user_token_for`（deps）——全部既有消费方
  （repos/tasks/templates/user_db/provider 系）自动获得 PAT 优先，
  无需逐个迁移；`get_user_token`（gitcode 专用）改薄委托统一入口
- **能力红线**：PAT 配置路径对 ei 更新用空 access_token（"保留旧值"语义），
  **不得清 ei 的 OAuth token**——否则 OAuth+PAT 共存用户清除 PAT 后无回落
  （行为锁 test_oauth_fallback_preserved_when_pat_configured_on_top）

### 写入路径收敛

- 专用字段路径改写 v6 形态：验证 token 后 `set_credential(<p>_pat)` +
  ei 归属行（access_token 空）+ `mark_identity_pat`；不 clear ei token
  （见能力红线）
- 泛型 platform_tokens 路径不变（本就是 v6 形态）

### 存量迁移（backfill 搬家，幂等，token 保留不断供）

| 证据 | 动作 |
|---|---|
| 凭据域有 `<p>_pat` 且解密后 token 相等（strip 归一） | 清空 ei token + source='pat' |
| refresh/expires 有痕迹（**解密后判空**——encrypt("") 密文非空） | 真 OAuth，保留 |
| OAuth 渠道未配置（client_id 判据，见下） | **搬家**：token 写凭据域 `<p>_pat` → 清 ei token + source='pat' |
| 渠道在配、无痕迹 | 保守保留（误显示已绑定好过误踢 OAuth；重绑自愈）|

渠道配置判据 `_oauth_channel_configured`：meta `oauth_<p>_client_id` 非空，
或卡条目内 client_id/client_key 非空。**卡里"有平台键"不算配置**——
gitcode/github 卡为内置默认形态（无 client_id，走 meta 且常空），OAuth 流程
发不起来；只按"卡在键"判定会让 uid=24 的 PAT 行被误保守保留（实测）。

落写语义：搬家落库走全局连接裸 SQL（与 set_credential 同连接同事务）——
ORM UoW 变更与全局连接混用时 ei 变更曾静默丢失（实测）。

### 前端（account.js 安全 tab 行，用户裁定简化模型）

纯 OAuth 三态，PAT 彻底退出本行（来源标签/patOnly 特殊态删除；PAT 的一切
归凭据 tab）：

| 渠道 | has_oauth_token | 显示 |
|---|---|---|
| 已配置 | true | 已绑定 + 解绑 |
| 已配置 | false | 未绑定 + 绑定按钮 |
| 未配置 | 任意 | 不支持 OAuth（灰ⓘ），绑定/解绑全不渲染 |

### 清除 PAT 级联（四象限矩阵，`_cascade_pat_clear`）

用户实弹：清 PAT 后回工作区仍看到该 PAT 绑定的仓库。根因：清除分支只删凭据，
ei 归属行与 user_repos 缓存不动（列表残留）；反向 github/gitcode 专用清除又
无条件删 ei——OAuth+PAT 共存用户清 PAT 连 OAuth 绑定一起删（能力丢失反向）。

统一收敛 `_cascade_pat_clear(user, provider)`，三处清除入口
（github/gitcode 专用字段、泛型 platform_tokens）全部改走：

| 场景 | 行为 |
|---|---|
| 有 OAuth 绑定（has_oauth_token） | 只清 PAT 凭据（revoke `<p>_pat`）；ei OAuth token 保留=回落能力；仓库列表保留（供给②接管）|
| 无 OAuth | 平台级解绑级联：删 ei 归属行（PAT 行非登录凭据）+ revoke 凭据 + D42 user_repos 缓存清理 → 列表消失、bindings 减少触发 S12f 引导页 |

登录方式保底检查（至少保留一种登录方式）保持既有语义。

### 工程修复（连带实锤）

- `_uow`/ORM 引擎 URL 未编码凭据：现网密码含 URL 保留字符（`@` 等）时 host 被
  解析成 "123@host"，**全部 _uow 身份域写入在现网 MySQL 连不上**（59 实锤
  P1）→ user/password quote_plus 编码（persistence/engine.py）
- `settings.py` v6 泛型路径 `_str` 未定义 NameError（存量活 bug，重复 PAT
  配置即 500）→ str()

## 明确不做

- get_user_token/get_user_github_token 其余消费方签名收敛（供给已统一，挂账）
- 歧义行（渠道在配+无痕迹）不强行搬家——保守保留
- 凭据 tab 三态角标矩阵不动

## 验收

- 行为锁 tests/test_d105k_bind_provenance.py（9 用例）：三档搬家/保留、
  OAuth 痕迹保护、等值副本、幂等、供给不断（PAT 层+优先级）、
  **OAuth 回落能力锁**、专用路径结构守卫
- 安全页三态守卫重写（tests/test_security_tab_bind_states.py）
- 59 真机：渠道在配场景零误搬家、init 零新增错误；本地 verify 库（渠道
  未配置）uid=24 gitcode/github 实搬家（cred 落 <p>_pat、ei 清空、
  has_oauth_token=False）
- `?v=` bump：account.js 66→67
