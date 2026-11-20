# Spec: 删除 sandbox_provider_config.name 列 + cloud-local.key 机制废弃（用户裁定 v3）

- 日期：2026-10-29（v3：并入 cloud worker key 文件机制废弃）
- 前版：v2（物理删列+清 cube）；用户追加裁定：**cloud worker 只通过页面生成凭据接入，
  key 文件机制删除**（普通 worker 无此机制，实测 agent.py 只认 RUNTIME_TOKEN env，
  key 机制是 cloud worker 在页面凭据诞生前的引导方案，现双轨并存徒增心智）。
- 现网实证：仅 2 Provider（cube/cred4 禁用 + s2/cred50 启用），全有凭据关联；
  本机 761978 手动起的 cloud_worker（PPID=1，无 env token）是 key 机制最后活体。

## 1. 用户裁定汇总

1. **物理删 `sandbox_provider_config.name` 列**——Provider 名=凭据名（rc.name JOIN）
2. **删 cube Provider**（禁用 9 天老链路；cred4 本机普通凭据保留）
3. **删 cloud-local.key 鉴权机制**——cloud worker 只认 `CLOUD_WORKER_TOKEN` env
   （页面「生成凭据」→ 一键安装注入）；key 文件与 rotate 脚本一并废弃
4. **本机收编**：761978 旧进程（key 认证）→ 页面新凭据 + 正规安装

## 2. Part A：name 列删除（同 v2 §3 十项接触面，不变）

| # | 位置 | 改动 |
|---|---|---|
| ① | db_setup DDL + SQLite 重建表迁移 | 去 name 列，数据保留 |
| ② | 列表/详情 SELECT（settings.py:3073/3080） | `rc.name AS name`（前端零改动） |
| ③ | 搜索（3057） | rc.name LIKE |
| ④ | 凭据生成 INSERT（1557） | 去 name |
| ⑤ | worker 上报兜底 INSERT（runtime.py:2376-2440） | **废弃**（见 Part B：key 机制删除后 cred_id=0 上报路径整体消失） |
| ⑥ | 删除预览（3423） | rc.name |
| ⑨ | 引擎名 provider 段 | rc.name——凭据改名 60s 跟随（rename-cleanup 兜底） |
| ⑩ | 凭据改名 | 无需联动（Provider 无名） |
| ⑪ | 编辑弹窗 | 去名称输入框 |

## 3. Part B：cloud-local.key 机制删除（v3 新增）

### 接触面（grep 实证全清单）

| # | 文件:位置 | 现状 | 改动 |
|---|---|---|---|
| 1 | cloud_worker.py:57 | key_file 配置项（env/默认路径） | 删 |
| 2 | cloud_worker.py:183-194 | `_read_key()`（key 文件读取+权限校验） | 删函数 |
| 3 | cloud_worker.py:1023-1027 | 三级鉴权（token env → key_file → 退出） | 收敛为：**只认 `CLOUD_WORKER_TOKEN` env，无则退出**（报错文案指向页面生成凭据） |
| 4 | isolation/policy.py:491-497 | 沙箱内回调主控 token 兜底读 key 文件 | 删兜底（沙箱 env 注入 RUNTIME_TOKEN，见 #7） |
| 5 | isolation/network.py:1071-1077 | 同上 | 删 |
| 6 | backends/bwrap.py:268-274 | 同上 | 删 |
| 7 | cloud_worker.py 沙箱 env 注入段 | RUNTIME_TOKEN 注入链核对 | 确保 #4-6 删除后沙箱内仍拿得到 token（env 注入为主通道，核对 GRAPE_SERVER_URL 三来源同款修复的注入段 382-384） |
| 8 | scripts/install-cloud-worker.sh | `--token` 必填（已是 env 注入）；key_file 写入段（~96/111-113） | ~~已实施~~ **脚本已退役**（同机多实例 spec addendum §一：并入 install-worker.sh，2026-10-29） |
| 9 | scripts/rotate-cloud-worker-token.sh | 整脚本为 key 轮换服务 | **删除脚本**（轮换=页面吊销+重新生成凭据） |
| 10 | cloud_worker.py:1028+ token 同步映射（升级脚本带 token 重装） | 保留 | 不动 |

### 本机 cloud-worker-local 清理（已执行 2026-10-29，用户裁定：该设备整体废弃）

1. 杀 761978 手动进程 ✓
2. 删 `/var/lib/grape/cloud-worker/cloud-local.key` ✓
3. 吊销 cred4（级联实测：引擎 4、cube Provider、4 条模板全清）✓
4. **不再重装**——本机如需 cloud worker，走页面生成凭据 + install-worker.sh `--token`（install-cloud-worker.sh 已退役并入 install-worker.sh）

### 上报兜底 INSERT 废弃（与 Part A ⑤合流）

v2 里 cred_id=0 上报路径的存在前提就是 key 机制（无凭据 worker 也能上报）——
key 删除后该前提消失，上报端点收敛为：**必须携带有效 rta_ 凭据才接受上报**，
无凭据上报 401。Provider 生命周期 = 凭据生命周期，彻底闭环。

## 4. 实施顺序与验收

| 序 | 改动 | 验收 |
|---|---|---|
| 1 | Part B 代码（key 删除十项） | 无 CLOUD_WORKER_TOKEN 启动 → 清晰报错退出；有 token 正常注册心跳 |
| 2 | 本机收编 | 新凭据 worker 在线；cloud-local.key 文件不存在；cred4 已吊销 |
| 3 | Part A（清 cube → 删列 → 读写改造） | spec v2 §5 1-7 验收全过 |
| 4 | <sandbox-host> 热修（cloud_worker.py 同步） | sandbox-worker-optgrape 正常（env token 不受影响）；无 key 相关日志 |
| 5 | 回归 | 17 套件全量 + 页面验证（引擎名 provider 段=凭据名/改名跟随/编辑弹窗） |

## 5. 改动量

Part B ~50 行删减（cloud_worker 20 + 三个 isolation/backends 文件各 8 + 脚本 2 个）
Part A ~60 行（v2 口径）
本机收编操作项 4 步。
合计预估 1 天内（Part B 半天 + Part A 半天）。
