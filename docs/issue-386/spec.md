# Spec: Issue #386 — 凭据写入传无效 token 误报 401（原子 AT-STT-03）

> 来源：Issue #386 + VOD 分诊评论（id=194729756）| 目标分支：weekly-08

## 需求
`PUT /api/settings/credentials` 传无效第三方 token（如 `gitcode_token="demo"`）时，
后端当前返回 `401 {"code":401,"message":"令牌无效或已过期"}`。该状态码与调用方自身
JWT 鉴权失败的 401 完全同构，前端 `api.js` 全局 401 拦截器（handle401 → refresh →
失败即清 token 跳登录）无法区分，把"第三方 token 无效"误判成"登录失效"踢出会话。

期望：第三方 token 校验失败返回 `400` + 用户化文案（如「GitCode token 校验失败，
请检查 token 有效性」），调用方鉴权语义 401 仅留给 `require_no_session_token`。

## 现状（缺陷）
`src/api/routes/settings.py` `update_credentials` 中共 4 处将「被校验的第三方 token
无效」误报为 401：
- GitHub 分支：上游 `_resp.status_code == 401`（L202-206）及其他非 200（L207-212）
- GitCode 分支：上游 `_resp.status_code == 401`（L296-300）及其他非 200（L301-306）

`_is_placeholder()`（L106）只负责跳过占位值不报错，与本缺陷无关；空体/清除路径
未写入坏值（安全面无问题），均不动。

## 范围
- In：上述 4 处第三方 token 校验失败改 400 + 用户化文案；GitHub/GitCode 对称。
- Out：`_is_placeholder` 占位值跳过逻辑、409 绑定冲突、500 保存失败、502 上游网络
  异常、前端 `api.js`/`account.js`（后端语义修正后 401 拦截器自然不再命中，无需改动）。

## 验收
- [ ] 无效 GitHub token / 无效 GitCode token（上游 401 及其他非 200）→ 响应
      400，`detail.code=400`，文案用户化（含平台名），前端不再触发全局 401 踢出
- [ ] 上游网络异常（requests 抛错）维持 502 不变
- [ ] 空字符串清除、占位符拒绝、未传字段不动等既有行为回归通过
- [ ] 原子 AT-STT-03（xfail）修好后 XPASS（外部原子测试集，本仓不含，验收时以
      响应码断言为准）
