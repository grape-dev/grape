# plugin-tester 校验规则清单（tester-checklist）

> 每条规则带编号，失败报告必须引用规则编号。来源：`plugin-creator/references/plugin-manifest-spec.md` + 插件市场方案 D4/D6/D7/D8。

## §1 L0 静态校验（一票否决）

| 编号 | 规则 |
|------|------|
| L0-1 | plugin.json 存在且可被 JSON 解析 |
| L0-2 | 必填顶层字段齐全：name / version / display_name / description / author / adapters / commands / compat |
| L0-3 | version 符合 semver（MAJOR.MINOR.PATCH） |
| L0-4 | 目录结构：plugin.json + SKILL.md 必备；scripts/ 可选且仅薄 adapter |
| L0-5 | SKILL.md frontmatter（name/description/version）与 manifest 一致 |
| L0-6 | SKILL.md 无凭据明文、无硬编码 secret（含 AK/SK 形态字符串扫描） |

## §2 契约校验

| 编号 | 规则 |
|------|------|
| C-1 | 每条 command.adapters 引用的 adapter 名真实存在于 adapters[] |
| C-2 | http 型 command 声明 method + path；path 与 OpenAPI 文档一致（抽样 ≥3 条） |
| C-3 | `level` ∈ {read, write, danger}；无 Spec 依据的 write/danger 命令 = 违规收录 |
| C-4 | credentials[] 只声明注入位（name/type/required/inject/scope/expiry）；任何字段出现明文值 = 违规 |
| C-5 | credentials[].scope 覆盖其 command 涉及的节点 Agent 类型 |
| C-6 | permissions.network.allowlist 覆盖全部 adapter base_url 的域 |
| C-7 | cli 型 adapter 必填 permissions.exec.allowlist，且 command 名 ⊆ 白名单 |
| C-8 | compat.engines 形态口径：cli/http ⊆ 6 引擎全集；mcp 仅 codex |
| C-9 | 每条 command.timeout 必填且 > 0 |
| C-10 | bundled_skills[]（如有）引用的技能可独立安装（非硬依赖，D1 校准口径） |

## §3 免凭据自检（协议层验证）

| 编号 | 规则 |
|------|------|
| T-1 | http 型只读端点方法名探测：404/405（路由层反馈）视为连通形态正常；连接拒绝/DNS 失败标「环境受限」 |
| T-2 | 无凭据请求返回 401/403（鉴权注入位正确）；返回 2xx 需核对是否真免鉴权；5xx = 注入位声明可疑 |
| T-3 | cli 型 `--help` 可用、退出码 0；白名单外子命令不可达 |
| T-4 | 探测请求一律 30s 超时；超时标「环境受限」降级 |

## §4 引擎路由实测（凭据可用时）

| 编号 | 规则 |
|------|------|
| R-1 | 按 compat.engines 抽样 ≥2 引擎，各跑 1 条 `level=read` 命令 |
| R-2 | 调用经 Runner 语义：env 注入生效、timeout 生效、审计事件可查 |
| R-3 | mcp 型仅 codex 路由；其他引擎装了跑不了 = C-8 违规在先 |

## §5 凭据缺失/过期引导

| 编号 | 规则 |
|------|------|
| G-1 | 凭据缺失用例标「待配置凭据」降级，不判红 |
| G-2 | session 过期（expiry < now）标「待重授权」+ 引导话术 |
| G-3 | 需凭据用例全部无法执行 → 结论「待部署后冒烟」，不虚构通过 |

## 评分与分流

- L0 任一失败 → 🔴 直接打回 coding（引用 L0-x）。
- §2/§3/§4 失败 → 🔴 打回 coding（每项至多 1 次），报告含修复建议。
- 全绿（含降级项）→ ✅ A2A sre plugin-publish。
