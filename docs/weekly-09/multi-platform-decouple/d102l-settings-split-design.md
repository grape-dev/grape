# D102-L：设置页「第三方账号 / 仓库凭据」职责拆分方案

> 状态：待用户确认（方案先行，确认后实施）
> 日期：2026-11-08
> 关联：D102-K（引导页数据源分离，同族——双卡两轴分离的设置页落点）
> 触发：用户实弹质疑「第三方平台的配置职责多了，要拆解」
>
> **边界声明**：本次改动仅涉及 UI 入口归组与弹窗职责瘦身；`/settings/credentials`
> 写入接口、external_identities、user_credentials 存储模型均不变。
> 与 D100/D102 系列「不合并存储、只归拢入口」的分寸一致。

---

## 一、现状问题（实码定位）

「安全 tab → 第三方账号」卡当前承载 **5 种职责**：

| # | 职责 | 实码位置 | 本质归属 | 处置 |
|---|---|---|---|---|
| 1 | 登录身份绑定（OAuth 授权） | `/auth/bind/{p}` → external_identities | 账号绑定域 ✓ | 保留 |
| 2 | 仓库 PAT 手动录入（模态框 token tab） | account.js:991 → **`/settings/credentials`** | 仓库凭据域 ✗ 入口错位 | 入口迁凭据管理 tab |
| 3 | token 过期告警 + 重绑定 | token_expired_providers（grape_auth.py:1164） | 仓库凭据域 ✗ 入口错位 | 告警迁凭据管理 tab |
| 4 | 解绑连带仓库管理（webhook 仓库检查/um 列清理/user_repos 清理，147 行） | grape_auth.py:994-1140 | **解绑的合法跨域副作用**（非错位） | 检查保留原样；清理逻辑本次不动，后续可评估委托仓库域事件 |
| 5 | 登录安全兜底（至少保留一种登录方式、placeholder 邮箱清理） | grape_auth.py:1058-1076 | 账号绑定域 ✓ | 保留 |

> **职责 4 定性澄清（评审修正采纳）**：解绑是账号绑定操作，它的保护性检查
> （"解绑后托管仓会断粮，确认吗？"）属于**解绑流程本身**，不是仓库凭据域
> 的职责——原表标"错位"与目标结构"原样保留"自相矛盾。正确归类：检查是
> 解绑保护的合法跨域查询；user_repos/um 清理是解绑的级联副作用（保留触发
> 点，本次不改；后续重构可评估委托仓库域事件，非本次范围）。
>
> **业界参照（外部评审 paste_15）**：①GitHub 明确要求操作凭据与登录账号
> 分开保存，设置页不通过账号绑定获取操作权限——绑定卡/凭据卡分离方向被
> 直接印证；②GitLab PAT 自 v15.3 归 Settings → Access Tokens，与账户设置
> 分离——PAT 入口归凭据管理对齐；③成熟 OAuth 实现明确"唯一登录方式拒绝
> 解绑+自动晋升备选"——登录方式保护属解绑流程固有语义，保留安全 tab 正确；
> ④级联清理最佳实践是集中协调而非内联——147 行清理逻辑归属后续评估委托
> 仓库域事件，本轮不动；⑤失败语义："不可用的授权决策不是显式拒绝"——
> PAT 文案须定位为正式路径（显式回退）而非降级。
>
> **信任模型论证（外部评审 paste_16/17 二次参照）**：PAT="本人直接操作"
> 的长期持有人秘密（长期有效、爆炸半径大、需最小权限+定期轮换）；OAuth=
> "授权第三方代理"的委托关系（同意屏幕+scope 最小化+短生命周期可刷新）。
> 两者是不可调和的两种信任模型——合并流程会诱导用户在交互式绑定中"顺便"
> 生成高权限静态密钥，模糊"我正在授权什么"的认知。业界共识模式=
> **显式的分离 + 清晰的回退路径**：本方案（绑定弹窗 OAuth-only + 跨 tab
> 引导 + 凭据 tab 独立配置上下文）正是该模式的标准实现。

**关键发现**：手动 PAT 的写入接口本来就是 `/settings/credentials`（凭据
管理 tab 的接口，account.js:991）——"仓库凭据配置"物理上已住在凭据域，
只是**入口**错挂在绑定弹窗里。本方案不是搬家，是入口归位。

**hwcloud 特殊性**：纯登录渠道（不产仓库凭据）。在绑定卡里正常展示
（OAuth 绑定/解绑合法）；在仓库凭据卡里不出现（无 PAT 概念），天然自洽。

---

## 二、目标结构

### 安全 tab（职责：账号与登录）

```
├── 密码管理（不动）
├── 邮箱管理（不动）
├── 第三方账号（职责单一化：账号绑定关系）
│   ├── 所有支持平台正常展示（/auth/providers 卡驱动：gitee/gitcode/
│   │   github/gitlab/hwcloud）
│   ├── 已绑定行：平台 logo + 身份信息（头像/external_login/绑定时间）
│   │   + 「解绑」按钮
│   ├── 未绑定行：平台 + 「绑定」按钮
│   └── hwcloud：与仓库平台同形态展示（OAuth 绑定/解绑完全合法）
└── 注销账号（不动）
```

**解绑语义保持**：
- 仓库依赖检查（webhook 仓库清单 409 确认）保留——解绑 gitee 这类
  仓库平台，托管仓断粮风险必须拦截（grape_auth.py:994-1140 原样）
- 登录方式保护保留——「至少保留一种登录方式」检查对全平台生效
  （hwcloud 解绑时若它是唯一登录方式且无密码，仍拦）

### 凭据管理 tab（职责：给 grape 的钥匙）

```
├── 第三方仓库凭据（新增卡）
│   ├── 平台列表 = 仓库平台卡目录（/api/settings/vcs/platforms：
│   │   gitee/gitcode/github/gitlab，卡驱动）
│   ├── 每平台行：平台名 + PAT 配置状态（已配置●/未配置○）
│   │   + [配置 PAT] 按钮 → PAT 表单（模态框）
│   ├── PAT 表单：token 输入 + 验证 + 保存（/settings/credentials，
│   │   接口与写入路径原样——github_token/gitcode_token 走 legacy 字段，
│   │   gitee/gitlab 走 platform_tokens 泛型）
│   ├── PAT 过期告警：token_expired_providers 有记录 → 行内红标
│   │   「已过期，仓库操作将失败」+ [重新配置]
│   └── PAT 创建引导：目录卡 pat_create_url 外链（勾选 projects 权限提示）
└── 云平台凭据（华为 AK/SK，不动）
```

### 绑定模态框（瘦身）

| 现状 | 改后 |
|---|---|
| OAuth tab + 手动 token tab 双页签 | **仅 OAuth 授权**（单按钮流） |
| OAuth 未配置时显示 token tab | OAuth 未配置时显示「该平台暂未开通 OAuth 授权，请到 凭据管理 配置 PAT 以启用仓库操作」+ 跳转链接（PAT=显式回退正式路径，非降级方案——评审文案修正采纳） |
| 保存后跳转项目页 | OAuth 授权回调后照旧 |

---

## 三、交互流变化（用户视角）

| 场景 | 现在 | 改后 |
|---|---|---|
| 用华为账号登录 | 绑定弹窗含 token tab（无意义——华为无仓库 PAT） | 绑定卡内 OAuth 一键授权，完 |
| gitee 用户配 PAT | 安全 tab → 绑定 → 切 token tab → 粘贴 | 凭据管理 → 第三方仓库凭据 → gitee 行 [配置 PAT] |
| gitee token 过期 | 安全 tab 红色告警卡（登录页里看仓库凭据告警，别扭） | 凭据管理 tab 红标（凭据坏了去凭据页修，直觉） |
| gitee 解绑 | 安全 tab 解绑 → 仓库影响确认 | 同左（保留）——但弹窗文案补一句「同时清除该平台 PAT」 |
| hwcloud 解绑 | 安全 tab → 登录方式保护拦截 | 同左（不变） |
| 引导页绑定 | 仓库平台目录（D102-K 已修） | 不变 |

---

## 四、技术改动点

### 前端（account.js + i18n + index.html bump）

1. `_accountRenderSecurity`：第三方账号卡拆出仓库平台（渲染列表 =
   /auth/providers 全量保留，但仓库平台行内不再有 token 相关元素——
   仅绑定/解绑）
2. `_accountBindProvider` 模态框：删 token tab 分支，仅 OAuth 流；
   OAuth 未配置 → 提示 + 凭据管理跳转链接
3. `_accountLoadCredentials`：新增「第三方仓库凭据」卡
   - 数据源：/api/settings/vcs/platforms（平台清单）
     + /auth/me（token_expired_providers 过期态）
     + /settings/credentials（已配置态——接口现成）
4. PAT 表单组件：从绑定模态框的 token tab 迁移（_accountDoManualBind
   原样，入口换了）
5. i18n：`third_party_accounts`→保留；新增 `third_party_repo_credentials`
   （zh:第三方仓库凭据 / en:Repository Credentials）
6. index.html：app.js bump v221→v222

### 后端

**零改动**。核查结论：
- `/settings/credentials` PUT 已支持 platform_tokens 泛型（N5 批就绪）
- `/auth/me` 已含 bindings/identities/token_expired_providers
- unbind_provider 仓库检查/登录保护逻辑对两类场景天然兼容
  （仓库平台解绑走 409 确认；hwcloud 解绑走登录保护）

### 场景用例（tests/test_d102l_settings_split.py）

1. 引导数据源分离不回潮（d102k 守卫延续）
2. 第三方账号卡：hwcloud 行存在且无 PAT 元素；gitee 行有解绑按钮
3. 绑定模态框：无 token tab（OAuth-only 断言）；OAuth 未配置时出现
   凭据管理跳转
4. 凭据 tab：仓库平台四行在册；hwcloud 不在册；过期告警红标断言
5. PAT 表单走 /settings/credentials（接口断言，legacy/泛型双路径）
6. 解绑保护：仓库平台 409 确认链 + hwcloud 登录保护链（后端已测，
   前端入口断言）

---

## 五、不做的事（防止过度设计）

- 不新建后端聚合接口（前端两个现成接口足够）
- 不动 external_identities 双写语义（S52：PAT 绑定写身份表是
  token_resolver 消费链的依赖，动它=凭据治理级改造）
- 不做"每个平台独立凭据子页"（行内展开足够，平台数 ≤6）
- 不迁移 hwcloud（它在两轴的语义本就不同，分流即正确）

---

## 六、验收口径

| 断言 | 验证方式 |
|---|---|
| 第三方账号卡只剩绑定/解绑（无 PAT 元素） | 守卫用例 DOM 断言 |
| hwcloud 正常展示绑定/解绑 | 同上 |
| 仓库凭据卡 hwcloud 不出现 | 守卫用例 |
| PAT 保存落 /settings/credentials 且 legacy/泛型双路径 | 守卫 + 既有后端测试 |
| 解绑 gitee 有仓库影响确认；解绑 hwcloud 有登录保护 | 既有后端测试 + 前端入口断言 |
| token 过期红标在凭据 tab | 守卫用例 |
| 登录页/引导页不回归 | test_d102k 延续 |

---

## 七、D102-M 实施记录（2026-11-08，99a8a56e5）——双范式并存收口

外部评审四层模型（存储/解析/验证/UX）对账结论与两处补齐：

| 层 | 业界模式 | grape 现状 | 处置 |
|---|---|---|---|
| 存储 | 物理分离（manual_pat ≠ oauth 字段） | ✅ 已是策略二（`<p>_pat` ∈ user_credentials；OAuth ∈ external_identities；S52 防双路径） | 不动 |
| 解析 | 显式优先级可审计 | ✅ Manual > OAuth（fetch_user_token ①②） | 维持 D10 拍板（PAT-first；业界 PAT-as-fallback 分歧记录） |
| 验证 | origin-agnostic AuthContext | ⚠️ 裸 token，无句柄 | 归 P8 既有挂账（本轮评审给了新必要性论据） |
| UX | 来源徽章 + 显式回退 | ❌ 缺 | **本轮补**：凭据 tab 三态来源徽章 |

补齐两件：
1. **来源徽章**：凭据 tab 仓库凭据行三态（手动 PAT 生效中🟢 / OAuth 自动供给中🔵 / 未配置⚪），判定逻辑与 token_resolver 解析序一致（Manual > OAuth）
2. **命中审计**：fetch_user_token pat/oauth 命中经 `_audit_source` 进 usage_log（`operation=user_token_hit_{source}`，失败不阻断）——"配了 PAT 走了 OAuth"类问题可从审计排查

守卫：test_d102m 3 用例（来源徽章断言 / _audit_source 调用+落库断言 / 优先级不回归）。