# Spec: Issue #3892 — install-worker 升级注册等待窗口误杀慢启动新 agent

> Spec 主体已由上游 Issue Agent 发布（见 Issue #3892 Spec 评论 #202998320，评级 ready）。本文为落地侧引用摘要，不重做分析。

## Objective

修复 `scripts/install-worker.ps1` 升级后的注册等待逻辑：注册成功但启动慢（35-60s）的新 agent 不再被误判回滚；回滚路径结尾提示如实反映「已回滚」。

## 现状与根因

- 注册等待是固定窗口：`for ($i = 1; $i -le 10; $i++) { tail 日志; Start-Sleep 3 }` = 30s 封顶。
- Windows 冷盘 + Defender 实时扫描 74MB 新文件 + anaconda 冷启动下，新 agent 实测 07:03:25.452 才写出
  `RuntimeAgent registered`；安装器约 07:03:15 已到期 → `restore_upgrade_backup` 把健康的新版本杀回旧代码，
  机器此后长期停留在旧版本。
- 回滚分支之后仍统一打印 `Install complete!`（旧第 807 行 `rollbackOk` 分支之后），回滚事实被掩盖。

## In Scope

- 注册等待窗口 30s → 60s（20 × 3s 封顶）；首轮 30s 到期未匹配时先读 `devops-bot.log` 尾部判定注册是否在途，
  有迹象续等至 60s，无迹象进入回滚判定。
- 回滚前交叉验证（可选加固）：以安装器已持有的 `rta_` 凭据查询主控该 runtime 最近 `last_seen_at`，
  窗口内刚更新则不回滚；端点不存在/403/超时/无该字段一律静默降级为纯日志判定。
- 结尾提示分支化：成功路径保留 `Install complete!`；回滚成功路径给出「旧版本标识 + 备份位置 + 重试建议」；
  回滚失败路径维持异常提示。

## Out of Scope

- `install-worker.sh`（Linux）行为调整；主控侧升级管控（#3647）；agent 启动序列慢的根因优化。

## 必须保持

快速失败路径（进程/daemon 早退）仍在原时限内回滚，窗口放宽不得变成无限等待。

## 验收标准

- [ ] Windows 冷盘/杀软扫描场景，新 agent 注册耗时 35-60s 时不被回滚，升级成功且设备保持在线。
- [ ] 窗口内日志出现 `RuntimeAgent registered` 或注册在途迹象时继续等待，已注册 runtime 不被回滚。
- [ ] 回滚路径结尾无 `Install complete!`，输出含回滚到的旧版本标识与重试建议。
- [ ] 快速失败路径行为不变；现有测试通过，新增覆盖等待窗口与回滚提示分支的测试。