"""DDL：insight 域基表建表语句（由 db_setup 拆分而来，归属本域）。

搬迁原因见 scripts/split_schema_ddl.py：DDL 属表的归属域（方案 D9），
放在 owner 域模块里才能让「跨域引用」统计反映真实依赖。
行为不变：db_setup 仍按原顺序执行这些语句。
"""

_MERGE_GATE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_merge_gate_log (
    id         TEXT PRIMARY KEY,
    repo_key   TEXT NOT NULL DEFAULT '',
    pr_number  INTEGER NOT NULL DEFAULT 0,
    phase      TEXT NOT NULL DEFAULT '',
    decision   TEXT NOT NULL DEFAULT '',
    hits_json  TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
)
"""

_API_LATENCY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS api_latency_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id VARCHAR(32) DEFAULT '',
    method VARCHAR(8) DEFAULT '',
    path VARCHAR(256) DEFAULT '',
    status INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    repo_key VARCHAR(128) DEFAULT '',
    user_id VARCHAR(32) DEFAULT '',
    source VARCHAR(16) DEFAULT 'backend',
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_OBS_LLM_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS obs_llm_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key          TEXT NOT NULL UNIQUE,        -- '{task_id}:{seq}' 幂等键(单列,兼容方言翻译)
    task_id            TEXT NOT NULL,               -- agent_tasks.id
    seq                INTEGER NOT NULL DEFAULT 0,  -- 确定性序号,见 llm_usage.stable_seq
    turn               INTEGER NOT NULL DEFAULT 0,  -- 轮次(引擎分轮上报时用)
    source             TEXT DEFAULT '',             -- report/codex/opencode/hermes/hwcloud
    repo_key           TEXT DEFAULT '',
    scene_id           TEXT DEFAULT '',
    agent_type         TEXT DEFAULT '',
    engine_id          INTEGER DEFAULT 0,
    user_id            TEXT DEFAULT 0,
    provider_raw       TEXT DEFAULT '',             -- 引擎上报的原始 provider
    model_raw          TEXT DEFAULT '',             -- 引擎上报的原始模型名
    model_key          TEXT DEFAULT '',             -- 抽样归一化后的模型名(聚合用)
    run_id             TEXT DEFAULT '',             -- 运行主体:task→同 task_id;其它→chat:<conv>:<msg> 等
    run_kind           TEXT DEFAULT 'task',         -- task/chat/devops/im/wiki(主控机内跑的引擎算后者)
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL DEFAULT NULL,           -- 引擎侧成本(可空,与自算成本交叉校验)
    cost_status        TEXT DEFAULT '',             -- ok/unknown/none
    created_at         TEXT DEFAULT (datetime('now')),
    updated_at         TEXT DEFAULT (datetime('now'))
)
"""

_OBS_LLM_USAGE_ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS obs_llm_usage_archive (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key          TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    seq                INTEGER NOT NULL DEFAULT 0,
    turn               INTEGER NOT NULL DEFAULT 0,
    source             TEXT DEFAULT '',
    repo_key           TEXT DEFAULT '',
    scene_id           TEXT DEFAULT '',
    agent_type         TEXT DEFAULT '',
    engine_id          INTEGER DEFAULT 0,
    user_id            TEXT DEFAULT 0,
    provider_raw       TEXT DEFAULT '',
    model_raw          TEXT DEFAULT '',
    model_key          TEXT DEFAULT '',
    run_id             TEXT DEFAULT '',
    run_kind           TEXT DEFAULT 'task',
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL DEFAULT NULL,
    cost_status        TEXT DEFAULT '',
    created_at         TEXT DEFAULT '',
    updated_at         TEXT DEFAULT '',
    archived_at        TEXT DEFAULT (datetime('now'))
)
"""

_OBS_ALERT_DDL = """
CREATE TABLE IF NOT EXISTS obs_alert_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'metric',  -- metric(阈值求值)/gap(数据缺失探测)
    level           TEXT NOT NULL DEFAULT 'user',   -- user(用户面) / platform(仅 L2/L3)
    metric          TEXT NOT NULL,                  -- kind=metric:指标名; kind=gap:gap_runtime/gap_repo
    comparator      TEXT NOT NULL DEFAULT 'gt',      -- gt/lt/gte/lte
    threshold       REAL NOT NULL DEFAULT 0,
    window_sec      INTEGER NOT NULL DEFAULT 300,    -- 指标统计窗口
    for_sec         INTEGER NOT NULL DEFAULT 300,    -- 持续满足多久才触发（抑制抖动）
    severity        TEXT NOT NULL DEFAULT 'warning',  -- critical/severe/important/warning/info
    dim_filter      TEXT DEFAULT '',                 -- 预留: repo=o/r
    enabled         INTEGER NOT NULL DEFAULT 1,
    notify_channels TEXT DEFAULT '',                 -- 空=只记录不推送；feishu/im/email
    -- 策略层（P1-1）：通知节流 / 聚合 / 时段 / 升级
    repeat_sec            INTEGER NOT NULL DEFAULT 0,  -- 0=只通知一次；>0=未恢复时每 N 秒复推
    aggregate_mode        TEXT NOT NULL DEFAULT 'none',-- none/rule(窗口内合并)/period(周期汇总)
    aggregate_window_sec  INTEGER NOT NULL DEFAULT 0,  -- 聚合窗口；mode!=none 且为 0 时按 300
    notify_window_json    TEXT NOT NULL DEFAULT '',    -- 空=全天；{"days":[1..7],"start":"09:00","end":"19:00"}
    escalate_json         TEXT NOT NULL DEFAULT '',    -- 空=不升级；{"after_sec":1800,"severity":"critical"}
    description     TEXT DEFAULT '',
    seed_source     TEXT DEFAULT '',                 -- 来源: deploy/alerts.yml 的 alert 名
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)
"""

_OBS_ALERT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS obs_alert_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id        INTEGER NOT NULL,
    rule_name      TEXT NOT NULL,
    level          TEXT NOT NULL DEFAULT 'user',
    severity       TEXT NOT NULL DEFAULT 'warning',
    status         TEXT NOT NULL DEFAULT 'firing',   -- firing/resolved
    source         TEXT NOT NULL DEFAULT 'monitor',  -- monitor(指标)/gap(数据缺失)/mutation(突变)/slo
    metric         TEXT DEFAULT '',
    value          REAL DEFAULT 0,
    threshold      REAL DEFAULT 0,
    dim_key        TEXT DEFAULT '',                  -- repo:<repo_key> / global
    repo_key       TEXT DEFAULT '',                  -- 冗余，便于按仓库过滤
    first_fired_at TEXT DEFAULT '',
    last_seen_at   TEXT DEFAULT '',
    resolved_at    TEXT DEFAULT '',
    silenced_until TEXT DEFAULT '',                  -- 非空且未过期 => 静默（不推送，仍记录）
    link_task_id   TEXT DEFAULT '',
    -- 策略层状态（P1-1）：通知节流 / 时段延后 / 升级
    notify_count     INTEGER NOT NULL DEFAULT 0,     -- 已通知次数（0=只记录未推）
    last_notified_at TEXT DEFAULT '',                -- 上次成功通知时间（重复抑制用）
    escalated_at     TEXT DEFAULT '',                -- 升级通知时间（非空=已升级过）
    notify_deferred  INTEGER NOT NULL DEFAULT 0,     -- 1=因通知时段只记录（窗口内恢复后补推一次）
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
)
"""

_OBS_RUNTIME_SAMPLES_DDL = """
CREATE TABLE IF NOT EXISTS obs_runtime_samples (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id        INTEGER NOT NULL,             -- agent_runtimes.id / runtime_credentials.id
    device_name       TEXT DEFAULT '',
    sampled_at        TEXT NOT NULL,
    -- 在线信号（P2-1 SLO 用）：采样时刻该引擎心跳是否新鲜（last_seen_at 在 ONLINE_WINDOW 内）。
    -- 注意 health_score 来自 runtime_health_scores，是"近 N 天任务健康度"**不是在线**，
    -- 且 dev 库实测 7454 行全为 0 —— 所以可用率必须另立此列，不能拿 health_score 顶。
    online            INTEGER NOT NULL DEFAULT 0,
    cpu_percent       REAL DEFAULT 0,
    memory_percent    REAL DEFAULT 0,
    disk_percent      REAL DEFAULT 0,
    bandwidth_percent REAL DEFAULT 0,
    health_score      INTEGER DEFAULT 0,
    status            TEXT DEFAULT ''
)
"""

_OBS_METRIC_ROLLUP_DDL = """
CREATE TABLE IF NOT EXISTS obs_metric_rollup (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution   TEXT NOT NULL DEFAULT 'minute', -- minute(7天) / hour(30天)
    bucket_start TEXT NOT NULL,                  -- 桶起点（ISO UTC，对齐分钟/小时）
    metric       TEXT NOT NULL,                  -- task_done / task_success_rate / token_total ...
    dim_key      TEXT NOT NULL DEFAULT '',       -- repo:<repo_key> / global
    repo_key     TEXT NOT NULL DEFAULT '',       -- 冗余：用户面按可见仓库过滤
    value        REAL NOT NULL DEFAULT 0,        -- 聚合值（计数/比率/均值/p95/求和）
    cnt          INTEGER NOT NULL DEFAULT 0,     -- 样本数（比率类=分母，供加权平均）
    updated_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (resolution, bucket_start, metric, dim_key)
)
"""

_OBS_SLO_DDL = """
CREATE TABLE IF NOT EXISTS obs_slo (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,            -- 稳定标识（前端/事件按它取文案）
    sli_type       TEXT NOT NULL,                   -- task_success_rate / task_queue_wait_p95 / engine_availability
    scope_type     TEXT NOT NULL DEFAULT 'global',  -- repo / global / engine
    scope_key      TEXT NOT NULL DEFAULT '',        -- scope_type=repo 时的仓库短名；global/engine 留空
    target         REAL NOT NULL DEFAULT 0.99,      -- 目标（比例；时延类=达标时间占比）
    window_days    INTEGER NOT NULL DEFAULT 30,     -- 考核窗口（天）
    threshold_json TEXT NOT NULL DEFAULT '{}',      -- 时延类的 {"max_p95_sec": 300} 等
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
)
"""

_OBS_SLO_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS obs_slo_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    slo_id           INTEGER NOT NULL,
    bucket_start     TEXT NOT NULL,                 -- 小时桶（ISO UTC）
    window_start     TEXT NOT NULL DEFAULT '',      -- 该次计算覆盖的窗口
    window_end       TEXT NOT NULL DEFAULT '',
    good             REAL NOT NULL DEFAULT 0,       -- 窗口内达标量（成功数 / 达标桶数 / 在线采样数）
    total            REAL NOT NULL DEFAULT 0,       -- 窗口内总量（分母）
    value            REAL NOT NULL DEFAULT 0,       -- SLI 值（比例）
    budget_total     REAL NOT NULL DEFAULT 0,       -- 错误预算总量 =(1-target)*有数据桶数
    budget_consumed  REAL NOT NULL DEFAULT 0,       -- 已消耗（坏桶数）
    state            TEXT NOT NULL DEFAULT 'ok',    -- ok / warning / breached / no_data
    computed_at      TEXT DEFAULT (datetime('now')),
    UNIQUE (slo_id, bucket_start)
)
"""

_OBS_LOG_DDL = """
CREATE TABLE IF NOT EXISTS obs_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT NOT NULL UNIQUE,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'WARNING',
    component   TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    trace_id    TEXT DEFAULT '',
    task_id     TEXT DEFAULT '',
    repo_key    TEXT DEFAULT '',
    runtime_id  INTEGER DEFAULT 0,
    msg         TEXT DEFAULT '',
    extra_json  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
)
"""


# ===== 仓库凭据单表（S80/D55 + D99）=====
# 归属 insight：读写语句全在 insight public（runtime public 转发）。DDL 原在
# src/api/db_setup.py 的迁移函数里（2026-11-15 归位），建表仍由该迁移触发，
# 但语句文本归 owner 域，避免「DDL 在 api、属主在 insight」的口径分裂。
_REPO_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_repo_credentials (
    id VARCHAR(256) NOT NULL PRIMARY KEY,
    repo_key TEXT NOT NULL,
    cred_type TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (repo_key, cred_type)
)
"""
