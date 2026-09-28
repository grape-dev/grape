# Autopilot 定时任务重构设计文档

> 版本: v3.0 | 日期: 2026-09-22 | 作者: Neko-chan

## 一、现有运行逻辑

### 1.1 完整调用链

```
scheduler_loop()                    每60秒扫描到期任务
  → _is_cron_due()                  判断cron是否到期
  → _execute_task()                 执行任务(含重试)
    → _create_issue_and_trigger()   创建Issue触发流水线
      → create_task_via_issue()     在GitCode创建Issue + 同步调run_pipeline
        → run_pipeline()            走完整5-agent流水线
          vod → issue → coding → qa → sre → done
```

### 1.2 函数路径说明

| 函数 | 文件:行 | 职责 |
|------|---------|------|
| scheduler_loop | scheduler.py:264 | asyncio死循环, 每60s扫描 enabled=1 的任务 |
| _is_cron_due | scheduler.py:48 | 用croniter判断cron表达式是否在当前60s窗口到期 |
| _execute_task | scheduler.py:226 | 内存互斥锁 + 创建执行记录 + 指数退避重试(10s→20s→40s, 上限300s) |
| _create_issue_and_trigger | scheduler.py:169 | 拼标题(任务名+时间戳), 构造internal_user身份, 直接调create_task_via_issue |
| create_task_via_issue | issues_center.py:791 | 幂等检查 → GitCode API创建Issue → 写场景 → 建档 → 同步触发run_pipeline |
| run_pipeline | event_pipeline.py:947 | 归一化 → 自循环检测 → L1过滤 → 阶段矩阵路由 → 入队 → _execute_agent |

### 1.3 DB 表结构

**autopilot_tasks** (db_setup.py:725)
- name, repo_key, scene_id(将移除), description(将移除), cron
- agent_chain(废弃), issue_number(废弃), max_retry, enabled
- last_run_at, next_run_at, created_at, updated_at, created_by

**autopilot_executions** (db_setup.py:745)
- task_id, status, retry_count, agent_chain_step(将移除)
- result, error, a2a_seq, issue_number(将移除)
- started_at, completed_at, created_at

### 1.4 前端现状

- 独立侧边栏导航项: `⏰ 自动化` (index.html:61)
- 列表页: 表格展示, 含筛选/搜索/排序/批量操作
- 详情页: 需求信息 + 时间表 + 执行历史
- 创建/编辑弹窗: 900x680px, 左右双栏(内容+配置), 含结构化cron编辑器 — **全面保留**
- 6个预置模板
- 文件: src/api/static/js/pages/autopilot.js (1196行)

### 1.5 现有问题

1. **资源浪费**: 定时任务无脑走完整5-agent流水线, vod/issue空转
2. **不稳定性**: 5环A2A串联链条可靠性约0.59
3. **安全漏洞**: autopilot所有API只做get_current_user, 未做check_repo_access
4. **废弃字段**: agent_chain/issue_number已废弃但仍占用表空间
5. **skill不匹配**: 现有skill(issue-advance/pr/cloud-deploy)为流水线场景设计, 与定时巡检上下文不匹配

## 二、新的运行逻辑

### 2.1 核心思路

定时任务是一个独立于现有pipeline的系统，拥有自己的调度、数据存储、prompt构建和展示。

- 调度器到期后直接执行单个agent（不走pipeline、不创建Issue、不走事件系统）
- 不使用现有skill（定时任务上下文与现有skill不匹配，详见§九分析）
- prompt在新的 _build_autopilot_prompt 中直接构建（不走 build_devops_agent_prompt）
- 底层引擎调用链路复用（token注入、workdir物化、stream_devops_agent）
- 执行结果独立存储在 autopilot_executions，不写issue表、不写events表
- 前端独立展示（列表 + 下拉历史 + 问题派发按钮，不碰pipeline.js/issues.js）

### 2.2 新调用链

```
scheduler_loop()                         每60秒扫描到期任务 (保留)
  → _is_cron_due()                       判断cron是否到期 (不变)
  → _execute_task()                      执行任务(含重试) (执行部分重写)
    → _execute_autopilot_agent()         [新] 定时任务专用执行入口
      → _build_autopilot_prompt()        [新] 构建prompt(巡检指令+findings格式)
      → _prepare_agent_workspace()       [新] 从_execute_agent_inner拆出的公共部分
        → get_agent_workdir              获取工作目录 (复用)
        → bot token获取                  按 repo_key 分流 (复用)
        → _materialize_direct_workdir    物化project-info到workdir (复用)
        → 环境变量注入                   (复用)
      → stream_devops_agent              调引擎执行 (复用)
      → parse_findings()                 [新] 解析agent产出中的问题列表
      → _update_execution()             记录执行结果+问题列表 (独立)
```

### 2.3 与现有链路的关系

定时任务附加了一条新链路。对现有流程的影响仅限一处：

**_execute_agent_inner 的拆分**: 从中提取公共的引擎调用准备逻辑（token获取、workdir物化、环境注入）为独立函数 _prepare_agent_workspace。现有6个调用入口（webhook/A2A/重派/PR订阅/direct）改为调用拆出的公共函数，逻辑不变。

不碰的现有代码:
- run_pipeline / create_task_via_issue / event_store
- pipeline.js / issues.js / issue表 / events表
- webhook.py / event_bus.py / pipeline_executor.py / pipeline_config.py
- build_devops_agent_prompt / prompt_builder.py

### 2.4 prompt 构建

不走 build_devops_agent_prompt（该函数依赖流水线上下文：issue_state/scene/parent_issue/A2A context）。

新的 _build_autopilot_prompt 直接构建，支持两种模式:

**无 skill（默认）**: prompt 里注入巡检指令 + findings 输出格式模板，agent 按指令执行。

**有 skill**: prompt 里额外注入 skill 文件路径（与 build_devops_agent_prompt 的 `{{SKILL_DIR}}/{skill}/SKILL.md` 方式一致），agent 先读 SKILL.md 再执行。_materialize_direct_workdir 在 skill_name 非空时物化 skill 文件到 workdir。

```python
def _build_autopilot_prompt(task: dict, repo_key: str, repository: str) -> str:
    """构建定时任务的prompt。不依赖issue/scene/pipeline上下文。"""
    parts = []
    parts.append(f"## 定时巡检任务\n")
    parts.append(f"任务名称: {task['name']}\n")
    parts.append(f"仓库: {repository}\n")
    parts.append(f"巡检指令:\n{task['instruction']}\n")

    # skill 注入（可选）
    if task.get("skill_name"):
        parts.append(f"\n## Skill: {task['skill_name']}\n")
        parts.append(f"Skill 文件：{{{{SKILL_DIR}}}}/{task['skill_name']}/SKILL.md\n")
        parts.append(f"**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令。\n")

    parts.append(f"\n请执行以上巡检指令，分析仓库代码/依赖/文档/配置等。")
    parts.append(f"\n## 输出格式要求\n")
    parts.append(_FINDINGS_FORMAT_TEMPLATE)  # autopilot-findings 标记块模板
    parts.append(f"\n## 项目知识\n")
    parts.append(f"项目知识文档位于工作目录中，存在则用 read tool 读取。")
    parts.append(f"\n执行以上指令，不要操作 GitCode Issue（不需要回帖/打标签/关Issue），结果直接输出。")
    return "\n".join(parts)
```

保留 skill 加载接口的设计考虑：
- 当前不强制使用 skill，但留好接口供后续适配
- 如果发现某些巡检场景需要 skill 的流程约束（如 pr skill 做 PR 审查），可以填 skill_name 激活
- skill_name 为空时 _materialize_direct_workdir 跳过 skill 物化，只物化 project-info

### 2.5 agent_type 的作用

定时任务不使用 skill，但 agent_type 仍然需要，用于:
- pick_engine: 按 agent_type 选引擎
- get_or_create_agent_session: 按 (repo_key, agent_type) 获取会话
- get_agent_workdir: 按 (repo_key, agent_type, issue_id) 生成工作目录

可选的 agent_type: issue / qa / sre（排除 vod 和 coding，定时任务不需要需求澄清和循环编码）

### 2.6 引擎选择与执行

复用现有 pick_engine 和引擎执行链路:

```python
from src.runtime.engine_select import pick_engine
engine_id = pick_engine(repo_key, task["agent_type"], fallback_runtime_id=0)

# direct模式
from src.api.event_pipeline import start_direct_agent, _create_direct_task
task_id = _create_direct_task(repo_key, seq=0, agent_type=task["agent_type"], ...)
start_direct_agent(_execute_autopilot_agent, (repo_key, task, ...))

# 广播模式
# 走 _enqueue_pipeline_task（但 autopilot 不走事件系统，需要确认广播模式的适配）
```

注意: _create_direct_task 依赖 event_seq 参数。定时任务不创建事件，seq 传 0 或用 autopilot_executions.id 替代。需确认 _create_direct_task 对 seq=0 的处理。

### 2.7 问题派发

agent 执行完成后，parse_findings 从产出中解析问题列表。

每个问题可派发为真实 Issue 走完整流水线:
- 调 create_task_via_issue 创建 Issue → 自动触发 run_pipeline
- 复用现有的完整流水线（vod→issue→coding→qa→sre）
- 派发是用户手动点击按钮触发，不是自动执行

## 三、预计改动范围

### 3.1 DB 表结构改动

**autopilot_tasks** — 移除字段 + 新增字段:

移除:
- scene_id, description, agent_chain, issue_number

新增:
- agent_type TEXT NOT NULL DEFAULT 'issue' (issue/qa/sre 三选一)
- instruction TEXT NOT NULL DEFAULT '' (巡检指令，替代原description)
- skill_name TEXT NOT NULL DEFAULT '' (可选，空=不用skill直接走prompt指令)

保留不变:
- id, name, repo_key, cron, max_retry, enabled
- last_run_at, next_run_at, created_at, updated_at, created_by

**autopilot_executions** — 移除字段 + 新增字段:

移除:
- agent_chain_step, issue_number

新增:
- agent_type TEXT DEFAULT '' (执行快照)
- findings_json MEDIUMTEXT DEFAULT '' (问题列表JSON)

保留不变:
- id, task_id, status, retry_count, result, error, a2a_seq
- started_at, completed_at, created_at

**数据迁移**: 直接清空两张表，新结构从头开始。不碰 issue 表、不碰 events 表。

### 3.2 后端改动

| 文件 | 改动类型 | 改动内容 |
|------|----------|----------|
| src/runtime/scheduler.py | 重写 | _create_issue_and_trigger → _execute_autopilot_agent; _execute_task 执行部分重写 |
| src/runtime/scheduler.py | 新增 | _build_autopilot_prompt / _execute_autopilot_agent / parse_findings |
| src/api/routes/autopilot.py | 修改 | CreateTaskBody/UpdateTaskBody 字段调整; 所有接口加 check_repo_access; _row_to_task 字段调整; 新增 GET /executions/{id}/findings; 新增 POST /executions/{id}/dispatch |
| src/api/db_setup.py | 修改 | autopilot_tasks DDL 改字段; autopilot_executions DDL 改字段; 迁移逻辑 |
| src/api/event_pipeline.py | 拆分 | 从 _execute_agent_inner 提取 _prepare_agent_workspace 公共函数; 现有调用入口改为调用拆出的函数 |

### 3.3 前端改动

| 文件 | 改动内容 |
|------|----------|
| src/api/static/js/pages/autopilot.js | 创建/编辑弹窗: 保留现有弹窗整体设计(900x680双栏+cron编辑器), 左栏"场景选择器"改为"agent选择器(issue/qa/sre)+skill选择器(可选)+指令文本框", 右栏时间表+执行设置不变; 列表页: 点击任务→下拉展开历史执行记录; 执行记录: 显示问题列表+派发按钮; 模板更新 |
| src/api/static/index.html | 侧边栏入口保留 |
| src/api/static/js/app.js | 不变 |

### 3.4 不碰的文件

- event_pipeline.py 的 run_pipeline / _enqueue_pipeline_task（拆分只动 _execute_agent_inner）
- pipeline_executor.py / pipeline_config.py / engine_select.py / event_bus.py
- webhook.py / issues_center.py（create_task_via_issue 被问题派发按钮复用，本身不改）
- pipeline.js / issues.js（定时任务不出现在流水线和需求中心）
- prompt_builder.py（不走 build_devops_agent_prompt）
- issue表 / events表（不写入）

## 四、影响评估

### 4.1 对现有流水线的影响

无影响。定时任务不调 run_pipeline、不创建 Issue、不写 events 表。

### 4.2 对 _execute_agent_inner 拆分的影响

拆分提取公共函数 _prepare_agent_workspace（token获取、workdir物化、环境注入）。现有调用入口改为调用拆出的函数。

风险: 拆分时如果逻辑迁移不完整，可能导致现有流程的 agent 执行异常。
缓解: 拆分后跑一次完整的 webhook → run_pipeline → _execute_agent 链路验证；git commit + tag 方便回退。

### 4.3 对引擎资源的影响

正向: 从每次触发占5个agent降到1个，引擎资源占用降低80%。

### 4.4 对安全的影响

所有 autopilot API 加 check_repo_access。

### 4.5 对现有数据的影响

autopilot_tasks 和 autopilot_executions 清空重建。issue表/events表不动。

### 4.6 定时任务与现有系统的耦合边界

| 层 | 关系 |
|----|------|
| 调度器 | 独立（scheduler.py 自有逻辑，cron 调度不变） |
| 数据存储 | 独立（autopilot_tasks / autopilot_executions，不写 issue/events） |
| prompt 构建 | 独立（_build_autopilot_prompt，不走 build_devops_agent_prompt） |
| 前端展示 | 独立（autopilot.js，不碰 pipeline.js / issues.js） |
| API | 独立（autopilot 路由，不依赖 pipeline/issues API） |
| 引擎选择 | 复用（pick_engine） |
| 工作目录 | 复用（get_agent_workdir / _materialize_direct_workdir） |
| token 获取 | 复用（按 repo_key 分流） |
| 引擎调用 | 复用（stream_devops_agent） |

结论: 附加一条新链路，不影响现有流程的运行逻辑。唯一需要谨慎的是 _execute_agent_inner 的拆分。

## 五、问题列表 JSON 结构

### 5.1 设计原则

- agent 产出中包含结构化 JSON，用固定标记包裹
- 每个问题直接映射到一个"派发任务"按钮
- 解析失败时降级为"未发现问题"

### 5.2 JSON 结构

agent 产出中需要包含:

```
<!-- autopilot-findings
{
  "summary": "本次巡检发现 3 个问题",
  "findings": [
    {
      "id": "f1",
      "title": "依赖 lodash@4.17.20 存在原型链污染漏洞",
      "severity": "high",
      "category": "security",
      "description": "lodash 4.17.20 存在 CVE-2020-8203, 建议升级到 4.17.21+",
      "suggested_assignee": "coding",
      "suggested_skill": "implementation"
    }
  ]
}
-->
```

### 5.3 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| summary | string | 是 | 巡检总结 |
| findings | array | 是 | 问题列表, 空数组=未发现问题 |
| findings[].id | string | 是 | 问题唯一标识(同次执行内) |
| findings[].title | string | 是 | 问题标题(创建Issue的标题) |
| findings[].severity | string | 是 | high/medium/low |
| findings[].category | string | 否 | security/quality/documentation/performance/other |
| findings[].description | string | 是 | 问题描述(创建Issue的body) |
| findings[].suggested_assignee | string | 否 | 建议派发agent(默认coding) |
| findings[].suggested_skill | string | 否 | 建议skill(默认implementation) |

### 5.4 解析逻辑

```python
import re, json

_FINDINGS_PATTERN = re.compile(
    r'<!--\s*autopilot-findings\s*\n(.*?)\n-->',
    re.DOTALL
)

def parse_findings(agent_output: str) -> dict:
    """从 agent 产出中解析问题列表。"""
    match = _FINDINGS_PATTERN.search(agent_output or "")
    if not match:
        return {"summary": "", "findings": [], "parse_error": "no findings block"}
    try:
        data = json.loads(match.group(1))
        findings = data.get("findings", [])
        valid = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            if not f.get("id") or not f.get("title") or not f.get("description"):
                continue
            f.setdefault("severity", "medium")
            f.setdefault("suggested_assignee", "coding")
            f.setdefault("suggested_skill", "implementation")
            valid.append(f)
        return {"summary": data.get("summary", ""), "findings": valid, "parse_error": ""}
    except json.JSONDecodeError as e:
        return {"summary": "", "findings": [], "parse_error": str(e)}
```

### 5.5 派发流程

点击派发按钮 → 调 create_task_via_issue 创建真实 Issue → 自动触发完整流水线。派发成功后 findings_json 中对应 finding 记录 dispatched_issue_number。

## 六、_execute_agent_inner 拆分方案

### 6.1 设计决策：拆分提取公共函数（不是两份代码并存）

不改 _execute_agent_inner 的逻辑，只从中提取公共的引擎调用准备逻辑为独立函数 _prepare_agent_workspace。_execute_agent_inner 和 _execute_autopilot_agent 各自调用这个公共函数。

不采用"两份相同代码并存"的方案，因为 token获取/workdir物化/环境注入 这些逻辑几乎完全相同，将来变更时必须同步改两处，漏一处就是bug。

### 6.2 _execute_agent_inner 现状

(event_pipeline.py:2345) 做了6件事:
1. 获取引擎实例名
2. 更新 task status → running
3. 获取 workdir / issue_session / bot_token / agent_display_name
4. build_devops_agent_prompt + _materialize_direct_workdir
5. 注入环境变量
6. stream_devops_agent + 状态更新 + 清理

### 6.3 拆分后的结构

```python
def _prepare_agent_workspace(repo_key, agent_type, issue_id, runtime_id, backend, evt):
    """公共的 agent 执行准备：workdir / token / session / 环境变量。
    返回 (ws, oc_session_id, repo_token, agent_display_name, env_extra)。
    """
    # 1. 引擎实例名
    inst_name = runtime_instance_name(runtime_id) or backend or ""
    # 2. workdir
    ws = get_agent_workdir(repo_key, agent_type, issue_id)
    os.makedirs(ws, exist_ok=True)
    # 3. session (issue_id 非空时创建 issue 级 session)
    oc_session_id = ""
    if _issue_num_from(issue_id):
        _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
        oc_session_id = _isess.get("runtime_session_id") or ""
    # 4. bot token
    repo_token = _get_repo_bot_token(repo_key)
    # 5. agent display name
    agent_display_name = get_agent_display_name(agent_type)
    # 6. env_extra
    env_extra = {"REPO_FULL_NAME": ..., "ACCOUNT": ...}
    _inject_cloud_credentials(env_extra, repo_key)
    return ws, oc_session_id, repo_token, agent_display_name, env_extra
```

```
_prepare_agent_workspace()        ← 公共函数（新提取）
  get_agent_workdir
  bot token获取
  _materialize_direct_workdir
  环境变量注入

_execute_agent_inner()            ← 现有，改调用方式，逻辑不变
  _prepare_agent_workspace()
  build_devops_agent_prompt()      ← 流水线场景专用
  stream_devops_agent()
  事件状态更新 + 清理

_execute_autopilot_agent()        ← 新建
  _prepare_agent_workspace()
  _build_autopilot_prompt()        ← 定时任务专用
  stream_devops_agent()
  parse_findings + 记录结果
```

### 6.4 对现有流程的影响

_execute_agent_inner 的改动是机械性的：把原来内联的 `ws = get_agent_workdir(...)` 等几行替换成 `ws, ... = _prepare_agent_workspace(...)` 函数调用。逻辑不变，6个现有调用入口的行为不受影响。

风险: 拆分时如果逻辑迁移不完整（遗漏了某个变量或步骤），可能导致现有流程的 agent 执行异常。
缓解: 拆分后跑一次完整的 webhook → run_pipeline → _execute_agent 链路验证；git commit + tag 方便回退。

## 七、测试案例

### 7.1 调度器

| 用例 | 操作 | 预期 |
|------|------|------|
| TC-1 cron到期触发 | cron="* * * * *" | 60s内触发, executions 产生 running 记录 |
| TC-2 cron未到期 | cron="0 9 * * *", 非9:00 | 不触发 |
| TC-3 互斥锁 | 同一任务并发到期 | 跳过, 日志 "already running, skip" |
| TC-4 重试 | 失败 max_retry=2 | 重试2次, 指数退避(10s→20s), 最终status=failed |
| TC-5 手动触发 | POST /tasks/{id}/trigger | 立即执行, 不等cron到期 |
| TC-6 无效cron | 创建任务 cron="invalid" | 400, 提示cron表达式无效 |
| TC-7 cron边界-月末 | cron="0 0 31 * *" (每月31日) | 2月不触发, 有31日的月份触发 |
| TC-8 调度器重启 | 任务正在执行时重启服务 | _RUNNING_TASKS 内存锁丢失, 下次到期可重新触发 |

### 7.2 单agent执行

| 用例 | 操作 | 预期 |
|------|------|------|
| TC-9 issue agent | agent_type=issue | _execute_autopilot_agent 正常执行, 产出记录到 executions.result |
| TC-10 qa agent | agent_type=qa | 正常执行 |
| TC-11 sre agent | agent_type=sre | 正常执行 |
| TC-12 无引擎 | 仓库未配置引擎, pick_engine返回None | status=failed, error="无可用引擎" |
| TC-13 prompt内容 | 检查构建的prompt | 含巡检指令+findings格式模板, 不含issue/scene/pipeline上下文 |
| TC-14 skill_name为空 | _materialize_direct_workdir(skill_name="") | 跳过skill物化, 只物化project-info, 不报错 |
| TC-14b skill_name非空 | skill_name="issue-advance" | prompt含skill文件路径, _materialize_direct_workdir物化SKILL.md到workdir |
| TC-15 workdir创建 | agent_type=issue 执行 | get_agent_workdir 返回独立路径, os.makedirs 创建成功 |
| TC-16 workdir清理 | agent执行完成后 | _cleanup_direct_workdir 清理工作目录(若产出已提取到executions) |
| TC-17 广播模式执行 | 仓库配置远端worker, pick_engine返回0 | _enqueue_pipeline_task 入队, worker认领执行(不走direct) |

### 7.3 event_seq 与并发

| 用例 | 操作 | 预期 |
|------|------|------|
| TC-18 exec_id作为seq | 定时任务执行 | agent_tasks.event_seq = autopilot_executions.id, 不为0 |
| TC-19 多任务并发 | 两个定时任务同时到期执行 | 各自event_seq不同(exec_id不同), _update_direct_task 不会互相覆盖 |
| TC-20 sweeper不误杀 | 定时任务running中, sweeper执行一次 | sweeper查events表无定时任务记录, 不触碰agent_tasks中定时任务的行 |
| TC-21 sweeper不清理workdir | 定时任务执行后workdir存在 | sweeper查issue表无定时任务记录(stage非done), 不清理定时任务workdir |
| TC-22 cancel_direct_task | 用exec_id取消定时任务 | _DIRECT_FUTURES中找到对应Future, 正常取消 |

### 7.4 问题解析与派发

| 用例 | 操作 | 预期 |
|------|------|------|
| TC-23 有问题 | agent产出含3个finding | parse_findings返回3个问题, findings_json存储 |
| TC-24 无问题 | agent产出含空findings数组 | 返回空列表, 前端显示"未发现问题" |
| TC-25 无标记块 | agent产出无autopilot-findings标记 | 降级为"未发现问题", parse_error="no findings block" |
| TC-26 JSON格式错误 | 标记块内JSON语法错误 | 降级为"未发现问题", parse_error含错误信息 |
| TC-27 缺必填字段 | finding缺title或description | 该finding被跳过, 其余有效finding正常返回 |
| TC-28 多条标记块 | agent产出含2个autopilot-findings块 | 取第一个匹配的块, 其余忽略 |
| TC-29 派发成功 | 点击问题按钮 | 创建真实Issue, 走完整流水线(vod→issue→coding→qa→sre) |
| TC-30 派发失败 | GitCode API报错(create_task_via_issue异常) | toast提示错误, 按钮可重试 |
| TC-31 派发后状态 | 派发成功 | findings_json对应finding记录dispatched_issue_number, 按钮变灰+Issue链接 |
| TC-32 重复派发 | 同一问题再次点击派发 | 提示"已派发过"(dispatched_issue_number已存在), 不重复创建 |

### 7.5 安全

| 用例 | 操作 | 预期 |
|------|------|------|
| TC-33 跨仓库-list | 用户A查仓库B的autopilot tasks | 返回空或403(取决于repo_key过滤) |
| TC-34 跨仓库-get | 用户A查仓库B的task详情 | 403 |
| TC-35 跨仓库-update | 用户A更新仓库B的task | 403 |
| TC-36 跨仓库-delete | 用户A删除仓库B的task | 403 |
| TC-37 跨仓库-trigger | 用户A触发仓库B的task | 403 |
| TC-38 跨仓库-findings | 用户A查仓库B的executions/findings | 403 |
| TC-39 跨仓库-dispatch | 用户A对仓库B的执行结果派发任务 | 403 |
| TC-40 未登录 | 无token调任意autopilot API | 401 |

### 7.6 DB迁移

| 用例 | 操作 | 预期 |
|------|------|------|
| TC-41 旧数据清理 | 启动迁移 | autopilot_tasks/executions表清空, 新字段(agent_type/instruction/findings_json)存在 |
| TC-42 旧字段移除 | 迁移后检查表结构 | scene_id/description/agent_chain/issue_number/agent_chain_step 不存在 |
| TC-43 issue表不受影响 | 迁移后检查 | issue表无source字段, 数据无变化 |
| TC-44 events表不受影响 | 迁移后检查 | events表数据无变化 |

### 7.7 拆分验证

| 用例 | 操作 | 预期 |
|------|------|------|
| TC-45 webhook链路 | 拆分后触发webhook → run_pipeline → _execute_agent | agent正常执行, prompt/skill/token/workdir行为不变 |
| TC-46 A2A链路 | 拆分后触发A2A → _execute_agent | agent正常执行 |
| TC-47 重派链路 | 拆分后重派任务 → _execute_agent | agent正常执行 |
| TC-48 PR订阅链路 | 拆分后PR订阅触发QA → _execute_agent | agent正常执行 |
| TC-49 direct执行链路 | 拆分后webhook触发direct执行 → _execute_agent | agent正常执行, _prepare_agent_workspace返回值正确 |
| TC-50 token获取 | 拆分后GitCode仓库执行 | bot_token按repo_key正确获取(GitCode分流) |
| TC-51 token获取-GitHub | 拆分后GitHub仓库执行 | bot_token按repo_key正确获取(GitHub分流) |

### 7.8 前端

| 用例 | 操作 | 预期 |
|------|------|------|
| TC-52 创建任务 | 弹窗选agent(issue/qa/sre)+填指令+cron, 提交 | 创建成功, 列表显示 |
| TC-53 指令为空校验 | 创建时不填instruction | 提示"请填写巡检指令", 不提交 |
| TC-54 编辑任务 | 编辑agent/instruction/cron | 更新成功, 详情显示新配置 |
| TC-55 列表下拉 | 点击任务行 | 展开历史执行记录(时间/状态/结果摘要) |
| TC-56 执行详情-有问题 | 查看有findings的执行记录 | 显示summary+问题列表(severity图标+title)+派发按钮 |
| TC-57 执行详情-无问题 | 查看无findings的执行记录 | 显示"未发现问题" |
| TC-58 执行详情-执行中 | 查看status=running的执行记录 | 显示加载中/进行中状态 |
| TC-59 模板创建 | 点击预置模板 | 自动填入agent_type+instruction+cron |
| TC-60 模板agent正确 | 检查模板填充 | 模板的agent_type为issue/qa/sre之一(非vod/coding) |
| TC-61 弹窗保留 | 对比新旧弹窗 | cron编辑器/预览/布局/结构化模式/高级模式 完全保留 |
| TC-62 启用/暂停 | 切换任务enabled状态 | 状态切换成功, 列表更新 |
| TC-63 删除任务 | 删除任务 | 任务+关联执行记录删除, 列表更新 |
| TC-64 批量操作 | 选中多个任务批量启用/暂停/删除 | 全部操作成功 |

## 八、改动文件清单

```
后端:
  src/runtime/scheduler.py              重写执行逻辑 + 新增autopilot执行函数
  src/api/routes/autopilot.py           字段调整 + 安全校验 + findings/dispatch接口
  src/api/db_setup.py                   autopilot表结构改动 + 迁移
  src/api/event_pipeline.py             拆分 _prepare_agent_workspace (唯一动现有代码)

前端:
  src/api/static/js/pages/autopilot.js  弹窗字段调整 + 下拉历史 + 问题派发

不碰:
  src/api/routes/pipeline.py
  src/api/routes/issues.py
  src/api/routes/webhook.py
  src/api/issues_center.py
  src/api/pipeline_executor.py
  src/api/pipeline_config.py
  src/api/event_bus.py
  src/api/event_store.py
  src/prompt_builder.py
  src/runtime/engine_select.py
  src/api/static/js/pages/pipeline.js
  src/api/static/js/pages/issues.js
  issue表 / events表
```

## 九、设计决策记录

### 9.1 为什么脱离pipeline和需求中心

原方案创建伪Issue(source='autopilot')让定时任务出现在流水线和需求中心。但引入了大量复杂度：伪Issue、issue表加source字段、pipeline API改造、issues API改造、两个前端页面改造。

且伪Issue不在GitCode上存在，agent skill中操作Issue的API调用会404。

主人演进后的判断：定时任务有自己的独立展示窗口（列表+下拉历史+派发按钮），不需要混入pipeline和需求中心。这消除了所有伪Issue相关的复杂度。

### 9.2 为什么不用现有skill

三个现有skill与定时巡检上下文不匹配:

- **issue-advance**: 硬规则要求调 POST /api/tasks/notify 上报任务（需要issue_number）；核心流程是需求拆解/可开发性分析/A2A交办coding
- **pr**: 整个流程围绕具体PR展开（PR配置检查→变更检视→代码审查→回帖），需要PR号
- **cloud-deploy**: 入口3"其他事件类型直接SKIP"；流程围绕构建→部署→验证

prompt注入可以覆盖部分行为，但skill内部的硬规则（notify/A2A/标签）是流程步骤，agent可能仍会尝试执行然后失败。

### 9.3 关于 skill 的策略

不是完全不用 skill，而是默认不用、保留加载接口：

- skill_name 字段保留在 autopilot_tasks 表中，默认为空
- 空 skill_name: prompt 里只有巡检指令 + findings 格式模板，agent 按指令执行
- 非空 skill_name: prompt 里注入 skill 文件路径，_materialize_direct_workdir 物化 skill 文件，agent 先读 SKILL.md 再执行
- 当前三个现有 skill（issue-advance/pr/cloud-deploy）有流水线硬规则冲突，直接用会有问题。但保留接口后，后续可以做适配或开发专用 skill
- 弹窗里 skill 选择器设为可选（留空=纯指令模式）

### 9.4 为什么只用 issue/qa/sre 三个agent

- 排除vod: 定时任务不需要需求澄清，没有开发者交互
- 排除coding: 不会定时循环开发同一件事
- issue: 综合处理类（Issue看板巡检/stale清理/周报）
- qa: 漏洞检查/代码质量扫描/PR过期审查
- sre: 定时部署/部署健康检查/资源巡检

### 9.5 为什么拆分 _execute_agent_inner

_execute_agent_inner 已有6个调用入口，每加一个场景就要确认内部逻辑是否冲突。加分支参数会让函数越来越臃肿（上帝函数反模式）。

拆分提取公共部分（_prepare_agent_workspace），每个场景有独立的执行入口，各自调用公共函数。新场景来了新建执行函数，不用改现有的。

### 9.6 event_seq=0 问题分析与解决

定时任务不创建事件，_create_direct_task 依赖 event_seq 参数。用 seq=0 逐函数追踪：

**1. _create_direct_task(seq=0) (event_pipeline.py:126)**
- 第139行 update_event_config(seq=0): `UPDATE events SET ... WHERE seq=0` → events 表无 seq=0 的行，静默跳过，不报错
- 第179行 agent_tasks 写入 event_seq=0 → 不冲突（真实事件 seq 从1开始）

**2. start_direct_agent (event_pipeline.py:102)**
- 第106行 `seq = args[1]` → seq=0
- 第107行 `if seq:` → False → **不注册到 _DIRECT_FUTURES**
- 后果: cancel_direct_task(0) 找不到 Future，无法取消排队中的任务。但定时任务无"重派"场景，影响不大。

**3. sweeper — sweep_stuck_events_once (cleanup.py:258)**
- 第278行 `SELECT seq FROM events WHERE status='running'` → 查 events 表
- 定时任务不写 events 表 → sweeper 查不到 → **不会触碰定时任务的 agent_tasks 记录**

**4. sweeper — sweep_once (cleanup.py:346)**
- 第370行 `SELECT ... FROM issue WHERE stage='done'` → 定时任务不写 issue 表 → 不被查到

**5. _update_direct_task(seq=0) (event_pipeline.py:293)** ⚠️ 真实问题
- 第301行 `UPDATE agent_tasks SET status=? ... WHERE event_seq=0 AND status IN ('queued','running','dispatched')`
- 会更新**所有** event_seq=0 的 agent_tasks 记录
- 多个定时任务并发执行时，event_seq 都是0，A 任务的结果会覆盖 B 任务的状态

**6. _execute_agent_inner 内部其他 seq 使用**
- update_task_status(task_id): 用 task_id 不用 seq，OK
- update_event_complete(seq): 写 events 表，seq=0 无匹配，静默跳过
- _DIRECT_FUTURES.pop(seq): seq=0 没注册过，返回 None，OK
- revoke_session_token(task_id): task_id 非空，用 task_id，OK

**结论**: seq=0 有一个真实问题——多定时任务并发时 _update_direct_task 互相覆盖。其余安全。

**解决方案**: 不用 seq=0，改用 autopilot_executions.id 作为 event_seq。autopilot_executions.id 是自增整数（从1开始），不会是0，也不会与 events 表的 seq 冲突（不同表）。这样每个定时任务有唯一 seq，_update_direct_task 按唯一 seq 更新，不会互相覆盖。cancel_direct_task 也能正常注册和取消。

## 十、后续扩展方向（当前不实现）

1. **巡检agent触发流水线**: agent 的产出中包含建议的Issue内容，由用户手动点击派发按钮创建真实Issue
2. **弹窗迁移到项目场景页**: 侧边栏导航项移除，项目场景页增加"定时任务"按钮
3. **问题派发增强**: 批量派发/一键全部派发/派发后关联Issue号回写
4. **专用skill**: 如果巡检质量不稳定，为高频场景开发专用skill（如 issue-patrol / qa-scan / sre-healthcheck）
