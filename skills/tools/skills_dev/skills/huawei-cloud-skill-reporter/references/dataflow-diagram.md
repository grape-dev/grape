# Data Flow Diagram — Huawei Cloud Skill Reporter v1

> Skill 使用监控与数据上报插件的工作流程。

## Architecture Overview

```mermaid
flowchart TD
  START([/"用户调用 huawei-cloud-* Skill"/])

  subgraph INSTALL["安装阶段"]
    INSTALL_CMD["npx huawei-cloud-skill-reporter --server <URL>"]
    INSTALL_JS["setup.js / setup.ps1 / setup.sh"]
    INSTALL_HERMES["安装 Hermes Python 插件"]
    INSTALL_OPENCODE["安装 Opencode JS 插件"]
    INSTALL_CONFIG["保存配置文件"]
  end

  subgraph RUNTIME["运行阶段"]
    SKILL_CALL["Skill 被调用"]
    HOOK_TRIGGER["钩子触发"]
    FILTER["过滤 huawei-cloud-*"]
    BUILD_PAYLOAD["构建上报数据"]
    REPORT["上报到服务器"]
    SKILL_RESULT["返回 Skill 结果"]
  end

  subgraph SERVER["服务端"]
    RECEIVE["接收数据"]
    STORE["存储到数据库"]
    QUERY["提供查询 API"]
  end

  START --> INSTALL_CMD
  INSTALL_CMD --> INSTALL_JS
  INSTALL_JS --> INSTALL_HERMES
  INSTALL_JS --> INSTALL_OPENCODE
  INSTALL_HERMES --> INSTALL_CONFIG
  INSTALL_OPENCODE --> INSTALL_CONFIG
  INSTALL_CONFIG --> INSTALL_DONE([/"安装完成"/])

  INSTALL_DONE --> SKILL_CALL
  SKILL_CALL --> HOOK_TRIGGER
  HOOK_TRIGGER --> FILTER

  FILTER -->|"是 huawei-cloud-*"| BUILD_PAYLOAD
  FILTER -->|"否"| SKILL_RESULT

  BUILD_PAYLOAD --> REPORT
  REPORT -->|"fire-and-forget"| SKILL_RESULT

  REPORT --> RECEIVE
  RECEIVE --> STORE
  STORE --> QUERY

  QUERY -->|"分页查询"| QUERY_PAGE["GET /api/telemetry"]
  QUERY -->|"统计概览"| QUERY_STATS["GET /api/telemetry/stats"]
```

## Data Flow

```mermaid
sequenceDiagram
  participant U as User
  participant A as Agent
  participant H as Hermes/Opencode
  participant P as Reporter Plugin
  participant S as Telemetry Server

  U->>A: 调用 huawei-cloud-obs-query
  A->>H: 执行 Skill
  H->>P: 触发 tool.execute.after / post_tool_call
  P->>P: 过滤检查
  alt 是 huawei-cloud-* Skill
    P->>P: 构建 payload
    P->>S: POST /api/telemetry (async)
    S-->>P: 200 OK
  end
  H-->>A: 返回 Skill 结果
  A-->>U: Skill 响应
```

## Configuration Flow

```mermaid
flowchart LR
  subgraph CONFIG_PRIORITY["配置优先级"]
    ENV["环境变量\nHUAWEI_CLOUD_SKILL_REPORT_URL"] --> P1["最高优先级"]
    FILE["配置文件\n~/.config/huawei-cloud-skill-reporter/server_url.txt"] --> P2["中等优先级"]
    DEFAULT["内置默认值\nskillsop.topxtopx.com"] --> P3["最低优先级"]
  end
```

## Report Payload Structure

```mermaid
flowchart TD
  subgraph PAYLOAD["上报数据"]
    SKILL["skill: string\nSkill 名称"]
    STATUS["status: string\nsuccess/failure"]
    TS["ts: string\nISO 8601 时间"]
    REQUEST["request: string\n参数摘要(≤500)"]
    RESULT["result: string\n结果摘要(≤1000)"]
  end
```

## Diagram Legend

| Symbol | Meaning |
|--------|---------|
| `([/ /])` | Start / End |
| `[ ]` | Process step |
| `-.->` | Async / Fire-and-forget |
| `-->` | Primary flow |
| `alt...end` | Conditional branch |
