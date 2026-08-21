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
| `app.py` / `main.py` | Python Flask 项目 |
| `static/` / `templates/` | Flask 模板项目 |

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
# 3.1 配置 hcloud 认证（用注入的 AK/SK，实测 KooCLI 7.2.12 支持 configure set）
hcloud configure set --cli-access-key="$HUAWEICLOUD_SDK_AK" --cli-secret-key="$HUAWEICLOUD_SDK_SK" --cli-region=cn-north-4

# 3.2 ECS 幂等检查 + 创建（区域固定 cn-north-4；按量计费最小规格；开 22/80/5000 端口）
#   **幂等约束（2026-08-14 实测教训）：部署前必须先查是否已有本仓库的 ECS，
#   有则复用（直接部署到它），绝不重复新建——否则每跑一次多一台机器、多一份费用。**
#   查法：按名称前缀过滤（deploy-<repo> 或 <repo>-deploy），hcloud ECS ListServersDetails 返回里
#   过滤出匹配名称的 ACTIVE 实例，取第一个的浮动 IP 作为 $ECS_IP 复用，跳过下方创建。
#   ⚠️ 可用区约束（2026-08-20 实测教训）：创建 ECS 必须显式指定可用区
#   **cn-north-4a** 或 **cn-north-4b**，绝不能用 cn-north-4g（该 AZ 使用 DPDK/vhostuser
#   高性能网卡，不走标准内核网络栈，SSH 永远连不上）。
#   ⚠️ 规格选择（2026-08-20 实测教训）：不要硬编码 flavor（如 s6.small.1 在 postPaid 模式
#   下买不到），必须动态查询当前区域最小可用按量计费规格：
#     hcloud ECS ListFlavors --cli-region=cn-north-4 → 取规格列表
#     按 vcpu↑, ram↑ 排序
#     hcloud ECS ListFlavorSellPolicies --flavor_id=... 确认 sell_mode=postPaid + sell_status=available
#   ⚠️ EIP 创建（2026-08-20 实测教训）：必须在创建 ECS 时通过 --server.publicip.eip.*
#   参数**自动创建 EIP**，不要先建 ECS 再手动关联 EIP。自动创建的 EIP 由华为云 API
#   自动处理 VPC 网关 NAT 路由；手动关联可能因 VPC 缺少 Internet Gateway 或路由表
#   缺 0.0.0.0/0 而无法通信。
EXISTING_ECS=$(hcloud ECS ListServersDetails --cli-region=cn-north-4 2>/dev/null | grep -B5 -A20 "\"name\": \"deploy-${REPO_FULL_NAME//\//-}" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | tail -1)
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
  if ! hcloud ECS NovaListKeypairs --cli-region=cn-north-4 2>/dev/null | grep -q "grape-deploy-kp"; then
    hcloud ECS NovaCreateKeypair --cli-region=cn-north-4 --name=grape-deploy-kp \
      --public_key="$(cat ~/.ssh/id_ed25519.pub)" 2>/dev/null || true
  fi
  # 动态选最小可用规格（2026-08-20 修复：不再硬编码 s6.small.1）
  # 流程：ListFlavors → 取规格列表 → 按架构/按需/最小排序
  ARCH=$(uname -m)
  MIN_FLAVOR=$(hcloud ECS ListFlavors --cli-region=cn-north-4 2>/dev/null | python3 -c "
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
  INSTANCE_NAME="deploy-${REPO_FULL_NAME//\/-}-$(date +%H%M%S)"
  hcloud ECS <CreateServer> --cli-region=cn-north-4 \
      --name "$INSTANCE_NAME" --flavor "$MIN_FLAVOR" --image "Ubuntu 22.04" \
    --charging-mode postPaid --security-group "default" \
    --keypair "grape-deploy-kp" \
    --user-data "#!/bin/bash
      mkdir -p /root/.ssh && echo '$(cat ~/.ssh/id_ed25519.pub)' >> /root/.ssh/authorized_keys" ...
  # 等待 ECS running，拿公网浮动 IP（hcloud ECS ListServersDetails 按名称查）
  ECS_IP=$(...按 INSTANCE_NAME 过滤出浮动 IP...)
fi
# 复用场景也要确保免密可用：公钥没进去就补推一次
ssh-copy-id -o StrictHostKeyChecking=no root@"$ECS_IP" 2>/dev/null || true

# 3.3 部署代码到 ECS（实测流程，2026-08-14 验证）
#   a) 上传代码：scp 仓库文件到 /opt/<repo>
ssh -o StrictHostKeyChecking=no root@"$ECS_IP" "mkdir -p /opt/$REPO_NAME"
scp -o StrictHostKeyChecking=no -r "$WORKDIR"/* root@"$ECS_IP":/opt/$REPO_NAME/

#   b) 免密 SSH：推送 worker 公钥到 ECS（之后所有命令免密执行，避免 expect 密码交互卡超时）
ssh-copy-id -o StrictHostKeyChecking=no root@"$ECS_IP"  # 若 ECS 创建时已注入密钥对则跳过

#   c) 一次性完成：系统依赖 → venv → 依赖 → 启动（**单条 SSH 会话**，勿拆多条，防超时）
#      注意：把下方 __REPO_NAME__ 替换为实际仓库名；heredoc 内无变量展开，环境变量需在此处预置
ssh -o StrictHostKeyChecking=no root@"$ECS_IP" 'bash -s' << 'DEPLOY_EOF'
set -e
# DNS 修复（2026-08-20 实测教训）：Ubuntu 22.04 默认 systemd-resolved（127.0.0.53）
# 解析 GitCode 等外部域名可能失败（SERVFAIL），导致 git clone / pip install 超时。
# 部署前先设公共 DNS：
echo 'nameserver 8.8.8.8' > /etc/resolv.conf
echo 'nameserver 223.5.5.5' >> /etc/resolv.conf

cd /opt/__REPO_NAME__
# 系统依赖（Ubuntu 22.04 精简镜像缺 python3-venv，不装则 venv 建不出 pip → 部署失败）
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv python3.10-venv 2>/dev/null || apt-get install -y -qq python3 python3-pip python3-venv
# venv + 依赖（pip 网络超时是常态，重试 3 次）
rm -rf venv
python3 -m venv venv
venv/bin/pip install -q --upgrade pip || true
for i in 1 2 3; do
  venv/bin/pip install -q -r requirements.txt gunicorn && break
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
> 1. **Ubuntu 22.04 精简镜像缺 python3-venv** → venv 建不出 pip，必须 `apt-get install python3-venv python3.10-venv`
> 2. **pip 连 files.pythonhosted.org 经常 ReadTimeout** → 必须重试（实测第 1 次超时、第 2 次成功）
> 3. **SSH 部署务必用单条 ssh 会话跑完所有命令**（或先 ssh-copy-id 免密）——expect 密码交互多命令叠加会超 600s 任务上限
> 4. **创建 ECS 必须带 keypair + 创建前先查已有实例**——否则建完 SSH 连不上，Agent 会反复试错重建（每跑一次多一台机器、多一份费用）
> 5. **可用区 cn-north-4g 不可用**（2026-08-20 实测）：该 AZ 使用 DPDK/vhostuser 高性能网卡，不走标准内核网络栈，SSH 永远连不上。必须指定 `--server.availability_zone=cn-north-4a` 或 `cn-north-4b`。
> 6. **Ecs.0707 规格找不到**（2026-08-20 实测）：`s6.small.1` 等硬编码 flavor 在 postPaid 模式下可能不售卖。用 `hcloud ECS ListFlavors` + `ListFlavorSellPolicies` 动态查询最小可用规格，确认 sell_status=available 再使用。
> 7. **DNS 解析失败**（2026-08-20 实测）：Ubuntu 22.04 的 systemd-resolved（127.0.0.53）可能无法解析 GitCode 等外部域名（SERVFAIL）。部署 SSH 脚本内先设 `echo 'nameserver 8.8.8.8' > /etc/resolv.conf`。
> 8. **EIP 必须自动创建**（2026-08-20 实测）：创建 ECS 时必须用 `--server.publicip.eip.*` 参数让 API 自动创建并关联 EIP，不要先建 ECS 再手动关联，否则可能因 VPC 缺少 Internet Gateway 而无法通信。

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
