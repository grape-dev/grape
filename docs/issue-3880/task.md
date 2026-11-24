# Tasks: Issue #3880

- [x] T1 修复 `runtime_claim_v2` 签名（增加 `request: Request`，置于 `engine_uid` 之后）
- [x] T2 调用改为四参透传 `runtime_claim(rid, request, _claim_body_v1(body), credential_id)`
- [x] T3 更新现有 v2 claim 直调用例（补传 request）
- [x] T4 新增回归守卫：v1/v2 claim 形参顺序 + `request` 注解为 `fastapi.Request` 契约测试
- [x] T5 回归：v1 claim / claim-by-tag / claim-bound / claim-by-tag-bound / claim-capabilities（v1+v2）测试通过
