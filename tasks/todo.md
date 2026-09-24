# TODO: Issue #242 跨仓库需求开发

## Phase 1: 数据模型 + API
- [x] DB: devops_pipeline_scene 新增 linked_repos TEXT DEFAULT '[]'
- [x] pipeline_config.py: _query_scenes_from_db / _query_scene_by_id / create_custom_scene / update_custom_scene 支持 linked_repos
- [x] scenes.py: CreateSceneRequest / UpdateSceneRequest / AdvancedOptionsRequest 增加 linked_repos；list_scenes / create_scene / update_scene 传递

## Phase 2: A2A + Runtime
- [x] event_pipeline.py: _inject_scene_env 注入 GRAPE_LINKED_REPOS 环境变量
- [x] repogit.py: ensure_repo_base_multi 批量初始化 + 多 worktree 支持
- [x] agent.py: coding agent 初始化时遍历 linked_repos 建 worktree

## Phase 3: 前端 + 测试
- [x] dashboard.js: 场景详情高级选项区增加关联仓库配置 UI
- [x] tests/test_linked_repos.py: 场景 CRUD + linked_repos 解析测试
