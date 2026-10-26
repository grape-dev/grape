# 模型流瞬时异常自动重试（Issue #403）需求文档

- Issue: https://gitcode.com/grape-dev/grape/issues/403（Refs #403）
- 分支: feat/issue-403-model-stream-retry（base: origin/weekly-08 @ 631bbb2ce）
- 状态: 已实现
- 类型: 稳定性修复（P1）

## R50 模型流瞬时异常错误分类

在判定层（任务终态收敛点）对失败原因做分类，区分：

- 上游瞬时（upstream_transient）：连接类错误（connection reset/refused、broken pipe、socket hang up、EOF）、模型流中断（stream error/aborted）、超时（timeout/timed out）、上游 5xx（502/503/504/bad gateway/service unavailable）、限流（rate limit/overloaded）、IAM 瞬断。
- exit 0 空输出（empty_output）：引擎 exit 0 但无可信产出（假完成硬门槛降级路径）。
- 配置错误（auth_config）：命中 is_auth_401_error（VCS token 过期/权限不足）——不重试。
- 任务失败（task_failure）：其余一律默认不重试（保守面）。

## R51 自动重试（requeue + 指数退避）

- 满足「瞬时分类 ∧ 无写操作证据 ∧ attempts < 上限」时，任务回 queued 重跑：
  status='queued'、started_at=NULL、runtime_id=0、attempts=attempts+1，
  并复位 stream_ended=0 / self_reported=0 / exit_code / result_text（防 cleanup 双信号判死误收）。
- 指数退避：延迟 = min(base × 2^(attempts), 300s)，base 默认 10s（GRAPE_TRANSIENT_RETRY_BASE），
  通过 threading.Timer 延迟执行 requeue；退避窗口内任务保持 running（远小于 30min/6h 僵尸阈值，安全）。
- 上限默认 3（GRAPE_TRANSIENT_RETRY_MAX），复用既有 attempts 列，无 schema 变更。

## R52 幂等/副作用闸门（零副作用放大）

- 写操作证据 = 结构化产出（actions+conclusion）或 tool_uses 中有真实写操作
  （POST /comments、POST /pulls、A2A handoff）——复用假完成硬门槛（event_pipeline evaluate 路径）的同一套证据检测。
- 有写操作证据的任务失败一律不自动重试（可能已发评论/PR，重跑=重复提交），留给人工。
- 重试本身走终态幂等契约：只拦截 running 状态任务；已终态任务跳过（与 #369/fail_task 既有幂等保护一致）。

## R53 用户可见提示与诊断

- 重试触发时（两路透出）：
  - task:retry 实时事件（借鉴 PR #573）：经 report_messages → task_messages
    落库 + events.agent_logs 追加 + SSE fanout，前端实时日志面板 ↻ 橙色渲染，
    文案「上游瞬时抖动（分类），正在自动重试（第 N/M 次）；原因: …」；
  - agent_tasks.errors 追加 {"error_code":"TRANSIENT_RETRY","attempt":N}，
    events 回 pending（复用 requeue_task 语义）。
- 重试耗尽后判死：errors 追加 {"error_code":"TRANSIENT_RETRY_EXHAUSTED","attempts":N}；
  失败 result 写分类失败摘要（借鉴 PR #573 format_stream_failure_result 形态）：
  「❌ 上游瞬时抖动（分类），自动重试 N/M 次后仍失败；原因: …；exit_code=…；
  原始错误: …」，配置类错误明确「未自动重试」（worker 路径 fail_task、
  direct 路径 event_pipeline 收尾均接入）。
- 配置常量集中在 src/config.py（GRAPE_TRANSIENT_RETRY_MAX/BASE，env 可覆盖）。

## 不在范围

- ACP 引擎层（hwcloud_acp/hermes_acp）内部会话级重发——引擎无法感知副作用边界，属 49 次 requeue 污染的翻版，明确不做。
- cleanup.py 兜底判死路径（6h/30min/双信号）不改——它们处理的是真僵尸，重试任务在退避窗口（≤300s）内不会被它们命中。
- #399 的 120s 排队等待超时上调——属排队层参数，另单处理（超时错误文本已纳入瞬时分类，可被本重试覆盖）。
- 前端 UI 大改（新页面/新接口）不做；既有渲染点的小适配在范围内：events.js 两处日志面板为
  task:retry 增 ↻ 橙色图标、详情页 msg 字段回退读 data（外审第 9 轮补）。

## 场景与场景用例（7 个场景）

S1【核心修复】瞬时失败自动重试（UC1-1..UC1-4）
- UC1-1 IAM/连接类错误 + 零产出 + 零写操作 → requeue，attempts+1，事件 pending + 重试提示。
- UC1-2 exit 0 空输出（devops 假完成降级）→ 同上重试。
- UC1-3 退避指数递增（10s/20s/40s，封顶 300s），attempts 达上限后不再 requeue。
- UC1-4 耗尽后判死：failed + TRANSIENT_RETRY_EXHAUSTED + 分类 + 摘要。

S2【防回归守卫】不该重试的绝不重试（UC2-1..UC2-4）
- UC2-1 401/token 过期（auth_config）→ 不重试，直接判死。
- UC2-2 普通业务失败（exit 1，非瞬时特征）→ 不重试。
- UC2-3 有写操作证据（tool_uses 含 POST comments）或结构化产出 → 不重试。
- UC2-4 已终态任务（failed/cancelled）收到失败上报 → 跳过（幂等）。

S3【兼容/回归】既有机制不受影响（UC3-1..UC3-3）
- UC3-1 成功任务（exit 0 有产出）路径零变化。
- UC3-1a github/gitcode 两条 direct 执行路径行为一致（重试在 task 终态收敛点，与 provider 无关）。
- UC3-2 requeue 后字段复位（stream_ended/self_reported/exit_code/result_text）不触发 cleanup 双信号判死。
- UC3-2b requeue 归零 runtime_id 时回填 pool_tag/pool_agent_type（空则取 agent_type，
  Python 侧计算非 SQL CASE）：legacy 定向任务转可认领池任务，防「池/定向两路
  都认领不到 → 永久死任务」（claim 兜底回收同款教训，service.py L1046-1072）；
  plugin 任务已带 tag，回填 no-op。
- UC3-3 环境变量缺省值正确（MAX=3、BASE=10、CAP=300）。

S4【QA 加固守卫】worker 路径证据回查与误分类防护（UC4-1..UC4-3，来自 MR QA 审查）
- UC4-1 worker 上报路径（fail_task/complete_task 无 tool_uses 变量）调用时，
  回查 events.agent_logs 中 worker 落库的 tool_use 条目补全写操作证据：
  已 POST 评论/PR/A2A → 不重试；只读操作 → 照常重试；无 event_seq → 跳过回查。
- UC4-2 误分类防护（特征收窄）：裸 timeout/eof/iam 高频词不再单独命中瞬时分类
  （防普通业务失败误重试）；带网络/流/鉴权上下文佐证的形态仍正确命中。
- UC4-3 证据提取统一：has_write_evidence 复用 event_store._extract_tool_command
  （读 input.command），与假完成硬门槛共用同一套证据提取——
  「R52 同一套证据检测」严格成立（worker 上报的 tool_use 形态是
  {tool, input:{command}}，旧 cmd/values 兜底会漏检）。

S5【外部审查加固】降级路径可观测性（UC5-1，来自外部审查报告发现 1）
- UC5-1 退避窗口内任务被并发路径写终态（人工取消/回收判死/停机标记）→
  requeue 原子 no-op，但用户已看到「正在自动重试」提示 → 补降级证据：
  log.warning + agent_tasks.errors 追加 {"error_code":"TRANSIENT_RETRY_SKIPPED",
  "scheduled_attempt":N,"reason":含终态值+第N次}；
  安全论据（对终态任务 errors 裸 UPDATE，新开先例非既有模式）：errors 是纯
  记录字段不迁移状态、不经 update_task_status 终态幂等门、db_lock 内读改写；
  不碰 events（update_event_complete 终态互斥无追加通道，且不绕过）。
  正常 requeue 不产生该记录。

S6【防回归守卫】worker 发布包依赖完整性（UC6-1，处置外部审查发现 3）
- UC6-1 transient_retry 的模块级依赖（config/session_manager）与函数级延迟
  import（event_store._extract_tool_command、token_service.is_auth_401_error）
  必须全部落在 worker 发布闭包（src/worker_package.py MANIFEST / release_pack.py）
  内——防 token_service 式漏包在 worker 侧 import 期崩。实测：`src/runtime/`
  为 MANIFEST 整目录条目（随目录打包）+ 依赖模块逐一在列 + 五处接入点的
  延迟 import 均有 try/except 兜底（缺包时降级为不重试、走原判死路径，
  不崩）。守卫测试锁住该依赖集，防未来打包清单改为文件枚举时漏检。

S7【部署实测】目标环境全流程验证（UC7-1..UC7-2，2026-10-26 本机 dev）
- UC7-1 七场景真实代码链路验证（部署 b2cfbb0d9，uvicorn 3000/8080 + nginx
  三路 health OK，MySQL 后端）：V1 瞬时失败不判死→requeue（attempts+1/事件
  pending/pool_tag 回填）；V2 task:retry 真实落 events.agent_logs；V3 401
  不重试+分类摘要；V4 写证据闸门不重试；V5 耗尽判死+EXHAUSTED+摘要
  （❌ 上游瞬时抖动…3/3 次）；V6 SKIPPED+scheduled_attempt；V7 成功路径
  零变化。验证走 pytest + 临时脚本（conftest throwaway DB，绝不触生产
  MySQL；脚本验收后删除，不入库）。
- UC7-2 部署实测抓到的 2 个真缺陷及修复（单测未覆盖面，commit f1c08584b）：
  ① fail_task 判死时 errors 整体覆盖 → 洗掉 TRANSIENT_RETRY_EXHAUSTED
  诊断（#369 同族问题的任务侧翻版）→ 改为合并保留（去重 + [-5:] 截断）；
  ② EXHAUSTED 条目漏写 max 字段 → 分类摘要回退 len(_retries) 显示 3/2
  → 补 attempts+max 双字段。修复后 V1-V7 复跑全过、二次重启部署、
  相邻回归全绿（多轮实测：72-116 不等，按当轮选择的套件组合）。
- UC7-3（外审第 5 轮实测发现，commit 7a4117e26）errors 截断按信息价值排序：
  fail_task 合并写入（本次失败原因在首位）后，NO_TOKEN_AVAILABLE 段的
  两段式追加 + 裸 [-5:] 会把首位挤掉——5 条满载 + 无 token 场景最终
  errors = [hist0-3, NO_TOKEN]，本次失败原因完全丢失（与合并的宣称意图
  相悖）。根因是 NO_TOKEN 段截断语义，非合并逻辑（合并本身正确）。
  修复：NO_TOKEN 截断改首条保护式（[:1] + [1:][-4:]）——本次失败原因
  永不截断，截的是最旧历史。信息价值序：本次错误 > TRANSIENT 诊断 >
  NO_TOKEN > 其他历史。
- UC7-4（外审第 6 轮 P1-1，commit 073d937e4）兜底错误文本过滤自污染：
  worker 上报路径的 errors 兜底读取会把旧 TRANSIENT_RETRY 记录（reason
  含瞬时特征词如 "connection reset"）拼进分类文本 → 第二轮普通业务失败
  （exit 1 + 调用方空文本）被误分类为瞬时、盲重试到耗尽（已复现：
  修复前 retry_transient_task 对普通失败返回 True）。修复：兜底过滤
  error_code 以 TRANSIENT_RETRY 开头的记录。
- UC7-5（外审第 6 轮 P1-2，commit 073d937e4）requeue 重置 created_at：
  sweeper 按 created_at 判 queued 180min 排队超时（cleanup.py L396），
  3h+ 长命任务瞬时失败重试回 queued 后保留旧 created_at → 立即被
  「排队超时」判死。修复：requeue 刷新 created_at。
- UC7-5b（外审第 8 轮，commit 11c06f90a）created_at 刷新值必须 UTC ISO
  （双后端通用）：初版用 time.strftime(localtime) 裸本地格式——与全库
  正规写法（UTC ISO aware）分裂，且 MySQL 列默认写服务器时区、SQLite
  写 UTC，本地时区串制造第二种分叉。实测下游影响：总时长统计静默丢失
  （aware-naive TypeError 被 except:pass 吞）、stale-queued SQL 字符串
  比较立即命中（' '<'T' 排序怪癖）、created_at 排序查询错位。修复：
  datetime.now(timezone.utc).isoformat()——纯 Python 侧预计算 + 参数化
  传递，无方言函数，SQLite/MySQL 收到同一字符串（标准 SQL 设计原则）。
  UC7-5b 断言锁契约：fromisoformat 可解析 + tzinfo 非空 + T 分隔符。
- UC7-6（外审第 7 轮 P0）fail_task 401 token 过期追加段同款首条保护：
  service.py L2755 裸 [-5:] 与 UC7-3 NO_TOKEN 段同病（5 条满载挤掉首位
  本次失败原因），本分支的 errors 合并让它真正可达。修复：同款首条
  保护式（[:1] + [1:][-4:]）。
- UC7-8（全家族扫描，commit abf3fc322）剩余三处裸 [-5:] 同病同修：COMMENT_POST_FAILED
  / NO_TOKEN 第二处（fail_task 401 分支 L2877，与 L2679 不同代码块）/ RESULT_POST_FAILED
  （direct 路径 L4111）——统一首条保护式；全库裸截断病灶清零。
- UC7-9（外审第 9 轮）requeue 归档本轮 errors：旧行为每轮往 5 槽叠 TRANSIENT_RETRY
  记录（3 轮即满载，激活截断病灶），且旧诊断含瞬时词会反喂下一轮分类（P1-1 的
  放大器）。修复：requeue 时清空 errors、只留一条本轮重试标记（attempt=重试后
  计数 + 归档说明）；历史诊断在事件侧 agent_logs 持久保留。
- UC7-11（外审第 9 轮终审发现）归档声明与实现一致：UC7-9 的 note 声称
  「archived to event agent_logs」但原实现无归档动作（旧诊断随 5 槽清空
  丢弃，note 误导）。修复：requeue 事务内读旧 errors（过滤 TRANSIENT_RETRY*
  自身记录），事务成功后经 report_messages 真实链路把旧诊断归档到事件侧
  agent_logs（「重试轮次归档（前轮诊断）」条目），清空不再丢数据。
- UC7-12（「值得换」终评场景 2）裸关键词收窄：\b50[234]\b 不再单独命中（防
  issue/PR 编号误判），要求 HTTP 上下文（http/status_code/response/exit 或
  bad gateway/service unavailable 后缀）；overloaded/capacity 不再单独命中，
  要求 upstream/provider/model/engine/api 佐证或 server overloaded + retry/
  requests 语境。带上下文的 5xx/过载仍正确命中。5 个反例 + 4 个正例参数化。
- UC7-10（外审第 9 轮）check_running_task 补查 queued：退避窗口内任务回 queued，
  原查询只认 running/dispatched → 同 issue 新事件建第二任务 → 双实例并发、
  双份评论。修复：'queued' 计入活跃（语义=「该 issue 有任务在链上」）。
- UC7-7（外审第 7 轮 P1）_do_requeue 主事务 try/except（Timer 线程安全）：
  原主事务体裸奔在 Timer 线程——DB 异常（断连/锁超时）会让任务永久卡
  running、无任何记录、静默丢失。修复：主事务全程 try/except，异常时
  log.warning(exc_info) + errors 裸 UPDATE 落 TRANSIENT_RETRY_SKIPPED
  （不走 _append_task_error——它用同一把 db_lock，锁级故障同样失败；
  裸 UPDATE 用新游标最小依赖，list 形态与 errors 列契约一致），
  任务由既有僵尸回收兜底（30min/6h）。

## 验收清单（对应 Issue #403）

- [x] IAM 瞬断/流中断/exit 0 空输出 → 自动重试默认 3 次、指数退避，重试期间不置 failed（UC1-1/1-2/1-3，单测覆盖）
- [x] 耗尽后判死，含错误分类、重试次数、原始错误摘要（UC1-4）
- [x] 非瞬时错误不盲目重试（UC2-1/2-2）
- [x] 事件流可见「自动重试（第 N/M 次）」；最终失败区分抖动 vs 配置错误（R53）
- [x] 幂等保护：写操作证据闸门 + 终态幂等（UC2-3/2-4）
- [x] 重试策略可配置（环境变量，UC3-3）
- [x] 目标环境实测（2026-10-26 本机 dev 部署 b2cfbb0d9→f1c08584b，V1-V7 全过，见 S7）

## 实测核验（代码位置）

- 判死收敛点：src/runtime/service.py complete_task（L2065 状态分流）、fail_task（L2512）、
  src/api/event_pipeline.py direct 执行收尾（L4021 终态写入前）。
- 假完成硬门槛（空输出降级）：event_pipeline.py L3978-4003。
- 49 次 requeue 历史包袱（为何默认无重试）：service.py L2062-2063 注释。
- attempts 列既有用法（超时回池 attempts>=3 判死）：service.py L997-1084、L3063。
- requeue 语义（事件回 pending + 槽位释放）：service.py requeue_task L1769。

## 场景用例代码（tests/test_transient_retry.py，62 用例全绿，随审查轮次增长）

Mock 策略：不触真实 OBS / 远端 VCS / 真实引擎。分类与闸门用纯函数直测；
fail_task / complete_task 接入用真实函数 + conftest 测试 SQLite；
direct 路径集成 mock 最外层边界 stream_devops_agent（假引擎流回放），
用量采集替身 _FakeUsage、workdir 物化/清理打桩，其余（任务/事件落库、
收尾判定、假完成降级、重试收口）全部走真实代码链。threading.Timer 以
_FakeTimer 替身捕获（记录退避延迟，不启动真线程）。

共享测试基座（节选）：

    TID = "t_403_retry"; REPO = "gitcode:issue403/test"

    class _FakeTimer:                      # 替身 Timer：记录 (delay, args)，不启动
        fired = []
        def __init__(self, delay, fn, args=None, kwargs=None):
            self.delay, self.fn, self.args = delay, fn, args or ()
        def start(self): _FakeTimer.fired.append((self.delay, self.args))

    def _seed_task(status="running", attempts=0, errors=None, event_seq=None,
                   stream_ended=0, self_reported=0, result_text=""):
        # INSERT agent_tasks：issue 类任务，attempts/stream_ended/self_reported 可控

    def _seed_direct(repo_key):            # direct 路径：append_event + running 任务
        seq = append_event(repo_key, "issue_opened", title="issue403 direct")
        # INSERT agent_tasks ... prompt='p'（预填 → 走复用路径，跳过 prompt 重建）

    def _run_direct(repo_key, tid, seq, events, agent_type="issue"):
        # patch stream_devops_agent（假引擎流）/ master_usage.for_task（替身）/
        #      _materialize_direct_workdir（透传）/ _cleanup_direct_workdir（no-op）
        # → event_pipeline._execute_agent_inner(...)  真实收尾链

S1 核心修复用例：

    def test_classify_transient(txt):                     # UC1-1 分类
        r = tr.classify_stream_failure(-1, txt, has_output=False)
        assert r["transient"] and r["error_class"] == tr.TRANSIENT
        # 参数化：IAM token / connection reset / timed out / 502 / stream aborted

    def test_retry_scheduled_on_transient(monkeypatch):   # UC1-1 收口
        _seed_task()
        assert tr.retry_transient_task(TID, -1, "connection reset by peer") is True
        assert _FakeTimer.fired and _FakeTimer.fired[0][0] == 10   # base 10s 退避
        assert _task_row()[0] == "running"                        # 未判死
        # errors 含 {"error_code": "TRANSIENT_RETRY", "attempt": 1}

    def test_direct_transient_failure_retries_gitcode():  # UC1-1 direct 全链路
        seq = _seed_direct(REPO)
        _run_direct(REPO, TID, seq, [
            ("text", "partial"),
            ("done", {"exit_code": -1, "text": "", "errors": ["connection reset by peer"], ...})])
        assert _task_row()[0] == "running"

    def test_classify_exit0_empty_output():               # UC1-2 分类
        r = tr.classify_stream_failure(0, "", has_output=False)
        assert r["transient"] and r["error_class"] == tr.EMPTY_OUTPUT

    def test_direct_exit0_empty_output_retries():         # UC1-2 direct 全链路
        # done exit_code=0 text="" → 假完成硬门槛降级 failed → 重试收口 → running
        assert _task_row()[0] == "running"
        # errors 含 TRANSIENT_RETRY 且 error_class == EMPTY_OUTPUT

    def test_backoff_delay_sequence_and_env(monkeypatch): # UC1-3 / UC3-3
        assert [tr._backoff_delay(n) for n in (0, 1, 2)] == [10, 20, 40]
        assert tr._backoff_delay(10) == 300                # 封顶
        monkeypatch.setenv("GRAPE_TRANSIENT_RETRY_MAX", "1"); assert tr.retry_max() == 1

    def test_retry_exhausted(monkeypatch):                # UC1-4 耗尽诊断
        monkeypatch.setenv("GRAPE_TRANSIENT_RETRY_MAX", "3")
        _seed_task(attempts=3)
        assert tr.retry_transient_task(TID, -1, "IAM token refresh failed") is False
        # errors 含 {"error_code": "TRANSIENT_RETRY_EXHAUSTED", "attempts": 3,
        #            "error_class": "upstream_transient", "reason": ...}；任务留 running
        # （终态判死由调用方收敛——见 test_fail_task_auth_401_still_fails 形态）

S2 防误重试用例：

    def test_classify_auth_401_not_transient():           # UC2-1 分类
        r = tr.classify_stream_failure(-1, "API returns 401 unauthorized, token expired", ...)
        assert not r["transient"] and r["error_class"] == tr.AUTH_CONFIG

    def test_fail_task_auth_401_still_fails():            # UC2-1 接入
        _seed_task(); svc.fail_task(TID, "401 unauthorized: token expired", exit_code=-1)
        assert _task_row()[0] == "failed"                 # 不重试，直接判死

    def test_classify_plain_failure_not_transient():      # UC2-2 分类
        r = tr.classify_stream_failure(1, "tests failed: 2 errors", has_output=True)
        assert not r["transient"] and r["error_class"] == tr.TASK_FAILURE

    def test_direct_plain_failure_still_failed():         # UC2-2 direct 全链路
        # done exit_code=1 errors=["2 test errors"] → 照旧 failed

    def test_no_retry_with_write_evidence():              # UC2-3 写操作证据
        assert tr.retry_transient_task(TID, -1, "connection reset",
            tool_uses=[{"cmd": "curl -X POST .../issues/403/comments"}], result_text="") is False

    def test_no_retry_with_structured_output():           # UC2-3 结构化产出
        out = json.dumps({"actions": [...], "conclusion": "done"})
        assert tr.retry_transient_task(TID, -1, "connection reset", result_text=out) is False

    def test_direct_write_evidence_no_retry():            # UC2-3 direct 全链路
        # tool_use 含 POST comments → failed，errors 无 TRANSIENT_RETRY

    def test_no_retry_on_terminal_status():               # UC2-4 终态幂等
        _seed_task(status="failed")
        assert tr.retry_transient_task(TID, -1, "connection reset") is False

S3 兼容/回归用例：

    def test_complete_task_success_untouched():           # UC3-1 成功路径零变化
        _seed_task(); svc.complete_task(TID, result_text="all good", exit_code=0)
        assert _task_row()[0] == "completed"

    def test_direct_transient_retries_github_provider_parity():  # UC3-1a
        gh_repo = "github:issue403/test"; seq = _seed_direct(gh_repo)
        # github: 前缀仓库 direct 路径 → 与 gitcode: 同样 running + TRANSIENT_RETRY

    def test_do_requeue_resets_signals_and_event():       # UC3-2 字段复位
        seq = append_event(REPO, "issue_opened", title="issue403")
        _seed_task(stream_ended=1, self_reported=1, result_text="partial", event_seq=seq)
        tr._do_requeue(TID)
        # status=queued, attempts=1, stream_ended=0, self_reported=0,
        # exit_code=NULL, result_text=''，事件回 pending

worker 上报路径接入（S1/S2 在 service 层的端到端）：

    def test_fail_task_triggers_retry_not_failed():       # UC1-1 fail_task 收口
        _seed_task(); svc.fail_task(TID, "connection reset by peer", exit_code=-1)
        assert _task_row()[0] == "running"                # 未判死

    def test_complete_task_transient_failure_retries():   # UC1-1 complete_task 收口
        # 任务 errors 预存 "stream error: connection reset"（兜底读取路径）
        svc.complete_task(TID, result_text="", exit_code=-1)
        assert _task_row()[0] == "running"

### UC ↔ 测试映射表

| 场景 | 用例 | 测试函数 | 状态 |
|---|---|---|---|
| S1 | UC1-1 | test_classify_transient / test_retry_scheduled_on_transient / test_fail_task_triggers_retry_not_failed / test_complete_task_transient_failure_retries / test_direct_transient_failure_retries_gitcode | ✅ |
| S1 | UC1-2 | test_classify_exit0_empty_output / test_direct_exit0_empty_output_retries | ✅ |
| S1 | UC1-3 | test_retry_scheduled_on_transient（退避 timer）/ test_backoff_delay_sequence_and_env | ✅ |
| S1 | UC1-4 | test_retry_exhausted | ✅ |
| S2 | UC2-1 | test_classify_auth_401_not_transient / test_fail_task_auth_401_still_fails | ✅ |
| S2 | UC2-2 | test_classify_plain_failure_not_transient / test_direct_plain_failure_still_failed | ✅ |
| S2 | UC2-3 | test_no_retry_with_write_evidence / test_no_retry_with_structured_output / test_direct_write_evidence_no_retry | ✅ |
| S2 | UC2-4 | test_no_retry_on_terminal_status | ✅ |
| S3 | UC3-1 | test_complete_task_success_untouched | ✅ |
| S3 | UC3-1a | test_direct_transient_retries_github_provider_parity | ✅ |
| S3 | UC3-2 | test_do_requeue_resets_signals_and_event | ✅ |
| S3 | UC3-3 | test_backoff_delay_sequence_and_env | ✅ |
| 部署后 | 真实 IAM 瞬断注入 / 3 轮真实退避全链路 / 双主 scp 并发重试 | （非单测材料，按验收清单在目标环境执行） | ⏳部署后验收 |
| R53 增强 | task:retry 实时透出 | test_retry_notify_emits_task_retry_event | ✅ |
| R53 增强 | 分类失败摘要（worker 路径） | test_format_stream_failure_result_transient / test_format_stream_failure_result_auth_config / test_fail_task_exhausted_writes_classified_summary | ✅ |
| R53 增强 | config 常量集中 | test_config_constants_module_level | ✅ |
| S4 | UC4-1 | test_no_retry_when_agent_logs_has_write_evidence / test_retry_when_agent_logs_no_write_evidence / test_no_retry_when_agent_logs_has_a2a_handoff / test_no_agent_logs_fallback_no_event_seq | ✅ |
| S4 | UC4-2 | test_classify_plain_timeout_not_transient / test_classify_plain_iam_not_transient / test_classify_network_timeout_still_transient / test_classify_iam_token_still_transient | ✅ |
| S4 | UC4-3 | test_write_evidence_via_input_command / test_write_evidence_input_command_no_write | ✅ |
| S3 扩展 | UC3-2b | test_do_requeue_backfills_pool_tag_for_legacy_task / test_do_requeue_keeps_existing_pool_tag | ✅ |
| S5 | UC5-1 | test_do_requeue_records_skipped_when_terminal_during_backoff / test_do_requeue_no_record_when_requeue_succeeds | ✅ |
| S6 | UC6-1 | test_worker_manifest_covers_transient_retry_deps | ✅ |
| S7 | UC7-1 | （部署实测 V1-V7，临时脚本已删不入库；核心断言同 S1-S6 用例） | ✅实测 |
| S7 | UC7-2 | 修复由 f1c08584b 携带；核心断言：耗尽后 errors 保留 EXHAUSTED dict 条目、摘要显示 N/N（复验于部署环境） | ✅实测 |
| S7 | UC7-3 | test_fail_task_error_priority_keeps_current_failure | ✅ |
| S7 | UC7-4 | test_stale_transient_record_does_not_pollute_classification | ✅ |
| S7 | UC7-5 | test_do_requeue_resets_created_at（含 UC7-5b UTC ISO 格式断言） | ✅ |
| S7 | UC7-6 | test_fail_task_401_append_keeps_current_failure | ✅ |
| S7 | UC7-7 | test_do_requeue_db_error_leaves_observable_record | ✅ |
| S7 | UC7-8 | test_remaining_append_blocks_keep_head_entry（COMMENT_POST_FAILED / NO_TOKEN 第二处 / RESULT_POST_FAILED 三块首条保护） | ✅ |
| S7 | UC7-9 | test_do_requeue_clears_prior_round_errors | ✅ |
| S7 | UC7-10 | test_check_running_task_counts_queued | ✅ |
| S7 | UC7-11 | test_do_requeue_archives_prior_errors_to_agent_logs | ✅ |
| S7 | UC7-12 | test_classify_bare_number_not_transient / test_classify_contextual_5xx_still_transient | ✅ |

注：部署类用例（双进程/集群/真实引擎注入）不是单测材料——按上方验收清单在
目标环境执行；验收工具不入库（临时脚本放仓库外）。

R53 增强（借鉴 PR #573，同 issue 竞品实现评审结论）：task:retry 实时事件
（report_messages 白名单 + agent.py worker 日志 + events.js 前端 ↻ 橙色）、
format_stream_failure_result 分类失败摘要、config.py 集中常量。不采纳其
流内重试架构（副作用闸门对「中断前已执行写操作」场景有缺口，判定层
requeue 闸门更严）。

R53 增强用例代码（借鉴 PR #573 移植，commit 4390d3e91）：

    def test_format_stream_failure_result_transient():    # 分类摘要（耗尽）
        s = tr.format_stream_failure_result(
            tr.TRANSIENT, 3, 3, "connection reset by peer", exit_code=-1,
            errors=["stream error"])
        assert "上游瞬时抖动" in s and "3/3" in s
        assert "connection reset" in s and "exit_code=-1" in s

    def test_format_stream_failure_result_auth_config():  # 分类摘要（配置错误）
        s = tr.format_stream_failure_result(tr.AUTH_CONFIG, 0, 3, "token expired")
        assert "配置类错误" in s and "未自动重试" in s

    def test_retry_notify_emits_task_retry_event():       # task:retry 实时透出
        # 任务带 event_seq → retry_transient_task 触发 →
        # report_messages 把 task:retry 落入 events.agent_logs
        assert tr.retry_transient_task(TID, -1, "connection reset by peer") is True
        # events.agent_logs 含 {"type": "task:retry", "data": "...第 1/3 次..."}

    def test_config_constants_module_level():             # config 集中常量
        import src.config as cfg
        assert cfg.GRAPE_TRANSIENT_RETRY_MAX == 3
        assert cfg.GRAPE_TRANSIENT_RETRY_BASE == 10

    def test_fail_task_exhausted_writes_classified_summary(monkeypatch):  # 端到端
        # MAX=0 直接耗尽 → fail_task 判死 → 事件 result 为「❌ 上游瞬时抖动…」
        monkeypatch.setattr(cfg, "GRAPE_TRANSIENT_RETRY_MAX", 0)
        svc.fail_task(TID, "connection reset by peer", exit_code=-1)
        assert "上游瞬时抖动" in event_result and "connection reset" in event_result

S4 QA 加固守卫用例（来自 MR QA 审查修复，commit 695176223）：

    def _seed_event_with_agent_logs(seq, logs_json):    # S4 共享基座
        # 向 events.agent_logs 写入 JSON 日志（模拟 worker report_messages 落库）

    def test_no_retry_when_agent_logs_has_write_evidence():   # UC4-1
        # tool_uses 为空但有 event_seq，agent_logs 含 POST comments → 不重试
        seq = append_event(REPO, "issue_opened", title="agent_logs_write")
        _seed_task(event_seq=seq)
        _seed_event_with_agent_logs(seq, json.dumps([
            {"type": "tool_use", "data": {"tool": "bash",
             "input": {"command": "curl -X POST .../repos/x/y/comments"}}}]))
        assert tr.retry_transient_task(TID, -1, "connection reset by peer",
                                       tool_uses=None) is False

    def test_retry_when_agent_logs_no_write_evidence():       # UC4-1
        # agent_logs 只有只读操作（git push 之外的读/查询）→ 照常重试

    def test_no_retry_when_agent_logs_has_a2a_handoff():       # UC4-1
        # agent_logs 含 grape a2a handoff → 不重试

    def test_no_agent_logs_fallback_no_event_seq():            # UC4-1 边界
        # 无 event_seq → 跳过回查，不抛错

    def test_classify_plain_timeout_not_transient():           # UC4-2 收窄
        # 裸 "timeout" / "operation timed out"（无网络上下文）→ 不重试
        r = tr.classify_stream_failure(1, "operation timeout after 3 retries", has_output=True)
        assert not r["transient"]

    def test_classify_plain_iam_not_transient():               # UC4-2 收窄
        # 裸 "iam"（无 token/auth/error 上下文）→ 不重试

    def test_classify_network_timeout_still_transient():       # UC4-2 不漏
        # "connection ... timeout" / "read timeout" 带网络佐证 → 仍可重试

    def test_classify_iam_token_still_transient():             # UC4-2 不漏
        # "IAM token request failed" / "iam auth error" → 仍可重试

    def test_write_evidence_via_input_command():               # UC4-3 统一
        tu = {"tool": "bash", "input": {"command": "curl -X POST .../comments"}}
        assert tr.has_write_evidence([tu], "") is True   # 经 _extract_tool_command 提取

    def test_write_evidence_input_command_no_write():          # UC4-3 统一
        tu = {"input": {"command": "git push origin feat/issue-403"}}
        assert tr.has_write_evidence([tu], "") is False

S5 外部审查加固用例（降级可观测性，transient_retry._do_requeue else 分支）：

    def test_do_requeue_records_skipped_when_terminal_during_backoff():  # UC5-1
        # 退避窗口内任务被写终态（模拟人工取消：status='cancelled'）
        seq = append_event(REPO, "issue_opened", title="skipped403")
        _seed_task(event_seq=seq)
        # UPDATE agent_tasks SET status='cancelled' / events SET status='cancelled'
        tr._do_requeue(TID)                       # WHERE status='running' 不命中 → no-op
        assert _task_row()[0] == "cancelled"      # 终态不被覆盖（幂等）
        # errors 追加 TRANSIENT_RETRY_SKIPPED，reason 含终态值
        assert any(e.get("error_code") == "TRANSIENT_RETRY_SKIPPED"
                   and "cancelled" in str(e.get("reason")) for e in _task_errors())
        assert ev_status == "cancelled"           # 事件终态不被触碰

    def test_do_requeue_no_record_when_requeue_succeeds():              # UC5-1 反例
        _seed_task(); tr._do_requeue(TID)         # 正常 requeue
        assert _task_row()[0] == "queued"
        # 无 TRANSIENT_RETRY_SKIPPED 记录

S6 防回归守卫用例（worker 发布包依赖完整性，处置外部审查发现 3）：

    def test_worker_manifest_covers_transient_retry_deps():             # UC6-1
        manifest_src = open("src/worker_package.py", encoding="utf-8").read()
        entries = set(re.findall(r'"(src/[^"]+)"', manifest_src))
        # src/runtime/ 整目录条目 → 本体随目录打包；依赖模块逐一在列；
        # 未来清单若改为文件枚举，本测试逼维护者显式补 transient_retry.py
        for d in ["src/config.py", "src/session_manager.py",
                  "src/api/event_store.py", "src/token_service.py"]:
            assert d in entries
        assert "src/runtime/" in entries or "src/runtime/transient_retry.py" in entries

S7 部署实测用例（目标环境，脚本不入库——断言形态记录于此供复验）：

    # V5 核心（UC7-2 ①）：耗尽判死后 errors 保留结构化诊断（合并非覆盖）
    _seed(attempts=3); svc.fail_task(TID, "connection reset by peer", exit_code=-1)
    assert _task_row()[0] == "failed"
    assert any(isinstance(e, dict) and e.get("error_code") == "TRANSIENT_RETRY_EXHAUSTED"
               for e in _task_errors())          # 旧版：被 [str(error)] 覆盖丢失

    # V5 核心（UC7-2 ②）：摘要显示 N/N（EXHAUSTED 带 max 字段）
    ev_result == "❌ 上游瞬时抖动（…），自动重试 3/3 次后仍失败；原因: …"
    # 旧版：3/2（max 缺失回退 len(_retries)）

    # V1（UC7-1）：真实 fail_task 上报 → 不判死 → requeue → attempts/pool_tag
    # V2：task:retry 经 report_messages 真实落 events.agent_logs
    # V3/V4/V6/V7：401 不重试、写证据不重试、SKIPPED 带 scheduled_attempt、
    #             成功路径零变化——断言同 S2/S4/S5 单测形态

UC7-3 回归用例（errors 截断信息价值序，外审第 5 轮实测复现转正）：

    def test_fail_task_error_priority_keeps_current_failure():
        # 5 条历史 + 空 token（触发 NO_TOKEN 追加）满载场景
        _seed_errors(["hist0", "hist1", "hist2", "hist3", "hist4"])
        svc.fail_task(TID, "new failure reason", exit_code=-1)
        errs = json.loads(final_errors)
        assert errs[0] == "new failure reason"   # 本次失败原因永远在首位
        assert any(e.get("error_code") == "NO_TOKEN_AVAILABLE" for e in errs)
        assert len(errs) == 5                    # 截断挤掉的是最旧历史（hist0）
        # 修复前实况：errs = [hist0..3, NO_TOKEN] —— 本次原因完全丢失

UC7-4/UC7-5 回归用例（外审第 6 轮 P1-1/P1-2 复现转正）：

    def test_stale_transient_record_does_not_pollute_classification():  # UC7-4
        # errors 里带上一轮 TRANSIENT_RETRY 记录（reason 含瞬时词）+
        # 本次普通业务失败（exit 1 + 空文本 → 走兜底读取）
        _seed_task(errors=[{"error_code": "TRANSIENT_RETRY", "attempt": 1,
                            "reason": "connection reset by peer"}])
        assert tr.retry_transient_task(TID, 1, "") is False
        # 修复前：True（旧记录污染分类 → 盲重试到耗尽）

    def test_do_requeue_resets_created_at():                            # UC7-5
        # created_at 置 2020（模拟 3h+ 长命任务）→ requeue → 刷新
        c.execute("UPDATE agent_tasks SET created_at='2020-01-01 00:00:00' ...")
        tr._do_requeue(TID)
        assert row[0] == "queued" and row[1] > "2026-"   # created_at 已刷新
        # 修复前：保留 2020 → queued sweeper 180min 超时立即判死
        # UC7-5b：格式契约（双后端通用）
        from datetime import datetime as _dt
        _parsed = _dt.fromisoformat(r[1])
        assert _parsed.tzinfo is not None    # UTC ISO aware（非本地裸格式）
        assert "T" in r[1]                   # T 分隔符（防 ' '<'T' 排序怪癖）
        # 旧版 time.strftime(localtime)：统计丢失 + stale-queued 提前命中

UC7-6/UC7-7 回归用例（外审第 7 轮 P0/P1）：

    def test_fail_task_401_append_keeps_current_failure():        # UC7-6
        # 5 条历史 + 401 token 过期（触发 401 追加段）满载场景
        _seed_errors(["hist0", ..., "hist4"])
        svc.fail_task(TID, "401 unauthorized: token expired", exit_code=-1)
        errs = json.loads(final_errors)
        assert errs[0] == "401 unauthorized: token expired"  # 首条保护
        assert len(errs) == 5

    def test_do_requeue_db_error_leaves_observable_record(monkeypatch):  # UC7-7
        class _BoomLock:                     # 锁替身：with 即抛 DB 异常
            def __enter__(self): raise RuntimeError("db connection lost")
            def __exit__(self, *a): return False
        monkeypatch.setattr(tr, "db_lock", _BoomLock())
        tr._do_requeue(TID)                  # 不抛异常（Timer 线程安全）
        assert row[0] == "running"           # 未 requeue
        assert any(e.get("error_code") == "TRANSIENT_RETRY_SKIPPED"
                   and "requeue transaction failed" in e.get("reason")
                   for e in _task_errors())  # 裸 UPDATE 兜底记录（非静默）
