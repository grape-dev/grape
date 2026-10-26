# SRE 部署 URL 未自动登记到需求中心"互联网小球"——定位分析与修复方案

- 事件：seq #29573 · Issue #11 · repo `gitcode:songspace/discovery-found`
- 时间：2026-10-26 17:21:48 CST
- 引擎：aishell-worker-e6bbae94f6b74d-51（远程 worker，60.204.249.93）
- 现象：部署成功，新隧道 `https://x7l4ciz4-5100.devbridge-s2.hwtunnel.com/grape-ai-native-practice.html` 回帖正常，但需求中心互联网小球仍显示旧地址 `https://zons3r3x-5100.devbridge-s2.hwtunnel.com/grape-ai-native-practice.html`。

## 一、结论（一句话）

**deploy_url 自动登记逻辑只存在于"本机直连执行"的收尾路径（`_execute_agent_inner`），本次 SRE 由远程 worker 执行，收尾走 `runtime/service.py` 的 `complete_task()`，该路径没有 deploy_url 登记钩子，新地址从未写入 `managed_repos.deploy_url`，前端读到的仍是上一轮的旧值。**

## 二、定位过程（证据链）

### 1. DB 层：新 URL 从未落库

```
managed_repos(repo_key=gitcode:songspace/discovery-found)
deploy_url = https://zons3r3x-5100...   ← 上一轮（Issue #10, seq#29569）的隧道
```

事件/任务状态全部正常：`events.seq=29573 status=completed exit_code=0`；`agent_tasks t_c63157351efc` completed、self_reported=1、stream_ended=1，`result_text` 为结构化 JSON，其中 artifacts[0] = `"deploy_url: https://x7l4ciz4-5100.devbridge-s2.hwtunnel.com/grape-ai-native-practice.html"`——数据源齐全，只是没人消费。

### 2. 日志层：登记代码没有被执行

两台主（174 / 172.16.2.79）的 `devops-bot.log` 在事件时间窗内：

- 均无 `SRE deploy_url auto-registered: ...`（成功路径日志）
- 均无 `SRE deploy_url extraction failed: ...`（失败路径日志）

即登记逻辑整段没有跑——不是正则没匹配到，也不是 UPDATE 失败。

### 3. 代码层：登记钩子只在直连路径上

登记逻辑位于 `src/api/event_pipeline.py` L4085-4099，包在 `_execute_agent_inner()`（L3485 起）末尾：

```python
# SRE 完成后 → 从 result_text 提取部署 URL 并写入 managed_repos.deploy_url
if new_status == "completed" and agent_type == "sre" and result_text:
    _urls = re.findall(r'https?://[\d\w.\-]+(?::\d+)?(?:/)?', result_text)
    if _urls:
        ... UPDATE managed_repos SET deploy_url=? WHERE repo_key=? ...
        log.info(f"SRE deploy_url auto-registered: ...")
```

而本事件的执行模式是**远程 worker**（engine=aishell-worker-...，任务在 worker 侧跑完通过 `grape tasks complete` 自报）。仓库内多处注释已自认这一分叉（`runtime/service.py` L2135）：

> `# remote worker 模式下 _execute_agent 不执行，系统回帖 + 会话同步在此补触发`

`complete_task()`（`src/runtime/service.py` L2020）中针对远程 worker 补触发的钩子共 5 个：

| # | 钩子 | 覆盖情况 |
|---|------|---------|
| 0 | 父 Issue stage=done | ✅ |
| 1 | 系统回帖 Issue | ✅（本次回帖正常的原因） |
| 2 | 子任务 SRE → 登记父 Issue | ✅ |
| 3/4 | 会话/评论同步 | ✅ |
| 5 | VOD 智能推荐重派 | ✅ |
| — | **SRE deploy_url 登记** | ❌ 缺失 |

`grep deploy_url src/runtime/service.py` 零命中，确认缺失。

### 4. 前端链路（排除嫌疑）

互联网小球读 `GET /api/repos/deploy-url/{owner}/{repo}` → `managed_repos.deploy_url`（`repos.py` L1817）。前端无问题，库里是什么就显示什么——库里是旧的，所以显示旧的。

## 三、根因归纳

同一个"SRE 完成"业务语义存在**两条收尾路径**（直连 `_execute_agent_inner` / 远程 `complete_task`），完成后的副作用钩子清单没有同步维护：历史上往直连路径加了 deploy_url 登记（以及 pending_review、子任务登记），远程路径只补了回帖等 5 个，deploy_url 漏掉了。凡 SRE 跑在远程 worker 上，登记就静默丢失（事件全绿，无任何告警）。

## 四、修复技术方案

### 方案：抽取共享钩子，两条路径统一调用（治本）

与 `_handle_vod_recommend_complete` 的既有做法一致——把副作用抽成共享函数，两条路径都调。

**改动 1：`src/api/event_pipeline.py`** —— 把 L4085-4099 的内联逻辑抽成公开函数：

```python
def register_sre_deploy_url(repo_key: str, result_text: str) -> str:
    """SRE 完成后从 result_text 提取部署 URL 写入 managed_repos.deploy_url。

    优先解析结构化 JSON 的 artifacts/issue_summary 中 "deploy_url: <url>"；
    无结构化数据时回落全文正则。返回登记到的 URL（未登记返回 ""）。
    """
    url = ""
    # ① 结构化优先：artifacts 里带 deploy_url: 前缀的条目
    try:
        d = json.loads(result_text)
        if isinstance(d, dict):
            for item in (d.get("artifacts") or []) + [d.get("issue_summary") or ""]:
                m = re.search(r'deploy_url:\s*(https?://\S+?)(?=["\'\s，。]|$)', str(item))
                if m:
                    url = m.group(1)
                    break
    except (json.JSONDecodeError, TypeError):
        pass
    # ② 回落：全文第一个 http(s) URL（保留完整 path，修复旧正则截断路径问题）
    if not url:
        m = re.search(r'https?://[^\s"\'\)\]，。]+', result_text)
        if m:
            url = m.group(0)
    if not url:
        return ""
    # 校验与写入（复用 _validate_deploy_url 语义：http(s) 前缀，长度 ≤512）
    if not url.startswith(("http://", "https://")) or len(url) > 512:
        return ""
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET deploy_url=? WHERE repo_key=?", (url, repo_key))
        if c.rowcount > 0:
            db.commit()
            log.info(f"SRE deploy_url auto-registered: {url} for {repo_key}")
            return url
    return ""
```

原 L4085 处改为调用 `register_sre_deploy_url(repo_key, result_text)`。

**改动 2：`src/runtime/service.py`** —— `complete_task()` 钩子清单末尾（第 5 项 VOD 之后）补第 6 项：

```python
# 6. SRE 完成 → deploy_url 登记（远程 worker 模式下 _execute_agent 不执行，补触发）
if _agent_type == "sre" and _result_text:
    try:
        from src.api.event_pipeline import register_sre_deploy_url
        register_sre_deploy_url(_repo_key, _result_text)
    except Exception as _se:
        log.warning(f"complete_task sre deploy_url register failed: {_se}")
```

注意 `_repo_key = task["repo_key"]` 在该块内已就绪，无需新增取值逻辑。

### 顺带修复的正则缺陷（同一处代码的历史问题）

旧正则 `r'https?://[\d\w.\-]+(?::\d+)?(?:/)?'` 只匹配到 host:port 后**单个斜杠**就停，会把 `https://host:5100/dir/page.html` 截成 `https://host:5100/`，丢掉资源路径。旧库里 `zons3r3x` 那条带完整 path，应是当时人工通过 `PUT /repos/deploy-url` 手动存的，掩盖了该缺陷。新方案的结构化优先 + 完整 URL 正则一并修掉。

### 兼容性与风险评估

- **幂等**：多次登记只是 UPDATE 覆盖，无副作用重放风险；本事件补跑一次即可把 `x7l4ciz4` 写入。
- **不影响的路径**：手动 `PUT /repos/deploy-url` 语义不变；SRE 失败（exit_code!=0）不触发。
- **同类问题排查结论**：`complete_task` 现有钩子与 `_execute_agent_inner` 后置副作用逐项比对，其余（回帖、子任务、stage=pending_review、VOD）均已双路径覆盖或已在远程路径有等价实现，仅 deploy_url 一项缺失。`pending_review`（L4102）建议同批核查远程路径是否也缺（本次事件 stage 已正常流转，未见症状，列为复核项而非缺陷）。
- **双主部署**：改动涉及 `event_pipeline.py` + `service.py`，需 scp 同步至 m2（172.16.2.79）并分别重启 grape-main。

### 数据修复（本次事件）

一条 SQL 即可（用户确认后执行）：

```sql
UPDATE managed_repos SET deploy_url='https://x7l4ciz4-5100.devbridge-s2.hwtunnel.com/grape-ai-native-practice.html'
WHERE repo_key='gitcode:songspace/discovery-found';
```

（隧道有效期 2 小时，若已过期则无修复价值，等下次部署验证新逻辑即可。）
