# Tasks: Issue #757

- [ ] T1 `mask_heredoc_bodies` 实现（含 `<<'EOF'`/`<<"EOF"`/`<<EOF`/`<<-EOF`、未闭合保守）+ 单测
- [ ] T2 `mask_python_c_literals` 实现（`python`/`python3`/`python3.x -c` 单/双引号）+ 单测
- [ ] T3 `extract_write_targets` 实现（仅 `>`/`>>`/`&>`/`&>>`/`N>`/`N>>`；fd 复制跳过）+ 单测
- [ ] T4 验收三场景用例（heredoc 内容路径 / python3 -c 路径字面量 / 重定向目标保留）
- [ ] T5 回归：`pytest tests/test_command_paths.py` 全绿 + 相邻模块冒烟
- [ ] T6 自审查（5 轴 + pre-pr 增量检查）→ 建 PR base=weekly-08-mys → 评论 + A2A QA
