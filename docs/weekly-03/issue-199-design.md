# Issue #199 设计方案：项目知识绑定 Agent/场景页面反馈优化

## 1. 问题概述

### 1.1 Issue 描述
> 项目知识新增编辑绑定agent和绑定场景页面反馈迟缓，页面响应缓慢，缺乏及时反馈

### 1.2 根因分析

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| R1 | 前端 toggleAgentRef (knowledge.js:295-309) | 点击 Agent 标签 → POST 绑定/解绑 → loadRefs → GET /knowledge/{id} 全量重载（含正文 + 所有 refs + 每个 scene 的 pipeline_config） | 2 轮串行 RTT + 后端 N+1 查询，用户等待时间长 |
| R2 | 前端 toggleAgentRef / removeSceneRef | 无 loading 态、无过渡动画，点击后页面无变化直到请求完成 | 用户无法感知"正在处理" |
| R3 | 前端 toggleAgentRef (296-309) | submitting 锁 10s 超时后自动释放，但超时后无提示、无恢复 | 超时后用户不知所措，页面状态不明 |
| R4 | 前端 confirmAddScenes (415-428) | 添加场景后同样走 loadRefs 全量重载 | 同 R1 |
| R5 | 前端 saveCreate (628-661) | 创建知识 → 绑定关联 → loadStats → loadList，4 步串行 | 新建操作等待 4 轮 RTT |
| R6 | 前端 saveEdit (509-529) | 编辑保存后 viewContent 全量重载详情 | 只需刷新元数据却重载全文 |
| R7 | 前端 openEntryModal (571-583) | loadScenes() + loadRepoScenes() 请求同一 API 两次 | 冗余请求 |
| R8 | 后端 get_knowledge_detail (knowledge.py:452-462) | 逐个 scene 调 get_pipeline_config，N 个场景 = N 组 DB 查询 | db_lock 持有时间随场景数线性增长 |
| R9 | 后端 POST /refs/batch、DELETE /ref/{id} | 返回值仅 {ok: true}，前端必须再发一次请求获取最新 refs | 多一轮 RTT |

### 1.3 不采用乐观更新的原因

乐观更新（点击立即切换 UI，失败回滚）在绑定/解绑场景有风险：
- 绑定失败时 UI 先变绿再变回，比"等一会儿"更让人困惑
- 用户可能以为已绑定成功，离开页面后实际未绑上
- 这是功能性误导，不是单纯体验问题

**方案选择**：不做乐观更新，改为缩短真实等待时间 + 请求期间的 loading 态 + 平滑动画过渡。

## 2. 改动范围

| 层 | 文件 | 改动内容 |
|----|------|----------|
| 后端 | src/api/routes/knowledge.py | get_knowledge_detail JOIN nodes_json 消除 N+1 |
| 前端 JS | src/api/static/js/pages/knowledge.js | 重写 toggleAgentRef / removeSceneRef / confirmAddScenes / saveEdit / saveCreate；删除死代码 loadScenes / loadRefs / removeAgentRef / addAgentByType / addSceneById / openAgentPopover / toggleAgentSelect / confirmAddAgents；清理 data 残留字段 scenes / agentSelected / addAgentPopover |
| 前端 JS | src/api/static/js/app.js | showToast 去重 + 淡入淡出过渡 |
| 前端 CSS | src/api/static/css/app.css | 新增 loading 态 + 进出场动画 |
| 前端 HTML | src/api/static/index.html | JS 版本号递增 |
| i18n | locales/zh/knowledge.json + locales/en/knowledge.json | 新增 11 个文案 key |

**后端 POST /refs/batch 和 DELETE /ref/{id} 不改**——前端操作成功/超时/失败后统一调 GET /knowledge/{id} 详情接口刷新。

## 3. 详细设计

### 3.1 后端改动

#### 3.1.1 get_knowledge_detail JOIN nodes_json 消除 N+1

**原代码**逐个 scene 调 get_pipeline_config（N 次循环 DB 查询）。

**实际实现**比设计文档原方案更简洁——不新增 _batch_query_pipeline_config 函数，直接在 LEFT JOIN 中查出 s.nodes_json，循环内 json.loads 解析：

```python
# knowledge.py get_knowledge_detail 内
refs = db.cursor().execute(
    "SELECT r.id, r.scene_id, r.agent_type, r.enabled, r.created_by, r.created_at, "
    "s.name, s.description, s.display_name, s.scene_id, s.nodes_json "
    "FROM scene_knowledge_ref r "
    "LEFT JOIN devops_pipeline_scene s ON r.scene_id = s.scene_id AND s.repo_key = ? AND s.is_active=1 "
    "WHERE r.knowledge_id = ? ORDER BY r.id",
    (row["repo_key"], knowledge_id)
).fetchall()

# 直接解析 JOIN 结果中的 nodes_json
for r in refs:
    pipeline_chain = ""
    if r[10]:
        try:
            nodes = json.loads(r[10])
            pipeline_chain = " → ".join(n.get("tag", n.get("id", "")) for n in nodes if n.get("enabled", True))
        except Exception:
            pass
```

0 次额外 DB 查询，原 N+1 循环和 get_pipeline_config 调用全部删除。

#### 3.1.2 后端接口不改

POST /refs/batch 和 DELETE /ref/{id} 保持返回 {ok: true}，不改响应格式。前端操作成功/超时/失败后统一调 GET /knowledge/{id} 详情接口刷新页面。

### 3.2 前端改动

#### 3.2.1 统一请求-反馈模式

所有绑定/解绑操作采用统一模式：

```
用户点击
  → 立即设置 loading 态（标签/卡片/按钮显示 spinner，禁用其他操作）
  → 发起请求（API 层传 timeout=15000，15s 超时）
  → 成功：调 GET /knowledge/{id} 刷新详情 + Toast 提示 + 动画过渡
  → 超时（15s）：Toast 提示"操作超时，正在刷新最新状态" + 主动调 GET /knowledge/{id} 全量重载
  → 失败（非超时）：Toast 提示错误信息 + 主动调 GET /knowledge/{id} 全量重载确保一致性
  → 导航/切 tab 取消（AbortError + e.silent）：静默，不提示
```

关键设计：
- 不做乐观更新，用户看到的是真实状态
- 请求期间有明确 loading 态（spinner 覆盖在标签/卡片/按钮上）
- 成功后调 GET /knowledge/{id} 刷新详情（含 refs），不再调 loadRefs
- 超时/失败后同样调 GET /knowledge/{id} 全量重载，保证页面与后端一致
- 所有操作超时统一 15s（API 层 _fetchWithTimeout 第三参数传 15000）
- AbortError 区分：!e.silent = 超时（提示+重载），e.silent = 导航取消（静默）

#### 3.2.2 toggleAgentRef 改造

```javascript
async toggleAgentRef(agentType) {
  if (this.submitting) return;
  this.submitting = true;
  this.pendingAgentType = agentType;
  try {
    const kid = this.detail.id;
    if (this.isAgentSelected(agentType)) {
      const ref = this.detail.refs.find(r => r.agent_type === agentType && r.scene_id === '' && r.enabled);
      if (ref) {
        await API.del('/knowledge/ref/' + ref.id, 15000);
        await this._refreshDetailFull();
        app.showToast(this.t('ref_unbind_agent_ok'));
      }
    } else {
      await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: [], agent_types: [agentType] }, 15000);
      await this._refreshDetailFull();
      app.showToast(this.t('ref_bind_agent_ok'));
    }
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      app.showToast(this.t('ref_timeout_refresh'));
      await this._refreshDetailFull();
    } else if (e.name === 'AbortError' && e.silent) {
      // 导航/切 tab 导致的取消，静默
    } else {
      app.showToast(e.message || this.t('ref_failed_refresh'));
      await this._refreshDetailFull();
    }
  } finally {
    this.submitting = false;
    this.pendingAgentType = null;
  }
},
```

超时通过 API 层 `_fetchWithTimeout(url, opts, timeout)` 第三参数控制，不需要前端创建 AbortController。API 层内部 15s 后 abort 并设 `controller._timedOut = true`，catch 里 `e.name === 'AbortError' && !e.silent` 区分超时，`e.silent` 区分导航取消。

#### 3.2.3 新增 _refreshDetailFull 方法

超时/失败后的兜底全量重载：

```javascript
async _refreshDetailFull() {
  if (!this.detail) return;
  try {
    const d = await API.get('/knowledge/' + this.detail.id);
    if (window._currentView !== 'knowledge') return;
    this.detail = d;
    this.$nextTick(() => this.updateSceneScrollBtns());
  } catch (e) {
    if (e.name === 'AbortError' && e.silent) return;
    // 全量重载也失败——提示用户手动刷新
    app.showToast(this.t('refresh_failed') || '刷新失败，请手动刷新页面');
  }
},
```

#### 3.2.4 removeSceneRef 改造

同样模式：loading 态 → 请求 → 成功用响应 refs 更新 / 超时全量重载

```javascript
async removeSceneRef(tag) {
  if (this.submitting) return;
  this.submitting = true;
  this.pendingSceneId = tag.sceneId;
  try {
    await API.del('/knowledge/ref/' + tag.ref.id, 15000);
    await this._refreshDetailFull();
    app.showToast(this.t('ref_unbind_scene_ok'));
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      app.showToast(this.t('ref_timeout_refresh'));
      await this._refreshDetailFull();
    } else if (e.name === 'AbortError' && e.silent) {
      // 导航/切 tab 导致的取消，静默
    } else {
      app.showToast(e.message || this.t('ref_failed_refresh'));
      await this._refreshDetailFull();
    }
  } finally {
    this.submitting = false;
    this.pendingSceneId = null;
  }
},
```

#### 3.2.5 confirmAddScenes 改造

```javascript
async confirmAddScenes() {
  if (this.submitting || this.sceneSelected.length === 0) return;
  this.submitting = true;
  const kid = this.detail.id;
  try {
    await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: this.sceneSelected, agent_types: [] }, 15000);
    await this._refreshDetailFull();
    app.showToast(this.t('ref_added_count', { count: this.sceneSelected.length }));
    this.addScenePopover = false;
    this.sceneSelected = [];
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      app.showToast(this.t('ref_timeout_refresh'));
      await this._refreshDetailFull();
    } else if (e.name === 'AbortError' && e.silent) {
      // 导航/切 tab 导致的取消，静默
    } else {
      app.showToast(e.message || this.t('ref_failed_refresh'));
      await this._refreshDetailFull();
    }
  } finally {
    this.submitting = false;
  }
},
```

#### 3.2.6 新增 data 字段

```javascript
data() {
  return {
    // ... 现有字段 ...
    pendingAgentType: null,    // 正在操作的 agent type，用于显示 loading
    pendingSceneId: null,      // 正在操作的 scene id，用于显示 loading
  };
}
```

#### 3.2.7 loadRefs 方法删除

不再需要 loadRefs 方法——所有操作成功后直接用响应中的 refs 更新。
超时/失败时走 _refreshDetailFull 全量重载。

#### 3.2.8 删除死代码 loadScenes

loadScenes (260-272) 请求 /scenes?repo=... 存到 this.scenes，但 this.scenes 从未被任何模板或 computed 使用。直接删除。

openEntryModal 中的 `this.loadScenes()` 调用也删除，只保留 `this.loadRepoScenes()`。

viewContent 中的 `await this.loadScenes()` 调用也删除。

#### 3.2.9 saveEdit 改造

保存成功后返回列表（backToList），15s 超时提示：

```javascript
async saveEdit() {
  // ... 校验逻辑不变 ...
  if (this.submitting) return;
  this.submitting = true;
  try {
    await API.put('/knowledge/' + this.detail.id, {
      content: this.editForm.content || '',
      description: this.editForm.description || '',
      name: this.editForm.name.trim()
    }, 15000);
    app.showToast(this.t('knowledge_updated'));
    // Issue #199: 保存成功后返回列表
    this.backToList();
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      app.showToast(this.t('save_timeout'));
    } else if (e.name === 'AbortError' && e.silent) {
      // 导航/切 tab 导致的取消，静默
    } else {
      app.showToast(e.message || I18N.t('common.load_failed'));
    }
  } finally {
    this.submitting = false;
  }
},
```

#### 3.2.10 saveCreate 改造

创建 + 绑关联后不再串行 4 步，改为并行 loadStats + loadList，15s 超时：

```javascript
async saveCreate() {
  // ... 校验逻辑不变 ...
  if (this.submitting) return;
  this.submitting = true;
  try {
    const d = await API.post('/knowledge/entry', {
      repo: this.currentRepo(),
      content: this.createForm.content,
      agent_type: '',
      description: this.createForm.description || '',
      name: this.createForm.name.trim()
    }, 15000);
    const kid = d.id || d.knowledge_id;
    if (kid && (this.createForm.agentSelected.length > 0 || this.createForm.sceneSelected.length > 0)) {
      await API.post('/knowledge/' + kid + '/refs/batch', {
        scene_ids: this.createForm.sceneSelected,
        agent_types: this.createForm.agentSelected
      }, 15000);
    }
    app.showToast(this.t('knowledge_created'));
    this.createForm = null;
    this.view = 'list';
    // 并行刷新统计 + 列表
    await Promise.all([this.loadStats(), this.loadList()]);
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      app.showToast(this.t('create_timeout'));
      this.createForm = null;
      this.view = 'list';
      await this.loadList();
    } else if (e.name === 'AbortError' && e.silent) {
      // 导航/切 tab 导致的取消，静默
    } else {
      app.showToast(e.message || I18N.t('common.load_failed'));
    }
  } finally {
    this.submitting = false;
  },
},
```

### 3.3 CSS 动画设计

#### 3.3.1 Agent 标签 loading 态

```css
/* Agent 标签操作中 loading */
.kb-agent-tag.kb-agent-pending {
  pointer-events: none;
  opacity: 0.6;
}
.kb-agent-tag.kb-agent-pending::after {
  content: '';
  position: absolute;
  top: 50%;
  left: 50%;
  width: 16px;
  height: 16px;
  margin: -8px 0 0 -8px;
  border: 2px solid var(--b);
  border-top-color: var(--a);
  border-radius: 50%;
  animation: kb-spin 0.6s linear infinite;
  z-index: 2;
}
```

模板中绑定：
```html
<div v-for="at in agentTypes.filter(a => a.value)" :key="at.value"
  class="kb-agent-tag"
  :class="{
    'kb-agent-selected': isAgentSelected(at.value),
    'kb-agent-pending': pendingAgentType === at.value
  }"
  @click="toggleAgentRef(at.value)">
```

#### 3.3.2 场景卡片 loading 态

```css
/* 场景卡片操作中 loading */
.scn-card.kb-scene-pending {
  pointer-events: none;
  opacity: 0.5;
}
.scn-card.kb-scene-pending .kb-scene-x {
  display: none;
}
.scn-card.kb-scene-pending::after {
  content: '';
  position: absolute;
  top: 50%;
  left: 50%;
  width: 20px;
  height: 20px;
  margin: -10px 0 0 -10px;
  border: 2px solid var(--b);
  border-top-color: var(--a);
  border-radius: 50%;
  animation: kb-spin 0.6s linear infinite;
  z-index: 5;
}
```

#### 3.3.3 Agent 标签选中/取消过渡

已有 `transition: all .25s cubic-bezier(.25,.1,.25,1)` 在 .kb-agent-tag 上，选中时边框/背景/阴影变化会自动有过渡。

额外补充 checkbox 过渡（已有 .kb-agent-check transition: all .2s）。

**无需额外 CSS**——现有 transition 已覆盖。

#### 3.3.4 场景卡片进出场动画

用 Vue <transition-group> 包裹场景卡片列表：

```html
<transition-group name="kb-scene" tag="div" class="scn-scroll-track" ref="sceneTrack" @scroll="updateSceneScrollBtns">
  <div v-for="tag in selectedSceneTags" :key="tag.sceneId" class="scn-card kb-scene-item" ...>
    ...
  </div>
</transition-group>
```

CSS：
```css
/* 场景卡片进场 */
.kb-scene-enter-active {
  transition: opacity 0.3s ease, transform 0.3s cubic-bezier(0.25, 0.1, 0.25, 1);
}
.kb-scene-enter-from {
  opacity: 0;
  transform: translateY(12px) scale(0.95);
}
/* 场景卡片出场 */
.kb-scene-leave-active {
  transition: opacity 0.25s ease, transform 0.25s cubic-bezier(0.25, 0.1, 0.25, 1);
  position: absolute;  /* leave 期间脱离文档流，其他卡片平滑补位 */
}
.kb-scene-leave-to {
  opacity: 0;
  transform: scale(0.9);
}
/* 卡片位移动画（其他卡片补位） */
.kb-scene-move {
  transition: transform 0.3s cubic-bezier(0.25, 0.1, 0.25, 1);
}
```

#### 3.3.5 确认按钮 loading 态

场景选择弹层和 Agent 选择弹层的确认按钮也加 loading：

```css
.bt.kb-btn-loading {
  pointer-events: none;
  opacity: 0.7;
}
.bt.kb-btn-loading::after {
  content: '';
  display: inline-block;
  width: 12px;
  height: 12px;
  margin-left: 6px;
  border: 1.5px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: kb-spin 0.6s linear infinite;
  vertical-align: middle;
}
```

### 3.4 i18n 文案

新增的 i18n key（中英文）：

| key | 中文 | 英文 |
|-----|------|------|
| knowledge.ref_bind_ok | 绑定成功 | Bound successfully |
| knowledge.ref_unbind_ok | 解绑成功 | Unbound successfully |
| knowledge.ref_bind_agent_ok | 绑定 Agent 成功 | Agent bound successfully |
| knowledge.ref_unbind_agent_ok | 解绑 Agent 成功 | Agent unbound successfully |
| knowledge.ref_bind_scene_ok | 绑定场景成功 | Scene bound successfully |
| knowledge.ref_unbind_scene_ok | 解绑场景成功 | Scene unbound successfully |
| knowledge.ref_agents_added_count | 已添加 {{count}} 个 Agent | Added {{count}} agents |
| knowledge.ref_timeout_refresh | 操作超时，正在刷新最新状态 | Operation timed out, refreshing latest state |
| knowledge.ref_failed_refresh | 操作失败，正在刷新最新状态 | Operation failed, refreshing latest state |
| knowledge.refresh_failed | 刷新失败，请手动刷新页面 | Refresh failed, please reload the page |
| knowledge.save_timeout | 保存超时，请刷新页面确认最新状态 | Save timed out, please reload to verify |
| knowledge.create_timeout | 创建超时，请刷新页面确认是否创建成功 | Create timed out, please reload to verify |

### 3.5 Toast 优化（app.js）

showToast 全局改进（影响所有页面，不只是知识页）：

- **去重**：新 Toast 出现前先移除已有 Toast（`[role="status"].app-toast`），避免连续操作时多个 Toast 叠加
- **淡入淡出**：`opacity:0 → 1` + `translateY(10px) → 0`，0.25s cubic-bezier 过渡
- **点击关闭**：点击 Toast 提前关闭
- **默认时长**：2500ms（原 3000ms），淡出动画 300ms 在计时结束前触发
- **圆角**：10px（原 8px），字号 13px（原 12px），阴影更深

## 4. 改动点清单

### 4.1 后端 (src/api/routes/knowledge.py)

| # | 改动 | 说明 |
|---|------|------|
| B1 | get_knowledge_detail SQL 加 `s.nodes_json` 列 + `s.is_active=1` 过滤 | LEFT JOIN 直接查出 nodes_json |
| B2 | 循环内 json.loads 解析 nodes_json 生成 pipeline_chain | 替换原 N+1 循环调 get_pipeline_config |

### 4.2 前端 JS (src/api/static/js/pages/knowledge.js)

| # | 改动 | 说明 |
|---|------|------|
| F1 | 新增 data: pendingAgentType, pendingSceneId | loading 态标记 |
| F2 | 新增 _refreshDetailFull() | 成功/超时/失败后调 GET /knowledge/{id} 刷新详情 |
| F3 | 重写 toggleAgentRef | 15s 超时 + loading + 成功调详情接口刷新 + 超时全量重载 + Toast 区分 Agent |
| F4 | 重写 removeSceneRef | 同 F3，Toast 区分场景 |
| F5 | 重写 confirmAddScenes | 同 F3 |
| F6 | 重写 saveEdit | 15s 超时 + loading + 成功后返回列表 + Toast |
| F7 | 重写 saveCreate | 15s 超时 + loading + Promise.all 并行 + 超时后清空 createForm 切回 list |
| F8 | 删除 loadRefs / loadScenes | 死代码，统一走 _refreshDetailFull |
| F9 | 删除 removeAgentRef / addAgentByType / addSceneById | 死代码，模板未调用 |
| F10 | 删除 openAgentPopover / toggleAgentSelect / confirmAddAgents | 死代码，弹层模板不存在 |
| F11 | 清理 data 残留字段 scenes / agentSelected / addAgentPopover | 无引用 |
| F12 | 模板：Agent 标签加 :class kb-agent-pending | loading 态绑定 |
| F13 | 模板：场景卡片加 :class kb-scene-pending | loading 态绑定 |
| F14 | 模板：详情页 + 新增页场景卡片用 <transition-group> 包裹 | 进出场动画 |
| F15 | 模板：saveEdit / saveCreate / confirmAddScenes 按钮加 :class kb-btn-loading | loading 态绑定 |

### 4.3 前端 JS (src/api/static/js/app.js)

| # | 改动 | 说明 |
|---|------|------|
| A1 | showToast 去重 | 新 Toast 替换旧 Toast，避免叠加 |
| A2 | showToast 淡入淡出 | opacity + translateY 过渡，0.25s |
| A3 | showToast 点击关闭 | 点击 Toast 提前移除 |

### 4.4 前端 CSS (src/api/static/css/app.css)

| # | 改动 | 说明 |
|---|------|------|
| C1 | .kb-agent-pending | Agent 标签 loading 态 + spinner |
| C2 | .kb-scene-pending | 场景卡片 loading 态 + spinner |
| C3 | .kb-scene-enter/leave/move | 场景卡片进出场动画 |
| C4 | .kb-btn-loading | 按钮 loading 态 + spinner |

### 4.5 前端 HTML (src/api/static/index.html)

| # | 改动 | 说明 |
|---|------|------|
| H1 | knowledge.js 版本号递增 | 缓存失效 |

### 4.6 i18n

| # | 改动 | 说明 |
|---|------|------|
| I1 | 新增 11 个 i18n key（zh + en） | 绑定/解绑 Agent/场景区分文案 + 超时/失败提示 |

## 5. 用户体验流程

### 5.1 绑定 Agent

```
用户点击 VOD 标签
  → VOD 标签立即显示 spinner + 半透明（loading 态）
  → 其他标签不可点击（submitting 锁）
  → POST /knowledge/{id}/refs/batch（15s 超时）
  → 成功（< 1s）：
      - 标签 spinner 消失
      - 标签平滑过渡到选中态（边框变蓝 + 背景变淡蓝 + ✓ 出现，0.25s CSS transition）
      - Toast 提示"绑定成功"
  → 超时（15s）：
      - Toast 提示"操作超时，正在刷新最新状态"
      - 自动调 GET /knowledge/{id} 全量重载
      - 页面恢复到后端真实状态
  → 失败（网络错误等）：
      - Toast 提示错误信息
      - 自动调 GET /knowledge/{id} 全量重载确保一致
```

### 5.2 解绑场景

```
用户点击场景卡片右上角 ×
  → 该卡片立即显示 spinner + 半透明
  → DELETE /knowledge/ref/{id}（15s 超时）
  → 成功：
      - 卡片平滑缩小淡出（0.25s leave 动画）
      - 其他卡片平滑补位（0.3s move 动画）
      - Toast 提示"解绑成功"
  → 超时/失败：
      - Toast 提示 + 全量重载
```

### 5.3 添加场景

```
用户在弹层选好场景，点确认
  → 确认按钮显示 spinner
  → POST /knowledge/{id}/refs/batch（15s 超时）
  → 成功：
      - 弹层关闭
      - 新场景卡片从下方滑入淡入（0.3s enter 动画）
      - Toast 提示"已添加 N 个场景"
  → 超时/失败：
      - Toast 提示 + 全量重载
```

## 6. 不改动的部分

- 后端单条 POST /refs 接口（add_knowledge_ref）——前端已改用 batch 接口，单条接口保留但不在此次改动范围
- 后端 PUT /ref/{id}（toggle enabled）——前端未使用此接口（解绑用 DELETE），不动
- 后端 GET /{id}/refs 接口——loadRefs 删除后不再调用，但接口保留兼容
- 后端知识 CRUD 逻辑（create/update/delete entry）——不动，只改前端请求后的处理方式
- 知识列表页加载逻辑——不动
- 权限校验逻辑——不动

## 8. 补充设计：创建知识 + 绑定关联合并为单接口（Issue #199 迭代）

### 8.1 问题

原 saveCreate 分两步调 API：
1. POST /knowledge/entry（创建条目）
2. POST /knowledge/{kid}/refs/batch（绑定关联）

如果第一步成功第二步失败，用户留在创建页重试会重复创建条目。

### 8.2 方案

后端 POST /knowledge/entry 的 KnowledgeEntryBody 加两个可选字段：
- `scene_ids: list[str] = []`
- `agent_types: list[str] = []`

后端创建条目拿到 kid 后，同一个请求内完成关联绑定。前端只调一次接口。

### 8.3 后端校验

**scene_ids 批量校验**（一条 SQL，不循环）：

```python
# 校验 scene_ids 存在性——批量查，不循环
if scene_ids:
    placeholders = ",".join("?" * len(scene_ids))
    valid_rows = c.execute(
        f"SELECT scene_id FROM devops_pipeline_scene "
        f"WHERE is_active=1 AND repo_key=? AND scene_id IN ({placeholders})",
        [repo_key] + scene_ids
    ).fetchall()
    valid_ids = {r[0] for r in valid_rows}
    invalid = [sid for sid in scene_ids if sid not in valid_ids]
    if invalid:
        raise HTTPException(status_code=400,
            detail={"code": 400, "message": f"场景不存在: {invalid}"})
```

**agent_types 内存校验**（不查 DB）：

```python
VALID_AGENT_TYPES = {'vod', 'issue', 'coding', 'qa', 'sre', 'chat'}
if agent_types:
    invalid = [at for at in agent_types if at not in VALID_AGENT_TYPES]
    if invalid:
        raise HTTPException(status_code=400,
            detail={"code": 400, "message": f"不支持的 Agent 类型: {invalid}"})
```

**空关联校验**：scene_id 和 agent_type 不能同时为空（复用已有逻辑）。

**幂等**：同一 scene_id + agent_type 跳过已存在的（复用已有逻辑）。

### 8.4 事务保证

创建条目 + 校验 + 绑定关联全部在同一个 db_lock 内，同一次 db.commit()。校验失败时 raise HTTPException，不执行 commit，SQLite 自动回滚——条目不会残留。绑定 INSERT 失败时异常传播，同样不 commit，自动回滚。

事务边界：

```python
from src.session_manager import db, db_lock
with db_lock:
    c = db.cursor()
    # 1. 校验 scene_ids（批量查，不循环）
    if scene_ids:
        placeholders = ",".join("?" * len(scene_ids))
        valid_rows = c.execute(
            f"SELECT scene_id FROM devops_pipeline_scene "
            f"WHERE is_active=1 AND repo_key=? AND scene_id IN ({placeholders})",
            [repo_key] + scene_ids
        ).fetchall()
        valid_ids = {r[0] for r in valid_rows}
        invalid = [sid for sid in scene_ids if sid not in valid_ids]
        if invalid:
            raise HTTPException(status_code=400,
                detail={"code": 400, "message": f"场景不存在: {invalid}"})

    # 2. 校验 agent_types（内存比对，不查 DB）
    if agent_types:
        invalid = [at for at in agent_types if at not in VALID_AGENT_TYPES]
        if invalid:
            raise HTTPException(status_code=400,
                detail={"code": 400, "message": f"不支持的 Agent 类型: {invalid}"})

    # 3. INSERT 条目
    c.execute("INSERT INTO agent_shared_knowledge ...", (...))
    kid = c.lastrowid

    # 4. 绑定关联（幂等，跳过已存在）
    for scene_id, agent_type in items:
        if not scene_id and not agent_type:
            continue
        existing = c.execute(
            "SELECT id FROM scene_knowledge_ref "
            "WHERE knowledge_id=? AND scene_id=? AND agent_type=?",
            (kid, scene_id, agent_type)
        ).fetchone()
        if not existing:
            c.execute(
                "INSERT INTO scene_knowledge_ref "
                "(knowledge_id, scene_id, agent_type, enabled, created_by) "
                "VALUES (?, ?, ?, 1, ?)",
                (kid, scene_id, agent_type, username)
            )

    # 5. 全部成功才 commit
    db.commit()

return {"ok": True, "message": "知识已创建", "id": kid}
```

校验在 INSERT 之前——校验失败时条目还没创建，raise 后不 commit，无副作用。

### 8.5 异常处理策略

| 异常类型 | 后端状态 | 前端处理 | 用户看到 |
|---------|---------|---------|---------|
| 超时（AbortError） | 不确定——请求可能已到后端，条目可能已创建也可能没创建 | 提示"创建超时" + 清空表单 + 跳回列表 + loadList 刷新 | 列表里有条目=成功，没有=未创建 |
| 非超时（400/500/网络错误） | 确定失败——事务回滚，条目不存在 | 提示错误信息 + 留在创建页 | 可安全重试，不会重复创建 |
| 导航取消（AbortError + e.silent） | 不确定 | 静默 | 无提示 |

**不需要 createdKid 防重复逻辑**——非超时失败已回滚可安全重试，超时跳回列表用户自行确认。

### 8.6 前端改动

saveCreate 合并为一次调用，非超时失败留在创建页可安全重试：

```javascript
async saveCreate() {
  // ... 校验逻辑不变 ...
  if (this.submitting) return;
  this.submitting = true;
  try {
    const d = await API.post('/knowledge/entry', {
      repo: this.currentRepo(),
      content: this.createForm.content,
      agent_type: '',
      description: this.createForm.description || '',
      name: this.createForm.name.trim(),
      scene_ids: this.createForm.sceneSelected,
      agent_types: this.createForm.agentSelected
    }, 15000);
    app.showToast(this.t('knowledge_created'));
    this.createForm = null;
    this.view = 'list';
    await Promise.all([this.loadStats(), this.loadList()]);
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      // 超时——不确定结果，跳回列表让用户自行确认
      app.showToast(this.t('create_timeout'));
      this.createForm = null;
      this.view = 'list';
      await this.loadList();
    } else if (e.name === 'AbortError' && e.silent) {
      // 导航/切 tab 导致的取消，静默
    } else {
      // 非超时——事务已回滚，留在创建页可安全重试
      app.showToast(e.message || I18N.t('common.load_failed'));
    }
  } finally {
    this.submitting = false;
  }
},
```

### 8.7 改动点

| # | 层 | 改动 | 说明 |
|---|---|------|------|
| B3 | 后端 | KnowledgeEntryBody 加 scene_ids / agent_types 字段 | 可选，默认空列表 |
| B4 | 后端 | create_knowledge_entry 内加 scene_ids 批量校验 | 一条 SQL 查存在性 |
| B5 | 后端 | create_knowledge_entry 内加 agent_types 内存校验 | 常量集合比对 |
| B6 | 后端 | create_knowledge_entry 内循环 INSERT ref | 幂等，复用已有逻辑 |
| F16 | 前端 | saveCreate 合并为一次 API 调用 | body 带 scene_ids / agent_types |
| F17 | 前端 | 删除 createdKid 防重复逻辑 | 事务保证回滚，非超时失败留创建页可安全重试 |
| I2 | i18n | 删除 create_ok_bind_failed key | 事务保证不会出现"条目已创建但绑定失败" |

## 9. 后续优化项（不属于本次 Issue 范围）

### 9.1 DB 连接池优化

**现状**：整个系统 201 处模块统一使用 `from src.session_manager import db, db_lock`，SQLite 和 MySQL 模式都是单连接 + 应用层 threading.Lock 串行化。没有连接池。

- SQLite 模式：单连接合理（SQLite 写操作锁文件，多连接写会 `database is locked`）
- MySQL 模式：MySQLCompatDB 用 pymysql 单连接 + db_lock 串行化，所有 DB 操作（含读）排队执行。MySQL 本身支持多连接并发，但当前架构没有利用

**问题**：MySQL 模式下单连接 + 全局锁成为并发瓶颈。高并发场景下所有 DB 操作串行排队，吞吐量受限。

**建议**：MySQL 模式下引入连接池（如 dbutils.PooledDB 或 PyMySQL + SQLAlchemy QueuePool），保留 db_lock 用于写操作串行化（防止同一行竞争），读操作不加锁走连接池并发执行。SQLite 模式保持单连接不变。

**前提条件**：需要梳理 201 处 `db, db_lock` 使用点，区分纯读操作和写操作，逐步迁移。工作量较大，建议独立 Issue 推进。
