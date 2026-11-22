# Issue #3646 方案设计

## 1. 新增纯函数模块：`src/domains/runtime/pipeline/owner_display.py`

责任人判定原是 `routes/pipeline.py` 里的内联逻辑（只有 `bot_usernames` 名单一个
判据），无法复用、也不便单测。抽成**无 IO 的纯函数**，展示层只做取值拼装：

```python
INTERNAL_LOGIN_PREFIXES = ("gcw_",)          # GitCode OAuth 自动生成登录名（平台自动化/Agent 身份）
_BOT_SUFFIX_RE   = r"(?:^|[-_])bot$"         # bot/*_bot/*-bot（与看板 is_bot_name 同规则）
_AGENT_DISPLAY_RE = r"(?:^|[\s_-])agent$"    # Agent 显示名（"Coding Agent"）
_RUNTIME_ID_RE   = r"(?:^|[-_])worker(?:[-_]|$)"   # worker/runtime 身份形态 id

def is_internal_agent_account(name, bot_usernames=()) -> bool
def resolve_human_owner(assignee_candidates, actor_candidates, bot_usernames=()) -> str
def resolve_current_owner(assignee_candidates, actor_candidates, bot_usernames=(), fallback="") -> str
```

- 空串**不算**内部账号（语义是「没有责任人」），由调用方按候选顺序跳过；
- `_BOT_SUFFIX_RE` 只认 `(^|[-_])bot$`，不用裸 `endswith('bot')`（会误杀
  `abbot` / `robot` / `talbot` 这类真人账号，看板既有口径）；
- 判定只看名字形态与传入的 bot 名单，不查库 → 单测无需 DB。

## 2. `routes/pipeline.py` 责任人解析重写（原 361-397 行）

候选按优先级拼装后交给纯函数，只有命中**人类**才过飞书昵称解析：

```
assignee_candidates = [issue_state.assignee, 事件 payload assignee]
actor_candidates    = [各阶段最新事件 actor（阶段倒序）, 最早一条事件的人类操作者]
bot_usernames       = 入参 bot_usernames（batch 查询结果，空时懒加载全局名单，仅一次）
human = resolve_human_owner(...)
current_owner = _resolve_feishu_name_cached(human) if human else get_agent_display_name(该阶段 agent)
```

- 阶段倒序扫描**不因最近 actor 是内部账号而提前结束**（原实现直接展示该 actor），
  保证 #3644 能穿过 qa 的 `gcw_0FAKEACCT` 落到 vod/issue 的 `songspace`；
- 末位补「最早一条事件的操作者」：同阶段后续事件被内部账号改写时仍能回到建单人；
- 全内部账号 → Agent 显示名（与旧口径一致），不输出原始账号；
- 飞书昵称解析只对人类候选执行（内部账号不再进该解析链，省查询）。

## 3. `src/api/session_store.py`：assignee 不再被推进路径抹掉 + 空值回填

- `set_issue_stage()`：`assignee` 仅在**显式非空**时写入卡片行。
  现状 `"assignee": assignee or ""` 会让 A2A 推进（`set_issue_stage(rk, iid, next_role, next_role)`）
  一次就把真实责任人覆盖成空串——这是 #3644 责任人只能靠 actor 推断的直接原因。
- 新增 `sync_issue_assignee(repo_key, issue_id, assignee)`：只写 `assignee` 单字段
  （不动 stage/owner_agent/updated_at，也不动 user_id 归属），且**仅当现有值为空**时回填。

## 4. `src/domains/runtime/pipeline/event_pipeline.py`：指派/更新事件回填 assignee

在 3.2「任务中心全覆盖」块内（该块已算出 payload/VCS 的 `_asg3`），
`ensure_issue_task()` 之后调用 `sync_issue_assignee(repo_key, _iid3, _asg3)`：
`issue_updated` / `issue_comment` / `issue_labeled` 等指派/更新事件即可把
VCS 当前 assignee 补进 `issue_state.assignee`（原仅 `issue_opened` 初始化），
且人类责任人永不被覆盖。异常只告警，不影响事件主流程。

## 5. 测试 seam 与用例

- 主 seam 沿用既有公开行为：`_get_issue_pipeline(repo_key, issue_id, events, stage_map=..., bot_usernames=...)`
  → `current_owner`（展示层唯一出口，不测内部细节）；`stage_map` 直接注入 assignee，
  避免依赖共享 `issue` 表。
- 纯函数 seam：`owner_display` 的 `is_internal_agent_account` / `resolve_current_owner`。
- 持久化 seam：`session_store.set_issue_stage` / `sync_issue_assignee`（测试库）。
- 新增文件 `tests/runtime/test_3646_current_owner.py`，覆盖：人类 assignee 优先、
  bot assignee 回退人类 actor、`gcw_*` actor 被穿透（#3644 场景）、全内部账号回退
  Agent 显示名、纯函数判定矩阵、推进不清空 assignee、空值回填。
- 顺带修复 `tests/runtime/test_pipeline.py` 里 4 个**存量红**用例的 mock 签名
  （`side_effect=lambda u: u` 与 `_resolve_feishu_name(name, repo_key)` 两参调用不符，
  baseline 已红），使 assignee 相关契约用例真正可用。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 内部账号识别遗漏新命名 | 中 | 四类判据（bot 名单 / bot 形态 / `gcw_*` 前缀 / Agent 运行时形态）集中在一个函数，扩展只需加一条 |
| 有人类账号恰好匹配形态规则 | 低 | 形态规则取 `(^|[-_])bot$`、`gcw_` 前缀等强特征；命中即展示 Agent 显示名而非报错 |
| assignee 回填增加写库 | 低 | 仅空值回填，单字段 UPDATE，不碰归属字段 |
| 历史脏值（已落库的 `gcw_*`） | 低 | 解析侧过滤，阅读时不展示；不做批量订正（Out of Scope） |