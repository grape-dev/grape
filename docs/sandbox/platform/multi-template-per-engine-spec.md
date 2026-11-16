# Spec: 同 Provider 多模板共用引擎（引擎名四段式 + 绑定多对多 + 场景直绑路由）

- 日期：2026-10-29（v2，吸收用户裁决：template 段位置/不可变名策略/去轮询）
- 状态：spec 定稿，待开工
- 背景：模板可声明多引擎已支持；改为「同 Provider 下不同模板可声明相同引擎」。
  用户设想场景：同一沙箱平台（Provider）创建多个模板，给不同仓库用——用户在
  场景中把 vod 等绑定到具体 (引擎,模板) 四元组，路由由用户配置决定。
- 现状约束：引擎名三段式 `{engine}:{platform}:{provider_id}` 无 template 维度；
  bind-engine R5 409 + DB `UNIQUE(provider_id, engine_type)` 双重禁止同引擎多模板。

## 1. 用户裁决记录

1. **引擎名四段式，template 紧跟 engine**：`{engine}:{template}:{platform}:{provider_id}`
   ——engine+template 是用户选引擎的语义单元（"hermes 用这个模板"），platform/
   provider_id 是寻址尾巴
2. **模板名不可变性**：name/alias 均可编辑改（实证 UPDATE 端点）；DB 自增 id
   唯一不可变。策略：**引擎名第四段 = 模板 name 规整化（`[a-z0-9-]`，≤16 字符
   截断）；name 冲突/为空时用 `t{id}` 兜底**。模板页/详情**展示 DB id**
   （小字/`#25` 徽标），保证引擎名 ↔ 模板肉眼可对。
   模板改名 → 引擎名随下次 refresh 变（旧行 diff 下线、新行注册，与存量
   过渡同机制，平滑）
3. **不轮询**：场景配置 `engine_runtime_ids` 本就支持把 agent 绑到具体引擎行
   （plugin 模式限定广播池，legacy 按优先级选）。四元组行对引擎选择层只是
   "更多可选行"，**路由完全遵循既有 pick_engine 语义，零特殊逻辑**——改动面
   大幅缩小
4. **Engines 页面每四元组一张独立卡片**：四段名注册即独立行，天然满足；
   验收确认卡片可读四元信息

## 2. 影响面（源码实证接触点）

### A. 引擎名（核心）

| # | 位置 | 改动 |
|---|---|---|
| A1 | cloud_worker.py:135 `_expand_templates` | 四段 `f"{engine}:{tpl_slug}:{platform}:{provider_id}"`；tpl_slug=模板 name 规整化，冲突/空 → `t{db_id}` |
| A2 | runtime.py:885 兜底解析 / settings.py:887 split | 段数兼容（engine 仍第 0 段；platform/provider 位置变——**逐处核对修正**） |
| A3 | runtime.py:1965/2002 `split(":")[0]` 取 engine | 不变 ✓ |
| A4 | worker_skills.py:100 / agent.py:659 段解析 | 逐处核对 |
| A5 | instances.py instance_name | 引擎名+~16 字符，TEXT 无约束 ✓ |

存量三段名行：继续工作（精确匹配），refresh diff 自然下线，平滑过渡。

### B. 绑定多对多

| # | 位置 | 改动 |
|---|---|---|
| B1 | `UNIQUE(provider_id, engine_type)` | → `UNIQUE(provider_id, engine_type, template_id)` |
| B2 | bind-engine R5 409 冲突拒绝 | 删除；同模板重复绑仍幂等 |
| B3 | unbind-engine | 按 (engine, template) 精确删 |
| B5 | 前端绑定状态/展示 | 按 (模板×引擎)；同引擎多模板共存展示 |

### C. 展开与注册

- `_expand_templates` 按 name 去重：四段名互异 → 每 (引擎,模板) 组合一个 runtime 行
- runtime_info.template_id = 该组合行的模板；模板禁用 → 仅该组合行下线（**本特性
  核心收益**），其他模板同引擎行保持
- 服务端 register 匹配键 name 四段化自动区分，无代码改动

### D. 路由

**无改动**（裁决 3）——pick_engine/场景 engine_runtime_ids/claim 既有语义直接
适用于四元组行。

## 3. 实施步骤

1. B1/B2/B3 绑定多对多（迁移 + 端点）
2. A1 四段式展开（tpl_slug 规整 + 兜底）
3. A2-A4 消费方段解析核对修正
4. 模板列表/详情展示 DB id（B5 顺带）
5. 前端绑定多对多展示
6. .17 热修 + E2E

## 4. 验收标准

### E2E

1. Provider X 模板 T1/T2 各绑 [hermes, opencode] → 注册 **4 行**四段名
   （hermes:t1slug:.../hermes:t2slug:.../opencode:t1slug:.../opencode:t2slug:...）
2. 同引擎多模板：T1、T2 都绑 hermes → 200（不 409）；两模板详情均显示已绑
3. **场景直绑**：场景 node engine_runtime_ids 绑定 hermes:T1 四元组行 →
   vod 任务只路由到 T1 建箱（sandbox_prepare log template=T1 的 tpl id）
4. **单模板禁用细粒度**：禁用 T2 → 仅 hermes:T2 行下线，hermes:T1 保持 online
   接任务
5. 解绑精确：T2 解绑 hermes → 仅 hermes:T2 行下线
6. 模板改名 → refresh 后引擎名更新（旧行下线新行注册）；模板页显示 DB id
7. 存量三段名行平滑过渡（diff 下线，服务不中断）

### 回归

8. Engines 页面四元组卡片可读（engine/template/platform/provider 全可见）
9. pick_engine 优先级/健康分语义不变（无任何路由代码改动，纯回归确认）
10. 15 套件全量 + 新增单测（绑定多对多/四段展开/段解析兼容）预估 +6~8 例

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 段位置变化（template 插入第 1 段，platform/provider 后移）——比 v1 方案多破一处段假设 | A2-A4 grep 清单逐个过 + 防御性解析 + 单测 |
| 模板 name 改名引发引擎行换名（短暂下线+上线） | refresh diff 机制天然处理；改名频率低 |
| tpl_slug 规整后撞名（两模板 name 规整后同 slug） | `t{db_id}` 兜底保证唯一 |

## 6. 改动量

- 后端 ~100 行（绑定迁移+端点 60 / 四段展开+消费方 40）
- 前端 ~50 行（绑定展示 + 模板 id 展示）
- 测试 +6~8 例
- 预估 1.5 天（含 .17 E2E）
