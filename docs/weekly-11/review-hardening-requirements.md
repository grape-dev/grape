# 评审补强四条 —— 需求

来源：2026-11-22 分支评审（clone 端点/凭据平面议题）的“建议（不阻断但该排期）”部分。
范围：四项独立的质量硬化，不改变对外协议；每项独立可回滚。

## R1 settings 存令牌不得半写

- 现状：`PATCH repo bot token` 先写令牌，再校验 bot 用户名是否与真人账号冲突；冲突时返回 400，
  但令牌已落库（半写）。自动用“令牌所属账号名”回填时，调用方未显式传名字也会命中该路径。
- 要求：任何写入之前完成用户名冲突校验；校验对象必须是**最终会写下去的名字**（显式值优先，
  否则令牌所属账号）。只改用户名（无令牌）的请求同样先校验。
- 验收：冲突时返回 400 且库中令牌、用户名均未变化；非冲突时行为不变。

## R2 写冻结守卫必须真的扫描源码

- 现状：`tests/mgmt/test_user_accounts_write_freeze_guard.py` 的源码目录解析为 `tests/src`（不存在），
  GC1–GC3 扫 0 个文件，永远通过。
- 要求：解析到仓库根 `src/`；扫描真实源码；存在写语句即失败。
- 验收：守卫在本分支通过，且“人为插入一条 INSERT INTO user_accounts”时守卫会红（人工验证）。

## R3 无硬编码 clone 地址的检查要覆盖全仓

- 现状：只参数化 3 个文件、只比对两种字面量，且不验数据流；`#1` 沙箱 clone 到错平台的问题逃得掉。
- 要求：
  - 扫描全仓 `src/`：同一行出现仓库 host 字面量且含 `.git` 即视为“自拼地址”；
  - 存量违约点登记在案（棘轮），只允许减少不允许新增；
  - 增加数据流断言：`REPO_KEY`（带前缀）与 `REPO_FULL_NAME`（裸名）同时存在时，沙箱取前者。
- 验收：新增一处自拼地址测试失败；gitee/github/gitlab 三个仓的 clone URL 由前缀决定。

## R4 OAuth 刷新不得形成轮换风暴

- 现状：刷新成功但上游仍 401 时只提示重新授权：身份仍是 status=1，下一次请求继续刷新；
  GitHub 每次刷新轮换 refresh_token → 逐请求刷新、链式消耗。
- 要求：
  - 成功刷新后 60s 内再次要求刷新 → 跳过 OAuth 调用，返回库中现有 access_token；
  - 刷新成功但重试仍为 token 错误 → 身份置 status=4（引导重新授权），结束刷新环；
  - 刷新彻底失败（invalid_grant/400）保持原有 status=4 行为，与上面共用同一函数。
- 验收：连续两次 401 只触发一次 OAuth 往返；日志出现 backoff；身份最终 status=4。

## R5 既有失败与基线一致的记录纪律

- `tests/mgmt/test_user_db.py::TestAliases::test_aliases_delegate` 在基线 `dcb3759e7` 同样失败，
  记为“既有失败”，不改断言迎合；本分支不因它变红而扩大改动范围。
## R6 传输凭据同源必须覆盖“重试/连带推送/远端探测”

- 现状（二次评审发现）：`_run_git` 已能按 repo_key 派生 scheme，但
  `_run_git_retry` 收了 `repo_key` 从不传给底层（死参数）→ 重试路径回落 x-access-token；
  `agent.py` 的 linked-repo push、`worktree_guard.py` 的 `ls-remote` 远端探测也未传。
- 要求：**任何会发网络的 git 动作**（fetch/push/ls-remote/clone）都必须把 repo_key 传到底；
  重试封装不得吞参数；远端探测失败与“分支未推送”必须可区分。
- 验收：`_run_git_retry(repo_key=…)` 的行为用例（不是字符串搜索）断言底层收到 repo_key；
  worktree_guard 与 agent 的调用点有用例钉住。

## R8 测试隔离纪律

- 要求：测试不得手工改写并“恢复”模块级全局（`orig = mod.f; mod.f = ...; finally: mod.f = orig` 之类）。
  一律用 pytest 的 monkeypatch 夹具恢复；同进程内多个测试文件必须互不影响。
- 验收：DDL 场景与 table_ownership 场景同进程全绿；DDL 场景源码中不存在手工赋值全局的写法。

## R9 云沙箱 executor 必须与 sandbox_context 用同一判定

- 现状：`agents/executor.py::_run_cloud_sandbox` 用 `proc_env["REPO_FULL_NAME"]`（剥前缀裸名）
  派生平台；`parse_repo_key` 对裸名默认 gitcode → gitee/github/gitlab 仓被 clone 到
  gitcode.com，令牌环境变量名也回落 `BOT_GITCODE_TOKEN`。
- 要求：判定顺序与 `sandbox_context` 完全一致：`REPO_KEY`（带前缀）→ `REPO_FULL_NAME`（裸名兜底）；
  `REPO_FULL_NAME` 只对 runbook 拼 URL 有意义，不得单独决定平台。
- 验收：用例钉住该顺序；gitee/github/gitlab 三个仓的 clone URL 由前缀决定。

## R10 文档/脚本/夹具脱敏红线

- 禁止出现在仓库内的真实值：个人绝对路径（`/home/<name>`、`/root/<name>`）、
  真实账号名（含平台账号）、真实库名、真实主机/IP、备份目录实路径。
- 允许形态：`<venv>`、`<ACCOUNT>`、`<db_mgmt>`、`<db_insight>`、`<BACKUP_DIR>`、
  `<host_a>`、`<server_ip>`、`<public_ip>`、`<platform_account>`；测试夹具用通用仓
  `acme/<repo>`。
- 验收：扫描本分支变更文件（`git diff --name-only <base>...HEAD`）不得命中上述真实值；
  基线已有的历史命中登记在白名单，只允许减少不允许新增。

## R11 提交署名

- 分支内所有 commit 的 author/committer 与 `Signed-off-by` 必须是机器账号
  `grape-bot <bot@grapedev.cn>`；历史遗留的个人署名在合入前重写清洗。
