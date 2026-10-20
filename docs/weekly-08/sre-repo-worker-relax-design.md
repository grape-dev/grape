# SRE 引擎放开至仓库级（sre-repo-worker-relax）设计文档

- 日期：2026-10-20
- 状态：已评审（主人逐项拍板 Q1-Q5 + 弹窗退役），待实施
- 前置：取代 `docs/weekly-06/sre-personal-machine-design.md` 的核心约束（该文档保留作历史背景，本文生效后其 §3.x 交互规范全部作废）
- 动机：SRE Agent 此前强制只能使用个人机器（scope=user 且 owner=本人），多人协作仓库下每个协作者都要各自连接个人机器才能让场景里的 SRE 节点跑起来，对协作用户不友好。放开为「个人 + 仓库」双档位，仓库管理员配置一次仓库级 worker 即可全员复用。

## 0. 决策记录（主人拍板）

| 编号 | 问题 | 决策 |
|---|---|---|
| Q1 | SRE 引擎弹窗显示范围 | A：个人 + 仓库 两档，提示文案同步改 |
| Q2 | 自动全选行为 | B：取消自动全选；启用后引擎默认全空，用户必须自己勾；未选择引擎时前端强调「必须选择，无广播」 |
| Q3 | 存量场景迁移 | A：不动存量（绑定的个人机器仍在合法集合内） |
| Q4 | 无机器引导弹窗 | A：个人+仓库都无引擎时不弹引导窗，降级为引擎弹窗内空态文案 |
| Q5 | 空 engine_runtime_ids 语义 | A：保留「空 = 无引擎不派发，不广播」，后端 400 校验保留 |
| 追加 | 引导弹窗去留 | 整个 sre-personal-machine 前端引导体系退役（见 §2.3） |

安全边界（与 A 方案的差异，即为什么不是完全放开）：SRE 是执行部署/运维命令的 agent，global 池任何用户都能贡献机器，广播等于任意机器都可能执行运维命令，责任边界不清。repo 级引擎由仓库管理员（repo owner 本人）配置，责任边界明确，故只放开 repo 档，不放 global、不广播。

## 1. 后端改动

### 1.1 派发层 `src/runtime/engine_select.py`

**`pick_engine` SRE 专属分支（357-370 行）**：从「只允许 user scope」改为「允许 user + repo scope」，选择顺序 user 优先于 repo（个人机器上跑过 SRE 任务、环境已有登录态，优先复用；repo 引擎作为协作兜底）：

```python
# 改造后语义
if _sre_personal_only(agent_type):   # 函数更名 _sre_engine_scopes / 保留名亦行，见 §1.4
    if not engine_ids:
        return None                  # Q5：空 = 无引擎，不回退不广播（不变）
    members = _engines_by_ids(engine_ids, repo, cfg)
    members = _filter_by_tags(members, agent_type)
    members = [m for m in members if m.get("scope") in ("user", "repo")]
    # 先 user 后 repo 两档依次健康选机；都无健康 → None
```

repo 档归属校验沿用 `_member_scope` 现有逻辑（scope=repo 引擎按 owner_id 与当前仓库匹配），无需新写。

**`get_scene_pool_engine_ids` SRE 分支（433-444 行）**：user scope 过滤改为 `scope in ("user","repo")`；「SRE 空 = None」语义保留（434 行不动）。日志文案「过滤掉 N 个非个人级引擎」改为「过滤掉 N 个非个人/仓库级引擎」。

### 1.2 场景校验 `src/api/routes/scenes.py` `_validate_scene_engine_ids`

- 第 4 条校验（1500-1509 行）改为：SRE 节点引擎 scope 必须为 `user` 或 `repo`；`user` 时 owner 必须是当前用户（保留），`repo` 时走下方既有的仓库归属校验（1511-1516 行已覆盖，注意它目前对 `tag=='sre'` 也生效，无需重复）。
- 报错文案：「SRE Agent 只能使用个人级机器」→「SRE Agent 只能使用个人或仓库级机器」；「只能使用当前用户自己的个人机器」保留语义但仅约束 user 档。
- 第 5 条（1476-1479 行）：SRE 启用且引擎为空 → 400 保留，文案改为「SRE Agent 不支持广播模式，请选择个人或仓库级机器（引擎列表不能为空）」。

### 1.3 引擎列表接口 `src/api/routes/dashboard.py`（1194 行附近）

SRE 分支从只查 user 档改为：

```python
engines = (query_engines(repo_key, "user", agent_type="sre", username=_uname)
           + query_engines(repo_key, "repo", agent_type="sre"))
```

（其余 agent 的 global 档依然不对 SRE 开放。组织隔离过滤逻辑不动。）

### 1.4 命名与注释清理

- `_sre_personal_only()` 更名为 `_sre_scoped_engines()`（或 `_sre_no_broadcast()`），docstring 更新为「SRE 只允许 user/repo 两档、空=无引擎不广播」；引用点：engine_select.py 358/434/440 三处。
- 各处引用 `sre-personal-machine-design §3.x` 的注释更新为指向本文档。
- `src/api/routes/runtime.py:201` 附近「agent_tags 为空导致前端漏掉——sre-personal-machine 修复」的注释：逻辑保留（该修复与本约束无关），仅注释引用改为本文档。

## 2. 前端改动（`src/api/static/js/pages/dashboard.js`、`agents.js`）

### 2.1 引擎弹窗（Q1=A）

- SRE 节点引擎弹窗分组显示 `user`（个人）+ `repo`（仓库）两组（`_SCOPE_ORDER2` 中 global/local 组对 SRE 隐藏）。
- 空态文案（1449 行）由「SRE Agent 只能使用个人级机器，请先连接个人机器」改为：

  「暂无可用的个人或仓库级机器。SRE Agent 不支持广播模式，请连接个人机器，或在仓库设置中配置仓库级机器。」

- `agents.js:123-125` SRE 只显示 `scope==='user'` 的过滤改为 `scope==='user' || scope==='repo'`。
- **个人档组标注（所有 agent 通用）**：引擎弹窗分组标题「个人 (N)」旁加小字注释「绑定个人机器后，其他用户将无法使用该场景」——提示个人级引擎的协作排他性，引导多人协作场景优先选仓库级。样式：分组标题行内小号灰字（11px → 10px，颜色 #86868b），不影响布局。i18n key：`engine_personal_scope_hint`（zh/en 双份）。

### 2.2 取消自动全选 + 必选强调（Q2=B）

- `toggleAgentInScene('sre')` 的 SRE 拦截分支删除：启用 SRE 后 `engine_runtime_ids` 保持空，由用户点「选择引擎」自行勾选——与其他 agent 的交互差异只剩「不能留空保存」。
- SRE 卡片（1358-1365 行区域）增加必选强调：SRE 启用且 `engine_runtime_ids` 为空时，引擎徽章位置显示红色提示徽章「未选择引擎 · 不支持广播」（样式沿用现有徽章体系，红色 #ff3b30 边框药丸）；「选择引擎」按钮保留。
- 保存前端拦截：SRE 启用且引擎为空 → toast「SRE Agent 不支持广播模式，请先选择引擎」并不发保存请求（后端 400 兜底已在 §1.2）。创建/编辑两条保存路径都要拦。

### 2.3 引导弹窗体系退役

以下函数与挂载点整体删除（调用点同步清理，`node --check` + 浏览器验收）：

- `_sreWakeUp`、`_sreShowNoMachineModal`、`_sreCloseNoMachineModal`、`_sreCloseAkSkModal`、`_sreWatchDeviceModal`、`_sreRetryWake`、`_sreCheckOffline`、`_sreEditCtx` 及两处 `_sreEnterEditScan` 自执行扫描块（编辑模式 1222-1232 行、创建模式 1836+ 行）。
- 「重新检测个人机器」按钮（1358 行 `_sreOffline` 分支）删除；无引擎时统一显示「选择引擎」按钮 → 点开见空态文案（Q4=A 的降级表达）。
- 保留：`openSreAddDeviceModal`（runtimes.js，机器管理页的独立入口，与此链路解耦）。

### 2.4 i18n

`locales/zh/dashboard.json` + `en/dashboard.json`：

- `sre_engine_personal_only` → 改文案「SRE Agent 只能使用个人或仓库级机器」（en 同步）。
- `sre_no_machine_desc` / `sre_no_machine_offline_desc` / `sre_no_machine_title` / `sre_btn_huawei` / `sre_btn_host` / `sre_wake_selected` / `sre_retry_wake` / `sre_save_to_apply`：随弹窗体系删除的 key 移除；新增 `sre_engine_required`（「未选择引擎 · 不支持广播」）、`sre_engine_empty_hint`（§2.1 空态文案）、`engine_personal_scope_hint`（§2.1 个人档组注释，所有 agent 通用）。
- 注意 I18N 陷阱：`t(key)` 找不到时返回 key 本身（truthy），`||` 兜底不生效——新文案必须中英两份都落 key。

### 2.5 版本号

`index.html` 静态资源版本号 bump（dashboard.js），避免缓存。

## 3. 多情景前端演进叙事（验收对照）

### 情景一：新用户创建场景（有仓库级机器，无个人机器）

小王是仓库协作者，没连过任何个人机器，仓库管理员已配置仓库级 worker 并打了 sre 标签。

1. 小王打开 grape → 仓库首页 → 场景区 → 点「创建场景」。
2. 流程卡片里 SRE 默认启用（create-mode default-on 不变）。**改造前**：点开编辑就弹「无个人机器」引导窗，不托管就走不下去；**改造后**：无任何弹窗，SRE 卡片引擎徽章位置显示红色「未选择引擎 · 不支持广播」提示。
3. 小王点 SRE 卡片的「选择引擎」→ 引擎弹窗打开：显示「个人 (0)」不出现（空组隐藏）、「仓库 (2)」一组，两台仓库级 worker 在线，各带 sre 标签徽章 + 仓库 scope 徽章。
4. 小王勾选其中一台 → 关闭弹窗 → SRE 卡片徽章变为该引擎名 + 在线绿点。
5. 点保存 → 前端校验通过（引擎非空）→ 落库成功，返回场景列表。

### 情景二：老用户编辑存量场景（原本绑了个人机器）

老张之前创建的场景 SRE 节点绑了自己的个人机器（存量，Q3=A 不迁移）。

1. 老张打开该场景详情 → SRE 节点徽章显示原个人机器名（行为不变，**改造前也不会变**）。
2. 点「编辑」→ SRE 卡片正常显示已绑引擎，无红色提示。
3. 老张点「选择引擎」想换机器 → 弹窗里现在能看到「个人 (1)」+「仓库 (3)」两组（改造前只看得到个人组）。他反选自己那台、勾选一台仓库机器。
4. 保存 → 成功。下次 SRE 任务派发时 user 档已无匹配 → 落到 repo 档执行（§1.1 顺序）。

### 情景三：用户启用 SRE 但不选引擎就保存

小李创建场景时直接把 SRE 留着没管引擎。

1. SRE 卡片显示红色「未选择引擎 · 不支持广播」提示徽章。
2. 小李点保存 → **前端拦截**：toast「SRE Agent 不支持广播模式，请先选择引擎」，不发请求。
3. 小李忽略 toast 再点一次保存（假设绕过）→ 后端 400 兜底：「SRE Agent 不支持广播模式，请选择个人或仓库级机器」（§1.2）。
4. 小李把 SRE 开关关掉再保存 → 通过（SRE 禁用时无引擎要求）。

### 情景四：个人 + 仓库都没有任何机器

小赵在全新仓库创建场景，仓库没配 worker，自己也没连过机器。

1. SRE 卡片红色「未选择引擎 · 不支持广播」提示。
2. 点「选择引擎」→ 弹窗主体为空态文案：「暂无可用的个人或仓库级机器。SRE Agent 不支持广播模式，请连接个人机器，或在仓库设置中配置仓库级机器。」（**改造前**这里会弹遮罩引导窗推华为云/托管；改造后降级为弹窗内文案，无按钮、无遮罩）。
3. 小赵三个选择：自己去机器管理页连个人机器、自己去仓库设置配置仓库级机器、或找管理员代配。配好后回到弹窗重开即可看到引擎。
4. 或者小赵直接关掉 SRE 开关，场景照常保存，其余 agent 不受影响。

### 情景五：SRE 任务派发时引擎全灭

场景绑定的仓库机器集体离线，事件触发 SRE 任务。

1. 前端场景详情 SRE 徽章显示引擎离线（灰色/失联色，沿用现有状态色，非本次新增）。
2. 派发层 `pick_engine`：user 档空 → repo 档全不健康 → 返回 None → 任务不入队，事件报「engine_unavailable」类错误（语义与现状一致，仅档位集合扩大）。
3. 不存在「回退 global 广播」路径（Q5 红线）。

## 4. 测试计划

- 后端单测：`_validate_scene_engine_ids` 四分支（user 合法 / repo 合法 / global 拒绝 400 / 空列表 400）；`pick_engine` SRE user 优先、repo 兜底、全灭 None；`get_scene_pool_engine_ids` SRE 过滤含 repo 引擎、空 None 保留。
- 前端：`node --check` dashboard.js/agents.js；浏览器验收五个情景逐一对照 §3。
- 回归：agents.js 引擎下拉、场景保存/编辑主路径、i18n key 加载。
- 存量兼容：升级前保存的场景（绑个人机器）加载、编辑、保存全通过（Q3）。

## 5. 实施顺序

1. 后端三文件（engine_select / scenes / dashboard.py）+ 更名与注释清理
2. i18n 两份 locale
3. 前端弹窗体系删除 + 必选强调 + 保存拦截
4. 版本号 bump → 浏览器验收 → 相关测试
5. 分支：`feat/sre-repo-worker-relax`，基于 weekly-clean-mys 最新头；提交全英文，push/MR 待主人明示
