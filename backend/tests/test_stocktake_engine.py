"""stocktake 对账引擎单元测试（仅依赖标准库，运行：python3 -m unittest discover -s tests）。"""

import sys
import unittest
from itertools import combinations, product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.stocktake.engine import (  # noqa: E402
    CHECKED_OUT, CONFIRMED, DISPUTED, LENT_MISSCAN, NOT_COUNTED, RETIRED, RETIRED_MISSCAN,
    UNKNOWN_MISSCAN, reconcile, validate_scans, validate_tools,
)


def tool(tool_id, status="in_custody", name=None):
    return {"tool_id": tool_id, "name": name or f"工具{tool_id}", "status": status}


def scan(scan_id, candidates, **extra):
    return {"scan_id": scan_id, "candidates": list(candidates), **extra}


def brute_force_max_matchings(adjacency, n_right):
    """枚举全部最大基数匹配，返回 (最大配对数, 出现在所有最大匹配中的边集合, 出现在任一最大匹配中的边集合)。"""
    edges = [(u, v) for u, neighbors in enumerate(adjacency) for v in neighbors]
    best = 0
    best_sets: list[frozenset] = []
    for size in range(min(len(adjacency), n_right), 0, -1):
        found = []
        for chosen in combinations(edges, size):
            left_used = {u for u, _ in chosen}
            right_used = {v for _, v in chosen}
            if len(left_used) == size and len(right_used) == size:
                found.append(frozenset(chosen))
        if found:
            best = size
            best_sets = found
            break
    if not best_sets:
        return 0, frozenset(), frozenset()
    universal = set(best_sets[0])
    for chosen in best_sets[1:]:
        universal &= set(chosen)
    somewhere = set().union(*[set(chosen) for chosen in best_sets])
    return best, frozenset(universal), frozenset(somewhere)


class ReconcileEngineTest(unittest.TestCase):
    def build_edges(self, result):
        """从引擎结果还原 (左序号->右序号) 邻接与配对角色，便于与暴力枚举对比。"""
        return result

    def cross_check(self, tools, scans):
        result = reconcile(tools, scans)
        custody = [item["tool_id"] for item in tools if item["status"] == "in_custody"]
        right_pos = {tool_id: v for v, tool_id in enumerate(custody)}
        adjacency = []
        for item in scans:
            neighbors = []
            for candidate in item["candidates"]:
                v = right_pos.get(candidate)
                if v is not None and v not in neighbors:
                    neighbors.append(v)
            adjacency.append(neighbors)
        best, universal, somewheres = brute_force_max_matchings(adjacency, len(custody))
        self.assertEqual(result["matching_size"], best)

        # 引擎标记为盘到的边，必须与暴力枚举的全匹配共用边完全一致。
        engine_essential = set()
        for item in result["confirmed"]:
            u = next(i for i, row in enumerate(scans) if row["scan_id"] == item["scan_id"])
            engine_essential.add((u, right_pos[item["tool_id"]]))
        self.assertEqual(engine_essential, set(universal))

        # 待核组里出现的边，必须恰好是「能进入某个最大匹配但非全部」的边。
        group_edges = set()
        for group in result["groups"]:
            for pair in group["optional_pairs"]:
                u = next(i for i, row in enumerate(scans) if row["scan_id"] == pair["scan_id"])
                group_edges.add((u, right_pos[pair["tool_id"]]))
        self.assertEqual(group_edges, set(somewheres) - set(universal))
        return result, custody

    def test_straightforward_one_to_one(self):
        tools = [tool("GQ-01"), tool("GQ-02"), tool("GQ-03")]
        scans = [scan("S1", ["GQ-01"]), scan("S2", ["GQ-02"])]
        result, custody = self.cross_check(tools, scans)
        self.assertEqual(result["metrics"]["confirmed"], 2)
        self.assertEqual(result["metrics"]["not_counted"], 1)
        self.assertEqual(result["groups"], [])
        outcomes = {item["scan_id"]: item["outcome"] for item in result["scans"]}
        self.assertEqual(outcomes, {"S1": CONFIRMED, "S2": CONFIRMED})
        not_counted = [item for item in result["tools"] if item["outcome"] == NOT_COUNTED]
        self.assertEqual([item["tool_id"] for item in not_counted], ["GQ-03"])
        self.assertIn("没有任何一条扫码", not_counted[0]["basis"])

    def test_alternating_cycle_becomes_one_dispute_group(self):
        # S1->{A,B}, S2->{B,A}：两条扫码互为候选，最优配法有两种且不唯一。
        tools = [tool("A"), tool("B")]
        scans = [scan("S1", ["A", "B"]), scan("S2", ["B", "A"])]
        result, _ = self.cross_check(tools, scans)
        self.assertEqual(result["matching_size"], 2)
        self.assertEqual(result["metrics"]["confirmed"], 0)
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["scan_count"], 2)
        self.assertEqual(group["tool_count"], 2)
        self.assertEqual(group["max_pairs_in_group"], 2)
        self.assertEqual(len(group["optional_pairs"]), 4)
        self.assertTrue(all(item["outcome"] == DISPUTED for item in result["scans"]))
        self.assertTrue(all(item["outcome"] == DISPUTED for item in result["tools"]))
        self.assertTrue(all(item["group_id"] == 0 for item in result["scans"]))

    def test_chain_from_free_left_is_disputed_not_confirmed(self):
        # 空闲扫码侧增广链：S1->{A,B}、S2->{A}、S3->{B}，最大配法有三种且没有共用边。
        tools = [tool("A"), tool("B")]
        scans = [scan("S1", ["A", "B"]), scan("S2", ["A"]), scan("S3", ["B"])]
        result, _ = self.cross_check(tools, scans)
        # 最优配法：{S2-A,S3-B}、{S1-A,S3-B}、{S1-B,S2-A}，无一边被全部配法共用。
        self.assertEqual(result["matching_size"], 2)
        self.assertEqual(result["metrics"]["confirmed"], 0)
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["scan_count"], 3)
        self.assertEqual(group["tool_count"], 2)
        self.assertEqual(set(group["scan_ids"]), {"S1", "S2", "S3"})
        self.assertEqual(set(group["tool_ids"]), {"A", "B"})
        self.assertTrue(all(item["outcome"] == DISPUTED for item in result["scans"]))

    def test_unusable_edge_leaves_tool_not_counted(self):
        # S1 可配 A 或 B，S2 只配 B；S2-B 为全匹配共用，S1-B 是不可采用边，B 仍判盘到。
        tools = [tool("A"), tool("B"), tool("C")]
        scans = [scan("S1", ["A", "B"]), scan("S2", ["B"])]
        result, _ = self.cross_check(tools, scans)
        self.assertEqual(result["matching_size"], 2)
        confirmed = {item["scan_id"]: item["tool_id"] for item in result["confirmed"]}
        self.assertEqual(confirmed["S2"], "B")
        self.assertEqual(confirmed["S1"], "A")
        not_counted = [item["tool_id"] for item in result["tools"] if item["outcome"] == NOT_COUNTED]
        self.assertEqual(not_counted, ["C"])
        # S1 上的 B 候选应被标注为 unusable 依据仍可追溯。
        s1 = next(item for item in result["scans"] if item["scan_id"] == "S1")
        self.assertEqual({c["candidate"] for c in s1["candidates"]}, {"A", "B"})

    def test_misscan_classification_and_basis(self):
        tools = [tool("A"), tool("B", status=CHECKED_OUT), tool("C", status=RETIRED)]
        scans = [
            scan("S1", ["A"]),
            scan("S2", ["B"]),                  # 借出误扫
            scan("S3", ["C"]),                  # 退役误扫
            scan("S4", ["X", "Y"]),             # 无效号
            scan("S5", ["C", "B"]),             # 含退役，优先退役误扫
        ]
        result = reconcile(tools, scans)
        outcomes = {item["scan_id"]: item["outcome"] for item in result["scans"]}
        self.assertEqual(outcomes["S1"], CONFIRMED)
        self.assertEqual(outcomes["S2"], LENT_MISSCAN)
        self.assertEqual(outcomes["S3"], RETIRED_MISSCAN)
        self.assertEqual(outcomes["S4"], UNKNOWN_MISSCAN)
        self.assertEqual(outcomes["S5"], RETIRED_MISSCAN)
        retired = next(item for item in result["scans"] if item["scan_id"] == "S3")
        self.assertTrue(any(c["kind"] == RETIRED for c in retired["candidates"]))
        self.assertIn("退役", retired["basis"])
        self.assertEqual(result["metrics"]["lent_tools"], 1)
        self.assertEqual(result["metrics"]["retired_tools"], 1)
        offline = {item["tool_id"]: item["status"] for item in result["offline_tools"]}
        self.assertEqual(offline, {"B": CHECKED_OUT, "C": RETIRED})

    def test_new_scan_merges_old_dispute_groups(self):
        # 初始：组1（S1,S2 ~ A,B）、组2（S3,S4 ~ C,D）各自独立。
        tools = [tool(t) for t in ("A", "B", "C", "D")]
        scans = [
            scan("S1", ["A", "B"]), scan("S2", ["B", "A"]),
            scan("S3", ["C", "D"]), scan("S4", ["D", "C"]),
        ]
        before = reconcile(tools, scans)
        self.assertEqual(len(before["groups"]), 2)
        # 补扫一条同时落在两组之间（候选 B、C），把两个旧组连成一个合并组。
        merged_scans = scans + [scan("S5", ["B", "C"])]
        after = reconcile(tools, merged_scans)
        self.assertEqual(len(after["groups"]), 1)
        self.assertEqual(after["groups"][0]["scan_count"], 5)
        self.assertEqual(after["groups"][0]["tool_count"], 4)

    def test_unchanged_groups_stable_when_unrelated_scan_added(self):
        tools = [tool(t) for t in ("A", "B", "C")]
        scans = [scan("S1", ["A", "B"]), scan("S2", ["B", "A"])]
        before = reconcile(tools, scans)
        after = reconcile(tools, scans + [scan("S3", ["C"])])
        # 原有争议组保持，新增扫码独立盘到。
        self.assertEqual(len(after["groups"]), 1)
        self.assertEqual(after["groups"][0]["scan_ids"], ["S1", "S2"])
        self.assertEqual({item["scan_id"] for item in after["confirmed"]}, {"S3"})

    def test_deterministic_recompute_idempotent(self):
        tools = [tool(t) for t in ("A", "B", "C", "D")]
        scans = [
            scan("S1", ["A", "B"]), scan("S2", ["A"]), scan("S3", ["B", "C"]),
            scan("S4", ["C", "D"]), scan("S5", ["D"]),
        ]
        first = reconcile(tools, scans)
        second = reconcile(tools, scans)
        self.assertEqual(first, second)

    def test_randomized_graphs_match_brute_force(self):
        # 随机生成小规模二部图，引擎结论必须与暴力枚举完全一致。
        import random
        rng = random.Random(20261121)
        for case in range(300):
            n_left = rng.randint(1, 5)
            n_right = rng.randint(1, 5)
            tools = [tool(f"T{v}") for v in range(n_right)]
            scans = []
            for u in range(n_left):
                candidates = [f"T{v}" for v in range(n_right) if rng.random() < 0.45]
                if not candidates:
                    candidates = [f"T{rng.randrange(n_right)}"]
                scans.append(scan(f"S{u}", candidates))
            # 允许引入借出/退役/无效号扰动。
            if case % 7 == 0:
                tools.append(tool("L1", status=CHECKED_OUT))
                scans.append(scan("LX", ["L1"]))
            if case % 11 == 0:
                scans.append(scan("UX", ["GHOST"]))
            self.cross_check(tools, scans)

    def test_validation_errors(self):
        with self.assertRaisesRegex(ValueError, "月末快照为空"):
            validate_tools([])
        with self.assertRaisesRegex(ValueError, "重复出现"):
            validate_tools([tool("A"), tool("A")])
        with self.assertRaisesRegex(ValueError, "状态须为"):
            validate_tools([tool("A", status="lost")])
        with self.assertRaisesRegex(ValueError, "至少需要一个候选"):
            validate_scans([scan("S1", [])])
        with self.assertRaisesRegex(ValueError, "扫码编号 S1 重复"):
            validate_scans([scan("S1", ["A"]), scan("S1", ["A"])])
        valid = validate_scans([scan("S1", ["A", "A", "B"])])
        self.assertEqual(valid[0].candidates, ("A", "B"))

    def test_duplicate_candidate_dedup_keeps_single_edge(self):
        tools = [tool("A")]
        scans = [scan("S1", ["A", "A", "A"])]
        result = reconcile(tools, scans)
        self.assertEqual(result["matching_size"], 1)
        self.assertEqual(result["confirmed"][0]["tool_id"], "A")


if __name__ == "__main__":
    unittest.main()
