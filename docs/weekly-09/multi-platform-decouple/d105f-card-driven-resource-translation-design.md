# 台阶3 实施设计：资源 API 翻译卡驱动化（新平台零 providers 模块）

状态：已实施（commit 见 git log `feat(vcs): card-driven resource translation`）。
前置：d105e 平台接入验收（①~⑦ 面）；本片把其中③（资源 API 面）从「必须写
providers/<name>.py」翻正为「卡 resources 段声明即接入」。

## 一、卡 schema（PlatformCard.resources）

```json
"resources": {
  "repo_segment": "repos/{owner}/{repo}",      // 仓库资源段模板（缺省=github 形态）
  "path_map": {"/pulls": "/merge_requests"},   // 网关子路径 → 原生子路径（字面路径）
  "query_state": {"open": "opened"},           // 中性 state → query 值（键⊆open/closed/all）
  "rules": {"issues.update": {                 // <resource>.<op> → 字段规则（apply_rules 形态）
    "body": "description",                     // str：重命名透传
    "assignee": {"drop": true},                // 声明化丢弃
    "state": {"native": "state_event", "values": {"open": "reopen", "closed": "close"}}
  }}
}
```

- 缺省语义（**字段级合并**）：卡未声明 resources → 整体回退 providers 模块
  （存量四平台行为零变化）；resources 段内某键缺省 → 逐字段合并 github 形态
  对应值（rules 按 (resource,op) 合 github `_RULES`；query_state 合 github
  恒等表；repo_segment 缺省 `repos/{owner}/{repo}`；path_map 缺省空）。新卡只写
  差异字段。
- **缺省仅覆盖 github 已定义的资源类型/操作**；新增资源类型必须显式声明，
  未声明且非 github 内置 → ValueError（不静默透传）。
- transform 回调（github 的 assignees 列表化 / Closes #N 追加、gitcode 的
  inner_issue_nums 拼接）纯 JSON 不可表达：作为 github **代码侧缺省**保留，
  卡可用同名 str 规则或 drop 覆盖；复杂语义仍走 providers 模块。

## 二、消费顺序

1. 卡 resources 段优先（`translate_inbound`/`translate_query_state`，逐字段合并
   github 缺省）；
2. `_PROVIDER_MODULES` 回退（存量四平台卡无 resources → 行为不变；双轨期，
   代码内 TODO 标注弃用条件：四平台卡迁移完成后删除回退）；
3. 两者皆无 → `ValueError("Git provider 未支持: ...")`。

URL/request/token 消费面同序：`providers.module_for(p)` 模块优先 → 纯配卡平台
（卡带 resources）落 `card_module.CardProviderModule` 通用垫片（api_auth 决定
鉴权形态：query_token / bearer|token_header / private_token；bot/user token 走
rc 单表通用链）。`git_repo_api` 的 `_provider`/`_repo_token`/`_vcs_url`/
`_vcs_req` 全部改经 `module_for`（`_is_supported_provider` = 模块 ∪ 卡 resources）。

## 三、守卫（build_platform_card，PlatformCardError）

- resources 段整体可缺省；段内已声明子结构才做必填校验；
- repo_segment 为路径模板 → 必含 {owner}/{repo} 占位符；path_map 为字面路径
  → 不要求占位符（静态路径不误伤）；rules 键必须 `<resource>.<op>`；dict 形态
  字段规则必须含 native（或 drop:true）；query_state 键 ⊆ open/closed/all；
- 报错信息三要素：卡 ID（provider 前缀）+ 字段路径（resources.xxx）+ 期望格式。

## 四、测试

- `tests/test_platform_onboarding_acceptance.py` ③ 翻正：正向（具体映射结构
  断言，防静默空映射）+ 负向（非法资源/非法 op 仍拒）。
- `tests/test_platform_card_matrix.py`：③ gate 三分支 + 合成卡资源 API 全绿
  （翻译/URL/垫片/接入判定）。
- `tests/test_s3_card_driven_resources.py`：四平台**双轨等价性**（模块规则转
  声明化彩排卡，同输入两路径逐字段结构化对比，transform 字段排除并单测锁定）
  + 守卫矩阵（含报错可读性）。
- 回归：tests/test_git_provider / test_platform_card_matrix /
  test_platform_onboarding_acceptance / test_vcs_dialect_backend /
  test_s107_provider_modules / test_gitrepo_string_ident_contract /
  test_git_repo_api 全绿（test_git_repo_api 一例
  `TestRepoCredentialProviderByPath::test_unknown_path_prefix_falls_back`
  基线红——clean HEAD 同样失败，双侧 diff 排除）。

## 五、迁移策略

存量四平台卡**暂不迁移**（gitee 模块保留）；迁移 = 把各模块 `_RULES` 的
JSON 可表达子集誊进卡 resources（彩排卡形态见 test_s3 等价性用例，逐字段
对比保证迁移不漂移），transform 字段留模块直至机制扩展。全部迁完后删
`_PROVIDER_MODULES` 回退（双轨期收口）。
