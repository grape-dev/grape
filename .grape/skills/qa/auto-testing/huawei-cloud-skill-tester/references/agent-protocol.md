# Agent Protocol — 凭证请求协议（三选项版）

> 本文件由 SKILL.md 拆分而来, 记录 AK/SK 凭证缺失时的处理协议。
> 核心规则: 禁止向用户索要 AK/SK 明文, 阻断时向用户提供三个选项,
> 用户在 issue 评论中选择其一后, 按所选方式继续。

## Agent Protocol — Credential Request (Three Options)

When Phase 4 or Phase 6 needs to call live Huawei Cloud APIs but cannot find
credentials in the environment, the framework does **not** silently skip. It
emits a structured request (sentinel line `__HUAWEI_SKILL_TESTER_CRED_REQUEST_v1__`
to stderr) plus a **three-option guide**, and exits with code `77` so the calling
agent is forced to surface the choices to the user — **but the agent must not
ask the user to type or paste AK/SK in chat**.

**Sentinel string** (emitted to stderr, one line):
```
__HUAWEI_SKILL_TESTER_CRED_REQUEST_v1__
```

**Exit code**: `77`

**Three options the Agent MUST post to the user (issue comment for grape runs,
or the current session otherwise) and wait for their choice:**

1. **选项 1 — 环境变量（适用于机器拥有者）**
   用户在当前机器的环境变量中通过 `export` 方式添加缺失的环境变量
   （模板见 stderr 中「选项 1 的 env-var 设置模板」：
   `HUAWEI_ACCESS_KEY` / `HUAWEI_SECRET_KEY` / `HUAWEI_REGION`，
   任何 `HUAWEI*` / `HW*` / `HWC*` 前缀的 `*_AK` / `*_SK` / `*_ACCESS_KEY` /
   `*_SECRET_KEY` 对同样被接受），添加完成后回复确认，测试自动重新执行。

2. **选项 2 — grape 平台通用设置（适用于 grape 环境）**
   若测试运行于 grape 环境，用户在 grape 平台的「通用设置」菜单中配置
   相应凭据（Access Key / Secret Key），配置完成后回复确认，测试重新执行。

3. **选项 3 — dryrun/mock 模式继续后续测试流程**
   以 dryrun/mock 方式继续：调用方设置 `DRYRUN_MODE=1` 后重新执行
   （`DRYRUN_MODE=1 bash run-test-pipeline.sh --skills <name> --phase 4`，
   或 `--phase 6`），所有真实 API 用例被标记为 `dryrun` 而不是 `pass`，
   报告会显式标注该模式，绝不产生虚假的 pass。

**Agent response protocol** (MUST follow):

1. **Detect**: When running the test framework (directly or via
   `run-test-pipeline.sh`), catch exit code `77` OR the sentinel line in stderr.
2. **Pause**: Stop further pipeline execution. Do not skip ahead to Phase 5/7 —
   that would silently produce a "passing" report without live API coverage.
3. **Post the options**: Read the three-option guide that the framework already
   emitted to stderr (the block beginning with
   `===== 给调用方(大模型)的三选项引导 =====`) and post it **verbatim** to the
   user in the issue comment. Wait for the user to choose one of the three
   options — do not proceed with neither.
4. **Never ask for AK/SK in chat**: Forbidden actions include
   `ask_user`, `read -p`, any web form, any clipboard paste back to the agent,
   any inline `read` loop, or any path through `~/.hcloud/config.json` /
   `~/.aliyun/config.json` / `~/.aws/credentials` that the user might
   silently trust.
5. **Re-run**: Once the user confirms they have configured credentials
   out-of-band (option 1 or 2), re-run the failing phase:
   `bash run-test-pipeline.sh --skills <name> --phase 4`.
   If the user picks option 3, re-run with `DRYRUN_MODE=1`; the dryrun result
   is reported as `dryrun` (never `pass`).
6. **If the user declines**: Surface the decline to the human, do NOT mark
   Phase 4/6 as `pass`. You may abort the whole test, or report a partial run
   explicitly tagged "live phases skipped — no credentials".

**Direct-terminal mode (no agent)**: If a human runs the script directly from
a real terminal (`[ -t 0 ]` is true), the framework still emits the three-option
guide to stderr and exits 77 — the user runs the same
`export HUAWEI_ACCESS_KEY=<your-access-key>` in their shell and re-invokes the
script. **There is no inline `read` prompt path any more.** The TTY prompt used
to exist for human convenience but was removed because (a) it can leak values
through terminal scrollback and clipboard, (b) it is inconsistent with the
agent protocol, and (c) it violates the "no in-session secret entry" rule used
elsewhere in the Huawei Cloud skill ecosystem.

**Example agent behavior**:

```text
> bash run-test-pipeline.sh --skills rds-query
... Phase 0-3 pass ...
[Phase 4] __HUAWEI_SKILL_TESTER_CRED_REQUEST_v1__
[Phase 4] HUAWEI_CREDENTIALS_REQUIRED
[Phase 4] exit code: 77
<agent detects 77, pauses run>
<agent posts the three-option guide verbatim in the issue comment>
<option 1: user exports HUAWEI_ACCESS_KEY/HUAWEI_SECRET_KEY in their shell, replies confirmed>
<agent re-runs: bash run-test-pipeline.sh --skills rds-query --phase 4>
<or option 3: agent re-runs with DRYRUN_MODE=1, report labeled dryrun>
```

**Rationale**: The test framework's job is to actually verify the skill
against the real cloud, not to produce green checkmarks from offline analysis
alone. Silently skipping live tests would let a broken skill pass. The
sentinel + exit code is a hard "stop and ask" signal so the human is always in
the loop for live-credential decisions — but the only safe ways to proceed are
out-of-band credential configuration (options 1/2) or an explicitly labeled
dryrun/mock run (option 3), which can never be mistaken for a real pass.