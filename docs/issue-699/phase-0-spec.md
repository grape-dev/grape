# Phase 0 Spec: 清理去重 + 技能统一 + pyproject + Makefile + deploy 归拢

> Issue #699 Phase 0 | 目标分支: weekly-09-ljt | 权威设计: docs/weekly-09/grape-unified-implementation-design.md §3/§12/§14

## 1. Objective

实施 Grape 融合架构迁移 Phase 0：清理重复目录、统一技能目录、合并依赖管理、创建构建入口、归拢部署文件。
**无业务代码逻辑变更**，仅目录重组/配置归拢/基础设施 plumbing。

## 2. 现状核对（基于 weekly-09-ljt 实际代码，非设计文档假设）

设计文档 §2.1 描述的"4 套技能目录 skills/(185)、.grape/(2)、.opencode/(1)、.skills/(1)"与实际 repo 状态有差异：

| 项 | 设计文档描述 | 实际状态（git tracked + on-disk） |
|----|-------------|--------------------------------|
| `skills/` | 185 个 SKILL.md，tracked | **gitignored**（.gitignore line `skills/`），运行时由 `scripts/sync_skills.sh` 从 grape-skills 仓铺入；fresh worktree 中不存在 |
| `configs/scenes/` | 与 `scenes/` 重复 | **gitignored**（.gitignore `configs/scenes/`），运行时由 sync_skills.sh 铺入；fresh worktree 中不存在 |
| `scenes/`（顶层） | 与 configs/scenes/ 重复 | **tracked**（10 个场景，20 文件）；无任何代码引用本地 `scenes/` 路径（scene_sync.py 读下载 ZIP 的 scenes/，release_pack.py 读外部 SKILLS_REPO/scenes/） |
| `admin/`（顶层） | 旧版残留 | **已不存在**（.gitignore `admin/` + `!src/admin/`；src/admin/ 存在） |
| `.grape/skills/` | 2 个技能 | tracked 2 个 qa 技能（ai-coding-review/grape-architecture-guardian、openspec-pr-reviewer），15 文件 |
| `.opencode/skills/` | 1 个技能 | tracked 1 个（grape-dev-workflow/SKILL.md） |
| `.skills/` | 1 个技能 | **gitignored**（.gitignore `.skills/`），运行时 agent workspace |
| `requirements*.txt` | 4 个 | 4 个 tracked：requirements.txt / requirements-worker.txt / requirements-dev.txt / requirements-local.txt |
| `deploy/` | 不存在 | 已存在（alerts.yml tracked） |
| `data/` `wheels/` | — | gitignored（.gitignore `data/` `wheels/`） |

**关键差异**：`skills/` 是 gitignored 运行时目录（非 tracked），"统一到 .grape/skills/" 实为变更 `SKILLS_DIR` config + sync 脚本目标路径，非 git tracked 文件搬迁。

## 3. 范围（Phase 0 交付清单）

### 3.1 删除重复目录
| 目录 | 操作 | 依据 |
|------|------|------|
| `scenes/`（顶层 tracked） | **删除** | 无代码引用本地路径；scenes 由 sync_skills.sh→configs/scenes/ + scene_sync.py→DB 同步 |
| `admin/`（顶层） | **已不存在，noop** | .gitignore 已排除 |
| `tasks/`（tracked 21 文件） | **移入 docs/plans/** | 纯文档（plan/todo markdown），无代码引用 |

### 3.2 部署文件归入 deploy/
| 文件 | 操作 |
|------|------|
| `nginx-grape.conf` | `git mv` → `deploy/nginx-grape.conf` |
| `Dockerfile` | `git mv` → `deploy/Dockerfile`（更新 COPY/pip install 适配 pyproject.toml） |
| `docker-compose.yml` | `git mv` → `deploy/docker-compose.yml`（更新 build context/volume 路径） |

### 3.3 data/ wheels/ .gitkeep
- 添加 `data/.gitkeep`、`wheels/.gitkeep`
- .gitignore 加 `!data/.gitkeep`、`!wheels/.gitkeep` 例外

### 3.4 Makefile（新建）
统一构建入口：`make help`/`make test`/`make run`/`make lint`/`make install`/`make sync-skills`

### 3.5 pyproject.toml（新建，合并 4 个 requirements）
- `[project] dependencies` = 核心依赖（requirements-local.txt 去可选）
- `[project.optional-dependencies]`：`mysql`/`obs`/`im`/`dev`/`worker`/`all`
- 删除 4 个 requirements*.txt
- 同步更新：worker_package.py MANIFEST、install-worker.sh/ps1、release_pack.py、Dockerfile

### 3.6 技能目录统一到 .grape/skills/
| 操作 | 详情 |
|------|------|
| `.opencode/skills/grape-dev-workflow/` → `.grape/skills/dev-workflow/` | `git mv`（tracked 文件搬迁） |
| 删除 `.opencode/` | 移走内容后删除空目录 |
| `config.py SKILLS_DIR` | `skills` → `.grape/skills` |
| `scripts/sync_skills.sh` | sync 目标 `skills/` → `.grape/skills/` |
| `scripts/adapt_skills_vcs.sh` | `SKILLS_DIR="$ROOT_DIR/skills"` → `.grape/skills` |
| `.gitignore` | 调整：track `.grape/skills/qa/` + `.grape/skills/dev-workflow/`，ignore synced official dirs |
| `worker_package.py MANIFEST` | 不含 skills 引用，noop |
| `scene_sync.py` | 不引用本地 scenes/ 路径，noop |
| `skill_sync.py` | 用 `from src.config import SKILLS_DIR`，auto-follow |

### 3.7 不做的事（Out of Scope）
- `src/kernel/` 提取（Phase 1）
- `src/domains/` 域内聚（Phase 1）
- 分 Schema（Phase 2）
- `_execute_agent` 迁移（Phase 3）
- 四进程拆分（Phase 4）
- 任何业务逻辑变更

## 4. 验收标准（TC-0.1 ~ TC-0.5）

| TC | 验证目标 | 验证命令 |
|----|---------|---------|
| TC-0.1 | scenes/ 删除无残留引用；tasks/ 在 docs/plans/ | `test ! -d scenes/ && test -d docs/plans/ && grep -r "scenes/" src/ --include="*.py" \| grep -v configs/scenes` |
| TC-0.2 | .grape/skills/ 统一；SKILLS_DIR 指向 .grape/skills/ | `python -c "from src.config import SKILLS_DIR; assert '.grape/skills' in SKILLS_DIR; print('OK')"` |
| TC-0.3 | 部署文件在 deploy/ | `ls deploy/nginx-grape.conf deploy/Dockerfile deploy/docker-compose.yml && test ! -f nginx-grape.conf` |
| TC-0.4 | pyproject.toml 存在；pip install -e . 正常 | `test -f pyproject.toml && test ! -f requirements.txt && pip install -e . 2>&1 \| tail -5` |
| TC-0.5 | Makefile 可用 | `test -f Makefile && make help` |

## 5. 风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| SKILLS_DIR 变更影响 130+ 引用 | 中 | SKILLS_DIR 是 config 常量，import 自动跟随；sync 脚本同步更新 |
| requirements-worker.txt 删除影响 worker 离线安装 | 中 | pyproject.toml [worker] extra 替代；install-worker.sh 改 `pip install -e .[worker] --find-links wheels/` |
| Dockerfile 移入 deploy/ 影响 docker build | 低 | docker-compose.yml build context 指向 `..`，dockerfile 指向 `deploy/Dockerfile` |
| scenes/ 删除丢失离线 fallback | 低 | scenes 由 sync_skills.sh + scene_sync.py 运行时同步，本地快照非唯一来源 |
