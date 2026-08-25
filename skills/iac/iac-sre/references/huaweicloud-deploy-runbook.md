# 华为云 Terraform 部署 Runbook（SRE 执行用）

## 适用场景

- 使用 Terraform 在华为云部署基础设施（ECS/RDS/VPC/安全组/EIP）
- 使用 OBS 作为 Terraform 远程状态后端（S3 兼容）
- 需要将资源清单（含敏感密码）保存到 OBS
- cn-north-4（华北4）区域部署

## 前置条件

- **Terraform >= 1.7.5 且 <= 1.10.5**（⚠️ 版本硬约束，详见下方「Terraform 版本兼容性」段）
- huaweicloud provider（最新版即可，实际验证 v1.94.0 可用；不要写 `~> 1.50` 或 `~> 1.60` 等上限约束，会匹配不到高版本）
- obsutil 已安装（用于 OBS 对象操作，替代 curl SigV4）
- 华为云 AK/SK 已获取（或通过 Grape 控制台注入）
- OBS bucket 已创建（或通过 scripts/bootstrap.sh 创建）

## Terraform 版本兼容性（⚠️ 关键约束）

OBS S3 backend 与 Terraform CLI 版本存在严格的兼容性窗口，**版本选错会导致 state 写入失败**。以下结论基于实测（8 个版本 × 3 种配置 = 24 组测试 + 端到端验证）：

| TF 版本 | OBS S3 backend | 根因 | 结论 |
|---------|---------------|------|------|
| 1.5.x | ✅ 可用 | aws-sdk-go v1，签名兼容 | 可用（minimal 配置，无需 skip 参数） |
| **1.6.x** | ❌ 不可用 | 要求获取 AWS account ID（调 STS），但不支持 `skip_requesting_account_id` 参数 | **断层版本，完全不可用** |
| **1.7.5 ~ 1.10.5** | ✅ 可用 | aws-sdk-go v2，但 `skip_s3_checksum=true` 能绕过 SHA256 不匹配 | **推荐版本区间** |
| **1.11.0+** | ❌ 不可用 | `skip_s3_checksum` 参数被接受但实际无效，`x-amz-content-sha256` header 仍不匹配 | **不可用** |

### 推荐配置

```
Terraform 版本: 1.9.8（或 1.7.5 ~ 1.10.5 区间内任意版本）
backend 配置: 必须包含以下 skip 参数：
  - skip_requesting_account_id = true  （1.7+ 必需，跳过 AWS STS 调用）
  - skip_s3_checksum            = true  （1.7~1.10 必需，绕过 SHA256 不匹配）
  - skip_region_validation      = true
  - skip_credentials_validation = true
  - skip_metadata_api_check     = true
  - endpoint 必须带 https:// 前缀
```

### 不可用版本的症状

TF 1.11+ 执行 `terraform apply` 时：
1. 资源创建成功（`Creation complete`）
2. state 写入 OBS 失败（`XAmzContentSHA256Mismatch: HTTP 400`）
3. state 落到本地 `errored.tfstate` 文件
4. `terraform destroy` 失败（因 state 不在后端，Terraform 不知道要销毁什么）

**遇到此症状首先检查 Terraform 版本是否在 1.7.5 ~ 1.10.5 区间内。**

## Terraform Provider 预装（防下载超时）

worker 机器网络到 registry.terraform.io / releases.hashicorp.com 可能慢或偶发超时。
**部署前必须确认所有 provider 二进制已预装到 plugin cache 目录**，不要依赖 terraform init 现下载。

### 使用国内镜像源（必须）

Terraform CLI 和 provider 默认从 `registry.terraform.io` / `releases.hashicorp.com` 下载，国内访问慢且经常超时。**必须配置华为云镜像源**：

```bash
# Terraform CLI 下载镜像（安装/升级 terraform 二进制时使用）
# https://mirrors.huaweicloud.com/terraform/

# Provider registry 镜像（terraform init 下载 provider 时使用）
# ⚠️ 华为云镜像不包含所有 provider（如 hashicorp/local、hashicorp/null 等），
# 必须使用 network_mirror + direct 混合配置：优先华为云镜像，找不到的 fallback 到官方 registry
cat > ~/.terraformrc << 'EOF'
provider_installation {
  network_mirror {
    url = "https://mirrors.huaweicloud.com/terraform/providers/"
  }
  direct {
    exclude = ["registry.terraform.io/huaweicloud/*"]
  }
}
EOF

# 或通过环境变量临时指定
export TF_CLI_CONFIG_FILE="$HOME/.terraformrc"
```

混合配置说明：
- `network_mirror`：优先从华为云镜像下载所有 provider（huaweicloud、hashicorp/random 等）
- `direct`：华为云镜像没有的 provider（如 hashicorp/local、hashicorp/null）从官方 registry 下载
- `exclude`：避免 huaweicloud provider 回退到官方 registry（官方源国内访问慢）

⚠️ **不配置镜像源时，terraform init 下载 provider 可能超时 5 分钟以上甚至失败。**
⚠️ **只用 `network_mirror` 不加 `direct` 时，hashicorp/local 等 provider 会下载失败。**

### 启用 plugin cache（必须）

Terraform 默认不启用 plugin cache，必须设置环境变量 `TF_PLUGIN_CACHE_DIR`，否则 `terraform init` 不会从 cache 目录读取，每次都重新下载。

```bash
# 在部署前设置（写入 ~/.bashrc 或部署脚本开头）
export TF_PLUGIN_CACHE_DIR="$HOME/.terraform.d/plugin-cache"
mkdir -p "$TF_PLUGIN_CACHE_DIR"
```

⚠️ **未设置 `TF_PLUGIN_CACHE_DIR` 时，即使 provider 已预装到 `~/.terraform.d/plugin-cache` 目录，`terraform init` 仍会重新下载。**

### 检查已装 provider

```bash
# 1. 确认机器架构（arm64 vs amd64）
uname -m   # aarch64 = arm64, x86_64 = amd64

# 2. 检查 plugin cache 里的 provider 二进制
find ~/.terraform.d/plugin-cache -name 'terraform-provider-*' -type f

# 3. 确认架构匹配——arm64 机器上不能放 _linux_amd64 的二进制
#    amd64 文件名: terraform-provider-xxx_X.Y.Z_linux_amd64.zip
#    arm64 文件名: terraform-provider-xxx_X.Y.Z_linux_arm64.zip
```

### 手动预装 provider（当 terraform init 下载超时时）

```bash
# huaweicloud provider（从华为云镜像下载对应架构的 zip）
ARCH=linux_arm64   # arm64 机器用 linux_arm64，amd64 机器用 linux_amd64
VERSION=1.97.0     # 用最新版

curl -L -o /tmp/huaweicloud_provider.zip \
  "https://mirrors.huaweicloud.com/terraform/providers/terraform-provider-huaweicloud/${VERSION}/terraform-provider-huaweicloud_${VERSION}_${ARCH}.zip"
unzip -o /tmp/huaweicloud_provider.zip -d ~/.terraform.d/plugin-cache/registry.terraform.io/huaweicloud/huaweicloud/${VERSION}/${ARCH}/

# hashicorp/random provider（同理）
RANDOM_VERSION=3.6.3
curl -L -o /tmp/random_provider.zip \
  "https://mirrors.huaweicloud.com/terraform/providers/terraform-provider-random/${RANDOM_VERSION}/terraform-provider-random_${RANDOM_VERSION}_${ARCH}.zip"
unzip -o /tmp/random_provider.zip -d ~/.terraform.d/plugin-cache/registry.terraform.io/hashicorp/random/${RANDOM_VERSION}/${ARCH}/
```

### terraform init 超时处理

如果 `terraform init` 仍然超时（provider 下载慢）：

```bash
# 方法 1：确认已配置华为云镜像源（见上方「使用国内镜像源」段）
# 方法 2：设置更长的超时
TF_REGISTRY_CLIENT_TIMEOUT=300 terraform init

# 方法 3：用 -backend=false 先跳过 backend，只验证 provider
terraform init -backend=false

# 方法 4：手动下载 zip 后解压到 plugin cache（见上方手动预装步骤，使用华为云镜像 URL）
```

⚠️ **架构陷阱**：arm64 机器上下载了 amd64 的 provider zip 会导致 `terraform init` 报 `incompatible provider version`。务必确认 `uname -m` 与下载的 zip 文件名架构一致。

## 部署 Runbook

### 1. 凭证设置

```bash
# 华为云凭证（Provider 认证 + OBS backend 认证共用）
export HW_ACCESS_KEY="<AK>"
export HW_SECRET_KEY="<SK>"
export HW_REGION="cn-north-4"

# AWS 兼容凭证（OBS S3 backend init 必需，与 HW 同值）
export AWS_ACCESS_KEY_ID="$HW_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$HW_SECRET_KEY"
export AWS_DEFAULT_REGION="cn-north-4"

# 启用 Terraform plugin cache（复用已预装的 provider，避免每次重新下载）
export TF_PLUGIN_CACHE_DIR="$HOME/.terraform.d/plugin-cache"
mkdir -p "$TF_PLUGIN_CACHE_DIR"

# 配置华为云镜像源（provider 下载走国内镜像，避免超时）
# ⚠️ 混合配置：华为云镜像优先，找不到的 provider fallback 到官方 registry
export TF_CLI_CONFIG_FILE="$HOME/.terraformrc"
if [ ! -f "$TF_CLI_CONFIG_FILE" ]; then
  cat > "$TF_CLI_CONFIG_FILE" << 'EOF'
provider_installation {
  network_mirror {
    url = "https://mirrors.huaweicloud.com/terraform/providers/"
  }
  direct {
    exclude = ["registry.terraform.io/huaweicloud/*"]
  }
}
EOF
fi

# 敏感密码（通过环境变量传入，绝不硬编码到 .tf 文件）
# ECS root 密码：8-26位，含大小写+数字+特殊字符 !@$%^-_=+
export TF_VAR_ecs_admin_pass='<ECS密码>'
# RDS root 密码：8-32位，含大小写+数字+特殊字符
export TF_VAR_rds_password='<RDS密码>'
```

⚠️ **凭证说明**：`HW_ACCESS_KEY`/`HW_SECRET_KEY` 和 `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` 是**同一条 AK/SK**，设两套变量是因为：
- huaweicloud provider 读 `HW_*` 变量
- Terraform S3 backend 读 `AWS_*` 变量
- 两条认证路径完全独立（provider 走 IAM token，backend 走 SigV4），但用同一组密钥

### 2. 预建 OBS Bucket（首次部署）

```bash
./scripts/bootstrap.sh
```

### 3. 部署基础设施

```bash
cd examples/blog-infrastructure

# 确认 Terraform 版本在 1.7.5 ~ 1.10.5 区间内
terraform version

# 初始化（配置 OBS 远程状态后端）
terraform init

# 预览资源变更
terraform plan

# 部署（RDS 创建可能需要 5-10 分钟）
terraform apply -auto-approve

# 输出资源清单（含 sensitive 密码）
terraform output -json
```

### 4. 保存资源清单到 OBS

```bash
cd ../..
./scripts/save-inventory-to-obs.sh
```

或使用 obsutil 手动上传：

```bash
obsutil cp ./inventory.json obs://my-terraform-state-bucket/blog-infrastructure/inventory.json
```

### 5. 验证

```bash
# 验证 OBS 清单可读
obsutil ls obs://my-terraform-state-bucket/blog-infrastructure/
obsutil cat obs://my-terraform-state-bucket/blog-infrastructure/inventory.json
```

## cn-north-4 区域已知坑点

### RDS Flavor 可用性

cn-north-4 仅支持 `n1` / `x1` / `arm` 类型 RDS flavor，不支持 `c2` 类型。

| 代码中配置 | cn-north-4 状态 | 替代方案 |
|-----------|----------------|---------|
| rds.mysql.c2.large.2 | 不存在 | rds.mysql.n1.large.2 (2c4g) |
| rds.mysql.c2.large.4 | 不存在 | rds.mysql.n1.large.4 (2c8g) |

部署前通过 `terraform plan` 验证 RDS flavor 可用性（plan 会调用 provider API 检查，使用环境变量中的 `HW_ACCESS_KEY`/`HW_SECRET_KEY`）：
```bash
# terraform plan 会自动验证 flavor 是否存在
terraform plan -out=tfplan

# 如需手动查询可用规格，使用环境变量中的 AK/SK 获取 IAM token（不要用 hcloud CLI 的凭证）
# IAM token 获取方式见部署 Runbook 第 1 步
```

### RDS 存储类型

| 类型 | cn-north-4 状态 | 说明 |
|------|----------------|------|
| ULTRAHIGH | 已弃用 | 不再支持，会报 DBS.280241 |
| CLOUDSSD | 可用 | SSD 云盘，推荐 |
| LOCALSSD | 可用 | 本地 SSD |
| ESSD | 可用 | 增强型 SSD |

部署前通过 `terraform plan` 验证存储类型可用性：
```bash
# terraform plan 会自动验证存储类型是否存在
terraform plan -out=tfplan
```

### OBS SigV4 签名日期格式

OBS SigV4 签名中 `x-amz-date` header 必须使用 `yyyyMMddTHHmmssZ` 格式（无连字符），不能使用 ISO 8601 标准格式。

```bash
# 错误（含连字符，OBS 返回 HTTP 400）
date -u +"%Y-%m-%dT%H:%M:%SZ"   # 2026-08-20T15:41:54Z

# 正确（无连字符）
date -u +"%Y%m%dT%H%M%SZ"        # 20260820T154154Z
```

建议：优先使用 obsutil 替代手动 SigV4 curl 签名，避免格式坑点。

### hcloud CLI 与凭证隔离

hcloud CLI 与 Terraform/provider 使用的凭证可能不同。**资源验证和查重必须使用环境变量中的 `HW_ACCESS_KEY`/`HW_SECRET_KEY`（与 Terraform provider 同源），禁止使用 hcloud CLI 中配置的凭证**，否则可能因权限不一致导致误操作或查重结果不准确。

| 操作 | 推荐方式 | 不推荐 |
|------|---------|--------|
| 资源规格验证 | `terraform plan`（自动调 provider API） | hcloud CLI |
| 资源查重 | 华为云 API + 环境变量 AK/SK 获取 IAM token | hcloud CLI |
| OBS 对象操作 | obsutil | hcloud CLI（OBS 命令不存在） |
| 部署执行 | `terraform apply` | hcloud CLI |

## 常见问题排查

### terraform init 失败：provider 下载超时

- 根因：worker 网络到 releases.hashicorp.com / registry.terraform.io 偶发慢，terraform init 默认超时不够
- 解决（按优先级）：
  1. 确认已配置华为云镜像源（`~/.terraformrc` 中 `network_mirror` 指向 `https://mirrors.huaweicloud.com/terraform/providers/`）
  2. 确认已设置 `TF_PLUGIN_CACHE_DIR` 且 cache 中有 provider 二进制
  3. 手动从华为云镜像下载 provider zip 解压到 plugin cache（见上方手动预装步骤）
  4. 设置 `TF_REGISTRY_CLIENT_TIMEOUT=300` 加长超时
- ⚠️ 务必确认架构匹配：arm64 机器（`uname -m` = `aarch64`）只能用 `linux_arm64` 的 zip，amd64 用 `linux_amd64`

### terraform init 失败：No valid credential sources found

- 根因：环境变量 HW_ACCESS_KEY / HW_SECRET_KEY 未设置
- 解决：在 Grape 控制台注入 AK/SK，或手动 `export HW_ACCESS_KEY=... HW_SECRET_KEY=...`
- 注意：同时需要设置 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY（S3 backend 兼容）

### terraform apply 失败：DBS.280434 Invalid specification code

- 根因：RDS flavor 在 cn-north-4 不可用（如 c2 类型）
- 解决：通过 ListFlavors API 查询可用规格，用 `-var="rds_flavor=rds.mysql.n1.large.2"` 覆盖
- 永久修复：修改 variables.tf 默认值

### terraform apply 失败：DBS.280241 Invalid storage type

- 根因：存储类型已弃用（如 ULTRAHIGH）
- 解决：用 `-var="rds_volume_type=CLOUDSSD"` 覆盖
- 永久修复：修改 variables.tf 默认值

### terraform apply 失败：Failed to save state (XAmzContentSHA256Mismatch)

- 根因：Terraform 版本不在兼容区间内（1.7.5 ~ 1.10.5），或 backend 配置缺少 `skip_s3_checksum`/`skip_requesting_account_id` 参数
- 症状：资源创建成功（`Creation complete`），但 state 写入 OBS 失败（HTTP 400），state 落到本地 `errored.tfstate`
- 恢复：`errored.tfstate` 含本次创建的资源信息，可用于人工排查，但**不应通过 obsutil 手动上传到 OBS 替代 state**（会破坏 state 一致性和锁机制）
- 解决：
  1. 检查 `terraform version` 是否在 1.7.5 ~ 1.10.5 区间
  2. 检查 backend 配置是否包含 `skip_s3_checksum = true` 和 `skip_requesting_account_id = true`
  3. 修正后重新 `terraform init && terraform apply`
- ⚠️ 如果资源已创建但 state 未写入 OBS，需通过 API/控制台确认资源状态后再决定是否重建

### terraform init 失败：Retrieving AWS account details (STS GetCallerIdentity)

- 根因：Terraform 1.6+ 尝试调用 AWS STS 获取 account ID，华为云没有 STS 服务
- 解决：backend 配置加 `skip_requesting_account_id = true`
- ⚠️ TF 1.6.x 不支持此参数（断层版本），必须升级到 1.7.5+

### terraform init 失败：VirtualHostDomainRequired

- 根因：backend 配置了 `force_path_style = true`，但 cn-north-4 的 OBS 要求 virtual-host-style
- 解决：移除 `force_path_style` 参数，让 Terraform 使用默认的 virtual-host-style

### save-inventory-to-obs.sh 失败：HTTP 400

- 根因：SigV4 签名日期格式错误（含连字符）
- 解决：使用 obsutil 替代 curl，或修复 date 格式为 `date -u +"%Y%m%dT%H%M%SZ"`
- 注意：OBS 对象操作必须使用 obsutil（hcloud CLI 不存在 PutObject 命令）

### RDS 创建耗时较长

- RDS MySQL 单机实例创建通常需要 5-10 分钟
- terraform apply 中 RDS 是最后一个创建的资源
- 属正常行为，无需干预

## 部署验证清单

**Terraform 层验证：**
- [ ] `terraform version` 在 1.7.5 ~ 1.10.5 区间内
- [ ] `terraform fmt -check -recursive` PASS
- [ ] `terraform validate` PASS
- [ ] `terraform init` PASS（OBS backend 配置成功，provider 下载成功）
- [ ] `terraform plan` PASS（⚠️ 必须通过——init 成功不等于 state 读写正常，plan 会实际访问 OBS backend）
- [ ] `terraform apply` PASS（全部资源创建成功，**state 成功写入 OBS，无 errored.tfstate 文件生成**）
- [ ] 工作目录下**不存在** `errored.tfstate` 文件（存在则说明 state 写入失败，需排查版本/配置）
- [ ] `terraform output -json` 获取完整资源清单（含 sensitive 密码）
- [ ] `save-inventory-to-obs.sh` 上传成功（或 obsutil cp 成功）
- [ ] OBS GET 验证 inventory.json 可读（HTTP 200）

**基础设施层验证（通过 terraform state / output 确认资源状态，不涉及应用层）：**
- [ ] VPC/子网/安全组状态为可用
- [ ] ECS 实例状态为 ACTIVE（运行中），EIP 已绑定
- [ ] RDS 实例状态为可用
- [ ] OBS bucket 创建成功

**不在验证范围内（⚠️ 应用层，SRE 不检查）：**
- ~~ECS 公网 80 端口可访问~~（应用是否启动是应用层问题）
- ~~RDS 3306 端口实际连接测试~~（应用层连接逻辑）
- ~~HTTP 状态码 200/500~~（应用页面是否正常）
- ~~SSH 登录 ECS 检查进程~~（应用运维问题）

## 资源清单 inventory.json 结构

```json
{
  "generated_at": "<时间戳>",
  "resources": {
    "ecs": {
      "id": "<ECS UUID>",
      "public_ip": "<公网 IP>",
      "admin_pass": "<ECS root 密码>"
    },
    "rds": {
      "id": "<RDS 实例 ID>",
      "private_ip": "<私网 IP>",
      "password": "<RDS root 密码>"
    }
  }
}
```

## 安全实践

1. AK/SK 通过环境变量传入，不写入任何文件/日志/评论
2. ECS/RDS 密码通过 TF_VAR_ 环境变量传入，variables.tf 中标记 `sensitive = true`
3. 临时凭证文件（如 /tmp/tf-deploy-env.sh）使用 `shred` 安全销毁
4. Terraform 远程状态存储于 OBS 私有 bucket
5. 安全组规则最小化：80/tcp 公网开放 + 3306/tcp 仅限 VPC 内网（10.0.0.0/16）
6. inventory.json 上传时设置 `x-amz-acl: private` header 双重保障
7. **敏感凭证获取方式**：
   - inventory.json 存储在 OBS 私有桶中，包含 ECS 密码、RDS 密码等敏感信息
   - Issue 评论中**只发布 OBS 桶名和对象路径**（如 `obs://bucket/inventory/inventory.json`），**不发布预签名 URL**
   - 维护者使用自己的 AK/SK 通过 obsutil 获取：`obsutil cp obs://bucket/inventory/inventory.json ./inventory.json`
   - 也可通过 `terraform output -json` 获取（需有 AK/SK 执行 terraform 命令）
8. ⚠️ **禁止在 Issue 评论、PR 评论或任何公开渠道发布 OBS 预签名 URL**——预签名 URL 任何人持有即可访问，等同于泄露密码

## 参考资源

- 华为云 Terraform Provider 文档：https://registry.terraform.io/providers/huaweicloud/huaweicloud/latest/docs
- OBS S3 兼容 API：https://support.huaweicloud.com/api-obs/obs_03_0152.html
- RDS Flavor 查询 API：https://support.huaweicloud.com/api-rds/rds_06_0005.html
