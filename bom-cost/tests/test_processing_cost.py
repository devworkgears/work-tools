"""加工費を品目の原価として登録する（取込・式）。品番・数値は架空。"""

import unittest
from decimal import Decimal

import example_settings  # noqa: F401
from bomcost import BomLine, ColumnMap, CostHead, CostRow, Item, Version, VersionImport
from bomcost.formula import Head, OwnValues, breakdown, registered_own
from bomcost.importer import TABLE_FIELDS, read_table_bytes
from bomcost.rollup import calculate_cost
from bomcost.scenario import Change, ChangeKind, Scenario

P = CostHead.PROCESSING
D = Decimal


def version_with_processing() -> Version:
    """P（加工費 20・償却費 10・直接費 5）→ C ×2（材料費 100・加工費 7）。"""
    v = Version("t")
    v.add_items([Item("P", "製品", "P"), Item("C", "部品C", "B")])
    v.add_bom_lines([BomLine("P", "1", "C", 2)])
    v.add_costs([
        CostRow("P", {P: 20, CostHead.AMORTIZATION: 10, CostHead.DIRECT: 5}),
        CostRow("C", {CostHead.MATERIAL: 100, P: 7, CostHead.DIRECT: 3}),
    ])
    return v


class Formula(unittest.TestCase):
    def test_uses_own_registered_value_not_children(self) -> None:
        v = version_with_processing()
        self.assertEqual(registered_own(v, "P"), OwnValues(D(20), D(10), D(5)))
        self.assertEqual(calculate_cost(v, "P", P).total, D(20))  # 子の 2×7 は足さない
        # (200 ＋ 20 ＋ 10) × 1.1 × 1.2 ＋ 5
        self.assertEqual(calculate_cost(v, "P", CostHead.MANUFACTURING).total, D(230) * D("1.1") * D("1.2") + 5)

    def test_empty_counts_as_zero_without_mark(self) -> None:
        v = version_with_processing()
        v.add_cost(CostRow("P", {CostHead.AMORTIZATION: 10, CostHead.DIRECT: 5}))
        self.assertEqual(registered_own(v, "P").processing, D(0))
        s = Scenario("s", "P", "P#", v, CostHead.MANUFACTURING)
        self.assertEqual(s.calculate().total, D(210) * D("1.1") * D("1.2") + 5)
        b =breakdown(D(200), registered_own(v, "P"))
        self.assertEqual(b.value(Head.PROCESSING), D(0))
        self.assertEqual(b.incomplete, frozenset())

    def test_set_head_overrides_registered(self) -> None:
        s = Scenario("s", "P", "P#", version_with_processing(), CostHead.MANUFACTURING)
        self.assertEqual(s.own_values().processing, D(20))
        s.apply(Change(ChangeKind.SET_HEAD, "P", 50, target_head="PROCESSING"))
        self.assertEqual(s.own_values().processing, D(50))
        self.assertEqual(s.calculate().total, D(260) * D("1.1") * D("1.2") + 5)
        s.undo()
        self.assertEqual(s.own_values().processing, D(20))


class Import(unittest.TestCase):
    def test_csv_mapping(self) -> None:
        spec = next(f for f in TABLE_FIELDS["cost"] if f.name == "processing")
        self.assertEqual((spec.label, spec.required), ("加工費", False))
        table = read_table_bytes("cost.csv", "原価の品番,材料費,加工\nP-1,100,25\nP-2,50,\n".encode())
        version = Version("v1")
        importer = VersionImport(version)
        importer.add_table(table, ColumnMap("cost", table.headers,
                                            {"mbom_code": "原価の品番", "material": "材料費", "processing": "加工"}))
        self.assertEqual((version.cost_value("P-1", P), version.cost_value("P-2", P)), (D(25), None))
        self.assertEqual(version.cost_value("P-1", CostHead.MATERIAL), D(100))

    def test_csv_without_column_leaves_empty(self) -> None:
        table = read_table_bytes("cost.csv", "原価の品番,材料費\nP-1,100\n".encode())
        version = Version("v1")
        VersionImport(version).add_table(table, ColumnMap("cost", table.headers,
                                                          {"mbom_code": "原価の品番", "material": "材料費"}))
        self.assertIsNone(version.cost_value("P-1", P))


if __name__ == "__main__":
    unittest.main()
