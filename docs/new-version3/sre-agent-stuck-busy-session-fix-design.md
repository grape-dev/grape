# grape worker sre 任务卡死（claim conflict busy）问题分析与修复方案

- 版本: v1.1
- 日期: 2026-08-18
- 状态: 待评审（立即处置项不执行，仅存档分析）
- 关联: issue-agent 发布链路注入复验通过（[DIAG:srenv] seq1091 令牌已注入）

## 一、问题现象

sre（skill-publish）任务被 issue agent A2A 转交后，卡在 `claim conflict: session <issue_session_id> busy, requeue` 死循环（3~4 秒一条），任务永久 `queued`、`started_at=NULL`、不执行。

实录：
- seq 1091（task t_a0283f740c0f）绑定坏 session `c1ca7606`，卡死，worker 日志持续 `claim conflict: session c1ca... busy, requeue task t_a0283f740c0f`。
- 同仓库其他用新 session 的任务（如 seq 1091 的另一个实例用 ses_feb0d0e1）能正常跑——**只有复用坏 session 的任务被拒**。

## 二、根因（代码级）

1. **worker 端 busy 判定在进程内存，不在 DB**：`/opt/grape/src/runtime/agent.py:512` `if sid in active_sessions:`。
   - `active_sessions` 是 worker 进程内的活跃会话集合（D9 串行槽：同 session 已有活跃任务则拒收）。
   - 成员在 claim 成功时 `add(sid)`（agent.py:529），在任务终态时才移除。
2. **坏 session 的 opencode 进程永不退出**：某 sre 任务曾用 session `ses_feb300e`（旧坏 session），引擎 hang（无输出）后 worker 只 SIGKILL 引擎，但 **opencode 客户端进程（`/usr/local/bin/opencode run --session ...`，root 用户）没被清理**，一直占用该会话 → worker 内存里该 session 永远 busy。
3. **修复缺口**：引擎级超时/失败路径没有把对应 sid 从 `active_sessions` 摘除 → 泄漏持续阻塞所有绑定该 session 的任务。

### 根因链示例（实录）
```
sre 任务 claim -> add(sid=c1ca) 到 active_sessions
  -> opencode 引擎 hang -> 引擎被 SIGKILL，但 opencode --session c1ca 的进程存活(root)
  -> worker 内存 active_sessions 里 c1ca 永不删除
  -> 后续任何绑定 c1ca 的任务 claim 都命中 if sid in active_sessions -> requeue
  -> 死循环
```

## 三、影响范围

- 绑定到「坏 session」的 sre 任务全部卡死（本次 hello-skill 的 skill-publish 任务之一类）。
- 间接：`busy_slots` 因反复 claim+requeue 泄漏飙高（实录 190 vs max 4；叠加既有「requeue 不释放 busy」缺陷）。

## 四、修复方案

### 4.1 立即处置（临时解卡）——本次暂不执行，仅记录
```bash
# 1) 杀占坏 session 的 opencode 进程（root 进程，普通用户 kill 无效）
sudo kill -9 449175

# 2) 让被卡任务重新入队
cd /home/yangaiwu/grape
mysql -h127.0.0.1 -ugrape -p'***' grape_test_db -e \
  "UPDATE agent_tasks SET status='queued', started_at=NULL, attempts=0 WHERE id='t_a0283f740c0f';"

# 3) 可选：强制该 sre session 下次开新会话
mysql ... -e "UPDATE issue_agent_session SET runtime_session_id='' WHERE id='c1...' AND agent_type='sre';"
```
- 临时代价：kill 只杀挂起进程，不影响 worker 主链路；被卡任务在新 session 重跑。
- 前提已确认：runtime 66 无 running 任务，杀该进程安全。

### 4.2 根源修复（动代码，worker 侧 + 主服务侧）

**改动点 A：worker 引擎终态时摘除 active_sessions（治本）**
- 文件：`/opt/grape/src/runtime/agent.py`
- 位置：引擎修复（completed / failed / 超时 SIGKILL / 无输出摘除）分支，统一 `active_sessions.discard(sid)`（sid=该任务本次使用的会话）。
- 效果：坏 session 不再常驻 active_sessions，后续任务 claim 不受阻。

**改动点 B：requeue 同步释放 busy_slots（补既有缺陷）**
- 文件：主服务 `/home/yangaiwu/grape/src/runtime/service.py`
- 位置：`requeue_task`（:605-616）置 queued 时若旧 runtime_id>0 → busy-1（对齐 :594 的 `CASE WHEN busy>0` 逻辑）。
- 效果：busy_slots 不再被冲突循环刷高（实录 190/310 vs max 4）。

**改动点 C（可选防御）：服务端死任务兜底清理**
- 服务端检测到 running 死任务（引擎无回报超时）时，主动置任务终态 + 清理该 issue_session 的 runtime_session_id（memory「坏 session 自愈」计划性建议）。
- 需区分「引擎级失败（该清）」vs「业务级失败 QA 驳回（保留 context续接）」。

## 五、评审确认点（请逐项给出）

1. **立即处置（4.1）是否执行**：kill 449175 + requeue？——现收到指示：**暂不执行**，已记录。
2. **改动点 A（worker 摘除 active_sessions）是否采纳**？——是/否/待定
3. **改动点 B（service.py requeue 释放 busy）是否采纳**？——是/否/待定
4. **改动点 C（服务端死任务兜底）是否采纳**？——是/否/暂不做
5. **修复顺序**：先执行处置还是直接改代码？建议：先确认 A/B/C 方案，再决定执行次序。

## 六、回归验证（采纳后）

- kill+requeue 后：sre 任务能正常 claim 且 completed（非 queued）。
- 三平台发布：复用已注入令牌，走通 signway/grape_skill、yangaiwu/grape_dev。
- worker 侧：`ps` 无残留 opencode 挂死进程；日志无 `claim conflict` 重复；busy_slots 不再刷高。