# 华为云 IaC 安全检查清单（QA Agent 用）

在通用安全检查（references/security-checklist.md）基础上，审查华为云 Terraform IaC PR 时增加以下专项检查。

## 凭证获取（QA 执行 terraform plan 时需要）

QA 执行 `terraform plan` 审查时需要 AK/SK 连接 OBS backend 和调用 provider API。AK/SK 由用户在【项目场景 → 点击单个场景 → 场景详情页 → 高级选项 → 自定义配置】添加 name=HUAWEI_AK/name=HUAWEI_SK 配置（平台按 name 直注同名 env；通用设置 HUAWEICLOUD_SDK_AK/SK 旧名兜底，与 SRE Agent 使用同一组凭证）。配置的变量名是 `HUAWEI_AK`/`HUAWEI_SK`，必须映射为 `HW_ACCESS_KEY`/`HW_SECRET_KEY`。仅用于 plan（只读），不执行 apply/destroy。

```bash
# 场景配置的 HUAWEI_AK/HUAWEI_SK → Terraform 期望的变量名映射
export HW_ACCESS_KEY="${HUAWEI_AK}"
export HW_SECRET_KEY="${HUAWEI_SK}"
export HW_REGION="cn-north-4"
export AWS_ACCESS_KEY_ID="$HW_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$HW_SECRET_KEY"
export AWS_DEFAULT_REGION="cn-north-4"
```

⚠️ 不要使用 obsutil 的 `~/.obsutilconfig` 凭证——那是另一套，不能用于 Terraform。

## 敏感信息检查

- [ ] AK/SK 未硬编码到任何 .tf 文件，仅通过环境变量（HW_ACCESS_KEY / HW_SECRET_KEY）传入
- [ ] ECS/RDS 密码未硬编码，通过 TF_VAR_ 环境变量传入
- [ ] variables.tf 中所有密码类变量标记 `sensitive = true`
- [ ] variables.tf 中密码变量无 default 值（强制运行时注入）
- [ ] terraform plan / state 输出中无明文密码
- [ ] 临时凭证文件（如 /tmp/tf-deploy-env.sh）使用 `shred` 销毁，非 rm

## OBS 远程状态安全

- [ ] Terraform 远程状态存储于 OBS 私有 bucket（非公开）
- [ ] inventory.json 上传时设置 `x-amz-acl: private` header
- [ ] OBS bucket 访问策略限制到指定 AK/SK
- [ ] **Issue/PR 评论中不包含 OBS 预签名 URL**（预签名 URL 任何人持有即可访问，等同于泄露密码）
- [ ] 敏感凭证只通过 OBS 桶名 + 对象路径告知，由维护者用 AK/SK 自行获取

## OBS Backend 配置审查（⚠️ 关键）

- [ ] backend 配置包含 `skip_s3_checksum = true`（TF 1.7~1.10 必需，缺失会导致 state 写入 OBS 失败）
- [ ] backend 配置包含 `skip_requesting_account_id = true`（TF 1.7+ 必需，跳过 AWS STS 调用）
- [ ] backend 配置包含 `skip_region_validation`、`skip_credentials_validation`、`skip_metadata_api_check`
- [ ] `endpoint` 值带 `https://` 前缀（如 `https://obs.cn-north-4.myhuaweicloud.com`）
- [ ] backend 配置**未使用** `force_path_style = true`（cn-north-4 OBS 要求 virtual-host-style）
- [ ] 如 .tf 文件或文档中指定了 Terraform CLI 版本约束，确认在 1.7.5 ~ 1.10.5 区间内

## 安全组规则审查

- [ ] 安全组规则最小化（最小权限原则）
- [ ] 80/tcp 公网开放需有明确业务理由
- [ ] 3306/tcp（数据库端口）禁止公网开放，仅限 VPC 内网（如 10.0.0.0/16）
- [ ] 无 0.0.0.0/0 开放非必要端口（如 22/tcp SSH、3306/tcp、6379/tcp Redis）

## cn-north-4 资源规格合规性

- [ ] RDS flavor 使用 cn-north-4 支持的类型（n1 / x1 / arm），非 c2
- [ ] RDS 存储类型使用可用类型（CLOUDSSD / LOCALSSD / ESSD），非 ULTRAHIGH
- [ ] 如 PR 引入新的资源规格，确认已通过 ListFlavors / ListStorageTypes API 验证可用性

## 可执行校验（只到 plan——硬约束⑦ 禁止 apply/destroy）

审查 PR 时执行以下校验（QA 只到 plan；apply 及部署后验证属 SRE 职责，见 iac-sre）：

- [ ] `terraform version` 在 1.7.5 ~ 1.10.5 区间内
- [ ] `terraform fmt -check -recursive` PASS
- [ ] `terraform validate` PASS
- [ ] `terraform init` PASS（OBS backend 配置成功，provider 下载成功）
- [ ] `terraform plan` PASS（⚠️ 不加 -backend=false，使用真实 OBS backend 验证 state 读写；plan 还会调用 provider API 验证镜像/flavor/规格是否存在）
- [ ] plan 输出中无报错（如镜像不存在、RDS flavor 不可用、provider 不兼容等）

**SRE 部署后验证项（QA 不执行不检查——apply 后产物）：** errored.tfstate 不存在、`terraform output -json` 资源清单、inventory.json 上传 OBS、ECS ACTIVE、RDS 可用、OBS bucket 创建成功。

**不在验证范围内（应用层，SRE/QA 不检查）：**
- ~~ECS 公网 80 端口可访问~~（应用是否启动是应用层问题）
- ~~RDS 3306 端口实际连接测试~~（应用层连接逻辑）
