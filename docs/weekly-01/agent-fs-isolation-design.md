# Agent 文件系统隔离方案设计（最终版）

> 状态：设计定稿 · 待主人审阅批准后实施
> 日期：2026-09-02
> 作者：Neko-chan（Hermes Agent）

---

## 0. 考察与讨论记录

- 第 1~3 遍全量代码考察：建立 agent 文件访问点地图、逐点精读、方案 impact 评估。
- 与主人多轮讨论后修正三处结论：
  1. agent 工作目录（含 git worktree）**可以任务级删除**，无需等整条流水线结束——代码传递靠远程 GitCode 分支 `feat/issue-{n}`，各 agent（coding/qa/sre）的 worktree 按 `agent_type` 分目录、互不读对方磁盘（repogit.py:47-50,153-182）。
  2. agent **不查 grape 数据库**（数据由服务端查好、打包、喂给 agent）；但 agent 有两只主动的手——**主动联网**（GitCode/A2A/云）与**主动翻盘**（读任意权限可达路径）。因此仅"清文件"不够，还需"锁范围"。
  3. 隔离粒度可做到 **issue + 职责级**（高于仓库级），因为 agent 干活所需文件本就物化在它自己的工位目录内。

---

## 1. agent 工作逻辑（设计前提共识）

1. **派单喂料**：agent 抢到任务（claim）→ 服务端把该任务所需的「技能 / .project-info 资料 / 知识」按「仓库 + issue + 职责」定向打包，随任务交给 agent → agent 在本地物化落地。
2. **干活出结果（两条路）**：
   - 业务动作（发评论 / 打标签 / 建 PR / A2A 交办）由 agent **自己 curl 相关 API** 直接完成（走网络，用 env token）；
   - 交差总结（含 `actions/conclusion/issue_summary` 的 JSON 块）经 stdout 流回 worker → worker 调 `complete` 回传服务端 → 落库、判成败、自动回帖。
3. **下游重新喂料**：服务端收到结果 → 交办下一 agent（agent 末尾主动调 A2A API，或服务端按结果自动触发）→ 下一 agent 入队、抢单、**重新为它定向打包**（不是搬上一个的文件）。
4. **两只主动的手**：agent 不查 DB，但会主动联网 **且** 主动读磁盘上任何权限可达路径——这是本方案要锁住的对象。

---

## 2. 现状问题（无隔离）

- 三处 `subprocess.Popen`（executor.py:419-423、hermes_acp.py:110-114 / 340-344）只传 `cwd + env + start_new_session`，无 chroot/bwrap/namespace/seccomp。
- agent 是普通用户态进程，read/terminal 工具能读任意路径；`cd ..` 或读绝对路径即可读同机任何文件。
- cwd 布局分两套：
  - **worker 端**：任务物化到 `{WORKSPACE_DIR}/{owner}/{repo}/issues/{num}/{agent_type}/`，含 `.skills`/`.project-info`/`.knowledge` + git worktree（均在 cwd 内）。
  - **direct 端**（服务端本机）：只 `makedirs`，不物化；prompt 里 `{{PROJECT_INFO_DIR}}` → `{WORKSPACE_DIR}/{owner}/{repo}/project-info` 原件、`{{SKILL_DIR}}` → 全局 SKILLS_DIR（在 cwd 之外）。
- 敏感资产：`configs/global-bot-token.json`、`configs/db-backend.json`、`.env`、`data/sessions.db`（服务端同机可达）；成员上传凭证文件（如 `repo-credentials.json`）会被打包进 `.project-info/member/`。

---

## 3. 设计目标

1. **隔离粒度 = issue + 职责级**（高于仓库级）：agent 只见自己工位。
2. **任务级生命周期**：来活建目录、干完（确认产物已交）删目录，worker 磁盘干净。
3. 保留 agent 干活必需能力：技能/资料/知识可读、git 可写 + push、网络可达、引擎自身运行依赖正常。
4. 统一 direct/worker 两套路径，消除不一致。

---

## 4. 方案设计（四大组件）

### 组件 1：任务级工作目录生命周期（来活建、干完删）
- 现状：任务结束后 `.skills`/`.project-info`/`.knowledge`/worktree 全残留，长期堆积。
- 改动：任务完成（成功/失败/异常）后执行清理：
  - **轻文件**（技能/资料/知识）：任务结束即删（下次派单重新物化）。
  - **git worktree**：任务结束也可删，**但须满足三个前提**：
    1. 确认 `git push` 已成功（产物已交远程，否则删本地即丢失改动）——这是唯一真丢东西的口子；
    2. 用 `git worktree remove` 正规卸载，不用 `rm -rf`（避免 bare 仓库残留悬挂 worktree 记录）；
    3. 会话续传不受影响：hermes 会话 state.db 按 `md5(cwd 路径)` 存于 `/tmp/hermes-acp-homes/{hash}/`，与 cwd **目录内文件**无关；路径字符串不变即续传不断。
- 实现：扩展 [`TaskResourceStack`]（resource_stack.py），或新增 `_cleanup_workdir` 钩子，在 worker 端 `_execute_task` 与 direct 端 `_execute_agent_inner` 收尾处调用。

### 组件 2：issue + 职责级隔离锁（只挂自己工位 + 引擎必需品）
- 技术选型：**bubblewrap（bwrap）**——无需 root、`--die-with-parent`、可 `--share-net` 保留网络；systemd/Flatpak 同款机制。
- 锁的本质：每个 agent 子进程启动时，用 bwrap 包一层，**只挂载自己的工位目录**（`.../issues/{num}/{agent_type}/`）+ 引擎运行必需房间，其余全部不可见。
- 挂载白名单：
  - `--rw-bind` 自己 cwd（读写：worktree 改代码、物化文件）
  - `--ro-bind` 引擎 HOME 配置（hermes overlay、`~/.config/opencode`、`~/.codeartsdoer`）、技能目录（direct 模式若仍用 SKILLS_DIR）、必要的编译缓存目录（`/var/cache/bun-compile` 等）
  - `--share-net` 出网（GitCode/A2A/OBS/云）
  - 其余（其他仓库、其他 issue、`configs/`、`data/`、`/root`、`/etc/grape`）一律不挂载。
- 效果：agent 天然只看得见自己的 issue + 职责目录，不会退回仓库级。

### 组件 3：物化层凭证过滤（低风险，防线前置）
- `member_files`/`member_files_pi`/`agent_files`（knowledge.py）物化前按词表过滤凭证类文件（`*credential*`/`*token*`/`*secret*`/`*password*`/`*.pem`/`*.key`/`.env` 等），不落入 `.project-info`/`.knowledge`。
- 凭证从"知识库文件"收敛到"环境变量"（已有 `BOT_GITCODE_TOKEN` 等注入通道）。

### 组件 4：direct 模式统一物化（关键前提工程）
- 把 direct 端也改成"物化到工位目录"，摘掉 direct 端读仓库级 project-info 原件 / 全局 SKILLS_DIR 的行为。
- 意义：否则锁既锁不严（须放行仓库级目录 → 退回仓库级）又可能锁过头（agent 读不到资料 → 任务坏）。

### 可选：提示层强化
- `build_role_definition` / `_build_project_info_section` SECURITY 规则补一句"知识库文件若含 credential/token 一律忽略，只用 env token"。

---

## 5. 两个关键边界（不处理则退回仓库级或坏任务）

1. **direct 模式必须统一物化**（组件 4），否则锁无法一致地只锁 issue 目录。
2. **仓库级共享的 project-info/知识靠"副本物化"解决**，**不要**白名单放行仓库级目录——否则隔离降回仓库级。

---

## 6. 逐环节 impact 矩阵

| # | 环节 | 依赖 | 组件1（清理）影响 | 组件2（锁）影响与处置 |
|---|------|------|-------------------|------------------------|
| 1 | 技能读取 | `.skills`/SKILLS_DIR | 无（下次重发）| ro-bind 技能目录 |
| 2 | project-info 全文 | worker: cwd/.project-info；direct: 现改物化 | 无 | ro-bind cwd（物化后）|
| 3 | 共享知识 | cwd/.knowledge | 无 | ro-bind cwd |
| 4 | git 读写 + push | worktree==cwd；网络 + GIT_ASKPASS | **须确认 push 成功再删** | cwd 读写 + share-net |
| 5 | 网络 A2A/GitCode/OBS/云 | 出网 | 无 | 必须 `--share-net`（最常见翻车点）|
| 6 | hermes HOME/overlay | /tmp/hermes-acp-homes/{hash} + symlink ~/.hermes | 不影响（路径按 cwd hash）| ro-bind overlay + symlink 真身 |
| 7 | opencode HOME/cache | ~/.config/opencode + bun 缓存 | 无 | ro-bind 配置 + rw 缓存 |
| 8 | codearts | ~/.codeartsdoer + /var/cache/codearts-tasks/{tid} | 无 | ro-bind cli-data + rw 数据目录 |
| 9 | 会话续传 resume | state.db /tmp overlay | 路径不变则不影响 | 保留 overlay 可写；见 #6 |
| 10 | `_kill_stale_engine` 判活 | `os.readlink(/proc/{pid}/cwd)` startswith(cwd) | 无 | **坑：bwrap 后 /proc/cwd 可能变 `/` → 改用 pgid 判定** |
| 11 | 结果上报/回帖/会话同步 | grape 进程内网络 API | 无 | 无（grape 进程内，非 agent 进程）|
| 12 | /tmp 临时产物 | tf-deploy/bun .so 等 | 任务级清理 | 谨慎：rw-bind 私有 tmp，勿全局 tmpfs 割断 hermes overlay |

---

## 7. 兼容性清单

| 维度 | 影响 | 处置 |
|------|------|------|
| 三引擎 hermes/opencode/codearts | 白名单不同 | 分别验证 |
| direct vs worker | 组件4 统一物化 | 锁才一致 |
| OBS/本地存储双模式 | 物化后路径一致 | 无差异 |
| 本机 worker（92/93 与服务端同机）| 收益最大（该 agent 能读服务端配置）| 组件2 直接封死 |
| 远程 worker（1.92.80.69）| 只删本地残留 + 锁本机 | 需装 bwrap |
| sweeper/redispatch | grape 进程内 | 无影响 |

---

## 8. 测试用例设计（详细版）

> 可测性标签：**【自动】** = 我可用 pytest/脚本/curl 自动验证；**【半自动】** = 需真实 worker/真实 GitCode 环境配合触发，我可搭脚本但需主人联动；**【Web/人工】** = 只能主人通过浏览器 Web 端或真实 GitCode 亲自验证。

### 8.1 组件1：git worktree 任务级清理

#### A 组——清理时机与三前提（核心）

- **A1** push 成功 → 删除 worktree
  目的：正常路径清理生效。前置：mock coding 完成且 `git push` 成功（`git log origin/feat-issue-5..feat-issue-5` 为空）。断言：issue_dir 不存在；bare 仓库 `git worktree list` 该目录消失。**【自动】**
- **A2** push 失败 → 不得删除
  目的：丢代码保护。前置：mock coding 已 commit 但 `push` 失败（origin 落后于本地）。断言：issue_dir **保留**，并产出告警日志"产物未 push，跳过清理"。**【自动】**
- **A3** 仅本地未提交改动 → 不得删除
  目的：未提交改动保护。前置：agent 改了文件但未 commit。断言：目录保留。**【自动】**
- **A4** 正规卸载（`worktree remove`），bare 无 prunable
  目的：验证不用 `rm -rf`。断言：清理后 `git -C base worktree list` 无该 issue 的 prunable 悬挂条目。**【自动】**
- **A5** 清理幂等（反复触发不报错）
  目的：任务收尾兜底 + sweeper 可能先后都触发清理，二调应为 no-op。断言：连续调两次无异常、无副作用。**【自动】**

#### B 组——轻文件清理（`.skills`/`.project-info`/`.knowledge`）

- **B1** 任务结束后三目录被删除
  断言：`{cwd}/.skills`、`{cwd}/.project-info`、`{cwd}/.knowledge` 均不存在。**【自动】**
- **B2** 下次任务重新物化成功（删了还能长回来）
  步骤：同一 issue 连跑两次，断言第二次三个目录重新物化、内容完整。**【自动/半自动】**
- **B3** 清理 A 工位不碰 B 工位（并行隔离）
  步骤：两 issue 并行，A 结束清理后断言 B 的目录与文件仍在。**【自动】**

#### C 组——会话续传（resume）

- **C1** 同 issue 二次执行 hermes resume 命中
  步骤：issue 第一次跑完清目录 → 再触发同 issue 同 agent；断言日志出现 `resumed session`（非 `session/new`）、上下文延续。**【自动/半自动】**
- **C2** 路径不变则 state.db hash 不变
  步骤：清目录前后各算 `md5(cwd 路径)`，断言一致（记忆目录不受删文件影响）。**【自动】**

#### D 组——下游链路不受影响

- **D1** coding 删 worktree 后 qa 仍能 checkout 到改动
  步骤：coding complete 且 push 成功 → 清理 → qa `ensure_issue_worktree` 从远程 checkout `feat/issue-5`；断言 qa worktree 含 coding 的提交。**【半自动】**
- **D2** 跨机器链路（远程 worker 的 coding 清理不影响另一台 worker 的 qa）——见 §8.3 W 组。**【Web/人工】**

#### E 组——失败/异常路径的资源释放

- **E1** 超时被杀 → 清理仍触发
  断言：`_kill_stale_engine` + dispose 路径后目录被清。**【自动】**
- **E2** agent 抛异常/崩溃 → 清理仍触发（finally 兜底）
  断言：异常路径同样清理、无残留。**【自动】**
- **E3** 进程被 kill 后 worktree 不残留、下次可重建
  断言：`ensure_issue_worktree` 重建成功不报错。**【自动】**

#### F 组——redispatch / 重跑

- **F1** redispatch 后 worktree 已删 → 重建成功
  步骤：任务 redispatch（旧 task 已清理）→ 重建 worktree 成功。**【自动/半自动】**

#### G 组——与 sweeper 的关系

- **G1** 任务级清理与 sweeper `cleanup_issue_worktree` 不冲突、双触发安全
  断言：两处先后触发均幂等、无 bare 悬挂、无空目录残留报错。**【自动】**

#### H 组——direct 模式

- **H1** direct 模式任务结束也触发清理。**【自动】**
- **H2** direct 模式 worktree 清理后，下次同 issue 重建成功。**【自动】**

#### I 组——OBS / 本地双模式

- **I1** OBS 技能 zip 物化 + 清理后，下次重新拉取正常。**【自动/半自动】**
- **I2** 本地技能目录物化 + 清理后，下次重新物化正常。**【自动】**

#### J 组——结果归档完整性

- **J1** 清理 worktree 不影响 event.json / result 落库 / 自动回帖
  断言：清理动作发生在结果已归档之后；清理后 Issue 评论、event 状态、`issue_summary` 完整。**【自动/半自动】**
- **J2** onboarding 结果文件（路径跟随 workdir）——风险点，需确认清理时机晚于结果文件被读取。**【自动】**

### 8.2 组件2/3/4（凭证过滤 / 锁 / direct 物化）

- **K1** 凭证过滤：fixture 含 `member/x/repo-credentials.json`、`design.md`、`x/api-key.txt` → 只保留 design.md。**【自动】**
- **K2** 凭证过滤不误伤：`pipelines-user.json`、`design.json` 正常通过（验证词表不过度）。**【自动】**
- **K3** 锁白名单：bwrap 命令行构造含 `--share-net`、`--die-with-parent`、cwd/引擎目录白名单，不含 `configs/`、`data/`、其他仓库/issue。**【自动】**
- **K4** 安全断言：agent 内 `cat /opt/grape/configs/global-bot-token.json`、`cd ../..` → 应 no such file。**【自动/半自动】**
- **K5** direct 物化后读资料/技能正常（组件4）。**【自动】**
- **K6** 三引擎各自白名单下任务跑通（hermes/opencode/codearts 分别验证）。**【半自动】**

### 8.3 我测不到、需要主人 Web 端 / 真实环境验证的清单（重点）

> 以下是 CLI/脚本环境**无法覆盖**、只能主人通过浏览器操作或真实 GitCode/多机 worker 环境才能暴露的问题。
> 每条含：**目的 / 操作路径 / 通过标准**。按钮文案以当前部署版本实际显示为准。

- **W1** 真实 GitCode 结果完整性
  - 目的：确认清理目录不会误删已产出的 PR / 评论 / 标签。
  - 操作路径：浏览器登录 gitcode.com → 打开目标仓库 → 点 Issues → 进入对应 Issue → 依次看：评论区（应有以 `[XXX Agent]` 开头的 agent 评论）、右侧 Labels（agent 打的标签还在）、顶部 Pull Requests（coding 建的 PR 在）。再回 grape Web「事件详情」看 artifacts 字段交叉核对。
  - 通过标准：PR / 评论 / 标签齐全，无因清理导致的缺失。

- **W2** 事件详情页
  - 目的：确认清理不影响事件详情页各区块的渲染。
  - 操作路径：登录 grape Web → 左侧导航进「事件 / Events」→ 点该事件行进入详情 → 逐块核对：①「接收上下文」折叠区有内容；② 最终结果 / 结论块（结构化的 actions/conclusion/issue_summary）完整；③ 实时执行日志 / 逐步输出无截断。
  - 通过标准：三块均完整，无「加载失败」、无空白块、无 undefined。

- **W3** 会话记忆体感
  - 目的：确认"清目录不丢记忆"，resume 在用户侧真实生效。
  - 操作路径：任务中心（或事件详情）→ 点「去对话」进入任务会话 → 先就某 Issue 发一句带上下文的话并等回复 → 再发一句"接着上面 X 继续做"→ 观察第二次回复是否引用了第一次的上下文。
  - 通过标准：第二次回复体现"记得上文"，而不是重新自我介绍 / 重新分析。

- **W4** 任务状态流转 + 实时日志
  - 目的：确认清理逻辑不干扰任务状态机与实时日志上报。
  - 操作路径：grape Web → 任务中心（Task Center）→ 找到该任务行 → 观察状态徽标是否按 `queued→dispatched→running→completed/failed` 流转 → 点「实时日志 / 运行日志」打开日志面板 → 观察逐步输出逐条滚动刷新。
  - 通过标准：状态到达符合预期的终态；日志逐条刷新、不卡死、不空白。

- **W5** Scene Center / 引擎页
  - 目的：确认清理不影响 runtime 会话列表与引擎状态展示。
  - 操作路径：grape Web → 引擎 / Workers 页（Engines/Workers 视图）→ 点某引擎行打开详情 / 会话 modal → 看 runtime 会话列表是否正常列出 → 再切「系统监控」页看该引擎 / worker 在线状态与心跳时间。
  - 通过标准：会话列表非空、引擎 status=online、心跳时间新鲜（<300s）。

- **W6** worker 磁盘占用真实下降
  - 目的：确认任务级清理确实让工作目录不再无限堆积。
  - 操作路径：SSH 到 worker（远程 1.92.80.69 用内部运维凭据；本机直接终端）→ 清理前 `du -sh ~/grape-workspaces/` → 跑若干任务结束后再 `du -sh ~/grape-workspaces/` 对比 → 也可以 `find ~/grape-workspaces -maxdepth 4 -type d | wc -l` 看目录数增长趋势。
  - 通过标准：工作区总占用与目录数**不再随任务数线性增长**，旧 issue 目录被回收。

- **W7** 前端无残留报错
  - 目的：确认清理没有间接触发前端 JS 对不存在路径的访问报错。
  - 操作路径：grape Web 逐页点查：事件列表 → 事件详情 → 任务中心 → Scene Center → Agent 页 → 系统监控；全程开浏览器 DevTools（F12）→ Console 面板观察有无红色报错（尤其 TypeError）→ 同时看页面有无「加载失败 / undefined」提示或异常空状态。
  - 通过标准：Console 无新增报错；各页无加载失败提示。

- **W8** 跨机器真实链路
  - 目的：验证远程 worker 的 coding worktree 被清理后，另一台 worker 的 qa/sre 仍能正常接棒（本地单机测不了）。
  - 操作路径：用真实多 worker 环境跑一个完整 Issue（vod→issue→coding→qa→sre）→ grape Web「事件 Events」列表观察整条链路每级状态都到 completed → 最后到 GitCode 看最终产物（PR 合并 / 部署评论 / sre 回帖）。
  - 通过标准：整条链全部 completed，无一级因"coding worktree 被清理"而卡在 running / failed。

---

## 9. 回滚方案

- 组件1/3：`git revert` knowledge 函数 + 清理钩子，重启服务端即可。
- 组件2/4：`GRAPE_AGENT_SANDBOX` 默认**关**；回滚=关开关；彻底回滚 revert bwrap 包装。

---

## 10. 分阶段落地顺序

- **阶段一（低风险，当日可上）**：组件3（凭证过滤）+ 组件1（任务级清理，含 worktree push 确认 + 正规卸载）。
- **阶段二（高风险，谨慎灰度）**：组件4（direct 统一物化）→ 组件2（bwrap 锁，按"本机 worker/本机 direct → 远程 worker"灰度）。
- 提示层强化随阶段一顺带做。

---

## 11. 涉及文件清单

| 文件 | 改动 |
|------|------|
| `src/api/knowledge.py` | 组件3：`_is_credential_file` 过滤器 |
| `src/runtime/agent.py` | 组件1：任务级清理钩子；组件2：#10 改用 pgid |
| `src/api/event_pipeline.py` | 组件1（direct 清理）/ 组件4（direct 物化）|
| `src/agent/executor.py`、`src/agent/hermes_acp.py` | 组件2：bwrap 包装 |
| `src/runtime/repogit.py` | 组件1：worktree 正规卸载封装（复用现有 cleanup）|
| `src/config.py` | 组件2：白名单路径常量 |
| `src/prompt_builder.py` | 可选：提示层强化 |

---

## 12. 待主人拍板

1. 阶段一立即上？阶段二何时启动（是否先只锁「本机 worker / 本机 direct」这类最危险面）？
2. 远程 worker 是否允许安装 bwrap（涉及运维）？
3. 凭证过滤词表是否按布尔匹配（推荐）而非全量 `*.json`（会误伤 pipelines-user.json 等）？
4. 最终隔离粒度确认：`issue + 职责`（锁到自己 cwd，推荐）还是仅 `issue`（锁整个 issue 目录）？

---

*（纯设计文档，未改任何代码；待主人明确批准后再进入编码。）*