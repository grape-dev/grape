# 修复「用户澄清回复被 outbound ledger 误伤，awaiting_input 无法自动恢复，VOD 不续跑」[weekly-08]

> 状态：**已实施（TDD 先红后绿 ✅）**｜目标分支：weekly-08（部署分支 weekly-08-lyz）
> **敏感信息声明**：本文不含真实 IP、域名、账号、仓库名或 token（一律 `<xxx>` 占位）。
> 现场（生产本周环境）：Issue 由 VOD（本机 direct 引擎 `opencode-cred0`）执行并追问 3 个澄清问题，
> 状态置 `awaiting_input`；用户在 WebUI Issue 会话回复后，VOD 不再续跑，事件在管道入口即被丢弃。

---

## 〇、问题现象与影响

- VOD 处理 `issue_opened`（seq=`<seq>`）时向用户提出澄清问题，回帖 `<bot_comment_id>`（作者 grape_bot），
  随后调用 `awaiting-input` 将 Issue 状态置为 `awaiting_input`（任务流畅：`Grilling → awaiting-input`）。
- 用户在 WebUI Issue 会话（issue-conversation）回复澄清内容。该回复由 grape 自身通道
  （`POST /api/git-repo/<owner>/<repo>/issues/<n>/comments`）代发到 VCS，评论作者为**真实用户**
  （`<real_user>`，与 bot 无关）。
- 由于评论经由 grape 的 VCS 代理接口发出，**写侧 `record_outbound` 无条件把它记入
  `outbound_content_ledger`**（台账 id=`<ledger_id>`、content_id=`<user_comment_id>`）。
- GitCode Note Hook 将「用户自己的这条评论」回流传给 grape → 管道命中台账
  → `Pipeline skip: outbound ledger hit` → `event_skipped`（reason=`outbound_ledger_hit`）。
- 结果：`run_pipeline` 在第 2.x 层提前 return，**路由与 `_resume_issue_waiting` 均未执行**，
  Issue 永久停在 `awaiting_input`，VOD 不再续跑，任务假死。

**与设计目标的偏差**：`awaiting_input`/`pending_review` 双态的**唯一恢复通道**是「用户评论」。
台账设计的初衷（Issue #453 S11/S16）是抑制 **bot/系统自回帖**的回声，避免自循环；它不该拦
**真实用户的澄清回复**。本事故即台账「写侧无差别记账」造成的**误拦截**。

---

## 一、目标语义

1. **等待态用户的真实回复必须恢复**：Issue 处于 `awaiting_input` / `pending_review` 时，
   无论是用户直接在 VCS 网站留言，还是经 grape 通道代发，注释回声都**不得**被 `outbound_ledger_hit`
   丢弃——而应触发 `_resume_issue_waiting`（等待→processing）并按阶段矩阵续跑对应 agent（本例 VOD）。
2. **台账的原始职责不回退**：bot / 系统自回帖（R18.6.10 系统回帖、路径 B 以用户身份代发的 agent 评论等）
   在**非等待态/与被消费事件无关**时仍正常被台账抑制，防自循环。
3. 语义判据唯一化：**「该注释是否恰好充当了等待态恢复输入」优先于「该注释是否在本台账」**。
   恢复一旦发生（`_resume_issue_waiting` 为真），即视为"用户输入已消费"，后续台账跳过不再成立。
4. 写侧台账语义净化（加固，非必需）：用户通道（JWT/API Key）发出的评论默认不记账，
   仅 agent/会话通道（`tk_` session token）记账——使台账只记录「系统代发内容」，与 Issue #453 问责一致。

---

## 二、方案设计

### 2.1 读侧主修复（核心，必做）：恢复动作豁免台账跳过

`src/api/event_pipeline.py` 管道内三个相关节点，调整如下：

**A. 2.0b 恢复块补齐 GitCode Note Hook 的 issue 号解析（RC-B）**

现状（`event_pipeline.py:1368-1371`）：
```python
_issue_num = str(_oa.get("iid") or _oa.get("number") or (_payload.get("issue", {}) or {}).get("number") or "").lstrip("#")
```
GitCode（GitLab 系）Note Hook 的 issue 号位于 `object_attributes.noteable_iid`，
`iid` 是评论自身的 id，`number` 通常缺失 → `_issue_num` 取空 → `_resume_issue_waiting` 不执行。
与 `resolve_issue_id`（`event_pipeline.py:667` 已读 `noteable_iid`）对齐，补一个 key：

```python
_issue_num = str(_oa.get("iid") or _oa.get("number") or _oa.get("noteable_iid")
                 or (_payload.get("issue", {}) or {}).get("number") or "").lstrip("#")
```

**B. 2.0b 块记录恢复发生标记**

`_resume_issue_waiting(repo_key, int(_issue_num))` 返回 True 时，置 `_resume_applied=True`
（模块内本次事件作用域局部变量），并保留现有日志
（`issue #N awaiting/pending_review → processing (issue_comment reply)`）。

**C. 台账跳过节点增加「恢复优先」豁免（RC-A，主拦截点）**

`event_pipeline.py:1553-1567` `issue_comment`/`note_hook_pr` 台账命中分支中，
在 `is_our_outbound(...)` 判定为 True 之后、`return result` 之前，追加：

```python
if event_type == "issue_comment" and _resume_applied:
    log.info(f"Pipeline: outbound ledger hit but resume supersedes "
             f"(user clarification reply consumed for issue={_pre_issue_id}), allow through")
    # 不 return，继续走自循环/路由 → 派发 stage owner agent（awaiting 出自 vot 即重派 VOD）
else:
    ...原有 skip+append_skipped_event+return ...
```

> 说明：自循环检测（2.0 节点）在台账检查（2.0a，行 1553）之前，bot 作者评论已被
> `self_loop` 拦下；能到达 1553 且 `_resume_applied=True` 的只有**非 bot 作者、且恰好把
> 等待态翻回 processing 的评论**——即用户的真实澄清回复。不存在放行 bot 自回帖的路径。

### 2.2 写侧台账语义净化（加固，可选但建议同 PR）

`src/api/git_repo_api.py` `create_issue_comment`（`386-400`）记账处：

- git-repo 透传端点仅接受会话级临时 token（`tk_`，`_repo_token` 对非 session 一律 403）——
  因此**判据不能用 auth_type 分 agent/jwt**，而要用 `user["agent_type"]`：
  - `agent_type == "chat"`（chat agent 会话，`_repo_token` 解析为**用户 token**）→ 评论是
    **用户的真实回复**（WebUI Issue 会话回复即此通道），**不记账**——回声必须触发 awaiting_input 恢复；
  - `agent_type != "chat"`（devops 事件 agent，bot token 仓库身份）→ 系统回帖，**记账**（回声抑制，保持现状）。
- 相应 `record_outbound` 调用在 chat 通道时跳过（或传 `actor_identity=user["username"]` 供审计）。
- 路径 B warning：agent 以用户身份代发走的是 devops 会话（`agent_type≠chat`），因此
  **S11-1「路径 B 用户身份回声 → 台账拦截」不受影响**。
- `src/api/routes/issues.py` `clarify_issue` 同理：开发者澄回复经 `require_no_session_token` 通道发出，
  且已在路由内同步 `_resume_issue_waiting`（issues.py:1439-1441），台账记账与否不影响恢复——列为可选对齐点。

> 读侧 2.1 是**行为兜底**（不依赖写侧是否改对）；写侧 2.2 是**台账语义还原**，二者可独立生效。
> 建议 2.1 必做、2.2 同 PR；若只做 2.1，事故可解但台账仍残留"用户评论 = 系统代发"的脏数据。

---

## 三、模块改动

| 模块 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | ①2.0b `_issue_num` 解析补 `noteable_iid`（RC-B）；②2.0b 恢复成功后置 `_resume_applied`；③台账命中分支加 `_resume_applied` 豁免（RC-A） |
| `src/api/git_repo_api.py` | `create_issue_comment`：`agent_type=="chat"`（用户 token 代发）不记账；devops 会话仍记账（写侧净化） |
| `tests/test_issue_comment_resume_ledger.py`（新增） | 见 §六 TDD（先红后绿） |
| （不改）`src/outbound_ledger.py` | 台账读写与 TTL 语义不动 |

---

## 四、关键实现要点

1. `_resume_applied` 须在 `run_pipeline` 本次事件作用域内定义、2.0b 置值、1553 读取，
   用同一实例贯穿，避免跨事件污染。
2. 2.0b 块已有 `except Exception` 兜底（`event_pipeline.py:1405-1406`），补 key 不影响其健壮性；
   `_resume_applied` 置值也包进该 try，异常时回落 False（退化为现状）。
3. 豁免分支只作用于 `issue_comment`；`note_hook_pr`（PR 评论）不豁免（PR 讨论非 issue 澄清恢复输入）。
4. 写侧判据用 `user.get("agent_type") == "chat"`（git-repo API 仅收 `tk_`，故不用 auth_type 分通道；
   chat 会话经 `_repo_token` 走用户 token = 用户回复）；`actor_identity` 透传 `user.get("username")` 提升审计可读性。
5. 兼容旧数据：即使历史台账已残留"用户评论"记录，读侧 2.1 仍能按 `_resume_applied` 放行——无需数据迁移。

---

## 五、影响面 / 风险

| 项 | 说明 |
|----|------|
| 用户检查恢复（RC-A） | 等待态用户回复不再被台账误拦，VOD/所在 stage owner 正常续跑——事故根治 |
| GitCode Note Hook 解析（RC-B） | `noteable_iid` 补入后，等待态恢复与 `/done` `/cancel` `/resume` 开发者指令对 GitCode 评论全部生效 |
| 台账职责 | 写侧只记 agent 会话代发；bot 自回帖/路径 B 用户身份回声仍被拦（Issue #453 不回退） |
| 风险（豁免面） | 豁免仅限「`_resume_applied` 为真」的 `issue_comment`，bot 自回帖到不了该分支（先被 self_loop 拦）——低风险 |
| 回归范围 | `test_issue_453_outbound_ledger.py`（S11-1/S16-1/S11-3/S16-3）、`test_issues_awaiting.py`、`test_issue_comment_routing.py`、`test_path_b_self_loop_r20.py` 全量复跑确认不回退 |

---

## 六、测试设计（TDD，先红后绿）

落地文件 `tests/test_issue_comment_resume_ledger.py`（沿用 `test_issue_453_outbound_ledger.py` 的
内存库 mock 夹具：`_DbProxy`/`_DbLockProxy` + `autouse` monkeypatch + `init_database`）。

| ID | 场景 | 断言（red 阶段现状 → 应 fail） | 归属修复 |
|----|------|--------------------------------|----------|
| QA-01 | GitCode Note Hook：`object_attributes` 仅含 `noteable_iid`，Issue 处 `awaiting_input`，无台账记录 | `_resume_issue_waiting` 生效：issue.status→`processing`；并产生 VOD 事件（`event_skipped` 无 `outbound_ledger_hit`） | RC-B |
| QA-02 | 同 QA-01 但该评论 ID已在台账（模拟"经 grape VCS API 代发的用户回复"） | **不产生 `outbound_ledger_hit` skip**；issue→processing；VOD 事件产生 | RC-A |
| QA-03 | 用户回复在 `pending_review` 状态 | 恢复为 `processing`，按 stage owner 续跑，不被台账拦 | RC-A/B |
| QA-04 | bot 作者评论（grape_bot，台账命中，Issue 非等待态） | 仍 `outbound_ledger_hit` skip，不产任务（S11-1 回归护栏） | 防回归 |
| QA-05 | 用户回复经 `POST /api/git-repo/.../comments`（chat 会话，`agent_type=chat`） | 写侧**不**记台账；回声走恢复链路 | 写侧 2.2 |
| QA-06 | devops 会话（`agent_type≠chat`）经同一接口发评论 | 写侧仍记账（台账语义净化不回退） | 写侧 2.2 |
| QA-07 | `/done` 指令在 GitCode 评论触发 | `confirm_issue_done` 生效（`noteable_iid` 解析生效的旁证） | RC-B |

red 阶段：先写以上断言，跑 `pytest tests/test_issue_comment_resume_ledger.py -x` → 全部 fail；
green 阶段：实现 §二改动，断言全部 pass；
全量：`pytest` 相关套件（§五回归范围）保持 green。

实施记录（2026-10-27）：
- RED：`tests/test_issue_comment_resume_ledger.py` 初版 QA-01/02/03/07 全 fail（QA-04/补充护栏 pass）；
- GREEN：读侧 RC-B（noteable_iid）+ `_resume_applied` + RC-A 台账豁免 → QA-01/02/03 转 pass；
  QA-07 断言修正（confirm_issue_done 仅翻 active/processing，seed 由 awaiting_input 改 processing）→ pass；
- 写侧 2.2：按 `agent_type=="chat"` 判据实现（git-repo API 仅收 tk_ 会话，chat 会话=用户 token 代发=用户回复不记账；
  devops 会话仍记账），QA-05/06 补入后 8/8 pass；
- 回归：`test_issue_453_outbound_ledger.py`/`test_issue_453_self_loop.py`/`test_issues_awaiting.py`/
  `test_issue_comment_routing.py`/`test_path_b_self_loop_r20.py` 共 124 项全 pass；`test_git_repo_api.py`
  等 4 处失败经 stash 对照确认与本次改动无关（weekly-08 合入前已存在）。

---

## References

- `docs/weekly-08/issue-453*.md`（代发台账设计 + S11/S16 用例）
- `docs/weekly-08/issue-explicit-awaiting-input-api-design.md` §3.6（issue_comment 自动恢复）
- `docs/weekly-08/issue-pending-review-explicit-design.md` §3.5（pending_review 双态）
- `src/api/event_pipeline.py:1353-1580`（2.0b/自循环/台账/标记链路）
- `src/api/event_pipeline.py:639-679` `resolve_issue_id`（`noteable_iid` 现成处理，对齐参考）
- `src/api/issues_center.py:492` `_resume_issue_waiting`
- `src/api/git_repo_api.py:386-400` / `src/api/routes/issues.py:1377-1413`（写侧台账点）
- `src/outbound_ledger.py:144/181`（`record_outbound` / `is_our_outbound`）
- `tests/test_issue_453_outbound_ledger.py`（测试夹具与 assert 模式）