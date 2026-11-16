# Spec: 换名孤儿引擎行即时清理——refresh diff 下线时收敛同前缀旧名行

- 日期：2026-10-29
- 状态：spec 待评审
- 背景：引擎名含可变段（模板 name slug / Provider name slug），改名 → refresh 注册新名行 +
  旧行 diff 标 offline——**旧行永不删除**（无自动 GC，实测一天积 5 条死行：
  cubesandbox / cubesandbox-s1 段的 hermes 双引擎全留存页面）。用户选定方案 2：
  换名场景即时收敛，不等 6 天 about_to_gc。
- 对照方案 1（周期 GC >6 天删）被否：等待期长、且把"换名残留"和"真死引擎"混为一谈——
  换名残留是**确定性的垃圾**（新名行在线 = 旧名行必死），可安全即时清。

## 1. 核心判定：什么是"换名孤儿"

```
引擎行身份前缀 = (engine, provider_id)  —— 不变的锚（第 1 段 + 第 4 段）
可变段         = template slug（第 2 段）+ provider_name slug（第 3 段）

换名孤儿 ≡ 同一 worker（credential_id）下：
  行 A（旧名）offline/无心跳
  且存在行 B（新名）online/心跳新鲜
  且 A、B 的 (engine 第1段, provider_id 第4段) 相同
  且 A 不在任何场景 engine_runtime_ids 绑定中
```

不能只按"名字相似"删——必须锚定不变段，防止误删真引擎。

## 2. 实施位置：refresh diff 下线流程（cloud_worker._refresh_templates）

现状 diff：`_deleted = _old_names - _new_names` → 注册 offline → 调用方从心跳剔除。

新增第 3 步（服务端处理更稳——worker 不该直接删行，见 §3 裁量）：

```python
# _refresh_templates 内，_deleted 非空时：
# 对每个 deleted 名，拆出锚 (engine, provider_id)
# 随 diff 注册一起上报"换名收敛"请求：DELETE 候选 = 同凭据下
#   name 以 {engine}: 开头 且 以 :{provider_id} 结尾
#   且不在 new_names 中（本轮还活着的不能删）
#   且无场景绑定（服务端查 engine_runtime_ids 引用）
```

## 3. 关键设计决策

### D1. 删除执行方：服务端 register 端点顺带处理（推荐）

worker 的 register 请求已带全量 runtimes 列表——服务端处理 diff 下线时**本就知道**
哪些名字消失了。扩展服务端 register（或专用下线注册路径）：

- 收到下线注册（healthy=False 批量）时，对每行拆锚 (engine, provider_id)
- 在**同 credential_id** 范围内找：`(name LIKE '{engine}:%:{provider_id}')`
  且 `status != 'online'`（防误删活行——竞态保护）
  且不被 `devops_pipeline_scene.nodes_json` 任何 engine_runtime_ids 引用
  且不被 `devops_agent_session.runtime_id` 引用（活跃会话主引擎）
- 满足 → DELETE 行 + engine_capabilities 级联
- 不满足绑定 → 保留行但记 log（下次再试）

**不在 worker 侧直接 DELETE**（DB 写权收敛在服务端，worker 只有 API 通道）。

### D2. 安全条件逐条

| 条件 | 理由 |
|---|---|
| 同 credential_id 限定 | 别的 worker 同名引擎不受影响 |
| 锚 (engine, provider_id) 精确 | 模板段/provider 段可变，锚不变；防跨 Provider 误删 |
| `status != 'online'` | 心跳竞态：万一同名行刚被别的实例刷活 |
| 不在 new_names（本轮） | 同锚的新行刚注册，锚对应的"活体"存在——删的只是旧壳 |
| 无场景绑定 | 用户绑过旧引擎的**不删**（绑定悬空比页面脏更糟）——保留行让用户看到并改绑（改名提示语闭环） |
| 无活跃会话引用 | devops_agent_session.runtime_id 同理 |

### D3. 与 about_to_gc 的关系

本方案只管"换名孤儿"（有活体新名行存在 → 确定性垃圾）；
真死引擎（无新名行，如 worker 永久下线）仍走现有 offline/about_to_gc 展示，
**不自动删**（方案 1 的周期 GC 若将来要做，条件独立、互不冲突）。

## 4. 影响面

| 文件 | 改动 |
|---|---|
| runtime.py register 端点（下线注册分支） | 锚拆解 + 候选查询 + 绑定检查 + DELETE ~50 行 |
| cloud_worker.py | 无改动（diff 上报已含足够信息；或下线注册 payload 带锚提示 ~5 行可选） |
| 前端 | 无（行消失即列表少一行） |

## 5. 验收标准

1. **换名收敛**：改名 Provider（s1 → s2）→ 60s refresh → 新名行 online、
   **同锚旧名行（s1 段）从页面消失**（不是 offline 留存）
2. **绑定保护**：场景绑定了旧名行 → 该行保留（offline），不删；
   log 记录"binding preserved"
3. **锚精确**：Provider 38 的 hermes 换名，不影响 Provider 25 的 hermes:...:25 行
4. **活体保护**：模拟竞态（旧名行突然 online）→ 不删
5. **真死引擎不受影响**：worker 永久下线的引擎行仍 offline 保留（现有语义）
6. 回归：register/refresh/心跳 15 套件全量 + 新增单测
   （锚拆解/候选查询/绑定检查三个纯函数 ~4 例）

## 6. 风险

| 风险 | 缓解 |
|---|---|
| LIKE 匹配引擎名含特殊字符（% _） | 锚用精确 parse（split 取段）+ 参数化，不走 LIKE 拼接；或 escape |
| 删除误伤历史审计需求 | 行删除不可逆——可选先记 tombstone 表（审计后再物理删）；首版直接删（页面本就当垃圾看） |
| 多模板同锚并发注册 | status!=online + 不在 new_names 双条件已覆盖 |

## 7. 改动量

后端 ~50-60 行 + 单测 4 例；预估半天（含 .17 热修 E2E：实际改一次 Provider 名观察收敛）。
