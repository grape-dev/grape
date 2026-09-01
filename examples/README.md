# examples

`examples/` 目录用于存放 Grape 官方项目模板（Hello World 系列示例），供「项目模板」一键创建使用。

## 来源

官方项目模板已迁移至独立仓库 **https://gitcode.com/grape-dev/grape-template**，本目录在**部署期**由

```bash
bash scripts/sync_templates.sh
```

从独立仓拉取铺入 `examples/`（示例工程）与 `configs/project-templates.json`（模板注册清单）。

> `git pull` / 部署后必须执行 `sync_templates.sh`，否则官方模板缺失（首次部署同理）。

## 部署后本目录应包含

| 模板 | 说明 |
|------|------|
| `hello-blog/` | 极简博客应用 |
| `hello-calculator/` | 计算器示例（含单测） |
| `hello-fastapi/` | FastAPI 服务示例 |
| `hello-flask/` | Flask 服务示例 |
| `hello-html/` | 静态 HTML 示例 |
| `hello-skill/` | 技能（Skill）示例 |
| `hello-todo/` | Todo 应用示例 |

## 说明

- 本地开发期本目录可为空（仅保留 `.gitkeep` / 本 README），模板由部署链路提供；
- 自定义/本地模板继续放在本目录渲染 `configs/project-templates.json` 时引用即可（详见 `docs/new-version3/project-templates-standalone-repo-design.md`）。