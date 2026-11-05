# Spec: Issue #900 — git-repo milestones 端点分页与过滤补齐

> 目标分支: weekly-mys | 前序：Issue Agent Spec 评论（2026-11-04，评级 ready）
> 本版：QA 复审（PR #905，事件 seq=47676）3 项 Spec 问题修复版（R2 实现）

## Objective
- 为 `GET /api/git-repo/{owner}/{repo}/milestones` 补齐 `page`/`per_page`（可选 `title`）查询参数并透传上游 VCS，消除默认小分页导致的列表截断。
- 契约登记后 CLI `grape git-repo milestones` 经 `grape refresh` 自动呈现 `--page`/`--per_page`。

## Scope
- In：milestones 路由参数解析/校验/上游透传；契约（OpenAPI Query 参数）登记 + 契约断言测试；新增测试。
- Out：CLI 客户端改动、milestone 创建命令（#668/#747）、runbook 资产（grape-skills 仓）、其他 list 命令（#730/#843）。

## Acceptance Criteria
- `--per_page 100` 可返回全部里程碑（含 weekly-07~10）。
- **per_page > 100 时钳制到 100 透传，不返回 422**（QA 复审 (a) 对齐：钳制口径）。
- **page/per_page 未提供时不发送上游**（URL 与 #900 之前一致，完全向后兼容；QA 复审 (c)）。
- OpenAPI schema 中 gitrepo_milestones 含 page/per_page/title Query 参数，并有测试断言（QA 复审 (b)）。
- 非法值（page/per_page < 1）仍 422。
- 现有测试全过 + 新增分页/过滤/钳制/默认兼容/契约测试覆盖。

## QA 复审遗留开放项
- 「per_page=100 端到端返回全部里程碑」为部署环境验收项（上游真实数据，mock 无法覆盖），合并后由部署侧 `grape refresh` + 实测复核。
