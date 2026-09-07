# 阶段一实施计划（Agent 文件系统隔离 · 低风险部分）

> 状态：已实施，代码改动完成，待主人验证 + 确认提交（2026-09-02）
> 范围：组件3（凭证过滤）+ 组件1（任务级工作目录清理）
> 不涉及：组件2（bwrap 锁）、组件4（direct 统一物化）—— 属二阶段，暂缓。

---

## 一、改动目标

1. **凭证过滤**：知识库在物化给 agent 前，过滤掉文件名含凭证特征的文件，阻断"凭证文件进工位被 agent 读取"这一已发生事故（VOD 拿错 token）。
2. **任务级清理**：任务结束（成功/失败/异常）后，删除工位内的轻文件（`.skills`/`.project-info`/`.knowledge`）与 git worktree，让 worker 磁盘不再堆积历史任务目录。

## 二、实际改动（与代码一致）

### 1. `src/api/knowledge.py` —— 凭证过滤

- 新增常量 `_CREDENTIAL_SUBSTRINGS`（credential / token / secret / password / passwd / apikey / api_key / api-key / private_key / private-key / access_key / access-key）与 `_CREDENTIAL_EXTENSIONS`（.pem / .key / .env / .p12 / .pfx）。
- 新增 `_is_credential_file(rel_path)`：只对文件名（basename）做大小写不敏感的布尔匹配 + 敏感扩展名判断。
- 在 `member_files`、`member_files_pi`、`agent_files` 三个函数的提取循环里（OBS 分支 + 本地分支共 6 处），命中则 `continue` 跳过。

### 2. `src/runtime/repogit.py` —— worktree 安全卸载

- 新增 `remove_issue_worktree_if_pushed(repo_key, agent_type, issue_num, token="") -> bool`：
  - 取本地 HEAD：`git -C {issue_dir} rev-parse HEAD`。
  - 取远程分支：`git ls-remote {远程URL} refs/heads/feat/issue-{num}`（**直连远程核对，不依赖 bare 镜像 fetch**，避免 fetch 滞后误判）。
  - 本地 HEAD == 远程分支 → 已 push → 复用现有 `cleanup_issue_worktree` 删除，`return True`。
  - 远程无此分支 / 不一致 / 异常 → `return False`（保留，宁可残留不可误删）。

### 3. `src/runtime/agent.py` —— worker 端任务级清理

- 新增 `_cleanup_task_workdir(task)`：删 `{cwd}/.skills`、`.project-info`、`.knowledge`；对 git agent 调 `remove_issue_worktree_if_pushed`。
- 挂接在 `_execute_task` 的 4 个出口（正常完成 / 异常 / 两次中途终止），全部在 `_res.dispose()` 之后触发。失败仅 log warning，不影响结果上报。

### 4. `src/api/event_pipeline.py` —— direct 端任务级清理

- 新增 `_cleanup_direct_workdir(repo_key, agent_type, issue_id)`：删 ws 下轻文件 + 确认已 push 的 worktree。
- 挂接在 `_execute_agent` 的 `finally` 里（覆盖正常 / 失败 / 异常全部路径，幂等）。

## 三、关键边界（已守住）

1. 清理只删当前 task 的 cwd，绝不越界删其它 issue / 其它 agent_type。
2. worktree 删除前必须确认已 push（`ls-remote` 与本地 HEAD 一致）。
3. 会话续传不受影响：state.db 按 `md5(cwd 路径)` 存 `/tmp`，删目录**内容**不影响。
4. 不改动派单/物化/结果回传主流程，只在收尾追加清理。

## 四、自动验证（我已做）

- `py_compile` 四个文件语法检查全过。
- `_is_credential_file` 13 个用例（含 repo-credentials.json / token.json / .env / .pem / secret 应拦；design.md / pipelines-user.json / monkey.txt / issues.json 应放行）全部 PASS。
- `pytest --tb=line -q` 全量在跑，结果待回（对比基线 failed 数不增加）。
- 前端 JS 无改动，不跑 node -c。

---

## 五、主人手动验证清单（pytest 覆盖不了，请逐条做）

> 按钮文案以当前部署版本实际显示为准。

### 【M1 · 最重要】完整流水线跑通 + PR 不丢

- 目的：验证"任务干完删工作目录"没有把代码/下游搞断。
- 操作路径：GitCode 开一个测试 Issue 触发流水线 → 等跑完 → 到 GitCode 该仓库看：① coding 建的 PR 是否真的在；② 评论区 `[XXX Agent]` 评论、标签是否齐全。再回 grape Web「事件 Events」列表看整条链每级状态是否都到 completed。
- 通过标准：全链 completed，PR / 评论 / 标签齐全，无一级卡 running/failed。

### 【M2】会话记忆不丢

- 目的：确认"清目录不丢记忆"。
- 操作路径：同一 Issue 触发第二次（或「去对话」先聊一句带上下文的话，再发"接着上面继续"）→ 看 agent 是否引用上文。
- 通过标准：第二次回复"记得上文"，不重新自我介绍/重新分析。

### 【M3】Web 端完整性

- 目的：确认清理不干扰事件展示与任务状态机。
- 操作路径：grape Web → 事件详情页看日志流 / 结构化结果 / issue_summary 是否完整；任务中心看该任务状态 `queued→dispatched→running→completed` 是否正常、实时日志面板是否逐条刷新。
- 通过标准：各块完整、无「加载失败 / undefined」；状态正常走到终态。

### 【M4】worker 磁盘真实下降

- 目的：确认清理真的生效（不只是代码层面）。
- 操作路径：SSH 到 worker → 跑若干任务前后 `du -sh ~/grape-workspaces/`，或 `find ~/grape-workspaces -maxdepth 4 -type d | wc -l` 看目录数趋势。
- 通过标准：任务结束后工作区占用 / 目录数**不再随任务数线性增长**，旧 issue 目录被回收。

### 【M5 · 可选】凭证过滤不误伤 + 负向验证

- 目的：确认凭证过滤既拦得住、又不误伤正常文件。
- 操作路径：① 确认 VOD 自定义场景推荐仍正常（`pipelines-user.json` 没被误拦）；② 若有兴趣，往项目知识里传一个文件名带 `token`/`credential` 的 json，触发任务后看 agent 是否已读不到该文件。
- 通过标准：① 正常文件照常生效；② 凭证文件被拦截。

---

## 六、回滚

- `git checkout -- src/api/knowledge.py src/runtime/repogit.py src/runtime/agent.py src/api/event_pipeline.py` 即回到改动前；清理逻辑为纯新增（追加式），回滚不影响既有功能。
- **当前改动未 commit / push**，等主人验证并确认后再提交。

---

## 七、补充项：state.db 过期清理（方向已获认可，待实施）

**背景**：hermes 会话记忆 state.db 按 `md5(cwd)` 存于 `/tmp/hermes-acp-homes/{hash}/`，ACP 模式 `keep_overays=True` 从不清理，会随任务无限累积（本机占磁盘、远程可能占内存）。

**方案**：过期清理（N 天未访问才删）
- 清理对象：`/tmp/hermes-acp-homes/{hash}/` 目录（含 state.db + WAL/SHM）
- 判断标准：目录 mtime 超过 N 天未更新（默认 7 天，env `GRAPE_STATE_DB_TTL_DAYS` 可配）
- 触发方式：hermes ACP 构建 overlay 时顺带 lazy 扫描清理（无独立定时器）
- 边界：只删过期目录，正在用（mtime 新鲜）的不碰
- 影响：短期（N 天内）同 issue resume 续接保留；超期清理防累积

**涉及文件**：`src/agent/hermes_acp.py`（stream_execute 构建 overlay 处加 lazy 清理）

**测试**：
- 单元：构造新旧混杂目录（mock mtime），断言只删过期的
- 集成：间隔 < N 天跑两次，确认 resume 仍命中；模拟过期确认被清理