# Plan: Issue #2121

## 方案概述
一个写后联想入口 + 一个显式回源参数 + 体验接口显式承载托管结果 + 前端两处刷新。

## 实现步骤
1. `src/user/db.py`：新增 `invalidate_user_repos(user_id)`，把 `synced_at` 置
   NULL（保留缓存行与 `is_join`），下一次 `/repos/member` 命中过期逻辑回源。
2. `src/api/routes/repos.py`
   - `/repos/member` 增加 `refresh: int = 0`；`refresh=1` 跳过 TTL 判定直接回源并落库。
   - `host_repo` 所有成功返回分支（GitCode/GitHub，复用/新建 webhook）统一经写后出口调用 `invalidate_user_repos`。
   - `remove_managed_repo` 成功移除后调用 `invalidate_user_repos`。
   - `create_from_template` 建仓/复用成功后显式失效（host 失败也能进未托管栏）。
3. `src/api/routes/scenes.py`：`experience_market_scene` 记录 `hosted/host_error`，
   host 失败时返回 `hosted=false` 且 message 明确说明可重新托管。
4. `src/api/static/js/pages/scene-market.js`：体验响应区分 host 失败提示；
   `_selectRepoAndNav` 清 `repo_member_cache` 并追加 `/repos/member?refresh=1`。
5. `src/api/static/js/pages/repos.js`：`renderRepos(forceRefresh)`、解除托管后带
   `refresh=1`；未托管空态保留「↻ 从 GitCode 同步」入口；chunk `?v=` 版本号 +1。

## 风险
- `refresh=1` 绕缓存会触发 VCS N+1 权限查询，仅限写后路径与手动管理页使用，
  默认路径维持 30 分钟缓存。
