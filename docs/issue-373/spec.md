# Spec — Issue #373 帮助中心新增第四大块「开发者文档」

> 来源：Issue #373（评论 id=194540420，Issue Agent 已发布，评级 ready）。本文件为开发侧落地副本。

## 目标
帮助中心新增 `help/developer/` 目录（第四大块「开发者文档」），7 篇起步（00~06），面向自建/私有化部署的运维/平台管理员，提供「装好之后配什么、在哪配、怎么验证」配置指南。

## 范围
- In：`help/developer/` 7 篇 + 帮助中心前端接入（`src/help_page_gen.py` 挂载第四区块）+ 测试覆盖。
- Out：usage/reference/api 三块内容改动、代码逻辑变更、新配置项引入。

## 内容结构
| 文件 | 内容 |
|------|------|
| 00-overview.md | 四层配置面全景（admin 控制台 / configs/*.json / 环境变量 / DB meta）、优先级、重启 vs 热生效 |
| 01-gitcode-integration.md | OAuth 应用、Bot Token（gitcode_api_token）、webhook_token/webhook_url、托管仓库验证 |
| 02-github-integration.md | OAuth App + github_bot_token + github_bot_username + webhook |
| 03-im-bot.md | 飞书扫码配置、lease 多实例机制、验证 `connected after acquiring lease` + test-notify |
| 04-storage-and-email.md | OBS（ENABLED/BUCKET/AK/SK/PREFIX，不配降级本地）+ SMTP（email.json） |
| 05-database-and-sync.md | db-backend.json（SQLite→MySQL、DB_POOL_*）+ scene-sync.json（30 分钟周期） |
| 06-security-and-engine.md | JWT_SECRET、COOKIE_SECURE、login-whitelist.json、engine-selection.json |

## 写作原则
1. 只写逐一核实过的配置项（字段名以 `configs/*.example.json` 与 `src/` 源码为准）。
2. 每项三件套：在哪配（含优先级）→ 配置示例 → 如何验证生效。
3. 敏感值一律占位符（`<your-ak>` 等），绝不出现真实凭据。
4. 文件名 `00-` 前缀对齐 api/；H1 手写编号（`# 1. ...`），正文无自动编号。

## 验收
- 7 篇落库且帮助中心可访问（help_page_gen 渲染 developer 区块）。
- 每项三件套齐全；03 含 `connected after acquiring lease` 验证项。
- `python scripts/check-doc-redaction.py --dir help` 通过（无真实凭据）。
- 测试：新增 developer 用例通过；不引入新失败（基线已有 18 个与本需求无关的预存失败）。
