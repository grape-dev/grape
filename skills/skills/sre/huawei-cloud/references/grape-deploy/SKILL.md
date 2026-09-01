---
name: grape-deploy
description: |
  Grape 平台专用部署子技能。当 SRE Agent 在 Grape 平台执行华为云 ECS 部署任务时使用此技能。
  核心职责：用 Grape 注入的 HUAWEICLOUD_SDK_AK/SK 环境变量配置 hcloud CLI 认证，
  确保使用仓库 owner 的凭据而非 worker 机器预配凭据，然后按 huawei-ecs 子技能流程创建/复用 ECS 并部署应用。
  Triggers: Grape A2A 交办部署、PR 合并后触发部署、用户请求 ECS 部署。
  NOT for: 非 Grape 环境的华为云操作（用 huawei-getting-started + huawei-ecs）。
version: 1.0.0
---

# Grape 平台部署（华为云 ECS）

## 适用场景

此技能仅在 **Grape 平台** 的 SRE Agent 执行华为云 ECS 部署任务时使用。

Grape 平台通过 `env_extra` 注入以下环境变量：
- `HUAWEICLOUD_SDK_AK` — 仓库 owner 的华为云 Access Key
- `HUAWEICLOUD_SDK_SK` — 仓库 owner 的华为云 Secret Key
- `BOT_GITCODE_TOKEN` — GitCode API Token
- `REPO_FULL_NAME` — 仓库全称（owner/repo）
- `ISSUE_NUMBER` — 关联 Issue 号
- `PR_NUMBER` — 关联 PR 号

## 安全硬约束（必须遵守）

1. **必须使用环境变量注入的 AK/SK**，严禁使用 worker 机器上 hcloud 预配的凭据。worker 机器可能配了其他用户的 AK/SK，用错凭据会把资源建到别人账号下。
2. 调用 GitCode API 只用 prompt 注入的 `$BOT_GITCODE_TOKEN`，严禁从数据库/configs/日志/历史中寻找其他 token。
3. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
4. API 返回 403/无权限时：立即停止该操作并回复用户，严禁尝试其他 token 或绕行手段。
5. token/AK/SK 不得写入任何文件/日志/评论。
6. 所有 curl / git / hcloud 调用设 30s 超时（curl --max-time 30）——超时判定失败，不 hang。

## 硬规则

| 行为 | 态度 |
|------|------|
| 执行任何 hcloud 命令前必须先 `hcloud configure set` 用注入的 AK/SK 覆盖凭据 | ✅ **必须做，不可跳过** |
| 用 `$HUAWEICLOUD_SDK_AK` / `$HUAWEICLOUD_SDK_SK` 环境变量配置 hcloud | ✅ **必须做** |
| 创建 ECS 前必须先查是否已有同名实例（幂等） | ✅ **必须做** |
| 创建 ECS 必须带 keypair | ✅ **必须做** |
| 部署完成后必须 curl 验证应用可访问 | ✅ **必须做** |
| 使用 worker 机器上 hcloud 预配的凭据 | ❌ **禁止** |
| 跳过 `hcloud configure set` 直接调 hcloud 命令 | ❌ **禁止** |
| 重复创建 ECS（已有同名实例时再建一台） | ❌ **禁止** |
| 直接修改仓库代码 | ❌ **禁止** |
| 删除仓库分支 | ❌ **禁止** |

## 流程

### Step 0: 安装 hcloud（如缺失）

```bash
if ! command -v hcloud >/dev/null 2>&1; then
  echo "hcloud not installed, installing KooCLI..."
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
  echo y | hcloud configure set --cli-agree-privacy-statement=true
fi
hcloud version || echo "hcloud still not installed — deployment cannot proceed"
```

### Step 1: 配置 hcloud 认证（必须执行，不可跳过）

```bash
# ⚠️ 这一步是整个流程的关键——必须用 Grape 注入的 AK/SK 覆盖 worker 机器预配凭据。
# worker 机器上 hcloud 可能已配了其他用户的凭据，不覆盖会把资源建到别人账号下。

# 检查注入的 AK/SK 是否非空
if [ -z "$HUAWEICLOUD_SDK_AK" ] || [ -z "$HUAWEICLOUD_SDK_SK" ]; then
  echo "❌ HUAWEICLOUD_SDK_AK/SK 未注入或为空，无法部署"
  echo "请在 Grape 管理台 → 设置 → 华为云凭据 中配置 AK/SK 后重试"
  exit 1
fi

# 用注入的 AK/SK 配置 hcloud CLI
hcloud configure set --cli-access-key="$HUAWEICLOUD_SDK_AK" --cli-secret-key="$HUAWEICLOUD_SDK_SK" --cli-region=cn-north-4

# 验证配置生效——确认 accessKeyId 末尾与注入的 AK 末尾一致
hcloud configure list
```

> **为什么这一步不能省**：Grape 的多用户隔离依赖此步骤。每个仓库 owner 有自己的华为云 AK/SK，Grape 通过 env_extra 注入。如果不执行 `hcloud configure set`，hcloud 会用 worker 机器上预配的凭据（可能是上一个用户的），导致 ECS/EIP/VPC 等资源创建到错误的华为云账号下。

### Step 2: 理解项目结构

读取仓库根目录的文件，判断构建方式和部署方式：

| 文件标记 | 推断 |
|----------|------|
| `package.json` | Node.js 项目，可能用 `npm` / `yarn` |
| `requirements.txt` / `pyproject.toml` / `Pipfile` | Python 项目，可能用 `pip` / `poetry` |
| `go.mod` | Go 项目，`go build` |
| `pom.xml` / `build.gradle` | Java 项目，`mvn` / `gradle` |
| `Dockerfile` | 容器化项目，构建镜像并推送 |
| `Makefile` | 自定义构建流程（读取 Makefile 中的目标） |
| `app.py` / `main.py` | Python Flask 项目 |
| `static/` / `templates/` | Flask 模板项目 |
| `deploy/` 目录 | 自定义部署配置（systemd/nginx/docker-compose） |

### Step 3: ECS 幂等检查 + 创建

```bash
# 幂等约束：部署前必须先查是否已有本仓库的 ECS，有则复用，绝不重复新建。
# 查法：按名称前缀过滤
EXISTING_ECS=$(hcloud ECS ListServersDetails --cli-region=cn-north-4 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for s in d.get('servers',[]):
    if s.get('status')=='ACTIVE':
        addrs=s.get('addresses',{})
        for vpc,ips in addrs.items():
            for ip in ips:
                if ip.get('OS-EXT-IPS:type')=='floating':
                    print(ip.get('OS-EXT-IPS:addr',''))
" 2>/dev/null)

if [ -n "$EXISTING_ECS" ]; then
  echo "复用已有 ECS: $EXISTING_ECS"
  ECS_IP="$EXISTING_ECS"
else
  # 新建 ECS 流程见 references/create-instance.md（huawei-ecs 子技能）
  # 关键参数：
  #   - 可用区必须 cn-north-4a 或 cn-north-4b（不能用 cn-north-4g，SSH 连不上）
  #   - 规格动态查询：hcloud ECS ListFlavors → 取最小可用 postPaid 规格
  #   - 必须带 keypair（否则 SSH 连不上）
  #   - EIP 自动创建（--server.publicip.eip.*）
  #   - 用 user_data 做 cloud-init 初始化
  #
  # 详细创建步骤请读取 huawei-ecs 子技能：
  #   read references/huawei-ecs/references/create-instance.md
  echo "需要新建 ECS，请读取 huawei-ecs 子技能的 create-instance.md 获取完整流程"
fi
```

### ⚠️ ECS 创建注意事项（实测教训）

| 陷阱 | 说明 |
|------|------|
| 可用区 cn-north-4g 不可用 | 该 AZ 使用 DPDK/vhostuser 高性能网卡，SSH 永远连不上。必须指定 cn-north-4a 或 cn-north-4b |
| 硬编码规格买不到 | s6.small.1 等已废弃。必须 `hcloud ECS ListFlavors` + `ListFlavorSellPolicies` 动态查询 sell_status=available |
| 不带 keypair | 建完 SSH 连不上，Agent 会反复试错重建（每跑一次多一台机器、多一份费用） |
| 手动关联 EIP | 必须在 CreateServers 时用 `--server.publicip.eip.*` 自动创建，手动关联可能因 VPC 缺 IG 失败 |
| DNS 解析失败 | Ubuntu 22.04 的 systemd-resolved 可能无法解析外部域名。SSH 脚本内先设 `nameserver 8.8.8.8` |
| pip 网络超时 | pip 连 files.pythonhosted.org 经常 ReadTimeout，必须重试 3 次 |

### Step 4: 部署代码到 ECS

```bash
# a) 上传代码
ssh -o StrictHostKeyChecking=accept-new -i <key> root@"$ECS_IP" "mkdir -p /opt/$REPO_NAME"
scp -o StrictHostKeyChecking=accept-new -r "$WORKDIR"/* root@"$ECS_IP":/opt/$REPO_NAME/

# b) 一次性完成部署（单条 SSH 会话，防超时）
ssh -o StrictHostKeyChecking=accept-new -i <key> root@"$ECS_IP" 'bash -s' << 'DEPLOY_EOF'
set -e
# DNS 修复
echo 'nameserver 8.8.8.8' > /etc/resolv.conf
echo 'nameserver 223.5.5.5' >> /etc/resolv.conf

cd /opt/__REPO_NAME__
# 系统依赖
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv python3-10-venv 2>/dev/null || apt-get install -y -qq python3 python3-pip python3-venv
# venv + 依赖
rm -rf venv
python3 -m venv venv
venv/bin/pip install -q --upgrade pip || true
for i in 1 2 3; do
  venv/bin/pip install -q -r requirements.txt gunicorn && break
  echo "pip retry $i/3"
  sleep 5
done
# 启动（按端口精确清理旧进程，禁止 pkill -f）
ss -tlnp 2>/dev/null | grep ':5000' | grep -oP 'pid=\K[0-9]+' | sort -u | xargs -r kill 2>/dev/null || true
sleep 1
nohup venv/bin/gunicorn -w 2 -b 0.0.0.0:5000 app:app > /tmp/flask.log 2>&1 &
sleep 4
DEPLOY_EOF
```

> **严禁 `pkill -f gunicorn` / `pkill -f app.py`**：worker 上 opencode 进程的命令行包含完整 prompt（含 "gunicorn" 字样），pkill -f 会 SIGTERM 掉 agent 自身进程导致任务自杀（seq#667 实测）。清理旧进程必须按端口精确操作：`ss -tlnp | grep ':5000'`。

### Step 5: 验证部署

```bash
# 端口检查
ssh -o StrictHostKeyChecking=accept-new -i <key> root@"$ECS_IP" 'ss -tlnp | grep 5000'

# HTTP 检查
curl -s -o /dev/null -w '%{http_code}' "http://$ECS_IP:5000/"
curl -s -o /dev/null -w '%{http_code}' "http://$ECS_IP:80/"

# API 功能验证（如有 API 端点）
curl -s -X POST "http://$ECS_IP:80/calculate" -H 'Content-Type: application/json' -d '{"op":"add","a":3,"b":5}'
```

验证通过后在 result 中报告：
- ECS 实例名 + ID + EIP
- Web 访问地址
- API 测试结果
- 部署架构（systemd/nginx/gunicorn）

## 与 huawei-ecs 子技能的关系

此技能专注于 Grape 平台部署场景，补充了 huawei-ecs 不包含的：
1. **Grape 环境变量凭据注入** — `hcloud configure set` 强制覆盖
2. **多用户隔离** — 确保用仓库 owner 的凭据，不用 worker 预配凭据
3. **幂等 ECS 复用** — 避免重复创建
4. **应用部署流程** — git clone → 依赖安装 → 服务启动 → 验证

ECS 创建的详细参数（flavor 选择、image 查询、VPC 创建等）请读取 huawei-ecs 子技能：
```
read references/huawei-ecs/references/create-instance.md
```

## 环境变量

| 变量 | 来源 | 说明 |
|------|------|------|
| `HUAWEICLOUD_SDK_AK` | Grape env_extra 注入 | 仓库 owner 的华为云 Access Key |
| `HUAWEICLOUD_SDK_SK` | Grape env_extra 注入 | 仓库 owner 的华为云 Secret Key |
| `BOT_GITCODE_TOKEN` | Grape env_extra 注入 | GitCode API Token |
| `REPO_FULL_NAME` | Grape env_extra 注入 | 仓库全称（owner/repo） |
| `ISSUE_NUMBER` | Grape env_extra 注入 | 关联 Issue 号 |
| `PR_NUMBER` | Grape env_extra 注入 | 关联 PR 号 |
