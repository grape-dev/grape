# 安全审计方法（Audit 套件执行细则）

对拉取到的**每一条检测规则**（`common_auditor.json` 的 `groups[].items[]`），根据其 `rule`、`detection`、`example` 字段执行审计。

## 审计方法映射

**根据规则 `check`/`detection` 字段内容动态匹配，不硬编码 ID。** 遇到未知规则，根据其 `rule` 和 `detection` 描述自行设计检测方法。

| 检测类型（按 check/detection 关键词匹配） | 执行方式 |
|---|---|
| 外部网络请求 | Grep 搜索代码中的 URL/域名，对比官方域名白名单，标记非官方域名 |
| 访问敏感文件 | Grep 搜索 `~/.ssh/id_rsa`、`.env`、`~/.aws/credentials`、`~/.gnupg` 等敏感路径 |
| 危险文件操作 | Grep 搜索 `rm -rf /`、`chmod 777`、`shutil.rmtree('/')`、`os.remove` 等危险操作 |
| 危险系统命令 | Grep 搜索 `sudo`、`dd if=`、`mkfs`、`shutdown`、`kill -9` 等提权/破坏性命令 |
| 代码注入模式 | Grep 搜索 `getattr(os,'system')`、`__class__.__bases__`、`sys.meta_path.insert` 等注入模式 |
| 后门植入 | Grep 搜索时间条件触发（`if datetime.now().day==`）、隐藏监听端口、C2 回连等 |
| 系统命令调用 | Grep 搜索 `os.system()`、`subprocess.run/call/Popen`、`os.popen()` 等命令执行函数 |
| 动态代码执行 | Grep 搜索 `eval()`、`exec()`、`compile()`、`__import__()` 等动态执行函数 |
| 全局包安装 | Grep 搜索 `npm install -g`、`pip install --user`、`pip install` without venv 等 |
| 强制版本覆盖 | Grep 搜索 `pip install pkg==`、`npm install pkg@<version> --force` 等 |
| 代码混淆模式 | Grep 搜索 `codecs.decode(...,'rot_13')`、`base64.b64decode(eval)`、`\x` 转义拼接等 |
| 隐藏命令 | Grep 搜索 `os.environ['LD_PRELOAD']`、零宽字符、ANSI 转义隐藏等 |
| CWE 漏洞模式 | 根据每个 CWE 模式的 `example` 和 `mapped_rule`，交叉验证检测覆盖面 |

## 判定逻辑

对每条规则，输出以下状态之一：

- **pass** — 未检测到该安全风险
- **fail** — 检测到该安全风险（根据 `severity` 和 `failure_handling` 决定严重性）
- **warn** — 存在潜在风险但无法完全确认（如代码中存在相关函数但用途不明确）
- **skip** — 不适用（注明原因，如 Skill 无可执行代码文件）

## 安全评分

```
安全评分 = 100 - (CRITICAL_fail × 20) - (WARNING_fail × 8) - (INFO_fail × 2) - (warn × 1)
```

星级/等级（与 lint、test 统一口径）：

- 90-100: ⭐⭐⭐⭐⭐ 安全等级：优秀
- 75-89: ⭐⭐⭐⭐ 安全等级：良好
- 60-74: ⭐⭐⭐ 安全等级：合格
- 40-59: ⭐⭐ 安全等级：需改进
- 0-39: ⭐ 安全等级：不合格

```
CWE 覆盖率 = 已检测 CWE 数 / 总 CWE 数 × 100%
```

**一票否决：** CRITICAL fail（严重发现）一票否决，安全评分直接降为不合格。

## 证据要求

每条审计结果必须附带**客观证据**：

- pass：说明扫描了哪些文件、搜索了哪些模式、未发现匹配
- fail：引用具体文件名、行号、匹配的代码片段、对应的 CWE 编号
- warn：说明潜在风险点和需要人工确认的原因
- skip：说明不适用的原因

**CWE 映射：** 每条 fail 必须关联对应的 CWE 编号（从拉取规则的 `cwe` 字段或 CWE 模式组的 `mapped_rule` 反查），便于安全团队追溯。