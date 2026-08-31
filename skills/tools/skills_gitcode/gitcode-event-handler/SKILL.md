---
name: gitcode-event-handler
description: 处理 GitCode webhook 事件(Issue/PR/评论/分发), 联动 Multica 四角色小队与数据库入库
category: software-development
---

# GitCode 事件处理器

通过 Hermes Gateway 的 webhook 路由(`/webhooks/gitcode-event`)接收 GitCode 推送的事件,
按固定业务逻辑处理: 识别 skill 开发任务 → 创建 Multica issue 并分配对应角色小队 →
数据入库 → 评论转发 → 进展通知。

## 触发条件

- Gateway 收到 GitCode webhook 事件, 会话 prompt 包含原始 payload
- 事件类型: issue 创建、issue 评论、PR(merge_request)创建/合并、PR 评论
- 手动处理历史事件 payload 文件

## 角色小队(默认, 均可环境变量覆盖)

| 角色 | 环境变量 | 默认 ID | 默认名称 | 负责逻辑 |
|------|----------|---------|----------|----------|
| 开发 | SQUAD_DEV / SQUAD_DEV_NAME | c5287391-b8c3-4843-ae77-5610eecd6b04 | skills_dev | 1 (issue 开发) |
| 测试 | SQUAD_TEST / SQUAD_TEST_NAME | c05558fa-8c50-43af-b80a-b9b0ea0c9650 | skills_test | 3/4 (PR 测试) |
| 验证 | SQUAD_VERIFY / SQUAD_VERIFY_NAME | 9a6d8a29-b2db-4329-b6e4-60b6870af834 | skills_verify | 3/4 (测试后验证) |
| 分发 | SQUAD_DISTRIBUTE / SQUAD_DISTRIBUTE_NAME | 77137221-0956-4d9c-b67c-71a210fb7816 | skills_distribute | 7 (distribute 分支) |

## GitCode API 实用技巧(2026-08-08 实测)
- **重新打开已关闭 issue**: `PATCH /repos/{repo}/issues/{n}` body=`{"state": "reopen"}`
  (state=open 会 400; state_event=reopen 报"至少一个参数缺失"是 GitCode 的
  校验 bug, state=reopen 最可靠)
- **关闭 issue**: `PATCH .../issues/{n}` body=`{"state": "close"}`(close_gitcode_issue)
- **删除 issue 评论**: `DELETE /repos/{repo}/issues/comments/{comment_id}`
  (注意路径是 issues/comments/{id}, 不是 issues/{n}/comments/{id})
- **issue 全局唯一 id**: GitCode issue 有全局唯一 `id`(如 4231475)与仓库内
  `number`(#221); DB 按 gitcode_issue_id(全局 id)匹配

## 部署要求(webhook 全自动处理)

### 本机部署状态(2026-08-02, 主机 /home/wangliangchu)

- 脚本实际路径: `/home/wangliangchu/.hermes/skills/software-development/gitcode-event-handler/scripts/gitcode_event_handler.py`
  (config.yaml 路由 prompt 中已用此路径, 勿再用 /home/skills 旧路径)
- `platform_toolsets.webhook: [terminal, file, hermes-webhook]` 已配置(否则 webhook
  会话无 terminal/file, 无法执行脚本)
- gateway payload 落盘补丁已应用到 `gateway/platforms/webhook.py`(升级 Hermes 后需重新应用)
- webhook 监听: 0.0.0.0:8080, 路由 `/webhooks/gitcode-event`, 签名验证实测通过
- nginx 反向代理: `location /webhook/skillbot` → `proxy_pass http://127.0.0.1:8080/webhooks/gitcode-event;`
  (sites-available/gitcode-pr-bot, 修改后 `sudo nginx -t` + `sudo systemctl reload nginx`)
- 飞书投递: 已配置 `platforms.feishu.home_channel`(chat_id=oc_f34bbe223c4125e209b0612c4acc211f,
  gateway 运行中会自动补 user_id), webhook 路由 `deliver: feishu` 投递实测成功
- 已知问题: 路由 `deliver: feishu` 但飞书无 home_channel/chat_id 时投递失败
  ("No chat_id or home channel for feishu"), 不影响事件处理主流程;
  用户用飞书给 gateway 发一条消息即自动记录 home_channel 恢复投递

webhook 事件要自动执行脚本, 需要两处配置(config.yaml):

```yaml
platform_toolsets:
  webhook:                    # 默认只含 web/vision/clarify(安全限制),
    - terminal                # 必须显式加 terminal + file 才能执行脚本
    - file
    - hermes-webhook
platforms:
  webhook:
    extra:
      routes:
        gitcode-event:
          secret: REPLACE_WITH_WEBHOOK_SECRET
          prompt: |-
            处理GitCode仓库事件。payload 完整内容已由 gateway 保存到 {_payload_file}, 立即执行:
            python3 /home/wangliangchu/.hermes/skills/software-development/gitcode-event-handler/scripts/gitcode_event_handler.py {_payload_file}
            用中文简要报告脚本输出(事件类型/处理动作/关键ID); 若失败报告错误原因。
          skills:
            - gitcode-event-handler
```

注意: `hermes config set` 会把数组存成字符串, 需要手动修正 YAML 类型。
修改后必须重启 gateway: `hermes gateway restart`。

### ⚠️ 必须的 gateway 补丁(升级 Hermes 后需重新应用)

`gateway/platforms/webhook.py` 的 `_handle_webhook()` 渲染 prompt 前新增
**完整 payload 落盘**(唯一改动, 框架测试 138 项全绿):

```python
        # Save the FULL raw payload to a per-delivery file so event-handler
        # scripts can read it directly — {__raw__} is truncated to 4KB and
        # concurrent webhook sessions would overwrite each other's copy.
        try:
            _delivery_id = request.headers.get(
                "X-GitHub-Delivery",
                request.headers.get(
                    "svix-id",
                    request.headers.get("X-Request-ID", str(int(time.time() * 1000))),
                ),
            )
            payload_file = f"/tmp/gitcode_payload_{_delivery_id}.json"
            with open(payload_file, "w", encoding="utf-8") as _pf:
                json.dump(payload, _pf, ensure_ascii=False, indent=2)
            payload["_payload_file"] = payload_file
        except Exception as _e:
            logger.warning("[webhook] 保存 payload 文件失败: %s", _e)
```

背景: GitCode 的 merge_request payload 通常 15KB+, 而 `{__raw__}` 模板
只渲染前 4000 字符(框架设计, 有测试锁定), 直接依赖它会导致
`object_kind`/`object_attributes` 丢失 → 脚本识别为 "object_kind=other"
而忽略(2026-08-01 曾因该问题漏处理 PR #3)。改为 gateway 落盘 + prompt
用 `{_payload_file}` 传路径, 同时解决: a) 大 payload 截断; b) 并发
webhook 会话共用 /tmp/gitcode_payload.json 互相覆盖; c) agent 用
write_file 复制 15KB payload 浪费 token。

## 核心脚本

```
scripts/gitcode_event_handler.py
```

```bash
# 处理 webhook payload 文件
python3 scripts/gitcode_event_handler.py /tmp/payload.json

# 从 stdin 读取(webhook 会话中推荐)
echo '<json>' | python3 scripts/gitcode_event_handler.py -

# 测试→验证 交接轮询(建议 cron 每 5-10 分钟):
# 扫描测试小队名下已完成(done/in_review/closed)的 issue, @验证小队并转移分配
python3 scripts/gitcode_event_handler.py --check-handoff

# 自检(配置 + 数据库/GitCode/Multica 连通性 + 四小队 ID 校验)
python3 scripts/gitcode_event_handler.py --test
```

## 事件识别

基于 GitCode webhook payload 的 `object_kind` 字段:

| object_kind | 事件 | 处理逻辑 |
|-------------|------|----------|
| `issue` (action=open) | Issue 创建 | 逻辑1 |
| `issue` (action=update) | Issue 更新(用户编辑补充) | 逻辑1b |
| `note` (noteable_type=Issue) | Issue 评论 | 逻辑2 |
| `merge_request` / `pull_request` (action=open/merge) | PR 创建/合并 | 逻辑3/4/7/8 |
| `note` (noteable_type=MergeRequest/PullRequest) | PR 评论 | 逻辑5 |
| `push` | 仓库推送 | 逻辑9(push-sync, 按仓库配置) |

### 前置检查(devops-bot 迁移, handle_event 统一执行)

1. **忽略标签**: issue 事件(创建/更新/评论)先检查标签, 命中全局忽略标签
   (`codeowner` / `评测参考`, 环境变量 `IGNORE_ISSUE_LABELS`)→ 整体忽略,
   不处理/不通知/不分发。payload 无标签信息时调 GitCode API 兜底(fail-open)。
2. **机器人 issue 忽略**(2026-08-05 用户要求): issue 创建/更新事件先检查
   提交者账号, 命中 `BOT_AUTHOR_LOGINS`(默认 `skills_bot,check_bot`,
   逗号分隔, 大小写不敏感, 环境变量可覆盖)→ 整体忽略, 不评论/不打标签/
   不分配/不建 Multica 任务, 避免机器人自处理噪音(评测系统自动建单等)。
3. **PR 文件数上限**: PR 创建事件先拉变更文件列表, 超过 `PR_FILE_LIMIT`(默认 50)
   → 发 `[skills_bot]` 评论提示按 skill 规范拆分, 跳过处理。
   **幂等(2026-08-12 #41 修复)**: 发评论前用 `_pr_has_bot_comment(repo, number,
   MARKERS["pr_file_limit"])` 检查, 已发过同款提示则跳过 —— GitCode 对 PR 生命周期
   连推多个 open/update 事件, 无检查时每个事件都重发一条(PR #41 实测 15:48:37/39/40
   三秒内重复 3 条, 累计 5 条相同评论)。marker="超过单次审查上限" 已入 MARKERS 常量表。

### 逻辑1b: Issue 更新事件(2026-08-02 新增)
GitCode 创建 issue 后还会推送多个 action=update 事件, 且用户编辑 issue
补充 body 后也触发 update(此前全部 ignored, 导致 need_info 的 issue 补充
信息后永远不会升级, 标签也一直停留在 need-info)。处理:
1. **已有 multica_issue_id → 拒绝修改, 不同步 body**(2026-08-08 用户要求):
   需求已在开发中(Multica 任务已建), 用户修改 title/body 不生效, DB 保持
   原需求(与开发任务一致); 内容确实变化时评论告知"⚠️ 该需求已在开发中,
   对 issue 的修改**不生效**, 变更请新建 issue"(幂等 marker"对 issue 的修改
   不生效"; 内容与 DB 一致的重复 update 事件静默跳过不刷屏)
2. 无 multica 任务: 按 status 分流
   - status=need_info 或外部写入的 pending 等 → 用新 body 重新质量检查:
     通过 → 升级创建 Multica 任务分配给开发小队 + 更新 requirement
     (status=dispatched) + flow.node_preprocess=done + 打 `{主标签},triaged`
     + 评论通知; 仍不完整 → 同步 body + **若此前未发过质量追问评论则补发
     (2026-08-05 #132, 幂等防刷屏)**
   - status=duplicate/invalid → 终态, 仅同步 body 后跳过
   - status=codeowner_assigned → **若标题/正文已含 "skill" 关键词则继续走
     重新评估(2026-08-05 #132 修复: 用户把 issue 改成 skill 任务后应重新
     识别); 仍非 skill 任务则仅同步 body 跳过**
   - **升级路径同步 2.1/2.2(2026-08-08)**: 质量检查通过后先走 bug 分流
     (2.2: 无 skill 名提示修改 / 仓库无 skill 提示改需求 / 有 skill 按贡献者
     分流 bot->修复非bot->codeowner), 再优化类(2.1.3: 无/多 skill 名提示
     修改, 单名按贡献者分流), 竞品云(2.1.1), 相关性判定 none->无关关闭
     (2.1.4)、weak->默认华为云(2.1.2) —— 与 handle_issue 创建路径一致;
     补 handle_issue_update 缺失的 author_login/author_name 变量(此前
     codeowner 调用会 NameError)
3. **DB 无记录时不再重走 handle_issue 创建**(2026-08-06 修复 #168 多标签):
   GitCode 创建 issue 后连推 create+多个 update 事件, handle_issue 内部
   先 multica_issue_create 再 db_insert_requirement(存在窗口期)——
   update 事件在窗口期到达(exist=None)若重走创建会再次 multica_issue_create
   撞 duplicate 抛异常(SKI-157 实测)且重复打标签。修复: exist=None 时
   检查 GitCode 是否已有 bot 处理评论(建任务/追问/无关等 marker), 有则
   跳过; 无则也静默跳过 —— 创建事件(GitCode 必发)负责完整流程, update
   不主动补建

### ⚠️ Payload 格式: GitCode 实际发送 GitLab 风格(实测)

GitCode webhook 真实 payload 是 **GitLab 风格**(`object_attributes` 结构):

```json
{
  "produce_random_id": "...",
  "event_type": "issue",
  "object_kind": "issue",
  "object_attributes": {
    "id": 4211473,          // issue 全局 ID
    "iid": 3,               // issue 编号(用于 API 调用)
    "title": "...",
    "description": "...",
    "action": "open",
    "state": "opened",
    "url": "https://gitcode.com/.../issues/3",
    "author": {"username": "songspace", "name": "吕军涛", ...}
  },
  "project": {"path_with_namespace": "songspace/shelltest", ...},
  "user": {...}
}
```

merge_request 事件额外含 `target_branch`(合入分支)/ `source_branch`(源分支),
逻辑7 依赖 `target_branch == DISTRIBUTE_BRANCH` 判断分发 PR。
脚本通过 `normalize_payload()` 在解析前自动把 GitLab 风格转换为内部 Gitee
风格, 两种格式都能处理。

## 处理逻辑

### 逻辑1: Issue 创建事件
1. 幂等检查: `gitcode_issue_id` 已存在且有 multica_issue_id → 跳过; status=need_info(追问中) → 跳过; status=codeowner_assigned → 跳过
2. 识别 skill 开发任务: 标题/描述匹配 `/skill/i`; **不匹配 → 走 codeowner 分配**(逻辑1c),
   而非直接跳过——该 issue 可能是开发者维护的 skill 出问题, 应按仓库 codeowner 映射
   分配给对应模块负责人
2.5 **bug 类 issue 分流(2026-08-08 判重 2.2 重写, 用户要求)**: `classify_issue`
   判为 bug 时, 按 2.2 流程:
   - **2.2.1 前提**: 必须提供 skill 具体名称 —— 未提供(标题+正文提取不到
     huawei-cloud-xxx / xxx-skill) -> 评论提示修改 issue(marker"未提供具体
     的 skill 名称"), 返回 action=bug_need_skill_name
   - **2.2.2 提供 skill 名后**: ① 查仓库是否有该 skill(`_repo_has_skill`,
     products 缓存 + contents 兜底; 全局限流时返回 None 降级走修复流程,
     不误报"无此 skill")—— 无 -> 评论"仓库中不存在 skill, 是否需要走需求
     开发, 请修改 issue 内容为需求内容"(marker"仓库中不存在 skill"), 返回
     action=bug_skill_not_found; 有 -> ② **2026-08-13 用户要求: 无论 skill
     是否人工开发, 一律直接进入机器人修复流水线**(`_bug_is_bot=True`, 建
     Multica 任务给 skills_dev, @skills_dev 附上 skill 名称)。原贡献者分流
     (`_skill_has_bot_contributor` 判断 + codeowner 分配艾特提交人)已移除;
     质量检查统一由主路径执行(描述过简仍 need-info 追问)
   - **2.2.3 无关内容** -> 直接提示并关闭(由华为云相关性判定 none 分支
     invalid_unrelated 处理)
   - 注: 旧逻辑用 `_is_bot_developed_skill`(查 DB requirement 表 + multica +
     status 终态)判断, 已按用户 2026-08-08 要求改为纯 git 贡献者判断;
     #187 场景(「huawei-cloud-evs-list 一直等待」)在新逻辑下: 提取到 skill
     名 -> 仓库有 -> check_bot 贡献 -> 走修复流水线, 行为不变
   - ⚠️ **GitCode 429 限流是高频外部因素**(2026-08-08 实测 12 类目全 429):
     `_get_repo_products` 重试 2->4 次 + 失败类目 >3 不缓存;
     `_repo_has_skill` contents 兜底全失败返回 None; 调用方降级走修复流程
     (宁走自动修复不误关/误指派)
3. **自动打标签**(skill-issue 集成, ENABLE_LABELS): `classify_issue()` 按
   关键词判定 feature/bug → **质量检查与判重均通过后**统一打
   `{主标签},triaged`; 质量不达标时打 `{主标签},need-info`(保留主标签,
   避免整体替换覆盖丢失类型)
   - bug 线索(2026-08-04 扩充, 修复 #98「查询数量不对」误判 feature):
     显式词 bug/故障/报错/错误/不生效/白屏/异常/500/堆栈/traceback;
     结果不符预期 不对/不正确/有误/有问题/出问题/错了/数量不对/结果不对;
     查询失败 查询不到/查不到/获取不到/拿不到/找不到/检索不到;
     无响应 没反应/无响应/卡住/卡死/超时(了/导致/报错)/无结果;
     不可用 无法/不能用/用不了/不可用/xxx失败;
     数据问题 失效/缺失/缺少/丢失/为空/重复数据/出现重复;
     崩溃乱码 崩溃/闪退/乱码/页面空白/错乱
   - feature 线索: 建议/需求/新增/支持/实现/希望/能不能/是否/可以(加|做|增加)
   - ⚠️ 组合词防误伤: 裸「不能/失败/超时/重复/为空/空白」会误伤 feature
     (「能不能」「支持失败重试」「支持去重」), 必须用组合模式匹配
   - **LLM 复核(2026-08-06 用户要求, ENABLE_LLM_REVIEW 默认开)**: 规则命中
     bug/feature 线索后调大模型复核, 冲突(规则判 bug 而 LLM 判 feature 或
     反之)以 LLM 为准; LLM 失败回退规则结论(fail-open)。关闭该开关回到
     纯规则快判。配置: ENABLE_LLM_CLASSIFY(总开关)/ENABLE_LLM_REVIEW(复核
     开关)/LLM_API_BASE(默认 https://api.deepseek.com/v1)/LLM_API_KEY(默认
     取 HERMES_CUSTOM_API_DEEPSEEK_COM_API_KEY)/LLM_MODEL(deepseek-v4-flash)/
     LLM_TIMEOUT(30s)
   - 无明确线索且正文非空 → 交给 LLM 判(feature/bug); LLM 失败/未启用 →
     按 feature 兜底(仓库是 skill 开发仓库)
   - **判重 LLM 语义确认(2026-08-07, ENABLE_LLM_DUP_CHECK 默认开)**: 规则
     弱信号(同产品词+同功能词, find_similar_requirement_in_db)命中候选后,
     调 LLM 确认是否语义重复(`_llm_is_duplicate`)。防两类误判: 同产品不同
     操作被误关(查询vpc vs 创建vpc)、换说法漏判(查ASM列表 vs 获取ASM名称
     清单)。LLM 判不重复/失败 → 按不重复放行(fail-open 不误关)。规则强信号
     (技能名全称/标题一致)不调 LLM 直接判
   - **判重关闭不入库(2026-08-07 用户要求, #181)**: 3 处判重分支
     (仓库已开发过 find_skill_in_repo / 已有 PR 提交 find_skill_in_open_prs /
     DB 已有类似需求 find_similar_requirement_in_db)命中时**只评论+关闭,
     不写 t_skills_dev_requirement**(requirement_id=None)。幂等靠评论 marker
     ("已在仓库中开发过" / "已有 PR 提交" / "已有类似需求" / "该需求已开发完成")
     防重复 webhook 刷屏
   - **#221 修复(2026-08-08)**: ① find_similar_requirement_in_db 增加
     **标题归一化完全一致直查**(不依赖产品词库)—— 产品词库未收录的产品
     (vpcep 等)时旧逻辑 products 为空直接 return, 同标题需求漏判后撞
     Multica duplicate 校验崩溃; ② 该直查**只有已交付(status=repoed/
     deployed)才判重关闭**, 未交付(developing/testing/tested/repoing 等,
     skill 未真正合入主仓)时**不判重放行走开发流程**(#221 实测: #212
     tested 但 PR #166 关闭未合入, 仓库无 vpcep skill, #221 是真实需求);
     ③ multica_issue_create 撞 "Active duplicate issue exists" 时**复用
     已有同标题任务不崩溃**(_find_existing_multica_issue), 避免判重漏判
     时 webhook 处理整体崩溃导致 issue 无响应
     防重复 webhook 刷屏
   - **实时判重查全量历史(2026-08-07 用户要求, #187)**: find_duplicate_issue
     查重范围从 open 扩到**全部状态**(state=all, 最近 100 个含 closed)——同
     一需求重复提交(如 #179/#182/#186/#187 同标题)时, 即使原 issue 已关闭
     也能命中判重。命中时评论"🔁 该需求与 #N 重复" + 关闭, 不入库(幂等靠
     评论 marker "已在原 issue 中跟进")
   - **判重提前到 bug 分流/codeowner 之前(2026-08-07 用户要求, #193)**: 原
     判重检查在华为云相关性检查之后, bug 非机器人开发 skill 的 issue
     (is_bot=False)在 bug 分流直接走 codeowner return, 判重执行不到 ——
     #185/#193/#194 "ges-graph 连不上"重复提交未被拦截。已把判重移到
     exist 幂等检查之后、通用守卫之前, 所有路径先判重
   - **closed 命中区分关闭原因(2026-08-07 讨论)**: find_duplicate_issue 返回
     (issue, kind), kind ∈ open / closed_dup / closed_other —— open(活跃重复)
     直接判重关闭; closed_dup(该 issue 上有 bot 判重评论, 因重复关闭)同样
     判重关闭; **closed_other**(已完成/need-info/invalid 等其他原因关闭)只发
     ℹ️ 提示评论("该需求与已关闭的 #N 相似")**不自动关闭**, 避免误伤"修正后
     重提/迭代增强"的合法新需求
   - **统一判重入口(K3 重构 2026-08-09)**: `_dup_check_issue(repo, title, body,
     number, label, exclude_id)` 收敛 5 链判重 —— LLM PR/skills 覆盖(feature
     类) + 实时判重(bug 用 LLM 同一问题语义, feature 用规则) + DB 相似需求
     (#221)。**handle_issue / handle_issue_update 统一调用**, 消除两处判重
     语义漂移(#231/#241/#248 三次修复均源于调用点不一致)。只判定不动作,
     评论/关闭/入库由调用方按各自语义处理(handle_issue 不入库,
     handle_issue_update 额外 update status=duplicate)。
   - **幂等 marker 常量表(S3 重构 2026-08-09)**: 所有 marker 集中为
     `MARKERS` 常量表(脚本 L150 附近), 禁止散落字符串字面量。⚠️ 硬性约定:
     **marker 必须是评论正文真实子串**(含 markdown 加粗/@ 链接等都要一致),
     否则幂等失效刷屏 —— 重构时通过 tests/test_markers_quality.py 自动校验,
     已顺带修复 3 个历史失效(dev_in_progress_lock/comment_change_rej 含 **、
     pr_merged_dev_notice 中间有 @作者)。
4. **竞品云前置检查**(2026-08-05 新增, 修复 #130): `is_competitor_cloud()`
   判定为竞品云(阿里云/腾讯云/AWS 等)→ 打 `invalid` 标签 + 评论"非华为云"
   + 关闭,**不写入 t_skills_dev_requirement**(2026-08-05 用户要求: 不符合的
   issue 不入库; 幂等由 `_issue_has_bot_comment("面向其他云厂商")` 保证)。
   ⚠️ marker 必须与评论正文真实关键词一致(2026-08-06 修复 #163 双评论:
   原用 "非华为云" 但正文无此词恒匹配失败, 重复 webhook 每次重发评论)。
   ⚠️ 标签也需幂等(2026-08-06 修复 #164 双标签): create+update 双事件会
   触发两次竞品云分支, set_issue_labels 必须与评论共用同一幂等检查,
   否则重复打 invalid 标签。
   **必须先于已存在检查**——竞品云需求与
   华为云 skill 绝不重复, 若排在后面会被 find_skill_in_open_prs 误判为
   "已有 PR 在开发" 错误关闭(#130 事故: 阿里云 vpc 需求被 PR #129 华为云
   vpc-list 拦截, 留了错误评论)
5. **已存在检查**(ENABLE_EXISTING_CHECK, 2026-08-04 新增, 修复 #112;
   **执行顺序提前到质量检查之前**, 修复 #118「dcs 需求 open 时质量不达标
   提前 return, 没机会识别仓库已有 skill」):
   重复开发防护, 三道判断(任一命中 → 评论提示 + 关闭 + 入库 duplicate):
   - `find_skill_in_repo()`: 需求对应 skill 是否已在仓库中开发过 —
     **仅按 skill 名称全称匹配**(huawei-cloud-xxx, 需求显式写的技能名在仓库
     已有同名目录才算; 按产品段优先只查对应产品目录, 减少 API 调用)。
     **不再按产品目录匹配** —— #119 教训: eip 产品目录存在
     (eip-cost-optimizer) ≠ eip-list 已开发, 产品目录存在不代表 skill 全称
     已存在
   - `find_skill_in_open_prs()`: **skill 名称是否与已提交 PR 重复**(用户要求
     2026-08-04) — ① 技能名精确匹配: 需求技能名出现在 PR 标题/body 即命中
     ② 产品名匹配: 需求产品词出现在 PR 标题 → 拉该 PR 文件列表确认含
     skills/ 下该产品文件(防误报)。拉 `pulls?state=all&per_page=30` 按更新
     倒序。如 #112「查询eip列表」对应 PR #130(add eip-list, open)
   - `find_similar_requirement_in_db()`: 需求表 t_skills_dev_requirement 中
     是否已有类似需求 — 同产品词 + 标题归一化互相包含或功能词(查询/列表/
     创建等)交集; 标题命中产品词优先排序; 进行中(dispatched/pr_created/
     verifying/repoing)提示"已在开发中", 已完成(repoed/tested/deployed)
     提示"已开发完成"。**正文命中产品词可能是复制残留(#113 教训), 排序
     权重低于标题命中**
6. **需求质量检查**(ENABLE_QUALITY_CHECK): `issue_quality_ok()` — bug 需有
   复现步骤/日志/堆栈等, feature 正文须 ≥30 字; 不达标 → 打 `{主标签},need-info`
   标签 + 评论追问 + 入库(status=need_info, 不创建 Multica 任务), 等用户
   补充后升级(评论补充走逻辑2意图2, 编辑 body 走逻辑1b)。
   **2026-08-09 #246(用户纠正): 质量检查对所有 issue 一律生效** ——
   曾为 #241 加过 `_bug_is_bot`(机器人开发 skill 的 bug)豁免, 用户纠正:
   描述过简就该先追问补充, 无论谁修复; 已撤销豁免
7. **华为云产品相关性检查**(ENABLE_HUAWEI_RELATED_CHECK, **2026-08-07 判重 2.1
   调整**): `huawei_related_level()` 判定 —— 竞品云(阿里云/腾讯云/aws/azure,
   COMPETITOR_CLOUD_WORDS)由前置检查 invalid 关闭(2.1.1); **无明确厂商 ->
   默认按华为云处理**(2.1.2, 不再追问产品词); 无华为云特征(产品词/cloud/云服务
   均未命中, 且非竞品云)-> **按无关内容 invalid 关闭**(2.1.4, 评论幂等
   marker"与仓库项目范围无关")。旧逻辑 none/weak -> need-info 追问产品词
   (2026-08-04 #113 温和模式)已按用户要求移除。
   **质量检查失败分支也先判无关(2026-08-13 #125 修复)**: 无关内容若描述短
   (<30 字)会被质量检查先拦截追问(need-info 追问 + 错误入库), 补充后才走
   2.1.4 关闭 —— 「上海天气很好」灌水 issue 实测。已修: 三处质量检查失败
   分支(通用 codeowner #244 / 主路径 / handle_issue_update)在发追问前先判
   `huawei_related_level()=="none" 且非优化类` -> `_close_unrelated_issue()`
   直接 invalid 关闭(不入库; update 路径已有 need_info/pending 记录一并
   删除 flow+requirement)。描述短但相关的需求仍正常 need-info 追问。
   **codeowner 分支质量通过后也判无关(2026-08-13 #126 修复)**: 无关内容
   描述够长(≥30 字)时质量检查通过, 旧逻辑打 feature,triaged 标签 + skip
   评论("未识别为 SKILL 开发任务...已跳过")后留在 open, 走不到 2.1.4 ——
   「今天天气怎么样」49 字灌水实测。已修: 通用 codeowner 分支质量检查通过
   后、打标签前加同样无关判断 -> 直接关闭。至此无关 issue 三态全覆盖:
   短描述(质量失败分支) / 长描述无 skill 词(codeowner 分支) / 长描述有
   skill 词(2.1.4 原有路径)。
7.5 **优化类需求处理**(2026-08-07 判重 2.1.3 用户要求): `has_optimization_intent()`
   命中优化/修改意图词时 —— **必须给出唯一具体 skill 名称**:
   - 未提取到 skill 名 或 提取到多个 -> 评论提示修改 issue(明确唯一 skill
     名称, marker"未明确指定要优化的 skill"), 不进入判重/开发流程, 等用户
     update 事件重新评估(返回 action=opt_need_skill_name)
   - 恰好一个 skill 名 -> **2026-08-13 用户要求: 无论 skill 是否人工开发,
     一律直接进入机器人开发流水线**(原"判断贡献者是否含机器人账号"分流 +
     codeowner 分配已移除, 与 bug 分流同步修改)。判重由 is_optimization_request
     跳过, 已存在检查由 is_skill_modification 跳过
   - **bug 类排除本分流(2026-08-13 #113 实测修复)**: 入口条件 `label != "bug"`
     —— 评测问题单正文含"改进/修改/修复"(修复建议等常规用语)会误命中优化
     意图词, 把 bug 报告劫持成优化类; bug 报告即使正文提改进也应走 bug 修复
     流程(与判重 2.0 bug 语义一致, #241)。配套: extract_skill_names 过滤
     `-issue-\d+` 结尾的问题单编号(如 huawei-cloud-monitoring-query-ISSUE-001
     不是 skill 名), 否则"多 skill 名"误拦截
   - 位置: 在华为云相关性判定(2.1.4 无关关闭)之前 —— 「帮我优化一下查询速度」
     无华为云特征但确属优化类, 应提示修改而非关闭; codeowner 分支(标题无
     "skill" 词)同样跳过优化类需求
8. **重复检查**(ENABLE_DUP_CHECK, **判重 2.0 2026-08-07 用户要求, LLM 语义驱动**):
   优化类需求(提到已有 skill 名 + 优化/增强/修改意图词, `is_optimization_request`)
   **不走判重逻辑**; 否则按序:
   - **LLM 第 1 步** `_llm_pr_covers_issue`: 当前 PR 列表(open+merged, 排除
     closed 未合并)能否覆盖 issue 功能点 → 能则评论"📦 该功能点已有 PR 实现
     (#N 链接)" + 关闭(不入库)
   - **LLM 第 2 步** `_llm_skills_covers_issue`: 仓库已有 skills 能否覆盖 →
     能则评论"📦 该功能点已有 skill 实现(`名称`)" + 关闭(不入库); skills
     清单先按产品词粗筛(≤15 候选)再喂 LLM 精判(73 个全量喂推理模型会漏看,
     实测 3 次仅 1 次命中, 粗筛后 3/3 稳定)
   - **DB 兜底** `find_duplicate_issue`: 同标题重复提交(历史含 closed)仍拦截
   - LLM 失败 fail-open 放行(不误关); max_tokens=300(推理模型 120 会被
     reasoning_content 占满致 content 空)
9. 创建 Multica issue, 分配 `skills开发小队` (SQUAD_DEV)
10. 入库 `t-skills-dev-requirement`(status=dispatched) + `t-skills-dev-flow`
   (node_source=done, node_preprocess=done)
11. GitCode issue 评论通知提交者(带 `[skills_bot]` 前缀)

### 逻辑1c: codeowner 模块负责人分配(2026-08-04 新增, 修复 issue #91/#92)
非 Multica 自动开发 skill 的 issue(标题/描述不含 'skill' 关键词)不再直接跳过,
改为按仓库 codeowner 映射处理:
1. **查映射**: 仓库中带 `codeowner` 标签的 issue(如 #72 "CODEOWNDERS"),
   其 body 是模块负责人映射表(``` 代码块内):
   `# 责任人：@username` / `# 模块：name` / `# 触发词：kw1, kw2` /
   `# 描述：...` / `# 仓库：owner/repo`(可选跨仓), 兜底行 `* @username`
2. **触发词匹配**: `match_codeowner_module()` 大小写不敏感子串匹配
   issue 标题+正文; 命中 → 取该模块责任人
3. **未命中模块 → 无关判断**(对齐 assignee-guide Step 5, 修复 #92):
   `is_unrelated_issue()` 检查标题+正文是否含仓库主题词
   (skill/技能/huawei/华为云/开发/cloud/ges/图数据库/graph/查询/cypher/gql)
   - **完全无关** → 打 `invalid` 标签 + 关闭 issue + 评论说明
     (对齐 #24 的处理: labels=invalid + state=close), **不写入
     t_skills_dev_requirement**(2026-08-05 用户要求; 幂等由
     `_issue_has_bot_comment("与仓库项目范围无关")` 保证)
   - **可能相关但无法确认** → 走兜底负责人(`* @username`), 让人工判断
4. **分配**: `PATCH /issues/{n}` `{"assignee": "username"}` + 评论通知
   `[skills_bot] 👤 该问题属于开发者维护的 skill, 已按 codeowner 映射分配给 @xxx 处理。`
4.5 **分配前打标签(2026-08-07 用户要求, #184)**: codeowner 分配前先
   `classify_issue` 分类并打 `{feature|bug},triaged` 标签(与正常流水线一致,
   便于 issue 分类追踪); 两个入口(is_skill_task=False 分支 / bug 分流分支)都补
5. **不入库(2026-08-07 用户要求, #182)**: codeowner 分配只评论+指派,
   **不写入 t_skills_dev_requirement**(与判重关闭/竞品云一致);
   幂等由 `_issue_has_bot_comment("已按 codeowner 映射分配")` 保证
   (重复 webhook 不刷屏)。注意: 历史遗留的 status=codeowner_assigned 记录
   仍按幂等跳过兼容, 但新记录不再产生
6. 无映射/无兜底 → 评论说明, 不关闭 issue, 不入库
7. 开关: ENABLE_CODEOWNER_ASSIGN(默认 1); 与 IGNORE_ISSUE_LABELS 的 codeowner
   不冲突: 前者用于"带 codeowner 标签的映射 issue 本身忽略", 后者用于
   "普通 issue 按映射分配开发者/关闭无关"

### 逻辑2: Issue 评论事件
1. 评论以 `[skills_bot]` 开头 → 程序自身评论, 跳过(防死循环)
2. **评论意图处理**(skill-issue 集成, ENABLE_COMMENT_INTENT):
   - 意图1: 用户请求关闭(请关闭/close/不用做等) → 关闭 GitCode issue +
     更新 DB state=closed + 评论确认
   - **无关评论过滤**(2026-08-05 用户要求, ENABLE_COMMENT_FILTER, 意图1 之后、
     意图2 之前): `is_irrelevant_comment()` 判定 — 灌水短文本(≤8 字且命中
     灌水词集 IRRELEVANT_COMMENT_WORDS 且无实质信号)/纯 emoji/与需求无关的
     闲聊(功能词集 COMMENT_FUNC_WORDS + 产品词库 + issue 标题归一化均无交集)
     → **静默跳过**(记日志, 不转发 Multica、不回复、不触发意图2 追问);
     关闭请求类词豁免(由意图1 先行处理); 判定异常 fail-open
   - 意图2: 该需求 status=need_info 且用户补充信息 → 以「原正文+补充评论」
     重新质量检查: 通过 → 创建 Multica 任务分配开发小队 + 更新 requirement
     (status=dispatched) + flow.node_preprocess=done + 打 `{主标签},triaged` +
     评论通知; 仍不足 → 评论继续追问(不创建任务)
3. **开发中 issue 的评论按内容分流**(2026-08-08 用户要求): 已有 multica 任务的
   issue 收到用户评论时, 命中**变更意图词**(改成/改为/换成/新增/增加/添加/删除/
   去掉/移除/不要/不需要/追加/取消 等)→ 评论拒绝"评论中的变更要求**不生效**,
   开发仍按原需求进行, 变更请新建 issue"(幂等 marker"评论中的变更要求不生效"),
   **不转发 Multica**(与 title/body 修改的拒绝语义一致); 其他评论(反馈/补充/
   优化建议)正常转发给开发小队
4. 按 `gitcode_issue_id` 查需求表 → 无记录则评论通知后跳过
5. 去重: 评论内容含 "GitCode 评论 ID: {note.id}" 的已转发记录 → 跳过
6. 转发到 Multica issue: 原文 + `@skills开发小队` mention
7. GitCode 评论通知提交者"已转发"

### 逻辑3: PR 创建事件(匹配到 issue)
0. **提交者分流**(2026-08-08 用户要求 1.1/1.2): PR 按提交者区分处理 ——
   - **机器人(skills_bot/check_bot)提交**: 预检查(审查报告)不通过 -> 审查
     报告写入 **multica 评论 + @skills_dev 处理**(不写 PR 评论); 每次 commit
     提交(act_desc=source_branch_changed)强制二次预检查; 审查 pass/warn ->
     **不做任何处理**(不 @skills_test, action=bot_pr_reviewed)
   - **开发者提交**: 预检查不通过 -> **仅 @开发者** 人工修改(PR 评论 @作者,
     幂等 marker"审查报告修改后重新提交"——**marker 必须与正文真实关键词
     一致**, #170 实测旧 marker"请根据审查报告修改"不在正文(正文是"请根据
     上方 PR 审查报告修改后"), 导致每个 update 事件都发一条重复 @开发者
     评论); 预检查通过 -> @skills_test 处理 + 贴任务链接(devflow, 现有流程)
0. **PR 预校验**(skill-pr 阶段0 迁移, ENABLE_PR_PRECHECK 默认开, 2026-08-04 修复 #126):
   进入测试流程前先做配置检查:
   - ① 标题规范: `{add|update} skill: [skills/]{path}/{skill-name} [v{version}]`
     (PR_TITLE_RE); 不匹配 → `_infer_pr_title()` 从变更文件列表推断
     (取第一个 skills/ 路径 + added/modified 判定 action) → PATCH 自动修复
   - ② 描述非空 ③ body 含 `Closes/Fixes/Related #N` 关联 Issue
     ④ 有 feature/bug/enhancement 分类标签 ⑤ 已设置 Assignee
   - 发现问题 → 评论提醒(幂等: 已有 `[skills_bot] PR 配置检查` 评论则跳过),
     **不阻断后续流程**(对齐 skill-pr 阶段0 "评论提醒...不阻塞后续流程";
     2026-08-04 修复 #131: 此前 not ok 直接 return precheck_failed, 导致审查
     报告从未生成)。进测试小队的关卡是 0b 审查报告(fail 才阻断)
0b. **PR 审查报告**(skill-pr 阶段1-5 迁移, ENABLE_PR_REVIEW 默认开, 2026-08-04):
   预校验通过后输出完整审查报告到 PR 评论(幂等), 含:
   - **评审人自动设置(2026-08-12 用户要求, ENABLE_PR_REVIEWER_ASSIGN 默认开)**:
     审查报告发出后自动设置 PR 评审人(approval_reviewers) —— 开发者 PR(非
     skills_bot)固定 DEFAULT_REVIEWER(胡盖/HugLovesWine); 机器人 PR 按关联
     issue 标签分流: bug=skill 贡献者(`_skill_primary_contributor` 提交历史
     推断, 如 lccode) / feature=issue 创建人; 无 feature/bug 标签
     静默跳过; 找不到评审人回退 DEFAULT_REVIEWER。约束(2026-08-12 API 实测):
     ① 评审人必须是仓库 collaborator(仓库外用户 400 `The user must have
     matching operation privileges`), 目标不在 collaborator 列表则回退胡盖;
     ② PR 作者不能设自己为评审人(`Merge request author cannot operate their
     own merge request`), 作者=目标时跳过; ③ 幂等: approval_reviewers 已含
     目标则跳过。API: `POST /pulls/{n}/reviewers` body `{"reviewers":"login"}`
     (逗号分隔多人), DELETE 同格式移除。collaborators 查询有文件缓存
     (/tmp/gitcode_repo_collaborators_cache.json, TTL 600s 防 429)。
     测试 tests/test_pr_reviewer_assign.py(10 用例)。
    - 关联 Issue(状态/标签) + 变更摘要(文件数/+/- 行数/涉及 Skill)
   - **Skill 审查 31 项检测点**(SKILL_CHECKLIST 对齐 skill-checklist.md):
     结构类(目录/命名/frontmatter/章节/references 文件/文件数/大小/行数/
     类型白名单)精确检查; **检测点31「Skill目录存在性检查」(2026-08-14 新增,
     2026-08-16 升级, 🟡 提示级不阻断)**: 对 SKILL.md 所在路径的**每一层
     父目录**做仓库存在性检查(`_repo_dir_exists`: contents API, 404=不存在/
     429=查询失败 精确区分), 直接输出逐层结论(路径完整存在 ✅ / 路径断裂
     ⚠️ 指出断点层) —— 中间层缺失=类目路径错误, 仅最后一层缺失=skill 根
     新增(客观事实陈述, 不再写"add 属正常/update 请核对"类需人工判断文案);
     查询失败 fail-open 跳过确认; 实现: 先查最深层(存在则父层必在, 1 次
     API), 不存在则自浅向深探测找断点(≤层数 API); 语义类(服务名/操作名)
     启发式; 安全类(凭证硬编码扫描, 排除占位符 `{AccessKeyId}` 教程)精确检查
   - **frontmatter 块标量支持 `|` / `|-` / `>` / `>-`**(2026-08-09 #171 修复):
     原只处理 `|`, `>-`(折叠块标量)被当普通值 `>-` 存入 → description 触发词
     检查匹配 `>-` 失败 → "未检测到触发词"误判。literal(`|`)保留换行,
     folded(`>`)空格连接
   - **references/ 文档补入缓存**(2026-08-09 #171 修复): 只改 1 个文件的
     PR 里 references/*.md 不在变更列表被误报"缺失" —— pr_review_report
     补入未变更 references 目录 + _skill_review names 合并 file_contents keys
   - **判重优先于规则(2026-08-09 #231 修复)**: LLM 判重(判重 2.0)执行过且
     未命中时, 已存在检查的 PR 分支 `find_skill_in_open_prs` 传
     `strict_skill_only=True` **仅技能名精确匹配, 跳过产品词匹配** —— 否则
     规则会按粗糙词面(如 maas 产品词)把 LLM 已判定"PR 不覆盖"的需求误关
     (#231: maas 列表查询被 maas-tokens-usage PR #164 误关)
   - **bug 类跳过 LLM 覆盖判重(2026-08-09 #241 修复)**: 判重 2.0 的
     "PR/仓库已覆盖功能点"语义只对需求类有效 —— bug 报告("xxx 运行一直
     失败")的 PR 已合入恰恰说明 skill 存在且有 bug, 应走修复流程而非判重
     关闭。bug 类跳过 LLM PR/skills 覆盖两步, 且 find_duplicate_issue
     传 skip_skill_match=True(只按标题归一化判重, 不因同 skill 名的优化/
     需求 issue 互相判重)
   - **bug 类 2.2 分支 skill 名格式校验**(2026-08-09 #235 修复): 提取到
     skill 名后先校验**格式完整**(必须 `huawei-cloud-{product}-{function}`
     至少 2 个连字符段, 或 `xxx-skill` 后缀)—— 用户写 "huawei-cloud-ascend"
     (前缀/缩写)不是完整名, 应 2.2.1 提示"提供完整 skill 名称", 而不是
     被 `_repo_has_skill` 宽松匹配误配到 huawei-cloud-ascend-command 走
     贡献者判断/codeowner。仓库存在性判断改用 exact=True 精确匹配
   - **bot 贡献者判定(2026-08-11 用户要求撤销 #243 占比规则)**: `_skill_has_bot_contributor`
     从"bot 提交占比 >=50% 才算 bot 开发"(#243)改回**任一提交者是 bot 即判 True** ——
     只要有机器人开发过/提交过代码就走自动修复流水线(宁走自动修复, 不误判人工
     分配)。提交作者为空(API 对部分 bot 提交不返回 login)时退化 PR 历史确认
   - **转机器人补建幂等加固(2026-08-12, issue #102 复盘)**: gitcode_issue_id
   依赖 payload 顶层 issue.id, GitCode 格式变化时可能解析为 None → 补建
   requirement 落 gitcode_issue_id=NULL, MySQL 唯一索引对 NULL 不生效 →
   每次评论都重复补建 + force_new 新建 Multica 任务(雪崩), 且 NULL 孤儿
   记录按 id 永远查不回。修复三件套: ① `normalize_payload` note 事件
   issue 对象构造放宽(原硬性要求 `"iid" in obj`, 缺 iid 时整体不构造;
   改为 id 或 iid 任一存在即构造, number 取 `iid or number`); ②
   `handle_issue_comment` 按 id 查不到时用
   `db_find_requirement_by_issue_number(number, repo)` 兜底(带仓库校验),
   历史 NULL 记录也能查回走执行中拦截; ③ 补建前无法定位 issue(id 和
   number 皆无)时拒绝补建返回 bot_fix_skip, 宁可失败提示不可静默重复建
   任务。测试 tests/test_bot_fix_idempotency.py(4 用例)。⚠️ 测试环境:
   走 handle_event 的用例必须 mock `gitcode_get → []` 隔离
   `_api_issue_labels` 真实 API(2026-08-12 实测 g30074593/skillspackage
   issue #103 带 '评测参考' 标签导致事件被 ignored, 现有 3 个用例已补
   该 mock)
- **评论触发转机器人处理**(2026-08-11 用户要求): 非 bot 开发 skill 的 issue
     (bug 修复 / 优化类需求)已按 codeowner 分配人工后, 用户评论含"让机器人修复/
     转机器人/机器人处理/机器人优化/@skills_bot 修复或优化/由机器人开发"等意图词
     (`is_bot_fix_request`, **不区分 label —— bug 与优化类均适用**), 且该 issue
     无 Multica 任务 -> 转自动流水线: 建 Multica 任务(@skills_dev) + DB 置
     dispatched + flow 更新 + 评论确认(幂等靠 MARKERS["bot_fix_requested"])。
     已有 Multica 任务的不转
   - **触发词支持"请你帮我修复"组合句式(2026-08-12 issue #104 实测)**: 原正则
     `@?skills_bot\s*(?:请|帮|帮我|麻烦)?\s*(?:修...)` 的 `(?:请|帮|帮我|麻烦)?`
     是**单个可选组**, 只能匹配"请"或"帮我"之一 —— 用户评论"请你帮我修复一下
     这个bug"("请"+"你"+"帮我"三 token)不命中 → 艾特无效, 落"未找到 Multica
     任务"。已拆组: `(?:请|麻烦)?(?:你)?(?:帮我|帮|帮忙)?`, 覆盖 请修复/帮我
     修复/请你帮我修复/麻烦你帮我修复 等句式。⚠️ 排查"艾特没反应"先重放
     payload 看 reason, 若是转机器人未触发先测 is_bot_fix_request(评论措辞
     是否被正则覆盖), 再查 DB status(外部系统改写 pending 时兜底提示是
     "未找到 Multica 任务"而非 codeowner 提示)。
   - **转机器人 req=None 支持 + 两处 marker 修正(2026-08-11 #101 实测)**:
     a) 原分支条件 `req and not req.get("multica_issue_id")` —— codeowner/提交
    历史分配的 issue 不入库(#182)导致 req 恒为 None, 该分支是死代码, 用户
    "@skills_bot 帮我处理一下这些BUG" 永远触发不了(实测落"未找到 Multica 任务")。
    已改为 `not req or not req.get("multica_issue_id")`: 无 DB 记录时先补建
     requirement(status=dispatched, author=评论者) + flow(node_source=done),
     再建 Multica 任务(与 handle_issue 路径一致);
     b) **marker 必须与评论正文一致**: `_assign_contributor` 评论原为
     "已根据**提交历史**分配给..." 不含 marker"已按 codeowner 映射分配" → 每次
     update 事件重复评论(#101 实测 2 条相同"已分配"评论); bot_fix 确认评论原为
     "已按您的要求转为机器人处理" 不含 marker"已转为机器人修复" → 重复触发会
     重复建任务撞 Multica duplicate 崩溃。均已改文案使 marker 成为真实子串,
     tests/test_markers_quality.py 补两条用例防回归
   - **bug 类型 issue 按需入库(2026-08-12 用户要求)**: bug 类型 issue **创建/
     修改都不入库**(codeowner 分配只发评论, 不建 requirement/flow); 唯一入库
     入口 = 评论艾特机器人转修复(补建 dispatched + flow done)。同时普通评论
     (非转机器人意图)无 Multica 任务时**静默跳过**(不再发"未找到 Multica 任务"
     提示)。bot 开发 skill 的 bug 自动建任务路径**保持入库**(评测自动建单依赖)。
     feature 类型创建保持入库(完整开发流程跟踪)。注意: _assign_contributor
     历史已有记录时也不动 DB —— 避免创建事件重放把转机器人补建的 dispatched
     覆盖回 codeowner_assigned(#104 教训)。
     ⚠️ **2026-08-13 演进(用户要求: bug/优化一律自动修复)**: bug 类型 issue
     无论 skill 是否人工开发, **创建/update 一律直接进机器人修复流水线**
     (建 Multica 任务 @skills_dev + 入库 dispatched + flow done), 优化类
     同步。原"贡献者分流"(bot 自动修 vs 人工 codeowner 艾特提交人)已从
     handle_issue / handle_issue_update 移除(测试 test_bug_opt_auto_fix.py
     4 用例, 断言 _skill_has_bot_contributor / codeowner_assign_issue 不再
     被调用)。"按需入库"语义相应收窄: 只剩**艾特转机器人补建**入口(历史 issue
     等未自动处理的); codeowner_assign_issue 仅剩通用 codeowner 分支(标题无
     skill 关键词的非 bug/非优化 issue)。
   - **评审人与审查解耦(2026-08-12 PR #41 实测)**: 评审人设置原本挂在
     pr_review_and_notify(审查报告)内部, PR #41 219 文件超限(>PR_FILE_LIMIT
     50)跳过审查 -> 评审人跟着空。已修复: handle_event 超限分支(check_pr_file_
     limit 返回 True 时)也调用 _assign_pr_reviewer, 与审查流程解耦 —— 无论
     审查是否执行, PR open/update 事件都会设置评审人。
   - **PR 评论触发重新扫描**(2026-08-11 用户要求): PR 下评论必须**同时**含
     ① 艾特机器人(@skills_bot/@check_bot/机器人) + ② 重新扫描关键字(重新扫描/
     重新审查/重新检查/重扫/再扫/重新评审/重新评估/重新生成报告), 两者缺一
     不可(`is_rescan_request`); 命中 -> force 强制重扫 + 按创建者分流(fail:
     机器人 PR 写 multica + @skills_dev / 开发者 PR @开发者; pass/warn: 机器人
     PR 不处理 / 开发者 PR 走测试)。幂等靠 MARKERS["pr_rescan_requested"]。
     其他评论一律拒绝(维持"仅处理测试报告"现状)
   - **非 bot bug 先过质量检查**(2026-08-09 #243 修复): 2.2 分支非 bot 走
     codeowner 前先 `issue_quality_ok` —— 描述过简(缺复现步骤/日志)应
     need_info 追问而非直接人工分配
   - **codeowner 前统一质量检查 + 正文剔除 skill 名**(2026-08-09 #244 修复):
     通用 codeowner 分支(非 skill 任务)同样先过质量检查; `issue_quality_ok`
     feature 分支改为**剔除 skill 名后的实质描述**计数 —— "huawei-cloud-
     ges-graph有问题,请协助定位下" 去 skill 名仅 9 字应拦截, 否则英文
     skill 名占字符数让空话通过(#244: LLM 复核把 bug 改判 feature 绕过
     2.2 分支落通用 codeowner, 无校验直接分配)
   - **LLM 复核禁止"倾向 feature"默认**(2026-08-09 #246 修复): `_llm_classify`
     加 as_review=True —— 规则已判 bug("有问题/失败"等强 bug 线索)后的
     复核, prompt 改为"除非有明确证据证明是需求(新增/实现/支持), 否则
     保持 bug", 不再用"无法判断倾向 feature"默认覆盖。否则 LLM 把
     "huawei-cloud-nat-list有问题" 改判 feature, 绕过 bug 分流被 LLM 判重
     关闭(#244/#246 三次同类误判)
   - **判重循环引用检测**(2026-08-09 #247 修复): find_duplicate_issue 对
     closed 候选检查其判重评论**指向当前 issue 自己**时返回 self_dup 跳过
     —— #246/#247 完全同标题时互相判重死锁(#247 被"与 #246 重复"关闭,
     #246 恢复处理时 #247 反过来把 #246 判重关闭), 现在 #246 恢复后
     #247 是它的重复源被跳过, #246 正常走修复流程
   - **bug 类 LLM 同一问题判重**(2026-08-09 #248 用户确认判重 2.0 问题类
     语义): bug 类判重不走"PR/skills 覆盖功能点"(PR 合入恰说明 skill 存在
     且有 bug, 应修复), 也不走规则标题归一化(会把"标题相似但现象不同"的
     bug 误关) —— 改由 `_llm_bug_is_same_issue` **LLM 判断是否同一问题**
     (同一 skill + 同一异常现象)。候选粗筛(标题归一化一致或技能名交集)后
     逐个 LLM 判断; 失败 fail-open 放行。效果: #248 与 #247 完全同内容 ->
     判重关闭; 同 skill 但"查询超时 vs 数据错误"不同现象 -> 不判重都处理
   - **multica 审查通知幂等 + 完整报告**(2026-08-09 #176 修复): 机器人 PR
     审查 fail 写 multica 前检查是否已发过同 PR 通知(marker"PR #N 审查未
     通过")—— 此前无幂等, 每个 fail 事件(创建/update/新 commit)都发一条,
     SKI-178 实测重复 5 条; 且改为写**完整审查报告全文**(此前只写摘要
     "见 PR 评论")
   - **SKILL.md 审查强制用仓库完整内容**(2026-08-09 #176 修复): raw_url
     拉取失败时 _file_content 退化为 patch 提取(仅变更区 ~17 行, 不含
     frontmatter), 导致 frontmatter/章节误报缺失 —— SKILL.md 在变更列表
     里时原逻辑不补入仓库内容。改为**总是优先用 contents API 仓库完整
     内容**(新增 skill 仓库无文件时才退化 patch)
   - **逻辑 3c 不再覆盖 gitcode_issue_number**(2026-08-09 隐患修复): 修复
     PR 按 skill 名匹配(skill 名匹配分支)时, 原代码无条件把 issue 号覆盖
     成 pr_number —— 修复 PR 合入时 _close_related_issue 用 PR 号去 close
     issue(不存在), 真实关联 issue 关不掉(实测 id=319/366/378)。改为仅当
     需求原本无关联 issue(纯 PR 开发)时才用 PR 号占位
   - **ON DUPLICATE 不再清空 gitcode_pr_id**(2026-08-09 隐患修复):
     db_insert_requirement 的 ON DUPLICATE KEY UPDATE 原含
     gitcode_pr_id = VALUES(gitcode_pr_id) —— issue 创建/need_info 路径
     的 gitcode_pr_id 恒为 None, 撞唯一键会**清空**已有记录的 PR 关联
     (issue 已关联 PR 后被 need_info 重插入), 导致合入时
     db_find_requirement_by_pr_id 找不到。已移除该覆盖, PR 关联由
     handle_pr 的 db_update_requirement 显式更新
   - **OPTIMIZE_WORDS 移除"新增/增加"**(2026-08-09 #231 修复): 它们是开发
     需求核心词(「新增...Skill」是标准开发需求), 误含导致 2.1.3 把新增需求
     误判为"优化类缺 skill 名"拦截; 迭代/增强由"增强/扩展/迭代/升级"覆盖
   - **#24/#25 LLM 语义判断**(2026-08-09 用户要求对齐原 skill-pr): 服务名
     要求(ECS/VPC 全大写、CloudPond 首字母大写)与操作名 PascalCase
     (ListServersDetails)由 `_llm_koocli_naming_review` 调 LLM 判定
     (SKILL.md 内容喂给 LLM); 失败 fail-open 保持通过(🟡 级不阻断)。
     原硬编码 True(启发式占位)已替换
   - 安全扫描(2026-08-11 重构: **废除内置 3 条正则**, 全部集成 huawei-cloud-skill-audit)
     - 扫描器: gitleaks(222 规则 + Shannon 熵) + skillspector(47 规则/17 类目),
       scan-level=high(critical+error 阻断, warning 仅提示)
     - 实现: `_run_skill_audit_scan()` 直接 import audit 技能
       `/root/.hermes/skills/huawei-cloud/huawei-cloud-skill-audit/scripts/check_registry`,
       PR 涉及的 skill 目录文件落盘临时目录后运行, 失败 fail-open(空列表→⚠️ 不阻断)
     - 展示: 表格 6 列 `| 级别 | 规则 | 问题文件 | 行号 | 问题描述 | 修复建议 |`,
       问题文件列 = markdown 链接 `[路径](https://gitcode.com/{repo}/blob/{branch}/{path}#L{line})`
       ⚠️ **blob 路径不带 `/-`**(2026-08-11 用户实测纠正): GitCode 前端路由是
       `blob/:branchName+/:filePath+`, 挂在 `/{owner}/{repo}/` 下 —— `/blob/...` 浏览器
       正常打开; `/-/blob/...` 前端路由不匹配显示"页面找不到"。注意 curl 无登录态访问
       /blob/ 会 403(服务端限制非浏览器请求), 浏览器登录态正常, 以浏览器实测为准
       ⚠️ **GitCode 不支持 #L{line} 行锚点跳转**(2026-08-11 官方调研确认): 前端是
       Vue SPA, 全站无 location.hash 行定位逻辑, 官方帮助文档 544 页无行号定位功能;
       #L39 打开页面后不会滚动/高亮到指定行(HTTP 200 只是 SPA 空壳)。行号列保留
       供用户打开文件后手动定位; 平台限制无法通过 URL 解决
     - 级别映射: critical/error→🔴(阻断), warning→🟠(提示), info→🔵(过滤)
     - 修复建议: 内置 `AUDIT_FIX_HINTS` 规则映射表 + 兜底文案, 不改 audit 技能
   - 合并检查
   - 结论: 任一 🔴 未过 → fail(阻断进测试, 返回 review_failed); 🟠 未过 →
     warn(放行); 全部通过 → pass(放行)
1. 用 PR number 查需求表 `gitcode_pr_id`(带仓库校验)→ 命中则关联
2. 未命中 → 从 PR body/title 提取 `Fixes #N` / `关联 Issue #N` 等 → 按
   `gitcode_issue_number` 二次匹配
3. 匹配成功: 在 Multica issue 下 `@skills测试小队` (SQUAD_TEST) 评论处理
4. 更新 requirement(status=pr_created, gitcode_pr_id); **flow node_repo 不在此点亮**,
   由 PR 合入事件(逻辑3-merge)点亮 node_repo=done
5. **测试小队完成后的交接**(自动): `--check-handoff` 轮询检测到测试小队名下
   issue 状态为 done/in_review/closed → 评论 `@skills验证小队` (SQUAD_VERIFY)
   + 把 issue 转移分配给验证小队 + 更新 requirement.status=verifying,
   flow.node_verify=in_progress
6. PR 评论通知提交者

> 事件识别补充: PR 的 `update/updated` action 也走 handle_pr(同 open), 用于
> 作者补齐预校验配置后的重试; 逻辑3/4 均有幂等(`_test_squad_notified` 检查
> Multica 评论中 "**GitCode PR #n 已创建**" + @测试小队 标记), 不会重复下发。

### 逻辑3-merge: PR 合入事件(点亮共建仓库节点)
普通 PR 合入(非 distribute 分支)时调用 `handle_pr_merged()`:
1. 按 PR number + 仓库校验查需求表
2. 匹配到 → `db_update_flow(node_repo=done)` 点亮共建仓库节点
3. **1.4 按提交者处理关联 issue**(2026-08-08 用户要求): 机器人 PR 合入 ->
   直接评论"该需求已开发完成并合入主仓" + **关闭关联 issue**(自动闭环);
   开发者 PR 合入 -> PR 评论提醒创建者关注 + 关联 issue 评论**通知作者
   自行关闭**(不自动关)
4. 不重复下发测试任务(测试/验证流程已完成)

### 逻辑4: PR 创建事件(未匹配到 issue)
1. 新建 Multica issue, 分配 `skills测试小队` (SQUAD_TEST)
2. 入库 requirement(含 gitcode_pr_id) + flow(node_source=done; node_repo 留待合入事件点亮)
3. 测试完成后同样由 `--check-handoff` 交接给 `skills验证小队` (同逻辑3-5)
4. PR 评论通知提交者

### 逻辑5: PR 评论事件(2026-08-04 调整: 仅处理测试报告)
1. `[skills_bot]` 前缀 → 跳过
2. `is_test_report()` 判断评论是否为该 PR 的测试报告
   (特征: 标题「# 总测试报告 / # 测试报告」+ 统计结构「总用例数/测试统计/
   被测 skill/报告类型: PR 唯一总测试报告」; 对齐 #126 实测格式)
3. 非测试报告评论 → **不做任何处理**(不再转发 Multica, 避免打扰测试小队)
4. 测试报告评论 → 回复确认「已收到该 PR 的测试报告, @PR创建者 请关注测试
   结果」(幂等: 已回复过则跳过); **不转发回 Multica**(测试报告本就是测试
   小队产出, #126 实测: 旧逻辑转发回 Multica 是多余的)

### 逻辑6: 进展通知与防死循环
- 所有程序评论带 `[skills_bot]` 前缀(见 `bot_comment()`)
- 收到带前缀的评论事件一律跳过, 不转发给 Multica
- 评论转发前用 "GitCode 评论 ID: {id}" 标记做幂等去重(webhook 重试防护)

### 逻辑7: distribute_for_github 分支 PR 监听(优先复用原任务)
1. 判断: `target_branch == DISTRIBUTE_BRANCH`(默认 distribute_for_github, cherry-pick 合入场景)
2. **提取技能名称**: `extract_skill_name_from_pr()` 依次从 文件路径
   (`skills/<cat>/<name>`)、`huawei-cloud-xxx` 命名、`skill: <name>`、`xxx-skill` 提取
3. **匹配原开发需求**: `find_requirement_by_skill()` 在 requirement 表的
   `skill_name`/`title`/`body` 各字段模糊匹配, `ORDER BY updated_at DESC` 取最近一条
   (requirement_type='requirement')
4. **匹配到** → 在**现有** multica issue 上评论 `@skills分发小队`,
   不新建任务(持续在原任务上处理); 更新 flow.node_distribute=in_progress +
   **node_distribute_url=Multica 任务 URL**(2026-08-13 用户要求统一, 原为
   PR URL); 幂等: 评论含 "distribute_for_github 分支分发任务" 标记则跳过
5. **匹配不到** → 新建 Multica issue 分配 `skills分发小队`, 入库
   requirement(requirement_type='distribute', skill_name 一并记录) + flow
6. PR 评论通知提交者

### 逻辑8: main/master 合并 PR 分发(devops-bot _dispatch_merged_pr 迁移)
合入 `MERGE_TARGET_BRANCH`(默认 main, 对应 devops-bot event-routing.json 的
merge_target_branch)的 PR:
1. 查 requirement 表: 先按 PR number 带仓库校验, 未命中再从 PR title/body 提取
   `#N` 按 issue number 二次匹配
2. 命中 → 点亮 flow node_repo=done(node_repo_status/url/time 一起更新);
   **requirement.status=repoed(已入库)**(已进入 developed/tested/verifying/
   deployed 等后期阶段的记录不倒退降级); **写版本表 t_skill_pr_version_history**
   (skill_name=PR 变更文件公共目录, new_version=标题 vX.Y.Z > SKILL.md frontmatter
   version > 兜底 add=1.0.0 / update=旧版本末位+1, old_version=该技能上一条
   new_version, change_type=add(无旧版本)/update, pr_number, merged_at;
   幂等: 同 skill+repo+pr_number 已存在则跳过); 验证 multica issue 存在:
   - 已指派给 `skills分发小队` → 跳过(幂等)
   - 否则 `multica issue assign` 指派给分发小队 + 评论 `@skills分发小队`,
     flow 更新 node_distribute=processing + **node_distribute_url=Multica 任务
     URL**(2026-08-13 用户要求统一, 原为仓库主页)
   - **评论并关闭关联 GitCode issue(2026-08-07 需求)**: 两个分支(已指派/
     新指派)都执行 —— 评论"✅ 该需求已开发完成并合入主仓 (PR #N 已合并)"
     并关闭该 issue(幂等: marker 评论已存在则跳过, 防重复 webhook 重复
     评论/重复关闭)
3. 未命中/issue 不存在 → 新建 Multica issue 分配给 `skills分发小队`,
   带 600s 内存守卫 `_merge_created_guard` 防重复 webhook 重复创建;
   **创建成功后必须入库**(requirement: requirement_type=distribute,
   status=dispatched, gitcode_pr_id=PR number; flow: node_source/node_repo/
   node_distribute=done), 否则重复事件再次触发时 multica issue create
   报 duplicate(2026-08-04 PR #128/SKI-127 实测)
4. 与逻辑7 并存: distribute_for_github 分支仍走逻辑7(cherry-pick 场景),
   main/master 走逻辑8, 其他分支合入走逻辑3-merge(仅点亮 node_repo)

### 逻辑9: push-sync(devops-bot handle_push_event 迁移)
`push` 事件按仓库配置分发: 仓库命中 `PUSH_SYNC_CONFIG`(JSON 环境变量,
格式 `{"owner/repo": {"branch": "github", "squad_id": "...", "squad_name": "..."}}`)
且推送分支匹配时, 创建 Multica issue 分配给配置的小队(提交信息/commit 链接入任务描述)。

## 测试→验证 交接轮询(cron 推荐)

`--check-handoff` 扫描分配给测试小队(SQUAD_TEST)的 issue, 对已完成
(done/in_review/closed/blocked)的 issue:
1. 评论 `@skills验证小队`("测试小队已完成处理")
2. `multica issue assign` 转移分配给验证小队
3. 更新数据库: requirement.status=verifying, flow.node_verify=in_progress
4. 防重复: 评论中含 "测试小队已完成处理" 标记的跳过

建议 cron 配置(每 10 分钟):
```bash
*/10 * * * * cd /home/wangliangchu/.hermes/skills/software-development/gitcode-event-handler && python3 scripts/gitcode_event_handler.py --check-handoff >> /tmp/handoff.log 2>&1
```

## 配置

脚本顶部常量区, 均可用环境变量覆盖:

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| GITCODE_TOKEN | REPLACE_WITH_GITCODE_TOKEN | GitCode API token (账号 skills_bot) |
| MULTICA_TOKEN | mul_2a84bda0e1e2439e22b1c6fa4ea4f9bec8d7f0a8 | Multica token (SKILLS开发验证(测试)) |
| MULTICA_WORKSPACE_ID | 270d4faf-4e8b-4d04-b8d3-ed88569abf4c | SKILLS开发验证(测试) 工作区 |
| MULTICA_ISSUE_URL_PREFIX | {MULTICA_SERVER}/skill-test/issues | Multica issue 页面 URL 前缀(按工作区 slug 调整) |
| DEVFLOW_BASE_URL | http://skillsop.topxtopx.com | issue/PR 回复中流程跟踪链接域名(拼 /devflow?requirementid=<主键id>) |
| DB_HOST / DB_USER / DB_PASS / DB_NAME | 192.168.1.18 / root / REPLACE_WITH_DB_PASS / rds-hd-skillsopr-db | MySQL |
| SQUAD_DEV / SQUAD_DEV_NAME | c5287391... / skills_dev | 逻辑1 开发小队 ID/名称 |
| SQUAD_TEST / SQUAD_TEST_NAME | c05558fa... / skills_test | 逻辑3/4/5 测试小队 ID/名称 |
| SQUAD_VERIFY / SQUAD_VERIFY_NAME | 9a6d8a29... / skills_verify | 逻辑3/4 handoff 验证小队 ID/名称 |
| SQUAD_DISTRIBUTE / SQUAD_DISTRIBUTE_NAME | 77137221... / skills_distribute | 逻辑7/8 分发小队 ID/名称 |
| DISTRIBUTE_BRANCH | distribute_for_github | 逻辑7 监听的分发目标分支 |
| MERGE_TARGET_BRANCH | main | 逻辑8 合并目标分支(devops-bot merge_target_branch) |
| IGNORE_ISSUE_LABELS | codeowner,评测参考 | 前置检查: 命中这些标签的 issue 事件整体忽略 |
| IGNORE_LABEL_RECHECK_DELAY | 2.5 | ignore 标签 API 兜底查不到标签时的补查间隔秒数(0=关闭补查) |
| PR_FILE_LIMIT | 50 | 前置检查: PR 变更文件数上限(超过发评论跳过) |
| PUSH_SYNC_CONFIG | 空 JSON | 逻辑9: push-sync 仓库配置 JSON |
| ENABLE_LABELS | 1 | skill-issue 集成: 自动打标签(feature/bug/triaged/need-info) |
| ENABLE_QUALITY_CHECK | 1 | skill-issue 集成: 需求质量检查(不达标 need-info 追问) |
| ENABLE_DUP_CHECK | 1 | skill-issue 集成: 重复 issue 实时判重+关闭 |
| ENABLE_EXISTING_CHECK | 1 | 已存在检查: 需求对应 skill 已在仓库开发过 / DB 已有类似需求 → 评论提示+关闭, 不重复建任务 |
| ENABLE_COMMENT_INTENT | 1 | skill-issue 集成: 评论意图(关闭请求/need-info 补充升级) |
| ENABLE_COMMENT_FILTER | 1 | 无关评论过滤(2026-08-05): 灌水短文本/纯 emoji/与需求无关闲聊 → 静默跳过, 不转发 Multica 不回复; 判定异常 fail-open |
| ENABLE_COMMENT_LLM_FILTER | 0 | LLM 兜底判断开关(预留位, 本次未实现调用; 后续开启时层3 未命中可走 LLM) |
| IRRELEVANT_COMMENT_WORDS | 40+ 灌水词 | 灌水词集(好的/收到/顶/👍/+1/辛苦了 等, 逗号分隔, 环境变量可覆盖) |
| COMMENT_FUNC_WORDS | 40+ 功能词 | 评论需求相关性判定词集(查询/列表/创建/修复/补充 等, 逗号分隔, 环境变量可覆盖; 与灌水词集不重叠) |
| ENABLE_CODEOWNER_ASSIGN | 1 | codeowner 分配: 非 Multica 开发 skill 的 issue 按 codeowner 映射分配给开发者 |
| ENABLE_PR_PRECHECK | 1 | PR 预校验: PR 创建/更新先做配置检查(标题/描述/关联Issue/标签/Assignee), 问题仅评论提醒不阻断 |
| ENABLE_PR_REVIEW | 1 | PR 审查报告: 预校验通过后输出 Skill 审查 30 项+安全扫描+合并检查, fail 阻断进测试 |
| ENABLE_PR_REVIEWER_ASSIGN | 1 | PR 扫描后自动设置评审人(2026-08-12): 开发者 PR->DEFAULT_REVIEWER; 机器人 PR 按 issue 标签 bug->skill 贡献者/feature->issue 创建人, 找不到回退 DEFAULT_REVIEWER |
| DEFAULT_REVIEWER | HugLovesWine | 兜底评审人(胡盖): 开发者 PR 固定 + 机器人 PR 找不到目标时回退 |
| GITCODE_PR_ASSIGNEE | lccode | bug 类型 issue 入库责任人(2026-08-16 e3a73bb): handle_issue 中 label==bug 或 payload 含 bug 标签时, 需求 author 覆盖为该值 —— bug issue 创建人≠修复人(实测 #15 创建人 wj911831, 修复人 lccode) |
| ENABLE_HUAWEI_RELATED_CHECK | 1 | 华为云产品相关性检查: 需求未命中华为云特征/产品词库 → need-info 追问; 补充后仍无关 → invalid 关闭 |
| HUAWEI_CLOUD_PRODUCTS | 60+ 产品名 | 华为云产品词库(逗号分隔, 覆盖 ecs/rds/obs/ges/弹性云服务器/云数据库 等, 环境变量可覆盖) |
| COMPETITOR_CLOUD_WORDS | 30+ 竞品云 | 竞品云特征词(阿里云/腾讯云/火山云/aws/azure 等) + 「X云」启发式(排除通用云名词): 命中且无华为字样 → 明确非华为云, invalid 关闭(环境变量可覆盖) |

> 分配对象 ID 支持小队或智能体 UUID(`multica squad list` / `multica agent list`
> 可查), 名称用于评论 @ 和通知文本, 二者需配套配置。换工作区时同时调整
> MULTICA_WORKSPACE_ID 与 MULTICA_ISSUE_URL_PREFIX(含 slug)。

依赖: `pymysql`(已装 2.2.8)、`multica` CLI(已装 /usr/local/bin/multica)。

## API 参考

- GitCode: `https://gitcode.com/api/v5`, 认证 header `PRIVATE-TOKEN`
  - 发 issue 评论: `POST /repos/{owner}/{repo}/issues/{number}/comments`
  - 发 PR 评论: `POST /repos/{owner}/{repo}/pulls/{number}/comments`(兼容 merge_requests)
- Multica: CLI 封装(multica issue create / comment add / comment list / assign)
  - issue 创建: `--assignee-id {squad_id}` 指定小队
  - 评论 @小队语法: `[@名称](mention://squad/{squad_id})`
  - issue 编号: 返回 JSON 的 `identifier` 字段(如 SKI-76)
  - issue list 返回 `{"issues": [...], "total": N}`, 支持 `--assignee-id`/`--status` 过滤
- MySQL: 表 `t-skills-dev-requirement`(gitcode_issue_id 唯一索引, 幂等插入)
  + `t-skills-dev-flow`(requirement_id 唯一, 节点状态机)

## 数据库约定

- requirement.state = 'opened'(GitCode issue 状态)
- requirement.status: pending → dispatched(已分发) → repoing(入库中, PR 已创建)
  → repoed(已入库, PR 合入主分支) → (后期: verifying/tested/deployed 等)
- requirement.requirement_type: 'requirement'(普通需求) / 'distribute'(分发任务)
- requirement.multica_issue_number 存 identifier(SKI-xx)
- multica_issue_url = `https://multica.clouddeveloper.club/skill-test/issues/{id}`
- flow 节点: node_source(issue 源) / node_preprocess(分发到 multica) /
  node_dev / node_test / node_verify / node_repo(PR 入库) / node_distribute / node_display
- 版本表 t_skill_pr_version_history: 主分支 PR 合入时写 skill 版本
  (skill_name/repo/old_version/new_version/change_type=add|update/pr_number/merged_at),
  幂等键 = skill_name+repo+pr_number
- 程序负责: issue 创建 → node_source=done + node_preprocess=done;
  PR 创建 → 不点亮 node_repo(仅 status=repoing); PR 合入主分支 → node_repo=done
  + status=repoed + 写版本表; handoff → node_verify=in_progress

## Pitfalls

1. **防死循环**: 程序发评论必须带 `[skills_bot]` 前缀; 收到该前缀的 note 事件直接跳过,
   否则"程序评论 → 新 note 事件 → 再评论"无限循环
2. **幂等**: requirement 表 gitcode_issue_id 有唯一索引, INSERT ... ON DUPLICATE KEY UPDATE;
   评论转发用 "GitCode 评论 ID: {id}" 标记去重(webhook 可能重试投递)
3. **PR 关联 issue**: GitCode PR payload 的 `close_related_issue` 常为 0, 必须从
   PR body 提取 `Fixes #N` / `关联 Issue #N` / `#N` 兜底匹配
4. **匹配必须带仓库校验**: 不同仓库的 PR/issue 编号会重复, 匹配时用
   `gitcode_issue_url LIKE '%/{repo}/%'` 校验仓库归属
5. **multica issue 状态**: `multica issue status <id> <status>`, status 值:
   backlog/todo/in_progress/in_review/done/blocked/cancelled
6. **GitCode token 账号即提交者账号**(skills_bot), 不能按 author 判断程序评论,
   只能靠 `[skills_bot]` 前缀
7. **评论通知失败不阻断主流程**: gitcode 评论异常时记录 notify_error 继续返回结果
8. **时间格式**: GitCode 时间为 `2026-08-01T10:12:55+08:00` 或 Z 结尾,
   `parse_datetime()` 统一转 MySQL datetime; 解析失败存 NULL
9. **node_repo_url 存仓库主页**(https://gitcode.com/{repo})而非 PR URL, 与既有数据一致
10. **GitCode 关闭 issue**: PATCH `/issues/{number}` 传 `{"state": "close"}`(不是
    "closed" 也不是 state_event); 返回 JSON 中 state 为 "closed"
11. **multica CLI 错误输出在 stderr**: `_multica_cli` 返回 (code, stdout, stderr),
    排查失败时看 stderr(如 "resource not found" 表示 issue id 无效)
12. **真实 payload 是 GitLab 风格**: GitCode webhook 用 `object_attributes`(含 iid/id
    分离 + target_branch/source_branch), `normalize_payload()` 已自动兼容
13. **multica issue list 返回 dict**: 取 `issues` 键, 不是直接列表
14. **已合并的 PR 不能评论**: 合并后 `POST /pulls/{n}/comments` 返回 405,
    safe_notify 容错不影响主流程
15. **PR 事件会多次触发, 必须幂等**: GitCode 在 PR 生命周期会推送多次
    merge_request 事件(创建 open、更新、合入 merge 等, 合入时甚至连续
    2-3 个事件)。对策: a) `handle_event` 只对 open/create 类 action 走
    handle_pr, 普通 PR 的 merge 事件走 handle_pr_merged 点亮 node_repo
    (幂等: 重复合入事件重复置 done 无副作用); b) matched/matched_by_issue
    分支用 `_test_squad_notified()` 检查
    multica issue 评论中是否已有 "GitCode PR #{n} 已创建" + @测试小队
    的评论, 已下发过则返回 matched_dup 跳过。2026-08-01 曾因缺失该保护
    对 PR #3 重复 @测试小队 5 次
16. **GitLab 风格 payload 的 action 在 object_attributes 中**: `parse_event()`
    需同时查顶层 action 和 `object_attributes.action`, 否则 merge/update
    事件会被误判为空 action 当作创建事件处理
17. **GitCode 标签颜色必须带 # 前缀**: POST /labels 的 color 传 `#d73a4a`(带#)
    才合法, 传 `d73a4a` 报 400 "Color must be a valid color code"(2026-08-02 实测);
    feature 标签默认色 #428BCA(仓库已有)
18. **判重只信技能名精确匹配/标题归一化完全一致**: 字符级 Jaccard 相似度对
    中文标题太宽松(公共前缀导致误判), 已移除; `_skill_names` 正则只匹配
    `huawei-cloud-xxx` / `xxx-skill` / `skill: xxx`, 裸词 skill 不算技能名
    (否则任何含 "skill" 的 issue 都会被判重)
19. **need_info/duplicate 也要入库**: 质量不达标与判重关闭的 issue 仍 insert
    requirement(status=need_info/duplicate, multica_issue_id=NULL), 用于幂等
    防重复事件反复触发, 且 need_info 供后续评论补充信息时 `db_find_requirement
    _by_issue_id` 查到记录升级创建任务
20. **GitCode issue 事件会连推 open+多个 update**: 创建 issue 后立即跟
    2-3 个 action=update 事件(描述相同), 用户编辑补充 body 也触发 update。
    update 事件不能 ignored: 需按逻辑1b 重新质量检查, need_info 可升级;
    但同内容 update 重复到达时必须幂等(仍不足则只同步 body 不重复评论)
21. **标签 PATCH 实为追加语义, set_issue_labels 已改为先删后加(2026-08-06 修正)**:
   2026-08-06 #168 实测: PATCH /issues/{n} {"labels": "a,b"} **不会覆盖旧标签,
   只会追加** —— 三次打标(open need-info / update need-info / 升级 feature,triaged)
   后 note webhook 里真实标签为 [feature, need-info, need-info, triaged], need-info
   重复且升级后从未被清掉(用户曾反馈"标签太多")。旧记载(2026-08-02 #44 事故)认为
   是整体替换语义, 已不成立(或 GitCode API 已变更)。已修: `set_issue_labels` 先
   DELETE 本脚本管理的全部标签(`DELETE /repos/{o}/{r}/issues/{n}/labels/{name}`,
   实测 204 幂等, 404 容忍), 再 PATCH 追加目标标签, 等效整体替换; codeowner/
   评测参考/pr-merged 等非管理标签不受影响。need-info 分支仍保留主标签
   `[feature, need-info]` 的习惯写法(现由先删后加保证不会残留/重复)。
22. **GitCode 合入事件 action 常为 update 而非 merge**: 已合入 PR 推送的
   事件 state=merged 但 action=update, 只认 action=="merge" 会把合入事件
   误判为"创建/更新"走预校验, 导致逻辑8/点亮 node_repo 漏执行
   (2026-08-04 PR #128 实测, 已修: handle_event 对 state=merged 且非
   open/create 类 action 的事件统一按合入处理)
23. **multica issue create 有 duplicate 校验**: 同标题活跃 issue 已存在时
   创建报 "Active duplicate issue exists"。逻辑8 创建兜底分支必须先查
   DB/确认未创建过; 已创建但 DB 无记录时(历史遗留)需手动补 requirement
   + flow 记录恢复幂等
24. **外部系统可能改写 requirement.status(如 pending), 触发重复处理崩溃**:
   t_skills_dev_requirement 可能被 skillsopr 后端等其他服务写入(2026-08-04
   issue #102「评测报告」实测: 外部服务把 status 写成 pending, 而脚本的幂等
   检查只认 need_info/codeowner_assigned/invalid/有 multica)。此时 issue update
   事件在 handle_issue_update 落入 fallback → handle_issue 重走创建流程 →
   重复发 need-info 评论 + db_insert_flow 撞 uk_requirement_id 唯一键 1062 崩溃
   (一次事件链产生 3 条重复评论)。对策(已修):
   a) `db_insert_flow` 改为 INSERT ... ON DUPLICATE KEY UPDATE(幂等, 已存在
      flow 行时更新节点状态而非崩溃);
   b) `handle_issue_update` 末尾: 已有记录(status 非 need_info 且无 multica)
      只同步 body 后 skip, 不重走 handle_issue;
   c) `handle_issue` 幂等检查补通用兜底: 任何已入库状态(exist 非空)一律 skip。
   排查手法: 脚本内 grep 'pending' 为 0 却出现在 DB, 即外部写入
25. **评测报告类 issue 是自动化噪音**: 评测系统创建「xxx 评测报告」issue
    (如 #102)会被误分类为 bug → need-info 追问循环。已实施策略(2026-08-04):
    `_is_evaluation_report()` 按标题含「评测报告」/ 标题含「评测」且正文含
    「评测完成/日期/结果/通过/不通过」特征前置忽略(第二道防线, 不依赖标签
    时序), 命中直接 ignored 不处理/不通知/不分发
26. **ignore 标签 API 兜底端点失效(feign 错误 JSON)**: GET
    `/repos/{repo}/issues/{number}/labels` 实测返回 200 但 body 是
    `{"error_code": 200, "error_message": "Error while extracting response..."}`,
    不是标签列表; 旧实现对 dict 静默取空 → 兜底形同虚设(#102 事故直接原因)。
    已修(2026-08-04): `_api_issue_labels()` 改为**详情端点优先**
    `GET /repos/{repo}/issues/{number}`(labels 字段, 实测可用), 响应异常/
    feign 错误时再试 /labels 端点, 两处都失败返回 None 由调用方 fail-open。
    且 payload 无标签 + API 查不到时按 IGNORE_LABEL_RECHECK_DELAY(默认 2.5s)
    补查一次, 覆盖「评测系统创建 issue 后 1 秒才打标签」的时序竞态
    (open 事件先到 → 标签未打上 → 无标签放行, 2026-08-04 #102 实测时间线)
27. **is_skill_task 只认 "skill" 关键词, 必须补产品相关性校验**: 仅凭
    "skill" 关键词, "帮我开发一个skill:查天气" 这类与华为云无关的需求也会
    直接建 Multica 任务开发。已加(2026-08-04) `is_huawei_cloud_related()`
    前置校验: 命中华为云特征词(huawei/华为云/华为/云服务/cloud)或产品词库
    HUAWEI_CLOUD_PRODUCTS 才放行; 未命中 → need-info 追问"补充说明对应
    华为云产品"(温和模式, 不直接关闭, 防误伤新产品/中文别名); 补充后升级
    路径(逻辑1b body 更新 / 逻辑2意图2 评论补充)二次校验, 仍不相关 →
    invalid 关闭。词库 60+ 产品名, 环境变量 HUAWEI_CLOUD_PRODUCTS 可覆盖;
    若用户用中文产品名(弹性云服务器等)或新产品名, 需补充词库
28. **need_info 升级路径必须先判重, 否则撞 Multica duplicate 校验崩溃**
    (2026-08-04 #103 实测): issue #103「帮我开发一个skill:支持查询华为云rds列表」
    与 #101 同标题(后者已开发完成, Multica 任务 SKI-130 in_review)。#103 open 时
    因正文 <30 字走 need-info 分支入库(判重逻辑在质量检查之后, 被提前 return
    跳过); 用户补充 body 后 update 事件走逻辑1b 升级分支 → 质量检查通过 →
    `multica_issue_create` 撞 Multica 的 "Active duplicate issue exists: SKI-130"
    校验 → RuntimeError 崩溃(一次事件链未处理)。对策(已修): `handle_issue_update`
    升级分支与 `handle_issue_comment` 意图2 在 `multica_issue_create` **之前**
    先调 `find_duplicate_issue()`(排除自身), 命中则评论说明 + 关闭 issue +
    入库 status=duplicate, 与逻辑1 判重行为一致。注意判重必须在质量检查**之后**
    做(信息不足的 issue 不应触发判重关闭), 但必须在创建任务**之前**做
    (创建后撞 duplicate 校验就晚了)
29. **GitCode 详情 API 的 labels 字段可能整体为空(2026-08-04 观察)**: 对仓库
    所有 open issues 逐个查 `GET /repos/{repo}/issues/{number}` 的 labels 字段
    均返回 `[]`(包括带 codeowner 标签的 #72、带评测参考的 #102、带
    feature+need-info 的 #103), 而 webhook payload 的 labels 数组却是真实的。
    说明该字段当前在详情 API 序列化时丢失, `_api_issue_labels()` 的详情端点
    兜底在此时会静默返回 [] → fail-open 放行; 标签真实性以 webhook payload
    为准。不影响本次 #103 duplicate 处理(判重靠标题归一化, 不依赖标签)
30. **外部系统可能改写 status=pending, 导致 update 事件不重新评估(#109 教训)**:
    t_skills_dev_requirement 会被 skillsopr 后端等外部服务写入 pending。
    旧 handle_issue_update 只在 status=need_info 时重新评估, 其余状态仅同步
    body —— 用户修改 body/title 后(21:14:50 #109 把 24 字补到 36 字)事件被
    跳过, 需求从未重新判断。已修(2026-08-04): **无 multica 任务的非终态
    记录(need_info/pending 等)一律重新评估**(质量检查 → 产品相关性 → 判重
    → 建任务), 仅终态(duplicate/invalid/codeowner_assigned)和有任务的同步
    body。评估评论用 `_issue_has_bot_comment()` 幂等防刷屏
31. **产品词库误伤竞品云需求(#109)**: 用户提交「支持阿里云查询eip列表」,
    eip 在华为云产品词库 → 旧 is_huawei_cloud_related 直接放行。已修
    (2026-08-04): 判定顺序改为 ① 正向强特征(huawei/华为)② 竞品云
    (阿里云/腾讯云/aws/azure 等, COMPETITOR_CLOUD_WORDS)且无华为字样 →
    明确非华为云 → invalid 关闭(评论幂等)③ 宽松特征(cloud/云服务)
    ④ 产品词库。注意「对比华为云和阿里云」类需求含"华为"仍按华为云放行
32. **竞品词表漏词 + 混合大小写匹配坑(#110)**: 「支持火山云查询eip列表」
    在词表只有「火山引擎」时漏判(eip 命中产品词库被放行, 建了 Multica 任务
    SKI-135 后补救取消)。已修(2026-08-04): ① COMPETITOR_CLOUD_WORDS 扩到
    30+ 竞品云(火山云/火山引擎/京东云/天翼云/移动云/联通云/UCloud/volcengine
    等); ② 新增「X云」启发式(`[\u4e00-\u9fa5]{2,3}云` 模式), 排除
    CLOUD_GENERIC_TERMS 通用云名词(华为云/云服务/云数据库/弹性云/混合云等),
    覆盖词表未收录的新竞品云; ③ 词表 ASCII 词比较必须 `.lower()`(UCloud 大写
    匹配小写文本永远 False); ④ ⚠️ 竞品词注意避开产品词子串(如 UCloud 含
    "cloud", 竞品检查必须在宽松特征 cloud 之前, 否则 "ucloud" 命中 "cloud"
    误放行)。补救流程: 关闭 GitCode issue + DB invalid + multica issue status
    cancelled + 评论说明
33. **正文复制残留会误命中产品词库(#113)**: 「查询每日新闻列表」标题与华为云
    无关, 但正文是从 #110 复制粘贴残留的「查询租户下的EIP的名称列表」, EIP
    命中产品词库被放行并建了任务 SKI-138(已补救关闭/取消)。已修
    (2026-08-04): 产品相关性改为**三级判定** `huawei_related_level()`:
    - strong: 标题命中产品词/华为云特征(或任意位置含 huawei/华为) → 放行
    - weak: 仅正文命中(复制残留风险) → need-info 追问"标题未体现华为云产品,
      请确认/更正标题"(评论幂等, 不直接放行也不关闭)
    - none: 未命中 → need-info 追问(原有逻辑)
    - 竞品云由 is_competitor_cloud 单独判断 → invalid 关闭
    is_huawei_cloud_related() 保留兼容(strong/weak 均视为相关)。
    代价: 标题简略("帮我开发一个skill")+ 正文明确产品的需求会先被追问一次,
    用户确认后升级, 可接受
34. **重复需求仍会新建任务(#112)**: 「支持查询eip列表」仓库已有 eip 产品线
    (skills/network/eip/), DB 已有 #100(tested)/#10/#28(repoed) 等历史需求,
    但旧判重只查 open issues + 标题完全一致, 重复需求仍建了 SKI-137。已修
    (2026-08-04, ENABLE_EXISTING_CHECK): ① `find_skill_in_repo()` 产品目录
    匹配(contents API 遍历 12 类目, 缓存 10 分钟; 仓库 tree API 递归只返回
    20 条不可用) + 技能名精确匹配; ② `find_similar_requirement_in_db()`
    同产品 + 标题归一化互相包含/功能词交集, 标题命中产品词优先, 进行中提示
    "已在开发中"、已完成提示"已开发完成"。命中任一 → 评论 + 关闭 + 入库
    duplicate, 不重复建任务。注意: 产品目录映射只有一级(skills/<cat>/<prod>),
    ops 目录下技能(如 huawei-cloud-vpc-list 在 network/ops/)需靠技能名精确
    匹配兜底
35. **无关需求别打类型标签(#114)**: 「每日新闻」需求(产品相关性 none)被
    质量检查打了 [feature, need-info], 用户补充后产品相关性检查**又打一次**
    [feature, need-info](update 事件重复触发), 标签语义混乱且重复(用户手动
    清空并反馈"标签太多")。已修(2026-08-04): 产品相关性失败(none/weak)只打
    `["need-info"]`(类型存疑, 不带 feature/bug); 竞品云只打 `["invalid"]`;
    **质量检查失败仍保留 [label, need-info]**(pitfall 21, 有效需求信息不足)。
    正常放行后仍打 [label, triaged]
36. **skill 名与已提交 PR 重复(#130 等)**: 需求「查询eip列表」对应的
    huawei-cloud-eip-list 正在 PR #130(open)开发, 仓库检查(find_skill_in_repo)
    只覆盖已合入 skill 查不到, 需求仍会新建任务。已修(2026-08-04, 用户要求):
    `find_skill_in_open_prs()` 检查已提交 PR — ① 技能名精确匹配(需求技能名
    出现在 PR 标题/body 即命中, 不再拉文件) ② 产品名匹配(需求产品词出现在
    PR 标题 → 拉该 PR 文件列表确认含 skills/ 下该产品文件, 防误报)。
    命中 → 评论"该 skill 已有 PR(#N)在开发/审查" + 关闭 + 入库 duplicate。
    注意: pulls API state=all 含 merged(已进仓库的由仓库检查先命中, PR 检查
    冗余无害); 每需求最多 30 个 PR + 少数文件列表调用, 成本可控
37. **升级路径缺已存在检查 + 标签叠加(#118)**: 「查询华为云dcs列表」open 时
    质量检查失败(22字)打 [feature, need-info] 入库 need_info; 用户补充后
    **handle_issue_update 升级路径直接建任务**(缺 find_skill_in_repo/PR/DB
    已存在检查), dcs 仓库已有(skills/storage/dcs)且 PR #117 在开发也没拦住,
    建了 SKI-141; 且升级又打 [feature, triaged], 与 [feature, need-info]
    叠加造成标签重复(用户清空后反馈)。已修(2026-08-04): **handle_issue_update
    和 handle_issue_comment 意图2 的升级路径补齐已存在检查**(判重后、建任务
    前, 与 handle_issue 一致: 仓库已有/PR 已有/DB 类似 → 评论 + 关闭 +
    DB duplicate)。标签: 升级 PATCH [label, triaged] 经 set_issue_labels
    先删后加(2026-08-06 修正, 见 pitfall 21), 正常会清掉 need-info, 无需人工清理
38. **PR 评论不应转发测试报告回 Multica(#126)**: 测试人员把「# 总测试报告」
    发在 GitCode PR 评论上(15:08:12, author=wj911831), 旧逻辑5把**所有**
    PR 评论转发到 Multica(15:08:33 评论"已转发至 SKI-123"), 测试报告本
    就是测试小队产出, 转回去纯属噪音且打扰小队。已修(2026-08-04, 用户
    要求): `is_test_report()` 识别测试报告(标题「# 总测试报告/# 测试报告」
    + 统计结构「总用例数/测试统计/被测 skill/报告类型: PR 唯一总测试报告」);
    非测试报告评论**不做任何处理**; 测试报告评论回复「已收到, @PR创建者
    请关注测试结果」(幂等, 用 pulls/{n}/comments 查 [skills_bot] 标记)且
    **不转发 Multica**
39. **已存在检查排在质量检查之后, 质量不达标需求错过提前识别(#118)**: 「查询
    华为云dcs列表」open 时正文 22 字(<30), 质量检查失败**提前 return**, 走
    不到排在后面的已存在检查, 先被追问质量; 用户补充后升级路径(当时缺检查)
    直接建了 SKI-141 —— 仓库已有 dcs + PR #117 都没拦住。已修(2026-08-04):
    **handle_issue 里把已存在检查(仓库/PR/DB)移到质量检查之前** —— 重复需求
    无论信息是否完整, open 即评论提示 + 关闭, 不再浪费一次质量追问; 质量
    检查失败仅在无重复时触发。注意 handle_issue 里 exist 变量(2255 定义)在
    已存在检查之前, 移动后仍可用
40. **产品目录匹配误报"已开发"(#119)**: 「支持华为云查询eip列表」被
    find_skill_in_repo 产品目录匹配拦截(eip 目录存在 → 评论"仓库已开发过"),
    但实际要开发的 huawei-cloud-eip-list 在仓库中**不存在**(eip 目录下只有
    huawei-cloud-eip-cost-optimizer), 该 skill 在 PR #130 开发中。产品目录
    存在 ≠ skill 名称全称已存在。已修(2026-08-04, 用户要求"名称全称不能与
    已提交 PR 或仓库重复"): find_skill_in_repo **去掉产品目录匹配**, 仅按
    skill 名称全称匹配(需求显式写 huawei-cloud-xxx 且仓库有同名目录);
    无全称的需求交给 find_skill_in_open_prs 产品名匹配兜底(拉文件确认,
    如 #119 命中 PR #130)。#119 已补救: 追加澄清评论 + DB pending->duplicate
41. **无关评论曾全部转发 Multica(2026-08-05 用户要求)**: 旧逻辑除关闭请求/
    need_info 补充外, 所有非程序评论(含"好的/收到/顶/👍/辛苦了/+1"灌水、
    与需求无关闲聊)都转发到 Multica 打扰开发小队。已修: `is_irrelevant_
    comment()` 双层判定(层2 短文本 ≤8 字命中灌水词集且无实质信号/纯 emoji;
    层3 功能词集+产品词库+issue 标题归一化交集为空 → 无关), 命中静默跳过
    (不转发不回复不触发意图2 追问)。**插入位置在意图1 之后、意图2 之前**:
    关闭请求"请关闭"不含功能词, 若过滤在意图1 前会被层3 误过滤(自查发现);
    "支持rds" 等灌水词+产品词短评论不过滤(层2 命中灌水词后检查实质信号);
    判定异常 fail-open。LLM 兜底预留 ENABLE_COMMENT_LLM_FILTER(默认 0)。
    注意: 灌水词集与功能词集不能重叠(如"支持"曾同时在两集, 导致"支持"
    单独不生效), 已从功能词集剔除
42. **@提及会绕过无关评论过滤(2026-08-05 实测)**: `is_irrelevant_comment()`
    判定前未剥离 mention 时, 评论 `[@skills_bot](https://gitcode.com/skills_bot)
    天气很好呀`(纯闲聊)被放行转发到 Multica —— mention 链接里的 "skills_bot"
    含 "skill" 子串, 命中 COMMENT_FUNC_WORDS 的 "skill" → 层3 `_has_substance`
    误判有实质内容。已修: 判定前先剥离 markdown mention 链接
    (`\[@[^\]]+\]\([^)]+\)`)与裸 `@提及`, 提及是结构性文本不算实质内容;
    回归 9 用例全过(mention+闲聊→过滤、裸@+闲聊→过滤、支持rds→不过滤、
    请关闭→豁免、提及+功能词→不过滤 等)。注意: 提及后跟真实功能词
    ("@skills_bot 请修复")仍正常放行
43. **竞品云需求必须前置判断, 否则被已存在检查误关(#130 事故, 2026-08-05)**:
    「帮我开发一个查询阿里云的vpc列表的skill」是**阿里云**需求, 正确处理应为
    竞品云 → invalid 标签+评论+关闭。但 handle_issue 里 ENABLE_EXISTING_CHECK
    (find_skill_in_open_prs)排在 ENABLE_HUAWEI_RELATED_CHECK **之前**:
    需求标题含产品词 "vpc" 命中 PR #129(华为云 vpc-list)标题 → 误判"该 skill
    已有 PR 在开发" → 直接关闭, 留了错误评论且没打 invalid 标签。已修
    (2026-08-05): handle_issue 中**竞品云检查提前到已存在检查之前**
    (classify 之后、ENABLE_EXISTING_CHECK 之前), 命中即 invalid 关闭入库,
    不再进入已存在/质量/相关性检查。handle_issue_update / comment 意图2 原本
    顺序正确(华为云相关性在已存在检查之前), 仅 handle_issue 创建路径有误。
    教训: **竞品云需求与华为云 skill 绝不重复**, 任何"重复开发"判断
    (仓库/PR/DB)都必须在竞品云判定之后, 否则把阿里云需求误当华为云 skill
    的重复需求处理。
44. **codeowner_assigned 不是绝对终态, 用户改标题后必须重新评估(#132)**:
    issue #132 创建时标题不含 "skill" → 走 codeowner 分配入库
    (status=codeowner_assigned); 用户把标题/body 改成「帮我实现一个查询
    南京天气的skill」(含 skill)后 update 事件到达, 旧 handle_issue_update
    把 codeowner_assigned 当终态 → **仅同步 body 后跳过, 无任何评论反馈**,
    需求从未被重新识别为 skill 开发任务。且即使走重新评估, 质量检查不通过
    (正文 18 字 <30)时 still_need_info 分支**只同步 body 不评论**(原设计
    防刷屏), 用户修改后完全静默。已修(2026-08-05):
    a) handle_issue_update 终态判断拆开: duplicate/invalid 仍终态跳过;
       **codeowner_assigned 仅在仍非 skill 任务时跳过**, 用户改成含
       "skill" 的标题后继续走重新评估(classify → 质量 → 相关性 → 判重
       → 已存在 → 建任务);
    b) still_need_info 分支: 同步 body 前先查 `_issue_has_bot_comment`
       ("需求信息不完整"), **未发过则补发一条质量追问评论**(用户修改后
       有反馈), 已发过则静默(同内容重复 update 防刷屏)。
    注意: handle_issue 创建路径的 codeowner_assigned 幂等跳过(2349 行)
    保持不变 —— 那是 open 事件重复到达的防重, 与 update 路径无关。
45. **修改/迭代已有 skill 的需求不能做"已开发"判断(#142)**: 「huawei-cloud-
    gaussdb-count 这个skill现在要改成只查询数量」是对已有 skill 的**修改
    需求**, 仓库里有同名目录是正常前提(就是要改它), 但旧 find_skill_in_repo
    按名称全称匹配到仓库已有同名 skill → 误判"重复开发"直接关闭。已修
    (2026-08-05): 新增 `is_skill_modification(title, body)` — 文本提到
    huawei-cloud-xxx skill 名称**且**命中修改/修复意图词(改成/修改/变更/
    更新/升级/优化/调整/改为/重写/改造/重构/迭代/改进/增强/修复/不对/报错/
    错误/异常/数量不对 等)→ 视为迭代需求, **跳过已存在检查三处调用点**
    (handle_issue / handle_issue_update / comment 意图2), 直接进入质量/
    相关性/判重/建任务流程。新开发同名 skill(「帮我开发一个huawei-cloud-
    xxx的skill」)不命中修改词, 仍正常判重关闭。注意意图词要**配合 skill
    名称全称出现**才生效, 避免"这个skill要改成xxx"无全称时误伤。GitCode
    重开 issue 用 PATCH `{"state_event": "reopen"}`(配合至少一个其他字段,
    如 title/labels, 否则 400 报"at least one parameter"; 响应 state 可能
    是旧值, 需二次 GET 确认)
46. **机器人账号提交的 issue 必须整体忽略(2026-08-05 用户要求)**: skills_bot
    等自动化账号创建的 issue(评测系统自动建单、机器人自建 issue 等)会被
    当普通需求处理 → 打标签/评论/建 Multica 任务, 产生机器人自处理噪音。
    已修: handle_event 中 kind=="issue" 分支最先检查提交者账号, 命中
    `BOT_AUTHOR_LOGINS`(默认 skills_bot,check_bot, 逗号分隔大小写不敏感,
    环境变量可覆盖)→ 返回 ignored, **零副作用**(不评论/不打标签/不分配/
    不建任务/不入库)。open 与 update 事件都过滤(过滤在 kind 分支, 不区分
    action)。注意与 BOT_PREFIX(评论前缀防死循环)互补: 前者管"机器人建的
    issue", 后者管"机器人发的评论"。
    **用户确认(2026-08-05 #148)**: 测试系统自动提交的 BUG 报告(如 #148
    「测试发现BUG: huawei-cloud-swr-list (PR #140)」)同样被无差别忽略,
    不进开发管线、无人跟进, 这是刻意设计, 保持现状。
47. **Skill 审查结论类型错位, 有 ❌ 仍判 ✅(#139 事故, 2026-08-05)**: PR #139
    (update skill: huawei-cloud-gaussdb-count)审查报告 6 项 ❌(含 #5/#6/#7/#17
    四项 🔴)+ 2 项 ⚠️, 结论却显示"✅ 通过(22/30 项通过)", 且合并检查"Skill
    审查通过"✅, PR 被放行进测试。根因: `_skill_review` 的 `add()` 把布尔
    ok 转成显示字符串(✅/❌/⚠️)存入 results 第 4 元素, 但结论计算仍用
    `ok is False` 判断 —— 字符串 `"❌" is False` 永远为 False → critical_fail/
    high_fail 恒 False → 结论恒 ✅。已修: 结论计算改为判断显示字符串
    `res == "❌"`(🔴 未过 → ❌; 🟠 未过 → ⚠️)。修复后 #139 结论为 fail,
    合并检查"Skill 审查通过"❌, 正确阻断进测试。**迁移教训: skill-pr 是
    LLM 技能(结论由模型按规则输出), 脚本化时"布尔 → 显示字符串"的转换点
    必须同步修正所有消费该字段的判断逻辑**。
48. **PR 审查把 scripts 子目录误当 skill 根 + 检测点4 恒通过(#140 事故,
    2026-08-05)**: PR #140(add skill: huawei-cloud-swr-list)顺带改了 ges
    skill 的 scripts/ges_graph_skill.py(测试残留注释), `_extract_skill_dirs`
    兜底"取倒数第二层"把 `skills/ai/ges/huawei-cloud-ges-graph/scripts`
    当成独立 skill 根审查 → 12/30 假失败, 且真正目标 skill 被连带。已修:
    a) `_extract_skill_dirs` 兜底改为**向上找最近的 huawei-cloud-* 目录**
       作为 skill 根(脚本/文档归属其所属 skill, 不单独成根);
    b) `pr_review_report` 对未变更的 SKILL.md 从仓库 contents API 拉取
       补入缓存, `_skill_review` 优先用变更文件、否则用缓存(检测点1 用
       md_content 非空判断, 不再因"PR 未改 SKILL.md"假报缺失);
    c) **检测点4「一个PR只提交一个Skill」真正生效**: 原实现恒 ✅(占位,
       "由 PR 级检查判定"但 PR 级从未实现), 现 PR 涉及多个 skill 目录时
       所有 skill 的该项标 ❌ 并注明违规, 结论 fail 阻断。注意: 检测点4
       判定的是"PR 变更文件跨 skill 目录", 一个 skill 内多个文件正常 ✅。
49. **PR 审查报告补齐 devops-bot 缺失能力(2026-08-05, 对齐 PR #82)**: 对比
    PR #82(devops-bot 生成)与 #140(脚本生成), 脚本报告缺三块能力, 已从
    `/home/wangliangchu/skill-for-robot/devops-bot` 继承:
    a) **Skill实现原理分析报告(阶段5)**: ~~集成 huaweicloud-skills-analyzer~~
       ~~(`SKILL_ANALYZER_RUN` 默认指向 devops-bot 的 run_analysis.sh,~~
       ~~`SKILL_ANALYZER_PYTHON` 指向其 venv), 浅克隆 PR 分支对每个 skill~~
       ~~运行实现原理分析(Skill 基本信息/依赖工具/Open API 接口识别/KooCLI~~
       ~~对应关系/业务等价性结论), 追加到审查报告末尾; 工具缺失或分析失败~~
       ~~时降级为"⚠️ 未生成, 不阻塞审查"(对齐 skill-pr 阶段5 容错)。~~
       **2026-08-12 用户要求已彻底移除**(PR #40 审查报告 52KB/54KB 被该
       章节占据, 且结论计算不依赖它): 删 `_run_skill_analyzer` 函数 +
       pr_review_report 阶段5 调用块 + ENABLE_SKILL_ANALYZER/SKILL_ANALYZER_* 
       配置, 审查提速约 1-4 分钟/PR。本地 skill_analyzer 工具链保留(未删),
       run_analysis.sh 不再被脚本调用。
    b) **安全扫描增强**: 增加 IaC 文件分类(Kubernetes/Terraform/Dockerfile/
       Helm)、密钥泄露结论、环境变量($HW_AK/$HW_SK 等)识别, 对齐 PR #82
       的安全扫描描述格式。
    c) **合并检查增强**: 关联 Issue 时增加"关联 Issue 已 accepted/triaged"
       检查项(用 `_pr_issue_status` 查状态+标签)。
    注意: 源码审查(5轴: 正确性/可读性/架构/安全/性能)是 devops-bot LLM 驱动的
    语义审查, 脚本暂未规则化实现, 需要时可按 code-review-and-quality.md
    补充。
50. **源码审查(阶段2.1)规则化补入(2026-08-05, 对齐 PR #82)**: devops-bot 的
    代码审查由 LLM 按 code-review-and-quality.md 5轴标准语义执行, 脚本此前
    缺失该段。已新增 `_source_code_review()`: 对代码/脚本类文件
    (.sh/.py/.js/.go/.java/.json/.yaml 等, 跳过 md/文档)做**规则化 5 轴
    检查**, 输出与 PR #82 相同的"### 代码审查"表格(级别/文件/行号/问题/
    建议), 插在 Skill 审查之前:
    - 正确性: shebang、set -euo pipefail、按位置取参 vs --flag 文档调用
    - 可读性: TODO/FIXME 残留、调试 print、**测试残留改动混入 PR**(中文/
      英文测试注释, 🔴)
    - 架构: 单文件 >1000 行
    - 安全: eval/危险命令、subprocess shell=True
    - 性能: 循环内网络调用(N+1)
    结论参与合并检查("代码审查通过")与最终结论(🔴 -> fail, ⚠️ -> warn)。
    纯文档 PR 不误报(✅ 0 项)。注意合并检查用 `code_conclusion == "✅"`
    而非 `!= "❌"`(⚠️ 也应按未通过处理, 初版写错已修)。
51. **测试失败后新 commit 不重新触发测试(#140 事故, 2026-08-05)**: PR #140
    测试失败(status=failed, SKI-149)后作者提交修复 commit, 16:37 收到
    `merge_request update` 事件(act_desc=source_branch_changed / update_reason=
    "source update"), 但 `_test_squad_notified` 幂等检查看到 15:47 已 @ 过
    skills_test → handle_pr 返回 matched_dup, **新 commit 未重新触发测试**。
    根因: 幂等检查只判断"是否发过通知", 不判断"是否有新 commit"。
    已修: handle_pr 逻辑3/兜底分支的幂等拦截加例外 —— `_is_new_commit`(
    act_desc=="source_branch_changed" 或 update_reason=="source update")时
    跳过幂等, 重新 @skills_test 触发测试; 标题编辑/状态变更等非源码更新
    仍幂等拦截(三场景验证: 新commit->matched, 标题编辑/普通update->
    matched_dup 零副作用)。注意: payload 的 `need_test` 字段为 False 是
    GitCode 侧标记, 不能作为触发依据; 判断源码更新用 act_desc/update_reason。
52. **文档提及 ≠ Skill间直接调用, 误报 fail 会连带阻断新 commit 重测
    (#142, 2026-08-05)**: PR #142 的 SKILL.md 新增只读指引「如需写操作, 请
    使用 `huawei-cloud-swr-image-management` skill」(该 skill 仓库中真实存在),
    检测点22 旧实现**对整个 skill 目录所有文件(含 .md)扫描 huawei-cloud-*
    名称**, 任何提及即判 🔴 ❌ → 审查 fail → review_failed。此时正值 SKI-152
    测试发现 BUG 后作者推新 commit 修复(source update 事件, 按 pitfall 51
    应重新触发测试), 被误报 fail 拦在审查关卡, **新 commit 无法重新测试**。
    已修: 检测点22 改为**仅扫描脚本/代码类文件**(.sh/.py/.js/.go/.java/.json/
    .yaml/.yml/.toml/.ini 等, `_CODE_EXTS`), .md/.txt 文档提及是正常引导,
    不算代码级直接调用。修复后 #142 结论 fail→warn(Skill 审查 30/30 ✅),
    重跑事件 matched SKI-152 重新 @skills_test。注意: 检测点22 语义是
    "运行时调用其他 skill"(脚本/配置里的引用), 文档里的功能指引(尤其
    "只读 skill 引导写操作用哪个 skill")应放行; 若脚本里出现其他 skill 名
    仍按 🔴 判。
53. **新 commit 后审查报告未重新生成(#142 第二次 commit, 2026-08-05)**: PR
    #142 第二次 commit 推送(act_desc=source_branch_changed)后测试通知重新
    触发了(pitfall 51 修复), 但 **PR 审查报告没有重新生成** —— `pr_review_
    and_notify` 的幂等("已有 [skills_bot] ## PR 审查报告 评论则跳过")对
    所有事件生效, 新 commit 也跳过; 而 handle_pr 的 reason 却写"修复后由
    update 事件重新审查", 设计意图从未实现。已修: `pr_review_and_notify`
    加 `force` 参数(force=True 跳过幂等强制重发报告), handle_pr 把
    `_is_new_commit` 提前到审查段计算并传 force —— 新 commit 强制重新审查
    + 重新发报告, 非源码更新(open/add tester/标题编辑)仍走幂等。
    三场景验证: 新commit -> force=True 重发; add tester/open -> force=False。
    `_is_new_commit` 只在 handle_pr 内提前计算一次, 逻辑3/兜底分支复用。
54. **同一 skill 的修复 PR 未关联原任务, 兜底新建撞 Multica duplicate 崩溃
    (#143, 2026-08-05)**: PR #142(add huawei-cloud-swr-namespace-list)测试
    发现 BUG 被关闭后, 作者修复并新开 PR #143(fix 同一 skill), 其 commit
    引用自定义 ID(HUAWEI-CLOUD-SWR-NAMESPACE-LIST-ISSUE-001/002)而非
    GitCode issue 编号 → `extract_issue_number_from_pr` 提取不到 → 逻辑3/
    逻辑3b 都未命中 → 逻辑4 兜底 `multica_issue_create` 撞 "Active
    duplicate issue exists: SKI-152" 校验 → RuntimeError 崩溃(事件未处理)。
    根因: handle_pr 普通分支缺"按 skill 名匹配已有需求"逻辑(仅 distribute
    分支的逻辑7 有 `find_requirement_by_skill`)。已修(2026-08-05):
    **handle_pr 在逻辑3b 之后、逻辑4 之前新增逻辑3c**: `extract_skill_name_
    from_pr` 提取技能名 → `find_requirement_by_skill`(requirement 表
    skill_name/title/body 模糊匹配, ORDER BY updated_at DESC 取最近) →
    命中且有多 multica 任务 → 更新该需求 PR 关联(gitcode_pr_id/
    gitcode_issue_number/gitcode_issue_url 指向修复 PR)+ 在原 multica issue
    上重新 `@skills_test`(`_test_squad_notified` 按 "**GitCode PR #{n} 已创建**"
    标记幂等, 新 PR 编号不同不会误拦)→ 返回 matched_by_skill。修复 PR 与
    add PR 同属一个开发/测试周期, 应复用原任务持续测试, 不新建任务
    (对齐逻辑7 分发任务复用思路)。注意: 逻辑3c 必须在逻辑4 之前, 否则
    仍会撞 duplicate; 若同一 skill 有多个历史需求, 取最近一条(updated_at
    DESC), 多轮迭代 PR 会逐次更新同一 requirement 的 PR 关联字段。
55. **「故障」能力词误判 bug + 相似需求检查误配(#161, 2026-08-06)**: issue
    #161「开发一个RDS相关的SKILL」正文是标准的故障排查类 skill 需求(核心场景:
    线上故障排查/故障定位排查, 技能名 huawei-cloud-rds-troubleshoot, 294 字),
    但 classify_issue 的 bug 线索词**裸「故障」**被能力描述误命中 → 判为 bug →
    质量检查按 bug 标准要求复现步骤/日志 → 误发「需求信息不完整」追问, 需求
    卡在 need_info。且若只修 classify, 升级路径的 find_similar_requirement_in_db
    还会把正文能力清单里的「查询」(全场景: 查询+运维管理+备份恢复)当成功能词,
    与历史 rds 查询需求(#104/#101)按"同产品+功能词交集"匹配 → 误关为重复。
    已修(2026-08-06):
    a) **classify_issue bug 正则「故障」加负向断言**: `故障(?!排查|定位|诊断|
       处理|恢复|检测|演练|发现|分析|预防|自愈|监控|告警|预警|隔离|转移|
       切换|注入|解决)` —— 故障排查/定位/诊断等是技能能力描述(尤其"故障排查类
       skill"是常见需求), 不算缺陷报告; 裸「故障」/「故障了」/「出现故障」仍
       判 bug。与「不能/失败/超时」等组合词防误伤同一思路, 补上了「故障」的
       漏网。
    b) **feat_hint 补「开发(一个|一款|一套|一)」**: 「开发一个…skill」是本仓库
       最典型 feature 句式, 旧词表(建议/需求/新增/支持/实现/希望)没有「开发」,
       只能靠 LLM 兜底。限定词防「开发环境」误伤; bug 检查在前, 不受影响。
    c) **find_similar_requirement_in_db 相似度判断仅用标题**: my_norm/my_funcs
       从"标题+正文"改为仅标题(对方侧本来就只取标题)。正文能力清单/枚举(如
       "全场景: 查询+运维管理+备份恢复")或复制残留(#113)不再参与功能词交集与
       包含判断; 产品词识别仍用全文本(标题命中优先排序)。真重复(同标题 rds
       列表查询)仍命中, #161 这类"同产品不同能力域"需求不再被误关。
    回归: 故障排查→feature、裸故障/故障了→bug、故障转移→feature、真重复
    命中、#161 不命中, 全过。注意: 标签 API 详情/labels 端点仍显示 [] 是已知
    序列化问题(pitfall 29), 用 `issues?labels=triaged` 过滤接口可确认标签已打上。
56. **质量检查失败分支漏判重, 同标题 bug 报告反复追问(#190, 2026-08-07)**:
    handle_issue 中质量检查失败(ENABLE_QUALITY_CHECK 不达标 → need_info 追问)
    在 3091 行**直接 return**, 而实时判重逻辑(3172 行)在质量检查**之后** ——
    质量不达标的重复提交永远走不到判重。用户连续提交同标题 issue
    「huawei-cloud-evs-list 在实际使用过程中老是一直等待」(lccode 测试链
    #186/#187/#189/#190/#191): #189/#191 因质量恰好达标或走 update 路径被
    判重关闭, #186 被旧 codeowner 逻辑错误分配, **#190 走了 need_info 追问**
    —— 同一条 bug 报告被反复追问, 用户收到 2+ 条「需求信息不完整」评论。
    已修(2026-08-07): 质量检查失败分支在 return need_info **之前**先执行
    `find_duplicate_issue`(强信号: 技能名精确匹配/标题归一化完全一致), 命中
    → 评论「🔁 与 #N 重复, 已在原 issue 中跟进」+ 关闭 + 不入库(幂等 marker
    与 3172 行判重共用 "已在原 issue 中跟进")。该修复不影响 pitfall 28 的
    "判重必须在质量检查之后" 原则 —— 那是针对弱信号(标题相似度)防误关,
    此处仅对强信号重复生效(同标题 bug 报告是典型场景, 信息不足但重复性
    确凿)。注意: 升级路径(handle_issue_update / comment 意图2)原本就带判重,
    只有 handle_issue 创建路径的质量检查失败分支有此漏洞。
57. **2.2/2.1.3 "等待用户补充"提示分支不入库, 用户按提示修改后 update 事件被
    跳过, 需求卡死(#241, 2026-08-09)**: bug 2.2 分流(未提供 skill 名称/格式
    不完整/仓库无此 skill)与优化类 2.1.3(未指定唯一 skill)分支会评论提示用户
    **修改 issue** 后返回, 但**不入库**(requirement_id=None, DB 无记录)。用户
    按提示编辑(如补充 skill 名称)触发的 update 事件落入 handle_issue_update
    的"无记录"分支 —— 该分支恒跳过(创建事件负责完整流程), 且 marker 清单
    (已识别SKILL开发任务/需求信息不完整等)不含这些提示 → 补充后永远无响应。
    #241 实测: 13:19 建 issue(无 skill 名)bot 追问, 14:50 用户补全
    `huawei-cloud-maas-tokens-usage` 后 update 被 skip, 需求卡死。已修:
    a) 新增 `WAITING_PROMPT_MARKERS`(未提供具体的 skill 名称/不是完整的
    skill 名称/未找到与/仓库中不存在 skill/未明确指定要优化的 skill)+
    `_has_waiting_prompt_comment()`; b) handle_issue 通用已处理守卫
    (`_issue_has_bot_comment(BOT_PREFIX)`)命中等待提示时**放行**重评估
    (否则 handle_issue_update 重走 handle_issue 也被守卫拦); c)
    handle_issue_update 无记录分支: 存在等待提示且无完成类 marker 时
    **重走 handle_issue 完整流程**(其 bug 2.2 分流在质量检查之前, 与
    create 路径语义一致), 提示评论有 marker 幂等不刷屏。
58. **bug 类 issue 会被 LLM PR/skills 覆盖判重误关(#241, 2026-08-09)**: 判重
    2.0 第 1/2 步(`_llm_pr_covers_issue` / `_llm_skills_covers_issue`)语义是
    "PR/仓库是否已实现该**需求**功能点", 对需求类有效; 但 bug 报告("xxx 运行
    一直失败")的 add-skill PR 已合入恰恰说明该 skill **存在且有问题**, 应走
    2.2 修复流程而非判重关闭 —— #241 实测: maas-tokens-usage 运行失败被
    "新增该 skill 的合入 PR #164" 误判"该功能点已有 PR 实现"而关闭, bug 永远
    无人修复。已修: classify_issue **提前到判重之前**计算 label(原在判重之后,
    打标签仍在质量检查/判重通过后统一执行), `label=="bug"` 时跳过两个 LLM
    覆盖检查(仅保留 find_duplicate_issue DB 兜底, 符合 #193 判重前置要求)。
59. **find_duplicate_issue 技能名精确匹配误伤同 skill 不同性质 issue(#241,
    2026-08-09)**: 判重策略 1"技能名精确匹配"假设同 skill = 同需求, 但同一
    skill 可有开发需求/优化需求/bug 报告多个**独立工作项** —— #241(bug"运行
    一直失败")被误判与 #218(优化"支持高并发", 仍 open)重复而关闭。已修:
    `find_duplicate_issue` 新增 `skip_skill_match` 参数, **bug 类调用时跳过
    技能名匹配**(只保留标题归一化完全一致判重; 同标题重复 bug 报告如
    evs-list 系列仍能命中), 4 个调用点(handle_issue 主判重/质量检查前置判重/
    handle_issue_update 判重/handle_issue_comment 意图2 判重)统一传
    `skip_skill_match=(label == "bug")`。注意: 该文件存在**多会话并发编辑**
    风险(2026-08-09 #241 实测另一 webhook 会话同时修复同一 bug), 改前必先
    grep 全文件确认无 `_is_bug_issue`/`_pre_label`/`_dup_label` 等半成品变量
    残留, 改后立即 py_compile + `--test` 验证。
60. **issue_quality_ok 剔除正则 \w 匹配中文, "skill：<中文描述>" 整段被当
    skill 名吃掉(2026-08-14 #6 实测)**: 剔除 skill 名的正则
    `skill[:：]?\s*[\w-]+` 中 `\w` 在 Python3 re.UNICODE 下**匹配中文** ——
    "帮我创建一个skill：实现查询租户下华为云sfs名称列表，仅返回名称列表就好"
    被剔除成 "帮我创建一个，仅返回名称列表就好"(15 字 <30) → 用户补充 body
    后 update 事件重评估仍 need-info, 且幂等 marker 存在不再补评论, 现象是
    "改了 body 没反应"。已修: skill 名固定 ASCII, 剔除正则改为
    `[a-zA-Z0-9_-]+`(huawei-cloud-xxx / xxx-skill / skill: name 仍正常剔除,
    #244 语义不变)。回归测试 test_quality_classify.py 两条: skill：中文描述
    通过 / skill: huawei-cloud-vpc-list 仍剔除。排查"补充 body 后仍被追问/
    无反应"先跑 `issue_quality_ok` 看剔除后字数的坑就在这。

## 验证

1. `python3 scripts/gitcode_event_handler.py --test` → 7 项全 OK(数据库/GitCode/Multica/四小队)
2. 构造各事件 payload 文件, 执行脚本看返回摘要(event/action/requirement_id)
3. 核对数据库: requirement 新记录 + flow 节点状态
4. 核对 GitCode issue/PR: `[skills_bot]` 前缀的进展评论
5. 核对 Multica: issue 已创建、assignee 为对应小队、评论已追加
6. `--check-handoff`: 测试小队名下 done 的 issue 应被 @验证小队并转移分配

## 关联

- `gitcode-webhook`(platform-setup): Gateway webhook 路由配置与签名验证
- `hermes-webhook-debug`(software-development): webhook 401/404 排查
- 生产环境 token 与工作区: SKILLS开发验证(生产) REPLACE_WITH_MULTICA_WORKSPACE_ID
