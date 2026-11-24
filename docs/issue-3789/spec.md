# Issue #3789 — 场景节点持久化 skill_id（repo→official）

## 目标
- 场景保存/应用链路解析并持久化节点 `skill_id`，保存期做技能档位（scope）校验。
- 派发 `resolve_skill_dispatch` 以 `skill_id` 为权威；缺 id 时按 **repo→official** 降级，
  并在事件 payload 标记 `skill_source=name_degraded`。
- 存量场景一次性幂等回填：repo 级场景禁止 user 档技能。

## 背景
- 场景 `nodes_json` 历史仅存 `skill_name`，派发每次降级名称解析并刷 WARNING
  （Loki 实测 39 条/时，覆盖 11 组场景节点）。
- Issue #979 的 202611040001 回填按 user→repo→official，repo 场景可能被写入同名
  user 档 `skill_id`，派发/应用按 id 命中 user 档 → 触发 #3788 scope fail-closed。

## 验收
- 保存后节点含 `skill_id`；无 "node without skill_id" WARNING（存量迁移后）。
- repo 级场景节点无 user 档技能；同名 user 技能不被误绑。
- 降级派发 payload 带 `skill_source=name_degraded`；非法档位保存被拦截（沿用 #1758）。
- 现有测试通过 + 新增测试。