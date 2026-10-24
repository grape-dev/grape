# Spec: Issue #461 — 项目场景详情页互联网标识小图标（登记场景访问地址）

> 来源：Issue Agent Spec（发布于 Issue #461 评论）| 开发分支: feat/issue-461 | 基线: weekly-08-ljt

## 目标
在项目场景的具体场景详情页中，于「复制场景」按钮（Issue #402）**前面**新增互联网标识小图标（地球仪 🌐），点击可登记/编辑该构建场景对应的访问地址（部署地址），交互与数据逻辑与需求中心「登记部署地址」（commits 4e9944a/dbd97f8/83b5589）保持一致，作用域为**具体场景级**（每个场景独立一条记录）。

## 范围
### In Scope
- 前端 dashboard.js 场景详情页头部：复制场景按钮前插入地球仪图标入口
- 点击/右击/长按进入行内编辑，保存当前场景访问地址（校验/XSS 防护与需求中心一致）
- 后端场景级存储：`devops_pipeline_scene.deploy_url` 列（按 scene_id + repo_key 维度）
- 新增 `GET/PUT /api/scenes/{scene_id}/deploy-url` 端点（复用 `_validate_deploy_url` 校验与 `check_repo_access` 归属校验）
- i18n 中/英文案
- help/api/06-scenes.md 文档 + OpenAPI 契约登记

### Out of Scope
- 需求中心（issues.js / managed_repos.deploy_url）已有功能不改动
- 全局/跨场景访问地址聚合视图
- 访问地址连通性探测

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 存储位置 | devops_pipeline_scene 新列 deploy_url（VARCHAR 512） | 场景级天然按 (scene_id, repo_key) 唯一，与 advanced_options 同表迁移模式一致（_ensure_column 幂等补列） |
| 交互形态 | 复用需求中心三态（未登记灰 globe / 已登记绿 globe 链接 / 编辑输入框） | 保持一致性，降低学习成本 |
| XSS 防护 | 沿用 #446：服务端 http(s) 前缀 + ≤512 校验；前端 _skillEsc 转义 href/title/value | 与需求中心完全一致 |
| 图标位置 | 复制场景按钮之前 | Issue 明确要求 |

## 验收标准
- [ ] 复制场景按钮前出现地球仪图标
- [ ] 点击图标可登记/编辑当前场景访问地址
- [ ] 校验/存储/展示逻辑与需求中心一致
- [ ] 场景级独立（不同场景互不影响，需求级数据不受影响）
- [ ] i18n 中/英齐全
- [ ] 复制场景/删除/高级选项等既有功能无回归
- [ ] XSS 防护（转义 + http(s) 前缀 + 长度限制）
- [ ] 新增测试覆盖 + 现有测试通过
