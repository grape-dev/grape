# Task: Issue #2121

- [x] `invalidate_user_repos` 失效入口（db 层）
- [x] `host_repo`（GitCode/GitHub 全部成功返回统一出口）/ `create_from_template` / `remove_managed_repo` 写后失效
- [x] `/repos/member?refresh=1` 强制回源，默认仍走缓存
- [x] 体验接口返回 `{full_name, hosted, host_error}`
- [x] 前端 `_selectRepoAndNav` / `_doUnhost` 刷新链路 + 未托管空态兜底
- [x] 新增测试：缓存失效、refresh 绕过缓存、默认命中缓存、GitHub 托管失效、解除托管复位与刷新结果、host 错误文案
- [x] 现有仓库路由测试回归
