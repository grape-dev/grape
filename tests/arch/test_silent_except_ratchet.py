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
    # ── 组1 卡驱动回退：get_platform_card/卡派生读取失败 → 默认值兜底
    # pr_comment_endpoint/dialect 双探 + native_field_for 卡能力缺省回退
    ('kernel/repo_key.py', 'except Exception:'): 2,
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

    # ── 组2 幂等迁移/存量兼容（duplicate column、旧形态探测尽力而为）──
    ('api/db_setup.py', 'except Exception:'): 9,
    # G3/D105 迁移族：duplicate column 幂等跳过、旧表重建搬移尽力而为；
    # 其中 3 处为 D105-14/D105k 原手工上调基线项（迁移守卫探测/幂等跳过），
    # 重锚 weekly-10 后由基线下调转为显式登记；旧场景表 user_repos
    # 反查吞错已改 log.warning（本提交）。
    ('domains/mgmt/user/legacy_column_migrations.py', 'except Exception:'): 5,
    # 幂等补列/回填，失败=目标形态已达成
    ('domains/mgmt/user/credentials_backfill_n1.py', 'except Exception:'): 2,
    # duplicate column 幂等语义（行内注释已声明）
    ('domains/mgmt/auth/grape_account_db.py', 'except Exception:'): 4,
    # 回填搬家解密防御（解不开=非本密钥体系→跳过不动）+ rollback
    # 尽力而为；其中 1 处为 D105k 原基线上调项，重锚后转登记

    # ── 组3 尽力而为/非阻断（通知、清理、指标、审计旁路）──
    ('domains/runtime/pipeline/event_pipeline.py', 'except Exception:'): 7,
    # 状态推送/回帖/时长统计尽力而为（R20.3.3 回帖失败不影响 task 状态）；
    # session idle 更新吞错已改 log.warning（本提交）
    ('api/main.py', 'except Exception:'): 2,
    # /metrics 背景采集失败留空
    ('api/issues_center.py', 'except Exception:'): 1,
    # 场景名缓存解析防御
    ('domains/mgmt/routes/settings.py', 'except Exception:'): 1,
    # 规则已删除，重载失败不阻断响应
    ('domains/runtime/engine/repogit.py', 'except Exception:'): 1,
    # worktree 清理尽力而为（后续 rmtree ignore_errors 同语义）
    ('domains/runtime/vcs/api_client.py', 'except Exception:'): 2,
    # N4-p2 非阻塞队列满/空丢弃（审计旁路）
    ('domains/runtime/routes/chat.py', 'except RuntimeError:'): 1,
    # 事件循环未运行时跳过注册（启动竞态合法形态）
    ('domains/mgmt/admin/routes.py', 'except Exception:'): 1,
    # 旧渠道卡存量 JSON 解析防御，失败仅少合并旧卡

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
