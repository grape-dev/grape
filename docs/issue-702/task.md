# Task: Issue #702 单写者租约

- [x] Task 1: 租约模块 sample_lease.py（acquire/release/status + 降级）
  - Acceptance: 互斥/续期/失联接管/降级测试全绿
  - Verify: `pytest tests/test_obs_sample_lease.py -q`
  - Files: src/observability/sample_lease.py, tests/test_obs_sample_lease.py
- [x] Task 2: 清理职责拆分（snapshot 不再 DELETE；新增 cleanup_expired_samples）
  - Acceptance: snapshot 后超龄样本保留；cleanup 后清除
  - Verify: `pytest tests/test_obs_sample_lease.py tests/test_obs_rollup.py -q`
  - Files: src/observability/rollup.py, tests
- [x] Task 3: 单写编排 sample_once_under_lease + main.py 循环接入
  - Acceptance: 他主持有时 0 INSERT 0 DELETE；持锁时正常采样+清理
  - Verify: `pytest tests/test_obs_sample_lease.py -q`（多主模拟用例）
  - Files: src/observability/rollup.py, src/api/main.py
- [x] Task 4: 回归 + 文档
  - Acceptance: obs 域 + atomic 套件全绿
  - Verify: `pytest tests/test_obs_sample_lease.py tests/test_obs_rollup.py tests/atomic -q`
  - Files: docs/issue-702/*
