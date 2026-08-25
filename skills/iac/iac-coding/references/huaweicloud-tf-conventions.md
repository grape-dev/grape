# 华为云 Terraform 编码规范与避坑指南（Coding Agent 用）

## 目录结构规范

所有 TF 代码和脚本文件统一放在仓库的 `iac/` 子目录下，不得放在仓库根目录或其他目录。如仓库中不存在 `iac/` 目录，新建该目录。

```
repo/
├── iac/                    # IaC 代码根目录
│   ├── versions.tf         # Provider 版本约束 + backend 配置
│   ├── providers.tf        # Provider 配置
│   ├── variables.tf        # 变量定义
│   ├── main.tf             # 资源定义（VPC/ECS/RDS/安全组等）
│   ├── outputs.tf          # 输出
│   ├── inventory.tf        # 资源清单生成
│   ├── Makefile            # 命令封装
│   ├── scripts/            # 脚本
│   └── tests/              # 验证测试
├── src/                    # 项目源代码（不混入 TF 文件）
└── ...
```

## 凭证设置规范

编码时必须遵循以下凭证约定，绝不硬编码到 .tf 文件：

### AK/SK 来源

华为云 AK/SK 从 Grape 控制台获取（与 SRE Agent 使用同一组凭证）。Coding 执行 `terraform plan` 时需要 AK/SK 调用 provider API 验证资源规格，但**仅用于 plan（只读），不执行 apply/destroy**。

```bash
# 从 Grape 控制台获取 AK/SK 后设置环境变量
export HW_ACCESS_KEY="<AK>"
export HW_SECRET_KEY="<SK>"
export HW_REGION="cn-north-4"
export AWS_ACCESS_KEY_ID="$HW_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$HW_SECRET_KEY"
export AWS_DEFAULT_REGION="cn-north-4"
```

⚠️ AK/SK 不得写入任何 .tf 文件、日志、评论或 PR body。仅在环境变量中使用。

### 环境变量名

| 用途 | 环境变量 | 说明 |
|------|---------|------|
| 华为云 AK | `HW_ACCESS_KEY` | Provider 认证（走 IAM token） |
| 华为云 SK | `HW_SECRET_KEY` | Provider 认证（走 IAM token） |
| 区域 | `HW_REGION` | 如 cn-north-4 |
| AWS 兼容 AK | `AWS_ACCESS_KEY_ID` | OBS S3 backend 认证（走 SigV4），与 HW_ACCESS_KEY 同值 |
| AWS 兼容 SK | `AWS_SECRET_ACCESS_KEY` | 同上，与 HW_SECRET_KEY 同值 |
| AWS 兼容 Region | `AWS_DEFAULT_REGION` | 同上，与 HW_REGION 同值 |
| ECS 密码 | `TF_VAR_ecs_admin_pass` | 8-26位，含大小写+数字+特殊字符 !@$%^-_=+ |
| RDS 密码 | `TF_VAR_rds_password` | 8-32位，含大小写+数字+特殊字符 |

⚠️ **两套凭证是同一条 AK/SK**，设两套变量是因为 huaweicloud provider 读 `HW_*`、Terraform S3 backend 读 `AWS_*`。两条认证路径完全独立（provider 走 IAM token，backend 走 SigV4）。

### TF variables.tf 规范

- 所有密码类变量必须标记 `sensitive = true`
- 密码变量通过 `TF_VAR_` 环境变量传入，不设 default 值
- AK/SK 不写入 variables.tf，由 provider block 通过环境变量自动读取

## cn-north-4 编码避坑

### RDS Flavor 选择

cn-north-4 仅支持 `n1` / `x1` / `arm` 类型 RDS flavor，不支持 `c2` 类型。

编写 variables.tf 时默认值选择：

| 错误默认值 | 正确默认值 | 规格 |
|-----------|-----------|------|
| rds.mysql.c2.large.2 | rds.mysql.n1.large.2 | 2c4g |
| rds.mysql.c2.large.4 | rds.mysql.n1.large.4 | 2c8g |

### RDS 存储类型选择

| 类型 | cn-north-4 状态 | 编码建议 |
|------|----------------|---------|
| ULTRAHIGH | 已弃用（报 DBS.280241） | 禁止使用 |
| CLOUDSSD | 可用 | 推荐设为默认值 |
| LOCALSSD | 可用 | 可选 |
| ESSD | 可用 | 可选 |

### OBS 操作工具选择

编码时涉及 OBS 对象操作，使用 obsutil 而非 curl SigV4 或 hcloud CLI：

| 操作 | hcloud（不存在） | obsutil（正确） |
|------|-----------------|----------------|
| 上传对象 | PutObject | cp |
| 列举对象 | ListBuckets | ls |
| 创建桶 | CreateBucket | mb |

### OBS SigV4 日期格式

如必须用 curl 签名（不推荐），日期格式必须无连字符：

```bash
# 正确（无连字符）
date -u +"%Y%m%dT%H%M%SZ"        # 20260820T154154Z

# 错误（含连字符，OBS 返回 HTTP 400）
date -u +"%Y-%m-%dT%H:%M:%SZ"
```

### 安全组规则编码规范

- 80/tcp 公网开放（0.0.0.0/0）
- 3306/tcp 仅限 VPC 内网（10.0.0.0/16）
- 遵循最小权限原则，不开放非必要端口
- inventory.json 上传时设置 `x-amz-acl: private` header

## 参考资源

- 华为云 Terraform Provider 文档：https://registry.terraform.io/providers/huaweicloud/huaweicloud/latest/docs
- OBS S3 兼容 API：https://support.huaweicloud.com/api-obs/obs_03_0152.html
- RDS Flavor 查询 API：https://support.huaweicloud.com/api-rds/rds_06_0005.html

## Provider 版本约束规范

生成 Terraform 代码时，`required_providers` 块中 huaweicloud provider 的版本约束遵循以下规则：

- ✅ 正确写法：不写版本约束（使用最新版），或写 `>= 1.50`（下限约束，允许高版本）
- ❌ 禁止写法：`~> 1.50`、`~> 1.60` 等 `~>` 上限约束——只匹配 minor 版本段，实际安装的 v1.94.0 会被排除导致 `terraform init` 下载 provider 失败

示例（正确）：
```hcl
terraform {
  required_providers {
    huaweicloud = {
      source = "huaweicloud/huaweicloud"
    }
  }
}
```

示例（错误，禁止）：
```hcl
terraform {
  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = "~> 1.60"  # ❌ 匹配不到 v1.94.0
    }
  }
}
```

## OBS S3 Backend 配置规范

生成 Terraform 代码时，OBS 远程状态后端（S3 兼容模式）的配置必须遵循以下规则。

### Terraform CLI 版本约束（⚠️ 关键）

OBS S3 backend 与 Terraform CLI 版本存在严格兼容性窗口（基于实测验证）：

| TF 版本 | 兼容性 | 原因 |
|---------|--------|------|
| 1.5.x | ✅ 可用 | aws-sdk-go v1，签名兼容（无需 skip 参数） |
| 1.6.x | ❌ 不可用 | 要求 STS account ID 但不支持 skip 参数（断层） |
| **1.7.5 ~ 1.10.5** | ✅ **推荐** | `skip_s3_checksum=true` 有效 |
| 1.11.0+ | ❌ 不可用 | `skip_s3_checksum` 被接受但无效，SHA256 仍不匹配 |

### Backend 配置模板（TF 1.7.5 ~ 1.10.5）

```hcl
terraform {
  backend "s3" {
    bucket                      = "<bucket-name>"
    key                         = "<project>/terraform.tfstate"
    region                      = "cn-north-4"
    endpoint                    = "https://obs.cn-north-4.myhuaweicloud.com"
    skip_region_validation      = true
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true   # 1.7+ 必需，跳过 AWS STS 调用
    skip_s3_checksum            = true   # 1.7~1.10 必需，绕过 SHA256 不匹配
  }
}
```

### 必须包含的 skip 参数

| 参数 | 作用 | 缺失后果 |
|------|------|---------|
| `skip_requesting_account_id` | 跳过 AWS STS GetCallerIdentity 调用 | init 失败（华为云无 STS 服务） |
| `skip_s3_checksum` | 不发送 CRC32/SHA256 校验头 | apply 时 PutObject 返回 400 |
| `skip_region_validation` | 跳过 AWS region 格式校验 | init 失败（非 AWS 标准 region 名） |
| `skip_credentials_validation` | 跳过 AWS 凭证预校验 | init 失败（非 AWS 标准 AK/SK） |
| `skip_metadata_api_check` | 跳过 AWS metadata API 探测 | init 慢或超时 |

### 禁止的配置

| 配置 | 原因 |
|------|------|
| `force_path_style = true` | cn-north-4 OBS 要求 virtual-host-style，path-style 返回 403 VirtualHostDomainRequired |
| `endpoint` 不带 `https://` 前缀 | TF 1.6+ 校验 endpoint 必须是完整 URL，返回 unsupported protocol scheme |
| `~> 1.x` 版本上限约束 | 匹配不到高版本 provider，导致 init 下载失败 |

## 国内镜像源配置

worker 机器在国内网络环境下，默认从 `registry.terraform.io` / `releases.hashicorp.com` 下载 Terraform CLI 和 provider 经常超时。编码时生成的 Makefile 或部署脚本中应包含华为云镜像源配置：

### Provider 镜像源（~/.terraformrc）

⚠️ 华为云镜像不包含所有 provider（如 `hashicorp/local`、`hashicorp/null`），必须使用 `network_mirror` + `direct` 混合配置：

```bash
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
export TF_CLI_CONFIG_FILE="$HOME/.terraformrc"
```

混合配置说明：
- `network_mirror`：优先从华为云镜像下载所有 provider
- `direct`：华为云镜像没有的 provider（如 hashicorp/local、hashicorp/null）从官方 registry 下载
- `exclude`：避免 huaweicloud provider 回退到官方 registry（官方源国内访问慢）

### Terraform CLI 镜像源

安装/升级 Terraform 二进制时从华为云镜像下载：
- `https://mirrors.huaweicloud.com/terraform/`

### Plugin Cache

配合镜像源使用，避免重复下载：
```bash
export TF_PLUGIN_CACHE_DIR="$HOME/.terraform.d/plugin-cache"
mkdir -p "$TF_PLUGIN_CACHE_DIR"
```
