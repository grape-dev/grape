---
name: deploy-guard
version: 1.0.0
description: 部署前安全与合规检查守卫
triggers:
  - event_type: deploy_start
  - event_type: build_success
tools:
  - kubectl
  - docker
---

# 部署安全守卫

## 场景

在部署启动前或构建成功后即将部署时，执行安全与合规检查，阻止不符合标准的部署进入生产环境。

## 知识

### 检查清单

#### 镜像安全

- [ ] 基础镜像是否为官方/可信镜像
- [ ] 镜像是否以非 root 用户运行
- [ ] 镜像是否经过漏洞扫描（无 Critical/High 级别 CVE）
- [ ] 镜像 tag 是否为固定版本（非 `latest`）
- [ ] 镜像大小是否合理（< 500MB 建议）

#### 配置安全

- [ ] 无硬编码密钥/Token（检查环境变量、ConfigMap、Secret 引用）
- [ ] 资源限制已设置（CPU/Memory requests 和 limits）
- [ ] 健康检查已配置（liveness + readiness probe）
- [ ] 不使用 `hostPort` 或 `hostNetwork`
- [ ] ServiceAccount 遵循最小权限原则

#### 合规检查

- [ ] 变更经过 Code Review 审批
- [ ] 所有测试通过（单元 + 集成）
- [ ] 变更日志已更新
- [ ] 数据库迁移为向后兼容（无破坏性变更）
- [ ] 回滚方案已确认

#### 流量安全

- [ ] 入口流量走 TLS
- [ ] 内部服务间通信走 mTLS 或 Service Mesh
- [ ] 不对外暴露调试端口和管理端点

### 部署策略对照

| 策略 | 适用场景 | 回滚方式 |
|------|---------|---------|
| Rolling Update | 日常发布，低风险 | `kubectl rollout undo` |
| Blue-Green | 需要零停机验证 | 切换 Service selector |
| Canary | 高风险变更，需渐进验证 | 调整流量比例回滚 |
| Feature Flag | 需要精确控制受众 | 关闭 Feature Flag |

## 步骤

1. 获取待部署的镜像/配置信息
2. 执行镜像安全检查（基础镜像、用户、漏洞）
3. 执行配置安全检查（资源限制、健康检查、权限）
4. 执行合规检查（审批、测试、迁移兼容性）
5. 汇总检查结果，计算风险评分
6. 风险评分低于阈值 → 阻止部署并给出修复项
7. 风险评分达标 → 放行并记录检查报告

## 判断标准

- Critical 级别检查项全部通过
- High 级别检查项通过率 ≥ 90%
- 无硬编码密钥
- 资源限制已设置

## 输出规范

```markdown
## 部署安全检查报告

**结果**: 🚫 阻止部署 / ✅ 允许部署
**风险评分**: X/100

### 检查结果

| 级别 | 检查项 | 状态 | 详情 |
|------|--------|------|------|
| Critical | 非 root 运行 | ❌ | 当前以 root 运行 |

### 阻止项（必须修复）
[列出所有未通过的 Critical 检查项及修复方法]

### 建议项（推荐修复）
[列出所有未通过的 High/Low 检查项及修复方法]
```

## 参考

需要调用 GitCode API 时，读取当前目录下的 `references/gitcode-api-reference.md`。
