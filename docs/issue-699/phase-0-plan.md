# Phase 0 Plan: 实施方案设计

> Issue #699 Phase 0 | 配套: phase-0-spec.md / phase-0-task.md

## 实施顺序（原子提交，每步一个 commit）

### Step 1: 删除 scenes/ + 移动 tasks/ → docs/plans/
- `git rm -r scenes/`（10 场景，20 文件）
- `mkdir docs/plans/ && git mv tasks/*.md docs/plans/`
- 验证：`grep -r "scenes/" src/ --include="*.py" | grep -v configs/scenes` → 空

### Step 2: 部署文件归入 deploy/
- `git mv nginx-grape.conf deploy/`
- `git mv Dockerfile deploy/`
- `git mv docker-compose.yml deploy/`
- 更新 `deploy/docker-compose.yml`：`build: {context: .., dockerfile: deploy/Dockerfile}`，volumes `../skills:/app/skills` → `../.grape/skills:/app/.grape/skills`
- 更新 `deploy/Dockerfile`：`COPY requirements.txt .` → `COPY pyproject.toml .`，`RUN pip install -r requirements.txt` → `RUN pip install -e .`（Step 5 后一起改）

### Step 3: data/ wheels/ .gitkeep
- `touch data/.gitkeep wheels/.gitkeep`
- .gitignore 加 `!data/.gitkeep` `!wheels/.gitkeep`

### Step 4: Makefile
- 新建 Makefile：help/test/run/lint/install/sync-skills/clean

### Step 5: pyproject.toml + 删除 requirements + 更新依赖引用
- 新建 pyproject.toml（合并 4 个 requirements）
- `git rm requirements.txt requirements-worker.txt requirements-dev.txt requirements-local.txt`
- 更新 `src/worker_package.py` MANIFEST：`requirements-worker.txt` → `pyproject.toml`
- 更新 `scripts/install-worker.sh`：`pip install -r requirements-worker.txt` → `pip install -e ".[worker]" --find-links wheels/`
- 更新 `scripts/install-worker.ps1`：同上
- 更新 `scripts/release_pack.py`：requirements 引用 → pyproject.toml
- 更新 `deploy/Dockerfile`：`COPY pyproject.toml .` + `RUN pip install -e .`

### Step 6: 技能目录统一到 .grape/skills/
- `git mv .opencode/skills/grape-dev-workflow .grape/skills/dev-workflow`
- 删除 `.opencode/` 空目录
- 更新 `src/config.py:116`：`SKILLS_DIR` → `.grape/skills`
- 更新 `scripts/sync_skills.sh`：`ROOT_DIR/skills/$d` → `ROOT_DIR/.grape/skills/$d`
- 更新 `scripts/adapt_skills_vcs.sh`：`SKILLS_DIR="$ROOT_DIR/skills"` → `.grape/skills`
- 更新 `.gitignore`：移除 `!.opencode/` 例外；加 `.grape/skills/` 例外（track qa/ + dev-workflow/，ignore synced official）

### Step 7: 验证测试
- 新建 `tests/arch/test_phase0_cleanup.py`：TC-0.1~TC-0.5 自动化验证

## commit message 规范（全英文）
- `chore: #699 remove duplicate scenes/ dir and move tasks/ to docs/plans/`
- `chore: #699 move deploy files (nginx/Dockerfile/compose) into deploy/`
- `chore: #699 add .gitkeep for data/ and wheels/ dirs`
- `chore: #699 add Makefile with unified build targets`
- `chore: #699 add pyproject.toml and remove requirements files`
- `chore: #699 unify skills dir to .grape/skills/ and update SKILLS_DIR`
- `test: #699 add Phase 0 verification tests (TC-0.1~TC-0.5)`
