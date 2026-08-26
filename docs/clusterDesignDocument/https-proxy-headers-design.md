# HTTPS 代理头 + Cookie 安全改造方案

## 背景

生产环境 nginx HTTPS(443) → proxy_pass http://127.0.0.1:3000 → uvicorn。

nginx 没传 X-Forwarded-Proto → uvicorn 认为自己是 HTTP → 后端生成的所有 URL 都是 http:// → 浏览器在 HTTPS 页面加载 HTTP 资源被 Mixed Content 拦截。

同时 refresh_token cookie 的 secure=False → cookie 在 HTTP 和 HTTPS 都发送 → 生产环境 HTTPS 应该 secure=True。

## 问题分析

### 问题链条

```
nginx HTTPS(443) → proxy_pass http://127.0.0.1:3000 → uvicorn

nginx 没传 X-Forwarded-Proto → uvicorn 认为自己是 HTTP
→ FastAPI request.url.scheme = "http"
→ 后端生成的所有 URL 都是 http://
→ 浏览器在 HTTPS 页面加载 HTTP 资源 → Mixed Content 拦截

cookie secure=False → refresh_token 在 HTTP 和 HTTPS 都发送
→ 生产环境 HTTPS 应该 secure=True 只在 HTTPS 发送
```

### 影响场景

| 场景 | 影响 |
|------|------|
| OAuth 回调 redirect_uri | http:// → 浏览器拒绝（HTTPS 页面跳 HTTP） |
| OBS 下载链接 | http:// → Mixed Content |
| 静态资源绝对 URL | http:// → Mixed Content |
| refresh_token cookie | secure=False → 不安全，应该在 HTTPS only |
| FastAPI /docs | http:// → 不影响生产但影响调试 |

## 修复方案

需要修 2 个代码文件 + 1 个运维配置：

### 1. Dockerfile CMD 加 --proxy-headers（代码）

```dockerfile
# 当前
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000"]

# 改后
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers", "--forwarded-allow-ips=*"]
```

- `--proxy-headers`：让 uvicorn 读 nginx 传的 X-Forwarded-Proto → request.url.scheme 正确返回 https
- `--forwarded-allow-ips=*`：允许所有来源的 X-Forwarded-* header（nginx 在同一台机器）
- 不影响 HTTP 部署：nginx 没传 X-Forwarded-Proto 时 uvicorn 认为是 HTTP，自动适配

### 2. auth.py cookie secure 改为环境变量控制（代码）

```python
# config.py 新增
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")
```

```python
# auth.py cookie
secure=COOKIE_SECURE,  # 从 secure=False 改为环境变量控制
```

- 默认 `true`（生产 HTTPS）：不配环境变量 → secure=True → cookie 只在 HTTPS 发送
- 开发环境 `false`：`.env` 里配 `COOKIE_SECURE=false` → secure=False → HTTP 也能用
- `.env` 文件通过 systemd `EnvironmentFile` 加载，`os.environ.get` 能读到

环境变量配置方式：
```
# .env 文件
COOKIE_SECURE=false    # 开发环境 HTTP
COOKIE_SECURE=true     # 生产环境 HTTPS（默认，不配也是这个）
```

### 3. nginx 配置加 X-Forwarded-Proto（运维配置，不在代码仓）

```nginx
# nginx 配置每个 location 块加：
proxy_set_header X-Forwarded-Proto $scheme;
```

- nginx 把协议（http/https）传给后端
- 没有 nginx 传 header，uvicorn --proxy-headers 也读不到
- 这是运维配置，部署时由运维在服务器上改，不进代码仓

## HTTP/HTTPS 双模式兼容

| 部署方式 | COOKIE_SECURE | --proxy-headers | nginx X-Forwarded-Proto | cookie 行为 |
|---------|:---:|:---:|:---:|------|
| 生产 HTTPS | true（默认） | ✅ | ✅ | 只在 HTTPS 发送 ✅ |
| 开发 HTTP | false | ✅ | 不传 | HTTP 也发送 ✅ |
| Docker HTTPS | true（默认） | ✅（CMD 里） | ✅ | 只在 HTTPS 发送 ✅ |
| systemd HTTPS | true（默认） | ✅（unit 里） | ✅ | 只在 HTTPS 发送 ✅ |

## 改动文件

| # | 文件 | 改动 | 类型 |
|---|------|------|------|
| 1 | Dockerfile | CMD 加 --proxy-headers --forwarded-allow-ips=* | 代码 |
| 2 | src/config.py | 新增 COOKIE_SECURE 环境变量 | 代码 |
| 3 | src/api/routes/auth.py | cookie secure=False → secure=COOKIE_SECURE | 代码 |
| 4 | .env.example | 加 COOKIE_SECURE 示例 | 代码 |
| 5 | nginx 配置 | 加 proxy_set_header X-Forwarded-Proto $scheme; | 运维 |

## 不需要改的

- systemd unit：115 那台已有 --proxy-headers ✅
- 本机 grape-main.service：已有 --proxy-headers ✅
- docker-compose.yml：用 Dockerfile CMD，不需要改
- auth.py 第 113 行手动读 X-Forwarded-Proto：保留（双重保险）
