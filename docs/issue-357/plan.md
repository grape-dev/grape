# Plan: Issue #357 — 实施方案设计

## 改动文件清单

| 文件 | 改动 | P级 |
|------|------|-----|
| `src/mysql_compat.py` | MySQLCompatDB 接 DBUtils PooledDB + DB_POOL_DISABLED 回滚 | P1-A |
| `src/api/pipeline_config.py` | 进程级缓存 _SCENE_CACHE + SHOW COLUMNS 一次性 + 写路径失效 | P1-B |
| `src/api/issues_center.py` | _query_tasks 循环外批量解析 scene_name | P1-B |
| `src/api/routes/scenes.py` | experience_market_scene 外部调用 asyncio.wait_for 10s/30s 超时 | P1-C |
| `src/runtime/task_lock.py` | 新增：分布式锁 async context manager | P1-D |
| `src/storage/scene_sync.py` | _scene_sync_worker 加分布式锁 | P1-D |
| `src/storage/skill_sync.py` | _skill_sync_worker 加分布式锁 | P1-D |
| `src/storage/upload_cleanup.py` | _upload_cleanup_worker 加分布式锁 | P1-D |
| `src/runtime/scheduler.py` | scheduler_loop 加分布式锁 | P1-D |
| `src/api/routes/issues.py` | 列表响应瘦身（去 agent_statuses/task_description，latest_event 4 字段） | P1-E |
| `src/user/db.py` | user_credentials DELETE 改 LIMIT 100 分批循环 | P2-A |
| `src/api/db_setup.py` | events(repo_key, issue_id) 复合索引 | P2-B |
| `src/runtime/table_maintenance.py` | 新增：agent_tasks/task_messages 定时清理 | P2-C |
| `src/api/main.py` | 注册 table_maintenance 定时任务 | P2-C |
| `src/api/routes/runtime.py` | grape-worker.tar.gz 支持 X-Accel-Redirect | P2-D |
| `src/api/static/js/pages/issues.js` | 轮询 15s→30s | P2-E |
| `requirements.txt` | 添加 DBUtils>=3.0 | P1-A |
| `tests/test_issue357_health.py` | 新增：14 个测试 | 测试 |
| `tests/test_mysql_compat.py` | 适配连接池模式 | P1-A |

## 回滚开关
- `DB_POOL_DISABLED=1`：一键回滚连接池到旧的每线程裸连接模式
- `GRAPE_WORKER_NGINX_REDIRECT` 不设置：回退到 FileResponse 直出
- 缓存 TTL 60s：写路径主动失效 + 自然过期双保险
- 分布式锁失败时 skip（不阻塞），任务下一轮重试

## 风险
- 连接池化动全平台 DB 访问层：灰度建议先读路径观察，保留 DB_POOL_DISABLED 回滚
- 多 worker 定时任务：分布式锁保证仅 1 个 worker 执行
- 分批 DELETE：LIMIT 100 小批量，不阻塞业务写入
