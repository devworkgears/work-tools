"""計算の性質（Decimal・未登録・重複・風船番号・履歴・版・型費の合計・二重計上・モデルチェンジ）。"""

import unittest
from decimal import Decimal

import example_data as ex
from bomcost import (
    Allocation,
    BomLine,
    Change,
    ChangeKind,
    CostHead,
    CostRow,
    Item,
    MatchKind,
    MoldCost,
    ReuseTarget,
    Scenario,
    Scope,
    ScopePolicy,
    Source,
    Verdict,
    Version,
    Volume,
    allocate_mold_cost,
    calculate_cost,
    compare_structures,
    judge_double_counting,
    tally,
)

M = CostHead.MATERIAL


def all_decimal(values) -> bool:
    return all(v is None or isinstance(v, Decimal) for v in values)


class DecimalOnly(unittest.TestCase):
    def test_results_are_decimal(self) -> None:
        result = calculate_cost(ex.version_2026_10(), "V-110", M, ex.policy())
        self.assertIsInstance(result.total, Decimal)
        self.assertTrue(all_decimal(result.item_costs.values()))

    def test_floats_become_decimal_at_entry(self) -> None:
        line = BomLine("P", None, "Q", 0.1)
        self.assertIsInstance(line.quantity, Decimal)
        self.assertEqual(line.quantity, Decimal("0.1"))
        row = CostRow("Q", {M: 1600.0})
        self.assertIsInstance(row.value(M), Decimal)
        self.assertEqual(MoldCost("Q", 2400000.0).amount, Decimal("2400000"))
        self.assertEqual(Volume("P", 1000.0).annual, Decimal("1000"))

    def test_float_int_and_text_give_same_result(self) -> None:
        totals = []
        for price in (1600.0, "1,600", 1600, " 1600 ", Decimal("1600")):
            v = Version("t")
            v.add_bom_lines([BomLine("P", None, "Q", 2)])
            v.add_cost(CostRow("Q", {M: price}))
            totals.append(calculate_cost(v, "P", M).total)
        self.assertEqual(set(totals), {Decimal("3200")})

    def test_fractional_quantity(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine("P", None, "Q", 0.5), BomLine("P", None, "R", "2.5")])
        v.add_costs([CostRow("Q", {M: 100}), CostRow("R", {M: "0.1"})])
        self.assertEqual(calculate_cost(v, "P", M).total, Decimal("50.25"))

    def test_bool_and_garbage_rejected(self) -> None:
        with self.assertRaises(TypeError):
            BomLine("P", None, "Q", True)
        with self.assertRaises(ValueError):
            CostRow("Q", {M: "abc"})
        with self.assertRaises(ValueError):
            CostRow("Q", {M: float("nan")})


class InputVariations(unittest.TestCase):
    def test_int_and_text_codes_join(self) -> None:
        v = Version("t")
        v.add_bom_lines(
            [
                BomLine(12345, 1, 67890123, 2),
                BomLine("12345", " 2 ", " 55555555 ", "3"),
            ]
        )
        v.add_costs(
            [
                CostRow.from_mbom("67890123000", {M: "1,600"}),
                CostRow.from_mbom(55555555000, {M: 500}),
            ]
        )
        result = calculate_cost(v, " 12345 ", M)
        self.assertEqual(result.total, Decimal("4700"))
        self.assertEqual(calculate_cost(v, 12345, M).total, Decimal("4700"))
        self.assertEqual({line.child for line in v.children(12345)}, {"67890123", "55555555"})

    def test_integral_float_code_joins_int_code(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine(12345.0, 1, 67890123.0, 2)])
        v.add_costs([CostRow.from_mbom("67890123000", {M: 100})])
        self.assertEqual(calculate_cost(v, 12345, M).total, Decimal("200"))
        with self.assertRaises(TypeError):
            BomLine(12345.5, 1, 67890, 2)

    def test_duplicate_with_int_and_text_codes_folds(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine(1, 1, 2, 1), BomLine("1", "1", "2", "1"), BomLine(" 1", 1, 2, 1.0)])
        self.assertEqual(len(v.lines), 1)
        self.assertEqual(v.conflicts, ())

    def test_flags(self) -> None:
        for value, expected in ((1, True), (0, False), ("1", True), ("0", False), (True, True), (False, False)):
            with self.subTest(value=value):
                item = Item("A", "名称", "B", has_children=value)
                self.assertIs(item.has_children, expected)
        with self.assertRaises(ValueError):
            Item("A", "名称", "B", has_children=2)

    def test_blank_cost_is_unregistered(self) -> None:
        row = CostRow("Q", {M: "", CostHead.DIRECT: None, CostHead.INDIRECT: "  ", CostHead.ADJUSTMENT: 0})
        self.assertIsNone(row.value(M))
        self.assertIsNone(row.value(CostHead.DIRECT))
        self.assertIsNone(row.value(CostHead.INDIRECT))
        self.assertEqual(row.value(CostHead.ADJUSTMENT), Decimal(0))


class Unregistered(unittest.TestCase):
    def test_missing_cost_listed_not_zero(self) -> None:
        v = ex.version_2026_10()
        v.add_bom_lines([BomLine("A-100", "9", "N-900", 1)])
        result = calculate_cost(v, "A-100", M, ex.policy())
        self.assertEqual(result.total, Decimal("4900"))
        self.assertIn("N-900", result.missing)
        self.assertIsNone(result.item_costs["N-900"])
        self.assertFalse(result.complete)

    def test_parent_without_value_is_built_from_children_and_listed(self) -> None:
        result = calculate_cost(ex.version_2026_10(), "V-110", M, ex.policy())
        self.assertEqual(result.total, Decimal("4650"))
        self.assertEqual(result.missing, ("V-110",))


class Duplicates(unittest.TestCase):
    def test_reading_many_times_gives_same_result(self) -> None:
        v = ex.version_2026_10()
        once = calculate_cost(v, "V-110", M, ex.policy()).total
        for _ in range(3):
            v.add_bom_lines(ex.bom_lines())
        self.assertEqual(len(v.lines), len(ex.bom_lines()))
        self.assertEqual(calculate_cost(v, "V-110", M, ex.policy()).total, once)
        self.assertEqual(v.conflicts, ())

    def test_conflicting_quantity_is_reported(self) -> None:
        v = ex.version_2026_10()
        v.add_bom_lines([BomLine("A-100", "1", "B-200", 4)])
        v.add_bom_lines([BomLine("A-100", "1", "B-200", 4)])
        result = calculate_cost(v, "A-100", M, ex.policy())
        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual(conflict.key, ("A-100", "1", "B-200"))
        self.assertEqual(conflict.quantities, (Decimal(2), Decimal(4)))
        self.assertFalse(result.complete)


class Balloons(unittest.TestCase):
    def test_merge_without_balloons(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine("P", "1", "Q", 2), BomLine("P", "2", "Q", 3), BomLine("P", "3", "R", 1)])
        with_balloon = v.children("P", by_balloon=True)
        self.assertEqual([(l.balloon, l.child, l.quantity) for l in with_balloon],
                         [("1", "Q", 2), ("2", "Q", 3), ("3", "R", 1)])
        merged = {l.child: l.quantity for l in v.children("P", by_balloon=False)}
        self.assertEqual(merged, {"Q": Decimal(5), "R": Decimal(1)})


class History(unittest.TestCase):
    def test_records_and_clear(self) -> None:
        s = Scenario("V-110 検討", "A-100", "V-110", ex.version_2026_10(), M, ex.policy())
        base = s.calculate().total
        s.apply(Change(ChangeKind.ADD, "F-600"))
        s.apply(Change(ChangeKind.REMOVE, "C-300"))
        s.apply(Change(ChangeKind.REPLACE, "F-600", replacement="X-610"))
        self.assertEqual([r.sequence for r in s.history], [1, 2, 3])
        self.assertEqual([(r.cost_before, r.cost_after) for r in s.history],
                         [(Decimal(4900), Decimal(5150)), (Decimal(5150), Decimal(4650)),
                          (Decimal(4650), Decimal(4600))])
        for r in s.history:
            self.assertEqual((r.version_id, r.head), ("2026-10", M))
        for i in range(1, len(s.history)):
            self.assertEqual(s.history[i - 1].cost_after, s.history[i].cost_before)
        s.undo()
        self.assertEqual(s.calculate().total, Decimal(4650))
        s.clear()
        self.assertEqual(s.history, ())
        self.assertEqual(s.calculate().total, base)
        self.assertEqual(base, Decimal(4900))

    def test_set_quantity_and_replace_inside_base(self) -> None:
        s = Scenario("t", "A-100", "S-1", ex.version_2026_10(), M, ex.policy())
        s.apply(Change(ChangeKind.REPLACE, "C-300", replacement="F-600"))
        self.assertEqual(s.calculate().total, Decimal(4900 - 500 + 250))
        s.apply(Change(ChangeKind.SET_QUANTITY, "F-600", 3))
        self.assertEqual(s.calculate().total, Decimal(4900 - 500 + 750))


class Recalculation(unittest.TestCase):
    def test_history_keeps_original_version_numbers(self) -> None:
        s = Scenario("t", "A-100", "V-110", ex.version_2026_10(), M, ex.policy())
        s.apply(Change(ChangeKind.ADD, "F-600"))
        s.apply(Change(ChangeKind.REMOVE, "C-300"))
        self.assertEqual(s.calculate(ex.version_2026_12()).total, Decimal(4690))
        self.assertEqual(s.history[-1].cost_after, Decimal(4650))


class MoldSum(unittest.TestCase):
    def test_by_usage_sums_to_annual_mold_cost(self) -> None:
        v = ex.version_2026_10()
        s = Scenario("t", "A-100", "V-110", v, M, ex.policy())
        for change in (Change(ChangeKind.ADD, "F-600"), Change(ChangeKind.REMOVE, "C-300"),
                       Change(ChangeKind.REPLACE, "F-600", replacement="X-610")):
            s.apply(change)
        targets = [ex.reuse_k200()]
        mold = ex.mold_x610()
        results = {a: allocate_mold_cost(v, mold, a, targets, s.overrides()) for a in Allocation}
        by_usage = results[Allocation.BY_USAGE]
        self.assertEqual(sum(sh.annual_total for sh in by_usage.shares), mold.amount / mold.years)
        self.assertEqual(sum(sh.annual * sh.quantity_per_unit * by_usage.per_part for sh in by_usage.shares),
                         mold.annual_amount)
        self.assertEqual(sum(sh.annual_total for sh in results[Allocation.NEW_PRODUCTS_ONLY].shares),
                         mold.annual_amount)
        self.assertEqual(sum(sh.annual_total for sh in results[Allocation.NONE].shares), 0)

    def test_by_usage_sum_with_uneven_division(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine("P1", None, "N", 3), BomLine("P2", None, "N", 1)])
        v.add_volume(Volume("P1", 777))
        v.add_volume(Volume("P2", 1234, Source.MANUAL))
        v.add_volume(Volume("P3", 50))
        mold = MoldCost("N", 1000000, 3)
        a = allocate_mold_cost(v, mold, Allocation.BY_USAGE, [ReuseTarget("N", "P3", 7)])
        total = sum(sh.annual_total for sh in a.shares)
        self.assertAlmostEqual(total, mold.annual_amount, delta=Decimal("1e-15"))
        self.assertEqual(a.annual_count, Decimal(777 * 3 + 1234 + 50 * 7))

    def test_unknown_volume_and_default_years(self) -> None:
        v = Version("t")
        mold = MoldCost("N", 100)
        self.assertEqual(mold.years, Decimal(2))
        a = allocate_mold_cost(v, mold, Allocation.BY_USAGE, [ReuseTarget("N", "Z-1", 1)])
        self.assertEqual(a.unknown_volume, ("Z-1",))
        self.assertIsNone(a.per_part)


class DoubleCounting(unittest.TestCase):
    def build(self) -> Version:
        v = Version("t")
        v.add_bom_lines([BomLine("P", None, "Q", 2), BomLine("P", None, "R", 1)])
        D, I, A, MF = CostHead.DIRECT, CostHead.INDIRECT, CostHead.ADJUSTMENT, CostHead.MANUFACTURING
        v.add_costs(
            [
                # 材料費: 子の足し上げ 2×100+300=500 に対し 520 → 下位込みらしい
                # 直接費: 子 2×50+100=200 に対し 30 → 自分だけらしい
                # 間接費: R が未登録 → 判定できない
                # 調整費: 子 20 に対し 15 → どちらとも言えない
                CostRow("P", {M: 520, D: 30, I: 10, A: 15, MF: 575}),
                CostRow("Q", {M: 100, D: 50, I: 5, A: 10}),
                CostRow("R", {M: 300, D: 100, I: None, A: 0}),
            ]
        )
        return v

    def test_verdicts(self) -> None:
        judgements = {(j.item, j.head): j for j in judge_double_counting(self.build())}
        self.assertEqual(judgements[("P", M)].verdict, Verdict.INCLUDES_CHILDREN_LIKELY)
        self.assertEqual(judgements[("P", M)].compared_value, Decimal(500))
        self.assertEqual(judgements[("P", CostHead.DIRECT)].verdict, Verdict.OWN_ONLY_LIKELY)
        self.assertEqual(judgements[("P", CostHead.INDIRECT)].verdict, Verdict.UNDETERMINABLE)
        self.assertEqual(judgements[("P", CostHead.ADJUSTMENT)].verdict, Verdict.UNCLEAR)
        self.assertEqual(judgements[("P", CostHead.MANUFACTURING)].verdict, Verdict.MANUFACTURING_IS_SUM_LIKELY)

    def test_thresholds_are_arguments(self) -> None:
        strict = {(j.item, j.head): j for j in judge_double_counting(self.build(), near_tolerance="0.01")}
        self.assertEqual(strict[("P", M)].verdict, Verdict.UNCLEAR)
        loose = {(j.item, j.head): j for j in judge_double_counting(self.build(), small_ratio=Decimal("0.9"))}
        self.assertEqual(loose[("P", CostHead.ADJUSTMENT)].verdict, Verdict.OWN_ONLY_LIKELY)

    def test_example_a100_material_is_own_only(self) -> None:
        judgements = judge_double_counting(ex.version_2026_10(), items=["A-100"])
        self.assertEqual(judgements[0].verdict, Verdict.OWN_ONLY_LIKELY)
        self.assertEqual(tally(judgements)[M][Verdict.OWN_ONLY_LIKELY], 1)


class ScopeAndExclusions(unittest.TestCase):
    def test_default_scope_uses_registered_value(self) -> None:
        self.assertEqual(calculate_cost(ex.version_2026_10(), "A-100", M).total, Decimal(200))

    def test_head_level_override(self) -> None:
        p = ScopePolicy({M: Scope.OWN_ONLY})
        self.assertEqual(calculate_cost(ex.version_2026_10(), "A-100", M, p).total, Decimal(4900))

    def test_amortization_excluded(self) -> None:
        with self.assertRaises(ValueError):
            calculate_cost(ex.version_2026_10(), "A-100", CostHead.AMORTIZATION)

    def test_cycle_detected(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine("P", None, "Q", 1), BomLine("Q", None, "P", 1)])
        with self.assertRaises(ValueError):
            calculate_cost(v, "P", M, ScopePolicy({M: Scope.OWN_ONLY}))


class ModelChange(unittest.TestCase):
    def test_three_stage_matching(self) -> None:
        v = ex.version_2026_10()
        s = Scenario("t", "A-100", "V-110", v, M, ex.policy())
        for change in (Change(ChangeKind.ADD, "F-600"), Change(ChangeKind.REMOVE, "C-300"),
                       Change(ChangeKind.ADD, "X-610", 2)):
            s.apply(change)
        v.add_bom_lines([BomLine("A-100", "3", "G-700", 1)])
        rows = compare_structures(v, "A-100", v, "V-110", part_map={"G-700": "X-610"},
                                  new_overrides=s.overrides())
        by_kind = {(r.kind, r.old_part, r.new_part): r for r in rows}
        same_c = by_kind[(MatchKind.SAME, "C-300", "C-300")]
        self.assertEqual((same_c.old_quantity, same_c.new_quantity, same_c.difference),
                         (Decimal(3), Decimal(2), Decimal(-1)))
        self.assertEqual(by_kind[(MatchKind.SAME, "B-200", "B-200")].difference, 0)
        # G-700 は A-100 経由で V-110 にも入るので同じ品番で対応し、対応表は使われない
        self.assertIn((MatchKind.SAME, "G-700", "G-700"), by_kind)
        self.assertIn((MatchKind.ADDED, None, "X-610"), by_kind)
        self.assertIn((MatchKind.ADDED, None, "F-600"), by_kind)

    def test_part_map_and_removed(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine("OLD", "1", "P1", 1), BomLine("OLD", "2", "P2", 2), BomLine("OLD", "3", "P3", 1),
                         BomLine("NEW", "1", "P1", 2), BomLine("NEW", "2", "P2X", 2), BomLine("NEW", "4", "P4", 1)])
        rows = compare_structures(v, "OLD", v, "NEW", part_map={"P2": "P2X"})
        kinds = {(r.kind, r.old_part, r.new_part, r.difference) for r in rows}
        self.assertEqual(kinds, {
            (MatchKind.SAME, "P1", "P1", Decimal(1)),
            (MatchKind.MAPPED, "P2", "P2X", Decimal(0)),
            (MatchKind.REMOVED, "P3", None, Decimal(-1)),
            (MatchKind.ADDED, None, "P4", Decimal(1)),
        })


if __name__ == "__main__":
    unittest.main()
