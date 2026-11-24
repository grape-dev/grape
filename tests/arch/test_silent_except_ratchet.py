"""反回潮 ratchet：静默 except（`except ...: pass/continue`）按文件基线计数。

背景：基线锚点 = weekly-10 tip 9c4c770b8（2026-11-12 拍板重锚）。原基线冻结自
扫雷⑩快照（854 点/120 文件），与分支基线树实数（989 点/151 文件）口径漂移——
锚错了树、守卫生而红。重锚后基线语义不变 = 只拦新增：

- 某文件当前计数 > 基线计数 → RED（新增静默 except，须显式处理/登记豁免）；
- 计数 ≤ 基线 → GREEN（修掉既有吞噬点允许且鼓励，基线随后同步下调）。

feat/weekly-10-yaw-dev 自身引入的 59 处已逐点审阅（守卫精确口径 multiset diff
对 weekly-10）：2 处吞 DB 错改 log.warning（db_setup 旧场景表 user_repos 反查、
event_pipeline session idle 更新），其余 57 处按族登记 EXEMPT_FILES 并写明理由。

扫描口径：src/**/*.py 非注释行，`except ...:` 的吞异常体为 `pass` 或
`continue`——同物理行冒号后（剥离行尾 `# ...` 注释）或紧随的下一非空
非纯注释行（与扫雷⑩ except_hits.json 扫描器同口径）。剥离行尾注释后
匹配，纯注释行（strip 后以 # 开头）不计。

豁免机制（参照 test_platform_whitelist_ratchet 手法）：EXEMPT_FILES 按
(相对 src/ 的路径, 行内容) 登记——每处一个豁免名额，豁免行数从该文件
计数中扣除后再与基线比较。每条豁免必须附理由注释，逐点审阅后写死。
基线文件 silent_except_baseline.json 不得随手上调：上调即豁免，必须走
EXEMPT_FILES 并写明理由；只有"修复删除了吞噬点"才允许下调基线。
> 2026-11-11 重锚：基线树由 weekly-10 tip 迁至 arch-weekly-11 合并基线（文件搬移导致计数口径漂移，
> 只拦新增语义不变；本次重锚前已逐条比对 arch 树，merge 比 arch 多的 61 处均落在我方最佳努力吞异常
> 文件（service.py/event_pipeline/agent.py/git_repo_api/token_resolver/repo_key/prompt_builder 等），
> 属合并前已审阅登记的族，随架构搬移重锚。
"""
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src"
BASELINE_PATH = Path(__file__).resolve().parents[1] / "silent_except_baseline.json"

# 匹配 `except ...: pass/continue`（行尾注释已剥离，同物理行形态）
_SILENT_EXCEPT = re.compile(r"^\s*except\b.*:\s*(pass|continue)\s*$")
# 匹配 `except ...:`（悬空形态，吞异常体在下一行）
_EXCEPT_HEADER = re.compile(r"^\s*except\b.*:\s*$")


def _strip_inline_comment(line: str) -> str:
    """剥离行尾 `# ...` 注释（忽略引号内的 #，覆盖本仓 f-string/字典值场景）。"""
    in_s = in_d = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            return line[:i]
    return line


def count_silent_excepts(path: Path):
    """返回 (计数, [(行号, 内容)])：except...pass/continue（非注释行）。

    两种形态：冒号后同行 pass/continue；或冒号悬空、紧随的下一非空
    非纯注释行为 pass/continue（与扫雷⑩基线扫描器同口径）。
    """
    hits = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for lineno, raw in enumerate(lines, 1):
        code = _strip_inline_comment(raw).rstrip()
        if not code or code.lstrip().startswith("#"):
            continue
        if _SILENT_EXCEPT.match(code):
            hits.append((lineno, code.strip()))
        elif _EXCEPT_HEADER.match(code):
            # 找下一非空非纯注释行
            for nxt in lines[lineno:]:
                nxt_code = _strip_inline_comment(nxt).rstrip()
                if not nxt_code or nxt_code.lstrip().startswith("#"):
                    continue
                if re.match(r"^\s*(pass|continue)\s*(#.*)?$", nxt_code):
                    hits.append((lineno, code.strip()))
                break
    return len(hits), hits


# ── 豁免登记：(相对 src/ 的路径, 行内容) → 名额。必须附理由。──
# feat/weekly-10-yaw-dev 真新增 57 处（对 weekly-10 multiset diff，另 2 处已改 log），
# 按族分类登记；豁免行数从该文件计数中扣除后再与基线比较。
EXEMPT_FILES: dict = {
    # outbox.stop()：await 一个已被 cancel 的 task，CancelledError 就是「取消成功」这个
    # 正常结果；不吞会一路冒到 lifespan，触发 "Application shutdown failed" + uvicorn
    # 退出码 3 + systemd 反复重启抢端口（d594cba3a 修的就是这个 crash loop）。
    # scanner 只认「except ...: pass/continue」的形态，无法区分这种 await-cancelled 场景。
    ('domains/runtime/im/outbox.py', 'except asyncio.CancelledError:'): 1,
    # ── 组Y 上游 aishell-dsh phase-2（#2045）新增：均为协议/计量链路的 fail-open ──
    # ACP JSON-RPC：会话收尾/管道写失败吞掉，避免污染上层任务状态
    ('domains/runtime/agents/aishell_acp.py', 'except Exception:'): 1,
    # 用量采集：解析/换算失败按"无用量"处理，绝不影响任务本身
    ('domains/runtime/engine/usage_collect.py', 'except (TypeError, ValueError):'): 2,
    ('domains/runtime/engine/usage_collect.py', 'except ValueError:'): 1,
    # ── 组X 合并后新入库文件（2026-11-12）：全部为 fail-open 语义 ──
    # 遥测上报：OTel SDK 任一失败（无 collector / 网络抖动 / 属性缺失）不得影响业务主流程，
    #   上报路径必须彻底 fail-open（本仓可观测性铁律）
    ('kernel/otel.py', 'except Exception:'): 8,
    # 凭据用量队列：空队列取数与满队列入队都是流控常态，非异常
    ('kernel/persistence/credential_usage_log.py', 'except (queue.Empty, queue.Full):'): 1,
    # 媒体清理任务：取消/删除临时文件失败不影响消息投递
    ('domains/runtime/im/engine/media.py', 'except (asyncio.CancelledError, Exception):'): 1,
    # IM 安装项批量回填：逐条容错（单条脏数据 continue，整体继续）
    ('domains/mgmt/routes/im.py', 'except Exception:'): 2,
    # ── 组1 卡驱动回退：get_platform_card/卡派生读取失败 → 默认值兜底 ──
    # pr_comment_endpoint/dialect 双探 + native_field_for 卡能力缺省回退
    ('kernel/repo_key.py', 'except Exception:'): 2,
    # _pr_cap（S88/D64 卡能力位读取，2026-11-13 自 b439764fd 恢复）+
    # _pflag 卡优先解析（S40-S42，2026-11-13 代理能力位上翻）：两处均为
    # 卡读取异常回退 default/模块开关=存量语义（组1 同族 fail-open）。
    # 注：同键两行会被 dict 字面量后定义覆盖，计数合并为 2。
    ('api/git_repo_api.py', 'except Exception:'): 2,
    # 方言/host 卡查询回退（映射表为数据非分支）
    ('domains/mgmt/user/repo_manager.py', 'except Exception:'): 3,
    # webhook_path/平台名单/bot_username 卡回退
    ('domains/mgmt/auth/providers.py', 'except Exception:'): 1,
    # 邮箱端点卡回退
    ('domains/runtime/vcs/platform_card.py', 'except Exception:'): 2,
    # 展示名/host 卡回退
    ('domains/runtime/vcs/adapter.py', 'except Exception:'): 1,
    # 通用后端 webhook 枚举回退
    ('domains/runtime/storage/scene_sync.py', 'except Exception:'): 1,
    # 仓库 token 卡回退
    ('domains/runtime/engine/bot_token_health.py', 'except Exception:'): 1,
    # 健康 JSON 解析回退 None
    ('kernel/token_resolver.py', 'except Exception:'): 2,
    # collaborator 权限字段类型解析防御，非 admin 判定按回退处理
    ('domains/mgmt/routes/repos.py', 'except Exception:'): 2,
    # provider/卡展示名回退

    # ── 组3 PR#1318 验收/平台化新增（2026-11-12 QA 第3轮登记）：均为刻意 fail-open ──
    # 上游 issue number 解析（gitee IK* 字符串/None 混型），坏条目跳过不炸列表
    ('api/issue_vcs_index.py', 'except (TypeError, ValueError):'): 1,
    # bot token 三段读序（卡列→rc 单表→存量列回落）逐步探测：表/列不存在=走下一段
    # （P9：老库无 rc 表、卡列平台无物理列均为合法形态，靠异常探测非错误）；
    # grape_account_db 白名单改名联动的豁免已并入下方组2同键条目（4→5）
    # PR#1507 增量（+2→共 7）：server_config/meta URL 回退探测族（两段探测
    # 第二段，异常即回退默认值，探测语义本体）
    ('domains/mgmt/user/db.py', 'except Exception:'): 7,
    # ── 组2 幂等迁移/存量兼容（duplicate column、旧形态探测尽力而为）──
    ('api/db_setup.py', 'except Exception:'): 9,
    # G3/D105 迁移族：duplicate column 幂等跳过、旧表重建搬移尽力而为；
    # 其中 3 处为 D105-14/D105k 原手工上调基线项（迁移守卫探测/幂等跳过），
    # 重锚 weekly-10 后由基线下调转为显式登记；旧场景表 user_repos
    # 反查吞错已改 log.warning（本提交）。
    ('domains/mgmt/user/legacy_column_migrations.py', 'except Exception:'): 2,
    # 幂等补列/回填，失败=目标形态已达成（两条 ALTER 合并进一个循环后只剩 1 处）
    ('domains/mgmt/user/credentials_backfill_n1.py', 'except Exception:'): 1,
    # duplicate column 幂等语义（行内注释已声明）；+1 = PR#1318 白名单改名联动
    # （渠道无老列的新装/未声明列存量跳过该渠道 UPDATE，fail-open 刻意）
    ('domains/mgmt/auth/grape_account_db.py', 'except Exception:'): 5,
    # 回填搬家解密防御（解不开=非本密钥体系→跳过不动）+ rollback
    # 尽力而为；其中 1 处为 D105k 原基线上调项，重锚后转登记

    # ── 组3 尽力而为/非阻断（通知、清理、指标、审计旁路）──
    ('domains/runtime/pipeline/event_pipeline.py', 'except Exception:'): 7,
    # 状态推送/回帖/时长统计尽力而为（R20.3.3 回帖失败不影响 task 状态）；
    # session idle 更新吞错已改 log.warning（本提交）
    ('api/main.py', 'except Exception:'): 2,
    # /metrics 背景采集失败留空
    ('domains/runtime/issues_center.py', 'except Exception:'): 1,
    # 场景名缓存解析防御；PR#1507 合入增量（+5→共 6）：模板异步校验三处
    # （校验失败不阻塞写路径）+ LLM 凭据权限预检（预检失败落显式 403 fail-closed）
    ('domains/mgmt/routes/settings.py', 'except Exception:'): 6,
    # 规则已删除，重载失败不阻断响应
    ('domains/runtime/engine/repogit.py', 'except Exception:'): 1,
    # worktree 清理尽力而为（后续 rmtree ignore_errors 同语义）
    ('domains/runtime/vcs/api_client.py', 'except Exception:'): 2,
    # N4-p2 非阻塞队列满/空丢弃（审计旁路）；事件循环未运行时跳过注册（启动竞态合法形态）
    ('domains/runtime/routes/chat.py', 'except RuntimeError:'): 1,

    # ── 原有登记 ──
    ('domains/runtime/vcs/providers/__init__.py', 'except Exception:'): 1,
    # 理由：D105-14 模块回退审计埋点——审计失败不阻断主流程（同 token_resolver
    # _audit_source 存量形态）；仅每 provider 进程内一次，非热路径数据面。

    # ── arch-weekly-11 合并基线口径补记（2026-11-11 合规整改轮）──
    ('domains/runtime/engine/agent.py', 'except Exception:'): 2,
    # 理由：任务执行链 fail-open 族——cwd makedirs 兜底 / env_extra 解析回退 {} /
    # get_task_env 拉新失败回落快照 / 进度快照 _ckpt.feed 尽力而为 / 失败路径
    # 用量补采尽力而为；均为"吞错=回退语义本体"，主错误另有 log.warning。
    ('domains/runtime/routes/webhook.py', 'except Exception:'): 1,
    # 理由：事件去重扫描里 ctx JSON 解析失败跳过该条（continue）——解析失败
    # 的条目本就无 dedup 语义可判，跳过即正确行为。
    ('domains/runtime/engine/atomcode.py', 'except FileNotFoundError:'): 1,
    # 理由：临时文件 unlink 的 FileNotFoundError: pass（清理幂等惯用法）；
    # atomcode.py 为 arch-weekly-11 基线自带文件，重锚时基线漏记该条目，
    # 本条为补登记（非 PR 新增吞噬点）。

    # ── arch-weekly-11-zj 线合入登记（2026-11-12 rebase onto arch-weekly-11）──
    # 均为 zj 线 #1982/#2133/#2134/#2041 已审阅的 fail-open/幂等收尾语义，
    # docstring/注释内声明吞错意图，主错误路径另有 log.warning。
    # 理由：#1982 fd 泄漏修复族——_close_proc_pipes 幂等关管道 / 收尾兜底
    # terminate+关管道 / reader 线程 join 超时后放行（作用域收尾绝不反向阻断）。
    # ── PR#1507 增量合并（+4→共 8）：云沙箱收尾族——iso_home rmtree /
    # devbox close_stdin 500 契约容错 / handle.kill 尽力而为×2（kill 失败
    # 后主路径已另行 yield done(exit_code=-1)，非静默降级）。
    ('domains/runtime/agents/executor.py', 'except Exception:'): 8,
    ('domains/runtime/engine/acp_bootstrap.py', 'except Exception:'): 7,
    # 理由：#2134/#1982 bootstrap 改造——子进程终止链（kill/socket close/wait/
    # stdin flush）收尾尽力而为 + 引擎 bin 路径探测候选循环逐个试错（探不到
    # 换下一个候选，探测语义本体）。
    ('domains/runtime/engine/branch_guard.py', 'except Exception:'): 2,
    # 理由：#2028/#2041 fail-open 族——守卫自检异常一律 fail-open（stderr 告警
    # + exit 0，真实违规由 hook_main 显式阻断）/ hook 幂等检查与租约释放尽力而为。
    ('domains/runtime/engine/recovery.py', 'except Exception:'): 1,
    # 理由：#2133 引擎自动恢复链——恢复步骢单项失败不阻断整体恢复循环
    # （循环内 continue，失败已 log.warning 留痕）。
    ('domains/runtime/engine/worktree_guard.py', 'except Exception:'): 2,
    # 理由：#2041 归档守卫族——bundle/补丁归档的幂等收尾（归档失败走
    # 「拒绝删除、宁可残留」主路径，吞噬点均为快照收尾尽力而为）。

    # ── PR#1507（feat/sandbox-uuid-onto-arch-weekly-11）QA 原子回归第 4 轮登记（2026-11-17）──
    # 本轮为 sandbox 子系统合入增量（head vs base dd992f15a multiset diff 实测 146 处
    # 新增静默 except）。全部逐点过目后按族登记，族语义如下：
    # ① 沙箱生命周期收尾族：kill/close/rmtree/unlink/rename/close_stdin/shutdown 等
    #   幂等收尾——清理失败绝不反向阻断任务/沙箱主路径（同 #1982 fd 收口族）；
    # ② 探测语义本体族：optional import / CDLL / 路径候选 / IP 归一化 / JSON 坏行
    #   / EAGAIN 非阻塞读 / 旧库缺列探测——异常即「走下一段/默认值」的探测本体；
    # ③ 尽力而为旁路族：report_progress/report_messages/progress_cb/遥测/Prom 计数/
    #   审计旁路——上报失败不影响业务主流程（可观测性 fail-open 铁律）；
    # ④ fail-closed 兜底族：sandbox_admin 管理员预检与 utils/crypto 读失败吞掉后
    #   落显式 403 / RuntimeError——吞错后走拒绝/必炸路径，非静默降级。
    # 基线文件未上调（守卫门规则）；settings.py 增量已并入上方既有键（1→6）。
    # ── api/session_store.py（场景 mapping 卡回退（读不到配置按无 mapping 处理，存量子项路径））──
    ('api/session_store.py', 'except Exception:'): 1,

    # ── domains/insight/routes/dashboard.py（advanced_options JSON 解析防御（坏串按未配置处理））──
    ('domains/insight/routes/dashboard.py', 'except Exception:'): 1,

    # ── domains/mgmt/routes/autopilot.py（repo token 尽力而为获取（取不到按空 token 下行，主流程不阻断））──
    ('domains/mgmt/routes/autopilot.py', 'except Exception:'): 1,

    # ── domains/mgmt/routes/sandbox_admin.py（管理员预检 SQL 探测异常吞掉后落显式 403（fail-closed：探测失败=拒绝））──
    ('domains/mgmt/routes/sandbox_admin.py', 'except Exception:'): 1,

    # ── domains/mgmt/routes/scenes.py（engine_uid 解析坏条目跳过（fail-open 列表面）+ linked_repos 列不存在旧库幂等兼容）──
    ('domains/mgmt/routes/scenes.py', 'except (TypeError, ValueError):'): 1,
    ('domains/mgmt/routes/scenes.py', 'except Exception:'): 1,

    # ── domains/runtime/agents/executor.py（policy 模块 ImportError 探测（模块不可用=Null 模式跳过，探测语义本体））──
    ('domains/runtime/agents/executor.py', 'except ImportError:'): 1,

    # ── domains/runtime/agents/hermes_acp.py（per-task HERMES_HOME overlay 写入/清理收尾尽力而为 + 管道 close 幂等收尾（关闭失败无害，作用域收尾不反向阻断任务））──
    ('domains/runtime/agents/hermes_acp.py', 'except Exception:'): 7,

    # ── domains/runtime/agents/materializer/knowledge.py（降级提示回帖尽力而为 + progress 回调旁路（上报失败不影响物化主流程））──
    ('domains/runtime/agents/materializer/knowledge.py', 'except Exception:'): 2,

    # ── domains/runtime/agents/materializer/skill.py（progress 回调旁路（上报失败不影响物化主流程））──
    ('domains/runtime/agents/materializer/skill.py', 'except Exception:'): 1,

    # ── domains/runtime/agents/sandbox/alert.py（端口串/退出码解析防御（坏值 continue/pass，探测语义本体））──
    ('domains/runtime/agents/sandbox/alert.py', 'except (ValueError, TypeError):'): 1,
    ('domains/runtime/agents/sandbox/alert.py', 'except ValueError:'): 1,

    # ── domains/runtime/agents/sandbox/audit.py（审计日志 rotate/末行解析尽力而为（坏行 continue、rename 幂等、read 失败回空——审计旁路不阻断沙箱主流程））──
    ('domains/runtime/agents/sandbox/audit.py', 'except Exception:'): 3,
    ('domains/runtime/agents/sandbox/audit.py', 'except json.JSONDecodeError:'): 1,

    # ── domains/runtime/agents/sandbox/backends/bwrap.py（bwrap 路径展开/audit flush/cleanup rmtree/域名白名单回退——沙箱收尾与探测族尽力而为（清理失败不反向阻断 sandbox 生命周期））──
    ('domains/runtime/agents/sandbox/backends/bwrap.py', 'except Exception:'): 5,

    # ── domains/runtime/agents/sandbox/backends/cloud_base.py（_init_sandbox 失败后 _do_kill 收尾兜底（kill 失败无害，主错误已 log.error 上抛））──
    ('domains/runtime/agents/sandbox/backends/cloud_base.py', 'except Exception:'): 1,

    # ── domains/runtime/agents/sandbox/backends/cubesandbox.py（cubesandbox SDK 收尾族：rm -f/sandbox.close/pty kill/workdir 清理幂等尽力而为 + 引擎批量探测单条失败不拖垮整批（探测语义本体）+ FileNotFoundError unlink 幂等惯用法）──
    ('domains/runtime/agents/sandbox/backends/cubesandbox.py', 'except Exception:'): 9,
    ('domains/runtime/agents/sandbox/backends/cubesandbox.py', 'except FileNotFoundError:'): 1,
    ('domains/runtime/agents/sandbox/backends/cubesandbox.py', 'except ValueError:'): 1,

    # ── domains/runtime/agents/sandbox/backends/devbox.py（devbox 句柄 close_stdin/kill/workdir 清理幂等收尾（CloseStdin 500=契约容错，主错误另有上抛路径））──
    ('domains/runtime/agents/sandbox/backends/devbox.py', 'except Exception:'): 5,

    # ── domains/runtime/agents/sandbox/bootstrap.py（审计路径候选逐个试错（不可写换下一个候选，探测语义本体））──
    ('domains/runtime/agents/sandbox/bootstrap.py', 'except (PermissionError, OSError):'): 1,

    # ── domains/runtime/agents/sandbox/detect.py（cgroup 检测解析失败按未知降级（探测语义本体））──
    ('domains/runtime/agents/sandbox/detect.py', 'except Exception:'): 1,

    # ── domains/runtime/agents/sandbox/isolation/network.py（IP 归一化 ValueError 坏值跳过（探测语义本体）+ TCP 中继 shutdown/close 幂等收尾 + 遥测旁路——转发泵收尾不阻断数据面（fail-closed 由主循环 break 保证））──
    ('domains/runtime/agents/sandbox/isolation/network.py', 'except Exception:'): 7,
    ('domains/runtime/agents/sandbox/isolation/network.py', 'except ValueError:'): 5,

    # ── domains/runtime/agents/sandbox/isolation/policy.py（egress 规则 IP 字面量坏值过滤（坏规则剔除不放行）+ skill 路径解析回退链逐段探测（server_url/官方包/DB 三段，探测语义本体））──
    ('domains/runtime/agents/sandbox/isolation/policy.py', 'except Exception:'): 3,
    ('domains/runtime/agents/sandbox/isolation/policy.py', 'except ValueError:'): 1,

    # ── domains/runtime/agents/sandbox/isolation/resource.py（cgroup 检测/memory.high 写入/挂载点 rmdir——资源限制尽力而为收尾（失败不阻断沙箱启动，主错误另有 log））──
    ('domains/runtime/agents/sandbox/isolation/resource.py', 'except Exception:'): 4,

    # ── domains/runtime/agents/sandbox/isolation/seccomp.py（libseccomp CDLL OSError 探测（无库=禁用 seccomp，探测语义本体）+ filter fd close 幂等收尾）──
    ('domains/runtime/agents/sandbox/isolation/seccomp.py', 'except Exception:'): 2,
    ('domains/runtime/agents/sandbox/isolation/seccomp.py', 'except OSError:'): 1,

    # ── domains/runtime/agents/sandbox/manager.py（沙箱模板查询尽力而为（查不到按无模板走默认路径））──
    ('domains/runtime/agents/sandbox/manager.py', 'except Exception:'): 1,

    # ── domains/runtime/agents/sandbox/overlay.py（overlay 建 symlink/env 文件写入/隔离目录清理收尾——HERMES_HOME 铺overlay族尽力而为（铺设失败不阻断任务下发））──
    ('domains/runtime/agents/sandbox/overlay.py', 'except Exception:'): 5,

    # ── domains/runtime/agents/sandbox/repository.py（模板查询兜底回 ("", "")（查不到走默认，探测语义本体））──
    ('domains/runtime/agents/sandbox/repository.py', 'except Exception:'): 1,


    # ── domains/runtime/agents/sandbox/stage2.py（stage2 UDS 代理进程（沙箱内常驻子进程）：信号注册/ready 文件/socket shutdown/close 幂等收尾——代理断连 fail-closed（主循环退出）且收尾绝不阻断 accept 泵）──
    ('domains/runtime/agents/sandbox/stage2.py', 'except (OSError, ValueError):'): 1,
    ('domains/runtime/agents/sandbox/stage2.py', 'except OSError:'): 2,
    ('domains/runtime/agents/sandbox/stage2.py', 'except OSError: pass'): 7,

    # ── domains/runtime/agents/sandbox/wrappers/_ndjson_common.py（NDJSON 行坏行跳过（JSONDecodeError continue，解析语义本体）+ proc kill/stderr 读收尾尽力而为）──
    ('domains/runtime/agents/sandbox/wrappers/_ndjson_common.py', 'except Exception:'): 2,
    ('domains/runtime/agents/sandbox/wrappers/_ndjson_common.py', 'except json.JSONDecodeError:'): 1,

    # ── domains/runtime/agents/sandbox/wrappers/acp_wrapper.py（非阻塞读 BlockingIOError continue（EAGAIN 语义本体）+ stderr 转发/stdin close/kill 收尾尽力而为）──
    ('domains/runtime/agents/sandbox/wrappers/acp_wrapper.py', 'except (BlockingIOError, OSError):'): 1,
    ('domains/runtime/agents/sandbox/wrappers/acp_wrapper.py', 'except Exception:'): 3,

    # ── domains/runtime/agents/sandbox/wrappers/bridge.py（PTY 非阻塞读 (OSError, BlockingIOError) continue（EAGAIN 语义本体）+ RX 日志写入旁路）──
    ('domains/runtime/agents/sandbox/wrappers/bridge.py', 'except (OSError, BlockingIOError):'): 1,
    ('domains/runtime/agents/sandbox/wrappers/bridge.py', 'except:'): 1,

    # ── domains/runtime/engine/agent_tags.py（标签快照预检失败走常规写路径（缺列/缺表降级，行为与未优化一致——行内注释声明））──
    ('domains/runtime/engine/agent_tags.py', 'except Exception:'): 1,

    # ── domains/runtime/engine/client.py（401 二次确认后的非 CredentialRevokedError 网络错误按可继续处理（server 重启场景，行内注释声明不 abort））──
    ('domains/runtime/engine/client.py', 'except Exception:'): 1,

    # ── domains/runtime/engine/cloud_worker.py（云 worker 心跳/消息上报尽力而为（report_* 失败不中断引擎执行流，上报旁路铁律）+ sandbox kill/revoke_proxy_token/审计旁路收尾 + 心跳等待循环 TimeoutError continue（轮询语义本体）+ refresh task cancel 收尾）──
    ('domains/runtime/engine/cloud_worker.py', 'except (Exception, _aio.CancelledError):'): 2,
    ('domains/runtime/engine/cloud_worker.py', 'except (Exception, _aio.CancelledError, _aio.TimeoutError):'): 1,
    ('domains/runtime/engine/cloud_worker.py', 'except (Exception, asyncio.CancelledError):'): 2,
    # QA G-6（PR #1847）：泄漏看护 add / 批末 kill / 看护超时三处已改为 log.debug 留痕，
    # 该形态登记数随之 18 → 16（不得整体上调基线抵消，只按下调收口）
    ('domains/runtime/engine/cloud_worker.py', 'except Exception:'): 16,
    ('domains/runtime/engine/cloud_worker.py', 'except asyncio.CancelledError:'): 1,
    ('domains/runtime/engine/cloud_worker.py', 'except asyncio.TimeoutError:'): 1,

    # ── domains/runtime/engine/service.py（task:failed 事件旁路（发布失败不影响任务状态）+ 锁连接 close/rollback 幂等收尾 + selector JSON 坏行 continue + mysql_config 探测回退——均为尽力而为旁路，主错误另有 log.warning）──
    ('domains/runtime/engine/service.py', 'except Exception:'): 7,

    # ── domains/runtime/routes/runtime.py（可选列/可选数据探测回退族（pool_fill/content_hash/admin_policy/skill 详情缺列回空）+ Prom 计数器旁路（无 collector 时 inc 失败无害）+ 标签 INSERT 幂等——探测与计量旁路）──
    ('domains/runtime/routes/runtime.py', 'except Exception:'): 9,

    # ── kernel/bulk_read.py（G-01 免锁读通道）：reader() 出块关游标/短连接是
    # best-effort 清理——调用方业务异常永远优先上抛，清理失败只降 debug 日志，
    # 不允许让一次资源回收失败把正常读路径变成 500（与 crypto.py fail-closed
    # 理由对称：这里是 fail-open 的资源清理面，非异常吞噬面）。
    # RuntimeError: 单库拓扑下 db_registry 无该域 DSN 是**预期形态**（注释载明
    # 回落语义），pass 即降级本身。──
    ('kernel/bulk_read.py', 'except Exception:'): 3,
    ('kernel/bulk_read.py', 'except RuntimeError:'): 1,
}


def _current_counts():
    counts, details = {}, {}
    # 配额池每次调用独立拷贝：主用例与 self_check 各自完整应用豁免，
    # 不原地耗尽全局 EXEMPT_FILES（否则测试顺序敏感——先跑的用例把
    # 配额扣完后，后跑的 self_check 计数回涨、误报口径漂移）。
    exempt_pool = dict(EXEMPT_FILES)
    for p in sorted(SRC_ROOT.rglob("*.py")):
        rel = str(p.relative_to(SRC_ROOT))
        n, hits = count_silent_excepts(p)
        # 应用豁免：同文件同内容行的名额逐个抵扣
        exempted = 0
        for lineno, content in hits:
            quota = exempt_pool.get((rel, content), 0)
            if quota > 0:
                exempt_pool[(rel, content)] = quota - 1
                exempted += 1
        counts[rel] = n - exempted
        details[rel] = hits
    return counts, details, exempt_pool


def test_silent_except_ratchet():
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    counts, details, exempt_pool = _current_counts()

    regressions = []
    for rel_with_src, base_n in sorted(baseline.items()):
        rel = rel_with_src[len("src/"):] if rel_with_src.startswith("src/") else rel_with_src
        cur = counts.get(rel, 0)
        if cur > base_n:
            known = {c for _, c in details.get(rel, [])}
            regressions.append(f"{rel}: baseline={base_n} current={cur}")
    for rel in sorted(set(counts) - {k[len("src/"):] for k in baseline}):
        if counts[rel] > 0:
            regressions.append(f"{rel}: NEW file with {counts[rel]} silent excepts")

    # 豁免名额未用完 = 豁免登记失效（内容漂移），提示修正
    stale = [k for k, v in exempt_pool.items() if v > 0]

    assert not regressions, (
        "静默 except ratchet 回潮（新增 `except...: pass/continue`）：\n"
        + "\n".join(regressions)
        + "\n修复：显式处理异常/记日志，或确实豁免则在 "
          "EXEMPT_FILES 登记 (路径, 行内容) 并写明理由（基线文件不许随手上调）"
    )
    assert not stale, f"EXEMPT_FILES 存在未命中行（内容漂移，请修正豁免登记）: {stale}"


def test_ratchet_baseline_self_check():
    """基线自检：基线文件覆盖 src/ 全部当前命中（口径一致性），且非空。"""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    counts, _, _ = _current_counts()
    assert baseline, "基线文件为空——扫描器口径坏了"
    # 基线只许包含真实存在且计数≥当前的文件（防手滑上调基线虚报）
    for rel_with_src, n in baseline.items():
        rel = rel_with_src[len("src/"):] if rel_with_src.startswith("src/") else rel_with_src
        assert (SRC_ROOT / rel).exists(), f"基线指向不存在的文件: {rel_with_src}"
        assert counts.get(rel, 0) <= n, f"基线 {rel_with_src}={n} 小于当前 {counts.get(rel)}（口径漂移）"
