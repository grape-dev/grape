# api-skill-dev 场景——通用 API skill 创建/测试纳入 grape 技能池

## 背景

grape 系统在发展，skill 创建场景需要扩展。当前 skill-dev 场景只有华为云专用 creator（CLI/SDK/OpenAPI 三级调研），需要新增通用 API 场景（OpenAPI/文档/基础信息三级调研）。

hello-skill 仓库的 rest-api-skill-creator/tester 已完善（项目知识优先、扫描所有 JSON 文件、test-vars.json 无敏感字段、Agent 角色称呼统一），可以纳入 grape 技能池。

场景命名：`api-skill-dev`（API Skill 开发）——不限定 REST，将来 GraphQL/数据库 API 也能用，和 `skill-dev`（华为云）形成清晰区分。

## 改动范围

### 1. 复制 skill 文件到 grape 仓库（2 个 skill 目录）

从 hello-skill 仓库复制到 grape 仓库：

| 源（hello-skill） | 目标（grape） |
|-------------------|---------------|
| skills/rest-api-skill-creator/ | skills/rest-api-skill-creator/ |
| skills/rest-api-skill-tester/ | skills/rest-api-skill-tester/ |

文件清单（12 个文件）：
- rest-api-skill-creator/: SKILL.md + references/(4 个) + scripts/(1 个) + templates/(1 个)
- rest-api-skill-tester/: SKILL.md + references/(2 个) + scripts/(2 个)

### 2. 新增 api-skill-dev 场景（configs/agent-skill-mapping.json）

```json
"api-skill-dev": {
    "name": "API Skill 开发",
    "mapping": {
        "vod": "vod",
        "issue": "skill-issue",
        "coding": "rest-api-skill-creator",
        "qa": "rest-api-skill-tester",
        "sre": "skill-publish"
    },
    "onboarding": "repo-onboarding"
}
```

- issue/sre 复用现有 skill-issue/skill-publish（通用）
- coding/qa 用新的 rest-api-skill-creator/rest-api-skill-tester
- vod 复用现有 vod

### 3. 更新 configs/skill-description.json

新增 2 个 skill 描述：

```json
"rest-api-skill-creator": {
    "name": "REST API Skill 创建生成器",
    "agent_type": "coding",
    "path": "rest-api-skill-creator",
    "description": "按需求自动创建 REST API 管理 skill：查重 → 读取 Spec → OpenAPI/文档/基础信息三级调研 → 骨架生成 → 自测 → 创建 PR → A2A 交棒 QA Agent 测试。通用不限目标系统"
},
"rest-api-skill-tester": {
    "name": "REST API Skill 测试验证",
    "agent_type": "qa",
    "path": "rest-api-skill-tester",
    "description": "REST API skill 功能验证（QA 门控）：凭据/地址检测（项目知识优先）→ 跑 skill 验证 + 规范检查（生成质量6项）→ Issue 报告 + PR 评论 → A2A 交棒 SRE Agent 发布"
}
```

### 4. 不改动

| 项目 | 原因 |
|------|------|
| skill-dev 场景 | 保持华为云专用（skill-creator/skill-tester） |
| skill-creator/skill-tester | 保持不变 |
| hello-skill 仓库 | 保持独立（grape 仓库是副本） |
| agent-description.json | 不需要新增（agent_type 还是 coding/qa） |

## 场景对比

| 场景 | coding | qa | 适用场景 |
|------|--------|-----|----------|
| skill-dev | skill-creator | skill-tester | 华为云 skill（CLI/SDK/OpenAPI 三级调研） |
| api-skill-dev | rest-api-skill-creator | rest-api-skill-tester | 通用 API skill（OpenAPI/文档/基础信息三级调研） |

## 验证

1. skills/rest-api-skill-creator/ 和 skills/rest-api-skill-tester/ 文件完整（12 个文件）
2. configs/agent-skill-mapping.json 含 api-skill-dev 场景
3. configs/skill-description.json 含 2 个新 skill 描述
4. grape 服务能加载新 skill（健康检查通过）
