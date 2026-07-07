---
name: incident-triage
version: 1.0.0
description: 生产事件分诊与定级方法论
triggers:
  - event_type: deploy_failure
  - event_type: build_failure
tools:
  - kubectl
  - git
---

# 事件分诊

## 场景

生产环境发生部署失败、构建失败或运行时异常时，需要快速分诊：判断影响范围、确定严重等级、分配处理优先级。

## 知识

### 严重等级定义

| 等级 | 代号 | 定义 | 响应时间 | 示例 |
|------|------|------|---------|------|
| P0 | SEV-0 | 全站不可用，影响所有用户 | 立即 | 数据库宕机、CDN 故障 |
| P1 | SEV-1 | 核心功能不可用，影响大量用户 | 15 分钟 | 支付失败、登录失败 |
| P2 | SEV-2 | 部分功能降级，影响部分用户 | 1 小时 | 搜索慢、图片加载失败 |
| P3 | SEV-3 | 非核心功能异常，影响少数用户 | 4 小时 | 通知延迟、导出失败 |
| P4 | SEV-4 | 体验问题，不影响功能 | 1 工作日 | 样式错位、文案错误 |

### 分诊决策树

```
事件发生
  ├─ 是否影响核心业务流程？
  │   ├─ 是 → 影响用户比例 > 50%？→ 是 → P0 / 否 → P1
  │   └─ 否 → 是否有替代路径？
  │       ├─ 是 → 影响用户比例 > 30%？→ 是 → P2 / 否 → P3
  │       └─ 否 → P2
  └─ 是否有数据丢失/不一致风险？
      └─ 是 → 升级为 P0
```

### 快速诊断命令

```bash
# 检查 Pod 状态
kubectl get pods -A | grep -v Running

# 检查最近事件
kubectl get events -A --sort-by='.lastTimestamp' | tail -20

# 检查资源使用
kubectl top pods -A --sort-by=memory | head -20

# 检查最近部署
kubectl rollout history deployment/<name>

# 检查日志错误
kubectl logs <pod> --tail=100 | grep -iE 'error|fatal|panic|exception'
```

### 通知矩阵

| 等级 | 通知渠道 | 通知对象 |
|------|---------|---------|
| P0 | 电话 + PagerDuty + Slack #incident | On-call + Tech Lead + Manager |
| P1 | PagerDuty + Slack #incident | On-call + Tech Lead |
| P2 | Slack #incident | On-call |
| P3 | Slack #alerts | On-call |
| P4 | Jira Ticket | Auto-assign |

## 步骤

1. 收集事件基本信息（时间、服务、错误信息）
2. 判断影响范围：哪些服务受影响、多少用户受影响
3. 沿决策树确定严重等级
4. 检查是否为已知问题（近期同类事件、重复告警）
5. 确定处理人/团队
6. 生成分诊报告并按通知矩阵通知
7. 如果是 P0/P1，建议启动 Incident Response 流程

## 判断标准

- 严重等级判定有依据（非主观猜测）
- 影响范围已量化（服务数、用户比例）
- 处理人已明确
- 通知已发出

## 输出规范

```markdown
## 事件分诊报告

**事件ID**: INC-XXXX
**严重等级**: P0/P1/P2/P3/P4
**影响范围**: X 个服务，约 X% 用户
**处理人**: @someone
**状态**: 分诊完成 / 需升级

### 事件描述
[简要描述发生了什么]

### 影响分析
[具体受影响的服务和用户群]

### 初步判断
[基于日志/指标的初步根因判断]

### 建议行动
[下一步处理建议]
```

## 参考

需要调用 GitCode API 时，读取当前目录下的 `references/gitcode-api-reference.md`。
