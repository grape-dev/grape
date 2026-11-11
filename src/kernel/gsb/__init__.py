"""Grape Service Bus (GSB) — 统一域间服务通信框架（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.

GSB 基于 FastAPI + httpx + DB 注册表，不引入新依赖：
- registry.py — DB 驱动服务注册表（register/heartbeat/discover）
- client.py  — GsbClient（httpx + 服务发现 + HMAC 令牌 + 重试）
- router.py  — GsbService 基类（注册 + 心跳 + 中间件）
- provider.py — Provider 工厂（Local/Remote 切换）
- health.py  — 服务健康检查端点

使用方式：
    from src.kernel.gsb.provider import get_mgmt_service
    svc = get_mgmt_service()  # 自动选择 Local 或 Remote
    cfg = svc.get_repo_webhook_config("owner/repo")
"""
