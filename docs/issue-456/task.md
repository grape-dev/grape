# Task: Issue #456 — 任务拆解与执行记录

## 任务清单
- [x] T1 读取 Issue/Spec 与相关源码（organizations.js / app.js `_accountGoBack` / account.js 样式先例 / locale 文件）
- [x] T2 编写 docs/issue-456/{spec.md, plan.md, task.md}
- [x] T3 zh/en organizations.json 新增 `back_to_workspace` 文案
- [x] T4 organizations.js `renderOrganizations()` 头部新增「返回工作台」按钮（`bt bt-o`，点击 `app._accountGoBack()`）
- [x] T5 自动化验证：node --check / JSON 校验 / pytest 后端回归
- [x] T6 新增前端测试 tests/js/test_issue_456_orgs_back_to_workspace.js（7 个断言点全通过）+ 手动回归清单（静态页改动）
- [x] T7 自审查（5 轴）→ 提交 → push → 建 PR（base=weekly-08-ljt）

## 自动化测试证据
- `node tests/js/test_issue_456_orgs_back_to_workspace.js`：7/7 通过（按钮渲染/样式/onclick/i18n 文案/同排位置 + `_accountGoBack` 三分支守卫：未绑定→引导页、有仓库→dash、无仓库→仓库列表）
- `node --check src/api/static/js/pages/organizations.js`：通过
- `python3 -m json.tool`：zh/en organizations.json 均合法
- 全量 pytest：`tests/playwright/*`（缺 playwright 模块）与 `tests/test_chat_task_scene.py`（引用不存在的 `build_task_chat_prompt`）为基线既有收集错误；静态资源相关子集（test_help_page_generation / test_issue_372_round3_frontend / test_runtimes_stats_i18n 等）带改动与干净基线结果完全一致（10 failed / 109 passed，均为基线既有失败）；tests/js 全目录仅 9 个基线既有失败（暂存改动复跑对比确认），本次新增/修改用例全部通过

## 手动回归清单（静态前端改动，浏览器路径验证记录）
| # | 场景 | 预期 | 结果 |
|---|------|------|------|
| 1 | 登录 → 工作台右上角组织菜单「管理组织」→ 组织管理列表页 | 页头可见「← 返回工作台」按钮，与「创建组织」同排，`bt bt-o` 描边样式 | 代码路径核对通过：按钮渲染于 `renderOrganizations()` 头部按钮容器（organizations.js:10-12），样式与 account.js:31 同款 |
| 2 | 有当前仓库时点击按钮 | 恢复侧边栏 → `nav('dash')` 回工作台 | 核对通过：`_accountGoBack()`（app.js:560-575）`currentRepo()` 非空走 `nav('dash')` |
| 3 | 未绑定平台（bindings 为空）时点击按钮 | 恢复侧边栏 → `goToRepos()` 回仓库引导页，无 403 | 核对通过：app.js:565-568 bindings 为空分支 |
| 4 | 无当前仓库但已绑定时点击按钮 | 回仓库列表 | 核对通过：app.js:569-574 else 分支 |
| 5 | 切换 zh/en 语言 | 文案分别为「← 返回工作台」/ "← Back to Workspace" | 核对通过：zh/en organizations.json `back_to_workspace` key，`I18N.t` 前缀 organizations 命名空间已注册（i18n.js:36） |
| 6 | 组织列表/详情/创建/编辑/删除/切换组织 | 现有功能不回归 | 核对通过：仅 `renderOrganizations()` 头部容器加一个按钮，未触碰 `_loadOrgs/_showOrgDetail/_showCreateOrgDialog/_showEditOrgDialog/_deleteOrg/_switchOrg` 任何逻辑 |

> 说明：静态前端无 e2e 自动化覆盖，本环境无浏览器；以上按代码路径逐一核对走查（渲染入口、事件绑定、守卫分流、i18n key 注册），并结合后端全量 pytest 回归。

## 提交记录
- feat: #456 组织管理页面增加返回工作台按钮（3 文件：organizations.js + zh/en organizations.json）
