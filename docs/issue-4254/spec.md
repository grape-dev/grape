# Issue #4254 · 自注册用户配置 OAuth/PAT 的内测白名单提示（Spec）

## 目标
登录后未命中内测白名单的用户，在「第三方账号（OAuth）」与「第三方仓库凭据（PAT）」场景看到非阻断的内测白名单提示，并可直达申请入口；已在白名单内的用户不受影响。判据只看白名单命中与否，不看建号来源（`created_by_oauth` 仅作展示标记）——OAuth 建号但不在白名单的用户（早期白名单未启用时建的号、换绑后身份不在名单等）同样需要提示。

## 范围
In：
- 账号中心 OAuth 区、PAT 区、BindDialog、PatDialog 的非阻断提示与申请入口。
- 后端内测资格判定接口 `GET /api/auth/beta-eligibility`；`/api/auth/profile` 增加 `created_by_oauth`。

Out：
- 不改造内测申请表单与审核流程；不新建白名单数据表。
- 已决策（负责人确认，2026-11-24）：**凭据配置对内测白名单用户开放**——未命中白名单的用户 `PUT /api/settings/credentials` 一律 403（`error_code=BETA_WHITELIST_REQUIRED`），前端保存按钮置灰；存量自注册账号（含仅靠 PAT 的账号）不做豁免，按负责人口径"只有通过第三方平台进来的用户可正常使用"。

## 判定规则
1. 命中白名单（任一已绑定第三方身份在 login_whitelist 内，或邮箱/域名命中 signup_whitelist）→ 不提示；`created_by_oauth=1` 不构成免提示条件。
   已决策（负责人确认，2026-11-24）：多身份用户**任一渠道命中即视为已具备资格、不提示**（方案 A）。理由：他能通过白名单渠道正常使用，再提示"去申请内测"是误导；对照口径"每个绑定渠道都必须命中才算已具备"（方案 B）已否决。
2. `login_whitelist_enabled=true` → 按用户已绑定第三方身份（provider + external_login）匹配 `login_whitelist`。
3. 否则按邮箱/域名匹配 `signup_whitelist`。
4. 两个白名单均未启用且为空 → 无白名单管控，不提示。
5. 白名单查询异常 → 不提示并记日志（避免误打扰已授权用户）；凭据保存门禁同样 fail-open（读取异常不放行拦截，避免锁死已授权用户）。
6. 凭据保存：未命中白名单 → `PUT /api/settings/credentials` 返回 403 `BETA_WHITELIST_REQUIRED`（附 `apply_url`），前端保存按钮置灰；命中白名单照常保存。
7. 三方绑定：与登录同源闸——未命中白名单的第三方身份**不建立绑定**（回调 `_handle_oauth_bind` 前置校验，命中才继续），拦截形态与登录一致（`oauth_error=WHITELIST:<provider>:<ticket>`，复用申请内测引导与预填票据）。已决策（负责人确认，2026-11-24）：不采用"绑定成功即视为已具备资格"的口径，绑定不得绕过白名单。

## 接口
`GET /api/auth/beta-eligibility` → `{"is_self_registered": bool, "whitelisted": bool, "apply_url": "/beta-apply"}`。
前端仅在 `is_self_registered && !whitelisted` 时展示提示。

## 验收
- [x] 未命中白名单的用户（含 OAuth 建号）可见提示与申请按钮
- [x] 申请入口复用既有内测申请弹窗 / 申请页
- [x] 白名单内用户无提示
- [x] 查询异常不误提示
- [x] 未命中白名单的用户不可保存凭据（403 + 前端保存置灰），命中白名单可正常保存
- [x] 未命中白名单的第三方身份不可建立绑定（回调前置闸，拦截形态与登录一致）
- [x] 新增测试覆盖判定分支，现有相关测试通过
