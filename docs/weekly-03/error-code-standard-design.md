# Grape API 错误码统一设计方案

> 目标：为 grape 对外 API 的错误返回建立**统一的错误码契约**——统一错误响应结构、全量状态码/错误码字典、
> 逐端点错误表模板，收敛当前「结构基本统一但格式有零星差异、无错误码枚举、文档与代码码表不同步」的现状。
> 现状分析见 §一；本方案为**设计（待实施）**。
> 分支：`weekly-02-lyz`；状态：设计草案；日期：2026-09-14

---

## 一、现状分析（已核对代码）

### 1.1 已有约定（大部分统一）

**代码中高度统一**：`src/api/routes/*.py` 的 `HTTPException` 几乎全部使用
`detail={"code": <int>, "message": "<中文>"}` 结构（抽查 400+ 处均为此形）。

**README 有状态码表 + 错误格式**（`help/api/README.md`）：
- 状态码：200/201/204/400/401/403/404/409/429
- 错误格式：`{"detail": {"code": 401, "message": "..."}}`

**实际状态码覆盖面（代码实测）**：
`400(153) · 404(109) · 403(44) · 502(43) · 500(35) · 409(29) · 401(9) · 503(7) · 302(3) · 410(1) · 200(8)`

### 1.2 不一致/缺口

| # | 缺口 | 现状证据 |
|---|------|----------|
| 1 | **401 格式文档过时** | README 写 `{"message":"401 Unauthorized"}`，但代码实际为 `{"code":401,"message":"未登录"}`（deps.py） |
| 2 | **两类错误返回并存** | `HTTPException(detail={"code","message"})` vs 6 处 `return {"ok":False,"code":N,"message":"..."}`（tasks 类，CLI 上传返回体） |
| 3 | **无错误码枚举统一** | code→message 全裸写中文串，无 `errors.py`/枚举常量 |
| 4 | **文档码表不全** | README 状态码表未覆盖 500/502/503/410；各 help/api/*.md「返回」行是叙述性罗列，无统一错误码字典 |
| 5 | **`code` 语义口径** | 多数场景 `detail.code == HTTP 状态码`，但部分业务语义码（如 `{"code":502}` 表"平台失败"）边界未文档化 |

---

## 二、目标：统一错误码契约

### 2.1 统一错误响应格式（HTTP 错误）

```json
{
  "detail": {
    "code": 404,
    "message": "资源不存在",
    "internal": ""        // 可选：内部错误码/追踪信息，不对外暴露敏感信息
  }
}
```

约定：
- `detail.code`：**默认等于 HTTP 状态码**；业务需要细分时使用 **4 位业务子码**（如 `40401` = 资源不存在细分 1）。
- `detail.message`：人类可读中文（对外）。
- 401 统一为 `{"code":401,"message":"未登录|会话 token 无效|API Key 无效或已吊销"…}`，**废除 README 旧的 `{"message":"401 Unauthorized"}`**。

### 2.2 CLI/上传类业务返回（非 HTTPException）

保留 `{"ok": False, "code": N, "message": "..."}`（任务/notify 等 CLI 语义端点，body 带 ok 标志便于命令判断）：
- **明确其定位**：仅用于「HTTP 200 但业务失败」的端点（如 `tasks/notify` 返回 `ok:false`）。
- `code` 也遵循 2.1 的 code 口径（默认 = HTTP 状态码语义）。

### 2.3 状态码字典（全面）

| 码 | 含义 | 典型来源 |
|----|------|----------|
| 200 | 成功 | 读取/修改 |
| 201 | 创建成功 | POST 资源 |
| 204 | 删除成功（无体） | DELETE |
| 302 | 重定向（OAuth 回调） | login 回调 |
| 400 | 参数缺失/校验失败/业务非法 | Query/Body 校验、状态非法迁移 |
| 401 | 未认证 | 缺 token / 会话失效 / API Key 失效 |
| 403 | 无权限 | session 型端点禁 tk_、token 作用域不符、白名单外 |
| 404 | 资源不存在 | issue/seq/task 不存在 |
| 409 | 冲突 | 同标题已存在、重复提交 |
| 410 | 已废弃（端点移除） | tasks/create 等已下线 |
| 429 | 请求过频 | 限流 |
| 500 | 服务内部错误 | 建档失败等 |
| 502 | 上游失败（平台/GitCode/GitHub） | GitCode API 调用失败、平台 token 解析失败 |
| 503 | 服务不可用 | 引擎不可用等 |

### 2.4 错误码枚举实现（建议）

新增 `src/api/errors.py`：

```python
# 状态码常量
OK = 200; CREATED = 201; NO_CONTENT = 204
BAD_REQUEST = 400; UNAUTHORIZED = 401; FORBIDDEN = 403; NOT_FOUND = 404
CONFLICT = 409; GONE = 410; TOO_MANY = 429
INTERNAL = 500; BAD_GATEWAY = 502; SERVICE_UNAVAILABLE = 503

# 统一工厂（替代散落 HTTPException）
def api_error(status: int, message: str, internal: str = "") -> HTTPException:
    detail = {"code": status, "message": message}
    if internal:
        detail["internal"] = internal
    return HTTPException(status_code=status, detail=detail)

def business_ok_false(code: int, message: str) -> dict:
    """CLI 类业务失败返回体（HTTP 200 但 ok=false）。"""
    return {"ok": False, "code": code, "message": message}
```

### 2.5 逐端点错误表模板（补 help/api/*.md）

每个接口文档统一增加「错误返回」小节（示例）：

| 码 | 说明 | 失败场景 |
|----|------|----------|
| 400 | 参数缺失/校验失败 | title 缺 / 非法状态迁移 |
| 403 | 无权限 / 会话 token 不适用 | 账户管理端点用 tk_ |
| 404 | 资源不存在 | issue #N 不存在 |

---

## 三、实施步骤（分阶段，可独立)

| 阶段 | 内容 | 产出 |
|------|------|------|
| P1 | 新增 `src/api/errors.py`（常量 + api_error 工厂 + business_ok_false） | 错误统一能力 |
| P2 | `deps.py` 401 报文对齐（读 README 过时格式删除）；路由内部改 `api_error(...)` 替换散落 HTTPException（可选，批量） | 代码统一 |
| P3 | README 状态码表补全（500/502/503/410/302）+ 删除过时 401 写法 + 明确两套返回体边界 | 契约文档 |
| P4 | 各 `help/api/*.md` 增补「错误返回」表（模板 §2.5） | 逐端点错误契约 |
| P5 | `custom_openapi` 注入标准错误 responses schema（可选，增强契约） | OpenAPI 级错误描述 |

> 说明：P2 批量替换为 refractor 收益（降低维护），非强制；P1/P3 是安全基线（不改行为、仅加能力与文档）。

---

## 四、约定与原则

1. **`detail.code` 默认 = HTTP 状态码**；需要细分用 **4 位子码**（`40xxx` 业务语义段）。
2. **HTTP 错误用 `HTTPException(detail={"code","message"})`**；**CLI 业务失败用 `{"ok":false,...}`**，两者边界 =「HTTP 层错误 vs 业务层返回」。
3. **对外 message 中文可读**，敏感信息（token/内网地址/堆栈）不外泄（internal 字段或日志）。
4. **契约先行**：README 为权威错误码字典，逐端点文档引用它，杜绝叙述性重复漂移。
5. **向后兼容**：不改变现有 200/400/4xx 行为；仅规范结构与文档。

---

## 五、影响面

| 项 | 影响 |
|----|------|
| 代码 | `errors.py` 新增；可选批量替换 HTTPException 工厂（行为不变） |
| 文档 | README 状态码表 + 401 修正；13 个 help/api/*.md 增错误表 |
| 测试 | 新增 `test_errors.py`（格式断言）+ 既有回归 |
| CLI | 错误返回体保持兼容（ok:false 结构不变），仅规范 code 口径 |

---

## 六、验证

1. `pytest tests/test_errors.py`（新增）：断言代表端点 401/403/404 返回体为 `{"detail":{"code","message"}}`。
2. 抽样扫描 `src/api/routes/*.py`：`detail` 键含 `code`+`message` 比例 → ≥99%（旧散落除外）。
3. help/api 文档错误表与代码 state 一致（人工抽查 tasks/issues/git-repo）。
4. 全量回归。