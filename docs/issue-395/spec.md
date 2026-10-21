# Spec: Issue #395 — #380~#383 四项缺陷集中修复（AT-KNW-06 / AT-ORGP-03 / AT-A2A-03 / AT-REPO-06）

> 来源：Issue #395 + VOD Agent 收敛评论+ 各子 Issue Spec（#380/#381/#382/#383 评论）| 目标分支：功能分支（GRAPE_WORK_BRANCH 注入值，含对应基线分支）

## 需求
一次性修复 #380~#383 四项已定位根因的缺陷，使 `grape-test` 原子测试
AT-KNW-06 / AT-ORGP-03 / AT-A2A-03 / AT-REPO-06 由 xfail 转 XPASS。

## 四项缺陷与根因（均已在子 Issue 中实测/核对）

| # | 缺陷 | 根因 | 优先级 |
|---|------|------|--------|
| #380 | `GET /api/knowledge` 越界分页（page_size=-1）返回 500 并回显 MySQL 1064 原文 | `src/api/routes/knowledge.py` list_knowledge 的 page/page_size Query 缺 ge/le 校验，负数 LIMIT 在 MySQL 语法错（SQLite 合法故单测隐身） | P1 |
| #381 | PUT `/api/orgs/{任意 id}` 返回 200 ok（读 403 写 200 语义矛盾，越权） | `src/api/deps.py` require_org_member 用 resolve_org_context 回退个人组织，不校验路径 org_id；orgs.py update_organization 对 update_org 返回值不判断 | P0 |
| #382 | POST /api/a2a 传 repository 为 dict → 500 AttributeError | `src/api/routes/webhook.py` a2a_endpoint 前置校验 `str(payload.get("repository"))` 字符串化绕过；:692 `repository.startswith` 对 dict 抛 AttributeError | P1 |
| #383 | 托管不存在仓库返回 502 且回显 GitCode 内部 API URL | `src/api/routes/repos.py` host_repo 把上游 RuntimeError 一律映射 502 并拼接原文 | P2 |

## 期望行为（验收标准）

### #380（AT-KNW-06）
- `page_size=-1` / `page=0` / `page_size=0` / `page_size=201` → 422
- 合法分页参数行为不变；DB 异常原文不进响应 message（全局兜底脱敏）

### #381（AT-ORGP-03）
- PUT `/api/orgs/{不存在 id}` → 404
- PUT/DELETE `/api/orgs/{他人组织 id}` → 403
- update_org/delete_org 影响行数=0 → 不报成功
- 非路径接口（X-Org-ID 头等）行为保持不变

### #382（AT-A2A-03）
- repository 为 dict/int/None/list 等非 str → 400（无 5xx）
- 正常 str repository（owner/repo、github: 前缀）不误拒

### #383（AT-REPO-06）
- 上游 404 → 404「仓库不存在或无权访问」，响应体不含 gitcode.com/api
- 上游 401/403 → 403「无权限托管，请联系管理员」
- 上游 5xx/网络异常 → 5xx 固定文案「托管服务暂时不可用，请稍后重试」，原文仅落服务端日志

## 范围
- In：src/api/routes/knowledge.py（分页校验）、src/api/deps.py + routes/orgs.py + org_db.py（org_id 绑定 + rows_affected）、src/api/routes/webhook.py（a2a repository 类型校验）、src/api/routes/repos.py（host_repo 异常分流）、src/api/main.py（全局异常脱敏）、tests/（四组回归测试）
- Out：分页协议重构、鉴权框架重构、A2A 协议变更、前端 UI、host_repo 之外端点错误处理

## 验收
- [ ] 四项修复各自单测通过（新增回归测试）
- [ ] 既有测试无回归（相关测试文件全绿）
- [ ] AT-KNW-06 / AT-ORGP-03 / AT-A2A-03 / AT-REPO-06 可由 xfail 转 XPASS（原子测试在 grape-test 仓，本仓提供修复）
