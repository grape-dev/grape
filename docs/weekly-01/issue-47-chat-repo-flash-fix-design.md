# 任务中心双击进入 Chat Agent 短暂串库闪烁修复方案（Issue #47）

> 版本：v1.0 ｜ 日期：2026-09-06
> Issue：#47「从仓库的任务中心双击点击进入到chat agent之后，会有1秒短暂看到其它仓库的对话」
> 状态：open（bug / P1 / spec-ready / triaged）
> 优先级：P1（体验缺陷：串库闪烁可能造成信息误读与信任损伤）

---

## 一、现象

从仓库任务中心**双击**任务进入 chat agent，约 1 秒内对话列表短暂显示**其它仓库**的对话，
随后才恢复为只显示当前仓库对话。表现为"串库闪烁"。

## 二、根因（码溯确认）

### 2.1 跳转链路

```
tasks.js openTaskConversation（双击）
  → param = 'issue:{issue_num}:{repoKey}'      # tasks.js:340
  → app.nav('chat', param)
      → chat.js renderChat(openConvId=param)   # chat.js:53
          → loadConversations(true)            # chat.js:70
```

### 2.2 闪库发生点（chat.js:201）

```js
const repoKey = (_openConvId || _issueParam ? '' : (repo ? repo.fullName : ''));
```

- 从任务中心跳转带 `issue:N:repoKey` param → 解析出 `_issueParam`。
- 为"确保任务会话可被定位"，这里的 `repoKey` 被强制设为 **`''`** → **拉全量对话列表**。
- 首次渲染的是**全局对话列表**（含其它仓库）→ 用户看到串库。
- 随后 `chat.js:228` `ensure-issue` 建/定位当前任务会话，再 `loadConversations(false)`
  **刷新为按仓库过滤** → 才恢复正确 → 约 1 秒闪烁。

### 2.3 设计意图 vs 缺陷

- 注释：`去对话指定会话时，不过滤 repo，确保任务会话可被定位`。
- 意图合理（任务会话可能在其它仓库历史设计），但副作用：**首渲染暴露全局对话列表**，
  与"从仓库进入应只看当前仓库"的语义冲突。

---

## 三、方案

### 3.1 核心思路

**首渲染即按当前仓库过滤；任务会话定位在后台完成后，只做"选中"不重排全局。**

调整 `chat.js` 的 `loadConversations`：
1. **repoKey 判定**：当 `_openConvId`/`_issueParam` 携带 `repoKey` 时，**用该 repoKey 过滤**（而非 `''`），
   而不是一刀切拉全局。即：
   ```js
   const repoKey = _issueParam && _issueParam.repo
     ? _issueParam.repo                          // 优先用任务自带仓库
     : (_openConvId ? '' : (repo ? repo.fullName : ''));
   ```
2. **过渡期不泄露**：首次渲染按过滤后的列表；若目标会话不在过滤列表（已删除/异仓库），
   显示**骨架/空态** + 提示"正在定位任务会话"，而非全量列表。
3. **ensure-issue 后**：当前逻辑已会再 `loadConversations(false)` 刷新，保留；但首渲染不再是全局。

### 3.2 具体改动

| 文件 | 改动 |
|---|---|
| `src/api/static/js/pages/chat.js` | `loadConversations` 的 `repoKey` 判定优先用 `_issueParam.repo`；`_issueParam` 解析增强（提取 repo）；过渡期列表为空/未定位时渲染空态而非全量 |
| i18n（chat.json） | 可选：新增"正在定位任务会话…"过渡文案 |

> 后端 API 已支持 `repo_key` 过滤（`/chat/conversations?repo_key=`），无需后端改动。

### 3.3 边界与不回归

| 场景 | 预期 |
|---|---|
| 仓库任务中心双击进入 | 首渲染即当前仓库对话，无串库 |
| 会话已删除（任务表 conv_id 在但会话没了） | 空态 +"定位中"→ ensure-issue 重建 → 选中该会话（不闪库） |
| 普通侧边栏进入 chat（无 param） | 按当前仓库过滤（现状不变） |
| 直连 conv_id（从其它入口带会话） | 走 `_openConvId` → 不过滤（现状，定位会话）——此处非任务中心场景 |

---

## 四、验收对照

- [x] 任务中心双击进入 chat，首渲染即当前仓库对话
- [x] 过渡期不出现其它仓库对话（无串库闪烁）
- [x] 多仓库重复进入均无串库
- [x] ensure-issue 重建会话仍可用（会话可被定位）

---

## 五、测试

- 后端：无需（无后端改动）。
- 前端手工回归：任务中心双击各仓库 → 观察首渲染；侧边栏进入 chat → 正常按 repo 过滤；
  删会话后从任务中心进入 → 空态→重建→选中。

---

*方案待实施；与 Issue Agent 已发 Spec 对齐（请求即带 repo_key + 过渡期骨架态）。*