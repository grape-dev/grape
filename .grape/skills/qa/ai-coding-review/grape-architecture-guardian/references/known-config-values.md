# Grape 已验证配置数值注册表

> 每个数值附带来源和适用范围。审查时遇到这些数值不需要重复质疑，
> 但需要验证其未被偏离。

## 服务端部署

| 参数 | 值 | 来源 | 文件 | 偏离判定 |
|------|-----|------|------|----------|
| uvicorn workers | 1 | db_lock 跨进程无效 + 定时任务重复 | 启动脚本/部署配置 | ≠1 → BLOCKED |
| uvicorn host | 0.0.0.0 | 需外网可达 | 启动脚本 | — |
| uvicorn port | 38080 | 生产端口约定 | 启动脚本 | — |
| uvicorn log-level | info | 生产日志级别 | 启动脚本 | — |

## Runtime 调度

| 参数 | 值 | 来源 | 文件 | 偏离判定 |
|------|-----|------|------|----------|
| CLAIM_INTERVAL | 3.0s | 与 worker RUNTIME_POLL_INTERVAL 一致 | src/runtime/local_claimer.py | — |
| CLAIM_BACKSTOP_DELAY | 15s | worker 3s 轮询 + 0~0.9s jitter，15s ≈ 4-5 轮 | src/runtime/local_claimer.py | — |
| CLAIMER_ENABLED | True (默认) | GRAPE_LOCAL_CLAIMER 环境变量 | src/runtime/local_claimer.py | — |

## 分布式锁

| 参数 | 值 | 来源 | 文件 | 偏离判定 |
|------|-----|------|------|----------|
| lock TTL | 60s | 防止死锁，过期自动释放 | src/lock.py | — |
| cleanup interval | 5min | 后台定时清理过期锁 | src/lock.py | — |

## 测试

| 参数 | 值 | 来源 | 文件 | 偏离判定 |
|------|-----|------|------|----------|
| pytest timeout | 120s | pytest.ini | pytest.ini | — |
| DEVOPS_DB_BACKEND (测试) | sqlite | conftest.py 强制覆盖 | tests/conftest.py | ≠sqlite → BLOCKED |
| asyncio_mode | auto | pytest.ini | pytest.ini | — |

## 网络

| 参数 | 值 | 来源 | 文件 | 偏离判定 |
|------|-----|------|------|----------|
| api.gitcode.com SSL verify | False | GFW SNI 拦截 | src/config.py | — |
| PIP_INDEX_URL | pypi.tuna.tsinghua.edu.cn | 中国网络镜像 | src/worker_package.py | — |

## 前端版本号

| 文件 | 当前版本 | 来源 |
|------|----------|------|
| app.js | v170 | index.html ?v=170 |
| issues.js | v113 | index.html ?v=113 |
| runtimes.js | v59 | index.html ?v=59 |

> 审查时：如果 PR 修改了上述 JS 文件但未 bump 版本号 → Critical。

## 验证方式

审查时遇到不在本注册表中的配置数值：
1. 运行 `scripts/magic_number_audit.py` 扫描
2. 查找附近注释是否有来源说明
3. 查找项目文档是否有 rationale
4. 如均无 → 标注 `INSUFFICIENT DATA TO VERIFY` + 记录到报告中
