# grape-template

Grape 官方项目模板独立仓库。内容由 grape 主仓库在构建/部署阶段通过 `scripts/sync_templates.sh`
拉取到部署根 `examples/` 与 `configs/project-templates.json`。

## 目录约定

| 目录/文件 | 说明 |
|---|---|
| `hello-*` | 官方项目模板，目录名即模板 `id`，与 `project-templates.json` 中 `source: "local"` 的模板一一对应 |
| `project-templates.json` | 官方模板注册表（含 local 与 git 模板全量条目），随本仓库独立发版 |

> 新增模板时，必须同时满足：新建同名模板目录 **且** 在 `project-templates.json` 中登记对应 `id`
>（`source: "local"`）。两者不一致会导致 grape 侧 `test_local_template_dir_exists` 门禁失败。

## 模板列表

- `hello-flask` — 极简 Flask Web 应用（default 场景）
- `hello-fastapi` — 极简 FastAPI 服务（default 场景）
- `hello-todo` — 命令行待办清单（default 场景）
- `hello-calculator` — 计算器 + 单元测试（default 场景）
- `hello-html` — 静态网页前端三件套（default 场景）
- `hello-blog` — Flask 多页博客骨架（default 场景）
- `hello-skill` — 最小 Skill 项目模板（skill-dev 场景）

## 使用

```bash
# 在 grape 主仓库部署时同步模板
bash scripts/sync_templates.sh
```