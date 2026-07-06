---
name: security-scan
version: 1.0.0
description: 安全漏洞扫描与修复建议
triggers:
  - event_type: push
  - event_type: pull_request_opened
tools:
  - git
---

# 安全扫描

## 场景

代码提交或 PR 创建时，对变更内容进行安全扫描，识别漏洞、密钥泄露和不安全模式，防止安全问题进入代码库。

## 知识

### 扫描维度

#### 1. 密钥与凭据泄露

常见泄露模式：
- AWS Access Key：`AKIA[0-9A-Z]{16}`
- GitHub Token：`ghp_[0-9a-zA-Z]{36}`
- 私钥：`-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----`
- JWT：`eyJ[A-Za-z0-9-_]+\.eyJ[A-Za-z0-9-_]+`
- 通用密码模式：`password\s*[:=]\s*['"][^'"]+['"]`
- API Key 赋值：`(api_key|apikey|secret|token)\s*[:=]\s*['"][^'"]+['"]`

#### 2. 依赖漏洞

| 工具 | 语言 | 命令 |
|------|------|------|
| `govulncheck` | Go | `govulncheck ./...` |
| `npm audit` | Node.js | `npm audit --production` |
| `pip-audit` | Python | `pip-audit -r requirements.txt` |
| `trivy` | 容器 | `trivy fs .` |
| `snyk test` | 多语言 | `snyk test` |

#### 3. 不安全代码模式

- SQL 拼接：`"SELECT * FROM " + table + " WHERE ..."`
- 命令注入：`os.exec(cmd + user_input)`
- 路径遍历：`open(user_path)` 未校验
- SSRF：`requests.get(user_url)` 未限制
- 反序列化：`pickle.loads(user_data)`
- 不安全随机：`math.random()` 用于安全场景
- 宽松 CORS：`Access-Control-Allow-Origin: *`

#### 4. IaC 安全

- Kubernetes：特权容器、hostPath 挂载、未限制 capabilities
- Terraform：公开 S3 bucket、安全组全开放、未加密 EBS
- Dockerfile：以 root 运行、使用 latest tag、暴露敏感端口

### 严重程度分级

| 级别 | CVSS 范围 | 处理要求 |
|------|----------|---------|
| Critical | 9.0 - 10.0 | 阻止合并，立即修复 |
| High | 7.0 - 8.9 | 阻止合并，24h 内修复 |
| Medium | 4.0 - 6.9 | 警告，下个迭代修复 |
| Low | 0.1 - 3.9 | 记录，择时修复 |

## 步骤

1. 获取变更文件列表和 diff
2. 扫描 diff 中的密钥/凭据泄露模式
3. 检查依赖文件（go.sum/package-lock.json/requirements.txt）是否有已知漏洞
4. 扫描变更代码中的不安全模式
5. 如有 IaC 变更，检查 IaC 安全配置
6. 按严重程度汇总发现
7. 对每个发现给出修复建议和参考链接

## 判断标准

- 无 Critical/High 级别密钥泄露
- 无 Critical 级别依赖漏洞
- 不安全代码模式均已标记

## 输出规范

```markdown
## 安全扫描报告

**结论**: 🚫 阻止合并 / ⚠️ 存在风险 / ✅ 安全

### 发现列表

| 级别 | 类型 | 文件 | 行号 | 描述 | 修复建议 |
|------|------|------|------|------|---------|
| 🔴 Critical | 密钥泄露 | config.go | 15 | AWS Key 硬编码 | 使用环境变量 |

### 依赖漏洞
| 包 | 当前版本 | 漏洞ID | 严重程度 | 修复版本 |
|----|---------|--------|---------|---------|

### 统计
- 🔴 Critical: X
- 🟠 High: X
- 🟡 Medium: X
- 🔵 Low: X
```
