# Task: Issue #533 任务拆解

- [x] T1 根因定位：get_scene_detail 缺省 repo 解析失效（app_current_repo placeholder）
- [x] T2 服务端修复：显式 repo 优先 + 会话 token repo_key 回落 + repo_var 回落（#534 同根因合并）
- [x] T3 404 提示：报错信息附 --repo 变通指引
- [x] T4 回归测试：四类场景（无 --repo 命中 / 显式 --repo 优先 / 不存在 scene_id 404 / A2A 目标判定不失真）
- [x] T5 全量相关测试通过（test_openapi / test_scene_sync 的 4 个失败为基线已有，与本改动无关，已用 git stash 对照验证）
- [ ] T6 自审查 + 敏感增量扫描 + 提交 + PR（base=weekly-08-mys）
