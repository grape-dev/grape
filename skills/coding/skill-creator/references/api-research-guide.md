# API 调研指南（CLI→SDK→OpenAPI 三级回退）

## 调研顺序

对每个功能点按序调研，找到可用接口即停止：

### 1. CLI（优先）

```bash
hcloud <Service> <Operation> --help
```

- Service 名首字母大写（如 ECS、VPC、BSS）
- Operation 名 PascalCase（如 ListServers、ShowServer）
- 命令存在且参数有效 → 用 CLI 模式

### 2. SDK（回退）

```bash
# 找包安装路径
python3 -c "import huaweicloudsdk{service}.v2 as m; import os; print(os.path.dirname(m.__file__))"

# 读所有 _http_info 方法（看全部 API 端点）
grep "_http_info" <path>/{service}_client.py

# 查特定方法的 API 路径
grep -A8 "_{method}_http_info" <path>/{service}_client.py
# 返回的 "resource_path" 值就是真实 REST 端点
```

- SDK 包可导入 → 用 SDK 模式
- 方法有 _http_info → 记录真实 API 端点
- 方法无 _http_info → 标记 ⛔，不推断

### 3. OpenAPI（最后回退）

- API Explorer：https://console.huaweicloud.com/apiexplorer
- 用户搜索确认端点 → 用 API 模式
- 用户无法提供 → 标记 ⛔

## 禁猜规则

API 端点只认以下可信来源：
1. SDK 源码 `_http_info` 的 `resource_path`
2. API Explorer 用户确认

严禁按命名模式猜测/推断（如从 coupons 推断 claim-vouchers）。

## 时间盒

每功能点调研 ≤5 分钟。超时标记 ⛔，不无限循环搜索。

## 失败门控

所有功能点都标记 ⛔ → 终止流程，不生成任何文件。

## 常见坑

- BSS 全局服务 → SDK 初始化须 GlobalCredentials + with_endpoints（BasicCredentials 会 400）
- BSS limit 参数最大 100（不是默认 200）
- SDK 有方法但 _http_info 无 resource_path → 该 API 不存在，不推断
