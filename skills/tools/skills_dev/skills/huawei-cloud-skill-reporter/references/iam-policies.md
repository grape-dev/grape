# IAM Policies — Huawei Cloud Skill Reporter v1

## 插件权限说明

Huawei Cloud Skill Reporter 是一个**监控插件**，本身不直接调用华为云 API。它的主要功能是：

1. 拦截 Skill 调用事件
2. 提取调用信息（名称、状态、参数）
3. 上报到配置的目标服务器

## 所需权限

### 对于插件本身

| 场景 | 所需权限 | 说明 |
|------|----------|------|
| 读取配置文件 | 文件系统读取权限 | 读取 `server_url.txt` |
| 上报数据 | 网络访问权限 | POST 请求到上报服务器 |
| 注册钩子 | Agent 平台权限 | Hermes/Opencode 插件注册 |

### 对于被监控的 Skill

Reporter 本身不改变 Skill 的权限。被监控的 Skill 需要什么权限，由各自的 SKILL.md 定义。

Reporter 只是**观察** Skill 的调用，不参与 Skill 的执行。

## 安全设计

### 不上报的敏感信息

- AK/SK 凭据
- 认证 token
- 用户密码
- 完整请求/响应体（仅截取前 500/1000 字符）

### Fire-and-Forget 模式

上报采用异步非阻塞模式：
- 不影响 Skill 执行性能
- 上报失败不影响 Skill 结果
- 仅记录 debug 日志

## 最小权限原则

### 插件开发者

- 只需文件系统写权限（安装插件时）
- 只需网络写权限（上报数据时）

### Skill 使用者

- Reporter 插件安装后自动生效
- 无需额外配置 IAM
- Skill 本身的权限由 Skill 定义

## 合规说明

- 符合华为云最小权限原则
- 不收集敏感认证信息
- 数据传输建议使用 HTTPS
- 支持私有化部署（可配置上报服务器地址）
