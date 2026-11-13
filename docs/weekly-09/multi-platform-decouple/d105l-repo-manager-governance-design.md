# D105-17：repo_manager 存量治理设计（2026-11-09 用户裁定 fail-closed）

## 目标

repo_manager.py 80 处平台字面量引用治理：调用方迁适配器端口、repo_manager
退化为 gitcode 存量实现 + 卡驱动、未知平台 fail-closed。完成后
test_platform_whitelist_ratchet 的 _COMPARE_BASELINE（repo_manager 2 行）与
二元 BASELINE（repo_manager 全部条目）清零摘除。

## 语义裁定（用户 2026-11-09）

**未知/未配卡平台 fail-closed**：平台必须在册（内置四平台或已配卡）才可
操作；未知平台显式报错，不再静默回退 gitcode。具体：
- `_host_of_url` 解析失败 → 不再回退 "gitcode.com"，按调用上下文报错或返回空由上游 fail-closed
- `resolve_bot_username` 未知平台 → 不再兜底 gitcode pool，显式 ValueError
- 模板 `source_platform` 缺省 "gitcode" → 缺省改为必填/显式（存量模板数据
  已带 source_platform 字段的保持兼容；缺失字段的模板按 gitcode 处理一次并
  log.warning 留痕——数据迁移由回填完成，代码不再持有缺省）

## 改动分工

### 批次 A（src 治理）

文件集：`src/user/repo_manager.py`、`src/token_resolver.py`、
`src/api/routes/repos.py`、`src/api/routes/scenes.py`

1. **repo_manager.py**：
   - 文件头定位注释改写："gitcode 存量实现模块——平台通用能力请走
     src/token_resolver 适配器端口"
   - `get_hosting_provider_config`：github 分支保留（github 专属 webhook 函数
     指针），其余平台从平台卡派生（webhook path/event header 走
     `_webhook_path_from_card` 已有机制）；未知平台 fail-closed ValueError
   - `resolve_bot_username`/`_host_of_url`/`_KNOWN_VCS_PROVIDERS`：去 gitcode
     兜底，未知平台 ValueError（消息含平台名+指引配卡）
   - `push_template_to_repo` 的 `if provider == "github"` 分叉：迁到
     `_card` 派生（target host/clone URL 从目标平台卡取；.github/workflows
     删除怪癖作为 github 卡能力位/方言处理——若卡表达不了允许保留在 github
     分支但需挂 module_fallback 同款审计）
   - 函数命名保留（get_user_gitcode_login/_download_gitcode_tarball 等
     gitcode 专名函数不更名——它们就是 gitcode 实现）
2. **token_resolver.py / repos.py / scenes.py 调用方**：凡从 repo_manager
   导入且已有适配器端口等价物的（fetch_repos_with_role/create_repo/
   find_existing_webhook/get_collaborator_permission），切到端口；无端口
   等价物的 gitcode 专属函数（如 accept_project_invite）保留直调并在调用点
   注释标注 gitcode-only

### 批次 B（测试基建与行为锁）

文件集：`tests/test_repo_manager_governance.py`（新建）、
`tests/test_platform_whitelist_ratchet.py`（BASELINE 摘除）、
`tests/test_uc38_n5_closure.py` 如受影响

1. **HTTP mock fixture**：repo_manager 的 `_req`/`_req_query` 打桩（requests
   mock 层面），支撑 fail-closed 与分派行为锁
2. **行为锁**：
   - 未知平台 fail-closed 三点（host/bot username/模板源）各一断言
   - github webhook 分派经 get_hosting_provider_config 正常
   - 卡驱动 target host：模板 push 到非 gitcode/github 平台 → push URL 用卡 host
3. **BASELINE 清零摘除**：test_platform_whitelist_ratchet.py 中
   _COMPARE_BASELINE 的 repo_manager 条目删除、二元 BASELINE 的
   repo_manager 条目删除（含看护测试自然通过）

## 验收

- 两 ratchet 文件 repo_manager 条目清零（test_baseline 看护不红）
- 家族回归：test_repo_collaborator / test_s88_bot_chain_and_dialect /
  test_s90_template_push_derivation / test_s59_* / test_issue_372_deep_analysis
  失败集与基线 diff 零新增
- 未知平台 fail-closed 行为锁 3 绿

## 实施记录（2026-11-09，双批次并行 + 主会话收口）

- 批次A：src 治理（fail-closed 三点、卡驱动 hosting config、调用方迁移；7157a6455）
- 批次B：行为锁 4 + BASELINE 摘除 2 条（2f362d96d）
- 主会话收口：未知模板源 fail-closed（list_vcs_platforms 判定）；workflows 怪癖与
  webhook 函数指针两处 provider 比较经审阅**合理保留**（gitcode 卡 dialect 同为
  github，dialect 无法区分；端口收口归 S62 后续），登记 _COMPARE_BASELINE
  x2 并附理由——repo_manager 比较点 8→2；s88 legacy_pool 测试翻转为 fail-closed
- 裁决：家族失败集 diff 零新增（project_templates 8 红为双 runs 共有存量基线）

用例：UC75（12-usecases-consumer）；需求：S102。行为锁 4 用例见 tests/test_repo_manager_governance.py。
