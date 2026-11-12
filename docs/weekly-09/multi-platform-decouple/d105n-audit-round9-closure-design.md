# D105-18 续：续审九轮收口设计（审计 F1-F6，2026-11-09）

## 背景

外部审计续审九轮（D105-17/18 增量）报六项 findings，全部实锤复核成立。
本批收口 + 场景锁固化。

## 逐项修法

- **F1（P1，棘轮基线失配）**：D105-18 把 push workflows 判定改为三元式
  `_wf_denied = (provider == "github") if _wf is None else (not _wf)`，
  但 _COMPARE_BASELINE 仍登记旧行 `if provider == "github":` ×2——旧点只剩
  webhook 1 处（报"基线点消失"）、新形态计新增回归（双 RED）。修法：基线
  改双键（三元式 ×1 + webhook if ×1），消失点摘除，注释同步。纪律入册：
  改守卫拦截的代码必须同步改基线并实跑（本分支第 5 次同类失守，收口）。
- **F2（P2，死代码）**：repo_manager 模板源平台 raise ValueError 之后的
  log.warning 不可达，且注释声称的"缺失字段留痕"实际静默。修法：死代码
  删除；warning 移入真正的缺失字段 else 分支（语义归位）。
- **F3（P2，守卫落后）**：backfill 精确比对守卫 pin `at != tok`，代码已是
  D105k 的 `at.strip() != tok.strip()` 归一形态。修法：守卫更新为 strip
  归一形态（decrypt 断言保留）。
- **F4（P2，哨兵残留）**：`_resolve_issue_org_id` explicit 路径 `or 0`
  数值哨兵残留 + repo 路径对 DB 返 "0"（D105-15 TEXT 保值哨兵）未归一。
  修法：repo 路径 0/"0" 统一 ''；explicit 原样透传（非 str 化，保
  `==31` 契约）、0/'' → ''。场景锁 S104（tests/test_s104_issue_org_sentinel.py
  5 用例：int 0 / '0' / 真实归属 / 无记录 / explicit 族）。
- **F5（P3，docstring 谎报）**：test_security_tab_bind_states 两处声称
  后端按 `token_source='oauth'` 计算——src 全库无该字段，真实判据是
  `bool(ei.access_token)`（has_oauth_token）。docstring 纠正。
- **F6（P3，台账数字漂移）**：D105-17 行"行为锁 4 绿"→ 实有 5 用例，
  台账更正。

## 边界（本批不做）

- test_cred_*_by_gitcode_id 2 例预存悬空引用失败（审计归因表原样保留登记，
  非 F1-F6 清单项，另轮处理）。
- 专用字段路径（gitcode_token/github_token）不支持 workflows_write 声明：
  审计 P3 观察项，登记为 D105-18 范围边界（声明走泛型 platform_tokens），
  文档已明确。

## 验收

- 审计失败表 6 例中 4 例转绿（F1 双 RED + F3 + F4），2 例为预存悬空引用。
- 定向集 9 文件 47 passed / 2 failed（仅预存 2 例）。
- 新增场景锁 test_s104_issue_org_sentinel.py 5 用例全绿。

## 续审十轮收口（2026-11-09 追记）

round-10 审计四问全成立、F1-F6 六/六验真。遗留项清偿：

1. **悬空兜底用例退役**：TestGetGitcodeCredByGitcodeId 原 2 用例引用
   D3 有意删除的 `_get_gitcode_credential_by_gitcode_id`（删除前生产库
   出报告留痕于 test_multi_platform_decouple_step1.py:127），连续 4 轮
   RED。转退役锁 `test_dangling_fallback_retired`：`hasattr=False` 断言，
   兜底回潮即 RED；现役供给链行为由 get_user_token_with_refresh
   （deps.py:566）用例族覆盖。
2. **命名债**：test_s94_workflows_capability_scenarios.py →
   test_s103_workflows_capability_scenarios.py（S94 被 observability
   占用），台账/UC76/S103/mapping/design 共 5 处引用同步。

验收：分支 RED 归零（372 文件 2 失败为台账在册测试基建专项预存族，
非本分支功能问题）。

## 续审十一轮收口（2026-11-09 追记）

round-11 审计四问全成立、56/56 首次全绿，仅 2 个 P4，本批清偿：

1. **F1 自我描述同步**：test_s103_* 文件改名后内部仍是 S94 口径——
   docstring 三处、NS 前缀 s94wf、用例名 test_s94a-d 全量对齐 S103
   （实际范围大于审计报告指出的两行）。
2. **F2 断言收紧**：S104-e `explicit_org=0` 从宽容 `in ("", 0, "0")`
   收紧为 `== ""`（实码 `or ""` 恒返 ""；宽容断言即审计六轮批判过的
   掩盖信号风格）。S104 场景行同步改写。

验收：S103 场景 4 + S104 哨兵 5 + D105m 行为锁 6 = 15 passed，
全测试面零 RED。
