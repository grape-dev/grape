"""Issue #2545：worker 安装脚本模型配置门（install-worker.sh / install-worker.ps1，Seam C）。

内容契约测试（沿用 #1934 CLI 版本门测试惯例）：安装/启动 hermes 前，安装脚本必须
调用统一校验入口 `python -m src.domains.runtime.engine.engine_model --check`（与探针/文档同一实现，
Linux/Windows 行为一致），模型名不在网关 /v1/models 可用列表 → 安装失败；
无 config.yaml → 跳过；网关不可达由模块自身降级（exit 0），脚本不得因此失败。

硬性时序约束：校验必须在「杀旧 worker / 停旧 daemon」之前执行——
校验失败时不能把正在运行的旧 worker 杀掉（保留可用现场，修复配置后再重装）。
"""
import pathlib

REPO = pathlib.Path(__file__).parent.parent
SH = REPO / "scripts" / "install-worker.sh"
PS1 = REPO / "scripts" / "install-worker.ps1"
CHECK_CMD = "src.domains.runtime.engine.engine_model"
CHECK_FLAG = "--check"


class TestInstallWorkerShModelGate:
    def _text(self):
        return SH.read_text(encoding="utf-8")

    def test_invokes_unified_engine_model_check(self):
        """sh：安装期调用统一校验入口 `python -m src.domains.runtime.engine.engine_model --check`。"""
        text = self._text()
        assert f"-m {CHECK_CMD}" in text and CHECK_FLAG in text

    def test_gate_runs_before_killing_old_worker(self):
        """sh：校验在停旧 worker（pkill 段）之前——失败不杀正在运行的旧实例。"""
        text = self._text()
        gate = text.index(f"-m {CHECK_CMD}")
        # arch11 适配：跳过函数定义区的 pkill（回滚守卫等），取 ENV_FILE 写入后的
        # 第一次运行时进程清理（第 6 步「杀旧起新」）
        first_kill = text.index("pkill", text.index('cat > "$ENV_FILE"'))
        assert gate < first_kill, "模型配置门必须在停旧 worker 之前执行"

    def test_gate_runs_after_venv_ready(self):
        """sh：校验在 venv 建好之后（依赖 $VENV/bin/python 解包内 src/）。"""
        text = self._text()
        gate = text.index(f"-m {CHECK_CMD}")
        # arch11 适配：ENV_FILE 写入块即 venv 就绪标记（源线为 Config written 日志行）
        venv_ready = text.index('cat > "$ENV_FILE"')
        assert venv_ready < gate, "模型配置门必须在 venv/src 就绪后执行"

    def test_misaligned_exits_install_with_failure(self):
        """sh：模块退出码非 0（不对齐）→ fail 结束安装（硬失败语义）。"""
        text = self._text()
        gate = text.index(f"-m {CHECK_CMD}")
        seg = text[gate:gate + 1200]
        assert "fail" in seg, "校验失败必须 fail（exit 1）结束安装"

    def test_skips_when_no_hermes_config(self):
        """sh：无 config.yaml → 跳过（降级，不阻断无 hermes 机器安装）。"""
        assert "skipping model validation" in self._text()


class TestInstallWorkerPs1ModelGate:
    def _text(self):
        return PS1.read_text(encoding="utf-8")

    def test_invokes_unified_engine_model_check(self):
        """ps1：与 sh 调同一统一入口（Linux/Windows 行为一致）。"""
        text = self._text()
        assert f"-m {CHECK_CMD}" in text and CHECK_FLAG in text

    def test_gate_runs_before_killing_old_worker(self):
        """ps1：校验在停旧 daemon/worker（Stop-Process 段）之前。"""
        text = self._text()
        gate = text.index(f"-m {CHECK_CMD}")
        first_kill = text.index("Stop-Process")
        assert gate < first_kill, "模型配置门必须在停旧 daemon/worker 之前执行"

    def test_misaligned_warns_and_continues(self):
        """ps1：退出码非 0 → 输出 Warning 并继续安装（Issue #3874 软门语义）。

        无法校验（缺 pyyaml / 不支持 sequences / 网关不可达）不等于配置错误，
        不应阻断 worker onboard；显式逃生口是 -SkipEngineCheck。
        """
        text = self._text()
        gate = text.index(f"-m {CHECK_CMD}")
        seg = text[gate:gate + 1400]
        assert "Warning: hermes model config validation FAILED" in seg, "校验失败必须输出 warning"
        assert 'Fail "hermes model config validation' not in seg, "校验失败不得再硬 Fail 中止安装"

    def test_skip_engine_check_switch_supported(self):
        """ps1：支持 -SkipEngineCheck，跳过 hermes 校验仅注册 opencode（Issue #3874）。"""
        text = self._text()
        assert "$SkipEngineCheck" in text, "缺少 -SkipEngineCheck 参数声明"
        assert "Skipping hermes model config validation" in text

    def test_skip_log_uses_powershell_switch_syntax(self):
        """ps1：跳过日志必须写 -SkipEngineCheck（bash 式 --skip-engine-check 照抄会失败）。"""
        text = self._text()
        skip_line = next(ln for ln in text.splitlines()
                         if "Skipping hermes model config validation" in ln)
        assert "-SkipEngineCheck" in skip_line, skip_line
        assert "--skip-engine-check" not in text, "日志文案不得使用 bash 式参数名"

    def test_skip_engine_check_guards_validation_block(self):
        """ps1：SkipEngineCheck 分支必须先于 config.yaml 校验块，跳过校验。"""
        text = self._text()
        skip = text.index("if ($SkipEngineCheck)")
        gate = text.index(f"-m {CHECK_CMD}")
        assert skip < gate, "SkipEngineCheck 分支必须位于校验调用之前"

    def test_skips_when_no_hermes_config(self):
        """ps1：无 config.yaml → 跳过（降级）。"""
        assert "skipping model validation" in self._text()

    def test_stderr_redirect_guarded_under_eap_stop(self):
        """ps1：校验调用必须有 EAP 防御（Issue #429：Stop 模式下原生命令 stderr 会终止脚本）。"""
        text = self._text()
        gate = text.index(f"-m {CHECK_CMD}")
        seg_before = text[max(0, gate - 1500):gate]
        assert "ErrorActionPreference" in seg_before, "校验调用前必须临时切换 EAP 防御 stderr"


class TestModelGatewayAlignmentDoc:
    """部署规范文档落库（Spec 验收：对齐要求 + 换 key/换网关同步动作 + 自检命令）。"""

    DOC = REPO / "docs" / "deployment" / "model-gateway-alignment.md"
    RUNTIMES_DOC = REPO / "help" / "usage" / "runtimes.md"

    def _text(self):
        assert self.DOC.is_file(), f"missing deployment spec doc: {self.DOC}"
        return self.DOC.read_text(encoding="utf-8")

    def test_doc_states_alignment_requirement(self):
        """/v1/models 对齐硬性约定：模型名必须在 key 所属网关可用列表。"""
        text = self._text()
        assert "/v1/models" in text
        assert "config.yaml" in text

    def test_doc_states_key_change_sync_actions(self):
        """换 key / 换网关的同步动作（必须同步换模型名并自检）。"""
        text = self._text()
        assert "换 key" in text or "更换 key" in text
        assert "自检" in text

    def test_doc_documents_windows_soft_gate_and_skip_switch(self):
        """Issue #3874 审查 Required：文档须记录 ps1 软门（不中止）与 -SkipEngineCheck。"""
        text = self._text()
        assert "-SkipEngineCheck" in text, "文档缺少 -SkipEngineCheck 说明"
        assert "non-blocking" in text, "文档缺少 ps1 校验失败不中止安装（non-blocking）语义"
        assert "失败即中止安装" not in text, "文档不得统一描述为失败即中止（与 ps1 软门冲突）"

    def test_doc_contains_selfcheck_commands(self):
        """自检命令：engine_model --check 与 curl /v1/models 两种方式。"""
        text = self._text()
        assert f"src.domains.runtime.engine.engine_model {CHECK_FLAG}" in text
        assert "curl" in text

    def test_runtimes_doc_cross_references(self):
        """部署文档在引擎使用文档交叉引用（help/usage/runtimes.md）。"""
        text = self.RUNTIMES_DOC.read_text(encoding="utf-8")
        assert "model-gateway-alignment" in text

    def test_readme_cross_references(self):
        """部署文档在 README 双语版交叉引用（QA Spec2：部署相关 README 交叉引用）。"""
        for readme in (REPO / "README.md", REPO / "README.zh.md"):
            assert readme.is_file(), f"missing {readme}"
            assert "model-gateway-alignment" in readme.read_text(encoding="utf-8"), \
                f"{readme.name} must cross-reference docs/deployment/model-gateway-alignment.md"
