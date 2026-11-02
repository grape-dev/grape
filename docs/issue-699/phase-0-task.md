# Phase 0 Task Breakdown

> Issue #699 Phase 0 | 配套: phase-0-spec.md / phase-0-plan.md

| # | Task | Files | Status |
|---|------|-------|--------|
| 1 | 删除 scenes/，移动 tasks/→docs/plans/ | scenes/*, tasks/*, docs/plans/ | pending |
| 2 | 部署文件归入 deploy/ | nginx-grape.conf, Dockerfile, docker-compose.yml → deploy/ | pending |
| 3 | data/ wheels/ .gitkeep | .gitignore, data/.gitkeep, wheels/.gitkeep | pending |
| 4 | 创建 Makefile | Makefile | pending |
| 5 | 创建 pyproject.toml + 删 requirements + 更新引用 | pyproject.toml, worker_package.py, install-worker.sh/ps1, release_pack.py, deploy/Dockerfile | pending |
| 6 | 技能目录统一 .grape/skills/ | config.py, sync_skills.sh, adapt_skills_vcs.sh, .gitignore, .opencode/→.grape/skills/ | pending |
| 7 | Phase 0 验证测试 | tests/arch/test_phase0_cleanup.py | pending |
| 8 | 自审查 + pre-pr-check | — | pending |
| 9 | 建 PR + 评论 + A2A QA | — | pending |

## 验收用例映射

| TC | 对应 Task | 验证方式 |
|----|----------|---------|
| TC-0.1 | 1 | scenes/ 不存在 + tasks/ 在 docs/plans/ + 无残留引用 |
| TC-0.2 | 6 | .grape/skills/ 存在 + SKILLS_DIR 指向 .grape/skills/ |
| TC-0.3 | 2 | deploy/ 含三个部署文件 + 顶层无 |
| TC-0.4 | 5 | pyproject.toml 存在 + pip install -e . 正常 |
| TC-0.5 | 4 | Makefile 存在 + make help 可用 |
