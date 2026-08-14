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

使用 `hcloud` CLI 部署到华为云。**部署前先确认 hcloud 可用，缺失则自动安装**：

```bash
# 3.0 确保 hcloud 已安装（worker 无 hcloud 时自动动态安装）
if ! command -v hcloud >/dev/null 2>&1; then
  echo "hcloud not installed, trying to install..."
  # 步骤 1：查询华为云官方 CLI 文档页，提取当前真实的下载地址
  #   （官方下载地址会随版本/文档更新变化，禁止臆造 URL）
  DOC_URL="https://support.huaweicloud.com/cli/index.html"
  DL_URL=""
  for cand in \
    "$(curl -fsSL --max-time 20 "$DOC_URL" 2>/dev/null | grep -oE 'https://[^"'"'"' ]*(hcloud|cli)[^"'"'"' ]*\.(tar\.gz|zip)' | head -1)" \
    "$(curl -fsSL --max-time 20 "https://support.huaweicloud.com/cli/cli_install.html" 2>/dev/null | grep -oE 'https://[^"'"'"' ]*\.(tar\.gz|zip)' | head -1)"; do
    [ -n "$cand" ] && DL_URL="$cand" && break
  done
  if [ -n "$DL_URL" ]; then
    # 步骤 2：下载并安装（自动适配 linux 架构）
    curl -fsSL --max-time 180 -o /tmp/hcloud-install.bin "$DL_URL"
    mkdir -p /tmp/hcloud-extract && tar -xzf /tmp/hcloud-install.bin -C /tmp/hcloud-extract 2>/dev/null \
      || unzip -o /tmp/hcloud-install.bin -d /tmp/hcloud-extract >/dev/null 2>&1 \
      || chmod +x /tmp/hcloud-install.bin
    BIN=$(find /tmp/hcloud-extract -type f -name 'hcloud' -o -type f -name 'hcloud*' 2>/dev/null | head -1)
    [ -n "$BIN" ] && install -m 0755 "$BIN" /usr/local/bin/hcloud
  fi
  # 步骤 3：兜底——若文档解析失败，按华为云 CLI 官方文档提示的渠道安装
  if ! command -v hcloud >/dev/null 2>&1; then
    echo "WARN: 自动安装 hcloud 失败，请按官方文档 https://support.huaweicloud.com/cli/index.html 手动安装，或联系管理员在 worker 预装 hcloud"
  fi
fi
hcloud version || echo "hcloud still not installed — deployment cannot proceed, report clearly to user"
```

> 硬约束：**禁止臆造 hcloud 下载 URL**。官方下载地址以文档页实时内容为准；解析失败时如实报告，
> 不得用猜测的链接代替（防供应链风险）。

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
