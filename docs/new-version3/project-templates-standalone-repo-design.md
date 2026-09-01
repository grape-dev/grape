# 项目模板独立仓库方案（构建/部署时拉取 · 方案 C）

> 版本：v1.0
> 状态：方案（未实施）
> 关联：`docs/new-version2/skills-standalone-repo-build-time-pull.md`（技能目录独立仓库方案 C，本方案为其同构改造）
> 目标：将 `examples/` 下的官方项目模板内容拆出 grape 代码仓库，独立维护于 `gitcode.com/grape-dev/grape-template`，
> 构建/部署阶段从该仓库拉取，实现「模板内容与代码解耦」，运行时零代码改动。

---

## 一、需求概述

当前 grape 的**本地项目模板**（`source: "local"` 的模板，共 7 个 hello-*）内置在 grape 代码仓库的 `examples/` 目录下，
随 grape 发版、随镜像打包。这带来几个问题：

1. **模板迭代受代码发版节奏约束** — 新增/修改一个模板要等 grape 版本发布才能生效；
2. **模板 review 与代码 review 耦合** — 社区想贡献模板需进主仓，权限/流程成本高；
3. **部署环境缺模板** — `configs/project-templates.json` 里声明了 local 模板，但部署根若没有对应的
   `examples/{id}` 目录，一键创建即报「本地模板目录不存在」。

目标与技能仓库解耦一致（`grape-skills`）：

1. **内容与代码解耦**——官方项目模板内容不再跟随 grape 代码仓库发版，独立存放于 `grape-template` 仓库；
2. **最小运行时改动**——`EXAMPLE_DIR` 物理位置不变（仍是 `<部署根>/examples`），Python / admin 运行时代码零改动；
3. **可独立发版**——模板仓库单独维护、单独 review，按 tag / commit 锁定版本；
4. **统一注册表**——`project-templates.json`（官方模板清单，含 local 与 git 模板）随模板仓库发版，构建期铺入 `configs/`。

---

## 二、现有模板使用场景分析

### 2.1 `examples/` 目录下的 7 个本地模板

| 模板 id | 场景（scene_id） | 内容 | 用途 |
|---|---|---|---|
| `hello-flask` | default | Flask 单路由 Hello World（`app.py` + `requirements.txt`） | 新手入门 Web 服务 |
| `hello-fastapi` | default | FastAPI 服务，`/` + `/health`，`app/main.py` | 新手入门现代 Python API |
| `hello-todo` | default | 命令行待办（`todo.py`，sys.argv + 列表） | 新手入门 CLI 交互 |
| `hello-calculator` | default | 计算器 + `unittest` 测试（`calculator.py` + `test_calculator.py`） | 新手入门 TDD |
| `hello-html` | default | 静态站（`index.html` + `style.css` + `script.js`） | 新手入门前端三件套 |
| `hello-blog` | default | Flask 多页博客骨架（路由 + `render_template_string`） | 新手入门路由/模板渲染 |
| `hello-skill` | skill-dev | 最小 Skill 模板（`SKILL.md` + `scripts/greet.py`） | Skill 开发起点，对应开发场景 `skill-dev` |

### 2.2 模板消费链路（当前）

```
configs/project-templates.json（注册表：分类 + 模板元数据，source=local/git）
  ├── GET /api/project-templates               → 前端「模板库」页渲染（templates.js）
  ├── POST /api/project-templates/create       → 一键创建
  │     ├── 1. create_repo                      → 用户 GitCode 个人名下建空仓
  │     ├── 2. push_template_to_repo()          → src/user/repo_manager.py:358
  │     │     ├── source="local" → 复制 examples/{template_id}/（旧路径 configs/project-templates/{id} 兜底）
  │     │     └── source="git"   → git clone source_repo（GitCode/GitHub tarball）
  │     ├── 3. host_repo()                      → 邀请 bot 为维护者 + 建 webhook + 写 managed_repos
  │     └── 4. apply_scene()                    → 应用模板 scene_id 的开发场景
  └── admin TemplatesService.listTemplates()    → 控制台模板列表 + 使用统计（managed_repos 按名匹配）
```

关键常量与约束：

- `src/config.py:100`：`EXAMPLE_DIR = <grape>/examples`（硬编码，部署根下）；
- `src/user/repo_manager.py:380-394`：local 模板源目录 = `EXAMPLE_DIR/{template_id}`，缺失时报错；
- `tests/test_project_templates.py:84`：校验 local 模板 id 对应的 `examples/{id}` 目录必须存在；
- 模板 id 与 `examples/` 子目录名**一一对应**。

### 2.3 其它模板来源（不影响本方案拆分）

- `source: "git"` 模板（GitCode 热门 / agent-best-practices / ai-office / cloud-native / iac 等）：内容来自外部仓库 `source_repo`，
  与本地 `examples/` 无关，仅注册表条目，可一并随注册表迁移；
- `featured_templates`（Top1000 优选）：来自 DB 表，不走 `project-templates.json`，不受影响。

---

## 三、方案选择

| 方案 | 说明 | 运行时改动 | 采纳 |
|---|---|---|---|
| A. git submodule | grape 以 submodule 挂 `grape-template` | 中（需 submodule 管理） | ❌ |
| B. 环境变量指向模板目录 | `EXAMPLE_DIR` 改读外部模板仓库路径 | 高（改 config / admin / 测试路径） | ❌ |
| **C. 构建/部署时拉取** | 同步脚本构建期 clone `grape-template` 铺入 `examples/` + `configs/project-templates.json` | **零**（物理位置不变） | ✅ |

选择 C 与技能目录方案 C 保持一致：运行时零代码改动、`EXAMPLE_DIR` 语义不变、不改 `config.py` / admin Java / 测试路径，
适合当前「模板目录树已稳定」的前提。

---

## 四、目标形态

### 4.1 模板独立仓库 `grape-dev/grape-template`（已创建）

```
grape-template/                          # https://gitcode.com/grape-dev/grape-template.git（默认分支 main）
├── hello-blog/                          # 与 grape examples/ 同名子目录逐一对应（模板 id == 目录名）
├── hello-calculator/
├── hello-fastapi/
├── hello-flask/
├── hello-html/
├── hello-skill/
├── hello-todo/
├── project-templates.json               # 官方模板注册表（含 source=local 与 source=git 全量条目）随本仓库发版
└── README.md                            # 仓库说明：目录约定 / 如何新增模板 / 如何同步
```

### 4.2 grape 代码仓库（改造后）

```
grape/
├── examples/                            # 构建/部署时由 sync 脚本铺入官方模板（空目录 .gitkeep 占位或直接清空）
├── configs/
│   └── project-templates.json           # 构建/部署时由 sync 脚本铺入（grape 仓库不再直接维护）
├── scripts/
│   └── sync_templates.sh                # 新增：构建/部署时拉取模板的入口（与 sync_skills.sh 同构）
└── ...
```

### 4.3 关键约定

1. **`EXAMPLE_DIR` 物理位置不变**：部署根仍为 `examples/`，模板子目录由构建/部署脚本从独立仓库铺入；
2. **`project-templates.json` 随模板仓库走**（解决双源漂移）：构建产物中该文件落到 `<部署根>/configs/project-templates.json`；
3. **版本锁定**：依赖方（grape 发布物 / CI / 部署脚本）显式固定模板仓库的 commit 或 tag；
4. **模板 id == 仓库子目录名**：`examples/<template_id>/` 与 `project-templates.json` 中 local 模板的 `id` 必须一一对应
   （CI 门禁校验，见 §8 风险 1）。

---

## 五、实施步骤

### 5.1 步骤 1：填充模板独立仓库（仓库已创建）

1. ✅ 远端 `grape-template.git` 已创建（`gitcode.com/grape-dev/grape-template.git`，默认分支 `main`）；
2. 本地把 7 个模板目录 + `project-templates.json` 推送上去：克隆空仓库 → 放入各模板目录 + 注册表 + README → commit + push；
3. 设定版本 tag（`v1.0.0` 起步）；
4. `.gitignore`：空（模板仓库不放运行时目录）。

### 5.2 步骤 2：新增同步脚本 `scripts/sync_templates.sh`

```bash
#!/usr/bin/env bash
# 构建/部署时从 grape-template 仓库拉取官方项目模板到部署根 examples/ + configs/
set -euo pipefail

TEMPLATES_REPO="${TEMPLATES_REPO:-https://gitcode.com/grape-dev/grape-template.git}"
TEMPLATES_REF="${TEMPLATES_REF:-main}"          # 或铺入时指定 tag/commit
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

git clone --depth 1 --branch "$TEMPLATES_REF" "$TEMPLATES_REPO" "$STAGE/templates"

# 1) 官方模板 → 部署根 examples/（仅同步模板子目录，忽略 README/注册表等非模板文件）
mkdir -p "$ROOT_DIR/examples"
find "$STAGE/templates" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | while read -r d; do
  rm -rf "$ROOT_DIR/examples/$d"
  cp -a "$STAGE/templates/$d" "$ROOT_DIR/examples/$d"
  echo "[sync-templates] synced examples/$d"
done

# 2) 注册表 → 部署根 configs/
if [ -f "$STAGE/templates/project-templates.json" ]; then
  cp -a "$STAGE/templates/project-templates.json" "$ROOT_DIR/configs/project-templates.json"
  echo "[sync-templates] synced configs/project-templates.json"
else
  echo "[sync-templates] WARN: project-templates.json missing in repo" >&2
fi

echo "[sync-templates] done (${TEMPLATES_REPO}@${TEMPLATES_REF})"
```

> 注：`find -printf` 在 GNU coreutils 下可用（部署机为 Linux）；如需 BSD/macOS 兼容可改用 `for d in "$STAGE/templates"/*/` 遍历目录。

### 5.3 步骤 3：grape 仓库清理本地模板与注册表

1. `git rm -r examples/hello-blog examples/hello-calculator examples/hello-fastapi examples/hello-flask examples/hello-html examples/hello-skill examples/hello-todo`；
2. `examples/` 保留空目录占位（`.gitkeep`），避免 `EXAMPLE_DIR` 解析失败；
3. 删除 `configs/project-templates.json`（改由 sync 脚本从模板仓库铺入）；
4. 更新文档：README 模板说明、DEPLOYMENT-*.md、CODEOWNERS（若含 examples 路径）。

### 5.4 步骤 4：容器/部署接入

**本机/裸机部署**（DEPLOYMENT-REMOTE.md 的 `git pull` 流程后追加）：

```bash
bash scripts/sync_templates.sh        # 参数可用环境变量 TEMPLATES_REPO / TEMPLATES_REF 覆盖
```

**docker-compose 卷挂载方案（✅ 已选定，与 skills 一致）**：

```yaml
services:
  backend:
    volumes:
      - ./data:/app/data
      - ./configs:/app/configs
      - ./skills:/app/skills
      - ./examples:/app/examples      # 新增：模板卷（宿主机 examples/ 已由 sync_templates.sh 铺入）
```

> 注：卷挂载后，模板更新仅需在宿主机重跑 `sync_templates.sh` 并重启容器，无需重建镜像。

**（备选，未采用）镜像内多阶段 COPY**：

```dockerfile
FROM alpine/git AS template-stage
ARG TEMPLATES_REPO=https://gitcode.com/grape-dev/grape-template.git
ARG TEMPLATES_REF=main
RUN git clone --depth 1 --branch ${TEMPLATES_REF} ${TEMPLATES_REPO} /templates
...
COPY --from=template-stage /templates/hello-blog      /app/examples/hello-blog
# ... 每个模板目录一条 COPY
COPY --from=template-stage /templates/project-templates.json /app/configs/project-templates.json
```

### 5.5 步骤 5：测试与 CI 适配

现有测试硬编码路径：`tests/test_project_templates.py:30`（`configs/project-templates.json`）与
`:84`（`examples/{id}` 目录存在性）。

- 选项 1（推荐）：CI 与本地开发的第一步先跑 `bash scripts/sync_templates.sh`（与 skills 的 `sync_skills.sh` 同法）；
- 选项 2：测试读取 `TEMPLATES_REPO`/环境变量动态铺入（复杂度高，不推荐）。

---

## 六、影响面评估（本方案刻意最小化）

| 组件 | 是否需改 | 说明 |
|---|---|---|
| `src/config.py` EXAMPLE_DIR | ❌ | 物理位置不变，仍是 `<grape>/examples` |
| `src/user/repo_manager.py` 模板复制 | ❌ | 继续读 `EXAMPLE_DIR/{template_id}` |
| `src/api/routes/templates.py` | ❌ | 继续读 `configs/project-templates.json` |
| admin `TemplatesService` | ❌ | `app.templates-config` 默认 `configs/project-templates.json` 不变 |
| `configs/project-templates.json` | ✅（随仓库走） | 改为构建期由 sync 脚本铺入 |
| `examples/*` 本地模板目录 | ✅（移出 grape） | 构建期由 sync 脚本铺入 |
| Dockerfile / docker-compose | ✅ | compose 加 `./examples` 卷 |
| worker | ❌ | 不依赖部署根模板（与 skills worker 同理） |
| 测试（tests/*路径常量） | ✅ | 需在 CI/本地先跑 sync（见 §5.5） |
| `.gitignore` | ✅ | 官方模板不再由 grape 跟踪，保留 examples 占位 |

---

## 七、风险与决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | **`project-templates.json` 双源漂移**：注册表随模板仓库走，新增/修改模板时模板仓库 PR 必须同步更新注册表；CI 门禁校验「local 模板 id 与 `examples/<id>` 目录一致」（当前 `test_local_template_dir_exists` 反向迁移到模板仓库 CI） | ✅ 可以 |
| 2 | **git 模板条目归属**：`source: "git"` 的外部模板注册表条目随 `project-templates.json` 迁到模板仓库，由模板仓库统一维护清单；content 仍来自外部 source_repo，无需迁移 | ✅ 可以 |
| 3 | **模板仓库与 grape 版本兼容**：模板仓库 tag 与 grape 版本建立约束，避免新模板引用 grape 尚不支持的字段 | ⏸ 暂不考虑 |
| 4 | **Top1000 / featured_templates 不受影响**：来自 DB 表，与 `project-templates.json` 解耦 | — 无需决策 |
| 5 | **本地开发**：`python start_server.py` 前需先 `bash scripts/sync_templates.sh`；README 补一句 | ✅ 可以 |
| 6 | **是否保留 git 历史**：`git subtree split` / `filter-repo` 把 `examples/` 历史带入模板仓库（可选项，非必需） | — 待后续

---

## 八、实施顺序与回滚

1. ✅ 模板仓库已创建：推送 7 模板目录 + `project-templates.json` + README 到 `gitcode.com/grape-dev/grape-template.git`（tag v1.0.0）；
2. 写 `scripts/sync_templates.sh`，本地验证铺入后跑模板相关 pytest 全绿；
3. grape 仓库删除 `examples/hello-*` + `configs/project-templates.json`，更新 README / DEPLOYMENT / CODEOWNERS；
4. docker-compose 增加 `./examples:/app/examples` 卷挂载（宿主机先跑 `sync_templates.sh`，过渡期不动构建产物）；
5. 发布：暴露 `TEMPLATES_REPO`/`TEMPLATES_REF` 变量，默认指向已验证 tag；
6. 回滚：git 历史保留，`git checkout` 删除提交即可恢复模板与注册表入仓；运行时不受影响（模板仍在部署根 examples/）。

---

## 九、结论

方案 C 以**构建/部署期一次 clone/copy** 完成模板内容与代码解耦，运行时零代码改动、`EXAMPLE_DIR` 语义不变、
worker 与 admin 不受影响，是当前模板目录树稳定前提下的最低成本拆分，且与技能仓库（`grape-skills`）的既有实践完全同构。
核心风险在于「注册表随模板仓库」与「模板 id 与目录一致」两个约定，需在 CI 门禁 + tag 语义中固化。

---

## 十、执行计划（阶段）

### 阶段 0 · 基线（可选，若有未提交改动）

1. `git status` 确认 examples/configs 无冲突改动，必要时先提交。

### 阶段 1 · 推送模板到独立仓库

1. 本地 clone 空 `grape-template.git`，放入 7 个模板目录 + `project-templates.json` + README，commit 后 push `main`；
2. 打 tag：`git tag v1.0.0 && git push --tags`；
3. **验收**：远端 clone 后 7 模板目录 + 注册表齐全。

### 阶段 2 · 新增同步脚本

1. grape 写 `scripts/sync_templates.sh`（默认 `TEMPLATES_REPO=https://gitcode.com/grape-dev/grape-template.git`、`TEMPLATES_REF=main`）；
2. 本地执行一次，验证 `examples/{id}` + `configs/project-templates.json` 铺入正确；
3. **验收**：`tests/test_project_templates.py` 全绿。

### 阶段 3 · grape 仓库清理

1. `git rm -r examples/hello-*`；`examples/` 保留 `.gitkeep` 占位；
2. `git rm configs/project-templates.json`（改由 sync 脚本铺入）；
3. 更新 README / DEPLOYMENT-REMOTE.md / CODEOWNERS；
4. 提交并推送；**验收**：grape 仓库不含官方模板；运行 `sync_templates.sh` 后可完整启动服务。

### 阶段 4 · 部署接入与发布

1. docker-compose 增加 `./examples:/app/examples` 卷（§5.4 正式方案，同时修正 compose 中重复的 volumes 段）；
2. 宿主机首次部署先跑 `bash scripts/sync_templates.sh`，再 `docker compose up -d`；
3. 全量回归 + 冒烟：`GET /api/project-templates` 返回完整分类，一键创建 local 模板成功。