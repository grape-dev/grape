---
name: build-debug
version: 1.0.0
description: 构建失败诊断方法论与常见模式
tools:
  - git
  - docker
---

# 构建失败诊断

## 场景

CI/CD 流水线构建失败时，需要快速定位根因并给出修复方案，减少构建中断时间。

## 知识

### 诊断方法论：从快到慢

1. **看日志尾部**：大多数构建错误的关键信息在最后 50 行
2. **看退出码**：非零退出码对应具体错误类型
3. **看变更范围**：`git diff HEAD~1` 找到最近变更，缩小排查范围
4. **看依赖状态**：依赖下载失败、版本冲突是常见原因
5. **看环境差异**：本地能构建但 CI 失败，通常是环境不一致

### 常见构建失败模式

| 模式 | 关键词 | 根因 | 修复 |
|------|--------|------|------|
| 编译错误 | `cannot find`, `undefined`, `not declared` | 缺少依赖或导入 | 检查 import/依赖声明 |
| 依赖下载失败 | `timeout`, `404`, `ECONNREFUSED` | 网络或仓库问题 | 重试或换镜像源 |
| 版本冲突 | `version conflict`, `incompatible` | 依赖版本不兼容 | 锁定版本或升级 |
| 测试失败 | `FAIL`, `AssertionError`, `expected` | 代码逻辑错误 | 修复测试或被测代码 |
| 磁盘不足 | `no space left`, `ENOSPC` | CI runner 磁盘满 | 清理缓存或扩容 |
| OOM | `OOM`, `out of memory`, `killed` | 内存不足 | 优化内存使用或调大限制 |
| Docker 构建失败 | `COPY failed`, `command not found` | Dockerfile 指令错误 | 修正 Dockerfile |
| 权限问题 | `permission denied`, `EACCES` | 文件/目录权限 | 检查文件权限和用户 |

### 语言/框架特定模式

- **Go**：`go.mod` 需 tidy、CGO 依赖缺失、交叉编译 CGO_ENABLED=0
- **Node.js**：`node_modules` 不一致需删锁重装、native 模块编译需 build-essential
- **Python**：pip 版本冲突需用 venv、C 扩展编译需 dev 包
- **Maven/Gradle**：仓库不可达需换 mirror、SNAPSHOT 版本不稳定

## 步骤

1. 提取构建日志中的错误信息（关注 stderr 和最后 50 行）
2. 匹配已知失败模式，判断最可能的根因
3. 检查最近一次代码变更是否与错误相关
4. 按优先级给出修复方案（可自动修复的优先）
5. 如果可以自动修复，执行修复并说明修改内容
6. 如果无法自动修复，给出手动修复步骤

## 判断标准

- 根因定位到具体文件/依赖/配置
- 修复方案可直接执行或步骤明确
- 不遗漏多因并发的情况（如同时有编译错误和依赖问题）

## 输出规范

```markdown
## 构建失败诊断

**根因**: [一句话描述]
**置信度**: 高/中/低
**可自动修复**: 是/否

### 错误分析
[从日志中提取的关键错误信息及解读]

### 修复方案
[具体修复步骤，可自动修复时标注 `AUTO-FIX`]

### 预防建议
[避免同类问题再次发生的建议]
```

## 参考

需要调用 GitCode API 时，读取当前目录下的 `references/gitcode-api-reference.md`。
