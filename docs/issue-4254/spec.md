# Issue #4254 · 自注册用户配置 OAuth/PAT 的内测白名单提示（Spec）

## 目标
通过平台注册入口自注册（`created_by_oauth=0`）的用户登录后，在「第三方账号（OAuth）」与「第三方仓库凭据（PAT）」场景看到非阻断的内测白名单提示，并可直达申请入口；已在白名单内或 OAuth 建号用户不受影响。

## 范围
In：
- 账号中心 OAuth 区、PAT 区、BindDialog、PatDialog 的非阻断提示与申请入口。
- 后端内测资格判定接口 `GET /api/auth/beta-eligibility`；`/api/auth/profile` 增加 `created_by_oauth`。

Out：
- 不改造内测申请表单与审核流程；不新建白名单数据表；不阻断凭据配置保存。

## 判定规则
1. `created_by_oauth=1`（OAuth 建号）→ 不提示。
2. `login_whitelist_enabled=true` → 按用户已绑定第三方身份（provider + external_login）匹配 `login_whitelist`。
3. 否则按邮箱/域名匹配 `signup_whitelist`。
4. 两个白名单均未启用且为空 → 无白名单管控，不提示。
5. 白名单查询异常 → 不提示并记日志（避免误打扰已授权用户）。

## 接口
`GET /api/auth/beta-eligibility` → `{"is_self_registered": bool, "whitelisted": bool, "apply_url": "/beta-apply"}`。
前端仅在 `is_self_registered && !whitelisted` 时展示提示。

## 验收
- [x] 自注册非白名单用户可见提示与申请按钮
- [x] 申请入口复用既有内测申请弹窗 / 申请页
- [x] 白名单内用户、OAuth 建号用户无提示
- [x] 查询异常不误提示
- [x] 新增测试覆盖判定分支，现有相关测试通过
