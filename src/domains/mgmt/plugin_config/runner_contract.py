"""A2 Runner 凭据代理注入契约（Issue #1082 → A2 #1080）。

resolve() 产出的有效配置视图经本模块固化为 #1080 Runner 凭据代理的
注入契约。引擎侧职责到「交付只含句柄的注入计划」为止；明文换取与
env/query/header 三注入位实现属 A2 #1080（薄 Runner 凭据代理）。

契约结构（contract_version=1.0）：
- mode: "credential_proxy" —— 注入方式固定为凭据代理；
- config_view: 非凭据项有效配置（可直接下发的普通配置，无凭据材料）；
- credential_refs: {凭据项点路径: vault://cred/<ref> 引用句柄}；
- exchange: 执行时刻换取语义 —— Runner 凭据代理在插件子进程启动
  那一刻，凭句柄经统一凭据库（plugin_config/vault.py get_secret）
  换取明文并注入，注入完成后明文即刻丢弃，不落引擎任何中间态。

防御规则：意外混入视图的明文凭据项（密钥命名但非句柄）一律从契约
中剔除（不进 config_view 也不进 credential_refs），保证契约任何字段
都不携带明文。QA 审查修复：防御对**容器**同样成立 —— 密钥命名键下
的 dict/list 整体剔除（不再按叶子键名判定；修复 dict 叶子判定漏明文、
与 list 行为不对称问题）。
"""
from __future__ import annotations

from typing import Any

from src.domains.mgmt.auth.vault import is_credential_name, is_handle

CONTRACT_VERSION = "1.0"
MODE = "credential_proxy"
EXCHANGE = ("a2.runner_credential_proxy: 在插件子进程执行时刻凭 handle 调用"
            " plugin_config.vault.CredentialVault.get_secret 换取明文注入"
            "（env/query/header 三注入位与 Bearer 前缀语义见 A2 #1080），"
            "注入后明文即刻丢弃")


def build_runner_injection(view: dict) -> dict:
    """把 resolve() 有效配置视图固化为 A2 Runner 凭据代理注入契约。

    - 句柄值凭据项 → credential_refs（点路径键控），从 config_view 剔除；
    - 密钥命名的明文残留 → 防御性剔除（契约零明文）；
    - 密钥命名键下的容器（dict/list）→ 整体剔除（QA 审查修复，
      容器内明文不再按叶子键名漏进 config_view）；
    - 其余配置项原样进入 config_view。
    """
    refs: dict[str, str] = {}
    config_view = _collect(view, "", refs)
    return {
        "contract_version": CONTRACT_VERSION,
        "mode": MODE,
        "config_view": config_view,
        "credential_refs": refs,
        "exchange": EXCHANGE,
    }


def _join_path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else str(key)


def _own_key(prefix: str) -> str:
    """点路径的所属键名（去掉列表 [idx] 下标）：db.AUTH_SECRET[0] → AUTH_SECRET。"""
    if not prefix:
        return ""
    return prefix.rsplit(".", 1)[-1].split("[", 1)[0]


def _collect(node: Any, prefix: str, refs: dict) -> Any:
    """递归拆分：句柄 → refs；明文凭据 → 剔除；密钥命名键下容器 → 整体剔除。"""
    if isinstance(node, (dict, list)):
        # QA Spec#2：密钥命名键下容器整体剔除 —— 容器内明文不做叶子判定
        if is_credential_name(_own_key(prefix)):
            return _DROP
        if isinstance(node, dict):
            out: dict = {}
            for key, value in node.items():
                collected = _collect(value, _join_path(prefix, key), refs)
                if collected is _DROP:
                    continue
                out[key] = collected
            return out
        out_list = []
        for idx, item in enumerate(node):
            collected = _collect(item, f"{prefix}[{idx}]" if prefix else f"[{idx}]", refs)
            if collected is _DROP:
                continue
            out_list.append(collected)
        return out_list
    if isinstance(node, str):
        if is_handle(node):
            refs[prefix] = node
            return _DROP
        if is_credential_name(_own_key(prefix)):
            return _DROP
    return node


class _DropType:
    """标记被剔除项的哨兵（凭据项不进 config_view）。"""


_DROP = _DropType()
