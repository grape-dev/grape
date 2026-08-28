# REST API 调研指南（OpenAPI → Markdown → 探测 三级回退）

## 调研顺序

对每个功能点按序调研，找到可用接口即停止：

### 1. OpenAPI/Swagger 文档（优先）

用户提供 OpenAPI 3.0 / Swagger 2.0 文档（JSON 或 YAML）：

```bash
# 解析 OpenAPI 文档，提取所有端点
python3 -c "
import json, sys
spec = json.load(open(sys.argv[1]))
for path, methods in spec.get('paths', {}).items():
    for method, info in methods.items():
        if method in ('get','post','put','patch','delete'):
            op_id = info.get('operationId', '')
            summary = info.get('summary', '')
            params = [p.get('name') for p in info.get('parameters', [])]
            print(f'{method.upper()} {path}  op={op_id}  summary={summary}  params={params}')
" openapi.json
```

- 解析得到全部端点（method + path + 参数 + 响应结构）
- 每个端点生成一个子命令
- 如果文档有 operationId → 优先用 operationId 作为子命令名
- 如果没有 operationId → 按子命令生成规则推导（见 SKILL.md Step 3）

### 2. Markdown 文档（回退）

用户提供 Markdown 格式的 API 文档（手写描述）：

agent 阅读文档，提取以下信息：
- 端点列表（method + path）
- 每个端点的参数（query/path/body）
- 每个端点的响应结构
- 认证方式

提取后生成端点列表，与 OpenAPI 模式相同的方式生成子命令。

### 3. curl 探测（最后回退）

用户提供 API 基础信息（base_url + 口头描述端点）：

> base_url 优先从项目知识（.project-info/member/ 目录）获取，缺失时从环境变量 $${PREFIX}_BASE_URL 获取。如两者都无，提示用户提供。

```bash
# 探测 API 可达性（PREFIX 由 skill name 推导，端点根据用户描述替换）
curl -s --max-time 30 -u "$${PREFIX}_USERNAME:$${PREFIX}_PASSWORD" \
  "$${PREFIX}_BASE_URL/" | python3 -m json.tool

# 探测用户描述的端点（示例：/api/posts /api/articles——实际端点由用户提供）
curl -s --max-time 30 -u "$${PREFIX}_USERNAME:$${PREFIX}_PASSWORD" \
  "$${PREFIX}_BASE_URL/{用户描述的端点}" | python3 -m json.tool
```

- 探测成功 → 记录端点 + 响应结构
- 探测失败（404）→ 标记该端点 ⛔
- 用户确认端点 → 生成子命令

## 禁猜规则

API 端点只认以下可信来源：
1. OpenAPI/Swagger 文档的 paths 定义
2. Markdown 文档中用户明确描述的端点
3. curl 探测返回 200 的端点

严禁按命名模式猜测/推断（如从 /api/posts 推断 /api/comments）。

## 时间盒

每端点调研 ≤5 分钟。超时标记 ⛔，不无限循环搜索。

## 失败门控

所有端点都标记 ⛔ → 终止流程，不生成任何文件。

## 认证探测

目标系统认证方式（需求澄清时确认，支持四种）：

```bash
# 无认证探测（公开 API）
curl -s --max-time 30 "$${PREFIX}_BASE_URL/api/" -w "\n%{http_code}"

# Basic Auth 探测
curl -s --max-time 30 -u "$${PREFIX}_USERNAME:$${PREFIX}_PASSWORD" \
  "$${PREFIX}_BASE_URL/api/" -w "\n%{http_code}"

# Bearer Token 探测
curl -s --max-time 30 -H "Authorization: Bearer $${PREFIX}_TOKEN" \
  "$${PREFIX}_BASE_URL/api/" -w "\n%{http_code}"

# API Key（Header）探测
curl -s --max-time 30 -H "X-API-Key: $${PREFIX}_API_KEY" \
  "$${PREFIX}_BASE_URL/api/" -w "\n%{http_code}"
```

- 200 → 认证成功，API 可用
- 401 → 凭据无效
- 403 → 凭据无权限
- 404 → 端点不存在
- 超时 → API 不可达
- PREFIX 由 skill name 推导（如 blog-manager → BLOG_MANAGER）

## 常见坑

- REST API 路径前缀可能不是 /api（如 /v1 /v2 /rest）
- 分页参数可能不是 page/size（可能是 offset/limit / cursor）
- 响应结构可能是 {data: [...]} 包裹（不是直接返回数组）
- DELETE 操作可能需要 body（如批量删除）
- PUT vs PATCH：PUT 全量更新，PATCH 部分更新——生成的脚本参数不同
