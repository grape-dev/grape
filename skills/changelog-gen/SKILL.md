---
name: changelog-gen
version: 1.0.0
description: 自动生成变更日志
triggers:
  - event_type: push
  - event_type: release_created
tools:
  - git
---

# 变更日志生成

## 场景

代码推送或发布创建时，自动分析变更内容并生成结构化的变更日志（CHANGELOG），保持项目变更历史的可追溯性。

## 知识

### 变更分类

| 类别 | 前缀 | 说明 | 示例 |
|------|------|------|------|
| 新功能 | `feat` | 新增功能或行为 | 新增用户注册接口 |
| 修复 | `fix` | 修复 Bug | 修复登录超时问题 |
| 破坏性变更 | `feat!` / `fix!` | 不兼容的 API 变更 | 重构认证接口签名 |
| 性能 | `perf` | 性能优化 | 优化列表查询性能 10x |
| 重构 | `refactor` | 不改变行为的代码重构 | 提取公共方法 |
| 文档 | `docs` | 文档变更 | 更新 API 文档 |
| 测试 | `test` | 新增或修改测试 | 补充边界条件测试 |
| 构建 | `build` | 构建系统或依赖变更 | 升级 Go 到 1.22 |
| CI | `ci` | CI 配置变更 | 添加 lint 步骤 |
| 杂项 | `chore` | 其他不涉及 src/test 的变更 | 更新 .gitignore |

### Commit 消息规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>[!]: <description>

[body]

[BREAKING CHANGE: <description>]
```

### CHANGELOG 格式

遵循 [Keep a Changelog](https://keepachangelog.com/)：

```markdown
# Changelog

## [Unreleased]

## [1.2.0] - 2024-01-15

### Added
- 新增用户注册接口 (#123)

### Changed
- 优化列表查询性能 (#124)

### Fixed
- 修复登录超时问题 (#125)

### Breaking
- 重构认证接口签名，旧版客户端需升级 (#126)
```

## 步骤

1. 获取自上次发布以来的 commit 列表：`git log <last-tag>..HEAD`
2. 获取关联的 Merge Request / Pull Request 信息
3. 按 Conventional Commits 规范分类每个 commit
4. 对不符合规范的 commit，尝试从变更内容推断类别
5. 合并同类变更，去重
6. 按版本和日期组织生成 CHANGELOG 片段
7. 输出变更日志（追加到 CHANGELOG.md 或作为评论输出）

## 判断标准

- 所有 commit 均已分类
- 破坏性变更已明确标注
- 无遗漏的重要变更

## 输出规范

```markdown
## [版本号] - 日期

### Added
- 变更描述 (#PR号)

### Changed
- 变更描述 (#PR号)

### Fixed
- 变更描述 (#PR号)

### Breaking
- 变更描述 (#PR号)

---
*自动生成 by Grape changelog-gen skill*
```

## 参考

需要调用 GitCode API 时，读取当前目录下的 `references/gitcode-api-reference.md`。
