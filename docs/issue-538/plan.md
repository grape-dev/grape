# Plan: Issue #538

## 根因
两处空结果判定仅以 `all_repos` 是否为空作为失败信号，未区分「拉取失败」与「拉取成功但账号确无仓库」，
导致合法空结果落入 502（无缓存时）分支。

## 方案
1. `list_member_repos`：
   - `if not all_repos and cached` → `if not all_repos and failed_providers and cached`（失败且有缓存 → `_stale` 兜底，行为不变）；
   - `if not all_repos` → `if not all_repos and failed_providers`（失败且无缓存 → 502，行为不变）；
   - 合法空结果（无失败信号）走原有正常路径：去重（空）→ `save_user_repos(user_id, [])` 落库空列表 →
     返回 `{"repos": [], "_cached": false}`。
2. `sync_repos`：`if not all_repos` → `if not all_repos and failed_providers`；
   合法空结果走去重 → `save_user_repos(user_id, [])` → 组织隔离过滤 → 返回 `{"repos": [], "message": "同步成功"}`。
   reauth 401 时 failed_providers 已包含该平台，行为不变。
3. 前端：`loadRepos`/`_forceSyncRepos` 均以 `d.repos || []` 接收，
   `renderReposFromData([])` 已走 `renderRepoSelection([])` 空态，无需改动。
4. 测试：新增 `tests/test_member_empty_repos.py`，mock `with_oauth_token` 返回 `[]` 覆盖
   member/sync 两接口 + 失败仍 502 两分支。

## 分支顺序保持
reauth+有结果 partial → reauth+无结果+无缓存 401 → 失败+有缓存 _stale → 失败+无缓存 502 → 合法空/有结果正常路径。
既有分支顺序不变，仅收紧 502/_stale 的触发条件。

## 任务拆解
- [x] 修改 list_member_repos 空结果判定
- [x] 修改 sync_repos 空结果判定
- [x] 新增回归测试（4 用例）
- [x] 运行相关既有测试回归
