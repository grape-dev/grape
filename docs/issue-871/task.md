# Tasks: Issue #871

- [x] T1 方案文档（spec/plan）
- [x] T2 engine_wiring：写方向开关 write_mode()（new/legacy，默认 new，文件+env 秒级切回）
- [x] T3 engine_wiring：sync_legacy_installation 直写主 + legacy 反向回写（user/repo 轨、global_im_bot_config 登记幂等）
- [x] T4 engine_wiring：revoke_legacy_installation 直写置 revoked + 反向回写解绑（清 im_*、无引用删登记）
- [x] T5 对账循环 new 模式自动收敛（missing→补 upsert；stale→回写 legacy）；legacy 模式维持仅告警
- [x] T6 对账脚本 scripts/im_reconcile_installations.py（键 diff + secret 比对，仅告警 exit 1）
- [x] T7 调用点注释语义更新（agents.py / chat.py / feishu_onboard.py）
- [x] T8 测试 tests/im：开关/直写回写/解绑回写/换 app/legacy 回滚/对账脚本/自动收敛
- [ ] T9 pytest tests/im 全绿 + 自审查 + 建 PR
