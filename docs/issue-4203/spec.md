# Issue #4203 — worker OOM 根因消除与重启窗口收敛

> Spec 来源：Issue #4203 评论 id 203318945（Issue Agent 发布）| 开发基线：`arch-weekly-11-ljt`

## 1. 背景

某主控机 24h 内多次 OOM kill（间隔分钟到小时量级）；最近一次主进程被 SIGKILL 后
systemd 重启 + uvicorn 启动共十余秒，窗口内网关 502 计数在数百量级，其中 `tasks/complete` 3 次、
`tasks/*/messages` 28 次回执丢失（任务可能卡 running）。被杀前节点 MemAvailable 约四成，
属进程/cgroup 级内存失控而非整机耗尽。

## 2. 目标

1. worker 进程与引擎子进程 RSS **可采集**（/metrics 暴露，含 cgroup 限额与比值）；
2. systemd 内存边界**显式化**（MemoryAccounting/MemoryHigh/MemoryMax + 启动日志打印生效值）；
3. OOM 与内存**可告警**（node_vmstat_oom_kill P1、接近上限、RSS 持续增长三条规则）；
4. 重启窗口**收敛**（RestartSec=1 + 退避；launcher 预 bind fd，先监听再 lifespan）；
5. 终态与消息回执**不丢**（先落盘 outbox 再发 + 退避续投；网络错误批次持久化）。

## 3. 非目标

- #3969 网关多上游本体（已合入，本单仅 004 核对，由 SRE 执行）；
- #3352 complete 超时槽位回收；
- 长跑任务业务内存优化（先靠采集定位，另开单）。

## 4. 现状盘点（仓库既有能力）

| 能力 | 现状 | 缺口 |
|------|------|------|
| 终态回执 outbox | `worker_report_queue.py` 已有：先 `q.add` 落盘再 `client.complete/fail`，ack 后出队，10s 重放循环 + run_token 幂等守卫 | 已落地，无缺口 |
| 消息回执 | 4xx 降级路径已落 outbox（#975）；网络错误只保留内存 batch 重试 | **SIGKILL 时内存 batch 丢失**（11:39 事故 28 条的成因）|
| 进程内存指标 | 无 | 全部新增 |
| systemd 单元 | 无 MemoryAccounting/High/Max，RestartSec=5；生产 worker 走 `grape@worker.service → scripts/grape-systemd-run.sh`，静态单元不一定是实际入口 | 两条路径都要落：静态单元内联参数 + `grape@worker.service` drop-in（`deploy/systemd/grape-worker-oom.conf`）|
| 告警 | 无 OOM/内存类规则 | 全部新增 |
| 端口先绑 | `--workers 2` 下 uvicorn supervisor 在 spawn 前 bind（多 worker 已满足） | 单 worker 路径无预 bind；统一由 launcher 预 bind socket 对象收口（worker 两条启动路径都改走 launcher）|

## 5. Open Question（不阻塞）

004 实跑单元端口 8083/9083 与仓库端口事实源（#3764：mgmt/event/worker/insight =
38081/38082/38083/38084）不符；MemoryMax 取值与 RSS 增长源
需 SRE 的 `systemctl cat` / `dmesg` victim 行。开发阶段按 MemoryHigh + 采集先落地，
MemoryMax 写入保守默认值并在注释中标注待 SRE 实测校准。

004 实际托管 unit 尚未取得证据 → 本单**不赌单一入口**：静态单元
`grape-worker-server.service` 与模板单元 `grape@worker.service`（含 drop-in）两条
路径都落同一套 Memory*/Restart*/预 bind，取证后只需按宿主机校准取值
（见 docs/weekly-11/deployment-runbook.md §3.1）。
