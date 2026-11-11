---
name: grape-dev-workflow
description: "Grape 项目开发工作流：创建分支、提交代码、提 MR 到 weekly-XX。使用方在 /opt/grape 目录时触发。"
---

# Grape 开发工作流规范

## 适用场景
在 Grape 项目中修改代码（bug 修复、功能开发、文档等）时使用。

## 前置检查
执行任何修改前，先确保 Git user 已配置：

```bash
GIT_NAME=$(git config user.name)
GIT_EMAIL=$(git config user.email)
if [ -z "$GIT_NAME" ] || [ -z "$GIT_EMAIL" ]; then
  git config user.name "Xiahou Yi"
  git config user.email "2830976536@qq.com"
fi
if [ -z "$GITCODE_TOKEN" ]; then
  echo "请先设置 GITCODE_TOKEN 环境变量"
  exit 1
fi
```

## 分支规则
- 从当期 `weekly-XX` 拉取特性分支（先确认当前周分支号）
- 命名格式：`<前缀>/<简短描述>`（小写 kebab-case）
- 前缀：`feat/`、`fix/`、`docs/`、`refactor/`、`chore/`、`hotfix/`

## 提交流程
1. `git checkout weekly-XX && git pull origin weekly-XX`
2. `git checkout -b <前缀>/<描述>`
3. 开发修改
4. `git add <文件列表>`
5. `git commit -s -m "<type>(<scope>): <subject>

Closes #<issue>"`
6. `python -m pytest tests/ -v`
7. `python -m pytest tests/ --cov`（行覆盖率 ≥ 70%）
8. `git push origin <分支名>`
9. `create_mr.sh <分支名> weekly-XX "<标题>" "<描述>"`

## 提交信息规范
- 英文，Conventional Commits 格式
- `git commit -s` 自动添加 DCO（`Signed-off-by: Xiahou Yi <2830976536@qq.com>`）
- 关联 Issue：`Closes #<id>`

## MR 提交流程
```bash
create_mr.sh <source> <target> "<title>" "<description>"
```

示例：
```bash
create_mr.sh fix/login-page-flash weekly-01 "fix(ui): prevent login page flash on refresh" "Closes #9"
```