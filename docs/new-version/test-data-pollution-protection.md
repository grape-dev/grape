# 测试数据污染防护与 Agent 路由修复

> 关联问题：PR #26 创建后未触发代码检视；Jason7602 仓库列表被清空
> 日期：2026-08-02

## 一、问题背景

同日出现两个关联故障，均指向同一根因：**集成测试直接在服务器生产库上执行破坏性操作，污染了生产数据**。

### 1.1 故障现象

| 现象 | 影响 |
|------|------|
| Jason7602 仓库列表突然清空，退出到加入页面 | `managed_repos` 被测试 `DELETE` 清空 |
| 加入/托管仓库提示权限不足 | GitCode token 无 hooks 权限（独立问题）+ managed_repos 缺失导致 `_filter_repos` 判定异常 |
| PR #26 创建后无代码检视 | `agent_type` 订阅配置被测试覆盖成简版，`pull_request_opened` 路由失败 |
| `chat_bot_test` bot 反复重试 WS 连接 | `global_im_bot_config` 残留测试 bot |

### 1.2 污染源

| 测试文件 | 破坏性操作 | 目标 |
|----------|-----------|------|
| `tests/ui_scenario_test.py` | SSH 直连服务器执行 `DELETE FROM managed_repos` | 服务器生产库 |
| `tests/test_api_routes.py` | `INSERT OR REPLACE INTO agent_type`（简版订阅） | 本地库（但可被误在服务器执行） |
| `tests/ui_test_playwright.py` | `DELETE FROM managed_repos / events` | 本地库 |

**关键问题**：测试代码未区分测试库与生产库，且 `TestClient(app)` 直接使用 `src.session_manager` 的 `db`（即 `data/sessions.db`），一旦在服务器运行即污染生产数据。

## 二、PR #26 流程审计

### 2.1 事件链（来自日志）

```
seq=175-179  issue_comment → issue agent（用户确认评论，多次触发）
seq=180      a2a_event → coding agent（skill=implementation）→ 创建 PR #26
             ├── 写需求文档 spec-44.md / 方案设计 plan.md / todo.md
             ├── 写测试用例（test_agent_executor.py / test_vod_handler_async.py）
             ├── 自审查（5 轴）后创建 PR
             └── Issue 发评论告知
seq=181      issue_comment → issue agent（bot 评论被回发）
```

### 2.2 Coding Agent 符合性

| 规范（implementation skill） | 符合性 |
|------------------------------|--------|
| 先输出需求文档和方案设计再编码 | ✅ 写了 spec/plan/todo |
| 开发任务必须包含测试用例 | ✅ 写了测试 |
| 创建 PR 前必须自审查 | ✅ 5 轴自审查 |
| PR 创建后 Issue 评论告知 | ✅ |
| 每个提交一个原子变更 | ✅ 分步 commit |
| **只通过 GitCode API 获取信息** | ❌ 大量读取本地 `/root/devops-bot/src/...` |
| **直接合并 PR** | ✅ 未合并，仅创建等待审批 |

**结论**：Coding Agent 大体符合流程，但违反"禁止探索 Bot 内部"（读取本地源码）。这是 prompt/skill 约束执行问题。

### 2.3 QA 未触发根因

```
PR #26 创建 → GitCode webhook 推送 pull_request_opened
  → run_pipeline → get_domain_agent("pull_request_opened")
  → 查询 agent_type.subscribe_events
  → 服务器 agent_type.coding.subscribe_events = []  ← 被测试覆盖成简版！
  → "Route: no match for type=pull_request_opened"
  → 路由失败，seq=0，PR 检视未触发
```

**设计意图**：PR 检视由 `pull_request_opened → coding agent（pr skill）` 承担，pr skill 内调用 `code-review-and-quality`（5 轴审查）标准执行代码审查。QA Agent 的 `code-review-and-quality` 技能是 pr skill 审查的参考标准。

**根因**：`agent_type` 表订阅配置退化（coding 应为 `["pull_request_opened","pull_request_updated","note_hook_pr"]`，被测试覆盖为 `[]`）。

## 三、修复措施（已完成）

1. **恢复 managed_repos**：从 `docs/new-version/database-tables-data-dump.md` 恢复 Jason7602 的 4 条托管记录（含 webhook_id）
2. **修复 agent_type 订阅**：重启服务触发 `_seed_from_json()`，用 `configs/agent-description.json` 正确覆盖（coding/issue/sre 订阅恢复）
3. **清理测试残留**：删除 `global_im_bot_config` 中的测试 bot（`test`/`chat_bot`）
4. **验证路由**：`pull_request_opened → coding`、`issue_comment → issue`、`pull_request_merged → sre` 恢复

## 四、防护方案（待实施）

### 4.1 目标

防止集成测试再次污染服务器生产库。

### 4.2 方案：测试环境隔离

**4.2.1 统一 DB 路径开关**

在 `src/config.py` 增加环境变量 `DEVOPS_DB_PATH`：

```python
DB_PATH = os.environ.get("DEVOPS_DB_PATH", os.path.join(PROJECT_ROOT, "data/sessions.db"))
```

`src/session_manager.py` 改用 `DB_PATH`：

```python
_db_path = DB_PATH
```

**4.2.2 测试 fixture 指定测试库**

`tests/conftest.py` 在 session 开始时：

```python
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data/test_sessions.db")
```

所有 `TestClient` 测试、UI 测试通过环境变量指向独立测试库，不再触碰生产库。

**4.2.3 `ui_scenario_test.py` 强制安全**

- `_setup()` 中的 SSH `DELETE FROM managed_repos` 前，先备份到 `managed_repos_backup`（已实施），测试结束后自动恢复
- 增加断言：备份行数 > 0 才允许清空（防止清空空表后误判）
- 环境变量 `DEVOPS_UI_TEST=1` 才允许对远端执行破坏性操作

**4.2.4 `test_api_routes.py` setup_module 修正**

`INSERT OR REPLACE INTO agent_type` 简版订阅改为完整配置（与 `agent-description.json` 一致），避免污染语义。

### 4.3 回归验证

- `pull_request_opened` 事件能路由到 coding，触发 pr skill 执行代码审查
- 生产库 agent_type 订阅保持完整
- 测试跑完后生产库 managed_repos / agent_type / global_im_bot_config 无变化

## 五、遗留问题（需人工处理）

| 问题 | 说明 | 处理 |
|------|------|------|
| Jason7602 GitCode token 无 hooks 权限 | `GET/POST /repos/*/hooks` 返回 403 | 在 GitCode 后台给 token 加 hooks 权限 |
| Coding Agent 探索 Bot 内部 | 读取本地 `/root/devops-bot/src/` | prompt/skill 约束需加强，或审查 agent 日志 |
| `chat_bot` secret 恢复 | 已恢复 2 bots | 保持 |
