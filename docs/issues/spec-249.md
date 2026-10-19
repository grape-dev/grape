# Spec: Issue #249 — 引擎失联后场景设置引擎选择计数未同步

> Bug Fix | Priority: P1 | 2026-09-25

## 目标
修复引擎失联（offline/about_to_gc）后场景设置页面引擎计数徽章未同步的问题，使已选引擎数量仅反映实际可用（online/recently_lost）的引擎数。

## 根因
`dashboard.js` 中引擎计数徽章直接使用 `node.engine_runtime_ids.length` 显示已选数量，未与 `agent_engines` 列表交叉过滤离线引擎。而后端 `dashboard.py` 返回的 `agent_engines` 已包含全部引擎（含 offline/about_to_gc）及其 `status` 字段。导致引擎失联后计数徽章仍计入该引擎，与实际可用引擎不一致。

## 改动范围

### In Scope
- `src/api/static/js/pages/dashboard.js`：提取公共函数 `_filterAvailableEngineIds`，在 3 处计数徽章渲染点交叉过滤

### Out of Scope
- 引擎选择 modal 内部过滤逻辑（modal 显示全部引擎含状态徽章，Issue #227 已确认此行为）
- 后端 `scenes.py get_scene_detail` 增强（可选，前端可独立完成）
- 引擎失联自动移除/清理逻辑（runtime 生命周期管理）

## 技术方案

### 公共函数
```js
function _filterAvailableEngineIds(engineIds, agentEngines, agentType) {
  if (!agentEngines || !agentEngines[agentType] || agentEngines[agentType].length === 0) {
    return engineIds || [];
  }
  const available = agentEngines[agentType];
  const availableMap = {};
  available.forEach(e => {
    if (e.status === 'online' || e.status === 'recently_lost') {
      availableMap[e.id] = true;
    }
  });
  return (engineIds || []).filter(id => availableMap[id]);
}
```

### 应用点（3 处）
1. `_renderFlowCards`（查看模式，~line 896）
2. `_renderEditFlowCards`（编辑模式，~line 1096）
3. `renderAgentCards`（场景详情页 agent 卡片，~line 1661）

### Fallback 策略
若 `agentEngines` 为空或该 agentType 无引擎列表，退回显示原始 `engineIds`（不劣于现状）。

## 验收标准
- [x] 引擎失联后计数徽章不计入该引擎
- [x] 已选数量与 modal 可见可用引擎数一致
- [x] 引擎恢复后计数自动恢复
- [x] 查看模式和编辑模式均正确同步
- [x] 所有现有测试通过 + 新增测试覆盖
