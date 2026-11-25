"""製造原価の式（材料費小計から諸経費・材料費合計・間接費・製造原価）と、検討案の費目変更。品番・数値は架空。"""

import unittest
from decimal import Decimal

import example_settings  # noqa: F401
from bomcost import BomLine, CostHead, CostRow, Item, Version
from bomcost.formula import Head, OwnValues, breakdown
from bomcost.rollup import calculate_cost
from bomcost.scenario import Change, ChangeKind, Scenario

M = CostHead.MATERIAL
D = Decimal


def values(b) -> dict[str, Decimal | None]:
    return {h.name: b.values[h] for h in Head}


class Formula(unittest.TestCase):
    def test_each_head_by_hand(self) -> None:
        b = breakdown(D(1000), OwnValues(D(0), D(100), D(50)))
        self.assertEqual(values(b), {
            "MATERIAL": D(1000), "PROCESSING": D(0), "AMORTIZATION": D(100),
            "ADJUSTMENT": D(110),  # (1000 ＋ 0 ＋ 100) × 0.1
            "MATERIAL_TOTAL": D(1210),
            "INDIRECT": D(242),  # 1210 × 0.2
            "DIRECT": D(50),
            "MANUFACTURING": D(1502),  # 1210 ＋ 242 ＋ 50
        })
        self.assertEqual(b.incomplete, frozenset())
        # 端数は丸めない。加工費も諸経費の元に入る
        c = breakdown(D("10.01"), OwnValues(D(5), D(0), D(0)))
        self.assertEqual(c.value(Head.ADJUSTMENT), D("1.501"))
        self.assertEqual(c.value(Head.MANUFACTURING), D("16.511") * D("1.2"))
        # 表示の文字は末尾の 0 を落とすが指数表記にしない
        self.assertEqual(format(breakdown(D(1000), OwnValues(D(0), D(0), D(0))).value(Head.MATERIAL_TOTAL), "f"), "1100")

    def test_material_missing_makes_dependents_missing(self) -> None:
        b = breakdown(None, OwnValues(D(0), D(100), D(50)))
        self.assertEqual(values(b), {
            "MATERIAL": None, "PROCESSING": D(0), "AMORTIZATION": D(100), "ADJUSTMENT": None,
            "MATERIAL_TOTAL": None, "INDIRECT": None, "DIRECT": D(50), "MANUFACTURING": None,
        })

    def test_amortization_and_direct_missing_count_as_zero_with_mark(self) -> None:
        b = breakdown(D(1000), OwnValues(D(0), None, None))
        self.assertEqual((b.value(Head.AMORTIZATION), b.value(Head.DIRECT)), (None, None))
        self.assertEqual(b.value(Head.ADJUSTMENT), D(100))
        self.assertEqual(b.value(Head.MANUFACTURING), D(1320))  # 1100 × 1.2
        self.assertEqual({h.name for h in b.incomplete},
                         {"AMORTIZATION", "ADJUSTMENT", "MATERIAL_TOTAL", "INDIRECT", "DIRECT", "MANUFACTURING"})
        only_direct = breakdown(D(1000), OwnValues(D(0), D(0), None))
        self.assertEqual({h.name for h in only_direct.incomplete}, {"DIRECT", "MANUFACTURING"})
        # 償却費を無視すると 0 として通し、印を付けない（欄は空）
        ignored = breakdown(D(1000), OwnValues(D(0), None, D(0)), ignore_amortization=True)
        self.assertEqual((ignored.value(Head.AMORTIZATION), ignored.value(Head.MANUFACTURING)), (None, D(1320)))
        self.assertEqual(ignored.incomplete, frozenset())
        ignored_registered = breakdown(D(1000), OwnValues(D(0), D(500), D(0)), ignore_amortization=True)
        self.assertEqual(ignored_registered.value(Head.ADJUSTMENT), D(100))
        # 材料費小計が一部未登録なら、それに依る費目も一部未登録
        partial = breakdown(D(1000), OwnValues(D(0), D(0), D(0)), material_incomplete=True)
        self.assertEqual({h.name for h in partial.incomplete},
                         {"MATERIAL", "ADJUSTMENT", "MATERIAL_TOTAL", "INDIRECT", "MANUFACTURING"})


def small_version() -> Version:
    """P（材料費は未登録で子を足す・償却費 10・直接費 5）→ C ×2。C・D は部品の製造原価も持つ（計算には使わない）。"""
    v = Version("t")
    v.add_items([Item("P", "製品", "P"), Item("C", "部品C", "B"), Item("D", "部品D", "B")])
    v.add_bom_lines([BomLine("P", "1", "C", 2)])
    v.add_costs([
        CostRow("P", {CostHead.AMORTIZATION: 10, CostHead.DIRECT: 5, CostHead.MANUFACTURING: 99999,
                      CostHead.INDIRECT: 7777, CostHead.ADJUSTMENT: 6666}),
        CostRow("C", {M: 100, CostHead.DIRECT: 30, CostHead.MANUFACTURING: 999}),
        CostRow("D", {M: 150, CostHead.DIRECT: 40, CostHead.MANUFACTURING: 5000}),
    ])
    return v


class RollupOnlyMaterial(unittest.TestCase):
    def test_parts_manufacturing_is_not_added_to_product(self) -> None:
        v = small_version()
        result = calculate_cost(v, "P", CostHead.MANUFACTURING)
        # 材料費小計 200（2×100）・償却費 10 → 材料費合計 231・間接費 46.2・直接費 5
        self.assertEqual(result.total, D("282.2"))
        self.assertEqual(calculate_cost(v, "P", CostHead.DIRECT).total, D(5))  # 子の 2×30 を足さない
        self.assertEqual(calculate_cost(v, "P", CostHead.INDIRECT).total, D("46.2"))
        # 部品にも同じ式を当てる（その値は親の計算に使わない）: 100 × 1.1 × 1.2 ＋ 30
        self.assertEqual(result.item_costs["C"], D(162))

    def test_swapping_parts_moves_only_material(self) -> None:
        v = small_version()
        s = Scenario("s", "P", "P#", v)
        before = {h: s.calculate(head=h).total for h in (M, CostHead.DIRECT, CostHead.MANUFACTURING)}
        s.apply(Change(ChangeKind.REPLACE, "C", 2, "D"))
        s.apply(Change(ChangeKind.ADD, "D", 1))
        after = {h: s.calculate(head=h).total for h in (M, CostHead.DIRECT, CostHead.MANUFACTURING)}
        self.assertEqual(after[M] - before[M], D(250))  # 2×(150−100) ＋ 150
        self.assertEqual(after[CostHead.DIRECT], before[CostHead.DIRECT])
        self.assertEqual(after[CostHead.MANUFACTURING] - before[CostHead.MANUFACTURING], D(250) * D("1.1") * D("1.2"))

    def test_set_head_changes_product_values(self) -> None:
        v = small_version()
        s = Scenario("s", "P", "P#", v, CostHead.MANUFACTURING)
        lines = s.root_lines()
        record = s.apply(Change(ChangeKind.SET_HEAD, "P", 80, target_head="DIRECT"))
        self.assertEqual(record.difference, D(75))  # 直接費 5 → 80
        s.apply(Change(ChangeKind.SET_HEAD, "P", 30, target_head="PROCESSING"))
        s.apply(Change(ChangeKind.SET_HEAD, "P", 0, target_head="AMORTIZATION"))
        self.assertEqual(s.root_lines(), lines)  # 構成は変えない
        self.assertEqual(s.own_values(), OwnValues(D(30), D(0), D(80)))
        # (200 ＋ 30 ＋ 0) × 1.1 × 1.2 ＋ 80
        self.assertEqual(s.calculate().total, D(230) * D("1.1") * D("1.2") + 80)
        self.assertEqual(s.calculate(head=M).total, D(200))
        s.undo()
        self.assertEqual(s.own_values().amortization, D(10))

    def test_set_head_checks(self) -> None:
        for bad in (dict(kind=ChangeKind.SET_HEAD, part="P", quantity=1),
                    dict(kind=ChangeKind.SET_HEAD, part="P", quantity=1, target_head="INDIRECT"),
                    dict(kind=ChangeKind.SET_HEAD, part="P", quantity=-1, target_head="DIRECT"),
                    dict(kind=ChangeKind.ADD, part="C", quantity=1, target_head="DIRECT")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Change(**bad)


if __name__ == "__main__":
    unittest.main()
