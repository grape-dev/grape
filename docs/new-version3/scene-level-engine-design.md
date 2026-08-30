# 场景级 Backend Engines 优化分析

## 一、现状：引擎绑定机制

### 1.1 当前引擎选择链路

```
pick_engine(repo_key, agent_type, fallback_runtime_id)
  ↓
get_binding(repo_key, agent_type)  →  devops_agent_runtime_binding 表
  ↓                                  
  集合非空 → 按 scope 优先级选: repo(10) → global(50) → local(90)
  集合为空 → fallback_runtime_id（session 级 legacy）
  都没有  → None（engine_unavailable，跳过执行）
```

关键表结构：
```
devops_agent_runtime_binding:
  repo_key    TEXT    -- 仓库全名（owner/repo）
  agent_type  TEXT    -- vod/issue/coding/qa/sre
  runtime_id  INTEGER -- agent_runtimes.id
  priority    INTEGER -- 10(repo) / 50(global) / 90(local)
  PRIMARY KEY (repo_key, agent_type, runtime_id)
```

### 1.2 引擎与场景的关系现状

```
场景配置（agent-skill-mapping.json）：
  scenes.default.mapping.coding    = "implementation"
  scenes.iac.mapping.coding        = "iac-coding"
  scenes.skill-dev.mapping.coding  = "skill-creator"

引擎绑定（devops_agent_runtime_binding）：
  (repo=hello-blog, agent=coding) → runtime_id=33  ← 仓库级，与场景无关
```

**问题**：场景切换只改 skill，不改引擎。同一仓库无论什么场景，Coding Agent 恒用同一引擎集合。

### 1.3 现有引擎实例

```
活跃引擎（credential status=active）：
  id=31  opencode  cred=19(h206-local-02)     worker-ecs-vod-dev-design
  id=32  hermes    cred=19(h206-local-02)     worker-ecs-vod-dev-design
  id=33  opencode  cred=21(h116-lvjuntao3)    worker-ecs-d27a
  id=34  hermes    cred=21(h116-lvjuntao3)    worker-ecs-d27a  (healthy=0)
  id=35  opencode  cred=22(h116-grape3)       worker-ecs-d27a
  id=36  hermes    cred=22(h116-grape3)       worker-ecs-d27a
  id=37  hermes    cred=0                     本机直连 (offline)
  id=38  opencode  cred=0                     本机直连 (offline)
```

3 台远端 worker，每台跑 2 个引擎类型（hermes + opencode）。

### 1.4 不同场景的引擎需求差异

| 场景 | Coding Skill | SRE Skill | 引擎特殊需求 |
|------|-------------|-----------|-------------|
| default | implementation | cloud-deploy | 通用代码能力 |
| iac | iac-coding | iac-sre | 需 terraform CLI、华为云 SDK |
| skill-dev | skill-creator | skill-publish | 需 skill 仓库访问、gitleaks |
| api-skill-dev | rest-api-skill-creator | skill-publish | 需 curl/httpie、OpenAPI 工具 |
| blog-publish | ai-blog-generation | blog-publish | 需 Markdown 工具链 |
| cloud-native | implementation | cloud-deploy | 需 docker/k8s CLI |

**当前绑定无法区分**：一台没有装 terraform 的 worker 可能被分派 iac-coding 任务，导致执行失败。

---

## 二、方案分析

### 2.1 方案对比

| 方案 | 描述 | 优点 | 缺点 | 改动量 |
|------|------|------|------|--------|
| A. 维持现状 | 引擎仍仓库级，场景只改 skill | 零改动 | 不同场景可能用错引擎 | 无 |
| B. 场景级引擎配置 | agent-skill-mapping.json 加 engine 字段 | 配置即引擎，一目了然 | 需改 pick_engine 链路 + 配置管理 | 中 |
| C. 引擎能力标签 | 引擎打标签（terraform/skill/docker），场景声明需求，匹配选择 | 最灵活，自动匹配 | 标签维护复杂，过度设计 | 大 |
| D. 仓库×场景级绑定 | 扩展 binding 表加 scene_id 列 | 细粒度控制 | 配置组合爆炸（7场景×5agent=35行/仓库） | 中 |
| **E. 场景默认引擎 + 仓库级覆盖** | 场景配置声明引擎偏好，仓库级 binding 优先 | 平衡灵活性和简洁性 | 需场景配置 + pick_engine 改造 | 中 |

### 2.2 推荐方案：E — 场景默认引擎 + 仓库级覆盖

**核心思路**：在 `agent-skill-mapping.json` 的场景配置中新增 `engines` 字段，声明该场景各 Agent 的引擎偏好。`pick_engine` 查找链路改为：

```
1. devops_agent_runtime_binding（仓库级手动绑定）→ 有则用
2. 场景配置 engines（agent-skill-mapping.json）→ 有则用
3. resolve_default_engine（按 scope 自动选）→ 兜底
```

### 2.3 配置格式扩展

```json
{
  "scenes": {
    "default": {
      "name": "通用开发",
      "mapping": {
        "vod": "vod",
        "issue": "issue-advance",
        "coding": "implementation",
        "qa": "pr",
        "sre": "cloud-deploy"
      },
      "engines": {
        "coding": "opencode",
        "qa": "hermes",
        "sre": "hermes"
      }
    },
    "iac": {
      "name": "IaC开发",
      "mapping": {
        "vod": "iac-vod",
        "issue": "iac-issue",
        "coding": "iac-coding",
        "qa": "iac-qa",
        "sre": "iac-sre"
      },
      "engines": {
        "coding": "hermes",
        "sre": "hermes"
      }
    },
    "skill-dev": {
      "name": "Skill开发",
      "mapping": { ... },
      "engines": {
        "coding": "opencode",
        "qa": "opencode"
      }
    }
  }
}
```

`engines` 字段语义：
- key = agent_type（vod/issue/coding/qa/sre）
- value = 引擎类型名（"opencode" / "hermes"），对应 `agent_runtimes.name`
- 空或缺省 = 不指定偏好，走默认选择

### 2.4 pick_engine 改造

```python
def pick_engine(repo_key, agent_type, fallback_runtime_id=0, scene_id="", issue_id=""):
    # 1. 仓库级手动绑定（最高优先级，用户显式配置）
    members = get_binding(repo_key, agent_type)
    if members:
        result = _select_from_members(members, cfg, repo)
        if result:
            return result
        # 集合有成员但不健康 → 不回退（保持现有语义）
        return None

    # 2. 场景级引擎偏好（新增）
    if scene_id:
        engine_pref = _get_scene_engine_pref(scene_id, agent_type)
        if engine_pref:
            # 按引擎类型名找健康的 runtime
            rid = _find_healthy_engine_by_name(engine_pref, repo_key)
            if rid:
                return rid

    # 3. 默认选择（兜底，现有逻辑）
    if fallback_runtime_id:
        return int(fallback_runtime_id)
    return resolve_default_engine(repo_key, agent_type)
```

新增方法：
```python
def _get_scene_engine_pref(scene_id: str, agent_type: str) -> str:
    """读场景配置的引擎偏好"""
    path = os.path.join(CONFIG_DIR, "agent-skill-mapping.json")
    with open(path) as f:
        data = json.load(f)
    scene = data.get("scenes", {}).get(scene_id, {})
    return scene.get("engines", {}).get(agent_type, "")

def _find_healthy_engine_by_name(engine_name: str, repo_key: str) -> int | None:
    """按引擎类型名（opencode/hermes）找健康 runtime"""
    # 优先仓库级（owner 匹配），次全局，最后本机
    for scope in ["repo", "global", "local"]:
        candidates = query_engines(repo_key, scope)
        for c in candidates:
            if c["name"] == engine_name and c["healthy"] and c["installed"]:
                return c["runtime_id"]
    return None
```

### 2.5 与任务级场景方案的协同

结合前一篇方案（任务级流水线），引擎选择链路变为：

```
Issue → VOD 澄清 → 判定 scene_id（如 "iac"）
  → A2A 交办携带 scene_id
  → issue_state.scene_id = "iac"
  → 后续 Agent 分派时：
    pick_engine(repo, agent_type, scene_id=resolve_issue_scene(repo, issue_id))
    → 1. 仓库级 binding（用户手动配置，最高优先）
    → 2. 场景 engines 偏好（iac-coding → hermes 引擎）
    → 3. 默认选择（兜底）
```

完整优先级链路：

```
引擎选择优先级（高 → 低）：
  1. devops_agent_runtime_binding     ← 用户手动绑定（Configure 页面）
  2. scene.engines[agent_type]        ← 场景引擎偏好（配置文件）
  3. resolve_default_engine           ← 按 scope 自动选（repo→global→local）
  4. fallback_runtime_id              ← 会话 legacy

Skill 选择优先级（高 → 低）：
  1. devops_agent_session.skill       ← 用户手动配置（Configure 页面）
  2. issue_state.scene_id → mapping   ← 任务级场景（VOD 判定）
  3. managed_repos.dev_scene → mapping ← 仓库级场景（Dashboard 设置）
  4. "default" → mapping              ← 兜底默认
```

---

## 三、为什么不选其他方案

### 3.1 不选 C（引擎能力标签）

原因：
- 引擎能力标签（terraform/skill/docker）需要额外维护一套标签体系
- worker 注册时自报能力不可靠（装了但不一定能用）
- 当前只有 3 台 worker、2 种引擎类型（hermes/opencode），标签匹配的收益远小于维护成本
- 场景数固定 7 个，显式配置比自动匹配更可控

### 3.2 不选 D（仓库×场景级绑定）

原因：
- 7 场景 × 5 Agent = 35 行 binding/仓库，配置组合爆炸
- 大多数场景的引擎偏好是固定的（iac 恒用 hermes），不需要每仓库单独配
- 仓库级 binding 已经作为最高优先级覆盖，不需要场景级也入库

### 3.3 不选 B（场景级引擎配置，存 DB）

原因：
- 引擎偏好是场景级静态配置（iac 场景偏好 hermes），不会因仓库不同而变
- 放 JSON 配置文件即可，无需入库
- 与 skill mapping 同源（都在 agent-skill-mapping.json），改一个文件完成场景配置

---

## 四、改动清单

### 4.1 配置文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `configs/agent-skill-mapping.json` | 每个 scene 加 `engines` 字段 | 场景引擎偏好 |

### 4.2 后端

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/runtime/engine_select.py` | `pick_engine` 加 `scene_id` 参数；新增 `_get_scene_engine_pref()` + `_find_healthy_engine_by_name()` | 核心链路改造 |
| `src/api/routes/webhook.py` | A2A 分派时传 `scene_id` 给 `pick_engine` | 调用点改造 |
| `src/api/event_pipeline.py` | pipeline 分派时传 `scene_id` 给 `pick_engine`（2 处） | 调用点改造 |
| `src/api/routes/events.py` | redispatch/chat 分派时传 `scene_id` | 调用点改造 |
| `src/api/routes/scenes.py` | `apply_scene` 时可选更新 binding（按场景 engines 预设） | 增强场景切换 |

### 4.3 scenes.py apply_scene 增强

场景切换时，除了更新 skill 映射，还可按 `engines` 预设更新 binding：

```python
# apply_scene 中新增：
engines = scene.get("engines", {})
for agent_type, engine_name in engines.items():
    rid = _find_healthy_engine_by_name(engine_name, repo_key)
    if rid:
        # 仅当该 agent 没有手动绑定（binding 表为空）时才预设
        existing = get_binding(repo_key, agent_type)
        if not existing:
            set_binding(repo_key, agent_type, [rid])
```

这样用户在 Dashboard 切换场景时，引擎也会自动调整为场景偏好。

### 4.4 不需要改的

- `devops_agent_runtime_binding` 表结构不变
- `agent_runtimes` 表不变
- 前端 Configure 页面不变（用户手动绑定的引擎仍然最高优先级）
- worker 注册流程不变

---

## 五、场景引擎偏好推荐配置

基于当前 worker 环境和各场景 skill 的特性：

```json
{
  "default": {
    "engines": {
      "coding": "opencode",
      "qa": "hermes",
      "sre": "hermes"
    }
  },
  "iac": {
    "engines": {
      "coding": "hermes",
      "qa": "hermes",
      "sre": "hermes"
    }
  },
  "skill-dev": {
    "engines": {
      "coding": "opencode",
      "qa": "opencode"
    }
  },
  "api-skill-dev": {
    "engines": {
      "coding": "opencode",
      "qa": "opencode"
    }
  },
  "blog-publish": {
    "engines": {
      "coding": "opencode"
    }
  },
  "cloud-native": {
    "engines": {
      "coding": "opencode",
      "sre": "hermes"
    }
  }
}
```

理由：
- **opencode** 擅长代码生成/编辑（implementation/skill-creator/blog-generation），交互式编码体验好
- **hermes** 擅长工具链调用/流程编排（iac-coding 执行 terraform、cloud-deploy 部署、pr 审查），工具使用能力强
- VOD/Issue Agent 不特别指定（两个引擎都能胜任，走默认选择）

---

## 六、完整链路示例

### 场景：IaC 任务

```
1. Issue #20: "创建华为云 VPC 和子网"
2. webhook → issue_opened → VOD Agent
3. VOD 澄清：确认是 IaC 需求 → 判定 scene_id="iac"
4. VOD A2A → POST /api/a2a {"agent":"issue", "payload":{..., "scene_id":"iac"}}
5. webhook.py:
   - 写入 issue_state.scene_id = "iac"
   - skill 查找: iac scene → issue = "iac-issue"
   - engine 查找: 
     a. binding(repo, issue) 有 → 用手动绑定
     b. binding 为空 → scene.engines["issue"] 无指定 → resolve_default_engine
6. Issue Agent 完成 spec → A2A → coding
7. webhook.py:
   - skill 查找: iac scene → coding = "iac-coding"
   - engine 查找:
     a. binding(repo, coding) 有 → 用手动绑定
     b. binding 为空 → scene.engines["coding"] = "hermes" → 找健康 hermes 引擎
     c. 选中 id=36 (hermes, cred=22, worker-ecs-d27a, healthy)
8. Coding Agent 用 hermes 引擎执行 iac-coding skill（terraform fmt/init/validate）
9. ... 后续 QA/SRE 同理
```

### 场景：Skill 开发任务

```
1. Issue #25: "创建一个华为云 OBS skill"
2. VOD 判定 scene_id="skill-dev"
3. 后续 Coding Agent:
   - skill = "skill-creator"
   - engine = scene.engines["coding"] = "opencode" → 选中 id=35 (opencode, cred=22)
4. QA Agent:
   - skill = "skill-tester"
   - engine = scene.engines["qa"] = "opencode" → 选中 id=35
```

---

## 七、向后兼容

| 场景 | 行为 |
|------|------|
| `engines` 字段缺失 | 走现有逻辑（binding → resolve_default_engine），无影响 |
| 仓库有手动 binding | 手动绑定优先，场景 engines 不生效 |
| 旧 worker 不支持场景 | 不影响（引擎选择在服务端，worker 只接收任务） |
| 现有仓库无 scene_id | 回退 managed_repos.dev_scene → "default" |

完全向后兼容，不影响现有部署。
