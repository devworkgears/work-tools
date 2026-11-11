"""計算の結果の完全さの印・同じ計算器を複数の製品に使ったときの結果の分離・変更で部品の数が負にならない・再生の重さ。"""

import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import bomcost.scenario as scenario_module
from bomcost.group import replayed_batches

from bomcost import BomLine, Change, ChangeKind, CostCalculator, CostHead, CostRow, Scenario, Version, calculate_cost
from bomcost.api import change_problem
from bomcost.demo import demo_policy, demo_versions

M = CostHead.MATERIAL


def _version(amortization=None, direct=None) -> Version:
    v = Version("t")
    v.add_bom_lines([BomLine("P", None, "Q", 2)])
    v.add_cost(CostRow("Q", {M: 100}))
    v.add_cost(CostRow("P", {M: 200, CostHead.AMORTIZATION: amortization, CostHead.DIRECT: direct}))
    return v


class FormulaHeadsCompleteness(unittest.TestCase):
    def test_unregistered_amortization_makes_formula_heads_incomplete(self) -> None:
        v = _version(amortization=None, direct=5)
        for head in (CostHead.ADJUSTMENT, CostHead.INDIRECT, CostHead.MANUFACTURING):
            with self.subTest(head=head):
                result = calculate_cost(v, "P", head)
                self.assertIsNotNone(result.total)
                self.assertEqual(result.missing, ())
                self.assertFalse(result.complete)

    def test_unregistered_direct_makes_manufacturing_incomplete(self) -> None:
        v = _version(amortization=10, direct=None)
        self.assertFalse(calculate_cost(v, "P", CostHead.MANUFACTURING).complete)
        self.assertFalse(calculate_cost(v, "P", CostHead.DIRECT).complete)
        # 直接費に依らない費目は一部未登録にならない
        self.assertTrue(calculate_cost(v, "P", CostHead.INDIRECT).complete)

    def test_all_registered_is_complete(self) -> None:
        v = _version(amortization=10, direct=5)
        for head in (CostHead.ADJUSTMENT, CostHead.INDIRECT, CostHead.MANUFACTURING,
                     CostHead.DIRECT, CostHead.PROCESSING):
            with self.subTest(head=head):
                self.assertTrue(calculate_cost(v, "P", head).complete)
        self.assertTrue(calculate_cost(v, "P", M).complete)


class CalculatorReusedAcrossRoots(unittest.TestCase):
    def _version(self) -> Version:
        v = Version("t")
        v.add_bom_lines([
            BomLine("A", "1", "X", 1),
            BomLine("A", "1", "X", 2),  # A の下だけに数量の食い違い
            BomLine("A", "2", "NA", 1),  # A の下だけに未登録
            BomLine("B", "1", "Y", 1),
            BomLine("B", "2", "S", 1),
            BomLine("A", "3", "S", 1),  # 共有の部品（未登録）
        ])
        v.add_costs([CostRow("X", {M: 10}), CostRow("Y", {M: 20}),
                     CostRow("A", {M: None}), CostRow("B", {M: None})])
        return v

    def test_second_root_has_only_its_own_missing_and_conflicts(self) -> None:
        v = self._version()
        calc = CostCalculator(v, M)
        a = calc.calculate("A")
        b = calc.calculate("B")
        fresh = CostCalculator(v, M).calculate("B")
        self.assertIn("NA", a.missing)
        self.assertEqual(len(a.conflicts), 1)
        self.assertEqual(b.missing, fresh.missing)
        self.assertEqual(b.conflicts, ())
        self.assertNotIn("NA", b.missing)
        self.assertIn("S", b.missing)  # メモ済みの共有の部品も、B の下なので B の未登録に入る
        self.assertEqual(b.total, fresh.total)
        self.assertEqual(dict(b.item_costs), dict(fresh.item_costs))

    def test_first_root_again_matches_fresh(self) -> None:
        v = self._version()
        calc = CostCalculator(v, M)
        calc.calculate("B")
        again = calc.calculate("A")
        fresh = CostCalculator(v, M).calculate("A")
        self.assertEqual(again.missing, fresh.missing)
        self.assertEqual(again.conflicts, fresh.conflicts)
        self.assertEqual(dict(again.item_costs), dict(fresh.item_costs))


class NoNegativeUsage(unittest.TestCase):
    """見本の V-110 は A-100 ×1、A-100 は B-200 ×2（製品1台あたり B-200 は 2）。"""

    def scenario(self) -> Scenario:
        return Scenario("t", "V-110", "V-110#", demo_versions()[0], CostHead.MATERIAL, demo_policy())

    def accepted(self, s: Scenario, change: Change) -> None:
        self.assertIsNone(change_problem(s, [change]))
        s.apply(change)

    def test_removing_more_than_built_in_refused(self) -> None:
        s = self.scenario()
        self.assertRegex(change_problem(s, [Change(ChangeKind.REMOVE, "B-200", 5)]) or "", "負")
        self.assertRegex(change_problem(s, [Change(ChangeKind.REPLACE, "B-200", 5, "F-600")]) or "", "負")

    def test_removing_twice_past_zero_refused(self) -> None:
        s = self.scenario()
        self.accepted(s, Change(ChangeKind.REMOVE, "B-200", 1))
        self.accepted(s, Change(ChangeKind.REMOVE, "B-200", 1))
        self.assertRegex(change_problem(s, [Change(ChangeKind.REMOVE, "B-200", 1)]) or "", "負")

    def test_part_wide_and_place_change_of_same_part_past_zero_refused_both_orders(self) -> None:
        place = Change(ChangeKind.REPLACE, "B-200", 1, "F-600", parent="A-100", balloon="1")
        wide = Change(ChangeKind.REMOVE, "B-200", 2)
        for first, second in ((wide, place), (place, wide)):
            with self.subTest(first=first.parent):
                s = self.scenario()
                self.accepted(s, first)
                self.assertRegex(change_problem(s, [second]) or "", "負")

    def test_within_built_in_quantity_still_accepted(self) -> None:
        s = self.scenario()
        self.accepted(s, Change(ChangeKind.REMOVE, "B-200", 1))
        self.accepted(s, Change(ChangeKind.REPLACE, "B-200", 1, "F-600"))
        self.accepted(s, Change(ChangeKind.ADD, "B-200", 3))
        self.accepted(s, Change(ChangeKind.REMOVE, "B-200", 3))


class ReplayCost(unittest.TestCase):
    def scenario(self) -> Scenario:
        s = Scenario("t", "V-110", "V-110#", demo_versions()[0], CostHead.MATERIAL, demo_policy())
        at = datetime(2026, 1, 1)
        for change in (Change(ChangeKind.SET_QUANTITY, "F-600", 2, parent="V-110", balloon="2"),
                       Change(ChangeKind.SET_QUANTITY, "C-300", 1, parent="A-100", balloon="2"),
                       Change(ChangeKind.REMOVE, "B-200", 1),
                       Change(ChangeKind.ADD, "X-610", 2),
                       Change(ChangeKind.SET_HEAD, "V-110", 10, target_head="DIRECT")):
            s.apply(change, recorded_at=at)
        return s

    def test_locks_walk_the_product_once_however_long_the_history(self) -> None:
        s = self.scenario()
        real = scenario_module.usage_per_unit

        def product_walks(changes) -> int:
            walked: list[str] = []

            def counting(version, product, overrides=None):
                walked.append(product)
                return real(version, product, overrides)

            with patch.object(scenario_module, "usage_per_unit", counting):
                s.locks(changes)
            return walked.count("V-110")

        self.assertEqual(product_walks(s.changes[:1]), product_walks(s.changes))

    def test_locks_and_replay_unchanged(self) -> None:
        s = self.scenario()
        self.assertEqual(s.locks(), (
            {"F-600": "F-600", "C-300": "C-300", "B-200": "B-200"},
            {"V-110": "V-110 の #2 F-600", "A-100": "A-100 の #2 C-300"},
        ))
        replayed = [(r.sequence, r.cost_before, r.cost_after) for b in replayed_batches(s) for r in b]
        self.assertEqual(replayed, [(1, Decimal(4650), Decimal(4900)), (2, Decimal(4900), Decimal(3900)),
                                    (3, Decimal(3900), Decimal(2300)), (4, Decimal(2300), Decimal(2300)),
                                    (5, Decimal(2300), Decimal(2300))])


if __name__ == "__main__":
    unittest.main()
