# 6. 运行时与引擎

## 6.1 引擎类型

| 引擎 | 说明 | 适用场景 |
|------|------|----------|
| opencode | OpenCode CLI，AI 编码助手 | Coding/Issue/QA Agent |
| hermes | Hermes Agent，支持 ACP 协议 | VOD/Chat/SRE Agent |
| codearts | 华为云 CodeArts CLI | 特定 CI/CD 场景 |

## 6.2 Worker 管理

- 在 **Agent 运行时** 页面查看所有 Worker 设备和引擎状态
- 每个 Worker 可以运行多个引擎实例（如同时运行 opencode + hermes + codearts）
- Worker 自动注册并定期心跳上报（CPU/内存/磁盘/探活结果）
- 系统监控页面展示每个引擎的探活状态（CLI 测试对话延迟）
