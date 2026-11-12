> ⚠️ 本文为拆分分片（内容唯一权威源）。索引导航：./multi-platform-decouple-design.md
> 来源：multi-platform-decouple-design.md L914-L984, L1070-L1145, L1406-L1442, L1463-L2307（原文原样搬运，零改写）。
> 域定义：落地后清障与质量线——测试基建、枚举收口方法论、清障执行批次、续审修复。
> 全局约束：继承索引页「表设计铁律/实现纪律/验收纪律」（见索引页）。
> ⚠️ 阅读顺序：本分片 D63 与 04 分片交错（04 的 D53-62 在本片 D50-52 之后、D64 之前）——连续阅读该段按 **04(D53-62) → 本片(D63) → 04(D64)**；04/05 交错的归属依据见索引页「阅读与结构说明」。
> ⚠️ 拆分判据：本片为下一拆分候选（>~1200 行或续审批次再增 3 节以上时，按「清障方法论 D45-52/63」vs「清障执行+续审 D65-88」二分）。

### D45：project-info 后台刷新单飞——段错误专项·线程风暴治理（S67，先测后改，faulthandler 驱动）

现场（2026-11-04 全量 pytest ~43% faulthandler dump，复现稳定）：
- 崩溃线程 **Thread-944**（编号即积压量）在 `builder._write_to_obs`
  DB 写段 SIGSEGV，C 栈止于 libc（_cffi_backend 等 18 个扩展已加载）；
- 泄漏源头：event_pipeline 每个 webhook 事件裸起
  `Thread(maybe_refresh_project_info)`，每线程携带 20s 级网络超时
  （_fetch_branches/_read_meta 的 urllib）+ 共享 sqlite 写；webhook 事件
  灌入快于线程排空 → 存活线程持续堆积；
- 测试期 mock 连接换出/关闭（_mock_db fixture）与在飞线程的写操作
  构成 UAF 窗口，线程数放大命中概率（生产无连接换出，但线程风暴
  本身就是缺陷：内存/fd 压力 + 锁竞争）。

修复（零危害：跳过即返回，无删除无阻断）：
- **_spawn_project_info_refresh 单飞**：同 repo_key 在飞即跳过
  （webhook 风暴下重复触发本就该被 TTL 节流吸收）；
- **全局并发上限 4**（_PI_REFRESH_MAX_CONCURRENT）：刷新是幂等 TTL
  操作，超限跳过晚一轮无损失；
- **finally 释放槽位**：工作函数异常不泄漏登记项（S67 用例锁）；
- **teardown 排空接线（根因收口）**：第一轮验证（仅单飞）仍在 43%
  崩——faulthandler 第二现场给出决定性证据：主线程正在
  test_issue_453 的 _mock_db fixture teardown 里 `_conn.close()`，
  同一瞬间唯一的 pi-refresh 线程在 _write_to_obs 执行 DB 语句
  （换出的 mock sqlite 连接被并发 close → sqlite C 层 UAF）。
  修法：event_pipeline 提供公共助手 `wait_pi_refresh_drain(timeout)`
  （轮询在飞集合排空）；4 个同族 fixture（test_issue_453_outbound_ledger
  / test_issue_453_self_loop / test_issue_comment_resume_ledger /
  test_bot_opened_recommend，均为"换出+close 共享连接"模式）teardown
  关连接前排空，超时仅告警不阻断。

效果：任一时刻后台刷新线程 ≤4 且 teardown 与在飞写不并发——风暴与
UAF 窗口同步根治；生产语义不变（TTL 节流 + 分布式锁 acquire_lock
兜底仍在 builder 内）。
验收 = S67 用例（单飞/并发上限/槽位释放/异常释放）+ 定向 95 绿 +
全量 pytest 跑过 43% 崩点零 Fatal（两轮全量复现崩点为基线）。
剩余风险登记：其他换库 fixture 若引入新的后台 DB 写线程，仍需各自
接线排空；架构级根治（连接依赖注入/线程本地连接）归批次 1 评估。

### D46：混跑污染专项——模块缓存/DB 绑定污染源收口（S68，先测后改，第三/四轮全量对账驱动）

现场（第三轮全量：1430F+711E，定向单跑全绿）家族分组：
- **E×239 no such table**：换库泄漏——test_recover_orphans_slots 把
  `_sm.db` 换成只建 3 张表的内存库，窗口期懒加载首次 import 的模块
  永久捕获空库句柄（monkeypatch 只还原被点名的 4 个模块）；
- **E×56+F×75 app ImportError**：test_scenes_detail 每用例
  `_fresh_imports()` 清空全部 src.* 模块缓存 + `reload(main)`——
  全进程模块缓存/单例/DB 句柄绑定被反复推倒，是最大污染放大器
  （reload 中途异常留下残缺模块，后续 import app 全炸）；
- **database is locked / env 错开**：test_issue_337 永久改写
  DEVOPS_DB_PATH/DB_PATH 到私有库不恢复；另 6 个文件模块级设私有库
  env 不恢复（a2a_scene_check/knowledge_session_access/task_complete/
  transfer_bind/r7_no_email_config/issue_372_round2_audit），后续按
  env 重开连接的导入全部错开共享库。

修复（9 文件，零危害：恢复/重绑，不删不改断言）：
- scenes_detail：模块缓存清空改空操作 + 去 reload(main)（现有 app 直用）；
- issue_337：module teardown 恢复 env+config+reload+句柄重绑扫描；
- recover_orphans_slots：teardown 句柄重绑扫描（含锁，先重绑后 close）；
- 6 个 env 污染文件：module 级 autouse 恢复 fixture（恢复共享库路径）。

对账（第四轮 vs 第三轮，同 worktree 同基线）：E 711→440、F 1430→1250、
通过 4855→5306（原始消除 692；新增 220 大头为 E→F 转化——原 setup
ERROR 全跳的文件现在真正跑测试、撞上数据污染显形为 F，属修复中间形态）；
段错误连续四轮全量零复现。9 文件单跑逐文件与基线一致（337/task_complete/
372_round2 的单跑失败为自身既有问题）。

剩余挂账（S69 候选，另行批次）：E 残留 440 的污染源未定位完
（test_tasks 39 / issue_627 38 / skills_obs 28 为 top）；F 数据族
（共享库行级污染，测试间行互见）体量大，需按文件逐个加行清理/隔离；
架构级根治（每测试独立库/连接依赖注入）归远期。

### D50：全量枚举收口——方法论升级与 11 组确认洞（S75，先测后改）

为什么前几轮一直有遗漏（根因复盘，固化进纪律）：
- 形状扫描是"抽样 grep"而非"全量枚举"——检索不是对账，没有
  "总命中=确认洞+基线预存+误报"的计数闭环，漏掉的调用点不会浮出来；
- 实弹炸一个实例就修一个实例，同形状的完整调用面没有程序化枚举
  （S70 修建单链 3 处 int() 即收工，event_pipeline 14 处 S73 才收）；
- 未按"入站（webhook→方言→验签→归一→管道→建档→agent）+ 出站
  （token→provider→URL/body 形态→ident 回写）"两条链全流程走读，
  走读过的段修得干净，没走读的段全是盲区（chat/autopilot/scheduler/
  澄清评论/去重读评论全在盲区）。

本轮方法：五族程序化枚举（int(ident)/token 折叠/isdigit 门/SQL ident/
硬编码 URL），每族输出总命中数并逐条分类。确认洞 11 组（需求文档 S75
行全列），全部先测后改（守卫 7+行为 1）：
- 崩溃类：issues 列表/展示 int(number)×3 → _native_issue_number；
- 错 token 类：chat×4 + repos 移除流 + autopilot + scheduler →
  get_user_token_for_repo / get_repo_token_with_refresh；
- 功能拒掉类：管道 ISSUE_NUMBER 注入/cancelled 检查/SRE 回填/
  附件 prompt/IM 任务号与链接的 isdigit 门 → 非空校验+native；
- 硬编码 URL 类：澄清评论 else=gitcode、fail_task 去重读评论
  else=gitcode → VcsApiClient(provider)（台账随方言派生）。

误报与预存量登记（不修，防过度收口）：scenes.py /user 校验为显式
gitcode 登录名语义（设计如此）；issues_center 1889/1927/1974 为
gitcode provider 存量委托（D35 收敛后仅 gitcode 调用）；分页/用户号
isdigit 与 ident 无关。

对账：S75 用例 8 绿；涉改族（chat/im/issue/token 管道）基线 diff
零新增失败（5 预存量为 tooluse 环境依赖型）；语法破坏事故 1 起
（handler_async 正则替换括号失衡）单文件隔离复跑即暴露并修复——
机械批量替换后必须 ast.parse + 隔离复跑，此教训入纪律。

### D51：族B/族A 终收口——第三轮枚举与五族清零（S76，先测后改）

第三轮枚举（D50 方法论再执行）：github/else 二分字面量族、SQL/参数
ident int() 族、gitcode.com/api URL 残余族。

确认洞（收口）：
- 族B 真 ident 崩溃点 15 处——S75 守卫正则只匹配 int(_issue_num) 形态，
  本轮变量名各异的 15 处（_n/_num3/_ce_num/_sub_iss_num/num/
  parent_issue_number/issue_number 等）是守卫盲区（教训：守卫正则要按
  语义族枚举变量名，不是按已见过的变量名）。含 2 处 isdigit 守卫内
  int()——守卫不崩但拒 str ident 平台（同门族功能语义）。
- 族A 展示错派 4 处：chat/IM 401 提示与 issues 会话 issue_url 的
  平台名/host 二分（gitee 仓显示 "GitCode"、链接指 gitcode.com）。
  新增 platform_card.display_name_for_repo_key / host_for_repo_key
  （卡派生，无卡回退存量二分默认——裸键=gitcode 语义保持）。

误报登记（确认不修，防过度收口）：
- user/db 2245/2332、event_pipeline 4163、service 3001 的
  startswith("github") 二分——else 已落 get_repo_token_with_refresh
  （provider 感知），github 分支只是专用函数快路；
- pipeline.py 407 gitcode↔github 镜像事件查询（设计如此，无 gitee 镜像）；
- issues_center 739 github 镜像关单（同上）；repogit 446 已有平台卡
  else 分支；issues_center 1889/1927/1974 存量委托仅 gitcode 调用。

对账：S76 用例 3 + 既有族 22 绿；涉改 11 文件族批（管道/webhook/
patrol/session_store/tasks/issues/chat/feishu）零失败。

### D52：IM 域终残留——绑定 provider 派生（S77，先测后改）

D51"other"族抽样分类的唯一真残留：handler_async 3 处
upsert_repo_im_binding 第二参硬编码 "gitcode"——gitee 仓 IM 绑定行
provider 错标（读侧 D44 已按 repo_key 派生、写侧折叠，FR-21.2 尾巴）。
修法：parse_repo_key 前缀派生（裸键=gitcode 存量语义），3 调用点。

解耦终态声明（本轮枚举对账后）：
- 仓库域（托管/协作/事件/任务/管道/凭据/展示）：平台名字面量分支清零，
  行为全由平台卡能力位 + 方言覆盖层注册表派生；
- IM 域：绑定 provider 派生（本轮）；
- 剩余字面量两类，均为设计语义不修：函数签名默认值（裸键=gitcode）、
  gitcode 专属子系统（OAuth 刷新注册表/settings 存量字段/scenes
  登录名校验——新平台走泛型 platform_tokens 与渠道卡）。
对账：S77 用例 2 绿（精化守卫+派生语义）；IM 族回归 96 绿。

### D63：二分全量枚举分类台账 + 真二分收口第一批（S87，先测后改，批次 2 续）

**全量枚举（D50 方法）**：vcs 层外平台字面量 265 处（305 总 - vcs 层
合法区 40），逐条带上下文分类——
- **方言/卡驱动域合法保留 5 处**：repo_key web 链接 gitlab 路径 ×2、
  event_pipeline PR 评论方言、git_repo_api 卡 dialect 判断、issues_center
  注释引用；
- **gitcode 专属子系统登记 3 组**（D51 清单延续）：milestone PATCH
  特判（gitcode 特有功能）、模板 tarball 按源平台下载、PR↔Issue 原生
  关联（仅 gitcode 有原生存储）；
- **真二分 8 组本批收口（S87）**：
  ①token_resolver 全局 bot 链 4 处（_cached_get_meta 文件字段特判 ×1、
  fallback get_global_github_bot_token ×2、self-loop username 特判 ×1）
  → 统一 get_global_bot_token_for / 新 helper get_global_bot_usernames_for
  （github 单值源 / 其余 gitcode 集合=存量语义）；
  ②feedback_issue_converter GRAPE_SKILLS_PROVIDER 特判 → 统一入口；
  ③grape_auth OAuth 落库读回校验 ×2 全渠道化（原 github 特判——一致性
  校验与平台无关，纯加固零危害）；
  ④user/db 孪生函数 get_user_gitcode_token / get_user_github_token_by_id
  泛化为 get_user_platform_token(user_id, provider)（薄委托保名）；
  ⑤storage 工作空间路径派生改 parse_repo_key。
- **登记批次项**：repo_manager 模板 tarball 卡化（api 段 tarball_path）、
  bot 身份来源链 github 特判统一（resolve_bot_username 已有链，接线归
  下批）、git_repo_api 树/ref 方言位（card tree_api 能力位）、github
  PR labels 拆分请求方言位、user/db get_github_token 族内部二分
  （函数域即平台，合法）。

守卫：token_resolver / feedback_issue_converter / grape_auth 三文件
平台比较清零（文件级零比较，S87 结构守卫）。

对账：S87 6 用例绿（守卫 3 + 行为 3）；S84/S85 回归 16 绿；
global-bot/self-loop/user_db/s59 家族单文件隔离全绿（混跑 5 失败=
D46 已知污染预存量，stash 对照法不适用于污染类，按隔离口径）。

对账（S85 存档）：S85 5 绿 + S84 5 绿 + S80/S80b/S82 回归绿；
test_add_and_scene 转绿；涉改族零新增。

### D65：树 API 家族卡位 + OAuth email 补抓卡位（S89，先测后改，批次 2 续）

**树 API 家族（D63 登记项，5 处 p=="github" 收口）**：判别器是 API
家族而非平台名——github_api（default_branch 字段/recursive 树/无斜杠
ref 直连）vs v5（branches[0]/commits?sha 预校验/recursive 拒绝）。
卡能力位 git_tree_family（builtin：github=github_api，gitcode/gitee/
gitlab=v5=存量语义；gitee 将来实弹验证 recursive 后可经卡配置翻转，
无需代码）。守卫口径修订：git_repo_api 禁 provider 变量比较
（p ==/!= "github"），card.dialect 判断属方言域合法保留——
D63 分类中"方言保留"与"平台比较"两类在守卫层自此分开。

**OAuth email 补抓卡位（providers.py 最后一处平台特判）**：
渠道卡 emails_endpoint 能力位（github 内置 /user/emails——其 /user
接口默认不返回邮箱；其余渠道空=无补抓），卡可配置覆盖；
providers.py 平台比较清零（文件级守卫）。

对账：S89 5 用例绿；S84-S88 回归 33 绿；git_repo_api 单文件
stash 对照失败集 IDENTICAL（7 预存量 stale-seam 零新增）。

对账（S88 存档）：S88 8 用例绿；S84-S87 回归 28 绿；git_repo_api/581/
admin 族 stash 对照失败集逐条相同（17 预存量 stale-seam，零新增）。

### D66：模板 push 目标 host 卡派生 + 源平台 tarball 分派表（S90，先测后改，批次 2 续）

**真二分缺陷修复**：push_template_to_repo 非 github 目标分支把 push
URL 硬编码 gitcode.com——gitee 目标建模板仓：仓库经 VcsApiClient 建在
gitee、内容却推到 gitcode.com。目标 host 改平台卡派生（gitcode 语义
逐字节不变；gitee → gitee.com）。
**tarball 源平台分派表**：三分支 if（github/gitcode+clone_url/gitcode
默认）→ _TEMPLATE_SOURCE_DOWNLOADERS（源平台=模板来源而非目标平台；
github 源误配置无 clone_url 也不再误用 gitcode 下载器——旧行为的隐性
错误形状顺带修正）。仓内既有 push 模板用例（oauth2:user_tok@ 断言）
全绿=gitcode parity 锁。

对账：S90 5 用例绿（先 4 RED）；templates 族回归 40 绿；S84-S89
回归 38 绿。

对账（S89 存档）：S89 5 用例绿；S84-S88 回归 33 绿；git_repo_api
单文件 stash 对照失败集 IDENTICAL（7 预存量 stale-seam 零新增）。

### D67：GRAPE_PERSISTENCE_ORM 开关退役 + engine 重绑跟随修复（S91，先测后改，批次 2 收口）

**开关退役（D60 拍板节奏兑现）**：身份域 7 函数 `if _orm_enabled():`
分支删除（raw SQL 路径退役，ORM 为唯一路径——S79 逐字段对账已锁等值；
退役用例 test_s79 转常态回归）；user/db get_webhook_secret_by_repo_key_any
去门；orm_enabled 助手删除；test_s78 默认关断言改为退役锁。
实施用 ast 块界精确折叠（首版行级折叠脚本切错块界留下孤儿碎片被
测试当场揪出——教训：块级重构必须走 ast，行级缩进推断不可靠）。

**engine 重绑跟随修复（S78 教训的残留盲区）**：get_engine 原为
lru_cache 单例 + creator 早捕获 session_manager.db 引用——连接对象
重绑后 ORM 停留旧 inode（世界分裂）。修正：缓存键含"当前连接对象 id
+ 路径"，creator 每次解析模块属性；cache_clear 保留（fixture 兼容）。

**未竟事项（登记，非本轮闭）**：test_issue_372_user_token 存在 5 个
基线预存量失败——实测与开关无关（raw/ORM 双态下失败集一致），真因=
该文件真写测试的读回可见性异常（create 后首读 0、一次 raw 语句后
转 1；生产单连接形态不受影响），属测试基建专项，登记独立轮。

对账：S78/S79/S81/S80b/S82/S60 全绿（5 预存量=372 文件基线，
stash 对照口径）；372 文件原始 mock 用例在 ORM 常开下全数通过。

### D68：um 平台归属列运行时停写（S92，先测后改，um 退役第一步落地）

**全量清点**：um 引用 228 处（写侧 SQL 25 / 读侧 68，函数级归并）。
关键发现：um 实为"两张表粘合"——①平台归属列（gitcode_user/
github_login）：**session_manager 启动反向回填已从身份域喂养真值**
（只填空），运行时写纯属冗余；②feishu_open_id/feishu_name/
gitcode_nickname：身份域无对应物（IM 映射域，批次 3 建模）。

**停写实施（仅①类列）**：_sync_um_platform_col no-op 化（FR-18.6
补写语义退役）、prompt_builder 分支合并、settings github_login/
gitcode_user 停写（保 nickname）、session_manager CASE WHEN 去除。
解绑清空语句保留（冻结数据清了无害+unbind 一致性）；启动回填桥
保留（读者兜底不断粮，守卫显式断言其存在）。

**语义更新**：um 平台列自此=启动时快照（只服务存量读者兜底），
新用户归属完全走身份域。S59 污染用例语义更新（no-op 后无写入即
无污染，gitee/gitcode 同样冻结）。

**um 退役路线图更新（D31 三步 → 现状）**：读侧降级 ✅（S51/S54）→
**平台列停写 ✅（本批）** → feishu/昵称域建模（批次 3）→ 只读对账 →
删表。

对账：S92 3 用例绿（bind 停写/解绑清空保留/守卫+回填桥存在）；
S59/S60/D31/D34/user_db 回归 71 绿。

### D69：observability 可见性候选集统一 + health 聚合动态化（S94，先测后改，"待人工"桶首批）

**D63"待人工"153 处分类完成**：合法区（函数签名默认/文档/向后兼容
包装/注册表数据）约 100 处；**真缺陷形状 2 组本批收口**：
①observability 族 8 处 `to_repo_key(repo, "github")` 双键集——gitee
仓的观测数据被可见性过滤静默排除（S25 已有 repo_key_candidates 抽象
却未接线到此）→ 统一 repo_key_candidates（7 文件 + routes/observability）；
②bot_token_health 聚合循环硬编码双平台——config-only 平台 token 巡检
了（D19）却不计入健康聚合 → 遍历 platforms 全集；顺带删除 gc/gh 死赋值。
**登记批次项（本批不动）**：webhook 别名端点双字面量（legacy alias，
合法）、repos.py 支持平台集合 {gitcode,github}×3、issues.py 关单双平台
结果字典、scenes.py 平台探测对、admin 路由 provider 白名单、config_store
client meta 键映射、bot_token_health 平台标签映射——各需卡/目录派生
载体，归"待人工"余量下批。

对账：S94 3 用例绿；bot_token_health 族 17 失败=基线预存量
（stash 对照口径）；S92/S84 回归绿。

### D71：D69"待人工"余量批次——登记站点卡/注册表派生收口 + 合法冻结分类（S96，先测后改）

D69 登记的 7 项逐一处置（三收口 + 四冻结，载体替换不动行为）：

**收口三件**：
①bot_token_health `_PLATFORM_LABELS` 字面映射（R25.4 引入）→ 平台卡
`name`（卡缺位回退 capitalize，卡 name 是权威展示名）；
②bot_token_health 探活基集 `{gitcode:…, github:…}` 字面 → provider 翻译
模块注册表（`src/vcs/providers._MODULES`，今日=双平台，注册新模块自动
纳入——与 D19 config-only 增补循环同向）；
③grape_auth 过期检查 `for _p in ("gitcode","github")` → 遍历用户实际
绑定平台（identities 的 provider 去重；gitee 等绑定用户的过期状态不再
漏检——真缺陷形状）；
④issues 镜像查找循环同 ③ 走注册表。

**分类合法冻结（本批不动，另需设计/重构）**：
- webhook 别名端点双字面量：legacy alias，语义就是"存量别名"，冻结；
- repos.py 优先级序 `("github","gitcode")` 与 66/98 回退集：优先级是
  语义常量（host-repo-provider-routing-fix 存量 github 优先），回退集
  是降级路径字面，冻结；
- issues.py 关单双平台结果字典（593/737/831）：关单链是 gitcode/GitHub
  镜像二元语义（_find_github_mirror_repo_key + is_github 分叉），改多
  平台需关单链重构，归批次 3；
- settings `_LEGACY_PAT_FIELD_PROVIDERS` / config_store oauth meta 键
  映射：legacy DB 键名，派生只会造出永不读取的键，冻结；
- scenes.py 平台探测对：实测已走 repo_key_candidates（S94 已收口），
  仅 docstring 措辞残留，登记为已解决。

对账：S96 3 用例绿（结构守卫 1 + 标签卡派生 1 + 基集注册表 1）；
S94 aggregation 1 失败=基线预存量（stash 对照口径一致）；相关族回归
（bot_token_health/global token/grape_auth/issues 关单族）与基线逐项
一致。

### D72：confirm-done 关单链多平台重构——仓库能力抽象（S97，先测后改，用户拍板"只要仓库能力抽象"）

关单链原状：confirm-done 人工链路 gitcode/github 二元硬分叉
（is_github 布尔、镜像只找 github: 单键、结果字典固定双 key）——
gitee/gitlab 绑定的仓被静默 skipped。数据依托不变：managed_repos
绑定关系（repo_key 体系）+ TokenResolver 凭据策略链（bot 列→全局→
用户 OAuth 降级，with_oauth_token 保护）。

**收口三件**：
①`_try_close_issue_for_repo` 去 is_github 参数：token 入口按 provider
选函数——github 保留 get_github_token（D32 补丁 owner identity 兜底，
逐字节保持）；其余平台 get_repo_bot_token（rc 唯一权威，#343 语义
跳 session 级，本身已 repo_key 驱动平台通用）；用户 token fallback
全平台同构（P1-2 flag 语义不动）；
②`_find_github_mirror_repo_key`（github-only 单键查询）→
`_mirror_repo_keys`：同 owner/repo 全平台行，候选集
repo_key_candidates（卡派生，D16/S25 抽象复用），ORDER BY 稳定序；
原函数保留为薄包装（github 语义：镜像中 github: 前缀第一条），
#656 存量消费者不动；
③`_close_vcs_issues`：主平台（repo_key 自身）+ 镜像全平台逐一尝试；
结果字典 legacy 双 key（gitcode/github）冻结——存量前端消费者依赖，
新平台按实际候选增量出现；bot_token_failed 提示改遍历 result.values()
（原双 key 元组）。

**行为约束实测**：gitcode/github 两条既有路径逐字节（token 入口、
降级链、返回值形态）；降级不阻断、权限校验（owner/admin 403）、
幂等检查不动。

**登记同族下批**：issues.py 澄清评论发送（~1693 行）同一 is_github
二元分叉形态（bot token 分派 + _github_req/_gitcode_req 裸调用），
归关单链同族下一批。

对账：S97 5 用例绿（结构守卫/legacy 冻结/主+镜像三平台/镜像泛化/
token 分派）；test_issue_209 + 393 + 372 失败集与 stash 基线逐项
一致（8 failed / FAIL-SETS-IDENTICAL 实测）；S96/S94 回归绿。

### D73：澄清评论发送统一——关单链同族收口（S98，先测后改）

D72 登记的同族站点（issues.py clarify_issue）：github 分支 _github_req
裸调 + 手工 #453 台账，其余平台 S75 已走 VcsApiClient。本批统一：

- token 入口按 provider（与 S97 _try_close_issue_for_repo 同构）：
  github=get_github_token（D32 兜底保持）；其余=get_repo_bot_token；
- 发送统一 VcsApiClient.post_issue_comment（卡驱动全平台方言派生，
  #453 台账内置、#345 401 失效提示、TOKEN_NOT_BOUND error_code、
  会话写入/awaiting_input 恢复全部不动）；
- github 语义对齐：同端点（/repos/o/r/issues/n/comments）同鉴权
  （token_header）；错误文案并入"评论失败"族（原"GitHub 评论失败"
  仅文案差异，无测试断言）；
- issues.py 全文件 _github_req 依赖清零（裸调族）。

对账：S98 4 用例绿（结构守卫 2 + 卡客户端行为 1 + TOKEN_NOT_BOUND
全平台 1）；test_issue_453/372_round2/s75/tasks_clarify 失败集与
stash 基线 FAIL-SETS-IDENTICAL（12 failed 两侧）；S97 回归绿。

### D74：um 退役批次 3——IM 通道目录 channel_directory（S99，先测后改，用户拍板方案 B）

**拍板记录**：feishu/昵称域建模二选一——A（feishu 并入
external_identities，需 app_id DDL + 6 调用点 IM 链路重写）vs B（模块
收敛，零行为变化）。用户拍板 B，理由：①风险不对称（IM 通知静默丢失
是用户可感知严重缺陷，S54 已修过该形态）；②B 已拿到访问面收敛+退役
路径打通的工程收益，A 多出的只是"列消失"形式收益；③A 建模约束比
表面复杂——open_id 是 app 级，通用身份模型塞 app 语义要么污染要么
provider 特判；④"生产事故优先"原则的正确应用。

**实现（五条护栏逐条落实）**：
1. 新增 src/im/channel_directory.py：feishu/gitcode_nickname 两子域，
   um 列降级为模块私有存储。API：bind（app_id 必填关键字，成对护栏）/
   write_back（FR-18.4 USER_ID>0）/ resolve_open_id_by_login（FR-20.8
   github_login 回退 + FR-22.5 注册名回退链原样）/ resolve_open_id_by_user /
   resolve_open_id_for_login（S54 顺序内置：身份域优先→um 兜底，
   D39/S59 无列平台跳过）/ reverse_lookup_by_open_id / clear_for_app /
   clear_for_user（FR-21.10 user_id 精确）/ set_gitcode_nickname。
2. 六处裸 SQL 读写收敛（行为原样搬运）：issues_center S54 解析链与
   by_user_id 查询（保留薄包装，存量消费者不动）、routes/settings
   昵称写、routes/chat app 级/user 级清空两分支、im/handler_async
   open_id 反查两处。prompt_builder 的 um 写为 agent 引导模板字符串
   （agent 自执行），非产品代码路径，注记对齐语义。
3. 依赖方向单向守卫：身份域/存储层零 channel_directory 引用（用例锁）。
4. app 级成对：bind/write_back 签名 app_id 必填关键字（可空串但必须
   显式传），用例锁 TypeError。
5. 残留登记：session_manager.update_unified_user_info（OAuth 登录整行
   维护）为剩余 um 行写入方，归属登录同步职责，存储切换批一并处理。

**存储切换批出口条件（挂账钉死，防永久双态）**：channel_directory
回归全绿 + 存储后端切换方案评审通过 → 模块边界内换后端 + IM 链路
实弹验证；届时 external_identities 若已演进 app_id/扩展位则并入身份域
（A 变低成本），否则维持模块私有存储。A 是可选终态非必达终态。

对账：S99 8 用例绿（六类行为 + 薄包装委托 + 依赖方向守卫）；
涉改族回归（tasks_clarify/453/s92/s96/s98/prompt_builder/feishu 族
101 过）与基线一致；test_feishu_chat_unified 1 失败=测试间 DB 污染
预存量（同顺序基线复现，S69 挂账形态）。

### D75：D16/S25"占位符半截工程"全场景收口（S100，先测后改，外部扫描报告逐条实测核验后采纳）

**根因（核验属实）**：36ca1cfb4（D16/S25）把查询参数从固定双/三前缀
改成动态 repo_key_candidates，但占位符只在部分调用点同步改。放大因素：
configs/platforms/ 默认 4 卡 → candidates 默认 5 元素——缺陷默认部署即
触发。归因 diff 实锤（engine_select 参数行 +、占位符行未动）。

**修复（一轮统一收口，不逐处打补丁）**：
- P0 engine_select.query_engines：OR 占位符写死 3 个 →
  " OR ".join(["rc.owner_id=?"] * len(candidates))；实锤证据链 =
  隔离复现 ProgrammingError(3 vs 5) + test_query_scopes 0==1（异常被
  except 吞 → 静默空，测试早已报警）；
- P0 dashboard.get_dashboard 9 处 IN (?,?) → _rk_placeholders（原已
  定义仅 1 处接线）；连带修 L1360 参数 (_repo_keys[0],_repo_keys[1],...)
  位置解包 → tuple(_repo_keys)+(cutoff,)（占位符动态化后位置解包必错位）；
- P1 skills 歧义守卫 len(rows)==2 → >=2（≥3 平台同名仓不再绕过
  strict），报错文案动态列出命中平台；
- P2 知识读侧精确匹配、前端二分残留 4 处：维持上轮登记（读侧回退归
  知识族批次；前端展示层归前端批次），本批不动。

**守卫（补覆盖空白）**：S100 5 用例——源码守卫 3（engine_select 零固定
OR 链；dashboard 零 IN (?,?)/零位置解包/_rk_placeholders 接线数=站点数；
skills >=2+动态文案）+ 行为锁 2（真实 sqlite 下 5 候选 SQL 不炸且命中，
candidates 数==占位符数严格配对）。

对账：S100 5 绿；test_engine_select 由 5 failed→3 failed（test_query_
scopes 族转绿=缺陷修复直接证据）；涉改族（skills/dashboard/engine_
select 族 8 文件）失败集与基线 FAIL-SETS-IDENTICAL（28==28 逐项一致，
命名 stash 基线口径）；S96-S99 回归绿。

### D76：repo_key_in_params 原子产物——候选集占位符/参数同源构造（S101，先测后改，外部评审采纳）

S100 修复后的评审迭代：OR 链形态虽语义正确，但 repo_key_candidates
在拼接与传参两处独立调用——卡缓存 TTL 失效 + admin 改卡窗口内两次
返回数量可漂移 → arity 不一致风险归零路径 = 占位符与参数做成同一
函数的同一返回值。评审四点全收：

1. **空集恒假防御**（评审抓到的注释/实现不一致硬伤）：空候选集返回
   ("NULL", ())——IN (NULL) 三值逻辑恒 UNKNOWN，不匹配任何行；
   否决 IN ('') 形态（会误匹配 repo_key='' 行，静默语义漂移）。
   注意与旧形态不等价（旧空集=IN () 语法炸），替换清单带恒假用例；
2. 类型标注收窄 tuple[str, tuple[str, ...]]；
3. 命名 repo_key_in_params（返回的是占位符+参数，非完整子句）；
4. 调用点 SQL 片段拼接（列名/表别名灵活性）记档为已知边界，不阻塞。

**守卫（评审建议的轻量 grep 形态，非 AST）**：
- engine_select 源码守卫升级：必须走 repo_key_in_params 且零
  repo_key_candidates 直调（绕过原子产物=漂移风险）；
- 全 src grep 守卫：手写 `"?" * len(` 与 repo_key_candidates 同文件
  共存即非法；现存合法站点白名单登记（dashboard/issues_center，
  待换批），名单只减不增；
- helper 契约 3 用例：arity 同源构造 / 空集恒假（含空串行实测）/
  真实 sqlite 5 候选命中。

**逐批替换清单**（白名单站点，替换时逐条带空集/单候选回归）：
dashboard._rk_placeholders 10 处、issues_center._mirror_repo_keys、
alerts.list_events_all 及 S100 报告"确认无恙"清单中的手写形态站点。

对账：S100+S101 共 12 用例绿；engine_select/s75/repos_host_provider
失败集与基线逐项一致（零新增）。

### D76 替换清单批次 1（S102，随批提交）：dashboard 10 处 + alerts 1 处迁入原子产物

- dashboard.get_dashboard：`_rk_placeholders/_repo_keys` 构造改
  `repo_key_in_params(_owner_repo)` 一行产出（手工 join 退役），列拼接
  站点改元组拼接（`_repo_keys + (atype,)` / `(_dev_scene,) + _repo_keys`
  / `+ (_week_cutoff,)`）——白名单缩至 issues_center；
- alerts.list_events_all（S94 报告 P1 项，上轮登记）：`IN (?,?)` 固定
  2 占位符 vs 5 元候选 → ProgrammingError 被 except 吞 → L2 管理台
  按仓库过滤告警事件永远空列表；改 `repo_key_in_params(to_repo_key(repo))`；
- 对账：S100+S101 守卫 9 绿（grep 守卫白名单缩至 issues_center 生效）；
  obs/alert 族 + s94 + s100 失败集 26==26 与基线逐项一致（零新增）。

### D77：扫描报告 P2 收口——知识读侧候选集 + 前端二分残留（S103，先测后改）

**P2 知识读侧（报告 #5）**：`_query_knowledge_obs` 单值 `repo_key=?`
精确匹配 → `repo_key_in_params` 候选集（候选含入参本身，既有命中形态
不变）——读侧入参为裸键/异前缀时不再静默丢注入。knowledge 族回归
0 失败。

**P2 前端二分残留（报告 #6，4 处）**——统一载体 = app.js 既有卡目录
（_appLoadVcsCatalog，/api/settings/vcs/platforms）：
- 新增 `_repoWebHost(repoKey)`（app.js，与 _repoWebLink 同源，裸键
  兜底 gitcode）；
- repos.js hostAndSelect：provider 按 repo_key 前缀解析（任意平台卡，
  gitee 仓不再传空回落登录平台误判路由）；
- dashboard.js issue 链接 host → _repoWebHost（gitee 仓 issue 不再指
  gitcode.com）；
- templates.js 模板跳转 → _repoWebLink（tplPlatform 前缀构造）；
- scene-market.js 模板跳转：后端 scenes.py 场景明细 payload 补下发
  `source_platform`（原只给 source_repo，前端无从知晓平台），前端
  _repoWebHost 派生 host；
- index.html ?v= 缓存版本五处 bump（只升不降纪律）；
- node --check 四个页面模块 + scenes/knowledge AST 全过。

对账：S100-S103 守卫+场景 25 用例绿；knowledge/453/s102 失败集
0==0（修复后 knowledge 族零失败）。
### D79：S69 测试基建批次 1-3——死用例清理 + 孤儿 inode 根因修复 + 两个真产品缺陷（S105/S106，先测后改）

**S105（test_tasks 31 失败 → 0）**：根因非污染而是死用例——文件仍测
tasks-issues-api-rebound（无兼容迁移）时删除的端点。端点迁移
（mine/repo/start → /api/issues）+ 删除死用例（close 已移除、backfill
前提失效 user_accounts 遗留空表）+ 语义对齐现行设计（403/public 读/
接管不完成）+ fixture 根因（裸键 repo_key 清理缺口、bot 识别走
managed_repos.bot_username——旧 INSERT 被 NOT NULL 约束 OR IGNORE 静默
吞掉的假 fixture）。

**S106（两个真产品缺陷，测试实跑暴露）**：
①session_store.list_release_subissues 引用 `_native_issue_number`
未导入 → NameError 被 except 吞 → 管理台发布子单列表 500（S86 同族
第三处）；
②storage.skill_key repo 分支：S87 重构把 parse_repo_key 返回的 owner
单段误当 owner/repo 两段再 split（恒 1 段 → 一切 repo scope 的
skill_key 全炸）+ 前一版 `_owner` 未定义笔误 → 技能上传（repo scope）/
仓库↔个人转换全链路 502。修为三段直接重组。

**conftest 孤儿 inode 根因修复（S69 核心项）**：删库重建原在 session
fixture（_ensure_test_db）里——pytest 先收集导入测试模块（73 个模块
module-level 绑定 session_manager.db），再删文件 → 已绑定连接写进被删
文件的孤儿 inode；凡开新连接的代码（fresh sqlite3.connect / 新引擎）
看到重建后的空库 → "no such table: skill" 型 E 残留。修法=删除上移到
conftest 导入期（先于所有测试模块导入，无孤儿窗口），fixture 只做建表。

**对账**：test_tasks 33/0、issue_627 38/0、skills_obs 28/0（三族从
台账 97 项残留清零）；广域回归基线 29 → 6（净 -23，零新增；余 6 为
issue179 族预存量）；S100-S105 守卫与场景用例全绿。

### D80：G2 方言翻译模块补齐——gitee/gitlab 注册进 provider 注册表（S107，先测后改，架构红线贯通）

多平台承诺"配置即接入"此前只在卡层成立：providers._MODULES（方言翻译）
仅注册 gitcode/github。本批把 gitee/gitlab 补进方言层，红线"加卡+加
翻译模块=新平台"真正可执行：

- **gitee.py**（v5 同族）：规则与 gitcode parity（update state
  reopen/close、query opened/closed/all、create 字段一致，用例锁逐条
  对比）；凭据 query access_token（v5 标准）；bot 走 rc 通用链、用户走
  get_user_token_for(provider)；PR 的 close_related_issue 机制不存在
  → 显式 drop（apply_rules 无 drop 形态，no-op transform 实现）；
- **gitlab.py**（v4 偏离形态，与 token_resolver 侧 GitLabProvider 对齐）：
  body→description、head/base→source/target_branch、state→state_event、
  assignee 用户名跨平台不可映射 → drop；URL 钩子 repo_segment
  （/projects/{owner}%2F{repo}）+ PATH_MAP（/pulls→/merge_requests）；
- **git_repo_api._vcs_url**：仓库资源段/子路径映射改为模块钩子消费
  （无钩子回落存量 /repos/ 形态，gitcode/github 逐字节零变更）；
- **注册表 2→4**：与平台卡集合对齐（用例锁 set 相等）——探活基集、
  镜像关单、镜像查找随之自动纳管全平台（S96/S97/S104 的注册表驱动
  站点即时受益，零改动）；
- **顺带入库**：issue179 sync 三用例修复（skill-description.json 已随
  58173299b 移出仓库——build-time artifact，改内联最小数据；category
  断言用既有真实 slug 防计数污染），余 3 项 category-table 为预存量。

对账：S107 6 用例绿；涉改族失败集 7==7 与基线逐项一致（零新增）；
存量 gitcode/github URL/规则零变更（用例锁）。

### D81：G3 收口——镜像语义全平台化完成（S108，收尾清障）

核验结论：S97/S104 完成实质迁移后，产品代码中 github-only 镜像语义
已无存活站点——唯一残留是零调用的 `_find_github_mirror_repo_key` 薄
包装，本批删除（守卫升级为全文件断言）。关单结果字典 legacy 双 key
维持冻结（前端兼容，S97 设计决策）。**G3 关闭**；G3 原"依赖 G2"的
判断在 G2（S107）完成后兑现——镜像族现已全平台语义。

对账：S97+S102 守卫 8 绿（含新增全文件断言）。

### D82：G5 测试基建批次 4——clarify/create/209 族清零（S109，先测后改）

- **tasks_clarify（3→0）**：端点迁移 /api/tasks/{id}/clarify* →
  /api/issues/{id}/*（S98 同族）；fixture 对齐产品约定
  （agent_tasks.issue_id 带 # 前缀）；bot token 凭据按 S82 rc 单表种值；
- **tasks_create（3→0）**：mock 缝迁移（_post_gitcode_issue →
  VcsApiClient.create_issue / find_open_issue_by_title）；token fixture
  迁身份域（create_external_identity，user_accounts 遗留列失效）；语义
  对齐现行设计——assignee 不默认指派（Issue #649，建档锚定=创建者
  身份域 login）、同标题 open issue → 409 防重复建单（#343）、建档
  触发 pipeline → processing、状态迁移走 PUT /api/issues/{id}/status；
- **issue_209（4→0）**：close_issue 断言迁移统一 _request 缝
  （_gitcode_request 平行族已消）；ConfirmDone 用例 token 主缝
  get_repo_bot_token + 无 token 降级语义修正（failed 非 skipped）；
- **issue179（3→0）**：conftest 孤儿 inode 修复的连带收益——
  category-table 三项预存量转绿（无需改动）。

对账：五文件 51 用例全绿（此前 24 失败）；S100-S108 守卫与场景
全绿。




**S69 余量**：issue179 族 6 项、clarify/create/209 族（endpoint 迁移
同款）、E 440 其余文件——同方法论逐文件清。
**场景用例组（S103，UC8-UC11，映射见 requirements 场景表续编）**：
UC8 知识读侧前缀不一致实弹（裸键/异前缀/本身前缀/无关仓四态）/
UC9 前端四站点源码守卫（JS 实弹按仓库既定口径 = 源码断言 + node
--check 部署验证）/ UC10 平台信息下发链路 / UC11 缓存版本纪律。
用例落位：test_s103_knowledge_frontend_closure.py（4 用例）。

### D78：parse_repo_key 内联回潮全仓扫描收口（S104，先测后改，D58 登记挂账清账）

**扫描口径**：三类内联形态全量枚举（startswith 平台前缀 20 / split(":")[0]
10 / replace-strip 5），逐站点分类——真回潮 6 处收口，合法 11 处登记冻结
（token 分派按 provider 选函数 = S97 既有模式；issues_center:668 github
薄包装 = S97 设计；is_github 二元助手；main.py gitcode 登录渠道语境；
URL/host 解析非 repo_key 语境；展示层 strip 3 处低危登记）。

**收口六处**：
①issues.py `_repo_keys_of` 手写双平台互查 → repo_key_candidates（超集
安全：repo_key 名空间全局唯一；漏 gitee/gitlab 缺陷消除）；
②pipeline.py 同款双查 → 同上；
③event_pipeline provider 内联 split → parse_repo_key（等价重构）；
④issues_center auto-finalize 手写 github-only 镜像 → _mirror_repo_keys
全平台统一（S97 同族第三处残留）；
⑤user/db get_webhook_token webhook_token_field 二元 → D22 卡字段派生
（get_platform_card().webhook_token_field，与二元取值逐字节一致，
gitee/gitlab 自动扩展）；
⑥session_manager history 目录名 replace 双平台 → 冒号安全形态
（gitee:/gitlab: 前缀冒号不再进目录路径）。

**登记（不动）**：prompt_builder:723 / events:1571 / issues:973 展示层
strip（低危，展示归前端批次）。

对账：涉改族失败集 3==3 与基线逐项一致（零新增）；S100-S103 守卫
25 用例绿。


**场景用例组（S100-S102，UC1-UC7，代码级映射见 requirements"候选集占位符
家族场景用例"表）**：UC1 默认部署 5 候选三族绑定 / UC2 单候选配置 /
UC3 空候选集恒假（IN (NULL) 三值逻辑）/ UC4 卡漂移原子性（helper 单次
求值）/ UC5 alerts 仓库过滤实弹（P1 闭环）/ UC6 skills ≥3 平台歧义 /
UC7 结构守卫组。用例落位：test_s100_candidates_placeholder_guard.py
（守卫+契约 9）+ test_s102_candidates_scenarios.py（场景 3）。

**代码合理性收尾**：alerts.py L1181 保留 repo_key_candidates 直调
（集合差运算用途，非 IN 参数构造——原子产物边界外的合法形态）；
engine_select/dashboard import 零冗余；get_dashboard 区域 AST 校验通过。

### D83：审计修复批次——P0 app.js 截断 / P1 持久化依赖 / gitee 凭据脱敏 / F5+R4 死代码 / 测试基建环境无关性（S111，先审后改，第八轮续审驱动）

**来源**：第八轮分支续审（tip 6643f50d1）确认的合并阻塞项 + 连续多轮未修挂账，
用户授权"你来修复他"后一轮收口（commit f418ab692）。

**P0：app.js stripRepoPrefix 截断（36ca1cfb4/D16 引入，连续八轮 CRASH）**
根因：D16 在 `stripRepoPrefix` 的 return 行后直接插入 S27/S103 PlatformCatalog
块（~25 行），再补 `}` 收尾——该 `}` 实际闭合的是 stripRepoPrefix 自身，
导致 `_appVcsCatalog/_appLoadVcsCatalog/_repoWebLink/_repoWebHost` 全部嵌套
在函数体内，而文件末尾 line 3432 的顶层调用 `_appLoadVcsCatalog()` 在深度 0
→ 加载即 `ReferenceError: _appLoadVcsCatalog is not defined`，defer script
顶层异常 = 全站白屏级。`node --check` 抓不到（括号平衡），必须直接 eval
实弹。v=214/215 每次递增都在把坏文件穿透缓存推给全量用户。
修法：函数在 return 后立即闭合，`window.stripRepoPrefix` 导出上移到紧随
其后，目录块回顶层（最小 diff，不搬移代码块）。
验证方法论沉淀：node v24 直接 eval 大文件可能触发 V8 isolate 内部断言
崩溃（非脚本错误）；IIFE 包装 eval 会**假绿**（调用点落回作用域内恰好
掩盖 bug）——复核必须用直接 eval 语义 + python 大括号深度分析三角验证，
结构证据才是决定性的。

**P1：SQLAlchemy 依赖清单缺失（4595d5a1d/S79 引入）**
`src/auth/grape_account_db.py` 模块级 `import src.persistence.repositories`
（非惰性），但 sqlalchemy 不在任何 requirements 清单——开发机 .venv 手工装过
掩盖缺依赖，按 requirements.txt/Dockerfile 全新部署主服务起不来。
修法：requirements.txt + requirements-local.txt 加 `SQLAlchemy>=2.0`
（DeclarativeBase/Mapped 为 2.0 形态，带根因注释）；Dockerfile 走
requirements.txt 全量安装自动覆盖；requirements-worker.txt 经全仓 grep
确认 worker 执行链零 persistence 引用，保持最小集不动。

**N1（P2）：gitee 模块凭据泄漏进日志（S107 新文件引入）**
gitee.py `request()` 把 access_token 拼进 URL query 后原样
`raise UpstreamAPIError(method, url, ...)`，而 `str(e)` 与 `_vcs_req` 的
`log.warning` 都渲染完整 URL → token 进日志。gitcode 存量 `_req_query`
同形态但属基线预存；gitee.py 是本分支新文件 = 分支引入，本批必修。
修法：`_redact_url`（`access_token=` 参数值替换 `***`），仅 raise 前脱敏，
请求本身不变。

**F5/R4（P2）：多轮未修死代码**
F5：PlatformCard dataclass `issue_write_form` 定义两次（line 59 权威定义
保留，line 76 重复删除，ast 验证零重复）；R4：`get_webhook_verify_token`
内 S80b 块逐字重复两遍（2377/2385，删第二份，行为等价——第二份是
第一份的完整复制）。

**P2：测试基建环境无关性（S87-S94 五文件 + 新发现的缓存隔离缺口）**
①五个测试文件硬编码 `<repo-root>/src/...`（作者本机工作副本），任何其他
环境 PermissionError 全红——统一改 `Path(__file__).resolve().parent.parent`
（对齐 s96/s97 既有写法，S96 轮已吸收教训但漏改这五个老文件）；
②新发现连带缺陷：s94 一处 `.read_text()` 误挂在字符串字面量上而非
`pathlib.Path(...)` 上（原作者路径替换笔误，AttributeError）；
③开发机 gitignored 的 `configs/global-bot-token.json` 经 60s TTL 缓存
文件 fallback 毒化 s87/s88 断言——fixture 写 meta 后调
`_invalidate_global_bot_cache()`（对齐 test_global_bot_token_db.py L36-37
既有手法），毒化文件在场也全绿。

**对账**：S78-S110 全系列 + platform_card + webhook + skills + issue179/209
回归 215 passed（唯一失败 test_issue179::test_featured_returns_category
在未修改分支快照同败 = 预存量）；app.js node --check + 直接 eval + 功能
验证（stripRepoPrefix 三形态 / _repoWebHost 卡目录前后 / _repoWebLink
gitee host）全绿；_redact_url 三形态实弹；skill_key 前缀/裸键/github
三形态 OK（S106 修复复核）。缓存 v=215→216。挂账：session_manager
`get_webhook_secret` 的 ORM 门控属 repo 域退役批（S91 设计预期），
gitcode `_req_query` 文件 fallback 形态属基线预存——均不动。

**场景用例组（S111，UC13-UC15，映射见 requirements 场景表续编）**：
UC13 app.js 闭合完整性（结构断言 + node 实弹 + 行为三件）/
UC14 web 凭据不入日志（三形态脱敏）/ UC15 测试基建环境无关性
（/root 零硬编码守卫 + 缓存隔离行为锁）。用例落位：
tests/test_s111_audit_closure.py。

### D84：worker 包 import 闭包缺 src/vcs——存量 worker 升级即启动失败（S112，第九轮续审深度演练发现）

**来源**：第九轮续审（tip fe21d78c8）后用户追问"对系统有伤害吗，老 worker 会有问题吗"，
按"深度检查+实弹依据"纪律做 worker 包模拟部署演练（MANIFEST 复制 + 干净 venv + 逐级 import）发现。

**P0：worker 包启动即崩（bc8c8f6b5/S75 引入，既有守卫已红但被漏看）**
根因：S75 给 `src/runtime/service.py` L11 加模块级
`from src.domains.runtime.vcs.api_client import _native_issue_number`（S75 ident 原生形态），
而 worker 包 MANIFEST（src/worker_package.py）与 release_pack.py tools 闭包
均无 `src/vcs/`。`src/runtime/__init__.py` 顶层 `from .service import ...`
→ worker agent 启动 import 链即炸 `ModuleNotFoundError: No module named 'src.domains.runtime.vcs'`。
**归因三步实锤**：① 基线 service.py L1-15 无该 import（git show 对照）；
② pickaxe `-S "_nin_evt" -- service.py` → bc8c8f6b5 唯一命中；③ 基线快照
同条件演练 IMPORT OK、分支包同条件 CRASH——净回归实锤。
**既有守卫为何没拦住**：`test_worker_package_import_closure`
（2026-10-14 五台 worker 心跳全断事故后加，dfbd1556a）在基线快照跑绿、
分支跑红（真阳性 `No module named 'src.domains.runtime.vcs'`）——守卫是活的，作者写 D83 时
未跑该测试。教训：**改 runtime import 面必须跑 worker_package 守卫**；
D83"worker 执行链零 persistence 引用"的结论只查了 persistence 没查 vcs——
同族缺口全查必须覆盖包闭包全体，不能只查当次引入的模块名。

**修法（方案 A：MANIFEST 补 src/vcs，否决方案 B 惰性化）**：
- src/worker_package.py MANIFEST 补 `"src/vcs/"`（src/user/db.py 与 src/auth/ 之间，
  对齐整目录既有写法）；
- scripts/release_pack.py tools 闭包 manifest_files 同步补 `"src/vcs/"`；
- 否决惰性化：`src/api/session_store.py`（同样在 MANIFEST 内）L7 也顶层
  `from src.domains.runtime.vcs.api_client import _native_issue_number`（S106 修复时加），
  单点惰性化 service.py 不彻底、留下第二颗雷；src/vcs 依赖面纯 stdlib
  （urllib/json/re/hmac/hashlib/dataclasses，providers 层零第三方），
  整目录入包零依赖成本。

**实弹演练矩阵（修复验证）**：
- 基线包 import src.domains.runtime.engine.agent：IMPORT OK（对照锚）
- 分支包（修复前）：CRASH No module named 'src.domains.runtime.vcs'
- L1（仅补 src/vcs、无 persistence/sqlalchemy）：IMPORT OK = 最小修复面实锤
- 全补（src/vcs+persistence+sqlalchemy）：IMPORT OK（persistence 非必需佐证）
- 既有守卫 test_worker_package_import_closure：基线绿→分支红→**修复后绿**（实跑 1 passed）

**场景用例组（S112，UC16，映射见 requirements 场景表续编）**：
UC16 worker 包 import 闭包完整性。用例落位：tests/test_s112_worker_pkg_closure.py，
5 用例四类分工：
①manifest_guard：MANIFEST 含 src/vcs/（修复本体结构断言）；
②tools_closure_guard：release_pack manifest_files 块含 src/vcs/（双清单同步，
正则定位字面量块防漂移）；
③import_face_guard（防回潮核心）：ast 扫闭包内模块**顶层** src.domains.runtime.vcs import ⇒
MANIFEST 必含 src/vcs/——MANIFEST 已含则短路通过，未来任何闭包内模块新增
顶层 import 无需改名单仍有效；_CLOSURE_MODULES_USING_VCS 名单只服务于
守卫退化检测（第④用例要求名单文件必须真有顶层 import，过期即红——防死名单）；
④known_import_sites_documented：名单完整性锁；
⑤vcs_third_party_guard：src/vcs 依赖面纯 stdlib 前置锁（sys.stdlib_module_names
ast 全扫）——整目录入包的安全性论证必须配守卫，否则未来引第三方 = 论证失效无告警。
**RED 验证（git stash 法）**：stash 两文件修复 → ③失败且 offenders 实锤
service.py L11 + session_store.py L7（抓对位置）→ pop → 5 passed。

**修复后增量（同句指令第三轮，D84 首版 release_pack 修复的假绿被实弹演练抓出）**：
- release_pack 取项循环 `os.path.isfile` 静默跳过目录项——首版加进 manifest_files
  的 "src/auth/"/"src/vcs/" 字面量进不了包，守卫②只查字面量=假绿。修法：循环加
  isdir 分支（目录项与文件同列表处理）。
- 连带发现基线预存漂移（a7cf5ffc4 形态回潮）：a7cf5ffc4（2026-10-21，issue-372 R25
  后）修 MANIFEST 加 token_service/token_resolver 但 release_pack 未同步 → release
  tools 包 service.py L13 顶层 import 即崩（同 D84 形态）；prompt_builder（Windows
  support 进 MANIFEST）同缺；event_archive（weekly-05 停写）为冗余项移除。本批补齐
  三文件+删冗余，tools-sim 演练 CRASH→IMPORT OK 实锤修复有效。
- 新守卫三条（各配 RED）：②b loop_handles_directories（清单含目录项⇒取项循环自身
  必须有 isdir 分支；口径圈定到 "# 整目录" 前——首版 500 字符窗口误命中硬编码循环
  的 isdir 假绿，RED 复验救回）；②c aligned_with_manifest（双清单逐项对齐含硬编码
  目录循环，反向独有项必须为空）。
**对账（最终）**：s112 7 passed（RED：回退 release_pack → 2 failed 字面量+对齐守卫
实锤；构造"目录项+仅 isfile 循环"→ ②b 红）+ worker_package 全量 36 passed（既有
import 闭包守卫修复前红→修复后绿）+ worker-sim 端到端 IMPORT OK + release tools-sim
CRASH→IMPORT OK + S 系列 66 passed。
**不动项**：test_worker_package_mcp 4 failed = 环境预存（stash 未改态同败 +
基线 5 failed，mcp 产物文件不在开发机工作区，非本批引入）；chat token 注入链
（agent.py:2372）与身份解析（engine_select.py:439）的 ModuleNotFoundError 降级
= 基线预存（基线 deps.py 同不在包内），登记挂账不修；tests/conftest.py 的
OBS_LOG_DB=0 未提交改动系另一会话所留，本批不碰不提交。

### D85：worker chat 用户 token 注入链修复——SQLAlchemy + deps.py 入包（S113，D84 挂账项收口）

**来源**：D84 修复后用户追问挂账项"之前不是做了吗"，核对确认 f418ab692 只修了
requirements.txt/requirements-local.txt 两份清单（worker 清单当时基于"worker 链无
persistence 引用"的判断明确不动）；后续实弹演练证明该判断只对了"不崩"没对"功能"。
用户拍板修复。

**问题（基线预存降级，非分支回归——演练归因实锤）**：
worker 端 chat 任务的用户 token 注入（agent.py:2369-2378）与 issue_creator
身份解析（engine_select.py:439）两条链的 import（src.api.deps /
src.domains.mgmt.auth.grape_account_db）在 worker 包里 ModuleNotFoundError，被 try/except
吞成 log.warning → user_token 静默为空、身份解析降级旧链。基线同条件演练
同败（基线 deps.py 同不在包内）= 基线预存。

**修法（方案 C：闭包补全，否决代码搬家）**：
- requirements-worker.txt：+ `SQLAlchemy>=2.0` + `fastapi>=0.100`
  （deps.py 顶层 `from fastapi import ...`；版本对齐 requirements.txt L16）；
- worker_package.MANIFEST：+ `"src/persistence/"` + `"src/api/deps.py"`；
- release_pack manifest_files：同步（守卫 ②c aligned_with_manifest 自动锁）。
- 否决代码搬家（把 get_user_token_for_repo 链拆独立模块）：链内函数
  （get_user_token_for/_get_oauth_token_from_identity/刷新链）互相缠绕且
  服务端还在持续迭代（S57/S73/D37 多轮改），搬家=复制粘贴双维护；
  deps.py 函数内 import 的服务端模块（admin.db/org_db/jwt_utils/auth_token）
  全部是惰性 import——worker chat 链只触达
  grape_account_db/user_db/credential_crypto/provider_link（均已在包），不碰。

**语义分析（两种部署形态都验证）**：
- import 通后 chat 链走真查询。SQLite 本地形态：grape_accounts/
  external_identities 表不存在 → 查空 → resolve=0/token=""，与降级结果一致
  （worker-sim 实测无异常）；MySQL 共享形态（DEVOPS_DB_BACKEND=mysql，
  .env.example 已支持）：worker 与服务端同库 → 查到真数据 → **功能恢复**。
- persistence engine 挂 session_manager 同一连接（StaticPool），ORM 与裸 SQL
  同一事务世界，worker 端无新连接管理面。

**实弹验证**：worker-sim（补 deps.py+persistence+sqlalchemy+fastapi）
import 链 OK + resolve_user_id_by_platform_login/get_grape_account_by_id
真查询返回正确（空库→0/None，无异常）。
**守卫升级**：s112 aligned_with_manifest 守卫自动锁 release_pack 对齐；
新增 chat-chain 探针守卫（worker 包闭包内三链 import 可达断言）。
**测试隔离补丁（S114 批混跑发现）**：sqlite_semantics_probe 首版只
monkeypatch sm.db——grape_account_db L9 from-import 持有 sm.db 的模块级
副本，裸 SQL 查询仍走共享测试库（混跑被先跑文件的 lp_test_* 种子数据
污染，实得 id=1 账号）。修 = 探针同时重绑 grape_account_db.db/db_lock。
教训：**from-import 的 db 副本不跟随 monkeypatch 源模块**——重绑连接必须
覆盖全部持有副本的模块。

### D86：测试迁移债清零 + host_repo 网络错放行回归修复（S114，30 净失败→0，独立分支）

**来源**：续审台账 30 净测试失败（基线对照实锤=迁移债非行为回归，九轮挂账"合并前
建议同批迁移"），用户指令继续执行。独立分支 feat/weekly-08-yaw-test-migration-debt
（从主分支 tip dfa7fa5a3 拉出，拆分铁律：测试域独立于主分支功能批次）。

**域1（22 失败→0）repos_host_provider / login_provider / issue455**：
- mock 目标迁移：`get_user_github_token_with_refresh` /
  `get_user_token_with_refresh` 双旧符号 → `deps.get_user_token_for`
  （S57/D30 provider 无关入口），按 provider 分发（github→gh_tok /
  其余→gc_tok），双点位 patch（repos 模块级 from-import 绑定 + deps 源——
  host_repo L285 等函数内 import 从 deps 源解析）。
- **坑（实测抓出）：for + 顺序 with 不嵌套**——每轮迭代 body 在前一个
  patch 退出后执行，只有最后一个 patch 生效（实测返回 ('','') 空结果）。
  修法 = ExitStack 组合单上下文（login_provider helper）。
- 身份探测端口迁移（S62/D42）：GitCodeProvider/GitHubProvider.get_user_login
  委托 repo_manager.get_user_*_login（真实网络）。mock 口径 = **网络缝**
  （repo_manager.get_user_gitcode_login/get_user_github_login），不 mock
  create_provider/端口类——方法面大（webhook_token_field 等），整类 mock
  等于换实现（首版 _P 类缺 webhook_token_field 即炸）。
- verify_token 层级陷阱：抽象基类(L48) → GenericVcsProvider(L182 覆写)
  → GitLabProvider(L552 再覆写)——patch 基类无效，必须打对层级（最终
  走网络缝口径绕开层级问题）。
- `_patch_base` 的 with_oauth_token mock 返回 None 是假绿源：S62 后
  host_repo 身份探测也走 with_oauth_token，mock 返 None → current_user=None
  → 502。修 = 执行 fn(token)（对齐 issue455 既有写法）。
- s17"未知平台"断言语义过时：S83 卡文件化后 gitlab 是已配平台，
  login_provider 优先逻辑尊重它 = 预期行为变化（非回归），改用真正
  未配置的平台名 nosuchplatform。

**域2（7 失败→0）issue_372**：
- 3×AttributeError：`deps._get_gitcode_credential_by_gitcode_id` /
  `deps.try_refresh_gitcode_token` 旧符号 → `user_db.get_credential` +
  `provider_link.refresh_token_for`（S57/D30 统一后注册表分派）。
- `get_repo_token_with_refresh` L4 降级：`deps.get_user_token_with_refresh`
  → `get_user_token_for(username, user_id=42, provider="gitcode")`
  （**keyword 实参形态**——assert_called_once_with 必须按实参形态写）。
- create_external_identity S79 ORM 化：mock db 层断言 INSERT/UPDATE 失效
  → spy 仓库层（_iar.create / _iar.update_by_id）+ _uow 真跑（不 mock
  数据访问层，对齐 s79 既有真库写法）。**spy 陷阱**：MagicMock 的
  get_active_by_user_provider 返回 MagicMock（truthy）→ 走 update 分支
  ——新建分支用例必须显式 return_value=None 落 create 分支。
- empty_repo：S85 前置 get_webhook_secret_by_repo_key_any（ORM 路径）
  不读 db mock → MagicMock 穿透断言 → 弃 mock 走真库（空表自然空串）。

**域3（1 失败→0）s17（test_vcs_dialect_backend.py 内）**：S83 卡文件化后
优先级链 meta > configs/platforms/*.json > 代码兜底——删 meta 后 gitee
回落文件默认卡（仍注册），"移除卡即摘除"语义已被"meta 覆盖=整卡替换"
取代（S18 语义更新）。断言更新为回落注册。

**连带产品修复（P2 行为回归，独立于测试迁移，唯一生产代码改动）**：
host_repo 身份+权限前置段（repos.py L1194-1242）——网络类异常（无状态码）
旧实现放行（"pre-check skipped" → 200，issue455 基线语义），S62/D42 端口
统一时写成半截（skip 后 current_user="" → 落 502）；with_oauth_token 返回
None 同病。修 = `_identity_unverified` 标志放行两条路径（RuntimeError 无
状态码 / 返回 None），身份+权限前置整体跳过，交由下游 webhook 创建的既有
错误路径兜底。403/404 语义保持（error_code 演进 PERMISSION_DENIED →
TOKEN_PERMISSION_INSUFFICIENT，前端 repos.js L531 两个 code 均兼容）。

**对账**：迁移五文件 130 passed；S 系列+worker 域+关联域 156 passed；
git_repo_api 7 failed = 预存（stash 对照同败，S69 已知混合债域，不在本批
30 清单内）。**不动项**：git_repo_api 混合债（另一个批次）；
tests/conftest.py OBS_LOG_DB=0（另一会话未提交改动，不碰）。

### D87：git_repo_api 6 净失败归因修正 + 清零（S115，续审用户拍板"分支引起的就修"）

**来源**：D86 对账把 git_repo_api 7 failed 笼统记为"预存"——续审基线快照同跑
实测基线仅 **1 failed**（test_issues_crud_mapping，grape-auto-comment 水印断言
过时，逐字节同败=真预存），**6 个是分支侧净失败**，被误归预存。用户拍板
"分支引起的就正常修复"。教训：**基线对照必须落到用例名级差集，不能止于
失败计数**（"7 failed 域内已知"掩盖了 6 个净回归）。

**归因**（全部 D42/28a36d682 引入）：
- 域 A（1×403→502）：S107/G2 后 gitlab 注册进 `_MODULES`（四平台满员），
  test_unknown_provider_rejected 的 gitlab"未接入"代表失效 → 过 403 守卫
  落 token 解析 502。与 D86 域3 s17 同根因（已配平台不再是未接入代表）。
- 域 B（5×毒化/502）：D42 后 git-credential `_repo_platform_token` 从直调
  `get_gitcode_token/get_github_token` 改走 TokenResolver 四级策略链
  （session→repo_bot→global→user），旧 mock `src.domains.mgmt.user.db.get_gitcode_token/
  get_github_token` 不在链上 → RepoBotToken 查空库返空 → GlobalBotToken 级
  开发机 `configs/global-bot-token.json` 文件 fallback 穿透（gitcode 侧返回
  真实全局 token m4kj4...，github 侧无 fallback 直接 502）。S85 mock 穿透
  新形态的姊妹案例：**实现加新查询层后旧 mock 层级必失效**。

**修法**（tests/test_git_repo_api.py，+58/-37，零产品代码改动）：
- 域 A：gitlab → `nosuchplatform:` 真未注册前缀，403 防御语义保留
  （对齐域3 s17 修法）。
- 域 B：弃 mock 走真库（对齐 D86 域2 empty_repo 先例）——autouse fixture
  `_seed_repo_bot_tokens` 按 repo_key 播种 managed_repos 列 + grape_t_repo_credentials
  双写（rc 优先 + 列回落，对齐迁移回填形态）；**不同 repo_key 播不同值**
  （linked/hello→gc_seed_tok/gh_seed_tok，o/hello→BOT）——返回 token 值即
  等价证明 repo_key 单前缀精确传递（保留原 seen["repo_key"] 断言语义）；
  teardown 清 4 行防混跑污染。
- 连带顺手清预存债（唯一断言更新）：test_issues_crud_mapping 水印断言
  `== "新正文内容"` → 前缀匹配——实现 issue body PATCH 追加
  `<!-- grape-auto-comment -->` 水印是**基线既有行为**（基线快照同败同因
  实锤），断言过时非行为回归；改为断言"以新正文内容开头 + 含水印注释"，
  语义更完整（既验证正文写入又锁定水印行为）。

**RED 验证**：git stash 修复 → 4 failed（毒化复现：m4kj4 穿透 + ByPath 3×502）
→ pop → 8 用例全绿。修复前另以独立原型脚本实弹验证四级链行为矩阵
（cross-gitcode/cross-github/no-prefix/default 四形态 200 + 各自 token +
clone_url 无前缀泄漏）。

**对账**：本文件 60 passed（6 净失败清零 + 水印债清零）；与 S114 迁移五文件
+ s111/s112/s113 守卫混跑 211 passed 零隔离污染；相邻域（global_bot_token_db/
s87）110 passed。**不动项**：tests/conftest.py OBS_LOG_DB=0（另一会话改动）；
untracked docs/ 若干（非本批产物）。

### D88：卡驱动漏网二元分叉收口（S115 续批：A2A fallback + templates 白名单 + admin verify-token）

**来源**：D71 全量 265 站点清点时登记冻结的"登记外二元分叉"（第七轮
能力矩阵挂账项：A2A fallback 判定 + templates 白名单两处挂账，域C admin
verify-token 为 D88 增补轮同族清点追加），用户确认"卡驱动的时候改漏了"
启动收口。三处均为**基线预存**，非本分支引入。

**归因三步法**（逐处实锤）：
1. 基线对照：`git show arch-optimization:webhook.py` L976 逐字存在
   `not in ("gitcode", "github")`（#671 引入）；templates 白名单 = weekly-07
   squash 777641b8f 预存；admin 白名单 = 同 squash 预存；
2. diff 引入行检查：三处判定行在本分支 diff 中均为 context（无 +/- 触碰）
   = 纯基线携带；
3. 下游能力链实测：VcsApiClient._request 卡驱动（D22，gitee 卡
   dialect=github + api_auth=query_token 天然可达）、create_provider 卡工厂
   （S72 注册表 + GenericVcsProvider 缺省）、gitee webhook backend（adapter
   _generic_backend_for 卡派生）、模板 push（S90 卡派生 host）、bot 配置
   （卡派生 meta key）、config_store.verify_token（D30/S50 卡驱动）——
   下游全链路就绪，只差入口判定最后一米。

**修法**（3 文件各 1 处判定，对齐 webhook.py L1751 既有卡派生守卫写法；域C 详见下文同族清点段）：
- 域A webhook.py `_a2a_repo_fallback_check`：`not in ("gitcode","github")` →
  `get_platform_card(_provider) is None` 卡派生。gitee/gitlab 仓 A2A 归属校验
  从"直接 unsupported"变为"真探测"（ok/not_found/no_permission 细分 verdict
  全平台生效）；未知前缀与残缺 owner/repo 照旧 unsupported（防御保留）。
- 域B templates.py `create_from_template`：`body.provider in ("gitcode","github")`
  → `body.provider and get_platform_card(body.provider) is not None`。显式传卡
  在册平台即走该平台 token 链；无卡平台（nosuchplatform）回落自动选择分支
  （与空 provider 同待遇，不 500）。

**测试**（tests/test_s115_platform_gate_closure.py 新文件，16 用例：域A 6 + 域B 3 + 域C 5 + 结构守卫 2）：
- 域A 6：gitee/gitlab 探测 ok/404→not_found、存量双平台逐字节回归、未知平台
  unsupported、残缺 owner unsupported（mock 缝对齐 test_issue_671 既有写法：
  TokenResolver.get_token + VcsApiClient.get_repo_info 方法级）；
- 域B 3：显式 gitee 走 gitee token 链、无卡平台回落自动选择（不进显式链）、
  显式 gitcode 回归；
- 结构守卫 2：两函数源码零二元判定残留 + 必含 get_platform_card（防回潮）。
- **否决理由**：①域A 否决"保留二元判定+追加白名单平台"——每接一平台改一行代码
  正是本批要消的形态（卡在册即真探测，零代码接入语义才算达成）；②域B 否决"白名单
  外抛 400"——未知平台回落自动选择与空 provider 同待遇，前端误传不致命（与基线
  else 分支行为一致）；③两处均否决"引入新的平台能力查询函数"——get_platform_card
  即既有能力点（webhook L1751 同款），新增封装=第三种判定形态。
- **RED**（git stash 修复前首跑，7 failed 逐条归位）：域A 3×unsupported
  （test_gitee_repo_reaches_probe / test_gitlab_repo_reaches_probe /
  test_gitee_repo_not_found_verdict）+ 域B 2×白名单拦截（test_explicit_gitee_
  uses_gitee_token / test_unknown_provider_falls_to_auto）+ 结构守卫 2×残留
  （test_webhook_a2a_no_binary_gate / test_templates_no_binary_gate）→ 修后 11 全绿。
- 连带顺手迁 2 处 S114 域1/D42 迁移漏网 mock（test_issue_671：旧符号
  get_user_token_with_refresh → get_user_token_for keyword 形态；建仓链旧 mock
  repos.create_repo 符号已被 D42 适配器端口消费淘汰 → 改工厂缝 create_provider
  返回桩适配器——mock 穿透姊妹案例第三例）。

**同族全仓清点（修一 grep 十原则，D88 增补）**：全 src/ 扫
`in ("gitcode","github")` 形态 9 处，逐站点语义分类——
- **收口（域C，本轮并入）**：admin/routes.py `verify_platform_token` 白名单 →
  纯卡派生。下游 config_store.verify_token D30/S50 已卡驱动（任何已配平台
  可用），gitee/gitlab token 验证从 400 INVALID_PROVIDER 变为真验证；未知
  平台照旧 400（防御保留）。测试 5 用例（gitee 进入链/存量回归/未知平台/空
  token/结构守卫圈定函数体），RED stash 复验 2 failed→绿。**修法教训：首版
  写成 `not in (...) and card is None` 复合形态，被自家结构守卫抓出（守卫红
  是对的——if-else 按平台名分叉必须全改，保留二元短路=半截工程）**。
- **合法冻结（登记不动项，防下轮重复报）**：
  - db.py 2381/2942：gc/gh 走历史列回落、新平台走 rc 泛型键（增量扩展非排除）；
  - repos.py 70：token 探测优先级顺序（默认排序非准入）；
  - scenes.py 1057：同名仓归属双轮探测（2026-10-24 生产事故修复结构）；
  - scenes.py 1090/1143：D19 已收口（带前缀直接保留+卡派生探测）；
  - webhook.py L1755：generic webhook 404 守卫（存量双平台走专属 backend，
    新平台走 generic——分流语义本身，非准入排除）；
  - admin/routes.py L420 whitelist：**表结构物理边界**（login_whitelist 仅
    gitcode/github 两列，D43/S63"无列平台拒绝不折叠"设计决策，新渠道列 DDL
    归登录渠道批次）——不收，登记。

**对账（D88 终版）**：新守卫 16 用例 + issue_671 回归 = 27 passed；模板/openapi/
git_repo_api/方言/S111-S113/admin 域守卫回归 174 passed（终版含 admin 两文件）；唯一失败 s66
list_collaborators = 预存（stash 对照同败，S66 混合债域，与本批无关）。
**不动项**：s66 混合债；admin whitelist（表结构冻结）；tests/conftest.py
（另一会话）；untracked docs/ 若干。


## Phase 5 白名单清除计划（check-arch 豁免登记的到期判据）

多平台解耦合并进 arch 布局时，`scripts/check-arch.py` 新增 13 条豁免登记（5 条 R1
kernel→domains 函数内延迟查表 + 8 条 R6 基线存量 shim import）。登记为
`(文件 × 导入目标)` 精确粒度（dict 形态 `{"targets": [...], "reason": ...}`；
字符串形态为历史整规则豁免，仅存量条目保留）——同文件今后的新增依赖不被吞掉。

清除判据（到期即删，不留白名单常驻）：

- R1 五件（mysql_compat→otel、repo_key/repo→platform_card、channel_strategy→
  providers/user、provider_link→providers/platform_card）：Phase 5 contracts 层
  落地后，各延迟查表反转为经 `src/domains/contracts` 的正置依赖，对应条目删除。
- R6 八件（aishell_acp、event_pipeline、account_env、direct_executor、webhook、
  events、issues、scenes 的存量 shim import）：调用方迁移到 canonical 模块路径
  即删（本 PR 已示范四处：otel→kernel.config、event_pipeline 三处调用方）。
- 每删一条，check-arch 必须 PASS（R4 cross-domain 160 条为 report-only，随
  Phase 5 一并收敛，不占豁免）。
