# 7. 运行时与引擎

## 引擎类型

| 引擎 | 说明 | 适用场景 |
|------|------|----------|
| opencode | OpenCode CLI，AI 编码助手 | Coding/Issue/QA Agent |
| hermes | Hermes Agent，支持 ACP 协议 | VOD/Chat/SRE Agent |
| codearts | 华为云 CodeArts CLI | 特定 CI/CD 场景 |

## Worker 管理

- 在 **Agent 运行时** 页面查看所有 Worker 设备和引擎状态
- 每个 Worker 可以运行多个引擎实例（如同时运行 opencode + hermes + codearts）
- Worker 自动注册并定期心跳上报（CPU/内存/磁盘/探活结果）
- 系统监控页面展示每个引擎的探活状态（CLI 测试对话延迟）

## 指定引擎执行（场景节点 engine_runtime_ids）

在场景（流水线）的节点配置中，`engine_runtime_ids` 是**派发硬约束**：为某个 Agent 节点配置引擎 ID 列表后，该节点的任务只会派发给列表中的引擎（自建/专用 Worker 的"指定机器执行"能力）。

```json
{"id": "vod", "tag": "vod", "skill": "vod-cli", "mode": "emit", "next": "issue", "engine_runtime_ids": [17042]}
```

语义（Issue #653）：

- **已配置**（列表非空）：任务只允许被列表内引擎认领；列表内引擎全部离线/心跳超时/已注销时，任务**显式失败并给出原因**（含场景、Agent、引擎清单），不会回退到全局池 ECS。
- **未配置**（空列表或缺失）：保持全量广播——任务可被任意符合标签的在线引擎认领（存量行为不变）。
- 所有派发通道（issue 触发、A2A 交办、手动重派、智能推荐重派）行为一致。

### 严格模式开关（strict_scene_engines）

`configs/engine-selection.json`：

```json
{"strict_scene_engines": true}
```

- `true`（默认）：已配置节点候选为空时任务失败（宁失败不跑飞）。
- `false`：兼容旧语义——候选为空时回退全量广播，但每次回退都会输出告警日志（可审计）。依赖全局池兜底的存量场景可显式关闭。
