# Grape 平台异常中断问题修复技术方案

> 日期：2026-10-10
> 来源：2026-10-09 会话故障排查（事件 #9601 / #9611 / #9612 / #9620 / #9622 / #9635 / #9644 / #9662）
> 涉及代码：/opt/grape/grape（master-001 174 / master-002 79，双主需同步）

---

## 0. 问题总览

| # | 类型 | 代表事件 | 死法 | 当前终态表现 |
|---|------|---------|------|-------------|
| P1 | 模型流挂死 → 看门狗硬杀 | #9601 | glm 流式响应中途停止，连接不断不报错不吐字，20 分钟零输出被无输出看门狗 SIGTERM | failed（exit=-9，诚实失败） |
| P2 | 派单撞坏引擎/低健康分 | #9612 / #9611 | 场景绑定的引擎探活失败 1000+ 次仍接单；健康分从任务历史现场计算，环境事故失败压死修复后引擎 | failed（"绑定引擎全部不可达"） |
| P3 | hermes 引擎 ACP 全局超时 | #9620 / #9644 | `_GLOBAL_TIMEOUT = max_exec_seconds*2 = 1200s` 写死，调用链全部没传 max_exec_seconds | failed（stopReason=timeout，20 分钟必死） |
| P4 | 场景误判跳过 QA | #9622 | scenes detail 接口 repo 解析是占位函数永远拿默认模板 + worker 缺 jq 且 2>/dev/null 吞错 | completed 但流水线断链 |
| P5 | 模型异常输出被当成成功 | #9635 / #9662 | 流挂死后引擎静默收场 / 空 end_turn，进程 exit 0，完成判定不看产出 | completed 但 result 为空（最危险：静默半成品 + 绿色对勾） |

**两个系统性病根**（P1/P5 的共同根源）：
1. 模型流没有读超时 + 自动重试；
2. 完成判定只看进程退出码，不校验产出内容。

**优先级**：P5（假成功）> P1（流挂死）> P3（超时墙）> P4（断链）> P2（派单）。
理由：假成功污染流水线状态且完全不可见；流挂死高频复现；P3 有绕过手段（换引擎）；P4/P2 已有临时缓解。

---

## 1. P5：完成判定不看产出（最高优先级）

### 现状
`src/agent/executor.py:783-788`：executor 收到引擎进程结束后 `yield ("done", {"exit_code": proc.returncode, ...})`，
直接取进程退出码。opencode 空 end_turn（9662）、codearts 流挂死静默收场（9635）都是进程正常退出
→ exit_code=0 → 上游标 completed，即使：
- 全任务 0 条 text 输出
- 无 PR / 无 A2A / 无 `grape tasks complete` 自报（self_reported=0）
- result_text 为空

### 修复方案

**位置**：executor.py 的 done 产出处（单点，覆盖三个引擎），加"有效性校验"层。

**判定规则（满足任一即判"假完成"）**：
```python
INVALID_COMPLETION = (
    exit_code == 0
    and (
        not full_text.strip()               # 零文本产出
        or text_count == 0                  # 整个任务没有任何 text 事件（9662 特征：27 tool_use / 0 text）
        or (error_events and not full_text) # 有错误且无正文
    )
)
```

**处理策略（分级）**：
1. **自动重试**：对 stream_agent 包装层加一次"无效完成重试"（复用现有 session 失效重跑的骨架，
   executor.py:790-799 `run_gen` / done 判定处）。重试 1 次，重新发起不带 session 的执行。
2. **重试仍无效 → 标 failed**，errors 写明 `"invalid completion: engine exited 0 with empty output (model stream anomaly)"`，
   不允许进入 completed。
3. **completed 前置门槛**：event_pipeline.py 收 `done` 时对 issue/coding/qa/sre 类 devops 任务校验
   `self_reported==1 or 有 A2A/PR 动作`，不满足则降级为 failed（防 executor 层被绕过）。

**验收用例**：
- 模拟 opencode 空输出退出 0 → 任务不标 completed，自动重试一次。
- 模拟 codearts reason=other 0 token 收场 → 同上。
- 正常任务（有 text 有交接）→ 不受影响（对照 9622：29 条 text）。

---

## 2. P1：模型流挂死（读超时 + 重试）

### 现状
glm 经 `https://opengw.clouddeveloper.club/v1/chat/completions` 流式响应，间歇性半路停住：
连接保持、不报错、不吐字。三个引擎各自的兜底行为不一致：
- opencode：executor.py:641-655 静默超时（默认 600s）+ 进程存活续期（AGENT_SILENT_LIMIT）→ 最终 SIGTERM
- codearts：引擎内部 ~75s 放弃，reason=other 静默收场（→ 变成 P5）
- hermes：hermes_acp.py:444-447 工具后 300s / 文本后 60s 静默超时

### 修复方案

1. **上游网关层（首选）**：在 opengw 网关（或 nginx 代理层）对 `/v1/chat/completions` SSE 加
   `proxy_read_timeout 120s`——流 2 分钟无字节即断开返回错误，让客户端立刻感知并重试，
   而不是干等 20 分钟。这是对三种引擎同时生效的一处修复。
2. **executor 层降级重试**：对 opencode 路径，"静默超时被杀但已有部分产出"的场景
   （executor.py:654-693 hang fallback）当前直接以 -9/已产出文本收场。改为：
   若 `collected_text` 为空且任务未完成任何工具调用 → 走 P5 的无效完成重试通道；
   若有部分产出 → 保持现状（诚实失败）。
3. **codearts 引擎层**（如在 Grape 侧无法修改引擎内部行为，则只做 P5 兜底）：
   记录已知限制——codearts 对流挂死返回 exit 0，必须靠 P5 校验拦截。

**验收**：人工把网关超时调成 10s 复现流挂死 → 任务应在 ~2 分钟内重试而非 20 分钟后被杀。

---

## 3. P3：hermes ACP 全局超时 1200s

### 现状
`src/agent/hermes_acp.py`：
- :57 `max_exec_seconds: int = 600`（默认值）
- :408 `_GLOBAL_TIMEOUT = max_exec_seconds * 2`（写死倍率，不吃环境变量）
- 调用链 `event_pipeline.py:3023` / `events.py:1576` / `scheduler.py:312` 调
  `stream_devops_agent` 时**全部没传 max_exec_seconds** → 永远 600×2=1200s
- 外层 `runtime/agent.py:1849` 的 RUNTIME_TASK_TIMEOUT（已配 3600s）永远轮不到生效

### 修复方案

**方案 A（推荐）：对齐 RUNTIME_TASK_TIMEOUT**

1. `hermes_acp.py` 增加：
   ```python
   _GLOBAL_TIMEOUT = int(os.environ.get(
       "HERMES_ACP_GLOBAL_TIMEOUT",
       str(max_exec_seconds * 2)))
   ```
   并在 `_GLOBAL_TIMEOUT` 计算处允许环境变量覆盖（保持下限保护，如 `max(600, 值)`）。
2. worker 侧 `runtime.env`（或 systemd Environment）增加：
   `HERMES_ACP_GLOBAL_TIMEOUT=3600`
3. **注意双主同步**：174/79 两台都要改；hermes_acp.py 属服务端代码，两台 master 同步重启。
4. **同类检查**（按用户要求全面排查）：`_SILENT_TIMEOUT=300` / `_TEXT_SILENT_TIMEOUT=60`
   保持不变（静默超时语义与本次问题无关，不要顺手改坏）。

**方案 B（备选）**：调用链显式传参——`stream_devops_agent(..., max_exec_seconds=1800)`，
需要改 event_pipeline.py / events.py / scheduler.py 三处调用点，遗漏风险比环境变量高。

**已做的临时规避**：场景 729740e3 已把 hermes 从 coding/qa/sre 绑定中移除（用户操作）。
修复验证通过后可评估恢复 hermes 绑定。

**验收**：hermes 引擎跑 >20 分钟任务不再 stopReason=timeout；RUNTIME_TASK_TIMEOUT=3600 生效边界测试。

---

## 4. P4：场景误判跳过 QA（两个独立缺陷）

### 4a. scenes detail 接口 repo 解析失效

**现状**：`src/api/routes/scenes.py:1713-1717` `app_current_repo()` 是写死 `return None` 的占位函数；
`scenes.py:1331-1348` `GET /api/scenes/{scene_id}` 依赖它解析 repo_key → 永远空 →
`pipeline_config.py:229-232` `WHERE scene_id=? AND repo_key=''` 不匹配 → 返回 fallback 默认模板。
前端正常是因为带 `?repo=` 参数；agent 的 CLI（openapi 中该接口只有 scene_id 参数）永远拿不到真实场景。

**修复**：会话 token 签发时已绑定 repo（`issue_session_token(session_key, repo_key, ...)`），
在 `get_scene_detail`（及所有用 `app_current_repo()` 的 12 处调用点）改为：
- 优先从 `Authorization: Bearer tk_...` 反解 repo_key（auth 层已有解析，复用）；
- 删除或实现 `app_current_repo()`（建议实现为读 request context 的 token 绑定信息，一次性修掉所有调用点）。

### 4b. agent 检测脚本依赖 jq

**现状**：worker 机器（master 174、coding worker、dev-build-0005 均确认）没装 jq；
runbook 引导 agent 写的检测命令用 `jq` + `2>/dev/null`，解析失败被静默吞掉 → 误判"无 QA 节点"。
其它流水线没出事是侥幸（模型那次碰巧用 python3 解析）。

**修复（三层）**：
1. **runbook/skill 模板固化**：`coding-cli` skill 中 JSON 解析命令统一写成 `python3 -c` 版本
   （python3 所有 worker 都有），并在 runbook 里加一条"禁止依赖 jq 等非保证安装的工具"；
   同时去掉示例命令中的 `2>/dev/null`，改为输出到 stderr 并在脚本里检查非空。
2. **批量安装 jq**：13 台内部 worker（114-126 段）批量 `yum install -y jq` 作为兜底
   （在单个 execute_code 里循环 terminal()，不逐台弹审批）；外部 worker（dev-build-0005 等）
   由 owner 自行安装。
3. **接口侧兜底**：4a 修好后 scenes detail 返回真实场景，即使解析工具失败，
   agent 重试时拿到的数据正确率大幅上升。

**验收**：在无 jq 的 worker 上跑 coding-cli，agent 能正确识别 qa 节点并发起 A2A。

---

## 5. P2：派单撞坏引擎 / 健康分误伤

### 现状（三个子问题）
1. 场景绑定的引擎探活失败 1000+ 次仍在线吃单（2629 EACCES、2631 venv 损坏时期的 #9612）；
2. 健康分不是读表，而是每次从 agent_tasks 近 7 天历史现场算
   （`health_scorer.py:_fetch_task_stats` → `_compute_and_persist`），改表无效；
   环境事故（改配置/重启/权限）导致的 failed 计入失败率，压死修复后的引擎（#9611，2629 曾 23 分、2631 曾 0 分）；
3. 引擎修复后无重置健康分的正规手段（本次用"把环境事故任务改 cancelled"的脏办法恢复）。

### 修复方案
1. **派单前实时校验**（用户已安排 coding 任务在做，即 #279 P0-B `verify_engine_realtime()` 方案：
   engine_select.py 绕过缓存实时查 DB，pick_engine 校验不健康则换候选）——与本方案兼容，验收时对齐。
2. **探活失败 N 次自动下线**：claim_by_tag 已有健康分拦截（service.py:1466-1473），但
   `agent_runtimes.status` 会被 worker 心跳重注册改回 online。建议：probe_fail_count 超阈值（如 50）时
   在 heartbeat 注册路径上拒绝自动恢复 online，需人工或探活成功才恢复。
3. **健康分区分环境事故**：
   - 短期：新增管理接口 `POST /api/runtime/{id}/health-reset`（鉴权 admin），重置
     agent_tasks 近 7 天 failed 计数（把指定 runtime 的环境事故任务标 cancelled + 清内存缓存
     `health_scorer._cache`，注意 force_refresh）；
   - 长期：任务失败时带 `failure_kind` 标记（engine_error / environment / model_stream / timeout_hard），
     健康分只统计 engine_error 类。P1/P3/P5 修复落地后，model_stream/timeout 类失败可自动豁免。
4. **120s 派单宽限期与 60s 健康分缓存对齐**：宽限期检测改用 force_refresh 的健康分，
   或宽限期延长到 180s，避免"刚修复的引擎在缓存过期前被掐"（#9611 两次死于此）。

---

## 6. 配套运维规范（非代码）

本次 #9607/#9608 证实：运维操作（重启 runtime-agent、改引擎配置）与运行中任务无互斥。
建立操作前检查流程：
1. 重启/升级 worker 前查 `agent_tasks WHERE runtime_id IN (该worker引擎) AND status IN ('running','queued')`，
   有活跃任务则等待或确认可杀；
2. runtime-agent 增加优雅退出（可后续做：SIGTERM handler 先把 running 任务标 failed 再退出，
   消灭"卡 running 的孤儿"）；
3. 改引擎配置（opencode.json 等）采用原子写（临时文件 + rename），避免任务读到半新半旧配置。

---

## 7. 实施顺序与工作量估算

| 阶段 | 内容 | 位置 | 预估 |
|------|------|------|------|
| 1 | P5 完成判定校验 + 无效完成重试 | executor.py / event_pipeline.py | 1 天（含用例） |
| 2 | P1 网关 SSE 读超时 | nginx/opengw 配置 | 0.5 天 |
| 3 | P3 HERMES_ACP_GLOBAL_TIMEOUT 环境变量 | hermes_acp.py + 双主 runtime.env | 0.5 天 |
| 4 | P4a token 反解 repo_key | scenes.py（12 处调用点统一） | 1 天 |
| 5 | P4b runbook 固化 python3 解析 + 批量装 jq | skill 模板 + 13 台 worker | 0.5 天 |
| 6 | P2 健康分 reset 接口 + 探活阈值下线 | health_scorer / service / routes | 1 天 |
| 7 | 运维规范 + 优雅退出 | runtime-agent | 1 天（可后置） |

阶段 1-3 落地后，本会话遇到的 8 个故障事件中 6 个（9601/9620/9635/9644/9662 + 重试覆盖）
将自动恢复或诚实失败，无需人工重派。

---

## 附：本次故障事件与问题映射

| 事件 | 引擎 | 问题类型 | 当时的人工处置 |
|------|------|---------|---------------|
| #9601 | opencode (issue worker) | P1 流挂死 | 看门狗兜底，无需处置 |
| #9607 | codearts | 运维重启杀任务 + 孤儿未收尾 | 自动恢复机制接上 |
| #9608 | opencode | 运维改配置窗口撞启动 | 手动 A2A 重派（9611/9613） |
| #9611 | opencode (2629) | P2 坏引擎 + 健康分 | 标 offline，后引擎修复 |
| #9612 | — | P2 绑定引擎全不可达 | 修 dev-build-0005 后重派 |
| #9620 | hermes (2631) | P3 全局超时 | 重派（9631） |
| #9622 | opencode (2629) | P4 场景误判跳 QA | 待补发 QA A2A |
| #9635 | codearts | P5 流挂死静默收场 | 手动重派（9660） |
| #9644 | hermes (2631) | P3 全局超时 | 用户改场景去 hermes，重派（9650） |
| #9662 | opencode (2629) | P5 空 end_turn | 待重派 |
