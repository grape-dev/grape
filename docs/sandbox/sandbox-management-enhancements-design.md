# 沙箱管理增强（feat/devbox-sandbox-integration2 补充，超出原设计）

> 分支: feat/devbox-sandbox-integration2
> 日期: 2026-10-20
> 状态: 已实施已验证
> 背景: 本会话在 Provider 插拔 v3 主设计（../cloud-sandbox-provider-pluggable-design.md）之外
> 补齐的增量能力。原设计未覆盖这些点，作为主设计的补充记录在此。

## 一、范围清单

| # | 能力 | 提交 | 类型 |
|---|------|------|------|
| S1 | 前端页面接线（侧边栏入口/路由/脚本/i18n） | d3d04be78 | 迁移遗漏修复 |
| S2 | 模板校验修复（factory 位置参数 TypeError + 真实建箱生命周期） | 3fad36dcf | 迁移遗漏修复 |
| S3 | Provider/模板操作按钮文字 + tooltip | 3fad36dcf | UX 增强 |
| S4 | 模板手动检查按钮 | 3eb80f268 | 新能力 |
| S5 | 模板编辑按钮（预填现有配置） | 839762cc4 | 新能力 |
| S6 | 模板扫描（adapter list_templates + 自动导入） | 3239b3670 | 新能力（超出原设计） |
| S7 | 列表单元格 tooltip（truncate 列悬停显示完整内容） | abbd073f8 | UX 增强 |
| S8 | 模板列表 engines 列显示（从 binding 表 JOIN） | 29af3af0b | 迁移遗漏修复 |
| S9 | RuntimeClient.get_task_env_full 补齐 | f4aeaf954 | 迁移遗漏修复（执行链路阻断级） |

## 二、S6 模板扫描（核心新设计）

### 2.1 需求

用户在页面上点「扫描模板」→ 自动调平台 API 发现已有模板 → 有效且未登记的直接导入 DB →
弹窗提示"发现有效模板 xx 个，已绑定 xx 个，新增绑定 xx 个"。

### 2.2 解耦设计（不强依赖 cubesandbox API）

扫描能力作为 **PlatformAdapter 的可选能力**，而非端点内嵌平台逻辑：

```
src/agent/sandbox/backends/api.py   Protocol 新增:
    def list_templates(self, config: dict) -> list[dict] | None:
        """统一格式: [{"template_id", "name", "status", "description"}]
           不支持时返回 None（默认实现）"""

src/agent/sandbox/backends/cubesandbox.py   CubeSandboxAdapter 实现:
    SDK Template.list() → 统一格式
    （SDK 自身兼容 list / dict 包裹两种返回；dataclass/dict 双形态取值）

端点层（settings.py scan_sandbox_templates）:
    只消费统一格式，不感知任何具体平台 API。
    新平台接入 = 实现一个 list_templates，端点零改动。
```

### 2.3 端点语义

`POST /api/settings/sandbox-providers/{provider_id}/scan-templates`

- 配置来源：`_provider_config_from_db(platform, provider_id)`（复用 health 的取配置路径，api_key 解密 + adapter 归一化）
- 有效性判定：`status ∈ {ready, ok}`（小写）
- 去重键：`(provider_id, template_id)`——DB 已有的跳过
- 导入行：`enabled=1`，name 取平台返回（空则用 template_id），description 存镜像信息
- 返回：`{scanned, valid, existing, added, added_templates[], existing_templates[]}`
- 错误语义：adapter 不支持 → 400；平台调用失败 → 502

### 2.4 前端

引擎模板卡片头右侧按钮组：`[🔍 扫描模板] [＋ 新增模板]`（gap:6px 紧邻，不居中）。
扫描中禁用防重（按钮文案变"扫描中…"），完成后 alert 结果并刷新列表。

## 三、S4 模板手动检查

操作列 `🏥 检查` 按钮 → `POST /settings/sandbox-templates/{id}/check`。
后端复用 `_do_check_template`（真实建箱 + 3 次重试 kill 的完整生命周期验证），
结果写 `last_check_status/last_check_error` 并 alert 给用户。
进行中禁用防重。

## 四、S2/S8 展示层修复

- **S2**：`_do_check_template` 原 `factory(None, None, None, None)` 位置参数与
  `build_provider(*, ...)` keyword-only 签名冲突 → TypeError → Last Check 永远 error。
  修复为 `_provider_config_from_db`（新增 provider_id 参数，模板校验按 provider 精确取）
  + keyword 调用 + `create_sandbox` + `_do_create()` 真实建箱 + kill。
- **S8**：模板列表 API 的 v3 补充层加 v3.1——`engines` 字段从 `engine_template_binding`
  按 template_id 聚合返回（两条路径统一：前端用户 + cloud_worker）。原实现只返回模板行
  的 `engines` 列（v3 起已废弃恒 `[]`），导致绑定后页面显示 `—`。

## 五、S5/S3/S7 UX 增强

- **S5 编辑**：`openEditTemplate(t)` 预填现有配置（name/template_id/alias/description/
  preinstalled/disk_size_mb/enabled），复用既有编辑弹窗，保存走 PUT（含模板同步校验）。
- **S3/S7 提示**：操作按钮裸 emoji → 文字标签 + `:title` tooltip（编辑/启停/健康检查/删除/
  绑引擎/扫描/检查，中英双语）；Provider/模板列表被 truncate/ellipsis 的列加 `:title`
  悬停显示完整原文。

## 六、踩坑记录

1. **悬空绑定**：模板行删除后 `engine_template_binding.template_id` 指向不存在行（FK 未强制）。
   本会话实际遇到：模板删除→扫描重导入 id 变化→旧绑定悬空。处理：手动 DELETE 悬空行重绑。
   建议后续：scan/导入时顺带清理悬空绑定，或 unbind 按行 id 删。
2. **engines 双源**：模板行的 `engines` 列（废弃）与 binding 表并存期间，任何读路径都必须
   JOIN binding——本次 S8 即漏 JOIN 的展示层实例。
3. **cloud_worker 断链**：`client.get_task_env_full` 迁移遗漏（S9）导致云沙箱任务执行时
   AttributeError——对照设计文档做完成度核查时发现，已移植补齐（63 测试通过）。
   教训：迁移完整性检查应包含"调用方-被调方符号对账"，不能只看文件是否移植。
