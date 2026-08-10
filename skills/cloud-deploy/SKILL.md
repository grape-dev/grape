---
name: cloud-deploy
version: 1.0.0
description: |
  Huawei Cloud deployment skill. Listens for PR merge events on master/main,
  automatically determines build method from project files,
  deploys to Huawei Cloud using hcloud CLI, and verifies deployment.
  NOT for: non-merge PR events, pushes to non-master branches.
tools:
  - curl
  - git
---
## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 \\，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。



# Cloud Deploy

## 入口判定

- 仅当 `pull_request_merged` 事件且分支为 `master` 或 `main` 时继续
- 其他事件类型直接输出 `SKIP` 并返回

## 流程

### 1. 理解项目结构

读取仓库根目录的文件，自行判断构建方式和部署方式：

| 文件标记 | 推断 |
|----------|------|
| `package.json` | Node.js 项目，可能用 `npm` / `yarn` |
| `requirements.txt` / `pyproject.toml` / `Pipfile` | Python 项目，可能用 `pip` / `poetry` |
| `go.mod` | Go 项目，`go build` |
| `pom.xml` / `build.gradle` | Java 项目，`mvn` / `gradle` |
| `Dockerfile` | 容器化项目，构建镜像并推送 |
| `Makefile` | 自定义构建流程（读取 Makefile 中的目标） |
| `README.md` | 查找安装、构建、部署说明 |

### 2. 构建

根据上一步的判断，执行对应构建命令。查看项目文档获取参数。

```bash
# 示例（根据实际项目类型选择）
git checkout master && git pull
cd /path/to/repo
# 按需执行：npm install && npm run build
# 或：pip install -r requirements.txt
# 或：go build ./...
```

### 3. 部署

使用 `hcloud` CLI 部署到华为云。确认 hcloud 可用：

```bash
hcloud version || echo "hcloud not installed"
```

通过环境变量 `HUAWEICLOUD_SDK_AK` 和 `HUAWEICLOUD_SDK_SK` 获取华为云凭证。根据项目类型选择部署目标并执行部署。

### 4. 验证

部署完成后验证结果：

- 检查目标服务的运行状态
- 通过 API 健康检查或 CLI 查询确认新版本生效
- 如果部署失败，输出错误信息并建议回滚

## 环境变量

| 变量 | 来源 | 说明 |
|------|------|------|
| `HUAWEICLOUD_SDK_AK` | account 配置 | 华为云 Access Key |
| `HUAWEICLOUD_SDK_SK` | account 配置 | 华为云 Secret Key |
| `BOT_GITCODE_TOKEN` | account 配置 | GitCode API Token |

## 硬约束

- ❌ 禁止直接修改仓库代码
- ❌ 禁止删除仓库分支
- ✅ 仅执行当前 PR 合并后的部署流程
- ✅ 部署前先理解项目结构
- ✅ 部署失败时输出错误信息，不自动回滚
