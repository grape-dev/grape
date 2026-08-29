# 技能目录独立仓库方案（构建/部署时拉取 · 方案 C）

> 版本：v1.0
> 状态：方案（未实施）
> 关联：`docs/new-version2/skill-directory-categorization-plan.md`（已完成的 37 技能→agent_type 重分类）
> 目标：将 `skills/` 官方技能内容拆出 grape 代码仓库，构建/部署阶段从独立仓库拉取，实现内容与代码解耦

## 1. 目标与范围

### 1.1 目标

1. **内容与代码解耦**：官方技能 SKILL.md / references / scripts 不再跟随 grape 代码仓库发版；
2. **最小运行时改动**：`SKILLS_DIR` 物理位置不变（仍是 `<部署根>/skills`），Python / admin 运行时代码零改动；
3. **可独立发版**：技能仓库单独维护、单独 review，按 tag / commit 锁定版本；
4. **保留运行时数据**：`custom/`（用户上传技能）、`repo/`（仓库技能）是运行时目录，不属于任何仓库。

### 1.2 方案选择依据

对比三种方案（A submodule / B 环境变量指向 / C 构建期拉取）：
本方案采用 **C：构建/部署时拉取**，因为其运行时改动成本最低、不改变 `SKILLS_DIR` 语义、
不需要改 `config.py` / admin Java / 测试路径，满足当前"官方技能目录树已稳定"的条件。

## 2. 现状快照（解耦前）

```
grape 代码仓库
└── skills/                         # 待拆分的目录
    ├── vod/  issue/  coding/  qa/  sre/  chat/    # 37 个官方技能（agent_type 分组，git 跟踪）
    ├── custom/                                  # 运行时：用户技能（.gitignore，git 不跟踪）
    ├── repo/                                    # 运行时：仓库技能（.gitignore，git 不跟踪）
    └── _template/                               # 技能模板（待确认随代码还是随技能走）
```

- `src/config.py:16`：`SKILLS_DIR = os.path.abspath(.../skills)`（硬编码，部署根下）
- `configs/skill-description.json`：37 条官方技能清单（agent_type/path），是 DB `skill` 表 seed 源
- admin：`app.skills-dir ${SKILLS_DIR:../../skills}` 环境变量可覆盖
- Docker：`COPY . .` 把 `skills/` 全量打进镜像；compose 只挂载 `./data`、`./configs`
- worker：不依赖本地 skills（技能随任务下发，见 DEPLOYMENT-WORKER.md §4）

## 3. 目标形态

```
技能独立仓库（grape-skills）
├── vod/  issue/  coding/  qa/  sre/  chat/          # 37 个官方技能（agent_type 分组）
├── _template/                                       # 技能模板
└── skill-description.json                           # 官方技能清单（随技能仓库发版）

grape 代码仓库（改造后）
├── scripts/sync_skills.sh            # 新增：构建/部署时拉取技能的入口
├── build/                             # 新增：容器构建用 Dockerfile 片段/引用
└── skills/
    ├── custom/                        # 运行时目录保留（.gitkeep 或空目录）
    └── repo/                          # 运行时目录保留（.gitkeep 或空目录）
```

### 3.1 关键约定

1. **`SKILLS_DIR` 物理位置不变**：部署根仍是 `skills/`，其中官方技能目录由构建/部署脚本从独立仓库铺入；
2. **`skill-description.json` 随技能仓库走**（解决 §7.1 双源漂移）：构建产物中该文件同时落到 `<部署根>/configs/skill-description.json`；
3. **版本锁定**：依赖方（grape 发布物 / CI / 部署脚本）显式固定技能仓库的 commit 或 tag；
4. **运行时三个 scope 位**：`custom/`、`repo/`、`_template/` 在仓库中保留占位，注入时不动。

## 4. 实施步骤

### 4.1 步骤 1：填充技能独立仓库（仓库已创建）

1. ✅ 远端 `grape-skills.git` 已创建（`gitcode.com/grape-dev/grape-skills.git`，默认分支 `main`），无需再建仓库；
2. 本地把 skill 内容推送上去：克隆空仓库 → 放入各 agent_type 目录 + `_template/` + `skill-description.json` → commit + push（见 §11 阶段 1）；
3. 设定版本 tag（`v1.0.0` 起步）；
4. `.gitignore` 覆盖：空（技能仓库不放运行时目录）。

### 4.2 步骤 2：grape 仓库清理官方技能

1. `git rm -r skills/vod skills/issue skills/coding skills/qa skills/sre skills/chat skills/_template`；
2. 保留 `skills/custom/`、`skills/repo/` 空目录（添加 `.gitkeep` 以便目录进入 git）；
3. 删除 `configs/skill-description.json`（改由同步脚本从技能仓库产物放置）；
4. 更新文档：README 技能目录说明、DEPLOYMENT-*.md、CODEOWNERS（若含 skills 路径）。

### 4.3 步骤 3：新增同步脚本 `scripts/sync_skills.sh`

```bash
#!/usr/bin/env bash
# 构建/部署时从 grape-skills 仓库拉取官方技能到部署根 skills/ + configs/
set -euo pipefail

SKILLS_REPO="${SKILLS_REPO:-https://gitcode.com/grape-dev/grape-skills.git}"
SKILLS_REF="${SKILLS_REF:-main}"          # 或铺入时指定 tag/commit
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"

git clone --depth 1 --branch "$SKILLS_REF" "$SKILLS_REPO" "$STAGE/skills"

# 1) 官方技能 → 部署根 skills/（保留 custom/ repo/ 运行时目录）
rm -rf "$ROOT_DIR"/skills/vod "$ROOT_DIR"/skills/issue "$ROOT_DIR"/skills/coding \
       "$ROOT_DIR"/skills/qa "$ROOT_DIR"/skills/sre "$ROOT_DIR"/skills/chat "$ROOT_DIR"/skills/_template
for d in vod issue coding qa sre chat _template; do
  [ -d "$STAGE/skills/$d" ] && cp -a "$STAGE/skills/$d" "$ROOT_DIR/skills/$d"
done

# 2) skill-description.json → 部署根 configs/
if [ -f "$STAGE/skills/skill-description.json" ]; then
  cp "$STAGE/skills/skill-description.json" "$ROOT_DIR/configs/skill-description.json"
fi

rm -rf "$STAGE"
echo "skills synced from ${SKILLS_REPO}@${SKILLS_REF}"
```

### 4.4 步骤 4：容器/部署接入

**本机/裸机部署**（DEPLOYMENT-REMOTE.md 的 `git pull` 流程后追加）：
```bash
bash scripts/sync_skills.sh        # 参数可用环境变量 SKILLS_REPO / SKILLS_REF 覆盖
```

**docker-compose 卷挂载方案（✅ 已选定）**：
```yaml
services:
  api:
    volumes:
      - ./data:/app/data
      - ./configs:/app/configs
      - ./skills:/app/skills        # 新增：技能卷（宿主机 skills/ 已由 sync_skills.sh 铺入）
```
> 注：卷挂载后宿主机 `skills/custom`、`skills/repo` 需存在（脚本保留占位 + 运行时自动创建），
> 技能更新仅需在宿主机重跑 `sync_skills.sh` 并重启容器，无需重建镜像。

**（备选，未采用）镜像内多阶段 COPY**：
```dockerfile
# 阶段 A：技能拉取
FROM alpine/git AS skills-stage
ARG SKILLS_REPO=https://gitcode.com/grape-dev/grape-skills.git
ARG SKILLS_REF=main
RUN git clone --depth 1 --branch ${SKILLS_REF} ${SKILLS_REPO} /skills

# 阶段 B：主应用
FROM python:3.12 AS app
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# 官方技能由阶段 A 铺入（运行时目录 custom/repo 用空占位）
COPY --from=skills-stage /skills/vod        /app/skills/vod
COPY --from=skills-stage /skills/issue      /app/skills/issue
COPY --from=skills-stage /skills/coding     /app/skills/coding
COPY --from=skills-stage /skills/qa         /app/skills/qa
COPY --from=skills-stage /skills/sre        /app/skills/sre
COPY --from=skills-stage /skills/chat       /app/skills/chat
COPY --from=skills-stage /skills/_template  /app/skills/_template
COPY --from=skills-stage /skills/skill-description.json /app/configs/skill-description.json
```

### 4.5 步骤 5：启动自愈（可选增强）

在服务启动时校验 37 个官方技能 SGILL.md 是否存在，缺失则打 warning 并提示运行 `sync_skills.sh`。
可放 `start_server.py` 或 `src/config.py` import 期（只读检查，不改逻辑）。

### 4.6 步骤 6：发布引入

在 grape 发布流程/CI 中把 `grape-skills@<tag>` 作为构建输入，安全地固定技能版本；
技能发生向后不兼容变更时通过 tag 主版本（`v2.x`）表达。

## 5. 影响面评估（本方案刻意最小化）

| 组件 | 是否需改 | 说明 |
|---|---|---|
| `src/config.py` SKILLS_DIR | ❌ | 物理位置不变 |
| `src/user/db.py` / router / prompt_builder | ❌ | 均基于 `SKILLS_DIR` 常量 |
| admin `app.skills-dir` | ❌ | 默认 `../../skills` 不变 |
| DB `skill` 表 / skill-description.json seed | ✅（数据随仓库走） | 文档/脚本变化，逻辑不变 |
| `skills/custom`、`skills/repo` | ✅（保留占位） | 运行时目录逻辑不变 |
| Dockerfile / docker-compose | ✅ | 卷挂载（构成）：compose 加 `./skills` 卷 |
| worker | ❌ | 技能随任务下发，不依赖部署根 |
| 测试（tests/*路径常量） | ✅ | 需改为读环境变量/相对 `SKILLS_DIR`（见 §6） |
| `.gitignore` | ✅ | 官方技能不再由 grape 跟踪，保留 custom/repo 忽略 |

## 6. 测试适配

现有测试硬编码 `skills/<agent>/<skill>/SKILL.md`（约 9 文件）：`test_vod_skill`、
`test_qa_skill`、`test_chat_skill`、`test_implementation_skill`、`test_issue_advance_skill`、
`test_sub_skill_path`、`test_runtime` 等。

- 选项 1（推荐）：测试统一改从 `src.config.SKILLS_DIR` 相对拼接（`os.path.join(SKILLS_DIR, "vod", "vod", "SKILL.md")`），并在 CI 第一步先跑 `sync_skills.sh` 拉取技能；
- 选项 2：CI 以子模块方式 checkout 技能仓库到 CI 工作区的 `skills/official/…`（复杂度高，不推荐）。

## 7. 风险与待决策项

1. **`skill-description.json` 双源漂移**：本方案让注册表随技能仓库走，构建时铺到 `configs/`。
   需约定：新增/修改官方技能时，技能仓库 PR 必须一起改该文件（CI 门禁校验 `skill-description.json` 的 path 与目录一致）。
2. **`_template/` 归属**：默认随技能仓库走（技能创建模板与技能同域维护）。若希望随 grape 代码（UI 依赖），可改为保留在 grape 仓库，此处待确认。
3. **custom/repo 运行时目录**：独立仓库不包含；任何部署必须保证部署根 `skills/` 下这两个目录存在（脚本保留 .gitkeep 占位 + 上传逻辑会自动建目录）。
4. **`skill-publish` / `skill-tester` 等「元技能」**：它们本身也是官方技能，随技能仓库走；若验收流程要求它们引用 grape 内部路径，需在版本约束上保证技能仓库 tag 与 grape 版本兼容。
5. **本地开发**：`python start_server.py` 前需先 `bash scripts/sync_skills.sh`；README 需补一句。
6. **`.grape/skills`**：本地索引副本，与代码库解耦后本就无需同步（此前已决策），保持不变。

## 8. 实施顺序与回滚

1. ✅ 技能仓库已创建：推送 37 技能 + `_template` + skill-description.json 到 `gitcode.com/grape-dev/grape-skills.git`（见 §11 阶段 1）；
2. 写 `scripts/sync_skills.sh`，本地验证铺入后启动 `python test`（skills 相关用例全绿）；
3. grape 仓库删官方技能目录 + config 中的 skill-description.json，更新 README/DEPLOYMENT/CODEOWNERS；
4. docker-compose 增加 `./skills:/app/skills` 卷挂载（宿主机先跑 `sync_skills.sh`，过渡期不动构建产物）；
5. 发布：暴露 `SKILLS_REPO`/`SKILLS_REF` 变量，默认指向已验证 tag；
6. 回滚：git 历史保留，`git checkout` 删除提交即可恢复官方技能入仓；运行时不影响（技能仍在部署根 skills/）。

## 9. 结论

方案 C 以 **构建/部署期一次 clone/copy** 完成技能与代码解耦，运行时零代码改动、
`SKILLS_DIR` 语义不变、worker 不受影响，是当前技能目录树稳定前提下的最低成本拆分。
核心风险在于「skill-description.json 随技能仓库」与「技能-代码版本兼容」两个约定，
需在实践流程（CI 校验 + tag 语义）中固化。

## 10. 待确认清单（已定稿）

- [x] `_template/` 归属：**随技能仓库走**（技能创建模板与技能同域维护）
- [x] 技能仓库托管位置 / 命名：**`https://gitcode.com/grape-dev/grape-skills.git`**，默认分支 `main`
- [x] `skill-description.json`：**随技能仓库走**（本方案取前者）
- [x] Docker 采用「镜像内 COPY（多阶段）」还是「卷挂载」：**卷挂载**（技能更新不重建镜像）
- [x] 首次拆分是否保留官方技能 git 历史：**可以保留**（`git subtree split` / filter-repo）

## 11. 执行计划

> 前置条件：当前技能重组（37 技能→agent_type 分组）与路径解析适配的代码变更尚在工作区
> 未提交（截至计划出稿时约 421 项变更）。`git subtree split` / filter-repo 需以干净提交为基底，
> 故阶段 0 必须先行。

### 阶段 0 · 基线准备（提交当前成果）

1. `git status` 检查 skills 目录移动 + 代码改动无冲突；
2. `git add -A && git commit -m "refactor(skills): 官方技能按 agent_type 分组 + 路径解析适配"`；
3. **验收**：`git log --oneline -1` 指向新提交，`git status` 干净。

### 阶段 1 · 推送技能到独立仓库（仓库已创建）

4. ✅ 远端 `grape-skills.git` 已创建（`gitcode.com/grape-dev/grape-skills.git`，默认分支 `main`）；
5. 本地 clone 空仓库，放入 7 个官方目录（`vod/ issue/ coding/ qa/ sre/ chat/ _template/`）
   + `skill-description.json`，commit 后 push 到 `main`；
6. 技能仓库补齐 `.gitignore`、README；
7. 打 tag：`git tag v1.0.0 && git push --tags`；
8. **验收**：远端 clone 后 37 技能 + `_template` + `skill-description.json` 齐全。

### 阶段 2 · 新增同步脚本

9. grape 仓库写 `scripts/sync_skills.sh`（§4.3，默认
   `SKILLS_REPO=https://gitcode.com/grape-dev/grape-skills.git`、`SKILLS_REF=main`）；
10. 本地执行一次，验证 `skills/{vod…chat}` + `_template` + `configs/skill-description.json` 铺入正确；
11. **验收**：skills 相关用例全绿
    （`tests/test_sub_skill_path.py`、`tests/test_vod_skill.py`、`tests/test_qa_skill.py`、
     `tests/test_chat_skill.py`、`tests/test_implementation_skill.py`、`tests/test_issue_advance_skill.py`、
     `tests/test_runtime.py`）。

### 阶段 3 · grape 仓库清理官方技能

12. `git rm -r skills/vod skills/issue skills/coding skills/qa skills/sre skills/chat skills/_template`；
13. `configs/skill-description.json` 由构建期 sync 脚本产物放置（随仓库版本），grape 仓库不再直接维护；
14. 保留 `skills/custom/`、`skills/repo/` 占位（`.gitkeep`）；
15. 更新 README / DEPLOYMENT-REMOTE.md / DEPLOYMENT-WORKER.md / CODEOWNERS；
16. 提交并推送；
17. **验收**：grape 仓库不含官方技能；执行 `sync_skills.sh` 后可完整启动服务。

### 阶段 4 · 部署接入

18. docker-compose 增加 `- ./skills:/app/skills` 卷（§4.4 正式方案）；
19. 宿主机首次部署先跑 `bash scripts/sync_skills.sh`，再 `docker compose up -d`；
20. （可选）启动自愈校验：启动时检查 37 个官方技能 SKILL.md，缺失打 warning 并提示先同步。

### 阶段 5 · 验证与发布

21. 全量回归：skills / chat / prompt_builder / onboarding / runtime 相关测试；
22. 冒烟：`POST /api/skills/sync-official` 返回 37，官方技能列表可读；
23. README/DEPLOYMENT 补齐"先 sync 再启动"说明；
24. **回滚预案**：`git revert` 阶段 3 的提交即可恢复官方技能入仓；运行时不受影响
    （技能仍在部署根 `skills/`，见 §8.6）。