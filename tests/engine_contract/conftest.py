"""B7 引擎接入契约测试套（engine-side conformance，P0b，2026-11-13 spec）。

「新 agent 接入完成 = descriptor + 过本套」，替代人肉 E2E。六段：
  ①注册表可发现 + schema 校验（L0：纯文件，CI 恒跑）
  ②探活 ACP 握手（L2 真机段，engine_real）
  ③会话全生命周期（L1 mock 段：协议状态机；L2 真机段：真 CLI）
  ④杀进程链无孤儿（L2 真机段）
  ⑤假完成守卫 stopReason 主判 + PREMATURE_END 兜底（L1 断言逻辑 + L2）
  ⑥max_concurrent 治理（L0 字段存在性；执行面生效属 P1 AccessService 契约，
    本期不做执行面断言——诚实划线，见 spec D-5）

运行分层（spec B7 fixture 策略）：
- L0/L1 无环境要求，CI 恒跑、恒不许 skip；
- L2 `pytest -m engine_real`：fixture 先探 descriptor.probe.command 的 CLI，
  不存在 → 显式 pytest.skip("engine not installed: <cli>")，job 末尾汇总，
  不恒红不静默空转。接入验收（A1）= L0+L1 绿 + L2 至少一台真机绿。

用法：
    pytest tests/engine_contract/ -q                          # L0+L1
    pytest tests/engine_contract/ -q -m engine_real            # L2（需真机引擎）
    pytest tests/engine_contract/ -q --engine=hermes           # 只跑单引擎
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.domains.runtime.engine.registry  # noqa: E402,F401 先触发（绕开循环导入）


def _all_acp_engines():
    """套件对象集：全部已注册引擎的 descriptor（参数化跑任意新接入引擎）。"""
    from src.domains.runtime.engine.descriptor import get_registry
    return sorted(get_registry().all().keys())


def pytest_addoption(parser):
    parser.addoption("--engine", action="store", default=None,
                     help="只跑指定 canonical 引擎的契约用例")


def pytest_generate_tests(metafunc):
    if "engine" in metafunc.fixturenames:
        engines = _all_acp_engines()
        only = metafunc.config.getoption("--engine")
        if only:
            engines = [only]
        metafunc.parametrize("engine", engines)


@pytest.fixture()  # function 级：engine 是参数化 fixture，session 级会 ScopeMismatch
def skip_engine_uninstalled(engine):
    """L2 真机段 fixture：CLI 缺失 → 显式 skip（附引擎名，可汇总）。

    用法：测试函数同时声明 engine + skip_engine_uninstalled。
    """
    from src.domains.runtime.engine.descriptor import get_registry
    d = get_registry().get(engine)
    if d is None:
        pytest.fail(f"unknown engine: {engine}")
    if d["protocol"] == "ghost":
        pytest.skip(f"ghost engine (known-probe only): {engine}")
    cli = d["discover"]["cli"]
    from src.domains.runtime.engine.os_adapter import find_cli
    if not find_cli(cli):
        pytest.skip(f"engine not installed: {cli} ({engine})")
    return d
