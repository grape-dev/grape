---
name: cloud-deploy
version: 1.0.0
description: |
  Huawei Cloud deployment skill. Supports two entry points:
  1. A2A direct deployment (a2a_event) — triggered by Issue/Coding Agent handing off a deploy task, no PR merge required.
  2. PR merge on master/main — full build→deploy→verify pipeline.
  Automatically determines build method from project files,
  deploys to Huawei Cloud using hcloud CLI, and verifies deployment.
  NOT for: non-merge, non-A2A PR events; pushes to non-master branches.
tools:
  - curl
  - git
---
## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用环境变量 `$BOT_GITCODE_TOKEN`，严禁从数据库/configs/日志/历史中寻找其他 token。
2. GitCode API 调用格式（必须用 Bearer）：
   ```bash
   curl -s -H "Authorization: Bearer $BOT_GITCODE_TOKEN" "https://gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER"
   ```
   ⚠️ 必须用 `Authorization: Bearer ***` `Authorization: token ***` 会被 GitCode API 返回 401。
3. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
4. API 返回 403/无权限时：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
5. token 不得写入任何文件/日志/评论。
6. AK/SK 必须从 Grape 控制台配置获取，严禁从数据库/configs/日志/历史/其他文件中寻找或硬编码凭证。
7. 如 Grape 控制台未配置 AK/SK，立即停止操作并回复用户「请在 Grape 控制台中配置华为云 AK/SK 后重试」，严禁尝试其他来源或绕行手段。
8. ⚠️ **严禁 `hcloud configure set` 改 default profile**：必须用 `--cli-profile` 创建 task 专属 profile，详见 §3.1。



# Cloud Deploy

## 入口判定

支持以下入口，按优先级判定。**首先根据 A2A context / Issue 内容判断操作类型（部署 vs 释放），再走对应流程。**

### 操作类型识别

读取 A2A context 或 Issue 正文，按关键词判定：
- **资源释放**：包含「释放/销毁/删除/回收/destroy/release/teardown/cleanup/清退」等关键词
- **部署**：其他情况（创建/部署/上线/更新/deploy 等）

### 入口 1：A2A 直接触发（`a2a_event` 类型，不依赖 PR merge）

当 Issue Agent 或 Coding Agent 通过 A2A 交办任务时直接执行，不要求 PR 已合并。
从 A2A context 中获取需求摘要；从 payload 中获取 `issue_number`、`pr_number`（可选）。

**部署场景**：
- 代码来源：有 `pr_number` 时拉取对应 PR 分支代码；无 `pr_number` 时拉取仓库默认分支最新代码。
- 走下方「流程 §1~§4」完整构建→部署→验证流程。

**资源释放场景**：
- 跳过构建/部署，直接走「资源释放流程」（见下方专节）。
- 从 Issue 正文/评论中提取要释放的资源信息（ECS 名称/IP、RDS 实例名、OBS 桶名等）。
- 如果 Issue 中没有明确的资源信息，先用 GitCode API 查 Issue 评论补充；仍无法确定时回帖追问用户。

### 入口 2：PR 合并触发（`pull_request_merged` 事件）

分支为 `master` 或 `main` 时继续，走完整构建→部署流程。

### 入口 3：其他事件类型

直接输出 `SKIP` 并返回。

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
| `app.py` / `main.py` | Python Flask 项目 |
| `static/` / `templates/` | Flask 模板项目 |

### 2. 获取代码并构建

根据入口判定结果获取代码，再按项目类型执行构建命令。

```bash
# 代码获取（根据入口类型选择）：
# A2A 直接触发 + 有 pr_number：拉取 PR 分支代码
#   git clone <repo_url> && git checkout <pr_branch>
# A2A 直接触发 + 无 pr_number：拉取仓库默认分支最新代码
#   git clone <repo_url>
# PR 合并触发：拉取 master/main 分支
#   git checkout master && git pull
cd /path/to/repo
# 按需执行：npm install && npm run build
# 或：pip install -r requirements.txt
# 或：go build ./...
```

### 3. 部署

使用 `hcloud`（KooCLI）CLI 部署到华为云。**部署前先确认 hcloud 可用，缺失则自动安装**（以下为 2026-08-14 实测验证过的流程）：

```bash
# 3.0 确保 hcloud 已安装（worker 无 hcloud 时自动动态安装）
#     官方下载：KooCLI = 华为云 CLI，镜像在 OBS（文档 qs-hcli/hcli_02_003.html）
if ! command -v hcloud >/dev/null 2>&1; then
  echo "hcloud not installed, installing KooCLI..."
  # 架构判定（worker 有 amd64 和 arm64 两种）
  case "$(uname -m)" in
    x86_64|amd64) APP_ARCH="amd64" ;;
    aarch64|arm64) APP_ARCH="arm64" ;;
    *) echo "unsupported arch: $(uname -m)"; exit 1 ;;
  esac
  DL_URL="https://cn-north-4-hdn-koocli.obs.cn-north-4.myhuaweicloud.com/cli/latest/huaweicloud-cli-linux-${APP_ARCH}.tar.gz"
  curl -fsSL --max-time 300 -o /tmp/hcloud.tar.gz "$DL_URL" || { echo "DOWNLOAD FAIL: $DL_URL"; exit 1; }
  mkdir -p /tmp/hcloud-x && tar -xzf /tmp/hcloud.tar.gz -C /tmp/hcloud-x
  find /tmp/hcloud-x -type f -name hcloud -exec install -m 0755 {} /usr/local/bin/hcloud \;
  chmod a+rx /usr/local/bin/hcloud
  # 首次运行必须同意隐私声明（否则卡交互提示）
  echo y | hcloud configure set --cli-agree-privacy-statement=true
fi
hcloud version || echo "hcloud still not installed — deployment cannot proceed, report clearly to user"
```

> ⚠️ 硬约束（2026-08-14 实测）：
> 1. **worker 架构决定包名**：arm64 机器必须下 `huaweicloud-cli-linux-arm64.tar.gz`，amd64 下 `-amd64-`，下错会 `Exec format error`。
> 2. **首次运行必须 `echo y | hcloud configure set --cli-agree-privacy-statement=true`**，否则卡在隐私声明确认提示。
> 3. 下载可能慢（OBS arm64 曾 90s+），`--max-time` 给足 300s；文件完整性用 `gzip -t` 校验。

通过环境变量 `HUAWEICLOUD_SDK_AK` 和 `HUAWEICLOUD_SDK_SK` 获取华为云凭证。**部署目标：华为云 ECS（区域 cn-north-4，与 grape 服务器同区域）**，流程如下：

```bash
# 3.1 配置 hcloud 认证（用独立 profile 隔离，避免并发竞态）
#   ⚠️ 严禁用 `hcloud configure set` 改 default profile！
#   同一 worker 上可能并发多个 SRE task（分属不同华为云账号），
#   hcloud configure 写的是全局文件（~/.hcloud/configure/），并发覆盖会导致
#   A 账号的 task 用了 B 账号的 AK 创建资源（2026-08-25 seq#2493 实测事故）。
#   正确做法：用 --cli-profile 创建 task 专属 profile，所有 hcloud 命令带 --cli-profile 参数。
#   profile 名用 $$（进程 PID）保证唯一性，部署结束后清理。
HC_PROFILE="task-$$"
hcloud configure set --cli-profile="$HC_PROFILE" \
  --cli-access-key="$HUAWEICLOUD_SDK_AK" --cli-secret-key="$HUAWEICLOUD_SDK_SK" \
  --cli-region=cn-north-4 --cli-mode=AKSK

# 封装包装函数：所有 hcloud / obsutil 命令都通过它执行，自动带 --cli-profile
hc() { hcloud "$@" --cli-profile="$HC_PROFILE"; }
# obsutil 也用独立配置（-i/-k 直接传 AK/SK，不写全局 .obsutilconfig）
obs() { obsutil "$@" -i "$HUAWEICLOUD_SDK_AK" -k "$HUAWEICLOUD_SDK_SK" -e obs.cn-north-4.myhuaweicloud.com; }

# 部署结束后清理 profile（trap 确保异常退出也清理）
trap 'hcloud configure delete --cli-profile="$HC_PROFILE" 2>/dev/null || true' EXIT

# 3.2 ECS 幂等检查 + 创建（区域固定 cn-north-4；按量计费最小规格；开 22/80/5000 端口）
#   **幂等约束（2026-08-14 实测教训）：部署前必须先查是否已有本仓库的 ECS，
#   有则复用（直接部署到它），绝不重复新建——否则每跑一次多一台机器、多一份费用。**
#   查法：按名称前缀过滤（deploy-<repo> 或 <repo>-deploy），hc ECS ListServersDetails 返回里
#   过滤出匹配名称的 ACTIVE 实例，取第一个的浮动 IP 作为 $ECS_IP 复用，跳过下方创建。
#   ⚠️ 可用区约束（2026-08-20 实测教训）：创建 ECS 必须显式指定可用区
#   **cn-north-4a** 或 **cn-north-4b**，绝不能用 cn-north-4g（该 AZ 使用 DPDK/vhostuser
#   高性能网卡，不走标准内核网络栈，SSH 永远连不上）。
#   ⚠️ 规格选择（2026-08-20 实测教训）：不要硬编码 flavor（如 s6.small.1 在 postPaid 模式
#   下买不到），必须动态查询当前区域最小可用按量计费规格：
#     hc ECS ListFlavors --cli-region=cn-north-4 → 取规格列表
#     按 vcpu↑, ram↑ 排序
#     hc ECS ListFlavorSellPolicies --flavor_id=... 确认 sell_mode=postPaid + sell_status=available
#   ⚠️ EIP 创建（2026-08-20 实测教训）：必须在创建 ECS 时通过 --server.publicip.eip.*
#   参数**自动创建 EIP**，不要先建 ECS 再手动关联 EIP。自动创建的 EIP 由华为云 API
#   自动处理 VPC 网关 NAT 路由；手动关联可能因 VPC 缺少 Internet Gateway 或路由表
#   缺 0.0.0.0/0 而无法通信。
EXISTING_ECS=$(hc ECS ListServersDetails --cli-region=cn-north-4 2>/dev/null | grep -B5 -A20 "\"name\": \"deploy-${REPO_FULL_NAME//\//-}" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | tail -1)
if [ -n "$EXISTING_ECS" ]; then
  echo "复用已有 ECS: $EXISTING_ECS"
  ECS_IP="$EXISTING_ECS"
else
  # 新建：动态选取当前区域最小可用按量规格
  #
  # 实测 cn-north-4 的 s6.small.1 已废弃，硬编码会导致
  # Ecs.0019（flavor abandoned）/ Ecs.0707（product not found）。
  # 必须动态查询：
  #   1) ListFlavors → 获取规格列表（含 vCPU/内存/架构）
  #   2) ListFlavorSellPolicies → 确认 postPaid 可售
  #   3) 按 vCPU↑、RAM↑ 取第一个符合架构的
  # **必须带 keypair**（否则建完 SSH 连不上，Agent 会反复试错重建——15:38 事故）
  #   keypair 不存在则先创建并保存私钥；公钥来自 worker ~/.ssh/id_ed25519.pub
  if ! hc ECS NovaListKeypairs --cli-region=cn-north-4 2>/dev/null | grep -q "grape-deploy-kp"; then
    hc ECS NovaCreateKeypair --cli-region=cn-north-4 --name=grape-deploy-kp \
      --public_key="$(cat ~/.ssh/id_ed25519.pub)" 2>/dev/null || true
  fi
  # 动态选最小可用规格（2026-08-20 修复：不再硬编码 s6.small.1）
  # 流程：ListFlavors → 取规格列表 → 按架构/按需/最小排序
  ARCH=$(uname -m)
  MIN_FLAVOR=$(hc ECS ListFlavors --cli-region=cn-north-4 2>/dev/null | python3 -c "
  import json,sys
  arch='$ARCH'  # bash 展开
  arch_map={'x86_64':'x86','amd64':'x86','aarch64':'ARM','arm64':'ARM'}
  want=arch_map.get(arch.strip(),'x86')
  data=json.load(sys.stdin)
  flavors=data.get('flavors',[]) if isinstance(data,dict) else data
  candidates=[f for f in flavors
    if f.get('os_extra_specs',{}).get('ecs:architecture','')==want
    and f.get('is_public',True)]
  candidates.sort(key=lambda f:(f.get('vcpus',999),f.get('ram',99999)))
  print(candidates[0]['id'] if candidates else 's6.small.1')
  " 2>/dev/null)
  echo "Selected flavor: $MIN_FLAVOR"
  INSTANCE_NAME="deploy-${REPO_FULL_NAME//\//-}-$(date +%H%M%S)"
  hc ECS <CreateServer> --cli-region=cn-north-4 \
      --name "$INSTANCE_NAME" --flavor "$MIN_FLAVOR" --image "Ubuntu 22.04" \
    --charging-mode postPaid --security-group "default" \
    --keypair "grape-deploy-kp" \
    --user-data "#!/bin/bash
      mkdir -p /root/.ssh && echo '$(cat ~/.ssh/id_ed25519.pub)' >> /root/.ssh/authorized_keys" ...
  # 等待 ECS running，拿公网浮动 IP（hcloud ECS ListServersDetails 按名称查）
  ECS_IP=$(...按 INSTANCE_NAME 过滤出浮动 IP...)
fi
# 确保免密 SSH（复用场景补推/新建场景确认 keypair 已生效）
ssh-copy-id -o StrictHostKeyChecking=no root@"$ECS_IP" 2>/dev/null || true

# 3.3 部署代码到 ECS（实测流程，2026-08-14 验证）
#   a) 上传代码：scp 仓库文件到 /opt/<repo>
ssh -o StrictHostKeyChecking=no root@"$ECS_IP" "mkdir -p /opt/$REPO_NAME"
scp -o StrictHostKeyChecking=no -r "$WORKDIR"/* root@"$ECS_IP":/opt/$REPO_NAME/

#   b) 一次性完成：系统依赖 → venv → 依赖 → 启动（**单条 SSH 会话**，勿拆多条，防超时）
#      注意：把下方 __REPO_NAME__ 替换为实际仓库名；heredoc 内无变量展开，环境变量需在此处预置
ssh -o StrictHostKeyChecking=no root@"$ECS_IP" 'bash -s' << 'DEPLOY_EOF'
set -e
# DNS 修复（2026-08-20 实测教训）：Ubuntu 22.04 默认 systemd-resolved（127.0.0.53）
# 解析 GitCode 等外部域名可能失败（SERVFAIL），导致 git clone / pip install 超时。
# 部署前先设公共 DNS：
echo 'nameserver 8.8.8.8' > /etc/resolv.conf
echo 'nameserver 223.5.5.5' >> /etc/resolv.conf

cd /opt/__REPO_NAME__
# 镜像加速：所有下载资源优先使用国内镜像源。优先级：阿里云 > 华为云 > 官方源
#   apt: mirrors.aliyun.com
#   pip: mirrors.aliyun.com/pypi/simple
#   npm: registry.npmmirror.com
#   docker: swr.cn-north-4.myhuaweicloud.com（华为云 SWR）

# 系统依赖 + apt 镜像源
sed -i 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true
sed -i 's|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv python3.10-venv 2>/dev/null || apt-get install -y -qq python3 python3-pip python3-venv
# venv + 依赖（阿里云镜像优先，回退到华为云镜像，最后官方源重试 3 次）
rm -rf venv
python3 -m venv venv
venv/bin/pip install -q --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com || true
for i in 1 2 3; do
  venv/bin/pip install -q -r requirements.txt gunicorn \
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com && break
  echo "pip retry $i/3"
  sleep 5
done
# 启动（gunicorn 持久化，nohup + 端口 5000）
# ⚠️ 严禁 `pkill -f gunicorn` / `pkill -f app.py` 这类按命令行模糊匹配的清理方式：
# worker 上 opencode 进程的命令行包含完整 prompt（含 "gunicorn"/"app.py" 字样），
# pkill -f 会 SIGTERM 掉 agent 自身进程导致任务自杀（exit=-15），
# 见 seq#667 hello-flask2 Issue #3 实测。清理旧进程必须按端口精确操作：
ss -tlnp 2>/dev/null | grep ':5000' | grep -oP 'pid=\K[0-9]+' | sort -u | xargs -r kill 2>/dev/null || true
sleep 1
nohup venv/bin/gunicorn -w 2 -b 0.0.0.0:5000 app:app > /tmp/flask.log 2>&1 &
sleep 4
DEPLOY_EOF

# 3.4 验证（期望 HTTP 200）
curl -s -o /dev/null -w '%{http_code}' "http://$ECS_IP:5000/"
curl -s -o /dev/null -w '%{http_code}' "http://$ECS_IP:5000/post/1"  # Flask 示例路由
```

> 注意：KooCLI 具体命令以 `hcloud --help` / `hcloud ECS --help` 实测为准（不同版本命令名不同，
> 如 NovaListServers / CreateServer）。执行时按实际语法调整；创建失败如实报告错误。

### 4. 验证

部署完成后验证结果（2026-08-14 实测验收标准）：

- **端口检查**：`ss -tlnp | grep 5000` 应有 gunicorn 监听
- **HTTP 检查**：`curl http://$ECS_IP:5000/` 与 `http://$ECS_IP:5000/post/1` 均期望 **200**
- **公网可达**：从外部访问 `http://$ECS_IP:5000/`（安全组已放行 22/80/5000）
- 如果部署失败，输出错误信息并建议回滚

> ⚠️ 部署阶段实测坑（持续积累）：
> 1. **架构/隐私声明/下载超时**（2026-08-14）：见 §3.0 注释。
> 2. **hcloud 全局配置竞态**（2026-08-25 seq#2493 事故）：见 §3.1 注释。
> 3. **ECS 幂等 + 可用区 cn-north-4g 不可用 + 动态选规格 + EIP 自动创建 + keypair**（2026-08-14/20）：见 §3.2 注释。
> 4. **DNS 修复 + 镜像加速 + pip 重试 + pkill 自杀风险**（2026-08-20/22）：见 §3.3 注释。

## 环境变量

| 变量 | 来源 | 说明 |
|------|------|------|
| `HUAWEICLOUD_SDK_AK` | Grape 控制台 | 华为云 Access Key |
| `HUAWEICLOUD_SDK_SK` | Grape 控制台 | 华为云 Secret Key |
| `BOT_GITCODE_TOKEN` | account 配置 | GitCode API Token |

## 硬约束

- ❌ 禁止直接修改仓库代码
- ❌ 禁止删除仓库分支
- ✅ 仅执行当前 PR 合并后的部署流程
- ✅ 部署前先理解项目结构
- ✅ 部署失败时输出错误信息，不自动回滚
- ❌ **禁止 `hcloud configure set` 改 default profile**：必须用 `--cli-profile` 隔离，详见 §3.1
- ✅ **下载资源优先使用阿里云国内镜像**：apt/pip/npm/docker，详见 §3.3

## 资源释放流程

当操作类型识别为「资源释放」时，跳过构建/部署，按以下步骤释放华为云资源。

### 1. 收集资源信息

从 Issue 正文和评论中提取要释放的资源：

- **ECS 实例**：名称（如 `deploy-songspace-forum-java-084723`）或 IP
- **RDS 实例**：实例名或 ID
- **OBS 桶**：桶名
- **安全组/密钥对/EIP**：如有关联资源也一并释放

如果 Issue 中资源信息不完整，用 GitCode API 查 Issue 评论补充：
```bash
curl -s -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  "https://gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/comments"
```

仍无法确定时，在 Issue 回帖追问用户，列出已知资源请用户确认。

### 2. 查询并释放资源

**先查询再释放，确认每个资源存在后再操作。释放后验证已删除。**

```bash
# profile 隔离 + hc/obs 包装函数 + trap 清理（与 §3.1 相同，此处复用）
HC_PROFILE="task-$$"
hcloud configure set --cli-profile="$HC_PROFILE" \
  --cli-access-key="$HUAWEICLOUD_SDK_AK" --cli-secret-key="$HUAWEICLOUD_SDK_SK" \
  --cli-region=cn-north-4 --cli-mode=AKSK
hc() { hcloud "$@" --cli-profile="$HC_PROFILE"; }
obs() { obsutil "$@" -i "$HUAWEICLOUD_SDK_AK" -k "$HUAWEICLOUD_SDK_SK" -e obs.cn-north-4.myhuaweicloud.com; }
trap 'hcloud configure delete --cli-profile="$HC_PROFILE" 2>/dev/null || true' EXIT

# 2.1 释放 ECS 实例
#   查询：按名称前缀过滤（deploy-<repo> 或 <repo>-deploy）
hc ECS ListServersDetails --cli-region=cn-north-4 | python3 -c "
import json,sys
data=json.load(sys.stdin)
for s in data.get('servers',[]):
    print(s.get('id',''), s.get('name',''), s.get('status',''))
"
#   确认目标实例后删除：
hc ECS DeleteServer --cli-region=cn-north-4 --server_id="<INSTANCE_ID>"
#   验证：再次 ListServersDetails 确认该实例已不存在

# 2.2 释放 RDS 实例
hc RDS ListInstances --cli-region=cn-north-4 | python3 -c "
import json,sys
data=json.load(sys.stdin)
for i in data.get('instances',[]):
    print(i.get('id',''), i.get('name',''), i.get('status',''))
"
#   确认目标实例后删除：
hc RDS DeleteInstance --cli-region=cn-north-4 --instance_id="<RDS_ID>"

# 2.3 释放 OBS 桶
#   先清空桶内对象（非空桶无法删除）：
obs rm <BUCKET_NAME> -r -f 2>/dev/null || true
#   再删桶：
obs rm <BUCKET_NAME> -b 2>/dev/null || hc OBS DeleteBucket --bucket="<BUCKET_NAME>" 2>/dev/null || true

# 2.4 释放 EIP（如有独立 EIP 未随 ECS 自动释放）
hc EIP ListPublicIps --cli-region=cn-north-4 2>/dev/null | python3 -c "
import json,sys
data=json.load(sys.stdin)
for e in data.get('publicips',[]):
    print(e.get('id',''), e.get('public_ip_address',''), e.get('status',''))
"
#   确认后删除：
hc EIP DeletePublicIp --cli-region=cn-north-4 --publicip_id="<EIP_ID>"
```

### 3. 验证与报告

- 逐个确认资源已释放（查询返回空或 404）
- 在 Issue 回帖汇总释放结果（资源名 → 状态：已释放/未找到/释放失败）
- 输出最终 JSON 块（`actions` 中列出释放操作，`conclusion` 汇总结果）

> ⚠️ 释放注意事项：
> 1. **先查再删**：必须先查询确认资源存在，避免误删或操作不存在的资源。
> 2. **ECS 先删再删 EIP**：ECS 关联的 EIP 在 ECS 删除后可能自动释放，独立 EIP 需手动删。
> 3. **OBS 桶必须先清空**：非空桶删除会失败，先 `obsutil rm <bucket> -r -f` 清空对象。
> 4. **RDS 删除可能异步**：RDS DeleteInstance 返回后实例可能仍在「删除中」状态，等几分钟再验证。
> 5. **幂等**：重复释放同一资源不应报错，资源已不存在视为成功。
