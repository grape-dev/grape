# 引擎必须以任务属主身份运行（不能是 root）

2026-11-16 事故：#96 / #97 的 issue 流水线卡在 `running / 无执行引擎`，根因不是调度器，
而是**引擎以 root 身份跑任务**，把 `grape-workspaces` 写成 root 拥有，业务侧（grape）
随后写 `issues/<n>/<agent>` 直接 EACCES。

## 证据链

```
1) 116 上 /etc/systemd/system/runtime-agent.service 里写着 User=root
   → agent 引擎（-m src.domains.runtime.engine.agent）以 root 运行
2) 该引擎执行 VOD 任务时创建 /home/grape/grape-workspaces/gitcode/songspace/flask-blog/issues/95|96
   属主 root:root（find -user root 在 116 上命中 11675 条，213 为 0）
3) 交棒（a2a_event）在 grape 进程里写 issues/96/issue：
   [Errno 13] Permission denied: '.../issues/96/issue'
   → 派发失败
4) 失败后本应把事件标 failed，但 event_store 的终态写入先执行
   PRAGMA table_info(events)（SQLite 专用），三库 MySQL 下 1064
   → 整条 UPDATE 被 except 吞掉 → 事件永远停在 running（UI 显示「无执行引擎」）
```

## 修复

代码（两侧都已上线）：

- `src/domains/runtime/engine/agent.py`：`__main__` 里 fail-fast——`geteuid()==0` 且未设
  `GRAPE_ALLOW_ROOT_ENGINE=1` 直接 `SystemExit(78)`，把「静默写坏属主」变成启动即拒。
- `src/domains/runtime/event_store.py`：`_events_columns()` 双后端取列（MySQL 走
  information_schema，SQLite 走 PRAGMA，进程内缓存），终态 UPDATE 不再 1064。

运维（116）：

```bash
chown root:grape /etc/grape/runtime.env && chmod 640 /etc/grape/runtime.env
sed -i 's/^User=root$/User=grape\nGroup=grape/' /etc/systemd/system/runtime-agent.service
systemctl daemon-reload && systemctl restart runtime-agent.service
find /home/grape/grape-workspaces -user root -print0 | xargs -0 -r chown grape:grape
```

## 验收

```
engine 进程属主             grape（原来 root）
root 拥有文件（116）         11675 → 4（剩余为 .venv 内符号链接，无影响）
修复后 Permission denied    0（两侧日志）
#95/#96 流水线              issue 段 completed → coding 段 running（9829/9831）
#97                        重新触发 vod（seq 9832）→ running
终端状态写入                事件能正常落 failed/completed（9825 = completed）
```

## 教训

1. 引擎 unit 的 `User=` 必须等于工作区属主。安装脚本以 root 执行时会把 unit 写成
   `User=root`，这是隐雷——所以代码侧加了 root 拒启动。
2. 任何 `PRAGMA ...` / `sqlite_master` 常驻代码在多后端路径上都要走后端判断；
   SQLite 专有语句被服务端拒绝后，`except` 吞掉会造成「状态写不进去」的静默故障。