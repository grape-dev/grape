# 一键部署闭环 — 设计文档

> 需求文档：oneclick-host-heal-requirements.md（场景与用例清单）

## 问题

AI 代理一键部署 Grape 到平台托管环境（华为云 workspace / 容器 / bwrap 沙箱）
时暴露两类静默故障：

1. **地址错误**：部署方常关闭公网探测（GRAPE_AUTO_DETECT_URL=false，探测慢
   且在托管环境拿不到平台签发域名）→ meta 空值 → get_server_base_url() 兜底
   http://localhost:3000 → OAuth 回调 / A2A / 前端接入静默失败。
2. **引擎不可见**：discover_backends 走 PATH 扫描，沙箱内 7 个引擎 CLI 全
   NOT FOUND，引擎列表只剩默认 opencode 离线记录，用户无从接入。

## 设计决策

### D1：Host 头自愈（地址错误）— 改检测，不改行为

复用 Issue #372 §14.2.2 既有机制（main.py 认证中间件从 Host 头推导
webhook_url），补全 server_base_url：值为空或本地无效值（localhost/127.x/
0.0.0.0/[::1]）时写入推导的公网基址。

- 为什么不改探测/不强制 fail-fast：探测在托管环境本来就拿不到正确答案，
  fail-fast 会打死合法的本地部署。Host 头是用户访问时天然携带、必然正确的
  信号——"客人上门直接看门牌"，不打电话问电信局。
- 安全边界全继承：_uid 认证门禁 + detect_webhook_from_host 本地排除 +
  TRUSTED_HOSTS 白名单，零新增信任面。有效公网地址不覆盖（幂等）。
- GitHub 复用同一基址（db.py 注册时 /webhook/gitcode → /webhook/github 替换）。

### D2：引擎远程接入引导（引擎不可见）— 编排，不搬引擎

引擎不装进 Grape 沙箱（与平台"引擎独立环境"设计冲突、体积大）。Grape 既有
远程 runtime 架构（rta_ 凭据 → worker 注册 → queue_remote 认领自报）就是
为此而生——本机 ECS 上 /opt/grape worker 注册 3 引擎即是活例。

新增唯一编排入口 GET /api/runtime/engine-onboarding（require_org_member）：
返回 server_base_url（Host 自愈后的正确地址）、--server 已代入的 Linux/Windows
一键安装命令、本机扫描结果、在线远端引擎、needs_worker 判定（本地全未装且
远端零在线 → true）。零新机制，全部复用既有端点（凭据签发/安装包/安装脚本/
注册协议）。

## 伤害半径自审

- D1：写库条件限定"空或本地值"，用户显式配置（server-config.json / 有效
  公网地址）永不被覆盖；写错最坏结果是写不进（本地 Host 被排除）。
- D2：纯只读查询 + 文案生成，无状态变更。

## 改动清单

| 文件 | 内容 |
|------|------|
| src/api/main.py | 中间件补 server_base_url Host 自愈（_is_local_base 判定） |
| src/api/routes/runtime.py | 新增 /engine-onboarding 编排端点 |
| src/api/static/js/app.js | 引导页引擎接入卡片（_obEngineGuide，i18n） |
| src/api/static/js/pages/account.js | 令牌创建直达链接（GitHub scopes 预选 / GitCode 直达） |
| src/api/static/locales/{zh,en}/app.json | ob_engine_* 5 键 + open_token_page 1 键 |
| tests/test_oneclick_host_heal.py | S1-S7 用例（16 个，含真实中间件集成） |
| tests/test_engine_onboarding.py | E1-E4 + 结构守卫用例 |
| tests/js/test_engine_onboarding_guide.js | O1-O5 前端用例 |
| tests/js/test_token_create_deeplink.js | T1-T5 前端用例 |

## 测试与验收

- S1-S7 + E1-E4 + O1-O5 + T1-T5 全部通过；既有 Issue #372 webhook 检测用例
  全量回归通过（合计 49 passed + 2 个 node 用例文件）
- 集成用例挂真实 request_id_and_log_middleware + TestClient（模拟平台反代
  Host + X-Forwarded-Proto），mock DB 与 JWT，不连真实数据库
- 前端用例读真实 locales json 与源码结构守卫，node 直接运行
