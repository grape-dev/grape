# Phase 5 Task — 契约接口填充（Issue #699）：检查清单

## 编码清单

### GSB 框架
- [ ] src/kernel/gsb/__init__.py
- [ ] src/kernel/gsb/registry.py — 服务注册表（DB 驱动）
- [ ] src/kernel/gsb/client.py — GsbClient 统一客户端
- [ ] src/kernel/gsb/router.py — GsbService 基类
- [ ] src/kernel/gsb/provider.py — Provider 工厂
- [ ] src/kernel/gsb/health.py — 健康检查端点

### 认证安全
- [ ] src/kernel/auth_sdk/service_token.py — HMAC-SHA256 服务令牌
- [ ] src/kernel/auth_sdk/internal_middleware.py — /internal/* 认证中间件

### Provider 工厂入口
- [ ] src/kernel/provider.py — 兼容入口（委托 gsb.provider）

### LocalProvider 实现
- [ ] src/domains/contracts/local/__init__.py
- [ ] src/domains/contracts/local/mgmt_local.py — MgmtServiceLocal
- [ ] src/domains/contracts/local/runtime_local.py — RuntimeServiceLocal
- [ ] src/domains/contracts/local/insight_local.py — InsightServiceLocal

### RemoteProvider 实现
- [ ] src/domains/contracts/remote/__init__.py
- [ ] src/domains/contracts/remote/http_client.py — ServiceHttpClient
- [ ] src/domains/contracts/remote/mgmt_remote.py — MgmtServiceRemote
- [ ] src/domains/contracts/remote/runtime_remote.py — RuntimeServiceRemote
- [ ] src/domains/contracts/remote/insight_remote.py — InsightServiceRemote

### /internal/* 端点
- [ ] src/domains/mgmt/routes/internal.py — Mgmt 内部端点
- [ ] src/domains/runtime/routes/internal.py — Runtime 内部端点
- [ ] src/domains/insight/routes/internal.py — Insight 内部端点

### 契约注册
- [ ] src/domains/contracts/__init__.py — 导入时注册三个 Provider

### 测试
- [ ] tests/test_issue_699_phase5_contracts.py（TC-5.1~TC-5.4 + GSB/token/provider 工厂测试）

## 验证清单

- [ ] python3 -m pytest tests/test_issue_699_phase5_contracts.py 通过
- [ ] 回归批次（kernel_db / phase4_servers / arch）通过
- [ ] pre-pr-check：无关文件拦截 0、敏感扫描 0、commit message 全英文
- [ ] PR #836 body 追加 Phase 5 章节并推送
- [ ] Issue #699 评论告知 + A2A QA

## 收尾

- [ ] grape tasks complete（actions/conclusion/artifacts/issue_summary 真实填充）
