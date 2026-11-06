# Tasks: Issue #666

- [x] M1 builder.py 环境自检告警（一次性、含影响与恢复指引）
- [x] M2 builder.py 降级分支模块级候选（git tree + API 兜底 + 无来源回退）
- [x] M3 Dockerfile 引擎镜像预装 codegraph（best-effort）
- [x] M4 worker 侧：MANIFEST 纳入 install-codegraph.sh；install-worker.sh 校验；requirements-worker.txt 说明
- [x] M5 测试：降级候选/告警一次性/MANIFEST 守卫；全量相关 pytest 回归
