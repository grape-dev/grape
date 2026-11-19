# Plan: Issue #2132 实施计划（旧布局生产 hotfix twin）

> 基线 `the legacy-layout branch`（旧布局）；实现与 PR #1271（the reviewed ljt line，已过 QA）等价。
> 提交拆分沿用 TDD 红/绿：先测试（红）后实现（绿），每竖切片一提交。

## 变更拆分

1. **A 线（`src/observability/rollup.py` + `src/api/routes/observability.py` + JS）**
   - SQL：`WHERE runtime_id IN (...) AND sampled_at >= ?`；cutoff = `now - RANGE_SPECS[range][0]`。
   - 谓词仅对 `sqlite3.OperationalError/NotSupportedError`（方言/语句不支持）回退，回退查询带 `ORDER BY sampled_at DESC LIMIT FALLBACK_ROW_CAP(50 万)` 行数上限 + 触顶 warning；瞬时异常不回退（避免放大成锁内全表扫描）。
   - `with db_lock` 只包 `execute/fetchall`；`rows` 快照在锁外迭代做分桶聚合；路由侧行转换同样移锁外。
   - JS：`_obsPageVisible()` 读 `document.visibilityState`；`startObsAuto` 后台不启动、tick 内再判；`visibilitychange` 隐藏→停表、可见→立即刷新 + 设备分区补拉 + 恢复轮询。
2. **B 线（`src/api/knowledge.py` + `src/runtime/service.py` + `src/api/issues_center.py`）**
   - `prefetch_repo_knowledge()`：DB 只取 (obs_key, content_hash, file_path) 元数据（持 db_lock 短平快），正文逐条 `asyncio.to_thread(read_entry_content)`，`asyncio.Semaphore` 限并发；limit+1 探测截断。
   - `collect_knowledge_files_async()`：整段同步收集丢线程池（语义不变）。
   - `warm_knowledge_cache()` + `_warm_knowledge_cache_bounded()`：建单（created）前置预热，`KNOWLEDGE_WARM_BUDGET_SEC`（默认 0.8s）预算内 `shield` 等待，超时转后台（强引用防 GC），请求不挂住。
   - `agent_knowledge_files` / `member_files_pi` SQL `ORDER BY k.id DESC LIMIT ?`（limit+1 探测截断 + 计数）。
   - `collect_knowledge_files` 防御性截断（最新优先，沿用插入序）+ `truncated` 返回字段。
   - 上限解析：env 默认 + `configs/knowledge-limits.json`（`default` / `repos` 覆盖，按 mtime 进程内缓存）。
   - `_knowledge_cache` 值改 `(content, hash, cached_at)`；TTL 判定 + LRU 容量淘汰；`_env_positive_int` 令 TTL 0/负值回退默认 300s。
3. **运维可观测（`src/api/health_endpoints.py`）**
   - `/health/realtime` 暴露 `knowledge_cache` 字段（hit/miss/truncated_files/size/max/ttl_sec）。
4. **CI（`.github/workflows/windows-smoke.yml`）**
   - `linux-backend` 增加本 issue 回归测试步骤（2 个 pytest 文件 + 1 个 node 文件）。

## 验证

```bash
python3 -m pytest tests/test_obs_rollup.py tests/test_issue_2132_obs_history.py -q
python3 -m pytest tests/test_issue_2132_knowledge_limits.py -q
python3 -m pytest tests/test_knowledge_ref.py tests/test_issue190_knowledge_dedup.py -q
node tests/js/test_issue_2132_obs_visibility.js
node tests/js/test_observability_sections.js
node tests/js/test_observability_events_flow.js
python3 -c "import src.api.main; print('import OK')"
```

## 回滚

- A 线与 B 线互相独立，可分别 revert：A 线仅 `rollup.py` + 路由 + JS；B 线仅 `knowledge.py` + `service.py` + `issues_center.py`。
- B 线默认阈值宽松（1000 条 / 16 并发 / 300s TTL / 0.8s 预算），必要时用环境变量即时收窄或放宽。
