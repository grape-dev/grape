# 组织仓库地图：业界实践与落地方法

> 调研时间：2026-11-24
> 目标：回答「组织仓库地图到底该怎么做」，给 Grape 的仓库地图/组织下派提供可复用的模型与实施路径。
> 结论先行：业界没有单一产品叫「组织仓库地图」；它通常是 **Software Catalog + Business Capability Map + Context Map + Code Graph** 的组合。

---

## 1. 先统一概念：组织仓库地图不是代码 Wiki

组织仓库地图要回答四个问题：

1. **组织有哪些业务域/系统？**
2. **每个仓库在组织里承担什么职责、边界在哪？**
3. **仓库之间、系统和团队之间是什么关系？**
4. **这张图是否可信、是否新鲜、覆盖是否完整？**

因此它至少包含五层：

```text
组织层：Domain（业务域） → System（系统）
仓库层：Repository / Component（owner、lifecycle、criticality）
能力层：Capability（L1/L2/L3 业务能力、成熟度）
关系层：partOf / ownedBy / provides / consumes / dependsOn / upstream-downstream
治理层：evidence、confidence、freshness、coverage、review
```

代码 Wiki（DeepWiki、RepoWiki 等）只解决「这个仓库内部怎么实现」，不能替代组织仓库地图。代码 Wiki 可以作为**证据来源**，但不应该成为地图本身。

---

## 2. 业界四类做法

### 2.1 Catalog-as-code + 自动发现（Backstage）

Backstage 是最接近「组织仓库地图」的开源实现。

核心做法：

- 每个仓库放 `catalog-info.yaml`，描述 `Component / API / Resource / System / Domain / Group`；
- 目录不是手工维护，而是由 **discovery provider 爬取 GitHub/GitLab 组织**，发现匹配路径的 descriptor；
- 支持 webhook（`push` / `repository`）触发刷新；
- Catalog 不是唯一事实源，**仓库里的 YAML 才是**，Catalog 是缓存和查询层；
- 关系是一等公民：`ownedBy / providesApi / consumesApi / dependsOn / partOf`；
- `System` 是若干 Component 的集合，对外暴露 API；`Domain` 是共享业务语言/模型的 System 集合；
- 官方明确：Catalog 的目标是**捕捉人的心智模型和关系**，不是穷举所有对象；自动化分类只建议用来**生成初始文件**，之后由人维护。

值得借鉴：

- **仓库内 descriptor + 组织级自动发现**，比平台单方面分析更可持续；
- **API 是独立实体**，不要塞进仓库简介；
- **System / Domain 分组**，避免地图变成一堆平铺仓库；
- **人工治理**：AI 生成初稿，人通过 Git 工作流修正。

参考：

- <https://backstage.io/docs/features/software-catalog/>
- <https://backstage.io/docs/features/software-catalog/system-model>
- <https://backstage.io/docs/features/software-catalog/creating-the-catalog-graph>
- <https://backstage.io/docs/integrations/github/discovery>
- <https://backstage.io/docs/features/software-catalog/well-known-relations>

### 2.2 商业 Service Catalog / Context Graph（Cortex、OpsLevel、Port、Compass）

这类产品解决的是「目录永远过期」的问题。

Cortex 的 Context Graph 思路：

1. 连接 Git、K8s、CI/CD、监控、 incident 等信号；
2. 自动发现 service / resource / ownership / dependency；
3. 用 AI 补全缺失字段、解决冲突；
4. 把结果用于所有权、成熟度、合规、变更决策。

OpsLevel 的 Service Detection：

- 集成 Git provider 后直接扫描仓库，自动建立 service catalog；
- 官方案例：9 分钟导入 315 个 service；
- 重点不是「列仓库」，而是发现 **orphaned services、依赖、ownership、健康和合规**。

Port / Compass 类似：把仓库、服务、API、团队、工具链映射成一张持续更新的 context graph，再叠加 scorecard / maturity。

值得借鉴：

- **自动发现优先**，人工补录兜底；
- **ownership 是最难也最重要的字段**；
- **地图要能自更新**：webhook + 周期扫描 + 多源信号；
- **地图要能回答治理问题**：谁负责、是否健康、是否合规、是否孤儿。

参考：

- <https://www.cortex.io/product/service-catalog>
- <https://www.opslevel.com/resources/build-your-catalog-with-service-detection>
- <https://www.opslevel.com/discover/service-catalog>
- <https://www.port.io/>
- <https://www.atlassian.com/software/compass>

### 2.3 业务能力地图（Ardoq、LeanIX）

企业架构（EA）领域把「组织能做什么」抽象成 Business Capability Map。

Ardoq 的 Business Capability Modeling 强调：

- 把 **capability、people、process、application** 连起来；
- 评估 capability 的 **maturity / differentiation / impact**；
- 做 **capability realization**：一个能力由哪些系统、仓库、团队实现；
- 用动态视图和 heatmap 回答「哪里重叠、哪里缺失、哪里该投资」。

LeanIX 也提供标准业务能力地图和 application portfolio 视图。

值得借鉴：

- 组织仓库地图不应该只有「仓库 → 能力」，还应该有「能力 → 仓库」的反向视图；
- 能力要分层（L1/L2/L3），否则会变成标签云；
- 能力要能评估成熟度、关键性、覆盖情况；
- 用 heatmap 发现 **能力重叠 / 能力空白 / 单点依赖**。

参考：

- <https://www.ardoq.com/business-capability-mapping>
- <https://www.leanix.net/en/products/business-capability-map>

### 2.4 Context Map + C4（DDD / 架构可视化）

DDD 的 Context Map 解决「边界和关系」：

- Bounded Context 是模型边界；
- 团队关系：Mutually Dependent / Upstream-Downstream / Free；
- 集成模式：Open-host Service、Conformist、Anticorruption Layer、Shared Kernel 等；
- Context Map 既能分析存量系统，也能做前置设计。

C4 解决「层次」：

- Software System → Container → Component → Code；
- 先系统上下文，再容器，再组件，最后才是代码；
- 细节放最后，避免一上来就淹没在文件里。

值得借鉴：

- 仓库职责描述应该写 **upstream / downstream / shared kernel / ACL**，而不只是「依赖了谁」；
- 组织仓库地图的默认视图应该是 System Context，而不是代码结构；
- 跨仓依赖要区分「业务上游/下游」和「技术依赖」。

参考：

- <https://martinfowler.com/bliki/BoundedContext.html>
- <https://github.com/ddd-crew/context-mapping>
- <https://c4model.com/>

### 2.5 Code Graph / AI Wiki（Nx、DeepWiki、OpenDeepWiki）

这类工具是「证据层」，不是「组织地图」：

- Nx 从 workspace 依赖自动生成 Project Graph，用于任务编排和缓存；
- DeepWiki / OpenDeepWiki / RepoWiki 从代码生成仓库 Wiki，适合理解实现；
- 它们擅长文件、模块、调用链，不擅长组织职责、业务能力、团队边界。

值得借鉴：

- 用代码图/依赖图**验证**仓库关系，而不是让 AI 凭感觉写依赖；
- 用 AI Wiki 作为单仓证据，但组织地图只消费它的「能力/职责摘要」；
- 不要让文件路径和接口清单进入组织地图主视图。

参考：

- <https://nx.dev/features/explore-graph>
- <https://github.com/AIDotNet/OpenDeepWiki>
- <https://github.com/AsyncFuncAI/deepwiki-open>
- <https://github.com/he-yufeng/RepoWiki>

---

## 3. 推荐的组织仓库地图模型

### 3.1 最小实体模型

不要一上来做很重的 EA 模型。建议先落这 6 类实体：

| 实体 | 作用 | 关键字段 |
|---|---|---|
| Domain | 业务域，组织级分组 | name、description、owner |
| System | 若干仓库组成的系统，对外提供能力 | name、domain、owner、repos |
| Repository | 代码仓库 | repo_key、owner、lifecycle、criticality |
| Capability | 业务能力（L1/L2/L3） | name、level、description、maturity |
| API | 仓库对外/对内提供的接口 | name、kind、provider_repo、consumers |
| Team | 负责团队 | name、members、owned_repos |

关系：

```text
Repository --partOf--> System --partOf--> Domain
Repository --ownedBy--> Team
Repository --provides--> API
Repository --consumes--> API
Repository --dependsOn--> Repository
Capability --realizedBy--> Repository
```

### 3.2 单仓卡片（Repository Card）

单仓卡片是组织地图的原子单位，建议固定四段：

1. **一句话定位**：为什么存在、为谁提供什么价值；
2. **核心功能**：3–5 项业务能力，不按文件/模块列；
3. **核心职责**：负责什么、不负责什么、组织角色/上下游；
4. **其他能力**：短名称概括，不展开。

接口、依赖、文件路径作为机器字段保留，供召回、依赖图和审计使用，不进入主视图。

### 3.3 组织地图视图

至少提供三种视图：

1. **目录视图**：Domain → System → Repository 的树/表；
2. **能力视图**：Capability → Realizing Repositories，回答「这个能力谁实现、覆盖如何」；
3. **关系视图**：仓库/系统依赖图，区分 `provides/consumes/dependsOn`，标记 observed / inferred。

再加一个治理视图：

- 未分析仓库；
- 无 owner 仓库；
- 未映射能力的仓库；
- 超过 N 天未刷新的卡片；
- 多个仓库声明同一核心能力的重叠项；
- 单点依赖/高 fan-in 仓库。

---

## 4. 推荐实施流水线

### Step 1：发现（Discover）

- 通过 VCS 组织 API 列出全部仓库；
- 支持手动绑定/解绑；
- 记录 `default_branch / topics / description / CODEOWNERS / languages / manifests / last_commit`；
- webhook 订阅 `push` / `repository`，作为增量刷新触发。

### Step 2：证据抽取（Extract）

先做确定性抽取，不直接让 AI 自由发挥：

- README、仓库自维护的 `REPO_MAP.md / CAPABILITIES.md`；
- `project-info/project-capabilities.md`、`project-graph.md`；
- 包管理文件（`pyproject.toml / package.json / go.mod / pom.xml`）；
- OpenAPI / AsyncAPI / Protobuf / GraphQL schema；
- CODEOWNERS、组织团队映射；
- 目录结构、入口文件、CI 配置；
- 跨仓依赖：内部包名、API client、事件 topic、共享库。

每条证据记录来源、时间、commit SHA、置信度。

### Step 3：能力聚类（Cluster）

推荐「确定性候选 + AI 归纳 + 人工确认」：

1. 确定性信号先给出候选：topics、README 标题、manifest 依赖、目录名、CODEOWNERS；
2. AI 只做「聚类和命名」：把仓库归到 Domain/System，提炼 3–5 个核心能力；
3. AI 必须引用证据，不能凭空造能力；
4. 低置信度进入人工复核队列；
5. 人工修改后写回仓库 descriptor，而不是只存平台 DB。

### Step 4：关系构建（Relate）

分两类边，必须可解释：

- **Observed（观测到的）**：manifest 内部包依赖、OpenAPI 引用、代码 import、CODEOWNERS、组织团队关系；
- **Inferred（推断的）**：AI 从 README/职责描述推断的上下游、共享领域。

推荐边类型：

```text
ownedBy / partOf / dependsOn / providesApi / consumesApi /
upstreamOf / downstreamOf / sharedKernelWith
```

每条边至少记录：`source / target / type / evidence / confidence / observed_at`。

### Step 5：发布（Publish）

采用「仓库是事实源，DB 是缓存」：

- 组织仓库 `grape-org` 分支保存人类可读 MD（组织地图 + 单仓卡片）；
- 同时保存机器索引 JSON（实体、关系、覆盖度、新鲜度）；
- 平台 DB 只做查询缓存和 UI 数据源；
- UI 提供目录、能力、关系、治理四种视图。

### Step 6：治理（Govern）

- `source_commit` / `analyzed_at` / `stale`；
- 增量刷新：push 触发 + 每日兜底；
- 人工复核：AI 初稿 → 人确认/修改 → 写回 descriptor；
- 质量指标：覆盖率、owner 覆盖率、能力覆盖率、孤儿仓库、过期卡片、能力重叠、单点依赖；
- 覆盖度低于阈值时，自动下派降级为人工确认；
- 建立路由回归集：历史已下派任务 + 人工标注的正确目标仓，跟踪 `precision@1`、`recall@3`、MRR；每次改 prompt/schema 都回归一次。

---

## 5. AI 应该怎么用

推荐「两阶段 + 证据约束」：

1. **事实抽取阶段**：从 README、capabilities、graph、manifest、OpenAPI 中抽事实；
2. **归纳阶段**：只基于事实聚类、命名、写职责边界；
3. **输出约束**：核心能力 3–5 个、其他能力 ≤10 个、每条结论带 evidence；
4. **人工兜底**：低置信度、冲突、无证据 → 进入 review，不直接进入自动下派；
5. **增量**：按 commit 变化只重跑受影响仓库，不每次全量 deep analyze。

不要做的事：

- 不要一次 prompt 让模型「读整个仓库并写完整地图」；
- 不要让模型自由生成依赖关系；
- 不要把接口清单/文件路径作为能力卡正文；
- 不要用 AI 覆盖人工维护的 descriptor；
- 不要对 20+ 仓库默认全量 deep analyze。

---

## 6. 单仓能力卡如何做到「可路由」

组织地图要能精准下派，单仓卡片不能只是描述，必须具备**判别力**。建议每个核心能力都带：

```json
{
  "name": "订单履约",
  "description": "支付成功后推进订单状态并触发发货",
  "keywords": ["订单状态", "支付回调", "履约"],
  "task_examples": [
    "支付成功后自动推进订单状态并触发发货",
    "订单状态机增加取消分支"
  ],
  "not_for": [
    "用户登录失败排查",
    "库存扣减接口性能优化"
  ],
  "owned_entities": ["订单", "订单状态", "履约单"],
  "change_types": ["api", "database", "business_logic"]
}
```

四个字段的作用不同：

- `task_examples`：让召回「按任务相似度」匹配，而不是只匹配关键词；
- `not_for`：显式表达边界，避免「看到订单两个字就派给订单仓」；
- `owned_entities`：数据/领域对象往往比接口名更稳定，适合跨仓路由；
- `change_types`：区分 API、DB、UI、业务逻辑变更，支持按任务类型选仓。

仓库级再加一个 `routing`：

```json
{
  "include_keywords": ["订单", "履约", "支付回调"],
  "exclude_keywords": ["用户登录", "库存扣减"],
  "typical_requests": ["订单支付后未发货", "取消订单"]
}
```

路由流程建议：

```text
任务文本
  → 规则召回（purpose/domain/keyword/interface + task_examples/owned_entities）
  → 取 top-K 候选
  → AI 只从候选中选，输出 matched_capability + reason + confidence
  → 平台校验（组织归属/场景/上限）
  → 高置信度自动下派；低置信度转人工
```

关键约束：

- AI 必须指出「命中了哪个核心能力」，不能只给仓库名；
- 必须能解释「为什么不是其他候选仓」；
- `not_for` / `exclude_keywords` 只做降权或边界提示，不做硬排除（多域任务可能同时涉及多个仓）；
- 低置信度不自动扩散。

## 7. 一句话总结

**组织仓库地图的核心不是「让 AI 描述每个仓库」，而是「建立一套可持续演进的实体-能力-关系模型」，让 AI 只负责生成初稿和证据，人负责治理事实源。**

最值得抄的三件事：

1. Backstage 的 **catalog-as-code + 自动发现 + System/Domain 分层**；
2. Cortex/OpsLevel 的 **多源信号自动发现 + ownership + 持续保鲜**；
3. Ardoq/LeanIX 的 **能力地图 + 能力实现/成熟度/覆盖度**。

最不该抄的一件事：把代码 Wiki 的文件/接口清单直接当成组织仓库地图。