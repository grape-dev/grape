#!/usr/bin/env python3
"""insert_test_detail.py 双智能体 BUG 合并入库单测。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from insert_test_detail import merge_bugs

LEADER_BUGS = [
    {"seq_no": "BUG-001", "severity": "中", "problem": "参数校验缺失", "resolved": 0},
    {"seq_no": "BUG-002", "severity": "低", "problem": "SDK 兜底崩溃", "resolved": 0},
]
S2_SAME = [
    {"seq_no": "BUG-001", "severity": "中", "problem": "参数校验缺失", "resolved": 0},
    {"seq_no": "BUG-002", "severity": "低", "problem": "SDK 兜底崩溃", "resolved": 0},
]
S2_DIFF = [
    {"seq_no": "BUG-001", "severity": "中", "problem": "参数校验缺失", "resolved": 0},
    {"seq_no": "BUG-003", "severity": "高", "problem": "新增发现的并发崩溃", "resolved": 0},
    {"seq_no": "BUG-004", "severity": "中", "problem": "错误码文案误导", "resolved": 1},
]


def test_merge_same_dedups_to_leader():
    merged = merge_bugs(LEADER_BUGS, S2_SAME)
    assert len(merged) == 2, f"相同结果应只保留队长 2 条, got {len(merged)}"
    assert merged[0]["seq_no"] == "BUG-001"
    assert merged[1]["seq_no"] == "BUG-002"


def test_merge_diff_adds_s2_only_bugs():
    merged = merge_bugs(LEADER_BUGS, S2_DIFF)
    # 队长 2 条 + skills_test2 独有的 2 条 = 4 条（BUG-001 相同去重）
    assert len(merged) == 4, f"应有 4 条, got {len(merged)}"
    seqs = [b["seq_no"] for b in merged]
    assert "BUG-001" in seqs and "BUG-002" in seqs
    assert "BUG-003" in seqs and "BUG-004" in seqs
    # 新增条目的 problem 来自 skills_test2
    b3 = [b for b in merged if b["seq_no"] == "BUG-003"][0]
    assert b3["problem"] == "新增发现的并发崩溃"
    b4 = [b for b in merged if b["seq_no"] == "BUG-004"][0]
    assert b4["resolved"] == 1


def test_merge_is_not_mutating():
    import copy
    b1 = copy.deepcopy(LEADER_BUGS)
    b2 = copy.deepcopy(S2_DIFF)
    merge_bugs(b1, b2)
    assert b1 == LEADER_BUGS and b2 == S2_DIFF


def test_merge_seq_no_continuous():
    merged = merge_bugs(LEADER_BUGS, S2_DIFF)
    seqs = sorted(int(b["seq_no"].split("-")[1]) for b in merged)
    assert seqs == [1, 2, 3, 4], f"seq 应连续 1-4, got {seqs}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n全部 {len(fns)} 个测试通过")
