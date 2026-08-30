# hcloud CLI 安装认证指南

## 安装

```bash
# pip 安装
pip install hcloud

# 或二进制安装（推荐）
# 从 https://console.huaweicloud.com/cli/ 下载
```

## 认证配置

```bash
hcloud configure set --access-key=<AK> --secret-key=<SK> --region=cn-north-4
hcloud configure list
```

## AK/SK 获取

- 项目知识：.project-info/ 下 JSON 文件 secrets.HUAWEI_AK / secrets.HUAWEI_SK
- 环境变量：HUAWEI_AK / HUAWEI_SK
- 华为云控制台：我的凭证 → 访问密钥

## 常见问题

- `Failed to obtain project ID`：AK/SK 无效
- `[USE_ERROR]`：参数格式用 --param=value
- `403`：缺少 ECS ReadOnlyAccess 权限
