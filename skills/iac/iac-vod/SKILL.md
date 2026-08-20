---
name: iac-vod
version: 1.1.0
description: |
  开发者声音分析（Voice of Developer）。收集开发者反馈，聚类分析共性诉求与痛点，
  提炼价值需求，自动创建 Issue 推进改进。
tools:
  - curl
  - git
requires:
tags:
  - feedback
  - analysis
  - community
---

## 概述

收集开发者多渠道反馈，经聚类分析和价值提炼后自动创建 Issue，推动产品改进。

## 前置条件

- 已安装 curl 和 git
- 已配置 `$BOT_GITCODE_TOKEN` 环境变量
- 仓库已存在 Gitee/GitCode Issue 模块

## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 `$BOT_GITCODE_TOKEN`，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。

## 参考文件

- 本地参考：`references/gitcode-api-reference.md`
- 本地参考：`references/labeling-guide.md`
- 本地参考：`references/comment-guide.md`

## 工作流

### 反馈收集
- 从 Issue 评论、PR 讨论、社区渠道多维收集开发者声音
- 定期扫描仓库活跃讨论，标记潜在共性需求

### 聚类分析
- 将收集到的反馈按主题聚类（分类：bug/feature/doc/ux/performance）
- 统计同类反馈频次，标记高频/高影响项

### 价值提炼
- 对每个聚类生成简要分析：现象 → 影响面 → 建议方案
- 标注优先等级（P0/P1/P2），关联已有 Issue/PR

### Issue 创建
- 对确认有价值的分析结果自动创建 Issue
- Issue 标题格式：`[VOD] <聚类标签>: <问题简述>`
- Issue 正文包含完整分析链路

### 原始职责内化
- 使用 Socratic 澄清方法收集开发者模糊需求：通过递进式提问逐步明确反馈的真实诉求
- 按回报价值判定标准区分"需回复"与"可静默"：根据反馈的完整性、可复现性和影响面积决定是否回应
- 创建 Issue 前对比已有 Issue 列表，按关键词搜索去重，避免重复报告

## 硬规则

| 行为 | 态度 |
|------|-------|
| 分析结果必须包含频率统计 | ✅ **必须做** |
| 创建 Issue 前须确认无重复 | ✅ **必须做** |
| Issue 标签须包含 `vod` | ✅ **必须做** |
| 单条反馈价值不足时标记存档不创建 Issue | ✅ **根据判断** |
| 修改非 VOD 相关 Issue | ❌ **禁止** |
| 回复 Token/密码等敏感信息 | ❌ **禁止** |
| 直接关闭/合并 Issue | ❌ **禁止** |